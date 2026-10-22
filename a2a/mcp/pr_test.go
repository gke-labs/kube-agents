package mcp

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// inspect_pr: GitHub PR metadata, diff files, and local patch output.

func TestInspectPullRequestNativeDiffFiles(t *testing.T) {
	const sampleDiff = `diff --git a/clusters/prod/nodepool.yaml b/clusters/prod/nodepool.yaml
index 1111111..2222222 100644
--- a/clusters/prod/nodepool.yaml
+++ b/clusters/prod/nodepool.yaml
@@ -5,3 +5,3 @@ spec:
-  version: "1.31.4-gke.100"
+  version: "1.32.1-gke.200"
`
	ghServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42" && r.Header.Get(acceptHeader) == acceptGitHubDiff:
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(sampleDiff))
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"number":        testPRNumber,
				"title":         "chore(gke): upgrade prod nodepool to 1.32.1",
				"state":         "open",
				"html_url":      testPRURL,
				"body":          "Automated GKE upgrade PR opened by kube-agents.",
				"additions":     1,
				"deletions":     1,
				"changed_files": 1,
				"user":          map[string]any{"login": "kube-agents-bot"},
				"head":          map[string]any{"ref": "upgrade-1.32"},
				"base":          map[string]any{"ref": "main"},
			})
		case r.URL.Path == "/repos/example-org/example-infra/pulls/42/files":
			_ = json.NewEncoder(w).Encode([]map[string]any{
				{
					"filename":  "clusters/prod/nodepool.yaml",
					"additions": 1,
					"deletions": 1,
					"status":    "modified",
				},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(ghServer.Close)

	tmpDir := t.TempDir()
	store, err := NewConfigStore(filepath.Join(tmpDir, "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	mcpSrv, err := NewServer(ServerOptions{Store: store, APIBaseURL: ghServer.URL})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(mcpSrv.Close)

	outDir := filepath.Join(tmpDir, "pr-out")
	args, _ := json.Marshal(map[string]any{
		"pr":         testPRURL,
		"output_dir": outDir,
	})
	res := mcpSrv.CallTool(context.Background(), toolInspectPR, args)
	if res.IsError {
		t.Fatalf("inspect_pr failed: %s", res.Content[0].Text)
	}
	text := res.Content[0].Text
	if !strings.Contains(text, "file://") || !strings.Contains(text, "clusters/prod/nodepool.yaml") {
		t.Errorf("inspect_pr output missing file:// links or filename:\n%s", text)
	}
	if _, err := os.Stat(filepath.Join(outDir, "pr-42.diff")); err != nil {
		t.Errorf("expected pr-42.diff on disk: %v", err)
	}
	if _, err := os.Stat(filepath.Join(outDir, "files", "clusters__prod__nodepool.yaml.patch")); err != nil {
		t.Errorf("expected per-file patch on disk: %v", err)
	}
	summaryBytes, err := os.ReadFile(filepath.Join(outDir, summaryFileName))
	if err != nil {
		t.Fatalf("expected %s on disk: %v", summaryFileName, err)
	}
	if !strings.Contains(string(summaryBytes), summaryFileName) {
		t.Errorf("expected %s to include its own SummaryFilePath link:\n%s", summaryFileName, string(summaryBytes))
	}

	// Verify default output_dir under ArtifactsDir scopes into scratch/prs/<repo>-pr-<num>/
	// so inspecting multiple PRs does not clobber PR_SUMMARY.md or pollute root brain artifacts.
	artDir := filepath.Join(tmpDir, "brain", "conv-1")
	ctxWithMeta := withCallMeta(context.Background(), rpcCallToolMeta{
		ConversationID: "conv-1",
		ArtifactsDir:   artDir,
	})
	defaultArgs, _ := json.Marshal(map[string]any{"pr": testPRURL})
	resDefault := mcpSrv.CallTool(ctxWithMeta, toolInspectPR, defaultArgs)
	if resDefault.IsError {
		t.Fatalf("inspect_pr with default ArtifactsDir failed: %s", resDefault.Content[0].Text)
	}
	expectedSubdir := filepath.Join(artDir, "scratch", "prs", "example-org-example-infra-pr-42")
	if _, err := os.Stat(filepath.Join(expectedSubdir, "pr-42.diff")); err != nil {
		t.Errorf("expected scoped pr-42.diff in %s: %v", expectedSubdir, err)
	}
	if _, err := os.Stat(filepath.Join(artDir, summaryFileName)); err == nil {
		t.Errorf("expected root %s NOT to be polluted with %s", artDir, summaryFileName)
	}

	// Verify sanitizePatchFilename strips traversal segments and caps >255-byte filenames.
	longPath := "../../" + strings.Repeat("deeply/nested/kubernetes/manifest/directory/", 8) + "deployment.yaml"
	safePatch := sanitizePatchFilename(longPath)
	if strings.Contains(safePatch, "..") || strings.Contains(safePatch, "/") || len(safePatch) > 220 || !strings.HasSuffix(safePatch, "deployment.yaml.patch") {
		t.Errorf("unexpected sanitizePatchFilename(%d chars) = %q (%d chars)", len(longPath), safePatch, len(safePatch))
	}
}
