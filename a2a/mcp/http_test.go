package mcp

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const testToken = "s3cret-test-token"

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func writeToken(t *testing.T, token string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func post(h http.Handler, header http.Header, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, Path, strings.NewReader(body))
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

const pingFrame = `{"jsonrpc":"2.0","id":1,"method":"ping"}`

// Every way in, and what it gets. The token is the only credential: there is
// no first-caller adoption and no fallback when it is missing.
func TestTheEndpointAuthenticatesEveryRequest(t *testing.T) {
	bearer := func(v string) http.Header { return http.Header{headerAuthorization: {v}} }
	cases := []struct {
		name      string
		tokenFile func(t *testing.T) string
		header    http.Header
		want      int
	}{
		{"the right token", func(t *testing.T) string { return writeToken(t, testToken) }, bearer("Bearer " + testToken), http.StatusOK},
		{"the right token with a trailing newline in the file", func(t *testing.T) string { return writeToken(t, testToken+"\n") }, bearer("Bearer " + testToken), http.StatusOK},
		{"the scheme in another case", func(t *testing.T) string { return writeToken(t, testToken) }, bearer("bearer " + testToken), http.StatusOK},
		{"no Authorization header", func(t *testing.T) string { return writeToken(t, testToken) }, nil, http.StatusUnauthorized},
		{"an empty bearer", func(t *testing.T) string { return writeToken(t, testToken) }, bearer("Bearer "), http.StatusUnauthorized},
		{"the wrong token", func(t *testing.T) string { return writeToken(t, testToken) }, bearer("Bearer nope"), http.StatusUnauthorized},
		{"the token without the Bearer scheme", func(t *testing.T) string { return writeToken(t, testToken) }, bearer(testToken), http.StatusUnauthorized},
		{"a token-file prefix", func(t *testing.T) string { return writeToken(t, testToken) }, bearer("Bearer " + testToken[:4]), http.StatusUnauthorized},
		{"an empty token file refuses even an empty bearer", func(t *testing.T) string { return writeToken(t, "") }, bearer("Bearer "), http.StatusUnauthorized},
		{"a missing token file refuses everyone", func(t *testing.T) string { return filepath.Join(t.TempDir(), "absent") }, bearer("Bearer " + testToken), http.StatusUnauthorized},
		{"a browser is refused before auth", func(t *testing.T) string { return writeToken(t, testToken) },
			http.Header{headerAuthorization: {"Bearer " + testToken}, headerOrigin: {"http://localhost:8089"}}, http.StatusForbidden},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := Handler(mustServer(t), tc.tokenFile(t), quietLog())
			if rec := post(h, tc.header, pingFrame); rec.Code != tc.want {
				t.Errorf("status = %d, want %d (body %q)", rec.Code, tc.want, rec.Body.String())
			}
		})
	}
}

// The token is read per request, so rotating the Secret changes who gets in
// without a restart.
func TestARotatedTokenTakesEffectWithoutARestart(t *testing.T) {
	path := writeToken(t, "old-token")
	h := Handler(mustServer(t), path, quietLog())
	if rec := post(h, http.Header{headerAuthorization: {"Bearer old-token"}}, pingFrame); rec.Code != http.StatusOK {
		t.Fatalf("old token before rotation: %d", rec.Code)
	}
	if err := os.WriteFile(path, []byte("new-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	if rec := post(h, http.Header{headerAuthorization: {"Bearer old-token"}}, pingFrame); rec.Code != http.StatusUnauthorized {
		t.Errorf("old token after rotation: %d, want 401", rec.Code)
	}
	if rec := post(h, http.Header{headerAuthorization: {"Bearer new-token"}}, pingFrame); rec.Code != http.StatusOK {
		t.Errorf("new token after rotation: %d, want 200", rec.Code)
	}
}

func TestTheHealthProbeNeedsNoToken(t *testing.T) {
	h := Handler(mustServer(t), filepath.Join(t.TempDir(), "absent"), quietLog())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, HealthPath, nil))
	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}

func TestTheEndpointAnswersAndBounds(t *testing.T) {
	h := Handler(mustServer(t), writeToken(t, testToken), quietLog())
	auth := http.Header{headerAuthorization: {"Bearer " + testToken}}

	rec := post(h, auth, pingFrame)
	if rec.Code != http.StatusOK || rec.Header().Get(headerContentType) != contentTypeJSON || !strings.Contains(rec.Body.String(), `"result":{}`) {
		t.Errorf("ping = %d %q %q", rec.Code, rec.Header().Get(headerContentType), rec.Body.String())
	}
	if rec := post(h, auth, `{"jsonrpc":"2.0","method":"notifications/initialized"}`); rec.Code != http.StatusAccepted {
		t.Errorf("notification = %d, want 202", rec.Code)
	}
	if rec := post(h, auth, strings.Repeat(" ", maxBodyBytes+1)); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body = %d, want 413", rec.Code)
	}
	// A revision this server speaks, or none (an older client), is served;
	// any other is refused before the body is read.
	for v, want := range map[string]int{"2025-06-18": http.StatusOK, "": http.StatusOK, "2099-01-01": http.StatusBadRequest} {
		hdr := http.Header{headerAuthorization: {"Bearer " + testToken}}
		if v != "" {
			hdr.Set(headerProtocolVersion, v)
		}
		if rec := post(h, hdr, pingFrame); rec.Code != want {
			t.Errorf("%s %q = %d, want %d", headerProtocolVersion, v, rec.Code, want)
		}
	}
	req := httptest.NewRequest(http.MethodGet, Path, nil)
	req.Header.Set(headerAuthorization, "Bearer "+testToken)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Errorf("GET %s = %d, want 405: there is no event stream here", Path, rec.Code)
	}
}

func TestReadTokenRefusesNothing(t *testing.T) {
	if _, err := ReadToken(writeToken(t, " \n")); err == nil {
		t.Error("a whitespace-only token file was accepted")
	}
	if _, err := ReadToken(filepath.Join(t.TempDir(), "absent")); err == nil {
		t.Error("a missing token file was accepted")
	}
}

// A batch's members share one budget, so two slow calls together still answer
// inside it, and with it the server's write timeout.
func TestABatchSharesOneBudget(t *testing.T) {
	old := requestBudget
	requestBudget = 200 * time.Millisecond
	t.Cleanup(func() { requestBudget = old })
	slow := Tool{
		Definition: ToolDefinition{Name: "slow", InputSchema: map[string]any{"type": "object"}},
		Call: func(ctx context.Context, _ json.RawMessage) (string, error) {
			select {
			case <-ctx.Done():
				return "out of budget", nil
			case <-time.After(5 * time.Second):
				return "finished", nil
			}
		},
	}
	h := Handler(mustServer(t, slow), writeToken(t, testToken), quietLog())
	call := func(id int) string {
		return fmt.Sprintf(`{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":"slow","arguments":{}}}`, id)
	}
	start := time.Now()
	rec := post(h, http.Header{"Authorization": {"Bearer " + testToken}}, "["+call(1)+","+call(2)+"]")
	if took := time.Since(start); took > 2*time.Second {
		t.Errorf("a batch of two slow calls took %s, past its %s budget", took, requestBudget)
	}
	if rec.Code != http.StatusOK || strings.Count(rec.Body.String(), "out of budget") != 2 {
		t.Errorf("status %d, body %s", rec.Code, rec.Body)
	}
}
