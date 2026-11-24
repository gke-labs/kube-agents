package controller

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	teamsInboundTestTenant = "759d68f0-65ed-44cd-8487-ce6221b72b2a"
	teamsInboundTestSecret = "custom-teams-secret"
)

// teamsInboundTestAgent is a next-mode agent with Teams on, a tenant, and
// the inbound receiver enabled.
func teamsInboundTestAgent() *agentv1alpha1.PlatformAgent {
	return withTeamsInboundOn(a2aTestAgent())
}

// withTeamsInboundOn is a copy of base with Teams, its tenant and the
// inbound receiver switched on, for walks over every rendered pod.
func withTeamsInboundOn(base *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
	agent := base.DeepCopy()
	if agent.Spec.Integration == nil {
		agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{}
	}
	agent.Spec.Integration.Teams = &agentv1alpha1.TeamsSpec{
		Enabled: ptr.To(true),
		AppIdSecretRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: teamsInboundTestSecret},
			Key:                  "teams-app-id",
		},
		AppPasswordSecretRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: teamsInboundTestSecret},
			Key:                  "teams-app-pwd",
		},
		TenantId: teamsInboundTestTenant,
		Inbound:  &agentv1alpha1.TeamsInboundSpec{Enabled: ptr.To(true)},
	}
	return agent
}

func TestTheReceiverRendersOnlyWithTeamsOnAnInboundSwitchAndATenant(t *testing.T) {
	if !teamsInboundEnabled(teamsInboundTestAgent()) {
		t.Fatal("the full fixture should enable the receiver")
	}
	for name, mutate := range map[string]func(*agentv1alpha1.PlatformAgent){
		"no integration":    func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration = nil },
		"teams off":         func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Teams.Enabled = ptr.To(false) },
		"no inbound":        func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Teams.Inbound = nil },
		"inbound off":       func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Teams.Inbound.Enabled = ptr.To(false) },
		"no tenant":         func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Teams.TenantId = "" },
		"tenant not a GUID": func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Teams.TenantId = "contoso.onmicrosoft.com" },
	} {
		t.Run(name, func(t *testing.T) {
			agent := teamsInboundTestAgent()
			mutate(agent)
			if teamsInboundEnabled(agent) {
				t.Fatal("the receiver should be off")
			}
			if teamsInboundBrokerEnv(agent) != nil || teamsInboundBrokerPeers(agent) != nil {
				t.Fatal("the broker should carry nothing for a receiver that is off")
			}
			if strings.Contains(allowedBrokerCallers(agent, allBrokerCallersOwned), teamsInboundNameSuffix) {
				t.Fatal("the receiver should not be an allowed caller while off")
			}
		})
	}
}

