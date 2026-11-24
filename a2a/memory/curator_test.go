package memory

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
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

func commitRecordInGit(t *testing.T, repoDir string, rec Record) {
	t.Helper()
	if rec.Meta.Status != StatusRetired {
		rec.Meta.Status = StatusActive
	}
	if len(rec.Meta.Scope.Paths) == 0 && len(rec.Meta.Scope.Resources) == 0 && len(rec.Meta.Scope.Checks) == 0 {
		rec.Meta.Scope.Paths = []string{"**"}
	}
	if rec.Meta.Type == RecordTypeLesson && rec.Meta.Evidence.PullRequest == "" {
		rec.Meta.Evidence.PullRequest = "#1"
	}
	if strings.TrimSpace(rec.Body) == "" {
		rec.Body = "## Context\n\nMerged in Git.\n"
	}
	vDir := filepath.Join(repoDir, verifiedGitSubdir)
	if err := SaveRecord(vDir, &rec); err != nil {
		t.Fatalf("SaveRecord for git commit: %v", err)
	}
}

func promoteExistingRecordInGit(t *testing.T, repoDir, id, prRef string) {
	t.Helper()
	recs, err := LoadRecords(repoDir)
	if err != nil {
		t.Fatalf("LoadRecords(%s): %v", repoDir, err)
	}
	for _, r := range recs {
		if r.Meta.ID == id {
			if prRef != "" {
				r.Meta.Evidence.PullRequest = prRef
			}
			commitRecordInGit(t, repoDir, r)
			return
		}
	}
	t.Fatalf("record %s not found in %s", id, repoDir)
}

func TestCuratorEndToEndNATSFlow(t *testing.T) {
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
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
		VerifiedRepoDir:     filepath.Join(repoDir, verifiedGitSubdir),
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

	// Merging the fleet-wide knowledge/ PR for DEC-2026-005 in Git promotes DEC-2026-005 to status:active
	// and supersedes LES-2026-001 and LES-2026-002 while keeping unrelated rejected DEC-2026-004 intact!
	promoteExistingRecordInGit(t, repoDir, "DEC-2026-005", "https://github.com/acme/infra/pull/289 (#289)")
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

	// Publish a new active decision merged in Git and verify that the NATS query cache is invalidated automatically.
	commitRecordInGit(t, repoDir, Record{
		Meta: RecordMeta{
			Type:     RecordTypeDecision,
			ID:       "DEC-2026-006",
			Title:    "Checkout PDB minimum available replicas",
			Status:   StatusActive,
			Date:     "2026-11-12",
			Scope:    RecordScope{Paths: []string{"clusters/**/checkout/pdb.yaml"}},
			Guard:    &RecordGuard{Rule: "Keep checkout PDB minAvailable >= 2.", Severity: "warn"},
			Evidence: RecordEvidence{PullRequest: "https://github.com/acme/infra/pull/290 (#290)"},
		},
		Body: "## Context\n\nRequire minAvailable >= 2 on checkout PodDisruptionBudget.\n",
	})
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
	// transitions in-place to status:active once merged in Git with the human-corrected guard
	// and records the superseded initial proposal in evidence.rejected_proposals.
	recsBeforeMod, _ := LoadRecords(repoDir)
	for _, r := range recsBeforeMod {
		if r.Meta.ID == prop1.RecordID {
			r.Meta.Guard = &RecordGuard{
				Rule:     "All staging and preprod Ingress resources must include networking.gke.io/managed-certificates.",
				Severity: "warn",
			}
			r.Meta.Evidence.PullRequest = "https://github.com/acme/infra/pull/273 (#273)"
			r.Body += "\n## Reviewer Modification Before Merge\n\nReviewer expanded scope from staging-only to staging and preprod before merging.\n"
			commitRecordInGit(t, repoDir, r)
			break
		}
	}
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
	cur, err := NewCurator(CuratorConfig{
		RepoDir:         curDir,
		VerifiedRepoDir: filepath.Join(curDir, verifiedGitSubdir),
		RepoID:          "acme/infra",
	})
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
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
	if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeRefused,
		RecordID:   "DEC-2026-091",
		RecordType: RecordTypeDecision,
		CheckID:    "pdb-min",
		Scope:      &RecordScope{Paths: []string{"clusters/**/pdb.yaml"}, Checks: []string{"pdb-min"}},
	}, ""); err != nil {
		t.Fatal(err)
	}
	commitRecordInGit(t, curDir, Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-092",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Approved PDB floor",
			Scope:  RecordScope{Paths: []string{"clusters/**/pdb.yaml"}, Checks: []string{"pdb-min"}},
		},
		Body: "## Context\n\nApproved PDB floor.\n",
	})
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

	seqHdr := nats.Header{}
	seqHdr.Set("Nats-Sequence", "42")
	cur.handleCuratorOutcomeMsg(ctx, &nats.Msg{
		Subject: SubjectCuratorOutcomes,
		Header:  seqHdr,
		Data:    forgedPayload,
	}, SubjectCuratorOutcomes)
	afterSeqForge, _ := LoadRecords(curDir)
	for _, r := range afterSeqForge {
		if r.Meta.ID == "DEC-2026-099" {
			t.Fatal("Nats-Sequence header delivery must be rejected on SubjectCuratorOutcomes")
		}
	}
}

func TestOutcomeProposedNeverOverwritesActiveOrConflictingProposed(t *testing.T) {
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
	ctx := context.Background()
	repoDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoDir, "clusters", "prod-eu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "clusters", "prod-eu", "pdb.yaml"), []byte("apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: web\nspec:\n  minAvailable: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cur, err := NewCurator(CuratorConfig{
		RepoDir:         repoDir,
		VerifiedRepoDir: filepath.Join(repoDir, verifiedGitSubdir),
		RepoID:          "acme/gitops",
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. Seed an active decision DEC-2026-001 verified in Git HEAD.
	if err := os.MkdirAll(filepath.Join(repoDir, "apps", "checkout"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "apps", "checkout", "deploy.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\nspec:\n  replicas: 2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	commitRecordInGit(t, repoDir, Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-001",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Keep checkout replicas at two or higher",
			Scope:  RecordScope{Paths: []string{"apps/checkout/*.yaml"}},
			Guard: &RecordGuard{
				Rule:     "Keep checkout replicas >= 2",
				Severity: "block",
				Expect:   &GuardExpect{File: "apps/checkout/*.yaml", Path: "spec.replicas", Op: ">=", Value: 2},
			},
			Evidence: RecordEvidence{PullRequest: "https://github.com/acme/gitops/pull/10 (#10)"},
		},
	})
	_, err = cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeApproved,
		RecordID:   "DEC-2026-001",
		RecordType: RecordTypeDecision,
		Title:      "Keep checkout replicas at two or higher",
		Rule:       "Keep checkout replicas >= 2",
		Repo:       "acme/gitops",
		PRNumber:   10,
		Scope:      &RecordScope{Paths: []string{"apps/checkout/*.yaml"}},
		Guard: &RecordGuard{
			Rule:     "Keep checkout replicas >= 2",
			Severity: "block",
			Expect:   &GuardExpect{File: "apps/checkout/*.yaml", Path: "spec.replicas", Op: ">=", Value: 2},
		},
	}, "")
	if err != nil {
		t.Fatal(err)
	}

	// Explicit OutcomeProposed targeting DEC-2026-001 must not demote or overwrite it.
	resActiveExplicit, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordID:   "DEC-2026-001",
		RecordType: RecordTypeDecision,
		Title:      "Overwrite attempt",
		Rule:       "Allow 0 replicas",
		Repo:       "acme/gitops",
		PRNumber:   99,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if resActiveExplicit.Status != StatusActive {
		t.Fatalf("explicit OutcomeProposed demoted active record: %+v", resActiveExplicit)
	}

	// Fuzzy OutcomeProposed matching DEC-2026-001's title must allocate a fresh ID rather than demoting DEC-2026-001.
	resActiveFuzzy, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Title:      "Keep checkout replicas at two or higher",
		Rule:       "Keep checkout replicas >= 2",
		Repo:       "acme/gitops",
		PRNumber:   105,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if resActiveFuzzy.RecordID == "DEC-2026-001" || resActiveFuzzy.Status != StatusProposed {
		t.Fatalf("fuzzy OutcomeProposed collided with active DEC-2026-001: %+v", resActiveFuzzy)
	}

	// 2. Seed a PR-less proposed record DEC-2026-010 with a guard and body.
	_, err = cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordID:   "DEC-2026-010",
		RecordType: RecordTypeDecision,
		Title:      "Require memory limits on payments",
		Summary:    "Initial synthesized body for payments.",
		Rule:       "Set memory limits on payments",
		Scope:      &RecordScope{Paths: []string{"apps/payments/*.yaml"}},
		Guard: &RecordGuard{
			Rule:     "Set memory limits on payments",
			Severity: "block",
			Expect:   &GuardExpect{File: "apps/payments/*.yaml", Path: "spec.limits.memory", Op: "exists"},
		},
	}, "")
	if err != nil {
		t.Fatal(err)
	}

	// Attaching PR #41 to PR-less DEC-2026-010 must preserve its Guard and Body.
	_, err = cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:     OutcomeProposed,
		RecordID: "DEC-2026-010",
		Repo:     "acme/gitops",
		PRNumber: 41,
	}, "")
	if err != nil {
		t.Fatal(err)
	}

	// A conflicting explicit OutcomeProposed for DEC-2026-010 with PR #42 must not overwrite PR #41.
	_, err = cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:     OutcomeProposed,
		RecordID: "DEC-2026-010",
		Repo:     "acme/gitops",
		PRNumber: 42,
		Title:    "Hijack PR link",
	}, "")
	if err != nil {
		t.Fatal(err)
	}

	// A fuzzy OutcomeProposed with the same title but PR #43 must allocate a fresh ID.
	resConflictFuzzy, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Title:      "Require memory limits on payments",
		Rule:       "Set memory limits on payments",
		Repo:       "acme/gitops",
		PRNumber:   43,
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if resConflictFuzzy.RecordID == "DEC-2026-010" {
		t.Fatalf("expected fresh ID for conflicting PR #43, got %s", resConflictFuzzy.RecordID)
	}

	recs, err := LoadRecords(repoDir)
	if err != nil {
		t.Fatal(err)
	}
	byID := map[string]Record{}
	for _, r := range recs {
		byID[r.Meta.ID] = r
	}
	if got := byID["DEC-2026-001"]; got.Meta.Status != StatusActive || got.Meta.Evidence.PullRequest != "https://github.com/acme/gitops/pull/10 (#10)" {
		t.Fatalf("DEC-2026-001 mutated: %+v", got.Meta)
	}
	dec10 := byID["DEC-2026-010"]
	if dec10.Meta.Evidence.PullRequest != "https://github.com/acme/gitops/pull/41 (#41)" {
		t.Fatalf("DEC-2026-010 PR = %q, want https://github.com/acme/gitops/pull/41 (#41)", dec10.Meta.Evidence.PullRequest)
	}
	if dec10.Meta.Guard == nil || dec10.Meta.Guard.Expect == nil || !strings.Contains(dec10.Body, "Initial synthesized body") {
		t.Fatalf("DEC-2026-010 guard or body wiped when attaching PR: meta=%+v body=%q", dec10.Meta, dec10.Body)
	}
}

