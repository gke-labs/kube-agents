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
