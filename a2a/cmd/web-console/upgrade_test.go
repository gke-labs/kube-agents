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
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestUpgradeStartAndStatusLifecycle(t *testing.T) {
	var (
		mu      sync.Mutex
		podJSON map[string]any
		phase   = "Running"
		logs    = ">>> 1. Validating Upgrade Target & Environment <<<\n  ✓ Loaded install configuration\n>>> 4. Upgrading Kubernetes Operator (CRDs & Controller Manager) <<<\n"
	)

	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer sa-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods/kube-agents-upgrade-runner":
			if podJSON == nil {
				w.WriteHeader(http.StatusNotFound)
				fmt.Fprint(w, `{"message":"not found"}`)
				return
			}
			w.Header().Set("Content-Type", contentTypeJSON)
			meta, _ := podJSON["metadata"].(map[string]any)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"metadata": meta,
				"status": map[string]any{
					"phase":     phase,
					"startTime": "2026-10-15T22:00:00Z",
				},
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods":
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &podJSON)
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write(raw)
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods/kube-agents-upgrade-runner/log":
			w.Header().Set("Content-Type", "text/plain")
			fmt.Fprint(w, logs)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer api.Close()

	_, s := setupServer(t)
	s.cfg.PodNamespace = "kubeagents-system"
	s.cfg.ProjectID = "gca-gke-test"
	s.cfg.ClusterName = "ka-webconsole-pr2321"
	s.cfg.Location = "us-central1"
	s.cfg.ImageTag = "0.8.0"
	s.cfg.ChartVersion = "0.8.0"
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}
	h := s.routes()

	// 1. Status is initially idle.
	rec0 := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/upgrade/status", nil))
	if rec0.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec0.Code, rec0.Body.String())
	}
	if st := decode[upgradeStatusResponse](t, rec0); st.State != "idle" {
		t.Fatalf("want idle, got %+v", st)
	}

	// 2. Start upgrade to 0.12.0.
	startReq := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/upgrade", strings.NewReader(`{"target":"0.12.0"}`))
	startReq.Header.Set("Content-Type", contentTypeJSON)
	startReq.Header.Set(consoleHeader, consoleHeaderValue)
	rec1 := serve(h, startReq)
	if rec1.Code != http.StatusAccepted {
		t.Fatalf("want 202, got %d: %s", rec1.Code, rec1.Body.String())
	}
	st1 := decode[upgradeStatusResponse](t, rec1)
	if st1.State != "running" || st1.Target != "0.12.0" || !strings.Contains(st1.Command, "0.12.0/upgrade.sh") {
		t.Fatalf("unexpected start response: %+v", st1)
	}

	// 3. Poll status while running.
	rec2 := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/upgrade/status", nil))
	st2 := decode[upgradeStatusResponse](t, rec2)
	if st2.State != "running" || st2.Phase != "4. Upgrading Kubernetes Operator (CRDs & Controller Manager)" {
		t.Fatalf("unexpected running status: %+v", st2)
	}

	// 4. Transition pod to Succeeded and verify version update.
	mu.Lock()
	phase = "Succeeded"
	logs += ">>> 🎉 Upgrade to 0.12.0 Complete! <<<\n"
	mu.Unlock()

	rec3 := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/upgrade/status", nil))
	st3 := decode[upgradeStatusResponse](t, rec3)
	if st3.State != "succeeded" || !strings.Contains(st3.Phase, "Upgrade to 0.12.0 Complete!") {
		t.Fatalf("unexpected succeeded status: %+v", st3)
	}
	if s.cfg.ImageTag != "0.12.0" || s.cfg.ChartVersion != "0.12.0" {
		t.Fatalf("server version was not updated after upgrade: ImageTag=%q ChartVersion=%q", s.cfg.ImageTag, s.cfg.ChartVersion)
	}
}

