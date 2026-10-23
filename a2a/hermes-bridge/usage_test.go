package hermesbridge

import (
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// stubTurnTime is how long a stub turn takes, so the wall time the bridge
// measures is a figure and not a zero the producer omits.
const stubTurnTime = 20 * time.Millisecond

// usageSeen is the task's usage artifact read as Usage, in stream order,
// failing if a part is not usage or any lands after the final event.
func usageSeen(t *testing.T, url, taskID string) []lib.Usage {
	t.Helper()
	final := finalSeq(t, url, taskID)
	var out []lib.Usage
	for i, env := range replayEvents(t, url, taskID) {
		if env.Kind != lib.KindArtifactUpdate {
			continue
		}
		var a lib.ArtifactUpdate
		if json.Unmarshal(env.Payload, &a) != nil || a.Artifact.Name != lib.ArtifactUsage {
			continue
		}
		if i > final {
			t.Fatalf("usage part at %d follows the final event at %d", i, final)
		}
		for _, p := range a.Artifact.Parts {
			u, ok := lib.UsageOf(p)
			if !ok {
				t.Fatalf("usage part is not usage: %s", p.Data)
			}
			out = append(out, u)
		}
	}
	return out
}

// The API executor carries the server's model and token counts, and the
// turn's wall time, as one usage part ahead of the answer. No cost: Hermes
// behind the gateway prices nothing, and the part carries no figure the
// bridge invented.
func TestAPI_UsageCarriesTheServersCounts(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		time.Sleep(stubTurnTime) // a measurable turn, so durationMs is a figure
		w.Header().Set(apiSessionIDHeader, c.sessionID)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"model":   "claude-opus-5-5",
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "three pods"}, "finish_reason": "stop"}},
			"usage":   map[string]any{"prompt_tokens": 1200, "completion_tokens": 9, "total_tokens": 1209},
		})
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-usage-api", "ctx-usage", "how many pods are pending")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 {
		t.Fatalf("usage parts = %+v, want one", got)
	}
	u := got[0]
	if u.Model != "claude-opus-5-5" || u.InputTokens != 1200 || u.OutputTokens != 9 || u.CostUsd != 0 || u.DurationMs <= 0 {
		t.Fatalf("usage = %+v", u)
	}
}

// A server that sends no usage and no model leaves the part with the alias
// the request named and the turn's wall time: absence, never zeroes.
func TestAPI_UsageWithoutCountsIsDurationAlone(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		time.Sleep(stubTurnTime)
		writeCompletion(w, c.sessionID, "answer to "+c.prompt)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-usage-plain", "ctx-usage-plain", "hello")
	waitTerminal(t, c, origin.TaskID)
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 {
		t.Fatalf("usage parts = %+v, want one", got)
	}
	u := got[0]
	if u.Model != DefaultAPIModel || u.InputTokens != 0 || u.OutputTokens != 0 || u.CostUsd != 0 || u.DurationMs <= 0 {
		t.Fatalf("usage = %+v", u)
	}
}

// A turn the server answered as failed still read tokens: its usage is on
// the stream ahead of the failed terminal.
func TestAPI_UsageOnAFailedTurn(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		time.Sleep(stubTurnTime)
		w.Header().Set(apiSessionIDHeader, c.sessionID)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "The provider rate-limited me."}, "finish_reason": "error"}},
			"hermes":  map[string]any{"failed": true, "error": "rate limited"},
			"usage":   map[string]any{"prompt_tokens": 300, "completion_tokens": 12},
		})
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-usage-failed", "ctx-usage-failed", "do the thing")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateFailed {
		t.Fatalf("state %s, want failed", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 || got[0].InputTokens != 300 || got[0].OutputTokens != 12 || got[0].DurationMs <= 0 {
		t.Fatalf("usage = %+v", got)
	}
}

