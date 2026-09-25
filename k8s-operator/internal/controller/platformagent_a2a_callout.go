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
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The auth callout Deployment.
//
// It sits ON the connection path: while it is down no NEW connection to the bus
// succeeds, though every established one keeps working. That shapes almost
// everything below — two replicas, a readiness probe that reports whether the
// map is actually being served, a liveness probe that deliberately does not,
// and a rollout that never takes both replicas out at once.
//
// The blast radius, stated honestly rather than implied: during a callout
// outage the fabric is dark to new work — no new sessions, no new workers — not
// gracefully degraded. Established connections and the client resilience
// contract are what make that acceptable at this stage. A hardened HA callout
// is production posture, not part of the dev toggle.

// a2aIdentityMapSchemaAnnotation carries a2aIdentityMapSchema onto the callout
// pod template.
const a2aIdentityMapSchemaAnnotation = "a2a.kubeagents.x-k8s.io/identity-map-schema"

// a2aIdentityMapSchema is the version of the rendered identity map's SHAPE, not
// of its contents. It is on the callout's pod template, so a release that
// changes the shape rolls the callout exactly once.
//
// Why it has to exist. The callout parses with DisallowUnknownFields, on
// purpose: a key it does not recognise is a grant or a narrowing it cannot
// enforce, and serving the map anyway would enforce less than the operator
// rendered. The cost of that strictness is that the two cannot skew across a
// field addition -- and nothing else in this Deployment's spec changes when the
// map does, so on an upgrade where the callout image reference is unchanged
// (the default is a mutable tag) the old pods are never rolled. They refuse
// every new map on the unknown field, keep serving the last one they accepted,
// keep their readiness probe green, and BusCredentialsReady goes on reporting
// True while naming a version no replica has. Measured on a2a-next-dev,
// 2026-09-14: operator upgraded alone, callout generation unchanged at 8, CR
// claiming `6ce6662285534d95` while both replicas served `c3017a17889c5f45`
// with `lastError: unknown field "narrowing"`, and every session refused with
// "platform-agent-a2a-session is not in the identity map".
//
// What rolling buys is not that the new pods can read the map -- with a
// mutable tag they pull a binary that can, but that is the registry's doing,
// not ours. It is that a pod which refuses its FIRST map has no previous one to
// fall back on, so Store.Ready is false, /readyz answers 503 `no identity map:
// ...`, the Deployment never goes Available and the rollout stops with the
// reason on the pod. The silent case only exists because the refusing replica
// was already serving something. Rolling removes the fallback, and with it the
// silence.
//
// Rolling the callout on every map CHANGE was considered and rejected -- see
// platformagent_a2a_buscreds.go, the callout is on the connection path and a
// restart is a window where new connections fail. This is the narrow version of
// that trade: once per release that changes the map's shape, never for a
// routine identity edit. A bounded window during an upgrade is worth a
// permanent one after it.
//
// Bump this when the rendered map gains, renames or removes a field, and add
// the new key set to a2aIdentityMapSchemaKeys in the test beside it.
//
//	1 -> serviceAccount, user, account, grants
//	2 -> adds narrowing
const a2aIdentityMapSchema = "2"

const (
	defaultA2ACalloutImage = "us-east4-docker.pkg.dev/bnaylor-kagents-dev/kube-agents/a2a-authcallout:dev"
	a2aCalloutImageEnvVar  = "A2A_CALLOUT_IMAGE"

	// a2aBusTokenAudience is the audience every bus token is bound to.
	//
	// Load-bearing rather than cosmetic. A TokenReview that requests no
	// audience validates against the API server's own, which every pod's
	// default ServiceAccount token already carries — so without this the bus
	// would accept any readable token in the cluster as proof of that pod's
	// identity. Bound to a dedicated audience, the only tokens it accepts
	// are ones minted for it by a projected volume that names it. Spelled in
	// the API package so the validating webhook refuses a user volume that
	// projects the same audience; see agentv1alpha1.BusCredentialRoutes.
	a2aBusTokenAudience = agentv1alpha1.A2ABusTokenAudience

	// a2aBusTokenPath is where every bus client finds its projected token.
	a2aBusTokenPath      = "/var/run/secrets/a2a-bus" // #nosec G101 -- Mount path, not a credential
	a2aBusTokenFile      = "token"
	a2aBusTokenVolume    = "a2a-bus-token" // #nosec G101 -- Volume name, not a credential
	a2aBusTokenExpirySec = 3600

	// a2aCalloutStatusPort serves readiness, liveness and the served map
	// version.
	a2aCalloutStatusPort = 8080
)

