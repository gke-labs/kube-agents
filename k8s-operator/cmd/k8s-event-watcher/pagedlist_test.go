package main

import (
	"context"
	"os"
	"runtime"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

// These tests run the watcher against a cluster whose control plane serves
// no watch-list stream, which is every cluster before 1.32 and every 1.33
// cluster: the reflector falls back to a paged list. Against such a cluster
// the watcher must not hold the whole backlog before delivering any of it.

const (
	// pagedListTestPageSize is the page the watcher asks for; the tests size
	// their backlogs in multiples of it, so the bound below follows the
	// production value.
	pagedListTestPageSize = pagedListPageSize
	// pagedListTestPages is how many pages the ordering test's backlog spans.
	// A watcher that delivers page by page runs ahead of its dispatcher only as
	// far as the delivery channel lets it: with deliveryQueueDepth Events
	// buffered it blocks inside the third data page, and the dispatcher's first
	// Dispatch has run before it lists a fourth, so at most
	// pagedListFirstDispatchBound lists have been served at the first dispatch
	// however the goroutines are scheduled. One that collects the backlog first
	// has served every page, pagedListTestPages plus the mark pass.
	pagedListTestPages = 10
	// pagedListMarkPassPages is the two field-selected lists the mark pass
	// makes, one per autoscaler reason.
	pagedListMarkPassPages = 2
	// pagedListFirstDispatchBound is the mark pass plus the pages the channel
	// can hold before a send blocks, rounded up to the page the block falls in.
	pagedListFirstDispatchBound = pagedListMarkPassPages + (deliveryQueueDepth+pagedListTestPageSize)/pagedListTestPageSize
	pagedListTestTimeout        = 60 * time.Second
	// pagedListCeilingClusters, pagedListCeilingEvents and
	// pagedListCeilingLiveHeap size the CI-run ceiling test: two clusters of
	// fifty thousand Events are a hundred thousand collected Events, about
	// 150 MiB live on the collected path, against near 12 MiB paged plus the
	// test process's own heap; 96 MiB separates the two with room for either.
	pagedListCeilingClusters = 2
	pagedListCeilingEvents   = 50000
	pagedListCeilingLiveHeap = 96 << 20
	pagedListCeilingTimeout  = 3 * time.Minute
	pagedListTestNodes       = 10
	pagedListTestPods        = 50

	// pagedListSoakClusters and pagedListSoakEvents are the issue's acceptance
	// shape: four clusters of 300,000 Events each, listed in pages, with the
	// watcher's process under 2 GiB. The ceiling here is on the live heap,
	// which is what the watcher holds: the four fake servers share the
	// process and their encoding garbage inflates HeapAlloc and RSS. The
	// watcher that collected each backlog before delivering it peaked at
	// 1,758 MiB of live heap on this shape (HeapAlloc 3,002 MiB, RSS
	// 3,216 MiB); one that delivers page by page holds pages, so the fleet
	// soak's 512 MiB ceiling separates the two. Hand-run (SOAK=1): the fake
	// servers encode 1.2 million Events, about two minutes.
	pagedListSoakClusters    = 4
	pagedListSoakEvents      = 300000
	pagedListSoakHeapCeiling = 512 << 20
	pagedListSoakTimeout     = 15 * time.Minute
)

// orderingDispatcher records, per cluster, how many list pages the fake had
// served when the first Event was dispatched, whether a scale-up mark was on
// record before that first dispatch, and how many Events arrived.
type orderingDispatcher struct {
	srv *fakeapiserver.Server

	mu                  sync.Mutex
	listsAtFirst        int64
	first               bool
	markBeforeFirst     bool
	marks               atomic.Int64
	delivered           atomic.Int64
	deliveredByReason   map[string]*atomic.Int64
	deliveredByReasonMu sync.Mutex
}

func newOrderingDispatcher(srv *fakeapiserver.Server) *orderingDispatcher {
	return &orderingDispatcher{srv: srv, listsAtFirst: -1, deliveredByReason: map[string]*atomic.Int64{}}
}

func (d *orderingDispatcher) Dispatch(_ context.Context, ev TriageEvent) {
	d.mu.Lock()
	if !d.first {
		d.first = true
		d.listsAtFirst = d.srv.Counts().Lists
		d.markBeforeFirst = d.marks.Load() > 0
	}
	d.mu.Unlock()
	d.delivered.Add(1)
	d.deliveredByReasonMu.Lock()
	c, ok := d.deliveredByReason[ev.Key.Reason]
	if !ok {
		c = new(atomic.Int64)
		d.deliveredByReason[ev.Key.Reason] = c
	}
	d.deliveredByReasonMu.Unlock()
	c.Add(1)
}

func (d *orderingDispatcher) RecordScaleUpMark(TriageEvent) bool {
	d.marks.Add(1)
	return true
}

func (d *orderingDispatcher) firstDispatch() (lists int64, markFirst bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.listsAtFirst, d.markBeforeFirst
}

func startPagedWatcher(t *testing.T, ctx context.Context, srv *fakeapiserver.Server, disp eventDispatcher) (*watcher, *atomic.Int64, chan error) {
	t.Helper()
	client, err := newClusterClient(srv.Config())
	if err != nil {
		t.Fatal(err)
	}
	w := newWatcher(client, disp, targetCluster{Name: "paged"})
	synced := new(atomic.Int64)
	errs := make(chan error, 1)
	go func() {
		errs <- w.Run(ctx, func(watching bool) {
			if watching {
				synced.Add(1)
			}
		})
	}()
	return w, synced, errs
}

func TestRun_WithoutWatchListDispatchesEachPageBeforeTheNextIsListed(t *testing.T) {
	events := pagedListTestPageSize * pagedListTestPages
	srv := fakeapiserver.Start(fakeapiserver.Options{Seed: 1, Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: events}, WatchListUnsupported: true})
	defer srv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), pagedListTestTimeout)
	defer cancel()
	disp := newOrderingDispatcher(srv)
	_, synced, _ := startPagedWatcher(t, ctx, srv, disp)

	eventually(t, pagedListTestTimeout, func() bool { return synced.Load() >= 1 && disp.delivered.Load() >= int64(events) }, "the backlog to be delivered and the cluster to sync")

	counts := srv.Counts()
	if counts.WatchLists != 0 {
		t.Errorf("the fake served %d watch-list streams; it is configured to refuse them", counts.WatchLists)
	}
	if counts.Lists < pagedListTestPages {
		t.Errorf("the fake served %d list pages, want at least the %d the backlog spans", counts.Lists, pagedListTestPages)
	}
	lists, _ := disp.firstDispatch()
	if lists > pagedListFirstDispatchBound {
		t.Errorf("the first Event was dispatched after %d list pages had been served; a watcher that delivers page by page dispatches after at most %d (the mark pass and the pages the delivery channel holds), one that collects the backlog first after all %d", lists, pagedListFirstDispatchBound, pagedListTestPages+pagedListMarkPassPages)
	}
	if got := disp.delivered.Load(); got != int64(events) {
		t.Errorf("delivered %d Events by the sync, want the backlog of %d", got, events)
	}
}

