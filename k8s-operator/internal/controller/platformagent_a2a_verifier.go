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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The capability verifier: the third workload on the A2A plane, and the only
// principal on the bus that may read the `cap` bucket.
//
// Why it is a workload of its own rather than a library every component links.
// The capability chain lives in KV, and the whole design (see
// docs/architecture/09-capability-envelope.md) rests on nobody but the verifier
// being able to read it: a broker that can read a capability can also copy one,
// and copying is indistinguishable from holding. That is a subject-permission
// property, and a subject permission attaches to a bus principal, so "only the
// verifier reads" is only true if the verifier is a principal — which means a
// ServiceAccount, which means a pod. Linking the walk into the gateway and the
// executor would give both of them the read, and there would be no boundary
// left to state.
//
// It answers requests on `a2a.cap.verify.<caller>` and replies on
// `a2a.cap.reply.<caller>.*`. The caller identity is the LAST subject token of
// the request, not anything in the payload, and the server is what makes that
// trustworthy: a principal may only publish on its own verify token. So this
// Deployment's grants and the callout's derived session grants are two halves
// of one mechanism, not two configurations that happen to agree.
//
// Availability, stated rather than implied: while no verifier is ready, every
// task is REFUSED. Not queued and not degraded — an executor that cannot reach
// a verifier fails closed, which is the correct direction and an expensive one.
// Hence two replicas in a queue group, a rollout that never drops to zero, and
// a readiness probe that reports the bus connection rather than the process
// being alive.

const (
	// The stage 1 dev registry, on the same terms as the gateway, the worker
	// and the auth callout: built and published here rather than by the
	// release pipeline, so deliberately absent from images.json and
	// overridable only by env var until the stack graduates (#1557). The
	// enumerations that have to name all four live in
	// platformagent_a2a_manifests.go, hack/check-image-inventory.sh and
	// docs/site/src/content/docs/deploy/docker-images.md.
	defaultA2AVerifierImage = "northamerica-northeast1-docker.pkg.dev/bnaylor-kagents-dev/a2a-demo/verifier:latest"
	a2aVerifierImageEnvVar  = "A2A_VERIFIER_IMAGE"

	// a2aVerifierStatusPort serves readiness and health. Same number as the
	// callout's, on a different pod.
	a2aVerifierStatusPort = 8080
)

func a2aVerifierImage() string {
	if override := os.Getenv(a2aVerifierImageEnvVar); override != "" {
		return override
	}
	return defaultA2AVerifierImage
}

// buildA2AVerifierServiceAccount is the verifier's identity, and it is a bus
// identity: unlike the callout — which cannot authenticate through itself and
// so holds a password — the verifier authenticates with a projected token the
// callout resolves, like the provision Job and every session pod.
//
// That is a deliberate difference from the gateway, which still holds a static
// nats.conf password. No shared secret exists for the component that can read
// every capability in flight, and none should: a Secret read would otherwise
// be a capability read. The gateway's password is a sequencing artefact and is
// recorded as one; this account is what the shape looks like when it is right.
//
// It holds no RBAC. The token exists to be presented to NATS.
func buildA2AVerifierServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aVerifierName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "verifier"),
		},
	}
}

