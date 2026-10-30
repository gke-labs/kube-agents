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

func TestEnvMCPTokenSelectorDecidesEvenWithOneDeploymentSaved(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := envTokenServer(t, gw.URL, "staging")
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "prod")
	if got := connectAuth(t, srv, auth, "staging"); got != "" {
		t.Fatalf("prod's token reached the only saved deployment, staging, as %q", got)
	}
	t.Setenv(EnvMCPTokenDeployment, "staging")
	if got := connectAuth(t, srv, auth, "staging"); got != "Bearer "+envTokenValue {
		t.Fatalf("staging, named by the selector, got Authorization %q; want the env token", got)
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

// secretClusterAccess serves every kube profile through gwURL and fills its token from a fake
// Secret named after the profile's target.
type secretClusterAccess struct {
	noClusterAccess
	gwURL string
}

func secretTokenFor(kubeContext, namespace string) string {
	return "secret-" + kubeContext + "-" + namespace
}

func (c secretClusterAccess) PopulateDeploymentCredentials(_ context.Context, prof *DeploymentProfile) error {
	prof.MCPToken = secretTokenFor(prof.KubeContext, prof.Namespace)
	return nil
}

func (c secretClusterAccess) StartGatewayPortForward(context.Context, DeploymentProfile) (Tunnel, error) {
	return fixedTunnel(c.gwURL), nil
}

type fixedTunnel string

func (t fixedTunnel) MCPURL() string { return string(t) }
func (fixedTunnel) Restart()         {}
func (fixedTunnel) Close()           {}

// kubeTokenServer saves profs (kube profiles, no direct URL) and serves them through gwURL.
func kubeTokenServer(t *testing.T, gwURL string, batches ...[]DeploymentProfile) *Server {
	t.Helper()
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, b := range batches {
		if err := store.UpsertDeployments(b); err != nil {
			t.Fatal(err)
		}
	}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: secretClusterAccess{gwURL: gwURL}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	return srv
}

func connectArgsAuth(t *testing.T, srv *Server, auth <-chan string, args string) string {
	t.Helper()
	res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(args))
	if res.IsError {
		t.Fatalf("connect_deployment %s: %+v", args, res)
	}
	select {
	case got := <-auth:
		return got
	case <-time.After(stdioTestWait):
		t.Fatalf("connect_deployment %s made no gateway call", args)
	}
	return ""
}

func TestEnvMCPTokenIsNotSentWhenTheCallOverridesTheDirectTarget(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := envTokenServer(t, gw.URL, "cluster-a")
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "")
	if got := connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a","namespace":"other-ns"}`); got != "" {
		t.Fatalf("a namespace override got Authorization %q; want none", got)
	}
}

func TestEnvMCPTokenIsNotSentWhenTheCallOverridesTheKubeTarget(t *testing.T) {
	gw, auth := envTokenGateway(t)
	srv := kubeTokenServer(t, gw.URL, []DeploymentProfile{{Name: "cluster-a", KubeContext: "ctx-a", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true}})
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "")
	if got := connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a"}`); got != "Bearer "+envTokenValue {
		t.Fatalf("the saved target got Authorization %q; want the env token", got)
	}
	got := connectArgsAuth(t, srv, auth, `{"deployment":"cluster-a","kube_context":"ctx-x","namespace":"ns-y"}`)
	if want := "Bearer " + secretTokenFor("ctx-x", "ns-y"); got != want {
		t.Fatalf("an overridden target got Authorization %q; want its own Secret's %q", got, want)
	}
}

func TestEnvMCPTokenBareSelectorSharedByAnotherKeyIsAmbiguous(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	// The first install kept the bare key; a second install of the same name in another context
	// was saved as "<context>/<name>".
	bare := DeploymentProfile{Name: "cluster-a", KubeContext: "ctx-1", Namespace: testNamespace}
	qualified := DeploymentProfile{Name: "ctx-2/cluster-a", KubeContext: "ctx-2", Namespace: testNamespace}
	saved := map[string]DeploymentProfile{bare.Name: bare, qualified.Name: qualified}

	// A bare name another saved key shares as its base name never settles who gets the token.
	t.Setenv(EnvMCPTokenDeployment, "cluster-a")
	if got := EnvMCPTokenFor(bare, saved); got != "" {
		t.Fatalf("an ambiguous bare selector reached the bare-key install as %q; want none", got)
	}
	if got := EnvMCPTokenFor(qualified, saved); got != "" {
		t.Fatalf("an ambiguous bare selector reached ctx-2/cluster-a as %q; want none", got)
	}
	// The qualified form names one install, including the one that kept the bare key.
	t.Setenv(EnvMCPTokenDeployment, "ctx-1/cluster-a")
	if got := EnvMCPTokenFor(bare, saved); got != envTokenValue {
		t.Fatalf("ctx-1/cluster-a: the bare-key install got %q; want the env token", got)
	}
	if got := EnvMCPTokenFor(qualified, saved); got != "" {
		t.Fatalf("ctx-1/cluster-a: ctx-2/cluster-a got %q; want none", got)
	}
	t.Setenv(EnvMCPTokenDeployment, "ctx-2/cluster-a")
	if got := EnvMCPTokenFor(qualified, saved); got != envTokenValue {
		t.Fatalf("ctx-2/cluster-a: its install got %q; want the env token", got)
	}
	if got := EnvMCPTokenFor(bare, saved); got != "" {
		t.Fatalf("ctx-2/cluster-a: the bare-key install got %q; want none", got)
	}
	// With no namesake, a bare selector still names its install.
	delete(saved, qualified.Name)
	t.Setenv(EnvMCPTokenDeployment, "cluster-a")
	if got := EnvMCPTokenFor(bare, saved); got != envTokenValue {
		t.Fatalf("a bare selector with no namesake got %q; want the env token", got)
	}
}

