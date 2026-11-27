package main

import (
	"context"
	"encoding/json"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"

	adkmodel "google.golang.org/adk/v2/model"
	"google.golang.org/genai"

	"github.com/go-steer/core-agent/v3/pkg/models"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// TestTheModelRequestCarriesOnlyTheGrantedServerSideTool: on the wire, the
// request the harness's Gemini model sends has only function declarations in
// its tools, plus Google Search when, and only when, the policy grants web
// search. URL context and code execution, which also run on the model's
// side and never reach the gate, are never on.
//
// core-agent sends search beside function declarations only to a model it
// recognises as Gemini 3 or later by name; for any other name (an alias such
// as model-default included) it drops search and logs that once, so a
// grant there is narrower, never wider.
func TestTheModelRequestCarriesOnlyTheGrantedServerSideTool(t *testing.T) {
	for _, c := range []struct {
		name, model           string
		webSearch, wantSearch bool
	}{
		{"no web search", "gemini-3-flash", false, false},
		{"web search", "gemini-3-flash", true, true},
		{"web search on a model named older or by alias", "model-default", true, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			bodies := modelRequestBodies(t, c.model, c.webSearch)
			for _, b := range bodies {
				var body struct {
					Tools []map[string]json.RawMessage `json:"tools"`
				}
				if err := json.Unmarshal([]byte(b), &body); err != nil {
					t.Fatalf("request body %q: %v", b, err)
				}
				kinds := map[string]bool{}
				for _, tl := range body.Tools {
					for k := range tl {
						kinds[k] = true
					}
				}
				want := map[string]bool{"functionDeclarations": true}
				if c.wantSearch {
					want["googleSearch"] = true
				}
				if !maps.Equal(kinds, want) {
					t.Errorf("request tool kinds %v, want %v: %s", slices.Sorted(maps.Keys(kinds)), slices.Sorted(maps.Keys(want)), b)
				}
				for _, banned := range []string{"urlContext", "codeExecution", "googleSearchRetrieval", "enterpriseWebSearch"} {
					if strings.Contains(b, banned) {
						t.Errorf("request mentions %s", banned)
					}
				}
			}
		})
	}
}

// modelRequestBodies sends one request with a function declaration through
// the harness's real Gemini client to a fake endpoint and returns the
// bodies it received.
func modelRequestBodies(t *testing.T, model string, webSearch bool) []string {
	t.Helper()
	req := &adkmodel.LLMRequest{
		Model:    model,
		Contents: []*genai.Content{{Role: "user", Parts: []*genai.Part{{Text: "hi"}}}},
		Config:   &genai.GenerateContentConfig{Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{Name: "read_file", Description: "read"}}}}},
	}
	return requestBodies(t, context.Background(), model, webSearch, req)
}

// requestBodies sends req through the harness's real Gemini client, built
// for model and webSearch, to a fake endpoint and returns the bodies it
// received.
func requestBodies(t *testing.T, ctx context.Context, model string, webSearch bool, req *adkmodel.LLMRequest) []string {
	t.Helper()
	var mu sync.Mutex
	var bodies []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, string(b))
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"candidates":[{"content":{"role":"model","parts":[{"text":"ok"}]},"finishReason":"STOP"}]}`)
	}))
	defer srv.Close()
	t.Setenv("GOOGLE_GEMINI_BASE_URL", srv.URL)
	t.Setenv("GEMINI_API_KEY", "")
	llm, err := buildModel(context.Background(), model, webSearch)
	if err != nil {
		t.Fatal(err)
	}
	for _, err := range llm.GenerateContent(ctx, req, false) {
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(bodies) == 0 {
		t.Fatal("no request reached the fake endpoint")
	}
	return slices.Clone(bodies)
}

// TestASideCallCarriesNoServerSideTool: a side call, such as the
// summarizer that compacts the history, reaches the endpoint with no tools,
// even when the turn's policy grants web search and on any model name.
// core-agent sends the summarizer's request with no function declarations,
// and core-agent's name check, which drops search for a model that isn't
// Gemini 3 or later, applies only to a request that has them; so without
// the harness's marker the compaction request would carry Google Search to
// an endpoint that may refuse it.
func TestASideCallCarriesNoServerSideTool(t *testing.T) {
	for _, model := range []string{"gemini-3-flash", "model-default"} {
		t.Run(model, func(t *testing.T) {
			req := &adkmodel.LLMRequest{
				Model:    model,
				Contents: []*genai.Content{{Role: "user", Parts: []*genai.Part{{Text: "summarise this"}}}},
				Config:   &genai.GenerateContentConfig{SystemInstruction: genai.NewContentFromText("summarise", genai.RoleUser)},
			}
			ctx := models.AsSideCall(context.Background(), "summarizer")
			m, err := buildModel(context.Background(), model, true)
			if err != nil {
				t.Fatal(err)
			}
			if _, ok := m.(interface{ WithoutBuiltins() adkmodel.LLM }); !ok {
				t.Error("the model no longer offers core-agent's WithoutBuiltins unwrap, which a subtask uses")
			}
			for _, b := range requestBodies(t, ctx, model, true, req) {
				var body struct {
					Tools []json.RawMessage `json:"tools"`
				}
				if err := json.Unmarshal([]byte(b), &body); err != nil {
					t.Fatalf("request body %q: %v", b, err)
				}
				if len(body.Tools) != 0 {
					t.Errorf("a side call's request carries tools: %s", b)
				}
			}
		})
	}
}

// TestATurnBuildsItsModelWithThePolicysWebSearch: the harness builds each
// turn's model with search on exactly when the envelope's policy grants
// web search.
func TestATurnBuildsItsModelWithThePolicysWebSearch(t *testing.T) {
	for _, want := range []bool{false, true} {
		var got []bool
		prev := newModel
		newModel = func(_ context.Context, _ string, webSearch bool) (adkmodel.LLM, error) {
			got = append(got, webSearch)
			return models.Adapt(&echoModel{}), nil
		}
		runTurn(t, t.TempDir(), coreagentwire.Envelope{Message: "hi", Policy: coreagentwire.Policy{WebSearch: want}})
		newModel = prev
		if !slices.Equal(got, []bool{want}) {
			t.Errorf("policy web search %v: the model was built with %v", want, got)
		}
	}
}
