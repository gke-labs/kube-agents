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
		Subjects:    []string{memory.SubjectAnnotations, memory.SubjectCuratorOutcomes},
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

