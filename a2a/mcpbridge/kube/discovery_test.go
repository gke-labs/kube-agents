package kube

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"k8s.io/client-go/kubernetes"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

// Deployment discovery and credential population through in-process client-go.

const (
	// testForbiddenCtx reaches an API server that denies the PlatformAgent list.
	testForbiddenCtx = "gke_example-project_us-central1_forbidden"
	// testNoCRDCtx reaches an API server without the kube-agents CRDs.
	testNoCRDCtx = "gke_example-project_us-central1_nocrd"
)

func TestDiscoverDeploymentsAndCredentials(t *testing.T) {
	var secretHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case crdAbsPathPlatformAgents:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "kubeagents.x-k8s.io/v1alpha1",
				"kind":       "PlatformAgentList",
				"items": []map[string]any{
					{
						"metadata": map[string]any{
							"name":      testPAName,
							"namespace": testNamespace,
						},
						"spec": map[string]any{
							"mode":       "next",
							"deployment": map[string]any{"tag": "0fe1c8ce"},
							"harness": map[string]any{
								"projectId":   testProjectID,
								"clusterName": testClusterName,
								"location":    testLocation,
							},
							"integration": map[string]any{
								"github": map[string]any{
									"gitRepo": testGitHubRepo,
								},
								"googleChat": map[string]any{"enabled": true},
							},
						},
						"status": map[string]any{
							"conditions": []map[string]any{
								{"type": "Ready", "status": "True", "message": "Reconciled"},
							},
						},
					},
				},
			})
		case crdAbsPathAgentProfiles:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "kubeagents.x-k8s.io/v1alpha1",
				"kind":       "AgentProfileList",
				"items": []map[string]any{
					{"metadata": map[string]any{"name": "default", "namespace": testNamespace}},
					{
						"metadata": map[string]any{"name": "cluster-upgrade", "namespace": testNamespace},
						"spec": map[string]any{
							"clusterRef": map[string]any{"cluster": testClusterName},
						},
					},
				},
			})
		case fmt.Sprintf("/api/v1/namespaces/%s/secrets/%s%s", testNamespace, testPAName, mcpbridge.MCPTokenSecretSuffix):
			secretHits.Add(1)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "Secret",
				"metadata": map[string]any{
					"name":      testPAName + mcpbridge.MCPTokenSecretSuffix,
					"namespace": testNamespace,
				},
				"data": map[string]string{
					mcpbridge.MCPTokenSecretKey: base64.StdEncoding.EncodeToString([]byte(testMCPToken + "\n")),
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	// A cluster whose API server denies the PlatformAgent list (expired credential, RBAC):
	// it must surface as a probe failure, not as "no deployments".
	forbiddenSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","message":"platformagents.kubeagents.x-k8s.io is forbidden","reason":"Forbidden","code":403}`))
	}))
	defer forbiddenSrv.Close()

	// A reachable cluster without the kube-agents CRDs: legitimately no deployments.
	noCRDSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.NotFound(w, r)
	}))
	defer noCRDSrv.Close()

	kubeconfigYAML := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: reachable
  cluster:
    server: %s
- name: unreachable
  cluster:
    server: http://127.0.0.1:1
- name: forbidden
  cluster:
    server: %s
- name: nocrd
  cluster:
    server: %s
contexts:
- name: %s
  context:
    cluster: reachable
- name: %s
  context:
    cluster: unreachable
- name: %s
  context:
    cluster: forbidden
- name: %s
  context:
    cluster: nocrd
