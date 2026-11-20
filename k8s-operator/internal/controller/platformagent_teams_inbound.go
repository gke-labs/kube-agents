package controller

import (
	"context"
	"fmt"
	"path"
	"strconv"

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

// The Microsoft Teams inbound receiver (#1034): the one component of an
// install that accepts connections from the internet. Bot Framework
// delivers each Teams message by an HTTPS request to the address registered
// for the bot, so without a listener a message typed in Teams never reaches
// the agent. The receiver (a2a/teamsinbound) verifies Microsoft's signed
// token on each request, pins the tenant, and hands the activity to the
// credential broker's Teams queue, which the install's Teams consumer
// already reads. It holds no credential of the install's own, runs in a
// pod of its own, and reaches the broker on one route under a role of its
// own (CALLER_ROLE_TEAMS_INBOUND). The admin exposes its Service with their
// own Ingress and TLS certificate; the operator renders no Ingress.
const (
	a2aTeamsInboundImageName   = "a2a-teams-inbound"
	a2aTeamsInboundImageEnvVar = "A2A_TEAMS_INBOUND_IMAGE"

	teamsInboundComponent       = "teams-inbound"
	teamsInboundNetpolComponent = teamsInboundComponent + "-netpol"
	teamsInboundNameSuffix      = "-teams-inbound"
	teamsInboundNetpolSuffix    = "-teams-inbound-netpol"
	teamsInboundContainer       = "receiver"

	// teamsInboundPort is the public listener, the one the Ingress fronts;
	// teamsInboundStatusPort serves the probes and the collector. The
	// receiver's own defaults (a2a/teamsinbound, DefaultListen and
	// DefaultStatusListen) are these; the env below pins them anyway so the
	// Service, the probes and the fence cannot drift from the binary.
	teamsInboundPort           int32 = 8100
	teamsInboundPortName             = "http"
	teamsInboundStatusPort     int32 = 9094
	teamsInboundStatusPortName       = "status"
	teamsInboundReadyzPath           = "/readyz"
	teamsInboundHealthzPath          = "/healthz"
	// teamsInboundHTTPSPort is the egress the receiver needs beyond the
	// broker: Microsoft's OpenID configuration and signing keys.
	teamsInboundHTTPSPort int32 = 443
	// teamsInboundAnyIPv4 is the egress peer for that fetch. Microsoft
	// publishes no stable address for its key endpoint, so the rule is the
	// internet on 443 less the metadata server; the FQDN policy, where the
	// cluster has one, narrows it further.
	teamsInboundAnyIPv4 = "0.0.0.0/0"

	// The receiver's environment, as a2a/teamsinbound reads it.
	teamsInboundEnvListen       = "TEAMS_INBOUND_LISTEN"
	teamsInboundEnvStatusListen = "TEAMS_INBOUND_STATUS_LISTEN"
	teamsInboundEnvAppID        = "TEAMS_INBOUND_APP_ID"
	teamsInboundEnvTenantID     = "TEAMS_INBOUND_TENANT_ID"
	teamsInboundEnvRelayURL     = "TEAMS_INBOUND_RELAY_URL"
	teamsInboundEnvTokenPath    = "TEAMS_INBOUND_TOKEN_PATH" // #nosec G101 -- an environment variable name, not a credential
	teamsInboundListenAnyHost   = ""

	// The projected ServiceAccount token the receiver presents to the
	// broker, under an audience of its own.
	teamsInboundTokenVolume     = "broker-token"
	teamsInboundTokenDir        = "/var/run/secrets/kube-agents/teams-inbound"
	teamsInboundTokenKey        = "token"
	teamsInboundTokenTTLSeconds = 3600
	teamsInboundTokenFileMode   = int32(0400)

	// credentialProxyTeamsInboundAudience is the token audience the broker
	// confers the teams-inbound role by; the callers env binds that role to
	// the receiver's ServiceAccount alone (credential_proxy.py,
	// TEAMS_INBOUND_AUDIENCE_ENV and TEAMS_INBOUND_CALLERS_ENV).
	credentialProxyTeamsInboundAudience       = "kubeagents-credential-proxy-teams-inbound" // #nosec G101 -- Token audience string, not a credential
	credentialProxyTeamsInboundAudienceEnvVar = "CREDENTIAL_PROXY_TEAMS_INBOUND_AUDIENCE"   // #nosec G101 -- Environment variable name, not a credential
	credentialProxyTeamsInboundCallersEnvVar  = "CREDENTIAL_PROXY_TEAMS_INBOUND_CALLERS"    // #nosec G101 -- Environment variable name, not a credential

	// The pod runs as the same unprivileged uid the other a2a binaries do,
	// with an fsGroup so the 0400 projected token is readable.
	teamsInboundRunAsUser int64 = 1000
	teamsInboundFSGroup   int64 = 1000

	teamsInboundReadinessPeriod  = 5
	teamsInboundReadinessFailure = 3
	teamsInboundLivenessPeriod   = 10
	teamsInboundLivenessFailure  = 6

	// Sized for what it does: verify a signature and forward a small body.
	teamsInboundCPURequest    = "50m"
	teamsInboundMemoryRequest = "64Mi"
	teamsInboundCPULimit      = "200m"
	teamsInboundMemoryLimit   = "128Mi"

	// teamsInboundFenceIndex is the fence's place in teamsInboundObjects: it
	// is removed last, once no receiver pod is left, so a terminating pod
	// is never unfenced.
	teamsInboundFenceIndex = 3
)

// teamsInboundEnabled reports whether the receiver is on: Teams enabled, the
// inbound half enabled, and a tenant to pin (the CRD rule requires it; this
// repeats the check so a CR written past the webhook renders nothing rather
// than a receiver that refuses every message).
func teamsInboundEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if !teamsEnabled(agent) {
		return false
	}
	teams := agent.Spec.Integration.Teams
	return teams.Inbound != nil && teams.Inbound.Enabled != nil && *teams.Inbound.Enabled && teams.TenantId != ""
}

