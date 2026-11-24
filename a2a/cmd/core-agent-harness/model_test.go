package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	adkmodel "google.golang.org/adk/v2/model"
	"google.golang.org/genai"
)

// TestTheModelRequestCarriesNoServerSideTools: on the wire, the request the
// harness's Gemini model sends has only function declarations in its
// tools. Google Search, URL context and code execution, which run on the
// model's side and never reach the gate, are off.
func TestTheModelRequestCarriesNoServerSideTools(t *testing.T) {
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
	llm, err := buildModel(context.Background(), "gemini-test")
	if err != nil {
		t.Fatal(err)
	}
	req := &adkmodel.LLMRequest{
		Model:    "gemini-test",
		Contents: []*genai.Content{{Role: "user", Parts: []*genai.Part{{Text: "hi"}}}},
		Config:   &genai.GenerateContentConfig{Tools: []*genai.Tool{{FunctionDeclarations: []*genai.FunctionDeclaration{{Name: "read_file", Description: "read"}}}}},
	}
	for _, err := range llm.GenerateContent(context.Background(), req, false) {
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
	}
	mu.Lock()
	defer mu.Unlock()
	if len(bodies) == 0 {
		t.Fatal("no request reached the fake endpoint")
	}
	for _, b := range bodies {
		var body struct {
			Tools []map[string]json.RawMessage `json:"tools"`
		}
		if err := json.Unmarshal([]byte(b), &body); err != nil {
			t.Fatalf("request body %q: %v", b, err)
		}
		if len(body.Tools) == 0 {
			t.Fatalf("request carries no tools at all: %s", b)
		}
		for _, tl := range body.Tools {
			for k := range tl {
				if k != "functionDeclarations" {
					t.Errorf("request tools carry %q: %s", k, b)
				}
			}
		}
		for _, banned := range []string{"googleSearch", "urlContext", "codeExecution", "googleSearchRetrieval"} {
			if strings.Contains(b, banned) {
				t.Errorf("request mentions %s", banned)
			}
		}
	}
}
