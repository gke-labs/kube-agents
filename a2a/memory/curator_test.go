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
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/documents", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		var items []HindsightDocument
		for docID, doc := range m.docs {
			items = append(items, HindsightDocument{
				ID:               docID,
				Tags:             doc.Tags,
				TextLength:       len(doc.Content),
				DocumentMetadata: doc.Metadata,
			})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"items": items})
	})
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/documents/", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		if r.Method == http.MethodDelete {
			docID := strings.TrimPrefix(r.URL.Path, "/v1/default/banks/kube-agents-decisions/documents/")
			delete(m.docs, docID)
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		http.NotFound(w, r)
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
		RepoID:              "acme/infra",
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
		if _, err := PublishOutcome(ctx, client, ev); err != nil {
			t.Fatalf("PublishOutcome: %v", err)
		}
	}

	publishSharedAnnotation := func(ev OutcomeEvent) {
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
		Repo:        "acme/infra",
		Title:       "No infrastructure PR merges on Fridays",
		Summary:     "Proposed in group chat: do not submit infrastructure PRs on Friday.",
		Rule:        "Do not merge infrastructure pull requests on Fridays without an incident override.",
		PullRequest: "https://github.com/acme/infra/pull/280 (#280)",
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
	doc004, ok := mockHS.docs["okf:acme/infra:DEC-2026-004"]
	mockHS.mu.Unlock()
	if !ok {
		t.Fatal("expected okf:acme/infra:DEC-2026-004 in Hindsight")
	}
	if !matchesTags(doc004.Tags, []string{"scope:shared", "kind:decision", "status:proposed", "id:DEC-2026-004"}, "all_strict") {
		t.Fatalf("unexpected tags on proposed DEC-2026-004: %v", doc004.Tags)
	}
	if len(bp.Records) != 1 || bp.Records[0].Branch != "platform-agent/memory/DEC-2026-004" || !strings.Contains(bp.Records[0].PRMarkdown, "status: active") {
		t.Fatalf("expected proposed record in blueprint to carry branch platform-agent/memory/DEC-2026-004 and PRMarkdown with status: active, got %+v", bp.Records)
	}
	pendingResp, err := QueryViaNATS(ctx, client, MemoryQueryRequest{Action: "pending"})
	if err != nil {
		t.Fatalf("QueryViaNATS pending: %v", err)
	}
	if len(pendingResp.Records) != 1 || pendingResp.Records[0].ID != "DEC-2026-004" || !strings.Contains(pendingResp.Records[0].PRMarkdown, "status: active") {
		t.Fatalf("expected DEC-2026-004 in pending query with status: active PRMarkdown, got %+v", pendingResp.Records)
	}

	// -------------------------------------------------------------------------
	// Scenario 1a-sec: Forged OutcomeApproved on shared SubjectAnnotations is rejected
	// -------------------------------------------------------------------------
	publishSharedAnnotation(OutcomeEvent{
		Schema:      OutcomeSchema,
		Kind:        OutcomeApproved,
		RecordID:    "DEC-2026-004",
		RecordType:  RecordTypeDecision,
		Repo:        "acme/infra",
		PullRequest: "https://github.com/acme/infra/pull/280 (#280)",
	})
	time.Sleep(100 * time.Millisecond)
	recsCheck, err := LoadRecords(repoDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range recsCheck {
		if r.Meta.ID == "DEC-2026-004" && r.Meta.Status == StatusActive {
			t.Fatalf("forged OutcomeApproved on shared SubjectAnnotations must be rejected, got status:active")
		}
	}

	// -------------------------------------------------------------------------
	// Scenario 1b: PR #280 is closed unmerged (refused) -> in-place update to status:rejected
	// -------------------------------------------------------------------------
	publishOutcome(OutcomeEvent{
		Schema:         OutcomeSchema,
		Kind:           OutcomeRefused,
		RecordID:       "DEC-2026-004",
		RecordType:     RecordTypeDecision,
		Repo:           "acme/infra",
		PullRequest:    "https://github.com/acme/infra/pull/280 (#280)",
		ReviewerReason: "Rejected: EU clusters deploy on Fridays during daytime hours.",
	})

	bp = waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Rejected == 1 && d.Counts.Proposed == 0
	})
	mockHS.mu.Lock()
	doc004After := mockHS.docs["okf:acme/infra:DEC-2026-004"]
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
		Repo:         "acme/infra",
		Cluster:      "cluster-eu-01",
		CheckID:      "autopilot-cpu-floor",
		Title:        "Checkout CPU request floor on cluster-eu-01",
		RootCause:    "Lowering checkout CPU request below 400m caused severe latency under load on cluster-eu-01.",
		Fix:          "Raised containers[name=checkout].resources.requests.cpu to 500m in PR #281.",
		Verification: "Latency returned to baseline and finding autopilot-cpu-floor cleared.",
		Prevention:   "Keep checkout CPU request >= 400m.",
		Rule:         "Keep checkout CPU request >= 400m.",
		PullRequest:  "https://github.com/acme/infra/pull/281 (#281)",
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

	// Post-merge soak synthesizes LES-2026-001 in status:proposed (requiring a knowledge/ PR merge for status:active).
	waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Proposed == 1 && len(d.ProposedIDs) == 1 && d.ProposedIDs[0] == "LES-2026-001"
	})

	// Second cluster reports the same check and guarded YAML path -> triggers graduation to DEC-2026-005 (also status:proposed)!
	publishOutcome(OutcomeEvent{
		Schema:       OutcomeSchema,
		Kind:         OutcomeResolvedMerged,
		RecordID:     "LES-2026-002",
		RecordType:   RecordTypeLesson,
		Repo:         "acme/infra",
		Cluster:      "cluster-us-01",
		CheckID:      "autopilot-cpu-floor",
		Title:        "Checkout CPU request floor on cluster-us-01",
		RootCause:    "Lowering checkout CPU request to 200m caused latency spikes on cluster-us-01.",
		Fix:          "Restored checkout CPU request to 500m in PR #284.",
		Verification: "Finding autopilot-cpu-floor resolved on cluster-us-01.",
		Prevention:   "Keep checkout CPU request >= 400m across all clusters.",
		Rule:         "Keep checkout CPU request >= 400m.",
		PullRequest:  "https://github.com/acme/infra/pull/284 (#284)",
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
		return len(d.GraduatedDecisions) == 1 && d.Counts.Proposed == 3 && d.Counts.Active == 0
	})
	gradID := bp.GraduatedDecisions[0]
	if gradID != "DEC-2026-005" {
		t.Fatalf("expected graduated decision DEC-2026-005, got %s", gradID)
	}

	// Merging the fleet-wide knowledge/ PR for DEC-2026-005 promotes DEC-2026-005 to status:active
	// and supersedes LES-2026-001 and LES-2026-002 while keeping unrelated rejected DEC-2026-004 intact!
	publishOutcome(OutcomeEvent{
		Schema:      OutcomeSchema,
		Kind:        OutcomeApproved,
		RecordID:    "DEC-2026-005",
		RecordType:  RecordTypeDecision,
		Repo:        "acme/infra",
		PullRequest: "https://github.com/acme/infra/pull/289 (#289)",
	})

	bp = waitForBlueprint(func(d BlueprintDigest) bool {
		return d.Counts.Active == 1 && d.Counts.Superseded == 2 && d.Counts.Rejected == 1
	})

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
		Repo:        "acme/infra",
		Title:       "Checkout PDB minimum available replicas",
		Summary:     "Require minAvailable >= 2 on checkout PodDisruptionBudget.",
		Rule:        "Keep checkout PDB minAvailable >= 2.",
		PullRequest: "https://github.com/acme/infra/pull/290 (#290)",
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

	// Verify duplicate proposed rule emission reuses the existing DEC-* ID, and linking a PR URL preserves the title/body.
	prop1, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Repo:       "acme/infra",
		Title:      "All staging Ingress resources must include managed-certificates",
		Summary:    "Staging Ingress resources require networking.gke.io/managed-certificates.",
		Rule:       "All staging Ingress resources must include the networking.gke.io/managed-certificates annotation.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome prop1: %v", err)
	}
	prop2, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Repo:       "acme/infra",
		Title:      "All staging Ingress resources must include managed-certificates",
		Summary:    "Staging Ingress resources require networking.gke.io/managed-certificates.",
		Rule:       "All staging Ingress resources must include the networking.gke.io/managed-certificates annotation.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome prop2: %v", err)
	}
	if prop1.RecordID != prop2.RecordID {
		t.Fatalf("expected duplicate proposal to reuse %s, got %s", prop1.RecordID, prop2.RecordID)
	}

	linked, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:      OutcomeSchema,
		Kind:        OutcomeProposed,
		RecordID:    prop1.RecordID,
		Repo:        "acme/infra",
		PullRequest: "https://github.com/acme/infra/pull/273 (#273)",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome link PR: %v", err)
	}
	if linked.Title != prop1.Title {
		t.Fatalf("expected PR link to preserve title %q, got %q", prop1.Title, linked.Title)
	}

	// Scenario 3: Reviewer modifies a proposed PR before merging (OutcomeModifiedMerged) ->
	// transitions in-place to status:active with the human-corrected guard and records the
	// superseded initial proposal in evidence.rejected_proposals.
	modRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:          OutcomeSchema,
		Kind:            OutcomeModifiedMerged,
		RecordID:        prop1.RecordID,
		Repo:            "acme/infra",
		PullRequest:     "https://github.com/acme/infra/pull/273 (#273)",
		Rule:            "All staging and preprod Ingress resources must include networking.gke.io/managed-certificates.",
		ReviewerReason:  "Reviewer expanded scope from staging-only to staging and preprod before merging.",
		InitialProposal: "All staging Ingress resources must include the networking.gke.io/managed-certificates annotation.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome modified-merged: %v", err)
	}
	if modRes.Status != StatusActive {
		t.Fatalf("expected modified-merged status=active, got %s", modRes.Status)
	}
	recordsAfterMod, err := LoadRecords(repoDir)
	if err != nil {
		t.Fatal(err)
	}
	var foundMod *Record
	for i := range recordsAfterMod {
		if recordsAfterMod[i].Meta.ID == prop1.RecordID {
			foundMod = &recordsAfterMod[i]
			break
		}
	}
	if foundMod == nil {
		t.Fatalf("expected %s on disk after modified-merged", prop1.RecordID)
	}
	if foundMod.Meta.Evidence.Outcome != string(OutcomeModifiedMerged) {
		t.Fatalf("expected evidence.outcome=modified-merged, got %q", foundMod.Meta.Evidence.Outcome)
	}
	if len(foundMod.Meta.Evidence.RejectedProposals) == 0 || !strings.Contains(foundMod.Meta.Evidence.RejectedProposals[0], "staging Ingress resources") {
		t.Fatalf("expected superseded initial proposal in evidence.rejected_proposals, got %+v", foundMod.Meta.Evidence.RejectedProposals)
	}
	if foundMod.Meta.Guard == nil || !strings.Contains(foundMod.Meta.Guard.Rule, "staging and preprod") {
		t.Fatalf("expected human-corrected guard rule on %s, got %+v", prop1.RecordID, foundMod.Meta.Guard)
	}
	if !strings.Contains(foundMod.Body, "## Reviewer Modification Before Merge") {
		t.Fatalf("expected Reviewer Modification Before Merge section in body, got:\n%s", foundMod.Body)
	}
	if foundMod.Meta.Guard.Severity != "warn" {
		t.Fatalf("expected general reviewer rule to preserve severity=warn, got %q", foundMod.Meta.Guard.Severity)
	}

	// Scenario 4: edit-before-merge on an operational remediation PR (not a knowledge/ PR)
	// synthesizes a status:proposed record so a separate knowledge/ PR is opened for review.
	remModRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:          OutcomeSchema,
		Kind:            OutcomeModifiedMerged,
		RecordType:      RecordTypeLesson,
		Repo:            "acme/infra",
		RemediationPR:   "https://github.com/acme/infra/pull/274 (#274)",
		FindingID:       "payments/Deployment/checkout:HighLatency",
		Title:           "Reviewer adjusted checkout replica count to 4",
		Rule:            "Keep checkout replicas >= 4 during peak hours.",
		ReviewerReason:  "Reviewer bumped replicas from 3 to 4 before merging remediation PR.",
		InitialProposal: "replicas: 3",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome remediation modified-merged: %v", err)
	}
	if remModRes.Status != StatusProposed {
		t.Fatalf("expected remediation edit-before-merge to stay status=proposed until knowledge/ PR merges, got %s", remModRes.Status)
	}
	if remModRes.Branch == "" || !strings.Contains(remModRes.PRMarkdown, "severity: warn") {
		t.Fatalf("expected branch and PRMarkdown with severity: warn for remediation edit-before-merge, got branch=%q md=%s", remModRes.Branch, remModRes.PRMarkdown)
	}
}

