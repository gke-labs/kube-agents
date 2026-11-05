package controller

import (
	"context"
	"os"

	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The A2A gateway at more than one replica (docs/designs/gateway-replicas.md,
// "Operator changes"): one replica holds the Lease and does the work, the
// others stand by. The operator renders more than one only once it knows the
// gateway image runs the election, since a gateway from before it holds no
// Lease and would act beside an elected one.
const (
	// gatewayElectedConditionType records that a gateway pod holds the Lease,
	// and the image it runs, in the message.
	gatewayElectedConditionType = "GatewayElected"
	gatewayElectedReason        = "LeaseHeld"
	// a2aGatewayDefaultReplicas is the replica count under next when the CR
	// names none: the active replica and one standby.
	a2aGatewayDefaultReplicas = int32(2)
	// a2aGatewayPDBMaxUnavailable lets a drain take one replica at a time.
	a2aGatewayPDBMaxUnavailable = 1
	// a2aGatewaySpreadTopologyKey spreads the replicas over zones, so a zone
	// outage leaves a standby. ScheduleAnyway, so a one-zone install still
	// schedules both.
	a2aGatewaySpreadTopologyKey = "topology.kubernetes.io/zone"
	a2aGatewaySpreadMaxSkew     = 1
	// a2aGatewaySpreadMatchLabelKey counts only the current ReplicaSet's pods,
	// as the callout's spread does, so a rollout's surge pod isn't placed
	// against the pods it replaces.
	a2aGatewaySpreadMatchLabelKey = "pod-template-hash"
)

// a2aGatewayReplicas is the replica count the CR asks for under next:
// spec.deployment.availability.a2aGateway.replicas, or two.
func a2aGatewayReplicas(agent *agentv1alpha1.PlatformAgent) int32 {
	if d := agent.Spec.Deployment; d != nil && d.Availability != nil && d.Availability.A2AGateway != nil {
		if r := d.Availability.A2AGateway.Replicas; r != nil && *r >= 1 {
			return *r
		}
	}
	return a2aGatewayDefaultReplicas
}

// a2aGatewayImageFromRelease reports whether the gateway image comes from
// the operator's own release (a2aReleaseImage's second rung), which runs the
// election by construction: no A2A_GATEWAY_IMAGE pin, and an operator image
// that names a tag.
func a2aGatewayImageFromRelease() bool {
	if os.Getenv(a2aGatewayImageEnvVar) != "" {
		return false
	}
	op := os.Getenv(operatorImageEnvVar)
	return op != "" && imageRefHasTag(op)
}

// observeA2AGatewayElection reads which image the pod holding the gateway's
// Lease runs. It returns nil when it can't tell (no Lease, no holder, the
// holder's pod gone, a read that failed), so the caller keeps what the CR
// already records rather than dropping replicas on a transient miss.
func (r *PlatformAgentReconciler) observeA2AGatewayElection(ctx context.Context, agent *agentv1alpha1.PlatformAgent) *string {
	lease := &coordinationv1.Lease{}
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayLeaseName(agent)}, lease); err != nil {
		return nil
	}
	if lease.Spec.HolderIdentity == nil || *lease.Spec.HolderIdentity == "" {
		return nil
	}
	pod := &corev1.Pod{}
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: *lease.Spec.HolderIdentity}, pod); err != nil {
		logf.FromContext(ctx).V(1).Info("the A2A gateway Lease holder's pod could not be read", "pod", *lease.Spec.HolderIdentity, "err", err.Error())
		return nil
	}
	// Only a pod of this gateway's Deployment counts: a Lease taken by
	// something else proves nothing about the image the operator renders.
	if pod.Labels["app"] != a2aGatewayName(agent) {
		return nil
	}
	for _, c := range pod.Spec.Containers {
		if c.Name == a2aGatewayContainerName {
			return ptr.To(c.Image)
		}
	}
	return nil
}

