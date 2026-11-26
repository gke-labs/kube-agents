package main

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	adkmodel "google.golang.org/adk/v2/model"
	"google.golang.org/genai"

	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// fakeGemini records each call's path and key header and answers with body.
type fakeGemini struct {
	body  string
	mu    sync.Mutex
	paths []string
	keys  []string
}

func (f *fakeGemini) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	_, _ = io.ReadAll(r.Body)
	f.mu.Lock()
	f.paths = append(f.paths, r.URL.Path+"?"+r.URL.RawQuery)
	f.keys = append(f.keys, r.Header.Get("x-goog-api-key"))
	f.mu.Unlock()
	w.Header().Set("Content-Type", "text/event-stream")
	_, _ = io.WriteString(w, f.body)
}

// generate runs one streamed turn on buildModel's model, through the real
// core-agent provider and genai client, to gw through the adapter's real
// loopback gateway proxy, and returns the text and the error the stream
// ended with.
func generate(t *testing.T, gw *fakeGemini) (string, error) {
	t.Helper()
	up := httptest.NewServer(gw)
	t.Cleanup(up.Close)
	// What the adapter's builder leaves in the harness's environment: only
	// the proxy's URL (a2a/cmd/worker-adapter, startHarnessGateway).
	for _, k := range []string{"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_VERTEX_BASE_URL"} {
		t.Setenv(k, "")
	}
	proxy, err := workeradapter.StartGatewayProxy(up.URL, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = proxy.Close() })
	t.Setenv("GOOGLE_GEMINI_BASE_URL", proxy.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	m, err := buildModel(ctx, "gemini-default")
	if err != nil {
		t.Fatal(err)
	}
	req := &adkmodel.LLMRequest{Model: "gemini-default", Contents: []*genai.Content{genai.NewContentFromText("ping", genai.RoleUser)}}
	var text strings.Builder
	var last error
	for resp, err := range m.GenerateContent(ctx, req, true) {
		if err != nil {
			last = err
			break
		}
		if resp != nil && resp.Content != nil && !resp.Partial {
			for _, p := range resp.Content.Parts {
				text.WriteString(p.Text)
			}
		}
	}
	return text.String(), last
}

// TestTheModelCallsTheBaseURLTheAdapterSets: buildModel's Gemini client, the
// real core-agent provider over the genai SDK, sends its call to
// GOOGLE_GEMINI_BASE_URL, the adapter's proxy, which admits its path and
// carries it to the gateway without the placeholder key, and the answer
// comes back.
func TestTheModelCallsTheBaseURLTheAdapterSets(t *testing.T) {
	gw := &fakeGemini{body: "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"pong\"}]},\"finishReason\":\"STOP\"}]}\n\n"}
	text, err := generate(t, gw)
	if err != nil {
		t.Fatal(err)
	}
	if text != "pong" {
		t.Errorf("text %q, want pong", text)
	}
	gw.mu.Lock()
	defer gw.mu.Unlock()
	if len(gw.paths) == 0 {
		t.Fatal("the fake gateway saw no call")
	}
	for i, p := range gw.paths {
		if !strings.HasPrefix(p, "/v1beta/models/gemini-default:streamGenerateContent?") {
			t.Errorf("call %d went to %q, want the proxy's streamGenerateContent path", i, p)
		}
		if gw.keys[i] != "" {
			t.Errorf("call %d reached the gateway with key %q; the proxy forwards none", i, gw.keys[i])
		}
	}
}

// TestAnAnswerThatEndsBadlyFailsTheTurn: the genai client reads a stream
// that ends cleanly before any finishReason, or that carries an error
// object, as a completed answer. Through the adapter's proxy, which breaks
// the connection on either, the turn ends in an error instead of completing
// with the partial text.
func TestAnAnswerThatEndsBadlyFailsTheTurn(t *testing.T) {
	partial := "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"partial\"}]}}]}\n\n"
	for name, body := range map[string]string{
		"cut before its finish": partial,
		"error in the stream":   partial + "data: {\"error\":{\"code\":500,\"message\":\"upstream\",\"status\":\"INTERNAL\"}}\n\n",
	} {
		t.Run(name, func(t *testing.T) {
			text, err := generate(t, &fakeGemini{body: body})
			if err == nil {
				t.Errorf("the turn completed with %q; want an error", text)
			}
		})
	}
}