func TestRun_WithoutWatchListRecordsMarksBeforeTheFirstDelivery(t *testing.T) {
	events := pagedListTestPageSize * 2
	srv := fakeapiserver.Start(fakeapiserver.Options{Seed: 2, Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: events}, WatchListUnsupported: true})
	defer srv.Close()
	// An added Event sits after every generated one, so the mark is on the
	// backlog's last page while FailedScheduling events fill the first.
	srv.AddEvent(corev1.Event{
		ObjectMeta:     metav1.ObjectMeta{Name: "scale-up-mark", Namespace: metav1.NamespaceDefault},
		Reason:         reasonTriggeredScaleUp,
		Type:           corev1.EventTypeNormal,
		InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: metav1.NamespaceDefault, Name: "pod-0"},
		Message:        "pod triggered scale-up",
		LastTimestamp:  metav1.Now(),
	})
	ctx, cancel := context.WithTimeout(context.Background(), pagedListTestTimeout)
	defer cancel()
	disp := newOrderingDispatcher(srv)
	_, synced, _ := startPagedWatcher(t, ctx, srv, disp)
	eventually(t, pagedListTestTimeout, func() bool { return synced.Load() >= 1 }, "the cluster to sync")

	if _, markFirst := disp.firstDispatch(); !markFirst {
		t.Errorf("the first Event was dispatched before the scale-up mark on the backlog's last page was on record")
	}
	if disp.marks.Load() < 1 {
		t.Errorf("no scale-up mark was recorded; the mark pass did not find the added TriggeredScaleUp event")
	}
}

