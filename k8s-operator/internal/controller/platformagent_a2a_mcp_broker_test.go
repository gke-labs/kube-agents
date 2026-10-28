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
	"path"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// mcpBrokerAgent is a mode-next agent with Chat off, so the callers list is
// about the MCP flag alone.
func mcpBrokerAgent() *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	agent.Spec.Mode = ptr.To("next")
	if agent.Spec.Integration != nil {
		agent.Spec.Integration.GoogleChat = nil
	}
	return agent
}

func TestTheMCPPodCarriesTheBrokerTokenOnlyUnderItsFlag(t *testing.T) {
	agent := a2aTestAgent()

	t.Setenv(a2aMCPDoorEnvVar, "")
	off := buildA2AMCPDeployment(agent).Spec.Template.Spec
	if off.ServiceAccountName != "" {
		t.Errorf("flag off: serviceAccountName = %q, want none", off.ServiceAccountName)
	}
	for _, v := range off.Volumes {
		if v.Name == a2aMCPBrokerTokenVolume || v.Projected != nil {
			t.Errorf("flag off: volume %s rendered", v.Name)
		}
	}
	for _, e := range off.Containers[0].Env {
		if e.Name == a2aMCPBrokerURLEnvVar || e.Name == a2aMCPBrokerTokenFileEnvVar {
			t.Errorf("flag off: env %s rendered", e.Name)
		}
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	pod := buildA2AMCPDeployment(agent).Spec.Template.Spec
	if pod.ServiceAccountName != a2aMCPName(agent) {
		t.Errorf("serviceAccountName = %q, want %q: the broker's caller must not be the namespace default", pod.ServiceAccountName, a2aMCPName(agent))
	}
	if pod.AutomountServiceAccountToken == nil || *pod.AutomountServiceAccountToken {
		t.Error("the default-audience token is mounted; the broker token is the pod's only one")
	}
	var proj *corev1.ServiceAccountTokenProjection
	for _, v := range pod.Volumes {
		if v.Name != a2aMCPBrokerTokenVolume {
			continue
		}
		if v.Projected == nil || len(v.Projected.Sources) != 1 || v.Projected.Sources[0].ServiceAccountToken == nil {
			t.Fatalf("volume %s = %+v, want one projected ServiceAccount token", v.Name, v.VolumeSource)
		}
		proj = v.Projected.Sources[0].ServiceAccountToken
	}
	if proj == nil {
		t.Fatal("no broker token volume")
	}
	if proj.Audience != "kubeagents-credential-proxy-mcp" {
		t.Errorf("audience = %q, want the broker's default kubeagents-credential-proxy-mcp", proj.Audience)
	}
	if proj.ExpirationSeconds == nil || *proj.ExpirationSeconds != 3600 {
		t.Errorf("expirationSeconds = %v, want 3600", proj.ExpirationSeconds)
	}

	c := pod.Containers[0]
	env := map[string]string{}
	for _, e := range c.Env {
		env[e.Name] = e.Value
	}
	if env[a2aMCPBrokerURLEnvVar] != credentialProxyBaseURL(agent) {
		t.Errorf("%s = %q, want %q", a2aMCPBrokerURLEnvVar, env[a2aMCPBrokerURLEnvVar], credentialProxyBaseURL(agent))
	}
	tokenFile := env[a2aMCPBrokerTokenFileEnvVar]
	var mount *corev1.VolumeMount
	for i := range c.VolumeMounts {
		if c.VolumeMounts[i].Name == a2aMCPBrokerTokenVolume {
			mount = &c.VolumeMounts[i]
		}
	}
	if mount == nil || !mount.ReadOnly || mount.SubPath != "" {
		t.Fatalf("broker token mount = %+v, want a read-only directory mount", mount)
	}
	if tokenFile != path.Join(mount.MountPath, proj.Path) {
		t.Errorf("%s = %q, want %q: the env and the projection must name one file", a2aMCPBrokerTokenFileEnvVar, tokenFile, path.Join(mount.MountPath, proj.Path))
	}
}

// The broker renders in both modes; the MCP server only under next. The flag
// on a today install must confer nothing.
func TestTheMCPBrokerAccessIsInertOutsideModeNext(t *testing.T) {
	t.Setenv(a2aMCPDoorEnvVar, "true")
	agent := brokerPodAgent() // mode unset: today
	if a2aMCPBrokerEnabled(agent) {
		t.Fatal("enabled outside mode next")
	}
	if strings.Contains(allowedBrokerCallers(agent, true), a2aMCPName(agent)) {
		t.Error("MCP caller named on a today install")
	}
	broker := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec.Containers[0]
	if _, found := brokerEnvValue(broker.Env, credentialProxyMCPAudienceEnvVar); found {
		t.Error("MCP audience rendered on a today install")
	}
	if _, found := brokerEnvValue(broker.Env, credentialProxyMCPCallersEnvVar); found {
		t.Error("MCP callers rendered on a today install")
	}
}

func TestTheBrokerConfersTheMCPAudienceOnlyUnderTheFlag(t *testing.T) {
	agent := mcpBrokerAgent()
	caller := "system:serviceaccount:test-ns:test-agent-a2a-mcp"

	t.Setenv(a2aMCPDoorEnvVar, "")
	if strings.Contains(allowedBrokerCallers(agent, true), caller) {
		t.Fatal("the MCP ServiceAccount is a broker caller with the flag unset")
	}
	off := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec.Containers[0]
	if _, found := brokerEnvValue(off.Env, credentialProxyMCPAudienceEnvVar); found {
		t.Fatal("the MCP audience is rendered with the flag unset")
	}
	if _, found := brokerEnvValue(off.Env, "CREDENTIAL_PROXY_MCP_CALLERS"); found {
		t.Fatal("the MCP callers are rendered with the flag unset")
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	if got := allowedBrokerCallers(agent, true); !strings.HasSuffix(got, ","+caller) {
		t.Fatalf("callers = %q, want it to end with %q", got, caller)
	}
	if strings.Contains(allowedBrokerCallers(agent, true), ":default") {
		t.Error("the namespace default ServiceAccount is a broker caller")
	}
	on := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec.Containers[0]
	if v, _ := brokerEnvValue(on.Env, "CREDENTIAL_PROXY_MCP_AUDIENCE"); v != "kubeagents-credential-proxy-mcp" {
		t.Fatalf("CREDENTIAL_PROXY_MCP_AUDIENCE = %q, want kubeagents-credential-proxy-mcp", v)
	}
	// The account the broker admits is the one it binds to the audience,
	// and nothing else: the broker refuses that account every other
	// audience, and the mcp audience to every other caller.
	if v, found := brokerEnvValue(on.Env, "CREDENTIAL_PROXY_MCP_CALLERS"); !found || v != caller {
		t.Fatalf("CREDENTIAL_PROXY_MCP_CALLERS = %q (found %v), want %q", v, found, caller)
	}
	for _, other := range []string{credentialProxyAudience, credentialProxyChatAudience, credentialProxyA2AChatAudience, credentialProxySessionAudience, credentialProxyWatcherAudience} {
		if credentialProxyMCPAudience == other {
			t.Fatalf("the MCP audience collides with %q; the broker would refuse to confer the role", other)
		}
	}
}

// The fence's peer must select the pod the MCP Deployment actually renders,
// on the port the broker actually listens on.
func TestTheBrokerFenceAdmitsTheMCPPodOnlyUnderTheFlag(t *testing.T) {
	agent := mcpBrokerAgent()
	admitsMCP := func() bool {
		podLabels := buildA2AMCPDeployment(agent).Spec.Template.Labels
		for _, rule := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress {
			onPort := false
			for _, p := range rule.Ports {
				if p.Port != nil && p.Port.IntVal == credentialProxyPort {
					onPort = true
				}
				if p.Port != nil && p.Port.IntVal == credentialProxyMetricsPort {
					onPort = false
					break
				}
			}
			if !onPort {
				continue
			}
			for _, peer := range rule.From {
				if peer.PodSelector == nil || peer.NamespaceSelector != nil || len(peer.PodSelector.MatchLabels) == 0 {
					continue
				}
				matches := true
				for k, v := range peer.PodSelector.MatchLabels {
					if podLabels[k] != v {
						matches = false
					}
				}
				if matches {
					return true
				}
			}
		}
		return false
	}

	t.Setenv(a2aMCPDoorEnvVar, "")
	if admitsMCP() {
		t.Error("the broker fence admits the MCP pod with the flag off")
	}
	t.Setenv(a2aMCPDoorEnvVar, "true")
	if !admitsMCP() {
		t.Errorf("the broker fence does not admit the MCP pod on %d with the flag on", credentialProxyPort)
	}
	// And only by its own app label: the a2a part-of/component pair alone
	// would also select every other next-stack pod.
	for _, peer := range a2aMCPBrokerPeers(agent) {
		if peer.PodSelector.MatchLabels["app"] != a2aMCPName(agent) {
			t.Errorf("peer %+v, want the MCP pod's app label", peer.PodSelector.MatchLabels)
		}
	}
}

func TestTheReservedListNamesTheMCPBrokerVariables(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: "CREDENTIAL_PROXY_MCP_AUDIENCE", Value: "kubeagents-credential-proxy"},
		{Name: "CREDENTIAL_PROXY_MCP_CALLERS", Value: "system:serviceaccount:ns:theirs"},
		{Name: "A2A_MCP_BROKER_URL", Value: "http://theirs"},
		{Name: "A2A_MCP_BROKER_TOKEN_FILE", Value: "/tmp/theirs"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	for _, name := range []string{"CREDENTIAL_PROXY_MCP_AUDIENCE", "CREDENTIAL_PROXY_MCP_CALLERS", "A2A_MCP_BROKER_URL", "A2A_MCP_BROKER_TOKEN_FILE"} {
		if _, count := envValueCount(merged, name); count != 0 {
			t.Errorf("%s survived the merge from spec.deployment.env", name)
		}
	}
	if value, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 || value != "kept" {
		t.Errorf("the merge dropped an unrelated plugin variable; the reserved list is too wide")
	}
}

// The ServiceAccount appears with the flag and goes when it is turned off.
func TestTheMCPBrokerIdentityFollowsTheFlag(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	key := types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}

	t.Setenv(a2aMCPDoorEnvVar, "")
	if _, err := r.reconcileA2AMCPBrokerIdentity(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.ServiceAccount{}); !apierrors.IsNotFound(err) {
		t.Fatalf("flag off: ServiceAccount present or unreadable: %v", err)
	}

	t.Setenv(a2aMCPDoorEnvVar, "true")
	if _, err := r.reconcileA2AMCPBrokerIdentity(ctx, agent); err != nil {
		t.Fatal(err)
	}
	sa := &corev1.ServiceAccount{}
	if err := cl.Get(ctx, key, sa); err != nil {
		t.Fatalf("flag on: %v", err)
	}
	if len(sa.OwnerReferences) != 1 || sa.OwnerReferences[0].Name != agent.Name {
		t.Errorf("owner references %+v, want the PlatformAgent", sa.OwnerReferences)
	}

	t.Setenv(a2aMCPDoorEnvVar, "")
	if _, err := r.reconcileA2AMCPBrokerIdentity(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.ServiceAccount{}); !apierrors.IsNotFound(err) {
		t.Errorf("flag turned off: ServiceAccount survived: %v", err)
	}
}

// A ServiceAccount under the reserved name that this PlatformAgent does not
// control is skipped with the flag off, so it cannot stop the rest of every
// next reconcile, and not adopted with the flag on, so its annotations (a
// planted Workload Identity binding) never become the MCP pod's identity.
// Neither is an error (#3871): with the flag on the function reports that
// the server may not be rendered, and reconcileA2A removes it and writes the
// A2AMCPServer condition instead of stopping.
func TestAnUnownedMCPServiceAccountIsSkippedOffAndNotAdoptedOn(t *testing.T) {
	const wiAnnotation = "iam.gke.io/gcp-service-account"
	ctx := context.Background()
	agent := a2aTestAgent()
	key := types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}
	setup := func(t *testing.T) (*PlatformAgentReconciler, client.Client) {
		t.Helper()
		scheme := setupScheme()
		squatter := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace,
			Annotations: map[string]string{wiAnnotation: "theirs@example.iam.gserviceaccount.com"}}}
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent.DeepCopy(), squatter).
			WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
		return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
	}
	untouched := func(t *testing.T, cl client.Client) {
		t.Helper()
		sa := &corev1.ServiceAccount{}
		if err := cl.Get(ctx, key, sa); err != nil {
			t.Fatalf("the unowned ServiceAccount is gone: %v", err)
		}
		if len(sa.OwnerReferences) != 0 {
			t.Errorf("the unowned ServiceAccount was adopted: owner references %+v", sa.OwnerReferences)
		}
	}

	t.Run("flag off: skipped, not fatal", func(t *testing.T) {
		t.Setenv(a2aMCPDoorEnvVar, "")
		r, cl := setup(t)
		if _, err := r.reconcileA2AMCPBrokerIdentity(ctx, agent); err != nil {
			t.Fatalf("an unowned ServiceAccount under the reserved name stopped the reconcile: %v", err)
		}
		untouched(t, cl)
	})

	t.Run("flag on: not adopted, not fatal, reported", func(t *testing.T) {
		t.Setenv(a2aMCPDoorEnvVar, "true")
		r, cl := setup(t)
		owned, err := r.reconcileA2AMCPBrokerIdentity(ctx, agent)
		if err != nil {
			t.Fatalf("an unowned ServiceAccount under the reserved name stopped the reconcile: %v", err)
		}
		if owned {
			t.Fatal("an unowned ServiceAccount was reported as the MCP server's own identity")
		}
		untouched(t, cl)
	})
}

