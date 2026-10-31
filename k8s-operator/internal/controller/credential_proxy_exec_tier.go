/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"path"
	"strconv"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The exec tier (docs/designs/credential-broker-exec-tier.md §3.3, names in
// §12): stateless broker replicas beside the stateful broker, serving /v1/exec
// and /v1/gcp/ only. Every name the tier renders is derived here.
const (
	credentialProxyExecTierSuffix         = "-credential-proxy-exec-tier"          // #nosec G101 -- Resource name suffix, not a credential
	credentialProxyExecTierComponent      = "credential-proxy-exec-tier"           // #nosec G101 -- Label value, not a credential
	credentialProxyExecTierReplicasSuffix = "-credential-proxy-exec-tier-replicas" // #nosec G101 -- Resource name suffix, not a credential
	// The ConfigMap's key and where its directory is mounted. A directory
	// mount, not a SubPath one: the kubelet refreshes a directory mount when
	// the ConfigMap changes, so a scale change reaches the running pods
	// without a new pod template (§3.5).
	credentialProxyExecTierReplicasKey       = "replicas"                            // #nosec G101 -- ConfigMap key, not a credential
	credentialProxyExecTierReplicasMountPath = "/etc/credential-proxy-exec-tier"     // #nosec G101 -- Mount path, not a credential
	credentialProxyExecTierReplicasVolume    = "credential-proxy-exec-tier-replicas" // #nosec G101 -- Volume name, not a credential
	credentialProxyExecTierReplicasFileEnv   = "CREDENTIAL_PROXY_EXEC_REPLICAS_FILE" // #nosec G101 -- Environment variable name, not a credential
	credentialProxyRoleEnv                   = "CREDENTIAL_PROXY_ROLE"               // #nosec G101 -- Environment variable name, not a credential
	credentialProxyExecRole                  = "exec"
	// Requests equal limits on every cluster mode (§3.3): the 2Gi request is
	// the floor of §4.2's admission rule, and the CPU request reserves the two
	// cores §8.4 plans with. There is no override field.
	credentialProxyExecTierCPU    = "2"
	credentialProxyExecTierMemory = "2Gi"
	// A PodDisruptionBudget only from two replicas: at one, maxUnavailable 1
	// guards nothing, and the tier being off means callers use the stateful
	// broker. The same maxUnavailable bounds the Deployment's rollout.
	credentialProxyExecTierPDBMinReplicas = 2
	credentialProxyExecTierMaxUnavailable = 1
	// The broker container's request port name, which the Service targets.
	credentialProxyExecTierPortName = "cred-proxy"
	labelAppKey                     = "app"
	labelComponentKey               = "kubeagents.x-k8s.io/component"
	// The tier's Service name and its `app` label value are the agent's name
	// and the suffix, and both are capped at 63 characters, so with the tier
	// on the agent's name is capped at 63 minus the suffix.
	credentialProxyExecTierMaxAgentName       = validation.DNS1035LabelMaxLength - len(credentialProxyExecTierSuffix)
	credentialProxyExecTierNameRefusalFmt     = "the agent's name %q is %d characters; with spec.deployment.credentialProxy.exec.replicas above 0 it must be at most %d, so <name>%s fits the 63-character Service name and label value. Rename the agent, or set exec.replicas to 0"
	conditionReasonInvalidCredentialProxyExec = "InvalidCredentialProxyExec"
)

// credentialProxyExecTierEnvDropped names the broker env the exec tier does
// not carry: the routes that read it are refused on an exec pod (§12), and
// the relays and the content workspace are not started there.
var credentialProxyExecTierEnvDropped = map[string]bool{
	"TOKEN_BROKER_URL":                   true,
	"GOOGLE_CHAT_PROJECT_ID":             true,
	"GOOGLE_CHAT_SUBSCRIPTION_NAME":      true,
	"CREDENTIAL_PROXY_CONTENT_WORKSPACE": true,
}

func credentialProxyExecTierName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + credentialProxyExecTierSuffix
}

func credentialProxyExecTierReplicasName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + credentialProxyExecTierReplicasSuffix
}

// credentialProxyExecTierSelector is the tier's own selector. It does not
// match credentialProxySelector, so the stateful broker's Service does not
// send chat and workspace calls to an exec pod, and the reverse.
func credentialProxyExecTierSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		labelAppKey:       credentialProxyExecTierName(agent),
		labelComponentKey: credentialProxyExecTierComponent,
	}
}