func a2aTeamsInboundImage() string {
	return a2aReleaseImage(a2aTeamsInboundImageEnvVar, a2aTeamsInboundImageName)
}

func teamsInboundName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + teamsInboundNameSuffix
}

func teamsInboundNetpolName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + teamsInboundNetpolSuffix
}

func teamsInboundPodSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": teamsInboundName(agent)}
}

// teamsInboundBrokerCaller is the TokenReview username of the receiver's
// ServiceAccount, as the broker sees it.
func teamsInboundBrokerCaller(agent *agentv1alpha1.PlatformAgent) string {
	return fmt.Sprintf("system:serviceaccount:%s:%s", agent.Namespace, teamsInboundName(agent))
}

// teamsInboundBrokerEnv is the broker's audience and caller binding for the
// receiver, rendered only while it runs: the audience confers the
// teams-inbound role, and the callers hold it to this one ServiceAccount.
func teamsInboundBrokerEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if !teamsInboundEnabled(agent) {
		return nil
	}
	return []corev1.EnvVar{
		{Name: credentialProxyTeamsInboundAudienceEnvVar, Value: credentialProxyTeamsInboundAudience},
		{Name: credentialProxyTeamsInboundCallersEnvVar, Value: teamsInboundBrokerCaller(agent)},
	}
}

// teamsInboundBrokerPeers admits the receiver pod to the broker's port while
// the receiver runs.
func teamsInboundBrokerPeers(agent *agentv1alpha1.PlatformAgent) []networkingv1.NetworkPolicyPeer {
	if !teamsInboundEnabled(agent) {
		return nil
	}
	return []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchLabels: teamsInboundPodSelector(agent)}}}
}

// buildTeamsInboundServiceAccount is the receiver's identity: no Role binds
// to it; its one use is the projected token the broker reviews.
func buildTeamsInboundServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      teamsInboundName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, teamsInboundComponent),
		},
	}
}