current-context: %s
`, srv.URL, forbiddenSrv.URL, noCRDSrv.URL, testKubeContext, testUnreachableCtx, testForbiddenCtx, testNoCRDCtx, testKubeContext)

	kcPath := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(kcPath, []byte(kubeconfigYAML), 0o600); err != nil {
		t.Fatalf("WriteFile kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", kcPath)

	entries, err := DiscoverDeployments(context.Background(), "")
	if err != nil {
		t.Fatalf("DiscoverDeployments: %v", err)
	}
	if got := secretHits.Load(); got != 0 {
		t.Fatalf("DiscoverDeployments read %d Secrets during discovery, want 0", got)
	}
	var profiles []mcpbridge.DeploymentProfile
	failures := make(map[string]string)
	for _, e := range entries {
		if e.IsContextProbeFailure() {
			failures[e.KubeContext] = e.StatusMessage
			continue
		}
		profiles = append(profiles, e)
	}
	if len(profiles) != 1 {
		t.Fatalf("got %d profiles, want 1 (entries: %+v)", len(profiles), entries)
	}
	p := profiles[0]
	if p.Name != testClusterName || !p.Ready || p.GitHubRepo != testGitHubRepo || p.Version != "0fe1c8ce" || p.ChatPlatform != mcpbridge.ChatPlatformGChat {
		t.Errorf("unexpected profile metadata: %+v", p)
	}
	if p.MCPService != testPAName+mcpbridge.MCPServiceSuffix || p.MCPPort != mcpbridge.DefaultMCPPort || p.TokenSecretName != testPAName+mcpbridge.MCPTokenSecretSuffix {
		t.Errorf("unexpected MCP target metadata: service=%q port=%d secret=%q", p.MCPService, p.MCPPort, p.TokenSecretName)
	}
	if len(p.AgentProfiles) != 1 || p.AgentProfiles[0] != "default" {
		t.Errorf("expected only interactive AgentProfile 'default', got %v", p.AgentProfiles)
	}
	// KUBE_AGENTS_MCP_TOKEN is the bridge's to apply (mcpbridge.EnvMCPTokenFor), never read
	// here, so the Secret is read whatever the env holds.
	t.Setenv(mcpbridge.EnvMCPToken, "token-from-env")
	if err := PopulateDeploymentCredentials(context.Background(), &p); err != nil {
		t.Fatalf("PopulateDeploymentCredentials: %v", err)
	}
	if p.MCPToken != testMCPToken {
		t.Errorf("MCPToken = %q, want %q", p.MCPToken, testMCPToken)
	}
	if got := secretHits.Load(); got != 1 {
		t.Errorf("secretHits = %d after PopulateDeploymentCredentials, want 1", got)
	}
	if len(failures) != 2 {
		t.Fatalf("got probe failures %v, want exactly the unreachable and forbidden contexts", failures)
	}
	if msg := failures[testUnreachableCtx]; msg == "" {
		t.Errorf("expected a probe failure for the unreachable context, got %v", failures)
	}
	if msg := failures[testForbiddenCtx]; !strings.Contains(msg, "forbidden") {
		t.Errorf("expected the forbidden context's failure to carry the API server's reason, got %q", msg)
	}
	if _, ok := failures[testNoCRDCtx]; ok {
		t.Errorf("a cluster without the kube-agents CRDs must read as no deployments, not as a probe failure: %v", failures)
	}

	// With only the forbidden context in scope nothing could be scanned, so the error names it.
	entries, err = DiscoverDeployments(context.Background(), testForbiddenCtx)
	if err == nil || !strings.Contains(err.Error(), testForbiddenCtx) {
		t.Fatalf("expected an error naming the only (unscannable) context, got err=%v entries=%+v", err, entries)
	}
	if len(entries) != 1 || !entries[0].IsContextProbeFailure() {
		t.Fatalf("expected exactly one probe failure entry, got %+v", entries)
	}
}

func TestResolveServiceTargetPodPrefersReadyPod(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case fmt.Sprintf("/api/v1/namespaces/%s/services/%s%s", testNamespace, testPAName, mcpbridge.MCPServiceSuffix):
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "Service",
				"metadata":   map[string]any{"name": testPAName + mcpbridge.MCPServiceSuffix, "namespace": testNamespace},
				"spec": map[string]any{
					"selector": map[string]string{"app": "a2a-mcp"},
				},
			})
		case fmt.Sprintf("/api/v1/namespaces/%s/pods", testNamespace):
			if r.URL.Query().Get("labelSelector") != "app=a2a-mcp" {
				_ = json.NewEncoder(w).Encode(map[string]any{"apiVersion": "v1", "kind": "PodList", "items": []any{}})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"apiVersion": "v1",
				"kind":       "PodList",
				"items": []map[string]any{
					{
						"metadata": map[string]any{"name": "mcp-0-not-ready", "namespace": testNamespace},
						"status": map[string]any{
							"phase": "Running",
							"conditions": []map[string]any{
								{"type": "Ready", "status": "False"},
							},
						},
					},
					{
						"metadata": map[string]any{"name": "mcp-1-ready", "namespace": testNamespace},
						"status": map[string]any{
							"phase": "Running",
							"conditions": []map[string]any{
								{"type": "Ready", "status": "True"},
							},
						},
					},
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	kubeconfigYAML := fmt.Sprintf(`apiVersion: v1
kind: Config
clusters:
- name: reachable
  cluster:
    server: %s
contexts:
- name: %s
  context:
    cluster: reachable
current-context: %s
`, srv.URL, testKubeContext, testKubeContext)

	kcPath := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(kcPath, []byte(kubeconfigYAML), 0o600); err != nil {
		t.Fatalf("WriteFile kubeconfig: %v", err)
	}
	t.Setenv("KUBECONFIG", kcPath)

	restCfg, err := restConfigForKubeContext(testKubeContext)
	if err != nil {
		t.Fatalf("restConfigForKubeContext: %v", err)
	}
	clientset, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		t.Fatalf("kubernetes.NewForConfig: %v", err)
	}
	podName, targetPort, err := resolveServiceTargetPod(context.Background(), clientset, testNamespace, testPAName+mcpbridge.MCPServiceSuffix, mcpbridge.DefaultMCPPort)
	if err != nil || podName != "mcp-1-ready" || targetPort != mcpbridge.DefaultMCPPort {
		t.Fatalf("resolveServiceTargetPod = (%q, %d, %v), want ('mcp-1-ready', %d)", podName, targetPort, err, mcpbridge.DefaultMCPPort)
	}

	// With neither the Service nor any pod found, the reason is returned instead of a guessed
	// StatefulSet-style pod name.
	if name, _, err := resolveServiceTargetPod(context.Background(), clientset, testNamespace, "missing", mcpbridge.DefaultMCPPort); err == nil ||
		!strings.Contains(err.Error(), "get service") {
		t.Fatalf("resolveServiceTargetPod(missing) = %q, %v; want an error naming the failed lookups", name, err)
	}
}
