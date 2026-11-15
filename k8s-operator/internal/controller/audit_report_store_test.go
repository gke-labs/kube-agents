package controller

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func reportStoreAgent(mode string) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns", UID: "uid-1"}}
	if mode != "" {
		agent.Spec.Mode = ptr.To(mode)
	}
	return agent
}

func storeEnvValue(env []corev1.EnvVar, name string) (string, bool) {
	for _, e := range env {
		if e.Name == name {
			return e.Value, true
		}
	}
	return "", false
}

// The listener's switch, subject and port are on agent-api-auth under next
// alone, and never on platform-agent.
func TestAuditReportStoreSidecarRendersUnderNextOnly(t *testing.T) {
	for _, tc := range []struct {
		mode string
		want bool
	}{{"", false}, {"today", false}, {"next", true}, {"later", false}} {
		agent := reportStoreAgent(tc.mode)
		sidecar := buildAgentAPIAuthSidecar(agent, "/opt/data")
		enabled, hasEnabled := storeEnvValue(sidecar.Env, auditReportStoreEnabledEnv)
		subject, hasSubject := storeEnvValue(sidecar.Env, auditReportStoreAllowedSubjectEnv)
		var port bool
		for _, p := range sidecar.Ports {
			if p.Name == auditReportStorePortName && p.ContainerPort == auditReportStorePort {
				port = true
			}
		}
		if hasEnabled != tc.want || hasSubject != tc.want || port != tc.want {
			t.Errorf("mode %q: enabled=%v subject=%v port=%v, want all %v", tc.mode, hasEnabled, hasSubject, port, tc.want)
		}
		if tc.want {
			if enabled != "1" {
				t.Errorf("mode %q: %s=%q, want 1", tc.mode, auditReportStoreEnabledEnv, enabled)
			}
			if subject != "system:serviceaccount:test-ns:test-agent" {
				t.Errorf("mode %q: subject %q is not the broker's ServiceAccount", tc.mode, subject)
			}
			if prefix, _ := storeEnvValue(sidecar.Env, auditReportStoreAllowedPodPrefixEnv); prefix != "test-agent-credential-proxy-" {
				t.Errorf("mode %q: pod prefix %q is not the broker Deployment's", tc.mode, prefix)
			}
		}
	}
}

// The subject follows spec.security.serviceAccountName, the broker's.
func TestAuditReportStoreSubjectIsTheBrokersServiceAccount(t *testing.T) {
	agent := reportStoreAgent("next")
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: "custom-broker"}
	if got := auditReportStoreAllowedSubject(agent); got != "system:serviceaccount:test-ns:custom-broker" {
		t.Errorf("subject %q", got)
	}
	if gw := gatewayServiceAccountName(agent); auditReportStoreAllowedSubject(agent) == "system:serviceaccount:test-ns:"+gw {
		t.Errorf("the subject is the gateway's own ServiceAccount")
	}
}

// A CR cannot set the store's names through spec.deployment.env in any mode.
func TestAuditReportStoreEnvIsReserved(t *testing.T) {
	for _, mode := range []string{"today", "next"} {
		testAuditReportStoreEnvIsReservedIn(t, mode)
	}
}

func testAuditReportStoreEnvIsReservedIn(t *testing.T, mode string) {
	agent := reportStoreAgent(mode)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: []corev1.EnvVar{
		{Name: auditReportStoreEnabledEnv, Value: "1"},
		{Name: auditReportStoreAllowedSubjectEnv, Value: "system:serviceaccount:test-ns:attacker"},
		{Name: auditReportStoreURLEnv, Value: "http://elsewhere"},
		{Name: auditReportStoreTokenFileEnv, Value: "/tmp/x"},
		{Name: auditReportStoreAllowedPodPrefixEnv, Value: "test-agent-credential-proxy-exec-"},
		{Name: "FLEET_AUDIT_REPORTS_DIR", Value: "/etc"},
		{Name: "AUDIT_REPORT_STORE_API_SERVER", Value: "https://attacker.example"},
		{Name: "AUDIT_REPORT_STORE_SA_DIR", Value: "/tmp/sa"},
	}}
	for _, env := range [][]corev1.EnvVar{buildAgentAPIAuthEnv(agent), buildCredentialProxyContainer(agent).Env} {
		for _, e := range env {
			for _, name := range auditReportStoreEnvNames {
				if e.Name != name {
					continue
				}
				if e.Value == "/etc" || e.Value == "https://attacker.example" || e.Value == "/tmp/sa" ||
					e.Value == "http://elsewhere" || e.Value == "/tmp/x" || e.Value == "test-agent-credential-proxy-exec-" ||
					e.Value == "system:serviceaccount:test-ns:attacker" || (mode == "today" && name == auditReportStoreEnabledEnv) {
					t.Errorf("mode %s: spec.deployment.env set %s=%q", mode, name, e.Value)
				}
			}
		}
	}
}

