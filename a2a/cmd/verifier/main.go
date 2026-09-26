// The a2a capability verifier: the only component that reads the `cap` bucket.
//
// A broker holding a capability reference cannot resolve it — no broker has
// read on the bucket, by design (09 §4). It asks here, on a subject named for
// itself, and gets back one boolean and a sentence. The chain walk, the
// attenuation rules and the verb table live in a2a/capability; this program is
// the workload that runs them.
//
// It is its own Deployment rather than a library or a sidecar, and that is the
// design decision 09 forces. Folding it into the auth callout would stack the
// read on the seed 09 already names the largest concentration of authority in
// the deployment. Folding it into the gateway would put minting and reading in
// one process, which is the separation the whole scheme rests on.
//
// It is on the request path: while it is down, no task starts. That is the
// cost of the control, it is deliberate, and it is why this runs two replicas
// with a rollout that never drops below the running count — the same shape the
// auth callout uses for the same reason.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// natsUser is the principal this program authenticates as. It is also its
// inbox prefix, and it must equal the `user` the operator renders for the
// verifier identity: the JetStream API replies every store read depends on
// land in _INBOX.<user>.>, and a mismatch authenticates fine and then times
// out on every get — the failure shape this deployment has found twice.
const natsUser = "verifier"

const (
	// reconnectJitter matches the callout's, and for the same reason: a bus
	// restart brings every client back at once (NR-6).
	reconnectJitter    = 500 * time.Millisecond
	reconnectJitterTLS = 2 * time.Second

	readyPort    = ":8080"
	readyTimeout = 5 * time.Second

	// drainTimeout bounds the wait for in-flight requests after SIGTERM. It
	// sits under Kubernetes' default 30s termination grace period on
	// purpose: past this the kubelet's SIGKILL is coming either way, and a
	// bounded wait that logs is more useful than an unbounded one that gets
	// killed mid-sentence. A verify round trip is a single KV read.
	drainTimeout = 10 * time.Second
)

func main() { os.Exit(run()) }

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	url := os.Getenv("NATS_URL")
	if url == "" {
		log.Error("NATS_URL is required")
		return 1
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// Buffered by close() and fired at most once: the deferred Close below
	// also runs the handler, and a clean shutdown must not look like a
	// failure. See connect for why MaxReconnects(-1) does not make this
	// unreachable.
	busClosed := make(chan struct{})
	var closeOnce sync.Once
	nc, err := connect(ctx, log, url, func() { closeOnce.Do(func() { close(busClosed) }) })
	if err != nil {
		log.Error("bus connect", "err", err)
		return 1
	}
	defer nc.Close()

	js, err := jetstream.New(nc)
	if err != nil {
		log.Error("jetstream", "err", err)
		return 1
	}
	// The store binds the bucket, which is a $JS.API.STREAM.INFO read. If
	// the grants are wrong this is where it shows, at boot, rather than as
	// every task in the deployment being refused one at a time.
	store, err := capability.NewStore(ctx, js)
	if err != nil {
		log.Error("bind the capability bucket; the verifier cannot answer anything without it",
			"bucket", capability.Bucket, "err", err)
		return 1
	}

	svc := &capability.Service{
		Resolver: &capability.Resolver{Store: store},
		Log:      log,
	}
	// Handlers run under a context of their own, deliberately not the signal
	// context. Every callback closes over whatever it is given, and the
	// resolver's store reads take it: hand it the signal context and SIGTERM
	// cancels it BEFORE the drain below, so each request still queued is
	// answered by way of `context.Canceled` reaching Answer's fail-closed
	// branch -- a refusal, sent to a broker that would read it as the
	// capability being bad and reject a task that was fine. That is the exact
	// outcome the drain exists to prevent, so the drain has to outlive the
	// signal. handlerCancel runs after the drain has finished, not before.
	handlerCtx, handlerCancel := context.WithCancel(context.Background())
	defer handlerCancel()

	// The handle is not kept: the shutdown below drains the whole connection
	// rather than this one subscription, so there is nothing left to call on
	// it.
	if _, err := svc.Subscribe(handlerCtx, nc); err != nil {
		log.Error("subscribe", "subject", capability.VerifySubscribe, "err", err)
		return 1
	}
	log.Info("verifying", "subject", capability.VerifySubscribe, "queue", capability.VerifyQueue,
		"bucket", capability.Bucket)

	srv := serveReady(log, nc)

	select {
	case <-ctx.Done():
	case <-busClosed:
		// Exit non-zero so the pod restarts. Returning to the drain below
		// would be worse than useless: the connection is gone, the drain
		// cannot flush, and the process would sit here answering the
		// liveness probe forever. /healthz is unconditional by design --
		// a transient disconnect must not kill a verifier that is about
		// to reconnect -- so this is the only thing that restarts it, and
		// without it the Deployment stays NotReady for good while every
		// executor refuses every task.
		log.Error("the bus connection ended and will not recover in this process; " +
			"exiting so the pod restarts")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), readyTimeout)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
		return 1
	}
	// Drain rather than Unsubscribe, wait for the drain rather than defer it,
	// and cancel the handlers only after. capability.DrainAndCancel carries
	// the three reasons; it is there rather than here so the ordering is
	// pinned by a test against the code that runs.
	capability.DrainAndCancel(log, nc, handlerCancel, drainTimeout)

	shutdownCtx, cancel := context.WithTimeout(context.Background(), readyTimeout)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
	log.Info("stopped")
	return 0
}

