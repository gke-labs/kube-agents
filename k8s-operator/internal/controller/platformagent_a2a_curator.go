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
	"os"
	"strconv"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The OKF memory curator (a2a/cmd/curator): a standalone, credential-free pod
// that ingests learning outcomes from NATS, indexes OKF records into Hindsight,
// publishes the active blueprint digest, and answers semantic memory queries.
// The operator renders this workload only when A2A_CURATOR is "true".
const (
	// a2aCuratorEnvVar enables the curator pod under mode next when set to "true".
	a2aCuratorEnvVar = "A2A_CURATOR"

	a2aCuratorComponent       = "curator"
	a2aCuratorContainer       = "curator"
	a2aCuratorNameSuffix      = "-curator"
	a2aCuratorNetpolSuffix    = "-curator-netpol"
	a2aCuratorNetpolComponent = a2aCuratorComponent + "-netpol"

	// The curator binary ships in the console image alongside /console and /mcp.
	a2aCuratorCommand = "/curator"

	a2aCuratorStatusPort = 8080
	a2aCuratorPortName   = "status"
	a2aCuratorHealthPath = "/healthz"
	a2aCuratorReadyPath  = "/readyz"

	a2aCuratorStateVolume    = "curator-state"
	a2aCuratorStateMountPath = "/var/lib/curator"
	a2aCuratorRepoDir        = "/var/lib/curator/knowledge-store"

	a2aCuratorHindsightPort = 8888

	a2aCuratorEnvGoMemLimit     = "GOMEMLIMIT"
	a2aCuratorGoMemLimitPercent = 85

	a2aCuratorReadinessPeriod  = 5
	a2aCuratorReadinessFailure = 3
	a2aCuratorLivenessPeriod   = 10
	a2aCuratorLivenessFailure  = 6
	a2aCuratorStartupPeriod    = 5
	a2aCuratorStartupFailure   = 60

	a2aCuratorCPURequest    = "50m"
	a2aCuratorCPULimit      = "500m"
	a2aCuratorMemoryRequest = "64Mi"
	a2aCuratorMemoryLimit   = "256Mi"

	a2aCuratorFenceIndex = 2
)

// a2aCuratorGoMemLimit returns 85 percent of the curator memory limit in bytes.
func a2aCuratorGoMemLimit() string {
	limit := resource.MustParse(a2aCuratorMemoryLimit)
	return strconv.FormatInt(limit.Value()*a2aCuratorGoMemLimitPercent/100, 10)
}

// a2aCuratorEnabled reports whether the operator renders the curator pod.
func a2aCuratorEnabled() bool {
	return os.Getenv(a2aCuratorEnvVar) == "true"
}

func a2aCuratorName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aCuratorNameSuffix
}

func a2aCuratorNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + a2aCuratorNetpolSuffix
}

func a2aCuratorPodSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{
		"app":                             a2aCuratorName(agent),
		"kubeagents.x-k8s.io/component": a2aCuratorComponent,
	}
}

// a2aCuratorNATSPeers returns the curator pod peer for port 4222 on the NATS
// NetworkPolicy when A2A_CURATOR is enabled.
func a2aCuratorNATSPeers(agent *agentv1alpha1.PlatformAgent) []networkingv1.NetworkPolicyPeer {
	if !a2aCuratorEnabled() {
		return nil
	}
	return []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{
		MatchLabels: a2aCuratorPodSelector(agent),
	}}}
}

// curatorIdentity returns the NATS auth-callout entry for the curator pod.
// It holds no task-plane grants and no JetStream consumer-create verbs.
func curatorIdentity(agent *agentv1alpha1.PlatformAgent, ns string) a2aIdentity {
	publish := []string{
		"a2a.topics.shared.annotations",
		"a2a.topics.shared.blueprint",
	}
	publish = append(publish, a2aAgentJetStreamGrants()...)
	publish = append(publish,
		"_INBOX."+a2aAgentBusUser+".>",
		"_INBOX."+a2aCuratorUser+".>",
	)
	return a2aIdentity{
		user:           a2aCuratorUser,
		account:        a2aAccountApp,
		comment:        "the OKF memory curator: indexes learning outcomes into Hindsight, publishes blueprint digests, and answers memory queries with no cluster or forge credentials",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(ns, a2aCuratorName(agent)),
		publish:        publish,
		subscribe: []string{
			"a2a.topics.shared.annotations",
			"a2a.topics.shared.blueprint",
			"_INBOX." + a2aAgentBusUser + ".memory.query",
			"_INBOX." + a2aAgentBusUser + ".curator.outcomes",
			"_INBOX." + a2aCuratorUser + ".>",
		},
	}
}

