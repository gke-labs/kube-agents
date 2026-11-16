package clusterprofiles

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	container "google.golang.org/api/container/v1"
)

func TestBrokerFromEnvRewritesWatcherGCPHostsForCustomUniverse(t *testing.T) {
	const customUniverse = "universe.example.test"
	var seenPaths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenPaths = append(seenPaths, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(&container.Cluster{
			Name:             "fleet-a",
			CurrentNodeCount: 4,
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{
				DnsEndpointConfig: &container.DNSEndpointConfig{
					AllowExternalTraffic: true,
					Endpoint:             "fleet-a.internal",
				},
			},
		})
	}))
	defer srv.Close()

	tokFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokFile, []byte("broker-token\n"), 0o600); err != nil {
		t.Fatalf("write token: %v", err)
	}

	t.Setenv(BrokerURLEnv, srv.URL)
	t.Setenv(BrokerTokenFileEnv, tokFile)
	t.Setenv(UniverseDomainEnv, customUniverse)

	b, err := BrokerFromEnv()
	if err != nil || b == nil {
		t.Fatalf("BrokerFromEnv() = (%v, %v), want non-nil broker", b, err)
	}
	if b.UniverseDomain != customUniverse {
		t.Fatalf("Broker.UniverseDomain = %q, want %q", b.UniverseDomain, customUniverse)
	}

	var d Discoverer
	cluster, err := d.DescribeCluster(context.Background(), Identity{
		Project:  "sample-project",
		Location: "us-central1",
		Cluster:  "fleet-a",
	})
	if err != nil {
		t.Fatalf("DescribeCluster failed: %v", err)
	}
	if cluster.CurrentNodeCount != 4 {
		t.Fatalf("CurrentNodeCount = %d, want 4", cluster.CurrentNodeCount)
	}
	wantPath := "GET /v1/watcher/gcp/container." + customUniverse + "/v1/projects/sample-project/locations/us-central1/clusters/fleet-a"
	if len(seenPaths) != 1 || seenPaths[0] != wantPath {
		t.Fatalf("seenPaths = %v, want [%s]", seenPaths, wantPath)
	}
}
