/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
)

// replicaMetrics is a trimmed LiteLLM /metrics body with two series per
// counter, the _created series the parser must skip, and a label value that
// holds a space and a brace.
func replicaMetrics(scale int) string {
	return fmt.Sprintf(`# HELP litellm_input_tokens_metric_total Total number of input tokens from LLM requests
# TYPE litellm_input_tokens_metric_total counter
litellm_input_tokens_metric_total{model="gemini-3.5-flash",team="a b}"} %d.0
litellm_input_tokens_metric_total{model="hermes-agent"} %d.0
litellm_input_tokens_metric_created{model="gemini-3.5-flash"} 1.7e+09
litellm_output_tokens_metric_total{model="gemini-3.5-flash"} %d.0
litellm_total_tokens_metric_total{model="gemini-3.5-flash"} %d.0
litellm_spend_metric_total{model="gemini-3.5-flash"} %d.25
litellm_spend_metric_created{model="gemini-3.5-flash"} 1.7e+09
litellm_requests_metric_total{model="gemini-3.5-flash"} 999.0
`, 100*scale, 50*scale, 30*scale, 180*scale, scale)
}

type fakeResolver struct {
	addrs []string
	err   error
	mu    sync.Mutex
	calls int
}

func (r *fakeResolver) LookupHost(_ context.Context, _ string) ([]string, error) {
	r.mu.Lock()
	r.calls++
	r.mu.Unlock()
	return r.addrs, r.err
}

func insightsServer(cfg config, resolver hostResolver, bodies map[string]string, failures map[string]error) *server {
	s := newServer(cfg)
	s.resolver = resolver
	s.fetchMetrics = func(_ context.Context, addr string) ([]byte, error) {
		if err := failures[addr]; err != nil {
			return nil, err
		}
		return []byte(bodies[addr]), nil
	}
	return s
}

func insightsReq() *http.Request {
	return httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/insights", nil)
}

func TestSumMetricsSkipsCreatedAndOtherSeries(t *testing.T) {
	got, err := sumMetrics(strings.NewReader(replicaMetrics(1)))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]float64{metricInputTokens: 150, metricOutputTokens: 30, metricTotalTokens: 180, metricSpendUSD: 1.25}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %v, want %v", k, got[k], v)
		}
	}
	if len(got) != len(want) {
		t.Errorf("kept series %v, want only the usage counters", got)
	}
	if _, err := sumMetrics(strings.NewReader("litellm_spend_metric_total{} abc\n")); err == nil {
		t.Errorf("a garbled value parsed without an error")
	}
}

func TestInsightsSumsEveryReplica(t *testing.T) {
	resolver := &fakeResolver{addrs: []string{"10.0.0.1", "10.0.0.2"}}
	cfg := config{LiteLLMPeersHost: "peers.ns.svc.cluster.local"}
	h := insightsServer(cfg, resolver, map[string]string{"10.0.0.1": replicaMetrics(1), "10.0.0.2": replicaMetrics(2)}, nil).routes()

	got := decode[insightsResponse](t, serve(h, insightsReq()))
	u := got.Usage
	if u.InputTokens != 450 || u.OutputTokens != 90 || u.TotalTokens != 540 || u.SpendUSD != 3.5 {
		t.Errorf("usage = %+v, want the two replicas summed", u)
	}
	if u.ReplicasFound != 2 || u.ReplicasRead != 2 || u.Error != "" {
		t.Errorf("replicas = %d of %d, error %q", u.ReplicasRead, u.ReplicasFound, u.Error)
	}
	serve(h, insightsReq())
	if resolver.calls != 1 {
		t.Errorf("a second request inside the cache interval looked the replicas up again (%d lookups)", resolver.calls)
	}
}

func TestInsightsReportsAPartialRead(t *testing.T) {
	resolver := &fakeResolver{addrs: []string{"10.0.0.1", "10.0.0.2"}}
	cfg := config{LiteLLMPeersHost: "peers"}
	h := insightsServer(cfg, resolver,
		map[string]string{"10.0.0.1": replicaMetrics(1)},
		map[string]error{"10.0.0.2": errors.New("connection refused")}).routes()

	u := decode[insightsResponse](t, serve(h, insightsReq())).Usage
	if u.ReplicasFound != 2 || u.ReplicasRead != 1 {
		t.Errorf("replicas = %d of %d, want 1 of 2", u.ReplicasRead, u.ReplicasFound)
	}
	if u.InputTokens != 150 {
		t.Errorf("input tokens = %d, want the one replica that answered", u.InputTokens)
	}
	if !strings.Contains(u.Error, "10.0.0.2") || !strings.Contains(u.Error, "connection refused") {
		t.Errorf("error %q does not name the failed replica", u.Error)
	}
}

