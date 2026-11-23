package upstream

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"

	"golang.org/x/net/http/httpguts"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	httpScheme                = "http"
	upgradeHeader             = "Upgrade"
	staticMaxIdleConnsPerHost = 32
)

// StaticConfig configures a StaticTransport for a single HTTP or HTTPS upstream endpoint.
type StaticConfig struct {
	// URL is the upstream http:// or https:// base URL. StaticTransport prepends an optional
	// path prefix to each request path and refuses user credentials, query strings, and fragments.
	URL string
	// ClusterID names the single cluster identifier RoundTripCluster serves; empty defaults to
	// k8sapiproxy.InClusterID ("in-cluster").
	ClusterID string
	// TLSClientConfig customizes TLS verification for https:// endpoints when Transport is nil.
	TLSClientConfig *tls.Config
	// Transport overrides the HTTP RoundTripper used to reach URL when non-nil.
	Transport http.RoundTripper
}

// StaticTransport implements k8sapiproxy.UpstreamTransport for one pre-resolved HTTP or
// HTTPS endpoint without cloud credential discovery.
type StaticTransport struct {
	clusterID       string
	target          *url.URL
	basePath        string
	baseEscapedPath string
	customRT        http.RoundTripper
	rt              http.RoundTripper
	upgrade         http.RoundTripper
}

var _ k8sapiproxy.UpstreamTransport = (*StaticTransport)(nil)

// NewStatic validates cfg and returns a StaticTransport targeting cfg.URL.
func NewStatic(cfg StaticConfig) (*StaticTransport, error) {
	clusterID := cfg.ClusterID
	if clusterID == "" {
		clusterID = k8sapiproxy.InClusterID
	} else if !k8sapiproxy.IsValidClusterID(clusterID) {
		return nil, fmt.Errorf("invalid static cluster ID %q", clusterID)
	}

	raw := strings.TrimSpace(cfg.URL)
	if raw == "" {
		return nil, errors.New("static upstream URL is required")
	}
	if strings.ContainsFunc(raw, k8sapiproxy.IsDisallowedURLRune) {
		return nil, errors.New("static upstream URL must not contain backslashes or control characters")
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil, fmt.Errorf("invalid static upstream URL: %w", err)
	}
	if u.Scheme != httpScheme && u.Scheme != httpsScheme {
		return nil, fmt.Errorf("static upstream URL scheme must be %q or %q", httpScheme, httpsScheme)
	}
	if u.Host == "" || u.Hostname() == "" || u.Opaque != "" {
		return nil, errors.New("static upstream URL must include a host")
	}
	if u.User != nil {
		return nil, errors.New("static upstream URL must not contain user credentials")
	}
	if u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.RawFragment != "" {
		return nil, errors.New("static upstream URL must not contain a query or fragment")
	}
	if err := k8sapiproxy.ValidateURLPath(u.Path, u.RawPath); err != nil {
		return nil, fmt.Errorf("static upstream URL path: %w", err)
	}

	var basePath, baseEscapedPath string
	if u.Path != "" && u.Path != "/" {
		basePath = strings.TrimSuffix(u.Path, "/")
		baseEscapedPath = strings.TrimSuffix(u.EscapedPath(), "/")
	}

	st := &StaticTransport{
		clusterID:       clusterID,
		target:          &url.URL{Scheme: u.Scheme, Host: u.Host},
		basePath:        basePath,
		baseEscapedPath: baseEscapedPath,
		customRT:        cfg.Transport,
	}
	if cfg.Transport == nil {
		base, ok := http.DefaultTransport.(*http.Transport)
		if !ok {
			base = &http.Transport{}
		}
		rt := base.Clone()
		if rt.MaxIdleConnsPerHost < staticMaxIdleConnsPerHost {
			rt.MaxIdleConnsPerHost = staticMaxIdleConnsPerHost
		}
		tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if cfg.TLSClientConfig != nil {
			tlsCfg = cfg.TLSClientConfig.Clone()
			if tlsCfg.MinVersion == 0 {
				tlsCfg.MinVersion = tls.VersionTLS12
			}
		}
		rt.TLSClientConfig = tlsCfg
		rt.ForceAttemptHTTP2 = u.Scheme == httpsScheme

		upgrade := base.Clone()
		upgradeTLS := tlsCfg.Clone()
		upgradeTLS.NextProtos = []string{http1Proto}
		upgrade.TLSClientConfig = upgradeTLS
		upgrade.ForceAttemptHTTP2 = false
		upgrade.TLSNextProto = make(map[string]func(string, *tls.Conn) http.RoundTripper)

		st.rt = rt
		st.upgrade = upgrade
	}
	return st, nil
}

