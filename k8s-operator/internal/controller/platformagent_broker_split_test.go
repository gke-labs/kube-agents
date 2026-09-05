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
	"net/url"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// brokerPodAgent is a CR in the only layout the operator renders: the credential
// broker in a Pod of its own, the shell in the sandbox, and the gateway Pod
// holding neither.
func brokerPodAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			UID:        types.UID("agent-uid"),
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   "proj",
				Location:    "us-central1",
				ClusterName: "cluster",
			},
			// Both chat relays are enabled because they are hosted in the
			// broker's process, so their URLs have to address its Service. With
			// them off, an assertion about those two variables would be an
			// assertion about nothing.
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Enabled:          ptr.To(true),
					ProjectID:        "proj",
					TopicName:        "topic",
					SubscriptionName: "sub",
				},
				Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
			},
		},
	}
}

func brokerContainerNamed(containers []corev1.Container, name string) *corev1.Container {
	for index := range containers {
		if containers[index].Name == name {
			return &containers[index]
		}
	}
	return nil
}

func brokerEnvValue(envVars []corev1.EnvVar, name string) (string, bool) {
	for _, env := range envVars {
		if env.Name == name {
			return env.Value, true
		}
	}
	return "", false
}

func hasVolume(volumes []corev1.Volume, name string) bool {
	for _, volume := range volumes {
		if volume.Name == name {
			return true
		}
	}
	return false
}

// TestTheBrokerIsNotInTheAgentPod is the containment property the whole design
// rests on: nothing in the gateway Pod mints a credential, and nothing in it
// holds the configuration that would let it.
func TestTheBrokerIsNotInTheAgentPod(t *testing.T) {
	agent := brokerPodAgent()
	pod := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{})

	if _, found := findContainer(pod.Spec, "envoy-credential-proxy"); found {
		t.Error("the credential broker must not be a container of the agent Pod")
	}
	frontDoor, found := findContainer(pod.Spec, "agent-api-auth")
	if !found {
		t.Fatal("the agent API front door must stay in the agent Pod")
	}
	if role, _ := brokerEnvValue(frontDoor.Env, "CREDENTIAL_PROXY_ROLE"); role != "api-proxy" {
		t.Errorf("expected the front door to run in the api-proxy role, got %q", role)
	}
	for _, forbidden := range []string{"CREDENTIAL_PROXY_POLICY", "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", "TOKEN_BROKER_URL"} {
		if _, found := brokerEnvValue(frontDoor.Env, forbidden); found {
			t.Errorf("the front door must carry no broker configuration, found %s", forbidden)
		}
	}

	agentContainer := brokerContainerNamed(pod.Spec.Containers, "platform-agent")
	// CREDENTIAL_PROXY_URL is emitted empty: the agent runs no wrapped CLI, so an
	// address for the broker in its environment is an invitation and not a
	// dependency. Emitted rather than omitted so an AgentPlugin cannot be the
	// only writer of the name — see buildPodTemplateSpec. The two relay URLs are
	// different; they are polled by the chat clients the agent container does run.
	if value, found := brokerEnvValue(agentContainer.Env, "CREDENTIAL_PROXY_URL"); !found || value != "" {
		t.Errorf("the agent container must not be told where the broker is, got %q (present=%v)", value, found)
	}
	wantURL := "http://test-agent-credential-proxy.test-ns.svc.cluster.local:8765"
	for _, name := range []string{"GOOGLE_CHAT_RELAY_URL", "SLACK_RELAY_URL"} {
		value, found := brokerEnvValue(agentContainer.Env, name)
		if !found {
			t.Errorf("expected %s to be set so the assertion below means something", name)
			continue
		}
		if value != wantURL {
			t.Errorf("expected %s to address the broker Service, got %q", name, value)
		}
	}
	if value, _ := brokerEnvValue(agentContainer.Env, "CREDENTIAL_PROXY_TOKEN_FILE"); value != credentialProxyTokenMountPath+"/token" {
		t.Errorf("expected the agent to present a projected token, got %q", value)
	}

	var mounted bool
	for _, mount := range agentContainer.VolumeMounts {
		if mount.Name == agentCredentialProxyTokenVolume {
			mounted = true
			if !mount.ReadOnly {
				t.Error("the agent's broker token must be mounted read-only")
			}
		}
	}
	if !mounted {
		t.Error("the agent container must mount the token it is told to send")
	}
	if !hasVolume(pod.Spec.Volumes, agentCredentialProxyTokenVolume) {
		t.Error("the agent Pod must project the broker token")
	}
	// The broker's own volumes went with the broker. Each one holds something
	// the agent must not read, so a leftover declaration here is the start of a
	// leftover mount.
	for _, name := range []string{"credential-proxy-policy", "credential-proxy-state", "credential-proxy-runtime"} {
		if hasVolume(pod.Spec.Volumes, name) {
			t.Errorf("volume %s belongs to the broker Pod, not the agent Pod", name)
		}
	}
}

