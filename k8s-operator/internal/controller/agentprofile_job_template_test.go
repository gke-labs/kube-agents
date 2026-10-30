package controller

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"testing"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func unattendedProfile(agent *agentv1alpha1.PlatformAgent, name string) agentv1alpha1.AgentProfile {
	return testAgentProfile(agent.Namespace, name, func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
}

func jobTemplateTestReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	withDispatcher(t)
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, chartProfileNamespace(agent)}, objs...)...).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	return &PlatformAgentReconciler{Client: c, Scheme: scheme}, c
}

// One rendering reaches both readers: the template in the dispatch config
// is the one in the admission policy's param, byte for byte, so the Jobs
// the dispatcher stamps from the config are the ones the policy admits. The
// index lists the template's account; a read-only profile gets neither. A
// second pass finds its own params and rewrites them rather than refusing
// them as foreign.
func TestTheJobTemplateInTheConfigIsThePolicysParam(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse"), testAgentProfile(agent.Namespace, "reader")}
	for pass := 1; pass <= 2; pass++ {
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("pass %d: %v / %v", pass, err, profileErr)
		}
	}
	pns := profileNamespaceName(agent)

	var cm corev1.ConfigMap
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, &cm); err != nil {
		t.Fatal(err)
	}
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatal(err)
	}
	var inConfig *corev1.PodTemplateSpec
	for _, p := range cfg.Profiles {
		switch {
		case p.Name == "tse":
			inConfig = p.JobTemplate
		case p.JobTemplate != nil:
			t.Errorf("profile %s, which doesn't allow the unattended tier, has a Job template", p.Name)
		}
	}
	if inConfig == nil {
		t.Fatal("the unattended profile has no Job template in the dispatch config")
	}

	var param corev1.PodTemplate
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &param); err != nil {
		t.Fatalf("the policy's param for tse: %v", err)
	}
	if !apiequality.Semantic.DeepEqual(param.Template, *inConfig) {
		t.Errorf("the param's template differs from the dispatch config's:\nparam  %+v\nconfig %+v", param.Template, *inConfig)
	}
	if param.Template.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Errorf("restartPolicy %q: a PodTemplate defaults it to Always, which a Job pod refuses", param.Template.Spec.RestartPolicy)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "reader"}, &corev1.PodTemplate{}); !apierrors.IsNotFound(err) {
		t.Errorf("a read-only profile has a Job template param: %v", err)
	}
	var index corev1.PodTemplate
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
		t.Fatalf("the policy's index: %v", err)
	}
	if got, want := index.Annotations[dispatchJobAccountsAnnotation], inConfig.Spec.ServiceAccountName; got != want || want == "" {
		t.Errorf("the index lists %q, want the template's account %q", got, want)
	}
	for _, pt := range []*corev1.PodTemplate{&param, &index} {
		for k, v := range dispatchJobParamLabels(agent) {
			if pt.Labels[k] != v {
				t.Errorf("%s: label %s=%q, want %q (the binding selects params by these)", pt.Name, k, pt.Labels[k], v)
			}
		}
	}

	// The profile drops the unattended tier: its param goes, and the index
	// stops listing its account.
	profiles[0].Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("drop the tier: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &corev1.PodTemplate{}); !apierrors.IsNotFound(err) {
		t.Errorf("the param outlived the profile's unattended tier: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
		t.Fatal(err)
	}
	if got := index.Annotations[dispatchJobAccountsAnnotation]; got != "" {
		t.Errorf("the index still lists %q", got)
	}
}

// A PodTemplate under a param's name that isn't this agent's is refused,
// not overwritten, and the refusal stops the Jobs Role from being granted
// (params before the Role).
func TestAForeignTemplateUnderAParamNameIsRefused(t *testing.T) {
	agent := a2aTestAgent()
	foreign := &corev1.PodTemplate{ObjectMeta: metav1.ObjectMeta{
		Name: dispatchJobTemplatePrefix + "tse", Namespace: profileNamespaceName(agent), Labels: map[string]string{"x": "y"},
	}}
	r, c := jobTemplateTestReconciler(t, agent, foreign)
	ctx := context.Background()
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}, true)
	if err != nil {
		t.Fatal(err)
	}
	var refused errForeignDispatchJobParam
	if !errors.As(profileErr, &refused) {
		t.Fatalf("profileErr = %v, want the foreign-param refusal", profileErr)
	}
	var got corev1.PodTemplate
	if err := c.Get(ctx, client.ObjectKeyFromObject(foreign), &got); err != nil || got.Labels["x"] != "y" {
		t.Errorf("the foreign template was touched: %v %v", err, got.Labels)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.Role{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Jobs Role was granted over a refused param: %v", err)
	}
}

// Param custody: no Role the dispatcher renders grants a podtemplate verb,
// so neither the dispatcher nor a worker can write the policy's params. The
// Jobs Role is jobs alone, with no update or patch.
func TestNoDispatcherRoleCanWriteThePolicysParams(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	var roles rbacv1.RoleList
	if err := c.List(ctx, &roles); err != nil {
		t.Fatal(err)
	}
	if len(roles.Items) < 3 {
		t.Fatalf("%d Roles rendered, want the config, pod-read and Jobs Roles at least", len(roles.Items))
	}
	for _, role := range roles.Items {
		for _, rule := range role.Rules {
			if slices.Contains(rule.Resources, "podtemplates") || slices.Contains(rule.Resources, "*") {
				t.Errorf("Role %s/%s grants %v on %v", role.Namespace, role.Name, rule.Verbs, rule.Resources)
			}
		}
	}
	jobs := buildA2ADispatcherJobsRole(agent)
	if len(jobs.Rules) != 1 || !slices.Equal(jobs.Rules[0].Resources, []string{"jobs"}) {
		t.Fatalf("the Jobs Role's rules are %+v, want jobs alone", jobs.Rules)
	}
	for _, v := range jobs.Rules[0].Verbs {
		if v == "update" || v == "patch" || v == "*" {
			t.Errorf("the Jobs Role grants %s: the policy checks CREATE, so a Job must not be reshaped after it", v)
		}
	}
}
