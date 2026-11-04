package controller

import (
	"context"
	"fmt"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's election Lease (docs/designs/gateway-replicas.md, "Operator
// changes"). The gateway campaigns for it when A2A_GATEWAY_LEASE names it,
// with its pod name as the identity (a2a/gateway/election.go). RBAC can't
// scope create to a name, so the operator creates the Lease and the gateway's
// Role gets only get and update on that one object; because the gateway can't
// recreate it, the operator watches it and recreates it after a deletion.
const (
	// a2aGatewayLeaseEnvVar names the Lease to the gateway, and
	// a2aGatewayPodNameEnvVar carries its identity there, the pod's name.
	a2aGatewayLeaseEnvVar   = "A2A_GATEWAY_LEASE"
	a2aGatewayPodNameEnvVar = "POD_NAME"

	// a2aGatewayLeaseLabel marks the gateway's Lease, and is what the
	// manager's cache selects Leases by (ManagerCacheOptions): every node
	// in a cluster renews a Lease of its own every ten seconds, and an
	// unscoped Owns() would cache and watch all of them.
	a2aGatewayLeaseLabel = "kubeagents.x-k8s.io/a2a-gateway-lease"

	// a2aGatewayLeaseRecreateHold is how long the operator waits after it
	// sees the Lease gone before creating it again: the gateway's lease
	// duration. A Lease recreated with no holder can be taken on a
	// standby's next retry, while the old leader keeps acting until its
	// renewal gives up, so the wait lets it step down first.
	a2aGatewayLeaseRecreateHold = 20 * time.Second
)

// a2aGatewayLeaseName is the Lease's fixed name, the gateway's own: the eval
// harness, the rollback script and hack/gateway-port-forward.sh read the
// holder from <agent>-a2a-gateway.
func a2aGatewayLeaseName(agent *agentv1alpha1.PlatformAgent) string { return a2aGatewayName(agent) }

// buildA2AGatewayLease is the Lease as the operator creates it: a name, the
// labels and nothing in its spec, which is the gateway's to write.
func buildA2AGatewayLease(agent *agentv1alpha1.PlatformAgent) *coordinationv1.Lease {
	labels := a2aLabels(agent, "gateway")
	labels[a2aGatewayLeaseLabel] = "true"
	return &coordinationv1.Lease{
		TypeMeta:   metav1.TypeMeta{APIVersion: "coordination.k8s.io/v1", Kind: "Lease"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aGatewayLeaseName(agent), Namespace: agent.Namespace, Labels: labels},
	}
}

// a2aGatewayLeaseEnv is the gateway container's election env: the Lease's
// name, and the pod's name from the downward API.
func a2aGatewayLeaseEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: a2aGatewayLeaseEnvVar, Value: a2aGatewayLeaseName(agent)},
		{Name: a2aGatewayPodNameEnvVar, ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
			APIVersion: "v1", FieldPath: "metadata.name",
		}}},
	}
}

// ensureA2AGatewayLease creates the gateway's Lease when it is missing, and
// returns how long to wait before the next pass when it is holding a
// recreate back. It never updates a Lease that exists: its spec is the
// gateway's, written by whichever replica holds it.
//
// A missing Lease this process has seen before was deleted, by hand or by a
// restore, and is recreated only once a2aGatewayLeaseRecreateHold has passed
// since this process first saw it gone. One it has never seen is created at
// once, which is the first install, and also a deletion that happened while
// the operator was restarting, which it can't tell apart.
func (r *PlatformAgentReconciler) ensureA2AGatewayLease(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (time.Duration, error) {
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}
	live := &coordinationv1.Lease{}
	err := r.Get(ctx, key, live)
	if err == nil {
		r.gatewayLeaseSeen.Store(key, true)
		r.gatewayLeaseGoneAt.Delete(key)
		return 0, nil
	}
	if !errors.IsNotFound(err) {
		return 0, fmt.Errorf("failed to read the A2A gateway Lease %s: %w", key, err)
	}
	if _, seen := r.gatewayLeaseSeen.Load(key); seen {
		goneAt, _ := r.gatewayLeaseGoneAt.LoadOrStore(key, time.Now())
		hold := r.gatewayLeaseHold
		if hold <= 0 {
			hold = a2aGatewayLeaseRecreateHold
		}
		if wait := hold - time.Since(goneAt.(time.Time)); wait > 0 {
			logf.FromContext(ctx).Info("the A2A gateway Lease is gone; holding its recreate back so the last leader steps down first",
				"lease", key.String(), "wait", wait.String())
			return wait, nil
		}
	}
	lease := buildA2AGatewayLease(agent)
	if err := ctrl.SetControllerReference(agent, lease, r.Scheme); err != nil {
		return 0, err
	}
	withCommonLabels(lease, agent)
	if err := r.Create(ctx, lease); err != nil && !errors.IsAlreadyExists(err) {
		return 0, fmt.Errorf("failed to create the A2A gateway Lease %s: %w", key, err)
	}
	r.gatewayLeaseSeen.Store(key, true)
	r.gatewayLeaseGoneAt.Delete(key)
	return 0, nil
}

// forgetA2AGatewayLease drops what this process remembers about an agent's
// Lease, when the agent is deleted.
func (r *PlatformAgentReconciler) forgetA2AGatewayLease(agent *agentv1alpha1.PlatformAgent) {
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}
	r.gatewayLeaseSeen.Delete(key)
	r.gatewayLeaseGoneAt.Delete(key)
}