// buildA2AVerifierDeployment renders the service.
func buildA2AVerifierDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aVerifierName(agent)
	labels := a2aLabels(agent, "verifier")
	selector := map[string]string{"app": name}
	podLabels := a2aLabels(agent, "verifier")
	podLabels["app"] = name

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			// Two, for the callout's reason turned up one notch: one replica
			// is a single point of failure in front of every task, not only
			// in front of every new connection. They join a NATS queue group,
			// so exactly one answers each verify request.
			Replicas: ptr.To(int32(2)),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					// Never drop below the running count. A moment with zero
					// ready verifiers is a moment when every submission is
					// refused, and the refusal is terminal on the task — the
					// executor does not retry it.
					MaxUnavailable: ptr.To(intstr.FromInt32(0)),
					MaxSurge:       ptr.To(intstr.FromInt32(1)),
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					ServiceAccountName: name,
					// No default-audience token. The only credential this pod
					// carries is the projected bus token below, and the
					// account it names holds no RBAC, so a second token would
					// be a credential with no purpose on the one pod whose
					// reads are the design's secret.
					AutomountServiceAccountToken: ptr.To(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:  "verifier",
						Image: a2aVerifierImage(),
						// #1259, on the same distroless base the gateway and
						// the callout carry it for: the image's WORKDIR
						// /home/nonroot is 0700 owned by 65532, and this pod
						// runs as 1000. Latent today because the binary never
						// stats ".", which is exactly why it is set here
						// rather than left for the change that would.
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							// NATS_USER is deliberately ABSENT. The binary
							// takes the static-password path only when it is
							// set, and warns when it does; unset is the
							// projected-token path, which is the one this
							// Deployment means.
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
								FieldPath: "metadata.namespace",
							}}},
						},
						VolumeMounts: []corev1.VolumeMount{a2aBusTokenVolumeMount()},
						Ports:        []corev1.ContainerPort{{Name: "status", ContainerPort: a2aVerifierStatusPort}},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/readyz", Port: intstr.FromInt32(a2aVerifierStatusPort),
							}},
							PeriodSeconds:    5,
							FailureThreshold: 3,
						},
						// Liveness does not consult the bus, on the callout's
						// reasoning: a verifier that cannot reach NATS should
						// leave the queue group, not restart. The client
						// reconnects on its own backoff and a restart discards
						// nothing useful while making the outage longer.
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/healthz", Port: intstr.FromInt32(a2aVerifierStatusPort),
							}},
							PeriodSeconds:    10,
							FailureThreshold: 6,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("50m"),
								corev1.ResourceMemory: resource.MustParse("64Mi"),
							},
							Limits: corev1.ResourceList{
								// A cpu limit as well as a memory one: a namespace
								// whose ResourceQuota sets limits.cpu refuses a pod
								// that omits it at admission, and a verifier that
								// never comes up means every executor refuses every
								// task. Matches the callout, which is the same shape
								// of request-reply service.
								corev1.ResourceCPU:    resource.MustParse("500m"),
								corev1.ResourceMemory: resource.MustParse("256Mi"),
							},
						},
						SecurityContext: hardenedSecurityContext(),
					}},
					Volumes: []corev1.Volume{a2aBusTokenVolumeSource()},
				},
			},
		},
	}
}

// buildA2AVerifierNetworkPolicy fences the verifier: DNS and the bus, nothing
// else, in either direction.
//
// The callout gets no policy of its own because it must reach the API server
// to run a TokenReview. The verifier must not — it never talks to Kubernetes,
// its ServiceAccount holds no RBAC, and its only credential is delivered by
// the kubelet through a volume rather than fetched over the network. So the
// egress allowlist is two destinations, and the absence of a 443 rule is the
// control: a verifier that has been made to run somebody else's code cannot
// carry what it read out of the cluster over the pod network.
//
// Ingress carries no rules on purpose. Nothing dials the verifier — requests
// arrive over NATS, which is egress from here — so a listener on this pod that
// something could reach would be an accident. The readiness and liveness
// probes enter from the node over the kubelet path, which NetworkPolicy does
// not govern, so they are unaffected; the status port is deliberately fronted
// by no Service for the same reason.
func buildA2AVerifierNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aVerifierNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "verifier-netpol"),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{"app": a2aVerifierName(agent)},
			},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    clusterDNSPeers(dnsClusterIPs),
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
			},
		},
	}
}

// reconcileA2AVerifier applies the verifier's objects in dependency order: the
// identity first, because the Deployment mounts a token for it and a pod that
// names a missing ServiceAccount is rejected at admission.
// Its fence is not applied here: it rides the shared NetworkPolicy loop in
// reconcileA2ANetworkFences with the bus and session fences, so all three appear and
// disappear with the stack they fence, including through the skew freeze.
func (r *PlatformAgentReconciler) reconcileA2AVerifier(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	owned := []client.Object{
		buildA2AVerifierServiceAccount(agent),
		buildA2AVerifierDeployment(agent),
	}
	for _, obj := range owned {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply A2A verifier %T: %w", obj, err)
		}
	}
	return nil
}