func TestTheAgentTokenIsAudienceBoundAndShortLived(t *testing.T) {
	volume := buildAgentCredentialProxyTokenVolume()
	projection := volume.VolumeSource.Projected
	if projection == nil || len(projection.Sources) != 1 {
		t.Fatalf("expected a single projected ServiceAccount token, got %+v", volume.VolumeSource)
	}
	token := projection.Sources[0].ServiceAccountToken
	if token == nil {
		t.Fatal("expected a ServiceAccountToken projection")
	}
	// The audience is what stops this token being replayed against the
	// Kubernetes API, or anything else in the cluster. The *chat* audience
	// specifically: it is also what stops this token opening the shell's
	// routes on the broker, which is the whole of the caller separation —
	// both Pods run as ServiceAccounts the broker will serve, so the username
	// cannot do it.
	if token.Audience != credentialProxyChatAudience {
		t.Errorf("expected audience %q, got %q", credentialProxyChatAudience, token.Audience)
	}
	if token.Audience == credentialProxyAudience {
		t.Error("the gateway must not be handed the sandbox's audience")
	}
	if token.ExpirationSeconds == nil || *token.ExpirationSeconds > 3600 {
		t.Errorf("expected the token to expire within an hour, got %v", token.ExpirationSeconds)
	}
}

func TestTheSandboxAndGatewayTokensNameDifferentAudiences(t *testing.T) {
	// The separation is only real while these two differ, and both are string
	// constants a later edit could quietly reconcile.
	gateway := buildAgentCredentialProxyTokenVolume().
		VolumeSource.Projected.Sources[0].ServiceAccountToken
	sandbox := buildShellSandboxCredentialProxyTokenVolume().
		VolumeSource.Projected.Sources[0].ServiceAccountToken

	if gateway.Audience == sandbox.Audience {
		t.Fatalf("the two Pods must not share an audience, both got %q", gateway.Audience)
	}
	if sandbox.Audience != credentialProxyAudience {
		t.Errorf("expected the sandbox audience %q, got %q",
			credentialProxyAudience, sandbox.Audience)
	}
}