// buildTeamsInboundDeployment runs the receiver: one replica, read-only
// root, no API server token mounted, the broker token projected under its
// own audience, and the bot's app id read from the same Secret key the
// broker reads it from (the password stays with the broker).
func buildTeamsInboundDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := teamsInboundName(agent)
	teams := agent.Spec.Integration.Teams
	podLabels := a2aLabels(agent, teamsInboundComponent)
	podLabels["app"] = name
	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, teamsInboundComponent)},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Selector: &metav1.LabelSelector{MatchLabels: teamsInboundPodSelector(agent)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					ServiceAccountName:           name,
					AutomountServiceAccountToken: ptr.To(false),
					ImagePullSecrets:             resolveImagePullSecrets(agent.Spec.Deployment),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(teamsInboundRunAsUser),
						FSGroup:        ptr.To(teamsInboundFSGroup),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: []corev1.Volume{{
						Name: teamsInboundTokenVolume,
						VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
							DefaultMode: ptr.To(teamsInboundTokenFileMode),
							Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
								Audience:          credentialProxyTeamsInboundAudience,
								ExpirationSeconds: ptr.To(int64(teamsInboundTokenTTLSeconds)),
								Path:              teamsInboundTokenKey,
							}}},
						}},
					}},
					Containers: []corev1.Container{{
						Name:  teamsInboundContainer,
						Image: a2aTeamsInboundImage(),
						Env: []corev1.EnvVar{
							{Name: teamsInboundEnvListen, Value: teamsInboundListenAnyHost + ":" + strconv.Itoa(int(teamsInboundPort))},
							{Name: teamsInboundEnvStatusListen, Value: teamsInboundListenAnyHost + ":" + strconv.Itoa(int(teamsInboundStatusPort))},
							{Name: teamsInboundEnvAppID, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(teams.AppIdSecretRef, defaultPlatformAgentSecrets, "TEAMS_APP_ID")}},
							{Name: teamsInboundEnvTenantID, Value: teams.TenantId},
							{Name: teamsInboundEnvRelayURL, Value: credentialProxyBaseURL(agent)},
							{Name: teamsInboundEnvTokenPath, Value: path.Join(teamsInboundTokenDir, teamsInboundTokenKey)},
						},
						Ports: []corev1.ContainerPort{
							{Name: teamsInboundPortName, ContainerPort: teamsInboundPort},
							{Name: teamsInboundStatusPortName, ContainerPort: teamsInboundStatusPort},
						},
						VolumeMounts: []corev1.VolumeMount{{Name: teamsInboundTokenVolume, MountPath: teamsInboundTokenDir, ReadOnly: true}},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: teamsInboundReadyzPath, Port: intstr.FromInt32(teamsInboundStatusPort)}},
							PeriodSeconds:    teamsInboundReadinessPeriod,
							FailureThreshold: teamsInboundReadinessFailure,
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: teamsInboundHealthzPath, Port: intstr.FromInt32(teamsInboundStatusPort)}},
							PeriodSeconds:    teamsInboundLivenessPeriod,
							FailureThreshold: teamsInboundLivenessFailure,
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse(teamsInboundCPURequest),
								corev1.ResourceMemory: resource.MustParse(teamsInboundMemoryRequest),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse(teamsInboundCPULimit),
								corev1.ResourceMemory: resource.MustParse(teamsInboundMemoryLimit),
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

// buildTeamsInboundService is what the admin's Ingress routes to: the
// public port alone. The status port is not on it.
func buildTeamsInboundService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	name := teamsInboundName(agent)
	return &corev1.Service{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, teamsInboundComponent)},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: teamsInboundPodSelector(agent),
			Ports: []corev1.ServicePort{{
				Name:       teamsInboundPortName,
				Port:       teamsInboundPort,
				TargetPort: intstr.FromInt32(teamsInboundPort),
			}},
		},
	}
}

