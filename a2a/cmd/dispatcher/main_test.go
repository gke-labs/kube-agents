package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/dispatch"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

func env(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func TestReadSettingsNeedsTheBusTheNamespaceAndTheConfigMap(t *testing.T) {
	full := map[string]string{"NATS_URL": "nats://x", envNamespace: "ka", envConfigMap: "agent-a2a-dispatch",
		"NATS_USER": "dispatcher", "NATS_PASSWORD": "pw"}
	if _, err := readSettings(env(full)); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"NATS_URL", envNamespace, envConfigMap} {
		m := map[string]string{}
		for kk, v := range full {
			m[kk] = v
		}
		delete(m, k)
		if _, err := readSettings(env(m)); err == nil {
			t.Errorf("accepted without %s", k)
		}
	}
	// The deployment's path: a projected token that must exist.
	noUser := map[string]string{"NATS_URL": "nats://x", envNamespace: "ka", envConfigMap: "m",
		lib.EnvBusTokenFile: filepath.Join(t.TempDir(), "missing")}
	if _, err := readSettings(env(noUser)); err == nil {
		t.Error("accepted a missing bus token")
	}
	tok := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tok, []byte("t"), 0o600); err != nil {
		t.Fatal(err)
	}
	noUser[lib.EnvBusTokenFile] = tok
	if _, err := readSettings(env(noUser)); err != nil {
		t.Errorf("with a token: %v", err)
	}
}