// RoundTripCluster forwards req for StaticConfig.ClusterID to the static upstream URL without
// following redirects, and refuses any other clusterID.
func (t *StaticTransport) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	if clusterID != t.clusterID {
		return nil, fmt.Errorf("cluster %q: static upstream only serves %q", clusterID, t.clusterID)
	}
	if req == nil || req.URL == nil {
		return nil, errors.New("request URL is required")
	}
	joinedPath, joinedRawPath, err := joinStaticPath(t.basePath, t.baseEscapedPath, req.URL)
	if err != nil {
		return nil, err
	}

	out := req.Clone(ctx)
	for k := range out.Header {
		if strings.EqualFold(k, authorizationHeader) {
			delete(out.Header, k)
		}
	}
	out.URL = &url.URL{
		Scheme:   t.target.Scheme,
		Host:     t.target.Host,
		Path:     joinedPath,
		RawPath:  joinedRawPath,
		RawQuery: req.URL.RawQuery,
	}
	out.Host = ""
	out.RequestURI = ""

	resp, err := t.roundTripperFor(out).RoundTrip(out)
	if err != nil {
		return nil, err
	}
	if isRedirect(resp.StatusCode) {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("cluster %q answered with a redirect (%d)", clusterID, resp.StatusCode)
	}
	return resp, nil
}

func (t *StaticTransport) roundTripperFor(req *http.Request) http.RoundTripper {
	if t.customRT != nil {
		return t.customRT
	}
	for k, vals := range req.Header {
		if strings.EqualFold(k, connectionHeader) && httpguts.HeaderValuesContainsToken(vals, upgradeToken) {
			return t.upgrade
		}
		if strings.EqualFold(k, upgradeHeader) {
			for _, v := range vals {
				if strings.TrimSpace(v) != "" {
					return t.upgrade
				}
			}
		}
	}
	return t.rt
}

func joinStaticPath(basePath, baseEscapedPath string, reqURL *url.URL) (path, rawPath string, err error) {
	reqPath := reqURL.Path
	if reqPath == "" {
		reqPath = "/"
	}
	if !strings.HasPrefix(reqPath, "/") {
		return "", "", errors.New("request path must start with '/'")
	}
	if err := k8sapiproxy.ValidateURLPath(reqPath, reqURL.RawPath); err != nil {
		return "", "", fmt.Errorf("request path: %w", err)
	}
	reqEscaped := reqURL.EscapedPath()
	if reqEscaped == "" {
		reqEscaped = "/"
	}
	if basePath == "" {
		if reqURL.RawPath != "" && reqEscaped == reqURL.RawPath {
			return reqPath, reqURL.RawPath, nil
		}
		return reqPath, "", nil
	}
	joinedPath := basePath + reqPath
	if baseEscapedPath != basePath || reqURL.RawPath != "" {
		joinedRaw := baseEscapedPath + reqEscaped
		if unescaped, uerr := url.PathUnescape(joinedRaw); uerr == nil && unescaped == joinedPath && joinedRaw != (&url.URL{Path: joinedPath}).EscapedPath() {
			return joinedPath, joinedRaw, nil
		}
	}
	return joinedPath, "", nil
}
