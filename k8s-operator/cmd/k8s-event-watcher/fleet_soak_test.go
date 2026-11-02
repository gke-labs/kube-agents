// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// The fleet soak CI runs: ten fake API servers at the thousand-node shape
// (fakeapiserver.Fleet; smaller under a hand-run -race), watched from one process
// through the gate and the wire format the binary ships, through two 429s per
// cluster at the start and the expiry of every watch once all ten have
// synced. It asserts the fleet-scale invariants the three-node fixtures in
// the rest of the package cannot: every cluster syncs and none is dropped,
// the servers never hand out more backlogs at once than the gate allows, the
// Event added after the expiry reaches every cluster's dispatcher, and the
// peak live heap stays under the ceiling. Unlike soak_test.go, which is
// hand-run and reports numbers, this one is a test: `-short` skips it.
package main

import (
	"context"
	"fmt"
	"runtime"
	runtimemetrics "runtime/metrics"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

const (
	fleetSoakClusters = 10
	// fleetSoakThrottles is how many 429s each cluster answers before its
	// first backlog, with the Retry-After they carry; client-go retries them
	// inside the request, so the watcher sees them as latency on a request
	// that holds a gate slot.
	fleetSoakThrottles  = 2
	fleetSoakRetryAfter = time.Second
	// fleetSoakLiveHeapCeiling bounds the peak live heap (what the last GC
	// marked reachable) over the run. With the gate at the binary's default,
	// at most four clusters' backlogs are in flight at once, each held once
	// in the reflector's temporary store with managedFields stripped, so the
	// bound is on the watcher's design and not on the dataset's garbage.
	fleetSoakLiveHeapCeiling = 512 << 20
	// fleetSoakTimeout bounds the whole run, every phase and the watchers'
	// return together, so a hang in any phase is reported by its name well
	// inside go test's own ten-minute timeout. A run that passes takes
	// seconds per phase.
	fleetSoakTimeout = 4 * time.Minute
	// fleetSoakSampleInterval is how often the heap is read; the live metric
	// moves once per GC, which under this allocation rate is far oftener.
	fleetSoakSampleInterval = 25 * time.Millisecond
	// fleetSoakAfterExpiryReason marks the Event added to every cluster once
	// it has recovered from the expiry, so the dispatcher can tell it from
	// the backlog.
	fleetSoakAfterExpiryReason = "FleetSoakAfterExpiry"
	fleetSoakAfterExpiryName   = "after-expiry"
	fleetSoakNamespace         = "team-0"
	fleetSoakClusterFormat     = "fleet-%02d"
	// The two runtime/metrics the sampler reads: the live heap the assertion
	// is on, and the HeapAlloc of runtime.MemStats (objects live or not yet
	// swept) logged beside it.
	fleetSoakLiveHeapMetric  = "/gc/heap/live:bytes"
	fleetSoakHeapAllocMetric = "/memory/classes/heap/objects:bytes"
	mebibyte                 = 1 << 20
)

// fleetCounter is the dispatcher: it counts what each cluster delivers and
// the post-expiry Events among them. The maps are built before the watchers
// start and only their values change, so they are read without a lock.
type fleetCounter struct {
	delivered   map[string]*atomic.Int64
	afterExpiry map[string]*atomic.Int64
}

func newFleetCounter(names []string) *fleetCounter {
	c := &fleetCounter{delivered: map[string]*atomic.Int64{}, afterExpiry: map[string]*atomic.Int64{}}
	for _, name := range names {
		c.delivered[name] = new(atomic.Int64)
		c.afterExpiry[name] = new(atomic.Int64)
	}
	return c
}

func (c *fleetCounter) Dispatch(_ context.Context, ev TriageEvent) {
	c.delivered[ev.Cluster].Add(1)
	if ev.Key.Reason == fleetSoakAfterExpiryReason {
		c.afterExpiry[ev.Cluster].Add(1)
	}
}

func (c *fleetCounter) RecordScaleUpMark(TriageEvent) bool { return false }

// heapSampler tracks the peak of the two heap metrics on its own goroutine
// until stopped.
type heapSampler struct {
	peakLive, peakAlloc uint64
	done                chan struct{}
}

func startHeapSampler(ctx context.Context) *heapSampler {
	s := &heapSampler{done: make(chan struct{})}
	go func() {
		defer close(s.done)
		samples := []runtimemetrics.Sample{{Name: fleetSoakLiveHeapMetric}, {Name: fleetSoakHeapAllocMetric}}
		for ctx.Err() == nil {
			runtimemetrics.Read(samples)
			if v := samples[0].Value; v.Kind() == runtimemetrics.KindUint64 && v.Uint64() > s.peakLive {
				s.peakLive = v.Uint64()
			}
			if v := samples[1].Value; v.Kind() == runtimemetrics.KindUint64 && v.Uint64() > s.peakAlloc {
				s.peakAlloc = v.Uint64()
			}
			time.Sleep(fleetSoakSampleInterval)
		}
	}()
	return s
}

func TestFleetSoak_TenClustersThroughThrottleAndExpiry(t *testing.T) {
	if testing.Short() {
		t.Skip("the fleet soak streams ten clusters' backlogs; skipped under -short")
	}
	sizes := fakeapiserver.Fleet
	names := make([]string, fleetSoakClusters)
	for i := range names {
		names[i] = fmt.Sprintf(fleetSoakClusterFormat, i)
	}
	counter := newFleetCounter(names)
	var backlogs, peakBacklogs atomic.Int64
	onBacklog := func(inFlight bool) {
		if !inFlight {
			backlogs.Add(-1)
			return
		}
		now := backlogs.Add(1)
		for {
			prev := peakBacklogs.Load()
			if now <= prev || peakBacklogs.CompareAndSwap(prev, now) {
				break
			}
		}
	}
	servers := make([]*fakeapiserver.Server, fleetSoakClusters)
	for i := range servers {
		servers[i] = fakeapiserver.Start(fakeapiserver.Options{Seed: int64(i), Sizes: sizes, OnBacklog: onBacklog})
		t.Cleanup(servers[i].Close)
		servers[i].ThrottleFirst(fleetSoakThrottles, fleetSoakRetryAfter)
	}

	runtime.GC()
	ctx, cancel := context.WithCancel(context.Background())
	sampler := startHeapSampler(ctx)
	gate := newListGate(defaultInitialListConcurrency)
	var synced, reportedDown atomic.Int64
	runErrs := make(chan error, fleetSoakClusters)
	start := time.Now()
	deadline := start.Add(fleetSoakTimeout)
	// The numbers a run is read by are logged on every exit, a phase timeout
	// included, so a red carries them as a green does: the two durations
	// stay zero for a phase the run did not reach. Cancelling first stops
	// the watchers and the sampler, so the peaks are final when they print.
	var syncedIn, elapsed time.Duration
	dataset := "full"
	if fakeapiserver.UnderRace {
		dataset = "race"
	}
	defer func() {
		cancel()
		<-sampler.done
		t.Logf("FLEET SOAK clusters=%d dataset=%s nodes=%d pods=%d events=%d gate=%d", fleetSoakClusters, dataset, sizes.Nodes, sizes.Pods, sizes.Events, defaultInitialListConcurrency)
		t.Logf("FLEET SOAK all clusters synced in %s; synced, expired, recovered and delivered in %s", syncedIn.Round(time.Millisecond), elapsed.Round(time.Millisecond))
		t.Logf("FLEET SOAK peak backlogs in flight across the servers: %d", peakBacklogs.Load())
		t.Logf("FLEET SOAK peak live heap (%s): %d MiB; peak HeapAlloc (%s): %d MiB", fleetSoakLiveHeapMetric, sampler.peakLive/mebibyte, fleetSoakHeapAllocMetric, sampler.peakAlloc/mebibyte)
		t.Logf("FLEET SOAK reported watching %d times, not watching %d times", synced.Load(), reportedDown.Load())
		for i, srv := range servers {
			t.Logf("FLEET SOAK %s: %+v", names[i], srv.Counts())
		}
	}()
	// phase waits for cond with what is left of the run's one deadline.
	phase := func(cond func() bool, what string) {
		t.Helper()
		eventually(t, time.Until(deadline), cond, what)
	}
	for i, srv := range servers {
		client, err := newClusterClient(srv.Config())
		if err != nil {
			t.Fatal(err)
		}
		w := newWatcher(client, counter, targetCluster{Name: names[i]})
		w.gate = gate
		go func() {
			runErrs <- w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				} else {
					reportedDown.Add(1)
				}
			})
		}()
	}

	// synced counts reports of watching, not clusters: a cluster that synced,
	// broke on a non-403 failure and recovered reports true twice, so the
	// wait is for at least ten and the reportedDown check at the end names a
	// flap; an exact count would wait out the deadline instead.
	phase(func() bool { return synced.Load() >= fleetSoakClusters }, "all ten clusters to sync")
	syncedIn = time.Since(start)
	// The sync is reported as the last initial Event is handed over, which
	// can be an instant before its dispatch returns (see Run), so the count
	// is given that instant; a count past the backlog would mean a relist
	// that nothing asked for.
	phase(func() bool {
		for _, name := range names {
			if counter.delivered[name].Load() < int64(sizes.Events) {
				return false
			}
		}
		return true
	}, "every cluster's backlog to be dispatched")
	for _, name := range names {
		if got := counter.delivered[name].Load(); got != int64(sizes.Events) {
			t.Errorf("%s delivered %d Events by its sync, want the backlog of %d", name, got, sizes.Events)
		}
	}

	for _, srv := range servers {
		srv.Expire()
	}
	phase(func() bool {
		for _, srv := range servers {
			if srv.Counts().WatchLists < 2 {
				return false
			}
		}
		return true
	}, "every cluster to stream its state again after the expiry")
	for i, srv := range servers {
		now := metav1.Now()
		srv.AddEvent(corev1.Event{
			ObjectMeta:     metav1.ObjectMeta{Name: fleetSoakAfterExpiryName, Namespace: fleetSoakNamespace},
			InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: fleetSoakNamespace, Name: names[i], APIVersion: "v1"},
			Reason:         fleetSoakAfterExpiryReason,
			Type:           corev1.EventTypeNormal,
			FirstTimestamp: now,
			LastTimestamp:  now,
			Count:          1,
		})
	}
	phase(func() bool {
		for _, name := range names {
			if counter.afterExpiry[name].Load() == 0 {
				return false
			}
		}
		return true
	}, "the post-expiry Event to reach every cluster's dispatcher")
	elapsed = time.Since(start)

	cancel()
	for range servers {
		select {
		case err := <-runErrs:
			if err != nil {
				t.Errorf("Run returned %v", err)
			}
		case <-time.After(time.Until(deadline)):
			t.Fatal("a watcher did not return after the context was cancelled")
		}
	}
	<-sampler.done

	if n := reportedDown.Load(); n != 0 {
		t.Errorf("%d cluster(s) were reported as not watching; a transient 429 or an expiry should take none out", n)
	}
	if got := peakBacklogs.Load(); got > defaultInitialListConcurrency {
		t.Errorf("the servers had %d backlogs in flight at once; the gate allows %d", got, defaultInitialListConcurrency)
	}
	for i, srv := range servers {
		c := srv.Counts()
		// Expired is at least the 410 that ended the open stream; how many
		// stale resumes follow it is client-go's recovery sequence, not the
		// watcher's, so it is not pinned.
		if c.Throttled != fleetSoakThrottles || c.WatchLists != 2 || c.Expired < 1 || c.Lists != 0 {
			t.Errorf("%s: %+v; want %d throttled, 2 watch-lists (start, after the expiry), at least 1 expired (the open stream), no list", names[i], c, fleetSoakThrottles)
		}
	}
	if sampler.peakLive >= fleetSoakLiveHeapCeiling {
		t.Errorf("peak live heap %d MiB is not under the %d MiB ceiling", sampler.peakLive/mebibyte, fleetSoakLiveHeapCeiling/mebibyte)
	}
}