// The binary's verifier is capability.Client's profile.run, asked as the
// dispatcher on the bus client's current connection: the verifier's verdict
// is the answer either way, a connection the client replaced is not used,
// and with nobody answering the check is a refusal, never a permit.
func TestTheVerifierAsksProfileRunOnTheCurrentConnection(t *testing.T) {
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	defer srv.Shutdown()
	dial := func() *nats.Conn {
		nc, err := nats.Connect(srv.ClientURL())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(nc.Close)
		return nc
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	ref := capability.Ref{Key: "cap-ok", Revision: 1}
	var current atomic.Pointer[nats.Conn]
	current.Store(dial())
	v := busVerifier{conn: current.Load}

	// Nobody answers: a refusal, within the caller's ctx.
	nctx, ncancel := context.WithTimeout(ctx, 2*time.Second)
	err = v.CheckProfileRun(nctx, ref, "tse", "read", "namespace/ka")
	ncancel()
	var r *capability.Refusal
	if !errors.As(err, &r) {
		t.Fatalf("with no verifier, got %v, want a refusal", err)
	}

	verifier := dial()
	var asked atomic.Pointer[capability.Request]
	verify, err := capability.VerifySubject(natsUser)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := verifier.Subscribe(verify, func(m *nats.Msg) {
		var req capability.Request
		_ = json.Unmarshal(m.Data, &req)
		asked.Store(&req)
		resp := capability.Response{Allowed: req.Ref.Key == "cap-ok", RequestID: req.RequestID}
		if !resp.Allowed {
			resp.Reason = "not this one"
		}
		b, _ := json.Marshal(resp)
		_ = verifier.Publish(m.Reply, b)
	}); err != nil {
		t.Fatal(err)
	}
	if err := verifier.Flush(); err != nil {
		t.Fatal(err)
	}
	// The client replaced its connection: the old one is closed.
	current.Load().Close()
	current.Store(dial())
	if err := v.CheckProfileRun(ctx, ref, "tse", "read", "namespace/ka"); err != nil {
		t.Fatalf("permitted ref: %v", err)
	}
	if req := asked.Load(); req == nil || req.Verb != capability.VerbProfileRun || req.Profile != "tse" ||
		req.Tier != "read" || req.Resource != "namespace/ka" || req.Ref != ref {
		t.Errorf("the verifier was asked %+v, want profile.run for tse at read on namespace/ka", req)
	}
	err = v.CheckProfileRun(ctx, capability.Ref{Key: "cap-bad", Revision: 1}, "tse", "read", "namespace/ka")
	if !errors.As(err, &r) || capability.Reason(err) != "not this one" {
		t.Errorf("refused ref: got %v, want the verifier's refusal", err)
	}
	current.Store(nil)
	if err := v.CheckProfileRun(ctx, ref, "tse", "read", "namespace/ka"); !errors.As(err, &r) {
		t.Errorf("with no connection, got %v, want a refusal", err)
	}
}

func TestAMovedProfileNamespaceEndsTheProcess(t *testing.T) {
	src := dispatch.NewStaticSource(lib.DispatchConfig{ProfileNamespace: "a"})
	ctx, cancel := context.WithCancelCause(context.Background())
	defer cancel(nil)
	go watchProfileNamespace(ctx, cancel, src, "a")
	src.Set(lib.DispatchConfig{ProfileNamespace: "b"})
	select {
	case <-ctx.Done():
		if !errors.Is(context.Cause(ctx), errProfileNamespaceMoved) {
			t.Errorf("cause %v", context.Cause(ctx))
		}
	case <-time.After(namespaceCheckInterval + 5*time.Second):
		t.Fatal("a moved profile namespace did not end the process")
	}
}

// The environment and the probe port are the ones the operator's
// Deployment renders (buildA2ADispatcherDeployment): a renamed variable
// here is a pod that exits at start.
func TestTheSettingsAreTheDeploymentsNames(t *testing.T) {
	if envNamespace != "POD_NAMESPACE" || envConfigMap != "DISPATCH_CONFIGMAP" {
		t.Errorf("env names %q %q", envNamespace, envConfigMap)
	}
	if lib.EnvNATSCAFile != "NATS_CA_FILE" {
		t.Errorf("CA env %q", lib.EnvNATSCAFile)
	}
	if lib.BusTokenPath != "/var/run/secrets/a2a-bus/token" {
		t.Errorf("bus token path %q", lib.BusTokenPath)
	}
	if statusAddr != ":8080" {
		t.Errorf("status address %q", statusAddr)
	}
	if natsUser != "dispatcher" {
		t.Errorf("bus user %q", natsUser)
	}
}

func probe(t *testing.T, p *probes, path string) (int, string) {
	t.Helper()
	rec := httptest.NewRecorder()
	p.mux().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	return rec.Code, rec.Body.String()
}

// /readyz needs the config, the bus and a dispatcher with every durable
// bound, and says which is missing; /healthz answers throughout.
func TestReadyzNeedsConfigBusAndBoundDurables(t *testing.T) {
	var loaded, connected atomic.Bool
	bound, why := false, "durables not bound yet: profile-tse-read-top"
	var mu sync.Mutex
	p := &probes{configLoaded: loaded.Load}
	check := func(wantCode int, wantBody string) {
		t.Helper()
		code, body := probe(t, p, "/readyz")
		if code != wantCode || !strings.Contains(body, wantBody) {
			t.Errorf("/readyz = %d %q, want %d containing %q", code, body, wantCode, wantBody)
		}
		if code, _ := probe(t, p, "/healthz"); code != http.StatusOK {
			t.Errorf("/healthz = %d", code)
		}
	}
	check(http.StatusServiceUnavailable, "config")
	loaded.Store(true)
	check(http.StatusServiceUnavailable, "bus")
	p.setBus(connected.Load)
	check(http.StatusServiceUnavailable, "bus")
	connected.Store(true)
	check(http.StatusServiceUnavailable, "not running")
	p.setDispatcher(func() (bool, string) { mu.Lock(); defer mu.Unlock(); return bound, why })
	check(http.StatusServiceUnavailable, "profile-tse-read-top")
	mu.Lock()
	bound, why = true, ""
	mu.Unlock()
	check(http.StatusOK, "ok")
	connected.Store(false)
	check(http.StatusServiceUnavailable, "bus")
}

// The first bus connect is retried while the bus is unreachable (it is often
// rolling in the same operator pass), and stops at once on a refused
// credential, which no retry fixes.
func TestTheFirstBusConnectIsRetried(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	calls := 0
	got, err := connectWithRetry(context.Background(), log, time.Millisecond, func(context.Context) (int, error) {
		calls++
		if calls < 3 {
			return 0, nats.ErrNoServers
		}
		return 7, nil
	})
	if err != nil || got != 7 || calls != 3 {
		t.Fatalf("got %d, %v after %d calls; want 7 after 3", got, err, calls)
	}
	calls = 0
	_, err = connectWithRetry(context.Background(), log, time.Millisecond, func(context.Context) (int, error) {
		calls++
		return 0, nats.ErrAuthorization
	})
	if err == nil || calls != 1 {
		t.Fatalf("a refused credential: %v after %d calls; want an error after 1", err, calls)
	}
}

// writeSeed writes seed to a file in a fresh temp dir and returns its path.
func writeSeed(t *testing.T, seed []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "seed")
	if err := os.WriteFile(path, seed, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// bufferLog is a logger writing JSON lines into the returned buffer.
func bufferLog() (*slog.Logger, *strings.Builder) {
	var b strings.Builder
	return slog.New(slog.NewJSONHandler(&b, nil)), &b
}

// The task-signing key (#3889). Unset, there is no key and a warning says so
// once: confirms carry no task credentials. A file holding a task-signing
// seed loads it, with no warning, and the seed is never logged. A file that
// can't be read, or holds a seed of another key type or no seed at all, or
// an unusable lifetime cap, is an error: the process refuses to start.
func TestTheTaskSigningKeyIsLoadedFromItsSeedFile(t *testing.T) {
	log, out := bufferLog()
	kp, lifetime, err := taskSigning(env(map[string]string{}), log)
	if err != nil || kp != nil || lifetime != 0 {
		t.Fatalf("unset: key %v, lifetime %v, err %v; want no key, the default and no error", kp, lifetime, err)
	}
	if n := strings.Count(out.String(), `"level":"WARN"`); n != 1 || !strings.Contains(out.String(), "running without a task-signing key") ||
		!strings.Contains(out.String(), "confirms will carry no task credentials") || !strings.Contains(out.String(), "profile-wide bus reach") {
		t.Errorf("unset: want one warning that confirms carry no task credentials, logged %s", out)
	}

	signing, err := nkeys.CreateCluster()
	if err != nil {
		t.Fatal(err)
	}
	seed, _ := signing.Seed()
	pub, _ := signing.PublicKey()
	log, out = bufferLog()
	kp, lifetime, err = taskSigning(env(map[string]string{envTaskSigningSeedFile: writeSeed(t, append(seed, '\n')), envTaskCredentialMaxLifetime: "2h"}), log)
	if err != nil || kp == nil || lifetime != 2*time.Hour {
		t.Fatalf("a valid seed: key %v, lifetime %v, err %v", kp, lifetime, err)
	}
	if got, _ := kp.PublicKey(); got != pub {
		t.Errorf("loaded key %s, want %s", got, pub)
	}
	if strings.Contains(out.String(), `"level":"WARN"`) || strings.Contains(out.String(), "running without") {
		t.Errorf("a loaded key logged the no-key warning: %s", out)
	}
	if strings.Contains(out.String(), string(seed)) {
		t.Error("the seed was logged")
	}

	account, _ := nkeys.CreateAccount()
	accountSeed, _ := account.Seed()
	user, _ := nkeys.CreateUser()
	userSeed, _ := user.Seed()
	for name, e := range map[string]map[string]string{
		"a missing file":       {envTaskSigningSeedFile: filepath.Join(t.TempDir(), "missing")},
		"a directory":          {envTaskSigningSeedFile: t.TempDir()},
		"an empty file":        {envTaskSigningSeedFile: writeSeed(t, nil)},
		"not a seed":           {envTaskSigningSeedFile: writeSeed(t, []byte("not a seed"))},
		"an account seed":      {envTaskSigningSeedFile: writeSeed(t, accountSeed)},
		"a user seed":          {envTaskSigningSeedFile: writeSeed(t, userSeed)},
		"a bad lifetime":       {envTaskSigningSeedFile: writeSeed(t, seed), envTaskCredentialMaxLifetime: "forever"},
		"a bad lifetime alone": {envTaskCredentialMaxLifetime: "-1h"},
	} {
		log, out := bufferLog()
		if kp, _, err := taskSigning(env(e), log); err == nil || kp != nil {
			t.Errorf("%s: key %v, err %v; want a refusal", name, kp, err)
		} else if strings.Contains(err.Error(), string(seed)) || strings.Contains(err.Error(), string(accountSeed)) || strings.Contains(err.Error(), string(userSeed)) {
			t.Errorf("%s: the error names the seed", name)
		}
		if strings.Contains(out.String(), string(seed)) {
			t.Errorf("%s: the seed was logged", name)
		}
	}
}

// A seed file that is set but unusable stops the process at startup, with
// the config exit code, before it reaches the cluster or the bus: it never
// runs minting nothing.
func TestABadSeedFileStopsTheDispatcherAtStartup(t *testing.T) {
	account, _ := nkeys.CreateAccount()
	accountSeed, _ := account.Seed()
	base := map[string]string{"NATS_URL": "nats://x", envNamespace: "ka", envConfigMap: "agent-a2a-dispatch",
		"NATS_USER": "dispatcher", "NATS_PASSWORD": "pw"}
	for name, path := range map[string]string{
		"missing":           filepath.Join(t.TempDir(), "missing"),
		"wrong key type":    writeSeed(t, accountSeed),
		"not a seed at all": writeSeed(t, []byte("junk")),
	} {
		e := map[string]string{envTaskSigningSeedFile: path}
		for k, v := range base {
			e[k] = v
		}
		if code := run(env(e)); code != 2 {
			t.Errorf("%s: run exited %d, want 2 (refused at startup)", name, code)
		}
	}
}
