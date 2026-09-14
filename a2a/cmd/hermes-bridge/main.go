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
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"

	hermesbridge "github.com/gke-labs/kube-agents/a2a/hermes-bridge"
)

const (
	// exitFailure is the exit for anything that went wrong after the
	// environment was read: bridge init, the run itself.
	exitFailure = 1
	// exitUsage is the exit for a missing NATS_URL, the one thing the bridge
	// cannot default; it is the code the sidecar has always used for it.
	exitUsage = 2

	// The default* values below are the environment's spelling of the
	// zero-value defaults hermesbridge.Config applies in defaults()
	// (a2a/hermes-bridge/bridge.go); the two must agree, because a variable
	// left unset and one set to its default have to configure the same
	// bridge. defaultConcurrency and defaultTaskDeadlineSeconds are the
	// platform profile's concurrency and activeDeadlineSeconds in
	// docs/designs/spec-subagent-profiles.md, which is where the numbers
	// come from.
	defaultProfile             = "platform"
	defaultConcurrency         = 2
	defaultTaskDeadlineSeconds = 7200
	defaultKillGraceSeconds    = 10
	defaultKVBucket            = "runtime-state"
)

// errUsage is what realMain returns when NATS_URL is missing, so run can
// keep the usage exit code distinct from every other failure.
var errUsage = errors.New("NATS_URL is required")

func main() {
	os.Exit(run())
}

// run owns the process logger, the signal context and the exit code.
// Everything that can fail is in realMain, which returns the error instead
// of exiting so a test can drive it to each failure.
func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	if err := realMain(ctx, log); err != nil {
		if errors.Is(err, errUsage) {
			return exitUsage
		}
		return exitFailure
	}
	return 0
}

// realMain is the bridge from environment to shutdown. Every failure is
// logged where it is found and then returned; a missing NATS_URL returns
// errUsage before anything is dialed.
func realMain(ctx context.Context, log *slog.Logger) error {
	url := os.Getenv("NATS_URL")
	if url == "" {
		log.Error("NATS_URL is required")
		return errUsage
	}
	cfg := hermesbridge.Config{
		NATSURL:      url,
		Profile:      envOr("BRIDGE_PROFILE", defaultProfile),
		Concurrency:  envInt(log, "BRIDGE_CONCURRENCY", defaultConcurrency),
		TaskDeadline: time.Duration(envInt(log, "BRIDGE_TASK_DEADLINE_SECONDS", defaultTaskDeadlineSeconds)) * time.Second,
		KillGrace:    time.Duration(envInt(log, "BRIDGE_KILL_GRACE_SECONDS", defaultKillGraceSeconds)) * time.Second,
		KVBucket:     envOr("BRIDGE_KV_BUCKET", defaultKVBucket),
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

	b, err := hermesbridge.New(ctx, cfg)
	if err != nil {
		log.Error("bridge init failed", "err", err)
		return err
	}
	if err := b.Run(ctx); err != nil {
		log.Error("bridge exited", "err", err)
		return err
	}
	log.Info("bridge shut down cleanly")
	return nil
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
