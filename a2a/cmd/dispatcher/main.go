// The profile dispatcher (#3655): the only fetcher and holder of profile
// submissions. The work is in a2a/dispatch; this program watches its config
// and its worker pools through the API, connects as the `dispatcher` bus
// identity and runs it. PR 2 of #3655 renders its Deployment, identity,
// grants, Role and the dispatch ConfigMap.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/dispatch"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// natsUser is the principal this program authenticates as and its inbox
// owner: the contract's static identity-map entry, whose grants include
// _INBOX.dispatcher.>.
const natsUser = "dispatcher"

const (
	// envNamespace is the dispatcher's own namespace, where its ConfigMap is.
	envNamespace = "POD_NAMESPACE"
	// envConfigMap names the dispatch ConfigMap (<agent>-a2a-dispatch) the
	// operator renders.
	envConfigMap = "DISPATCH_CONFIGMAP"

	// configWait bounds the wait for the first config at startup: past it
	// the process exits and the kubelet's restart says so.
	configWait = 2 * time.Minute
	// namespaceCheckInterval paces the check that the profile namespace the
	// pool watches is still the config's.
	namespaceCheckInterval = 10 * time.Second

	// statusAddr serves /readyz and /healthz: the container port the
	// Deployment names "status" and probes.
	statusAddr    = ":8080"
	statusTimeout = 5 * time.Second

	reconnectWait   = 2 * time.Second
	reconnectJitter = 500 * time.Millisecond
	clientName      = "a2a-dispatcher"
)

// busVerifier is the dispatcher's verifier: capability.Client's
// CheckProfileRun (profile.run, #3734), asked as natsUser on the bus
// client's current connection. A capability.Client is bound to one
// connection, and the dispatcher replaces its connection when its grants
// change (lib.Client.Reconnect), so one is built per check: it is only the
// connection and a subject, and the check's reply subscription ends with
// the check. Any error, an unreachable verifier included, is a refusal.
type busVerifier struct {
	conn func() *nats.Conn
	log  *slog.Logger
}

func (v busVerifier) CheckProfileRun(ctx context.Context, ref capability.Ref, profile string, tier capability.Tier, target capability.Scope) error {
	nc := v.conn()
	if nc == nil {
		return &capability.Refusal{Rule: "the dispatcher is not connected to the bus"}
	}
	c, err := capability.NewClient(nc, natsUser)
	if err != nil {
		return err
	}
	c.Log = v.log
	return c.CheckProfileRun(ctx, ref, profile, tier, target)
}

// errProfileNamespaceMoved ends the process when the config's profile
// namespace changes under the pool's watch, so the restart re-watches it.
var errProfileNamespaceMoved = errors.New("the profile namespace changed; restarting to watch the new one")

func main() { os.Exit(run(os.Getenv)) }

func run(getenv func(string) string) int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)
	settings, err := readSettings(getenv)
	if err != nil {
		log.Error("config", "err", err)
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	restCfg, err := rest.InClusterConfig()
	if err != nil {
		log.Error("in-cluster config", "err", err)
		return 1
	}
	kube, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		log.Error("kubernetes client", "err", err)
		return 1
	}
	if err := serve(ctx, log, settings, kube); err != nil {
		log.Error("dispatcher stopped", "err", err)
		return 1
	}
	log.Info("stopped")
	return 0
}

// settings is what the environment gives the process.
type settings struct {
	natsURL, namespace, configMap string
	auth                          []nats.Option
}

func readSettings(getenv func(string) string) (settings, error) {
	s := settings{natsURL: getenv("NATS_URL"), namespace: getenv(envNamespace), configMap: getenv(envConfigMap)}
	switch {
	case s.natsURL == "":
		return s, errors.New("NATS_URL is required")
	case s.namespace == "":
		return s, fmt.Errorf("%s is required", envNamespace)
	case s.configMap == "":
		return s, fmt.Errorf("%s is required", envConfigMap)
	}
	auth, err := authOptions(getenv)
	if err != nil {
		return s, fmt.Errorf("bus credentials: %w", err)
	}
	s.auth = auth
	return s, nil
}

// serve watches the config, waits for the first one, watches the worker
// pools in its profile namespace, then dispatches until ctx ends.
func serve(ctx context.Context, log *slog.Logger, s settings, kube kubernetes.Interface) error {
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	store := dispatch.NewConfigStore(log)
	// The status server comes up before the config wait, which can take up
	// to configWait: a /healthz that isn't answering yet fails liveness at
	// 10s x 6 whatever the reason. /readyz stays false until the rest is up.
	pr := &probes{configLoaded: store.Ready}
	srv := serveStatus(log, pr)
	defer func() {
		sctx, scancel := context.WithTimeout(context.Background(), statusTimeout)
		defer scancel()
		_ = srv.Shutdown(sctx)
	}()
	go func() { _ = store.WatchConfigMap(ctx, kube, s.namespace, s.configMap, lib.DispatchConfigKey) }()
	if err := store.WaitForConfig(ctx, configWait); err != nil {
		return err
	}
	profileNS := store.Config().ProfileNamespace
	pool := dispatch.NewPodPool(ctx, kube, profileNS, log)
	go pool.Run(ctx)
	go watchProfileNamespace(ctx, cancel, store, profileNS)

	client, err := connectWithRetry(ctx, log, dialBackoff, func(ctx context.Context) (*lib.Client, error) {
		return lib.Connect(ctx, s.natsURL, lib.WithName(clientName), lib.WithLogger(log),
			lib.WithAgreementPolicy(lib.AgreementPolicy{Submitter: dispatch.DefaultSubmitter}),
			lib.WithNATSOptions(append(s.auth, nats.ReconnectWait(reconnectWait), nats.ReconnectJitter(reconnectJitter, reconnectJitter))...))
	})
	if err != nil {
		return fmt.Errorf("bus connect: %w", err)
	}
	defer client.Close()
	pr.setBus(func() bool { return client.Conn().IsConnected() })
	d, err := dispatch.New(dispatch.DefaultConfig(), client, store, pool, busVerifier{conn: client.Conn, log: log}, log)
	if err != nil {
		return err
	}
	// Job mode for the unattended tier. It runs only for a profile with a
	// Job template in the config, which the operator renders (with this
	// dispatcher's Jobs Role) only where the chart installed the Job
	// admission policy; other profiles keep the pool.
	d.UseJobs(dispatch.NewKubeJobs(kube, store.Config))
	pr.setDispatcher(d.Ready)
	log.Info("dispatching", "configmap", s.configMap, "profileNamespace", profileNS)
	if err := d.Run(ctx); err != nil {
		return err
	}
	if cause := context.Cause(ctx); errors.Is(cause, errProfileNamespaceMoved) {
		return cause
	}
	return nil
}

