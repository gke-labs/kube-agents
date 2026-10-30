package mcpbridge

import (
	"context"
	"encoding/json"
	"reflect"
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
