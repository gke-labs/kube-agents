package main

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"strings"
	"testing"

	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// `worker-adapter mcp` reads the turn's delegate targets from the env the
// adapter wrote and names them in tools/list: the entry the harness starts,
// not the server function alone.
func TestTheMCPEntryNamesTheTargetsTheAdapterWrote(t *testing.T) {
	t.Setenv(workeradapter.EnvDelegateTargets, `{"targets":[{"name":"platform"},{"name":"e2e5","description":"reads one cluster"}]}`)
	in := strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}` + "\n")
	var out strings.Builder
	if err := serveMCP(context.Background(), in, &out, slog.New(slog.NewTextHandler(io.Discard, nil))); err != nil && err != io.EOF {
		t.Fatalf("serveMCP: %v", err)
	}
	var resp struct {
		Result struct {
			Tools []struct {
				InputSchema struct {
					Properties map[string]struct {
						Description string `json:"description"`
					} `json:"properties"`
				} `json:"inputSchema"`
			} `json:"tools"`
		} `json:"result"`
	}
	if err := json.Unmarshal([]byte(out.String()), &resp); err != nil || len(resp.Result.Tools) != 1 {
		t.Fatalf("tools/list answer %q: %v", out.String(), err)
	}
	if desc := resp.Result.Tools[0].InputSchema.Properties["addressee"].Description; !strings.Contains(desc, `"e2e5"`) {
		t.Errorf("the entry's addressee description %q doesn't name the target the adapter wrote", desc)
	}
}
