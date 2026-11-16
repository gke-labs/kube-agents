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
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"sync"
	"time"

	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/clusterprofiles"
)

const (
	// defaultFleetShapeInterval is how often the fleet's shape is measured
	// (--fleet-shape-interval): the operator's usage poll cadence, so a
	// reading is at most one poll older than the series it rides beside, and
	// five lists plus one describe per cluster every five minutes is a cost
	// no API server notices.
	defaultFleetShapeInterval = 5 * time.Minute
	// fleetShapeWorkers is how many clusters are measured at once. The reads
	// are tiny, so the bound is on connections and on the credential proxy's
	// slots, not on memory.
	fleetShapeWorkers = 8
	// fleetShapeCallTimeout bounds each read: a control plane that does not
	// answer a one-item list in this long is not going to, and the probe has
	// a fleet to finish inside its interval.
	fleetShapeCallTimeout = 5 * time.Second
	// fleetShapeListLimit is the one item each list asks for. The count is
	// the item plus the server's remainingItemCount, so the body never
	// carries more than one object's metadata.
	fleetShapeListLimit = "1"
	// fleetShapeAccept asks the API server for metadata only: a
	// PartialObjectMetadataList carries each item's ObjectMeta and nothing
	// of its spec or status. ObjectMeta is the whole of it, annotations
	// included, so the one item a count returns can still carry a
	// kubectl.kubernetes.io/last-applied-configuration annotation, which is
	// the manifest as applied, template and inline env and all, on an
	// object a client-side kubectl apply created; the Accept bounds the
	// body to one object's metadata, not to names. The credential proxy
	// admits a count list only under this shape of Accept (watcher_policy's
	// count-list rule), so the header is the rule as much as the saving.
	fleetShapeAccept = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
	// listLimitParam and acceptHeader name the query key and header the
	// count list is held to.
	listLimitParam = "limit"
	acceptHeader   = "Accept"
	// The outcomes of k8s_event_watcher_fleet_shape_clusters: a cluster whose
	// six reads all answered and is in the totals, or one with a read that
	// failed, which contributes nothing, so the totals read as partial.
	fleetShapeOutcomeCounted = "counted"
	fleetShapeOutcomeFailed  = "failed"
)

// fleetShapeListPaths are the five cluster-scoped lists a count is read from:
// pods on their own, and the four controller kinds summed as controllers. The
// credential proxy's count-list rule admits exactly these paths, so a path
// added here without the same addition there is refused through the broker
// and the cluster reads as failed.
var fleetShapeListPaths = []fleetShapeListPath{
	{path: "/api/v1/pods", pods: true},
	{path: "/apis/apps/v1/deployments"},
	{path: "/apis/apps/v1/statefulsets"},
	{path: "/apis/apps/v1/daemonsets"},
	{path: "/apis/batch/v1/cronjobs"},
}

// errListUncountable is a list the server paged (a continue token) without
// saying how many items remain, which happens when the request carried a
// selector or the server chose not to count. The probe sends no selector, so
// it is not expected; counting such a list would mean walking it, which this
// probe does not do.
var errListUncountable = errors.New("the list is paged without a remainingItemCount, so it cannot be counted from one item")

type fleetShapeListPath struct {
	path string
	// pods says the count is the pod count rather than part of the
	// controller sum.
	pods bool
}

// clusterShape is one cluster's reading.
type clusterShape struct {
	nodes       int64
	pods        int64
	controllers int64
}

// fleetShape is one probe's reading over the watch set: the totals, the
// largest single cluster's nodes and pods, and how many clusters are in the
// totals against how many are not.
type fleetShape struct {
	nodes           int64
	maxClusterNodes int64
	controllers     int64
	pods            int64
	maxClusterPods  int64
	counted         int
	failed          int
}

// fleetShapeProbe measures the watch set's shape and publishes it as the
// fleet gauges. One per process; runFleetShapeLoop drives it.
type fleetShapeProbe struct {
	// clusters is the watch set as it stands at the probe, so a cluster a
	// rescan added or removed is in or out of the next reading.
	clusters func() []targetCluster
	// describe reads a cluster's GKE description for its node count, through
	// whatever discovery reaches the GKE API with. A test replaces it.
	describe func(ctx context.Context, id clusterprofiles.Identity) (*container.Cluster, error)
	m        *metrics
	// callTimeout bounds each read, workers how many clusters are measured
	// at once; fields so a test can shorten the first.
	callTimeout time.Duration
	workers     int
}

func newFleetShapeProbe(clusters func() []targetCluster, m *metrics) *fleetShapeProbe {
	return &fleetShapeProbe{
		clusters:    clusters,
		describe:    discovery.DescribeCluster,
		m:           m,
		callTimeout: fleetShapeCallTimeout,
		workers:     fleetShapeWorkers,
	}
}

