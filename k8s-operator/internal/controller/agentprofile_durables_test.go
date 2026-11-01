package controller

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// jetStreamWithProfileSubmits starts an embedded JetStream server holding a
// stream shaped like the provisioned PROFILE_SUBMITS (every a2a.submit subject).
func jetStreamWithProfileSubmits(t *testing.T) jetstream.JetStream {
	t.Helper()
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1, JetStream: true, StoreDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateStream(context.Background(), jetstream.StreamConfig{Name: a2aProfileSubmitStream, Subjects: []string{a2aProfileSubmitPrefix + ">"}}); err != nil {
		t.Fatal(err)
	}
	return js
}

// The durables a profile gets are one per (allowed tier, kind), with the
// held marker, an explicit-ack pull consumer the dispatcher's SubscribeHeld
// takes, filtered to that kind's submissions and capped at the profile's
// concurrency.
func TestEnsureDurableMakesWhatTheDispatcherBinds(t *testing.T) {
	js := jetStreamWithProfileSubmits(t)
	ctx := context.Background()
	p := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.Concurrency = 3 })
	want := desiredProfileDurables(&p)
	if len(want) != 2 {
		t.Fatalf("durables = %+v, want read top and child", want)
	}
	for _, d := range want {
		if err := ensureDurable(ctx, js, d); err != nil {
			t.Fatalf("ensure %s: %v", d.name, err)
		}
		c, err := js.Consumer(ctx, a2aProfileSubmitStream, d.name)
		if err != nil {
			t.Fatal(err)
		}
		cfg := c.CachedInfo().Config
		if cfg.FilterSubject != d.filter || cfg.AckPolicy != jetstream.AckExplicitPolicy || cfg.MaxAckPending != 3 ||
			cfg.AckWait != a2aHeldAckWait || cfg.Metadata[a2aHeldMarkerKey] != a2aHeldMarkerValue || cfg.Durable != d.name {
			t.Errorf("%s config = %+v", d.name, cfg)
		}
	}
	// A concurrency change moves only the cap.
	p.Spec.Concurrency = 5
	d := desiredProfileDurables(&p)[0]
	if err := ensureDurable(ctx, js, d); err != nil {
		t.Fatal(err)
	}
	c, _ := js.Consumer(ctx, a2aProfileSubmitStream, d.name)
	if got := c.CachedInfo().Config.MaxAckPending; got != 5 {
		t.Errorf("MaxAckPending after the change = %d, want 5", got)
	}
}

// A consumer under a durable's name that this operator didn't make is refused
// and never deleted; one it made whose filter or AckWait has moved is refused
// rather than moved under a bound dispatcher, and is deleted when its profile
// or tier goes (it carries the mark).
func TestEnsureDurableRefusesWhatItDidntMakeAndDeletesOnlyItsOwn(t *testing.T) {
	js := jetStreamWithProfileSubmits(t)
	ctx := context.Background()
	p := testAgentProfile("ns", "tse")
	d := desiredProfileDurables(&p)[0]
	// Someone else's consumer under the name, with a wider filter.
	if _, err := js.CreateOrUpdateConsumer(ctx, a2aProfileSubmitStream, jetstream.ConsumerConfig{Durable: d.name, FilterSubject: a2aProfileSubmitPrefix + ">", AckPolicy: jetstream.AckExplicitPolicy}); err != nil {
		t.Fatal(err)
	}
	if err := ensureDurable(ctx, js, d); err == nil || !strings.Contains(err.Error(), "held-delivery mark") {
		t.Errorf("ensure over a foreign consumer = %v, want a refusal naming the mark", err)
	}
	if err := deleteDurable(ctx, js, d.name); err != nil {
		t.Fatal(err)
	}
	if _, err := js.Consumer(ctx, a2aProfileSubmitStream, d.name); err != nil {
		t.Errorf("a consumer the operator didn't make was deleted: %v", err)
	}
	// Ours, but with a filter that moved: refused, not moved.
	_ = js.DeleteConsumer(ctx, a2aProfileSubmitStream, d.name)
	if _, err := js.CreateOrUpdateConsumer(ctx, a2aProfileSubmitStream, jetstream.ConsumerConfig{
		Durable: d.name, FilterSubject: profileDurableFilter("tse", "read", "child"), AckPolicy: jetstream.AckExplicitPolicy,
		AckWait: a2aHeldAckWait, Metadata: map[string]string{a2aHeldMarkerKey: a2aHeldMarkerValue},
	}); err != nil {
		t.Fatal(err)
	}
	if err := ensureDurable(ctx, js, d); err == nil || !strings.Contains(err.Error(), "refusing to move") {
		t.Errorf("ensure over a moved filter = %v, want a refusal", err)
	}
	// And ours is deleted.
	if err := deleteDurable(ctx, js, d.name); err != nil {
		t.Fatal(err)
	}
	if _, err := js.Consumer(ctx, a2aProfileSubmitStream, d.name); err == nil {
		t.Error("the operator's own durable survived its delete")
	}
}

