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
// an authorized sandbox. The caller gets a fixed 401, or a fixed 503 when the package's
// TokenReview authenticator could not complete the review, never the error's text.
type CallerAuthenticator interface {
	AuthenticateRequest(r *http.Request) (*CallerIdentity, error)
}

// partitionKey carries Config.CallerPartition's value for the request to the cache and the
// Upstream.
type partitionKey struct{}

// PartitionFromContext returns the partition Config.CallerPartition gave the request whose
// context ctx is; ok is false when the handler has no CallerPartition, so a transport that
// chooses a credential per caller can refuse rather than fall back to a shared one. ""
// is a partition like any other. The transport chooses by this value alone: a coalesced
// fetch serves every request of its partition, and its context is the first request's.
func PartitionFromContext(ctx context.Context) (partition string, ok bool) {
	partition, ok = ctx.Value(partitionKey{}).(string)
	return partition, ok
}

// UpstreamTransport forwards an admitted request to the cluster clusterID names. A transport
// that holds more than one credential picks it by PartitionFromContext(ctx) and nothing else. req carries
// the API server path and query, with the caller's credentials already removed; the
// transport sets the scheme, host and its own credentials. For a 101 Switching Protocols
// response the body must be an io.ReadWriteCloser, as net/http/httputil.ReverseProxy requires
// to splice the upgraded stream.
type UpstreamTransport interface {
	RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error)
}
