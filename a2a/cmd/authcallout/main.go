// Command authcallout is the NATS auth callout service.
//
// It answers the bus server's authorization requests: a client presents a
// projected Kubernetes ServiceAccount token, this validates it against the
// cluster with a TokenReview, and answers with the account and permission set
// the operator mapped that ServiceAccount to.
//
// It renders under `mode: next` and nowhere else.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/a2a/authcallout"
)

const (
	envNATSURL      = "NATS_URL"
	envNATSUser     = "NATS_USER"
	envNATSPassword = "NATS_PASSWORD"
	envNamespace    = "POD_NAMESPACE"
	envAuthMapName  = "A2A_AUTHMAP_NAME"
	envAuthMapKey   = "A2A_AUTHMAP_KEY"
	envAudience     = "A2A_TOKEN_AUDIENCE"
	envIssuerSeed   = "A2A_ISSUER_SEED"
	envXKeySeed     = "A2A_XKEY_SEED"
	envGrantTTL     = "A2A_GRANT_TTL_SECONDS"
	envStatusAddr   = "A2A_STATUS_ADDR"

	defaultAuthMapKey  = "identities.json"
	defaultStatusAddr  = ":8080"
	defaultMapWait     = 60 * time.Second
	statusReadTimeout  = 5 * time.Second
	statusWriteTimeout = 5 * time.Second
	shutdownGrace      = 5 * time.Second

	// natsClientName is what this process calls itself on the bus. It is
	// what `nats server report connections` shows, so it is the string
	// somebody debugging an authorization outage searches for.
	natsClientName = "a2a-auth-callout"

	// reconnectJitter and reconnectJitterTLS are the upper bounds of the
	// random delay added to each reconnect attempt, raised from the client
	// defaults of 100ms and 1s. Both replicas plus every other bus client
	// come back at once after a restart, and the callout is the one that
	// must not arrive inside that burst: while it is disconnected the server
	// authorizes nobody, so it is contending for the connection everything
	// else is waiting on. Same NR-6 reasoning as the callout's own
	// grantTTLJitter, one layer down.
	reconnectJitter    = 500 * time.Millisecond
	reconnectJitterTLS = 2 * time.Second
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	if err := run(log); err != nil {
		log.Error("auth callout exiting", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	namespace := os.Getenv(envNamespace)
	if namespace == "" {
		return fmt.Errorf("%s is required", envNamespace)
	}
	authMapName := os.Getenv(envAuthMapName)
	if authMapName == "" {
		return fmt.Errorf("%s is required", envAuthMapName)
	}
	authMapKey := os.Getenv(envAuthMapKey)
	if authMapKey == "" {
		authMapKey = defaultAuthMapKey
	}
	audience := os.Getenv(envAudience)
	if audience == "" {
		// Refused rather than defaulted. A TokenReview with no audience
		// validates against the API server's own, which every pod's
		// default token carries — so an unset audience would quietly turn
		// every readable token in the cluster into a bus credential.
		return fmt.Errorf("%s is required; see authcallout.NewTokenValidator on why it may not be empty", envAudience)
	}
	issuerSeed := os.Getenv(envIssuerSeed)
	if issuerSeed == "" {
		return fmt.Errorf("%s is required", envIssuerSeed)
	}

	restCfg, err := rest.InClusterConfig()
	if err != nil {
		return fmt.Errorf("in-cluster config: %w", err)
	}
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return fmt.Errorf("building the Kubernetes client: %w", err)
	}

	store := authcallout.NewStore(log)
	validator, err := authcallout.NewTokenValidator(clientset, audience)
	if err != nil {
		return err
	}

	grantTTL := time.Duration(0)
	if raw := os.Getenv(envGrantTTL); raw != "" {
		secs, err := strconv.Atoi(raw)
		if err != nil {
			return fmt.Errorf("%s: %w", envGrantTTL, err)
		}
		grantTTL = time.Duration(secs) * time.Second
	}

	svc, err := authcallout.NewService(store, validator, authcallout.Config{
		IssuerSeed: issuerSeed,
		XKeySeed:   os.Getenv(envXKeySeed),
		GrantTTL:   grantTTL,
	}, log)
	if err != nil {
		return err
	}

	// The status surface comes up first, so a callout that cannot reach the
	// API server fails its readiness probe and is taken out of the Service
	// rather than sitting in it answering nothing.
	statusAddr := os.Getenv(envStatusAddr)
	if statusAddr == "" {
		statusAddr = defaultStatusAddr
	}
	// busConn is set once the connection is up and read by the readiness
	// probe. Nil until then, which is the honest answer: a callout that has
	// not attached to the bus yet answers no authorization request either.
	var busConn atomic.Pointer[nats.Conn]
	busAttached := func() bool {
		nc := busConn.Load()
		return nc != nil && nc.IsConnected()
	}
	statusSrv := &http.Server{
		Addr:              statusAddr,
		Handler:           authcallout.StatusHandler(store, busAttached),
		ReadHeaderTimeout: statusReadTimeout,
		WriteTimeout:      statusWriteTimeout,
	}
	go func() {
		if err := statusSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("status server stopped", "error", err)
		}
	}()
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownGrace)
		defer cancel()
		_ = statusSrv.Shutdown(shutdownCtx)
	}()

	// WatchConfigMap returns only when the context ends, so there is no
	// error branch here to log: the informer retries internally and a
	// genuinely broken watch surfaces as the store never becoming ready,
	// which WaitForMap below reports with the reason attached.
	go func() { _ = store.WatchConfigMap(ctx, clientset, namespace, authMapName, authMapKey) }()

	// Serving nothing means refusing everything, so do not subscribe until
	// there is a map to answer from. Subscribing first would mean the window
	// between process start and first sync is one where the callout actively
	// refuses legitimate clients rather than leaving them to retry.
	if err := store.WaitForMap(ctx, defaultMapWait); err != nil {
		return fmt.Errorf("waiting for the identity map: %w", err)
	}

	// Buffered and fired at most once: the deferred Close below also runs the
	// handler, and a shutdown must not look like a failure.
	busClosed := make(chan struct{})
	var closeOnce sync.Once
	nc, err := connectToBus(log, func() { closeOnce.Do(func() { close(busClosed) }) })
	if err != nil {
		return err
	}
	defer nc.Close()

	busConn.Store(nc)

	if _, err := svc.Subscribe(nc); err != nil {
		return err
	}

	// Exit rather than sit here detached. Both halves of that are deliberate.
	//
	// Sitting here was the old behaviour and it is the worst of the options:
	// the process stays up, the pod stays Ready — readiness now says
	// otherwise, but the Deployment still holds a Pod that will never recover
	// — and nothing on the cluster says the bus has stopped authorizing
	// anyone. A component whose whole job is on one connection should not
	// outlive it.
	//
	// And restarting is not merely a way to retry. The connection ends for
	// good on a repeated authorization failure, whose live cause is a rotated
	// callout password; the password is read from the environment at startup,
	// so the new one reaches this process only through a new process. Retrying
	// in place would loop on the old credential forever.
	select {
	case <-ctx.Done():
		log.Info("shutting down")
		return nil
	case <-busClosed:
		return errors.New("the bus connection ended and will not recover in this process; " +
			"restarting to re-read the callout credential")
	}
}

