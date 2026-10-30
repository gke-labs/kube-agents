// Package kube is the Kubernetes-facing side of the kube-agents MCP bridge:
// it discovers deployments from the local kubeconfig, reads their
// <agent>-a2a-mcp-token Secret when connecting, and carries the bridge's
// connections to the <agent>-a2a-mcp HTTP listener as in-process SPDY
// port-forward streams, with no local listener. It implements
// mcpbridge.ClusterAccess so the bridge core stays free of k8s.io dependencies.
package kube

import (
	"context"

	"github.com/gke-labs/kube-agents/a2a/mcpbridge"
)

// Access implements mcpbridge.ClusterAccess with k8s.io/client-go.
type Access struct{}

// New returns the client-go backed ClusterAccess used by the kube-agents-mcp binary.
func New() mcpbridge.ClusterAccess {
	return Access{}
}

// DiscoverDeployments implements mcpbridge.ClusterAccess.
func (Access) DiscoverDeployments(ctx context.Context, kubeContextFilter string) ([]mcpbridge.DeploymentProfile, error) {
	return DiscoverDeployments(ctx, kubeContextFilter)
}

// PopulateDeploymentCredentials implements mcpbridge.ClusterAccess.
func (Access) PopulateDeploymentCredentials(ctx context.Context, prof *mcpbridge.DeploymentProfile) error {
	return PopulateDeploymentCredentials(ctx, prof)
}

// StartGatewayPortForward implements mcpbridge.ClusterAccess.
func (Access) StartGatewayPortForward(ctx context.Context, prof mcpbridge.DeploymentProfile) (mcpbridge.Tunnel, error) {
	tun, err := StartGatewayPortForward(ctx, prof)
	if err != nil {
		return nil, err
	}
	return tun, nil
}
