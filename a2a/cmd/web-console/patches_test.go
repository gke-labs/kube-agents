package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

func TestPatchesGetAndPostWorkflow(t *testing.T) {
	var mu sync.Mutex
	annotations := make(map[string]string)

	fakeAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		mu.Lock()
		defer mu.Unlock()

		if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent") {
			pa := map[string]any{
				"metadata": map[string]any{
					"name":        "platform-agent",
					"annotations": annotations,
				},
				"spec": map[string]any{
					"deployment": map[string]any{
						"tag": "0.13.0",
					},
				},
			}
			_ = json.NewEncoder(w).Encode(pa)
			return
		}

		if r.Method == http.MethodPatch && strings.HasPrefix(r.URL.Path, "/apis/kubeagents.x-k8s.io/v1alpha1/namespaces/kubeagents-system/platformagents/platform-agent") {
			var patch struct {
				Metadata struct {
					Annotations map[string]string `json:"annotations"`
				} `json:"metadata"`
			}
			if err := json.NewDecoder(r.Body).Decode(&patch); err == nil {
				for k, v := range patch.Metadata.Annotations {
					annotations[k] = v
				}
			}
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
			return
		}

		http.NotFound(w, r)
	}))
	defer fakeAPI.Close()

	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ReleaseRepo:       "gke-labs/kube-agents",
		ImageTag:          "0.13.0",
	})
	s.kube = &kubeClient{base: fakeAPI.URL, token: "sa-token", client: fakeAPI.Client()}

	// 1. Initial GET - empty patches list
	req := httptest.NewRequest(http.MethodGet, "/api/patches", nil)
	rec := httptest.NewRecorder()
	s.handlePatchesGet(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var resp patchesResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.IsPatched {
		t.Errorf("expected is_patched=false initially")
	}
	if resp.RunningDisplay != "0.13.0" {
		t.Errorf("expected running_display='0.13.0', got %q", resp.RunningDisplay)
	}
	if len(resp.AppliedPRs) != 0 {
		t.Errorf("expected 0 applied PRs, got %d", len(resp.AppliedPRs))
	}

	// 2. POST - Add PR #3284
	addBody := `{"action":"add","pr":3284,"issue":3121,"title":"feat(web-console): Scion Chat UI","component":"web-console"}`
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(addBody))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !resp.IsPatched {
		t.Errorf("expected is_patched=true after add")
	}
	if resp.RunningDisplay != "0.13.0+" {
		t.Errorf("expected running_display='0.13.0+', got %q", resp.RunningDisplay)
	}
	if len(resp.AppliedPRs) != 1 {
		t.Fatalf("expected 1 applied PR, got %d", len(resp.AppliedPRs))
	}
	pr1 := resp.AppliedPRs[0]
	if pr1.PR != 3284 || pr1.Issue != 3121 || pr1.Component != "web-console" {
		t.Errorf("unexpected pr data: %+v", pr1)
	}
	if pr1.URL != "https://github.com/gke-labs/kube-agents/pull/3284" {
		t.Errorf("unexpected default URL: %q", pr1.URL)
	}
	if pr1.IssueURL != "https://github.com/gke-labs/kube-agents/issues/3121" {
		t.Errorf("unexpected default issue URL: %q", pr1.IssueURL)
	}

	// 3. POST - Add second PR #3352
	addBody2 := `{"action":"add","pr":3352,"issue":3350,"title":"perf(chat): fast identity","component":"agents/chat"}`
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(addBody2))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var resp2 patchesResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp2); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(resp2.AppliedPRs) != 2 {
		t.Fatalf("expected 2 applied PRs, got %d", len(resp2.AppliedPRs))
	}
	if resp2.RunningDisplay != "0.13.0+" {
		t.Errorf("expected running_display='0.13.0+', got %q", resp2.RunningDisplay)
	}

	// 4. Verify GET /api/patches reflects both
	req = httptest.NewRequest(http.MethodGet, "/api/patches", nil)
	rec = httptest.NewRecorder()
	s.handlePatchesGet(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var respGet patchesResponse
	if err := json.NewDecoder(rec.Body).Decode(&respGet); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !respGet.IsPatched || len(respGet.AppliedPRs) != 2 || respGet.RunningDisplay != "0.13.0+" {
		t.Errorf("unexpected GET response: %+v", respGet)
	}

	// 5. POST - Remove PR #3284
	remBody := `{"action":"remove","pr":3284}`
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(remBody))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var respRem patchesResponse
	if err := json.NewDecoder(rec.Body).Decode(&respRem); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(respRem.AppliedPRs) != 1 || respRem.AppliedPRs[0].PR != 3352 {
		t.Errorf("expected only PR 3352 remaining, got %+v", respRem.AppliedPRs)
	}

	// 6. POST - Remove remaining PR #3352
	remBody2 := `{"action":"remove","pr":3352}`
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(remBody2))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var respClean patchesResponse
	if err := json.NewDecoder(rec.Body).Decode(&respClean); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if respClean.IsPatched {
		t.Errorf("expected is_patched=false after removing all")
	}
	if respClean.RunningDisplay != "0.13.0" {
		t.Errorf("expected running_display='0.13.0', got %q", respClean.RunningDisplay)
	}
}

