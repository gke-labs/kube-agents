package main

import (
	"bytes"
	"context"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"golang.org/x/oauth2"
	"k8s.io/client-go/rest"
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
				"accessToken": "test-wif-impersonated-token",
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
	if tok.AccessToken != "test-wif-impersonated-token" {
		t.Fatalf("unexpected access token: %q", tok.AccessToken)
	}

	// Second call hits the reuse cache.
	tok2, err := ts.Token()
	if err != nil {
		t.Fatalf("second Token() failed: %v", err)
	}
	if tok2.AccessToken != "test-wif-impersonated-token" {
		t.Fatalf("unexpected cached token: %q", tok2.AccessToken)
	}
	if stsCalls != 1 || iamCalls != 1 {
		t.Fatalf("expected 1 STS and 1 IAM call, got sts=%d iam=%d", stsCalls, iamCalls)
	}
}

func TestParseAllowedCallers(t *testing.T) {
	for _, raw := range []string{"", "   ", ",", " , , "} {
		if got, err := parseAllowedCallers(raw); err == nil {
			t.Fatalf("parseAllowedCallers(%q) = %v, want error", raw, got)
		}
	}
	got, err := parseAllowedCallers(" system:serviceaccount:ns:sa1 , , system:serviceaccount:ns:sa2 ")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(got) != 2 || got[0] != "system:serviceaccount:ns:sa1" || got[1] != "system:serviceaccount:ns:sa2" {
		t.Fatalf("unexpected callers: %v", got)
	}
}

func TestScopedPoolEnabled(t *testing.T) {
	for _, off := range []string{"", "0", "false", "FALSE", "no", "off", "  0  "} {
		if scopedPoolEnabled(off) {
			t.Errorf("scopedPoolEnabled(%q) = true, want false", off)
		}
	}
	for _, on := range []string{"1", "true", "yes", "on", "anything"} {
		if !scopedPoolEnabled(on) {
			t.Errorf("scopedPoolEnabled(%q) = false, want true", on)
		}
	}
}

func TestParseScopedSAPool(t *testing.T) {
	valid := `{"version":2,"serviceAccounts":[{"projectId":"proj-a","serviceAccountEmail":"ka-proj-a-1a2b3c4d@host-proj.iam.gserviceaccount.com"}]}`
	members, err := parseScopedSAPool([]byte(valid))
	if err != nil {
		t.Fatalf("unexpected error on valid pool: %v", err)
	}
	if got := members["proj-a"]; got != "ka-proj-a-1a2b3c4d@host-proj.iam.gserviceaccount.com" {
		t.Fatalf("unexpected member for proj-a: %q", got)
	}

	invalidCases := []struct {
		name string
		raw  string
		want string
	}{
		{"not json", `{`, "JSON object"},
		{"version 1", `{"version":1,"serviceAccounts":[]}`, "unsupported pool file version"},
		{"empty list", `{"version":2,"serviceAccounts":[]}`, "lists no service accounts"},
		{"invalid projectId", `{"version":2,"serviceAccounts":[{"projectId":"BAD_PROJ","serviceAccountEmail":"ka-proj-a-1a2b3c4d@host-proj.iam.gserviceaccount.com"}]}`, "projectId"},
		{"invalid email", `{"version":2,"serviceAccounts":[{"projectId":"proj-a","serviceAccountEmail":"user@example.com"}]}`, "serviceAccountEmail"},
		{"duplicate projectId", `{"version":2,"serviceAccounts":[{"projectId":"proj-a","serviceAccountEmail":"ka-proj-a-11111111@host-proj.iam.gserviceaccount.com"},{"projectId":"proj-a","serviceAccountEmail":"ka-proj-a-22222222@host-proj.iam.gserviceaccount.com"}]}`, "repeats proj-a"},
	}
	for _, tc := range invalidCases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parseScopedSAPool([]byte(tc.raw))
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("parseScopedSAPool(%s) err = %v, want substring %q", tc.name, err, tc.want)
			}
		})
	}
}

func serverCertPEM(srv *httptest.Server) []byte {
	cert, err := x509.ParseCertificate(srv.TLS.Certificates[0].Certificate[0])
	if err != nil {
		panic(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})
}

