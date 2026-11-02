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
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

// countServer stands in for one cluster's API server for the five count
// lists: it answers each path from counts as the real server would answer
// a limit=1 list with the metadata Accept (one item plus remainingItemCount
// when more remain, the item alone when it is the only one, nothing when
// there are none), and records every request's path, query and Accept.
type countServer struct {
	mu       sync.Mutex
	requests []string // "path?query accept"
	counts   map[string]int64
	// status, when set, answers every request with it; delay holds each
	// answer that long first.
	status int
	delay  time.Duration
	// pageWithoutRemaining answers with a continue token and no
	// remainingItemCount: a list the server paged but did not count.
	pageWithoutRemaining bool
}

func (s *countServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.requests = append(s.requests, r.URL.Path+"?"+r.URL.RawQuery+" "+r.Header.Get("Accept"))
	status, delay, total, paged := s.status, s.delay, s.counts[r.URL.Path], s.pageWithoutRemaining
	s.mu.Unlock()
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-r.Context().Done():
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if status != 0 {
		w.WriteHeader(status)
		_, _ = fmt.Fprintf(w, `{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"Forbidden","code":%d}`, status)
		return
	}
	const item = `{"metadata":{"name":"one","namespace":"default"}}`
	switch {
	case paged:
		_, _ = fmt.Fprintf(w, `{"kind":"PartialObjectMetadataList","apiVersion":"meta.k8s.io/v1","metadata":{"continue":"more"},"items":[%s]}`, item)
	case total == 0:
		_, _ = fmt.Fprint(w, `{"kind":"PartialObjectMetadataList","apiVersion":"meta.k8s.io/v1","metadata":{},"items":[]}`)
	case total == 1:
		_, _ = fmt.Fprintf(w, `{"kind":"PartialObjectMetadataList","apiVersion":"meta.k8s.io/v1","metadata":{},"items":[%s]}`, item)
	default:
		_, _ = fmt.Fprintf(w, `{"kind":"PartialObjectMetadataList","apiVersion":"meta.k8s.io/v1","metadata":{"continue":"more","remainingItemCount":%d},"items":[%s]}`, total-1, item)
	}
}

func (s *countServer) seen() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.requests...)
}

// newCountCluster starts a countServer and returns it with a targetCluster
// read through the binary's own client (newClusterClient, protobuf-first, so
// the test proves the count's Accept overrides that preference).
func newCountCluster(t *testing.T, name, project string, counts map[string]int64) (*countServer, targetCluster) {
	t.Helper()
	s := &countServer{counts: counts}
	srv := httptest.NewServer(s)
	t.Cleanup(srv.Close)
	client, err := newClusterClient(&rest.Config{Host: srv.URL})
	if err != nil {
		t.Fatal(err)
	}
	tc := targetCluster{Name: name, Profile: "cluster-" + name, Client: client}
	if project != "" {
		tc.ProjectID, tc.Location = project, "us-central1"
	}
	return s, tc
}

// shapeCounts builds a counts table from the five numbers in path order.
func shapeCounts(pods, deployments, statefulsets, daemonsets, cronjobs int64) map[string]int64 {
	return map[string]int64{
		"/api/v1/pods":               pods,
		"/apis/apps/v1/deployments":  deployments,
		"/apis/apps/v1/statefulsets": statefulsets,
		"/apis/apps/v1/daemonsets":   daemonsets,
		"/apis/batch/v1/cronjobs":    cronjobs,
	}
}

// newTestProbe builds a probe over clusters whose describe answers from
// nodes by cluster name, on a fresh metrics registry.
func newTestProbe(clusters []targetCluster, nodes map[string]int64) (*fleetShapeProbe, *metrics) {
	m := newMetrics()
	p := newFleetShapeProbe(func() []targetCluster { return clusters }, m)
	p.describe = func(_ context.Context, id clusterprofiles.Identity) (*container.Cluster, error) {
		n, ok := nodes[id.Cluster]
		if !ok {
			return nil, errors.New("no such cluster")
		}
		return &container.Cluster{Name: id.Cluster, CurrentNodeCount: n}, nil
	}
	return p, m
}

func gauge(t *testing.T, m *metrics, name string) float64 {
	t.Helper()
	switch name {
	case "nodes":
		return testutil.ToFloat64(m.fleetNodes)
	case "max_cluster_nodes":
		return testutil.ToFloat64(m.fleetMaxClusterNodes)
	case "controllers":
		return testutil.ToFloat64(m.fleetControllers)
	case "pods":
		return testutil.ToFloat64(m.fleetPods)
	case "max_cluster_pods":
		return testutil.ToFloat64(m.fleetMaxClusterPods)
	case "counted":
		return testutil.ToFloat64(m.fleetShapeClusters.WithLabelValues(fleetShapeOutcomeCounted))
	case "failed":
		return testutil.ToFloat64(m.fleetShapeClusters.WithLabelValues(fleetShapeOutcomeFailed))
	}
	t.Fatalf("unknown gauge %q", name)
	return 0
}