// buildA2ACuratorServiceAccount builds the dedicated ServiceAccount for the
// curator pod. No Role or ClusterRole binds to this ServiceAccount.
func buildA2ACuratorServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aCuratorName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aCuratorComponent),
		},
	}
}

// buildA2ACuratorDeployment builds the credential-free curator Deployment.
func buildA2ACuratorDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aCuratorName(agent)
	selector := a2aCuratorPodSelector(agent)
	podLabels := a2aLabels(agent, a2aCuratorComponent)
	for k, v := range selector {
		podLabels[k] = v
	}

	probe := func(path string, period, failure int32) *corev1.Probe {
		return &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
				Path: path, Port: intstr.FromInt32(a2aCuratorStatusPort),
			}},
			PeriodSeconds:    period,
			FailureThreshold: failure,
		}
	}

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, a2aCuratorComponent)},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					ServiceAccountName:           name,
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(a2aConsoleRunAsUser),
						FSGroup:        ptr.To(a2aConsoleFSGroup),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: []corev1.Volume{
						a2aBusTokenVolumeSource(),
						a2aNATSCABundleVolume(agent),
						{
							Name:         a2aCuratorStateVolume,
							VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
						},
					},
					Containers: []corev1.Container{{
						Name:       a2aCuratorContainer,
						Image:      a2aConsoleImage(),
						Command:    []string{a2aCuratorCommand, "daemon", "--repo", a2aCuratorRepoDir},
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							a2aNATSCAFileEnvVar(),
							{Name: a2aBusUserEnv, Value: a2aCuratorUser},
							{Name: "CURATOR_STATUS_LISTEN", Value: fmt.Sprintf(":%d", a2aCuratorStatusPort)},
							{Name: "CURATOR_REPO_DIR", Value: a2aCuratorRepoDir},
							{Name: "HINDSIGHT_API_URL", Value: fmt.Sprintf("http://hindsight-api.%s.svc.cluster.local:%d", agent.Namespace, a2aCuratorHindsightPort)},
							{Name: "LITELLM_BASE_URL", Value: fmt.Sprintf("http://inference-gateway.%s.svc.cluster.local/v1", agent.Namespace)},
							{Name: a2aCuratorEnvGoMemLimit, Value: a2aCuratorGoMemLimit()},
						},
						Ports: []corev1.ContainerPort{{Name: a2aCuratorPortName, ContainerPort: a2aCuratorStatusPort}},
						VolumeMounts: []corev1.VolumeMount{
							a2aBusTokenVolumeMount(),
							a2aNATSCABundleMount(),
							{Name: a2aCuratorStateVolume, MountPath: a2aCuratorStateMountPath},
						},
						StartupProbe:   probe(a2aCuratorHealthPath, a2aCuratorStartupPeriod, a2aCuratorStartupFailure),
						ReadinessProbe: probe(a2aCuratorReadyPath, a2aCuratorReadinessPeriod, a2aCuratorReadinessFailure),
						LivenessProbe:  probe(a2aCuratorHealthPath, a2aCuratorLivenessPeriod, a2aCuratorLivenessFailure),
						Resources: a2aResources(
							a2aCuratorCPURequest,
							a2aCuratorMemoryRequest,
							a2aCuratorCPULimit,
							a2aCuratorMemoryLimit,
						),
						SecurityContext: hardenedSecurityContext(),
					}},
				},
			},
		},
	}
}

