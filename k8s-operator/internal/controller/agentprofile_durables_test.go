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
		if err := syncDurables(ctx, js, "tse", want, a2aBuiltinTierNames); err != nil {
			t.Fatal(err)
		}
	}
	exists := func(name string) bool {
		_, err := js.Consumer(ctx, a2aProfileSubmitStream, name)
		return err == nil
	}
	sync(desiredProfileDurables(&both))
	for _, d := range desiredProfileDurables(&both) {
		if !exists(d.name) {
			t.Fatalf("%s missing with both tiers", d.name)
		}
	}
	readOnly := testAgentProfile("ns", "tse")
	sync(desiredProfileDurables(&readOnly))
	if exists("profile-tse-unattended-top") || exists("profile-tse-unattended-child") || !exists("profile-tse-read-top") {
		t.Error("dropping unattended didn't leave exactly the read durables")
	}
	sync(nil)
	for _, d := range desiredProfileDurables(&both) {
		if exists(d.name) {
			t.Errorf("%s survived the profile's withdrawal", d.name)
		}
	}
}

// A durable of a tier that is neither read nor unattended (a Tier resource,
// since deleted or dropped) is found by name from the probe tiers and removed, while another
// profile's durables, including one whose name shares a prefix, and a durable
// the operator didn't make, are left alone (#3659).
func TestADroppedCustomTiersDurablesAreFoundAndOthersLeftAlone(t *testing.T) {
	js := jetStreamWithProfileSubmits(t)
	ctx := context.Background()
	nightly := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "nightly"} })
	neighbour := testAgentProfile("ns", "tse-b", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	probe := []string{"read", "nightly"}
	if err := syncDurables(ctx, js, "tse", desiredProfileDurables(&nightly), probe); err != nil {
		t.Fatal(err)
	}
	if err := syncDurables(ctx, js, "tse-b", desiredProfileDurables(&neighbour), probe); err != nil {
		t.Fatal(err)
	}
	// A consumer under this profile's name pattern that the operator didn't make.
	if _, err := js.CreateOrUpdateConsumer(ctx, a2aProfileSubmitStream, jetstream.ConsumerConfig{
		Durable: "profile-tse-hand-top", FilterSubject: "a2a.submit.tse.hand.top.*",
	}); err != nil {
		t.Fatal(err)
	}
	readOnly := testAgentProfile("ns", "tse")
	if err := syncDurables(ctx, js, "tse", desiredProfileDurables(&readOnly), probe); err != nil {
		t.Fatal(err)
	}
	exists := func(name string) bool { _, err := js.Consumer(ctx, a2aProfileSubmitStream, name); return err == nil }
	if exists("profile-tse-nightly-top") || exists("profile-tse-nightly-child") {
		t.Error("the dropped custom tier's durables survived")
	}
	if !exists("profile-tse-read-top") || !exists("profile-tse-b-read-top") || !exists("profile-tse-hand-top") {
		t.Error("a kept, a neighbour's or an unmarked durable was deleted")
	}
	for name, want := range map[string]bool{
		"profile-tse-read-top": true, "profile-tse-nightly-child": true, "profile-tse-b-read-top": false,
		"profile-tse-read": false, "profile-tse-read-top-x": false, "profile-tse-ni-ght-top": false,
	} {
		if got := profileDurableOf("tse", name); got != want {
			t.Errorf("profileDurableOf(tse, %s) = %v, want %v", name, got, want)
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
	sync := func(want []profileDurable) error { return syncDurables(ctx, js, "tse", want, a2aBuiltinTierNames) }
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

var a2aBuiltinTierNames = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}

// The operator's durable sync works under the operator's own bus grants
// (operatorIdentity, with a dispatcher): consumer create, info and delete on
// PROFILE_SUBMITS and nothing more. A sync that listed the stream's consumers
// (stream info, consumer names) was refused on a live install and held every
// profile's card and finalizer (#3659, 2b-i's live run).
func TestTheDurableSyncNeedsOnlyTheOperatorsGrants(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aDispatcherImageEnvVar, "dispatcher:test")
	id, ok := operatorIdentity()
	if !ok {
		t.Fatal("no operator identity")
	}
	s, err := natsserver.NewServer(&natsserver.Options{Port: -1, JetStream: true, StoreDir: t.TempDir(),
		Users: []*natsserver.User{
			{Username: "admin", Password: "admin"},
			{Username: "operator", Password: "operator", Permissions: &natsserver.Permissions{
				Publish:   &natsserver.SubjectPermission{Allow: id.publish},
				Subscribe: &natsserver.SubjectPermission{Allow: []string{"_INBOX.>"}},
			}},
		}})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	ctx := context.Background()
	admin, err := nats.Connect(s.ClientURL(), nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(admin.Close)
	ajs, _ := jetstream.New(admin)
	if _, err := ajs.CreateStream(ctx, jetstream.StreamConfig{Name: a2aProfileSubmitStream, Subjects: []string{a2aProfileSubmitPrefix + ">"}}); err != nil {
		t.Fatal(err)
	}
	op, err := nats.Connect(s.ClientURL(), nats.UserInfo("operator", "operator"), nats.CustomInboxPrefix("_INBOX."+a2aOperatorBusUser))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(op.Close)
	ojs, _ := jetstream.New(op)
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	// The grants refuse a list, which is what the live install hit: the
	// sync below must not need one.
	probeCtx, probeCancel := context.WithTimeout(context.Background(), 2*time.Second)
	_, err = ojs.Stream(probeCtx, a2aProfileSubmitStream)
	probeCancel()
	if err == nil {
		t.Fatal("the operator's grants allow stream info; this test no longer guards the list")
	}
	nightly := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "nightly"} })
	probe := []string{"read", "unattended", "nightly"}
	if err := syncDurables(ctx, ojs, "tse", desiredProfileDurables(&nightly), probe); err != nil {
		t.Fatalf("create under the operator's grants: %v", err)
	}
	readOnly := testAgentProfile("ns", "tse")
	if err := syncDurables(ctx, ojs, "tse", desiredProfileDurables(&readOnly), probe); err != nil {
		t.Fatalf("drop a tier under the operator's grants: %v", err)
	}
	if _, err := ajs.Consumer(ctx, a2aProfileSubmitStream, "profile-tse-nightly-top"); err == nil {
		t.Error("the dropped tier's durable survived")
	}
	if err := syncDurables(ctx, ojs, "tse", nil, probe); err != nil {
		t.Fatalf("withdraw under the operator's grants: %v", err)
	}
}
