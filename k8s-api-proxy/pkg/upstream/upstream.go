// Package upstream implements k8sapiproxy.UpstreamTransport for the cluster the proxy runs in
// and for GKE clusters, which it reaches with the proxy's own Google credentials.
package upstream

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"golang.org/x/net/http/httpguts"
	"golang.org/x/oauth2"
	"golang.org/x/oauth2/google"
	"golang.org/x/sync/singleflight"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	defaultContainerAPIURL = "https://container.googleapis.com"
	cloudPlatformScope     = "https://www.googleapis.com/auth/cloud-platform"
	defaultEndpointTTL     = 10 * time.Minute
	// failureTTL bounds how often a cluster that cannot be resolved costs a Container API call.
	failureTTL          = 10 * time.Second
	discoveryTimeout    = 15 * time.Second
	kindClusterPrefix   = "kind-"
	httpsScheme         = "https"
	schemeSeparator     = "://"
	authorizationHeader = "Authorization"
	connectionHeader    = "Connection"
	upgradeToken        = "upgrade"
	http1Proto          = "http/1.1"
)

// Config configures a Transport.
type Config struct {
	// InCluster reaches the cluster the proxy runs in. Nil reads rest.InClusterConfig, which
	// rereads the pod's service-account token as the kubelet rotates it.
	InCluster *rest.Config
	// HostClusterID is the gke_ ID of the cluster the proxy runs in, if it has one; requests
	// for it go through InCluster rather than its public endpoint.
	HostClusterID string
	// TokenSource authenticates to the Container API and to GKE control planes. Nil uses
	// Application Default Credentials: on GKE, the Workload Identity the proxy's service
	// account maps to.
	TokenSource oauth2.TokenSource
	// ContainerAPIURL is the Container API base URL; empty is container.googleapis.com.
	ContainerAPIURL string
	// EndpointTTL is how long a resolved GKE endpoint is reused; zero is ten minutes.
	EndpointTTL time.Duration
}

// Transport forwards requests to the cluster a request's cluster ID names.
type Transport struct {
	hostClusterID   string
	inClusterTarget *url.URL
	inClusterRT     *roundTrippers
	tokens          oauth2.TokenSource
	discovery       *http.Client
	containerAPIURL string
	endpointTTL     time.Duration
	// dnsCAData, when set, replaces the system roots for a DNS endpoint. Tests only.
	dnsCAData []byte
	now       func() time.Time

	mu       sync.Mutex
	clusters map[string]clusterEntry
	resolve  singleflight.Group
}

var _ k8sapiproxy.UpstreamTransport = (*Transport)(nil)

type clusterEntry struct {
	target   *url.URL
	rt       *roundTrippers
	err      error
	resolved time.Time
	expires  time.Time
}

// New returns a Transport. It reads the in-cluster configuration and finds the default Google
// credentials up front, so a pod missing either fails at startup rather than per request.
func New(ctx context.Context, cfg Config) (*Transport, error) {
	inCluster := cfg.InCluster
	if inCluster == nil {
		var err error
		if inCluster, err = rest.InClusterConfig(); err != nil {
			return nil, fmt.Errorf("in-cluster config: %w", err)
		}
	}
	target, err := targetURL(inCluster.Host)
	if err != nil {
		return nil, fmt.Errorf("in-cluster host: %w", err)
	}
	inClusterRT, err := newRoundTrippers(inCluster)
	if err != nil {
		return nil, fmt.Errorf("in-cluster transport: %w", err)
	}
	if cfg.HostClusterID != "" {
		if _, err := parseGKEClusterID(cfg.HostClusterID); err != nil {
			return nil, fmt.Errorf("host cluster ID: %w", err)
		}
	}
	tokens := cfg.TokenSource
	if tokens == nil {
		if tokens, err = google.DefaultTokenSource(ctx, cloudPlatformScope); err != nil {
			return nil, fmt.Errorf("default Google credentials: %w", err)
		}
	}
	apiURL := strings.TrimRight(cfg.ContainerAPIURL, "/")
	if apiURL == "" {
		apiURL = defaultContainerAPIURL
	}
	if u, err := url.Parse(apiURL); err != nil || u.Scheme != httpsScheme || u.Host == "" {
		// The Container API is sent the proxy's Google token.
		return nil, errors.New("container API URL must be https")
	}
	ttl := cfg.EndpointTTL
	if ttl <= 0 {
		ttl = defaultEndpointTTL
	}
	return &Transport{
		hostClusterID:   cfg.HostClusterID,
		inClusterTarget: target,
		inClusterRT:     inClusterRT,
		tokens:          tokens,
		discovery:       &http.Client{Transport: &oauth2.Transport{Source: tokens}, Timeout: discoveryTimeout},
		containerAPIURL: apiURL,
		endpointTTL:     ttl,
		clusters:        make(map[string]clusterEntry),
		now:             time.Now,
	}, nil
}

