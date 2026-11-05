package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sort"
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

// tokenRecipients returns, sorted, the saved keys EnvMCPTokenFor hands the env token to.
func tokenRecipients(saved map[string]DeploymentProfile) []string {
	got := []string{}
	for key, p := range saved {
		if EnvMCPTokenFor(p, saved) != "" {
			got = append(got, key)
		}
	}
	sort.Strings(got)
	return got
}

// wantRecipients asserts that, with the selector set to sel, exactly the keys in want get the
// env token.
func wantRecipients(t *testing.T, saved map[string]DeploymentProfile, sel string, want ...string) {
	t.Helper()
	t.Setenv(EnvMCPTokenDeployment, sel)
	if want == nil {
		want = []string{}
	}
	sort.Strings(want)
	if got := tokenRecipients(saved); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("selector %q reached %v of %v; want %v", sel, got, keysOf(saved), want)
	}
}

// The selector is a saved key, matched exactly: the install that kept the bare key answers to
// it, and the one a collision qualified answers only to its qualified key. "<context>/<name>"
// for the bare-key install is no saved key and names nothing.
func TestEnvMCPTokenSelectorMatchesOnlyTheExactSavedKey(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	bare := DeploymentProfile{Name: "cluster-a", KubeContext: "ctx-1", Namespace: testNamespace}
	qualified := DeploymentProfile{Name: "ctx-2/cluster-a", KubeContext: "ctx-2", Namespace: testNamespace}
	saved := map[string]DeploymentProfile{bare.Name: bare, qualified.Name: qualified}
	wantRecipients(t, saved, "cluster-a", "cluster-a")
	wantRecipients(t, saved, "ctx-2/cluster-a", "ctx-2/cluster-a")
	wantRecipients(t, saved, "ctx-1/cluster-a")
	wantRecipients(t, saved, "CLUSTER-A")
	wantRecipients(t, saved, "cluster")
}

// A key that holds a slash of its own (discovery's "<namespace>/<alias>") is matched like any
// other: as a whole, never split at the slash.
func TestEnvMCPTokenSlashInAKeyIsMatchedWhole(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	bare := DeploymentProfile{Name: "ns-a/pa", KubeContext: "ctx-1", Namespace: "ns-a"}
	qualified := DeploymentProfile{Name: "ctx-2/ns-a/pa", KubeContext: "ctx-2", Namespace: "ns-a"}
	plain := DeploymentProfile{Name: "pa", KubeContext: "ctx-3", Namespace: testNamespace}
	saved := map[string]DeploymentProfile{bare.Name: bare, qualified.Name: qualified, plain.Name: plain}
	wantRecipients(t, saved, "ns-a/pa", "ns-a/pa")
	wantRecipients(t, saved, "ctx-2/ns-a/pa", "ctx-2/ns-a/pa")
	wantRecipients(t, saved, "pa", "pa")
	wantRecipients(t, saved, "ctx-1/ns-a/pa")
	wantRecipients(t, saved, "ctx-3/pa")
	wantRecipients(t, saved, "x/pa")
}

// A kube context whose name holds slashes (an EKS ARN) gives the selector no second spelling.
func TestEnvMCPTokenContextWithSlashesGivesNoSecondSpelling(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	eks := "arn:aws:eks:us-east-1:111122223333:cluster/prod"
	bare := DeploymentProfile{Name: "prod", KubeContext: eks, Namespace: testNamespace}
	qualified := DeploymentProfile{Name: "ctx-2/prod", KubeContext: "ctx-2", Namespace: testNamespace}
	saved := map[string]DeploymentProfile{bare.Name: bare, qualified.Name: qualified}
	wantRecipients(t, saved, "prod", "prod")
	wantRecipients(t, saved, eks+"/prod")
}

// When one profile's "<context>/<key>" is literally another profile's saved key, the selector
// names the profile saved under that key and no other; a string no profile is saved under names
// nothing, however many profiles could spell it.
func TestEnvMCPTokenSelectorSpelledByAnotherProfileNamesOnlyTheKeyHolder(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	a := DeploymentProfile{Name: "pa", KubeContext: "ctx-1/ns-a", Namespace: testNamespace}
	b := DeploymentProfile{Name: "ctx-1/ns-a/pa", KubeContext: "ctx-1", Namespace: "ns-a"}
	saved := map[string]DeploymentProfile{a.Name: a, b.Name: b}
	wantRecipients(t, saved, "ctx-1/ns-a/pa", "ctx-1/ns-a/pa")
	wantRecipients(t, saved, "pa", "pa")

	nsAlias := DeploymentProfile{Name: "b/c", KubeContext: "a", Namespace: "b"}
	slashCtx := DeploymentProfile{Name: "c", KubeContext: "a/b", Namespace: testNamespace}
	saved = map[string]DeploymentProfile{nsAlias.Name: nsAlias, slashCtx.Name: slashCtx}
	wantRecipients(t, saved, "a/b/c")
}

