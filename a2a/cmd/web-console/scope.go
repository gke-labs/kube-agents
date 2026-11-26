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

// GET /api/scope: the reconcile's project and cluster connectivity outcomes
// (fleet_scope.json + per-profile identities), local k8s-event-watcher
// readiness, and the next scheduled daily fleet audit run time.
//
// Fetched from the gateway's authenticated front door (GET /api/scope on
// AgentAPIProxyHandler), which reads the shared data PVC and probes the event
// watcher's metrics port on loopback. When the gateway predates /api/scope or
// the snapshot file has not been written yet, the handler falls back to the
// existing /health probe and reports the host cluster from the console's own
// config, so the #alerts empty-state banner and drawers always render.

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"
)

const (
	scopeTimeout = 5 * time.Second

	scopePath   = "/api/scope"
	healthPath  = "/health"
	stateInScope = "in-scope"

	outcomeOK          = "ok"
	outcomeDenied      = "denied"
	outcomeUnreachable = "unreachable"
	outcomeAPIDisabled = "api-disabled"
	outcomeOverCap     = "over-cap"

	remediationDenied      = "Grant roles/container.viewer (and roles/compute.viewer) to the Platform Agent's Google Cloud service account on this project so cluster_agent_reconcile.py can list and fetch credentials for its clusters."
	remediationUnreachable = "The GKE API or control plane did not answer within the reconcile budget. Check VPC egress, firewall rules, Private Google Access, or control-plane authorized networks."
	remediationAPIDisabled = "Enable the Kubernetes Engine API (container.googleapis.com) in this project (`gcloud services enable container.googleapis.com --project=<project>`), or add the project to spec.scope.exclude.projects."
	remediationOverCap     = "This project sits past spec.scope.maxProjects (default 100) and gets no new Cluster Agent profiles. Raise spec.scope.maxProjects on the PlatformAgent or narrow spec.scope."

	viaManagement = "management"
	viaExplicit   = "explicit"

	watcherStatusWatching    = "watching"
	watcherStatusStarting    = "starting"
	watcherStatusDisabled    = "disabled"
	watcherStatusUnreachable = "unreachable"

	alertsStarterPrompt = "Run an on-demand cluster health and security posture check across the fleet and summarise any failing pods, recent warning events, or RBAC risks."

	minutesPerHour = 60
	hoursPerDay    = 24
)

// dailyAuditSlot is one daily fleet audit from agents/platform/cron/jobs.json,
// ordered by UTC time of day.
type dailyAuditSlot struct {
	ID         string
	Name       string
	HourUTC    int
	MinuteUTC  int
	CronExpr   string
	TimeLabel  string
}

// dailyAuditRoster lists the daily fleet audits shipped in
// agents/platform/cron/jobs.json in ascending UTC fire order.
var dailyAuditRoster = []dailyAuditSlot{
	{
		ID:        "compliance-audit",
		Name:      "Security & RBAC Posture Audit",
		HourUTC:   6,
		MinuteUTC: 20,
		CronExpr:  "20 6 * * *",
		TimeLabel: "06:20 UTC",
	},
	{
		ID:        "obtainability-audit",
		Name:      "Workload Reliability Audit",
		HourUTC:   6,
		MinuteUTC: 50,
		CronExpr:  "50 6 * * *",
		TimeLabel: "06:50 UTC",
	},
	{
		ID:        "gce-compute-fleet-audit",
		Name:      "GCE Compute Engine and MIG Fleet Audit",
		HourUTC:   7,
		MinuteUTC: 45,
		CronExpr:  "45 7 * * *",
		TimeLabel: "07:45 UTC",
	},
	{
		ID:        "gcp-networking-fabric-audit",
		Name:      "GCP Networking Fabric & VPC IPAM Audit",
		HourUTC:   8,
		MinuteUTC: 0,
		CronExpr:  "0 8 * * *",
		TimeLabel: "08:00 UTC",
	},
	{
		ID:        "ai-security-audit",
		Name:      "AI Workload Security Audit",
		HourUTC:   8,
		MinuteUTC: 50,
		CronExpr:  "50 8 * * *",
		TimeLabel: "08:50 UTC",
	},
	{
		ID:        "stockout-prevention",
		Name:      "Fleet Stockout Prevention & Capacity Audit",
		HourUTC:   9,
		MinuteUTC: 20,
		CronExpr:  "20 9 * * *",
		TimeLabel: "09:20 UTC",
	},
}