func a2aCalloutImage() string {
	if override := os.Getenv(a2aCalloutImageEnvVar); override != "" {
		return override
	}
	return defaultA2ACalloutImage
}

// a2aBusTokenVolumeSource is the projected token every callout-authenticated
// client mounts. Audience-bound and short-lived, rotated by the kubelet.
func a2aBusTokenVolumeSource() corev1.Volume {
	return corev1.Volume{
		Name: a2aBusTokenVolume,
		VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{
				Sources: []corev1.VolumeProjection{{
					ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
						Audience: a2aBusTokenAudience,
						// The kubelet refreshes this well before expiry, so
						// a client must re-read the file when it reconnects
						// rather than caching the first value it saw — a
						// long-lived process holding a stale token fails
						// exactly when the bus restarts, which the
						// deployment spec calls a routine operation.
						ExpirationSeconds: ptr.To(int64(a2aBusTokenExpirySec)),
						Path:              a2aBusTokenFile,
					},
				}},
			},
		},
	}
}

func a2aBusTokenVolumeMount() corev1.VolumeMount {
	return corev1.VolumeMount{Name: a2aBusTokenVolume, MountPath: a2aBusTokenPath, ReadOnly: true}
}

// a2aStripBusTokenMounts removes a mount of the projected bus token from
// user-authored containers.
//
// The token belongs to the platform-agent container and to nothing else in the
// pod. That is the whole A5 split: the callout resolves the pod's
// ServiceAccount, so a second container holding this token is a second workload
// wearing the `agent` identity, and one that also holds bridge-password holds
// the union of the two grant sets -- `worker` rebuilt out of a volumeMount.
// Reachable in an ordinary CR: spec.deployment.sidecars and .initContainers are
// copied verbatim, the volume is in the pod under `next`, and the bridge image
// is built FROM the platform-agent image (a2a/docs/hermes-bridge.md) so it
// already ships the client that would read it.
//
// Stripped rather than rejected, for the reason SensitiveEnvVars gives one
// field over: the validating webhook's chart default failurePolicy is Ignore,
// so an unreachable webhook admits the object with validation skipped. The
// webhook's refusal is what tells the author why; this is what holds.
//
// Name-matched, and only that. The source-matched half -- a user volume
// projecting the same audience under another name, or mounting the
// credentials Secret -- is a2aStripBusCredentialSources and the mount strips
// beside it, below. ReservedVolumeNames carries what the pair does and does
// not buy.
//
// The input slices belong to the CR, so the copy stripContainerMountsMatching
// makes is not incidental.
func a2aStripBusTokenMounts(containers []corev1.Container) []corev1.Container {
	return stripContainerMountsMatching(containers, a2aIsBusTokenMount)
}

// a2aIsBusTokenMount is the predicate the two mount strips on either side of
// it share, named rather than written twice so they cannot drift apart. Name
// matching is the whole of what it claims; a2aStripBusTokenMounts says what a
// name match does not buy.
func a2aIsBusTokenMount(m corev1.VolumeMount) bool { return m.Name == a2aBusTokenVolume }

// a2aStripBusTokenVolumeMounts is the same removal against a bare mount list,
// for the one user-authored mount surface that reaches a container the operator
// builds rather than one the CR declares.
//
// spec.deployment.extraVolumeMounts is that surface, and it is the widest of
// the five: buildBaseContainers appends it verbatim to the platform-agent
// container AND to platform-agent-dashboard, so a CR naming this volume there
// mints the agent's bus identity into the dashboard -- a second container
// wearing it, which is exactly what a2aStripBusTokenMounts above exists to
// prevent for a sidecar. The dashboard runs the same image as the agent, so it
// already ships the client that would read the file.
//
// Nothing in the operator's own lists names this volume, so this runs over
// user-authored mounts only and the platform-agent container gets its real
// mount from mountIntoContainer after the strip.
func a2aStripBusTokenVolumeMounts(mounts []corev1.VolumeMount) []corev1.VolumeMount {
	return stripMatching(mounts, a2aIsBusTokenMount)
}

