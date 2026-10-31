package dispatch

import (
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Each admission refusal ends the task with a visible `rejected` terminal
// carrying its reason code, and terminates the submission: it is never
// offered and never redelivered.
func TestAdmissionRefusalsAreVisibleAndTerminal(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 4, 0, "read", "unattended")
	w := b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	ti := oneProfile("tse", 4, "tse-0")
	ti.update(func(c *lib.DispatchConfig) {
		c.Profiles[0] = testProfile("tse", 4, "read", "unattended")
		c.Profiles[0].Parameters = []lib.ProfileParameter{{Name: "repo", Required: true}, {Name: "note"}}
		c.Profiles[0].QueueTimeoutSeconds = 2
	})

	// Queued before the dispatcher starts, and older than the timeout by
	// the time it does.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-stale", "cap-ok", map[string]string{"repo": "r"})
	time.Sleep(2500 * time.Millisecond)
	v := &fakeVerifier{refuse: map[string]bool{"cap-bad": true}}
	b.runDispatcher(testConfig(), ti, v)
	// The profile drops unattended from its tiers while the operator still
	// lists the durable (the config moved under a bound durable).
	waitFor(t, 10*time.Second, "the stale task refused", func() bool { return len(b.terminalsOf("task-stale")) == 1 })
	ti.update(func(c *lib.DispatchConfig) { c.Profiles[0].Tiers = []string{"read"} })

	b.submitWith("tse", "unattended", lib.SubmitKindTop, "task-tier", "cap-ok", map[string]string{"repo": "r"})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-refused", "cap-bad", map[string]string{"repo": "r"})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-nocap", "", map[string]string{"repo": "r"})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-missing", "cap-ok", map[string]string{"note": "x"})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-partial", "cap-ok", map[string]string{"repo": "r", "project": "p", "cluster": "c"})
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-noparams", "cap-ok", nil)

	want := map[string]string{
		"task-stale":    ReasonQueueTimeout,
		"task-tier":     ReasonTierNotAllowed,
		"task-refused":  ReasonVerifierRefused,
		"task-nocap":    ReasonVerifierRefused,
		"task-missing":  ReasonParametersInvalid,
		"task-partial":  ReasonParametersInvalid,
		"task-noparams": ReasonParametersInvalid,
	}
	for id, reason := range want {
		ts := b.waitTerminal(id, 10*time.Second)
		if ts.state != lib.StateRejected || !hasReason(ts, reason) || ts.from != selfSession || ts.class != lib.TaskClassSupervisor {
			t.Errorf("%s: terminal %+v, want the dispatcher's rejected %s", id, ts, reason)
		}
	}
	time.Sleep(time.Second)
	for id := range want {
		if n := len(b.terminalsOf(id)); n != 1 {
			t.Errorf("%s: %d terminals, want 1 (a refused submission came back)", id, n)
		}
	}
	if n := w.offerCount(); n != 0 {
		t.Errorf("a refused task was offered (%d offers)", n)
	}
	for _, d := range []string{lib.ProfileDurableName("tse", "read", lib.SubmitKindTop), lib.ProfileDurableName("tse", "unattended", lib.SubmitKindTop)} {
		if ack, num := b.pending(d); ack != 0 || num != 0 {
			t.Errorf("%s: %d held, %d queued after every submission was refused", d, ack, num)
		}
	}
	// A task with every required parameter and no cluster target is asked
	// about the install's namespace.
	b.submitWith("tse", "read", lib.SubmitKindTop, "task-ok", "cap-ok", map[string]string{"repo": "r"})
	if ts := b.waitTerminal("task-ok", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("the admissible task: %+v", ts)
	}
	found := false
	for _, c := range v.callList() {
		if c == "cap-ok|tse|read|namespace/kube-agents" {
			found = true
		}
	}
	if !found {
		t.Errorf("verifier calls %v, want one for namespace/kube-agents", v.callList())
	}
}
