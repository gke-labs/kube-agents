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

// The listener's switch, subject and port are on agent-api-auth whatever the
// retired spec.mode says, as the A2A stack is, and never on platform-agent.
func TestAuditReportStoreSidecarRendersOnEveryInstall(t *testing.T) {
	for _, tc := range []struct {
		mode string
		want bool
	}{{"", true}, {"today", true}, {"next", true}} {
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
		for _, c := range buildDeployment(agent, "", "", "", "", nil, renderOptions{}).Spec.Template.Spec.Containers {
			if _, ok := storeEnvValue(c.Env, auditReportStoreEnabledEnv); ok && c.Name != agentAPIAuthContainerName {
				t.Errorf("mode %q: container %s carries %s", tc.mode, c.Name, auditReportStoreEnabledEnv)
			}
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

// A CR cannot set the store's names through spec.deployment.env.
func TestAuditReportStoreEnvIsReserved(t *testing.T) {
	agent := reportStoreAgent("")
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
					e.Value == "system:serviceaccount:test-ns:attacker" {
					t.Errorf("spec.deployment.env set %s=%q", name, e.Value)
				}
			}
		}
	}
}

// The broker carries the store token and address on every install, and the
// exec tier derived from it drops both.
func TestAuditReportStoreBrokerTokenAndExecTierDrop(t *testing.T) {
	for _, mode := range []string{"", "today", "next"} {
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
		if volume == nil || !hasURL || !hasFile || !hasProfiles {
			t.Fatalf("mode %q: volume=%v url=%v file=%v profiles=%v, want all", mode, volume != nil, hasURL, hasFile, hasProfiles)
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

// The gateway policy carries the store rule, the broker's pods to 8700,
// exactly once, and nothing else in it admits that port. Without the rule its
// ingress is what main renders (the goldens' gateway-netpol differs from
// main's by this rule alone), so an older operator's rewrite of the policy
// changes nothing but dropping it. spec.networkPolicy.enabled false renders no
// policy, and so no rule.
func TestGatewayNetpolCarriesTheStoreRule(t *testing.T) {
	profile := netpolProfile{Generated: true, OperatorNamespace: "kubeagents-system"}
	agent := reportStoreAgent("")
	np := buildNetworkPolicy(agent, nil, profile, false, "", false)
	want := auditReportStoreIngressRule(agent)
	var count int
	for _, rule := range np.Spec.Ingress {
		if reflect.DeepEqual(rule, want) {
			count++
			continue
		}
		if admits(rule, corev1.ProtocolTCP, auditReportStorePort) {
			t.Errorf("a rule other than the broker's admits 8700: %+v", rule)
		}
	}
	if count != 1 {
		t.Fatalf("the gateway policy carries the store rule %d times, want 1: %+v", count, np.Spec.Ingress)
	}
	broker := credentialProxySelector(agent)
	if got := want.From[0].PodSelector.MatchLabels; !reflect.DeepEqual(got, broker) || len(want.From) != 1 {
		t.Errorf("the store rule admits %+v, want the broker's pods alone", want.From)
	}
	if len(want.Ports) != 1 || want.Ports[0].Port.IntVal != auditReportStorePort {
		t.Errorf("the store rule admits ports %+v, want %d alone", want.Ports, auditReportStorePort)
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

// The reconcile renders the Service and no NetworkPolicy for the store,
// deletes the one an earlier build of this change left, and never deletes a
// same-named policy it does not own.
func TestReconcileAuditReportStore(t *testing.T) {
	ctx := context.Background()
	key := types.NamespacedName{Namespace: "test-ns", Name: "test-agent-report-store"}
	for _, mode := range []string{"", "today", "next"} {
		t.Run("mode "+mode, func(t *testing.T) {
			scheme := setupScheme()
			agent := reportStoreAgent(mode)
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, leftoverStorePolicy(t, agent)).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			if err := r.reconcileAuditReportStore(ctx, agent); err != nil {
				t.Fatal(err)
			}
			svc := &corev1.Service{}
			if err := cl.Get(ctx, key, svc); err != nil {
				t.Errorf("Service not rendered: %v", err)
			} else if !metav1.IsControlledBy(svc, agent) {
				t.Errorf("Service is not controlled by the agent")
			}
			if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); err == nil {
				t.Errorf("the leftover store NetworkPolicy survived")
			}
		})
	}

	scheme := setupScheme()
	agent := reportStoreAgent("")
	foreign := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace}}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, foreign).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileAuditReportStore(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &networkingv1.NetworkPolicy{}); err != nil {
		t.Errorf("deleted a NetworkPolicy it does not own: %v", err)
	}
}
