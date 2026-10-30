package mcpbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// ConfigStore: persistence, deployment disambiguation, release enrichment, and cross-process coherence.

func TestConfigStorePersistenceAndConversations(t *testing.T) {
	cfgFile := filepath.Join(t.TempDir(), "mcp-config.json")
	store, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}

	err = store.UpsertDeployments([]DeploymentProfile{
		{
			Name:              testClusterName,
			KubeContext:       testKubeContext,
			Namespace:         testNamespace,
			PlatformAgentName: testPAName,
			ClusterName:       testClusterName,
			GitHubRepo:        testGitHubRepo,
			MCPToken:          "in-memory-secret-never-persisted",
			Ready:             true,
		},
	})
	if err != nil {
		t.Fatalf("UpsertDeployments: %v", err)
	}

	rawDisk, err := os.ReadFile(cfgFile)
	if err != nil {
		t.Fatalf("ReadFile config: %v", err)
	}
	if strings.Contains(string(rawDisk), "in-memory-secret-never-persisted") {
		t.Fatalf("MCPToken must never be persisted to disk, got:\n%s", rawDisk)
	}

	active, ok := store.ActiveDeployment()
	if !ok || active.Name != testClusterName {
		t.Fatalf("ActiveDeployment = %+v, ok=%v; want %s", active, ok, testClusterName)
	}

	conv, err := store.EnsureConversation("incident-42", true, "Investigating node upgrade")
	if err != nil {
		t.Fatalf("EnsureConversation: %v", err)
	}
	if conv.Alias != "incident-42" || !strings.HasPrefix(conv.Token, defaultConvTokenPrefix) {
		t.Fatalf("unexpected conversation entry: %+v", conv)
	}
	if _, err := store.SetConversationSessionMode("incident-42", true); err != nil {
		t.Fatalf("SetConversationSessionMode: %v", err)
	}

	reloaded, err := NewConfigStore(cfgFile)
	if err != nil {
		t.Fatalf("reloading ConfigStore: %v", err)
	}
	if reloaded.ActiveConversation().Alias != "incident-42" || !reloaded.ActiveConversation().SessionMode {
		t.Fatalf("reloaded conversation state mismatch: %+v", reloaded.ActiveConversation())
	}
}

func TestConfigStoreCrossProcessCoherence(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mcp-config.json")
	storeA, err := NewConfigStore(path)
	if err != nil {
		t.Fatal(err)
	}
	storeB, err := NewConfigStore(path)
	if err != nil {
		t.Fatal(err)
	}

	if err := storeA.SetDebugMode(true); err != nil {
		t.Fatal(err)
	}
	if err := storeB.UpsertDeployments([]DeploymentProfile{{Name: "host", KubeContext: "ctx-host", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true}}); err != nil {
		t.Fatal(err)
	}
	if got := storeA.Snapshot(); !got.DebugMode || got.Deployments["host"].KubeContext != "ctx-host" {
		t.Fatalf("storeA sees debug=%v deployments=%v; want both writes", got.DebugMode, got.Deployments)
	}
	if got := storeB.Snapshot(); !got.DebugMode {
		t.Fatalf("storeB lost storeA's DebugMode write")
	}

	const writers, perWriter = 6, 10
	var wg sync.WaitGroup
	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			st, err := NewConfigStore(path)
			if err != nil {
				t.Error(err)
				return
			}
			for i := 0; i < perWriter; i++ {
				if _, err := st.EnsureConversation(fmt.Sprintf("conv-%d-%d", w, i), true, ""); err != nil {
					t.Error(err)
					return
				}
			}
		}(w)
	}
	wg.Wait()
	snap := storeA.Snapshot()
	// 1 default + writers*perWriter conversations
	if len(snap.Conversations) < writers*perWriter {
		t.Fatalf("Conversations len = %d, want at least %d", len(snap.Conversations), writers*perWriter)
	}
}