func TestTheBrokerPodAuthenticatesItsCallers(t *testing.T) {
	agent := brokerPodAgent()
	deployment := buildCredentialProxyDeployment(agent, "policy-hash")

	if len(deployment.Spec.Template.Spec.Containers) != 1 {
		t.Fatalf("expected exactly one container in the broker Pod, got %d",
			len(deployment.Spec.Template.Spec.Containers))
	}
	broker := deployment.Spec.Template.Spec.Containers[0]

	// Both callers are named: the gateway's chat clients present the agent's
	// ServiceAccount token, the sandbox's wrapped CLIs present the sandbox's.
	want := map[string]string{
		"CREDENTIAL_PROXY_ROLE":            "broker",
		"CREDENTIAL_PROXY_AUTH_MODE":       "serviceaccount",
		"CREDENTIAL_PROXY_AUDIENCE":        credentialProxyAudience,
		"CREDENTIAL_PROXY_CHAT_AUDIENCE":   credentialProxyChatAudience,
		"CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:test-ns:test-agent,system:serviceaccount:test-ns:test-agent-shell",
		"CREDENTIAL_PROXY_ENVOY_ADDRESS":   "0.0.0.0",
	}
	for name, expected := range want {
		if value, _ := brokerEnvValue(broker.Env, name); value != expected {
			t.Errorf("expected %s=%q, got %q", name, expected, value)
		}
	}
	// Without these the TokenReview call cannot be made, and every request
	// would be refused.
	if _, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_KUBE_CA_FILE"); !found {
		t.Error("the broker needs the cluster CA to verify a token")
	}
	if _, found := brokerEnvValue(broker.Env, "API_SERVER_EXTERNAL_KEY"); found {
		t.Error("the external API key stayed with the front door; the broker must not hold it")
	}

	var mountsAPIAccess bool
	for _, mount := range broker.VolumeMounts {
		if mount.MountPath == kubeAPIAccessMountPath {
			mountsAPIAccess = true
		}
	}
	if !mountsAPIAccess {
		t.Error("the broker must mount a default-audience token to call TokenReview")
	}

	podSpec := deployment.Spec.Template.Spec
	if podSpec.SecurityContext == nil || podSpec.SecurityContext.RunAsNonRoot == nil ||
		!*podSpec.SecurityContext.RunAsNonRoot {
		t.Errorf("the broker Pod may not run as root, got %v", podSpec.SecurityContext)
	}
	if podSpec.AutomountServiceAccountToken == nil || *podSpec.AutomountServiceAccountToken {
		t.Error("the broker's tokens are projected explicitly; automount must stay off")
	}
	// The workspace is emphatically not here. A ReadWriteOnce claim cannot be
	// mounted by a second Pod at all, and mounting it would put the broker's
	// working tree where the shell can read it.
	if hasVolume(podSpec.Volumes, "platform-agent-data-vol") {
		t.Error("the broker Pod must not mount the agent's workspace claim")
	}
	if value, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_WORKSPACE_ROOT"); found {
		t.Errorf("the broker has no shared workspace, so naming one points it at a path that is not there: %q", value)
	}
	if value, _ := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_CONTENT_WORKSPACE"); value != "1" {
		t.Errorf("with no shared filesystem, content has to move over the workspace API; got %q", value)
	}
}

// TestTheBrokerMountsEveryPathItsEnvironmentNames catches the failure that has
// no symptom until a skill runs: an environment variable pointing at a file
// that no volume supplies. The container starts, and the GitOps or
// scoped-identity path fails at the first read.
func TestTheBrokerMountsEveryPathItsEnvironmentNames(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{ScopedServiceAccounts: []agentv1alpha1.ScopedServiceAccount{{
		ProjectID:           "proj",
		Location:            "us-central1",
		ClusterName:         "cluster",
		ServiceAccountEmail: "scoped-agent@proj.iam.gserviceaccount.com",
	}}}
	container := buildCredentialProxyContainer(agent)
	volumes := buildCredentialProxyRuntimeVolumes(agent)

	for _, name := range []string{"GITOPS_STATE_PATH", "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE", "CREDENTIAL_PROXY_POLICY", "KUBECONFIG", "KSA_TOKEN_FILE"} {
		value, found := brokerEnvValue(container.Env, name)
		if !found {
			t.Errorf("expected %s to be set", name)
			continue
		}
		mount := mountCovering(&container, value)
		if mount == nil {
			t.Errorf("%s=%s is not supplied by any mount in the broker container", name, value)
			continue
		}
		if !hasVolume(volumes, mount.Name) {
			t.Errorf("%s=%s is mounted from volume %q, which the broker Pod does not declare", name, value, mount.Name)
		}
	}
}

// TestTheScopedPoolMountFollowsTheKey pins the pair that has to move together.
// The mount is a SubPath, so naming a key the ConfigMap does not carry leaves
// the container unable to start.
func TestTheScopedPoolMountFollowsTheKey(t *testing.T) {
	agent := brokerPodAgent()
	for _, mount := range buildCredentialProxyVolumeMounts(agent) {
		if mount.SubPath == scopedSAPoolKey {
			t.Error("the scoped-pool SubPath mount must be absent when the ConfigMap carries no such key")
		}
	}
}

func TestTheBrokerServiceAddressesTheBrokerPod(t *testing.T) {
	service := buildCredentialProxyService(brokerPodAgent())
	if service.Spec.Selector["app"] != "test-agent-credential-proxy" {
		t.Errorf("unexpected selector %v", service.Spec.Selector)
	}
	if len(service.Spec.Ports) != 1 || service.Spec.Ports[0].Port != credentialProxyPort {
		t.Errorf("unexpected ports %v", service.Spec.Ports)
	}
}

