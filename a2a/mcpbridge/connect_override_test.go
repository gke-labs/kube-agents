package mcpbridge

import (
	"context"
	"encoding/json"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// dropSession closes srv's session, as a dead port-forward would, so the next call reconnects.
func dropSession(t *testing.T, srv *Server) {
	t.Helper()
	srv.mu.Lock()
	sess := srv.session
	srv.mu.Unlock()
	if sess == nil {
		t.Fatal("no session to drop")
	}
	sess.Close()
}

// reconnectAuth makes a call that reconnects srv and returns the Authorization header the
// gateway saw, which names the target the reconnect went to (secretTokenFor).
func reconnectAuth(t *testing.T, srv *Server, auth <-chan string) string {
	t.Helper()
	res := srv.CallTool(context.Background(), ToolConnectionStatus, json.RawMessage(`{}`))
	if res.IsError {
		t.Fatalf("connection_status: %+v", res)
	}
	select {
	case got := <-auth:
		return got
	case <-time.After(stdioTestWait):
		t.Fatal("connection_status made no gateway call")
	}
	return ""
}

func overrideTestServer(t *testing.T) (*Server, <-chan string) {
	t.Helper()
	gw, auth := envTokenGateway(t)
	srv := kubeTokenServer(t, gw.URL, []DeploymentProfile{
		{Name: "cluster-a", KubeContext: "ctx-a", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: "cluster-b", KubeContext: "ctx-b", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
	})
	t.Setenv(EnvMCPToken, "")
	return srv, auth
}

func TestConnectDeploymentOverrideIsNotSaved(t *testing.T) {
	srv, auth := overrideTestServer(t)
	connectArgsAuth(t, srv, auth, `{"deployment":"cluster-b"}`)
	before := srv.Store().Snapshot()
	if before.ActiveDeployment != "cluster-b" {
		t.Fatalf("setup: active deployment %q; want cluster-b", before.ActiveDeployment)
	}

	for _, args := range []string{
		`{"deployment":"cluster-a","kube_context":"ctx-x","namespace":"ns-y"}`,
		`{"deployment":"cluster-a","namespace":"ns-y"}`,
		`{"deployment":"cluster-a","kube_context":"ctx-x"}`,
	} {
		connectArgsAuth(t, srv, auth, args)
		after := srv.Store().Snapshot()
		if after.ActiveDeployment != before.ActiveDeployment {
			t.Errorf("%s: active deployment became %q; want it left at %q", args, after.ActiveDeployment, before.ActiveDeployment)
		}
		if !reflect.DeepEqual(after.Deployments, before.Deployments) {
			t.Errorf("%s: saved deployments changed\nbefore: %+v\nafter:  %+v", args, before.Deployments, after.Deployments)
		}
	}
}

func TestReconnectAfterAnOverrideKeepsTheOverriddenTarget(t *testing.T) {
	srv, auth := overrideTestServer(t)
	connectArgsAuth(t, srv, auth, `{"deployment":"cluster-b"}`)
	connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a","kube_context":"ctx-x","namespace":"ns-y"}`)

	dropSession(t, srv)
	if got, want := reconnectAuth(t, srv, auth), "Bearer "+secretTokenFor("ctx-x", "ns-y"); got != want {
		t.Fatalf("reconnect after an override sent %q; want the overridden target's %q", got, want)
	}

	// The keep-alive reconnect goes to the same target.
	dropSession(t, srv)
	srv.ensureConnectedAsync()
	srv.connectMu.Lock()
	srv.connectMu.Unlock() //nolint:staticcheck // waits for the async reconnect to finish
	if got, want := reconnectAuth(t, srv, auth), "Bearer "+secretTokenFor("ctx-x", "ns-y"); got != want {
		t.Fatalf("keep-alive reconnect after an override reached %q; want the overridden target's %q", got, want)
	}
}

func TestConnectWithoutAnOverrideClearsIt(t *testing.T) {
	srv, auth := overrideTestServer(t)
	connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a","kube_context":"ctx-x","namespace":"ns-y"}`)
	connectArgsAuth(t, srv, auth, `{"deployment":"cluster-b"}`)

	dropSession(t, srv)
	if got, want := reconnectAuth(t, srv, auth), "Bearer "+secretTokenFor("ctx-b", testNamespace); got != want {
		t.Fatalf("reconnect after a plain connect sent %q; want the saved target's %q", got, want)
	}
}

// listingClusterAccess is secretClusterAccess over a kubeconfig holding contexts.
type listingClusterAccess struct {
	secretClusterAccess
	contexts []string
}

func (c listingClusterAccess) KubeContexts() ([]string, error) { return c.contexts, nil }

func TestConnectDeploymentResolvesAPartialKubeContextOverride(t *testing.T) {
	gw, auth := envTokenGateway(t)
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{
		{Name: "cluster-a", KubeContext: "ctx-prod", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: listingClusterAccess{
		secretClusterAccess: secretClusterAccess{gwURL: gw.URL},
		contexts:            []string{"ctx-prod", "gke_proj_us_staging-1", "gke_proj_eu_dev-1", "gke_proj_eu_dev-2"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	t.Setenv(EnvMCPToken, "")

	// "STAG" names one kubeconfig context, case-insensitively, as discovery's filter would.
	got := connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a","kube_context":"STAG"}`)
	if want := "Bearer " + secretTokenFor("gke_proj_us_staging-1", testNamespace); got != want {
		t.Fatalf("a partial kube_context override connected with Authorization %q; want the resolved context's %q", got, want)
	}
	for args, want := range map[string]string{
		`{"deployment":"cluster-a","kube_context":"dev"}`:     "matches more than one kubeconfig context",
		`{"deployment":"cluster-a","kube_context":"nowhere"}`: "matches no kubeconfig context",
	} {
		res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(args))
		if !res.IsError || !strings.Contains(res.Content[0].Text, want) {
			t.Fatalf("connect_deployment %s: %+v; want it refused (%s)", args, res, want)
		}
	}
	// The override stays one-off: nothing saved, cluster-a still points at ctx-prod.
	if p := store.Snapshot().Deployments["cluster-a"]; p.KubeContext != "ctx-prod" {
		t.Fatalf("cluster-a saved with kube context %q; want ctx-prod", p.KubeContext)
	}
}