// a2aStripBusTokenVolume removes a user-supplied volume that shadows the
// projected bus token's name. Two reasons, and the second holds even if the
// first is someone's honest mistake: a Secret or hostPath volume under this
// name is a token of the author's choosing presented as the pod's, and two
// volumes with one name is a Deployment server-side apply refuses outright,
// which wedges every reconcile of the CR with nothing in status to say why.
func a2aStripBusTokenVolume(volumes []corev1.Volume) []corev1.Volume {
	return stripMatching(volumes, func(v corev1.Volume) bool { return v.Name == a2aBusTokenVolume })
}

// a2aBusCredentialVolumeNames is the set of user-authored volumes, on both of
// the CR's volume lists, whose SOURCE would deliver the bus credential to
// whichever container mounts them: a projected serviceAccountToken for the
// bus audience under any name, or one of the Secrets the operator renders
// with bus credentials in them (agentv1alpha1.BusCredentialRoutes has the two
// routes, the three Secrets, and why env is not among them). It is
// what the mount strips below key on, and it is empty for a CR that carries
// neither, in which case every strip below returns its input unchanged. A
// pure function of the CR, so buildBaseContainers recomputes it rather than
// taking a parameter.
//
// This is a guard against a misconfiguration by the CR's author, who is the
// platform operator, and not a boundary against a hostile sidecar: KSA tokens
// are pod-scoped and the callout cannot tell which container presented one.
// Reading the reservation as stronger than that was the defect in the
// name-only version above (gke-labs#1667).
//
// Silent, like a2aStripBusTokenVolume: the webhook's field.Forbidden is what
// tells the author why, and this is what holds when admission did not run.
func a2aBusCredentialVolumeNames(agent *agentv1alpha1.PlatformAgent) map[string]bool {
	if agent.Spec.Deployment == nil {
		return nil
	}
	var names map[string]bool
	for _, list := range [][]corev1.Volume{agent.Spec.Deployment.SidecarVolumes, agent.Spec.Deployment.ExtraVolumes} {
		for _, v := range list {
			if len(agentv1alpha1.BusCredentialRoutes(v, agent.Name)) == 0 {
				continue
			}
			if names == nil {
				names = map[string]bool{}
			}
			names[v.Name] = true
		}
	}
	return names
}

// a2aStripBusCredentialSources removes the volumes a2aBusCredentialVolumeNames
// describes from one of the CR's volume lists. The operator's own projection
// never passes through here -- buildPodTemplateSpec appends
// a2aBusTokenVolumeSource to the pod separately -- so the only volumes this
// can drop are the author's.
func a2aStripBusCredentialSources(volumes []corev1.Volume, agentName string) []corev1.Volume {
	return stripMatching(volumes, func(v corev1.Volume) bool {
		return len(agentv1alpha1.BusCredentialRoutes(v, agentName)) > 0
	})
}

// The two mount strips that used to live here -- one over a bare mount list,
// one over a list of containers -- were character-for-character
// stripVolumeMountsNamed and stripContainerMountsNamed in
// platformagent_manifests.go. They were written separately because
// gke-labs#1675 had not landed yet. The callers use those two directly now;
// what is bus-specific is the name set a2aBusCredentialVolumeNames builds, not
// the removal.

// buildA2ACalloutServiceAccount is the identity the callout runs as. It is not
// a bus identity: the callout authenticates to NATS with a password, because it
// cannot authenticate through itself.
func buildA2ACalloutServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "callout")},
	}
}

