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
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"net/http"

	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	container "google.golang.org/api/container/v1"
	"k8s.io/client-go/rest"
)

const (
	// clusterResourceFormat is the GKE Cluster Manager API's name for one
	// cluster. The location segment takes a region or a zone, so one form
	// covers both.
	clusterResourceFormat = "projects/%s/locations/%s/clusters/%s"

	// GKEAuthScope is the scope gke-gcloud-auth-plugin requests. A
	// cloud-platform token is accepted by the GKE control plane as a bearer
	// credential. Exported because a caller that mints its own token source
	// has to ask for the same scope.
	GKEAuthScope = "https://www.googleapis.com/auth/cloud-platform"

	// httpsScheme prefixes the control-plane addresses the GKE API reports,
	// which are bare hosts.
	httpsScheme = "https://"
)

// describeClusterViaAPI reads one cluster's control-plane addressing from the
// GKE API.
func describeClusterViaAPI(ctx context.Context, id Identity) (*container.Cluster, error) {
	svc, err := container.NewService(ctx)
	if err != nil {
		return nil, fmt.Errorf("GKE API client: %w", err)
	}
	name := fmt.Sprintf(clusterResourceFormat, id.Project, id.Location, id.Cluster)
	cluster, err := svc.Projects.Locations.Clusters.Get(name).Context(ctx).Do()
	if err != nil {
		return nil, fmt.Errorf("get %s: %w", name, err)
	}
	return cluster, nil
}

// defaultTokenSource mints the credential every reached control plane is
// authenticated with.
func defaultTokenSource(ctx context.Context) (oauth2.TokenSource, error) {
	return google.DefaultTokenSource(ctx, GKEAuthScope)
}

// ClientConfigForIdentity turns a GKE cluster description into a rest.Config
// addressing that cluster's control plane. It carries no credential; the caller
// attaches one with UseGoogleTokenSource.
//
// The DNS endpoint wins wherever the cluster publishes one that accepts
// external traffic, which is the same rule agents/platform/scripts/
// gke_endpoint.py applies when it decides whether to pass --dns-endpoint to
// `gcloud container clusters get-credentials`. Keep the two in step: they exist
// to reach the same control planes, and a cluster whose IP endpoint this pod
// cannot route to is exactly the case the DNS endpoint was added for. It needs
// no CA of its own — it terminates on a Google frontend with a WebPKI
// certificate — where the IP endpoint is signed by the cluster's own CA.
//
// An address is required. Returning a config with an empty Host would hand
// rest.Config a relative URL and produce a client that talks to nothing in a
// way no error names.
func ClientConfigForIdentity(cluster *container.Cluster) (*rest.Config, error) {
	if cluster == nil {
		return nil, errors.New("the GKE API returned no cluster")
	}
	if endpoints := cluster.ControlPlaneEndpointsConfig; endpoints != nil && endpoints.DnsEndpointConfig != nil {
		dns := endpoints.DnsEndpointConfig
		if dns.AllowExternalTraffic && dns.Endpoint != "" {
			return &rest.Config{Host: httpsScheme + dns.Endpoint}, nil
		}
	}
	if cluster.Endpoint == "" {
		return nil, errors.New("the cluster publishes neither an externally reachable DNS endpoint nor an IP endpoint")
	}
	if cluster.MasterAuth == nil || cluster.MasterAuth.ClusterCaCertificate == "" {
		return nil, errors.New("the cluster's IP endpoint has no CA certificate to verify it against")
	}
	ca, err := base64.StdEncoding.DecodeString(cluster.MasterAuth.ClusterCaCertificate)
	if err != nil {
		return nil, fmt.Errorf("decode the cluster CA certificate: %w", err)
	}
	return &rest.Config{
		Host:            httpsScheme + cluster.Endpoint,
		TLSClientConfig: rest.TLSClientConfig{CAData: ca},
	}, nil
}

// UseGoogleTokenSource attaches a bearer token minted from this process's
// Google credentials to every request the config makes.
//
// A kubeconfig from `gcloud container clusters get-credentials` would
// authenticate by shelling out to gke-gcloud-auth-plugin. That binary is
// deliberately absent here: the image build refuses to ship credential-aware
// CLIs into the agent's containers (deploy/docker/Dockerfile), concentrating
// them in the credential proxy instead. Rather than widen that boundary, mint
// the token directly — the pod already authenticates to Google as this identity
// via Workload Identity, which is the same identity the plugin would have used.
//
// Only the credential is set. The API server address and CA certificate come
// from ClientConfigForIdentity, and are untouched.
func UseGoogleTokenSource(cfg *rest.Config, ts oauth2.TokenSource) {
	cfg.ExecProvider = nil
	cfg.AuthProvider = nil
	cfg.Wrap(func(rt http.RoundTripper) http.RoundTripper {
		return &oauth2.Transport{Source: ts, Base: rt}
	})
}