// runFleetShapeLoop measures once at start and then every interval until ctx
// ends. Nothing is read between ticks: a scrape of /metrics reads the gauges
// the last probe set, so the reading is at most interval old and a scrape
// costs the fleet nothing.
func runFleetShapeLoop(ctx context.Context, p *fleetShapeProbe, interval time.Duration) {
	p.probe(ctx)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			p.probe(ctx)
		}
	}
}

// probe measures every cluster in the watch set, workers at a time, and
// publishes the reading. A cluster with any failed read is left out of the
// totals and counted as failed, so a total is never a mix of this probe's
// numbers and a guess; the previous reading's share of it is simply gone, and
// the failed count says so.
func (p *fleetShapeProbe) probe(ctx context.Context) fleetShape {
	clusters := p.clusters()
	type outcome struct {
		shape clusterShape
		err   error
	}
	outcomes := make([]outcome, len(clusters))
	slots := make(chan struct{}, p.workers)
	var wg sync.WaitGroup
	for i, tc := range clusters {
		wg.Add(1)
		go func(i int, tc targetCluster) {
			defer wg.Done()
			slots <- struct{}{}
			defer func() { <-slots }()
			shape, err := p.measureCluster(ctx, tc)
			outcomes[i] = outcome{shape: shape, err: err}
		}(i, tc)
	}
	wg.Wait()

	var total fleetShape
	for i, o := range outcomes {
		if o.err != nil {
			total.failed++
			if ctx.Err() == nil {
				log.Printf("k8s-event-watcher: [%s] fleet shape: not counted this probe: %v", clusters[i].Name, o.err)
			}
			continue
		}
		total.counted++
		total.nodes += o.shape.nodes
		total.pods += o.shape.pods
		total.controllers += o.shape.controllers
		total.maxClusterNodes = max(total.maxClusterNodes, o.shape.nodes)
		total.maxClusterPods = max(total.maxClusterPods, o.shape.pods)
	}
	if ctx.Err() != nil {
		// Shutting down: the reads were cut, and a reading made of cut
		// reads is not one to publish.
		return total
	}
	p.m.publishFleetShape(total)
	log.Printf("k8s-event-watcher: fleet shape: %d cluster(s) counted, %d failed: nodes=%d (largest cluster %d) controllers=%d pods=%d (largest cluster %d)",
		total.counted, total.failed, total.nodes, total.maxClusterNodes, total.controllers, total.pods, total.maxClusterPods)
	return total
}

// measureCluster reads one cluster: its node count from the GKE describe,
// when the cluster has an identity to describe (the direct cluster that no
// profile named has none, and contributes zero nodes), then the five counts.
// The first failure ends the reading; the caller treats the cluster as failed.
func (p *fleetShapeProbe) measureCluster(ctx context.Context, tc targetCluster) (clusterShape, error) {
	var shape clusterShape
	if tc.ProjectID != "" {
		cctx, cancel := context.WithTimeout(ctx, p.callTimeout)
		described, err := p.describe(cctx, clusterprofiles.Identity{Project: tc.ProjectID, Location: tc.Location, Cluster: tc.Name})
		cancel()
		if err != nil {
			return shape, fmt.Errorf("describe: %w", err)
		}
		shape.nodes = described.CurrentNodeCount
	}
	for _, l := range fleetShapeListPaths {
		n, err := countList(ctx, tc.Client, l.path, p.callTimeout)
		if err != nil {
			return shape, fmt.Errorf("%s: %w", l.path, err)
		}
		if l.pods {
			shape.pods = n
		} else {
			shape.controllers += n
		}
	}
	return shape, nil
}

// countList is how many objects a cluster-scoped list holds, read as the web
// console reads it: one item with the metadata-only Accept, plus the
// remainingItemCount the server reports beside it. A list the server did not
// page is complete, and its length is the count; one it paged without a
// remaining count is errListUncountable.
func countList(ctx context.Context, client kubernetes.Interface, path string, timeout time.Duration) (int64, error) {
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	raw, err := client.CoreV1().RESTClient().Get().
		AbsPath(path).
		Param(listLimitParam, fleetShapeListLimit).
		SetHeader(acceptHeader, fleetShapeAccept).
		DoRaw(cctx)
	if err != nil {
		return 0, err
	}
	var list struct {
		Metadata struct {
			Continue           string `json:"continue"`
			RemainingItemCount *int64 `json:"remainingItemCount"`
		} `json:"metadata"`
		Items []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(raw, &list); err != nil {
		return 0, fmt.Errorf("decode: %w", err)
	}
	n := int64(len(list.Items))
	if list.Metadata.RemainingItemCount != nil {
		return n + *list.Metadata.RemainingItemCount, nil
	}
	if list.Metadata.Continue != "" {
		return 0, errListUncountable
	}
	return n, nil
}
