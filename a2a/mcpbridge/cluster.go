package mcpbridge

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"time"
)

// ClusterAccess is the Kubernetes-facing part of the bridge: kubeconfig
// discovery, credential lookup, and the port-forward to the in-cluster a2a-mcp
// listener. It is implemented by package kube (k8s.io/client-go) and injected
// through ServerOptions and RunCLI so this package carries no Kubernetes
// dependency and distributions can supply their own implementation.
type ClusterAccess interface {
	// DiscoverDeployments scans the kubeconfig contexts (or the single filtered
	// context) for kube-agents PlatformAgents and returns their profiles.
	//
	// DiscoverDeployments is metadata-only and never reads cluster Secrets.
	//
	// A context that was scanned and holds no PlatformAgent contributes nothing.
	// A context that could not be scanned (unreachable API server, expired or
	// missing credentials, RBAC denial) contributes a ContextProbeFailure entry
	// instead, so callers can tell "none installed" from "could not look": the
	// store keeps that context's saved profiles rather than pruning them and
	// the tool output names the failure. The error is non-nil only when the
	// kubeconfig itself cannot be loaded or no context at all could be scanned.
	DiscoverDeployments(ctx context.Context, kubeContextFilter string) ([]DeploymentProfile, error)
	// PopulateDeploymentCredentials fills the in-memory MCP Bearer token of prof
	// from the deployment's <agent>-a2a-mcp-token Secret
	// when connecting to that deployment.
	PopulateDeploymentCredentials(ctx context.Context, prof *DeploymentProfile) error
	// StartGatewayPortForward opens a port-forward to the deployment's
	// in-cluster a2a-mcp listener.
	StartGatewayPortForward(ctx context.Context, prof DeploymentProfile) (Tunnel, error)
}

// Tunnel is a port-forward to the in-cluster a2a-mcp listener.
type Tunnel interface {
	// MCPURL returns the base URL requests through the tunnel use.
	MCPURL() string
	// Restart tears down the forward so the next connection re-establishes it.
	Restart()
	// Close stops the forward.
	Close()
}

// StreamTunnel is a Tunnel that carries each HTTP connection itself (the kube package's
// port-forward streams through the API server) instead of through a local listener. The
// bridge dials every request to it with DialContext, so its bearer token never crosses a local
// port another process could hold; MCPURL's host is then only a placeholder.
type StreamTunnel interface {
	Tunnel
	DialContext(ctx context.Context, network, addr string) (net.Conn, error)
}

// ErrNoClusterAccess is returned when a Kubernetes-backed operation is requested
// but the bridge was started without a ClusterAccess implementation.
var ErrNoClusterAccess = errors.New("kube-agents-mcp: no Kubernetes cluster access configured")

// noClusterAccess is the ClusterAccess used when none is injected: every
// Kubernetes-backed operation fails with ErrNoClusterAccess, while direct
// MCP URLs and all host-local state keep working.
type noClusterAccess struct{}

func (noClusterAccess) DiscoverDeployments(context.Context, string) ([]DeploymentProfile, error) {
	return nil, ErrNoClusterAccess
}

func (noClusterAccess) PopulateDeploymentCredentials(context.Context, *DeploymentProfile) error {
	return ErrNoClusterAccess
}

func (noClusterAccess) StartGatewayPortForward(context.Context, DeploymentProfile) (Tunnel, error) {
	return nil, ErrNoClusterAccess
}

// ContextProbeFailure is the entry DiscoverDeployments reports for a kubeconfig
// context it could not scan. It describes the context, not a deployment: it
// carries no Name, so UpsertDeployments never saves it, and its StatusMessage
// holds the failure. SyncDiscoveredDeployments keeps (and marks Not Ready) the
// saved profiles of such a context instead of pruning them, and
// FormatDeploymentsMarkdown lists the failure next to the deployment table.
func ContextProbeFailure(kubeContext string, err error) DeploymentProfile {
	return DeploymentProfile{
		KubeContext:      strings.TrimSpace(kubeContext),
		StatusMessage:    strings.TrimSpace(fmt.Sprint(err)),
		LastDiscoveredAt: time.Now().UTC(),
	}
}

// IsContextProbeFailure reports whether p was produced by ContextProbeFailure
// rather than discovered as a deployment.
func (p DeploymentProfile) IsContextProbeFailure() bool {
	return p.Name == "" && p.KubeContext != ""
}

// Deployments returns the non-ContextProbeFailure deployment entries of a
// DiscoverDeployments result.
func Deployments(profiles []DeploymentProfile) []DeploymentProfile {
	var out []DeploymentProfile
	for _, p := range profiles {
		if !p.IsContextProbeFailure() {
			out = append(out, p)
		}
	}
	return out
}

// ProbeFailures returns the ContextProbeFailure entries of a DiscoverDeployments result.
func ProbeFailures(profiles []DeploymentProfile) []DeploymentProfile {
	var failures []DeploymentProfile
	for _, p := range profiles {
		if p.IsContextProbeFailure() {
			failures = append(failures, p)
		}
	}
	return failures
}

// NewDiscoveryError is the error DiscoverDeployments returns when no kubeconfig
// context could be scanned, naming each context and its failure.
func NewDiscoveryError(failures []DeploymentProfile) error {
	if len(failures) == 0 {
		return nil
	}
	parts := make([]string, 0, len(failures))
	for _, f := range failures {
		parts = append(parts, fmt.Sprintf("%s: %s", f.KubeContext, f.StatusMessage))
	}
	return fmt.Errorf("could not scan any kubeconfig context: %s", strings.Join(parts, "; "))
}