// upstreamScopePayload is the JSON body AgentAPIProxyHandler returns on
// GET /api/scope.
type upstreamScopePayload struct {
	ResolvedAt        string                    `json:"resolved_at"`
	Present           bool                      `json:"present"`
	Boundary          bool                      `json:"boundary"`
	MaxProjects       int                       `json:"max_projects"`
	Resolver          string                    `json:"resolver"`
	Projects          []upstreamScopeProject    `json:"projects"`
	Containers        []upstreamScopeContainer  `json:"containers"`
	Profiles          map[string]string         `json:"profiles"`
	Unmanaged         []upstreamUnmanagedEntry  `json:"unmanaged"`
	ProfileIdentities []upstreamProfileIdentity `json:"profile_identities"`
	EventWatcher      *upstreamEventWatcher     `json:"event_watcher"`
}

type upstreamScopeProject struct {
	ID          string   `json:"id"`
	Via         []string `json:"via"`
	Outcome     string   `json:"outcome"`
	State       string   `json:"state"`
	Clusters    *int     `json:"clusters"`
	AbsentSince string   `json:"absentSince,omitempty"`
	Number      string   `json:"number,omitempty"`
}

type upstreamScopeContainer struct {
	ID       string `json:"id"`
	Outcome  string `json:"outcome"`
	Projects int    `json:"projects"`
}

type upstreamUnmanagedEntry struct {
	Profile string `json:"profile"`
	Project string `json:"project"`
	Reason  string `json:"reason"`
}

type upstreamProfileIdentity struct {
	Profile  string `json:"profile"`
	Project  string `json:"project"`
	Cluster  string `json:"cluster"`
	Location string `json:"location"`
	Ready    bool   `json:"ready"`
}

type upstreamEventWatcher struct {
	Enabled         bool                         `json:"enabled"`
	Reachable       bool                         `json:"reachable"`
	WatchSetReady   bool                         `json:"watch_set_ready"`
	ClustersUp      int                          `json:"clusters_up"`
	ClustersTotal   int                          `json:"clusters_total"`
	DiscoveryErrors int                          `json:"discovery_errors"`
	Clusters        []upstreamEventWatcherTarget `json:"clusters"`
	Error           string                       `json:"error,omitempty"`
}

type upstreamEventWatcherTarget struct {
	Cluster  string `json:"cluster"`
	Project  string `json:"project"`
	Location string `json:"location"`
	Up       bool   `json:"up"`
}

// scopeProjectRow is one project row in GET /api/scope, enriched with profile
// names, discovered cluster identities, and remediation guidance.
type scopeProjectRow struct {
	ID          string                `json:"id"`
	Via         []string              `json:"via"`
	Outcome     string                `json:"outcome"`
	State       string                `json:"state"`
	Clusters    *int                  `json:"clusters"`
	Profiles    []string              `json:"profiles"`
	ClusterRows []scopeClusterRow     `json:"cluster_rows,omitempty"`
	Remediation string                `json:"remediation,omitempty"`
	AbsentSince string                `json:"absent_since,omitempty"`
}

// scopeClusterRow is one discovered or configured cluster entry with its
// project, location, name, connectivity status, and any onboarding hint.
type scopeClusterRow struct {
	Project     string `json:"project"`
	Location    string `json:"location"`
	Name        string `json:"name"`
	Profile     string `json:"profile,omitempty"`
	Outcome     string `json:"outcome"`
	Ready       bool   `json:"ready"`
	Watching    *bool  `json:"watching,omitempty"`
	Remediation string `json:"remediation,omitempty"`
}

type scopeContainerRow struct {
	ID          string `json:"id"`
	Outcome     string `json:"outcome"`
	Projects    int    `json:"projects"`
	Remediation string `json:"remediation,omitempty"`
}

type scopeWatcherStatus struct {
	Enabled         bool   `json:"enabled"`
	Healthy         bool   `json:"healthy"`
	Status          string `json:"status"`
	WatchSetReady   bool   `json:"watch_set_ready"`
	ClustersUp      int    `json:"clusters_up"`
	ClustersTotal   int    `json:"clusters_total"`
	DiscoveryErrors int    `json:"discovery_errors,omitempty"`
	Detail          string `json:"detail"`
}