// The broker carries the store token and address under next only, and the
// exec tier derived from it drops both.
func TestAuditReportStoreBrokerTokenAndExecTierDrop(t *testing.T) {
	for _, mode := range []string{"today", "next"} {
		agent := reportStoreAgent(mode)
		broker := buildCredentialProxyDeployment(agent, "hash")
		spec := broker.Spec.Template.Spec
		var volume *corev1.Volume
		for i := range spec.Volumes {
			if spec.Volumes[i].Name == auditReportStoreTokenVolume {
				volume = &spec.Volumes[i]
			}
		}
		_, hasURL := storeEnvValue(spec.Containers[0].Env, auditReportStoreURLEnv)
		tokenFile, hasFile := storeEnvValue(spec.Containers[0].Env, auditReportStoreTokenFileEnv)
		want := mode == "next"
		if (volume != nil) != want || hasURL != want || hasFile != want {
			t.Fatalf("mode %s: volume=%v url=%v file=%v, want %v", mode, volume != nil, hasURL, hasFile, want)
		}
		if !want {
			continue
		}
		token := volume.Projected.Sources[0].ServiceAccountToken
		if token == nil || token.Audience != auditReportStoreAudience {
			t.Errorf("the store token is not bound to %s: %+v", auditReportStoreAudience, token)
		}
		if tokenFile != auditReportStoreTokenMountPath+"/token" {
			t.Errorf("token file %q", tokenFile)
		}
		if url, _ := storeEnvValue(spec.Containers[0].Env, auditReportStoreURLEnv); url != "http://test-agent-report-store.test-ns.svc:8700" {
			t.Errorf("url %q", url)
		}
		exec := buildCredentialProxyExecTierDeployment(agent, broker.DeepCopy())
		for _, v := range exec.Spec.Template.Spec.Volumes {
			if v.Name == auditReportStoreTokenVolume {
				t.Errorf("the exec tier keeps the store token volume")
			}
		}
		for _, c := range exec.Spec.Template.Spec.Containers {
			for _, m := range c.VolumeMounts {
				if m.Name == auditReportStoreTokenVolume {
					t.Errorf("the exec tier mounts the store token")
				}
			}
			for _, name := range []string{auditReportStoreURLEnv, auditReportStoreTokenFileEnv} {
				if _, ok := storeEnvValue(c.Env, name); ok {
					t.Errorf("the exec tier keeps %s", name)
				}
			}
		}
	}
}

func admits(rule networkingv1.NetworkPolicyIngressRule, protocol corev1.Protocol, port int32) bool {
	if len(rule.Ports) == 0 {
		return true
	}
	for _, p := range rule.Ports {
		proto := corev1.ProtocolTCP
		if p.Protocol != nil {
			proto = *p.Protocol
		}
		if proto != protocol {
			continue
		}
		if p.Port == nil {
			return true
		}
		end := p.Port.IntVal
		if p.EndPort != nil {
			end = *p.EndPort
		}
		if p.Port.IntVal <= port && port <= end {
			return true
		}
	}
	return false
}

// With the gateway policy isolating the pod, the store policy is the broker
// rule alone: a from-anywhere rule would widen that policy's allowlist. With
// nothing isolating it, the policy keeps every other port open to every
// source and holds 8700 to the broker.
func TestAuditReportStoreNetworkPolicyShape(t *testing.T) {
	agent := reportStoreAgent("next")
	broker := credentialProxySelector(agent)

	isolated := buildAuditReportStoreNetworkPolicy(agent, true)
	if len(isolated.Spec.Ingress) != 1 {
		t.Fatalf("isolated: %d rules, want the broker rule alone", len(isolated.Spec.Ingress))
	}
	open := buildAuditReportStoreNetworkPolicy(agent, false)
	if len(open.Spec.Ingress) != 2 {
		t.Fatalf("not isolated: %d rules, want the broker rule and the rest-of-the-pod rule", len(open.Spec.Ingress))
	}
	for _, np := range []*networkingv1.NetworkPolicy{isolated, open} {
		if got := np.Spec.PodSelector.MatchLabels["app"]; got != "test-agent-gateway" {
			t.Errorf("selects %q", got)
		}
		if len(np.Spec.PolicyTypes) != 1 || np.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
			t.Errorf("policyTypes %v, want Ingress alone", np.Spec.PolicyTypes)
		}
		for _, rule := range np.Spec.Ingress {
			if !admits(rule, corev1.ProtocolTCP, auditReportStorePort) {
				continue
			}
			if len(rule.From) != 1 || rule.From[0].PodSelector == nil || rule.From[0].NamespaceSelector != nil ||
				len(rule.From[0].PodSelector.MatchLabels) != len(broker) {
				t.Fatalf("a rule admitting 8700 admits %+v", rule.From)
			}
			for k, v := range broker {
				if rule.From[0].PodSelector.MatchLabels[k] != v {
					t.Errorf("a rule admitting 8700 does not select the broker: %+v", rule.From)
				}
			}
		}
	}
	rest := open.Spec.Ingress[1]
	if len(rest.From) != 0 {
		t.Errorf("the rest-of-the-pod rule names peers: %+v", rest.From)
	}
	for _, port := range []int32{1, 53, 8642, 8643, 8699, 8701, 9095, 65535} {
		if !admits(rest, corev1.ProtocolTCP, port) {
			t.Errorf("TCP %d is no longer open to every source", port)
		}
	}
	for _, port := range []int32{1, 53, 8700, 65535} {
		if !admits(rest, corev1.ProtocolUDP, port) {
			t.Errorf("UDP %d is no longer open to every source", port)
		}
	}
}