// A turn the server answered with a 5xx ran and failed with no answer: its
// usage is the wall time alone, ahead of the failed terminal. A 4xx ran
// nothing and publishes none.
func TestAPI_UsageOnA5xxTurnIsDurationAlone(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		parts  int
	}{{"5xx", http.StatusBadGateway, 1}, {"4xx", http.StatusForbidden, 0}} {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, _ apiCall) {
				time.Sleep(stubTurnTime)
				http.Error(w, `{"error":{"message":"upstream failed"}}`, tc.status)
			})
			startAPIBridge(t, url, stub, nil)
			c := gatewayClient(t, url)
			origin := submitIn(t, c, "task-usage-"+tc.name, "ctx-usage-"+tc.name, "do the thing")
			task := waitTerminal(t, c, origin.TaskID)
			if task.State != lib.StateFailed {
				t.Fatalf("state %s, want failed", task.State)
			}
			got := usageSeen(t, url, origin.TaskID)
			if len(got) != tc.parts {
				t.Fatalf("usage parts = %+v, want %d", got, tc.parts)
			}
			if tc.parts == 1 && (got[0].DurationMs <= 0 || got[0].InputTokens != 0 || got[0].Model != DefaultAPIModel) {
				t.Fatalf("usage = %+v, want the alias and the duration alone", got[0])
			}
		})
	}
}

// A count that is negative or past what the part carries is that field
// dropped: the part keeps the other count, the model and the wall time, and
// the turn completes.
func TestAPI_OutOfRangeCountsDropTheFieldNotThePart(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		time.Sleep(stubTurnTime)
		w.Header().Set(apiSessionIDHeader, c.sessionID)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"model":   "claude-opus-5-5",
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "three pods"}, "finish_reason": "stop"}},
			"usage":   map[string]any{"prompt_tokens": -3, "input_tokens": -2, "completion_tokens": 9},
		})
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-usage-range", "ctx-usage-range", "how many pods are pending")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s, want completed", task.State)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 || got[0].Model != "claude-opus-5-5" || got[0].InputTokens != 0 || got[0].OutputTokens != 9 || got[0].DurationMs <= 0 {
		t.Fatalf("usage = %+v, want the model, 9 output tokens and the duration with the input count dropped", got)
	}
	u, dropped := apiTurnUsage(&apiChatResponse{Usage: json.RawMessage(`{"prompt_tokens":1e19,"completion_tokens":1}`)}, "alias", time.Second)
	if u.InputTokens != 0 || u.OutputTokens != 1 || len(dropped) != 1 || dropped[0] != "usage.prompt_tokens" {
		t.Fatalf("overflowing count: usage=%+v dropped=%v", u, dropped)
	}
}

// A model or usage in a shape the bridge does not read is dropped, never the
// turn: the answer completes and the part carries what did decode.
func TestAPI_OffShapeTelemetryDoesNotFailTheTurn(t *testing.T) {
	_, url := startServer(t)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		time.Sleep(stubTurnTime)
		w.Header().Set(apiSessionIDHeader, c.sessionID)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"model":   7,
			"choices": []any{map[string]any{"message": map[string]any{"role": "assistant", "content": "three pods"}, "finish_reason": "stop"}},
			"usage":   []any{},
		})
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-usage-odd", "ctx-usage-odd", "how many pods are pending")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s, want completed", task.State)
	}
	if got := task.Artifact(lib.ArtifactResult); got == nil || got.Parts[0].Text != "three pods" {
		t.Fatalf("result = %+v", got)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 || got[0].Model != DefaultAPIModel || got[0].InputTokens != 0 || got[0].DurationMs <= 0 {
		t.Fatalf("usage = %+v, want the alias and the duration alone", got)
	}
}

// A CLI run that exits non-zero on its own publishes its wall time ahead of
// the failed terminal.
func TestCLI_UsageOnAFailedRun(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `sleep 0.05; echo "boom" >&2; exit 3`))
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-usage-cli-failed", "what is the fleet status")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateFailed {
		t.Fatalf("state %s, want failed", task.State)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 || got[0].DurationMs <= 0 {
		t.Fatalf("usage = %+v, want one part with a duration", got)
	}
}

// The CLI executor knows no tokens, so its usage is the turn's wall time.
func TestCLI_UsageIsDurationAlone(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `sleep 0.05; echo "the platform answer for $1"`))
	c := gatewayClient(t, url)
	origin := submit(t, c, "task-usage-cli", "what is the fleet status")
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCompleted {
		t.Fatalf("state %s", task.State)
	}
	if err := task.ValidateArtifacts(); err != nil {
		t.Fatalf("assertion 18: %v", err)
	}
	got := usageSeen(t, url, origin.TaskID)
	if len(got) != 1 || got[0].DurationMs <= 0 || got[0].InputTokens != 0 || got[0].Model != "" {
		t.Fatalf("usage = %+v, want one part with a duration alone", got)
	}
}