// Dropping a tier deletes that tier's durables and keeps the others; a
// withdrawn profile loses them all.
func TestSyncingAProfilesDurablesDropsATiersQueue(t *testing.T) {
	js := jetStreamWithProfileSubmits(t)
	ctx := context.Background()
	both := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	sync := func(want []profileDurable) {
		t.Helper()
		if err := syncDurables(ctx, js, "tse", want); err != nil {
			t.Fatal(err)
		}
	}
	exists := func(name string) bool {
		_, err := js.Consumer(ctx, a2aProfileSubmitStream, name)
		return err == nil
	}
	sync(desiredProfileDurables(&both))
	for _, name := range allProfileDurableNames("tse") {
		if !exists(name) {
			t.Fatalf("%s missing with both tiers", name)
		}
	}
	readOnly := testAgentProfile("ns", "tse")
	sync(desiredProfileDurables(&readOnly))
	if exists("profile-tse-unattended-top") || exists("profile-tse-unattended-child") || !exists("profile-tse-read-top") {
		t.Error("dropping unattended didn't leave exactly the read durables")
	}
	sync(nil)
	for _, name := range allProfileDurableNames("tse") {
		if exists(name) {
			t.Errorf("%s survived the profile's withdrawal", name)
		}
	}
}

// On a stream with no consumer slot left, the durable's refusal says why,
// rather than a bare server error.
func TestADurableThatDoesntFitSaysWhy(t *testing.T) {
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1, JetStream: true, StoreDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, _ := jetstream.New(nc)
	ctx := context.Background()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: a2aProfileSubmitStream, Subjects: []string{a2aProfileSubmitPrefix + ">"}, MaxConsumers: 1}); err != nil {
		t.Fatal(err)
	}
	p := testAgentProfile("ns", "tse")
	want := desiredProfileDurables(&p)
	if err := ensureDurable(ctx, js, want[0]); err != nil {
		t.Fatalf("the first durable: %v", err)
	}
	if err := ensureDurable(ctx, js, want[1]); err == nil || !strings.Contains(err.Error(), "no consumer slot left") || !strings.Contains(err.Error(), "16 profiles") {
		t.Errorf("a durable past max_consumers = %v, want the slot explanation", err)
	}
}

// A consumer with the held mark and the right filter, AckWait and cap is
// still not adopted when it carries a setting the operator never sets: the
// mark is a public constant.
func TestEnsureDurableRefusesAForgedMark(t *testing.T) {
	js := jetStreamWithProfileSubmits(t)
	ctx := context.Background()
	p := testAgentProfile("ns", "tse")
	d := desiredProfileDurables(&p)[0]
	if _, err := js.CreateOrUpdateConsumer(ctx, a2aProfileSubmitStream, jetstream.ConsumerConfig{
		Durable: d.name, FilterSubject: d.filter, AckPolicy: jetstream.AckExplicitPolicy, MaxAckPending: d.maxAckPending,
		AckWait: a2aHeldAckWait, Metadata: map[string]string{a2aHeldMarkerKey: a2aHeldMarkerValue}, MaxDeliver: 1,
	}); err != nil {
		t.Fatal(err)
	}
	if err := ensureDurable(ctx, js, d); err == nil || !strings.Contains(err.Error(), "stops redelivering") {
		t.Errorf("ensure over a forged-mark consumer = %v, want it refused", err)
	}
}

// A tier swap on a stream with no spare consumer slot completes: the
// dropped tier's durables are deleted before the new tier's are made.
func TestATierSwapFitsOnAFullStream(t *testing.T) {
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1, JetStream: true, StoreDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, _ := jetstream.New(nc)
	ctx := context.Background()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: a2aProfileSubmitStream, Subjects: []string{a2aProfileSubmitPrefix + ">"}, MaxConsumers: 2}); err != nil {
		t.Fatal(err)
	}
	sync := func(want []profileDurable) error { return syncDurables(ctx, js, "tse", want) }
	read := testAgentProfile("ns", "tse")
	if err := sync(desiredProfileDurables(&read)); err != nil {
		t.Fatalf("read tier: %v", err)
	}
	unattended := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	})
	if err := sync(desiredProfileDurables(&unattended)); err != nil {
		t.Fatalf("swapping read for unattended on a full stream: %v", err)
	}
}

// heldDurableFixturePath is the durable config the operator makes and the
// worker pool labels it will put on worker pods, which lib reads to hold its
// constants equal to these copies (this module can't import lib).
const heldDurableFixturePath = "../../../a2a/lib/testdata/rendered-held-durable.json"

func TestRenderedHeldDurableFixture(t *testing.T) {
	p := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.Concurrency = 3 })
	d := desiredProfileDurables(&p)[0]
	doc := map[string]any{
		"stream":        a2aProfileSubmitStream,
		"durable":       d.name,
		"filter":        d.filter,
		"ackPolicy":     "explicit",
		"ackWait":       a2aHeldAckWait.String(),
		"maxAckPending": d.maxAckPending,
		"metadata":      map[string]string{a2aHeldMarkerKey: a2aHeldMarkerValue},
		"poolLabels": map[string]string{
			a2aComponentLabel:     agentProfileWorkerComponent,
			agentProfileNameLabel: "tse",
			agentProfileTierLabel: "read",
		},
	}
	body, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, heldDurableFixturePath, string(body)+"\n")
}