func TestBuildTeamsInboundDeployment(t *testing.T) {
	agent := teamsInboundTestAgent()
	dep := buildA2ATeamsInboundDeployment(agent)
	if dep.Name != "test-agent-teams-inbound" || dep.Namespace != "test-ns" {
		t.Fatalf("name/namespace = %s/%s", dep.Name, dep.Namespace)
	}
	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != "test-agent-teams-inbound" || ptr.Deref(pod.AutomountServiceAccountToken, true) {
		t.Fatalf("the receiver must run as its own ServiceAccount with no API server token mounted: %+v", pod.ServiceAccountName)
	}
	if len(pod.Containers) != 1 {
		t.Fatalf("containers = %d", len(pod.Containers))
	}
	c := pod.Containers[0]
	if !strings.HasSuffix(strings.Split(c.Image, ":")[0], "/"+a2aTeamsInboundImageName) {
		t.Fatalf("image = %s, want the %s image", c.Image, a2aTeamsInboundImageName)
	}
	env := map[string]corev1.EnvVar{}
	for _, e := range c.Env {
		env[e.Name] = e
	}
	if env[teamsInboundEnvListen].Value != ":8100" || env[teamsInboundEnvStatusListen].Value != ":9094" {
		t.Fatalf("listen env: %+v / %+v", env[teamsInboundEnvListen], env[teamsInboundEnvStatusListen])
	}
	if ref := env[teamsInboundEnvAppID].ValueFrom.SecretKeyRef; ref.Name != teamsInboundTestSecret || ref.Key != "teams-app-id" {
		t.Fatalf("the app id must come from the Teams Secret the broker reads: %+v", ref)
	}
	if env[teamsInboundEnvTenantID].Value != teamsInboundTestTenant {
		t.Fatalf("tenant env = %q", env[teamsInboundEnvTenantID].Value)
	}
	if env[teamsInboundEnvRelayURL].Value != credentialProxyBaseURL(agent) {
		t.Fatalf("relay env = %q", env[teamsInboundEnvRelayURL].Value)
	}
	if env[teamsInboundEnvTokenPath].Value != teamsInboundTokenDir+"/"+teamsInboundTokenKey {
		t.Fatalf("token path env = %q", env[teamsInboundEnvTokenPath].Value)
	}
	for _, name := range []string{"TEAMS_APP_PASSWORD", "NATS_URL", "NATS_PASSWORD"} {
		if _, ok := env[name]; ok {
			t.Fatalf("the receiver must hold no %s", name)
		}
	}
	ports := map[string]int32{}
	for _, p := range c.Ports {
		ports[p.Name] = p.ContainerPort
	}
	if ports[teamsInboundPortName] != teamsInboundPort || ports[teamsInboundStatusPortName] != teamsInboundStatusPort {
		t.Fatalf("ports = %v", ports)
	}
	if c.ReadinessProbe.HTTPGet.Path != teamsInboundReadyzPath || c.ReadinessProbe.HTTPGet.Port.IntVal != teamsInboundStatusPort {
		t.Fatalf("readiness probe = %+v", c.ReadinessProbe.HTTPGet)
	}
	if sc := c.SecurityContext; !ptr.Deref(sc.ReadOnlyRootFilesystem, false) || ptr.Deref(sc.AllowPrivilegeEscalation, true) {
		t.Fatalf("container security context = %+v", sc)
	}
	if !ptr.Deref(pod.SecurityContext.RunAsNonRoot, false) || ptr.Deref(pod.SecurityContext.RunAsUser, 0) != teamsInboundRunAsUser {
		t.Fatalf("pod security context = %+v", pod.SecurityContext)
	}
	if len(pod.Volumes) != 1 || pod.Volumes[0].Projected == nil {
		t.Fatalf("volumes = %+v, want the one projected token", pod.Volumes)
	}
	tok := pod.Volumes[0].Projected.Sources[0].ServiceAccountToken
	if tok.Audience != credentialProxyTeamsInboundAudience || tok.Path != teamsInboundTokenKey {
		t.Fatalf("projected token = %+v", tok)
	}
	if ptr.Deref(dep.Spec.Replicas, 0) != 1 {
		t.Fatalf("replicas = %d", ptr.Deref(dep.Spec.Replicas, 0))
	}
	if dep.Spec.Strategy.Type != "Recreate" {
		t.Fatalf("strategy = %q, want Recreate: the quota budgets no surge pod for the receiver", dep.Spec.Strategy.Type)
	}
	if c.WorkingDir != "/" {
		t.Fatalf("working dir = %q, want / on the distroless nonroot image under uid 1000", c.WorkingDir)
	}
	if pod.DNSConfig == nil || len(pod.DNSConfig.Options) == 0 || pod.DNSConfig.Options[0].Name != "ndots" {
		t.Fatalf("the receiver dials the broker and Microsoft by name and must carry the broker callers' ndots: %+v", pod.DNSConfig)
	}
	if _, reserved := reservedProfileServiceAccounts(agent)[teamsInboundName(agent)]; !reserved {
		t.Fatal("the receiver's ServiceAccount must be reserved against profiles and spec.security.serviceAccountName")
	}
}

func TestTheReceiverImageCanBeOverridden(t *testing.T) {
	t.Setenv(a2aTeamsInboundImageEnvVar, "registry.example/teams-inbound:dev")
	if got := buildA2ATeamsInboundDeployment(teamsInboundTestAgent()).Spec.Template.Spec.Containers[0].Image; got != "registry.example/teams-inbound:dev" {
		t.Fatalf("image = %s", got)
	}
}

