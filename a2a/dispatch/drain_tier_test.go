package dispatch

import (
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A submission queued at a tier its profile now drains (#3659, 2b-ii) is
// refused tier-withdrawn, with a terminal its requester sees, and never
// offered; the profile's live tier still runs.
func TestASubmissionQueuedAtADrainingTierIsRefusedTierWithdrawn(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 4, 0, "read", "oncall")
	w := b.worker("tse", "read", "tse-0", answerAccept, 200*time.Millisecond)
	ti := oneProfile("tse", 4, "tse-0")
	ti.update(func(c *lib.DispatchConfig) {
		p := testProfile("tse", 4, "read", "oncall")
		p.Tiers = []string{"read"}
		p.DrainingTiers = []lib.DrainingTier{{Tier: "oncall", Mode: "interactive", Until: time.Now().Add(time.Hour)}}
		c.Profiles[0] = p
	})
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "oncall", lib.SubmitKindTop, "task-drained")
	ts := b.waitTerminal("task-drained", 10*time.Second)
	if ts.state != lib.StateRejected || !hasReason(ts, ReasonTierWithdrawn) {
		t.Fatalf("terminal %+v, want rejected %s", ts, ReasonTierWithdrawn)
	}
	if ack, num := b.pending(lib.ProfileDurableName("tse", "oncall", lib.SubmitKindTop)); ack != 0 || num != 0 {
		t.Errorf("the draining tier's durable still holds %d and queues %d", ack, num)
	}
	b.submit("tse", "read", lib.SubmitKindTop, "task-live")
	if ts := b.waitTerminal("task-live", 10*time.Second); ts.state != lib.StateCompleted {
		t.Errorf("the live tier's task: %+v", ts)
	}
	if w.startedTask("task-drained") {
		t.Error("the draining tier's task was offered")
	}
}

// The config refuses a draining tier that is also live, listed twice, not a
// token, or without an end.
func TestTheConfigRefusesABadDrainingTier(t *testing.T) {
	base := testDispatchConfig(6, testProfile("tse", 1, "read"))
	until := time.Now().Add(time.Hour)
	for name, drains := range map[string][]lib.DrainingTier{
		"live":     {{Tier: "read", Mode: "interactive", Until: until}},
		"twice":    {{Tier: "oncall", Until: until}, {Tier: "oncall", Until: until}},
		"no token": {{Tier: "on.call", Until: until}},
		"no end":   {{Tier: "oncall"}},
	} {
		cfg := base
		p := cfg.Profiles[0]
		p.DrainingTiers = drains
		cfg.Profiles = []lib.DispatchProfile{p}
		if err := cfg.Validate(); err == nil || !strings.Contains(err.Error(), "drains tier") {
			t.Errorf("%s: Validate = %v, want the draining-tier refusal", name, err)
		}
	}
}