func watchProfileNamespace(ctx context.Context, cancel context.CancelCauseFunc, src dispatch.Source, watched string) {
	t := time.NewTicker(namespaceCheckInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if ns := src.Config().ProfileNamespace; ns != watched {
				cancel(errProfileNamespaceMoved)
				return
			}
		}
	}
}

// authOptions is the projected-token identity the deployment uses, or a
// static user for a run by hand against a bus with no callout. The token
// path is the verifier's: lib.KSATokenNATSOptions (what lib.WithKSAToken
// wraps), read here so a missing token fails before the config wait. The
// bus CA (NATS_CA_FILE) is lib.Connect's, through lib.TLSOptions.
func authOptions(getenv func(string) string) ([]nats.Option, error) {
	if user := getenv("NATS_USER"); user != "" {
		return []nats.Option{nats.UserInfo(user, getenv("NATS_PASSWORD")), nats.CustomInboxPrefix("_INBOX." + user)}, nil
	}
	tokenFile := getenv(lib.EnvBusTokenFile)
	if tokenFile == "" {
		tokenFile = lib.BusTokenPath
	}
	return lib.KSATokenNATSOptions(tokenFile, natsUser)
}

// probes is the status server's view of the process. Ready means the
// dispatch config is loaded, the bus is connected, and the dispatcher is
// serving with every listed durable bound (dispatch.Dispatcher.Ready). The
// bus and the dispatcher arrive after the config, so until each is set it
// reads as not ready.
type probes struct {
	configLoaded func() bool

	mu        sync.Mutex
	connected func() bool
	serving   func() (bool, string)
}

func (p *probes) setBus(connected func() bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.connected = connected
}

func (p *probes) setDispatcher(serving func() (bool, string)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.serving = serving
}

func (p *probes) ready() (bool, string) {
	if !p.configLoaded() {
		return false, "the dispatch config is not loaded yet"
	}
	p.mu.Lock()
	connected, serving := p.connected, p.serving
	p.mu.Unlock()
	if connected == nil || !connected() {
		return false, "not connected to the bus"
	}
	if serving == nil {
		return false, "the dispatcher is not running yet"
	}
	return serving()
}

// mux routes the two probes. /healthz is unconditional: a bus disconnect
// or a durable the operator hasn't made yet must not get a dispatcher that
// is about to recover killed, and holding tasks across a restart costs a
// restart grace.
func (p *probes) mux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if ok, why := p.ready(); !ok {
			http.Error(w, why, http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	return mux
}

func serveStatus(log *slog.Logger, p *probes) *http.Server {
	srv := &http.Server{Addr: statusAddr, Handler: p.mux(), ReadHeaderTimeout: statusTimeout}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("status listener", "err", err)
		}
	}()
	return srv
}

const (
	// dialBackoff is the wait after the first failed bus connect, doubled
	// up to maxDialBackoff: the bus is often still rolling when the
	// dispatcher starts in the same operator pass, and exiting on it would
	// cost a CrashLoopBackOff whose delay grows with each restart.
	dialBackoff    = time.Second
	maxDialBackoff = 30 * time.Second
)

// connectWithRetry calls dial until it succeeds, the bus refuses the
// credential or certificate (connectRefused, which no retry fixes), or ctx is
// done, logging each failure with the wait before the next attempt.
func connectWithRetry[C any](ctx context.Context, log *slog.Logger, backoff time.Duration, dial func(context.Context) (C, error)) (C, error) {
	for {
		c, err := dial(ctx)
		if err == nil {
			return c, nil
		}
		if connectRefused(err) {
			var zero C
			return zero, fmt.Errorf("the bus refused the connection (check the dispatcher's bus token and the bus CA): %w", err)
		}
		log.Warn("the bus is not reachable yet; retrying", "in", backoff, "err", err)
		select {
		case <-ctx.Done():
			var zero C
			return zero, ctx.Err()
		case <-time.After(backoff):
		}
		backoff = min(2*backoff, maxDialBackoff)
	}
}

// connectRefused reports a connect failure no retry fixes: a refused
// credential or an untrusted certificate.
func connectRefused(err error) bool {
	var unknownCA x509.UnknownAuthorityError
	var wrongName x509.HostnameError
	var unverified *tls.CertificateVerificationError
	return errors.Is(err, nats.ErrAuthorization) ||
		strings.Contains(strings.ToLower(err.Error()), "authorization violation") ||
		errors.As(err, &unknownCA) || errors.As(err, &wrongName) || errors.As(err, &unverified)
}
