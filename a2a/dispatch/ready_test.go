package dispatch

import (
	"bytes"
	"context"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Ready is the pod's readiness: false until Run holds its subscriptions and
// every listed durable is bound, false again while a durable the config
// added isn't there yet, naming it, and true once it binds.
func TestReadyWaitsForEveryListedDurable(t *testing.T) {
	b := newBus(t, "tse-0")
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2, "tse-0")
	d, err := New(fastRegrant(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{}, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	if ok, why := d.Ready(); ok || why == "" {
		t.Fatalf("ready before Run: %v %q", ok, why)
	}
	d, stop := b.runDispatcher(fastRegrant(), ti, &fakeVerifier{})
	waitFor(t, 15*time.Second, "ready with tse bound", func() bool { ok, _ := d.Ready(); return ok })

	ti.update(func(c *lib.DispatchConfig) { c.Version = "v2"; c.Profiles = append(c.Profiles, testProfile("tse2", 2)) })
	missing := lib.ProfileDurableName("tse2", "read", lib.SubmitKindTop)
	waitFor(t, 10*time.Second, "unready naming the missing durable", func() bool {
		ok, why := d.Ready()
		return !ok && strings.Contains(why, missing)
	})
	b.ensure("tse2", 2, 0, "read")
	waitFor(t, 30*time.Second, "ready once tse2's durables exist", func() bool { ok, _ := d.Ready(); return ok })

	stop()
	if ok, _ := d.Ready(); ok {
		t.Error("ready after Run returned")
	}
}

// syncBuffer is a log sink a test reads while the dispatcher writes it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}

// A pool with no worker that takes the task stalls a durable as surely as a
// cap does, but the cause is the pool: the health line says so, and does
// not send the operator looking for a cross-chain cycle. The pool's pods
// are Ready but never answer, so the submission spends most of its time
// being offered: an offer nobody accepted is not a running task, and the
// submission stays visible as waiting for a worker throughout.
func TestAStallForWantOfAWorkerIsNamedAsOne(t *testing.T) {
	b := newBus(t, "tse-0", "tse-1")
	b.ensure("tse", 2, 0, "read")
	w0 := b.worker("tse", "read", "tse-0", answerSilent, 0)
	w1 := b.worker("tse", "read", "tse-1", answerSilent, 0)
	ti := oneProfile("tse", 2, "tse-0", "tse-1")
	var logs syncBuffer
	d, err := New(testConfig(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{},
		slog.New(slog.NewTextHandler(&logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(b.ctx)
	done := make(chan struct{})
	go func() { defer close(done); _ = d.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 15*time.Second, "the durable stalled", func() bool { return slices.Contains(d.Health().Stalled, durable) })
	// Sample across several offer rounds (each two accept timeouts long).
	sawOffering := false
	for end := time.Now().Add(5 * time.Second); time.Now().Before(end); time.Sleep(50 * time.Millisecond) {
		h := d.Health()
		if h.Running["tse"] != 0 {
			t.Fatalf("running %v while no pod accepted: an offer counted as a running task", h.Running)
		}
		if h.NoWorker[durable] != 1 || h.Waiting[durable] != 1 || !slices.Contains(h.Stalled, durable) {
			t.Fatalf("waiting %v, no worker %v, stalled %v, offering %v; want the one submission waiting for a worker, stalled",
				h.Waiting, h.NoWorker, h.Stalled, h.Offering)
		}
		sawOffering = sawOffering || h.Offering["tse"] == 1
	}
	if !sawOffering {
		t.Errorf("never reported the submission as being offered")
	}
	if w0.offerCount()+w1.offerCount() < 3 {
		t.Errorf("offered %d+%d times, want several rounds", w0.offerCount(), w1.offerCount())
	}
	waitFor(t, 5*time.Second, "the stall logged", func() bool { return strings.Contains(logs.String(), "no ready worker") })
	if strings.Contains(logs.String(), "cross-chain") {
		t.Errorf("a stall for want of a worker was logged as a cap cycle:\n%s", logs.String())
	}
}

// A pool with no pods at all is named the same way.
func TestAStallOnAnEmptyPoolIsNamedAsOne(t *testing.T) {
	b := newBus(t)
	b.ensure("tse", 2, 0, "read")
	ti := oneProfile("tse", 2) // no pods
	var logs syncBuffer
	d, err := New(testConfig(), b.connect("dispatcher"), ti.src, ti.pool, &fakeVerifier{},
		slog.New(slog.NewTextHandler(&logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(b.ctx)
	done := make(chan struct{})
	go func() { defer close(done); _ = d.Run(ctx) }()
	t.Cleanup(func() { cancel(); <-done })
	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	durable := lib.ProfileDurableName("tse", "read", lib.SubmitKindTop)
	waitFor(t, 10*time.Second, "the durable stalled", func() bool { return slices.Contains(d.Health().Stalled, durable) })
	if h := d.Health(); h.NoWorker[durable] != 1 || h.Waiting[durable] != 1 {
		t.Errorf("waiting %v, no worker %v; want the one submission waiting for a worker", h.Waiting, h.NoWorker)
	}
	waitFor(t, 5*time.Second, "the stall logged", func() bool { return strings.Contains(logs.String(), "no ready worker") })
	if strings.Contains(logs.String(), "cross-chain") {
		t.Errorf("a stall for want of a worker was logged as a cap cycle:\n%s", logs.String())
	}
}