func TestHandleQueryEnforcesClusterPathMaxResultsAndBank(t *testing.T) {
	ctx := context.Background()
	repoDir := t.TempDir()

	var mu sync.Mutex
	banksQueried := []string{}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/default/banks/kube-agents-decisions/memories/recall", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		banksQueried = append(banksQueried, "kube-agents-decisions")
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{
			"results": []HindsightRecallResult{
				{ID: "LES-2026-001", DocumentID: "okf:acme/gitops:LES-2026-001", Text: "EU checkout lesson", Tags: []string{"scope:shared", "cluster:prod-eu"}},
				{ID: "LES-2026-002", DocumentID: "okf:acme/gitops:LES-2026-002", Text: "US payments lesson", Tags: []string{"scope:shared", "cluster:prod-us"}},
				{ID: "DEC-2026-003", DocumentID: "okf:acme/gitops:DEC-2026-003", Text: "Fleet-wide checkout rule", Tags: []string{"scope:shared", "cluster:*"}},
			},
		})
	})
	mux.HandleFunc("/v1/default/banks/kube-agents-memory/memories/recall", func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		banksQueried = append(banksQueried, "kube-agents-memory")
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{
			"results": []HindsightRecallResult{
				{ID: "conv-1", Text: "Team prefers canary rollouts on Tuesdays", Tags: []string{"scope:shared"}},
			},
		})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	cur, err := NewCurator(CuratorConfig{
		RepoDir:   repoDir,
		RepoID:    "acme/gitops",
		Hindsight: NewHindsightClient(srv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, rec := range []Record{
		{
			Meta: RecordMeta{
				ID: "LES-2026-001", Type: RecordTypeLesson, Status: StatusActive,
				Title: "EU checkout lesson", Date: "2026-11-11",
				Scope: RecordScope{Clusters: []string{"prod-eu"}, Paths: []string{"apps/checkout/**"}},
			},
			Body: "## Summary\nEU checkout lesson.\n",
		},
		{
			Meta: RecordMeta{
				ID: "LES-2026-002", Type: RecordTypeLesson, Status: StatusActive,
				Title: "US payments lesson", Date: "2026-11-11",
				Scope: RecordScope{Clusters: []string{"prod-us"}, Paths: []string{"apps/payments/**"}},
			},
			Body: "## Summary\nUS payments lesson.\n",
		},
		{
			Meta: RecordMeta{
				ID: "DEC-2026-003", Type: RecordTypeDecision, Status: StatusActive,
				Title: "Fleet-wide checkout rule", Date: "2026-11-11",
				Scope: RecordScope{Clusters: []string{"*"}, Paths: []string{"apps/checkout/**"}},
			},
			Body: "## Summary\nFleet-wide checkout rule.\n",
		},
	} {
		rCopy := rec
		if err := SaveRecord(repoDir, &rCopy); err != nil {
			t.Fatal(err)
		}
	}

	// 1. Cluster + Path filtering drops prod-us payments hit.
	r1 := cur.HandleQuery(ctx, MemoryQueryRequest{
		Query:      "lesson",
		Cluster:    "prod-eu",
		Path:       "apps/checkout/deployment.yaml",
		MaxResults: 1,
	})
	if len(r1.Results) != 1 {
		t.Fatalf("MaxResults=1 returned %d results: %+v", len(r1.Results), r1.Results)
	}

	// 2. Cache key includes MaxResults so MaxResults=5 is not served the truncated MaxResults=1 entry.
	r2 := cur.HandleQuery(ctx, MemoryQueryRequest{
		Query:      "lesson",
		Cluster:    "prod-eu",
		Path:       "apps/checkout/deployment.yaml",
		MaxResults: 5,
	})
	if len(r2.Results) != 2 {
		t.Fatalf("expected 2 filtered hits (LES-2026-001 and DEC-2026-003), got %d: %+v", len(r2.Results), r2.Results)
	}
	for _, hit := range r2.Results {
		if hit.ID == "LES-2026-002" {
			t.Fatalf("HandleQuery leaked out-of-scope cluster/path record LES-2026-002: %+v", r2.Results)
		}
	}

	// 3. Disallowed Bank (kube-agents-memory) and user:* tags are rejected by HandleQuery's allowlist.
	rBank := cur.HandleQuery(ctx, MemoryQueryRequest{
		Query: "canary rollouts",
		Bank:  "kube-agents-memory",
		Tags:  []string{"scope:shared"},
	})
	if rBank.Error == "" || len(rBank.Results) != 0 {
		t.Fatalf("expected kube-agents-memory bank query to be rejected, got %+v", rBank)
	}
	rUserTag := cur.HandleQuery(ctx, MemoryQueryRequest{
		Query: "canary rollouts",
		Tags:  []string{"user:alice"},
	})
	if rUserTag.Error == "" || len(rUserTag.Results) != 0 {
		t.Fatalf("expected user:alice tag query to be rejected, got %+v", rUserTag)
	}
	mu.Lock()
	for _, b := range banksQueried {
		if b == "kube-agents-memory" {
			mu.Unlock()
			t.Fatalf("Curator queried disallowed bank kube-agents-memory")
		}
	}
	mu.Unlock()

	// 4. Redirect responses from Hindsight are refused (ErrUseLastResponse).
	redirSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://169.254.169.254/latest/meta-data", http.StatusFound)
	}))
	defer redirSrv.Close()
	redirClient := NewHindsightClient(redirSrv.URL, DefaultBankID)
	if _, err := redirClient.RecallBank(ctx, DefaultBankID, "test", nil, ""); err == nil {
		t.Fatal("expected HindsightClient to reject HTTP 302 redirect")
	}
}

