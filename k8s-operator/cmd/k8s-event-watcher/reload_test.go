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
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

// reloadFixture is a clusterSet over a stubbed GKE and a temp profiles
// directory. Every described cluster's endpoint is a closed local port, so a
// watcher started for it fails its first connection at once rather than
// hanging in DNS, and cancelling the fixture's ctx ends it.
type reloadFixture struct {
	t        *testing.T
	ctx      context.Context
	cancel   context.CancelFunc
	dir      string
	persist  string
	set      *clusterSet
	failures map[string]error
}

func newReloadFixture(t *testing.T, clusterName string) *reloadFixture {
	t.Helper()
	captureLog(t)
	failures := stubGKE(t)
	base := discovery.Describe
	discovery.Describe = func(ctx context.Context, id clusterprofiles.Identity) (*container.Cluster, error) {
		c, err := base(ctx, id)
		if err != nil {
			return nil, err
		}
		c.Endpoint = "127.0.0.1:1"
		return c, nil
	}
	dir := t.TempDir()
	persist := filepath.Join(t.TempDir(), "dedup.json")
	f := &flags{
		mode:         "per-incident",
		dryRun:       true,
		dedupWindow:  5 * time.Minute,
		dedupPersist: persist,
		clusterName:  clusterName,
		profilesDir:  dir,
	}
	m := newMetrics()
	set := newClusterSet(f, newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})), nil, m, newListGate(2))
	ctx, cancel := context.WithCancel(context.Background())
	fx := &reloadFixture{t: t, ctx: ctx, cancel: cancel, dir: dir, persist: persist, set: set, failures: failures}
	t.Cleanup(func() {
		cancel()
		set.wait(ctx)
	})
	return fx
}

func (fx *reloadFixture) profiles() []string {
	got := fx.set.profiles()
	sort.Strings(got)
	return got
}

func (fx *reloadFixture) clusterUpSeries() int {
	return testutil.CollectAndCount(fx.set.m.clusterUp)
}

func TestReconcileProfiles_StartsAProfileWrittenAfterBootAndStopsOneRemoved(t *testing.T) {
	fx := newReloadFixture(t, "host")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.profiles(); len(got) != 0 {
		t.Fatalf("running after a rescan of an empty directory = %v; want none", got)
	}

	writeClusterProfile(t, fx.dir, "cluster-a", "proj", "a", "us-central1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-a"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the profile was written = %v; want %v", got, want)
	}
	if got := fx.clusterUpSeries(); got != 1 {
		t.Fatalf("cluster_up series = %d; want 1 for the started cluster", got)
	}
	// A second rescan with nothing changed starts nothing twice: the running
	// cluster is excluded by identity before any describe.
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.set.count(); got != 1 {
		t.Fatalf("running after an unchanged rescan = %d; want 1", got)
	}

	if err := os.RemoveAll(filepath.Join(fx.dir, "cluster-a")); err != nil {
		t.Fatal(err)
	}
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.profiles(); len(got) != 0 {
		t.Fatalf("running after the profile was removed = %v; want none", got)
	}
	if _, err := os.Stat(dedupPersistPath(fx.persist, "cluster-a")); err != nil {
		t.Errorf("no dedup snapshot written on removal: %v", err)
	}
	if got := fx.clusterUpSeries(); got != 0 {
		t.Errorf("cluster_up series after removal = %d; want 0 (the removed cluster's series must be deleted)", got)
	}
}

// A profile whose GKE lookup was refused on one read is tried again on the
// next, with no retry code of its own: a 429 for the first read and an answer
// for the second is a cluster watched from the second rescan.
func TestReconcileProfiles_RetriesAProfileWhoseDescribeWasRefused(t *testing.T) {
	fx := newReloadFixture(t, "host")
	writeClusterProfile(t, fx.dir, "cluster-b", "proj", "b", "us-central1")
	fx.failures["proj/us-central1/b"] = errors.New("googleapi: Error 429: Quota exceeded")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.set.count(); got != 0 {
		t.Fatalf("running after a refused describe = %d; want 0", got)
	}
	if got := testutil.ToFloat64(fx.set.m.clusterDiscoveryErrors.WithLabelValues("cluster-b")); got != 1 {
		t.Fatalf("cluster_discovery_errors_total{profile=cluster-b} = %v; want 1", got)
	}

	delete(fx.failures, "proj/us-central1/b")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-b"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the describe succeeded = %v; want %v", got, want)
	}
}

