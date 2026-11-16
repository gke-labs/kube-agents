package controller

import (
	"context"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
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
		{Name: auditReportProfilesEnv, Value: "attacker-profile"},
	}}
	for _, env := range [][]corev1.EnvVar{buildAgentAPIAuthEnv(agent), buildCredentialProxyContainer(agent).Env} {
		for _, e := range env {
			for _, name := range auditReportStoreEnvNames {
				if e.Name != name {
					continue
				}
				if e.Value == "/etc" || e.Value == "https://attacker.example" || e.Value == "/tmp/sa" || e.Value == "attacker-profile" ||
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
		profiles, hasProfiles := storeEnvValue(spec.Containers[0].Env, auditReportProfilesEnv)
		want := mode == "next"
		if (volume != nil) != want || hasURL != want || hasFile != want || hasProfiles != want {
			t.Fatalf("mode %s: volume=%v url=%v file=%v profiles=%v, want %v", mode, volume != nil, hasURL, hasFile, hasProfiles, want)
		}
		if !want {
			continue
		}
		token := volume.Projected.Sources[0].ServiceAccountToken
		if token == nil || token.Audience != auditReportStoreAudience {
			t.Errorf("the store token is not bound to %s: %+v", auditReportStoreAudience, token)
		}
		if profiles != "fleet-audit,platform" {
			t.Errorf("profiles %q", profiles)
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
			for _, name := range []string{auditReportStoreURLEnv, auditReportStoreTokenFileEnv, auditReportProfilesEnv} {
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

// Under next the gateway policy carries exactly one more ingress rule than
// under today, the broker's pods to 8700, and nothing else in it admits that
// port. Today's ingress is what an operator before this change renders in
// both modes (its ingress rules did not turn on mode), so the rule is the only
// thing an older operator's rewrite of the policy changes, and it drops it.
func TestGatewayNetpolCarriesTheStoreRuleUnderNextOnly(t *testing.T) {
	profile := netpolProfile{Generated: true, OperatorNamespace: "kubeagents-system"}
	today := buildNetworkPolicy(reportStoreAgent("today"), nil, profile, false, "", false)
	next := buildNetworkPolicy(reportStoreAgent("next"), nil, profile, false, "", false)
	want := auditReportStoreIngressRule(reportStoreAgent("next"))
	var extra []networkingv1.NetworkPolicyIngressRule
	remaining := append([]networkingv1.NetworkPolicyIngressRule{}, next.Spec.Ingress...)
	for i, rule := range remaining {
		if reflect.DeepEqual(rule, want) {
			extra = append(extra, rule)
			remaining = append(remaining[:i:i], remaining[i+1:]...)
			break
		}
	}
	if len(extra) != 1 {
		t.Fatalf("the next gateway policy does not carry the store rule: %+v", next.Spec.Ingress)
	}
	if !reflect.DeepEqual(remaining, today.Spec.Ingress) {
		t.Errorf("next's ingress differs from today's by more than the store rule:\nnext-minus-rule %+v\ntoday %+v", remaining, today.Spec.Ingress)
	}
	for _, rule := range today.Spec.Ingress {
		if admits(rule, corev1.ProtocolTCP, auditReportStorePort) {
			t.Errorf("today's gateway policy admits 8700: %+v", rule)
		}
	}
	for _, rule := range next.Spec.Ingress {
		if admits(rule, corev1.ProtocolTCP, auditReportStorePort) && !reflect.DeepEqual(rule, want) {
			t.Errorf("a rule other than the broker's admits 8700: %+v", rule)
		}
	}
	broker := credentialProxySelector(reportStoreAgent("next"))
	if got := want.From[0].PodSelector.MatchLabels; !reflect.DeepEqual(got, broker) || len(want.From) != 1 {
		t.Errorf("the store rule admits %+v, want the broker's pods alone", want.From)
	}
}

func leftoverStorePolicy(t *testing.T, agent *agentv1alpha1.PlatformAgent) *networkingv1.NetworkPolicy {
	np := &networkingv1.NetworkPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: auditReportStoreName(agent), Namespace: agent.Namespace},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{MatchLabels: map[string]string{"app": agent.Name + "-gateway"}},
			PolicyTypes: []networkingv1.PolicyType{networkingv1.PolicyTypeIngress},
			Ingress:     []networkingv1.NetworkPolicyIngressRule{auditReportStoreIngressRule(agent)},
		},
	}
	if err := ctrl.SetControllerReference(agent, np, setupScheme()); err != nil {
		t.Fatal(err)
	}
	return np
}

// Under next the reconcile renders the Service and no NetworkPolicy for the
// store, and deletes the one an earlier build of this change left; under
// today it deletes the Service too; under skew it touches nothing; and it
// never deletes a same-named object it does not own.
func TestReconcileAuditReportStore(t *testing.T) {
	ctx := context.Background()
	key := types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}
	for _, tc := range []struct {
		mode, name  string
		modeOK      bool
		wantService bool
		wantPolicy  bool
	}{
		{"next", "next renders the Service and clears the old policy", true, true, false},
		{"today", "today deletes both", true, false, false},
		{"later", "skew touches nothing", false, true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			owner := reportStoreAgent("next")
			svc := buildAuditReportStoreService(owner)
			if err := ctrl.SetControllerReference(owner, svc, scheme); err != nil {
				t.Fatal(err)
			}
			agent := reportStoreAgent(tc.mode)
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, svc, leftoverStorePolicy(t, owner)).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			if err := r.reconcileAuditReportStore(ctx, agent, tc.modeOK); err != nil {
				t.Fatal(err)
			}
			if err := cl.Get(ctx, key, &corev1.Service{}); (err == nil) != tc.wantService {
				t.Errorf("Service present=%v, want %v (%v)", err == nil, tc.wantService, err)
			}
			if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); (err == nil) != tc.wantPolicy {
				t.Errorf("store NetworkPolicy present=%v, want %v (%v)", err == nil, tc.wantPolicy, err)
			}
		})
	}

	scheme := setupScheme()
	foreign := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}
	foreignSvc := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(foreign, foreignSvc).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileAuditReportStore(ctx, reportStoreAgent("today"), true); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); err != nil {
		t.Errorf("deleted a NetworkPolicy it does not own: %v", err)
	}
	if err := cl.Get(ctx, key, &corev1.Service{}); err != nil {
		t.Errorf("deleted a Service it does not own: %v", err)
	}
}