// connectToBus dials as the callout's own statically-authenticated user. It
// cannot authenticate through itself, so nats.conf's auth_users exempts it.
//
// onClosed fires if the connection ends for good. MaxReconnects(-1) does not
// make that unreachable: nats.go aborts its own reconnect loop when the same
// server answers with the same authorization error twice running
// (processAuthError, nats.go v1.53.1), which is exactly what a rotated
// callout password looks like — and the password is read from the environment
// at startup, so no amount of retrying in this process would pick up the new
// one. See run for what the callout does about it.
func connectToBus(log *slog.Logger, onClosed func()) (*nats.Conn, error) {
	url := os.Getenv(envNATSURL)
	if url == "" {
		return nil, fmt.Errorf("%s is required", envNATSURL)
	}
	user, password := os.Getenv(envNATSUser), os.Getenv(envNATSPassword)
	if user == "" || password == "" {
		return nil, fmt.Errorf("%s and %s are required", envNATSUser, envNATSPassword)
	}

	nc, err := nats.Connect(url,
		nats.UserInfo(user, password),
		nats.Name(natsClientName),
		// The callout must survive a bus restart, and it is the component
		// where failing to is worst: while it is disconnected the server
		// authorizes nobody. Retry forever rather than exiting, and jitter
		// the attempts so two replicas plus every other client do not
		// arrive together (NR-6).
		nats.RetryOnFailedConnect(true),
		nats.MaxReconnects(-1),
		nats.ReconnectJitter(reconnectJitter, reconnectJitterTLS),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Warn("disconnected from the bus; no new connection can be authorized until this recovers", "error", err)
		}),
		nats.ReconnectHandler(func(c *nats.Conn) {
			log.Info("reconnected to the bus", "url", c.ConnectedUrl())
		}),
		nats.ClosedHandler(func(_ *nats.Conn) {
			log.Error("bus connection closed for good; the callout can no longer authorize anything")
			onClosed()
		}),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			log.Error("bus error", "error", err)
		}),
	)
	if err != nil {
		return nil, fmt.Errorf("connecting to the bus: %w", err)
	}
	return nc, nil
}
