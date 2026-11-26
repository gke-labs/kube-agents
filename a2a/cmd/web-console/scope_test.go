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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOutcomeRemediationCoversAllReconcileStates(t *testing.T) {
	if got := outcomeRemediation(outcomeOK); got != "" {
		t.Errorf("outcomeRemediation(ok) = %q, want empty", got)
	}
	cases := []struct {
		outcome string
		wantSub string
	}{
		{outcomeDenied, "roles/container.viewer"},
		{outcomeUnreachable, "VPC egress"},
		{outcomeAPIDisabled, "container.googleapis.com"},
		{outcomeOverCap, "spec.scope.maxProjects"},
	}
	for _, tc := range cases {
		got := outcomeRemediation(tc.outcome)
		if !strings.Contains(got, tc.wantSub) {
			t.Errorf("outcomeRemediation(%q) = %q, want substring %q", tc.outcome, got, tc.wantSub)
		}
	}
}

func TestNextDailyAuditComputesNextUTCFireTime(t *testing.T) {
	cases := []struct {
		name     string
		now      string
		wantID   string
		wantAt   string
		wantMins int
	}{
		{
			name:     "before first daily audit",
			now:      "2026-11-26T05:20:00Z",
			wantID:   "compliance-audit",
			wantAt:   "2026-11-26T06:20:00Z",
			wantMins: 60,
		},
		{
			name:     "between compliance and obtainability",
			now:      "2026-11-26T06:20:00Z",
			wantID:   "obtainability-audit",
			wantAt:   "2026-11-26T06:50:00Z",
			wantMins: 30,
		},
		{
			name:     "between obtainability and gce compute",
			now:      "2026-11-26T07:00:00Z",
			wantID:   "gce-compute-fleet-audit",
			wantAt:   "2026-11-26T07:45:00Z",
			wantMins: 45,
		},
		{
			name:     "between gce compute and networking fabric",
			now:      "2026-11-26T07:50:00Z",
			wantID:   "gcp-networking-fabric-audit",
			wantAt:   "2026-11-26T08:00:00Z",
			wantMins: 10,
		},
		{
			name:     "between networking fabric and ai security",
			now:      "2026-11-26T08:10:00Z",
			wantID:   "ai-security-audit",
			wantAt:   "2026-11-26T08:50:00Z",
			wantMins: 40,
		},
		{
			name:     "between ai security and stockout prevention",
			now:      "2026-11-26T09:00:00Z",
			wantID:   "stockout-prevention",
			wantAt:   "2026-11-26T09:20:00Z",
			wantMins: 20,
		},
		{
			name:     "after last daily audit wraps to tomorrow",
			now:      "2026-11-26T10:20:00Z",
			wantID:   "compliance-audit",
			wantAt:   "2026-11-27T06:20:00Z",
			wantMins: 20 * 60,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			now, err := time.Parse(time.RFC3339, tc.now)
			if err != nil {
				t.Fatal(err)
			}
			got := nextDailyAudit(now)
			if got.NextAuditID != tc.wantID {
				t.Errorf("NextAuditID = %q, want %q", got.NextAuditID, tc.wantID)
			}
			if got.NextAuditAt != tc.wantAt {
				t.Errorf("NextAuditAt = %q, want %q", got.NextAuditAt, tc.wantAt)
			}
			if got.NextAuditInMins != tc.wantMins {
				t.Errorf("NextAuditInMins = %d, want %d", got.NextAuditInMins, tc.wantMins)
			}
			if got.WindowUTC != "06:20–09:20 UTC" {
				t.Errorf("WindowUTC = %q, want 06:20–09:20 UTC", got.WindowUTC)
			}
			if len(got.DailyAudits) != 6 {
				t.Errorf("len(DailyAudits) = %d, want 6", len(got.DailyAudits))
			}
			if got.StarterPrompt == "" {
				t.Errorf("StarterPrompt is empty")
			}
		})
	}
}

