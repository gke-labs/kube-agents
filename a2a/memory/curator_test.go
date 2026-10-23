package memory

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

type mockHindsightItem struct {
	Content    string            `json:"content"`
	DocumentID string            `json:"document_id"`
	UpdateMode string            `json:"update_mode"`
	Context    string            `json:"context"`
	Tags       []string          `json:"tags"`
	Metadata   map[string]string `json:"metadata"`
	Strategy   string            `json:"strategy"`
}

type mockHindsightServer struct {
	mu          sync.Mutex
	bankMission string
	bankConfig  map[string]any
	docs        map[string]mockHindsightItem
}

func newMockHindsightServer(t *testing.T) (*mockHindsightServer, *httptest.Server) {
	t.Helper()
	m := &mockHindsightServer{
		bankConfig: map[string]any{},
		docs:       map[string]mockHindsightItem{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		m.bankMission, _ = body["mission"].(string)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/config", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		var body struct {
			Updates map[string]any `json:"updates"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		for k, v := range body.Updates {
			m.bankConfig[k] = v
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/memories", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		var body struct {
			Items []mockHindsightItem `json:"items"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		for _, item := range body.Items {
			// update_mode: "replace" overwrites the document in place by document_id.
			m.docs[item.DocumentID] = item
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/memories/recall", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		var req struct {
			Query     string   `json:"query"`
			Tags      []string `json:"tags"`
			TagsMatch string   `json:"tags_match"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		var results []HindsightRecallResult
		for docID, doc := range m.docs {
			if !matchesTags(doc.Tags, req.Tags, req.TagsMatch) {
				continue
			}
			results = append(results, HindsightRecallResult{
				ID:         doc.Metadata["id"],
				DocumentID: docID,
				Text:       doc.Content,
				Tags:       doc.Tags,
			})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
	})

	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return m, srv
}

func matchesTags(have, want []string, mode string) bool {
	if len(want) == 0 {
		return true
	}
	set := map[string]bool{}
	for _, t := range have {
		set[t] = true
	}
	if mode == "any_strict" {
		for _, w := range want {
			if set[w] {
				return true
			}
		}
		return false
	}
	for _, w := range want {
		if !set[w] {
			return false
		}
	}
	return true
}

func startEmbeddedJetStream(t *testing.T) string {
	t.Helper()
	opts := &server.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		NoLog:     true,
		NoSigs:    true,
		JetStream: true,
		StoreDir:  t.TempDir(),
	}
	ns, err := server.NewServer(opts)
	if err != nil {
		t.Fatalf("new nats server: %v", err)
	}
	go ns.Start()
	if !ns.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server not ready")
	}
	t.Cleanup(func() {
		ns.Shutdown()
		ns.WaitForShutdown()
	})

	nc, err := nats.Connect(ns.ClientURL())
	if err != nil {
		t.Fatalf("connect setup: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream setup: %v", err)
	}
	ctx := context.Background()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:              lib.StreamTopicsState,
		Subjects:          []string{"a2a.topics.agent.platform.upgrade-readiness", SubjectBlueprint},
		MaxMsgsPerSubject: 8,
		AllowDirect:       true,
	}); err != nil {
		t.Fatalf("create TOPICS-STATE: %v", err)
	}
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:        lib.StreamTopicsJournal,
		Subjects:    []string{SubjectAnnotations},
		AllowDirect: true,
	}); err != nil {
		t.Fatalf("create TOPICS-JOURNAL: %v", err)
	}
	return ns.ClientURL()
}

func TestCuratorEndToEndNATSFlow(t *testing.T) {
	natsURL := startEmbeddedJetStream(t)
	mockHS, hsSrv := newMockHindsightServer(t)

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	client, err := lib.Connect(ctx, natsURL, lib.WithName("test-curator"))
	if err != nil {
		t.Fatalf("connect lib.Client: %v", err)
	}
	defer client.Close()

	repoDir := t.TempDir()
	// Create a sample deployment manifest so GuardPassesOnHead / CheckChangedFiles can evaluate it.
	manifestRel := "clusters/prod/checkout/deployment.yaml"
	manifestFull := filepath.Join(repoDir, filepath.FromSlash(manifestRel))
	if err := os.MkdirAll(filepath.Dir(manifestFull), 0o755); err != nil {
		t.Fatal(err)
	}
	validManifest := `apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: payments
spec:
  template:
    spec:
      containers:
        - name: checkout
          resources:
            requests:
              cpu: 500m
              memory: 256Mi
`
	if err := os.WriteFile(manifestFull, []byte(validManifest), 0o644); err != nil {
		t.Fatal(err)
	}

	cur, err := NewCurator(CuratorConfig{
		RepoDir:             repoDir,
		RepoID:              "dshnayder-org/infra",
		Session:             "agent",
		Profile:             "platform",
		RecurrenceThreshold: 2,
		Client:              client,
		Hindsight:           NewHindsightClient(hsSrv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatalf("NewCurator: %v", err)
	}
	if err := cur.Start(ctx); err != nil {
		t.Fatalf("cur.Start: %v", err)
	}
	defer cur.Stop()

	publishOutcome := func(ev OutcomeEvent) {
		t.Helper()
		art, err := lib.NewTopicArtifact("annotations", ev.Summary, ev)
		if err != nil {
			t.Fatalf("NewTopicArtifact: %v", err)
		}
		if err := client.PublishTopic(ctx, SubjectAnnotations,
			lib.Party{Session: "agent", Profile: "platform"},
			"", "", lib.NewCorrelationID(), art); err != nil {
			t.Fatalf("PublishTopic: %v", err)
		}
	}

	waitForBlueprint := func(cond func(BlueprintDigest) bool) BlueprintDigest {
		t.Helper()
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			env, err := client.ReadTopicLatest(ctx, lib.StreamTopicsState, SubjectBlueprint)
			if err == nil && env != nil {
				var art lib.Artifact
				if json.Unmarshal(env.Payload, &art) == nil {
					for _, p := range art.Parts {
						if p.Kind == "data" {
							var d BlueprintDigest
							if json.Unmarshal(p.Data, &d) == nil && cond(d) {
								return d
							}
						}
					}
				}
			}
			time.Sleep(30 * time.Millisecond)
		}
		t.Fatal("timed out waiting for expected blueprint state")
		return BlueprintDigest{}
	}

	// -------------------------------------------------------------------------
	// Scenario 1a: Chat proposes a team rule ("Do not submit infrastructure PRs on Friday")
	// -------------------------------------------------------------------------
	publishOutcome(OutcomeEvent{
		Schema:      OutcomeSchema,
		Kind:        OutcomeProposed,
		RecordID:    "DEC-2026-004",
		RecordType:  RecordTypeDecision,
		Repo:        "dshnayder-org/infra",
		Title:       "No infrastructure PR merges on Fridays",
		Summary:     "Proposed in group chat: do not submit infrastructure PRs on Friday.",
		Rule:        "Do not merge infrastructure pull requests on Fridays without an incident override.",
		PullRequest: "https://github.com/dshnayder-org/infra/pull/280 (#280)",
		Scope: &RecordScope{
			Paths: []string{"clusters/**"},
		},
	})

	bp := waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Proposed == 1 && len(d.ProposedIDs) == 1 && d.ProposedIDs[0] == "DEC-2026-004"
	})
	if bp.Counts.Active != 0 {
		t.Fatalf("expected 0 active records while proposed, got %d", bp.Counts.Active)
	}
	mockHS.mu.Lock()
	doc004, ok := mockHS.docs["okf:dshnayder-org/infra:DEC-2026-004"]
	mockHS.mu.Unlock()
	if !ok {
		t.Fatal("expected okf:dshnayder-org/infra:DEC-2026-004 in Hindsight")
	}
	if !matchesTags(doc004.Tags, []string{"scope:shared", "kind:decision", "status:proposed", "id:DEC-2026-004"}, "all_strict") {
		t.Fatalf("unexpected tags on proposed DEC-2026-004: %v", doc004.Tags)
	}

	// -------------------------------------------------------------------------
	// Scenario 1b: PR #280 is closed unmerged (refused) -> in-place update to status:rejected
	// -------------------------------------------------------------------------
	publishOutcome(OutcomeEvent{
		Schema:         OutcomeSchema,
		Kind:           OutcomeRefused,
		RecordID:       "DEC-2026-004",
		RecordType:     RecordTypeDecision,
		Repo:           "dshnayder-org/infra",
		PullRequest:    "https://github.com/dshnayder-org/infra/pull/280 (#280)",
		ReviewerReason: "Rejected: EU clusters deploy on Fridays during daytime hours.",
	})

	bp = waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Rejected == 1 && d.Counts.Proposed == 0
	})
	mockHS.mu.Lock()
	doc004After := mockHS.docs["okf:dshnayder-org/infra:DEC-2026-004"]
	mockHS.mu.Unlock()
	if !matchesTags(doc004After.Tags, []string{"scope:shared", "kind:decision", "status:rejected", "id:DEC-2026-004"}, "all_strict") {
		t.Fatalf("expected DEC-2026-004 to transition in-place to status:rejected, got tags %v", doc004After.Tags)
	}
	if matchesTags(doc004After.Tags, []string{"status:proposed"}, "all_strict") {
		t.Fatalf("stale status:proposed tag remained on DEC-2026-004: %v", doc004After.Tags)
	}
	if !strings.Contains(doc004After.Content, "EU clusters deploy on Fridays") {
		t.Fatalf("expected reviewer rejection reason in Hindsight chunk, got:\n%s", doc004After.Content)
	}

	// -------------------------------------------------------------------------
	// Scenario 2: Troubleshooting outcomes across 2 clusters -> automatic fleet-wide DEC graduation
	// -------------------------------------------------------------------------
	publishOutcome(OutcomeEvent{
		Schema:       OutcomeSchema,
		Kind:         OutcomeResolvedMerged,
		RecordID:     "LES-2026-001",
		RecordType:   RecordTypeLesson,
		Repo:         "dshnayder-org/infra",
		Cluster:      "cluster-eu-01",
		CheckID:      "autopilot-cpu-floor",
		Title:        "Checkout CPU request floor on cluster-eu-01",
		RootCause:    "Lowering checkout CPU request below 400m caused severe latency under load on cluster-eu-01.",
		Fix:          "Raised containers[name=checkout].resources.requests.cpu to 500m in PR #281.",
		Verification: "Latency returned to baseline and finding autopilot-cpu-floor cleared.",
		Prevention:   "Keep checkout CPU request >= 400m.",
		Rule:         "Keep checkout CPU request >= 400m.",
		PullRequest:  "https://github.com/dshnayder-org/infra/pull/281 (#281)",
		Scope: &RecordScope{
			Paths:    []string{"clusters/**/checkout/deployment.yaml"},
			Clusters: []string{"cluster-eu-01"},
			Checks:   []string{"autopilot-cpu-floor"},
		},
		Guard: &RecordGuard{
			Rule:     "Keep checkout CPU request >= 400m.",
			Severity: "block",
			Expect: &GuardExpect{
				File:  "clusters/**/checkout/deployment.yaml",
				Path:  "spec.template.spec.containers[name=checkout].resources.requests.cpu",
				Op:    ">=",
				Value: "400m",
			},
		},
	})

	waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Active == 1 && len(d.ActiveIDs) == 1 && d.ActiveIDs[0] == "LES-2026-001"
	})

	// Second cluster reports the same check and guarded YAML path -> triggers graduation to DEC-2026-005!
	publishOutcome(OutcomeEvent{
		Schema:       OutcomeSchema,
		Kind:         OutcomeResolvedMerged,
		RecordID:     "LES-2026-002",
		RecordType:   RecordTypeLesson,
		Repo:         "dshnayder-org/infra",
		Cluster:      "cluster-us-01",
		CheckID:      "autopilot-cpu-floor",
		Title:        "Checkout CPU request floor on cluster-us-01",
		RootCause:    "Lowering checkout CPU request to 200m caused latency spikes on cluster-us-01.",
		Fix:          "Restored checkout CPU request to 500m in PR #284.",
		Verification: "Finding autopilot-cpu-floor resolved on cluster-us-01.",
		Prevention:   "Keep checkout CPU request >= 400m across all clusters.",
		Rule:         "Keep checkout CPU request >= 400m.",
		PullRequest:  "https://github.com/dshnayder-org/infra/pull/284 (#284)",
		Scope: &RecordScope{
			Paths:    []string{"clusters/**/checkout/deployment.yaml"},
			Clusters: []string{"cluster-us-01"},
			Checks:   []string{"autopilot-cpu-floor"},
		},
		Guard: &RecordGuard{
			Rule:     "Keep checkout CPU request >= 400m.",
			Severity: "block",
			Expect: &GuardExpect{
				File:  "clusters/**/checkout/deployment.yaml",
				Path:  "spec.template.spec.containers[name=checkout].resources.requests.cpu",
				Op:    ">=",
				Value: "400m",
			},
		},
	})

	bp = waitForBlueprint(func(d BlueprintDigest) bool {
		return len(d.GraduatedDecisions) == 1 && d.Counts.Superseded == 2 && d.Counts.Active == 1
	})
	gradID := bp.GraduatedDecisions[0]
	if gradID != "DEC-2026-005" {
		t.Fatalf("expected graduated decision DEC-2026-005, got %s", gradID)
	}

	// Verify active recall in Hindsight returns only the graduated DEC-2026-005, not the rejected DEC-2026-004 or superseded LES-2026-001/002.
	hsClient := NewHindsightClient(hsSrv.URL, DefaultBankID)
	activeResults, err := hsClient.Recall(ctx, "checkout cpu request", []string{SharedTag, "status:active"}, "all_strict")
	if err != nil {
		t.Fatalf("Hindsight Recall: %v", err)
	}
	if len(activeResults) != 1 || activeResults[0].ID != "DEC-2026-005" {
		t.Fatalf("expected exactly DEC-2026-005 in active recall, got %+v", activeResults)
	}

	// Verify NATS request-reply query (QueryViaNATS) answers from Hindsight, caches repeated queries,
	// and invalidates the cache when a new outcome is published on NATS.
	qReq := MemoryQueryRequest{
		Query:     "checkout cpu request",
		Tags:      []string{SharedTag, "status:active"},
		TagsMatch: "all_strict",
	}
	qResp1, err := QueryViaNATS(ctx, client, qReq)
	if err != nil {
		t.Fatalf("QueryViaNATS first call: %v", err)
	}
	if qResp1.Cached {
		t.Fatalf("expected first NATS query to be uncached (Cached=false)")
	}
	if qResp1.Backend != "hindsight" {
		t.Fatalf("expected backend=hindsight, got %q", qResp1.Backend)
	}
	if len(qResp1.Results) != 1 || qResp1.Results[0].ID != "DEC-2026-005" {
		t.Fatalf("expected DEC-2026-005 from NATS query, got %+v", qResp1.Results)
	}

	qResp2, err := QueryViaNATS(ctx, client, qReq)
	if err != nil {
		t.Fatalf("QueryViaNATS second call: %v", err)
	}
	if !qResp2.Cached {
		t.Fatalf("expected second identical NATS query to hit cache (Cached=true)")
	}

	// Publish a new active decision and verify that the NATS query cache is invalidated automatically.
	publishOutcome(OutcomeEvent{
		Schema:      OutcomeSchema,
		Kind:        OutcomeApproved,
		RecordID:    "DEC-2026-006",
		RecordType:  RecordTypeDecision,
		Repo:        "dshnayder-org/infra",
		Title:       "Checkout PDB minimum available replicas",
		Summary:     "Require minAvailable >= 2 on checkout PodDisruptionBudget.",
		Rule:        "Keep checkout PDB minAvailable >= 2.",
		PullRequest: "https://github.com/dshnayder-org/infra/pull/290 (#290)",
		Scope: &RecordScope{
			Paths: []string{"clusters/**/checkout/pdb.yaml"},
		},
	})
	waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Active == 2
	})

	qResp3, err := QueryViaNATS(ctx, client, qReq)
	if err != nil {
		t.Fatalf("QueryViaNATS after outcome invalidation: %v", err)
	}
	if qResp3.Cached {
		t.Fatalf("expected NATS query cache to be invalidated after new outcome")
	}
	if len(qResp3.Results) != 2 {
		t.Fatalf("expected 2 active records after DEC-2026-006 approval, got %d", len(qResp3.Results))
	}

	// Verify CheckChangedFiles blocks a regression lowering CPU to 100m.
	records, err := LoadRecords(repoDir)
	if err != nil {
		t.Fatal(err)
	}
	badYAML := strings.Replace(validManifest, "cpu: 500m", "cpu: 100m", 1)
	report := CheckChangedFiles(records, map[string]*string{
		manifestRel: &badYAML,
	})
	if report.Verdict != "block" {
		t.Fatalf("expected verdict=block on 100m CPU regression, got %s (%+v)", report.Verdict, report)
	}
	if len(report.Findings) != 1 || report.Findings[0].ID != "DEC-2026-005" {
		t.Fatalf("expected finding from DEC-2026-005, got %+v", report.Findings)
	}
}