// buildTeamsInboundNetworkPolicy fences the receiver both ways. Ingress: the
// public port from anywhere, since the Ingress controller or load balancer
// that fronts it is the cluster owner's and has no label this policy can
// name; and the status port from the collector's namespace. Egress: the
// cluster's DNS, the broker's port, and 443 to the internet less the
// metadata server, for Microsoft's key endpoint. Nothing else: not the bus,
// not the gateway, not the API server.
func buildTeamsInboundNetworkPolicy(agent *agentv1alpha1.PlatformAgent, dnsClusterIPs []string) *networkingv1.NetworkPolicy {
	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{APIVersion: "networking.k8s.io/v1", Kind: "NetworkPolicy"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      teamsInboundNetpolName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, teamsInboundNetpolComponent),
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: teamsInboundPodSelector(agent)},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress},
			Ingress: []networkingv1.NetworkPolicyIngressRule{
				{Ports: []networkingv1.NetworkPolicyPort{tcpPort(teamsInboundPort)}},
				{
					From: []networkingv1.NetworkPolicyPeer{{
						NamespaceSelector: &metav1.LabelSelector{MatchLabels: map[string]string{labelMetadataName: gmpNamespace}},
					}},
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(teamsInboundStatusPort)},
				},
			},
			Egress: []networkingv1.NetworkPolicyEgressRule{
				{
					Ports: []networkingv1.NetworkPolicyPort{udpPort(a2aDNSPort), tcpPort(a2aDNSPort)},
					To:    clusterDNSPeers(dnsClusterIPs),
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(credentialProxyPort)},
					To:    []networkingv1.NetworkPolicyPeer{namespacedPodPeer(agent.Namespace, credentialProxySelector(agent))},
				},
				{
					Ports: []networkingv1.NetworkPolicyPort{tcpPort(teamsInboundHTTPSPort)},
					To: []networkingv1.NetworkPolicyPeer{{IPBlock: &networkingv1.IPBlock{
						CIDR:   teamsInboundAnyIPv4,
						Except: []string{metadataResolverCIDR},
					}}},
				},
			},
		},
	}
}

// reconcileTeamsInbound renders the receiver while it is enabled and takes
// it away when it is not. The fence is reconcileA2ANetworkFences's while the
// receiver runs, and teamsInboundObjects' to remove.
func (r *PlatformAgentReconciler) reconcileTeamsInbound(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if !teamsInboundEnabled(agent) {
		return r.removeTeamsInbound(ctx, agent)
	}
	for _, obj := range []client.Object{
		buildTeamsInboundServiceAccount(agent),
		buildTeamsInboundDeployment(agent),
		buildTeamsInboundService(agent),
	} {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("failed to apply the Teams inbound receiver's %T: %w", obj, err)
		}
	}
	return nil
}

// removeTeamsInbound deletes the receiver's objects when it is off, the
// fence last and only once no receiver pod is left.
func (r *PlatformAgentReconciler) removeTeamsInbound(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	noPodLeft := func() (bool, error) {
		pods := &corev1.PodList{}
		if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace),
			client.MatchingLabels(teamsInboundPodSelector(agent))); err != nil {
			return false, fmt.Errorf("failed to list the Teams inbound receiver's pods: %w", err)
		}
		return len(pods.Items) == 0, nil
	}
	for i, entry := range r.teamsInboundObjects(agent) {
		var ready func() (bool, error)
		if i == teamsInboundFenceIndex {
			ready = noPodLeft
		}
		if err := r.deleteA2AObjectIfOwned(ctx, agent, entry.obj, entry.reader, ready); err != nil {
			return fmt.Errorf("failed to remove the Teams inbound receiver's %T: %w", entry.obj, err)
		}
	}
	return nil
}

// teamsInboundObjects are the receiver's objects in removal order; the
// fence is at teamsInboundFenceIndex.
func (r *PlatformAgentReconciler) teamsInboundObjects(agent *agentv1alpha1.PlatformAgent) []a2aTeardownEntry {
	ns := agent.Namespace
	name := teamsInboundName(agent)
	return []a2aTeardownEntry{
		{&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}}, r.Client},
		{&corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}}, r.Client},
		{&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}}, r.Client},
		{&networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: teamsInboundNetpolName(agent), Namespace: ns}}, r.Client},
	}
}
