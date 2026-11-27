package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	adkmodel "google.golang.org/adk/v2/model"
	"google.golang.org/genai"
)

// TestAnErrorInTheStreamFailsTheTurn: a gateway that streams some of an
// answer and then an SSE event whose data is an error object fails the
// turn with that error. The genai client read such an event as an empty
// chunk before v1.76.0, so the turn completed with the partial text.
//
// The call goes to the v1beta path, the only one the adapter's gateway
// proxy and the gateway's caller routes admit: genai v1.78.0 made v1 the
// Developer API's default, which would refuse every call.
func TestAnErrorInTheStreamFailsTheTurn(t *testing.T) {
	var path string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.ReadAll(r.Body)
		path = r.URL.Path
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: {\"candidates\":[{\"content\":{\"role\":\"model\",\"parts\":[{\"text\":\"partial\"}]}}]}\n\n"+
			"data: {\"error\":{\"code\":500,\"message\":\"Internal error encountered.\",\"status\":\"INTERNAL\"}}\n\n")
	}))
	t.Cleanup(up.Close)
	for _, k := range []string{"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_VERTEX_BASE_URL"} {
		t.Setenv(k, "")
	}
	t.Setenv("GOOGLE_GEMINI_BASE_URL", up.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	m, err := buildModel(ctx, "gemini-default", false)
	if err != nil {
		t.Fatal(err)
	}
	req := &adkmodel.LLMRequest{Model: "gemini-default", Contents: []*genai.Content{genai.NewContentFromText("ping", genai.RoleUser)}}
	var last error
	for _, err := range m.GenerateContent(ctx, req, true) {
		if err != nil {
			last = err
			break
		}
	}
	if path != "/v1beta/models/gemini-default:streamGenerateContent" {
		t.Errorf("the call went to %q, want the v1beta streamGenerateContent path", path)
	}
	if last == nil {
		t.Fatal("the turn completed from a stream that carried an error; want the error")
	}
	var apiErr genai.APIError
	if !errors.As(last, &apiErr) || apiErr.Code != http.StatusInternalServerError {
		t.Errorf("err %v, want the stream's genai.APIError with code 500", last)
	}
}
