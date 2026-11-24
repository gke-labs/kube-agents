package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/memory"
	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

func startCLIJetStream(t *testing.T) string {
	t.Helper()
	ns, err := server.NewServer(&server.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		NoLog:     true,
		NoSigs:    true,
		JetStream: true,
		StoreDir:  t.TempDir(),
	})
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
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	_, _ = js.CreateStream(ctx, jetstream.StreamConfig{
		Name:              lib.StreamTopicsState,
		Subjects:          []string{memory.SubjectBlueprint},
		MaxMsgsPerSubject: 8,
		AllowDirect:       true,
	})
	_, _ = js.CreateStream(ctx, jetstream.StreamConfig{
		Name:        lib.StreamTopicsJournal,
		Subjects:    []string{memory.SubjectAnnotations},
		AllowDirect: true,
	})
	return ns.ClientURL()
}

func captureStdout(t *testing.T, fn func() error) (string, error) {
	t.Helper()
	orig := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	runErr := fn()
	_ = w.Close()
	os.Stdout = orig
	var buf bytes.Buffer
	_, _ = io.Copy(&buf, r)
	_ = r.Close()
	return buf.String(), runErr
}

func TestBusIdentityRequired(t *testing.T) {
	t.Setenv(lib.EnvBusUser, "")
	t.Setenv("NATS_USER", "")
	t.Setenv("A2A_POD_NAME", "spoofed-pod-name")
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")

	if got := busUser(); got != "" {
		t.Fatalf("busUser() = %q, want empty when A2A_BUS_USER and NATS_USER are unset", got)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	_, err := connectNATS(context.Background(), "test", log)
	if err == nil || !strings.Contains(err.Error(), "no bus identity") {
		t.Fatalf("expected no bus identity error, got %v", err)
	}
}

func TestRunEmitPropagatesCorrelationIDAndPublishedSubject(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "agent")

	ctx := context.Background()
	responder, err := lib.Connect(ctx, natsURL, lib.WithName("emit-responder"))
	if err != nil {
		t.Fatal(err)
	}
	defer responder.Close()

	var mu sync.Mutex
	var seenCorr string
	sub, err := responder.SubscribeCore(memory.SubjectMemoryQuery, func(msg *nats.Msg) {
		var req memory.MemoryQueryRequest
		_ = json.Unmarshal(msg.Data, &req)
		mu.Lock()
		seenCorr = req.CorrelationID
		mu.Unlock()
		reply, _ := json.Marshal(memory.MemoryQueryResponse{
			Schema: memory.MemoryQueryReplySchema,
			Outcome: &memory.OutcomeProcessResult{
				RecordID: "DEC-2026-055",
				Status:   memory.StatusProposed,
				Title:    "Test emit correlation",
			},
		})
		_ = msg.Respond(reply)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Stop()

	stdout, err := captureStdout(t, func() error {
		return runEmit([]string{"--kind", "proposed", "--title", "Test emit correlation", "--rule", "Keep replicas >= 2"})
	})
	if err != nil {
		t.Fatalf("runEmit failed: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(stdout), &out); err != nil {
		t.Fatalf("decode runEmit output %q: %v", stdout, err)
	}
	if out["published"] != memory.SubjectMemoryQuery {
		t.Errorf("published = %v, want %q", out["published"], memory.SubjectMemoryQuery)
	}
	mu.Lock()
	defer mu.Unlock()
	if seenCorr == "" || out["correlation_id"] != seenCorr {
		t.Errorf("correlation_id = %v, responder saw %q", out["correlation_id"], seenCorr)
	}
}

func TestRunCheckHonorsExplicitKnowledgeRepo(t *testing.T) {
	targetRepo := t.TempDir()
	// Populate targetRepo with an empty knowledge/ directory AND .kube-agents/intent.yaml
	// to verify explicit --knowledge-repo is NOT clobbered by auto-detection.
	if err := os.MkdirAll(filepath.Join(targetRepo, "knowledge", "decisions"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(targetRepo, ".kube-agents"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(targetRepo, ".kube-agents", "intent.yaml"), []byte("version: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(targetRepo, "apps"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(targetRepo, "apps", "deploy.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	externalKnowledge := t.TempDir()
	extRec := memory.Record{
		Meta: memory.RecordMeta{
			ID: "DEC-2026-088", Type: memory.RecordTypeDecision, Status: memory.StatusActive,
			Title: "External policy store replica floor", Date: "2026-11-11",
			Scope: memory.RecordScope{Clusters: []string{"*"}, Paths: []string{"apps/*.yaml"}},
			Guard: &memory.RecordGuard{
				Rule:     "Keep replicas >= 2",
				Severity: "block",
				Expect:   &memory.GuardExpect{File: "apps/*.yaml", Path: "spec.replicas", Op: ">=", Value: 2},
			},
		},
		Body: "## Summary\nExternal policy store rule.\n",
	}
	if err := memory.SaveRecord(externalKnowledge, &extRec); err != nil {
		t.Fatal(err)
	}

	_, err := captureStdout(t, func() error {
		return runCheck([]string{"--repo", targetRepo, "--knowledge-repo", externalKnowledge, "--", "apps/deploy.yaml"})
	})
	exitErr, ok := err.(*exitCodeError)
	if !ok || exitErr.code != exitCodeBlocked {
		t.Fatalf("expected exitCodeError(%d) from explicit --knowledge-repo, got %v", exitCodeBlocked, err)
	}
}

func TestRunRecallDirectHindsightFiltersClusterPathAndEmptyPositionalQuery(t *testing.T) {
	repoDir := t.TempDir()
	les31 := memory.Record{
		Meta: memory.RecordMeta{
			ID: "LES-2026-031", Type: memory.RecordTypeLesson, Status: memory.StatusActive,
			Title: "EU ingress lesson", Date: "2026-11-11",
			Scope: memory.RecordScope{Clusters: []string{"prod-eu"}, Paths: []string{"clusters/prod-eu/**"}},
		},
		Body: "## Summary\nEU ingress lesson.\n",
	}
	if err := memory.SaveRecord(repoDir, &les31); err != nil {
		t.Fatal(err)
	}
	les32 := memory.Record{
		Meta: memory.RecordMeta{
			ID: "LES-2026-032", Type: memory.RecordTypeLesson, Status: memory.StatusActive,
			Title: "US ingress lesson", Date: "2026-11-11",
			Scope: memory.RecordScope{Clusters: []string{"prod-us"}, Paths: []string{"clusters/prod-us/**"}},
		},
		Body: "## Summary\nUS ingress lesson.\n",
	}
	if err := memory.SaveRecord(repoDir, &les32); err != nil {
		t.Fatal(err)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"results": []memory.HindsightRecallResult{
				{ID: "conv-untagged", Text: "Untagged conversational note", Tags: []string{"scope:shared"}},
				{ID: "LES-2026-032", DocumentID: "okf:acme/gitops:LES-2026-032", Text: "US ingress lesson", Tags: []string{"scope:shared", "cluster:prod-us"}},
				{ID: "LES-2026-031", DocumentID: "okf:acme/gitops:LES-2026-031", Text: "EU ingress lesson", Tags: []string{"scope:shared", "cluster:prod-eu"}},
			},
		})
	}))
	defer srv.Close()

	stdout, err := captureStdout(t, func() error {
		return runRecall([]string{
			"--direct-hindsight",
			"--hindsight-url", srv.URL,
			"--repo", repoDir,
			"--repo-id", "acme/gitops",
			"--cluster", "prod-eu",
			"--path", "clusters/prod-eu/ingress.yaml",
			"--max-results", "1",
		})
	})
	if err != nil {
		t.Fatalf("runRecall failed: %v", err)
	}
	var resp struct {
		Results []memory.HindsightRecallResult `json:"results"`
	}
	if err := json.Unmarshal([]byte(stdout), &resp); err != nil {
		t.Fatalf("decode runRecall output: %v", err)
	}
	if len(resp.Results) != 1 || resp.Results[0].ID != "LES-2026-031" {
		t.Fatalf("expected only LES-2026-031 after --cluster/--path/--max-results filtering, got %+v", resp.Results)
	}
}

func TestDaemonStatusEndpoints(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "curator")

	// Pick a free ephemeral port for --status-listen.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- runDaemonContext(ctx, []string{"--repo", t.TempDir(), "--status-listen", addr})
	}()

	httpClient := &http.Client{
		Timeout:   2 * time.Second,
		Transport: &http.Transport{DisableKeepAlives: true},
	}

	deadline := time.Now().Add(5 * time.Second)
	readyOK := false
	for time.Now().Before(deadline) {
		resp, gErr := httpClient.Get("http://" + addr + "/readyz")
		if gErr == nil {
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				readyOK = true
				break
			}
		}
		time.Sleep(25 * time.Millisecond)
	}
	if !readyOK {
		t.Fatalf("curator daemon /readyz did not become 200 OK on %s", addr)
	}
	hResp, err := httpClient.Get("http://" + addr + "/healthz")
	if err != nil || hResp.StatusCode != http.StatusOK {
		t.Fatalf("/healthz failed: resp=%+v err=%v", hResp, err)
	}
	_, _ = io.Copy(io.Discard, hResp.Body)
	_ = hResp.Body.Close()

	mResp, err := httpClient.Get("http://" + addr + "/metrics")
	if err != nil || mResp.StatusCode != http.StatusOK {
		t.Fatalf("/metrics failed: resp=%+v err=%v", mResp, err)
	}
	mBytes, rErr := io.ReadAll(mResp.Body)
	_ = mResp.Body.Close()
	if rErr != nil {
		t.Fatalf("read /metrics body: %v", rErr)
	}
	mText := string(mBytes)
	for _, series := range []string{
		"kubeagents_curator_outcomes_total",
		"kubeagents_curator_records",
		"kubeagents_curator_graduations_total",
		"kubeagents_curator_queries_total",
		"process_start_time_seconds",
	} {
		if !strings.Contains(mText, series) {
			t.Errorf("/metrics output missing %q:\n%s", series, mText)
		}
	}

	cancel()
	select {
	case dErr := <-done:
		if dErr != nil {
			t.Fatalf("runDaemonContext returned error on shutdown: %v", dErr)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("runDaemonContext did not shut down within 10s of context cancel")
	}
}

func TestRunCheckOverseerCapsLLMFindingsAtWarnAndDreamSubcommand(t *testing.T) {
	repoDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repoDir, "clusters", "prod"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoDir, "clusters", "prod", "app.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: app\nspec:\n  replicas: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Seed a prose-only active record with severity: block (like mergeGuard sets on crash evidence).
	proseBlockRec := memory.Record{
		Meta: memory.RecordMeta{
			ID:     "DEC-2026-040",
			Type:   memory.RecordTypeDecision,
			Status: memory.StatusActive,
			Title:  "Prose-only rule with block severity",
			Date:   "2026-11-12",
			Scope:  memory.RecordScope{Paths: []string{"clusters/**"}},
			Guard: &memory.RecordGuard{
				Rule:     "Do not change replica counts on Fridays.",
				Severity: "block",
			},
		},
		Body: "## Context\n\nProse-only rule.\n",
	}
	if err := memory.SaveRecord(repoDir, &proseBlockRec); err != nil {
		t.Fatal(err)
	}
	diffPath := filepath.Join(repoDir, "change.diff")
	if err := os.WriteFile(diffPath, []byte("+  replicas: 1\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	llmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"violations\":[{\"id\":\"DEC-2026-040\",\"severity\":\"block\",\"rule\":\"Do not change replica counts on Fridays.\",\"reason\":\"Changed replicas on Friday\"}]}"}}]}`))
	}))
	defer llmSrv.Close()

	t.Setenv("DECISION_MEMORY_LLM_URL", llmSrv.URL)
	t.Setenv("DECISION_MEMORY_LLM_MODEL", "test-model")

	// High 1: LLM Overseer finding must be capped at "warn" (exit 0, Verdict: "warn"), never "block" (exit 2).
	stdout, err := captureStdout(t, func() error {
		return runCheck([]string{"--repo", repoDir, "--overseer", "--diff-file", diffPath, "--", "clusters/prod/app.yaml"})
	})
	if err != nil {
		t.Fatalf("expected LLM Overseer finding to be advisory (exit 0), got err=%v", err)
	}
	var checkOut memory.CheckReport
	if err := json.Unmarshal([]byte(stdout), &checkOut); err != nil {
		t.Fatalf("decode runCheck output: %v", err)
	}
	if checkOut.Verdict != "warn" || len(checkOut.Findings) != 1 || checkOut.Findings[0].Severity != "warn" {
		t.Fatalf("expected LLM finding capped at warn, got verdict=%q findings=%+v", checkOut.Verdict, checkOut.Findings)
	}

	// Low 1: Verify `curator dream` subcommand audits active guards against HEAD and outputs a DreamReport.
	driftedRec := memory.Record{
		Meta: memory.RecordMeta{
			ID:     "DEC-2026-041",
			Type:   memory.RecordTypeDecision,
			Status: memory.StatusActive,
			Title:  "Drifted replica guard",
			Date:   "2026-11-12",
			Scope:  memory.RecordScope{Paths: []string{"clusters/**"}},
			Guard: &memory.RecordGuard{
				Rule:     "Keep replicas >= 3",
				Severity: "block",
				Expect:   &memory.GuardExpect{File: "clusters/**/*.yaml", Path: "spec.replicas", Op: ">=", Value: 3},
			},
		},
		Body: "## Context\n\nDrifted guard.\n",
	}
	if err := memory.SaveRecord(repoDir, &driftedRec); err != nil {
		t.Fatal(err)
	}
	dreamOut, err := captureStdout(t, func() error {
		return run([]string{"dream", "--repo", repoDir, "--repo-id", "acme/gitops"})
	})
	if err != nil {
		t.Fatalf("curator dream failed: %v", err)
	}
	var dreamRep memory.DreamReport
	if err := json.Unmarshal([]byte(dreamOut), &dreamRep); err != nil {
		t.Fatalf("decode curator dream output: %v", err)
	}
	if len(dreamRep.DemotedDrifted) != 1 || dreamRep.DemotedDrifted[0] != "DEC-2026-041" {
		t.Fatalf("expected DEC-2026-041 in demoted_drifted, got %+v", dreamRep)
	}
}

func TestEmitBackfillPendingAndIndexRoleViaDaemonAndConfigFallback(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "curator")
	t.Setenv("HINDSIGHT_API_URL", "")
	t.Setenv("DECISION_MEMORY_HINDSIGHT_URL", "")

	// Configure Hindsight URL via <PLATFORM_AGENT_HOME>/hindsight/config.json fallback.
	var hsMu sync.Mutex
	retainedDocs := map[string][]string{}
	hsMux := http.NewServeMux()
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/config", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/memories", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Items []struct {
				DocumentID string   `json:"document_id"`
				Tags       []string `json:"tags"`
			} `json:"items"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		hsMu.Lock()
		for _, it := range body.Items {
			retainedDocs[it.DocumentID] = it.Tags
		}
		hsMu.Unlock()
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/documents", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"items":[]}`))
	})
	hsSrv := httptest.NewServer(hsMux)
	defer hsSrv.Close()

	agentHome := t.TempDir()
	if err := os.MkdirAll(filepath.Join(agentHome, "hindsight"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(agentHome, "hindsight", "config.json"), []byte(`{"api_url":"`+hsSrv.URL+`"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLATFORM_AGENT_HOME", agentHome)

	if gotURL := defaultHindsightURL(); gotURL != hsSrv.URL {
		t.Fatalf("defaultHindsightURL() = %q, want %q from config.json", gotURL, hsSrv.URL)
	}

	daemonStore := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	done := make(chan error, 1)
	go func() {
		done <- runDaemonContext(ctx, []string{"--repo", daemonStore, "--repo-id", "acme/gitops", "--status-listen", addr})
	}()
	defer func() {
		cancel()
		<-done
	}()

	httpClient := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if resp, rErr := httpClient.Get("http://" + addr + "/readyz"); rErr == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}

	// 1. curator emit --outcome backfill stamps outcome="backfill" and surfaces it in curator pending.
	emitOut, err := captureStdout(t, func() error {
		return run([]string{
			"emit",
			"--kind", "proposed",
			"--type", "lesson",
			"--outcome", "backfill",
			"--repo", "acme/gitops",
			"--pr", "https://github.com/acme/gitops/pull/42 (#42)",
			"--title", "Backfilled revert lesson",
			"--rule", "Pin image digest before rollout",
			"--paths", "clusters/**",
		})
	})
	if err != nil {
		t.Fatalf("curator emit --outcome backfill failed: %v", err)
	}
	var emitJSON struct {
		RecordID string `json:"record_id"`
	}
	if err := json.Unmarshal([]byte(emitOut), &emitJSON); err != nil || emitJSON.RecordID == "" {
		t.Fatalf("decode emit output %q: %v", emitOut, err)
	}

	pendingOut, err := captureStdout(t, func() error {
		return run([]string{"pending", "--repo", daemonStore, "--repo-id", "acme/gitops"})
	})
	if err != nil {
		t.Fatalf("curator pending failed: %v", err)
	}
	var pendingJSON struct {
		Records []memory.BlueprintRecordDigest `json:"records"`
	}
	if err := json.Unmarshal([]byte(pendingOut), &pendingJSON); err != nil {
		t.Fatalf("decode pending output %q: %v", pendingOut, err)
	}
	if len(pendingJSON.Records) != 1 || pendingJSON.Records[0].ID != emitJSON.RecordID || pendingJSON.Records[0].Outcome != "backfill" {
		t.Fatalf("expected pending record %s with outcome=backfill, got %+v", emitJSON.RecordID, pendingJSON.Records)
	}

	// Also verify local fallback (with NATS_URL temporarily cleared) surfaces outcome="backfill".
	t.Setenv("NATS_URL", "")
	localPendingOut, err := captureStdout(t, func() error {
		return run([]string{"pending", "--repo", daemonStore, "--repo-id", "acme/gitops"})
	})
	if err != nil {
		t.Fatalf("local curator pending failed: %v", err)
	}
	var localPendingJSON struct {
		Records []memory.BlueprintRecordDigest `json:"records"`
	}
	if err := json.Unmarshal([]byte(localPendingOut), &localPendingJSON); err != nil {
		t.Fatalf("decode local pending output: %v", err)
	}
	if len(localPendingJSON.Records) != 1 || localPendingJSON.Records[0].Outcome != "backfill" {
		t.Fatalf("expected local pending fallback to preserve outcome=backfill, got %+v", localPendingJSON.Records)
	}
	t.Setenv("NATS_URL", natsURL)

	// 2. curator index --role context routes over NATS to daemon and strips blocking guards.
	ctxSnapDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(ctxSnapDir, "docs", "runbooks"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ctxSnapDir, "docs", "runbooks", "0005-node-drain.md"), []byte("# Node drain runbook\n\nCheck PDB status before cordoning nodes.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(ctxSnapDir, "knowledge", "decision"), 0o755); err != nil {
		t.Fatal(err)
	}
	ctxGuardMD := "---\ntype: decision\nid: DEC-2026-009\ntitle: Context guard\nstatus: active\ndate: \"2026-11-20\"\nscope:\n  paths:\n    - apps/**\nguard:\n  rule: Block rule from context repo\n  severity: block\n  expect:\n    file: apps/*.yaml\n    path: spec.replicas\n    op: \">=\"\n    value: 5\n---\n\nContext decision.\n"
	if err := os.WriteFile(filepath.Join(ctxSnapDir, "knowledge", "decision", "DEC-2026-009.md"), []byte(ctxGuardMD), 0o600); err != nil {
		t.Fatal(err)
	}

	idxOut, err := captureStdout(t, func() error {
		return run([]string{"index", "--repo", ctxSnapDir, "--repo-id", "acme/handbook", "--role", "context"})
	})
	if err != nil {
		t.Fatalf("curator index --role context failed: %v", err)
	}
	var idxReceipt map[string]any
	if err := json.Unmarshal([]byte(idxOut), &idxReceipt); err != nil {
		t.Fatalf("decode index receipt %q: %v", idxOut, err)
	}
	if idxReceipt["repo_id"] != "acme/handbook" || idxReceipt["role"] != "context" || int(idxReceipt["indexed"].(float64)) != 2 {
		t.Fatalf("unexpected index receipt: %+v", idxReceipt)
	}

	hsMu.Lock()
	ctxTags := retainedDocs["okf:acme/handbook:DEC-2026-009"]
	runTags := retainedDocs["okf:acme/handbook:RUN-0005"]
	hsMu.Unlock()
	if len(ctxTags) == 0 || len(runTags) == 0 {
		t.Fatalf("expected both DEC-2026-009 and RUN-0005 in Hindsight, got %+v", retainedDocs)
	}
	for _, tag := range ctxTags {
		if tag == "guard:block" || tag == "status:active" {
			t.Fatalf("context record retained forbidden tag %q in %v", tag, ctxTags)
		}
	}
}

func TestExtractRepoSlugAndDefaultRepoIDNonGitHubHosts(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"https://github.com/acme/infra.git", "acme/infra"},
		{"https://www.github.com/acme/infra", "acme/infra"},
		{"ssh://git@ssh.github.com:443/acme/infra.git", "acme/infra"},
		{"git@github.com:acme/infra.git", "acme/infra"},
		{"github.com/acme/infra", "acme/infra"},
		{"github.com//acme/infra", "acme/infra"},
		{"acme/infra", "acme/infra"},
		{"my.org/infra", "my.org/infra"},
		{"https://gitlab.com/org/repo.git", "gitlab.com/org/repo"},
		{"https://gitlab.com/org/sub/repo.git", "gitlab.com/org/sub/repo"},
		{"git@gitlab.com:org/repo.git", "gitlab.com/org/repo"},
		{"gitlab.com/org/repo", "gitlab.com/org/repo"},
		{"gitlab.com//org/repo.git", "gitlab.com/org/repo"},
		{"https://token@gitlab.com/org/repo.git", "gitlab.com/org/repo"},
		{"user:secret@gitlab.com/org/repo.git", "gitlab.com/org/repo"},
		{"token@github.com//acme/infra.git", "acme/infra"},
		{"https://user:bad%zz@github.com/acme/infra.git", ""},
		{"https:///acme/infra", ""},
		{"github.com/acme/../other", ""},
		{"user@notahost/repo", ""},
		{"https://gitea.internal:8443/platform/gitops.git", "gitea.internal/platform/gitops"},
		{"ssh://git@gitlab.corp.example:2222/team/sub/cluster.git", "gitlab.corp.example/team/sub/cluster"},
	}
	for _, tc := range cases {
		if got := extractRepoSlug(tc.in); got != tc.want {
			t.Errorf("extractRepoSlug(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}

	t.Setenv("CURATOR_REPO_ID", "")
	stateDir := t.TempDir()
	stateFile := filepath.Join(stateDir, "managed_repos")
	if err := os.WriteFile(stateFile, []byte(`[{"type":"gitlab","role":"gitops","url":"https://gitlab.com/org/gitops-fleet.git"}]`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GITOPS_STATE_PATH", stateFile)
	if got := defaultRepoID(); got != "gitlab.com/org/gitops-fleet" {
		t.Fatalf("defaultRepoID() = %q, want %q", got, "gitlab.com/org/gitops-fleet")
	}
}

func TestRunEmitCommitFlagGuardSeverityCapAndRunIndexContextRepoIsolation(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "curator")

	llmSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"title\":\"LLM block guard\",\"context\":\"OOM exit 137\",\"decision\":\"Replicas >= 2\",\"guard\":{\"rule\":\"Replicas >= 2\",\"severity\":\"block\",\"expect\":{\"file\":\"apps/*.yaml\",\"path\":\"spec.replicas\",\"op\":\">=\",\"value\":2}}}"}}]}`))
	}))
	defer llmSrv.Close()
	t.Setenv("DECISION_MEMORY_LLM_URL", llmSrv.URL)
	t.Setenv("DECISION_MEMORY_LLM_MODEL", "test-model")

	var hsMu sync.Mutex
	retainedDocs := map[string][]string{}
	hsMux := http.NewServeMux()
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/config", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/memories", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Items []struct {
				DocumentID string   `json:"document_id"`
				Tags       []string `json:"tags"`
			} `json:"items"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		hsMu.Lock()
		for _, it := range body.Items {
			retainedDocs[it.DocumentID] = it.Tags
		}
		hsMu.Unlock()
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	hsMux.HandleFunc("/v1/default/banks/kube-agents-decisions/documents", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"items":[]}`))
	})
	hsSrv := httptest.NewServer(hsMux)
	defer hsSrv.Close()
	t.Setenv("HINDSIGHT_API_URL", hsSrv.URL)

	// Configure a broker URL that fails if called so we verify local `curator index` sets FromLocal=true even on 0 docs.
	var brokerCalled atomic.Bool
	brokerSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		brokerCalled.Store(true)
		http.Error(w, "broker should not be called when FromLocal=true", http.StatusInternalServerError)
	}))
	defer brokerSrv.Close()
	t.Setenv("CREDENTIAL_PROXY_URL", brokerSrv.URL)

	// Set up context_repos state + clone under <PLATFORM_AGENT_HOME>/gitops/lease-1/acme__ctx-handbook.
	agentHome := t.TempDir()
	ctxClone := filepath.Join(agentHome, "gitops", "lease-1", "acme__ctx-handbook")
	if err := os.MkdirAll(filepath.Join(ctxClone, "docs", "adr"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ctxClone, "docs", "adr", "0099-context-only.md"), []byte("# ADR 99: Context handbook ADR\n\nMust never leak into managed index.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	stateDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(stateDir, "context_repos"), []byte(`[{"type":"github","url":"https://github.com/acme/ctx-handbook.git"}]`), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PLATFORM_AGENT_HOME", agentHome)
	t.Setenv("GITOPS_STATE_PATH", filepath.Join(stateDir, "managed_repos"))

	daemonStore := t.TempDir()
	if err := os.MkdirAll(filepath.Join(daemonStore, "apps"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(daemonStore, "apps", "web.yaml"), []byte("apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\nspec:\n  replicas: 3\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- runDaemonContext(ctx, []string{"--repo", daemonStore, "--repo-id", "acme/gitops", "--status-listen", addr})
	}()
	defer func() {
		cancel()
		<-done
	}()

	httpClient := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if resp, rErr := httpClient.Get("http://" + addr + "/readyz"); rErr == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}

	// 1. Verify `curator emit` accepts `--commit` and `--guard-severity warn` (capping LLM block synthesis).
	emitOut, err := captureStdout(t, func() error {
		return run([]string{
			"emit",
			"--kind", "proposed",
			"--repo", "acme/gitops",
			"--pr", "https://github.com/acme/gitops/pull/77 (#77)",
			"--commit", "deadbeef1234567890",
			"--guard-severity", "warn",
			"--title", "OOM exit 137 guard proposal",
			"--summary", "Pod hit OOM exit 137",
			"--paths", "apps/**",
		})
	})
	if err != nil {
		t.Fatalf("curator emit --commit failed: %v", err)
	}
	var emitResp struct {
		RecordID   string              `json:"record_id"`
		PRMarkdown string              `json:"pr_markdown"`
		Event      memory.OutcomeEvent `json:"event"`
	}
	if err := json.Unmarshal([]byte(emitOut), &emitResp); err != nil {
		t.Fatalf("decode emit output %q: %v", emitOut, err)
	}
	if emitResp.Event.Commit != "deadbeef1234567890" || !strings.Contains(emitResp.PRMarkdown, "commit: deadbeef1234567890") {
		t.Fatalf("expected commit deadbeef1234567890 in event and PRMarkdown, got event=%+v md=\n%s", emitResp.Event, emitResp.PRMarkdown)
	}
	if strings.Contains(emitResp.PRMarkdown, "severity: block") || !strings.Contains(emitResp.PRMarkdown, "severity: warn") {
		t.Fatalf("expected --guard-severity warn to cap LLM block synthesis in PRMarkdown:\n%s", emitResp.PRMarkdown)
	}

	// 2. Verify `curator index --role managed` ignores context_repos clones on disk.
	managedSnap := t.TempDir()
	if err := os.MkdirAll(filepath.Join(managedSnap, "docs", "adr"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(managedSnap, "docs", "adr", "0001-managed.md"), []byte("# ADR 1: Managed repo ADR\n\nManaged content.\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	idxOut, err := captureStdout(t, func() error {
		return run([]string{"index", "--repo", managedSnap, "--repo-id", "acme/secondary-managed", "--role", "managed"})
	})
	if err != nil {
		t.Fatalf("curator index --role managed failed: %v", err)
	}
	var idxReceipt map[string]any
	if err := json.Unmarshal([]byte(idxOut), &idxReceipt); err != nil {
		t.Fatal(err)
	}
	if int(idxReceipt["indexed"].(float64)) != 1 {
		t.Fatalf("expected exactly 1 managed doc indexed (no context_repos leak), got %+v", idxReceipt)
	}
	hsMu.Lock()
	_, leaked := retainedDocs["okf:acme/secondary-managed:ADR-0099"]
	_, hasManaged := retainedDocs["okf:acme/secondary-managed:ADR-0001"]
	hsMu.Unlock()
	if leaked || !hasManaged {
		t.Fatalf("expected ADR-0001 only (hasManaged=%v) and zero ADR-0099 context_repos leak (leaked=%v)", hasManaged, leaked)
	}

	// 3. Verify `curator index` on an empty local repo passes FromLocal=true and never calls BrokerURL.
	emptySnap := t.TempDir()
	if _, err := captureStdout(t, func() error {
		return run([]string{"index", "--repo", emptySnap, "--repo-id", "acme/empty-managed", "--role", "managed"})
	}); err != nil {
		t.Fatalf("curator index on empty local dir failed: %v", err)
	}
	if brokerCalled.Load() {
		t.Fatal("expected FromLocal=true on local `curator index` to skip broker snapshot HTTP call")
	}
}

func TestRunEmitDefaultGuardSeverityPreservesBlockAndExplicitWarnCaps(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "curator")
	t.Setenv("DECISION_MEMORY_LLM_URL", "")
	t.Setenv("HINDSIGHT_API_URL", "")
	t.Setenv("DECISION_MEMORY_HINDSIGHT_URL", "")

	daemonStore := t.TempDir()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- runDaemonContext(ctx, []string{"--repo", daemonStore, "--repo-id", "acme/gitops", "--status-listen", addr})
	}()
	defer func() {
		cancel()
		<-done
	}()

	httpClient := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if resp, rErr := httpClient.Get("http://" + addr + "/readyz"); rErr == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}

	// 1. Omitting --guard-severity on resolved-merged with crash evidence leaves ev.GuardSeverity == "" and promotes to severity: block.
	resolvedOut, err := captureStdout(t, func() error {
		return run([]string{
			"emit",
			"--kind", "resolved-merged",
			"--repo", "acme/gitops",
			"--pr", "https://github.com/acme/gitops/pull/91 (#91)",
			"--title", "OOM exit 137 memory floor",
			"--summary", "Container hit OOM exit 137 under load",
			"--rule", "Keep memory limit >= 512Mi",
			"--paths", "apps/**",
		})
	})
	if err != nil {
		t.Fatalf("curator emit resolved-merged failed: %v", err)
	}
	var resolvedResp struct {
		RecordID   string              `json:"record_id"`
		PRMarkdown string              `json:"pr_markdown"`
		Event      memory.OutcomeEvent `json:"event"`
	}
	if err := json.Unmarshal([]byte(resolvedOut), &resolvedResp); err != nil {
		t.Fatalf("decode resolved-merged output: %v", err)
	}
	if resolvedResp.Event.GuardSeverity != "" {
		t.Fatalf("expected omitted --guard-severity to leave ev.GuardSeverity empty, got %q", resolvedResp.Event.GuardSeverity)
	}
	if !strings.Contains(resolvedResp.PRMarkdown, "severity: block") {
		t.Fatalf("expected crash resolved-merged without --guard-severity to preserve severity: block, got:\n%s", resolvedResp.PRMarkdown)
	}

	// 2. Omitting --guard-severity on modified-merged preserves existing severity: block.
	modOut, err := captureStdout(t, func() error {
		return run([]string{
			"emit",
			"--kind", "modified-merged",
			"--id", resolvedResp.RecordID,
			"--repo", "acme/gitops",
			"--pr", "https://github.com/acme/gitops/pull/92 (#92)",
			"--rule", "Keep memory limit >= 1Gi",
		})
	})
	if err != nil {
		t.Fatalf("curator emit modified-merged failed: %v", err)
	}
	var modResp struct {
		PRMarkdown string              `json:"pr_markdown"`
		Event      memory.OutcomeEvent `json:"event"`
	}
	if err := json.Unmarshal([]byte(modOut), &modResp); err != nil {
		t.Fatalf("decode modified-merged output: %v", err)
	}
	if modResp.Event.GuardSeverity != "" || !strings.Contains(modResp.PRMarkdown, "severity: block") {
		t.Fatalf("expected modified-merged without --guard-severity to preserve severity: block, got event=%+v md:\n%s", modResp.Event, modResp.PRMarkdown)
	}

	// 3. Explicit --guard-severity warn caps existing severity: block to warn.
	cappedOut, err := captureStdout(t, func() error {
		return run([]string{
			"emit",
			"--kind", "modified-merged",
			"--id", resolvedResp.RecordID,
			"--repo", "acme/gitops",
			"--pr", "https://github.com/acme/gitops/pull/93 (#93)",
			"--guard-severity", "warn",
			"--rule", "Keep memory limit >= 1Gi",
		})
	})
	if err != nil {
		t.Fatalf("curator emit --guard-severity warn failed: %v", err)
	}
	var cappedResp struct {
		PRMarkdown string              `json:"pr_markdown"`
		Event      memory.OutcomeEvent `json:"event"`
	}
	if err := json.Unmarshal([]byte(cappedOut), &cappedResp); err != nil {
		t.Fatalf("decode capped output: %v", err)
	}
	if cappedResp.Event.GuardSeverity != "warn" {
		t.Fatalf("expected explicit --guard-severity warn to set ev.GuardSeverity=warn, got %q", cappedResp.Event.GuardSeverity)
	}
	if strings.Contains(cappedResp.PRMarkdown, "severity: block") || !strings.Contains(cappedResp.PRMarkdown, "severity: warn") {
		t.Fatalf("expected explicit --guard-severity warn to cap block to warn, got:\n%s", cappedResp.PRMarkdown)
	}
}