// a2aAuthDelegatorRole is the built-in ClusterRole that grants TokenReview.
//
// Bound rather than authored. TokenReview is a cluster-scoped API, so the
// callout's grant is a ClusterRoleBinding either way; what differs is which
// ClusterRole it points at.
//
// Why the operator needs `bind` at all, measured rather than reasoned: the
// operator has held unscoped create/update/patch/delete on clusterroles and
// clusterrolebindings since long before this callout existed, so `bind` is not
// standing in for a grant it lacks. What stops that CRUD from being a general
// escalation is the API server's escalation check plus the absence of
// `escalate`. system:auth-delegator grants subjectaccessreviews/create, which
// the operator does NOT hold — so without `bind` the API server refuses the
// binding outright:
//
//	clusterrolebindings.rbac.authorization.k8s.io "..." is forbidden: user
//	"system:serviceaccount:kubeagents-system:kubeagents-controller" is
//	attempting to grant RBAC permissions not currently held:
//	{APIGroups:["authorization.k8s.io"], Resources:["subjectaccessreviews"],
//	Verbs:["create"]}
//
// That grant is therefore load-bearing, and scoped by resourceNames to this one
// role so it cannot attach cluster-admin to anything.
//
// What it costs, and the alternative it was chosen over: system:auth-delegator
// also carries subjectaccessreviews/create, which the callout never calls. An
// operator-authored ClusterRole holding only tokenreviews/create would give the
// callout exactly what it uses and would need no new operator permission at all
// — the operator already holds tokenreviews/create, so the escalation check
// passes (verified by server-side dry run against a cluster with no `bind`
// rule: the tokenreviews-only role is admitted, a subjectaccessreviews one is
// refused). Binding the role kube-apiserver ships is the conventional pattern
// for a TokenReview client and keeps the grant auditable by name in role.yaml,
// which is why it is what ships; the narrower option is tracked as #1320.
//
// It is part of kube-apiserver's default RBAC bootstrap, so it exists on every
// cluster with RBAC enabled — no GKE dependency, consistent with the deployment
// spec's stock-Kubernetes requirement.
const a2aAuthDelegatorRole = "system:auth-delegator"

// a2aCalloutClusterRoleBindingName qualifies the binding by namespace as well
// as agent name.
//
// It is cluster-scoped, so a bare CR name is ambiguous between two agents of
// the same name in different namespaces — and the collision is not benign.
// Each would rewrite the other's Subjects on every reconcile, so one namespace's
// callout silently loses TokenReview and refuses every connection with an error
// indistinguishable from a bad token; and teardown then fails the ownership
// check and returns "refusing to delete unowned" forever. The controller
// already spells cluster-scoped names this way elsewhere.
func a2aCalloutClusterRoleBindingName(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("kubeagents:a2a-callout-tokenreview:%s:%s", agent.Namespace, agent.Name)
}

func buildA2ACalloutClusterRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRoleBinding {
	return &rbacv1.ClusterRoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "ClusterRoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutClusterRoleBindingName(agent), Labels: a2aLabels(agent, "callout")},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     a2aAuthDelegatorRole,
		},
		Subjects: []rbacv1.Subject{{
			Kind:      "ServiceAccount",
			Name:      a2aCalloutName(agent),
			Namespace: agent.Namespace,
		}},
	}
}

// buildA2ACalloutRole grants the read on the identity map, namespaced and
// named.
//
// get/list/watch, because the callout reads the map through an informer and a
// reflector needs all three. Scoped to the one object by name: the callout has
// no business seeing any other ConfigMap in the namespace, and a namespace-wide
// read would make its token a lever on everything else in there.
//
// **This Role works only because the informer lists and watches with a
// metadata.name field selector, and that coupling is invisible from either
// side.** The widely-repeated rule is that resourceNames cannot restrict list
// or watch, because a collection request names no resource — and that used to
// be the whole story. Since selector-aware authorization the API server matches
// resourceNames against a metadata.name field selector, so a narrowed LIST is
// authorized where a bare one is refused. Measured against a real API server at
// 1.36 rather than taken from the documentation, because the documented
// folklore says this Role should not work:
//
//	GET  themap                              -> allowed
//	LIST configmaps?metadata.name=themap     -> allowed
//	LIST configmaps                          -> forbidden
//	WATCH configmaps?metadata.name=themap    -> allowed
//
// Two consequences. Do not "fix" this Role by widening it to namespace-wide
// list/watch — it is not broken. And do not drop the field selector from
// Store.WatchConfigMap, which would turn the informer's LIST into the bare form
// and get it refused. The pairing is asserted by
// TestTheCalloutRoleAuthorizesTheInformerItIsPairedWith.
//
// The version floor this implies is real: on a cluster predating selector-aware
// authorization the LIST is refused, the informer never syncs, the callout never
// serves a map and its readiness probe stays red. That is a loud failure with
// BusCredentialsReady false and a named error in the log, not a silent one —
// which is the reason this is acceptable rather than hedged with a wider grant.
func buildA2ACalloutRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "callout")},
		Rules: []rbacv1.PolicyRule{{
			APIGroups:     []string{""},
			Resources:     []string{"configmaps"},
			ResourceNames: []string{a2aAuthMapName(agent)},
			Verbs:         []string{"get", "list", "watch"},
		}},
	}
}