// serveReady answers the readiness probe. Ready means connected to the bus:
// a verifier that cannot reach the bus cannot answer, and taking it out of
// the queue group's endpoints is better than having it silently not receive.
func serveReady(log *slog.Logger, nc *nats.Conn) *http.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		if !nc.IsConnected() {
			http.Error(w, "not connected to the bus", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	srv := &http.Server{Addr: readyPort, Handler: mux, ReadHeaderTimeout: readyTimeout}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("readiness listener", "err", err)
		}
	}()
	return srv
}

// onClosed fires if the connection ends for good. MaxReconnects(-1) does not
// make that unreachable: nats.go aborts its own reconnect loop when the same
// server answers with the same authorization error twice running
// (processAuthError, nats.go v1.53.1, unless IgnoreAuthErrorAbort). This
// component is callout-authenticated through a projected token, so a bus that
// answers twice the same way -- a revoked identity, a callout that has lost
// the verifier's user, a token file the kubelet has stopped refreshing --
// lands exactly there. See run for what the verifier does about it.
func connect(ctx context.Context, log *slog.Logger, url string, onClosed func()) (*nats.Conn, error) {
	opts := []nats.Option{
		nats.Name("a2a-cap-verifier"),
		// Retry forever rather than exiting. While this is disconnected no
		// task in the deployment can start, so crash-looping to get a
		// fresh connection is strictly worse than reconnecting.
		nats.RetryOnFailedConnect(true),
		nats.MaxReconnects(-1),
		nats.ReconnectJitter(reconnectJitter, reconnectJitterTLS),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Warn("disconnected from the bus; no task can start until this recovers", "err", err)
		}),
		nats.ReconnectHandler(func(c *nats.Conn) {
			log.Info("reconnected to the bus", "url", c.ConnectedUrl())
		}),
		nats.ClosedHandler(func(_ *nats.Conn) {
			log.Error("bus connection closed for good; this verifier can no longer answer, " +
				"so no task anywhere in this install can start")
			onClosed()
		}),
		nats.ErrorHandler(func(_ *nats.Conn, s *nats.Subscription, err error) {
			// Permission violations are asynchronous and land here. On
			// this component they are the whole diagnosis: a missing
			// grant on $JS.API means every get times out with no error
			// of its own.
			subject := ""
			if s != nil {
				subject = s.Subject
			}
			log.Error("bus error", "subject", subject, "err", err)
		}),
	}
	// The projected ServiceAccount token, the same contract session pods
	// use. No shared password exists for this principal anywhere: the
	// component that reads every capability in flight should not be
	// reachable by whoever can read a Secret.
	tokenFile := os.Getenv(lib.EnvBusTokenFile)
	if tokenFile == "" {
		tokenFile = lib.BusTokenPath
	}
	switch user := os.Getenv("NATS_USER"); {
	case user != "":
		// The static path, for a run by hand against a bus with no
		// callout in front of it. The deployment does not take it.
		log.Warn("authenticating with a static password; the deployment uses a projected token", "user", user)
		opts = append(opts,
			nats.UserInfo(user, os.Getenv("NATS_PASSWORD")),
			nats.CustomInboxPrefix("_INBOX."+user))
	default:
		tokenOpts, err := lib.KSATokenNATSOptions(tokenFile, natsUser)
		if err != nil {
			return nil, err
		}
		opts = append(opts, tokenOpts...)
	}
	_ = ctx
	return nats.Connect(url, opts...)
}