func TestRunPendingReturnsAllManagedReposWithoutFlagAndFiltersWithExplicitRepoID(t *testing.T) {
	natsURL := startCLIJetStream(t)
	t.Setenv("NATS_URL", natsURL)
	t.Setenv(lib.EnvBusUser, "curator")
	t.Setenv("CURATOR_REPO_ID", "acme/gitops")
	t.Setenv("HINDSIGHT_API_URL", "")
	t.Setenv("DECISION_MEMORY_HINDSIGHT_URL", "")

	daemonStore := t.TempDir()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- runDaemonContext(ctx, []string{"--repo", daemonStore, "--repo-id", "acme/gitops", "--status-listen", addr})
	}()
	defer func() {
		cancel()
		<-done
	}()

	httpClient := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if resp, rErr := httpClient.Get("http://" + addr + "/readyz"); rErr == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}

	for _, spec := range []struct {
		repo  string
		title string
		pr    string
	}{
		{"acme/gitops", "Primary bare GitHub proposal", "https://github.com/acme/gitops/pull/10 (#10)"},
		{"github.com/acme/gitops", "Primary host-qualified GitHub proposal", "https://github.com/acme/gitops/pull/11 (#11)"},
		{"gitlab.com/acme/payments", "Secondary GitLab managed proposal", "https://gitlab.com/acme/payments/-/merge_requests/12 (!12)"},
	} {
		if _, err := captureStdout(t, func() error {
			return run([]string{
				"emit",
				"--kind", "proposed",
				"--type", "lesson",
				"--repo", spec.repo,
				"--pr", spec.pr,
				"--title", spec.title,
				"--rule", "Rule for " + spec.title,
				"--paths", "apps/**",
			})
		}); err != nil {
			t.Fatalf("emit %s failed: %v", spec.repo, err)
		}
	}

	assertPendingCounts := func(mode string) {
		t.Helper()
		// Without --repo-id: returns all 3 pending records across all repos while preserving envelope "repo".
		allOut, err := captureStdout(t, func() error {
			return run([]string{"pending", "--repo", daemonStore})
		})
		if err != nil {
			t.Fatalf("%s: curator pending without --repo-id failed: %v", mode, err)
		}
		var allJSON struct {
			Repo    string                         `json:"repo"`
			Records []memory.BlueprintRecordDigest `json:"records"`
		}
		if err := json.Unmarshal([]byte(allOut), &allJSON); err != nil {
			t.Fatalf("%s: decode all pending: %v", mode, err)
		}
		if allJSON.Repo != "acme/gitops" || len(allJSON.Records) != 3 {
			t.Fatalf("%s: expected repo=acme/gitops and 3 pending records across all repos, got repo=%q records=%+v", mode, allJSON.Repo, allJSON.Records)
		}

		// With explicit --repo-id acme/gitops (and github.com/acme/gitops): matches both GitHub slug forms (2 records).
		for _, ghQuery := range []string{"acme/gitops", "github.com/acme/gitops"} {
			ghOut, err := captureStdout(t, func() error {
				return run([]string{"pending", "--repo", daemonStore, "--repo-id", ghQuery})
			})
			if err != nil {
				t.Fatalf("%s: curator pending --repo-id %s failed: %v", mode, ghQuery, err)
			}
			var ghJSON struct {
				Records []memory.BlueprintRecordDigest `json:"records"`
			}
			if err := json.Unmarshal([]byte(ghOut), &ghJSON); err != nil {
				t.Fatalf("%s: decode gh pending: %v", mode, err)
			}
			if len(ghJSON.Records) != 2 {
				t.Fatalf("%s: expected 2 GitHub pending records for --repo-id %s, got %+v", mode, ghQuery, ghJSON.Records)
			}
		}

		// With explicit --repo-id gitlab.com/acme/payments: returns only the GitLab record.
		glOut, err := captureStdout(t, func() error {
			return run([]string{"pending", "--repo", daemonStore, "--repo-id", "gitlab.com/acme/payments"})
		})
		if err != nil {
			t.Fatalf("%s: curator pending --repo-id gitlab failed: %v", mode, err)
		}
		var glJSON struct {
			Records []memory.BlueprintRecordDigest `json:"records"`
		}
		if err := json.Unmarshal([]byte(glOut), &glJSON); err != nil {
			t.Fatalf("%s: decode gl pending: %v", mode, err)
		}
		if len(glJSON.Records) != 1 || glJSON.Records[0].Repo != "gitlab.com/acme/payments" {
			t.Fatalf("%s: expected 1 GitLab pending record, got %+v", mode, glJSON.Records)
		}
	}

	assertPendingCounts("nats")
	t.Setenv("NATS_URL", "")
	assertPendingCounts("local-fallback")
}