func buildA2ACalloutRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aCalloutName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "callout")},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aCalloutName(agent)},
		Subjects: []rbacv1.Subject{{
			Kind:      "ServiceAccount",
			Name:      a2aCalloutName(agent),
			Namespace: agent.Namespace,
		}},
	}
}

// buildA2ACalloutDeployment renders the service.
func buildA2ACalloutDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aCalloutName(agent)
	labels := a2aLabels(agent, "callout")
	selector := map[string]string{"app": name}
	podLabels := a2aLabels(agent, "callout")
	podLabels["app"] = name

	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{
			// Two, because one is a single point of failure in front of every
			// new connection on the bus. They join a queue group, so exactly
			// one of them answers each request.
			Replicas: ptr.To(int32(2)),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					// Never drop below the running count during a rollout.
					// A moment with zero ready callouts is a moment when
					// nothing new can connect to the bus, and the clients
					// that hit it see an Authorization Violation they cannot
					// distinguish from a bad token.
					MaxUnavailable: ptr.To(intstr.FromInt32(0)),
					MaxSurge:       ptr.To(intstr.FromInt32(1)),
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: podLabels,
					// Not a config hash: this changes when the map's
					// SHAPE does, not when its contents do.
					Annotations: map[string]string{
						a2aIdentityMapSchemaAnnotation: a2aIdentityMapSchema,
					},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:           name,
					AutomountServiceAccountToken: ptr.To(true),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:  "callout",
						Image: a2aCalloutImage(),
						// The image's WORKDIR is /home/nonroot, 0700 owned by
						// 65532, and the pod above imposes 1000 - measured with
						// `crane config` on a build of Dockerfile.authcallout,
						// and on the distroless base it inherits it from. An
						// image's WORKDIR is chosen for the user that image
						// expects, so a render overriding the user owns the
						// working directory too (#1259, and hardenedSecurityContext()
						// carries the general form). Latent rather than broken
						// here, the same as the gateway: the binary never stats
						// "." and this Deployment has run at 2/2 live. It is set
						// anyway because "latent" is a property of today's code
						// and the next line that touches the filesystem would
						// end it silently. "/" rather than a mount because the
						// callout writes nothing - it needs to enter its cwd,
						// not write to it.
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							{Name: "NATS_USER", Value: a2aCalloutConfUser},
							{Name: "NATS_PASSWORD", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
								LocalObjectReference: corev1.LocalObjectReference{Name: a2aCredsSecretName(agent)},
								Key:                  a2aCalloutPasswordKey,
							}}},
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
								FieldPath: "metadata.namespace",
							}}},
							{Name: "A2A_AUTHMAP_NAME", Value: a2aAuthMapName(agent)},
							{Name: "A2A_AUTHMAP_KEY", Value: a2aAuthMapKey},
							{Name: "A2A_TOKEN_AUDIENCE", Value: a2aBusTokenAudience},
							// The seeds. This Deployment is the only thing
							// that mounts them, and the issuer is the key
							// that decides what every connection on this bus
							// may do.
							{Name: "A2A_ISSUER_SEED", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
								LocalObjectReference: corev1.LocalObjectReference{Name: a2aCalloutKeysName(agent)},
								Key:                  a2aCalloutIssuerSeedKey,
							}}},
							{Name: "A2A_XKEY_SEED", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
								LocalObjectReference: corev1.LocalObjectReference{Name: a2aCalloutKeysName(agent)},
								Key:                  a2aCalloutXKeySeedKey,
							}}},
						},
						Ports: []corev1.ContainerPort{{Name: "status", ContainerPort: a2aCalloutStatusPort}},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/readyz", Port: intstr.FromInt32(a2aCalloutStatusPort),
							}},
							PeriodSeconds:    5,
							FailureThreshold: 3,
						},
						// Liveness does NOT consult the map, deliberately. A
						// callout that cannot reach the API server should be
						// taken out of the Service, not restarted: a restart
						// discards the map it already had and cannot make the
						// API server answer any sooner.
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/livez", Port: intstr.FromInt32(a2aCalloutStatusPort),
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
								corev1.ResourceCPU:    resource.MustParse(a2aCalloutCPULimit),
								corev1.ResourceMemory: resource.MustParse("256Mi"),
							},
						},
						SecurityContext: &corev1.SecurityContext{
							AllowPrivilegeEscalation: ptr.To(false),
							ReadOnlyRootFilesystem:   ptr.To(true),
							Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
							SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
						},
					}},
				},
			},
		},
	}
}