// TestTheGatewayMayReachTheRelayItIsPointedAt asserts the halves of the pair
// against each other rather than each against a constant. The relay moved into
// the broker's pod, so the two RELAY_URL variables became a cross-pod call and
// the default gateway policy needed an egress rule it did not have. Both
// policies read as though they permitted the call — the broker's ingress named
// the gateway — and the pods stayed Running with the CR Ready while every chat
// pull was dropped on an enforcing dataplane. That is rule 11's failure a second
// time, which is why this checks the URL the agent is actually given rather
// than a port somebody remembered to write down.
func TestTheGatewayMayReachTheRelayItIsPointedAt(t *testing.T) {
	agent := brokerPodAgent()
	pod := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{})
	agentContainer := brokerContainerNamed(pod.Spec.Containers, "platform-agent")

	relay, found := brokerEnvValue(agentContainer.Env, "SLACK_RELAY_URL")
	if !found || relay == "" {
		t.Fatal("SLACK_RELAY_URL is unset, so this test asserts nothing")
	}
	target, err := url.Parse(relay)
	if err != nil {
		t.Fatalf("SLACK_RELAY_URL is not a URL: %v", err)
	}
	if target.Hostname() == "127.0.0.1" || target.Hostname() == "localhost" {
		t.Fatalf("the relay is back on loopback (%q); this test and the rule it "+
			"guards both assume it is a cross-pod call", relay)
	}
	port, err := strconv.ParseInt(target.Port(), 10, 32)
	if err != nil {
		t.Fatalf("SLACK_RELAY_URL names no port: %v", err)
	}

	gateway := buildNetworkPolicy(agent, nil, defaultTestNetpolProfile(), false, "", false)
	if !allowsPeerOnPort(gateway, agent.Namespace, credentialProxySelector(agent), int32(port)) {
		t.Errorf("the gateway policy has no egress rule reaching %s on %d — the chat "+
			"clients poll the relay there and an enforcing dataplane drops every "+
			"request while the agent reads Ready", relay, port)
	}

	// The other half, so a fix that deleted the ingress rule instead would not
	// pass by making both sides equally wrong.
	broker := buildCredentialProxyNetworkPolicy(agent)
	var admitted bool
	for _, rule := range broker.Spec.Ingress {
		for _, peer := range rule.From {
			if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == agent.Name+"-gateway" {
				admitted = true
			}
		}
	}
	if !admitted {
		t.Error("the broker policy no longer admits the gateway; the pair is one-sided again")
	}
}

// TestAPluginCannotDisableCallerAuthentication guards the reserved list. A
// plugin that could set CREDENTIAL_PROXY_AUTH_MODE could turn the check off,
// and one that could set CREDENTIAL_PROXY_ALLOWED_CALLERS could add itself.
func TestAPluginCannotDisableCallerAuthentication(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_AUTH_MODE", Value: "none"},
		{Name: "CREDENTIAL_PROXY_ALLOWED_CALLERS", Value: "system:serviceaccount:evil:evil"},
		{Name: "CREDENTIAL_PROXY_AUDIENCE", Value: "https://kubernetes.default.svc"},
		// Setting this to the shell audience would collapse the two roles back
		// into one and hand the gateway's token the exec routes.
		{Name: "CREDENTIAL_PROXY_CHAT_AUDIENCE", Value: credentialProxyAudience},
		{Name: "CREDENTIAL_PROXY_ENVOY_ADDRESS", Value: "127.0.0.1"},
		{Name: "CREDENTIAL_PROXY_ROLE", Value: "api-proxy"},
	}}

	envVars := buildCredentialProxyEnv(agent)
	expected := map[string]string{
		"CREDENTIAL_PROXY_AUTH_MODE":       "serviceaccount",
		"CREDENTIAL_PROXY_ALLOWED_CALLERS": allowedBrokerCallers(agent),
		"CREDENTIAL_PROXY_AUDIENCE":        credentialProxyAudience,
		"CREDENTIAL_PROXY_CHAT_AUDIENCE":   credentialProxyChatAudience,
		"CREDENTIAL_PROXY_ENVOY_ADDRESS":   "0.0.0.0",
		"CREDENTIAL_PROXY_ROLE":            "broker",
	}
	for name, want := range expected {
		var seen []string
		for _, env := range envVars {
			if env.Name == name {
				seen = append(seen, env.Value)
			}
		}
		if len(seen) != 1 || seen[0] != want {
			t.Errorf("expected %s to be exactly [%q], got %v", name, want, seen)
		}
	}
}

