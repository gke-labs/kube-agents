package k8sapiproxy

import (
	"context"
	"fmt"
	"net/http"

	"go.opentelemetry.io/otel/trace"
)

// CallerIdentity describes an authenticated sandbox caller.
type CallerIdentity struct {
	Username string
	UID      string
	Groups   []string
}

// CallerAuthenticator verifies that an incoming request comes from an authorized sandbox.
type CallerAuthenticator interface {
	AuthenticateRequest(r *http.Request) (*CallerIdentity, error)
}

// UpstreamTransport forwards a sanitized Kubernetes API request to the target cluster.
// The request URL Path and RawQuery are already normalized to canonical kube-apiserver paths
// (for example, "/api/v1/namespaces/default/pods").
type UpstreamTransport interface {
	RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error)
}

// Mode controls which Kubernetes API verbs the proxy permits.
type Mode string

const (
	// ModeReadOnly permits get, list, and watch on resources, plus safe discovery paths.
	ModeReadOnly Mode = "read-only"

	// ModeReadOnlyWithDryRun additionally permits create, update, and patch when dryRun=All is set.
	// This is the mode that kube-agents uses.
	ModeReadOnlyWithDryRun Mode = "read-only-dry-run"

	// ModeReadWrite permits mutating verbs (create, update, patch, delete, deletecollection)
	// for external agent platforms that authorize writes, while still stripping caller auth/impersonation
	// headers, redacting Secret payloads, and refusing credential-minting subresources.
	ModeReadWrite Mode = "read-write"
)

// ParseMode parses and validates a Mode string.
func ParseMode(raw string) (Mode, error) {
	switch Mode(raw) {
	case "", ModeReadOnlyWithDryRun:
		return ModeReadOnlyWithDryRun, nil
	case ModeReadOnly:
		return ModeReadOnly, nil
	case ModeReadWrite:
		return ModeReadWrite, nil
	default:
		return "", fmt.Errorf("invalid proxy mode %q: expected %q, %q, or %q", raw, ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite)
	}
}

// Config configures the k8sapiproxy HTTP handler.
type Config struct {
	Authenticator            CallerAuthenticator
	Upstream                 UpstreamTransport
	DefaultClusterID         string
	Mode                     Mode
	RedactSecrets            bool
	AllowUpgradeSubresources bool
	Metrics                  *Metrics
	TracerProvider           trace.TracerProvider
	AuditSink                AuditSink
}