func TestEnvMCPTokenSelectorDoesNotMatchARenamedProfileByItsBaseName(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	renamed := DeploymentProfile{Name: "ctx-1/cluster-a", KubeContext: "ctx-1", Namespace: testNamespace}
	other := DeploymentProfile{Name: "cluster-b", KubeContext: "ctx-2", Namespace: testNamespace}
	saved := map[string]DeploymentProfile{renamed.Name: renamed, other.Name: other}
	t.Setenv(EnvMCPTokenDeployment, "cluster-a")
	if got := EnvMCPTokenFor(renamed, saved); got != "" {
		t.Fatalf("the bare name reached the renamed profile as %q; want none", got)
	}
	t.Setenv(EnvMCPTokenDeployment, "ctx-1/cluster-a")
	if got := EnvMCPTokenFor(renamed, saved); got != envTokenValue {
		t.Fatalf("the qualified key: renamed profile got %q; want the env token", got)
	}
	if got := EnvMCPTokenFor(other, saved); got != "" {
		t.Fatalf("the other profile got %q; want none", got)
	}
}

// A token bound to one "prod" never moves to another "prod": not when a same-named install in a
// second context renames both to "<context>/prod", and not when the first is then pruned and the
// second is the only one whose base name is "prod".
func TestEnvMCPTokenNeverMovesOnARenameOrAPrune(t *testing.T) {
	gw, auth := envTokenGateway(t)
	prod1 := DeploymentProfile{Name: "prod", KubeContext: "ctx-1", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true}
	prod2 := DeploymentProfile{Name: "prod", KubeContext: "ctx-2", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true}
	srv := kubeTokenServer(t, gw.URL, []DeploymentProfile{prod1})
	t.Setenv(EnvMCPToken, envTokenValue)
	t.Setenv(EnvMCPTokenDeployment, "prod")
	if got := connectArgsAuth(t, srv, auth, `{"deployment":"prod"}`); got != "Bearer "+envTokenValue {
		t.Fatalf("setup: prod got Authorization %q; want the env token", got)
	}
	ownSecret := func(step, dep, kubeContext string) {
		t.Helper()
		got := connectArgsAuth(t, srv, auth, `{"deployment":"`+dep+`"}`)
		if want := "Bearer " + secretTokenFor(kubeContext, testNamespace); got != want {
			t.Fatalf("%s: %s got Authorization %q; want its own Secret's %q", step, dep, got, want)
		}
	}
	// One scan finds both: both are saved qualified, and the bare selector names neither.
	if err := srv.Store().SyncDiscoveredDeployments([]DeploymentProfile{prod1, prod2}); err != nil {
		t.Fatal(err)
	}
	if d := srv.Store().Snapshot().Deployments; len(d) != 2 || d["ctx-1/prod"].Name == "" || d["ctx-2/prod"].Name == "" {
		t.Fatalf("setup: want ctx-1/prod and ctx-2/prod saved, have %v", d)
	}
	ownSecret("after the rename", "ctx-1/prod", "ctx-1")
	ownSecret("after the rename", "ctx-2/prod", "ctx-2")
	// ctx-1 leaves the kubeconfig: the next full scan prunes ctx-1/prod.
	if err := srv.Store().SyncDiscoveredDeployments([]DeploymentProfile{prod2}); err != nil {
		t.Fatal(err)
	}
	if d := srv.Store().Snapshot().Deployments; len(d) != 1 || d["ctx-2/prod"].Name == "" {
		t.Fatalf("setup: want only ctx-2/prod saved, have %v", d)
	}
	ownSecret("after the prune", "ctx-2/prod", "ctx-2")
}

func TestConnectDeploymentKeepsTheFullContextAPartialKubeContextMatched(t *testing.T) {
	gw, auth := envTokenGateway(t)
	const full = "gke_example-project_us-central1_prod-us-central1"
	srv := kubeTokenServer(t, gw.URL, []DeploymentProfile{{Name: "prod-us-central1", KubeContext: full, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true}})
	t.Setenv(EnvMCPToken, "")
	got := connectArgsAuth(t, srv, auth, `{"deployment":"prod-us-central1","kube_context":"PROD"}`)
	if want := "Bearer " + secretTokenFor(full, testNamespace); got != want {
		t.Fatalf("a partial kube_context connected with Authorization %q; want the full context's %q", got, want)
	}
}