// gatewayElectedImage is the image the GatewayElected condition should
// name, or "" for none. observed is what observeA2AGatewayElection read this
// pass (nil when it couldn't tell, or didn't run). The condition describes
// the gateway that's running: it's cleared under today, with the election
// off, and when the gateway rolls to an image outside the operator's release
// that no elected pod has run yet.
func gatewayElectedImage(agent *agentv1alpha1.PlatformAgent, observed *string) string {
	if !a2aStackRendering(agent) || !a2aGatewayElectionEnabled() {
		return ""
	}
	elected := ""
	if c := meta.FindStatusCondition(agent.Status.Conditions, gatewayElectedConditionType); c != nil && c.Status == metav1.ConditionTrue {
		elected = c.Message
	}
	if observed != nil {
		elected = *observed
	}
	if elected != "" && elected != a2aGatewayImage() && !a2aGatewayImageFromRelease() {
		return ""
	}
	return elected
}

func gatewayElectedConditionCurrent(agent *agentv1alpha1.PlatformAgent, image string) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, gatewayElectedConditionType)
	if image == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionTrue && existing.Message == image
}

func setGatewayElectedCondition(agent *agentv1alpha1.PlatformAgent, image string, now metav1.Time) {
	if image == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, gatewayElectedConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               gatewayElectedConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             gatewayElectedReason,
		Message:            image,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// scaleOutA2AGateway renders the CR's replica count and a rolling update
// once elected names a gateway that runs the election on the image rendered
// (gatewayElectedImage has already dropped any other). Until then the
// Deployment stays as buildA2AGatewayDeployment renders it: one replica,
// Recreate. Two or more roll with a surge pod, so the new pod comes up as a
// standby before an old one stops; one rolls by stopping the old pod first,
// for a namespace with no room for a surge.
func scaleOutA2AGateway(dep *appsv1.Deployment, agent *agentv1alpha1.PlatformAgent, elected string) {
	if elected == "" {
		return
	}
	replicas := a2aGatewayReplicas(agent)
	surge, unavailable := intstr.FromInt32(1), intstr.FromInt32(0)
	if replicas == 1 {
		surge, unavailable = intstr.FromInt32(0), intstr.FromInt32(1)
	}
	dep.Spec.Replicas = ptr.To(replicas)
	dep.Spec.Strategy = appsv1.DeploymentStrategy{
		Type:          appsv1.RollingUpdateDeploymentStrategyType,
		RollingUpdate: &appsv1.RollingUpdateDeployment{MaxSurge: &surge, MaxUnavailable: &unavailable},
	}
}

// a2aGatewayTopologySpread spreads the gateway's replicas over zones.
func a2aGatewayTopologySpread(agent *agentv1alpha1.PlatformAgent) []corev1.TopologySpreadConstraint {
	return []corev1.TopologySpreadConstraint{{
		MaxSkew:           a2aGatewaySpreadMaxSkew,
		TopologyKey:       a2aGatewaySpreadTopologyKey,
		WhenUnsatisfiable: corev1.ScheduleAnyway,
		LabelSelector:     &metav1.LabelSelector{MatchLabels: map[string]string{"app": a2aGatewayName(agent)}},
		MatchLabelKeys:    []string{a2aGatewaySpreadMatchLabelKey},
	}}
}

// buildA2AGatewayPDB lets a drain take one gateway replica at a time.
// AlwaysAllow, as the callout's budget has: a replica that isn't Ready
// (the bus down, a lease not yet read) never holds a drain.
func buildA2AGatewayPDB(agent *agentv1alpha1.PlatformAgent) *policyv1.PodDisruptionBudget {
	return &policyv1.PodDisruptionBudget{
		TypeMeta: metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aGatewayName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "gateway"),
		},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MaxUnavailable:             ptr.To(intstr.FromInt32(a2aGatewayPDBMaxUnavailable)),
			Selector:                   &metav1.LabelSelector{MatchLabels: map[string]string{"app": a2aGatewayName(agent)}},
			UnhealthyPodEvictionPolicy: ptr.To(policyv1.AlwaysAllow),
		},
	}
}