// RoundTripCluster sends req to the cluster clusterID names: the proxy's own cluster for
// k8sapiproxy.InClusterID or HostClusterID, and a GKE cluster's control plane for any other
// gke_ ID. A kind- context has no upstream here.
func (t *Transport) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	var target *url.URL
	var rt *roundTrippers
	switch {
	case clusterID == k8sapiproxy.InClusterID || t.hostClusterID != "" && clusterID == t.hostClusterID:
		target, rt = t.inClusterTarget, t.inClusterRT
	case strings.HasPrefix(clusterID, kindClusterPrefix):
		return nil, fmt.Errorf("cluster %q: kind clusters have no upstream", clusterID)
	default:
		entry, err := t.cluster(ctx, clusterID)
		if err != nil {
			return nil, err
		}
		target, rt = entry.target, entry.rt
	}

	out := req.Clone(ctx)
	out.URL.Scheme, out.URL.Host = target.Scheme, target.Host
	out.Host = ""
	// client-go's bearer token round tripper keeps an Authorization header it finds, so a
	// caller's credential must not reach it even if a caller of this method left one.
	out.Header.Del(authorizationHeader)
	resp, err := rt.forRequest(out).RoundTrip(out)
	if err != nil {
		if ctx.Err() == nil && target != t.inClusterTarget {
			// The endpoint may have moved, as when a cluster is recreated; resolve it again,
			// though no sooner than a failed resolution would be retried.
			t.forget(clusterID, rt)
		}
		return nil, err
	}
	if isRedirect(resp.StatusCode) {
		// The proxy does not follow a redirect, and relaying one would hand the caller an
		// upstream address.
		_ = resp.Body.Close()
		return nil, fmt.Errorf("cluster %q answered with a redirect (%d)", clusterID, resp.StatusCode)
	}
	return resp, nil
}

// cluster returns the cached endpoint for a GKE cluster, resolving it when it is missing or
// stale. Concurrent callers share one resolution, which runs on its own deadline so that a
// caller giving up does not fail the others.
func (t *Transport) cluster(ctx context.Context, clusterID string) (clusterEntry, error) {
	if entry, ok := t.cached(clusterID); ok {
		return entry, entry.err
	}
	ch := t.resolve.DoChan(clusterID, func() (any, error) {
		rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), discoveryTimeout)
		defer cancel()
		entry := t.resolveCluster(rctx, clusterID)
		t.store(clusterID, entry)
		return entry, nil
	})
	select {
	case res := <-ch:
		entry := res.Val.(clusterEntry)
		return entry, entry.err
	case <-ctx.Done():
		return clusterEntry{}, ctx.Err()
	}
}

func (t *Transport) cached(clusterID string) (clusterEntry, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	entry, ok := t.clusters[clusterID]
	return entry, ok && t.now().Before(entry.expires)
}

