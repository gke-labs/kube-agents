package k8sapiproxy

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func buildFakeJWT(exp int64) string {
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	payloadBytes, _ := json.Marshal(map[string]any{
		"sub": "system:serviceaccount:ns:shell",
		"exp": exp,
	})
	payload := base64.RawURLEncoding.EncodeToString(payloadBytes)
	return fmt.Sprintf("%s.%s.sig", header, payload)
}

func TestTokenReviewAuthenticator(t *testing.T) {
	tmpDir := t.TempDir()
	tokenFile := filepath.Join(tmpDir, "token")
	if err := os.WriteFile(tokenFile, []byte("reviewer-token\n"), 0600); err != nil {
		t.Fatal(err)
	}

	t.Run("deletes Authorization header immediately and bounds cache TTL by JWT exp", func(t *testing.T) {
		tokenReviewCalls := 0
		apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tokenReviewCalls++
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{
				"apiVersion": "authentication.k8s.io/v1",
				"kind": "TokenReview",
				"status": {
					"authenticated": true,
					"audiences": ["kube-proxy"],
					"user": {
						"username": "system:serviceaccount:ns:shell",
						"uid": "uid-1",
						"groups": ["system:serviceaccounts"]
					}
				}
			}`)
		}))
		defer apiServer.Close()

		auth, err := NewTokenReviewAuthenticator(TokenReviewConfig{
			APIServerURL:   apiServer.URL,
			TokenFile:      tokenFile,
			Audience:       "kube-proxy",
			AllowedCallers: []string{"system:serviceaccount:ns:shell"},
			CacheTTL:       10 * time.Minute,
			HTTPClient:     apiServer.Client(),
		})
		if err != nil {
			t.Fatalf("NewTokenReviewAuthenticator failed: %v", err)
		}

		shortExp := time.Now().Add(2 * time.Second).Unix()
		jwt := buildFakeJWT(shortExp)

		req := httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil)
		req.Header.Set("Authorization", "Bearer "+jwt)

		ident, err := auth.AuthenticateRequest(req)
		if err != nil {
			t.Fatalf("AuthenticateRequest failed: %v", err)
		}
		if ident.Username != "system:serviceaccount:ns:shell" {
			t.Errorf("Username = %q, want system:serviceaccount:ns:shell", ident.Username)
		}
		if got := req.Header.Get("Authorization"); got != "" {
			t.Errorf("expected Authorization header to be deleted immediately, got %q", got)
		}

		sum := sha256.Sum256([]byte(jwt))
		hash := hex.EncodeToString(sum[:])
		auth.mu.RLock()
		entry, ok := auth.cache[hash]
		auth.mu.RUnlock()
		if !ok {
			t.Fatalf("expected token to be cached")
		}
		maxExpected := time.Unix(shortExp, 0).Add(time.Second)
		if entry.expiresAt.After(maxExpected) {
			t.Errorf("expected cache expiry bounded by JWT exp (%v), got %v", time.Unix(shortExp, 0), entry.expiresAt)
		}
	})

	t.Run("deletes Authorization header even when authentication fails", func(t *testing.T) {
		apiServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusForbidden)
		}))
		defer apiServer.Close()

		auth, err := NewTokenReviewAuthenticator(TokenReviewConfig{
			APIServerURL:   apiServer.URL,
			TokenFile:      tokenFile,
			AllowedCallers: []string{"system:serviceaccount:ns:shell"},
			HTTPClient:     apiServer.Client(),
		})
		if err != nil {
			t.Fatalf("NewTokenReviewAuthenticator failed: %v", err)
		}

		req := httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil)
		req.Header.Set("Authorization", "Bearer invalid-token")

		if _, err := auth.AuthenticateRequest(req); err == nil {
			t.Fatalf("expected error on 403 TokenReview")
		}
		if got := req.Header.Get("Authorization"); got != "" {
			t.Errorf("expected Authorization header to be deleted on failure, got %q", got)
		}
	})

	t.Run("rejects empty AllowedCallers configuration", func(t *testing.T) {
		_, err := NewTokenReviewAuthenticator(TokenReviewConfig{
			APIServerURL:   "https://kubernetes.default.svc",
			TokenFile:      tokenFile,
			AllowedCallers: []string{"", "   "},
		})
		if err == nil {
			t.Fatalf("expected NewTokenReviewAuthenticator to fail when AllowedCallers is empty")
		}
	})
}
