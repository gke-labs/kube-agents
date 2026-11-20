// Command teams-inbound is the Microsoft Teams inbound receiver: the address
// Bot Framework delivers Teams messages to. It verifies Microsoft's signed
// token on each request, pins the tenant, and hands the activity to the
// credential broker's Teams queue. It holds no credential of the install's
// own. See a2a/teamsinbound.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/teamsinbound"
)

const (
	// shutdownGrace is how long in-flight requests get once a stop arrives.
	shutdownGrace = 10 * time.Second
	// headerTimeout bounds a slow client's headers on the public listener;
	// readTimeout the whole request, body included, so a client that trickles
	// bytes under a bearer it never proves cannot hold a goroutine; writeTimeout
	// the answer; idleTimeout a kept-alive connection that sends nothing.
	headerTimeout = 10 * time.Second
	readTimeout   = 20 * time.Second
	writeTimeout  = 20 * time.Second
	idleTimeout   = 60 * time.Second
	// warmTimeout bounds the startup read of Microsoft's signing keys
	// (Verifier.Warm runs its fetch on this context); a failure is logged
	// and the first request fetches again.
	warmTimeout = 15 * time.Second
)

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	cfg, err := teamsinbound.FromEnv(os.Getenv)
	if err != nil {
		log.Error("teams inbound: configuration refused", "err", err)
		os.Exit(1)
	}
	srv := teamsinbound.NewServer(cfg, log)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	warmCtx, cancel := context.WithTimeout(ctx, warmTimeout)
	if err := srv.Verifier().Warm(warmCtx); err != nil {
		log.Warn("teams inbound: Microsoft's signing keys were not read at startup; the first request will read them", "err", err)
	}
	cancel()

	public := &http.Server{Addr: cfg.Listen, Handler: srv.Handler(), ReadHeaderTimeout: headerTimeout, ReadTimeout: readTimeout, WriteTimeout: writeTimeout, IdleTimeout: idleTimeout}
	status := &http.Server{Addr: cfg.StatusListen, Handler: srv.StatusHandler(), ReadHeaderTimeout: headerTimeout, ReadTimeout: readTimeout, WriteTimeout: writeTimeout, IdleTimeout: idleTimeout}
	errs := make(chan error, 2)
	go func() { errs <- public.ListenAndServe() }()
	go func() { errs <- status.ListenAndServe() }()
	log.Info("teams inbound: listening", "public", cfg.Listen, "status", cfg.StatusListen, "relay", cfg.RelayURL)

	failed := false
	select {
	case <-ctx.Done():
	case err := <-errs:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("teams inbound: a listener failed", "err", err)
			failed = true
		}
	}
	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), shutdownGrace)
	defer cancelShutdown()
	_ = public.Shutdown(shutdownCtx)
	_ = status.Shutdown(shutdownCtx)
	if failed {
		// A listener that could not bind is a crashed pod, not a completed one.
		os.Exit(1)
	}
}