func TestUpsertAndSyncCrossContextNameDisambiguation(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	const (
		ctxA = "gke_proj_us-central1_a"
		ctxB = "gke_proj_us-east1_b"
	)
	// Full scan with two contexts sharing the same cluster alias "shared-cluster".
	fullScan := []DeploymentProfile{
		{Name: "shared-cluster", KubeContext: ctxA, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: "shared-cluster", KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
	}
	if err := store.SyncDiscoveredDeployments(fullScan); err != nil {
		t.Fatalf("SyncDiscoveredDeployments: %v", err)
	}
	snap := store.Snapshot()
	if _, ok := snap.Deployments[ctxA+"/shared-cluster"]; !ok {
		t.Fatalf("missing disambiguated key %q in %v", ctxA+"/shared-cluster", snap.Deployments)
	}
	if _, ok := snap.Deployments[ctxB+"/shared-cluster"]; !ok {
		t.Fatalf("missing disambiguated key %q in %v", ctxB+"/shared-cluster", snap.Deployments)
	}

	// Subsequent single-context scan (where discovery returns bare "shared-cluster" for ctxB)
	// must update ctxB+"/shared-cluster" in-place rather than creating a bare "shared-cluster"
	// entry or clobbering ctxA.
	filteredScan := []DeploymentProfile{
		{Name: "shared-cluster", KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Version: "0.16.0", Ready: true},
	}
	if err := store.UpsertDeployments(filteredScan); err != nil {
		t.Fatalf("UpsertDeployments(filteredScan): %v", err)
	}
	snap = store.Snapshot()
	if len(snap.Deployments) != 2 {
		t.Fatalf("expected 2 deployments after filtered scan, got %d: %v", len(snap.Deployments), snap.Deployments)
	}
	if got := snap.Deployments[ctxB+"/shared-cluster"].Version; got != "0.16.0" {
		t.Fatalf("expected %s/shared-cluster version updated to 0.16.0, got %q", ctxB, got)
	}
}

func TestSyncDiscoveredDeploymentsKeepsProfilesOfUnscannedContexts(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatalf("NewConfigStore: %v", err)
	}
	const (
		ctxA, ctxB, ctxC = "gke_example_us-central1_a", "gke_example_us-central1_b", "gke_example_us-central1_c"
		depA, depB, depC = "cluster-a", "cluster-b", "cluster-c"
	)
	saved := []DeploymentProfile{
		{Name: depA, KubeContext: ctxA, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: depB, KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: depC, KubeContext: ctxC, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: "manual", DirectMCPURL: "http://127.0.0.1:8089", Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
	}
	if err := store.UpsertDeployments(saved); err != nil {
		t.Fatalf("UpsertDeployments: %v", err)
	}
	if _, err := store.SetActiveDeployment(depA); err != nil {
		t.Fatalf("SetActiveDeployment: %v", err)
	}

	scan := []DeploymentProfile{
		{Name: depB, KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true, LastDiscoveredAt: time.Now().UTC()},
		ContextProbeFailure(ctxA, fmt.Errorf("Unauthorized")),
	}
	if err := store.SyncDiscoveredDeployments(scan); err != nil {
		t.Fatalf("SyncDiscoveredDeployments: %v", err)
	}
	snap := store.Snapshot()
	if snap.ActiveDeployment != depA {
		t.Fatalf("active deployment re-targeted to %q after a failed probe of %q; want it kept", snap.ActiveDeployment, depA)
	}
	keptA, ok := snap.Deployments[depA]
	if !ok {
		t.Fatalf("profile %q was pruned although its context only failed to answer", depA)
	}
	if keptA.Ready || !strings.Contains(keptA.StatusMessage, "Unauthorized") {
		t.Fatalf("kept profile should be marked Not Ready with the probe failure, got %+v", keptA)
	}
	if _, ok := snap.Deployments[depC]; ok {
		t.Fatalf("profile %q should have been pruned: its context was not in the scan and did not fail", depC)
	}
	if _, ok := snap.Deployments["manual"]; !ok {
		t.Fatalf("manually configured direct-URL profile must survive a kubeconfig sync")
	}

	// Next scan that reaches A restores it.
	recovered := []DeploymentProfile{
		{Name: depA, KubeContext: ctxA, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true, StatusMessage: "Reconciled", LastDiscoveredAt: time.Now().UTC()},
		{Name: depB, KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true, LastDiscoveredAt: time.Now().UTC()},
	}
	if err := store.SyncDiscoveredDeployments(recovered); err != nil {
		t.Fatalf("SyncDiscoveredDeployments (recovered): %v", err)
	}
	snap = store.Snapshot()
	if got := snap.Deployments[depA]; !got.Ready || got.StatusMessage != "Reconciled" || snap.ActiveDeployment != depA {
		t.Fatalf("recovered profile mismatch: %+v (active=%q)", got, snap.ActiveDeployment)
	}
}

func TestEnrichDeploymentVersionStatusSemverAndTargetCommitish(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case githubLatestReleasePath:
			_ = json.NewEncoder(w).Encode(map[string]any{
				"tag_name":         "0.16.0",
				"target_commitish": "main",
				"html_url":         "https://github.com/gke-labs/kube-agents/releases/tag/0.16.0",
			})
		case "/repos/gke-labs/kube-agents/compare/0.15.0...0.16.0":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"status":   "ahead",
				"ahead_by": 4,
			})
		case "/repos/gke-labs/kube-agents/compare/main...0.16.0":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"status":   "behind",
				"ahead_by": 0,
			})
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	ResetReleaseCacheForTest(srv.URL, srv.Client())

	// 1. Newer semver installed than latest release -> UpgradeAvailable must be false.
	newer := DeploymentProfile{Name: "newer", Version: "0.17.0"}
	EnrichDeploymentVersionStatus(context.Background(), &newer)
	if newer.UpgradeAvailable || newer.CommitsBehind != 0 {
		t.Fatalf("newer semver 0.17.0 vs 0.16.0 marked UpgradeAvailable=%v CommitsBehind=%d", newer.UpgradeAvailable, newer.CommitsBehind)
	}

	// 2. Older semver installed -> UpgradeAvailable true, CommitsBehind=4.
	older := DeploymentProfile{Name: "older", Version: "0.15.0"}
	EnrichDeploymentVersionStatus(context.Background(), &older)
	if !older.UpgradeAvailable || older.CommitsBehind != 4 {
		t.Fatalf("older semver 0.15.0 vs 0.16.0: UpgradeAvailable=%v CommitsBehind=%d, want true / 4", older.UpgradeAvailable, older.CommitsBehind)
	}

	// 3. Branch name "main" when target_commitish is "main" and compare says ahead_by=0 -> UpgradeAvailable false.
	branchProf := DeploymentProfile{Name: "branch", Version: "main"}
	EnrichDeploymentVersionStatus(context.Background(), &branchProf)
	if branchProf.UpgradeAvailable {
		t.Fatalf("branch 'main' with ahead_by=0 should not report UpgradeAvailable=true")
	}
}