// store caches entry and drops expired entries, so cluster IDs a caller makes up do not
// accumulate.
func (t *Transport) store(clusterID string, entry clusterEntry) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := t.now()
	for id, e := range t.clusters {
		if !now.Before(e.expires) {
			delete(t.clusters, id)
		}
	}
	t.clusters[clusterID] = entry
}

// forget shortens the entry holding rt to expire failureTTL after it was resolved, so an
// endpoint that cannot be dialled costs a Container API call at most that often.
func (t *Transport) forget(clusterID string, rt *roundTrippers) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if entry, ok := t.clusters[clusterID]; ok && entry.rt == rt {
		if retry := entry.resolved.Add(failureTTL); retry.Before(entry.expires) {
			entry.expires = retry
			t.clusters[clusterID] = entry
		}
	}
}

func (t *Transport) resolveCluster(ctx context.Context, clusterID string) clusterEntry {
	// Entries are stamped when the resolution ends, so a describe slower than failureTTL is
	// still cached.
	fail := func(err error) clusterEntry {
		now := t.now()
		return clusterEntry{err: fmt.Errorf("cluster %q: %w", clusterID, err), resolved: now, expires: now.Add(failureTTL)}
	}
	coords, err := parseGKEClusterID(clusterID)
	if err != nil {
		return fail(err)
	}
	ep, err := t.discover(ctx, coords)
	if err != nil {
		return fail(err)
	}
	target, err := targetURL(ep.host)
	if err != nil {
		return fail(err)
	}
	caData := ep.caData
	if ep.dns {
		caData = t.dnsCAData
	}
	rt, err := newRoundTrippers(&rest.Config{
		Host:            target.String(),
		TLSClientConfig: rest.TLSClientConfig{CAData: caData},
		WrapTransport: func(base http.RoundTripper) http.RoundTripper {
			return &oauth2.Transport{Source: t.tokens, Base: base}
		},
	})
	if err != nil {
		return fail(err)
	}
	now := t.now()
	return clusterEntry{target: target, rt: rt, resolved: now, expires: now.Add(t.endpointTTL)}
}

// roundTrippers holds a cluster's transport and an HTTP/1.1-only one for upgrades. client-go
// offers HTTP/2, which the API server accepts, and net/http keeps an upgrade other than
// WebSocket on that connection, where the HTTP/2 transport refuses an Upgrade header: a SPDY
// exec, attach or port-forward would fail.
type roundTrippers struct {
	rt, upgrade http.RoundTripper
}

func newRoundTrippers(cfg *rest.Config) (*roundTrippers, error) {
	rt, err := rest.TransportFor(cfg)
	if err != nil {
		return nil, err
	}
	h1 := rest.CopyConfig(cfg)
	h1.NextProtos = []string{http1Proto}
	upgrade, err := rest.TransportFor(h1)
	if err != nil {
		return nil, err
	}
	return &roundTrippers{rt: rt, upgrade: upgrade}, nil
}

func (r *roundTrippers) forRequest(req *http.Request) http.RoundTripper {
	if httpguts.HeaderValuesContainsToken(req.Header[connectionHeader], upgradeToken) {
		return r.upgrade
	}
	return r.rt
}

// targetURL parses an API server address, adding https:// to a bare host or IP address, and
// refuses any other scheme and any path.
func targetURL(host string) (*url.URL, error) {
	if ip := net.ParseIP(host); ip != nil && ip.To4() == nil {
		host = "[" + host + "]"
	}
	if !strings.Contains(host, schemeSeparator) {
		host = httpsScheme + schemeSeparator + host
	}
	u, err := url.Parse(host)
	if err != nil {
		return nil, err
	}
	if u.Scheme != httpsScheme || u.Host == "" || strings.Trim(u.Path, "/") != "" || u.User != nil {
		return nil, errors.New("API server address must be https://host[:port]")
	}
	return &url.URL{Scheme: u.Scheme, Host: u.Host}, nil
}

func isRedirect(code int) bool {
	return code >= http.StatusMultipleChoices && code < http.StatusBadRequest && code != http.StatusNotModified
}