// Under today the reconcile deletes the two objects it owns, and leaves a
// same-named object it does not own; under skew it touches neither.
func TestReconcileAuditReportStoreTearsDownOnTodayOnly(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	next := reportStoreAgent("next")
	svc := buildAuditReportStoreService(next)
	np := buildAuditReportStoreNetworkPolicy(next, true)
	for _, obj := range []metav1.Object{svc, np} {
		if err := ctrl.SetControllerReference(next, obj, scheme); err != nil {
			t.Fatal(err)
		}
	}
	key := types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}

	// Skew: nothing deleted.
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(svc.DeepCopy(), np.DeepCopy()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileAuditReportStore(ctx, reportStoreAgent("later"), false, true); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Errorf("skew deleted the Service: %v", err)
	}

	// Today: both owned objects go.
	if err := r.reconcileAuditReportStore(ctx, reportStoreAgent("today"), true, true); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); !apierrors.IsNotFound(err) {
		t.Errorf("today kept the Service: %v", err)
	}
	if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); !apierrors.IsNotFound(err) {
		t.Errorf("today kept the NetworkPolicy: %v", err)
	}

	// Today with an object someone else made under the name: kept.
	foreign := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}
	cl = fake.NewClientBuilder().WithScheme(scheme).WithObjects(foreign).Build()
	r = &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileAuditReportStore(ctx, reportStoreAgent("today"), true, true); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Errorf("today deleted a Service it does not own: %v", err)
	}
}

// The store policy's catch-all rule renders only while no other policy
// isolates the gateway pod for ingress, whoever's policy that is.
func TestAuditReportStorePolicyReadsTheNamespacesPolicies(t *testing.T) {
	ctx := context.Background()
	gatewayPod := map[string]string{"app": "test-agent-gateway"}
	policy := func(name string, selector map[string]string, types ...networkingv1.PolicyType) *networkingv1.NetworkPolicy {
		return &networkingv1.NetworkPolicy{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns"},
			Spec: networkingv1.NetworkPolicySpec{
				PodSelector: metav1.LabelSelector{MatchLabels: selector},
				PolicyTypes: types,
			},
		}
	}
	for _, tc := range []struct {
		name         string
		existing     []*networkingv1.NetworkPolicy
		wantCatchAll bool
	}{
		{"no other policy", nil, true},
		{"a foreign ingress policy selecting the pod", []*networkingv1.NetworkPolicy{
			policy("customer-lockdown", gatewayPod, networkingv1.PolicyTypeIngress)}, false},
		{"a foreign policy with no policyTypes selecting the pod", []*networkingv1.NetworkPolicy{
			policy("customer-default", gatewayPod)}, false},
		{"a foreign policy selecting every pod", []*networkingv1.NetworkPolicy{
			policy("default-deny", nil, networkingv1.PolicyTypeIngress)}, false},
		{"a foreign egress-only policy selecting the pod", []*networkingv1.NetworkPolicy{
			policy("customer-egress", gatewayPod, networkingv1.PolicyTypeEgress)}, true},
		{"an ingress policy selecting other pods", []*networkingv1.NetworkPolicy{
			policy("other-app", map[string]string{"app": "litellm"}, networkingv1.PolicyTypeIngress)}, true},
		{"the store's own policy does not count", []*networkingv1.NetworkPolicy{
			policy("test-agent-report-store", gatewayPod, networkingv1.PolicyTypeIngress)}, true},
		{"the operator's gateway policy", []*networkingv1.NetworkPolicy{
			policy("test-agent-gateway-netpol", gatewayPod, networkingv1.PolicyTypeIngress, networkingv1.PolicyTypeEgress)}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := reportStoreAgent("next")
			builder := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(fakeServerSideApplyInterceptors())
			for _, np := range tc.existing {
				builder = builder.WithObjects(np)
			}
			cl := builder.Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			if err := r.reconcileAuditReportStore(ctx, agent, true, true); err != nil {
				t.Fatal(err)
			}
			var got networkingv1.NetworkPolicy
			if err := cl.Get(ctx, types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}, &got); err != nil {
				t.Fatal(err)
			}
			if catchAll := len(got.Spec.Ingress) == 2; catchAll != tc.wantCatchAll {
				t.Errorf("rules %d, want the catch-all %v", len(got.Spec.Ingress), tc.wantCatchAll)
			}
		})
	}
}