// TestAnUnownedMCPServiceAccountRemovesTheServerAndReportsIt (#3871): with
// the door on and an unowned account under the MCP pod's name, the whole
// next pass completes, the MCP server is removed rather than rendered over
// that identity, and the CR carries the A2AMCPServer condition. Once the
// account is this CR's, the server renders and the condition clears.
func TestAnUnownedMCPServiceAccountRemovesTheServerAndReportsIt(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	t.Setenv(a2aMCPDoorEnvVar, "true")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("precondition: the stack with the MCP door on renders: %v", err)
	}
	mcpKey := types.NamespacedName{Name: a2aMCPName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, mcpKey, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: the MCP server was not rendered: %v", err)
	}
	saKey := types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}
	tokenKey := types.NamespacedName{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace}
	minted := &corev1.Secret{}
	if err := cl.Get(ctx, tokenKey, minted); err != nil {
		t.Fatalf("precondition: the MCP token was not minted: %v", err)
	}
	// The render owns its account; a foreign one under the name can only
	// exist in its place, so swap it.
	if err := cl.Delete(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: saKey.Name, Namespace: saKey.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: saKey.Name, Namespace: saKey.Namespace,
		Annotations: map[string]string{"iam.gke.io/gcp-service-account": "theirs@example.iam.gserviceaccount.com"}}}); err != nil {
		t.Fatal(err)
	}

	fenceKey := types.NamespacedName{Name: a2aMCPNetpolName(agent), Namespace: agent.Namespace}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("an unowned MCP ServiceAccount stopped the next reconcile: %v", err)
	}
	if state.mcpNotRendered != a2aMCPIdentityUnownedReason {
		t.Fatalf("mcpNotRendered = %q, want %q", state.mcpNotRendered, a2aMCPIdentityUnownedReason)
	}
	if err := cl.Get(ctx, mcpKey, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the MCP server stayed rendered over an identity that is not ours: %v", err)
	}
	// The fence stands across passes: the fences pass applies it while the
	// door is on, and a removal here would make every pass create and
	// delete it, each an event that wakes the next. The token stands too:
	// removing it would rotate every client's bearer token when the server
	// renders again.
	for pass := 1; pass <= 2; pass++ {
		if err := cl.Get(ctx, fenceKey, &networkingv1.NetworkPolicy{}); err != nil {
			t.Fatalf("pass %d: the MCP fence is gone while the door is on: %v", pass, err)
		}
		kept := &corev1.Secret{}
		if err := cl.Get(ctx, tokenKey, kept); err != nil || string(kept.Data[a2aMCPAccessKey]) != string(minted.Data[a2aMCPAccessKey]) {
			t.Fatalf("pass %d: the MCP token was removed or rotated behind an unowned account: err=%v", pass, err)
		}
		if _, err := r.reconcileA2A(ctx, agent); err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
	}
	sa := &corev1.ServiceAccount{}
	if err := cl.Get(ctx, saKey, sa); err != nil || len(sa.OwnerReferences) != 0 {
		t.Errorf("the unowned ServiceAccount was adopted or removed: err=%v owners=%+v", err, sa.OwnerReferences)
	}
	if err := r.syncA2AConditions(ctx, agent, state); err != nil {
		t.Fatalf("syncA2AConditions: %v", err)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, a2aMCPIdentityConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != a2aMCPIdentityUnownedReason || !strings.Contains(cond.Message, saKey.Name) {
		t.Fatalf("A2AMCPServer condition = %+v, want True/%s naming %s", cond, a2aMCPIdentityUnownedReason, saKey.Name)
	}

	// The account becomes ours again: the server renders and the condition clears.
	if err := cl.Delete(ctx, sa); err != nil {
		t.Fatal(err)
	}
	state, err = r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A after the account was removed: %v", err)
	}
	if state.mcpNotRendered != "" {
		t.Errorf("the pass still reports %q with the account gone", state.mcpNotRendered)
	}
	if err := cl.Get(ctx, mcpKey, &appsv1.Deployment{}); err != nil {
		t.Errorf("the MCP server was not rendered again: %v", err)
	}
	if err := r.syncA2AConditions(ctx, agent, state); err != nil {
		t.Fatalf("syncA2AConditions: %v", err)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, a2aMCPIdentityConditionType) != nil {
		t.Error("the A2AMCPServer condition did not clear")
	}
}

