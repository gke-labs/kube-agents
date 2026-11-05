package dispatch

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Cross-chain admission (#3655, decided 2026-10-22): parents wait by staying
// open, holding their slots while their children run. Two chains whose
// tasks wait on children in each other's profile are the case.

// child is a task a fake worker starts and waits on.
type child struct{ profile, taskID string }

// chainPools starts pods worker pods for each profile, each running its
// task by starting the child spawn names (if any), waiting for the child's
// terminal, then finishing after work.
func chainPools(t *testing.T, b *bus, spawn map[string]child, work time.Duration, pods int, profiles ...string) *testInstall {
	t.Helper()
	ti := &testInstall{src: NewStaticSource(testDispatchConfig(3)), pool: NewStaticPool()}
	for _, p := range profiles {
		var names []string
		for i := 0; i < pods; i++ {
			pod := fmt.Sprintf("%s-%d", p, i)
			names = append(names, pod)
			w := b.worker(p, "read", pod, answerAccept, 0)
			w.run = func(ctx context.Context, offer lib.DispatchMessage) {
				if c, ok := spawn[offer.TaskID]; ok {
					b.submit(c.profile, "read", lib.SubmitKindChild, c.taskID)
					for len(b.terminalsOf(c.taskID)) == 0 {
						select {
						case <-ctx.Done():
							return
						case <-time.After(20 * time.Millisecond):
						}
					}
				}
				select {
				case <-time.After(work):
				case <-ctx.Done():
				}
			}
		}
		b.ensure(p, 4, 0, "read")
		ti.pool.Set(p, "read", names...)
		ti.update(func(c *lib.DispatchConfig) {
			prof := testProfile(p, 4)
			prof.ActiveDeadlineSeconds = 20
			c.Profiles = append(c.Profiles, prof)
		})
	}
	return ti
}

// Ceiling 3: reserves of 1 (child) and 1 (interactive) and one shared slot,
// so top-level work stops at 2. Three
// chains' parents ask for every slot; two get them and wait on children in
// the other profile, the third waits in the queue. The children still run,
// one at a time through the reserved slot, and every chain completes.
func TestChildrenRunThroughTheChildReserve(t *testing.T) {
	var pods []string
	for _, p := range []string{"px", "py"} {
		for i := 0; i < 4; i++ {
			pods = append(pods, fmt.Sprintf("%s-%d", p, i))
		}
	}
	b := newBus(t, pods...)
	spawn := map[string]child{
		"parent-1": {"py", "child-1"},
		"parent-2": {"px", "child-2"},
		"parent-3": {"py", "child-3"},
	}
	ti := chainPools(t, b, spawn, 300*time.Millisecond, 4, "px", "py")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("px", "read", lib.SubmitKindTop, "parent-1")
	b.submit("py", "read", lib.SubmitKindTop, "parent-2")
	b.submit("px", "read", lib.SubmitKindTop, "parent-3")
	for _, id := range []string{"parent-1", "parent-2", "parent-3", "child-1", "child-2", "child-3"} {
		if ts := b.waitTerminal(id, 30*time.Second); ts.state != lib.StateCompleted {
			t.Errorf("%s: %+v, want completed", id, ts)
		}
	}
}

// A cycle the reserve can't break: each chain's child waits on a grandchild
// in the other profile. With ceiling 3 both parents and one child hold every
// slot, and the other child and the grandchild the first child waits on
// queue behind them. It must surface rather than hang: the health line
// names the stalled queue first, then the parents' and the child's deadlines
// fail them, which frees the slots, and every task ends.
func TestACrossChainCycleSurfaces(t *testing.T) {
	var pods []string
	for _, p := range []string{"px", "py"} {
		for i := 0; i < 4; i++ {
			pods = append(pods, fmt.Sprintf("%s-%d", p, i))
		}
	}
	b := newBus(t, pods...)
	spawn := map[string]child{
		"parent-a": {"py", "child-a"}, "child-a": {"px", "grand-a"},
		"parent-b": {"px", "child-b"}, "child-b": {"py", "grand-b"},
	}
	ti := chainPools(t, b, spawn, 200*time.Millisecond, 4, "px", "py")
	ti.update(func(c *lib.DispatchConfig) {
		for i := range c.Profiles {
			c.Profiles[i].ActiveDeadlineSeconds = 5
		}
	})
	d, _ := b.runDispatcher(testConfig(), ti, &fakeVerifier{})
	b.submit("px", "read", lib.SubmitKindTop, "parent-a")
	b.submit("py", "read", lib.SubmitKindTop, "parent-b")

	var once sync.Once
	var stalled []string
	var stalledBeforeFailure bool
	ids := []string{"parent-a", "parent-b", "child-a", "child-b", "grand-a", "grand-b"}
	waitFor(t, 40*time.Second, "every task to end", func() bool {
		failed := false
		ended := 0
		for _, id := range ids {
			ts := b.terminalsOf(id)
			if len(ts) > 0 {
				ended++
				if ts[0].state == lib.StateFailed {
					failed = true
				}
			}
		}
		if h := d.Health(); len(h.Stalled) > 0 {
			once.Do(func() { stalled, stalledBeforeFailure = h.Stalled, !failed })
		}
		return ended == len(ids)
	})
	if !stalledBeforeFailure {
		t.Errorf("the health line never named a stalled queue before the deadlines fired (stalled %v)", stalled)
	}
	deadlines := 0
	for _, id := range ids {
		ts := b.terminalsOf(id)[0]
		if ts.state == lib.StateFailed {
			if !hasReason(ts, ReasonDeadlineExceeded) {
				t.Errorf("%s failed with %q, want deadline-exceeded", id, ts.text)
			}
			deadlines++
		}
	}
	if deadlines == 0 {
		t.Error("the cycle ended with no deadline failure; it was not a cycle")
	}
	t.Logf("stalled %v; %d deadline failures", stalled, deadlines)
}
