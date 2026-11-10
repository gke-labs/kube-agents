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
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeKubeAPI answers the reads /api/environment makes. Every list carries
// names the response must never repeat.
func fakeKubeAPI(t *testing.T) *httptest.Server {
	t.Helper()
	items := func(n int) string {
		var b strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, `{"metadata":{"name":"customer-secret-name-%d","namespace":"acme-payments"}}`, i)
			if i < n-1 {
				b.WriteString(",")
			}
		}
		return b.String()
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer sa-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == "/version":
			fmt.Fprint(w, `{"major":"1","minor":"33+","gitVersion":"v1.33.4-gke.1234000","platform":"linux/amd64"}`)
		case r.URL.Path == "/api/v1/pods":
			// A server that reports the remainder: one item plus 41 more.
			fmt.Fprintf(w, `{"metadata":{"remainingItemCount":41,"continue":"x"},"items":[%s]}`, items(1))
		case r.URL.Path == "/api/v1/nodes":
			// A server that does not: two pages of 3 and 2.
			if r.URL.Query().Get("continue") == "" && r.URL.Query().Get("limit") == "1" {
				fmt.Fprintf(w, `{"metadata":{"continue":"p1"},"items":[%s]}`, items(1))
			} else if r.URL.Query().Get("continue") == "" {
				fmt.Fprintf(w, `{"metadata":{"continue":"p2"},"items":[%s]}`, items(3))
			} else {
				fmt.Fprintf(w, `{"metadata":{},"items":[%s]}`, items(2))
			}
		case r.URL.Path == "/apis/apps/v1/deployments":
			fmt.Fprintf(w, `{"metadata":{},"items":[%s]}`, items(1))
		case strings.HasSuffix(r.URL.Path, "/cronjobs"):
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"message":"cronjobs.batch is forbidden: User \"system:serviceaccount:ns:web\" cannot list"}`)
		case strings.HasPrefix(r.URL.Path, "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent"):
			fmt.Fprint(w, `{"metadata":{"name":"platform-agent","creationTimestamp":"2026-10-06T19:07:47Z","generation":2},"status":{"phase":"Ready","lastReconcileTime":"2026-10-09T08:54:42Z"}}`)
		case r.URL.Path == "/apis/apps/v1/namespaces/kubeagents-system/replicasets":
			fmt.Fprint(w, `{"items":[
				{"metadata":{"name":"platform-agent-gateway-1","creationTimestamp":"2026-10-06T19:08:00Z","ownerReferences":[{"kind":"Deployment","name":"platform-agent-gateway"}]}},
				{"metadata":{"name":"platform-agent-gateway-2","creationTimestamp":"2026-10-06T22:41:30Z","ownerReferences":[{"kind":"Deployment","name":"platform-agent-gateway"}]}},
				{"metadata":{"name":"kube-agents-web-console-9","creationTimestamp":"2026-10-09T19:11:37Z","ownerReferences":[{"kind":"Deployment","name":"kube-agents-web-console"}]}}]}`)
		default:
			fmt.Fprintf(w, `{"metadata":{},"items":[%s]}`, items(0))
		}
	}))
}

func TestEnvironmentFromFakeAPI(t *testing.T) {
	api := fakeKubeAPI(t)
	defer api.Close()
	_, s := setupServer(t)
	s.cfg.ChartVersion, s.cfg.AppVersion, s.cfg.ImageTag, s.cfg.HelmRevision = "0.8.0", "v0.8.0", "06e1ca65", "2"
	s.cfg.PodNamespace, s.cfg.PlatformAgentName = "kubeagents-system", "platform-agent"
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}
	h := s.routes()
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/environment", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	for _, leak := range []string{"customer-secret-name", "acme-payments", "sa-token"} {
		if strings.Contains(body, leak) {
			t.Errorf("the environment response carried %q: %s", leak, body)
		}
	}
	got := decode[environmentResponse](t, rec)
	if got.Versions.Kubernetes != "v1.33.4-gke.1234000" || got.Versions.KubernetesMinor != "1.33" || got.Versions.ChartVersion != "0.8.0" || got.Versions.HelmRevision != "2" {
		t.Errorf("versions = %+v", got.Versions)
	}
	if got.Counts["pods"] != 42 || got.Counts["nodes"] != 5 || got.Counts["deployments"] != 1 {
		t.Errorf("counts = %v, want pods 42 (remainder), nodes 5 (paged), deployments 1", got.Counts)
	}
	if _, has := got.Counts["cronjobs"]; has || !strings.Contains(got.Unavailable["cronjobs"], "HTTP 403") {
		t.Errorf("a forbidden list should be reported unavailable, not counted: counts=%v unavailable=%v", got.Counts, got.Unavailable)
	}
	if got.Install.InstalledAt != "2026-10-06T19:07:47Z" || got.Install.Phase != "Ready" || got.Install.Generation != 2 {
		t.Errorf("install = %+v", got.Install)
	}
	if got.Install.LastRolloutAt != "2026-10-06T22:41:30Z" {
		t.Errorf("last rollout = %q, want the newest agent ReplicaSet, not the console's", got.Install.LastRolloutAt)
	}
	// A second read within the TTL does not hit the API again.
	api.Close()
	rec2 := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/environment", nil))
	if rec2.Code != http.StatusOK || decode[environmentResponse](t, rec2).Counts["pods"] != 42 {
		t.Errorf("cached read failed: %d %s", rec2.Code, rec2.Body.String())
	}
}

func TestEnvironmentWithoutAServiceAccount(t *testing.T) {
	_, s := setupServer(t)
	s.kube = nil
	rec := serve(s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/environment", nil))
	got := decode[environmentResponse](t, rec)
	if rec.Code != http.StatusOK || got.Unavailable["kubernetes"] == "" || len(got.Counts) != 0 {
		t.Errorf("without a token: %d %+v", rec.Code, got)
	}
}

func TestInstallManagedBy(t *testing.T) {
	helm := map[string]string{helmReleaseNameAnnotation: "kube-agents", helmReleaseNamespaceAnnotation: "kubeagents-system"}
	if m, rel, ns, app := installManagedBy(map[string]string{managedByLabel: "Helm"}, helm); m != "helm" || rel != "kube-agents" || ns != "kubeagents-system" || app != "" {
		t.Fatalf("helm: %s %s %s %s", m, rel, ns, app)
	}
	if m, _, _, app := installManagedBy(map[string]string{argoCDInstanceLabel: "kube-agents-prod", managedByLabel: "Helm"}, helm); m != "argocd" || app != "kube-agents-prod" {
		t.Fatalf("argocd should win over helm: %s %s", m, app)
	}
	tracked := map[string]string{helmReleaseNameAnnotation: "kube-agents", argoCDTrackingAnnotation: "kube-agents-prod:kubeagents.x-k8s.io/PlatformAgent:kubeagents-system/platform-agent"}
	if m, _, _, app := installManagedBy(map[string]string{managedByLabel: "Helm"}, tracked); m != "argocd" || app != "kube-agents-prod" {
		t.Fatalf("annotation-tracked argocd should win over helm: %s %s", m, app)
	}
	if m, rel, _, _ := installManagedBy(nil, nil); m != "unknown" || rel != "" {
		t.Fatalf("unknown: %s %s", m, rel)
	}
}

func TestKubeClientRereadsTheRotatedToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("first\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	k := &kubeClient{token: "startup", tokenPath: path}
	if got := k.bearer(); got != "first" {
		t.Fatalf("bearer = %q, want first", got)
	}
	if err := os.WriteFile(path, []byte("rotated"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := k.bearer(); got != "rotated" {
		t.Errorf("bearer after rotation = %q, want rotated", got)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if got := k.bearer(); got != "startup" {
		t.Errorf("bearer with the file gone = %q, want the startup token", got)
	}
}

// An install whose PlatformAgent and Helm release carry non-default names:
// the live spec.deployment.tag and the ownership metadata are read through
// the route, and the rollout date comes from this install's Deployments,
// not ones named after the defaults.
func TestEnvironmentReadsTheLiveTagAndThisInstallsDeployments(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentTypeJSON)
		switch r.URL.Path {
		case fmt.Sprintf(platformAgentsAPIPath, "ops", "ops-agent"):
			fmt.Fprint(w, `{"metadata":{"creationTimestamp":"2026-10-06T19:07:47Z",
				"labels":{"app.kubernetes.io/managed-by":"Helm"},
				"annotations":{"meta.helm.sh/release-name":"ka-prod","meta.helm.sh/release-namespace":"ops"}},
				"spec":{"deployment":{"tag":"0.12.0"}}}`)
		case fmt.Sprintf(replicaSetsAPIPath, "ops"):
			fmt.Fprint(w, `{"items":[
				{"metadata":{"creationTimestamp":"2026-10-07T10:00:00Z","ownerReferences":[{"kind":"Deployment","name":"ops-agent-gateway"}]}},
				{"metadata":{"creationTimestamp":"2026-10-08T10:00:00Z","ownerReferences":[{"kind":"Deployment","name":"ka-prod-controller-manager"}]}},
				{"metadata":{"creationTimestamp":"2026-10-09T10:00:00Z","ownerReferences":[{"kind":"Deployment","name":"platform-agent-gateway"}]}},
				{"metadata":{"creationTimestamp":"2026-10-09T11:00:00Z","ownerReferences":[{"kind":"Deployment","name":"kube-agents-controller-manager"}]}}]}`)
		default:
			fmt.Fprint(w, `{"metadata":{},"items":[]}`)
		}
	}))
	defer api.Close()
	_, s := setupServer(t)
	s.cfg.ImageTag, s.cfg.ChartVersion = "06e1ca65", "0.1.0"
	s.cfg.PodNamespace, s.cfg.PlatformAgentName = "ops", "ops-agent"
	s.kube = &kubeClient{base: api.URL, client: api.Client()}
	rec := serve(s.routes(), httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/environment", nil))
	got := decode[environmentResponse](t, rec)
	if got.Versions.ImageTag != "0.12.0" {
		t.Errorf("image tag = %q, want the PlatformAgent's 0.12.0 over cfg's 06e1ca65", got.Versions.ImageTag)
	}
	if got.Install.ManagedBy != "helm" || got.Install.HelmRelease != "ka-prod" || got.Install.ReleaseNamespace != "ops" {
		t.Errorf("ownership = %+v", got.Install)
	}
	if got.Install.LastRolloutAt != "2026-10-08T10:00:00Z" {
		t.Errorf("last rollout = %q, want ka-prod-controller-manager's, not a default-named Deployment's", got.Install.LastRolloutAt)
	}
}

// A list walk that fails after its first page is reported unavailable, not
// as a partial count flagged as a floor.
func TestEnvironmentReportsAFailedWalkAsUnavailable(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentTypeJSON)
		if r.URL.Path != "/api/v1/nodes" {
			fmt.Fprint(w, `{"metadata":{},"items":[]}`)
			return
		}
		switch {
		case r.URL.Query().Get("limit") == "1":
			fmt.Fprint(w, `{"metadata":{"continue":"p1"},"items":[{}]}`)
		case r.URL.Query().Get("continue") == "":
			fmt.Fprint(w, `{"metadata":{"continue":"p2"},"items":[{},{},{}]}`)
		default:
			w.WriteHeader(http.StatusGone)
		}
	}))
	defer api.Close()
	_, s := setupServer(t)
	s.kube = &kubeClient{base: api.URL, client: api.Client()}
	got := s.readEnvironment(t.Context())
	if _, counted := got.Counts["nodes"]; counted || !strings.Contains(got.Unavailable["nodes"], "HTTP 410") {
		t.Errorf("counts=%v unavailable=%v, want nodes unavailable with the 410", got.Counts, got.Unavailable)
	}
	for _, k := range got.Floor {
		if k == "nodes" {
			t.Errorf("a failed walk was flagged as a floor: %v", got.Floor)
		}
	}
}

// A refresh whose request was cancelled is not cached for the next caller.
func TestEnvironmentDoesNotCacheACancelledRefresh(t *testing.T) {
	api := fakeKubeAPI(t)
	defer api.Close()
	_, s := setupServer(t)
	s.cfg.PodNamespace = "kubeagents-system"
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	s.currentEnvironment(ctx)
	if s.environment.value != nil {
		t.Fatalf("a cancelled refresh was cached: %+v", *s.environment.value)
	}
	if got := s.currentEnvironment(t.Context()); got.Counts["pods"] != 42 || s.environment.value == nil {
		t.Fatalf("the next refresh did not read the API: %+v", got)
	}
}

func TestRolloutDeploymentsFollowTheInstallNames(t *testing.T) {
	got := rolloutDeployments("ops-agent", "ka-prod")
	for _, name := range []string{"ka-prod-controller-manager", "ops-agent-gateway", "ops-agent-credential-proxy", "litellm"} {
		if !got[name] {
			t.Errorf("%s missing from %v", name, got)
		}
	}
	if got["platform-agent-gateway"] || got["kube-agents-controller-manager"] {
		t.Errorf("default names kept for a renamed install: %v", got)
	}
	if def := rolloutDeployments("", ""); !def["kube-agents-controller-manager"] || !def["platform-agent-gateway"] {
		t.Errorf("defaults = %v", def)
	}
}

func TestInstallVersionFollowsThePlatformAgentTag(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentTypeJSON)
		if r.URL.Path == fmt.Sprintf(platformAgentsAPIPath, "kubeagents-system", "platform-agent") {
			fmt.Fprint(w, `{"metadata":{"name":"platform-agent","creationTimestamp":"2026-10-01T00:00:00Z"},"spec":{"deployment":{"tag":"0.12.0"}}}`)
			return
		}
		fmt.Fprint(w, `{"metadata":{},"items":[]}`)
	}))
	defer api.Close()

	_, s := setupServer(t)
	s.cfg.PodNamespace = "kubeagents-system"
	s.cfg.PlatformAgentName = "platform-agent"
	s.cfg.ImageTag = "0.8.0"
	s.cfg.ChartVersion = "0.8.0"
	s.cfg.AppVersion = "v0.8.0"
	s.cfg.ReleaseCheck = false
	s.kube = &kubeClient{base: api.URL, token: "sa-token", client: api.Client()}

	if tag, chart := s.liveInstallVersion(t.Context()); tag != "0.12.0" || chart != "0.8.0" {
		t.Fatalf("liveInstallVersion = %q, %q; want the PlatformAgent's 0.12.0 tag and the chart's 0.8.0 version", tag, chart)
	}
	if rel := s.currentRelease(t.Context()); rel.Running != "0.12.0" {
		t.Fatalf("currentRelease running %q, want 0.12.0 from the PlatformAgent", rel.Running)
	}
	env := s.readEnvironment(t.Context())
	if env.Versions.ImageTag != "0.12.0" || env.Versions.ChartVersion != "0.8.0" || env.Versions.AppVersion != "v0.8.0" {
		t.Fatalf("readEnvironment versions %+v, want ImageTag 0.12.0 from the PlatformAgent and ChartVersion/AppVersion preserved from the chart", env.Versions)
	}
}
