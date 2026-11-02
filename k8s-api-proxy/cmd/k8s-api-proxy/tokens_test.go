package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestNewGCPTokenSourceWIFExchange(t *testing.T) {
	var stsCalls, iamCalls int
	var stsServer *httptest.Server
	stsServer = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/token":
			stsCalls++
			if err := r.ParseForm(); err != nil {
				t.Fatalf("parse form: %v", err)
			}
			if got := r.FormValue("subject_token"); got != "projected-k8s-jwt" {
				t.Fatalf("unexpected subject_token: %q", got)
			}
			if got := r.FormValue("audience"); got != "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/p/providers/pr" {
				t.Fatalf("unexpected audience: %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"access_token": "sts-federated-token",
				"expires_in":   3600,
			})
		case "/v1/projects/-/serviceAccounts/agent@p.iam.gserviceaccount.com:generateAccessToken":
			iamCalls++
			if got := r.Header.Get("Authorization"); got != "Bearer sts-federated-token" {
				t.Fatalf("unexpected STS Authorization header: %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"accessToken": "ya29.wif-impersonated-token",
				"expireTime":  "2099-01-01T00:00:00Z",
			})
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer stsServer.Close()

	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("projected-k8s-jwt\n"), 0o600); err != nil {
		t.Fatalf("write token file: %v", err)
	}

	ts := newGCPTokenSource(gcpTokenConfig{
		WIFAudience:       "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/p/providers/pr",
		WIFServiceAccount: "agent@p.iam.gserviceaccount.com",
		WIFTokenFile:      tokenFile,
		STSURL:            stsServer.URL + "/v1/token",
		IAMCredentialsURL: stsServer.URL + "/v1",
		HTTPClient:        stsServer.Client(),
	})

	tok, err := ts.Token()
	if err != nil {
		t.Fatalf("first Token() failed: %v", err)
	}
	if tok.AccessToken != "ya29.wif-impersonated-token" {
		t.Fatalf("unexpected access token: %q", tok.AccessToken)
	}

	// Second call hits the reuse cache.
	tok2, err := ts.Token()
	if err != nil {
		t.Fatalf("second Token() failed: %v", err)
	}
	if tok2.AccessToken != "ya29.wif-impersonated-token" {
		t.Fatalf("unexpected cached token: %q", tok2.AccessToken)
	}
	if stsCalls != 1 || iamCalls != 1 {
		t.Fatalf("expected 1 STS and 1 IAM call, got sts=%d iam=%d", stsCalls, iamCalls)
	}
}