func TestBuildTeamsInboundServiceAndNetworkPolicy(t *testing.T) {
	agent := teamsInboundTestAgent()
	svc := buildTeamsInboundService(agent)
	if svc.Spec.Type != corev1.ServiceTypeClusterIP || len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != teamsInboundPort {
		t.Fatalf("service = %+v", svc.Spec)
	}
	if svc.Spec.Selector["app"] != "test-agent-teams-inbound" {
		t.Fatalf("selector = %v", svc.Spec.Selector)
	}
	np := buildTeamsInboundNetworkPolicy(agent, []string{"10.0.0.10"})
	if np.Name != "test-agent-teams-inbound-netpol" {
		t.Fatalf("netpol name = %s", np.Name)
	}
	if len(np.Spec.PolicyTypes) != 2 {
		t.Fatalf("the fence must govern ingress and egress: %v", np.Spec.PolicyTypes)
	}
	if len(np.Spec.Ingress) != 2 || len(np.Spec.Ingress[0].From) != 0 || np.Spec.Ingress[0].Ports[0].Port.IntVal != teamsInboundPort {
		t.Fatalf("the public port must admit any source and nothing else must: %+v", np.Spec.Ingress)
	}
	if np.Spec.Ingress[1].Ports[0].Port.IntVal != teamsInboundStatusPort || np.Spec.Ingress[1].From[0].NamespaceSelector.MatchLabels[labelMetadataName] != gmpNamespace {
		t.Fatalf("the status port must admit the collector's namespace alone: %+v", np.Spec.Ingress[1])
	}
	var brokerRule, internetRule, dnsRule bool
	for _, rule := range np.Spec.Egress {
		for _, port := range rule.Ports {
			switch port.Port.IntVal {
			case credentialProxyPort:
				brokerRule = len(rule.To) == 1 && rule.To[0].PodSelector != nil
			case teamsInboundHTTPSPort:
				internetRule = len(rule.To) == 1 && rule.To[0].IPBlock != nil && rule.To[0].IPBlock.CIDR == teamsInboundAnyIPv4 && len(rule.To[0].IPBlock.Except) == 5 && rule.To[0].IPBlock.Except[0] == "10.0.0.0/8"
			case a2aDNSPort:
				dnsRule = len(rule.To) > 0
			}
		}
	}
	if !brokerRule || !internetRule || !dnsRule {
		t.Fatalf("egress must be DNS, the broker and 443 to the internet less the private ranges: broker=%v internet=%v dns=%v", brokerRule, internetRule, dnsRule)
	}
	for _, rule := range np.Spec.Egress {
		for _, to := range rule.To {
			if to.PodSelector != nil && to.PodSelector.MatchLabels["app"] == a2aGatewayName(agent) {
				t.Fatal("the receiver must not reach the gateway")
			}
		}
	}
}

func TestTheBrokerAdmitsTheReceiverOnItsOwnAudienceWhileItRuns(t *testing.T) {
	agent := teamsInboundTestAgent()
	callers := allowedBrokerCallers(agent, allBrokerCallersOwned)
	if !strings.Contains(callers, "system:serviceaccount:test-ns:test-agent-teams-inbound") {
		t.Fatalf("allowed callers = %s", callers)
	}
	env := map[string]string{}
	for _, e := range buildCredentialProxyContainer(agent).Env {
		env[e.Name] = e.Value
	}
	if env[credentialProxyTeamsInboundAudienceEnvVar] != credentialProxyTeamsInboundAudience {
		t.Fatalf("audience env = %q", env[credentialProxyTeamsInboundAudienceEnvVar])
	}
	if env[credentialProxyTeamsInboundCallersEnvVar] != "system:serviceaccount:test-ns:test-agent-teams-inbound" {
		t.Fatalf("callers env = %q", env[credentialProxyTeamsInboundCallersEnvVar])
	}
	var admitted bool
	for _, peer := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress[0].From {
		if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == "test-agent-teams-inbound" {
			admitted = true
		}
	}
	if !admitted {
		t.Fatal("the broker's fence must admit the receiver pod")
	}
	// Off, every one of those is gone.
	agent.Spec.Integration.Teams.Inbound = nil
	for _, e := range buildCredentialProxyContainer(agent).Env {
		if e.Name == credentialProxyTeamsInboundAudienceEnvVar || e.Name == credentialProxyTeamsInboundCallersEnvVar {
			t.Fatalf("%s rendered while the receiver is off", e.Name)
		}
	}
	for _, peer := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress[0].From {
		if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == "test-agent-teams-inbound" {
			t.Fatal("the broker's fence admits a receiver that is off")
		}
	}
}

func TestTheReceiverFenceRidesWithTheOthersOnlyWhileItRuns(t *testing.T) {
	// The fence is appended in reconcileA2ANetworkFences under the same
	// switch as the objects; the teardown list names it last so a
	// terminating pod is never unfenced.
	r := &PlatformAgentReconciler{}
	objs := r.teamsInboundObjects(teamsInboundTestAgent())
	if _, ok := objs[teamsInboundFenceIndex].obj.(*networkingv1.NetworkPolicy); !ok || len(objs) != teamsInboundFenceIndex+1 {
		t.Fatalf("the fence must be the last teardown entry: %T", objs[len(objs)-1].obj)
	}
	meta := objs[teamsInboundFenceIndex].obj.(metav1.Object)
	if meta.GetName() != "test-agent-teams-inbound-netpol" {
		t.Fatalf("fence name = %s", meta.GetName())
	}
}