func TestHandleScopeEnrichesGatewaySnapshotAndWatcherMetrics(t *testing.T) {
	cCount := 2
	zeroCount := 0
	upstreamPayload := upstreamScopePayload{
		ResolvedAt:  "2026-11-26T06:00:00Z",
		Present:     true,
		Boundary:    true,
		MaxProjects: 100,
		Resolver:    "explicit",
		Projects: []upstreamScopeProject{
			{
				ID:       "proj-host",
				Via:      []string{"management"},
				Outcome:  "ok",
				State:    "in-scope",
				Clusters: &cCount,
			},
			{
				ID:       "proj-denied",
				Via:      []string{"explicit"},
				Outcome:  "denied",
				State:    "in-scope",
				Clusters: nil,
			},
			{
				ID:       "proj-unreachable",
				Via:      []string{"explicit"},
				Outcome:  "unreachable",
				State:    "in-scope",
				Clusters: nil,
			},
			{
				ID:       "proj-disabled",
				Via:      []string{"folders/123"},
				Outcome:  "api-disabled",
				State:    "in-scope",
				Clusters: nil,
			},
			{
				ID:       "proj-overcap",
				Via:      []string{"explicit"},
				Outcome:  "over-cap",
				State:    "in-scope",
				Clusters: &zeroCount,
			},
		},
		Profiles: map[string]string{
			"cluster-proj-host-c1-us-central1": "proj-host",
			"cluster-proj-host-c2-us-east1":    "proj-host",
		},
		ProfileIdentities: []upstreamProfileIdentity{
			{
				Profile:  "cluster-proj-host-c1-us-central1",
				Project:  "proj-host",
				Cluster:  "c1",
				Location: "us-central1",
				Ready:    true,
			},
			{
				Profile:  "cluster-proj-host-c2-us-east1",
				Project:  "proj-host",
				Cluster:  "c2",
				Location: "us-east1",
				Ready:    false,
			},
		},
		EventWatcher: &upstreamEventWatcher{
			Enabled:         true,
			Reachable:       true,
			WatchSetReady:   true,
			ClustersUp:      1,
			ClustersTotal:   2,
			DiscoveryErrors: 1,
			Clusters: []upstreamEventWatcherTarget{
				{Cluster: "c1", Project: "proj-host", Location: "us-central1", Up: true},
				{Cluster: "c2", Project: "proj-host", Location: "us-east1", Up: false},
			},
		},
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+testKey {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if r.URL.Path == "/api/scope" {
			_ = json.NewEncoder(w).Encode(upstreamPayload)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	t.Cleanup(upstream.Close)

	s := newServer(config{
		HermesURL:    upstream.URL,
		APIServerKey: testKey,
		ClusterName:  "c1",
		ProjectID:    "proj-host",
		Location:     "us-central1",
	})
	rec := serve(s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/scope", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	got := decode[scopeResponse](t, rec)

	if got.Summary.TotalProjects != 5 || got.Summary.OKProjects != 1 || got.Summary.ErrorProjects != 4 {
		t.Errorf("project summary = %+v, want total=5 ok=1 error=4", got.Summary)
	}
	if !got.EventWatcher.Enabled || !got.EventWatcher.Healthy || got.EventWatcher.Status != watcherStatusWatching || got.EventWatcher.ClustersUp != 1 {
		t.Errorf("EventWatcher = %+v", got.EventWatcher)
	}

	byProject := map[string]scopeProjectRow{}
	for _, p := range got.Projects {
		byProject[p.ID] = p
	}
	if len(byProject["proj-host"].ClusterRows) != 2 {
		t.Fatalf("proj-host cluster rows = %+v, want 2", byProject["proj-host"].ClusterRows)
	}
	// c1 is ready -> ok; c2 is unready -> unreachable with scaffold remediation.
	c1 := byProject["proj-host"].ClusterRows[0]
	c2 := byProject["proj-host"].ClusterRows[1]
	if c1.Name != "c1" || c1.Outcome != outcomeOK || c1.Watching == nil || !*c1.Watching {
		t.Errorf("c1 row = %+v", c1)
	}
	if c2.Name != "c2" || c2.Outcome != outcomeUnreachable || !strings.Contains(c2.Remediation, "kubeconfig.yaml or USER.md missing") {
		t.Errorf("c2 unready row = %+v", c2)
	}
	if !strings.Contains(byProject["proj-denied"].Remediation, "roles/container.viewer") {
		t.Errorf("proj-denied remediation = %q", byProject["proj-denied"].Remediation)
	}
	if !strings.Contains(byProject["proj-unreachable"].Remediation, "VPC egress") {
		t.Errorf("proj-unreachable remediation = %q", byProject["proj-unreachable"].Remediation)
	}
	if !strings.Contains(byProject["proj-disabled"].Remediation, "container.googleapis.com") {
		t.Errorf("proj-disabled remediation = %q", byProject["proj-disabled"].Remediation)
	}
	if !strings.Contains(byProject["proj-overcap"].Remediation, "spec.scope.maxProjects") {
		t.Errorf("proj-overcap remediation = %q", byProject["proj-overcap"].Remediation)
	}
}

func TestHandleScopeFallsBackToGatewayHealthWhenScopeRouteIsAbsent(t *testing.T) {
	_, s := setupServer(t)
	s.cfg.ProjectID = "pilot-proj"
	s.cfg.Location = "us-central1"
	s.cfg.ClusterName = "pilot-c1"

	rec := serve(s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/scope", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	got := decode[scopeResponse](t, rec)
	if len(got.Projects) != 1 || got.Projects[0].ID != "pilot-proj" || got.Projects[0].Outcome != outcomeOK {
		t.Errorf("fallback projects = %+v, want pilot-proj ok", got.Projects)
	}
	if len(got.Clusters) != 1 || got.Clusters[0].Name != "pilot-c1" || got.Clusters[0].Outcome != outcomeOK {
		t.Errorf("fallback clusters = %+v, want pilot-c1 ok", got.Clusters)
	}
	if !got.EventWatcher.Healthy || got.EventWatcher.Status != watcherStatusWatching {
		t.Errorf("fallback EventWatcher = %+v, want watching", got.EventWatcher)
	}
}
