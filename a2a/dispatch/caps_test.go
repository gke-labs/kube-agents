package dispatch

import (
	"fmt"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func bareDispatcher() *Dispatcher {
	return &Dispatcher{cfg: testConfig(), now: time.Now, active: map[holdKey]*holding{}, waiting: map[string]waiting{},
		lastAdmit: map[string]time.Time{}, released: map[string]time.Time{}, verified: map[holdKey]permit{}}
}

func capsSnapshot(ceiling int) lib.DispatchConfig {
	return testDispatchConfig(ceiling, testProfile("chat", 100, "read", "unattended"), testProfile("tse", 2))
}

// fill reserves n tasks of one shape, failing if any is refused.
func fill(t *testing.T, d *Dispatcher, snap lib.DispatchConfig, profile, tier, kind string, n int) {
	t.Helper()
	p, _ := profileOf(snap, profile)
	for i := 0; i < n; i++ {
		if h, why := d.reserve(snap, p, tier, kind, fmt.Sprintf("%s-%s-%s-%d-%d", profile, tier, kind, i, len(d.active))); h == nil {
			t.Fatalf("reserve %s/%s/%s #%d refused: %s", profile, tier, kind, i, why)
		}
	}
}

func refused(d *Dispatcher, snap lib.DispatchConfig, profile, tier, kind string) string {
	p, _ := profileOf(snap, profile)
	h, why := d.reserve(snap, p, tier, kind, "probe")
	if h != nil {
		d.release(h, false)
		return ""
	}
	return why
}

func TestTheTestReservesMatchTheOperatorsExamples(t *testing.T) {
	for _, tc := range []struct{ ceiling, child, interactive int }{{1, 0, 0}, {2, 1, 0}, {3, 1, 1}, {6, 2, 2}} {
		c := capsSnapshot(tc.ceiling)
		if c.ChildReserve != tc.child || c.InteractiveReserve != tc.interactive {
			t.Errorf("ceiling %d: child %d interactive %d, want %d and %d", tc.ceiling, c.ChildReserve, c.InteractiveReserve, tc.child, tc.interactive)
		}
		if err := c.Validate(); err != nil {
			t.Errorf("ceiling %d: %v", tc.ceiling, err)
		}
	}
}

// Ceiling 6 (child 2, interactive 2, shared 2). Unattended work gets the 2
// shared slots and no more; interactive work still gets its 2 reserved, and
// children their 2.
func TestCeilingSixKeepsEachReserve(t *testing.T) {
	d := bareDispatcher()
	snap := capsSnapshot(6)
	fill(t, d, snap, "chat", "unattended", lib.SubmitKindTop, 2)
	if why := refused(d, snap, "chat", "unattended", lib.SubmitKindTop); why == "" {
		t.Fatal("a third unattended top-level task took a reserved slot")
	}
	fill(t, d, snap, "chat", "read", lib.SubmitKindTop, 2)
	if why := refused(d, snap, "chat", "read", lib.SubmitKindTop); why == "" {
		t.Fatal("a third interactive top-level task took the child reserve")
	}
	fill(t, d, snap, "chat", "read", lib.SubmitKindChild, 2)
	if why := refused(d, snap, "chat", "read", lib.SubmitKindChild); why != "install ceiling" {
		t.Errorf("a child past the ceiling: %q", why)
	}
}

// Ceiling 6, children first: children fill their reserve, then the shared
// slots, but never the interactive reserve.
func TestChildrenOverflowIntoSharedNotInteractive(t *testing.T) {
	d := bareDispatcher()
	snap := capsSnapshot(6)
	fill(t, d, snap, "chat", "read", lib.SubmitKindChild, 4)
	if why := refused(d, snap, "chat", "read", lib.SubmitKindChild); why == "" {
		t.Fatal("a fifth child took the interactive reserve")
	}
	if why := refused(d, snap, "chat", "unattended", lib.SubmitKindTop); why == "" {
		t.Fatal("unattended work found a shared slot the children hold")
	}
	fill(t, d, snap, "chat", "read", lib.SubmitKindTop, 2)
}

// Ceiling 2 (child 1, interactive 0, shared 1): one top-level task of
// either side, and its child still runs.
func TestCeilingTwoLeavesAChildSlot(t *testing.T) {
	for _, tier := range []string{"read", "unattended"} {
		d := bareDispatcher()
		snap := capsSnapshot(2)
		fill(t, d, snap, "chat", tier, lib.SubmitKindTop, 1)
		if why := refused(d, snap, "chat", "read", lib.SubmitKindTop); why == "" {
			t.Fatalf("%s: a second top-level task took the child reserve", tier)
		}
		fill(t, d, snap, "chat", "read", lib.SubmitKindChild, 1)
	}
}

// Ceiling 1 (no reserves, one shared slot): one task of any kind, so a
// parent there can't start a child.
func TestCeilingOneRunsOneTaskOfAnyKind(t *testing.T) {
	for _, tier := range []string{"read", "unattended"} {
		d := bareDispatcher()
		snap := capsSnapshot(1)
		fill(t, d, snap, "chat", tier, lib.SubmitKindTop, 1)
		if why := refused(d, snap, "chat", "read", lib.SubmitKindChild); why != "install ceiling" {
			t.Errorf("%s: a child beside the one task: %q", tier, why)
		}
	}
	d := bareDispatcher()
	fill(t, d, capsSnapshot(1), "chat", "read", lib.SubmitKindChild, 1)
}

// A profile's concurrency caps its top-level tasks and, separately, its
// children.
func TestProfileConcurrencyCapsEachKind(t *testing.T) {
	d := bareDispatcher()
	snap := capsSnapshot(20)
	fill(t, d, snap, "tse", "read", lib.SubmitKindTop, 2)
	if why := refused(d, snap, "tse", "read", lib.SubmitKindTop); why != "profile concurrency" {
		t.Errorf("tse's third top-level task: %q", why)
	}
	fill(t, d, snap, "tse", "read", lib.SubmitKindChild, 2)
	if why := refused(d, snap, "tse", "read", lib.SubmitKindChild); why != "profile concurrency" {
		t.Errorf("tse's third child: %q", why)
	}
}

// Caps follow the config per decision: raising the ceiling admits at once.
func TestCapsFollowTheConfig(t *testing.T) {
	d := bareDispatcher()
	snap := capsSnapshot(3)
	fill(t, d, snap, "chat", "read", lib.SubmitKindTop, 2)
	if refused(d, snap, "chat", "read", lib.SubmitKindTop) == "" {
		t.Fatal("ceiling 3 admitted a third top-level task into the child reserve")
	}
	if why := refused(d, capsSnapshot(30), "chat", "read", lib.SubmitKindTop); why != "" {
		t.Errorf("after the ceiling rose: refused for %q", why)
	}
}

// Over a cap the task waits: no terminal, not offered, still queued; it runs
// when a slot frees. Never failed for the cap.
func TestOverCapStaysQueuedAndRunsLater(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 4, 0, "read")
	w0 := b.worker("tse", "read", "tse-0", answerAccept, 3*time.Second)
	w1 := b.worker("tse", "read", "tse-1", answerAccept, 3*time.Second)
	d, _ := b.runDispatcher(testConfig(), oneProfile("tse", 1, "tse-0", "tse-1"), &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	waitFor(t, 10*time.Second, "the first offer", func() bool { return w0.offerCount()+w1.offerCount() == 1 })
	b.submit("tse", "read", lib.SubmitKindTop, "task-2")
	time.Sleep(1500 * time.Millisecond)
	if n := w0.offerCount() + w1.offerCount(); n != 1 {
		t.Fatalf("%d offers with concurrency 1", n)
	}
	if ts := b.terminalsOf("task-2"); len(ts) != 0 {
		t.Fatalf("the over-cap task got a terminal: %+v", ts)
	}
	if h := d.Health(); h.Waiting[lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)] != 1 {
		t.Errorf("health %+v, want task-2 waiting", h)
	}
	for _, id := range []string{"task-1", "task-2"} {
		if ts := b.waitTerminal(id, 15*time.Second); ts.state != lib.StateCompleted {
			t.Errorf("%s: %+v, want completed", id, ts)
		}
	}
}