// shellSandboxKeysSecret is the authorized-keys Secret every install surface
// generates, as a fixture. A reconcile that cannot find it reports
// Degraded/ShellSandboxKeysMissing, so a test about anything else has to seed it
// or it is testing a broken install.
func shellSandboxKeysSecret(agent *agentv1alpha1.PlatformAgent) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      shellSandboxAuthorizedKeysSecretName(agent),
			Namespace: agent.Namespace,
		},
		StringData: map[string]string{"authorized_keys": "ssh-ed25519 AAAAC3Nz test@fixture"},
	}
}

func newSplitReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, objects ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	all := append([]client.Object{agent, shellSandboxKeysSecret(agent)}, objects...)
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(all...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func TestReconcileRendersAndKeepsTheBrokerPod(t *testing.T) {
	agent := brokerPodAgent()
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}

	// Twice. The bug this replaces was a controller that created objects and
	// then deleted them on the following reconcile.
	for pass := 0; pass < 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile pass %d failed: %v", pass, err)
		}
	}

	key := types.NamespacedName{Name: "test-agent-credential-proxy", Namespace: "test-ns"}
	if err := cl.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Errorf("the broker Deployment must survive repeated reconciles: %v", err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Errorf("the broker Service must survive repeated reconciles: %v", err)
	}
	roleKey := types.NamespacedName{Name: "kubeagents:tokenreview:test-ns:test-agent"}
	if err := cl.Get(ctx, roleKey, &rbacv1.ClusterRole{}); err != nil {
		t.Errorf("the broker's TokenReview ClusterRole must exist: %v", err)
	}
	binding := &rbacv1.ClusterRoleBinding{}
	if err := cl.Get(ctx, roleKey, binding); err != nil {
		t.Fatalf("the broker's TokenReview ClusterRoleBinding must exist: %v", err)
	}
	// The binding has to name the identity the broker itself runs as — not the
	// sandbox's, which is a caller of the broker and not the thing asking the
	// question.
	want := agentServiceAccountName(agent)
	if len(binding.Subjects) != 1 || binding.Subjects[0].Name != want {
		t.Errorf("the binding must name the ServiceAccount the broker runs as (%s), got %+v", want, binding.Subjects)
	}
}

// TestTheAgentPodDeclaresNoVolumeItDoesNotMount catches the class the
// event-watcher volumes fell into: the broker's volume list was pruned by hand
// when it left, and entries survived that nothing in the agent Pod mounts any
// more, because the container that used them went with it.
func TestTheAgentPodDeclaresNoVolumeItDoesNotMount(t *testing.T) {
	spec := buildPodTemplateSpec(
		brokerPodAgent(), "c", "f", "s", "p", nil,
		renderOptions{imageVolumeSupported: true},
	).Spec

	mounted := map[string]bool{}
	for _, container := range append(append([]corev1.Container{}, spec.Containers...), spec.InitContainers...) {
		for _, mount := range container.VolumeMounts {
			mounted[mount.Name] = true
		}
	}
	for _, volume := range spec.Volumes {
		if !mounted[volume.Name] {
			t.Errorf("volume %q is declared and never mounted", volume.Name)
		}
	}
}

// TestTheBrokerPodDeclaresNoVolumeItDoesNotMount is the same rule on the other
// Pod, and it is the one that would have caught the gitops-state mount going
// missing when the broker left the agent's Pod.
func TestTheBrokerPodDeclaresNoVolumeItDoesNotMount(t *testing.T) {
	agent := brokerPodAgent()
	spec := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec

	mounted := map[string]bool{}
	for _, container := range append(append([]corev1.Container{}, spec.Containers...), spec.InitContainers...) {
		for _, mount := range container.VolumeMounts {
			mounted[mount.Name] = true
		}
	}
	for _, volume := range spec.Volumes {
		if !mounted[volume.Name] {
			t.Errorf("volume %q is declared and never mounted", volume.Name)
		}
	}
}