func TestAutoUpgradeToggleAndAutomaticTrigger(t *testing.T) {
	var (
		mu       sync.Mutex
		podJSON  map[string]any
		paAnn    = map[string]string{}
		runningV = "0.8.0"
	)

	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentTypeJSON)
		_ = json.NewEncoder(w).Encode([]map[string]any{
			{
				"tag_name":     "v0.12.0",
				"html_url":     "https://github.com/gke-labs/kube-agents/releases/tag/v0.12.0",
				"published_at": "2026-10-16T01:00:00Z",
			},
		})
	}))
	defer gh.Close()

	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer sa-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent":
			w.Header().Set("Content-Type", contentTypeJSON)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"metadata": map[string]any{
					"name":        "platform-agent",
					"namespace":   "kubeagents-system",
					"annotations": paAnn,
				},
				"spec": map[string]any{
					"deployment": map[string]any{
						"tag": runningV,
					},
				},
			})
		case r.Method == http.MethodPatch && r.URL.Path == "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent":
			var body struct {
				Metadata struct {
					Annotations map[string]string `json:"annotations"`
				} `json:"metadata"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			for k, v := range body.Metadata.Annotations {
				paAnn[k] = v
			}
			w.Header().Set("Content-Type", contentTypeJSON)
			_ = json.NewEncoder(w).Encode(map[string]any{"metadata": map[string]any{"annotations": paAnn}})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods/kube-agents-upgrade-runner":
			if podJSON == nil {
				w.WriteHeader(http.StatusNotFound)
				fmt.Fprint(w, `{"message":"not found"}`)
				return
			}
			w.Header().Set("Content-Type", contentTypeJSON)
			meta, _ := podJSON["metadata"].(map[string]any)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"metadata": meta,
				"status": map[string]any{
					"phase":     "Running",
					"startTime": "2026-10-16T10:00:00Z",
				},
			})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods":
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &podJSON)
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write(raw)
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods/kube-agents-upgrade-runner/log":
			w.Header().Set("Content-Type", "text/plain")
			fmt.Fprint(w, ">>> Downloading and executing release-pinned upgrade.sh (0.12.0) <<<\n")
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer api.Close()

	_, s := setupServer(t)
	s.cfg.PodNamespace = "kubeagents-system"
	s.cfg.PlatformAgentName = "platform-agent"
	s.cfg.ProjectID = "gca-gke-test"
	s.cfg.ClusterName = "ka-webconsole-pr2321"
	s.cfg.Location = "us-central1"
	s.cfg.ImageTag = "0.8.0"
	s.cfg.ChartVersion = "0.8.0"
	s.cfg.ReleaseCheck = true
	s.cfg.GitHubAPIBase = gh.URL
	s.githubClient = gh.Client()
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}
	// The release is 72h old and no kanban card is running, so every
	// automatic upgrade gate passes.
	s.cfg.AutoUpgradeMinReleaseAge = defaultAutoUpgradeMinReleaseAge
	s.now = func() time.Time { return time.Date(2026, 10, 19, 1, 0, 0, 0, time.UTC) }
	s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	h := s.routes()

	// 1. Initially auto_upgrade is false.
	rec0 := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/upgrade/auto", nil))
	if rec0.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec0.Code, rec0.Body.String())
	}
	var autoResp struct {
		AutoUpgrade bool `json:"auto_upgrade"`
	}
	_ = json.Unmarshal(rec0.Body.Bytes(), &autoResp)
	if autoResp.AutoUpgrade {
		t.Fatalf("expected auto_upgrade false by default")
	}

	// 2. Enable auto_upgrade via POST /api/upgrade/auto; since 0.8.0 < 0.12.0, it immediately triggers the upgrade pod.
	enableReq := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/upgrade/auto", strings.NewReader(`{"enabled":true}`))
	enableReq.Header.Set("Content-Type", contentTypeJSON)
	enableReq.Header.Set(consoleHeader, consoleHeaderValue)
	rec1 := serve(h, enableReq)
	if rec1.Code != http.StatusOK {
		t.Fatalf("want 200, got %d: %s", rec1.Code, rec1.Body.String())
	}
	st1 := decode[upgradeStatusResponse](t, rec1)
	if !st1.AutoUpgrade || st1.State != "running" || st1.Target != "0.12.0" || st1.TriggeredBy != "auto" {
		t.Fatalf("expected auto-upgrade to trigger immediately for 0.12.0, got %+v", st1)
	}
	mu.Lock()
	persisted := paAnn[autoUpgradeAnnotation]
	mu.Unlock()
	if persisted != "true" {
		t.Fatalf("expected PlatformAgent annotation %s=true, got %q", autoUpgradeAnnotation, persisted)
	}

	// 3. Disable auto_upgrade and verify persistence.
	disableReq := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/upgrade/auto", strings.NewReader(`{"enabled":false}`))
	disableReq.Header.Set("Content-Type", contentTypeJSON)
	disableReq.Header.Set(consoleHeader, consoleHeaderValue)
	rec2 := serve(h, disableReq)
	st2 := decode[upgradeStatusResponse](t, rec2)
	if st2.AutoUpgrade {
		t.Fatalf("expected auto_upgrade false after disabling, got %+v", st2)
	}
}

// newKanbanBoard fakes the Hermes kanban board read: running cards in the
// running column, or a bare status code when status is not 200.
func newKanbanBoard(t *testing.T, running, status int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != kanbanBoardPath || r.Header.Get("Authorization") != bearerPrefix+testKey {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if status != http.StatusOK {
			w.WriteHeader(status)
			return
		}
		tasks := make([]map[string]any, running)
		for i := range tasks {
			tasks[i] = map[string]any{"id": fmt.Sprintf("t_%d", i), "status": "running"}
		}
		w.Header().Set("Content-Type", contentTypeJSON)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"columns": []map[string]any{
				{"name": "todo", "tasks": []map[string]any{{"id": "t_todo"}}},
				{"name": "running", "tasks": tasks},
				{"name": "done", "tasks": []map[string]any{{"id": "t_done"}}},
			},
		})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// autoUpgradeFixture is a console on 0.8.0 with release 0.12.0 published
// on GitHub, auto-upgrade switched on through the PlatformAgent annotation,
// and a fake Kubernetes API that counts runner pod creations.
type autoUpgradeFixture struct {
	s     *server
	mu    sync.Mutex
	posts int
}

func (f *autoUpgradeFixture) podPosts() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.posts
}

func newAutoUpgradeFixture(t *testing.T, publishedAt string, autoOn bool) *autoUpgradeFixture {
	t.Helper()
	f := &autoUpgradeFixture{}
	var podJSON map[string]any

	gh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rel := map[string]any{"tag_name": "v0.12.0", "html_url": "https://github.com/gke-labs/kube-agents/releases/tag/v0.12.0"}
		if publishedAt != "" {
			rel["published_at"] = publishedAt
		}
		w.Header().Set("Content-Type", contentTypeJSON)
		_ = json.NewEncoder(w).Encode([]map[string]any{rel})
	}))
	t.Cleanup(gh.Close)

	ann := map[string]string{}
	if autoOn {
		ann[autoUpgradeAnnotation] = "true"
	}
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent":
			w.Header().Set("Content-Type", contentTypeJSON)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"metadata": map[string]any{"name": "platform-agent", "annotations": ann},
				"spec":     map[string]any{"deployment": map[string]any{"tag": "0.8.0"}},
			})
		case r.Method == http.MethodGet && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods/kube-agents-upgrade-runner":
			if podJSON == nil {
				w.WriteHeader(http.StatusNotFound)
				fmt.Fprint(w, `{"message":"not found"}`)
				return
			}
			meta, _ := podJSON["metadata"].(map[string]any)
			w.Header().Set("Content-Type", contentTypeJSON)
			_ = json.NewEncoder(w).Encode(map[string]any{"metadata": meta, "status": map[string]any{"phase": "Running"}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/namespaces/kubeagents-system/pods":
			f.posts++
			raw, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(raw, &podJSON)
			w.Header().Set("Content-Type", contentTypeJSON)
			w.WriteHeader(http.StatusCreated)
			_, _ = w.Write(raw)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(api.Close)

	_, s := setupServer(t)
	s.cfg.PodNamespace = "kubeagents-system"
	s.cfg.PlatformAgentName = "platform-agent"
	s.cfg.ImageTag = "0.8.0"
	s.cfg.ChartVersion = "0.8.0"
	s.cfg.ReleaseCheck = true
	s.cfg.GitHubAPIBase = gh.URL
	s.cfg.AutoUpgradeMinReleaseAge = defaultAutoUpgradeMinReleaseAge
	s.githubClient = gh.Client()
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}
	f.s = s
	return f
}

func mustWindow(t *testing.T, raw string) *upgradeWindow {
	t.Helper()
	w, err := parseUpgradeWindow(raw)
	if err != nil {
		t.Fatalf("parseUpgradeWindow(%q): %v", raw, err)
	}
	return w
}

// Saturday 24 Oct 2026 07:00 UTC is 03:00 in Toronto (EDT), inside
// "Sat,Sun 02:00-06:00 America/Toronto"; Wednesday 21 Oct 12:00 UTC is not.
var (
	insideTorontoWindow  = time.Date(2026, 10, 24, 7, 0, 0, 0, time.UTC)
	outsideTorontoWindow = time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
)

const (
	torontoWindow  = "Sat,Sun 02:00-06:00 America/Toronto"
	oldReleaseTime = "2026-10-16T01:00:00Z" // more than 72h before both instants above
)

func TestAutoUpgradeOutsideWindowDoesNotLaunch(t *testing.T) {
	f := newAutoUpgradeFixture(t, oldReleaseTime, true)
	f.s.cfg.AutoUpgradeWindow = mustWindow(t, torontoWindow)
	f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	f.s.now = func() time.Time { return outsideTorontoWindow }

	triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context())
	if triggered || f.podPosts() != 0 {
		t.Fatalf("outside the window: triggered=%v, runner pod POSTs=%d; want no launch", triggered, f.podPosts())
	}
	if !strings.Contains(st.Detail, "maintenance window") || !strings.Contains(st.Detail, torontoWindow) {
		t.Fatalf("Detail %q does not name the window", st.Detail)
	}
	// GET /api/upgrade/status reports the same hold.
	rec := serve(f.s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/upgrade/status", nil))
	if got := decode[upgradeStatusResponse](t, rec); got.State != "idle" || got.Detail != st.Detail {
		t.Fatalf("status = %+v, want idle with Detail %q", got, st.Detail)
	}
}

func TestAutoUpgradeInsideWindowWaitsForRunningCard(t *testing.T) {
	f := newAutoUpgradeFixture(t, oldReleaseTime, true)
	f.s.cfg.AutoUpgradeWindow = mustWindow(t, torontoWindow)
	f.s.cfg.HermesURL = newKanbanBoard(t, 2, http.StatusOK).URL
	f.s.now = func() time.Time { return insideTorontoWindow }

	triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context())
	if triggered || f.podPosts() != 0 {
		t.Fatalf("running cards: triggered=%v, runner pod POSTs=%d; want no launch", triggered, f.podPosts())
	}
	if !strings.Contains(st.Detail, "2 running kanban cards") {
		t.Fatalf("Detail %q does not report the running cards", st.Detail)
	}
}

func TestAutoUpgradeInsideWindowIdleLaunches(t *testing.T) {
	f := newAutoUpgradeFixture(t, oldReleaseTime, true)
	f.s.cfg.AutoUpgradeWindow = mustWindow(t, torontoWindow)
	f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	f.s.now = func() time.Time { return insideTorontoWindow }

	triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context())
	if !triggered || f.podPosts() != 1 {
		t.Fatalf("inside window, idle: triggered=%v, runner pod POSTs=%d; want one launch", triggered, f.podPosts())
	}
	if st.State != "running" || st.Target != "0.12.0" || st.TriggeredBy != "auto" {
		t.Fatalf("unexpected status after launch: %+v", st)
	}
}

func TestAutoUpgradeWithoutWindowRunsAnyTime(t *testing.T) {
	f := newAutoUpgradeFixture(t, oldReleaseTime, true)
	f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	f.s.now = func() time.Time { return outsideTorontoWindow }

	if triggered, _ := f.s.maybeTriggerAutoUpgrade(t.Context()); !triggered || f.podPosts() != 1 {
		t.Fatalf("no window set: triggered=%v, runner pod POSTs=%d; want one launch", triggered, f.podPosts())
	}
}

func TestAutoUpgradeFailsClosedWhenBoardUnreadable(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closed.Close()
	for name, hermesURL := range map[string]string{
		"404":                newKanbanBoard(t, 0, http.StatusNotFound).URL,
		"500":                newKanbanBoard(t, 0, http.StatusInternalServerError).URL,
		"connection refused": closed.URL,
	} {
		t.Run(name, func(t *testing.T) {
			f := newAutoUpgradeFixture(t, oldReleaseTime, true)
			f.s.cfg.HermesURL = hermesURL
			f.s.now = func() time.Time { return insideTorontoWindow }

			triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context())
			if triggered || f.podPosts() != 0 {
				t.Fatalf("unreadable board: triggered=%v, runner pod POSTs=%d; want no launch", triggered, f.podPosts())
			}
			if !strings.Contains(st.Detail, "could not confirm the agents are idle") {
				t.Fatalf("Detail %q does not explain the idle check failure", st.Detail)
			}
		})
	}
}

func TestAutoUpgradeWaitsForMinimumReleaseAge(t *testing.T) {
	cases := map[string]struct {
		publishedAt string
		want        string
	}{
		"published 24h ago": {"2026-10-23T07:00:00Z", "waiting until 2026-10-26T07:00:00Z"},
		"no published time": {"", "did not report when the release was published"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			f := newAutoUpgradeFixture(t, tc.publishedAt, true)
			f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
			f.s.now = func() time.Time { return insideTorontoWindow }

			triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context())
			if triggered || f.podPosts() != 0 {
				t.Fatalf("young release: triggered=%v, runner pod POSTs=%d; want no launch", triggered, f.podPosts())
			}
			if !strings.Contains(st.Detail, tc.want) {
				t.Fatalf("Detail %q does not contain %q", st.Detail, tc.want)
			}
		})
	}

	// AUTO_UPGRADE_MIN_RELEASE_AGE=0 turns the check off.
	f := newAutoUpgradeFixture(t, "2026-10-24T06:59:00Z", true)
	f.s.cfg.AutoUpgradeMinReleaseAge = 0
	f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	f.s.now = func() time.Time { return insideTorontoWindow }
	if triggered, _ := f.s.maybeTriggerAutoUpgrade(t.Context()); !triggered || f.podPosts() != 1 {
		t.Fatalf("min age 0: triggered=%v, runner pod POSTs=%d; want one launch", triggered, f.podPosts())
	}
}

func TestManualUpgradeIgnoresAutoUpgradeGates(t *testing.T) {
	f := newAutoUpgradeFixture(t, "2026-10-21T11:00:00Z", true)
	f.s.cfg.AutoUpgradeWindow = mustWindow(t, torontoWindow)
	f.s.cfg.HermesURL = newKanbanBoard(t, 3, http.StatusOK).URL
	f.s.now = func() time.Time { return outsideTorontoWindow }

	req := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/upgrade", strings.NewReader(`{"target":"0.12.0"}`))
	req.Header.Set("Content-Type", contentTypeJSON)
	req.Header.Set(consoleHeader, consoleHeaderValue)
	rec := serve(f.s.routes(), req)
	if rec.Code != http.StatusAccepted || f.podPosts() != 1 {
		t.Fatalf("manual upgrade: status %d, runner pod POSTs=%d; want 202 and one launch: %s", rec.Code, f.podPosts(), rec.Body.String())
	}
	if st := decode[upgradeStatusResponse](t, rec); st.TriggeredBy != "manual" {
		t.Fatalf("TriggeredBy = %q, want manual", st.TriggeredBy)
	}
}

func TestAutoUpgradeDefaultsOff(t *testing.T) {
	t.Setenv(envAutoUpgrade, "")
	if loadConfig().AutoUpgrade {
		t.Fatalf("%s unset: AutoUpgrade = true, want false", envAutoUpgrade)
	}
	// No annotation on the PlatformAgent and no env: nothing launches even
	// though every gate would pass.
	f := newAutoUpgradeFixture(t, oldReleaseTime, false)
	f.s.cfg.HermesURL = newKanbanBoard(t, 0, http.StatusOK).URL
	f.s.now = func() time.Time { return insideTorontoWindow }
	if triggered, st := f.s.maybeTriggerAutoUpgrade(t.Context()); triggered || f.podPosts() != 0 || st.AutoUpgrade {
		t.Fatalf("default: triggered=%v, runner pod POSTs=%d, auto_upgrade=%v; want off", triggered, f.podPosts(), st.AutoUpgrade)
	}
}

func TestUpgradeWindowContains(t *testing.T) {
	utc := func(day, hour, minute int) time.Time { return time.Date(2026, 10, day, hour, minute, 0, 0, time.UTC) }
	// 24 Oct 2026 is a Saturday.
	cases := []struct {
		window string
		at     time.Time
		want   bool
	}{
		{"Sat 23:00-02:00", utc(24, 23, 30), true},  // Saturday night
		{"Sat 23:00-02:00", utc(25, 1, 30), true},   // early Sunday belongs to Saturday's window
		{"Sat 23:00-02:00", utc(25, 2, 0), false},   // end is exclusive
		{"Sat 23:00-02:00", utc(25, 23, 30), false}, // Sunday night is not listed
		{"Sat 23:00-02:00", utc(24, 1, 30), false},  // early Saturday belongs to Friday
		{"22:00-04:00", utc(21, 3, 0), true},        // no days: every day
		{"22:00-04:00", utc(21, 12, 0), false},
		{"Mon-Fri 09:00-17:00", utc(23, 9, 0), true}, // Friday, start is inclusive
		{"Mon-Fri 09:00-17:00", utc(24, 10, 0), false},
		{"fri-mon 00:00-24:00", utc(25, 12, 0), true}, // range wraps through the weekend
		{"fri-mon 00:00-24:00", utc(21, 12, 0), false},
		{torontoWindow, insideTorontoWindow, true},
		{torontoWindow, time.Date(2026, 10, 24, 5, 30, 0, 0, time.UTC), false}, // 01:30 in Toronto
		{torontoWindow, outsideTorontoWindow, false},
	}
	for _, tc := range cases {
		if got := mustWindow(t, tc.window).contains(tc.at); got != tc.want {
			t.Errorf("%q contains %s = %v, want %v", tc.window, tc.at.Format(time.RFC3339), got, tc.want)
		}
	}
}

func TestParseUpgradeWindowErrors(t *testing.T) {
	for _, raw := range []string{
		"",
		"Sat",
		"Sat 02:00",
		"Sat 02:00-25:00",
		"Funday 02:00-03:00",
		"Sat,,Sun 02:00-03:00",
		"02:00-02:00",
		"02:60-03:00",
		"24:00-03:00",
		"2:0-03:00",
		"02:00-03:00 Mars/Olympus_Mons",
		"Sat 02:00-03:00 UTC extra",
	} {
		if w, err := parseUpgradeWindow(raw); err == nil {
			t.Errorf("parseUpgradeWindow(%q) = %+v, want an error", raw, w)
		}
	}
}

func TestLoadAutoUpgradePolicy(t *testing.T) {
	cases := []struct {
		window, age string
		wantWindow  bool
		wantAge     time.Duration
	}{
		{"", "", false, 72 * time.Hour},
		{torontoWindow, "", true, 72 * time.Hour},
		{"weekends at night", "", false, 72 * time.Hour}, // parse error: treated as unset
		{"", "0", false, 0},
		{"", "24h", false, 24 * time.Hour},
		{"", "three days", false, 72 * time.Hour},
		{"", "-1h", false, 72 * time.Hour},
	}
	for _, tc := range cases {
		t.Setenv(envAutoUpgradeWindow, tc.window)
		t.Setenv(envAutoUpgradeMinReleaseAge, tc.age)
		w, age := loadAutoUpgradePolicy()
		if (w != nil) != tc.wantWindow || age != tc.wantAge {
			t.Errorf("window=%q age=%q: got window %v, age %s; want window %v, age %s", tc.window, tc.age, w != nil, age, tc.wantWindow, tc.wantAge)
		}
	}
}
