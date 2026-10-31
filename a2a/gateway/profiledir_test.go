package gateway

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func cardEnvelope(t *testing.T, card lib.AgentCard) []byte {
	t.Helper()
	payload, err := json.Marshal(card)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewAgentCardEnvelope(lib.Party{Session: "operator", Profile: card.Name}, "corr-card", payload)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func closedEnvelope(t *testing.T, profile string) []byte {
	t.Helper()
	env, err := lib.NewAgentClosedEnvelope(lib.Party{Session: "operator", Profile: profile}, "corr-closed")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestProfileDirectory_ReadsWhatDirectoryHoldsThenFollowsChanges(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: directoryStream, Subjects: []string{"a2a.agents.>"}, MaxMsgsPerSubject: 1}); err != nil {
		t.Fatal(err)
	}
	// On the stream before the gateway starts: only the read can see it.
	pinned := lib.AgentCard{Name: "prod-east", ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"},
		Parameters: []lib.ProfileParameter{{Name: "project"}, {Name: "location"}, {Name: "cluster"}}}
	if _, err := js.Publish(ctx, lib.AgentSubject("prod-east"), cardEnvelope(t, pinned)); err != nil {
		t.Fatal(err)
	}

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	g, err := New(Options{Client: client, Adapter: newFakeAdapter(), Backend: "discord",
		Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Config: &Config{NATSURL: url, PrincipalMapPath: writePrincipalMap(t), DefaultAddressee: "platform", IdleTTL: time.Minute, AttributionSalt: []byte("s")}})
	if err != nil {
		t.Fatal(err)
	}
	g.startProfileDirectory(ctx)

	waitFor(t, "the card already on DIRECTORY", func() bool {
		c, ok := g.profiles.lookup("prod-east")
		return ok && c.ClusterRef != nil && c.ClusterRef.Cluster == "prod"
	})

	// Published after the start: only the live feed can see it.
	generic := lib.AgentCard{Name: "cluster", Parameters: []lib.ProfileParameter{{Name: "project"}, {Name: "location"}, {Name: "cluster"}}}
	if _, err := js.Publish(ctx, lib.AgentSubject("cluster"), cardEnvelope(t, generic)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "a card published after the start", func() bool {
		c, ok := g.profiles.lookup("cluster")
		return ok && takesTarget(c)
	})

	// A deleted profile's tombstone forgets it.
	if _, err := js.Publish(ctx, lib.AgentSubject("prod-east"), closedEnvelope(t, "prod-east")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the tombstone to forget the profile", func() bool {
		_, ok := g.profiles.lookup("prod-east")
		return !ok
	})
}

func TestDirectoryFeed_TheReadNeverOverwritesALiveCard(t *testing.T) {
	f := &directoryFeed{dir: newProfileDirectory(), log: slog.New(slog.NewTextHandler(io.Discard, nil)), live: map[string]bool{}}
	newer := lib.AgentCard{Name: "cluster", Description: "newer"}
	older := lib.AgentCard{Name: "cluster", Description: "older"}
	if !f.apply(lib.AgentSubject("cluster"), cardEnvelope(t, newer), true) {
		t.Fatal("live card not applied")
	}
	f.apply(lib.AgentSubject("cluster"), cardEnvelope(t, older), false)
	if c, _ := f.dir.lookup("cluster"); c.Description != "newer" {
		t.Fatalf("description = %q, want the live card's", c.Description)
	}
}

func TestDirectoryFeed_RefusesACardForAnotherProfile(t *testing.T) {
	f := &directoryFeed{dir: newProfileDirectory(), log: slog.New(slog.NewTextHandler(io.Discard, nil)), live: map[string]bool{}}
	// An envelope from cluster's own profile on its own subject, whose card
	// names another profile: the envelope agrees with the subject, the card
	// doesn't.
	payload, err := json.Marshal(lib.AgentCard{Name: "prod-east"})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewAgentCardEnvelope(lib.Party{Session: "operator", Profile: "cluster"}, "corr-card", payload)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	if f.apply(lib.AgentSubject("cluster"), raw, true) {
		t.Fatal("a card relocated onto another profile's subject was applied")
	}
	if _, ok := f.dir.lookup("cluster"); ok {
		t.Fatal("the relocated card was recorded")
	}
}

func TestDirectoryFeed_RefusesAnEnvelopeFromAnotherProfile(t *testing.T) {
	f := &directoryFeed{dir: newProfileDirectory(), log: slog.New(slog.NewTextHandler(io.Discard, nil)), live: map[string]bool{}}
	// prod-east's own card, relocated onto cluster's subject.
	if f.apply(lib.AgentSubject("cluster"), cardEnvelope(t, lib.AgentCard{Name: "prod-east"}), true) {
		t.Fatal("a card relocated onto another profile's subject was applied")
	}
}

func writePrincipalMap(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(path, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}