func TestExternalDocRecordsAndRepoBoundary(t *testing.T) {
	repoDir := t.TempDir()
	adrDir := filepath.Join(repoDir, "docs", "adr")
	if err := os.MkdirAll(adrDir, 0o755); err != nil {
		t.Fatal(err)
	}
	adrText := "# ADR 12: Regional PostgreSQL Failover\n\n## Decision\nAlways keep a warm standby replica in a secondary zone.\n"
	if err := os.WriteFile(filepath.Join(adrDir, "0012-regional-postgres-failover.md"), []byte(adrText), 0o644); err != nil {
		t.Fatal(err)
	}
	runbookDir := filepath.Join(repoDir, "knowledge", "runbooks")
	if err := os.MkdirAll(runbookDir, 0o755); err != nil {
		t.Fatal(err)
	}
	runbookText := "---\ntype: runbook\ntitle: CoreDNS High Latency Triage\nstatus: active\n---\n\nCheck upstream DNS forwarder saturation and conntrack table usage.\n"
	if err := os.WriteFile(filepath.Join(runbookDir, "0003-coredns-latency.md"), []byte(runbookText), 0o644); err != nil {
		t.Fatal(err)
	}
	bpDir := filepath.Join(repoDir, "knowledge", "cluster-blueprints")
	if err := os.MkdirAll(bpDir, 0o755); err != nil {
		t.Fatal(err)
	}
	bpText := "# Regional GKE Production Blueprint\n\nUse regional control planes across 3 zones with Workload Identity enabled.\n"
	if err := os.WriteFile(filepath.Join(bpDir, "0001-regional-gke.md"), []byte(bpText), 0o644); err != nil {
		t.Fatal(err)
	}
	recs, err := LoadExternalDocRecords(repoDir)
	if err != nil {
		t.Fatalf("LoadExternalDocRecords: %v", err)
	}
	if len(recs) != 3 {
		t.Fatalf("expected 3 external/OKF records (ADR-0012, BP-0001, RUN-0003), got %+v", recs)
	}
	byID := map[string]Record{}
	for _, r := range recs {
		byID[r.Meta.ID] = r
	}
	if r, ok := byID["ADR-0012"]; !ok || r.Meta.Type != RecordTypeDecision || !strings.Contains(r.Meta.Title, "Regional PostgreSQL Failover") {
		t.Fatalf("unexpected ADR-0012 record: %+v", byID["ADR-0012"])
	}
	if r, ok := byID["RUN-0003"]; !ok || r.Meta.Type != RecordTypeRunbook || r.Meta.Title != "CoreDNS High Latency Triage" {
		t.Fatalf("unexpected RUN-0003 record: %+v", byID["RUN-0003"])
	}
	if r, ok := byID["BP-0001"]; !ok || r.Meta.Type != RecordTypeClusterBlueprint || r.Meta.Title != "Regional GKE Production Blueprint" {
		t.Fatalf("unexpected BP-0001 record: %+v", byID["BP-0001"])
	}

	badDeclaresYAML := "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\n  namespace: payments\nspec:\n  template:\n    spec:\n      containers:\n        - name: app\n          resources:\n            requests:\n              memory: 128Mi\n"
	decDir := filepath.Join(repoDir, "knowledge", "lessons")
	if err := os.MkdirAll(decDir, 0o755); err != nil {
		t.Fatal(err)
	}
	lesDeclares := "---\ntype: lesson\nid: LES-2026-015\ntitle: checkout OOM below 256Mi\nstatus: active\ndeclares:\n  - check: oom-kill\n    namespace: payments\n    object: Deployment/checkout\n    cluster: prod-eu\n    file: apps/payments/checkout.yaml\n    path: spec.template.spec.containers[name=app].resources.requests.memory\n    op: \">=\"\n    value: 256Mi\n    severity: block\n    rule: Keep checkout memory request at or above 256Mi.\nevidence:\n  pull_request: \"#31\"\n---\n\nKeep memory >= 256Mi.\n"
	if err := os.WriteFile(filepath.Join(decDir, "LES-2026-015-checkout-oom.md"), []byte(lesDeclares), 0o644); err != nil {
		t.Fatal(err)
	}
	loadedRecs, err := LoadRecords(repoDir)
	if err != nil || len(loadedRecs) != 1 {
		t.Fatalf("expected 1 loaded record from declares:, got %v (%+v)", err, loadedRecs)
	}
	repBlock := CheckChangedFiles(loadedRecs, map[string]*string{
		"apps/payments/checkout.yaml": &badDeclaresYAML,
	})
	if repBlock.Verdict != "block" || len(repBlock.Findings) != 1 || repBlock.Findings[0].ID != "LES-2026-015" {
		t.Fatalf("expected LES-2026-015 block finding from declares:, got %+v", repBlock)
	}

	// Verify custom .kube-agents/intent.yaml paths and DECISION_MEMORY_GITOPS_PRS=false proposed status.
	customRepo := t.TempDir()
	intentDir := filepath.Join(customRepo, ".kube-agents")
	if err := os.MkdirAll(intentDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(intentDir, "intent.yaml"), []byte("paths:\n  - ops-intent/decisions\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	customDecDir := filepath.Join(customRepo, "ops-intent", "decisions")
	if err := os.MkdirAll(customDecDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(customDecDir, "DEC-2026-099-test.md"), []byte(lesDeclares), 0o644); err != nil {
		t.Fatal(err)
	}
	customLoaded, err := LoadRecords(customRepo)
	if err != nil || len(customLoaded) != 1 {
		t.Fatalf("expected 1 record from custom .kube-agents/intent.yaml paths, got %v (%+v)", err, customLoaded)
	}

	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "false")
	curOptOut, err := NewCurator(CuratorConfig{
		RepoDir: customRepo,
		RepoID:  "acme/infra",
	})
	if err != nil {
		t.Fatal(err)
	}
	resOptOut, err := curOptOut.ProcessOutcome(context.Background(), OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeResolvedMerged,
		RecordID:   "LES-2026-088",
		RecordType: RecordTypeLesson,
		Repo:       "acme/infra",
		Title:      "Checkout OOM below 256Mi",
		Summary:    "Checkout OOM-kills below 256Mi.",
		Rule:       "Keep checkout memory request >= 256Mi.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome with DECISION_MEMORY_GITOPS_PRS=false: %v", err)
	}
	if resOptOut.Status != StatusProposed {
		t.Fatalf("expected status=proposed when DECISION_MEMORY_GITOPS_PRS=false, got %s", resOptOut.Status)
	}
}