func TestRun_WithoutWatchListReportsARelist(t *testing.T) {
	events := pagedListTestPageSize
	srv := fakeapiserver.Start(fakeapiserver.Options{Seed: 3, Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: events}, WatchListUnsupported: true})
	defer srv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), pagedListTestTimeout)
	defer cancel()
	disp := newOrderingDispatcher(srv)
	client, err := newClusterClient(srv.Config())
	if err != nil {
		t.Fatal(err)
	}
	w := newWatcher(client, disp, targetCluster{Name: "paged"})
	var relists atomic.Int64
	w.onRelist = func() { relists.Add(1) }
	synced := new(atomic.Int64)
	go func() {
		_ = w.Run(ctx, func(watching bool) {
			if watching {
				synced.Add(1)
			}
		})
	}()
	eventually(t, pagedListTestTimeout, func() bool { return synced.Load() >= 1 && disp.delivered.Load() >= int64(events) }, "the initial sync")
	if relists.Load() != 0 {
		t.Fatalf("the initial list was reported as a relist")
	}

	srv.Expire()
	eventually(t, pagedListTestTimeout, func() bool { return relists.Load() >= 1 }, "the relist after the expiry to be reported")
	eventually(t, pagedListTestTimeout, func() bool { return disp.delivered.Load() >= int64(2*events) }, "the relist to deliver the backlog again")
	// One expiry is one relist. The reflector asked for a watch-list stream
	// first and was refused; that attempt is not a second re-read.
	eventually(t, pagedListTestTimeout, func() bool { return srv.Counts().Watches >= 2 }, "the watch after the relist to open")
	if got := relists.Load(); got != 1 {
		t.Errorf("relists = %d after one expiry; the refused watch-list attempt before the paged relist must not count", got)
	}
	if c := srv.Counts(); c.WatchListsRefused < 2 {
		t.Errorf("counts = %+v; the reflector asks for a watch-list stream before the initial list and before the relist, so two refusals should be on record", c)
	}
}

// TestSoak_PagedListFourLargeClusters is the issue's acceptance line: four
// clusters of 300,000 Events each, listed in pages because the control plane
// serves no watch-list stream, with the process's live heap under 2 GiB.
// Hand-run: SOAK=1 go test -run TestSoak_PagedListFourLargeClusters ./cmd/k8s-event-watcher/
func TestSoak_PagedListFourLargeClusters(t *testing.T) {
	if os.Getenv(soakEnabledEnv) != soakEnabledValue {
		t.Skipf("set %s=%s to run the four-cluster paged-list soak", soakEnabledEnv, soakEnabledValue)
	}
	ctx, cancel := context.WithTimeout(context.Background(), pagedListSoakTimeout)
	defer cancel()
	sampler := startHeapSampler(ctx)
	servers := make([]*fakeapiserver.Server, pagedListSoakClusters)
	for i := range servers {
		servers[i] = fakeapiserver.Start(fakeapiserver.Options{Seed: int64(i), Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: pagedListSoakEvents}, WatchListUnsupported: true})
		defer servers[i].Close()
	}
	gate := newListGate(defaultInitialListConcurrency)
	var synced atomic.Int64
	delivered := make([]*atomic.Int64, pagedListSoakClusters)
	start := time.Now()
	for i, srv := range servers {
		delivered[i] = new(atomic.Int64)
		client, err := newClusterClient(srv.Config())
		if err != nil {
			t.Fatal(err)
		}
		d := &soakCountingDispatcher{n: delivered[i]}
		w := newWatcher(client, d, targetCluster{Name: "soak-" + strconv.Itoa(i)})
		w.gate = gate
		go func() {
			_ = w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				}
			})
		}()
	}
	eventually(t, pagedListSoakTimeout, func() bool {
		if synced.Load() < pagedListSoakClusters {
			return false
		}
		for _, n := range delivered {
			if n.Load() < pagedListSoakEvents {
				return false
			}
		}
		return true
	}, "four clusters to sync and deliver their backlogs")
	cancel()
	<-sampler.done
	t.Logf("PAGED SOAK %d clusters x %d Events in %s: peak live heap %d MiB, peak HeapAlloc %d MiB, RSS high-water %d MiB", pagedListSoakClusters, pagedListSoakEvents, time.Since(start).Round(time.Second), sampler.peakLive/mebibyte, sampler.peakAlloc/mebibyte, rssHighWater()/mebibyte)
	if sampler.peakLive > pagedListSoakHeapCeiling {
		t.Errorf("peak live heap %d MiB is over the %d MiB ceiling", sampler.peakLive/mebibyte, pagedListSoakHeapCeiling/mebibyte)
	}
}

// soakCountingDispatcher counts dispatches and records no marks.
type soakCountingDispatcher struct{ n *atomic.Int64 }