// credentialProxyExecTierReplicas is spec.deployment.credentialProxy.exec.replicas,
// with unset as 0.
func credentialProxyExecTierReplicas(agent *agentv1alpha1.PlatformAgent) int32 {
	d := agent.Spec.Deployment
	if d == nil || d.CredentialProxy == nil || d.CredentialProxy.Exec == nil || d.CredentialProxy.Exec.Replicas == nil {
		return 0
	}
	return *d.CredentialProxy.Exec.Replicas
}

// ValidateCredentialProxyExec refuses an agent name too long for the tier's
// objects while the tier is on. The admission webhook calls it at apply, and
// the reconciler before rendering the tier, because the chart installs with
// the webhook off.
func ValidateCredentialProxyExec(agent *agentv1alpha1.PlatformAgent, path *field.Path) field.ErrorList {
	if credentialProxyExecTierReplicas(agent) <= 0 || len(agent.Name) <= credentialProxyExecTierMaxAgentName {
		return nil
	}
	return field.ErrorList{field.Invalid(path, agent.Name, fmt.Sprintf(credentialProxyExecTierNameRefusalFmt,
		agent.Name, len(agent.Name), credentialProxyExecTierMaxAgentName, credentialProxyExecTierSuffix))}
}

func credentialProxyExecTierResources() corev1.ResourceRequirements {
	list := corev1.ResourceList{
		corev1.ResourceCPU:    resource.MustParse(credentialProxyExecTierCPU),
		corev1.ResourceMemory: resource.MustParse(credentialProxyExecTierMemory),
	}
	return corev1.ResourceRequirements{Requests: list, Limits: list.DeepCopy()}
}

// buildCredentialProxyExecTierDeployment derives the tier's Deployment from the
// stateful broker's, as rendered for this reconcile, so the image, the policy,
// the caller authentication, the profile identities and the drain stay the
// same on both. What differs:
//   - its own name, selector and labels, without has-credential-proxy, which
//     opens the github-token-minter to a pod;
//   - the replica count and RollingUpdate with maxUnavailable 1: no exec call
//     keeps state across calls, a second replica serves while one rolls, and
//     a rollout needs no surge Pod, so a namespace quota with no headroom
//     cannot stall it (at one replica the callers fall back to the stateful
//     broker for the gap);
//   - CREDENTIAL_PROXY_ROLE=exec, and the replica count mounted from the
//     ConfigMap;
//   - fixed resources;
//   - no Secret: no Secret-backed volume (forge credentials and CA bundles
//     included) and no env read from one (chat, Teams and plugin credentials),
//     dropped by source rather than by name so a Secret the broker gains later
//     does not reach N exec pods; and no forge, relay or workspace settings.
func buildCredentialProxyExecTierDeployment(agent *agentv1alpha1.PlatformAgent, broker *appsv1.Deployment) *appsv1.Deployment {
	dep := broker.DeepCopy()
	name := credentialProxyExecTierName(agent)
	selector := credentialProxyExecTierSelector(agent)
	dep.ObjectMeta = metav1.ObjectMeta{Name: name, Namespace: agent.Namespace}
	withCommonLabels(dep, agent)
	dep.Labels[labelAppKey] = name
	dep.Spec.Replicas = ptr.To(credentialProxyExecTierReplicas(agent))
	dep.Spec.Strategy = appsv1.DeploymentStrategy{
		Type:          appsv1.RollingUpdateDeploymentStrategyType,
		RollingUpdate: &appsv1.RollingUpdateDeployment{MaxUnavailable: ptr.To(intstr.FromInt32(credentialProxyExecTierMaxUnavailable))},
	}
	dep.Spec.Selector = &metav1.LabelSelector{MatchLabels: selector}

	podLabels := commonLabels(agent)
	for k, v := range selector {
		podLabels[k] = v
	}
	dep.Spec.Template.Labels = podLabels
	delete(dep.Spec.Template.Annotations, secretEnvHashAnnotation)

	dropped := map[string]bool{}
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if volumeHoldsSecret(v) {
			dropped[v.Name] = true
		}
	}
	forgeEnv := map[string]bool{}
	for _, e := range buildVCSForgesEnv(agent) {
		forgeEnv[e.Name] = true
	}
	// By path, because the forge configuration is a SubPath of the policy
	// volume, which the exec pod keeps.
	forgeMounts := map[string]bool{}
	for _, m := range buildVCSForgesVolumeMounts(agent) {
		forgeMounts[m.MountPath] = true
	}

	spec := &dep.Spec.Template.Spec
	volumes := spec.Volumes[:0]
	for _, v := range spec.Volumes {
		if !dropped[v.Name] {
			volumes = append(volumes, v)
		}
	}
	spec.Volumes = append(volumes, corev1.Volume{
		Name: credentialProxyExecTierReplicasVolume,
		VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: credentialProxyExecTierReplicasName(agent)},
		}},
	})

	for i := range spec.Containers {
		c := &spec.Containers[i]
		if c.Name != credentialProxyContainerName {
			continue
		}
		env := c.Env[:0]
		for _, e := range c.Env {
			if forgeEnv[e.Name] || credentialProxyExecTierEnvDropped[e.Name] || e.Name == credentialProxyRoleEnv {
				continue
			}
			if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
				continue
			}
			env = append(env, e)
		}
		c.Env = append(env,
			corev1.EnvVar{Name: credentialProxyRoleEnv, Value: credentialProxyExecRole},
			corev1.EnvVar{Name: credentialProxyExecTierReplicasFileEnv, Value: path.Join(credentialProxyExecTierReplicasMountPath, credentialProxyExecTierReplicasKey)},
		)
		envFrom := c.EnvFrom[:0]
		for _, src := range c.EnvFrom {
			if src.SecretRef == nil {
				envFrom = append(envFrom, src)
			}
		}
		c.EnvFrom = envFrom
		mounts := c.VolumeMounts[:0]
		for _, m := range c.VolumeMounts {
			if !dropped[m.Name] && !forgeMounts[m.MountPath] {
				mounts = append(mounts, m)
			}
		}
		c.VolumeMounts = append(mounts, corev1.VolumeMount{
			Name: credentialProxyExecTierReplicasVolume, MountPath: credentialProxyExecTierReplicasMountPath, ReadOnly: true,
		})
		c.Resources = credentialProxyExecTierResources()
	}
	return dep
}