// buildA2ACuratorNetworkPolicy denies all ingress to the curator pod and limits
// egress to cluster DNS, NATS, LiteLLM, and hindsight-api.
func buildA2ACuratorNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) *networkingv1.NetworkPolicy {
	dnsPeers := clusterDNSPeers(dnsClusterIPs)
	egress := []networkingv1.NetworkPolicyEgressRule{
		{
			Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
			To:    dnsPeers,
		},
		{
			Ports: []networkingv1.NetworkPolicyPort{tcpPort(a2aNATSClientPort)},
			To: []networkingv1.NetworkPolicyPeer{
				namespacedPodPeer(agent.Namespace, map[string]string{
					labelPartOf:       a2aPartOf,
					a2aComponentLabel: "nats",
				}),
			},
		},
		{
			Ports: []networkingv1.NetworkPolicyPort{
				tcpPort(a2aLiteLLMServicePort),
				tcpPort(a2aLiteLLMUpstreamPort),
				tcpPort(a2aLiteLLMContainerPort),
			},
			To: []networkingv1.NetworkPolicyPeer{
				namespacedPodPeer(agent.Namespace, map[string]string{"app": "litellm"}),
			},
		},
		{
			Ports: []networkingv1.NetworkPolicyPort{tcpPort(a2aCuratorHindsightPort)},
			To: []networkingv1.NetworkPolicyPeer{
				namespacedPodPeer(agent.Namespace, map[string]string{
					"app.kubernetes.io/name":      "hindsight",
					"app.kubernetes.io/component": "api",
				}),
			},
		},
	}

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aCuratorNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, a2aCuratorNetpolComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: a2aCuratorPodSelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Egress: egress,
		},
	}
}

// reconcileA2ACurator applies the curator ServiceAccount and Deployment when
// A2A_CURATOR is enabled, or removes them when the flag is off.
func (r *PlatformAgentReconciler) reconcileA2ACurator(ctx context.Context, agent *agentv1alpha1.PlatformAgent, busHeld bool) error {
	if !a2aCuratorEnabled() {
		return r.removeA2ACurator(ctx, agent)
	}
	sa := buildA2ACuratorServiceAccount(agent)
	dep := buildA2ACuratorDeployment(agent)
	objs := []client.Object{sa}
	if !busHeld {
		objs = append(objs, dep)
	} else if err := r.Get(ctx, client.ObjectKeyFromObject(dep), &appsv1.Deployment{}); errors.IsNotFound(err) {
		objs = append(objs, dep)
	} else if err != nil {
		return err
	}
	for _, obj := range objs {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply the curator's %T: %w", obj, err)
		}
	}
	return nil
}

// removeA2ACurator deletes owned curator resources when A2A_CURATOR is off,
// holding the NetworkPolicy until all curator pods terminate.
func (r *PlatformAgentReconciler) removeA2ACurator(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	noPodLeft := func() (bool, error) {
		pods := &corev1.PodList{}
		if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace),
			client.MatchingLabels(a2aCuratorPodSelector(agent))); err != nil {
			return false, fmt.Errorf("failed to list the curator's pods: %w", err)
		}
		return len(pods.Items) == 0, nil
	}
	for i, entry := range r.a2aCuratorObjects(agent) {
		var ready func() (bool, error)
		if i == a2aCuratorFenceIndex {
			ready = noPodLeft
		}
		if err := r.deleteA2AObjectIfOwned(ctx, agent, entry.obj, entry.reader, ready); err != nil {
			return fmt.Errorf("failed to remove the curator's %T: %w", entry.obj, err)
		}
	}
	return nil
}

// a2aCuratorObjects returns the curator Deployment, ServiceAccount, and fence.
func (r *PlatformAgentReconciler) a2aCuratorObjects(agent *agentv1alpha1.PlatformAgent) []a2aTeardownEntry {
	ns := agent.Namespace
	return []a2aTeardownEntry{
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: a2aCuratorName(agent), Namespace: ns}}, r.Client},
		{&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: a2aCuratorName(agent), Namespace: ns}}, r.Client},
		{&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: a2aCuratorNetpolName(agent), Namespace: ns}}, r.Client},
	}
}
