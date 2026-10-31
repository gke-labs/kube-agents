package dispatch

import (
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The callout fixes the dispatcher's grants per connection, enumerating the
// durables and the profiles' `.supervisor` in the config it rendered against.
// Modelled here with two users: "disp-old", which may not touch the
// durables of profile `tse2` nor publish on its `.supervisor`, and "disp-new",
// which may. The dispatcher's connection authenticates as
// whichever the test says, read at each dial, as a fresh token would be.

const dispPass = "disp-pw"

func dispatcherUsers() []*natsserver.User {
	var deny []string
	for _, kind := range submitKinds {
		name := lib.ProfileDurableName("tse2", "read", kind)
		deny = append(deny, "$JS.API.CONSUMER.INFO."+lib.ProfileSubmitStream+"."+name,
			"$JS.API.CONSUMER.MSG.NEXT."+lib.ProfileSubmitStream+"."+name, "$JS.ACK."+lib.ProfileSubmitStream+"."+name+".>")
	}
	deny = append(deny, "a2a.tasks.tse2.*.supervisor")
	return []*natsserver.User{
		{Username: "disp-old", Password: dispPass, Permissions: &natsserver.Permissions{
			Publish: &natsserver.SubjectPermission{Allow: []string{">"}, Deny: deny}}},
		{Username: "disp-new", Password: dispPass},
	}
}

// grantSwitch is the identity the next dial takes.
type grantSwitch struct{ user atomic.Value }

func (g *grantSwitch) set(u string) { g.user.Store(u) }

func (b *bus) connectAs(g *grantSwitch) *lib.Client {
	b.t.Helper()
	c, err := lib.Connect(b.ctx, b.url, lib.WithName("dispatcher"), lib.WithLogger(quietLog()),
		lib.WithNATSOptions(nats.UserInfoHandler(func() (string, string) { return g.user.Load().(string), dispPass })))
	if err != nil {
		b.t.Fatal(err)
	}
	b.t.Cleanup(c.Close)
	return c
}

func fastRegrant() Config {
	c := testConfig()
	c.RegrantBackoff = 500 * time.Millisecond
	c.BindAttemptTimeout = time.Second
	c.ReconnectTimeout = 10 * time.Second
	return c
}

// A config version adding a durable costs exactly one reconnect, and the
// new profile's tasks run on it; a task held across the reconnect stays
// held (never redelivered, never offered again) and finishes. A version
// removing the durable costs none.
func TestAnAddedDurableReconnectsOnceAndKeepsHeldWork(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse-0", "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	w := b.worker("tse", "read", "tse-0", answerAccept, 8*time.Second)
	w2 := b.worker("tse2", "read", "tse2-0", answerAccept, 500*time.Millisecond)
	var g grantSwitch
	g.set("disp-old")
	ti := oneProfile("tse", 2, "tse-0")
	d, _ := b.runDispatcherOn(b.connectAs(&g), fastRegrant(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "task-1 handed out", func() bool { return w.offerCount() == 1 })
	time.Sleep(time.Second)

	// The operator renders tse2, and the callout's map with it.
	g.set("disp-new")
	ti.pool.Set("tse2", "read", "tse2-0")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 15*time.Second, "the reconnect", func() bool { return d.Reconnects() == 1 })
	b.submit("tse2", "read", lib.SubmitKindTop, "task-2")
	if ts := b.waitTerminal("task-2", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse2-0" {
		t.Errorf("task-2 on the added profile: %+v", ts)
	}
	// The dispatcher's own `.supervisor` publish for the new profile rides the
	// new grants too: a refusal's terminal lands.
	b.submitWith("tse2", "read", lib.SubmitKindTop, "task-refused", "", map[string]string{})
	if ts := b.waitTerminal("task-refused", 15*time.Second); ts.state != lib.StateRejected || ts.from != selfSession || ts.class != lib.TaskClassSupervisor {
		t.Errorf("the dispatcher's terminal on the added profile: %+v", ts)
	}
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted || ts.from != "tse-0" {
		t.Errorf("task-1 held across the reconnect: %+v", ts)
	}
	if n := w.offerCount(); n != 1 {
		t.Errorf("task-1 offered %d times, want 1", n)
	}
	if w2.offerCount() != 1 {
		t.Errorf("task-2 offered %d times", w2.offerCount())
	}
	cons, err := b.js.Consumer(b.ctx, lib.ProfileSubmitStream, lib.ProfileDurableName("tse", "read", lib.SubmitKindTop))
	if err != nil {
		t.Fatal(err)
	}
	if info, err := cons.Info(b.ctx); err != nil || info.NumRedelivered != 0 {
		t.Errorf("redelivered %d across the reconnect (%v), want 0", info.NumRedelivered, err)
	}

	// Removing tse2 needs no reconnect.
	g.set("disp-old")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v3"; c.Profiles = c.Profiles[:1] })
	time.Sleep(time.Second) // several refreshes
	if n := d.Reconnects(); n != 1 {
		t.Errorf("%d reconnects after a removal, want still 1", n)
	}
	// Adding it back needs one again: its grant went with it.
	g.set("disp-new")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v4"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 15*time.Second, "the second reconnect", func() bool { return d.Reconnects() == 2 })
	b.submit("tse2", "read", lib.SubmitKindTop, "task-3")
	if ts := b.waitTerminal("task-3", 15*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-3 after the re-add: %+v", ts)
	}
}

// The callout's watch lags the config: the first reconnect still carries
// the old grants and the added durable's bind is refused. The dispatcher
// reconnects again (bounded, with backoff) and the bind succeeds once the
// grants catch up.
func TestARefusedBindAfterReconnectIsRetriedUntilGranted(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	b.worker("tse2", "read", "tse2-0", answerAccept, 300*time.Millisecond)
	var g grantSwitch
	g.set("disp-old")
	ti := oneProfile("tse", 2)
	d, _ := b.runDispatcherOn(b.connectAs(&g), fastRegrant(), ti, &fakeVerifier{})
	time.Sleep(500 * time.Millisecond)

	ti.pool.Set("tse2", "read", "tse2-0")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 15*time.Second, "a first reconnect, still on the old grants", func() bool { return d.Reconnects() >= 1 })
	time.Sleep(1500 * time.Millisecond)
	g.set("disp-new") // the callout catches up
	b.submit("tse2", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 30*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-1: %+v", ts)
	}
	if n := d.Reconnects(); n < 2 || n > int64(fastRegrant().RegrantAttempts) {
		t.Errorf("%d reconnects, want more than one and at most the bound", n)
	}
}

