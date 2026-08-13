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
	"net"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// metrics holds the Prometheus counters and gauges for the event watcher.
//
// Every CounterVec carries cluster, project and location labels, sourced from
// the event rather than from process config. All three, because a GKE cluster
// name is unique only within a project and location — labelling on the name
// alone would silently merge "prod" in us-central1 with "prod" in
// europe-west1 into one series. project and location are functionally
// dependent on the cluster, so they cost no real cardinality.
//
// The labels are unconditional: a CounterVec is registered once with a fixed
// label set and WithLabelValues panics on an arity mismatch, so they cannot be
// added only when more than one cluster is watched. The direct
// --in-cluster cluster reports both as empty, having no cluster_identity.
//
// activeIncidents is a GaugeVec for a different reason: it reports
// dedupCache.Len(), and each watched cluster has its own cache. As a scalar
// gauge, whichever dispatcher wrote last would clobber the others and the
// value would oscillate between clusters rather than mean anything.
type metrics struct {
	registry            *prometheus.Registry
	eventsSeen          *prometheus.CounterVec
	eventsInjected      *prometheus.CounterVec
	eventsDedupSuppress *prometheus.CounterVec
	// eventsQuotaSuppress is the far side of the same wall as
	// eventsDedupSuppress, and kept separate because the two mean opposite
	// things operationally. A dedup suppression is the watcher working: the
	// incident is already open and someone has been told. A quota suppression
	// is an alert nobody received, dropped by the daemon's per-severity daily
	// ceiling. Without its own counter such an event increments nothing —
	// neither injected nor an error — and the drop is invisible.
	eventsQuotaSuppress *prometheus.CounterVec
	injectErrors        *prometheus.CounterVec
	sessionCreates      *prometheus.CounterVec
	activeIncidents     *prometheus.GaugeVec
	// clusterDiscoveryErrors and clusterUp are the two halves of "is this
	// cluster actually being watched". Discovery skips profiles it cannot load
	// and counts them here; everything after discovery — the dedup cache, the
	// informer and its initial sync — is covered by clusterUp instead. A
	// cluster can drop out at either stage, and the stages fail for different
	// reasons (malformed files vs. RBAC, unreachable control planes, expired
	// CAs), so watching only one of them leaves half the surface dark.
	clusterDiscoveryErrors *prometheus.CounterVec
	clusterUp              *prometheus.GaugeVec
}

// newMetrics instantiates and registers all watcher metrics using a custom registry.
func newMetrics() *metrics {
	reg := prometheus.NewRegistry()
	m := &metrics{
		registry: reg,
		// No namespace label, unlike the post-filter counters below. This one
		// counts pre-filter, so its reason and namespace are whatever any
		// controller in the cluster emits — both unbounded. Under fan-in that
		// product is multiplied by the cluster count, so namespace is dropped
		// to keep the series count sane.
		eventsSeen: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_seen_total",
			Help: "Total k8s events observed by the informer, before filter.",
		}, []string{"cluster", "project", "location", "reason"}),
		eventsInjected: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_injected_total",
			Help: "Total events that survived filter + dedup and were POSTed to the daemon.",
		}, []string{"cluster", "project", "location", "reason", "namespace"}),
		eventsDedupSuppress: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_deduped_total",
			Help: "Total events suppressed by the rolling-window dedup cache.",
		}, []string{"cluster", "project", "location", "reason", "namespace"}),
		eventsQuotaSuppress: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_quota_suppressed_total",
			Help: "Total events the daemon accepted and then dropped against its per-severity daily alert ceiling. These reached nobody; the watcher rolls back the dedup entry so the next sighting is re-offered.",
		}, []string{"cluster", "project", "location", "reason", "namespace"}),
		injectErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_inject_errors_total",
			Help: "Total inject (or session-create) attempts that returned a non-2xx response or transport error.",
		}, []string{"cluster", "project", "location", "reason", "http_code"}),
		sessionCreates: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_session_creates_total",
			Help: "Total POST /sessions attempts, labeled by outcome.",
		}, []string{"cluster", "project", "location", "outcome"}),
		activeIncidents: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "k8s_event_watcher_active_incidents",
			Help: "Current number of incidents in a cluster's dedup cache.",
		}, []string{"cluster", "project", "location"}),
		// Labeled by profile directory, not cluster: a profile too broken to
		// yield a cluster_identity has no cluster name to report. Bounded by
		// the number of profiles on disk.
		clusterDiscoveryErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_cluster_discovery_errors_total",
			Help: "Cluster Agent profiles that could not be turned into a watched cluster. Non-zero means a cluster is not being monitored.",
		}, []string{"profile"}),
		// 1 once the initial list has completed and events are flowing; 0
		// before that and again once the informer stops. Deliberately not
		// goroutine liveness: WaitForCacheSync has no timeout and the reflector
		// retries forever, so an informer that cannot reach its cluster stays
		// blocked and alive indefinitely. This gauge is the only thing that
		// tells that apart from a working one.
		//
		// 0 is therefore the normal state during startup, which inverts the
		// obvious alert: it wants a "for" comfortably longer than a healthy
		// initial list, not a short one.
		clusterUp: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "k8s_event_watcher_cluster_up",
			Help: "1 once this cluster's informer has completed its initial list and is delivering events; 0 while it has not synced (including a stuck informer retrying an unreachable API server) or has stopped.",
		}, []string{"cluster", "project", "location"}),
	}
	reg.MustRegister(
		m.eventsSeen,
		m.eventsInjected,
		m.eventsDedupSuppress,
		m.eventsQuotaSuppress,
		m.injectErrors,
		m.sessionCreates,
		m.activeIncidents,
		m.clusterDiscoveryErrors,
		m.clusterUp,
	)
	return m
}

type metricsServer struct {
	server *http.Server
	ln     net.Listener
}

// startMetrics binds to the TCP address synchronously and registers endpoints.
func startMetrics(addr string, m *metrics) (*metricsServer, error) {
	if addr == "" {
		return nil, nil
	}
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{}))
	// Simple liveness probe endpoint.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	server := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, fmt.Errorf("metrics: listen %s: %w", addr, err)
	}
	return &metricsServer{server: server, ln: ln}, nil
}

// Run executes the server event loop, blocking until context cancellation.
func (s *metricsServer) Run(ctx context.Context) error {
	if s == nil {
		<-ctx.Done()
		return nil
	}
	errCh := make(chan error, 1)
	go func() { errCh <- s.server.Serve(s.ln) }()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = s.server.Shutdown(shutdownCtx)
		return nil
	case err := <-errCh:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}
