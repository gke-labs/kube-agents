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
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func splitBrokerAgent(split bool) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
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
<<<<<<< HEAD
			// Both chat relays are enabled because they share the broker's
			// listener, so their URLs have to follow it across the Pod
			// boundary. With them off, an assertion about those two variables
			// would be an assertion about nothing.
			Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
				GoogleChat: &agentv1alpha1.GoogleChatSpec{
					Enabled:          ptr.To(true),
					ProjectID:        "proj",
					TopicName:        "topic",
					SubscriptionName: "sub",
				},
				Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
			},
=======
>>>>>>> 735b41e (feat(operator): render the credential broker in its own Pod, behind a gate)
		},
	}
	if split {
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{SplitCredentialBrokerPod: ptr.To(true)}
	}
	return agent
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

// TestTheGateOffLeavesTheSidecarLayoutAlone is the cheap half of the
// compatibility guarantee; the byte-for-byte half is the golden fixture, which
// is regenerated from the same code path and is unchanged by this feature.
func TestTheGateOffLeavesTheSidecarLayoutAlone(t *testing.T) {
	pod := buildPodTemplateSpec(splitBrokerAgent(false), "c", "f", "s", "p", nil, renderOptions{})

<<<<<<< HEAD
	if _, found := findContainer(pod.Spec, "envoy-credential-proxy"); !found {
		t.Error("expected the credential proxy sidecar in the agent Pod (as native sidecar initContainer)")
=======
	if brokerContainerNamed(pod.Spec.Containers, "envoy-credential-proxy") == nil {
		t.Error("expected the credential proxy sidecar in the agent Pod")
>>>>>>> 735b41e (feat(operator): render the credential broker in its own Pod, behind a gate)
	}
	if brokerContainerNamed(pod.Spec.Containers, "agent-api-proxy") != nil {
		t.Error("the split-only API proxy container must not appear with the gate off")
	}
	agentContainer := brokerContainerNamed(pod.Spec.Containers, "platform-agent")
	if value, _ := brokerEnvValue(agentContainer.Env, "CREDENTIAL_PROXY_URL"); value != "http://127.0.0.1:8765" {
		t.Errorf("expected the loopback broker URL, got %q", value)
	}
	if _, found := brokerEnvValue(agentContainer.Env, "CREDENTIAL_PROXY_TOKEN_FILE"); found {
		t.Error("the agent must hold no broker credential in the sidecar layout")
	}
	if hasVolume(pod.Spec.Volumes, agentCredentialProxyTokenVolume) {
		t.Error("the agent token volume must not be projected with the gate off")
	}
	if !hasVolume(pod.Spec.Volumes, "credential-proxy-policy") {
		t.Error("the sidecar's own volumes must still be on the agent Pod with the gate off")
	}
}

func TestTheGateOnMovesTheBrokerOffTheAgentPod(t *testing.T) {
	agent := splitBrokerAgent(true)
	pod := buildPodTemplateSpec(agent, "c", "f", "s", "p", nil, renderOptions{})

<<<<<<< HEAD
	if _, found := findContainer(pod.Spec, "envoy-credential-proxy"); found {
=======
	if brokerContainerNamed(pod.Spec.Containers, "envoy-credential-proxy") != nil {
>>>>>>> 735b41e (feat(operator): render the credential broker in its own Pod, behind a gate)
		t.Error("the credential broker must not remain in the agent Pod")
	}
	apiProxy := brokerContainerNamed(pod.Spec.Containers, "agent-api-proxy")
	if apiProxy == nil {
		t.Fatal("the agent API front door must stay in the agent Pod")
	}
	if role, _ := brokerEnvValue(apiProxy.Env, "CREDENTIAL_PROXY_ROLE"); role != "api-proxy" {
		t.Errorf("expected the front door to run in the api-proxy role, got %q", role)
	}
	for _, forbidden := range []string{"CREDENTIAL_PROXY_POLICY", "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", "TOKEN_BROKER_URL"} {
		if _, found := brokerEnvValue(apiProxy.Env, forbidden); found {
			t.Errorf("the front door must carry no broker configuration, found %s", forbidden)
		}
	}
	if apiProxy.SecurityContext == nil || apiProxy.SecurityContext.RunAsUser == nil ||
		*apiProxy.SecurityContext.RunAsUser != credentialProxyUID {
		t.Error("the front door holds the external API key and must not run as the sandbox user")
	}

	agentContainer := brokerContainerNamed(pod.Spec.Containers, "platform-agent")
	wantURL := "http://test-agent-credential-proxy.test-ns.svc.cluster.local:8765"
<<<<<<< HEAD
	for _, name := range []string{"CREDENTIAL_PROXY_URL", "GOOGLE_CHAT_RELAY_URL", "SLACK_RELAY_URL"} {
		value, found := brokerEnvValue(agentContainer.Env, name)
		if !found {
			t.Errorf("expected %s to be set so the assertion below means something", name)
			continue
		}
		if value != wantURL {
=======
	for _, name := range []string{"CREDENTIAL_PROXY_URL", "GOOGLE_CHAT_RELAY_URL"} {
		if value, found := brokerEnvValue(agentContainer.Env, name); found && value != wantURL {
>>>>>>> 735b41e (feat(operator): render the credential broker in its own Pod, behind a gate)
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
	// The event-watcher container still mounts these, and it did not move.
	for _, name := range []string{"event-watcher-kubeconfig", "event-watcher-ksa-token"} {
		if !hasVolume(pod.Spec.Volumes, name) {
			t.Errorf("the agent Pod still needs the %s volume for its event watcher", name)
		}
	}
	// The broker's own volumes went with the broker.
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
	// Kubernetes API, or anything else in the cluster.
	if token.Audience != credentialProxyAudience {
		t.Errorf("expected audience %q, got %q", credentialProxyAudience, token.Audience)
	}
	if token.ExpirationSeconds == nil || *token.ExpirationSeconds > 3600 {
		t.Errorf("expected the token to expire within an hour, got %v", token.ExpirationSeconds)
	}
}

func TestTheBrokerPodAuthenticatesItsCallers(t *testing.T) {
	agent := splitBrokerAgent(true)
	deployment := buildCredentialBrokerDeployment(agent, "policy-hash", "/opt/data")

	if len(deployment.Spec.Template.Spec.Containers) != 1 {
		t.Fatalf("expected exactly one container in the broker Pod, got %d",
			len(deployment.Spec.Template.Spec.Containers))
	}
	broker := deployment.Spec.Template.Spec.Containers[0]

	want := map[string]string{
		"CREDENTIAL_PROXY_ROLE":            "broker",
		"CREDENTIAL_PROXY_AUTH_MODE":       "serviceaccount",
		"CREDENTIAL_PROXY_AUDIENCE":        credentialProxyAudience,
		"CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:test-ns:test-agent",
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
	if podSpec.SecurityContext == nil || podSpec.SecurityContext.RunAsUser == nil ||
		*podSpec.SecurityContext.RunAsUser != credentialProxyUID {
		t.Errorf("expected the broker Pod to run as %d, got %v", credentialProxyUID, podSpec.SecurityContext)
	}
	if podSpec.AutomountServiceAccountToken == nil || *podSpec.AutomountServiceAccountToken {
		t.Error("the broker's tokens are projected explicitly; automount must stay off")
	}
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 1 {
		t.Error("two brokers would race over the shared workspace")
	}
	if !hasVolume(podSpec.Volumes, "platform-agent-data-vol") {
		t.Error("the broker runs commands in the agent's workspace and must mount it")
	}
	var mountsWorkspace bool
	for _, mount := range broker.VolumeMounts {
		if mount.Name == "platform-agent-data-vol" && mount.MountPath == "/opt/data" && !mount.ReadOnly {
			mountsWorkspace = true
		}
	}
	if !mountsWorkspace {
		t.Error("the workspace must be mounted read-write at the agent's own home path")
	}
}

func TestTheBrokerServiceAddressesTheBrokerPod(t *testing.T) {
	service := buildCredentialBrokerService(splitBrokerAgent(true))
	if service.Spec.Selector["app"] != "test-agent-credential-proxy" {
		t.Errorf("unexpected selector %v", service.Spec.Selector)
	}
	if len(service.Spec.Ports) != 1 || service.Spec.Ports[0].Port != credentialProxyPort {
		t.Errorf("unexpected ports %v", service.Spec.Ports)
	}
}

// TestAPluginCannotDisableCallerAuthentication guards the reserved list. A
// plugin that could set CREDENTIAL_PROXY_AUTH_MODE could turn the check off,
// and one that could set CREDENTIAL_PROXY_ALLOWED_CALLERS could add itself.
func TestAPluginCannotDisableCallerAuthentication(t *testing.T) {
	agent := splitBrokerAgent(true)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_AUTH_MODE", Value: "none"},
		{Name: "CREDENTIAL_PROXY_ALLOWED_CALLERS", Value: "system:serviceaccount:evil:evil"},
		{Name: "CREDENTIAL_PROXY_AUDIENCE", Value: "https://kubernetes.default.svc"},
		{Name: "CREDENTIAL_PROXY_ENVOY_ADDRESS", Value: "127.0.0.1"},
		{Name: "CREDENTIAL_PROXY_ROLE", Value: "api-proxy"},
	}}

	envVars := buildCredentialProxyEnv(agent)
	expected := map[string]string{
		"CREDENTIAL_PROXY_AUTH_MODE":       "serviceaccount",
		"CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:test-ns:test-agent",
		"CREDENTIAL_PROXY_AUDIENCE":        credentialProxyAudience,
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

func newSplitReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, objects ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	all := append([]client.Object{agent}, objects...)
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(all...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(ssaApplyInterceptor()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

func TestReconcileRendersAndKeepsTheBrokerPod(t *testing.T) {
	agent := splitBrokerAgent(true)
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
	if err := cl.Get(ctx, roleKey, &rbacv1.ClusterRoleBinding{}); err != nil {
		t.Errorf("the broker's TokenReview ClusterRoleBinding must exist: %v", err)
	}
}

func TestReconcileRemovesTheBrokerPodWhenTheGateIsOff(t *testing.T) {
	agent := splitBrokerAgent(false)
	ownerReference := metav1.OwnerReference{
		APIVersion: agentv1alpha1.GroupVersion.String(),
		Kind:       "PlatformAgent",
		Name:       agent.Name,
		UID:        agent.UID,
		Controller: ptr.To(true),
	}
	managed := map[string]string{labelManagedBy: fieldOwner}
	leftovers := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
			Name: "test-agent-credential-proxy", Namespace: "test-ns",
			OwnerReferences: []metav1.OwnerReference{ownerReference},
		}},
		&corev1.Service{ObjectMeta: metav1.ObjectMeta{
			Name: "test-agent-credential-proxy", Namespace: "test-ns",
			OwnerReferences: []metav1.OwnerReference{ownerReference},
		}},
		&rbacv1.ClusterRole{ObjectMeta: metav1.ObjectMeta{
			Name: "kubeagents:tokenreview:test-ns:test-agent", Labels: managed,
		}},
		&rbacv1.ClusterRoleBinding{ObjectMeta: metav1.ObjectMeta{
			Name: "kubeagents:tokenreview:test-ns:test-agent", Labels: managed,
		}},
	}
	r, cl := newSplitReconciler(t, agent, leftovers...)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}

	for _, object := range leftovers {
		err := cl.Get(ctx, client.ObjectKeyFromObject(object), object)
		if !errors.IsNotFound(err) {
			t.Errorf("turning the gate off must remove %T %s, got %v",
				object, object.GetName(), err)
		}
	}
}

// TestTheBrokerPodIsNotDeletedByTheLegacyCleanup pins the interaction that
// broke the two-pod layout last time: the cleanup pass ran after the workload
// pass and removed what it had just created.
func TestTheBrokerPodIsNotDeletedByTheLegacyCleanup(t *testing.T) {
	agent := splitBrokerAgent(true)
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
