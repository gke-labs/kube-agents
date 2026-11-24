package workeradapter

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"reflect"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

var scTargets = &lib.DelegateTargets{Targets: []lib.DelegateTarget{
	{Name: "cluster", Description: "works on one cluster", Parameters: []lib.ProfileParameter{
		{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}},
}, WidthMax: 3}

// toolWithScope lists the tools over a fresh server given a page, and
// returns the delegate tool's own description and its schema as JSON.
func toolWithScope(t *testing.T, targets *lib.DelegateTargets, scope *lib.ScopeClusters) (string, string) {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = ServeDelegateMCPWith(ctx, inR, outW, "/nonexistent.sock", targets, scope, slog.Default()) }()
	list := rpc(t, inW, bufio.NewReader(outR), `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`)
	tool := list["result"].(map[string]any)["tools"].([]any)[0].(map[string]any)
	schema, err := json.Marshal(tool["inputSchema"])
	if err != nil {
		t.Fatal(err)
	}
	return tool["description"].(string), string(schema)
}

// The page is named once, at the end of the tool's description: the count
// of clusters with a published listing, each cluster on the page, how many
// are off it, the oldest listing, the projects listed elsewhere, and what
// the page was checked against. The input schema, and so every batch entry,
// is the one without a page.
func TestDelegateMCPNamesTheScopeClustersPage(t *testing.T) {
	page := &lib.ScopeClusters{Count: 4, Truncated: true, Clusters: []lib.ScopeCluster{
		{Project: "member-a", Location: "europe-west1", Cluster: "a3"},
		{Project: "member-a", Location: "us-central1", Cluster: "a1"},
		{Project: "member-a", Location: "us-central1", Cluster: "a2"},
	}, UnlistedProjects: []string{"explicit-a", "mgmt-proj"}, UnlistedCount: 3, OldestListing: "2026-11-23T10:00:00Z"}
	baseDesc, baseSchema := toolWithScope(t, scTargets, nil)
	cases := []struct {
		name string
		page *lib.ScopeClusters
		want string
	}{
		{"page", page, " Clusters this task may delegate to that have a published listing: 4." +
			" On this page (project/location/cluster): member-a/europe-west1/a3, member-a/us-central1/a1, member-a/us-central1/a2." +
			" Not on this page: 1. Oldest cluster listing read at 2026-11-23T10:00:00Z." +
			" Projects in scope whose clusters are listed elsewhere: explicit-a, mgmt-proj, and 1 more." +
			" These clusters pass the install's scope check; a profile's tiers and pins are checked when a task is delegated."},
		{"unknown", &lib.ScopeClusters{Unknown: true, Truncated: true, Clusters: []lib.ScopeCluster{},
			UnlistedProjects: []string{"explicit-a"}, UnlistedCount: 1},
			" Clusters this task may delegate to that have a published listing: unknown, as the gateway has no full listing; 0 known." +
				" Projects in scope whose clusters are listed elsewhere: explicit-a."},
		{"unbounded", &lib.ScopeClusters{Unbounded: true, Unknown: true, Truncated: true, Count: 1,
			Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}, OldestListing: "2026-11-23T10:00:00Z"},
			" Clusters this task may delegate to: unknown, as the install's scope draws no boundary; 1 with a published listing are known." +
				" On this page (project/location/cluster): p/l/c. Oldest cluster listing read at 2026-11-23T10:00:00Z." +
				" These clusters pass the install's scope check; a profile's tiers and pins are checked when a task is delegated."},
		{"age unknown", &lib.ScopeClusters{Count: 1, Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}},
			" Clusters this task may delegate to that have a published listing: 1." +
				" On this page (project/location/cluster): p/l/c. The listing's age is unknown." +
				" These clusters pass the install's scope check; a profile's tiers and pins are checked when a task is delegated."},
		{"parent scoped", &lib.ScopeClusters{ParentScoped: true, Count: 1, Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}},
			" This task is scoped to one cluster: p/l/c." +
				" These clusters pass the install's scope check; a profile's tiers and pins are checked when a task is delegated."},
		{"parent scoped, out of scope", &lib.ScopeClusters{ParentScoped: true, Clusters: []lib.ScopeCluster{}},
			" This task is scoped to one cluster, which the install's scope does not admit."},
	}
	for _, tc := range cases {
		desc, schema := toolWithScope(t, scTargets, tc.page)
		if got := strings.TrimPrefix(desc, baseDesc); !strings.HasPrefix(desc, baseDesc) || got != tc.want {
			t.Errorf("%s: the page reads\n%q\nwant\n%q", tc.name, got, tc.want)
		}
		if schema != baseSchema {
			t.Errorf("%s: the input schema moved with the page", tc.name)
		}
	}

	// No page, no agents, or no agent that takes a cluster: no page named.
	pinnedOnly := &lib.DelegateTargets{Targets: []lib.DelegateTarget{{Name: "prod-east", Description: "pinned"}}, WidthMax: 3}
	for name, targets := range map[string]*lib.DelegateTargets{
		"no list": nil, "no agents": {Targets: []lib.DelegateTarget{}}, "no cluster-taking agent": pinnedOnly,
	} {
		bare, _ := toolWithScope(t, targets, nil)
		if desc, _ := toolWithScope(t, targets, page); desc != bare {
			t.Errorf("%s: %q, want %q", name, desc, bare)
		}
	}
}