func TestPublishOutcomeWithMetaSurfacesAppErrorWithoutSecondSubjectRetry(t *testing.T) {
	natsURL := startEmbeddedJetStream(t)
	ctx := context.Background()

	serverClient, err := lib.Connect(ctx, natsURL, lib.WithName("test-responder"))
	if err != nil {
		t.Fatal(err)
	}
	defer serverClient.Close()

	callerClient, err := lib.Connect(ctx, natsURL, lib.WithName("test-caller"))
	if err != nil {
		t.Fatal(err)
	}
	defer callerClient.Close()

	var outcomesCalls int
	var mu sync.Mutex
	var gotFallbackCorr string

	qSub, err := serverClient.SubscribeCore(SubjectMemoryQuery, func(msg *nats.Msg) {
		raw, _ := json.Marshal(MemoryQueryResponse{
			Schema: MemoryQueryReplySchema,
			Error:  "application error: invalid transition",
		})
		_ = msg.Respond(raw)
	})
	if err != nil {
		t.Fatal(err)
	}
	oSub, err := serverClient.SubscribeCore(SubjectCuratorOutcomes, func(msg *nats.Msg) {
		mu.Lock()
		outcomesCalls++
		if msg.Header != nil {
			gotFallbackCorr = msg.Header.Get("Nats-Correlation-Id")
		}
		mu.Unlock()
		raw, _ := json.Marshal(MemoryQueryResponse{
			Schema:  MemoryQueryReplySchema,
			Outcome: &OutcomeProcessResult{RecordID: "DEC-2026-077", Status: StatusActive},
		})
		_ = msg.Respond(raw)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer oSub.Stop()

	// When SubjectMemoryQuery replies with an application error, PublishOutcomeWithMeta must NOT retry on SubjectCuratorOutcomes.
	_, subj, err := PublishOutcomeWithMeta(ctx, callerClient, OutcomeEvent{
		Kind:     OutcomeApproved,
		RecordID: "DEC-2026-077",
	}, "corr-app-err")
	if err == nil || !strings.Contains(err.Error(), "invalid transition") {
		t.Fatalf("expected application error to surface, got err=%v", err)
	}
	if subj != SubjectMemoryQuery {
		t.Fatalf("expected subject %q, got %q", SubjectMemoryQuery, subj)
	}
	mu.Lock()
	if outcomesCalls != 0 {
		mu.Unlock()
		t.Fatalf("SubjectCuratorOutcomes called %d times on application error, want 0", outcomesCalls)
	}
	mu.Unlock()

	// When SubjectMemoryQuery has no responders, PublishOutcomeWithMeta falls back to SubjectCuratorOutcomes with CorrelationID.
	qSub.Stop()
	if err := serverClient.Conn().FlushTimeout(2 * time.Second); err != nil {
		t.Fatalf("flush serverClient after qSub.Stop: %v", err)
	}
	fallbackCtx, fallbackCancel := context.WithTimeout(ctx, 5*time.Second)
	defer fallbackCancel()
	res, fallbackSubj, err := PublishOutcomeWithMeta(fallbackCtx, callerClient, OutcomeEvent{
		Kind:     OutcomeApproved,
		RecordID: "DEC-2026-077",
	}, "corr-fallback-1")
	if err != nil {
		t.Fatalf("expected fallback to SubjectCuratorOutcomes to succeed, got %v", err)
	}
	if fallbackSubj != SubjectCuratorOutcomes || res.RecordID != "DEC-2026-077" {
		t.Fatalf("unexpected fallback result: subj=%q res=%+v", fallbackSubj, res)
	}
	mu.Lock()
	defer mu.Unlock()
	if gotFallbackCorr != "corr-fallback-1" {
		t.Fatalf("expected Nats-Correlation-Id=corr-fallback-1, got %q", gotFallbackCorr)
	}
}

func TestCheckChangedFilesResourceScopedPrecedenceMultiDocYAML(t *testing.T) {
	records := []Record{
		{
			Path: "knowledge/decisions/DEC-2026-020.md",
			Meta: RecordMeta{
				ID: "DEC-2026-020", Type: RecordTypeDecision, Status: StatusActive,
				Title: "Fleet Deployment replica floor", Date: "2026-11-01",
				Scope: RecordScope{Clusters: []string{"*"}, Paths: []string{"clusters/**/*.yaml"}},
				Guard: &RecordGuard{
					Rule:     "All Deployments must run at least 2 replicas",
					Severity: "block",
					Expect:   &GuardExpect{File: "clusters/**/*.yaml", Path: "spec.replicas", Op: ">=", Value: 2},
				},
			},
		},
		{
			Path: "knowledge/lessons/LES-2026-021.md",
			Meta: RecordMeta{
				ID: "LES-2026-021", Type: RecordTypeLesson, Status: StatusActive,
				Title: "Checkout single-replica exception on prod-eu", Date: "2026-11-10",
				Scope: RecordScope{
					Clusters:  []string{"prod-eu"},
					Paths:     []string{"clusters/prod-eu/*.yaml"},
					Resources: []string{"Deployment/*/checkout"},
				},
				Guard: &RecordGuard{
					Rule:     "Checkout on prod-eu may run 1 replica",
					Severity: "warn",
					Expect:   &GuardExpect{File: "clusters/prod-eu/*.yaml", Path: "spec.replicas", Op: ">=", Value: 1},
				},
			},
		},
	}

	multiDoc := strings.Join([]string{
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\n  namespace: shop\nspec:\n  replicas: 1",
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n  namespace: shop\nspec:\n  replicas: 1",
	}, "\n---\n")

	report := CheckChangedFiles(records, map[string]*string{
		"clusters/prod-eu/workloads.yaml": &multiDoc,
	})
	if report.Verdict != "block" {
		t.Fatalf("expected fleet DEC-2026-020 to still block uncovered Deployment/payments in multi-doc YAML, got verdict=%q findings=%+v", report.Verdict, report.Findings)
	}

	// If payments satisfies replicas >= 2 while checkout has replicas: 1, the narrow checkout lesson exempts checkout and report passes!
	compliantPayments := strings.Join([]string{
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: checkout\n  namespace: shop\nspec:\n  replicas: 1",
		"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments\n  namespace: shop\nspec:\n  replicas: 2",
	}, "\n---\n")
	reportPass := CheckChangedFiles(records, map[string]*string{
		"clusters/prod-eu/workloads.yaml": &compliantPayments,
	})
	if reportPass.Verdict == "block" {
		t.Fatalf("expected narrow checkout lesson to exempt Deployment/checkout when payments has replicas=2, got findings=%+v", reportPass.Findings)
	}
}

func TestLifecycleBuildersMatchFormattedPRRefAndCheckScopedGraduation(t *testing.T) {
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
	ctx := context.Background()

	// Mock LLM server returning a synthesized guard for check-scoped graduation.
	llmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []map[string]any{{
				"message": map[string]any{
					"content": `{"title":"Enforce PDB minAvailable across fleet","context":"Two clusters suffered drain outages.","decision":"Keep minAvailable >= 1 on PodDisruptionBudgets.","consequences":"Prevents voluntary drain outages.","guard":{"rule":"Keep minAvailable >= 1","severity":"block","expect":{"file":"clusters/**/pdb.yaml","path":"spec.minAvailable","op":">=","value":1}}}`,
				},
			}},
		})
	}))
	defer llmSrv.Close()

	repoDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoDir, "clusters", "prod-eu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "clusters", "prod-eu", "pdb.yaml"), []byte("apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: web\nspec:\n  minAvailable: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cur, err := NewCurator(CuratorConfig{
		RepoDir:             repoDir,
		VerifiedRepoDir:     filepath.Join(repoDir, verifiedGitSubdir),
		RepoID:              "acme/gitops",
		RecurrenceThreshold: 2,
		LLM: LLMConfig{
			BaseURL: llmSrv.URL,
			Model:   "test-model",
			Client:  &http.Client{Timeout: 5 * time.Second},
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	// Thread 8: Propose with Repo + PRNumber, then transition via Repo + PRNumber with empty RecordID and PullRequest.
	prop, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Repo:       "acme/gitops",
		PRNumber:   77,
		Title:      "Pin PDB minAvailable",
		Rule:       "Keep minAvailable >= 1",
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	promoteExistingRecordInGit(t, repoDir, prop.RecordID, "https://github.com/acme/gitops/pull/77 (#77)")
	modMerged, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeModifiedMerged,
		Repo:        "acme/gitops",
		PRNumber:    77,
		KnowledgePR: true,
		Summary:     "Reviewer tightened wording before merge.",
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if modMerged.RecordID != prop.RecordID || modMerged.Status != StatusActive {
		t.Fatalf("expected modified-merged via Repo+PRNumber to activate %s, got %+v", prop.RecordID, modMerged)
	}

	// Thread 10: Check-scoped lessons (no Expect.Path) across 2 clusters preserve Rule, block Severity, and synth.Guard.
	for i, cluster := range []string{"prod-eu", "prod-us"} {
		res, err := cur.ProcessOutcome(ctx, OutcomeEvent{
			Kind:       OutcomeResolvedMerged,
			RecordType: RecordTypeLesson,
			Repo:       "acme/gitops",
			PRNumber:   200 + i,
			Cluster:    cluster,
			CheckID:    "pdb-min-check",
			Title:      "PDB drain outage on " + cluster,
			Rule:       "Keep minAvailable >= 1 on critical services",
			Scope:      &RecordScope{Clusters: []string{cluster}, Paths: []string{"clusters/**/pdb.yaml"}, Checks: []string{"pdb-min-check"}},
			Guard: &RecordGuard{
				Rule:     "Keep minAvailable >= 1 on critical services",
				Severity: "block",
			},
		}, "")
		if err != nil {
			t.Fatal(err)
		}
		if i == 1 {
			if len(res.GraduatedDecisions) != 1 {
				t.Fatalf("expected check-scoped recurrence graduation on second cluster, got %+v", res)
			}
			gradID := res.GraduatedDecisions[0]
			recs, _ := LoadRecords(repoDir)
			for _, r := range recs {
				if r.Meta.ID == gradID {
					if r.Meta.Guard == nil || r.Meta.Guard.Severity != "block" || r.Meta.Guard.Expect == nil {
						t.Fatalf("graduated decision %s lost block severity or synthesized guard: %+v", gradID, r.Meta.Guard)
					}
				}
			}
		}
	}
}

func TestExternalDocDisambiguationAndWorkspaceCloneDirNames(t *testing.T) {
	// Thread 12: workspaceCloneDirName matches gitops_workspace.py for GitHub, GitLab subgroups, ports, and SCP URLs.
	cases := map[string]string{
		"https://github.com/acme/infra.git":              "acme__infra",
		"git@github.com:acme/infra.git":                  "acme__infra",
		"https://gitlab.com/acme/platform/infra.git":     "gitlab.com%2Facme%2Fplatform%2Finfra",
		"https://git.corp.internal:8443/ops/runbooks":    "git.corp.internal%2Fops%2Frunbooks",
		"git@gitlab.corp.internal:ops/team/runbooks.git": "gitlab.corp.internal%2Fops%2Fteam%2Frunbooks",
	}
	for rawURL, want := range cases {
		if got := workspaceCloneDirName(rawURL); got != want {
			t.Errorf("workspaceCloneDirName(%q) = %q, want %q", rawURL, got, want)
		}
	}

	// Thread 11: Colliding numbered external docs across directories and context repos keep distinct IDs.
	extRepoDir := t.TempDir()
	agentHome := t.TempDir()
	ctxDir := filepath.Join(agentHome, "gitops", "gitlab.com%2Facme%2Fplatform%2Frunbooks")
	if err := os.MkdirAll(filepath.Join(extRepoDir, "docs", "adr"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(extRepoDir, "adr"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(ctxDir, "docs", "adr"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(extRepoDir, "docs", "adr", "0001-ingress.md"), []byte("# ADR 1: Ingress policy\nUse Gateway API.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(extRepoDir, "adr", "0001-egress.md"), []byte("# ADR 1: Egress policy\nDeny by default.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ctxDir, "docs", "adr", "0001-mesh.md"), []byte("# ADR 1: Service mesh\nUse mTLS.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	stateDir := t.TempDir()
	ctxState := filepath.Join(stateDir, "context_repos")
	if err := os.WriteFile(ctxState, []byte(`[{"url":"https://gitlab.com/acme/platform/runbooks.git"}]`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLATFORM_AGENT_HOME", agentHome)
	t.Setenv("GITOPS_STATE_PATH", filepath.Join(stateDir, "managed_repos"))

	ext, err := LoadExternalDocRecords(extRepoDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(ext) != 3 {
		t.Fatalf("expected all 3 colliding 0001-*.md docs to be retained with disambiguated IDs, got %d: %+v", len(ext), ext)
	}
	seenIDs := map[string]bool{}
	for _, r := range ext {
		if seenIDs[r.Meta.ID] {
			t.Fatalf("duplicate external doc ID %q", r.Meta.ID)
		}
		seenIDs[r.Meta.ID] = true
	}

	// Thread 5: Oversized >4 MiB Markdown file in knowledge/decisions is bounded and skipped/rejected safely.
	decDir := filepath.Join(extRepoDir, "knowledge", "decisions")
	if err := os.MkdirAll(decDir, 0o755); err != nil {
		t.Fatal(err)
	}
	huge := "---\nid: DEC-2026-999\ntype: decision\nstatus: active\ntitle: Huge\ncreated: \"2026-11-11\"\n---\n" + strings.Repeat("x", maxRecordFileBytes+1024)
	if err := os.WriteFile(filepath.Join(decDir, "DEC-2026-999-huge.md"), []byte(huge), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadRecords(extRepoDir); err == nil {
		t.Fatal("expected LoadRecords to reject record exceeding 4 MiB limit")
	}
}

func TestCuratorStartConcurrentWithProcessOutcomeNoRace(t *testing.T) {
	natsURL := startEmbeddedJetStream(t)
	ctx := context.Background()
	client, err := lib.Connect(ctx, natsURL, lib.WithName("curator-race-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	repoDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoDir, "clusters", "prod-eu"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "clusters", "prod-eu", "pdb.yaml"), []byte("apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: web\nspec:\n  minAvailable: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cur, err := NewCurator(CuratorConfig{
		RepoDir: repoDir,
		RepoID:  "acme/gitops",
		Client:  client,
	})
	if err != nil {
		t.Fatal(err)
	}
	// Seed one record so Start's startup-sync branch has data to inspect.
	if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeApproved,
		RecordID:   "DEC-2026-001",
		RecordType: RecordTypeDecision,
		Title:      "Seed rule",
		Rule:       "Seed rule",
	}, ""); err != nil {
		t.Fatal(err)
	}
	// Reset outcomesProcessed to 0 on a fresh Curator sharing repoDir to exercise Start vs live ProcessOutcome.
	cur2, err := NewCurator(CuratorConfig{
		RepoDir: repoDir,
		RepoID:  "acme/gitops",
		Client:  client,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer cur2.Stop()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		if err := cur2.Start(ctx); err != nil {
			t.Errorf("Start failed: %v", err)
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 5; i++ {
			_, _ = cur2.ProcessOutcome(ctx, OutcomeEvent{
				Kind:       OutcomeApproved,
				RecordType: RecordTypeLesson,
				Cluster:    "prod-eu",
				Title:      "Concurrent outcome",
				Rule:       "Concurrent rule",
				PRNumber:   300 + i,
				Repo:       "acme/gitops",
			}, "")
		}
	}()
	wg.Wait()
}

func TestReviewInvariantsGitVerificationEmptyDirRecoveryAndMutantGuards(t *testing.T) {
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
	natsURL := startEmbeddedJetStream(t)
	mockHS, hsSrv := newMockHindsightServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	client, err := lib.Connect(ctx, natsURL, lib.WithName("curator-invariants-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	// 1. Low 5: SaveRecord rejects invalid record IDs.
	tmpRepo := t.TempDir()
	for _, badID := range []string{"", "../evil", "DEC-2026-1", "FOO-2026-001"} {
		badRec := Record{Meta: RecordMeta{ID: badID, Type: RecordTypeDecision, Title: "Bad ID", Status: StatusProposed}}
		if err := SaveRecord(tmpRepo, &badRec); err == nil {
			t.Fatalf("expected SaveRecord to reject invalid ID %q", badID)
		}
	}

	// 2. Low 2: GuardPassesOnHead enforces the 4 MiB file cap.
	oversizedRepo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(oversizedRepo, "apps"), 0o755); err != nil {
		t.Fatal(err)
	}
	hugeYAML := "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: big\ndata:\n  blob: " + strings.Repeat("a", maxRecordFileBytes+1024) + "\n"
	if err := os.WriteFile(filepath.Join(oversizedRepo, "apps", "big.yaml"), []byte(hugeYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	if GuardPassesOnHead(oversizedRepo, &RecordGuard{
		Rule:     "Check text",
		Severity: "warn",
		Expect:   &GuardExpect{File: "apps/*.yaml", Path: "$text", Op: "contains", Value: "ConfigMap"},
	}, nil) {
		t.Fatal("expected GuardPassesOnHead to skip/reject target file exceeding 4 MiB cap")
	}

	// 3. Low 3: MatchGlob with many ** segments runs in linear DP time (<50ms).
	startGlob := time.Now()
	deepPattern := "a/**/**/**/**/**/**/**/**/**/**/**/z.yaml"
	deepTarget := "a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t/u/v/w/x/y/not-z.yaml"
	if MatchGlob(deepPattern, deepTarget) {
		t.Fatal("expected non-matching deep target to return false")
	}
	if elapsed := time.Since(startGlob); elapsed > 100*time.Millisecond {
		t.Fatalf("MatchGlob with 11 ** segments took %v, expected <100ms DP", elapsed)
	}

	// 4. Low 5: mergeGuard rejects an LLM-synthesized Guard.Expect that fails GuardPassesOnHead.
	headCheckRepo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(headCheckRepo, "apps"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(headCheckRepo, "apps", "web.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	badSynth := &SynthesizedRecord{
		Title: "Hallucinated guard",
		Guard: &RecordGuard{
			Rule:     "Keep replicas >= 10",
			Severity: "block",
			Expect:   &GuardExpect{File: "apps/*.yaml", Path: "spec.replicas", Op: ">=", Value: 10},
		},
	}
	mergedG := mergeGuard(headCheckRepo, nil, OutcomeEvent{Rule: "Keep replicas >= 2"}, badSynth, nil)
	if mergedG == nil || mergedG.Expect != nil {
		t.Fatalf("expected mergeGuard to reject synthesized Guard.Expect failing HEAD check, got %+v", mergedG)
	}

	// 5. High 2: Unverified OutcomeApproved / OutcomeModifiedMerged (not committed in Git HEAD)
	// stays StatusProposed and cannot supersede, reject, or retire an existing Git-verified active record.
	gitRepo := t.TempDir()
	// Write a valid manifest so a publisher-supplied Guard.Expect would pass GuardPassesOnHead
	// if buildApprovedRecord / buildModifiedMergedRecord failed to ignore publisher-supplied guards.
	if err := os.MkdirAll(filepath.Join(gitRepo, "clusters"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gitRepo, "clusters", "deploy.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 99\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	gitCommitted001 := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-001",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Human-merged active rule",
			Date:   "2026-11-12",
			Scope:  RecordScope{Paths: []string{"clusters/**"}},
			Guard:  &RecordGuard{Rule: "Require 2 replicas.", Severity: "warn"},
		},
		Body: "## Context\n\nMerged by human.\n",
	}
	// Seed DEC-2026-001 in both RepoDir and .git-verified so it is active from the start.
	if err := SaveRecord(gitRepo, &gitCommitted001); err != nil {
		t.Fatal(err)
	}
	commitRecordInGit(t, gitRepo, gitCommitted001)

	// Also commit a guardless active record DEC-2026-010 (Guard == nil) in Git to verify
	// that neither OutcomeApproved nor OutcomeModifiedMerged adopts ev.Guard when gitRec.Meta.Guard is nil.
	gitCommitted010 := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-010",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Guardless human-merged rule",
			Date:   "2026-11-12",
			Scope:  RecordScope{Paths: []string{"clusters/**"}},
			Guard:  nil,
		},
		Body: "## Context\n\nGuardless rule merged by human.\n",
	}
	commitRecordInGit(t, gitRepo, gitCommitted010)

	cur, err := NewCurator(CuratorConfig{
		RepoDir:         gitRepo,
		VerifiedRepoDir: filepath.Join(gitRepo, verifiedGitSubdir),
		RepoID:          "acme/gitops",
		Client:          client,
		Hindsight:       NewHindsightClient(hsSrv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := cur.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer cur.Stop()

	// Unverified OutcomeApproved attempting to mint an active rule DEC-2026-002 and supersede DEC-2026-001.
	unverifiedRes, err := PublishOutcome(ctx, client, OutcomeEvent{
		Kind:       OutcomeApproved,
		RecordID:   "DEC-2026-002",
		RecordType: RecordTypeDecision,
		Title:      "Unverified outcome attempting to supersede DEC-2026-001",
		Rule:       "Allow 1 replica",
		Supersedes: []string{"DEC-2026-001"},
		Scope:      &RecordScope{Paths: []string{"clusters/**"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if unverifiedRes.Status != StatusProposed {
		t.Fatalf("unverified OutcomeApproved must produce status=proposed, got %s", unverifiedRes.Status)
	}
	// Verify DEC-2026-001 is still StatusActive (not superseded, rejected, or retired).
	for _, attemptKind := range []OutcomeKind{OutcomeRefused, OutcomeRetired} {
		resAttempt, err := cur.ProcessOutcome(ctx, OutcomeEvent{
			Kind:     attemptKind,
			RecordID: "DEC-2026-001",
		}, "")
		if err != nil {
			t.Fatal(err)
		}
		if resAttempt.Status != StatusActive {
			t.Fatalf("%s demoted Git-verified active record DEC-2026-001 to %s", attemptKind, resAttempt.Status)
		}
	}

	// Verify an unverified OutcomeModifiedMerged with KnowledgePR=true stays StatusProposed when not active in Git.
	unverifiedModRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeModifiedMerged,
		RecordID:    "DEC-2026-002",
		RecordType:  RecordTypeDecision,
		KnowledgePR: true,
		Title:       "Unverified modified-merged claiming KnowledgePR=true",
		Rule:        "Allow 1 replica",
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if unverifiedModRes.Status != StatusProposed {
		t.Fatalf("unverified OutcomeModifiedMerged with KnowledgePR=true must stay status=proposed, got %s", unverifiedModRes.Status)
	}

	// Verify all 5 targeted mutants on BOTH OutcomeApproved and OutcomeModifiedMerged:
	// - Git record with no guard (DEC-2026-010) keeps Guard == nil even when ev.Guard / ev.Rule is supplied
	// - Git record with a guard (DEC-2026-001) keeps its own Guard ("Require 2 replicas.", severity "warn")
	// - Git record keeps its own Scope ({Paths: ["clusters/**"]}), ignoring ev.Scope
	// - Git record keeps its own Title, ignoring ev.Title
	// - Git record keeps its own Body, ignoring ev.Summary and ev.ReviewerReason
	for _, kind := range []OutcomeKind{OutcomeApproved, OutcomeModifiedMerged} {
		for _, targetID := range []string{"DEC-2026-001", "DEC-2026-010"} {
			if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
				Kind:           kind,
				RecordID:       targetID,
				RecordType:     RecordTypeDecision,
				KnowledgePR:    true,
				Title:          "Injected Publisher Title",
				Summary:        "Injected publisher summary body section",
				ReviewerReason: "Injected reviewer reason body section",
				Rule:           "Injected publisher rule text",
				Cluster:        "injected-cluster",
				CheckID:        "injected-check",
				Scope: &RecordScope{
					Paths:     []string{"injected/**"},
					Clusters:  []string{"injected-cluster"},
					Resources: []string{"Deployment/injected"},
					Checks:    []string{"injected-check"},
				},
				Guard: &RecordGuard{
					Rule:     "Injected publisher guard",
					Severity: "block",
					Expect:   &GuardExpect{File: "clusters/*.yaml", Path: "spec.replicas", Op: ">=", Value: 99},
				},
			}, ""); err != nil {
				t.Fatalf("ProcessOutcome(%s, %s): %v", kind, targetID, err)
			}
		}

		recsAfterVerify, err := LoadRecords(gitRepo)
		if err != nil {
			t.Fatal(err)
		}
		gotByID := map[string]Record{}
		for _, r := range recsAfterVerify {
			gotByID[r.Meta.ID] = r
		}

		r001 := gotByID["DEC-2026-001"]
		if r001.Meta.Title != "Human-merged active rule" {
			t.Fatalf("%s on DEC-2026-001: expected Title=%q, got %q", kind, "Human-merged active rule", r001.Meta.Title)
		}
		if !reflect.DeepEqual(r001.Meta.Scope, RecordScope{Paths: []string{"clusters/**"}}) {
			t.Fatalf("%s on DEC-2026-001: expected Git Scope only, got %+v", kind, r001.Meta.Scope)
		}
		if r001.Meta.Guard == nil || r001.Meta.Guard.Expect != nil || r001.Meta.Guard.Rule != "Require 2 replicas." || r001.Meta.Guard.Severity != "warn" {
			t.Fatalf("%s on DEC-2026-001: expected Git Guard {Rule: Require 2 replicas., Severity: warn, Expect: nil}, got %+v", kind, r001.Meta.Guard)
		}
		if strings.TrimSpace(r001.Body) != "## Context\n\nMerged by human." {
			t.Fatalf("%s on DEC-2026-001: expected exact Git Body with no publisher text, got:\n%s", kind, r001.Body)
		}

		r010 := gotByID["DEC-2026-010"]
		if r010.Meta.Title != "Guardless human-merged rule" {
			t.Fatalf("%s on DEC-2026-010: expected Title=%q, got %q", kind, "Guardless human-merged rule", r010.Meta.Title)
		}
		if !reflect.DeepEqual(r010.Meta.Scope, RecordScope{Paths: []string{"clusters/**"}}) {
			t.Fatalf("%s on DEC-2026-010: expected Git Scope only, got %+v", kind, r010.Meta.Scope)
		}
		if r010.Meta.Guard != nil {
			t.Fatalf("%s on DEC-2026-010: expected nil Guard on guardless Git record, got %+v", kind, r010.Meta.Guard)
		}
		if strings.TrimSpace(r010.Body) != "## Context\n\nGuardless rule merged by human." {
			t.Fatalf("%s on DEC-2026-010: expected exact Git Body with no publisher text, got:\n%s", kind, r010.Body)
		}
	}

	// 6. Medium 3: Simulate emptyDir restart by creating a new Curator on a fresh empty directory
	// connected to the same Hindsight and NATS TOPICS-STATE. NextRecordID must not reset to DEC-2026-001.
	freshEmptyDir := t.TempDir()
	restartedCur, err := NewCurator(CuratorConfig{
		RepoDir:   freshEmptyDir,
		RepoID:    "acme/gitops",
		Client:    client,
		Hindsight: NewHindsightClient(hsSrv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatal(err)
	}
	afterRestartRes, err := restartedCur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeProposed,
		RecordType: RecordTypeDecision,
		Title:      "Proposal after emptyDir restart",
		Rule:       "Keep PDB minAvailable >= 1",
		Scope:      &RecordScope{Paths: []string{"clusters/**"}},
	}, "")
	if err != nil {
		t.Fatal(err)
	}
	if afterRestartRes.RecordID != "DEC-2026-011" {
		t.Fatalf("expected post-restart allocation to skip DEC-2026-001..010 and allocate DEC-2026-011, got %s", afterRestartRes.RecordID)
	}
	mockHS.mu.Lock()
	_, has002 := mockHS.docs["okf:acme/gitops:DEC-2026-002"]
	_, has011 := mockHS.docs["okf:acme/gitops:DEC-2026-011"]
	mockHS.mu.Unlock()
	if !has002 || !has011 {
		t.Fatalf("expected Hindsight to retain both DEC-2026-002 and DEC-2026-011 without collision")
	}

	// 7. VCS Broker /v1/vcs/snapshot sync into an isolated emptyDir RepoDir:
	// Verify that a Curator with BrokerURL + BrokerTokenFile pulls the verified knowledge/
	// tar archive from credential-proxy, promotes a proposed record to StatusActive,
	// supersedes listed records, and retires a record whose status is retired in Git.
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("test-curator-broker-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)
	tarFiles := map[string]string{
		"knowledge/decision/DEC-2026-020-broker.md":  "---\ntype: decision\nid: DEC-2026-020\ntitle: Broker-verified active rule\nstatus: active\ndate: \"2026-11-16\"\nscope:\n  paths:\n    - clusters/**\nguard:\n  rule: Keep broker-verified replicas >= 3.\n  severity: block\nsupersedes:\n  - DEC-2026-011\n---\n\n## Context\n\nVerified via broker snapshot.\n",
		"knowledge/decision/DEC-2026-021-retired.md": "---\ntype: decision\nid: DEC-2026-021\ntitle: Broker-retired rule\nstatus: retired\ndate: \"2026-11-16\"\nscope:\n  paths:\n    - clusters/**\n---\n\n## Context\n\nRetired in Git.\n",
	}
	for name, content := range tarFiles {
		if err := tw.WriteHeader(&tar.Header{
			Name: name,
			Mode: 0o600,
			Size: int64(len(content)),
		}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	// Also include a path-traversal entry and a symlink entry to verify both are rejected.
	evilContent := "---\ntype: decision\nid: DEC-2026-998\ntitle: Evil\nstatus: active\ndate: \"2026-11-16\"\n---\n\nEvil.\n"
	if err := tw.WriteHeader(&tar.Header{
		Name: "../evil-outside.md",
		Mode: 0o600,
		Size: int64(len(evilContent)),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write([]byte(evilContent)); err != nil {
		t.Fatal(err)
	}
	if err := tw.WriteHeader(&tar.Header{
		Name:     "knowledge/decision/DEC-2026-997-symlink.md",
		Typeflag: tar.TypeSymlink,
		Linkname: "/etc/passwd",
		Mode:     0o777,
	}); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	tarB64 := base64.StdEncoding.EncodeToString(tarBuf.Bytes())

	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/vcs/snapshot" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		if r.Header.Get("Authorization") != "Bearer test-curator-broker-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"archiveBase64": tarB64,
			"revision":      "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
		})
	}))
	defer brokerSrv.Close()

	brokerCur, err := NewCurator(CuratorConfig{
		RepoDir:         freshEmptyDir,
		BrokerURL:       brokerSrv.URL,
		BrokerTokenFile: tokenFile,
		RepoID:          "acme/gitops",
		Client:          client,
		Hindsight:       NewHindsightClient(hsSrv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatal(err)
	}
	// Seed DEC-2026-021 as active in RepoDir first, then verify OutcomeRetired transitions it
	// to StatusRetired once the broker snapshot reports status: retired in Git.
	active021 := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-021",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Broker-retired rule",
			Date:   "2026-11-16",
			Scope:  RecordScope{Paths: []string{"clusters/**"}},
		},
		Body: "## Context\n\nActive before retirement.\n",
	}
	if err := SaveRecord(freshEmptyDir, &active021); err != nil {
		t.Fatal(err)
	}
	brokerApproveRes, err := brokerCur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:       OutcomeApproved,
		RecordID:   "DEC-2026-020",
		RecordType: RecordTypeDecision,
		Title:      "Ignored publisher title",
	}, "")
	if err != nil {
		t.Fatalf("broker ProcessOutcome approved: %v", err)
	}
	if brokerApproveRes.Status != StatusActive || brokerApproveRes.Title != "Broker-verified active rule" {
		t.Fatalf("expected broker snapshot to verify DEC-2026-020 as active with Git title, got %+v", brokerApproveRes)
	}
	brokerRetireRes, err := brokerCur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:     OutcomeRetired,
		RecordID: "DEC-2026-021",
	}, "")
	if err != nil {
		t.Fatalf("broker ProcessOutcome retired: %v", err)
	}
	if brokerRetireRes.Status != StatusRetired {
		t.Fatalf("expected broker snapshot to allow retiring DEC-2026-021, got %s", brokerRetireRes.Status)
	}
	finalRecs, err := LoadRecords(freshEmptyDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range finalRecs {
		if r.Meta.ID == "DEC-2026-011" && r.Meta.Status != StatusSuperseded {
			t.Fatalf("expected DEC-2026-011 to be superseded by broker-verified DEC-2026-020, got %s", r.Meta.Status)
		}
	}
	if _, err := os.Stat(filepath.Join(freshEmptyDir, "evil-outside.md")); !os.IsNotExist(err) {
		t.Fatalf("expected traversal tar entry ../evil-outside.md to be rejected, err=%v", err)
	}
	if _, err := os.Lstat(filepath.Join(freshEmptyDir, verifiedGitSubdir, "knowledge", "decision", "DEC-2026-997-symlink.md")); !os.IsNotExist(err) {
		t.Fatalf("expected symlink tar entry to be rejected, err=%v", err)
	}
}

func TestCuratorPrometheusMetricsIsolationAndSeries(t *testing.T) {
	ctx := context.Background()
	dir1 := t.TempDir()
	dir2 := t.TempDir()

	cur1, err := NewCurator(CuratorConfig{
		RepoDir:             dir1,
		RepoID:              "acme/gitops",
		RecurrenceThreshold: 2,
	})
	if err != nil {
		t.Fatalf("NewCurator(cur1): %v", err)
	}
	cur2, err := NewCurator(CuratorConfig{
		RepoDir:             dir2,
		RepoID:              "acme/other",
		RecurrenceThreshold: 2,
	})
	if err != nil {
		t.Fatalf("NewCurator(cur2): %v", err)
	}

	// 1. Outcome processing: processed, duplicate, ignored, and unknown-kind error.
	propRes, err := cur1.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeProposed,
		RecordType:  RecordTypeDecision,
		Title:       "Keep checkout replicas >= 2",
		Rule:        "Checkout deployment must run at least 2 replicas.",
		PullRequest: "#101",
		Scope:       &RecordScope{Paths: []string{"clusters/**/*.yaml"}},
		Guard: &RecordGuard{
			Rule:     "Keep checkout replicas >= 2",
			Severity: "warn",
			Expect: &GuardExpect{
				File:  "clusters/**/*.yaml",
				Path:  "spec.replicas",
				Op:    ">=",
				Value: 2,
			},
		},
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome proposed: %v", err)
	}
	// Duplicate proposal for same PR -> status="duplicate".
	if _, err := cur1.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeProposed,
		RecordID:    propRes.RecordID,
		PullRequest: "#101",
	}, ""); err != nil {
		t.Fatalf("ProcessOutcome duplicate: %v", err)
	}
	// Conflicting PR with explicit RecordID -> status="ignored".
	if _, err := cur1.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeProposed,
		RecordID:    propRes.RecordID,
		PullRequest: "#999",
	}, ""); err != nil {
		t.Fatalf("ProcessOutcome ignored: %v", err)
	}
	// Unknown outcome kind -> kind="other", status="error".
	if _, err := cur1.ProcessOutcome(ctx, OutcomeEvent{
		Kind: OutcomeKind("unexpected-kind"),
	}, ""); err == nil {
		t.Fatal("expected error on unknown outcome kind")
	}

	// 2. Cross-cluster recurrence graduation across 2 clusters -> graduations_total = 1.
	for _, cluster := range []string{"prod-east", "prod-west"} {
		if _, err := cur1.ProcessOutcome(ctx, OutcomeEvent{
			Kind:       OutcomeResolvedMerged,
			RecordType: RecordTypeLesson,
			Cluster:    cluster,
			CheckID:    "oom-memory-floor",
			Title:      "Raise memory floor in " + cluster,
			Summary:    "OOM exit 137 fixed by raising memory.",
			Scope:      &RecordScope{Clusters: []string{cluster}, Paths: []string{"clusters/" + cluster + "/app.yaml"}, Checks: []string{"oom-memory-floor"}},
		}, ""); err != nil {
			t.Fatalf("ProcessOutcome resolved-merged (%s): %v", cluster, err)
		}
	}

	// 3. Seed an active blocking decision to test block guard verdict as well.
	activeBlock := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-099",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Block hostNetwork in prod",
			Date:   "2026-11-17",
			Scope:  RecordScope{Paths: []string{"clusters/prod/*.yaml"}},
			Guard: &RecordGuard{
				Rule:     "hostNetwork must be absent",
				Severity: "block",
				Expect: &GuardExpect{
					File: "clusters/prod/*.yaml",
					Path: "spec.hostNetwork",
					Op:   "absent",
				},
			},
		},
		Body: "## Context\n\nActive blocking rule.\n",
	}
	if err := SaveRecord(dir1, &activeBlock); err != nil {
		t.Fatalf("SaveRecord activeBlock: %v", err)
	}

	// 4. Queries: recall hit, empty, and error increment metrics; administrative RPCs (pending/propose/resolve) do not.
	if qPending := cur1.HandleQuery(ctx, MemoryQueryRequest{Action: "pending"}); qPending.Error != "" {
		t.Fatalf("expected pending action ok, got %+v", qPending)
	}
	if qPropose := cur1.HandleQuery(ctx, MemoryQueryRequest{Action: "propose"}); qPropose.Error == "" {
		t.Fatalf("expected propose action without outcome error, got %+v", qPropose)
	}
	if qResolve := cur1.HandleQuery(ctx, MemoryQueryRequest{Action: "resolve"}); qResolve.Error == "" {
		t.Fatalf("expected resolve action without outcome error, got %+v", qResolve)
	}
	if qHit := cur1.HandleQuery(ctx, MemoryQueryRequest{Query: "checkout replicas"}); qHit.Error != "" || len(qHit.Results) == 0 {
		t.Fatalf("expected query hit, got %+v", qHit)
	}
	if qEmpty := cur1.HandleQuery(ctx, MemoryQueryRequest{Query: "nonexistentzzzyyyxxx"}); qEmpty.Error != "" || len(qEmpty.Results) != 0 {
		t.Fatalf("expected empty query, got %+v", qEmpty)
	}
	if qErr := cur1.HandleQuery(ctx, MemoryQueryRequest{Query: "checkout", Tags: []string{"user:alice"}}); qErr.Error == "" {
		t.Fatalf("expected error on user:* tag, got %+v", qErr)
	}

	// 5. Seed active blocking decision and verify package CheckChangedFiles still blocks prod hostNetwork.
	blockYAML := "apiVersion: v1\nkind: Pod\nmetadata:\n  name: bad\nspec:\n  hostNetwork: true\n  replicas: 2\n"
	loaded, err := LoadRecords(dir1)
	if err != nil {
		t.Fatal(err)
	}
	if rep := CheckChangedFiles(loaded, map[string]*string{"clusters/prod/pod.yaml": &blockYAML}); rep.Verdict != "block" {
		t.Fatalf("expected block verdict from package CheckChangedFiles, got %+v", rep)
	}

	scrape := func(h http.Handler) string {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET /metrics status = %d, want 200", rec.Code)
		}
		return rec.Body.String()
	}

	body1 := scrape(cur1.MetricsHandler())
	if strings.Contains(body1, "kubeagents_curator_guard_checks_total") {
		t.Errorf("cur1 /metrics unexpectedly exposed removed kubeagents_curator_guard_checks_total:\n%s", body1)
	}
	for _, want := range []string{
		`kubeagents_curator_outcomes_total{kind="proposed",status="processed"} 1`,
		`kubeagents_curator_outcomes_total{kind="proposed",status="duplicate"} 1`,
		`kubeagents_curator_outcomes_total{kind="proposed",status="ignored"} 1`,
		`kubeagents_curator_outcomes_total{kind="resolved-merged",status="processed"} 2`,
		`kubeagents_curator_outcomes_total{kind="other",status="error"} 1`,
		`kubeagents_curator_records{status="proposed",type="decision"} 2`,
		`kubeagents_curator_records{status="active",type="decision"} 1`,
		`kubeagents_curator_records{status="proposed",type="lesson"} 2`,
		`kubeagents_curator_graduations_total 1`,
		`kubeagents_curator_queries_total{status="hit"} 1`,
		`kubeagents_curator_queries_total{status="empty"} 1`,
		`kubeagents_curator_queries_total{status="error"} 1`,
		`# TYPE process_start_time_seconds gauge`,
	} {
		if !strings.Contains(body1, want) {
			t.Errorf("cur1 /metrics missing %q;\nfull output:\n%s", want, body1)
		}
	}

	// Verify cur2's dedicated registry remained isolated at zero.
	body2 := scrape(cur2.MetricsHandler())
	for _, wantZero := range []string{
		`kubeagents_curator_outcomes_total{kind="proposed",status="processed"} 0`,
		`kubeagents_curator_records{status="proposed",type="decision"} 0`,
		`kubeagents_curator_graduations_total 0`,
		`kubeagents_curator_queries_total{status="hit"} 0`,
	} {
		if !strings.Contains(body2, wantZero) {
			t.Errorf("cur2 /metrics expected isolated zero %q;\nfull output:\n%s", wantZero, body2)
		}
	}
}

func TestGitopsPRsDefaultDisabledAndRemediationPRInProposed(t *testing.T) {
	// Ensure DECISION_MEMORY_GITOPS_PRS is truly unset and restored on exit.
	t.Setenv("DECISION_MEMORY_GITOPS_PRS", "true")
	if err := os.Unsetenv("DECISION_MEMORY_GITOPS_PRS"); err != nil {
		t.Fatal(err)
	}
	if !gitopsPRsDisabled() {
		t.Fatal("expected gitopsPRsDisabled() == true when DECISION_MEMORY_GITOPS_PRS is unset")
	}

	ctx := context.Background()
	cur, err := NewCurator(CuratorConfig{
		RepoDir: t.TempDir(),
		RepoID:  "acme/infra",
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. Newly proposed lesson with remediation PR records RemediationPR in pending digest.
	lesRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeProposed,
		RecordID:   "LES-2026-070",
		RecordType: RecordTypeLesson,
		Repo:       "acme/infra",
		PRNumber:   297,
		FindingID:  "checkout/Deployment/checkout:OOMKilled",
		SoakWindow: "24h",
		Title:      "Raise checkout memory limit",
		Rule:       "Keep checkout memory >= 512Mi.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome proposed lesson: %v", err)
	}
	if lesRes.Status != StatusProposed {
		t.Fatalf("expected status=proposed, got %s", lesRes.Status)
	}
	var found70 bool
	for _, rec := range lesRes.Blueprint.Records {
		if rec.ID == "LES-2026-070" {
			found70 = true
			wantPR := "https://github.com/acme/infra/pull/297 (#297)"
			if rec.RemediationPR != wantPR {
				t.Fatalf("LES-2026-070 RemediationPR = %q, want %q", rec.RemediationPR, wantPR)
			}
		}
	}
	if !found70 {
		t.Fatal("LES-2026-070 missing from blueprint records")
	}

	// Verify pending query exposes RemediationPR too.
	pending := cur.HandleQuery(ctx, MemoryQueryRequest{Action: "pending"})
	if pending.Error != "" || len(pending.Records) != 1 || pending.Records[0].RemediationPR != "https://github.com/acme/infra/pull/297 (#297)" {
		t.Fatalf("pending query missing RemediationPR: %+v", pending)
	}

	// 2. Attaching a PR to an already-existing PR-less LES-* record must NOT fabricate RemediationPR.
	if _, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeProposed,
		RecordID:   "LES-2026-071",
		RecordType: RecordTypeLesson,
		Repo:       "acme/infra",
		Title:      "Pre-existing PR-less lesson",
		Rule:       "Keep replicas >= 2.",
	}, ""); err != nil {
		t.Fatalf("ProcessOutcome seed PR-less lesson: %v", err)
	}
	attachRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:   OutcomeSchema,
		Kind:     OutcomeProposed,
		RecordID: "LES-2026-071",
		Repo:     "acme/infra",
		PRNumber: 350,
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome attach PR to LES-2026-071: %v", err)
	}
	for _, rec := range attachRes.Blueprint.Records {
		if rec.ID == "LES-2026-071" && rec.RemediationPR != "" {
			t.Fatalf("attaching PR to existing PR-less LES-2026-071 fabricated RemediationPR=%q", rec.RemediationPR)
		}
	}

	// 3. With DECISION_MEMORY_GITOPS_PRS unset, OutcomeResolvedMerged keeps StatusProposed and records RemediationPR.
	soakRes, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Schema:     OutcomeSchema,
		Kind:       OutcomeResolvedMerged,
		RecordID:   "LES-2026-072",
		RecordType: RecordTypeLesson,
		Repo:       "acme/infra",
		PRNumber:   410,
		Title:      "Verified OOM fix",
		Rule:       "Keep worker memory >= 1Gi.",
	}, "")
	if err != nil {
		t.Fatalf("ProcessOutcome resolved-merged unset env: %v", err)
	}
	if soakRes.Status != StatusProposed {
		t.Fatalf("expected StatusProposed with DECISION_MEMORY_GITOPS_PRS unset, got %s", soakRes.Status)
	}
}

func makeSnapshotTarBase64(t *testing.T, files map[string]string) string {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	for _, name := range names {
		content := files[name]
		if err := tw.WriteHeader(&tar.Header{
			Name: name,
			Mode: 0o600,
			Size: int64(len(content)),
		}); err != nil {
			t.Fatalf("tar header %s: %v", name, err)
		}
		if _, err := tw.Write([]byte(content)); err != nil {
			t.Fatalf("tar write %s: %v", name, err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatalf("tar close: %v", err)
	}
	return base64.StdEncoding.EncodeToString(buf.Bytes())
}

func TestExternalDocDecisionsRunbooksAndNormalizeContextRecords(t *testing.T) {
	repoDir := t.TempDir()
	for _, entry := range []struct {
		rel     string
		content string
	}{
		{
			rel:     "docs/decisions/0001-runbook-layout.md",
			content: "# Decision 1: Standardize runbook layout\n\nKeep operational documents concise.\n",
		},
		{
			rel:     "decisions/0002-network-policy.md",
			content: "# Decision 2: Default network isolation\n\nRequire explicit ingress rules.\n",
		},
		{
			rel:     "docs/runbooks/0001-decision-service-drain.md",
			content: "# Drain procedure\n\nCordon nodes one zone at a time.\n",
		},
		{
			rel:     "runbooks/0002-etcd-compaction.md",
			content: "# Etcd compaction guide\n\nCompact revisions during low traffic.\n",
		},
		{
			rel:     "playbooks/0003-dns-saturation.md",
			content: "# DNS saturation playbook\n\nScale CoreDNS replicas.\n",
		},
	} {
		full := filepath.Join(repoDir, filepath.FromSlash(entry.rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, []byte(entry.content), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	recs, err := LoadSingleRepoExternalDocRecords(repoDir)
	if err != nil {
		t.Fatalf("LoadSingleRepoExternalDocRecords: %v", err)
	}
	if len(recs) != 5 {
		t.Fatalf("expected 5 external doc records, got %d: %+v", len(recs), recs)
	}
	byID := map[string]Record{}
	for _, r := range recs {
		byID[r.Meta.ID] = r
	}
	if r, ok := byID["ADR-0001"]; !ok || r.Meta.Type != RecordTypeDecision {
		t.Fatalf("expected docs/decisions/0001-runbook-layout.md -> (decision, ADR-0001), got %+v", byID["ADR-0001"])
	}
	if r, ok := byID["ADR-0002"]; !ok || r.Meta.Type != RecordTypeDecision {
		t.Fatalf("expected decisions/0002-network-policy.md -> (decision, ADR-0002), got %+v", byID["ADR-0002"])
	}
	for _, id := range []string{"RUN-0001", "RUN-0002", "RUN-0003"} {
		if r, ok := byID[id]; !ok || r.Meta.Type != RecordTypeRunbook {
			t.Fatalf("expected %s to load as RecordTypeRunbook, got %+v", id, byID[id])
		}
	}

	origPaths := []string{"apps/*.yaml"}
	origExpect := &GuardExpect{File: "apps/*.yaml", Path: "spec.replicas", Op: ">=", Value: 3}
	input := []Record{
		{
			Path: "knowledge/decision/DEC-2026-001.md",
			Meta: RecordMeta{
				ID:     "DEC-2026-001",
				Type:   RecordTypeDecision,
				Status: StatusActive,
				Title:  "Context repo guard rule",
				Scope:  RecordScope{Paths: origPaths, Clusters: []string{"*"}},
				Declares: []DeclareItem{{
					File:     "apps/*.yaml",
					Path:     "spec.replicas",
					Op:       ">=",
					Value:    3,
					Severity: SeverityBlock,
				}},
				Guard: &RecordGuard{
					Rule:     "Keep replicas >= 3",
					Severity: SeverityBlock,
					Expect:   origExpect,
				},
			},
			Body: "Context rule body.",
		},
		byID["RUN-0001"],
	}

	norm := NormalizeRepoRecords("acme/shared-docs", RoleContext, input)
	if len(norm) != 2 {
		t.Fatalf("expected 2 normalized records, got %d", len(norm))
	}
	// Verify caller structs were deep-copied and not mutated.
	if input[0].Meta.Guard.Expect == nil || input[0].Meta.Guard.Severity != SeverityBlock || input[0].Meta.Status != StatusActive {
		t.Fatalf("NormalizeRepoRecords mutated caller input slice: %+v", input[0].Meta)
	}
	normGuardRec := norm[0]
	if normGuardRec.Meta.Repo != "acme/shared-docs" || normGuardRec.Meta.Role != RoleContext {
		t.Fatalf("unexpected repo/role on normalized guard record: %+v", normGuardRec.Meta)
	}
	if normGuardRec.Meta.Status != StatusProposed {
		t.Fatalf("expected context guard record status=proposed, got %s", normGuardRec.Meta.Status)
	}
	if normGuardRec.Meta.Guard == nil || normGuardRec.Meta.Guard.Expect != nil || normGuardRec.Meta.Guard.Severity != SeverityWarn {
		t.Fatalf("expected context guard Expect=nil and Severity=warn, got %+v", normGuardRec.Meta.Guard)
	}
	if normGuardRec.Meta.Declares[0].File != "" || normGuardRec.Meta.Declares[0].Severity != SeverityWarn {
		t.Fatalf("expected context Declares assertion stripped and downgraded, got %+v", normGuardRec.Meta.Declares[0])
	}
	guardTags := normGuardRec.Tags()
	if !matchesTags(guardTags, []string{"repo:acme/shared-docs", "role:context", "guard:warn", "status:proposed"}, "all_strict") {
		t.Fatalf("unexpected tags on normalized context guard record: %v", guardTags)
	}
	repoTagCount := 0
	for _, tag := range guardTags {
		if strings.HasPrefix(tag, "repo:") {
			repoTagCount++
		}
	}
	if repoTagCount != 1 {
		t.Fatalf("expected exactly 1 repo: tag, got %d in %v", repoTagCount, guardTags)
	}

	// Verify a normalized context guard record never triggers a block verdict in CheckChangedFiles.
	badYAML := "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec:\n  replicas: 1\n"
	rep := CheckChangedFiles(norm, map[string]*string{"apps/deploy.yaml": &badYAML})
	if rep.Verdict == SeverityBlock {
		t.Fatalf("context repo record must never produce Verdict=block, got %+v", rep)
	}

	// Advisory runbook keeps Guard == nil, StatusActive, and role:context.
	normRunbook := norm[1]
	if normRunbook.Meta.Guard != nil || normRunbook.Meta.Status != StatusActive || normRunbook.Meta.Role != RoleContext {
		t.Fatalf("unexpected advisory runbook normalization: %+v", normRunbook.Meta)
	}
}

func TestMultiRepoSnapshotIsolation413FallbackAndNATSIndexBoundary(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	natsURL := startEmbeddedJetStream(t)
	mockHS, hsSrv := newMockHindsightServer(t)

	client, err := lib.Connect(ctx, natsURL, lib.WithName("curator-multirepo-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	var brokerMu sync.Mutex
	var brokerRequests []map[string]any

	repoATar := makeSnapshotTarBase64(t, map[string]string{
		"docs/decisions/ADR-001-gateway.md": "# ADR-001: Gateway controller in repo-a\n\nUse Envoy gateway for edge traffic.\n",
	})
	repoBTar := makeSnapshotTarBase64(t, map[string]string{
		"decisions/ADR-001-gateway.md":       "# ADR-001: Gateway controller in repo-b\n\nUse internal load balancer for mesh traffic.\n",
		"runbooks/0001-mesh- failover.md":    "# Mesh drain runbook\n\nShift weight to zero before restart.\n",
		"knowledge/decision/DEC-2026-001.md": "---\ntype: decision\nid: DEC-2026-001\ntitle: Repo B replica policy\nstatus: active\ndate: \"2026-11-20\"\nscope:\n  paths:\n    - services/**\n---\n\nKeep two replicas in repo-b.\n",
	})

	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/vcs/snapshot" || r.Method != http.MethodPost {
			http.NotFound(w, r)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		brokerMu.Lock()
		brokerRequests = append(brokerRequests, body)
		brokerMu.Unlock()

		repo, _ := body["repository"].(string)
		_, hasPaths := body["paths"]
		w.Header().Set("Content-Type", "application/json")

		switch repo {
		case "acme/repo-a":
			if hasPaths {
				t.Errorf("expected initial snapshot request for acme/repo-a without paths, got %+v", body)
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"revision":      "1111111111111111111111111111111111111111",
				"archiveBase64": repoATar,
			})
		case "acme/repo-b":
			// Simulate HTTP 413 SNAPSHOT_TOO_LARGE on whole-tree request, then 200 on narrowed paths.
			if !hasPaths {
				w.WriteHeader(http.StatusRequestEntityTooLarge)
				_ = json.NewEncoder(w).Encode(map[string]any{
					"code":     "SNAPSHOT_TOO_LARGE",
					"revision": "2222222222222222222222222222222222222222",
					"entries": []map[string]any{
						{"path": "vendor", "type": "tree"},
						{"path": "knowledge", "type": "tree"},
						{"path": "decisions", "type": "tree"},
						{"path": "runbooks", "type": "tree"},
					},
				})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"revision":      "2222222222222222222222222222222222222222",
				"archiveBase64": repoBTar,
			})
		case "acme/no-docs":
			w.WriteHeader(http.StatusRequestEntityTooLarge)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"code":     "SNAPSHOT_TOO_LARGE",
				"revision": "3333333333333333333333333333333333333333",
				"entries": []map[string]any{
					{"path": "cmd", "type": "tree"},
					{"path": "pkg", "type": "tree"},
				},
			})
		default:
			http.Error(w, "unknown repo", http.StatusNotFound)
		}
	}))
	defer brokerSrv.Close()

	storeDir := t.TempDir()
	cur, err := NewCurator(CuratorConfig{
		RepoDir:   storeDir,
		BrokerURL: brokerSrv.URL,
		RepoID:    "acme/repo-a",
		Client:    client,
		Hindsight: NewHindsightClient(hsSrv.URL, DefaultBankID),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := cur.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer cur.Stop()

	// 1. Action: "index" MUST be rejected on SubjectMemoryQuery (read-only query channel).
	if _, qErr := QueryViaNATS(ctx, client, MemoryQueryRequest{
		Action: "index",
		Repo:   "acme/repo-a",
	}); qErr == nil || !strings.Contains(qErr.Error(), SubjectCuratorOutcomes) {
		t.Fatalf("expected Action=index on SubjectMemoryQuery to be rejected, got err=%v", qErr)
	}

	// 2. IndexViaNATS on SubjectCuratorOutcomes for acme/repo-a and acme/repo-b (413 fallback) and acme/no-docs.
	respA, err := IndexViaNATS(ctx, client, MemoryQueryRequest{
		Repo: "acme/repo-a",
		Role: RoleManaged,
	})
	if err != nil {
		t.Fatalf("IndexViaNATS(acme/repo-a): %v", err)
	}
	if !respA.OK || respA.Revision != "1111111111111111111111111111111111111111" || len(respA.Upserted) != 1 {
		t.Fatalf("unexpected respA: %+v", respA)
	}

	respB, err := IndexViaNATS(ctx, client, MemoryQueryRequest{
		Repo: "acme/repo-b",
		Role: RoleManaged,
	})
	if err != nil {
		t.Fatalf("IndexViaNATS(acme/repo-b): %v", err)
	}
	if !respB.OK || respB.Revision != "2222222222222222222222222222222222222222" || len(respB.Upserted) != 3 {
		t.Fatalf("unexpected respB: %+v", respB)
	}

	respNoDocs, err := IndexViaNATS(ctx, client, MemoryQueryRequest{
		Repo: "acme/no-docs",
		Role: RoleManaged,
	})
	if err != nil {
		t.Fatalf("IndexViaNATS(acme/no-docs): %v", err)
	}
	if !respNoDocs.OK || respNoDocs.Revision != "3333333333333333333333333333333333333333" || len(respNoDocs.Upserted) != 0 {
		t.Fatalf("unexpected respNoDocs: %+v", respNoDocs)
	}

	// Verify narrowed 413 retry requested only matching doc roots ["knowledge", "decisions", "runbooks"].
	brokerMu.Lock()
	var narrowedPaths []string
	for _, reqMap := range brokerRequests {
		if reqMap["repository"] == "acme/repo-b" {
			if rawPaths, ok := reqMap["paths"].([]any); ok {
				for _, p := range rawPaths {
					if s, ok := p.(string); ok {
						narrowedPaths = append(narrowedPaths, s)
					}
				}
			}
		}
	}
	brokerMu.Unlock()
	if !reflect.DeepEqual(narrowedPaths, []string{"knowledge", "decisions", "runbooks"}) {
		t.Fatalf("expected 413 fallback paths [knowledge decisions runbooks], got %v", narrowedPaths)
	}

	// Verify per-repo .git-verified/<slug> isolation kept BOTH acme__repo-a and acme__repo-b intact.
	if _, err := os.Stat(filepath.Join(storeDir, verifiedGitSubdir, "acme__repo-a", "docs", "decisions", "ADR-001-gateway.md")); err != nil {
		t.Fatalf("expected acme__repo-a snapshot to survive syncing acme/repo-b: %v", err)
	}
	if _, err := os.Stat(filepath.Join(storeDir, verifiedGitSubdir, "acme__repo-b", "decisions", "ADR-001-gateway.md")); err != nil {
		t.Fatalf("expected acme__repo-b snapshot on disk: %v", err)
	}

	// 3. Index a context repository via IndexViaNATS and verify zero disk writes + guard stripping.
	ctxRec := Record{
		Path: "docs/decisions/ADR-001-context.md",
		Meta: RecordMeta{
			ID:     "ADR-001",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Gateway controller reference architecture",
			Scope:  RecordScope{Clusters: []string{"*"}, Paths: []string{"**"}},
			Guard: &RecordGuard{
				Rule:     "Never allow hostPort in gateway pods",
				Severity: SeverityBlock,
				Expect:   &GuardExpect{File: "gateway.yaml", Path: "spec.hostPort", Op: "absent"},
			},
		},
		Body: "Shared platform reference architecture for gateway controllers.",
	}
	respCtx, err := IndexViaNATS(ctx, client, MemoryQueryRequest{
		Repo:    "acme/platform-handbook",
		Role:    RoleContext,
		Records: []Record{ctxRec},
	})
	if err != nil {
		t.Fatalf("IndexViaNATS(context): %v", err)
	}
	if !respCtx.OK || len(respCtx.Upserted) != 1 {
		t.Fatalf("unexpected context index response: %+v", respCtx)
	}
	if _, err := os.Stat(filepath.Join(storeDir, verifiedGitSubdir, "acme__platform-handbook")); !os.IsNotExist(err) {
		t.Fatalf("context repo must never write to .git-verified, stat err=%v", err)
	}

	mockHS.mu.Lock()
	ctxDoc, hasCtxDoc := mockHS.docs["okf:acme/platform-handbook:ADR-001"]
	docA, hasDocA := mockHS.docs["okf:acme/repo-a:ADR-001"]
	docB, hasDocB := mockHS.docs["okf:acme/repo-b:ADR-001"]
	mockHS.mu.Unlock()
	if !hasCtxDoc || !hasDocA || !hasDocB {
		t.Fatalf("expected ADR-001 retained separately for repo-a (%v), repo-b (%v), and platform-handbook (%v)", hasDocA, hasDocB, hasCtxDoc)
	}
	if !matchesTags(docA.Tags, []string{"repo:acme/repo-a", "role:managed"}, "all_strict") {
		t.Fatalf("expected repo-a tags on docA, got %v", docA.Tags)
	}
	if !matchesTags(docB.Tags, []string{"repo:acme/repo-b", "role:managed"}, "all_strict") {
		t.Fatalf("expected repo-b tags on docB, got %v", docB.Tags)
	}
	if !matchesTags(ctxDoc.Tags, []string{"repo:acme/platform-handbook", "role:context", "guard:warn", "status:proposed"}, "all_strict") {
		t.Fatalf("expected context doc in Hindsight to be downgraded to guard:warn and status:proposed, got %v", ctxDoc.Tags)
	}

	// 4. Verify HandleQuery returns ADR-001 from repo-a, repo-b, and platform-handbook without collision (including local fallback without Hindsight).
	curNoHS, err := NewCurator(CuratorConfig{
		RepoDir:   storeDir,
		BrokerURL: brokerSrv.URL,
		RepoID:    "acme/repo-a",
	})
	if err != nil {
		t.Fatal(err)
	}
	curNoHS.IndexRepo(ctx, MemoryQueryRequest{
		Repo:    "acme/platform-handbook",
		Role:    RoleContext,
		Records: []Record{ctxRec},
	})
	localQuery := curNoHS.HandleQuery(ctx, MemoryQueryRequest{
		Query: "gateway controller",
	})
	seenDocIDs := map[string]bool{}
	for _, hit := range localQuery.Results {
		seenDocIDs[hit.DocumentID] = true
	}
	for _, wantDocID := range []string{
		"okf:acme/repo-a:ADR-001",
		"okf:acme/repo-b:ADR-001",
		"okf:acme/platform-handbook:ADR-001",
	} {
		if !seenDocIDs[wantDocID] {
			t.Fatalf("HandleQuery local fallback missing %s; got %+v", wantDocID, localQuery.Results)
		}
	}
}

func TestExtraRepoDocsNormalizedAsContextAndPrimaryContextRoleRejected(t *testing.T) {
	primaryDir := t.TempDir()
	extraDir := t.TempDir()

	if err := os.MkdirAll(filepath.Join(extraDir, "knowledge", "decision"), 0o755); err != nil {
		t.Fatal(err)
	}
	extraBlockMD := "---\ntype: decision\nid: DEC-2026-077\ntitle: Extra repo active block guard\nstatus: active\ndate: \"2026-11-24\"\ndeclares:\n  - file: apps/*.yaml\n    path: spec.replicas\n    op: \">=\"\n    value: 4\n    severity: block\n    rule: Require at least 4 replicas.\nscope:\n  paths:\n    - apps/**\nguard:\n  rule: Require at least 4 replicas.\n  severity: block\n  expect:\n    file: apps/*.yaml\n    path: spec.replicas\n    op: \">=\"\n    value: 4\n---\n\nExtra repo rule.\n"
	if err := os.WriteFile(filepath.Join(extraDir, "knowledge", "decision", "DEC-2026-077.md"), []byte(extraBlockMD), 0o600); err != nil {
		t.Fatal(err)
	}

	recs, err := LoadExternalDocRecords(primaryDir, extraDir)
	if err != nil {
		t.Fatalf("LoadExternalDocRecords: %v", err)
	}
	if len(recs) != 1 {
		t.Fatalf("expected 1 extra repo record, got %+v", recs)
	}
	got := recs[0]
	if got.Meta.Role != RoleContext || got.Meta.Status != StatusProposed {
		t.Fatalf("expected extra repo guard record to be downgraded to role=context status=proposed, got role=%q status=%q", got.Meta.Role, got.Meta.Status)
	}
	if got.Meta.Guard == nil || got.Meta.Guard.Expect != nil || got.Meta.Guard.Severity != SeverityWarn {
		t.Fatalf("expected extra repo guard expect stripped and severity capped at warn, got %+v", got.Meta.Guard)
	}
	if len(got.Meta.Declares) != 1 || got.Meta.Declares[0].File != "" || got.Meta.Declares[0].Severity != SeverityWarn {
		t.Fatalf("expected extra repo declares assertion stripped and downgraded, got %+v", got.Meta.Declares)
	}
	badYAML := "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 1\n"
	rep := CheckChangedFiles(recs, map[string]*string{"apps/web.yaml": &badYAML})
	if rep.Verdict == SeverityBlock {
		t.Fatalf("extra/context repo record must never produce block verdict, got %+v", rep)
	}

	cur, err := NewCurator(CuratorConfig{
		RepoDir: t.TempDir(),
		RepoID:  "acme/primary-gitops",
	})
	if err != nil {
		t.Fatal(err)
	}
	rej := cur.IndexRepo(context.Background(), MemoryQueryRequest{
		Action:  "index",
		Repo:    "ACME/PRIMARY-GITOPS",
		Role:    RoleContext,
		Records: recs,
	})
	if rej.OK || !strings.Contains(rej.Error, "cannot index primary managed repository") {
		t.Fatalf("expected IndexRepo to reject role=context for primary managed repo, got %+v", rej)
	}
}

func TestSnapshotTooLargeNullEntriesAndMultiPathNotFoundFailClosed(t *testing.T) {
	ctx := context.Background()
	mockHS, hsSrv := newMockHindsightServer(t)
	hsClient := NewHindsightClient(hsSrv.URL, DefaultBankID)
	if err := hsClient.EnsureBank(ctx); err != nil {
		t.Fatal(err)
	}

	seedActive := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-001",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Preserve Git-governed record on 413 failure",
			Date:   "2026-11-24",
			Repo:   "acme/oversized",
			Role:   RoleManaged,
			Scope:  RecordScope{Paths: []string{"clusters/**"}},
		},
		Body: "Must not be deleted when broker omits entries on 413.",
	}
	for _, repo := range []string{"acme/oversized", "acme/omitted-entries", "acme/proxy-413", "acme/toctou", "acme/single-gone"} {
		cp := seedActive
		cp.Meta.Repo = repo
		if err := hsClient.RetainRecord(ctx, repo, cp); err != nil {
			t.Fatal(err)
		}
	}

	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		repo, _ := body["repository"].(string)
		_, hasPaths := body["paths"]
		w.Header().Set("Content-Type", "application/json")

		switch repo {
		case "acme/oversized":
			w.WriteHeader(http.StatusRequestEntityTooLarge)
			_, _ = w.Write([]byte(`{"code":"SNAPSHOT_TOO_LARGE","revision":"aaaa","entries":null}`))
		case "acme/omitted-entries":
			w.WriteHeader(http.StatusRequestEntityTooLarge)
			_, _ = w.Write([]byte(`{"code":"SNAPSHOT_TOO_LARGE","error":"clone exceeds max_clone_bytes"}`))
		case "acme/proxy-413":
			w.WriteHeader(http.StatusRequestEntityTooLarge)
			_, _ = w.Write([]byte(`{"error":"Request Entity Too Large"}`))
		case "acme/toctou":
			if !hasPaths {
				w.WriteHeader(http.StatusRequestEntityTooLarge)
				_, _ = w.Write([]byte(`{"code":"SNAPSHOT_TOO_LARGE","revision":"bbbb","entries":[{"path":"knowledge","type":"tree"},{"path":"runbooks","type":"tree"}]}`))
				return
			}
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"code":"PATH_NOT_FOUND","error":"PATH_NOT_FOUND: runbooks"}`))
		case "acme/single-gone":
			if !hasPaths {
				w.WriteHeader(http.StatusRequestEntityTooLarge)
				_, _ = w.Write([]byte(`{"code":"SNAPSHOT_TOO_LARGE","revision":"cccc","entries":[{"path":"runbooks","type":"tree"}]}`))
				return
			}
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"code":"PATH_NOT_FOUND","error":"PATH_NOT_FOUND: runbooks"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer brokerSrv.Close()

	cur, err := NewCurator(CuratorConfig{
		RepoDir:   t.TempDir(),
		BrokerURL: brokerSrv.URL,
		RepoID:    "acme/primary",
		Hindsight: hsClient,
	})
	if err != nil {
		t.Fatal(err)
	}

	for _, failRepo := range []string{"acme/oversized", "acme/omitted-entries", "acme/proxy-413", "acme/toctou"} {
		resp := cur.IndexRepo(ctx, MemoryQueryRequest{
			Action: "index",
			Repo:   failRepo,
			Role:   RoleManaged,
		})
		if resp.OK || resp.Error == "" {
			t.Fatalf("expected IndexRepo(%s) to fail closed, got %+v", failRepo, resp)
		}
		mockHS.mu.Lock()
		_, stillPresent := mockHS.docs["okf:"+failRepo+":DEC-2026-001"]
		mockHS.mu.Unlock()
		if !stillPresent {
			t.Fatalf("fail-closed violation: Hindsight record for %s was deleted on error!", failRepo)
		}
	}

	// Single matched root disappearing on narrowed retry is unambiguously empty and removes stale doc.
	okResp := cur.IndexRepo(ctx, MemoryQueryRequest{
		Action: "index",
		Repo:   "acme/single-gone",
		Role:   RoleManaged,
	})
	if !okResp.OK || okResp.Error != "" || len(okResp.Deleted) != 1 {
		t.Fatalf("expected single-root PATH_NOT_FOUND to succeed and prune stale doc, got %+v", okResp)
	}
}

func TestConcurrentHandleQueryNotBlockedBySlowIndexRepo(t *testing.T) {
	enteredBroker := make(chan struct{})
	releaseBroker := make(chan struct{})
	var once sync.Once

	slowTar := makeSnapshotTarBase64(t, map[string]string{
		"docs/decisions/ADR-009-slow.md": "# ADR-009: Slow broker indexed architecture\n\nIndexed without blocking readers.\n",
	})

	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		once.Do(func() { close(enteredBroker) })
		<-releaseBroker
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"revision":      "9999999999999999999999999999999999999999",
			"archiveBase64": slowTar,
		})
	}))
	defer brokerSrv.Close()

	storeDir := t.TempDir()
	seed := Record{
		Meta: RecordMeta{
			ID:     "DEC-2026-001",
			Type:   RecordTypeDecision,
			Status: StatusActive,
			Title:  "Fast local query baseline policy",
			Date:   "2026-11-24",
			Scope:  RecordScope{Clusters: []string{"*"}, Paths: []string{"clusters/**"}},
		},
		Body: "Readers must not block while IndexRepo waits on HTTP.",
	}
	if err := SaveRecord(storeDir, &seed); err != nil {
		t.Fatal(err)
	}

	cur, err := NewCurator(CuratorConfig{
		RepoDir:   storeDir,
		BrokerURL: brokerSrv.URL,
		RepoID:    "acme/primary",
	})
	if err != nil {
		t.Fatal(err)
	}

	indexDone := make(chan MemoryQueryResponse, 1)
	go func() {
		indexDone <- cur.IndexRepo(context.Background(), MemoryQueryRequest{
			Action: "index",
			Repo:   "acme/slow-secondary",
			Role:   RoleManaged,
		})
	}()

	select {
	case <-enteredBroker:
	case <-time.After(5 * time.Second):
		t.Fatal("IndexRepo did not enter broker HTTP handler")
	}

	// Execute concurrent HandleQuery calls while IndexRepo is blocked inside the HTTP handler.
	var wg sync.WaitGroup
	errCh := make(chan string, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			qCtx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
			defer cancel()
			doneCh := make(chan MemoryQueryResponse, 1)
			go func() {
				doneCh <- cur.HandleQuery(qCtx, MemoryQueryRequest{Query: "baseline policy"})
			}()
			select {
			case qResp := <-doneCh:
				if qResp.Error != "" || len(qResp.Results) == 0 {
					errCh <- "unexpected query response: " + qResp.Error
				}
			case <-qCtx.Done():
				errCh <- "HandleQuery blocked on IndexRepo mutex!"
			}
		}()
	}
	wg.Wait()
	close(errCh)
	for e := range errCh {
		t.Fatal(e)
	}

	close(releaseBroker)
	idxResp := <-indexDone
	if !idxResp.OK || len(idxResp.Upserted) != 1 {
		t.Fatalf("unexpected IndexRepo response after unblock: %+v", idxResp)
	}

	afterQuery := cur.HandleQuery(context.Background(), MemoryQueryRequest{Query: "Slow broker indexed"})
	if len(afterQuery.Results) == 0 || afterQuery.Results[0].DocumentID != "okf:acme/slow-secondary:ADR-009" {
		t.Fatalf("expected newly indexed ADR-009 in HandleQuery after IndexRepo finished, got %+v", afterQuery.Results)
	}
}

func TestGuardSeverityCapMultiRepoDreamAndExplicitVerifiedRepoDir(t *testing.T) {
	ctx := context.Background()
	mockHS, hsSrv := newMockHindsightServer(t)
	hsClient := NewHindsightClient(hsSrv.URL, DefaultBankID)

	llmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"title\":\"Synthesized OOM guard\",\"context\":\"OOM exit 137\",\"decision\":\"Keep replicas >= 2\",\"guard\":{\"rule\":\"Keep replicas >= 2\",\"severity\":\"block\",\"expect\":{\"file\":\"apps/*.yaml\",\"path\":\"spec.replicas\",\"op\":\">=\",\"value\":2}}}"}}]}`))
	}))
	defer llmSrv.Close()

	storeDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(storeDir, "apps"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(storeDir, "apps", "web.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 3\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	explicitVDir := t.TempDir()
	primaryTar := makeSnapshotTarBase64(t, map[string]string{
		"docs/adr/0001-primary.md": "# ADR 1: Primary architecture\n\nPrimary repo doc.\n",
	})
	secondaryTar := makeSnapshotTarBase64(t, map[string]string{
		"docs/adr/0002-secondary.md": "# ADR 2: Secondary architecture\n\nSecondary repo doc.\n",
	})
	var brokerHits atomic.Int32
	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		brokerHits.Add(1)
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		repo, _ := body["repository"].(string)
		w.Header().Set("Content-Type", "application/json")
		switch repo {
		case "acme/gitops":
			_ = json.NewEncoder(w).Encode(map[string]any{"revision": "rev-pri", "archiveBase64": primaryTar})
		case "acme/payments":
			_ = json.NewEncoder(w).Encode(map[string]any{"revision": "rev-sec", "archiveBase64": secondaryTar})
		default:
			http.Error(w, "unexpected broker call for "+repo, http.StatusInternalServerError)
		}
	}))
	defer brokerSrv.Close()

	cur, err := NewCurator(CuratorConfig{
		RepoDir:         storeDir,
		VerifiedRepoDir: explicitVDir,
		BrokerURL:       brokerSrv.URL,
		RepoID:          "acme/gitops",
		Hindsight:       hsClient,
		LLM: LLMConfig{
			BaseURL: llmSrv.URL,
			Model:   "test-model",
		},
	})
	if err != nil {
		t.Fatal(err)
	}

	// 1. GuardSeverity: "warn" MUST cap LLM-synthesized "block" guard to "warn" even on crash OOM resolved-merged.
	resWarn, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:          OutcomeResolvedMerged,
		Repo:          "acme/gitops",
		Title:         "Checkout OOM exit 137 fix",
		Summary:       "Container hit OOM exit 137 under spike.",
		PullRequest:   "#88",
		GuardSeverity: SeverityWarn,
	}, "corr-warn")
	if err != nil {
		t.Fatalf("ProcessOutcome(GuardSeverity=warn): %v", err)
	}
	if strings.Contains(resWarn.PRMarkdown, "severity: block") || !strings.Contains(resWarn.PRMarkdown, "severity: warn") {
		t.Fatalf("expected GuardSeverity=warn to cap synthesized LLM block guard in PRMarkdown:\n%s", resWarn.PRMarkdown)
	}

	// 2. Outcome: "backfill" on secondary repo "acme/payments" MUST cap severity to warn, stamp Meta.Repo, and survive RunDream under okf:acme/payments:*.
	resSec, err := cur.ProcessOutcome(ctx, OutcomeEvent{
		Kind:        OutcomeProposed,
		Outcome:     "backfill",
		RecordType:  RecordTypeLesson,
		Repo:        "acme/payments",
		Title:       "Payments OOM exit 137 revert",
		Summary:     "Reverted memory drop after OOM exit 137.",
		PullRequest: "https://github.com/acme/payments/pull/19 (#19)",
	}, "corr-sec")
	if err != nil {
		t.Fatalf("ProcessOutcome(acme/payments backfill): %v", err)
	}
	if strings.Contains(resSec.PRMarkdown, "severity: block") || !strings.Contains(resSec.PRMarkdown, "repo: acme/payments") {
		t.Fatalf("expected secondary backfill PRMarkdown to have repo: acme/payments and capped warn guard:\n%s", resSec.PRMarkdown)
	}

	dreamRep, err := cur.RunDream(ctx)
	if err != nil {
		t.Fatalf("RunDream: %v", err)
	}
	if len(dreamRep.Upserted) < 2 {
		t.Fatalf("expected both primary and secondary records upserted in RunDream, got %+v", dreamRep)
	}
	mockHS.mu.Lock()
	secDoc, hasSecDoc := mockHS.docs["okf:acme/payments:"+resSec.RecordID]
	_, misattributed := mockHS.docs["okf:acme/gitops:"+resSec.RecordID]
	mockHS.mu.Unlock()
	if !hasSecDoc || misattributed {
		t.Fatalf("expected %s under okf:acme/payments: (has=%v) and never okf:acme/gitops: (misattributed=%v)", resSec.RecordID, hasSecDoc, misattributed)
	}
	if !matchesTags(secDoc.Tags, []string{"repo:acme/payments", "guard:warn"}, "all_strict") {
		t.Fatalf("expected repo:acme/payments and guard:warn tags on secondary doc, got %v", secDoc.Tags)
	}

	// 3. FromLocal: true with 0 records must NOT trigger broker snapshot pull.
	hitsBefore := brokerHits.Load()
	emptyLocalResp := cur.IndexRepo(ctx, MemoryQueryRequest{
		Action:    "index",
		Repo:      "acme/empty-local",
		Role:      RoleManaged,
		FromLocal: true,
		Records:   nil,
	})
	if !emptyLocalResp.OK || brokerHits.Load() != hitsBefore {
		t.Fatalf("expected FromLocal=true with 0 records to skip broker (hits %d -> %d), resp=%+v", hitsBefore, brokerHits.Load(), emptyLocalResp)
	}

	// 4. Explicit VerifiedRepoDir isolates secondary repo under <VerifiedRepoDir>/<slug> in both sync orders.
	if rSec := cur.IndexRepo(ctx, MemoryQueryRequest{Action: "index", Repo: "acme/payments", Role: RoleManaged}); !rSec.OK {
		t.Fatalf("IndexRepo(acme/payments): %+v", rSec)
	}
	if rPri := cur.IndexRepo(ctx, MemoryQueryRequest{Action: "index", Repo: "acme/gitops", Role: RoleManaged}); !rPri.OK {
		t.Fatalf("IndexRepo(acme/gitops): %+v", rPri)
	}
	if _, err := os.Stat(filepath.Join(explicitVDir, "docs", "adr", "0001-primary.md")); err != nil {
		t.Fatalf("expected primary ADR in explicit VerifiedRepoDir: %v", err)
	}
	if _, err := os.Stat(filepath.Join(explicitVDir, "acme__payments", "docs", "adr", "0002-secondary.md")); err != nil {
		t.Fatalf("expected secondary ADR isolated under explicit VerifiedRepoDir/acme__payments: %v", err)
	}
}
