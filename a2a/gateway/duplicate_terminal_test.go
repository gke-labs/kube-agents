package gateway

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// dispatcherTerminals is n copies of the dispatcher's failed terminal for
// taskID: one payload, each under its own envelope id, as two retry loops of
// the dispatcher write it (#6883).
func dispatcherTerminals(t *testing.T, taskID string, n int) []*lib.Envelope {
	t.Helper()
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{Role: "agent", MessageID: "m-" + taskID,
			Parts: []lib.Part{{Kind: "text", Text: "reason: lease-expired"}}}}, Final: true})
	if err != nil {
		t.Fatal(err)
	}
	var out []*lib.Envelope
	for range n {
		env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: lib.DispatcherSession, AgentType: "a2a-dispatcher"},
			taskID, "ctx-"+taskID, "corr-"+taskID, payload)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, env)
	}
	return out
}

// A dispatcher that restarts between its terminal's publish and its settle
// writes the terminal again under a new envelope id (#6883). On the relay the
// second identical final status on the six-token subject changes nothing: the
// task ends once, with the first.
func TestASecondIdenticalSixTokenTerminalChangesNothingOnTheRelay(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	conv := "discord:cut/duplicate"
	r.putRecord(t, cutoffRecord(conv, cutoffProfileAddressee, cutoffProfileTask, true))
	subject := lib.ProfileSupervisorSubject(cutoffProfileAddressee, cutoffProfileTask)
	envs := dispatcherTerminals(t, cutoffProfileTask, 2)
	d1 := &stampedDelivery{fakeDelivery: fakeDelivery{env: envs[0], subj: subject, seq: 701}, at: time.Now()}
	r.g.relayBatch(conv, []relayItem{{env: envs[0], subject: subject, d: d1}})
	waitFor(t, "the first terminal", func() bool { return len(r.terminalsFor(cutoffProfileTask)) == 1 })

	d2 := &stampedDelivery{fakeDelivery: fakeDelivery{env: envs[1], subj: subject, seq: 702}, at: time.Now()}
	r.g.relayBatch(conv, []relayItem{{env: envs[1], subject: subject, d: d2}})
	time.Sleep(300 * time.Millisecond)
	if got := r.terminalsFor(cutoffProfileTask); len(got) != 1 || got[0].state != lib.StateFailed || got[0].source != TerminalFromSupervisor {
		t.Fatalf("terminals after a second identical one = %+v, want the first alone", got)
	}
	if d2.acks != 1 {
		t.Errorf("the second terminal was acked %d times, want settled once", d2.acks)
	}
}

// The same with both terminals in one relay batch: the task ends once.
func TestTwoIdenticalSixTokenTerminalsInOneBatchEndTheTaskOnce(t *testing.T) {
	t.Parallel()
	r := startCutoffRig(t, time.Now().Add(-time.Minute))
	conv := "discord:cut/duplicate-batch"
	r.putRecord(t, cutoffRecord(conv, cutoffProfileAddressee, cutoffProfileTask, true))
	subject := lib.ProfileSupervisorSubject(cutoffProfileAddressee, cutoffProfileTask)
	envs := dispatcherTerminals(t, cutoffProfileTask, 2)
	d1 := &stampedDelivery{fakeDelivery: fakeDelivery{env: envs[0], subj: subject, seq: 801}, at: time.Now()}
	d2 := &stampedDelivery{fakeDelivery: fakeDelivery{env: envs[1], subj: subject, seq: 802}, at: time.Now()}
	r.g.relayBatch(conv, []relayItem{{env: envs[0], subject: subject, d: d1}, {env: envs[1], subject: subject, d: d2}})
	waitFor(t, "the terminal", func() bool { return len(r.terminalsFor(cutoffProfileTask)) >= 1 })
	time.Sleep(300 * time.Millisecond)
	if got := r.terminalsFor(cutoffProfileTask); len(got) != 1 {
		t.Fatalf("terminals from one batch of two = %+v, want one", got)
	}
	if d1.acks != 1 || d2.acks != 1 {
		t.Errorf("acks %d and %d, want each settled once", d1.acks, d2.acks)
	}
}