// TestAnUnownedMCPTokenSecretRemovesTheServerAndReportsIt (#3871): the token
// Secret is the MCP server's other reserved name. With the door on and an
// unowned Secret under it, the pass completes, a server rendered earlier is
// removed rather than left mounting a token that is not ours, and the CR
// carries the condition with the token's reason.
func TestAnUnownedMCPTokenSecretRemovesTheServerAndReportsIt(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	t.Setenv(a2aMCPDoorEnvVar, "true")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("precondition: the stack with the MCP door on renders: %v", err)
	}
	mcpKey := types.NamespacedName{Name: a2aMCPName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, mcpKey, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: the MCP server was not rendered: %v", err)
	}
	// The render owns its token; a foreign one under the name can only
	// exist in its place, so swap it.
	tokenKey := types.NamespacedName{Name: a2aMCPAccessName(agent), Namespace: agent.Namespace}
	if err := cl.Delete(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: tokenKey.Name, Namespace: tokenKey.Namespace}}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Create(ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: tokenKey.Name, Namespace: tokenKey.Namespace},
		Data: map[string][]byte{a2aMCPAccessKey: []byte("theirs")}}); err != nil {
		t.Fatal(err)
	}

	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("an unowned MCP token Secret stopped the next reconcile: %v", err)
	}
	if state.mcpNotRendered != a2aMCPTokenUnownedReason {
		t.Fatalf("mcpNotRendered = %q, want %q", state.mcpNotRendered, a2aMCPTokenUnownedReason)
	}
	if err := cl.Get(ctx, mcpKey, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the MCP server stayed rendered over a token that is not ours: %v", err)
	}
	got := &corev1.Secret{}
	if err := cl.Get(ctx, tokenKey, got); err != nil || string(got.Data[a2aMCPAccessKey]) != "theirs" || len(got.OwnerReferences) != 0 {
		t.Errorf("the unowned Secret was touched: %v %+v", err, got.OwnerReferences)
	}
	if err := r.syncA2AConditions(ctx, agent, state); err != nil {
		t.Fatal(err)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, a2aMCPIdentityConditionType)
	if cond == nil || cond.Reason != a2aMCPTokenUnownedReason || !strings.Contains(cond.Message, tokenKey.Name) {
		t.Fatalf("A2AMCPServer condition = %+v, want %s naming %s", cond, a2aMCPTokenUnownedReason, tokenKey.Name)
	}
}