func TestDeleteConversationRefusesAmbiguousRoleMatch(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	const (
		parentA, parentB = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
		workerA, workerB = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
	)
	if err := store.WithUpdateLock(func(cfg *PersistentConfig) bool {
		now := time.Now().UTC()
		cfg.Conversations["session-a"] = ConversationEntry{Alias: "session-a", Token: MintConversationToken(), HostConversationID: workerA, ParentHostConversationID: parentA, SubagentRole: "Reviewer", CreatedAt: now, LastUsedAt: now}
		cfg.Conversations["session-b"] = ConversationEntry{Alias: "session-b", Token: MintConversationToken(), HostConversationID: workerB, ParentHostConversationID: parentB, SubagentRole: "Reviewer", CreatedAt: now, LastUsedAt: now}
		return true
	}); err != nil {
		t.Fatal(err)
	}

	// Both windows' workers carry the role "Reviewer"; deleting by its slug must not
	// pick one of them at random.
	if deleted, ok := store.DeleteConversation("session-reviewer"); ok {
		t.Fatalf("ambiguous role match deleted %q; want nothing deleted", deleted.Alias)
	}
	snap := store.Snapshot()
	if _, okA := snap.Conversations["session-a"]; !okA {
		t.Fatal("window A's worker entry was deleted")
	}
	if _, okB := snap.Conversations["session-b"]; !okB {
		t.Fatal("window B's worker entry was deleted")
	}

	// An exact alias still deletes exactly that entry.
	deleted, ok := store.DeleteConversation("session-a")
	if !ok || deleted.HostConversationID != workerA {
		t.Fatalf("DeleteConversation(session-a) = %+v, %v", deleted, ok)
	}
	// With one worker left the role slug is unambiguous again.
	deleted, ok = store.DeleteConversation("session-reviewer")
	if !ok || deleted.HostConversationID != workerB {
		t.Fatalf("DeleteConversation(session-reviewer) = %+v, %v; want the remaining worker", deleted, ok)
	}
}