func buildCredentialProxyExecTierService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	svc := &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierName(agent), Namespace: agent.Namespace},
		Spec: corev1.ServiceSpec{
			Selector: credentialProxyExecTierSelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       credentialProxyExecTierPortName,
				Port:       credentialProxyPort,
				TargetPort: intstr.FromString(credentialProxyExecTierPortName),
			}},
		},
	}
	withCommonLabels(svc, agent)
	return svc
}

// volumeHoldsSecret reports whether a volume's content comes from a Secret,
// directly or as a projected source.
func volumeHoldsSecret(v corev1.Volume) bool {
	if v.Secret != nil {
		return true
	}
	if v.Projected != nil {
		for _, src := range v.Projected.Sources {
			if src.Secret != nil {
				return true
			}
		}
	}
	return false
}

// buildCredentialProxyExecTierReplicasConfigMap holds the replica count the
// exec pods read on each admission request (§3.5).
func buildCredentialProxyExecTierReplicasConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	cm := &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierReplicasName(agent), Namespace: agent.Namespace},
		Data: map[string]string{
			credentialProxyExecTierReplicasKey: strconv.Itoa(int(credentialProxyExecTierReplicas(agent))),
		},
	}
	withCommonLabels(cm, agent)
	return cm
}

// buildCredentialProxyExecTierPDB guards voluntary evictions such as a node
// drain at two replicas or more; a rollout is RollingUpdate's job. AlwaysAllow,
// because a replica that is not Ready serves nothing, so holding its eviction
// only holds the drain.
func buildCredentialProxyExecTierPDB(agent *agentv1alpha1.PlatformAgent) *policyv1.PodDisruptionBudget {
	alwaysAllow := policyv1.AlwaysAllow
	pdb := &policyv1.PodDisruptionBudget{
		TypeMeta:   metav1.TypeMeta{APIVersion: "policy/v1", Kind: "PodDisruptionBudget"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierName(agent), Namespace: agent.Namespace},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MaxUnavailable:             ptr.To(intstr.FromInt32(credentialProxyExecTierMaxUnavailable)),
			Selector:                   &metav1.LabelSelector{MatchLabels: credentialProxyExecTierSelector(agent)},
			UnhealthyPodEvictionPolicy: &alwaysAllow,
		},
	}
	withCommonLabels(pdb, agent)
	return pdb
}

