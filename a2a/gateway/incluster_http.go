package gateway

import (
	"net/http"
	"net/url"
	"time"

	"k8s.io/client-go/rest"
)

// noProxyFunc disables HTTP(S)_PROXY resolution for client-go rest.Config
// transports. Unlike net/http.Transport (where Proxy == nil means no proxy),
// client-go treats a nil rest.Config.Proxy as http.ProxyFromEnvironment.
func noProxyFunc(*http.Request) (*url.URL, error) { return nil, nil }

// newRelayHTTPClient returns an HTTP client for in-cluster credential-proxy
// relay calls with Proxy explicitly disabled, so corporate HTTP_PROXY and
// HTTPS_PROXY variables forwarded onto the A2A gateway container never
// intercept cluster-internal relay traffic.
func newRelayHTTPClient(timeout time.Duration) *http.Client {
	var transport *http.Transport
	if base, ok := http.DefaultTransport.(*http.Transport); ok {
		transport = base.Clone()
	} else {
		transport = &http.Transport{}
	}
	transport.Proxy = nil
	return &http.Client{
		Timeout:   timeout,
		Transport: transport,
	}
}

// disableRESTConfigProxy sets Proxy to noProxyFunc on rc so cluster-internal
// Kubernetes API requests bypass forwarded HTTP_PROXY and HTTPS_PROXY settings
// even when NO_PROXY omits KUBERNETES_SERVICE_HOST.
func disableRESTConfigProxy(rc *rest.Config) *rest.Config {
	if rc == nil {
		return nil
	}
	rc.Proxy = noProxyFunc
	return rc
}

// InClusterRESTConfig returns rest.InClusterConfig with HTTP proxy resolution
// explicitly disabled for cluster-internal Kubernetes API traffic.
func InClusterRESTConfig() (*rest.Config, error) {
	rc, err := rest.InClusterConfig()
	if err != nil {
		return nil, err
	}
	return disableRESTConfigProxy(rc), nil
}
