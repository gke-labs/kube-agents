// hermes-bridge consumes tasks addressed to the platform profile and answers
// them by invoking the hermes CLI, one subprocess per task. It runs as a
// sidecar in the platform-agent pod. Design: a2a/docs/hermes-bridge.md.
//
// PLAYGROUND POSTURE: this deployment exists to prove the A2A fabric shape.
// Static bus credentials, a shared bus user, and no queue-staleness guard
// are the playground, not the product - the auth callout, per-identity
// users, and the stage-3 dispatcher replace them.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	hermesbridge "github.com/gke-labs/kube-agents/a2a/hermes-bridge"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	url := os.Getenv("NATS_URL")
	if url == "" {
		log.Error("NATS_URL is required")
		os.Exit(2)
	}
	cfg := hermesbridge.Config{
		NATSURL:      url,
		Profile:      envOr("BRIDGE_PROFILE", "platform"),
		Concurrency:  envInt(log, "BRIDGE_CONCURRENCY", 2),
		TaskDeadline: time.Duration(envInt(log, "BRIDGE_TASK_DEADLINE_SECONDS", 7200)) * time.Second,
		KillGrace:    time.Duration(envInt(log, "BRIDGE_KILL_GRACE_SECONDS", 10)) * time.Second,
		KVBucket:     envOr("BRIDGE_KV_BUCKET", "runtime-state"),
		Logger:       log,
	}
	if bin := os.Getenv("HERMES_BIN"); bin != "" {
		cfg.Command = []string{bin, "-p", cfg.Profile, "chat", "-Q", "-q"}
	}
	if user := os.Getenv("NATS_USER"); user != "" {
		cfg.NATSOptions = append(cfg.NATSOptions, nats.UserInfo(user, os.Getenv("NATS_PASSWORD")))
		// Push delivery answers on inbox subjects, and this user may only
		// subscribe under its own prefix - the CLI default _INBOX.<nuid>
		// would be refused and every JS API call would time out.
		cfg.NATSOptions = append(cfg.NATSOptions, nats.CustomInboxPrefix("_INBOX."+user))
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	b, err := hermesbridge.New(ctx, cfg)
	if err != nil {
		log.Error("bridge init failed", "err", err)
		os.Exit(1)
	}
	if err := b.Run(ctx); err != nil {
		log.Error("bridge exited", "err", err)
		os.Exit(1)
	}
	log.Info("bridge shut down cleanly")
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(log *slog.Logger, key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		log.Error("bad integer env value; using default", "key", key, "value", v, "default", def)
		return def
	}
	return n
}