// TestReconcileReportsAnUnownedMCPServiceAccountOnTheStatusItWrites (#3871)
// drives the whole Reconcile, since the wiring is what the unit tests above
// cannot see: the broker Deployment the pass renders names the MCP account
// only while this CR owns it, and the condition reaches the CR through
// whichever status writer the pass ends on, Ready path included.
func TestReconcileReportsAnUnownedMCPServiceAccountOnTheStatusItWrites(t *testing.T) {
	t.Setenv(a2aMCPDoorEnvVar, "true")
	agent := mcpBrokerAgent()
	caller := "system:serviceaccount:" + agent.Namespace + ":" + a2aMCPServiceAccountName(agent)
	saKey := types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}
	squatter := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: saKey.Name, Namespace: saKey.Namespace}}
	r, cl := newSplitReconciler(t, agent, squatter)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	brokerKey := types.NamespacedName{Name: agent.Name + "-credential-proxy", Namespace: agent.Namespace}

	readBroker := func(t *testing.T) corev1.Container {
		t.Helper()
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, brokerKey, dep); err != nil {
			t.Fatalf("the broker Deployment was not rendered: %v", err)
		}
		return dep.Spec.Template.Spec.Containers[0]
	}
	readCondition := func(t *testing.T) *metav1.Condition {
		t.Helper()
		live := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, live); err != nil {
			t.Fatal(err)
		}
		return meta.FindStatusCondition(live.Status.Conditions, a2aMCPIdentityConditionType)
	}

	for pass := 1; pass <= 3; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d beside an unowned MCP ServiceAccount: %v", pass, err)
		}
	}
	broker := readBroker(t)
	if v, _ := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_ALLOWED_CALLERS"); strings.Contains(v, caller) {
		t.Errorf("the broker admits %s while somebody else owns it: %q", caller, v)
	}
	if _, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_MCP_CALLERS"); found {
		t.Error("CREDENTIAL_PROXY_MCP_CALLERS is rendered while somebody else owns the account")
	}
	if cond := readCondition(t); cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != a2aMCPIdentityUnownedReason {
		t.Fatalf("the CR carries no A2AMCPServer/%s condition after the pass: %+v", a2aMCPIdentityUnownedReason, cond)
	}
	if err := cl.Get(ctx, saKey, &corev1.ServiceAccount{}); err != nil {
		t.Fatalf("the unowned ServiceAccount is gone: %v", err)
	}

	// The account removed: the next passes admit the one this CR creates
	// and clear the condition.
	if err := cl.Delete(ctx, squatter); err != nil {
		t.Fatal(err)
	}
	for pass := 1; pass <= 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d after the account was removed: %v", pass, err)
		}
	}
	broker = readBroker(t)
	if v, _ := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_ALLOWED_CALLERS"); !strings.Contains(v, caller) {
		t.Errorf("the broker does not admit %s once the account is this CR's: %q", caller, v)
	}
	if cond := readCondition(t); cond != nil {
		t.Errorf("the A2AMCPServer condition did not clear: %+v", cond)
	}
}

