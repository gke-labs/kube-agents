package upstream

import (
	"encoding/base64"
	"testing"
)

func TestParseGKEClusterID(t *testing.T) {
	for _, tc := range []struct {
		id   string
		want gkeCluster
		ok   bool
	}{
		{"gke_proj_us-central1_c1", gkeCluster{"proj", "us-central1", "c1"}, true},
		{"gke_example.com:proj_us-east4-a_c-1", gkeCluster{"example.com:proj", "us-east4-a", "c-1"}, true},
		{"gke_proj_us-central1", gkeCluster{}, false},
		{"gke_proj_us-central1_c1_extra", gkeCluster{}, false},
		{"gke_Proj_us-central1_c1", gkeCluster{}, false},
		{"gke_proj_us-central1_c/1", gkeCluster{}, false},
		{"gke__us-central1_c1", gkeCluster{}, false},
		{"kind-dev", gkeCluster{}, false},
		{"in-cluster", gkeCluster{}, false},
	} {
		got, err := parseGKEClusterID(tc.id)
		if (err == nil) != tc.ok || got != tc.want {
			t.Errorf("parseGKEClusterID(%q) = %+v, %v; want %+v, ok=%v", tc.id, got, err, tc.want, tc.ok)
		}
	}
}

func TestSelectEndpoint(t *testing.T) {
	yes, no := true, false
	ca := base64.StdEncoding.EncodeToString([]byte("ca"))
	describe := func(endpoint, caCert, dns string, external *bool, ipConfig bool, ipEnabled *bool) clusterDescription {
		var d clusterDescription
		d.Endpoint = endpoint
		d.MasterAuth.ClusterCACertificate = caCert
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.Endpoint = dns
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.AllowExternalTraffic = external
		if ipConfig {
			d.ControlPlaneEndpointsConfig.IPEndpointsConfig = &struct {
				Enabled *bool `json:"enabled"`
			}{ipEnabled}
		}
		return d
	}
	for _, tc := range []struct {
		name string
		desc clusterDescription
		want endpoint
		ok   bool
	}{
		{"rule 1: DNS endpoint open to external traffic", describe("1.2.3.4", ca, "gke-x.goog", &yes, true, &yes), endpoint{host: "gke-x.goog", dns: true}, true},
		{"rule 2: IP endpoints disabled", describe("1.2.3.4", ca, "gke-x.goog", &no, true, &no), endpoint{host: "gke-x.goog", dns: true}, true},
		{"rule 2: IP endpoints config without enabled", describe("1.2.3.4", ca, "gke-x.goog", nil, true, nil), endpoint{host: "gke-x.goog", dns: true}, true},
		{"rule 2: IP endpoints disabled and no DNS endpoint", describe("1.2.3.4", ca, "", nil, true, &no), endpoint{}, false},
		{"rule 4: IP endpoints enabled", describe("1.2.3.4", ca, "gke-x.goog", &no, true, &yes), endpoint{host: "1.2.3.4", caData: []byte("ca")}, true},
		{"rule 4: no endpoint config", describe("1.2.3.4", ca, "", nil, false, nil), endpoint{host: "1.2.3.4", caData: []byte("ca")}, true},
		{"rule 4: no CA", describe("1.2.3.4", "", "", nil, false, nil), endpoint{}, false},
		{"rule 4: CA not base64", describe("1.2.3.4", "!!", "", nil, false, nil), endpoint{}, false},
		{"rule 4: no endpoint", describe("", ca, "", nil, false, nil), endpoint{}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := selectEndpoint(tc.desc)
			if (err == nil) != tc.ok || got.host != tc.want.host || got.dns != tc.want.dns || string(got.caData) != string(tc.want.caData) {
				t.Errorf("selectEndpoint = %+v, %v; want %+v, ok=%v", got, err, tc.want, tc.ok)
			}
		})
	}
}