func TestInsightsWithoutLiteLLM(t *testing.T) {
	resolver := &fakeResolver{}
	h := insightsServer(config{}, resolver, nil, nil).routes()
	u := decode[insightsResponse](t, serve(h, insightsReq())).Usage
	if u.Error != errUsageDisabled || u.ReplicasFound != 0 {
		t.Errorf("usage = %+v, want the not-enabled explanation", u)
	}
	if resolver.calls != 0 {
		t.Errorf("looked up replicas with no peers host configured")
	}
}

func TestInsightsReportsALookupFailure(t *testing.T) {
	resolver := &fakeResolver{err: errors.New("no such host")}
	h := insightsServer(config{LiteLLMPeersHost: "peers"}, resolver, nil, nil).routes()
	u := decode[insightsResponse](t, serve(h, insightsReq())).Usage
	if !strings.Contains(u.Error, "no such host") {
		t.Errorf("error %q does not carry the lookup failure", u.Error)
	}
}

func TestInsightsModelIdentityAndClusterFromEnv(t *testing.T) {
	t.Setenv(envModelName, "gemini-3.5-flash")
	t.Setenv(envModelProvider, "vertex_ai")
	t.Setenv(envAgentKSA, "kubeagents-platform-agent")
	t.Setenv(envAgentGSA, "agent@p.iam.gserviceaccount.com")
	t.Setenv(envAgentRoles, " roles/container.viewer, ,roles/logging.viewer ")
	t.Setenv(envClusterName, "c1")
	t.Setenv(envProjectID, "p")
	t.Setenv(envLocation, "us-central1")
	t.Setenv(envLiteLLMPeersHost, "")
	got := decode[insightsResponse](t, serve(newServer(loadConfig()).routes(), insightsReq()))
	if got.Model != (modelInfo{Name: "gemini-3.5-flash", Provider: "vertex_ai"}) {
		t.Errorf("model = %+v", got.Model)
	}
	id := got.Identity
	if id.KubernetesServiceAccount != "kubeagents-platform-agent" || id.GCPServiceAccount != "agent@p.iam.gserviceaccount.com" ||
		strings.Join(id.Roles, "|") != "roles/container.viewer|roles/logging.viewer" {
		t.Errorf("identity = %+v", id)
	}
	if got.Cluster != (clusterInfo{Name: "c1", Project: "p", Location: "us-central1"}) {
		t.Errorf("cluster = %+v", got.Cluster)
	}

	t.Setenv(envAgentRoles, "")
	rec := serve(newServer(loadConfig()).routes(), insightsReq())
	if !strings.Contains(rec.Body.String(), `"roles":[]`) {
		t.Errorf("no roles should encode as an empty list: %s", rec.Body.String())
	}
}

// The real fetcher, against a replica served over HTTP: it reads /metrics and
// refuses a body over the limit instead of undercounting.
func TestFetchPeerMetrics(t *testing.T) {
	big := false
	replica := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != litellmMetricsPath {
			http.NotFound(w, r)
			return
		}
		if big {
			_, _ = w.Write([]byte(strings.Repeat("#", maxMetricsBodyBytes+1)))
			return
		}
		_, _ = w.Write([]byte(replicaMetrics(1)))
	}))
	defer replica.Close()
	u, _ := url.Parse(replica.URL)
	host, port, _ := net.SplitHostPort(u.Host)
	s := newServer(config{LiteLLMPeersHost: "peers"})
	s.resolver = &fakeResolver{addrs: []string{host}}
	s.peerPort = port

	if got := s.readUsage(context.Background()); got.ReplicasRead != 1 || got.InputTokens != 150 {
		t.Errorf("usage = %+v", got)
	}
	big = true
	if got := s.readUsage(context.Background()); got.ReplicasRead != 0 || !strings.Contains(got.Error, "larger") {
		t.Errorf("oversized body: usage = %+v", got)
	}
}
