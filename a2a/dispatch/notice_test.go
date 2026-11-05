package dispatch

import (
	"testing"
	"time"

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