// TestTheBrokerAdmitsTheMCPAccountOnlyWhenOwned (#3871): with the door on,
// the caller list and the MCP audience binding name the account only while
// this PlatformAgent owns it; a2aMCPIdentityOwned is the read that decides,
// true for an absent account (this pass creates it) and with the door off.
func TestTheBrokerAdmitsTheMCPAccountOnlyWhenOwned(t *testing.T) {
	agent := mcpBrokerAgent()
	caller := "system:serviceaccount:test-ns:test-agent-a2a-mcp"
	t.Setenv(a2aMCPDoorEnvVar, "true")
	if got := allowedBrokerCallers(agent, false); strings.Contains(got, caller) {
		t.Errorf("callers = %q: the MCP account is admitted while somebody else owns it", got)
	}
	if env := a2aMCPBrokerEnv(agent, false); len(env) != 0 {
		t.Errorf("the MCP audience binding is rendered while somebody else owns the account: %+v", env)
	}
	unowned := buildCredentialProxyDeploymentForPlugins(agent, "policy-hash", nil, false).Spec.Template.Spec.Containers[0]
	if _, found := brokerEnvValue(unowned.Env, "CREDENTIAL_PROXY_MCP_CALLERS"); found {
		t.Error("CREDENTIAL_PROXY_MCP_CALLERS is rendered while somebody else owns the account")
	}
	if v, _ := brokerEnvValue(unowned.Env, "CREDENTIAL_PROXY_ALLOWED_CALLERS"); strings.Contains(v, caller) {
		t.Errorf("CREDENTIAL_PROXY_ALLOWED_CALLERS = %q names the account while somebody else owns it", v)
	}

	ctx := context.Background()
	key := types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}
	for _, tc := range []struct {
		name string
		door string
		sa   *corev1.ServiceAccount
		want bool
	}{
		{"door on, account absent", "true", nil, true},
		{"door on, account ours", "true", &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}, true},
		{"door on, account somebody else's", "true", &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}, false},
		{"door off, account somebody else's", "", &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aMCPDoorEnvVar, tc.door)
			scheme := setupScheme()
			objs := []client.Object{agent.DeepCopy()}
			if tc.sa != nil {
				sa := tc.sa.DeepCopy()
				if tc.want && tc.door == "true" {
					if err := ctrl.SetControllerReference(agent, sa, scheme); err != nil {
						t.Fatal(err)
					}
				}
				objs = append(objs, sa)
			}
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			got, err := r.a2aMCPIdentityOwned(ctx, agent)
			if err != nil {
				t.Fatal(err)
			}
			if got != tc.want {
				t.Errorf("a2aMCPIdentityOwned = %t, want %t", got, tc.want)
			}
		})
	}
}

