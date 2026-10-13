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
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"

	container "google.golang.org/api/container/v1"
	"google.golang.org/api/option"
	"k8s.io/client-go/rest"
)

const (
	// BrokerURLEnv and BrokerTokenFileEnv are the two variables the operator
	// sets on the gateway pod's agent-api-auth container to route this
	// package's Google and fleet calls through the credential proxy. Both or
	// neither: see BrokerFromEnv.
	BrokerURLEnv       = "WATCHER_BROKER_URL"
	BrokerTokenFileEnv = "WATCHER_BROKER_TOKEN_FILE"

	// brokerGCPPathFormat is the proxy's route for a Google REST API, taking
	// the proxy's base URL and the API's host. The trailing slash is
	// load-bearing: the generated clients resolve their "v1/..." paths against
	// it as a base, and without the slash the host segment would be replaced
	// rather than extended.
	brokerGCPPathFormat = "%s/v1/watcher/gcp/%s/"

	// brokerK8sPathFormat is the proxy's route for one fleet cluster's
	// Kubernetes API, taking the base URL, project, location and cluster.
	// client-go appends /api/v1/... to it (rest.DefaultServerURL treats a
	// path on Host as a prefix).
	brokerK8sPathFormat = "%s/v1/k8s/%s/%s/%s"

	// ContainerAPIHost and PubsubAPIHost are the Google APIs the proxy's
	// watcher route forwards to, named as the proxy's path names them.
	ContainerAPIHost = "container.googleapis.com"
	PubsubAPIHost    = "pubsub.googleapis.com"

	// authorizationHeader and bearerScheme carry the projected ServiceAccount
	// token the proxy authenticates this pod with.
	authorizationHeader = "Authorization"
	bearerScheme        = "Bearer "
)

// brokerSegment is what one project, location or cluster name must look like
// before it is spliced into a proxy path. The identity comes from a profile's
// config.yaml, and the proxy authorizes by path, so a "/" or ".." in it would
// address a route the identity does not name. Covers GKE names, regions and
// zones, and legacy domain-scoped project IDs (example.com:project).
var brokerSegment = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.:_-]*$`)

// brokerSchemes are the schemes BrokerURLEnv may carry: http for the
// in-cluster Service the operator points it at, https for a proxy behind TLS.
var brokerSchemes = map[string]bool{"http": true, "https": true}

// Broker is the credential proxy this process reaches Google and the fleet
// through, in place of its own Workload Identity. Every request carries the
// pod's projected ServiceAccount token, and the proxy attaches the Google or
// cluster credential itself, so no Google token is minted in this process.
type Broker struct {
	// URL is the proxy's base URL, with no trailing slash.
	URL string
	// TokenFile is the projected ServiceAccount token. The kubelet rotates it
	// in place, so it is read per request rather than once.
	TokenFile string
}

// BrokerFromEnv returns the proxy BrokerURLEnv and BrokerTokenFileEnv name, or
// nil when neither is set -- the Workload Identity path, unchanged.
//
// Exactly one set is an error rather than a fall back to Workload Identity:
// the operator sets them as a pair, so half a pair is a broken install, and
// quietly reaching Google with the pod's own identity is the behaviour the
// variables exist to turn off.
func BrokerFromEnv() (*Broker, error) {
	base := strings.TrimRight(os.Getenv(BrokerURLEnv), "/")
	tokenFile := os.Getenv(BrokerTokenFileEnv)
	switch {
	case base == "" && tokenFile == "":
		return nil, nil
	case base == "" || tokenFile == "":
		return nil, fmt.Errorf("%s and %s are set together or not at all; got %s=%q %s=%q",
			BrokerURLEnv, BrokerTokenFileEnv, BrokerURLEnv, base, BrokerTokenFileEnv, tokenFile)
	}
	u, err := url.Parse(base)
	if err != nil || !brokerSchemes[u.Scheme] || u.Host == "" {
		return nil, fmt.Errorf("%s=%q is not an http(s) URL", BrokerURLEnv, base)
	}
	return &Broker{URL: base, TokenFile: tokenFile}, nil
}

// HTTPClient returns a client that sends the current projected token on every
// request.
func (b *Broker) HTTPClient() *http.Client {
	return &http.Client{Transport: &bearerFileTransport{path: b.TokenFile, base: http.DefaultTransport}}
}

// GoogleAPIOptions points a google.golang.org/api client at the proxy's route
// for apiHost. WithHTTPClient alone already bypasses Application Default
// Credentials (transport/http.NewClient returns the supplied client as is);
// WithoutAuthentication states it, so a later option cannot bring ADC back.
func (b *Broker) GoogleAPIOptions(apiHost string) []option.ClientOption {
	return []option.ClientOption{
		option.WithEndpoint(fmt.Sprintf(brokerGCPPathFormat, b.URL, apiHost)),
		option.WithHTTPClient(b.HTTPClient()),
		option.WithoutAuthentication(),
	}
}

// ClientConfig addresses one fleet cluster's Kubernetes API through the
// proxy, which resolves the control plane and authenticates to it. Plain HTTP
// and no CA: the hop is in-cluster, and what is verified on the far side is the
// proxy's business. BearerTokenFile, unlike BearerToken, is re-read by
// client-go as the kubelet rotates it.
func (b *Broker) ClientConfig(id Identity) (*rest.Config, error) {
	if err := id.brokerSafe(); err != nil {
		return nil, err
	}
	return &rest.Config{
		Host:            fmt.Sprintf(brokerK8sPathFormat, b.URL, id.Project, id.Location, id.Cluster),
		BearerTokenFile: b.TokenFile,
	}, nil
}

// describeCluster is describeClusterViaAPI through the proxy: the same
// generated client and resource name, so the Cluster comes back exactly as the
// GKE API wrote it.
func (b *Broker) describeCluster(ctx context.Context, id Identity) (*container.Cluster, error) {
	if err := id.brokerSafe(); err != nil {
		return nil, err
	}
	svc, err := container.NewService(ctx, b.GoogleAPIOptions(ContainerAPIHost)...)
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

// brokerSafe checks each part of the identity against brokerSegment.
func (i Identity) brokerSafe() error {
	for _, part := range []string{i.Project, i.Location, i.Cluster} {
		if !brokerSegment.MatchString(part) {
			return fmt.Errorf("cluster_identity %q has a part that cannot be a proxy path segment: %q", i.String(), part)
		}
	}
	return nil
}

// bearerFileTransport sets Authorization from a token file on every request.
// Read each time rather than cached: the file is on a tmpfs and the callers
// make at most a few requests a second, so a stale token after rotation is not
// worth a cache to avoid.
type bearerFileTransport struct {
	path string
	base http.RoundTripper
}

func (t *bearerFileTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	raw, err := os.ReadFile(t.path) // #nosec G304 -- the operator-set BrokerTokenFileEnv, not request data
	if err == nil && strings.TrimSpace(string(raw)) == "" {
		err = errors.New("file is empty")
	}
	if err != nil {
		// RoundTrip owns the body even when it never sends it.
		if req.Body != nil {
			_ = req.Body.Close()
		}
		return nil, fmt.Errorf("read the credential proxy token %s: %w", t.path, err)
	}
	out := req.Clone(req.Context())
	out.Header.Set(authorizationHeader, bearerScheme+strings.TrimSpace(string(raw)))
	return t.base.RoundTrip(out)
}