func expectGauges(t *testing.T, m *metrics, want map[string]float64) {
	t.Helper()
	names := make([]string, 0, len(want))
	for name := range want {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if got := gauge(t, m, name); got != want[name] {
			t.Errorf("fleet_%s = %v; want %v", name, got, want[name])
		}
	}
}

// Three clusters with distinct remainingItemCount answers: the totals are the
// sums, the max gauges the largest single cluster, nodes come from the
// describe for the two clusters with an identity and are zero for the direct
// cluster no profile named, and every request carried limit=1 and the
// metadata-only Accept.
func TestFleetShapeProbe_CountsThreeClustersExactly(t *testing.T) {
	a, tcA := newCountCluster(t, "a", "p", shapeCounts(120, 10, 2, 3, 1))
	b, tcB := newCountCluster(t, "b", "p", shapeCounts(45, 0, 1, 0, 0))
	c, tcC := newCountCluster(t, "c", "", shapeCounts(1, 1, 1, 1, 1))
	p, m := newTestProbe([]targetCluster{tcA, tcB, tcC}, map[string]int64{"a": 30, "b": 4})

	shape := p.probe(context.Background())

	if shape.counted != 3 || shape.failed != 0 {
		t.Fatalf("counted=%d failed=%d; want 3 and 0", shape.counted, shape.failed)
	}
	expectGauges(t, m, map[string]float64{
		"nodes":             34,
		"max_cluster_nodes": 30,
		"controllers":       16 + 1 + 4,
		"pods":              120 + 45 + 1,
		"max_cluster_pods":  120,
		"counted":           3,
		"failed":            0,
	})
	for name, srv := range map[string]*countServer{"a": a, "b": b, "c": c} {
		seen := srv.seen()
		if len(seen) != 5 {
			t.Errorf("[%s] %d requests; want exactly the five count lists: %v", name, len(seen), seen)
		}
		paths := map[string]bool{}
		for _, req := range seen {
			if !strings.Contains(req, "?limit=1 ") {
				t.Errorf("[%s] request %q did not carry limit=1 as its whole query", name, req)
			}
			if !strings.HasSuffix(req, " "+fleetShapeAccept) {
				t.Errorf("[%s] request %q did not carry the metadata-only Accept %q", name, req, fleetShapeAccept)
			}
			paths[req[:strings.Index(req, "?")]] = true
		}
		for _, l := range fleetShapeListPaths {
			if !paths[l.path] {
				t.Errorf("[%s] %s was not read", name, l.path)
			}
		}
	}
}

// A cluster with one failed read is left out of the totals and counted as
// failed: a 403 on a list, a read past the call timeout, and a describe that
// errors. The one healthy cluster is the whole reading.
func TestFleetShapeProbe_AFailedReadLeavesTheClusterOut(t *testing.T) {
	_, healthy := newCountCluster(t, "healthy", "p", shapeCounts(7, 1, 1, 1, 1))
	forbidden, tcForbidden := newCountCluster(t, "forbidden", "p", shapeCounts(1000, 1, 1, 1, 1))
	forbidden.status = http.StatusForbidden
	slow, tcSlow := newCountCluster(t, "slow", "p", shapeCounts(1000, 1, 1, 1, 1))
	slow.delay = 2 * time.Second
	_, tcUndescribed := newCountCluster(t, "undescribed", "p", shapeCounts(1000, 1, 1, 1, 1))
	p, m := newTestProbe([]targetCluster{healthy, tcForbidden, tcSlow, tcUndescribed}, map[string]int64{"healthy": 2, "forbidden": 9, "slow": 9})
	p.callTimeout = 100 * time.Millisecond

	start := time.Now()
	shape := p.probe(context.Background())
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("the probe took %s; the slow cluster's reads should have been cut at the call timeout", elapsed)
	}
	if shape.counted != 1 || shape.failed != 3 {
		t.Fatalf("counted=%d failed=%d; want 1 and 3", shape.counted, shape.failed)
	}
	expectGauges(t, m, map[string]float64{
		"nodes":             2,
		"max_cluster_nodes": 2,
		"controllers":       4,
		"pods":              7,
		"max_cluster_pods":  7,
		"counted":           1,
		"failed":            3,
	})
}

// A list the server paged without saying how many remain cannot be counted
// from one item, and the probe does not walk it: the cluster reads as failed
// rather than as a cluster of one pod.
func TestCountList_APagedListWithoutARemainingCountIsAnError(t *testing.T) {
	s, tc := newCountCluster(t, "paged", "", shapeCounts(5, 5, 5, 5, 5))
	s.pageWithoutRemaining = true
	_, err := countList(context.Background(), tc.Client, "/api/v1/pods", time.Second)
	if !errors.Is(err, errListUncountable) {
		t.Fatalf("err = %v; want errListUncountable", err)
	}
	p, m := newTestProbe([]targetCluster{tc}, nil)
	p.probe(context.Background())
	expectGauges(t, m, map[string]float64{"pods": 0, "counted": 0, "failed": 1})
}