func TestSyncKeepsDisambiguatedNameWhenTheOtherContextFails(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	const (
		ctxA, ctxB = "gke_proj-a_us-central1_kube-agents", "gke_proj-b_us-central1_kube-agents"
		shared     = "kube-agents"
	)
	both := []DeploymentProfile{
		{Name: shared, KubeContext: ctxA, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		{Name: shared, KubeContext: ctxB, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
	}
	if err := store.SyncDiscoveredDeployments(both); err != nil {
		t.Fatal(err)
	}
	keyA, keyB := ctxA+"/"+shared, ctxB+"/"+shared
	if _, err := store.SetActiveDeployment(keyB); err != nil {
		t.Fatal(err)
	}

	// ctxB fails its probe, so ctxA's profile arrives under the bare name.
	partial := []DeploymentProfile{
		{Name: shared, KubeContext: ctxA, Namespace: testNamespace, PlatformAgentName: testPAName, Ready: true},
		ContextProbeFailure(ctxB, fmt.Errorf("Unauthorized")),
	}
	for _, scan := range [][]DeploymentProfile{partial, both} {
		if err := store.SyncDiscoveredDeployments(scan); err != nil {
			t.Fatal(err)
		}
		snap := store.Snapshot()
		if _, ok := snap.Deployments[shared]; ok {
			t.Fatalf("a bare %q profile appeared: %v", shared, snap.Deployments)
		}
		if _, ok := snap.Deployments[keyA]; !ok {
			t.Fatalf("%q was renamed or pruned: %v", keyA, snap.Deployments)
		}
		if _, ok := snap.Deployments[keyB]; !ok {
			t.Fatalf("%q was pruned: %v", keyB, snap.Deployments)
		}
		if snap.ActiveDeployment != keyB {
			t.Fatalf("active deployment re-targeted to %q; want %q", snap.ActiveDeployment, keyB)
		}
	}
}

func TestSelectDeploymentRefusesAnAmbiguousSelector(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.WithUpdateLock(func(cfg *PersistentConfig) bool {
		cfg.Deployments = map[string]DeploymentProfile{
			"ctxA/platform": {Name: "ctxA/platform", KubeContext: "ctxA", Namespace: "ns", PlatformAgentName: "platform"},
			"ctxB/platform": {Name: "ctxB/platform", KubeContext: "ctxB", Namespace: "ns", PlatformAgentName: "platform"},
			"prod-east":     {Name: "prod-east", KubeContext: "ctxC", Namespace: "ns", PlatformAgentName: "pa-c", ClusterName: "c"},
			"prod-west":     {Name: "prod-west", KubeContext: "ctxD", Namespace: "ns", PlatformAgentName: "pa-d", ClusterName: "d"},
		}
		cfg.ActiveDeployment = ""
		return true
	}); err != nil {
		t.Fatal(err)
	}
	// Run each lookup several times: a map-order pick would come back with a profile on some
	// runs.
	for i := 0; i < 20; i++ {
		for _, sel := range []string{"platform", "prod", ""} {
			if p, err := store.SelectDeployment(sel); !errors.Is(err, errDeploymentAmbiguous) {
				t.Fatalf("SelectDeployment(%q) = %q, %v; want it refused as ambiguous", sel, p.Name, err)
			}
		}
	}
	for sel, want := range map[string]string{"ctxB/platform": "ctxB/platform", "ctxA": "ctxA/platform", "prod-west": "prod-west", "east": "prod-east"} {
		if p, err := store.SelectDeployment(sel); err != nil || p.Name != want {
			t.Errorf("SelectDeployment(%q) = %q, %v; want %q", sel, p.Name, err, want)
		}
	}
}

func TestDiscoverDeploymentsEmptyFullScanPrunesSavedProfiles(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{
		{Name: "gone", KubeContext: "ctx-gone", Namespace: "ns", PlatformAgentName: "pa", Ready: true},
		{Name: "direct", DirectMCPURL: "https://mcp.example.com", Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: failingClusterAccess{}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	if res := srv.CallTool(context.Background(), ToolDiscoverDeployments, json.RawMessage(`{}`)); res.IsError {
		t.Fatalf("discover_deployments: %+v", res)
	}
	snap := store.Snapshot()
	if _, ok := snap.Deployments["gone"]; ok {
		t.Fatal("a clean full scan that found no installs kept the old kube profile")
	}
	if _, ok := snap.Deployments["direct"]; !ok {
		t.Fatal("the scan removed a direct-URL profile")
	}
}

func TestSyncPrunesAProfileWhoseNameAnotherContextNowHolds(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SyncDiscoveredDeployments([]DeploymentProfile{
		{Name: "kube-agents", KubeContext: "ctx-a", Namespace: "ns", PlatformAgentName: "pa", Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	// ctx-a left the kubeconfig; ctx-b's install resolves to the same alias.
	if err := store.SyncDiscoveredDeployments([]DeploymentProfile{
		{Name: "kube-agents", KubeContext: "ctx-b", Namespace: "ns", PlatformAgentName: "pa", Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	for k, p := range store.Snapshot().Deployments {
		if p.KubeContext == "ctx-a" {
			t.Fatalf("the ctx-a profile survived as %q after its context was gone: %v", k, store.Snapshot().Deployments)
		}
	}
}

func TestUpsertQualifiesAThirdSameNamedDeployment(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.SyncDiscoveredDeployments([]DeploymentProfile{
		{Name: "platform", KubeContext: "ctxA", Namespace: "ns", PlatformAgentName: "platform", Ready: true},
		{Name: "platform", KubeContext: "ctxB", Namespace: "ns", PlatformAgentName: "platform", Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{
		{Name: "platform", KubeContext: "ctxC", Namespace: "ns", PlatformAgentName: "platform", Ready: true},
	}); err != nil {
		t.Fatal(err)
	}
	snap := store.Snapshot()
	if _, ok := snap.Deployments["platform"]; ok {
		t.Fatalf("ctxC's deployment was stored bare beside two qualified siblings: %v", snap.Deployments)
	}
	if _, ok := snap.Deployments["ctxC/platform"]; !ok {
		t.Fatalf("missing ctxC/platform in %v", snap.Deployments)
	}
}