// buildCredentialProxyExecTierNetworkPolicy admits the tier's callers (§3.3):
// the sandbox, the session pods under the cluster-view flag, and the profile
// namespace while there is a profile caller, on the credentialed port; and the
// metrics port to Managed Prometheus and, where it is set, the operator. No
// gateway and no agent pod: neither calls /v1/exec.
func buildCredentialProxyExecTierNetworkPolicy(agent *agentv1alpha1.PlatformAgent, operatorNamespace string, profiles credentialProxyProfileConfig) *networkingv1.NetworkPolicy {
	tcp := corev1.ProtocolTCP
	callers := []networkingv1.NetworkPolicyPeer{
		{PodSelector: &metav1.LabelSelector{MatchLabels: shellSandboxSelector(agent)}},
	}
	if a2aSessionClusterViewEnabled(agent) {
		callers = append(callers, networkingv1.NetworkPolicyPeer{PodSelector: &metav1.LabelSelector{MatchLabels: a2aSessionPodSelector()}})
	}
	if profiles.enabled() {
		callers = append(callers, credentialProxyProfilePeer(agent))
	}
	np := &networkingv1.NetworkPolicy{
		TypeMeta:   metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: credentialProxyExecTierSelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{
				{
					From: callers,
					Ports: []networkingv1.NetworkPolicyPort{{
						Protocol: &tcp,
						Port:     ptr.To(intstr.FromInt32(credentialProxyPort)),
					}},
				},
				{
					From: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{
							MatchLabels: map[string]string{labelMetadataName: gmpNamespace},
						},
					}},
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyMetricsPort)},
				},
			},
		},
	}
	if rule, ok := operatorMetricsIngressRule(operatorNamespace, credentialProxyMetricsPort); ok {
		np.Spec.Ingress = append(np.Spec.Ingress, rule)
	}
	withCommonLabels(np, agent)
	return np
}

// reconcileCredentialProxyExecTier applies the tier at a positive replica
// count and removes it at 0. broker is the stateful broker's Deployment as
// this reconcile rendered it.
func (r *PlatformAgentReconciler) reconcileCredentialProxyExecTier(ctx context.Context, agent *agentv1alpha1.PlatformAgent, broker *appsv1.Deployment, profiles credentialProxyProfileConfig) error {
	replicas := credentialProxyExecTierReplicas(agent)
	if errs := ValidateCredentialProxyExec(agent, nil); len(errs) > 0 {
		// Refused: rendered as off, and said so. The objects could not be
		// created under a name past 63 characters anyway.
		logf.FromContext(ctx).Info("refusing spec.deployment.credentialProxy.exec.replicas; the exec tier is not rendered",
			"name", agent.Name, "namespace", agent.Namespace, "refusal", errs[0].Detail)
		r.recordEvent(agent, corev1.EventTypeWarning, conditionReasonInvalidCredentialProxyExec, errs[0].Detail)
		replicas = 0
	}
	if replicas <= 0 {
		return r.removeCredentialProxyExecTier(ctx, agent)
	}
	pdb := buildCredentialProxyExecTierPDB(agent)
	objs := []client.Object{
		buildCredentialProxyExecTierReplicasConfigMap(agent),
		buildCredentialProxyExecTierService(agent),
		buildCredentialProxyExecTierNetworkPolicy(agent, r.OperatorNamespace, profiles),
		buildCredentialProxyExecTierDeployment(agent, broker),
	}
	if replicas >= credentialProxyExecTierPDBMinReplicas {
		objs = append(objs, pdb)
	} else if err := r.deleteA2AObjectIfOwned(ctx, agent, pdb, r.Client, nil); err != nil {
		return fmt.Errorf("failed to delete credential proxy exec tier PodDisruptionBudget: %w", err)
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return fmt.Errorf("failed to set controller reference on credential proxy exec tier %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
		if p, ok := obj.(*policyv1.PodDisruptionBudget); ok {
			if err := r.clearForeignPDBBudgetField(ctx, p); err != nil {
				return err
			}
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply credential proxy exec tier %T %s/%s: %w", obj, obj.GetNamespace(), obj.GetName(), err)
		}
	}
	return nil
}

// removeCredentialProxyExecTier deletes the tier's objects the agent owns,
// the workload first and the NetworkPolicy last, so no exec pod is left
// running without its fence.
func (r *PlatformAgentReconciler) removeCredentialProxyExecTier(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	ns := agent.Namespace
	name := credentialProxyExecTierName(agent)
	objs := []client.Object{
		&policyv1.PodDisruptionBudget{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}},
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}},
		&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: credentialProxyExecTierReplicasName(agent), Namespace: ns}},
	}
	for _, obj := range objs {
		if err := r.deleteA2AObjectIfOwned(ctx, agent, obj, r.Client, nil); err != nil {
			return fmt.Errorf("failed to delete credential proxy exec tier %T %s/%s: %w", obj, ns, obj.GetName(), err)
		}
	}
	np := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}}
	return r.deleteA2AObjectIfOwned(ctx, agent, np, r.Client, func() (bool, error) {
		return r.credentialProxyExecTierPodsGone(ctx, agent)
	})
}

// credentialProxyExecTierPodsGone holds the fence's delete until no exec pod
// is left.
func (r *PlatformAgentReconciler) credentialProxyExecTierPodsGone(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(credentialProxyExecTierSelector(agent))); err != nil {
		return false, err
	}
	return len(pods.Items) == 0, nil
}
