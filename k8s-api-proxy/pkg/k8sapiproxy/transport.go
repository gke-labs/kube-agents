package k8sapiproxy

import (
	"context"
	"net/http"
)

// CallerIdentity is an authenticated sandbox caller.
type CallerIdentity struct {
	Username string
	UID      string
	Groups   []string
}

// CallerAuthenticator identifies the caller of a request, returning an error when it is not
// an authorized sandbox. The caller gets a fixed 401, never the error's text.
type CallerAuthenticator interface {
	AuthenticateRequest(r *http.Request) (*CallerIdentity, error)
}

// UpstreamTransport forwards an admitted request to the cluster clusterID names. req carries
// the API server path and query, with the caller's credentials already removed; the
// transport sets the scheme, host and its own credentials. For a 101 Switching Protocols
// response the body must be an io.ReadWriteCloser, as net/http/httputil.ReverseProxy requires
// to splice the upgraded stream.
type UpstreamTransport interface {
	RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error)
}
