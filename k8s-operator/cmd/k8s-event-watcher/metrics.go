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
type metrics struct {
	registry            *prometheus.Registry
	eventsSeen          *prometheus.CounterVec
	eventsInjected      *prometheus.CounterVec
	eventsDedupSuppress *prometheus.CounterVec
	injectErrors        *prometheus.CounterVec
	sessionCreates      *prometheus.CounterVec
	activeIncidents     prometheus.Gauge
}

// newMetrics instantiates and registers all watcher metrics using a custom registry.
func newMetrics() *metrics {
	reg := prometheus.NewRegistry()
	m := &metrics{
		registry: reg,
		eventsSeen: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_seen_total",
			Help: "Total k8s events observed by the informer, before filter.",
		}, []string{"reason", "namespace"}),
		eventsInjected: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_injected_total",
			Help: "Total events that survived filter + dedup and were POSTed to the daemon.",
		}, []string{"reason", "namespace"}),
		eventsDedupSuppress: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_events_deduped_total",
			Help: "Total events suppressed by the rolling-window dedup cache.",
		}, []string{"reason", "namespace"}),
		injectErrors: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_inject_errors_total",
			Help: "Total inject (or session-create) attempts that returned a non-2xx response or transport error.",
		}, []string{"reason", "http_code"}),
		sessionCreates: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "k8s_event_watcher_session_creates_total",
			Help: "Total POST /sessions attempts, labeled by outcome.",
		}, []string{"outcome"}),
		activeIncidents: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "k8s_event_watcher_active_incidents",
			Help: "Current number of incidents in the sidecar's dedup cache.",
		}),
	}
	reg.MustRegister(
		m.eventsSeen,
		m.eventsInjected,
		m.eventsDedupSuppress,
		m.injectErrors,
		m.sessionCreates,
		m.activeIncidents,
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