// For any saved set and any selector, KUBE_AGENTS_MCP_TOKEN reaches exactly {sel} when sel is a
// saved key and nothing otherwise; unset, it reaches the only saved key, or nothing with several.
// The expected set is computed from the saved keys alone, not from the implementation. Checked
// over saved sets of one to three installs mixing bare, namespace-qualified and
// context-qualified keys, with contexts that hold slashes of their own, and over selectors that
// are keys, base names, "<context>/<key>" and "<context>/<base name>" spellings, and prefixes
// or suffixes cut at a slash.
func TestEnvMCPTokenReachesExactlyTheSavedKeyItNames(t *testing.T) {
	t.Setenv(EnvMCPToken, envTokenValue)
	contexts := []string{"ctx-1", "ctx-1/ns-a", "arn:aws:eks:us-east-1:111122223333:cluster/prod", "default/api-example-com:6443/admin"}
	names := []string{"prod", "ns-a/pa", "pa"}
	type install struct{ ctx, name string }
	var all []install
	for _, c := range contexts {
		for _, n := range names {
			all = append(all, install{c, n})
		}
	}
	sets, checks, hits := 0, 0, 0
	for a := 0; a < len(all); a++ {
		for b := a; b < len(all); b++ {
			for c := b; c < len(all); c++ {
				// a == b == c is a one-install set, a < b == c a two-install set.
				picked := []install{all[a]}
				if b != a {
					picked = append(picked, all[b])
				}
				if c != b {
					picked = append(picked, all[c])
				}
				for mask := 0; mask < 1<<len(picked); mask++ {
					saved := map[string]DeploymentProfile{}
					valid := true
					for i, in := range picked {
						key := in.name
						if mask&(1<<i) != 0 {
							key = in.ctx + "/" + in.name
						}
						if _, dup := saved[key]; dup {
							valid = false
							break
						}
						saved[key] = DeploymentProfile{Name: key, KubeContext: in.ctx, Namespace: testNamespace}
					}
					if !valid {
						continue
					}
					sets++
					selectors := map[string]bool{"": true, "nope": true}
					for _, in := range picked {
						for _, s := range []string{in.name, in.ctx + "/" + in.name, in.ctx + "/" + in.ctx + "/" + in.name} {
							selectors[s] = true
							for i := range s {
								if s[i] == '/' {
									selectors[s[:i]] = true
									selectors[s[i+1:]] = true
								}
							}
						}
					}
					for sel := range selectors {
						want := []string{}
						if sel == "" {
							if len(saved) == 1 {
								want = keysOf(saved)
							}
						} else if _, ok := saved[sel]; ok {
							want = []string{sel}
						}
						t.Setenv(EnvMCPTokenDeployment, sel)
						got := tokenRecipients(saved)
						checks++
						if len(got) > 0 {
							hits++
						}
						if fmt.Sprint(got) != fmt.Sprint(want) {
							t.Fatalf("selector %q reached %v of %v; want %v", sel, got, keysOf(saved), want)
						}
					}
				}
			}
		}
	}
	if sets < 200 || hits < 200 || checks-hits < 200 {
		t.Fatalf("%d saved sets, %d selectors, %d reaching a deployment: the generator is not covering the space", sets, checks, hits)
	}
}

func keysOf(saved map[string]DeploymentProfile) []string {
	out := make([]string, 0, len(saved))
	for k := range saved {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
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

// A token bound to one "prod" never moves to another "prod": a same-named install in a second
// context renames both to "<context>/prod", and the selector "prod" then matches no saved key; it
// still matches none once the first is pruned and the second is the only one whose base name is
// "prod".
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
	// Neither the bare name nor the pruned install's qualified key matches what is left.
	t.Setenv(EnvMCPTokenDeployment, "ctx-1/prod")
	ownSecret("after the prune, selector ctx-1/prod", "ctx-2/prod", "ctx-2")
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