// The two shapes a complete list takes: no items, and one item with no
// remaining count. Both are their length.
func TestCountList_ACompleteListIsItsLength(t *testing.T) {
	_, tc := newCountCluster(t, "small", "", shapeCounts(0, 1, 0, 0, 0))
	for path, want := range map[string]int64{"/api/v1/pods": 0, "/apis/apps/v1/deployments": 1} {
		n, err := countList(context.Background(), tc.Client, path, time.Second)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if n != want {
			t.Errorf("%s = %d; want %d", path, n, want)
		}
	}
}

// Before the first probe every gauge reads 0 and both outcome children
// exist, so a scrape finds counted=0 rather than an absent series.
func TestNewMetrics_FleetGaugesStartAtZeroWithBothOutcomes(t *testing.T) {
	m := newMetrics()
	expectGauges(t, m, map[string]float64{
		"nodes": 0, "max_cluster_nodes": 0, "controllers": 0, "pods": 0, "max_cluster_pods": 0, "counted": 0, "failed": 0,
	})
	families, err := m.registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]int{}
	for _, f := range families {
		names[f.GetName()] = len(f.GetMetric())
	}
	for _, name := range []string{
		"k8s_event_watcher_fleet_nodes", "k8s_event_watcher_fleet_max_cluster_nodes", "k8s_event_watcher_fleet_controllers",
		"k8s_event_watcher_fleet_pods", "k8s_event_watcher_fleet_max_cluster_pods",
	} {
		if names[name] != 1 {
			t.Errorf("%s: %d series before the first probe; want 1", name, names[name])
		}
	}
	if names["k8s_event_watcher_fleet_shape_clusters"] != 2 {
		t.Errorf("k8s_event_watcher_fleet_shape_clusters: %d series before the first probe; want counted and failed", names["k8s_event_watcher_fleet_shape_clusters"])
	}
}

// The loop reads once at start and then only on its ticks: with an interval
// of an hour, the server sees the five count lists exactly once however long
// the loop runs, and the gauges carry that one reading.
func TestRunFleetShapeLoop_ReadsNothingBetweenTicks(t *testing.T) {
	s, tc := newCountCluster(t, "only", "", shapeCounts(3, 1, 1, 1, 1))
	p, m := newTestProbe([]targetCluster{tc}, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		runFleetShapeLoop(ctx, p, time.Hour)
	}()
	deadline := time.Now().Add(5 * time.Second)
	for gauge(t, m, "counted") != 1 {
		if time.Now().After(deadline) {
			t.Fatal("the loop's first probe did not publish within 5s")
		}
		time.Sleep(10 * time.Millisecond)
	}
	// Long enough for a second probe to have run if anything but the ticker
	// drove one.
	time.Sleep(200 * time.Millisecond)
	cancel()
	<-done
	if seen := s.seen(); len(seen) != 5 {
		t.Errorf("%d requests after the first probe; want exactly 5 (nothing between ticks): %v", len(seen), seen)
	}
	expectGauges(t, m, map[string]float64{"pods": 3, "controllers": 4, "counted": 1, "failed": 0})
}

// The probe reads the watch set as it stands at each probe: a cluster a
// rescan removed is gone from the next reading and one it added is in it.
func TestFleetShapeProbe_FollowsTheRunningSet(t *testing.T) {
	_, tcA := newCountCluster(t, "a", "", shapeCounts(10, 0, 0, 0, 0))
	_, tcB := newCountCluster(t, "b", "", shapeCounts(20, 0, 0, 0, 0))
	var mu sync.Mutex
	current := []targetCluster{tcA}
	m := newMetrics()
	p := newFleetShapeProbe(func() []targetCluster {
		mu.Lock()
		defer mu.Unlock()
		return append([]targetCluster(nil), current...)
	}, m)
	p.probe(context.Background())
	expectGauges(t, m, map[string]float64{"pods": 10, "counted": 1})
	mu.Lock()
	current = []targetCluster{tcB}
	mu.Unlock()
	p.probe(context.Background())
	expectGauges(t, m, map[string]float64{"pods": 20, "max_cluster_pods": 20, "counted": 1, "failed": 0})
}

// clusterSet.clusters is what the probe reads in the binary: every running
// cluster, in profile order, with the direct entry under directProfile.
func TestClusterSet_ClustersListsTheRunningSetInProfileOrder(t *testing.T) {
	_, tcB := newCountCluster(t, "b", "p", nil)
	_, tcA := newCountCluster(t, "a", "p", nil)
	s := newClusterSet(&flags{dedupWindow: time.Minute, clusterName: "host"}, newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})), nil, newMetrics(), nil)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	for _, tc := range []targetCluster{tcB, tcA} {
		if err := s.start(ctx, tc); err != nil {
			t.Fatal(err)
		}
	}
	got := s.clusters()
	if len(got) != 2 || got[0].Profile != "cluster-a" || got[1].Profile != "cluster-b" {
		t.Errorf("clusters() = %v; want cluster-a then cluster-b", identities(got))
	}
	var _ kubernetes.Interface = got[0].Client
	s.stop("cluster-a")
	if got := s.clusters(); len(got) != 1 || got[0].Profile != "cluster-b" {
		t.Errorf("after stopping cluster-a, clusters() = %v; want cluster-b alone", identities(got))
	}
}