// A policy that lands later drops the catch-all on the next pass.
func TestAuditReportStorePolicyIsRecomputedEachPass(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := reportStoreAgent("next")
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	key := types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}
	rules := func() int {
		if err := r.reconcileAuditReportStore(ctx, agent, true, true); err != nil {
			t.Fatal(err)
		}
		var np networkingv1.NetworkPolicy
		if err := cl.Get(ctx, key, &np); err != nil {
			t.Fatal(err)
		}
		return len(np.Spec.Ingress)
	}
	if n := rules(); n != 2 {
		t.Fatalf("first pass: %d rules, want 2", n)
	}
	lockdown := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: "customer-lockdown", Namespace: "test-ns"},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": "test-agent-gateway"}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
		},
	}
	if err := cl.Create(ctx, lockdown); err != nil {
		t.Fatal(err)
	}
	if n := rules(); n != 1 {
		t.Errorf("after a foreign ingress policy: %d rules, want the broker rule alone", n)
	}
}

// spec.networkPolicy.enabled false: no store policy, an owned one deleted,
// and the Service still rendered.
func TestAuditReportStorePolicyWithheldWhenPoliciesAreOff(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := reportStoreAgent("next")
	existing := buildAuditReportStoreNetworkPolicy(agent, true)
	if err := ctrl.SetControllerReference(agent, existing, scheme); err != nil {
		t.Fatal(err)
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, existing).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileAuditReportStore(ctx, agent, true, false); err != nil {
		t.Fatal(err)
	}
	key := types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}
	if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); !apierrors.IsNotFound(err) {
		t.Errorf("the store policy survived spec.networkPolicy.enabled false: %v", err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Errorf("the Service is not rendered: %v", err)
	}
}

// Doubt counts as isolating: a selector on a label no gateway label set
// carries, one that cannot be evaluated, and one matching a live pod's label.
func TestAuditReportStoreDoubtfulPoliciesDropTheCatchAll(t *testing.T) {
	ctx := context.Background()
	ingress := []networkingv1.PolicyType{networkingv1.PolicyTypeIngress}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "test-agent-gateway-abc", Namespace: "test-ns",
		Labels: map[string]string{"app": "test-agent-gateway", "team": "platform"}}}
	for _, tc := range []struct {
		name     string
		selector metav1.LabelSelector
		pods     bool
		want     bool
	}{
		{"a label nobody can see", metav1.LabelSelector{MatchLabels: map[string]string{"role": "db"}}, false, true},
		{"a selector that cannot be evaluated", metav1.LabelSelector{MatchExpressions: []metav1.LabelSelectorRequirement{
			{Key: "app", Operator: "Sometimes", Values: []string{"x"}}}}, false, true},
		{"a live pod's own label", metav1.LabelSelector{MatchLabels: map[string]string{"team": "platform"}}, true, true},
		// The live pod makes the key known, so the selector is evaluated
		// rather than doubted: another value selects other pods.
		{"a seen label with another value", metav1.LabelSelector{MatchLabels: map[string]string{"team": "data"}}, true, false},
		{"the same, with no pod to read", metav1.LabelSelector{MatchLabels: map[string]string{"team": "data"}}, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := reportStoreAgent("next")
			np := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: "foreign", Namespace: "test-ns"},
				Spec: networkingv1.NetworkPolicySpec{PodSelector: tc.selector, PolicyTypes: ingress}}
			builder := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, np)
			if tc.pods {
				builder = builder.WithObjects(pod)
			}
			r := &PlatformAgentReconciler{Client: builder.Build(), Scheme: scheme}
			isolated, err := r.gatewayPodIngressIsolated(ctx, agent)
			if err != nil {
				t.Fatal(err)
			}
			if isolated != tc.want {
				t.Errorf("isolating %v, want %v", isolated, tc.want)
			}
		})
	}
}