type scopeScheduleSlot struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Cron      string `json:"cron"`
	TimeLabel string `json:"time_label"`
}

type scopeScheduleInfo struct {
	NextAuditID     string              `json:"next_audit_id"`
	NextAuditName   string              `json:"next_audit_name"`
	NextAuditAt     string              `json:"next_audit_at"`
	NextAuditInMins int                 `json:"next_audit_in_minutes"`
	WindowUTC       string              `json:"window_utc"`
	DailyAudits     []scopeScheduleSlot `json:"daily_audits"`
	StarterPrompt   string              `json:"starter_prompt"`
}

type scopeSummaryCounts struct {
	TotalProjects int `json:"total_projects"`
	OKProjects    int `json:"ok_projects"`
	ErrorProjects int `json:"error_projects"`
	TotalClusters int `json:"total_clusters"`
	OKClusters    int `json:"ok_clusters"`
	ErrorClusters int `json:"error_clusters"`
}

type scopeResponse struct {
	ResolvedAt   string              `json:"resolved_at,omitempty"`
	Present      bool                `json:"present"`
	Boundary     bool                `json:"boundary"`
	MaxProjects  int                 `json:"max_projects,omitempty"`
	Resolver     string              `json:"resolver,omitempty"`
	Summary      scopeSummaryCounts  `json:"summary"`
	Projects     []scopeProjectRow   `json:"projects"`
	Clusters     []scopeClusterRow   `json:"clusters"`
	Containers   []scopeContainerRow `json:"containers,omitempty"`
	EventWatcher scopeWatcherStatus  `json:"event_watcher"`
	Schedule     scopeScheduleInfo   `json:"schedule"`
}

// outcomeRemediation returns actionable remediation guidance for non-ok
// reconcile outcomes.
func outcomeRemediation(outcome string) string {
	switch outcome {
	case outcomeOK, "":
		return ""
	case outcomeDenied:
		return remediationDenied
	case outcomeUnreachable:
		return remediationUnreachable
	case outcomeAPIDisabled:
		return remediationAPIDisabled
	case outcomeOverCap:
		return remediationOverCap
	default:
		return "Check cluster_agent_reconcile.py logs on the platform-agent pod for details on outcome " + outcome + "."
	}
}

// nextDailyAudit computes the next UTC fire time across dailyAuditRoster at or
// after now.
func nextDailyAudit(now time.Time) scopeScheduleInfo {
	utc := now.UTC()
	slots := make([]scopeScheduleSlot, 0, len(dailyAuditRoster))
	for _, s := range dailyAuditRoster {
		slots = append(slots, scopeScheduleSlot{
			ID:        s.ID,
			Name:      s.Name,
			Cron:      s.CronExpr,
			TimeLabel: s.TimeLabel,
		})
	}

	var chosen dailyAuditSlot
	var nextFire time.Time
	foundToday := false
	for _, s := range dailyAuditRoster {
		candidate := time.Date(utc.Year(), utc.Month(), utc.Day(), s.HourUTC, s.MinuteUTC, 0, 0, time.UTC)
		if candidate.After(utc) {
			chosen = s
			nextFire = candidate
			foundToday = true
			break
		}
	}
	if !foundToday {
		chosen = dailyAuditRoster[0]
		tomorrow := utc.AddDate(0, 0, 1)
		nextFire = time.Date(tomorrow.Year(), tomorrow.Month(), tomorrow.Day(), chosen.HourUTC, chosen.MinuteUTC, 0, 0, time.UTC)
	}

	diffMins := int(nextFire.Sub(utc).Round(time.Minute) / time.Minute)
	if diffMins < 1 && nextFire.After(utc) {
		diffMins = 1
	}

	firstLabel := dailyAuditRoster[0].TimeLabel
	lastLabel := dailyAuditRoster[len(dailyAuditRoster)-1].TimeLabel
	windowUTC := strings.TrimSuffix(firstLabel, " UTC") + "–" + lastLabel

	return scopeScheduleInfo{
		NextAuditID:     chosen.ID,
		NextAuditName:   chosen.Name,
		NextAuditAt:     nextFire.Format(time.RFC3339),
		NextAuditInMins: diffMins,
		WindowUTC:       windowUTC,
		DailyAudits:     slots,
		StarterPrompt:   alertsStarterPrompt,
	}
}

