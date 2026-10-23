package gkehttps

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

type staticTokenProvider struct {
	token string
}

func (s staticTokenProvider) AccessToken(_ context.Context) (string, error) {
	return s.token, nil
}

func TestParseGKEClusterID(t *testing.T) {
	coords, err := ParseGKEClusterID("gke_my-project-123_us-central1_cluster-a")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if coords.Project != "my-project-123" || coords.Location != "us-central1" || coords.Cluster != "cluster-a" {
		t.Errorf("unexpected coords: %+v", coords)
	}

	if _, err := ParseGKEClusterID("gke_bad_parts"); err == nil {
		t.Errorf("expected error for 2 segments")
	}
	if _, err := ParseGKEClusterID("gke_UPPER_us-central1_clu"); err == nil {
		t.Errorf("expected error for uppercase segment")
	}
}

func TestTransportGKEAndInCluster(t *testing.T) {
	clusterServer := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"path":"`+r.URL.Path+`","auth":"`+r.Header.Get("Authorization")+`"}`)
	}))
	defer clusterServer.Close()

	certPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "CERTIFICATE",
		Bytes: clusterServer.Certificate().Raw,
	})

	containerAPI := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/projects/proj-1/locations/us-central1/clusters/clu-1" {
			http.NotFound(w, r)
			return
		}
		resp := map[string]any{
			"endpoint": clusterServer.Listener.Addr().String(),
			"masterAuth": map[string]any{
				"clusterCaCertificate": base64.StdEncoding.EncodeToString(certPEM),
			},
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer containerAPI.Close()

	tmpDir := t.TempDir()
	caFile := filepath.Join(tmpDir, "ca.crt")
	tokenFile := filepath.Join(tmpDir, "token")
	if err := os.WriteFile(caFile, certPEM, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenFile, []byte("ksa-in-cluster-token\n"), 0600); err != nil {
		t.Fatal(err)
	}

	tr, err := NewTransport(Config{
		InClusterServerURL:  clusterServer.URL,
		InClusterCAFile:     caFile,
		InClusterTokenFile:  tokenFile,
		HostClusterID:       "gke_proj-host_us-central1_host-clu",
		ContainerAPIBaseURL: containerAPI.URL,
		TokenProvider:       staticTokenProvider{token: "gcp-oauth-token"},
		DiscoveryClient:     containerAPI.Client(),
	})
	if err != nil {
		t.Fatalf("NewTransport failed: %v", err)
	}

	t.Run("in-cluster round trip", func(t *testing.T) {
		req, _ := http.NewRequest(http.MethodGet, "https://placeholder/api/v1/pods", nil)
		resp, err := tr.RoundTripCluster(context.Background(), "in-cluster", req)
		if err != nil {
			t.Fatalf("RoundTripCluster(in-cluster) failed: %v", err)
		}
		defer resp.Body.Close()
		var got map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&got)
		if got["auth"] != "Bearer ksa-in-cluster-token" {
			t.Errorf("auth = %q, want Bearer ksa-in-cluster-token", got["auth"])
		}
	})

	t.Run("host GKE cluster ID routes via in-cluster transport without hitting container.googleapis.com", func(t *testing.T) {
		req, _ := http.NewRequest(http.MethodGet, "https://placeholder/api/v1/pods", nil)
		resp, err := tr.RoundTripCluster(context.Background(), "gke_proj-host_us-central1_host-clu", req)
		if err != nil {
			t.Fatalf("RoundTripCluster(hostClusterID) failed: %v", err)
		}
		defer resp.Body.Close()
		var got map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&got)
		if got["auth"] != "Bearer ksa-in-cluster-token" {
			t.Errorf("auth = %q, want Bearer ksa-in-cluster-token", got["auth"])
		}
	})

	t.Run("GKE cluster discovery and round trip", func(t *testing.T) {
		req, _ := http.NewRequest(http.MethodGet, "https://placeholder/api/v1/namespaces/default/pods", nil)
		resp, err := tr.RoundTripCluster(context.Background(), "gke_proj-1_us-central1_clu-1", req)
		if err != nil {
			t.Fatalf("RoundTripCluster(gke) failed: %v", err)
		}
		defer resp.Body.Close()
		var got map[string]string
		_ = json.NewDecoder(resp.Body).Decode(&got)
		if got["auth"] != "Bearer gcp-oauth-token" {
			t.Errorf("auth = %q, want Bearer gcp-oauth-token", got["auth"])
		}
		if got["path"] != "/api/v1/namespaces/default/pods" {
			t.Errorf("path = %q, want /api/v1/namespaces/default/pods", got["path"])
		}
	})

	t.Run("canceled caller context does not evict cached GKE cluster transport", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		req, _ := http.NewRequest(http.MethodGet, "https://placeholder/api/v1/namespaces/default/pods", nil)
		_, err := tr.RoundTripCluster(ctx, "gke_proj-1_us-central1_clu-1", req)
		if err == nil {
			t.Fatalf("expected error on canceled context")
		}
		tr.mu.RLock()
		_, cached := tr.clusters["gke_proj-1_us-central1_clu-1"]
		tr.mu.RUnlock()
		if !cached {
			t.Errorf("expected cached transport for gke_proj-1_us-central1_clu-1 to remain after caller context cancellation")
		}
	})
}
