package gkehttps

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

// Config configures the GKE HTTPS UpstreamTransport.
type Config struct {
	HostClusterID       string
	InClusterServerURL  string
	InClusterCAFile     string
	InClusterTokenFile  string
	ContainerAPIBaseURL string
	TokenProvider       TokenProvider
	DiscoveryClient     *http.Client
	EndpointCacheTTL    time.Duration
}

type cachedClusterTransport struct {
	serverURL *url.URL
	rt        http.RoundTripper
	expiresAt time.Time
}

type discoveryCall struct {
	done  chan struct{}
	entry cachedClusterTransport
	err   error
}

// Transport implements k8sapiproxy.UpstreamTransport over HTTPS for in-cluster and GKE control planes.
type Transport struct {
	hostClusterID      string
	inClusterURL       *url.URL
	inClusterTokenFile string
	inClusterRT        http.RoundTripper

	containerAPIBaseURL string
	tokenProvider       TokenProvider
	discoveryClient     *http.Client
	cacheTTL            time.Duration

	mu       sync.RWMutex
	clusters map[string]cachedClusterTransport
	inflight map[string]*discoveryCall
}

// NewTransport constructs an HTTPS UpstreamTransport.
func NewTransport(cfg Config) (*Transport, error) {
	inClusterServer := strings.TrimSpace(cfg.InClusterServerURL)
	if inClusterServer == "" {
		inClusterServer = "https://kubernetes.default.svc"
	}
	parsedInCluster, err := url.Parse(inClusterServer)
	if err != nil {
		return nil, fmt.Errorf("invalid InClusterServerURL %q: %w", inClusterServer, err)
	}

	caFile := strings.TrimSpace(cfg.InClusterCAFile)
	if caFile == "" {
		caFile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	}
	tokenFile := strings.TrimSpace(cfg.InClusterTokenFile)
	if tokenFile == "" {
		tokenFile = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	}

	inClusterTLS := &tls.Config{MinVersion: tls.VersionTLS12}
	if caBytes, err := os.ReadFile(caFile); err == nil && len(caBytes) > 0 {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caBytes) {
			return nil, fmt.Errorf("failed to parse in-cluster CA bundle at %s", caFile)
		}
		inClusterTLS.RootCAs = pool
	}

	inClusterRT := &http.Transport{
		TLSClientConfig:     inClusterTLS,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        100,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}

	discoveryClient := cfg.DiscoveryClient
	if discoveryClient == nil {
		discoveryClient = &http.Client{Timeout: 15 * time.Second}
	}

	tokenProvider := cfg.TokenProvider
	if tokenProvider == nil {
		tokenProvider = NewGCPTokenSource(GCPTokenConfig{})
	}

	cacheTTL := cfg.EndpointCacheTTL
	if cacheTTL <= 0 {
		cacheTTL = 5 * time.Minute
	}

	return &Transport{
		hostClusterID:       strings.TrimSpace(cfg.HostClusterID),
		inClusterURL:        parsedInCluster,
		inClusterTokenFile:  tokenFile,
		inClusterRT:         inClusterRT,
		containerAPIBaseURL: strings.TrimSpace(cfg.ContainerAPIBaseURL),
		tokenProvider:       tokenProvider,
		discoveryClient:     discoveryClient,
		cacheTTL:            cacheTTL,
		clusters:            make(map[string]cachedClusterTransport),
		inflight:            make(map[string]*discoveryCall),
	}, nil
}

// RoundTripCluster forwards req to the control plane identified by clusterID.
func (t *Transport) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	if clusterID == "" || clusterID == "in-cluster" || strings.HasPrefix(clusterID, "kind-") || (t.hostClusterID != "" && clusterID == t.hostClusterID) {
		return t.roundTripInCluster(ctx, req)
	}
	if strings.HasPrefix(clusterID, "gke_") {
		return t.roundTripGKECluster(ctx, clusterID, req)
	}
	return nil, fmt.Errorf("unsupported cluster ID %q", clusterID)
}

func (t *Transport) roundTripInCluster(ctx context.Context, req *http.Request) (*http.Response, error) {
	tokenBytes, err := os.ReadFile(t.inClusterTokenFile)
	if err != nil {
		return nil, fmt.Errorf("failed to read in-cluster service account token: %w", err)
	}
	token := strings.TrimSpace(string(tokenBytes))
	if token == "" {
		return nil, fmt.Errorf("in-cluster service account token at %s is empty", t.inClusterTokenFile)
	}

	outReq := req.Clone(ctx)
	rewriteRequestTarget(outReq, t.inClusterURL)
	outReq.Header.Set("Authorization", "Bearer "+token)
	resp, err := t.inClusterRT.RoundTrip(outReq)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("upstream control plane returned redirect HTTP %d, which is not permitted", resp.StatusCode)
	}
	return resp, nil
}