func (s *server) handleScope(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), scopeTimeout)
	defer cancel()
	writeJSON(w, http.StatusOK, s.readScope(ctx, time.Now().UTC()))
}

func (s *server) readScope(ctx context.Context, now time.Time) scopeResponse {
	var raw upstreamScopePayload
	scopeFetched := false

	resp, err := s.hermes(ctx, http.MethodGet, scopePath, nil)
	if err == nil {
		if resp.StatusCode == http.StatusOK {
			if decErr := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&raw); decErr == nil {
				scopeFetched = true
			}
		}
		drainAndClose(resp)
	}

	gatewayHealthy := scopeFetched
	gatewayDetail := ""
	if !scopeFetched {
		hResp, hErr := s.hermes(ctx, http.MethodGet, healthPath, nil)
		switch {
		case hErr != nil:
			gatewayDetail = "agent gateway unreachable: " + hErr.Error()
		default:
			drainAndClose(hResp)
			gatewayHealthy = hResp.StatusCode == http.StatusOK
			gatewayDetail = fmt.Sprintf("agent gateway answered HTTP %d", hResp.StatusCode)
		}
	}

	return buildScopeResponse(s.cfg, raw, scopeFetched, gatewayHealthy, gatewayDetail, now)
}

func buildScopeResponse(cfg config, raw upstreamScopePayload, scopeFetched, gatewayHealthy bool, gatewayDetail string, now time.Time) scopeResponse {
	watcher := buildWatcherStatus(raw.EventWatcher, scopeFetched, gatewayHealthy, gatewayDetail)
	watchByTriple := map[[3]string]bool{}
	if raw.EventWatcher != nil {
		for _, c := range raw.EventWatcher.Clusters {
			watchByTriple[[3]string{c.Project, c.Location, c.Cluster}] = c.Up
		}
	}

	identByProfile := map[string]upstreamProfileIdentity{}
	identByProject := map[string][]upstreamProfileIdentity{}
	for _, id := range raw.ProfileIdentities {
		if id.Profile == "" || id.Project == "" || id.Cluster == "" {
			continue
		}
		identByProfile[id.Profile] = id
		identByProject[id.Project] = append(identByProject[id.Project], id)
	}

	profilesByProject := map[string][]string{}
	for prof, proj := range raw.Profiles {
		if prof == "" || proj == "" {
			continue
		}
		profilesByProject[proj] = append(profilesByProject[proj], prof)
	}
	for proj := range profilesByProject {
		sort.Strings(profilesByProject[proj])
	}

	projects := make([]scopeProjectRow, 0, len(raw.Projects))
	allClusters := []scopeClusterRow{}
	seenClusters := map[[3]string]bool{}

	for _, p := range raw.Projects {
		if p.ID == "" {
			continue
		}
		via := p.Via
		if via == nil {
			via = []string{}
		}
		outcome := p.Outcome
		if outcome == "" {
			outcome = outcomeOK
		}
		state := p.State
		if state == "" {
			state = stateInScope
		}
		remedy := outcomeRemediation(outcome)

		profSet := map[string]bool{}
		var profs []string
		for _, name := range profilesByProject[p.ID] {
			if !profSet[name] {
				profSet[name] = true
				profs = append(profs, name)
			}
		}
		for _, id := range identByProject[p.ID] {
			if !profSet[id.Profile] {
				profSet[id.Profile] = true
				profs = append(profs, id.Profile)
			}
		}
		sort.Strings(profs)

		var projClusters []scopeClusterRow
		for _, profName := range profs {
			if id, ok := identByProfile[profName]; ok {
				row := makeClusterRow(id.Project, id.Location, id.Cluster, id.Profile, outcome, id.Ready, remedy, watchByTriple)
				projClusters = append(projClusters, row)
				triple := [3]string{row.Project, row.Location, row.Name}
				if !seenClusters[triple] {
					seenClusters[triple] = true
					allClusters = append(allClusters, row)
				}
			} else {
				row := scopeClusterRow{
					Project:     p.ID,
					Name:        profName,
					Profile:     profName,
					Outcome:     outcome,
					Ready:       outcome == outcomeOK,
					Remediation: remedy,
				}
				projClusters = append(projClusters, row)
				triple := [3]string{row.Project, row.Location, row.Name}
				if !seenClusters[triple] {
					seenClusters[triple] = true
					allClusters = append(allClusters, row)
				}
			}
		}

		// When a project failed listing or has no profile directories on disk
		// yet, surface a project-level row in allClusters so the cluster table
		// shows why its clusters could not be onboarded.
		if len(projClusters) == 0 {
			placeholderName := "(no clusters discovered)"
			if p.Clusters != nil && *p.Clusters > 0 {
				placeholderName = fmt.Sprintf("(%d %s discovered)", *p.Clusters, pluralWord(*p.Clusters, "cluster", "clusters"))
			} else if outcome != outcomeOK {
				placeholderName = "(cluster discovery failed)"
			}
			if cfg.ClusterName != "" && p.ID == cfg.ProjectID {
				placeholderName = cfg.ClusterName
			}
			row := makeClusterRow(p.ID, cfg.Location, placeholderName, "", outcome, outcome == outcomeOK, remedy, watchByTriple)
			if p.ID != cfg.ProjectID {
				row.Location = ""
			}
			projClusters = append(projClusters, row)
			triple := [3]string{row.Project, row.Location, row.Name}
			if !seenClusters[triple] {
				seenClusters[triple] = true
				allClusters = append(allClusters, row)
			}
		}

		projects = append(projects, scopeProjectRow{
			ID:          p.ID,
			Via:         via,
			Outcome:     outcome,
			State:       state,
			Clusters:    p.Clusters,
			Profiles:    profs,
			ClusterRows: projClusters,
			Remediation: remedy,
			AbsentSince: p.AbsentSince,
		})
	}

	// Include any profile identities whose project was not in raw.Projects
	// (for example before the first reconcile snapshot is written).
	for _, id := range raw.ProfileIdentities {
		triple := [3]string{id.Project, id.Location, id.Cluster}
		if seenClusters[triple] || id.Project == "" || id.Cluster == "" {
			continue
		}
		seenClusters[triple] = true
		row := makeClusterRow(id.Project, id.Location, id.Cluster, id.Profile, outcomeOK, id.Ready, "", watchByTriple)
		allClusters = append(allClusters, row)
	}

	// Fallback when neither fleet_scope.json nor profile directories exist yet:
	// populate the host project and cluster from the console's own startup env
	// so a fresh UI-only install never renders an empty roster.
	if len(projects) == 0 && (cfg.ProjectID != "" || cfg.ClusterName != "") {
		projID := cfg.ProjectID
		if projID == "" {
			projID = "management"
		}
		outcome := outcomeOK
		if !gatewayHealthy {
			outcome = outcomeUnreachable
		}
		remedy := outcomeRemediation(outcome)
		count := 1
		var projClusters []scopeClusterRow
		if cfg.ClusterName != "" {
			row := makeClusterRow(projID, cfg.Location, cfg.ClusterName, "", outcome, gatewayHealthy, remedy, watchByTriple)
			projClusters = append(projClusters, row)
			allClusters = append(allClusters, row)
		} else {
			count = 0
		}
		projects = append(projects, scopeProjectRow{
			ID:          projID,
			Via:         []string{viaManagement},
			Outcome:     outcome,
			State:       stateInScope,
			Clusters:    &count,
			Profiles:    []string{},
			ClusterRows: projClusters,
			Remediation: remedy,
		})
	}

	containers := make([]scopeContainerRow, 0, len(raw.Containers))
	for _, c := range raw.Containers {
		if c.ID == "" {
			continue
		}
		containers = append(containers, scopeContainerRow{
			ID:          c.ID,
			Outcome:     c.Outcome,
			Projects:    c.Projects,
			Remediation: outcomeRemediation(c.Outcome),
		})
	}

	summary := scopeSummaryCounts{
		TotalProjects: len(projects),
		TotalClusters: len(allClusters),
	}
	for _, p := range projects {
		if p.Outcome == outcomeOK {
			summary.OKProjects++
		} else {
			summary.ErrorProjects++
		}
	}
	for _, c := range allClusters {
		if c.Outcome == outcomeOK {
			summary.OKClusters++
		} else {
			summary.ErrorClusters++
		}
	}

	return scopeResponse{
		ResolvedAt:   raw.ResolvedAt,
		Present:      raw.Present,
		Boundary:     raw.Boundary,
		MaxProjects:  raw.MaxProjects,
		Resolver:     raw.Resolver,
		Summary:      summary,
		Projects:     projects,
		Clusters:     allClusters,
		Containers:   containers,
		EventWatcher: watcher,
		Schedule:     nextDailyAudit(now),
	}
}

