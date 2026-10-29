package hermesbridge

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestPostBoardActionRelaysTheKnownFields: a Retry or Drop reaches session-kv's
// /v1/board/actions with the bearer and the request's known fields only, and
// its answer comes back as it came.
func TestPostBoardActionRelaysTheKnownFields(t *testing.T) {
	var gotPath, gotAuth string
	var gotBody map[string]any
	answer := `{"action":"retry","results":[{"id":"t_1","title":"Triage x","status":"ready","outcome":"done"}]}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotAuth = r.URL.Path, r.Header.Get("Authorization")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		_, _ = io.WriteString(w, answer)
	}))
	defer srv.Close()
	b := &Bridge{cfg: Config{RouteURL: srv.URL + "/", RouteKey: "kv-key", Logger: discardLogger()}, routeClient: http.DefaultClient}
	raw, err := b.postBoardAction([]byte(`{"action":"retry","tasks":["t_1"],"by":"Jayanti","extra":"dropped"}`))
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != answer {
		t.Errorf("answer = %s, want %s", raw, answer)
	}
	if gotPath != boardActionsPath || gotAuth != "Bearer kv-key" {
		t.Errorf("path %q auth %q", gotPath, gotAuth)
	}
	if _, ok := gotBody["extra"]; ok || gotBody["action"] != "retry" || gotBody["by"] != "Jayanti" {
		t.Errorf("body = %v", gotBody)
	}
}

// TestPostBoardActionRefusals: a refused request, an unreadable one and an
// oversized one are errors the gateway is told about, never a silent answer.
func TestPostBoardActionRefusals(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"action must be one of ['drop', 'retry']"}`, http.StatusBadRequest)
	}))
	defer srv.Close()
	b := &Bridge{cfg: Config{RouteURL: srv.URL, RouteKey: "kv-key", Logger: discardLogger()}, routeClient: http.DefaultClient}
	for name, request := range map[string]string{
		"refused":    `{"action":"delete","tasks":["t_1"]}`,
		"unreadable": `{"action":`,
		"oversized":  `{"tasks":["` + strings.Repeat("a", boardActionMaxBytes) + `"]}`,
	} {
		if _, err := b.postBoardAction([]byte(request)); err == nil {
			t.Errorf("%s: no error", name)
		}
	}
}

// TestBoardActionResultCarriesTheError: what the bridge answers when it could
// not reach session-kv reads as an error on the gateway's side.
func TestBoardActionResultCarriesTheError(t *testing.T) {
	data, _ := json.Marshal(lib.BoardActionResult{Error: "HTTP 503: busy"})
	var got lib.BoardActionResult
	if err := json.Unmarshal(data, &got); err != nil || got.Error != "HTTP 503: busy" || len(got.Results) != 0 {
		t.Errorf("result = %+v err %v", got, err)
	}
}