func TestPatchesValidationAndErrors(t *testing.T) {
	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
	})

	// Bad JSON
	req := httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader("not json"))
	rec := httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for bad json, got %d", rec.Code)
	}

	// Invalid action
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(`{"action":"destroy"}`))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for invalid action, got %d", rec.Code)
	}

	// Missing PR number for add
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(`{"action":"add","pr":0}`))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for pr=0, got %d", rec.Code)
	}

	// Missing PR number for remove
	req = httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(`{"action":"remove","pr":-1}`))
	rec = httptest.NewRecorder()
	s.handlePatchesPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for pr=-1 on remove, got %d", rec.Code)
	}
}

func TestPatchesIntegratedWithRelease(t *testing.T) {
	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ReleaseRepo:       "gke-labs/kube-agents",
		ImageTag:          "0.13.0",
		ReleaseCheck:      false,
	})

	// Initial release without patches
	ctx := context.Background()
	rel := s.readRelease(ctx)
	if rel.IsPatched || rel.RunningDisplay != "0.13.0" {
		t.Errorf("expected unpatched, got %+v", rel)
	}

	// Add patch via saveAppliedPRs
	err := s.saveAppliedPRs(ctx, []AppliedPR{
		{PR: 3284, Title: "feat(web-console): Scion Chat UI"},
	})
	if err != nil {
		t.Fatalf("saveAppliedPRs: %v", err)
	}

	// Now release should show 0.13.0+ and IsPatched=true
	rel2 := s.readRelease(ctx)
	if !rel2.IsPatched {
		t.Errorf("expected IsPatched=true, got false")
	}
	if rel2.RunningDisplay != "0.13.0+" {
		t.Errorf("expected RunningDisplay='0.13.0+', got %q", rel2.RunningDisplay)
	}
	if len(rel2.AppliedPRs) != 1 {
		t.Errorf("expected 1 applied PR, got %d", len(rel2.AppliedPRs))
	}
	if !strings.Contains(rel2.Detail, "1 PR patch applied directly") {
		t.Errorf("expected detail to mention PR patch, got %q", rel2.Detail)
	}
}

func TestPatchesTransientReadErrorDoesNotOverwriteAnnotation(t *testing.T) {
	var mu sync.Mutex
	failGet := true
	patchCalled := false

	fakeAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		if r.Method == http.MethodGet {
			if failGet {
				w.WriteHeader(http.StatusInternalServerError)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"metadata": map[string]any{
					"annotations": map[string]string{
						appliedPRsAnnotation: `[{"pr":1001,"title":"existing"}]`,
					},
				},
			})
			return
		}
		if r.Method == http.MethodPatch {
			patchCalled = true
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok"})
			return
		}
		http.NotFound(w, r)
	}))
	defer fakeAPI.Close()

	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
	})
	s.kube = &kubeClient{base: fakeAPI.URL, token: "sa-token", client: fakeAPI.Client()}

	// First GET fails transiently; patchesLoaded must stay false
	if prs := s.getAppliedPRs(context.Background()); len(prs) != 0 {
		t.Fatalf("expected 0 PRs on failed GET, got %d", len(prs))
	}
	if s.patchesLoaded {
		t.Fatalf("patchesLoaded was set to true on transient cluster read failure")
	}
	if patchCalled {
		t.Fatalf("unexpected PATCH during GET")
	}

	// POST add while GET is failing must return 500 without sending PATCH
	postReq := httptest.NewRequest(http.MethodPost, "/api/patches", strings.NewReader(`{"action":"add","pr":2002,"title":"new"}`))
	postRec := httptest.NewRecorder()
	s.handlePatchesPost(postRec, postReq)
	if postRec.Code != http.StatusInternalServerError {
		t.Fatalf("handlePatchesPost during transient read failure status = %d, want 500", postRec.Code)
	}
	if patchCalled {
		t.Fatalf("unexpected PATCH sent when pre-write cluster read failed")
	}

	// Subsequent GET succeeds and loads PR #1001
	mu.Lock()
	failGet = false
	mu.Unlock()
	prs := s.getAppliedPRs(context.Background())
	if len(prs) != 1 || prs[0].PR != 1001 {
		t.Fatalf("expected existing PR #1001 after recovery, got %+v", prs)
	}
}
