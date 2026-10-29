package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"
)

// envTokenGateway records the Authorization header of every /mcp call.
func envTokenGateway(t *testing.T) (*httptest.Server, <-chan string) {
	t.Helper()
	auth := make(chan string, 16)
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != remoteMCPPath {
			http.NotFound(w, r)
			return
		}
		auth <- r.Header.Get("Authorization")
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		w.Header().Set("Content-Type", contentTypeJSON)
		_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: "ok"}},
		}))
	}))
	t.Cleanup(gw.Close)
	return gw, auth
}

// envTokenServer saves one direct-URL deployment per name, all pointing at gwURL.
func envTokenServer(t *testing.T, gwURL string, names ...string) *Server {
	t.Helper()
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	var profs []DeploymentProfile
	for _, n := range names {
		profs = append(profs, DeploymentProfile{Name: n, Namespace: testNamespace, PlatformAgentName: testPAName, DirectMCPURL: gwURL, Ready: true})
	}
	if err := store.UpsertDeployments(profs); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	return srv
}

// connectAuth connects srv to dep and returns the Authorization header the gateway saw.
func connectAuth(t *testing.T, srv *Server, auth <-chan string, dep string) string {
	t.Helper()
	res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(`{"deployment":"`+dep+`"}`))
	if res.IsError {
		t.Fatalf("connect_deployment %s: %+v", dep, res)
	}
	select {
	case got := <-auth:
		return got
	case <-time.After(stdioTestWait):
		t.Fatalf("connect_deployment %s made no gateway call", dep)
	}
	return ""
}

const envTokenValue = "token-from-env"

func TestEnvMCPTokenGoesToTheOnlyDeploymentWithoutASelector(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := envTokenServer(t, gw.URL, "cluster-a")
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "")
	if got := connectAuth(t, srv, auth, "cluster-a"); got != "Bearer "+envTokenValue {
		t.Fatalf("the only deployment got Authorization %q; want the env token", got)
	}
}

func TestEnvMCPTokenGoesNowhereWithSeveralDeploymentsAndNoSelector(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := envTokenServer(t, gw.URL, "cluster-a", "cluster-b")
	t.Setenv(EnvMCPToken, envTokenValue)
	for _, sel := range []string{"", "cluster-c"} {
		t.Setenv(EnvMCPTokenDeployment, sel)
		for _, dep := range []string{"cluster-a", "cluster-b"} {
			if got := connectAuth(t, srv, auth, dep); got != "" {
				t.Fatalf("selector %q: %s got Authorization %q; want none", sel, dep, got)
			}
		}
	}
}

func TestEnvMCPTokenGoesOnlyToTheSelectedDeployment(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := envTokenServer(t, gw.URL, "cluster-a", "cluster-b")
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "cluster-a")
	if got := connectAuth(t, srv, auth, "cluster-b"); got != "" {
		t.Fatalf("cluster-a's token reached cluster-b as %q", got)
	}
	if got := connectAuth(t, srv, auth, "cluster-a"); got != "Bearer "+envTokenValue {
		t.Fatalf("cluster-a got Authorization %q; want the env token", got)
	}
}