// TestTheMCPServiceAccountIsReservedWithTheDoorOnOrOff pins the MCP pod's
// broker identity on the reserved list in both flag states: an AgentProfile may
// not run as it, and spec.security.serviceAccountName may not name it, so a
// door turned on later never finds the name already taken. Taken, the override
// would put the broker's Workload Identity annotation on the MCP pod's account.
func TestTheMCPServiceAccountIsReservedWithTheDoorOnOrOff(t *testing.T) {
	for _, door := range []string{"", "true"} {
		t.Run("door="+door, func(t *testing.T) {
			t.Setenv(a2aMCPDoorEnvVar, door)
			agent := mcpBrokerAgent()
			sa := a2aMCPServiceAccountName(agent)

			p := testAgentProfile(agent.Namespace, "sneaky", func(p *agentv1alpha1.AgentProfile) {
				p.Spec.Identity.ServiceAccountName = sa
			})
			got := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{p})["sneaky"]
			if got.refused == nil || got.reason != reasonAgentProfileRefused {
				t.Errorf("a profile naming %q resolved to %+v, want refused with %q", sa, got, reasonAgentProfileRefused)
			}

			agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: sa}
			if msg := ReservedServiceAccountOverride(agent); msg == "" {
				t.Errorf("an override naming %q is not refused", sa)
			}
		})
	}
}