func (d *soakCountingDispatcher) Dispatch(context.Context, TriageEvent) { d.n.Add(1) }
func (d *soakCountingDispatcher) RecordScaleUpMark(TriageEvent) bool    { return false }

// A control plane with watch-list re-streams the backlog after an expiry
// through a second sendInitialEvents stream; that is the same full re-read
// and is counted the same way.
func TestRun_WithWatchListReportsARelist(t *testing.T) {
	events := pagedListTestPageSize
	srv := fakeapiserver.Start(fakeapiserver.Options{Seed: 4, Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: events}})
	defer srv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), pagedListTestTimeout)
	defer cancel()
	disp := newOrderingDispatcher(srv)
	client, err := newClusterClient(srv.Config())
	if err != nil {
		t.Fatal(err)
	}
	w := newWatcher(client, disp, targetCluster{Name: "streamed"})
	var relists atomic.Int64
	w.onRelist = func() { relists.Add(1) }
	synced := new(atomic.Int64)
	go func() {
		_ = w.Run(ctx, func(watching bool) {
			if watching {
				synced.Add(1)
			}
		})
	}()
	eventually(t, pagedListTestTimeout, func() bool { return synced.Load() >= 1 && disp.delivered.Load() >= int64(events) }, "the initial sync")
	if c := srv.Counts(); c.WatchLists != 1 || c.Lists != 0 {
		t.Fatalf("counts after the sync = %+v; want one watch-list stream and no list", c)
	}
	if relists.Load() != 0 {
		t.Fatalf("the initial stream was reported as a relist")
	}

	srv.Expire()
	eventually(t, pagedListTestTimeout, func() bool { return relists.Load() >= 1 }, "the re-stream after the expiry to be reported")
	eventually(t, pagedListTestTimeout, func() bool { return srv.Counts().WatchLists >= 2 && disp.delivered.Load() >= int64(2*events) }, "the second stream to deliver the backlog again")
	if c := srv.Counts(); c.Lists != 0 {
		t.Errorf("counts after the expiry = %+v; the re-read should be a stream, not a paged list", c)
	}
}

// The paged path holds one page of a backlog, not the backlog: two clusters
// of fifty thousand Events each, refusing watch-list, stay under a live-heap
// ceiling a collected list of the same backlogs passes by a wide margin
// (about 1.5 KiB retained per collected Event). Run by every go test of the
// package; -short skips it, as it skips the fleet soak.
func TestRun_WithoutWatchListHoldsOnePageOfTheBacklog(t *testing.T) {
	if testing.Short() {
		t.Skip("streams two fifty-thousand-Event backlogs; skipped under -short")
	}
	ctx, cancel := context.WithTimeout(context.Background(), pagedListCeilingTimeout)
	defer cancel()
	runtime.GC()
	sampler := startHeapSampler(ctx)
	delivered := new(atomic.Int64)
	d := &soakCountingDispatcher{n: delivered}
	synced := new(atomic.Int64)
	for i := 0; i < pagedListCeilingClusters; i++ {
		srv := fakeapiserver.Start(fakeapiserver.Options{Seed: int64(i + 1), Sizes: fakeapiserver.Sizes{Nodes: pagedListTestNodes, Pods: pagedListTestPods, Events: pagedListCeilingEvents}, WatchListUnsupported: true})
		defer srv.Close()
		client, err := newClusterClient(srv.Config())
		if err != nil {
			t.Fatal(err)
		}
		w := newWatcher(client, d, targetCluster{Name: "ceiling-" + strconv.Itoa(i)})
		go func() {
			_ = w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				}
			})
		}()
	}
	want := int64(pagedListCeilingClusters * pagedListCeilingEvents)
	eventually(t, pagedListCeilingTimeout, func() bool { return synced.Load() >= pagedListCeilingClusters && delivered.Load() >= want }, "both backlogs to be delivered")
	cancel()
	<-sampler.done
	t.Logf("paged ceiling: %d clusters x %d Events: peak live heap %d MiB, peak HeapAlloc %d MiB", pagedListCeilingClusters, pagedListCeilingEvents, sampler.peakLive/mebibyte, sampler.peakAlloc/mebibyte)
	if sampler.peakLive > pagedListCeilingLiveHeap {
		t.Errorf("peak live heap %d MiB is above the %d MiB ceiling; the paged list is holding more than a page", sampler.peakLive/mebibyte, pagedListCeilingLiveHeap/mebibyte)
	}
}
