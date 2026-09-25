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

// buildAdapters puts the configured chat backend and the console adapter
// behind one mux. The console runs whenever the gateway runs: its identity
// renders under mode next like the rest of the bus, and a render that
// predates it surfaces as a logged refusal on the console subscription, not a
// boot failure (spec-chatops-gateway.md, "The console adapter"). With no real
// backend (an inject-only eval install) the console is the only chat backend
// and is returned alone, since a mux of one would need a key for nothing.
func buildAdapters(cfg *gateway.Config, primary gateway.Adapter, natsOpts []nats.Option, log *slog.Logger) (gateway.Adapter, error) {
	console, err := gateway.NewConsoleAdapter(cfg.NATSURL, natsOpts, log)
	if err != nil {
		return nil, err
	}
	if primary == nil {
		return console, nil
	}
	return gateway.NewMultiAdapter(cfg.Backend(), map[string]gateway.Adapter{
		cfg.Backend(): primary,
		"console":     console,
	})
}

// composeAdapters is the whole stack the gateway drives: the chat backends
// behind the mux, and the doors (inject, A2A), when armed, beside the mux
// rather than inside it. The order matters. The gateway finds the door's
// ProbeSink, TaskObserver and InboundObserver by type assertion on the top
// of the stack, and the side door implements them for exactly that reason
// (sidedoor.go); MultiAdapter implements none of them, and its prefix
// dispatch has no key for an inject: conversation.
func composeAdapters(cfg *gateway.Config, primary gateway.Adapter, doors []gateway.DoorSpec, natsOpts []nats.Option, log *slog.Logger) (gateway.Adapter, error) {
	chat, err := buildAdapters(cfg, primary, natsOpts, log)
	if err != nil {
		return nil, err
	}
	if len(doors) == 0 {
		return chat, nil
	}
	return gateway.WithSideDoors(chat, doors, log), nil
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

	// FromEnv already enforced at most one real backend, and that the door
	// carries a token if it is armed at all.
	backend := cfg.Backend()
	var adapter gateway.Adapter
	switch backend {
	case "gchat":
		adapter, err = gateway.NewGoogleChatAdapter(cfg.GchatRelayURL, cfg.GchatTokenPath, log)
	case "":
		// No real backend: the inject door and the console are the only
		// ingresses, which is what lets an eval install's gateway start at
		// all (#1660).
	default:
		adapter, err = gateway.NewDiscordAdapter(cfg.DiscordToken, log)
	}
	if err != nil {
		log.Error("adapter", "backend", backend, "err", err)
		return err
	}
	// The door is a side door, not a backend: it can be armed beside either
	// of the above, and the composite routes by conversation key. Dev and
	// eval installs only; the operator renders A2A_INJECT_LISTEN and the
	// token only under its eval flag. See a2a/gateway/inject.go.
	var doors []gateway.DoorSpec
	if cfg.InjectArmed() {
		door, err := gateway.NewInjectAdapter(cfg.InjectListen, cfg.InjectToken, cfg.FirstEventGrace, log)
		if err != nil {
			log.Error("inject door", "err", err)
			return err
		}
		doors = append(doors, gateway.InjectDoorSpec(door))
	}
	// The A2A door is the same kind of thing for an agent caller: armed
	// beside either backend or alone, routed by its own key prefix, its
	// callers resolved through its own map. See a2a/gateway/a2adoor.go.
	if cfg.A2ADoorArmed() {
		door, err := gateway.NewA2ADoor(cfg.A2ADoorListen, cfg.A2ADoorToken, gateway.A2ADoorOptions{
			PublicURL:        cfg.A2ADoorPublicURL,
			DefaultAddressee: cfg.DefaultAddressee,
			FirstEventGrace:  cfg.FirstEventGrace,
			Logger:           log,
		})
		if err != nil {
			log.Error("A2A door", "err", err)
			return err
		}
		doors = append(doors, gateway.A2ADoorSpec(door))
	}

	adapter, err = composeAdapters(cfg, adapter, doors, natsOpts, log)
	if err != nil {
		log.Error("console adapter", "err", err)
		return err
	}

	gw, err := gateway.New(gateway.Options{
		Client:  client,
		Adapter: adapter,
		Config:  cfg,
		Logger:  log,
		Backend: backend,
	})
	if err != nil {
		log.Error("gateway", "err", err)
		return err
	}

	log.Info("a2a gateway starting",
		"nats", cfg.NATSURL,
		"backend", backend,
		"injectDoor", cfg.InjectArmed(),
		"a2aDoor", cfg.A2ADoorArmed(),
		"defaultAddressee", cfg.DefaultAddressee,
		"spawnSessions", cfg.SpawnSessions,
		"idleTTL", cfg.IdleTTL.String())
	if err := gw.Run(ctx); err != nil && ctx.Err() == nil {
		log.Error("gateway exited", "err", err)
		return err
	}
	return nil
}
