package controller

import (
	"context"
	"fmt"
	"os"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's election Lease (docs/designs/gateway-replicas.md, "Operator
// changes"). The gateway campaigns for it when A2A_GATEWAY_LEASE names it,
// with its pod name as the identity (a2a/gateway/election.go). RBAC can't
// scope create to a name, so the operator creates the Lease and the gateway's
// Role gets only get and update on that one object; because the gateway can't
// recreate it, the operator watches it and recreates it after a deletion.
const (
	// a2aGatewayElectionEnvVar, set to "true" in the operator's own
	// environment, switches the election on: the Lease, the gateway's grant
	// on it and the gateway's election env. Off by default. With the
	// election on, the gateway needs the API server to keep leading, so a
	// control-plane outage longer than its renew deadline (a zonal
	// cluster's control-plane upgrade) stops it until the API server is
	// back; the default flips once that trade-off is decided. CI and the
	// live runs turn it on.
	a2aGatewayElectionEnvVar = "A2A_GATEWAY_ELECTION"

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
	// duration, electionLeaseDuration in a2a/gateway/election.go, which
	// changes with it. A Lease recreated with no holder can be taken on a
	// standby's next retry, while the old leader keeps acting until its
	// renewal gives up, so the wait lets it step down first.
	a2aGatewayLeaseRecreateHold = 20 * time.Second
)

// a2aGatewayLeaseEvents lets only a Lease's deletion wake a reconcile. The
// gateway renews its Lease every two seconds, each renewal an update, and a
// reconcile per renewal would re-apply every object of every next install
// that often. Nothing in an update concerns the operator: it never writes
// the spec, and a Lease that loses its label is a delete to the
// label-scoped informer.
var a2aGatewayLeaseEvents = predicate.Funcs{
	CreateFunc:  func(event.CreateEvent) bool { return false },
	UpdateFunc:  func(event.UpdateEvent) bool { return false },
	DeleteFunc:  func(event.DeleteEvent) bool { return true },
	GenericFunc: func(event.GenericEvent) bool { return false },
}

// a2aGatewayElectionEnabled reports whether the gateway's election is on:
// by default, and off only for an explicit "false". An elected gateway
// steps down while the API server is unreachable
// (docs/designs/gateway-replicas.md, "What it costs"); "false" is the way
// back to one unelected replica.
func a2aGatewayElectionEnabled() bool {
	return os.Getenv(a2aGatewayElectionEnvVar) != "false"
}

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
// name, and the pod's name from the downward API. Nothing when the election
// is off, and the gateway runs unelected.
func a2aGatewayLeaseEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if !a2aGatewayElectionEnabled() {
		return nil
	}
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
	err = r.Create(ctx, lease)
	if errors.IsAlreadyExists(err) {
		// The cache, which sees only labelled Leases, said missing and the
		// API server says it exists: a Lease under this name without the
		// label (made by hand, or left from before it). Ours is relabelled
		// so the cache and the delete watch see it again; anyone else's is
		// left alone and the gateway campaigns on it as it is.
		return 0, r.adoptA2AGatewayLease(ctx, agent, key)
	}
	if err != nil {
		return 0, fmt.Errorf("failed to create the A2A gateway Lease %s: %w", key, err)
	}
	r.gatewayLeaseSeen.Store(key, true)
	r.gatewayLeaseGoneAt.Delete(key)
	return 0, nil
}

// adoptA2AGatewayLease relabels a Lease under the gateway's name that the
// cache can't see, when this PlatformAgent controls it. It is read live,
// since the cache holds only labelled Leases. One controlled by something
// else is reported and left as it is, and not counted as seen, so it never
// starts a held-back recreate.
func (r *PlatformAgentReconciler) adoptA2AGatewayLease(ctx context.Context, agent *agentv1alpha1.PlatformAgent, key types.NamespacedName) error {
	reader := client.Reader(r.Client)
	if r.APIReader != nil {
		reader = r.APIReader
	}
	live := &coordinationv1.Lease{}
	if err := reader.Get(ctx, key, live); err != nil {
		return fmt.Errorf("failed to read the A2A gateway Lease %s: %w", key, err)
	}
	owner := metav1.GetControllerOf(live)
	if owner == nil || owner.UID != agent.UID {
		logf.FromContext(ctx).Info("a Lease under the A2A gateway's name is not this PlatformAgent's; leaving it as it is",
			"lease", key.String())
		return nil
	}
	patch := client.MergeFrom(live.DeepCopy())
	if live.Labels == nil {
		live.Labels = map[string]string{}
	}
	live.Labels[a2aGatewayLeaseLabel] = "true"
	if err := r.Patch(ctx, live, patch); err != nil {
		return fmt.Errorf("failed to relabel the A2A gateway Lease %s: %w", key, err)
	}
	r.gatewayLeaseSeen.Store(key, true)
	r.gatewayLeaseGoneAt.Delete(key)
	return nil
}

// forgetA2AGatewayLease drops what this process remembers about an agent's
// Lease, when the agent is deleted.
func (r *PlatformAgentReconciler) forgetA2AGatewayLease(agent *agentv1alpha1.PlatformAgent) {
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}
	r.gatewayLeaseSeen.Delete(key)
	r.gatewayLeaseGoneAt.Delete(key)
}
