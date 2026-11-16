package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
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
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clientfeatures "k8s.io/client-go/features"
	clientfeaturestesting "k8s.io/client-go/features/testing"
	"k8s.io/client-go/kubernetes/fake"

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

	// Nobody answers: no verdict, so the dispatcher holds the submission
	// rather than rejecting it. It ends within the caller's ctx.
	nctx, ncancel := context.WithTimeout(ctx, 2*time.Second)
	err = v.CheckProfileRun(nctx, ref, "tse", "read", "namespace/ka", nil)
	ncancel()
	if !errors.Is(err, dispatch.ErrVerifierUnreachable) {
		t.Fatalf("with no verifier, got %v, want ErrVerifierUnreachable", err)
	}
	var r *capability.Refusal

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
		resp := capability.Response{Allowed: req.Ref.Key == "cap-ok"}
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
	if err := v.CheckProfileRun(ctx, ref, "tse", "read", "namespace/ka", nil); err != nil {
		t.Fatalf("permitted ref: %v", err)
	}
	if req := asked.Load(); req == nil || req.Verb != capability.VerbProfileRun || req.Profile != "tse" ||
		req.Tier != "read" || req.Resource != "namespace/ka" || req.Ref != ref {
		t.Errorf("the verifier was asked %+v, want profile.run for tse at read on namespace/ka", req)
	}
	err = v.CheckProfileRun(ctx, capability.Ref{Key: "cap-bad", Revision: 1}, "tse", "read", "namespace/ka", nil)
	if !errors.As(err, &r) || capability.Reason(err) != "not this one" || errors.Is(err, dispatch.ErrVerifierUnreachable) {
		t.Errorf("refused ref: got %v, want the verifier's refusal", err)
	}
	current.Store(nil)
	if err := v.CheckProfileRun(ctx, ref, "tse", "read", "namespace/ka", nil); !errors.Is(err, dispatch.ErrVerifierUnreachable) {
		t.Errorf("with no connection, got %v, want ErrVerifierUnreachable", err)
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
	// An authorization violation is retried: the bus gives the same text
	// for a bad token as for an auth callout that is down or not yet
	// subscribed, and the callout's outage passes.
	for _, refusal := range []error{nats.ErrAuthorization, errors.New("nats connect: nats: Authorization Violation")} {
		calls = 0
		got, err = connectWithRetry(context.Background(), log, time.Millisecond, func(context.Context) (int, error) {
			calls++
			if calls < 3 {
				return 0, refusal
			}
			return 7, nil
		})
		if err != nil || got != 7 || calls != 3 {
			t.Fatalf("%v: got %d, %v after %d calls; want 7 after 3", refusal, got, err, calls)
		}
	}
	// An untrusted certificate is not: no retry fixes it.
	for _, untrusted := range []error{x509.UnknownAuthorityError{}, x509.HostnameError{Host: "nats"}, &tls.CertificateVerificationError{Err: errors.New("bad")}} {
		calls = 0
		ctx, cancel := context.WithCancel(context.Background())
		_, err = connectWithRetry(ctx, log, time.Millisecond, func(context.Context) (int, error) {
			calls++
			if calls == 3 {
				cancel() // retried: stop it rather than loop forever
			}
			return 0, fmt.Errorf("nats connect: %w", untrusted)
		})
		cancel()
		if err == nil || calls != 1 {
			t.Fatalf("an untrusted certificate (%T): %v after %d calls; want an error after 1", untrusted, err, calls)
		}
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

// signalWriter collects a log and closes seen the first time it holds want.
type signalWriter struct {
	mu   sync.Mutex
	buf  strings.Builder
	want string
	seen chan struct{}
	once sync.Once
}

func (w *signalWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.buf.Write(p)
	if strings.Contains(w.buf.String(), w.want) {
		w.once.Do(func() { close(w.seen) })
	}
	return len(p), nil
}

// A SIGTERM while the process still waits for its first config, or for its
// first bus connect, is a stop like one while dispatching: serve returns nil
// (run logs "stopped" at Info and exits 0), not context.Canceled.
func TestAStopBeforeDispatchingIsACleanStop(t *testing.T) {
	clientfeaturestesting.SetFeatureDuringTest(t, clientfeatures.WatchListClient, false)
	const ns, name = "kube-agents", "agent-a2a-dispatch"
	// Nothing listens on port 1: every connect fails and is retried.
	s := settings{natsURL: "nats://127.0.0.1:1", namespace: ns, configMap: name}
	cfg := lib.DispatchConfig{Version: "test", Namespace: ns, ProfileNamespace: "kube-agents-profiles", Ceiling: 6,
		InteractiveReserve: 2, ChildReserve: 2, LeaseSeconds: 3, RenewSeconds: 1, RestartGraceSeconds: 2, AcceptTimeoutSeconds: 1,
		Profiles: []lib.DispatchProfile{{Name: "tse", Tiers: []string{"read"}, Concurrency: 1, QueueTimeoutSeconds: 60, ActiveDeadlineSeconds: 60,
			Durables: []lib.DispatchDurable{
				{Tier: "read", Kind: lib.SubmitKindTop, Name: lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)},
				{Tier: "read", Kind: lib.SubmitKindChild, Name: lib.ProfileDurableName("tse", "read", lib.SubmitKindChild)}}}}}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: ns, Name: name}, Data: map[string]string{lib.DispatchConfigKey: string(raw)}}
	for _, tc := range []struct {
		name string
		kube *fake.Clientset
		// stopAt is the log line that says serve is in the wait the stop
		// lands in; the config wait logs nothing.
		stopAt string
	}{
		{"during the config wait", fake.NewSimpleClientset(), ""},
		{"during the first connect", fake.NewSimpleClientset(cm), "the bus is not reachable yet"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := &signalWriter{want: tc.stopAt, seen: make(chan struct{})}
			log := slog.New(slog.NewTextHandler(w, &slog.HandlerOptions{Level: slog.LevelDebug}))
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { done <- serve(ctx, log, s, tc.kube) }()
			if tc.stopAt == "" {
				time.Sleep(300 * time.Millisecond)
			} else {
				select {
				case <-w.seen:
				case <-time.After(10 * time.Second):
					t.Fatalf("serve never reached the connect retry; log:\n%s", w.buf.String())
				}
			}
			cancel()
			select {
			case err := <-done:
				if err != nil {
					t.Errorf("serve returned %v on a stop, want nil (a clean stop)", err)
				}
			case <-time.After(10 * time.Second):
				t.Fatal("serve did not return after the stop")
			}
		})
	}
	// A moved profile namespace cancels the same context, and is still the
	// error that restarts the process.
	ctx, cancel := context.WithCancelCause(context.Background())
	cancel(errProfileNamespaceMoved)
	if err := stopped(ctx, fmt.Errorf("bus connect: %w", ctx.Err())); !errors.Is(err, errProfileNamespaceMoved) {
		t.Errorf("a moved namespace during the connect: %v, want errProfileNamespaceMoved", err)
	}
}