// buildA2ACalloutService fronts the status endpoints. The bus reaches the
// callout over NATS rather than over this, so nothing on the authorization path
// depends on it; it exists so an operator can read the served map version and
// so the readiness state is visible in one place.
func buildA2ACalloutService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := a2aCalloutName(agent)
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, "callout")},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{"app": name},
			Ports: []corev1.ServicePort{{
				Name:       "status",
				Port:       a2aCalloutStatusPort,
				TargetPort: intstr.FromInt32(a2aCalloutStatusPort),
			}},
		},
	}
}

// reconcileA2ACallout applies the callout's objects in dependency order.
func (r *PlatformAgentReconciler) reconcileA2ACallout(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	// Namespaced objects get an owner reference so they are reclaimed with
	// the CR. The one cluster-scoped object cannot: a cluster-scoped object
	// owned by a namespaced one is treated as an orphan by the garbage
	// collector, which deletes it immediately. It is reclaimed by name
	// instead, from both the mode flip and the deletion path.
	owned := []client.Object{
		buildA2ACalloutServiceAccount(agent),
		// The provision Job's identity, applied here so it exists before the
		// Job that mounts a token for it.
		buildA2AProvisionServiceAccount(agent),
		// The session identity, applied here so it exists before the gateway
		// can spawn a pod that names it. A pod naming a missing ServiceAccount
		// is rejected by the API server, which the gateway would surface as a
		// failed spawn rather than as a misconfiguration.
		buildA2ASessionServiceAccount(agent),
		buildA2ACalloutRole(agent),
		buildA2ACalloutRoleBinding(agent),
		buildA2ACalloutDeployment(agent),
		buildA2ACalloutService(agent),
	}
	for _, obj := range owned {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply A2A callout %T: %w", obj, err)
		}
	}

	// Cluster-scoped: no owner reference, because the garbage collector
	// treats a cluster-scoped object owned by a namespaced one as an orphan
	// and deletes it immediately. cleanupA2A removes it by name.
	for _, obj := range []client.Object{
		buildA2ACalloutClusterRoleBinding(agent),
	} {
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply A2A callout %T: %w", obj, err)
		}
	}
	return nil
}

// buildA2ASessionServiceAccount is the identity every spawned session pod runs
// as. Like the provisioner's it holds no RBAC — the token exists so the auth
// callout has something to resolve, not so the pod can talk to the API server —
// and here that matters more, because the workload running under it is the one
// executing model output.
func buildA2ASessionServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aSessionServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "session"),
		},
	}
}

// buildA2AProvisionServiceAccount is the provision Job's identity. It holds no
// RBAC: the token exists so the auth callout has something to resolve, not so
// the Job can talk to the API server.
func buildA2AProvisionServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aProvisionServiceAccountName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "provision"),
		},
	}
}