// TestTheSandboxCannotBeSwitchedOff pins the refusal. The CRD field survives so
// that an install carrying `enabled: true` still applies, and so that the one
// value the operator cannot honour is answered rather than ignored.
func TestTheSandboxCannotBeSwitchedOff(t *testing.T) {
	for _, testCase := range []struct {
		name    string
		enabled *bool
		refused bool
	}{
		{"absent", nil, false},
		{"explicitly on", ptr.To(true), false},
		{"explicitly off", ptr.To(false), true},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			agent := brokerPodAgent()
			if testCase.enabled != nil {
				agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
					ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: testCase.enabled},
				}
			}
			reason, msg := validateShellSandbox(agent)
			if !testCase.refused {
				if reason != "" {
					t.Fatalf("expected no refusal, got %s: %s", reason, msg)
				}
				return
			}
			if reason != reasonShellSandboxCannotBeDisabled {
				t.Fatalf("expected %s, got %q", reasonShellSandboxCannotBeDisabled, reason)
			}
			// The message has to name the field, because it is the only thing
			// the operator sees and it is the way out.
			if !strings.Contains(msg, "spec.harness.experimental.shellSandbox.enabled") {
				t.Errorf("the refusal must name the field; got %q", msg)
			}
		})
	}
}

// TestReconcileRefusesADisabledSandboxBeforeRenderingAnything is the same rule
// at the reconcile level. A validator that returned the right string while
// Reconcile went on to render the agent anyway would be a refusal in name only.
func TestReconcileRefusesADisabledSandboxBeforeRenderingAnything(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
		ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
	}
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("a refusal is a Degraded status, not a reconcile error: %v", err)
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("re-reading the agent failed: %v", err)
	}
	if updated.Status.Phase != "Degraded" {
		t.Errorf("expected phase Degraded, got %q", updated.Status.Phase)
	}
	ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if ready == nil || ready.Reason != reasonShellSandboxCannotBeDisabled {
		t.Errorf("expected Ready=False/%s, got %+v", reasonShellSandboxCannotBeDisabled, ready)
	}

	// Nothing rendered. The refusal is before the workload, so this covers the
	// agent Deployment as well as the broker's and the sandbox's.
	for _, object := range []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-credential-proxy", Namespace: "test-ns"}},
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-shell", Namespace: "test-ns"}},
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-gateway", Namespace: "test-ns"}},
	} {
		if err := cl.Get(ctx, client.ObjectKeyFromObject(object), object); !errors.IsNotFound(err) {
			t.Errorf("a refused spec must render no %T %s, got %v", object, object.GetName(), err)
		}
	}
}

// TestARefusalStillReconcilesTheAgentsNetworkPolicies is the rule step 11e of
// Reconcile states and steps 9b and 9c now keep: a refusal withholds the
// workload, and it must not also withhold a guardrail. A NetworkPolicy that
// stops being reconciled is one an operator can delete permanently, and with
// nothing selecting the agent Pod, NetworkPolicy permits all egress — behind a
// Degraded status that names something else entirely.
func TestARefusalStillReconcilesTheAgentsNetworkPolicies(t *testing.T) {
	for _, testCase := range []struct {
		name   string
		mutate func(*agentv1alpha1.PlatformAgent)
		reason string
	}{
		{
			name: "a disabled sandbox",
			mutate: func(agent *agentv1alpha1.PlatformAgent) {
				agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
					ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
				}
			},
			reason: reasonShellSandboxCannotBeDisabled,
		},
		{
			name: "a forbidden volume mount",
			mutate: func(agent *agentv1alpha1.PlatformAgent) {
				agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
					ExtraVolumeMounts: []corev1.VolumeMount{
						{Name: "credential-proxy-state", MountPath: "/var/lib/credential-proxy"},
					},
				}
			},
			reason: "ForbiddenVolumeMount",
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			agent := brokerPodAgent()
			testCase.mutate(agent)
			r, cl := newSplitReconciler(t, agent)
			ctx := context.Background()
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}

			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("a refusal is a Degraded status, not a reconcile error: %v", err)
			}

			updated := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
				t.Fatalf("re-reading the agent failed: %v", err)
			}
			ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
			if ready == nil || ready.Reason != testCase.reason {
				t.Fatalf("expected Ready=False/%s, got %+v", testCase.reason, ready)
			}

			policy := &networkingv1.NetworkPolicy{}
			key := types.NamespacedName{Name: agent.Name + "-gateway-netpol", Namespace: agent.Namespace}
			if err := cl.Get(ctx, key, policy); err != nil {
				t.Fatalf("the refusal withheld %s, leaving the agent Pod's egress unrestricted: %v",
					key.Name, err)
			}
			if len(policy.Spec.Egress) == 0 {
				t.Error("the reconciled policy has no egress rules, which permits nothing and is not what this renders")
			}
		})
	}
}

