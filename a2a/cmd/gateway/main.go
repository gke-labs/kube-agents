// The a2a chatops gateway: Discord in, tasks on the bus out.
//
// PLAYGROUND POSTURE: static per-component NATS users instead of the auth
// callout, bot token as a plain Secret, no exporter, no breaker, gateway
// sweep as the only janitor. Each has a decided design in
// docs/designs/spec-nats-deployment.md and spec-chatops-gateway.md; the
// auth callout is the product path and stage 2 work. Static creds are the
// playground, not the product.
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

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	cfg, err := gateway.FromEnv()
	if err != nil {
		log.Error("config", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

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
		lib.WithNATSOptions(natsOpts...),
	)
	if err != nil {
		log.Error("nats connect", "err", err)
		os.Exit(1)
	}
	defer client.Close()

	adapter, err := gateway.NewDiscordAdapter(cfg.DiscordToken, log)
	if err != nil {
		log.Error("discord", "err", err)
		os.Exit(1)
	}

	gw, err := gateway.New(gateway.Options{
		Client:  client,
		Adapter: adapter,
		Config:  cfg,
		Logger:  log,
		Backend: "discord",
	})
	if err != nil {
		log.Error("gateway", "err", err)
		os.Exit(1)
	}

	log.Info("a2a gateway starting",
		"nats", cfg.NATSURL,
		"defaultAddressee", cfg.DefaultAddressee,
		"spawnSessions", cfg.SpawnSessions,
		"idleTTL", cfg.IdleTTL.String())
	if err := gw.Run(ctx); err != nil && ctx.Err() == nil {
		log.Error("gateway exited", "err", err)
		os.Exit(1)
	}
}
