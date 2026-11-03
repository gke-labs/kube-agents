package dispatch

import (
	"testing"
	"time"

	"github.com/nats-io/nats.go"

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