// TestAMissingSandboxKeypairIsReportedRatherThanRendered covers the install that
// supplied no keypair. The pod then sits in ContainerCreating on a mount error
// kubelet reports only as an event on the pod, so the CR is where an operator
// has to be able to read it.
//
// Everything is still rendered, unlike the refusals above: the StatefulSet is
// wanted in place so the pod starts by itself once the Secret appears, and the
// gateway is wanted so the operator can be told what is wrong over chat.
func TestAMissingSandboxKeypairIsReportedRatherThanRendered(t *testing.T) {
	agent := brokerPodAgent()
	scheme := setupScheme()
	// Not newSplitReconciler, which seeds the Secret this test is about.
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}

	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("a missing Secret is a Degraded status, not a reconcile error: %v", err)
	}
	// Secrets are not watched, so nothing wakes this reconcile when one is
	// created. Without the requeue the agent stays Degraded after the fix.
	if result.RequeueAfter == 0 {
		t.Error("expected a requeue: creating the Secret is not an event this controller sees")
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("re-reading the agent failed: %v", err)
	}
	if updated.Status.Phase != "Degraded" {
		t.Errorf("expected phase Degraded, got %q", updated.Status.Phase)
	}
	ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready")
	if ready == nil || ready.Reason != reasonShellSandboxKeysMissing {
		t.Fatalf("expected Ready=False/%s, got %+v", reasonShellSandboxKeysMissing, ready)
	}
	// The message is the whole value of the condition, so it has to name the
	// object that is missing and a way to create it.
	for _, want := range []string{"test-agent-shell-authorized-keys", "SANDBOX_SSH_PUBLIC_KEY", "upgrade.sh"} {
		if !strings.Contains(ready.Message, want) {
			t.Errorf("the message must mention %q; got %q", want, ready.Message)
		}
	}

	for _, object := range []client.Object{
		&appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-shell", Namespace: "test-ns"}},
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-gateway", Namespace: "test-ns"}},
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-credential-proxy", Namespace: "test-ns"}},
	} {
		if err := cl.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
			t.Errorf("a missing keypair must withhold nothing; %T %s: %v", object, object.GetName(), err)
		}
	}
}

// TestTheBrokerPodIsNotDeletedByTheLegacyCleanup pins the interaction that
// broke the two-pod layout last time: the cleanup pass ran after the workload
// pass and removed what it had just created.
func TestTheBrokerPodIsNotDeletedByTheLegacyCleanup(t *testing.T) {
	agent := brokerPodAgent()
	ownerReference := metav1.OwnerReference{
		APIVersion: agentv1alpha1.GroupVersion.String(),
		Kind:       "PlatformAgent",
		Name:       agent.Name,
		UID:        agent.UID,
		Controller: ptr.To(true),
	}
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: "test-agent-credential-proxy", Namespace: "test-ns",
		OwnerReferences: []metav1.OwnerReference{ownerReference},
	}}
	service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Name: "test-agent-credential-proxy", Namespace: "test-ns",
		OwnerReferences: []metav1.OwnerReference{ownerReference},
	}}
	r, cl := newSplitReconciler(t, agent, deployment, service)
	ctx := context.Background()

	if err := r.deleteLegacyCredentialIsolationResources(ctx, agent); err != nil {
		t.Fatalf("legacy cleanup failed: %v", err)
	}
	for _, object := range []client.Object{deployment, service} {
		if err := cl.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
			t.Errorf("the legacy cleanup must not touch the broker's own objects: %v", err)
		}
	}
}
