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

package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

// clusterRun is one watched cluster's running state: what stop needs to end
// it and snapshot what it leaves behind.
type clusterRun struct {
	cluster targetCluster
	cache   *dedupCache
	cancel  context.CancelFunc
	// done is closed once the cluster's watcher goroutine has returned.
	done chan struct{}
}

// clusterSet is every cluster this process is watching, with the shared
// pieces each one is started from. main builds it once, starts the boot watch
// set through it, and the reload loop (runProfileReload) adds and removes
// clusters through the same two calls, so a cluster started on a rescan runs
// exactly as one started at boot: its own dedup cache and dispatcher, its
// snapshot and sweep loops, the shared filter, injector, metrics and gate.
type clusterSet struct {
	f      *flags
	filter *filter
	inj    *injector
	m      *metrics
	gate   *listGate
	// synced counts clusters whose initial list has completed at least once;
	// main's no-cluster-synced exit reads it.
	synced atomic.Int64

	mu      sync.Mutex
	running map[string]*clusterRun // keyed by targetCluster.Profile
	// directIdentity is the project/location the direct cluster
	// (directProfile) stands for, when a profile has said: the one
	// buildWatchSet absorbed at boot, or one absorbed on a rescan (see
	// reconcileProfiles). Empty while no profile has named it.
	directIdentity clusterprofiles.Identity
	// stopping counts stops in progress, which wait joins through stopped
	// before it snapshots; closed is set by wait once ctx is done, after which
	// stop leaves a run to the shutdown snapshot rather than racing it.
	stopping int
	stopped  *sync.Cond
	closed   bool
}

func newClusterSet(f *flags, filter *filter, inj *injector, m *metrics, gate *listGate) *clusterSet {
	s := &clusterSet{f: f, filter: filter, inj: inj, m: m, gate: gate, running: map[string]*clusterRun{}}
	s.stopped = sync.NewCond(&s.mu)
	return s
}

// start begins watching tc: a dedup cache of its own (restored from its
// snapshot when --dedup-persist is set), a dispatcher around it, the snapshot
// and sweep loops, and the watcher goroutine. The error is a dedup cache that
// could not be built, which is the one per-cluster failure before the
// informer; everything after it is reported through cluster_up, never
// returned, because an informer that cannot reach its cluster waits rather
// than fails (see watcher.Run).
func (s *clusterSet) start(ctx context.Context, tc targetCluster) error {
	// Suffixed with the profile, not the cluster name: two clusters can share
	// a name across locations, and they must not share a snapshot.
	cache, err := newDedupCache(s.f.dedupWindow, dedupPersistPath(s.f.dedupPersist, tc.Profile))
	if err != nil {
		return fmt.Errorf("dedup cache: %w", err)
	}
	disp := newDispatcher(s.f, s.filter, cache, s.inj, s.m)
	disp.cluster = tc
	cctx, cancel := context.WithCancel(ctx)
	run := &clusterRun{cluster: tc, cache: cache, cancel: cancel, done: make(chan struct{})}
	// done closes once the watcher and both loops have returned, so a stop
	// that waits on it deletes the cluster's series after the last goroutine
	// that could write one. Local to this start, so the group is never reused.
	var loops sync.WaitGroup
	loops.Add(2)
	go func() {
		defer loops.Done()
		if s.f.dedupPersist != "" && s.f.snapshotInterval > 0 {
			runSnapshotLoop(cctx, cache, s.f.snapshotInterval)
		}
	}()
	go func() {
		defer loops.Done()
		runSweepLoop(cctx, disp, sweepInterval)
	}()

	// Registered under the lock that wait takes once ctx is done, so a start
	// racing shutdown either lands in wait's snapshot or is refused here.
	s.mu.Lock()
	if ctx.Err() != nil {
		s.mu.Unlock()
		cancel()
		return fmt.Errorf("not started: %w", ctx.Err())
	}
	s.running[tc.Profile] = run
	if tc.Profile == directProfile && tc.ProjectID != "" {
		s.directIdentity = clusterprofiles.Identity{Project: tc.ProjectID, Location: tc.Location, Cluster: tc.Name}
	}
	s.mu.Unlock()

	// Published at 0 before the goroutine exists, and 1 only once the initial
	// list completes. Setting 1 any earlier would have reported every cluster
	// up the instant its goroutine started, including ones whose control
	// plane was unreachable — those block inside Run forever rather than
	// failing, so liveness of the goroutine proves nothing.
	s.m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
	loops.Add(1)
	go func() {
		loops.Wait()
		close(run.done)
	}()
	go func() {
		defer loops.Done()
		defer s.m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
		w := newWatcher(tc.Client, disp, tc)
		w.gate = s.gate
		log.Printf("k8s-event-watcher: [%s] starting: preflight, then informer (source=%s project=%s location=%s)",
			tc.Name, tc.Profile, tc.ProjectID, tc.Location)
		// The gauge follows every transition the watcher reports: 1 on the
		// initial sync and again when a held cluster recovers, 0 while a
		// 403 holds it. Only the first 1 counts toward synced — a recovery
		// is the same cluster coming back, not another one syncing — and
		// the guard is atomic because the watcher calls in from its own
		// goroutines.
		var counted atomic.Bool
		onWatching := func(watching bool) {
			if !watching {
				s.m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
				// Blind again: nothing it does not see can be called quiet.
				disp.readyAt.Store(0)
				log.Printf("k8s-event-watcher: [%s] events are not being delivered (the watch was refused or failed), no longer watching until the next successful attempt", tc.Name)
				return
			}
			s.m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(1)
			disp.readyAt.Store(disp.clock().UnixNano())
			if !counted.Swap(true) {
				s.synced.Add(1)
				log.Printf("k8s-event-watcher: [%s] informer synced, now watching", tc.Name)
				return
			}
			log.Printf("k8s-event-watcher: [%s] events permitted again, watching resumed", tc.Name)
		}
		if err := w.Run(cctx, onWatching); err != nil {
			// Log and continue — one cluster's informer failing
			// must not blind the rest. The peer goroutines keep
			// running.
			log.Printf("k8s-event-watcher: [%s] informer exited: %v", tc.Name, err)
		}
	}()
	return nil
}