// The direct cluster's own profile, written by the first reconcile tick after
// a fresh install, starts no second watcher for it. A second profile with the
// same name in another project or location makes the bare-name match a guess,
// as it does at boot: the direct entry is dropped and both clusters are
// watched through their profiles, the second on the read that found it and
// the first on the read after, once its identity is no longer excluded.
func TestReconcileProfiles_AbsorbsTheDirectClustersProfile(t *testing.T) {
	fx := newReloadFixture(t, "host")
	if err := fx.set.start(fx.ctx, targetCluster{Name: "host", Profile: directProfile, Client: fake.NewClientset()}); err != nil {
		t.Fatal(err)
	}
	writeClusterProfile(t, fx.dir, "cluster-host", "proj", "host", "us-central1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{directProfile}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the direct cluster's profile appeared = %v; want %v only", got, want)
	}
	if got := fx.set.directIdentity.String(); got != "proj/us-central1/host" {
		t.Fatalf("directIdentity = %q; want the absorbed profile's", got)
	}
	// Absorbed, and so watched: excluded from the next read's describes.
	fx.failures["proj/us-central1/host"] = errors.New("must not be described again")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := testutil.ToFloat64(fx.set.m.clusterDiscoveryErrors.WithLabelValues("cluster-host")); got != 0 {
		t.Fatalf("the absorbed profile was described again (%v discovery errors)", got)
	}
	delete(fx.failures, "proj/us-central1/host")

	writeClusterProfile(t, fx.dir, "cluster-host-eu", "proj2", "host", "europe-west1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-host-eu"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after a same-named profile in another location = %v; want %v (direct dropped)", got, want)
	}
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-host", "cluster-host-eu"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running one read later = %v; want both same-named clusters through their profiles", got)
	}
	if got := fx.set.directIdentity; got != (clusterprofiles.Identity{}) {
		t.Errorf("directIdentity = %q after the ambiguity; want cleared", got)
	}
}

// The order the first reconcile tick can produce on a fresh install: the
// foreign same-named profile is read first and absorbed on the guess, and the
// host's own profile arrives after. The guess is undone the same way, so the
// foreign cluster is not left unwatched and the host is not watched twice.
func TestReconcileProfiles_UndoesAWrongAbsorptionWhenTheHostsProfileArrives(t *testing.T) {
	fx := newReloadFixture(t, "host")
	if err := fx.set.start(fx.ctx, targetCluster{Name: "host", Profile: directProfile, Client: fake.NewClientset()}); err != nil {
		t.Fatal(err)
	}
	writeClusterProfile(t, fx.dir, "cluster-host-eu", "proj2", "host", "europe-west1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{directProfile}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the foreign profile = %v; want %v (absorbed on the guess)", got, want)
	}
	writeClusterProfile(t, fx.dir, "cluster-host", "proj", "host", "us-central1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-host", "cluster-host-eu"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the host's profile and one more read = %v; want %v, no direct entry", got, want)
	}
	// Both profiles present in one read, direct identity unknown: the same
	// two-step outcome whichever is read first.
	fx2 := newReloadFixture(t, "host")
	if err := fx2.set.start(fx2.ctx, targetCluster{Name: "host", Profile: directProfile, Client: fake.NewClientset()}); err != nil {
		t.Fatal(err)
	}
	writeClusterProfile(t, fx2.dir, "cluster-a-host", "a-proj", "host", "europe-west1")
	writeClusterProfile(t, fx2.dir, "cluster-b-host", "b-proj", "host", "us-central1")
	fx2.set.reconcileProfiles(fx2.ctx, fx2.dir)
	fx2.set.reconcileProfiles(fx2.ctx, fx2.dir)
	if got, want := fx2.profiles(), []string{"cluster-a-host", "cluster-b-host"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after both same-named profiles in one read = %v; want %v, no direct entry", got, want)
	}
}