func TestSyncRecordsPreservesProposedAndRejectedAndContextRepos(t *testing.T) {
	mockHS, hsSrv := newMockHindsightServer(t)
	hsClient := NewHindsightClient(hsSrv.URL, DefaultBankID)

	repoDir := t.TempDir()
	agentHome := t.TempDir()
	stateDir := t.TempDir()
	t.Setenv("PLATFORM_AGENT_HOME", agentHome)
	t.Setenv("GITOPS_STATE_PATH", filepath.Join(stateDir, "managed_repos"))

	ctxClone := filepath.Join(agentHome, "gitops", "lease-1", "acme__runbooks")
	if err := os.MkdirAll(filepath.Join(ctxClone, "knowledge", "runbook"), 0o755); err != nil {
		t.Fatal(err)
	}
	rbText := "---\ntype: runbook\ntitle: Etcd Defrag Procedure\nstatus: active\n---\n\nDefragment one member at a time.\n"
	if err := os.WriteFile(filepath.Join(ctxClone, "knowledge", "runbook", "0007-etcd-defrag.md"), []byte(rbText), 0o644); err != nil {
		t.Fatal(err)
	}
	ctxReposJSON := `[{"type":"github","url":"https://github.com/acme/runbooks.git"}]`
	if err := os.WriteFile(filepath.Join(stateDir, "context_repos"), []byte(ctxReposJSON), 0o644); err != nil {
		t.Fatal(err)
	}

	// Seed singular knowledge/decision/DEC-2026-001.md in Git.
	activeRec := Record{
		Meta: RecordMeta{
			Type:   RecordTypeDecision,
			ID:     "DEC-2026-001",
			Title:  "Regional clusters in prod",
			Status: StatusActive,
			Date:   "2026-11-01",
			Scope: RecordScope{
				Paths: []string{"clusters/**"},
			},
			Evidence: RecordEvidence{
				PullRequest: "#10",
			},
		},
		Body: "Keep regional GKE control planes.",
	}
	if err := SaveRecord(repoDir, &activeRec); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(activeRec.Path, "knowledge/decision/") {
		t.Fatalf("expected singular knowledge/decision/ path by default, got %s", activeRec.Path)
	}

	// Pre-populate Hindsight with:
	// 1. Stale active record DEC-2026-099 (removed from Git -> MUST be deleted)
	// 2. Pending proposed record LES-2026-050 (not in Git yet -> MUST survive)
	// 3. Closed rejected record DEC-2026-051 (never merged to Git -> MUST survive)
	ctx := context.Background()
	_ = hsClient.EnsureBank(ctx)
	for _, seed := range []Record{
		{
			Meta: RecordMeta{
				Type: RecordTypeDecision, ID: "DEC-2026-099", Title: "Removed active rule", Status: StatusActive,
			},
			Body: "Removed from Git.",
		},
		{
			Meta: RecordMeta{
				Type: RecordTypeLesson, ID: "LES-2026-050", Title: "Pending proposal", Status: StatusProposed,
			},
			Body: "Awaiting PR review.",
		},
		{
			Meta: RecordMeta{
				Type: RecordTypeDecision, ID: "DEC-2026-051", Title: "Rejected proposal", Status: StatusRejected,
			},
			Body: "Closed by reviewer.",
		},
	} {
		if err := hsClient.RetainRecord(ctx, "acme/infra", seed); err != nil {
			t.Fatal(err)
		}
	}

	upserted, deleted, err := SyncRecordsToHindsight(ctx, hsClient, "acme/infra", repoDir)
	if err != nil {
		t.Fatalf("SyncRecordsToHindsight: %v", err)
	}
	if len(upserted) != 2 {
		t.Fatalf("expected 2 upserted records (DEC-2026-001 + RUN-0007 from context_repos), got %v", upserted)
	}
	if len(deleted) != 1 || deleted[0] != "okf:acme/infra:DEC-2026-099" {
		t.Fatalf("expected only stale active DEC-2026-099 to be deleted, got %v", deleted)
	}
	mockHS.mu.Lock()
	defer mockHS.mu.Unlock()
	if _, ok := mockHS.docs["okf:acme/infra:LES-2026-050"]; !ok {
		t.Fatalf("expected status:proposed LES-2026-050 to survive Hindsight index sync")
	}
	if _, ok := mockHS.docs["okf:acme/infra:DEC-2026-051"]; !ok {
		t.Fatalf("expected status:rejected DEC-2026-051 to survive Hindsight index sync")
	}
	if _, ok := mockHS.docs["okf:acme/infra:RUN-0007"]; !ok {
		t.Fatalf("expected RUN-0007 from context_repos to be indexed into Hindsight")
	}
}