// The widest page the part allows, of the longest names, with the most
// unlisted projects, renders inside its bound.
func TestScopeClustersDescriptionStaysInBounds(t *testing.T) {
	long := strings.Repeat("a", 63)
	widest := lib.ScopeClusters{Count: 1 << 30, Truncated: true, OldestListing: "2026-11-23T10:00:00Z", UnlistedCount: 1 << 30}
	for range lib.ScopeClustersPageMax {
		widest.Clusters = append(widest.Clusters, lib.ScopeCluster{Project: long, Location: long, Cluster: long})
	}
	for range lib.ScopeClustersUnlistedMax {
		widest.UnlistedProjects = append(widest.UnlistedProjects, long)
	}
	if _, err := lib.ScopeClustersPart(widest); err != nil {
		t.Fatalf("the widest page isn't a valid part: %v", err)
	}
	if n := len(scopeClustersDescription(widest)); n > scopeClustersDescriptionMax {
		t.Fatalf("the widest page renders in %d bytes, over %d", n, scopeClustersDescriptionMax)
	}
}

// The adapter hands the page to the server's env beside the targets, and the
// server reads back what the gateway sent. A page that doesn't parse is
// dropped and the targets still go; a page with no targets list goes
// nowhere, as the targets' own fallback names platform only.
func TestTheTurnsScopeClustersReachTheMCPServer(t *testing.T) {
	want := lib.ScopeClusters{Count: 1, Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}},
		UnlistedProjects: []string{"q"}, UnlistedCount: 1}
	scopePart, err := lib.ScopeClustersPart(want)
	if err != nil {
		t.Fatal(err)
	}
	targetsPart, err := lib.DelegateTargetsPart(*scTargets)
	if err != nil {
		t.Fatal(err)
	}
	cfg := `{"mcpServers":{"a2a":{"command":"worker-adapter","args":["mcp"]}}}`
	argv := []string{"claude", MCPConfigFlag, cfg, "-p"}
	envOf := func(parts ...lib.Part) (map[string]any, bool) {
		t.Helper()
		payload, _ := json.Marshal(lib.Message{Role: "user", Parts: append([]lib.Part{{Kind: "text", Text: "sweep"}}, parts...)})
		out, rewrote := withDelegateTargets(argv, &lib.Envelope{Payload: payload})
		var parsed map[string]map[string]map[string]any
		if err := json.Unmarshal([]byte(out[2]), &parsed); err != nil {
			t.Fatal(err)
		}
		env, _ := parsed[MCPServersKey][DelegateMCPServer][MCPEnvKey].(map[string]any)
		return env, rewrote
	}

	env, rewrote := envOf(targetsPart, scopePart)
	if !rewrote || env[EnvDelegateTargets] == nil {
		t.Fatalf("rewrote=%v env=%v", rewrote, env)
	}
	t.Setenv(EnvScopeClusters, env[EnvScopeClusters].(string))
	if got := ScopeClustersFromEnv(slog.Default()); got == nil || !reflect.DeepEqual(*got, want) {
		t.Fatalf("server read %+v, want %+v", got, want)
	}

	bad := lib.Part{Kind: "data", Data: json.RawMessage(`{"scopeClusters":{"count":0,"clusters":[],"truncated":false,"cursor":"x"}}`)}
	if env, rewrote := envOf(targetsPart, bad); !rewrote || env[EnvDelegateTargets] == nil || env[EnvScopeClusters] != nil {
		t.Fatalf("a bad page: rewrote=%v env=%v, want the targets alone", rewrote, env)
	}
	if env, rewrote := envOf(scopePart); rewrote || env != nil {
		t.Fatalf("a page without targets: rewrote=%v env=%v, want argv untouched", rewrote, env)
	}
	t.Setenv(EnvScopeClusters, `{"count":0,"clusters":[],"truncated":true,"cursor":""}`)
	if got := ScopeClustersFromEnv(slog.Default()); got != nil {
		t.Fatalf("an invalid page read as %+v, want none", got)
	}
}

// The worker's other readers of a submission (the prompt text, the targets,
// the width, the task parameters) give the same answer with the
// scopeClusters part present: each reads text parts or its own data key and
// passes over any other. They are unchanged from before the part, so this is
// also how a worker that predates it reads the submission.
func TestTheScopeClustersPartLeavesTheWorkersOtherReadersAlone(t *testing.T) {
	scopePart, err := lib.ScopeClustersPart(lib.ScopeClusters{Count: 2, Truncated: true,
		Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}})
	if err != nil {
		t.Fatal(err)
	}
	targetsPart, _ := lib.DelegateTargetsPart(*scTargets)
	paramsPart, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: map[string]string{"service": "web"}})
	if err != nil {
		t.Fatal(err)
	}
	base := []lib.Part{{Kind: "text", Text: "sweep"}, targetsPart, paramsPart}
	with := append(append([]lib.Part{}, base...), scopePart)
	envelope := func(parts []lib.Part) *lib.Envelope {
		payload, _ := json.Marshal(lib.Message{Role: "user", Parts: parts})
		return &lib.Envelope{Payload: payload}
	}
	if textFromParts(base) != textFromParts(with) {
		t.Error("the prompt text moved")
	}
	a, _, _ := lib.DelegateTargetsOf(base)
	b, _, _ := lib.DelegateTargetsOf(with)
	if !reflect.DeepEqual(a, b) || delegateWidthOf(envelope(base)) != delegateWidthOf(envelope(with)) {
		t.Error("the targets or the width moved")
	}
	pa, _, _ := lib.TaskParametersOf(base)
	pb, _, errB := lib.TaskParametersOf(with)
	if errB != nil || !reflect.DeepEqual(pa, pb) {
		t.Errorf("the task parameters moved: %v %v %v", pa, pb, errB)
	}
}