func TestScopedPoolTransportRefusesUnmappedClusterAndMintsPerProjectToken(t *testing.T) {
	var seenClusterAuth string
	var clusterCalls, containerCalls, iamCalls int

	k8sSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		clusterCalls++
		seenClusterAuth = r.Header.Get("Authorization")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"kind":"PodList","apiVersion":"v1","items":[]}`))
	}))
	defer k8sSrv.Close()

	k8sCAPEM := serverCertPEM(k8sSrv)
	k8sEndpoint := strings.TrimPrefix(k8sSrv.URL, "https://")

	gcpSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/projects/-/serviceAccounts/ka-mapped-1a2b3c4d@host-proj.iam.gserviceaccount.com:generateAccessToken",
			"/v1/projects/-/serviceAccounts/ka-second-5e6f7a8b@host-proj.iam.gserviceaccount.com:generateAccessToken":
			iamCalls++
			if got := r.Header.Get("Authorization"); got != "Bearer base-broker-token" {
				t.Fatalf("unexpected base Authorization header at IAM: %q", got)
			}
			var body struct {
				Scope    []string `json:"scope"`
				Lifetime string   `json:"lifetime"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode IAM request: %v", err)
			}
			if len(body.Scope) != 1 || body.Scope[0] != cloudPlatformScope {
				t.Fatalf("expected scope [%q], got %v", cloudPlatformScope, body.Scope)
			}
			if body.Lifetime != "900s" {
				t.Fatalf("expected lifetime 900s, got %q", body.Lifetime)
			}
			token := "test-scoped-mapped-proj"
			if strings.Contains(r.URL.Path, "ka-second-5e6f7a8b") {
				token = "test-scoped-second-proj"
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"accessToken": token,
				"expireTime":  "2099-01-01T00:00:00Z",
			})
		case "/v1/projects/mapped-proj/locations/us-central1/clusters/cluster-a":
			containerCalls++
			if got := r.Header.Get("Authorization"); got != "Bearer test-scoped-mapped-proj" {
				t.Fatalf("unexpected Container API Authorization header: %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"endpoint": k8sEndpoint,
				"masterAuth": map[string]any{
					"clusterCaCertificate": base64.StdEncoding.EncodeToString(k8sCAPEM),
				},
			})
		case "/v1/projects/second-proj/locations/us-east1/clusters/cluster-c":
			containerCalls++
			if got := r.Header.Get("Authorization"); got != "Bearer test-scoped-second-proj" {
				t.Fatalf("unexpected Container API Authorization header for second-proj: %q", got)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(map[string]any{
				"endpoint": k8sEndpoint,
				"masterAuth": map[string]any{
					"clusterCaCertificate": base64.StdEncoding.EncodeToString(k8sCAPEM),
				},
			})
		default:
			t.Fatalf("unexpected GCP API path: %s", r.URL.Path)
		}
	}))
	defer gcpSrv.Close()

	// Trust gcpSrv's TLS certificate on DefaultTransport for Container API discovery.
	origTransport := http.DefaultTransport
	http.DefaultTransport = gcpSrv.Client().Transport
	defer func() { http.DefaultTransport = origTransport }()

	inCluster := &rest.Config{
		Host:            k8sSrv.URL,
		BearerToken:     "in-cluster-ksa-token",
		TLSClientConfig: rest.TLSClientConfig{CAData: k8sCAPEM},
	}
	baseTokens := oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "base-broker-token"})

	tr, err := newScopedPoolTransport(
		context.Background(),
		map[string]string{
			"mapped-proj": "ka-mapped-1a2b3c4d@host-proj.iam.gserviceaccount.com",
			"second-proj": "ka-second-5e6f7a8b@host-proj.iam.gserviceaccount.com",
		},
		baseTokens,
		gcpSrv.URL+"/v1",
		gcpSrv.Client(),
		inCluster,
		gcpSrv.URL,
	)
	if err != nil {
		t.Fatalf("newScopedPoolTransport: %v", err)
	}

	// 1. An unmapped project is refused before any IAM, Container API, or kube-apiserver call.
	unmappedReq, err := http.NewRequestWithContext(context.Background(), http.MethodGet, "http://proxy/api/v1/pods", nil)
	if err != nil {
		t.Fatal(err)
	}
	if resp, err := tr.RoundTripCluster(context.Background(), "gke_unmapped-proj_us-central1_cluster-b", unmappedReq); err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected unmapped cluster to be refused, but RoundTripCluster succeeded")
	} else if !strings.Contains(err.Error(), "no scoped service account for project unmapped-proj") {
		t.Fatalf("unexpected refusal message: %v", err)
	}
	if iamCalls != 0 || containerCalls != 0 || clusterCalls != 0 {
		t.Fatalf("unmapped cluster triggered upstream calls: iam=%d container=%d cluster=%d", iamCalls, containerCalls, clusterCalls)
	}

	// 2. Non-gke_ cluster IDs (such as in-cluster) are refused when the pool is armed.
	if resp, err := tr.RoundTripCluster(context.Background(), "in-cluster", unmappedReq); err == nil {
		_ = resp.Body.Close()
		t.Fatal("expected in-cluster to be refused when scoped pool is armed")
	}

	// 3. Each mapped project exchanges the base token for its own scoped SA token and forwards it.
	mappedReq, err := http.NewRequestWithContext(context.Background(), http.MethodGet, "http://proxy/api/v1/pods", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := tr.RoundTripCluster(context.Background(), "gke_mapped-proj_us-central1_cluster-a", mappedReq)
	if err != nil {
		t.Fatalf("mapped cluster RoundTripCluster failed: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("unexpected status: %d", resp.StatusCode)
	}
	if seenClusterAuth != "Bearer test-scoped-mapped-proj" {
		t.Fatalf("cluster saw Authorization %q, want scoped token", seenClusterAuth)
	}
	if iamCalls != 1 || containerCalls != 1 || clusterCalls != 1 {
		t.Fatalf("unexpected call counts: iam=%d container=%d cluster=%d", iamCalls, containerCalls, clusterCalls)
	}

	secondReq, err := http.NewRequestWithContext(context.Background(), http.MethodGet, "http://proxy/api/v1/pods", nil)
	if err != nil {
		t.Fatal(err)
	}
	resp2, err := tr.RoundTripCluster(context.Background(), "gke_second-proj_us-east1_cluster-c", secondReq)
	if err != nil {
		t.Fatalf("second mapped cluster RoundTripCluster failed: %v", err)
	}
	_ = resp2.Body.Close()
	if seenClusterAuth != "Bearer test-scoped-second-proj" {
		t.Fatalf("second cluster saw Authorization %q, want second scoped token", seenClusterAuth)
	}
	if iamCalls != 2 || containerCalls != 2 || clusterCalls != 2 {
		t.Fatalf("unexpected call counts after second project: iam=%d container=%d cluster=%d", iamCalls, containerCalls, clusterCalls)
	}
}

func TestParseSessionAudienceCallers(t *testing.T) {
	t.Parallel()

	m, err := parseSessionAudienceCallers("", "")
	if err != nil || m != nil {
		t.Fatalf("expected nil map and nil error when session audience is unset, got %v, %v", m, err)
	}

	if _, err := parseSessionAudienceCallers("kubeagents-credential-proxy-session", "  "); err == nil {
		t.Fatal("expected error when session audience is set without session callers")
	}

	m, err = parseSessionAudienceCallers(
		"kubeagents-credential-proxy-session",
		"system:serviceaccount:kubeagents-sessions:a2a-session",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	got := m["kubeagents-credential-proxy-session"]
	if len(got) != 1 || got[0] != "system:serviceaccount:kubeagents-sessions:a2a-session" {
		t.Fatalf("unexpected callers: %v", got)
	}
}

func TestEnvInt64AndDurationOrDefaultLogWarnings(t *testing.T) {
	var buf bytes.Buffer
	origOut := log.Writer()
	log.SetOutput(&buf)
	defer log.SetOutput(origOut)

	t.Setenv("KUBE_PROXY_CACHE_MAX_BYTES", "64Mi")
	if got := envInt64OrDefault("KUBE_PROXY_CACHE_MAX_BYTES", 1024); got != 1024 {
		t.Fatalf("envInt64OrDefault(64Mi) = %d, want 1024", got)
	}
	if !strings.Contains(buf.String(), `warning: KUBE_PROXY_CACHE_MAX_BYTES is not a base-10 integer; using default 1024`) {
		t.Errorf("expected warning log for invalid int64 env, got %q", buf.String())
	}

	buf.Reset()
	t.Setenv("KUBE_PROXY_CACHE_RESOURCE_TTL", "30")
	if got := envDurationOrDefault("KUBE_PROXY_CACHE_RESOURCE_TTL", 10*time.Second); got != 10*time.Second {
		t.Fatalf("envDurationOrDefault(30) = %s, want 10s", got)
	}
	if !strings.Contains(buf.String(), `warning: KUBE_PROXY_CACHE_RESOURCE_TTL is not a valid duration; using default 10s`) {
		t.Errorf("expected warning log for invalid duration env, got %q", buf.String())
	}

	t.Setenv("KUBE_PROXY_CACHE_MAX_BYTES", "2048")
	if got := envInt64OrDefault("KUBE_PROXY_CACHE_MAX_BYTES", 1024); got != 2048 {
		t.Fatalf("envInt64OrDefault(2048) = %d, want 2048", got)
	}
	t.Setenv("KUBE_PROXY_CACHE_RESOURCE_TTL", "15s")
	if got := envDurationOrDefault("KUBE_PROXY_CACHE_RESOURCE_TTL", 10*time.Second); got != 15*time.Second {
		t.Fatalf("envDurationOrDefault(15s) = %s, want 15s", got)
	}
}