func TestMatchGlobMultiDocGuardLLMAndSecurityControls(t *testing.T) {
	if !MatchGlob("clusters/**/checkout/*.yaml", "clusters/prod/eu-west1/checkout/deployment.yaml") {
		t.Fatal("expected ** glob to match multi-segment path")
	}
	if !MatchGlob("clusters/**/deployment.yaml", "clusters/deployment.yaml") {
		t.Fatal("expected ** glob to match zero intermediate segments")
	}
	if MatchGlob("clusters/*/deployment.yaml", "clusters/prod/eu/deployment.yaml") {
		t.Fatal("expected single * glob not to cross slash boundaries")
	}

	// Multi-document YAML: Service + compliant Deployment + non-compliant 2nd Deployment missing CPU request.
	multiDocYAML := `apiVersion: v1
kind: Service
metadata:
  name: checkout
spec:
  ports:
    - port: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-canary
spec:
  template:
    spec:
      containers:
        - name: app
          resources:
            requests:
              cpu: 500m
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout-primary
spec:
  template:
    spec:
      containers:
        - name: app
          resources:
            requests:
              memory: 256Mi
`
	recMulti := Record{
		Meta: RecordMeta{
			Type:   RecordTypeDecision,
			ID:     "DEC-2026-070",
			Title:  "Require CPU request >= 400m on all Deployments",
			Status: StatusActive,
			Scope: RecordScope{
				Paths:     []string{"apps/**/*.yaml"},
				Resources: []string{"Deployment/*"},
			},
			Guard: &RecordGuard{
				Rule:     "Every Deployment app container must request cpu >= 400m.",
				Severity: "block",
				Expect: &GuardExpect{
					File:  "apps/**/*.yaml",
					Path:  "spec.template.spec.containers[name=app].resources.requests.cpu",
					Op:    ">=",
					Value: "400m",
				},
			},
		},
	}
	repMulti := CheckChangedFiles([]Record{recMulti}, map[string]*string{
		"apps/checkout/all.yaml": &multiDocYAML,
	})
	if repMulti.Verdict != "block" {
		t.Fatalf("expected missing CPU on 2nd Deployment in multi-doc YAML to block, got %+v", repMulti)
	}

	// Verify active narrow cluster record suppresses wildcard fleet record only on files covered by the narrow record.
	narrowActive := Record{
		Meta: RecordMeta{
			Type:   RecordTypeLesson,
			ID:     "LES-2026-071",
			Title:  "Dev cluster allows lower CPU floor",
			Status: StatusActive,
			Scope: RecordScope{
				Paths:    []string{"apps/dev/*.yaml"},
				Clusters: []string{"dev-01"},
			},
			Guard: &RecordGuard{
				Rule:     "Dev allows cpu >= 100m.",
				Severity: "warn",
				Expect: &GuardExpect{
					File:  "apps/dev/*.yaml",
					Path:  "spec.template.spec.containers[name=app].resources.requests.cpu",
					Op:    ">=",
					Value: "100m",
				},
			},
			Evidence: RecordEvidence{PullRequest: "#71"},
		},
	}
	fleetWide := recMulti
	fleetWide.Meta.Scope.Clusters = []string{"*"}
	devDoc := "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  template:\n    spec:\n      containers:\n        - name: app\n          resources:\n            requests:\n              cpu: 200m\n"
	prodDoc := devDoc
	repBoth := CheckChangedFiles([]Record{fleetWide, narrowActive}, map[string]*string{
		"apps/dev/web.yaml":  &devDoc,
		"apps/prod/web.yaml": &prodDoc,
	})
	if repBoth.Verdict != "block" {
		t.Fatalf("expected fleet-wide rule to still block apps/prod/web.yaml when narrow rule only covers apps/dev/*, got %+v", repBoth)
	}

	// Verify LLMConfig.Synthesize and LLMConfig.JudgeDiff (clamping severity and dropping hallucinated IDs).
	callNum := 0
	llmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		callNum++
		w.Header().Set("Content-Type", "application/json")
		if callNum == 1 {
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"title\":\"Synthesized rule\",\"context\":\"ctx\",\"decision\":\"dec\",\"consequences\":\"cons\"}"}}]}`))
			return
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"violations\":[{\"id\":\"DEC-2026-080\",\"severity\":\"block\",\"rule\":\"No Friday deploys\",\"reason\":\"Diff enables Friday sync\"},{\"id\":\"DEC-9999-999\",\"severity\":\"block\",\"rule\":\"Fake\",\"reason\":\"Hallucinated\"}]}"}}]}`))
	}))
	defer llmSrv.Close()

	llmCfg := LLMConfig{BaseURL: llmSrv.URL, APIKey: "test-key", Model: "test-model"}
	synthOut := llmCfg.Synthesize(context.Background(), RecordTypeDecision, OutcomeEvent{Title: "Input"})
	if synthOut == nil || synthOut.Title != "Synthesized rule" {
		t.Fatalf("unexpected Synthesize output: %+v", synthOut)
	}
	nlRule := Record{
		Meta: RecordMeta{
			Type:   RecordTypeDecision,
			ID:     "DEC-2026-080",
			Title:  "Avoid Friday maintenance windows",
			Status: StatusActive,
			Scope:  RecordScope{Paths: []string{"clusters/**"}},
			Guard:  &RecordGuard{Rule: "Avoid Friday maintenance windows.", Severity: "warn"},
		},
	}
	violations := llmCfg.JudgeDiff(context.Background(), []Record{nlRule}, "+schedule: Friday")
	if len(violations) != 1 || violations[0].ID != "DEC-2026-080" || violations[0].Severity != "warn" {
		t.Fatalf("expected JudgeDiff to drop hallucinated ID and preserve declared warn severity, got %+v", violations)
	}

	// Verify Curator lifecycle: OutcomePRMergedPersists, OutcomeRetired, Precedence Rule 3 on same check,
	// and rejection of spoofed/redirected JetStream deliveries.
	curDir := t.TempDir()
	cur, err := NewCurator(CuratorConfig{RepoDir: curDir, RepoID: "acme/infra"})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	persistsRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:          OutcomePRMergedPersists,
		RecordID:      "LES-2026-090",
		RemediationPR: "#90",
		CheckID:       "pdb-check",
		Summary:       "PDB violation persisted after merge.",
	}, "")
	if err != nil || persistsRes.Status != StatusProposed {
		t.Fatalf("expected OutcomePRMergedPersists -> proposed, got %+v (%v)", persistsRes, err)
	}
	retiredRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:           OutcomeRetired,
		RecordID:       "LES-2026-090",
		ReviewerReason: "Closed as stale duplicate.",
	}, "")
	if err != nil || retiredRes.Status != StatusRetired {
		t.Fatalf("expected OutcomeRetired -> retired, got %+v (%v)", retiredRes, err)
	}

	// Precedence Rule 3: rejected record on check "pdb-min" IS superseded when active record on check "pdb-min" merges.
	if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeRefused,
		RecordID:   "DEC-2026-091",
		RecordType: RecordTypeDecision,
		CheckID:    "pdb-min",
		Scope:      &RecordScope{Paths: []string{"clusters/**/pdb.yaml"}, Checks: []string{"pdb-min"}},
	}, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeApproved,
		RecordID:   "DEC-2026-092",
		RecordType: RecordTypeDecision,
		CheckID:    "pdb-min",
		Title:      "Approved PDB floor",
		Scope:      &RecordScope{Paths: []string{"clusters/**/pdb.yaml"}, Checks: []string{"pdb-min"}},
	}, ""); err != nil {
		t.Fatal(err)
	}
	allRecs, _ := LoadRecords(curDir)
	for _, r := range allRecs {
		if r.Meta.ID == "DEC-2026-091" && r.Meta.Status != StatusSuperseded {
			t.Fatalf("expected DEC-2026-091 on same check pdb-min to be superseded by DEC-2026-092, got %s", r.Meta.Status)
		}
	}

	// Redirected JetStream delivery ($JS.ACK reply or Nats-Stream header) must be ignored by outcome handler.
	forgedPayload, _ := json.Marshal(OutcomeEvent{
		Kind:     OutcomeApproved,
		RecordID: "DEC-2026-099",
		Title:    "Forged via MSG.NEXT",
	})
	cur.handleCuratorOutcomeMsg(ctx, &nats.Msg{
		Subject: SubjectCuratorOutcomes,
		Reply:   "$JS.ACK.TOPICS-JOURNAL.attacker.1",
		Data:    forgedPayload,
	}, SubjectCuratorOutcomes)
	hdr := nats.Header{}
	hdr.Set("Nats-Stream", "TOPICS-JOURNAL")
	cur.handleCuratorOutcomeMsg(ctx, &nats.Msg{
		Subject: SubjectCuratorOutcomes,
		Header:  hdr,
		Data:    forgedPayload,
	}, SubjectCuratorOutcomes)
	afterForge, _ := LoadRecords(curDir)
	for _, r := range afterForge {
		if r.Meta.ID == "DEC-2026-099" {
			t.Fatal("redirected JetStream delivery must be rejected on SubjectCuratorOutcomes")
		}
	}
}
