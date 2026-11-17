package dispatch

import (
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The dispatcher's terminal is read by the gateway's relay and its tasks/get
// replay, both under the gateway's agreement policy, which refuses a
// `.supervisor` writer it does not name. The terminal as published must pass
// that policy, or the gateway drops it as a protocol error and the task never
// ends there.
func TestTheDispatchersTerminalPassesTheGatewaysAgreement(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	got := make(chan *nats.Msg, 4)
	sub, err := b.admin.ChanSubscribe("a2a.tasks.*.*."+lib.TaskClassSupervisor, got)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	b.runDispatcher(testConfig(), oneProfile("tse", 2, "tse-0"), &fakeVerifier{})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-refused", "", map[string]string{})
	select {
	case msg := <-got:
		env, err := lib.ParseEnvelope(msg.Data)
		if err != nil {
			t.Fatal(err)
		}
		if err := lib.CheckSubjectAgreement(msg.Subject, env, gateway.SupervisorAgreement(nil)); err != nil {
			t.Errorf("the gateway refuses the dispatcher's terminal: %v", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("no terminal on .supervisor")
	}
}

// A task id is unique only within its (profile, tier, kind), so the
// terminal's envelope id, which the TASKS stream deduplicates on, is keyed
// by all four: two profiles refusing the same task id each store their
// terminal, and a repeat publish of one (a redelivery after a lost ack) is
// still dropped by the duplicate window.
func TestTwoProfilesSharingATaskIDEachStoreTheirTerminal(t *testing.T) {
	b := newBus(t, "tse-0", "tse2-0")
	b.ensure("tse", 2, 0, "read")
	b.ensure("tse2", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	ti.pool.Set("tse2", "read", "tse2-0")
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	// No capability key: each is refused at admission with a terminal.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-1", "", map[string]string{})
	b.submitWith("tse2", "read", lib.SubmitKindTop, "task-1", "", map[string]string{})
	stored := func(profile string) uint64 {
		t.Helper()
		info, err := b.js.Stream(b.ctx, "TASKS")
		if err != nil {
			t.Fatal(err)
		}
		si, err := info.Info(b.ctx, jetstream.WithSubjectFilter(lib.TaskSupervisorSubject(profile, "task-1")))
		if err != nil {
			t.Fatal(err)
		}
		return si.State.Subjects[lib.TaskSupervisorSubject(profile, "task-1")]
	}
	waitFor(t, 15*time.Second, "both profiles' terminals stored", func() bool { return stored("tse") == 1 && stored("tse2") == 1 })
	hk := holdKey{"tse", "read", lib.SubmitKindTop, "task-1"}
	sub := submissionEnv(t, "tse", "task-1", "", map[string]string{})
	if err := d.publishTerminal(b.ctx, hk, sub, lib.StateRejected, ReasonVerifierRefused, ""); err != nil {
		t.Fatal(err)
	}
	if n := stored("tse"); n != 1 {
		t.Errorf("a repeat terminal for the same task was stored again: %d stored", n)
	}
}

// Under the operator's switch (DispatchConfig.ProfileSupervisorForm, #5301)
// the terminal goes on the profile's six-token supervisor subject, and the
// gateway's agreement still takes it; without the switch it stays on the
// five-token one.
func TestTheTerminalTakesTheConfiguredSupervisorForm(t *testing.T) {
	for _, tc := range []struct {
		form, want string
	}{
		{"", lib.TaskSupervisorSubject("tse", "task-form")},
		{lib.ProfileSupervisorFormProfile, lib.ProfileSupervisorSubject("tse", "task-form")},
	} {
		t.Run("form="+tc.form, func(t *testing.T) {
			b := newBus(t, "tse-0")
			b.ensure("tse", 2, 0, "read")
			got := make(chan *nats.Msg, 4)
			sub, err := b.admin.ChanSubscribe("a2a.tasks.>", got)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = sub.Unsubscribe() })
			ti := oneProfile("tse", 2, "tse-0")
			ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = tc.form })
			b.runDispatcher(testConfig(), ti, &fakeVerifier{})
			b.submitWith("tse", "read", lib.SubmitKindTop, "task-form", "", map[string]string{})
			deadline := time.After(15 * time.Second)
			for {
				select {
				case msg := <-got:
					if !lib.IsTaskSupervisorSubject(msg.Subject, "tse", "task-form") {
						continue
					}
					if msg.Subject != tc.want {
						t.Fatalf("terminal on %q, want %q", msg.Subject, tc.want)
					}
					env, err := lib.ParseEnvelope(msg.Data)
					if err != nil {
						t.Fatal(err)
					}
					if err := lib.CheckSubjectAgreement(msg.Subject, env, gateway.SupervisorAgreement(nil)); err != nil {
						t.Errorf("the gateway refuses the dispatcher's terminal on %q: %v", msg.Subject, err)
					}
					return
				case <-deadline:
					t.Fatal("no terminal on a supervisor subject")
				}
			}
		})
	}
}

// A dispatcher switched to the six-token form whose connection is refused it
// (a grant that hasn't reached it, or a partial rollback, #5301) writes the
// terminal on the five-token form instead, which readers also watch, and
// counts it, rather than retrying without end while the task holds its slot.
func TestARefusedSixTokenTerminalFallsBackToTheFiveTokenForm(t *testing.T) {
	b := newBusWith(t, []*natsserver.User{{Username: "disp-nosix", Password: dispPass, Permissions: &natsserver.Permissions{
		Publish: &natsserver.SubjectPermission{Allow: []string{">"}, Deny: []string{"a2a.tasks." + lib.TaskProfileSupervisorToken + ".>"}}}}}, "tse-0")
	b.ensure("tse", 2, 0, "read")
	got := make(chan *nats.Msg, 8)
	sub, err := b.admin.ChanSubscribe("a2a.tasks.>", got)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	var g grantSwitch
	g.set("disp-nosix")
	ti := oneProfile("tse", 2, "tse-0")
	ti.update(func(c *lib.DispatchConfig) { c.ProfileSupervisorForm = lib.ProfileSupervisorFormProfile })
	d, _ := b.runPrepared(b.connectAs(&g), testConfig(), ti, &fakeVerifier{}, nil)
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-fallback", "", map[string]string{})
	deadline := time.After(30 * time.Second)
	for {
		select {
		case msg := <-got:
			if !lib.IsTaskSupervisorSubject(msg.Subject, "tse", "task-fallback") {
				continue
			}
			if msg.Subject != lib.TaskSupervisorSubject("tse", "task-fallback") {
				t.Fatalf("terminal on %q, want the five-token fallback", msg.Subject)
			}
			if n := d.Health().SupervisorFallbacks; n < 1 {
				t.Errorf("SupervisorFallbacks = %d, want the fallback counted", n)
			}
			return
		case <-deadline:
			t.Fatal("no terminal: the refused six-token write was not fallen back from")
		}
	}
}