func (t *Transport) roundTripGKECluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	coords, err := ParseGKEClusterID(clusterID)
	if err != nil {
		return nil, err
	}

	entry, err := t.getOrDiscoverCluster(ctx, clusterID, coords)
	if err != nil {
		return nil, err
	}

	token, err := t.tokenProvider.AccessToken(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to acquire upstream GCP access token: %w", err)
	}

	outReq := req.Clone(ctx)
	rewriteRequestTarget(outReq, entry.serverURL)
	outReq.Header.Set("Authorization", "Bearer "+token)

	resp, err := entry.rt.RoundTrip(outReq)
	if err != nil {
		if ctx.Err() == nil && !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded) {
			t.mu.Lock()
			if current, ok := t.clusters[clusterID]; ok && current.rt == entry.rt {
				delete(t.clusters, clusterID)
				if ht, ok := current.rt.(*http.Transport); ok {
					ht.CloseIdleConnections()
				}
			}
			t.mu.Unlock()
		}
		return nil, err
	}
	if resp.StatusCode >= 300 && resp.StatusCode < 400 {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("upstream control plane returned redirect HTTP %d, which is not permitted", resp.StatusCode)
	}
	return resp, nil
}

func (t *Transport) getOrDiscoverCluster(ctx context.Context, clusterID string, coords *GKEClusterCoordinates) (cachedClusterTransport, error) {
	now := time.Now()
	t.mu.RLock()
	if entry, ok := t.clusters[clusterID]; ok && now.Before(entry.expiresAt) {
		t.mu.RUnlock()
		return entry, nil
	}
	t.mu.RUnlock()

	t.mu.Lock()
	if entry, ok := t.clusters[clusterID]; ok && time.Now().Before(entry.expiresAt) {
		t.mu.Unlock()
		return entry, nil
	}
	if call, ok := t.inflight[clusterID]; ok {
		t.mu.Unlock()
		select {
		case <-call.done:
			return call.entry, call.err
		case <-ctx.Done():
			return cachedClusterTransport{}, ctx.Err()
		}
	}
	call := &discoveryCall{done: make(chan struct{})}
	t.inflight[clusterID] = call
	t.mu.Unlock()

	entry, err := t.discoverClusterTransport(ctx, clusterID, coords)

	t.mu.Lock()
	delete(t.inflight, clusterID)
	if err == nil {
		if prev, hadPrev := t.clusters[clusterID]; hadPrev && prev.rt != entry.rt {
			if ht, ok := prev.rt.(*http.Transport); ok {
				ht.CloseIdleConnections()
			}
		}
		t.clusters[clusterID] = entry
	}
	call.entry = entry
	call.err = err
	close(call.done)
	t.mu.Unlock()

	return entry, err
}

func (t *Transport) discoverClusterTransport(ctx context.Context, clusterID string, coords *GKEClusterCoordinates) (cachedClusterTransport, error) {
	resolved, err := DiscoverGKEEndpoint(ctx, t.discoveryClient, t.containerAPIBaseURL, t.tokenProvider, coords)
	if err != nil {
		return cachedClusterTransport{}, err
	}

	parsedURL, err := url.Parse(resolved.ServerURL)
	if err != nil {
		return cachedClusterTransport{}, fmt.Errorf("invalid resolved GKE endpoint URL %q: %w", resolved.ServerURL, err)
	}

	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if len(resolved.CACertPEM) > 0 {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(resolved.CACertPEM) {
			return cachedClusterTransport{}, fmt.Errorf("failed to parse cluster CA certificate for %s", clusterID)
		}
		tlsCfg.RootCAs = pool
	}

	rt := &http.Transport{
		TLSClientConfig:     tlsCfg,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        100,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}

	return cachedClusterTransport{
		serverURL: parsedURL,
		rt:        rt,
		expiresAt: time.Now().Add(t.cacheTTL),
	}, nil
}

func rewriteRequestTarget(req *http.Request, target *url.URL) {
	req.URL.Scheme = target.Scheme
	req.URL.Host = target.Host
	req.Host = target.Host
	basePath := strings.TrimRight(target.Path, "/")
	if basePath != "" {
		if !strings.HasPrefix(req.URL.Path, "/") {
			req.URL.Path = "/" + req.URL.Path
		}
		req.URL.Path = basePath + req.URL.Path
	}
	req.URL.RawPath = ""
}
