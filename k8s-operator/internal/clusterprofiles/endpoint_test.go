// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package clusterprofiles

import (
	"encoding/base64"
	"net/http"
	"strings"
	"testing"

	"golang.org/x/oauth2"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/rest"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

func TestClientConfigForIdentity(t *testing.T) {
	ca := base64.StdEncoding.EncodeToString([]byte("ca-bytes"))
	external := &container.DNSEndpointConfig{Endpoint: "gke-abc.us-central1.gke.goog", AllowExternalTraffic: true}
	internal := &container.DNSEndpointConfig{Endpoint: "gke-abc.us-central1.gke.goog"}

	for _, tc := range []struct {
		name    string
		cluster *container.Cluster
		host    string
		ca      string
		wantErr string
	}{{
		// The DNS endpoint terminates on a Google frontend with a WebPKI
		// certificate, so it carries no CA of its own — and it is reachable
		// from pods that cannot route to the IP endpoint, which is why
		// gke_endpoint.py prefers it under exactly this condition.
		name: "external DNS endpoint wins over the IP endpoint",
		cluster: &container.Cluster{
			Endpoint:                    "10.0.0.2",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: external},
		},
		host: "https://gke-abc.us-central1.gke.goog",
	}, {
		// Published but closed to external traffic, with an IP endpoint
		// beside it: the DNS host would 403 from outside the VPC, so the IP
		// endpoint is the address. Only the case below, where the IP
		// endpoint is gone and GKE reports the DNS hostname in Endpoint,
		// takes a closed DNS endpoint as the address.
		name: "a DNS endpoint that refuses external traffic is ignored beside an IP endpoint",
		cluster: &container.Cluster{
			Endpoint:                    "10.0.0.2",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: internal},
		},
		host: "https://10.0.0.2",
		ca:   "ca-bytes",
	}, {
		// With the IP endpoint disabled, GKE publishes the DNS hostname in
		// Endpoint whether or not external traffic is allowed. The host is
		// the same Google frontend, so the cluster CA must not be attached:
		// paired, every request fails with an x509 error.
		name: "the DNS hostname in Endpoint takes no CA even with external traffic off",
		cluster: &container.Cluster{
			Endpoint:                    "gke-abc.us-central1.gke.goog",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: internal},
		},
		host: "https://gke-abc.us-central1.gke.goog",
	}, {
		// The same shape without MasterAuth: no CA is needed on a Google
		// frontend, so its absence is not the IP branch's missing-CA error.
		name: "the DNS hostname in Endpoint needs no MasterAuth",
		cluster: &container.Cluster{
			Endpoint:                    "gke-abc.us-central1.gke.goog",
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: internal},
		},
		host: "https://gke-abc.us-central1.gke.goog",
	}, {
		// A DNS config with no hostname published cannot match Endpoint, so
		// the IP branch decides, CA included.
		name: "an empty DNS hostname never matches Endpoint",
		cluster: &container.Cluster{
			Endpoint:                    "10.0.0.2",
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: &container.DNSEndpointConfig{}},
		},
		host: "https://10.0.0.2",
		ca:   "ca-bytes",
	}, {
		// Both fields empty, the transient shape of a cluster still
		// provisioning: two empty strings are equal, and without the
		// non-empty guard the equality branch would return "https://" as
		// the host, the relative-URL client the function exists to refuse.
		name: "two empty endpoints are the missing-address error, not an empty host",
		cluster: &container.Cluster{
			MasterAuth:                  &container.MasterAuth{ClusterCaCertificate: ca},
			ControlPlaneEndpointsConfig: &container.ControlPlaneEndpointsConfig{DnsEndpointConfig: &container.DNSEndpointConfig{}},
		},
		wantErr: "neither",
	}, {
		name:    "IP endpoint with no DNS config",
		cluster: &container.Cluster{Endpoint: "10.0.0.2", MasterAuth: &container.MasterAuth{ClusterCaCertificate: ca}},
		host:    "https://10.0.0.2",
		ca:      "ca-bytes",
	}, {
		// An empty Host would hand rest.Config a relative URL and build a
		// client that talks to nothing without saying so.
		name:    "neither endpoint is an error, not an empty host",
		cluster: &container.Cluster{MasterAuth: &container.MasterAuth{ClusterCaCertificate: ca}},
		wantErr: "neither",
	}, {
		name:    "an IP endpoint with no CA is an error",
		cluster: &container.Cluster{Endpoint: "10.0.0.2"},
		wantErr: "CA certificate",
	}, {
		name:    "no cluster at all",
		wantErr: "no cluster",
	}} {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := ClientConfigForIdentity(tc.cluster)
			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("expected an error containing %q, got config %+v", tc.wantErr, cfg)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error %q does not contain %q", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("ClientConfigForIdentity: %v", err)
			}
			if cfg.Host != tc.host {
				t.Errorf("host = %q, want %q", cfg.Host, tc.host)
			}
			if got := string(cfg.TLSClientConfig.CAData); got != tc.ca {
				t.Errorf("CA = %q, want %q", got, tc.ca)
			}
			// The credential is attached separately, by UseGoogleTokenSource.
			if cfg.BearerToken != "" || cfg.ExecProvider != nil {
				t.Error("ClientConfigForIdentity must carry no credential")
			}
		})
	}
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestUseGoogleTokenSource(t *testing.T) {
	// Whatever authentication the config arrived with must be dropped and
	// replaced with a bearer token, while the server address and CA are left
	// alone. The case that matters is a GKE kubeconfig's exec directive: it
	// runs gke-gcloud-auth-plugin, which this image does not ship, so leaving
	// it in place fails at the first request rather than at construction.
	cfg := &rest.Config{
		Host:         "https://example.invalid",
		ExecProvider: &clientcmdapi.ExecConfig{Command: "gke-gcloud-auth-plugin"},
	}
	UseGoogleTokenSource(cfg, oauth2.StaticTokenSource(&oauth2.Token{AccessToken: "test-token"}))

	if cfg.ExecProvider != nil {
		t.Error("ExecProvider still set; the missing plugin would still be invoked")
	}
	if cfg.Host != "https://example.invalid" {
		t.Errorf("Host = %q; the server address must survive untouched", cfg.Host)
	}
	if cfg.WrapTransport == nil {
		t.Fatal("WrapTransport not set; no credential would be attached")
	}

	// The wrapper must actually put the token on the wire.
	var got string
	rt := cfg.WrapTransport(roundTripperFunc(func(r *http.Request) (*http.Response, error) {
		got = r.Header.Get("Authorization")
		return &http.Response{StatusCode: 200, Body: http.NoBody, Request: r}, nil
	}))
	req, err := http.NewRequest(http.MethodGet, "https://example.invalid/api/v1/events", nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	if _, err := rt.RoundTrip(req); err != nil {
		t.Fatalf("round trip: %v", err)
	}
	if want := "Bearer test-token"; got != want {
		t.Errorf("Authorization = %q; want %q", got, want)
	}
}
