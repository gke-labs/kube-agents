package k8sapiproxy

import (
	"context"
	"errors"
	"net/http"
)

// CallerIdentity is an authenticated sandbox caller.
type CallerIdentity struct {
	Username string
	UID      string
	Groups   []string
	// Partition is a verified tenant or namespace the authenticator read from the caller's
	// credential, for a Config.CallerPartition that separates callers by it; empty when the
	// authenticator names none.
	Partition string
	// Authenticator names the authenticator that admitted the caller, for a deployment
	// with more than one; empty when the embedder's authenticator does not say.
	Authenticator string
	// Extra holds further attributes of the caller that the authenticator verified, for a
	// Config.Authorize or Config.CallerPartition that decides on more than the name; nil
	// when the authenticator supplies none. The keys are the authenticator's own:
	// pkg/tokenreview copies the TokenReview's status.user.extra, where Kubernetes names the
	// pod a pod-bound ServiceAccount token belongs to
	// (authentication.kubernetes.io/pod-name and authentication.kubernetes.io/pod-uid, for
	// example). The handler reads none of it, and NewSlogAuditSink does not write it: the
	// values can be large and are not the proxy's to judge safe to log. A custom AuditSink
	// that wants them reads AuditEvent.Caller.
	Extra map[string][]string
}

// ErrAuthenticationUnavailable is the error a CallerAuthenticator wraps when it could not
// decide (a TokenReview or a key fetch failed), so the handler answers 503 rather than
// telling the caller its credential is wrong.
var ErrAuthenticationUnavailable = errors.New("caller authentication is unavailable")

// ErrCallerRefused is the error a CallerAuthenticator wraps when it identified the caller's
// credential as its own kind and refused it, such as a pod's ServiceAccount token sent from
// another address. The handler answers 401 as for any refusal; a chain of authenticators
// stops there and does not offer the credential to the next one.
var ErrCallerRefused = errors.New("caller refused")

// CallerAuthenticator identifies the caller of a request, returning an error when it is not
// an authorized sandbox. The caller gets a fixed 401, or a fixed 503 when the error wraps
// ErrAuthenticationUnavailable, never the error's text.
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