// stop ends the watcher started for profile, waits for its goroutine to
// return, writes the cluster's dedup snapshot (the restore a restart would
// read, had the profile come back) and deletes every metric series carrying
// its labels, so a torn-down cluster does not read as a cluster at
// cluster_up=0 for the life of the process. A profile that is not running is
// a no-op.
//
// Once wait has begun shutting the set down (closed), a stop is a no-op: the
// run stays in the set, wait joins it and the shutdown snapshot writes it,
// which is what a removal would have done. A stop already in progress is
// joined by wait before it snapshots, so no removal's snapshot is cut by the
// process exiting.
func (s *clusterSet) stop(profile string) {
	s.mu.Lock()
	run, ok := s.running[profile]
	if ok && !s.closed {
		delete(s.running, profile)
		s.stopping++
	} else {
		ok = false
	}
	s.mu.Unlock()
	if !ok {
		return
	}
	defer func() {
		s.mu.Lock()
		s.stopping--
		s.stopped.Broadcast()
		s.mu.Unlock()
	}()
	run.cancel()
	<-run.done
	if err := run.cache.Snapshot(); err != nil {
		log.Printf("k8s-event-watcher: [%s] dedup snapshot on removal: %v", run.cluster.Name, err)
	}
	s.m.deleteClusterSeries(run.cluster.Name, run.cluster.ProjectID, run.cluster.Location)
}

// wait blocks until ctx is done and every watcher goroutine has returned.
// Watchers return only once their context, derived from ctx, ends (see
// watcher.Run), so there is nothing to wait for before then; and once ctx is
// done start refuses, under the same lock, so the snapshot taken here is
// every watcher there will be. A stop in progress is joined first, and a
// stop that arrives later finds the set closed and leaves its run here.
func (s *clusterSet) wait(ctx context.Context) {
	<-ctx.Done()
	s.mu.Lock()
	s.closed = true
	for s.stopping > 0 {
		s.stopped.Wait()
	}
	runs := make([]*clusterRun, 0, len(s.running))
	for _, run := range s.running {
		runs = append(runs, run)
	}
	s.mu.Unlock()
	for _, run := range runs {
		<-run.done
	}
}

// snapshotAll writes every running cluster's dedup snapshot, for shutdown.
func (s *clusterSet) snapshotAll() {
	s.mu.Lock()
	runs := make([]*clusterRun, 0, len(s.running))
	for _, run := range s.running {
		runs = append(runs, run)
	}
	s.mu.Unlock()
	for _, run := range runs {
		if err := run.cache.Snapshot(); err != nil {
			log.Printf("dedup snapshot on shutdown: %v", err)
		}
	}
}

// count is how many clusters are running.
func (s *clusterSet) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.running)
}

// profiles lists the running profiles, for logs and tests.
func (s *clusterSet) profiles() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.running))
	for p := range s.running {
		out = append(out, p)
	}
	return out
}

// watchedIdentities is the set of identity triples the running clusters
// stand for, by Identity.String(): every profile cluster's, and the direct
// cluster's once a profile has named it. The rescan's Want excludes these,
// so a cluster already watched costs no describe and is never started twice.
func (s *clusterSet) watchedIdentities() map[string]bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	ids := make(map[string]bool, len(s.running)+1)
	for profile, run := range s.running {
		if profile == directProfile {
			continue
		}
		tc := run.cluster
		ids[clusterprofiles.Identity{Project: tc.ProjectID, Location: tc.Location, Cluster: tc.Name}.String()] = true
	}
	if s.directIdentity != (clusterprofiles.Identity{}) {
		ids[s.directIdentity.String()] = true
	}
	return ids
}

// runProfileReload reads the profiles directory again every interval until
// ctx ends (see reconcileProfiles).
func runProfileReload(ctx context.Context, s *clusterSet, dir string, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.reconcileProfiles(ctx, dir)
		}
	}
}

