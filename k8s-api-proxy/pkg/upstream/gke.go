package upstream

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

const (
	gkeClusterPrefix = "gke_"
	gkeIDParts       = 3
	gkeIDSeparator   = "_"
	// maxClusterResponse bounds a Container API cluster description.
	maxClusterResponse = 4 << 20
	clusterPathFormat  = "%s/v1/projects/%s/locations/%s/clusters/%s"
)

var (
	// gkeProject accepts a project ID, optionally domain-scoped (example.com:project), as
	// k8sapiproxy's cluster ID validation does.
	gkeProject = regexp.MustCompile(`^([a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z0-9-]+\z`)
	gkeSegment = regexp.MustCompile(`^[a-z0-9-]+\z`)
)

type gkeCluster struct {
	project, location, name string
}

// parseGKEClusterID splits a kubeconfig context name of the form gke_PROJECT_LOCATION_NAME.
func parseGKEClusterID(clusterID string) (gkeCluster, error) {
	rest, ok := strings.CutPrefix(clusterID, gkeClusterPrefix)
	parts := strings.Split(rest, gkeIDSeparator)
	if !ok || len(parts) != gkeIDParts || !gkeProject.MatchString(parts[0]) ||
		!gkeSegment.MatchString(parts[1]) || !gkeSegment.MatchString(parts[2]) {
		return gkeCluster{}, fmt.Errorf("%q is not a gke_PROJECT_LOCATION_NAME cluster ID", clusterID)
	}
	return gkeCluster{project: parts[0], location: parts[1], name: parts[2]}, nil
}

// clusterDescription holds the fields of a Container API Cluster that choose its endpoint.
type clusterDescription struct {
	Endpoint   string `json:"endpoint"`
	MasterAuth struct {
		ClusterCACertificate string `json:"clusterCaCertificate"`
	} `json:"masterAuth"`
	ControlPlaneEndpointsConfig struct {
		DNSEndpointConfig struct {
			Endpoint             string `json:"endpoint"`
			AllowExternalTraffic *bool  `json:"allowExternalTraffic"`
		} `json:"dnsEndpointConfig"`
		IPEndpointsConfig *struct {
			Enabled *bool `json:"enabled"`
		} `json:"ipEndpointsConfig"`
	} `json:"controlPlaneEndpointsConfig"`
}

type endpoint struct {
	host string
	// dns is a DNS endpoint, whose certificate is publicly trusted.
	dns    bool
	caData []byte
}

func (t *Transport) discover(ctx context.Context, c gkeCluster) (endpoint, error) {
	u := fmt.Sprintf(clusterPathFormat, t.containerAPIURL,
		url.PathEscape(c.project), url.PathEscape(c.location), url.PathEscape(c.name))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return endpoint{}, err
	}
	resp, err := t.discovery.Do(req)
	if err != nil {
		return endpoint{}, fmt.Errorf("describe: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return endpoint{}, fmt.Errorf("describe: Container API answered %d", resp.StatusCode)
	}
	var desc clusterDescription
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxClusterResponse)).Decode(&desc); err != nil {
		return endpoint{}, fmt.Errorf("describe: %w", err)
	}
	return selectEndpoint(desc)
}

// selectEndpoint picks the endpoint gcloud container clusters get-credentials would write,
// following rules 1, 2 and 4 of docs/designs/private-endpoint-selection.md: the DNS endpoint
// when it accepts external traffic or the IP endpoints are off, otherwise the cluster's
// endpoint with its CA. Rule 3, the private endpoint on the proxy's own network, needs facts
// about the proxy's cluster and is not applied.
func selectEndpoint(desc clusterDescription) (endpoint, error) {
	dns := desc.ControlPlaneEndpointsConfig.DNSEndpointConfig
	ip := desc.ControlPlaneEndpointsConfig.IPEndpointsConfig
	ipOff := ip != nil && (ip.Enabled == nil || !*ip.Enabled)
	if dns.Endpoint != "" && dns.AllowExternalTraffic != nil && *dns.AllowExternalTraffic {
		return endpoint{host: dns.Endpoint, dns: true}, nil
	}
	if ipOff {
		if dns.Endpoint == "" {
			return endpoint{}, errors.New("IP endpoints are off and there is no DNS endpoint")
		}
		return endpoint{host: dns.Endpoint, dns: true}, nil
	}
	if desc.Endpoint == "" {
		return endpoint{}, errors.New("the cluster reports no endpoint")
	}
	ca, err := base64.StdEncoding.DecodeString(desc.MasterAuth.ClusterCACertificate)
	if err != nil || len(ca) == 0 {
		// An IP endpoint's certificate is signed by the cluster's own CA; without it the
		// control plane cannot be verified.
		return endpoint{}, errors.New("the cluster reports no usable CA certificate for its IP endpoint")
	}
	return endpoint{host: desc.Endpoint, caData: ca}, nil
}
