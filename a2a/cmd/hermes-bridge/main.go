// hermes-bridge consumes tasks addressed to the platform profile and answers
// them by invoking the hermes CLI, one subprocess per task. It runs as a
// sidecar in the platform-agent pod. Design: a2a/docs/hermes-bridge.md.
//
// PLAYGROUND POSTURE: this deployment exists to prove the A2A fabric shape.
// No queue-staleness guard is the playground, not the product; the stage-3
// dispatcher replaces it.
//
// The bus user is no longer shared. It was `worker`, one credential covering
// both this program's task plane and the `a2a` CLI's topic blackboard in the
// container next door; that split into `bridge` (here) and `agent` (the CLI),
// and main() below dials as `bridge` with a password from
// <agent>-a2a-nats-creds.
//
// Still a password, and not a debt this program can pay. The auth callout is
// armed, but it resolves an identity from a ServiceAccount token and the API
// server issues one ServiceAccount per POD -- this container shares the agent's
// pod, so a token would resolve it to the `agent` entry in the map and hand
// both containers the union of the two grant sets, which is `worker` rebuilt.
// The map's `agent` principal is therefore the one identity this program must
// NOT reach for. What unblocks a token here is the bridge leaving the pod,
// which is the stage-3 dispatcher; see bridgeIdentity in
// k8s-operator/internal/controller/platformagent_a2a_identities.go and
// a2a/docs/hermes-bridge.md.
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
	// defaultProgressIntervalSeconds is hermesbridge.DefaultProgressInterval
	// in the environment's unit.
	defaultProgressIntervalSeconds = 60
	// activityListenOff is the value that closes the activity door. The
	// Config zero value means "off" but an empty environment variable reads
	// as unset, so the daemon needs a word for it.
	activityListenOff = "off"
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
		// The activity door (a2a/hermes-bridge/activity.go): on by default at
		// the address the platform profile's hooks.overlay.yaml names.
		ActivityListen:   activityListen(envOr("BRIDGE_ACTIVITY_LISTEN", hermesbridge.DefaultActivityListen)),
		ProgressInterval: time.Duration(envInt(log, "BRIDGE_PROGRESS_INTERVAL_SECONDS", defaultProgressIntervalSeconds)) * time.Second,
		Logger:           log,
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

// activityListen maps the environment's spelling of "off" to the Config's.
func activityListen(v string) string {
	if v == activityListenOff {
		return ""
	}
	return v
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