// reconcileProfiles brings the running set in step with the profiles
// directory, which buildWatchSet read once at boot:
//
//   - a running profile cluster whose directory is gone is stopped (see stop);
//   - discovery runs again with Want excluding every cluster already watched,
//     so a profile written since the last read, or one skipped on an earlier
//     read because the GKE lookup was refused (a 429 or a 5xx, which Discover
//     skips rather than retries), is started now, through the same start as
//     boot; a profile already running under a different identity is replaced;
//   - a profile naming the direct cluster is absorbed as buildWatchSet absorbs
//     it at boot: the direct entry keeps the watch and the profile starts
//     nothing. The identity is recorded for the rescan's own bookkeeping; the
//     running direct entry's labels and payloads carry it from the next start,
//     since a dispatcher's cluster is fixed when it is built.
//
// A discovery error on a tick (the directory gone, the credential proxy
// unreachable) is logged and the running set is left as it is, never an exit:
// the boot already proved the set could be built, and the next tick asks again.
func (s *clusterSet) reconcileProfiles(ctx context.Context, dir string) {
	// The directory itself gone is not every profile removed: a volume that
	// has come unmounted says nothing about the clusters, and Discover would
	// refuse it anyway. Keep the set and ask again next tick.
	if _, err := os.Stat(dir); err != nil {
		log.Printf("k8s-event-watcher: profile rescan: %v; keeping the current watch set", err)
		return
	}
	s.mu.Lock()
	var removed []*clusterRun
	for profile, run := range s.running {
		if profile == directProfile {
			continue
		}
		if _, err := os.Stat(filepath.Join(dir, profile)); errors.Is(err, os.ErrNotExist) {
			removed = append(removed, run)
		}
	}
	s.mu.Unlock()
	for _, run := range removed {
		log.Printf("k8s-event-watcher: profile %s is gone; no longer watching %s", run.cluster.Profile, run.cluster.identity())
		s.stop(run.cluster.Profile)
	}

	watched := s.watchedIdentities()
	discovered, err := discoverClusterProfilesWanting(ctx, dir, s.m, func(id clusterprofiles.Identity) bool {
		return !watched[id.String()]
	})
	if err != nil {
		log.Printf("k8s-event-watcher: profile rescan of %s failed, keeping the current watch set: %v", dir, err)
		return
	}
	for _, tc := range discovered {
		if ctx.Err() != nil {
			return
		}
		absorbed, ambiguous := s.absorbIntoDirect(tc)
		if absorbed {
			continue
		}
		if ambiguous {
			s.stop(directProfile)
		}
		s.mu.Lock()
		_, replacing := s.running[tc.Profile]
		s.mu.Unlock()
		if replacing {
			log.Printf("k8s-event-watcher: profile %s now names %s; restarting its watcher", tc.Profile, tc.identity())
			s.stop(tc.Profile)
		} else {
			log.Printf("k8s-event-watcher: profile %s found on rescan; starting a watcher for %s", tc.Profile, tc.identity())
		}
		if err := s.start(ctx, tc); err != nil {
			log.Printf("k8s-event-watcher: [%s] %v, this cluster will NOT be watched", tc.Name, err)
			s.m.clusterUp.WithLabelValues(tc.Name, tc.ProjectID, tc.Location).Set(0)
		}
	}
}

// absorbIntoDirect decides what a discovered profile named like the direct
// cluster is. The match is the one buildWatchSet makes at boot, on
// --cluster-name against the profile's cluster name, and it is a guess: the
// in-cluster client reads no cluster_identity, so a bare name is all there is
// to compare, and a fleet may run two clusters of that name in different
// projects or locations. Boot resolves the guess by count: one same-named
// profile is absorbed, two or more and the direct entry is dropped and every
// one of them watched through its profile. The rescan sees the profiles one
// at a time, so the same rule plays out in two steps.
//
// absorbed: no profile has named the direct cluster yet, or this one names
// the identity already recorded; the identity is recorded and the caller
// starts nothing. ambiguous: a profile has already been absorbed and this one
// carries a different project or location, so the earlier absorption was a
// guess between two clusters. The recorded identity is cleared and the caller
// stops the direct entry and starts this profile; the one absorbed earlier is
// no longer excluded by identity, so the next read discovers it and starts it
// too, which leaves both watched through their profiles and the direct entry
// gone, as at boot. Neither is true when no direct entry is running or the
// name differs.
func (s *clusterSet) absorbIntoDirect(tc targetCluster) (absorbed, ambiguous bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, direct := s.running[directProfile]; !direct || tc.Name != s.f.clusterName {
		return false, false
	}
	id := clusterprofiles.Identity{Project: tc.ProjectID, Location: tc.Location, Cluster: tc.Name}
	if s.directIdentity != (clusterprofiles.Identity{}) && s.directIdentity != id {
		log.Printf("k8s-event-watcher: profiles name both %s and %s %q, and the in-cluster client cannot say which one it is; watching both through their profiles and dropping the direct entry",
			s.directIdentity, id, s.f.clusterName)
		s.directIdentity = clusterprofiles.Identity{}
		return false, true
	}
	s.directIdentity = id
	log.Printf("k8s-event-watcher: profile %s names %s, which the direct client already watches as %s; starting no second watcher for it",
		tc.Profile, id, s.f.clusterName)
	return true, false
}
