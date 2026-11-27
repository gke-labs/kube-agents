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
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestDailyAuditRosterMatchesPlatformCronJobsJSON(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "agents", "platform", "cron", "jobs.json"))
	if err != nil {
		t.Fatalf("read agents/platform/cron/jobs.json: %v", err)
	}
	var parsed struct {
		Jobs []struct {
			ID       string   `json:"id"`
			Name     string   `json:"name"`
			Skills   []string `json:"skills"`
			Enabled  bool     `json:"enabled"`
			Schedule struct {
				Kind string `json:"kind"`
				Expr string `json:"expr"`
			} `json:"schedule"`
		} `json:"jobs"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil {
		t.Fatalf("unmarshal jobs.json: %v", err)
	}

	var expected []dailyAuditSlot
	for _, j := range parsed.Jobs {
		if !j.Enabled || j.Schedule.Kind != "cron" || !slices.Contains(j.Skills, "fleet-audit") {
			continue
		}
		fields := strings.Fields(j.Schedule.Expr)
		if len(fields) != 5 || fields[2] != "*" || fields[3] != "*" || fields[4] != "*" {
			continue
		}
		minUTC, err1 := strconv.Atoi(fields[0])
		hourUTC, err2 := strconv.Atoi(fields[1])
		if err1 != nil || err2 != nil {
			continue
		}
		expected = append(expected, dailyAuditSlot{
			ID:        j.ID,
			Name:      j.Name,
			HourUTC:   hourUTC,
			MinuteUTC: minUTC,
			CronExpr:  j.Schedule.Expr,
			TimeLabel: fmt.Sprintf("%02d:%02d UTC", hourUTC, minUTC),
		})
	}
	sort.Slice(expected, func(i, j int) bool {
		if expected[i].HourUTC != expected[j].HourUTC {
			return expected[i].HourUTC < expected[j].HourUTC
		}
		return expected[i].MinuteUTC < expected[j].MinuteUTC
	})

	if len(dailyAuditRoster) != len(expected) {
		t.Fatalf("len(dailyAuditRoster) = %d, want %d from jobs.json", len(dailyAuditRoster), len(expected))
	}
	for i := range expected {
		if dailyAuditRoster[i] != expected[i] {
			t.Errorf("dailyAuditRoster[%d] = %+v, want %+v from jobs.json", i, dailyAuditRoster[i], expected[i])
		}
	}
}

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
	oneCount := 1
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
			{
				ID:          "proj-retiring",
				Via:         []string{"explicit"},
				Outcome:     "ok",
				State:       "retiring",
				AbsentSince: "2026-11-26T05:00:00Z",
				Clusters:    &oneCount,
			},
			{
				ID:       "proj-carried",
				Via:      []string{"folders/123"},
				Outcome:  "unreachable",
				State:    "in-scope",
				Clusters: &oneCount,
			},
		},
		Profiles: map[string]string{
			"cluster-proj-host-c1-us-central1":      "proj-host",
			"cluster-proj-host-c2-us-east1":         "proj-host",
			"cluster-proj-host-c3-no-ident":         "proj-host",
			"cluster-proj-retiring-old-us-central1": "proj-retiring",
			"cluster-proj-unmanaged-u1-us-central1": "proj-unmanaged",
			"cluster-proj-carried-c4-us-central1":   "proj-carried",
		},
		Unmanaged: []upstreamUnmanagedEntry{
			{
				Profile: "cluster-proj-unmanaged-u1-us-central1",
				Project: "proj-unmanaged",
				Reason:  "never in scope",
			},
			{
				Profile: "cluster-proj-carried-c4-us-central1",
				Project: "proj-carried",
				Reason:  "asset index lag",
			},
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
			{
				Profile:  "cluster-proj-retiring-old-us-central1",
				Project:  "proj-retiring",
				Cluster:  "old",
				Location: "us-central1",
				Ready:    true,
			},
			{
				Profile:  "cluster-proj-unmanaged-u1-us-central1",
				Project:  "proj-unmanaged",
				Cluster:  "u1",
				Location: "us-central1",
				Ready:    true,
			},
			{
				Profile:  "cluster-proj-carried-c4-us-central1",
				Project:  "proj-carried",
				Cluster:  "c4",
				Location: "us-central1",
				Ready:    true,
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

	if got.Summary.TotalProjects != 6 || got.Summary.OKProjects != 1 || got.Summary.ErrorProjects != 5 {
		t.Errorf("project summary = %+v, want total=6 ok=1 error=5 (retiring project excluded, carried project included)", got.Summary)
	}
	// Only real cluster profiles (c1 ready=ok, c2 unready=error, c3 missing identity=error, c4 carried=error) are counted in cluster summary.
	if got.Summary.TotalClusters != 4 || got.Summary.OKClusters != 1 || got.Summary.ErrorClusters != 3 {
		t.Errorf("cluster summary = %+v, want total=4 ok=1 error=3 (no synthetic placeholder rows or retiring/never-in-scope profiles)", got.Summary)
	}
	if !got.EventWatcher.Enabled || !got.EventWatcher.Healthy || got.EventWatcher.Status != watcherStatusWatching || got.EventWatcher.ClustersUp != 1 {
		t.Errorf("EventWatcher = %+v", got.EventWatcher)
	}

	byProject := map[string]scopeProjectRow{}
	for _, p := range got.Projects {
		byProject[p.ID] = p
	}
	if _, exists := byProject["proj-retiring"]; exists {
		t.Errorf("retiring project should not be included in active scope projects even when ProfileIdentities still exist on disk")
	}
	if _, exists := byProject["proj-unmanaged"]; exists {
		t.Errorf("unmanaged never-in-scope project should not be promoted to active scope projects")
	}
	if len(byProject["proj-carried"].ClusterRows) != 1 || byProject["proj-carried"].ClusterRows[0].Name != "c4" || byProject["proj-carried"].ClusterRows[0].Outcome != outcomeUnreachable {
		t.Errorf("proj-carried cluster rows = %+v, want 1 row for c4 with unreachable outcome", byProject["proj-carried"].ClusterRows)
	}
	if len(byProject["proj-host"].ClusterRows) != 3 {
		t.Fatalf("proj-host cluster rows = %+v, want 3", byProject["proj-host"].ClusterRows)
	}
	// c1 is ready -> ok; c2 is unready -> unreachable with scaffold remediation;
	// c3 has no readable cluster_identity -> Ready=false and unreachable with scaffold remediation.
	c1 := byProject["proj-host"].ClusterRows[0]
	c2 := byProject["proj-host"].ClusterRows[1]
	c3 := byProject["proj-host"].ClusterRows[2]
	if c1.Name != "c1" || c1.Outcome != outcomeOK || c1.Watching == nil || !*c1.Watching {
		t.Errorf("c1 row = %+v", c1)
	}
	if c2.Name != "c2" || c2.Outcome != outcomeUnreachable || c2.Ready || !strings.Contains(c2.Remediation, "kubeconfig.yaml, or USER.md missing") {
		t.Errorf("c2 unready row = %+v", c2)
	}
	if c3.Name != "cluster-proj-host-c3-no-ident" || c3.Outcome != outcomeUnreachable || c3.Ready || !strings.Contains(c3.Remediation, "kubeconfig.yaml, or USER.md missing") {
		t.Errorf("c3 missing-identity row = %+v, want Ready=false and outcomeUnreachable", c3)
	}
	if len(byProject["proj-denied"].ClusterRows) != 0 {
		t.Errorf("proj-denied cluster rows = %+v, want empty (no placeholder cluster row)", byProject["proj-denied"].ClusterRows)
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

func TestBuildScopeResponseGroupsOrphanProfileIdentitiesAndScopesInClusterWatch(t *testing.T) {
	cfg := config{
		ProjectID:   "proj-host",
		Location:    "us-central1",
		ClusterName: "prod",
	}
	raw := upstreamScopePayload{
		ProfileIdentities: []upstreamProfileIdentity{
			{
				Profile:  "cluster-proj-host-prod-us-central1",
				Project:  "proj-host",
				Cluster:  "prod",
				Location: "us-central1",
				Ready:    true,
			},
			{
				Profile:  "cluster-proj-host-prod-us-east1",
				Project:  "proj-host",
				Cluster:  "prod",
				Location: "us-east1",
				Ready:    true,
			},
			{
				Profile:  "cluster-proj-other-prod-europe-west1",
				Project:  "proj-other",
				Cluster:  "prod",
				Location: "europe-west1",
				Ready:    true,
			},
		},
		EventWatcher: &upstreamEventWatcher{
			Enabled:       true,
			Reachable:     true,
			WatchSetReady: true,
			ClustersUp:    1,
			ClustersTotal: 1,
			Clusters: []upstreamEventWatcherTarget{
				// Unabsorbed --in-cluster metric entry carries empty project and location.
				{Cluster: "prod", Project: "", Location: "", Up: true},
			},
		},
	}

	got := buildScopeResponse(cfg, raw, true, true, "", time.Date(2026, 11, 26, 5, 0, 0, 0, time.UTC))
	if len(got.Projects) != 2 || got.Summary.TotalProjects != 2 || got.Summary.TotalClusters != 3 {
		t.Fatalf("Projects = %+v, Summary = %+v; want 2 projects and 3 clusters", got.Projects, got.Summary)
	}
	byProj := map[string]scopeProjectRow{}
	for _, p := range got.Projects {
		byProj[p.ID] = p
	}
	hostCluster := byProj["proj-host"].ClusterRows[0]
	if hostCluster.Location != "us-central1" || hostCluster.Watching == nil || !*hostCluster.Watching {
		t.Errorf("host cluster (us-central1) Watching = %v, want true from --in-cluster fallback", hostCluster.Watching)
	}
	sameNameDifferentLoc := byProj["proj-host"].ClusterRows[1]
	if sameNameDifferentLoc.Location != "us-east1" || sameNameDifferentLoc.Watching != nil {
		t.Errorf("same-project cluster in different location (us-east1) Watching = %v, want nil", sameNameDifferentLoc.Watching)
	}
	otherCluster := byProj["proj-other"].ClusterRows[0]
	if otherCluster.Watching != nil {
		t.Errorf("non-host cluster with same bare name Watching = %v, want nil", *otherCluster.Watching)
	}

	// When cfg.ProjectID is unset, the --in-cluster fallback must not match any project.
	gotNoProj := buildScopeResponse(config{ClusterName: "prod", Location: "us-central1"}, raw, true, true, "", time.Date(2026, 11, 26, 5, 0, 0, 0, time.UTC))
	for _, c := range gotNoProj.Clusters {
		if c.Watching != nil {
			t.Errorf("cluster %+v matched --in-cluster fallback with empty cfg.ProjectID", c)
		}
	}
}

func TestBuildWatcherStatusReportsUnreachableWhenMetricsListenerRefuses(t *testing.T) {
	got := buildWatcherStatus(&upstreamEventWatcher{
		Enabled:   true,
		Reachable: false,
		Error:     "k8s-event-watcher metrics unreachable: ConnectionRefusedError",
	}, true, true, "")
	if got.Healthy || got.Status != watcherStatusUnreachable {
		t.Errorf("buildWatcherStatus with unreachable metrics = %+v, want Healthy=false Status=unreachable", got)
	}
	if !strings.Contains(got.Detail, "ConnectionRefusedError") {
		t.Errorf("Detail = %q, want ConnectionRefusedError", got.Detail)
	}

	degraded := buildWatcherStatus(&upstreamEventWatcher{
		Enabled:       true,
		Reachable:     true,
		WatchSetReady: true,
		ClustersUp:    0,
		ClustersTotal: 2,
	}, true, true, "")
	if degraded.Healthy || degraded.Status != watcherStatusDegraded {
		t.Errorf("buildWatcherStatus with 0 of 2 clusters up = %+v, want Healthy=false Status=degraded", degraded)
	}

	starting := buildWatcherStatus(&upstreamEventWatcher{
		Enabled:       true,
		Reachable:     true,
		WatchSetReady: false,
		ClustersUp:    0,
		ClustersTotal: 2,
	}, true, true, "")
	if starting.Healthy || starting.Status != watcherStatusStarting {
		t.Errorf("buildWatcherStatus while starting = %+v, want Healthy=false Status=starting", starting)
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