// TestAnOverrideNamingTheMCPServiceAccountWritesNoServiceAccount is the
// controller's step 2z for the MCP account, door on, with no webhook in front:
// the pass goes Degraded and writes no ServiceAccount of that name, so the
// broker's Workload Identity annotation never lands on the MCP pod's identity.
func TestAnOverrideNamingTheMCPServiceAccountWritesNoServiceAccount(t *testing.T) {
	t.Setenv(a2aMCPDoorEnvVar, "true")
	agent := mcpBrokerAgent()
	if agent.Spec.Security == nil {
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{}
	}
	agent.Spec.Security.ServiceAccountName = a2aMCPServiceAccountName(agent)
	agent.Spec.Security.ServiceAccountAnnotations = map[string]string{
		"iam.gke.io/gcp-service-account": "broker@p.iam.gserviceaccount.com",
	}
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("a refusal is a Degraded status, not a reconcile error: %v", err)
	}
	sa := &corev1.ServiceAccount{}
	err := cl.Get(ctx, types.NamespacedName{Name: a2aMCPServiceAccountName(agent), Namespace: agent.Namespace}, sa)
	if err == nil {
		t.Fatalf("a ServiceAccount named %q was written, annotations %v", sa.Name, sa.Annotations)
	}
	if !apierrors.IsNotFound(err) {
		t.Fatalf("get: %v", err)
	}
	var live agentv1alpha1.PlatformAgent
	if err := cl.Get(ctx, req.NamespacedName, &live); err != nil {
		t.Fatal(err)
	}
	degraded := false
	for _, c := range live.Status.Conditions {
		degraded = degraded || c.Reason == reasonReservedServiceAccount
	}
	if !degraded {
		t.Errorf("status carries no %s condition: %v", reasonReservedServiceAccount, live.Status.Conditions)
	}
}
