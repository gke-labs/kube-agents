// The a2a chatops gateway: chat (Discord or Google Chat) in, tasks on the bus out.
//
// PLAYGROUND POSTURE: bot token as a plain Secret, no exporter, no breaker,
// gateway sweep as the only janitor. Each has a decided design in
// docs/designs/spec-nats-deployment.md and spec-chatops-gateway.md.
//
// The auth callout is no longer on that list - it is armed, and the sessions
// this gateway spawns authenticate through it with per-pod grants. The gateway
// itself is still a static NATS user, and that is sequencing rather than
// posture: it has a ServiceAccount and a map entry could be rendered for it
// tomorrow, but this program dials with NATS_USER/NATS_PASSWORD and moving the
// identity before the program would refuse the gateway at connect on every
// install.
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// exitFailure is the one non-zero exit code this binary has: config,
	// dial, adapter and run failures all leave through it, each having
	// logged its own reason at the site that found it.
	exitFailure = 1
)

func main() {
	os.Exit(run())
}

// run owns what a test cannot: the process logger, the signal context and
// the exit code. Everything that can fail is in realMain, which returns the
// error instead of exiting so a test can drive it to each failure.
func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := realMain(ctx, log); err != nil {
		return exitFailure
	}
	return 0
}

// realMain is the gateway from configuration to shutdown. Every failure is
// logged where it is found and then returned; realMain itself logs nothing
// about the exit, and run maps every error to the same exit code.
// gateway.FromEnv is the first call, so a configuration error returns before
// anything is dialed.
func realMain(ctx context.Context, log *slog.Logger) error {
	cfg, err := gateway.FromEnv()
	if err != nil {
		log.Error("config", "err", err)
		return err
	}

	natsOpts := []nats.Option{
		// The gateway user may only subscribe under its own inbox prefix
		// (per-user _INBOX prefixes, deployment spec); the JS API replies
		// every publish and consume depends on land there.
		nats.CustomInboxPrefix("_INBOX.gateway"),
	}
	if cfg.NATSUser != "" {
		natsOpts = append(natsOpts, nats.UserInfo(cfg.NATSUser, cfg.NATSPassword))
	}
	client, err := lib.Connect(ctx, cfg.NATSURL,
		lib.WithName("a2a-gateway"),
		lib.WithLogger(log),
		lib.WithAgreementPolicy(gateway.SupervisorAgreement(cfg)),
		lib.WithNATSOptions(natsOpts...),
	)
	if err != nil {
		log.Error("nats connect", "err", err)
		return err
	}
	defer client.Close()

	// FromEnv already enforced exactly one backend.
	var adapter gateway.Adapter
	switch backend := cfg.Backend(); backend {
	case "gchat":
		adapter, err = gateway.NewGoogleChatAdapter(cfg.GchatRelayURL, cfg.GchatTokenPath, log)
	case "inject":
		// Dev and eval installs only; the adapter logs the posture at boot
		// and the operator renders A2A_INJECT_LISTEN only under its eval
		// flag. See a2a/gateway/inject.go.
		adapter, err = gateway.NewInjectAdapter(cfg.InjectListen, log)
	default:
		adapter, err = gateway.NewDiscordAdapter(cfg.DiscordToken, log)
	}
	if err != nil {
		log.Error("adapter", "backend", cfg.Backend(), "err", err)
		return err
	}

	gw, err := gateway.New(gateway.Options{
		Client:  client,
		Adapter: adapter,
		Config:  cfg,
		Logger:  log,
		Backend: cfg.Backend(),
	})
	if err != nil {
		log.Error("gateway", "err", err)
		return err
	}

	log.Info("a2a gateway starting",
		"nats", cfg.NATSURL,
		"defaultAddressee", cfg.DefaultAddressee,
		"spawnSessions", cfg.SpawnSessions,
		"idleTTL", cfg.IdleTTL.String())
	if err := gw.Run(ctx); err != nil && ctx.Err() == nil {
		log.Error("gateway exited", "err", err)
		return err
	}
	return nil
}