// A durable the config lists that the bus doesn't have yet (the operator
// creates durables asynchronously) is "not yet": the bind is retried with
// backoff, nothing fails, and the queued submission runs once it appears.
func TestADurableNotCreatedYetIsBoundWhenItAppears(t *testing.T) {
	b := newBus(t, "tse-0")
	b.worker("tse", "read", "tse-0", answerAccept, 300*time.Millisecond)
	d, _ := b.runDispatcher(fastRegrant(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	time.Sleep(2 * time.Second)
	if ts := b.terminalsOf("task-1"); len(ts) != 0 {
		t.Fatalf("terminal before the durable existed: %+v", ts)
	}
	b.ensure("tse", 2, 0, "read")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-1: %+v", ts)
	}
	if n := d.Reconnects(); n != 0 {
		t.Errorf("%d reconnects for a durable that was merely late, want 0", n)
	}
}

// A profile added with grants in place but its durable not created yet
// costs one reconnect (the bus answers "no such consumer", so the grant is
// there), not a round of them; the durable binds when it appears.
func TestAnAddedProfileWithALateDurableReconnectsOnce(t *testing.T) {
	b := newBusWith(t, dispatcherUsers(), "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.worker("tse2", "read", "tse2-0", answerAccept, 300*time.Millisecond)
	var g grantSwitch
	g.set("disp-new")
	ti := oneProfile("tse", 2)
	d, _ := b.runDispatcherOn(b.connectAs(&g), fastRegrant(), ti, &fakeVerifier{})
	time.Sleep(500 * time.Millisecond)
	ti.pool.Set("tse2", "read", "tse2-0")
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 15*time.Second, "the reconnect", func() bool { return d.Reconnects() == 1 })
	time.Sleep(2 * time.Second)
	b.ensure("tse2", 2, 0, "read")
	b.submit("tse2", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 15*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("task-1: %+v", ts)
	}
	if n := d.Reconnects(); n != 1 {
		t.Errorf("%d reconnects, want 1", n)
	}
}

// A durable the grants never cover costs a bounded round of reconnects,
// then a loud log and a cooldown: never a reconnect loop.
func TestANeverGrantedDurableStopsReconnecting(t *testing.T) {
	b := newBusWith(t, dispatcherUsers())
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	var g grantSwitch
	g.set("disp-old")
	cfg := fastRegrant()
	cfg.RegrantAttempts = 3
	ti := oneProfile("tse", 2)
	d, _ := b.runDispatcherOn(b.connectAs(&g), cfg, ti, &fakeVerifier{})
	time.Sleep(500 * time.Millisecond)
	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	waitFor(t, 20*time.Second, "the bounded round", func() bool { return d.Reconnects() == 3 })
	time.Sleep(4 * time.Second) // many refreshes
	if n := d.Reconnects(); n != 3 {
		t.Errorf("%d reconnects, want the round of 3 and then the cooldown", n)
	}
}

// A tier added to a profile already granted is a new durable: one
// reconnect. Removed, none; added back, one more, since its grant went
// with it.
func TestAnAddedTierReconnects(t *testing.T) {
	b := newBus(t)
	b.ensure("tse", 2, 0, "read", "unattended")
	ti := oneProfile("tse", 2)
	d, _ := b.runDispatcher(fastRegrant(), ti, &fakeVerifier{})
	time.Sleep(500 * time.Millisecond)
	withTiers := func(version string, tiers ...string) {
		ti.update(func(c *lib.DispatchConfig) { c.Version = version; c.Profiles[0] = testProfile("tse", 2, tiers...) })
	}
	withTiers("v2", "read", "unattended")
	waitFor(t, 15*time.Second, "the reconnect", func() bool { return d.Reconnects() == 1 })
	withTiers("v3", "read")
	time.Sleep(time.Second)
	if n := d.Reconnects(); n != 1 {
		t.Fatalf("%d reconnects after dropping a tier, want 1", n)
	}
	withTiers("v4", "read", "unattended")
	waitFor(t, 15*time.Second, "the second reconnect", func() bool { return d.Reconnects() == 2 })
}