func makeClusterRow(project, location, name, profile, projectOutcome string, profileReady bool, projectRemedy string, watchByTriple map[[3]string]bool) scopeClusterRow {
	outcome := projectOutcome
	if outcome == "" {
		outcome = outcomeOK
	}
	remedy := projectRemedy
	if outcome == outcomeOK && profile != "" && !profileReady {
		outcome = outcomeUnreachable
		remedy = "Cluster Agent profile scaffold is incomplete (kubeconfig.yaml or USER.md missing); the next cluster_agent_reconcile.py tick will retry get-credentials."
	}
	var watching *bool
	if up, ok := watchByTriple[[3]string{project, location, name}]; ok {
		watching = &up
	} else if up, ok := watchByTriple[[3]string{"", "", name}]; ok {
		watching = &up
	}
	return scopeClusterRow{
		Project:     project,
		Location:    location,
		Name:        name,
		Profile:     profile,
		Outcome:     outcome,
		Ready:       profileReady,
		Watching:    watching,
		Remediation: remedy,
	}
}

func buildWatcherStatus(ew *upstreamEventWatcher, scopeFetched, gatewayHealthy bool, gatewayDetail string) scopeWatcherStatus {
	if ew != nil {
		if !ew.Enabled {
			return scopeWatcherStatus{
				Enabled: false,
				Healthy: false,
				Status:  watcherStatusDisabled,
				Detail:  "k8s-event-watcher is disabled by configuration (EVENT_WATCHER_ENABLED=false).",
			}
		}
		if ew.Reachable {
			if ew.WatchSetReady {
				detail := fmt.Sprintf("Watching %d of %d %s for Kubernetes Warning events",
					ew.ClustersUp, ew.ClustersTotal, pluralWord(ew.ClustersTotal, "cluster", "clusters"))
				if ew.ClustersTotal == 0 {
					detail = "Active and listening for Kubernetes Warning events"
				}
				if ew.DiscoveryErrors > 0 {
					detail += fmt.Sprintf(" (%d profile discovery %s)", ew.DiscoveryErrors, pluralWord(ew.DiscoveryErrors, "error", "errors"))
				}
				return scopeWatcherStatus{
					Enabled:         true,
					Healthy:         true,
					Status:          watcherStatusWatching,
					WatchSetReady:   true,
					ClustersUp:      ew.ClustersUp,
					ClustersTotal:   ew.ClustersTotal,
					DiscoveryErrors: ew.DiscoveryErrors,
					Detail:          detail,
				}
			}
			return scopeWatcherStatus{
				Enabled:         true,
				Healthy:         true,
				Status:          watcherStatusStarting,
				WatchSetReady:   false,
				ClustersUp:      ew.ClustersUp,
				ClustersTotal:   ew.ClustersTotal,
				DiscoveryErrors: ew.DiscoveryErrors,
				Detail:          "Assembling initial cluster watch set",
			}
		}
		// Watcher is enabled in the sidecar, but its metrics port was not
		// reachable (or not configured). When the gateway itself answered
		// /api/scope, report it as running with metrics unavailable.
		if scopeFetched {
			detail := "Active in gateway pod (metrics listener not reachable)"
			if ew.Error != "" {
				detail = "Active in gateway pod (" + ew.Error + ")"
			}
			return scopeWatcherStatus{
				Enabled: true,
				Healthy: true,
				Status:  watcherStatusWatching,
				Detail:  detail,
			}
		}
	}

	if gatewayHealthy {
		return scopeWatcherStatus{
			Enabled: true,
			Healthy: true,
			Status:  watcherStatusWatching,
			Detail:  "Agent gateway reachable; k8s-event-watcher opens an #alerts thread when a Warning event arrives",
		}
	}
	detail := "Agent gateway unreachable"
	if gatewayDetail != "" {
		detail = gatewayDetail
	}
	return scopeWatcherStatus{
		Enabled: true,
		Healthy: false,
		Status:  watcherStatusUnreachable,
		Detail:  detail,
	}
}

func pluralWord(n int, singular, plural string) string {
	if n == 1 {
		return singular
	}
	return plural
}