// A stop running on the reload goroutine when shutdown begins is joined by
// wait, so its snapshot is written before the process exits; a stop that
// arrives after wait has closed the set leaves the run to the shutdown
// snapshot.
func TestClusterSet_WaitJoinsAStopInProgress(t *testing.T) {
	fx := newReloadFixture(t, "host")
	if err := fx.set.start(fx.ctx, targetCluster{Name: "host", Profile: directProfile, Client: fake.NewClientset()}); err != nil {
		t.Fatal(err)
	}
	stopDone := make(chan struct{})
	go func() {
		fx.set.stop(directProfile)
		close(stopDone)
	}()
	fx.cancel()
	fx.set.wait(fx.ctx)
	select {
	case <-stopDone:
	case <-time.After(50 * time.Millisecond):
		t.Fatal("wait returned while a stop was still in progress")
	}
	// As realMain does after wait. Whichever won, the stop's own snapshot or
	// the shutdown's over a run the closed set kept, the file is there.
	fx.set.snapshotAll()
	if _, err := os.Stat(dedupPersistPath(fx.persist, directProfile)); err != nil {
		t.Errorf("the stopped cluster's snapshot is missing: %v", err)
	}
	// After close, a stop is a no-op and the run is left for snapshotAll.
	if err := fx.set.start(fx.ctx, targetCluster{Name: "late", Profile: "cluster-late", Client: fake.NewClientset()}); err == nil {
		t.Fatal("start after shutdown must be refused")
	}
}

// A profile that comes to name a different cluster is restarted under the new
// identity; the old watcher is stopped first, so the profile's snapshot file
// is never shared between two caches.
func TestReconcileProfiles_ReplacesAProfileWhoseIdentityChanged(t *testing.T) {
	fx := newReloadFixture(t, "host")
	writeClusterProfile(t, fx.dir, "cluster-x", "proj", "x", "us-central1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	writeClusterProfile(t, fx.dir, "cluster-x", "proj", "x", "europe-west1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.set.count(); got != 1 {
		t.Fatalf("running = %d; want 1", got)
	}
	fx.set.mu.Lock()
	loc := fx.set.running["cluster-x"].cluster.Location
	fx.set.mu.Unlock()
	if loc != "europe-west1" {
		t.Fatalf("cluster-x runs as location %q; want europe-west1", loc)
	}
}

// A read that fails as a whole — the directory gone — changes nothing: the
// running set is kept and the tick is logged, never an exit.
func TestReconcileProfiles_DiscoveryErrorKeepsTheRunningSet(t *testing.T) {
	fx := newReloadFixture(t, "host")
	logs := captureLog(t)
	writeClusterProfile(t, fx.dir, "cluster-a", "proj", "a", "us-central1")
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got := fx.set.count(); got != 1 {
		t.Fatalf("running = %d; want 1", got)
	}
	if err := os.RemoveAll(fx.dir); err != nil {
		t.Fatal(err)
	}
	fx.set.reconcileProfiles(fx.ctx, fx.dir)
	if got, want := fx.profiles(), []string{"cluster-a"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("running after the directory vanished = %v; want %v kept", got, want)
	}
	if !strings.Contains(logs.String(), "keeping the current watch set") {
		t.Errorf("no line saying the set was kept in:\n%s", logs.String())
	}
}

func TestParseFlags_RescanAndGateDefaults(t *testing.T) {
	f, err := parseFlags([]string{"--dry-run", "--cluster-name", "x"})
	if err != nil {
		t.Fatal(err)
	}
	if f.profilesRescan != time.Minute {
		t.Errorf("--profiles-rescan default = %s; want 1m", f.profilesRescan)
	}
	if f.initialListConcurrency != 4 {
		t.Errorf("--initial-list-concurrency default = %d; want 4", f.initialListConcurrency)
	}
	if err := f.validate(); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"--dry-run", "--cluster-name", "x", "--profiles-rescan", "-1s"},
		{"--dry-run", "--cluster-name", "x", "--initial-list-concurrency", "-1"},
	} {
		f, err := parseFlags(args)
		if err != nil {
			t.Fatal(err)
		}
		if err := f.validate(); err == nil {
			t.Errorf("validate(%v) = nil; want an error for the negative value", args)
		}
	}
}
