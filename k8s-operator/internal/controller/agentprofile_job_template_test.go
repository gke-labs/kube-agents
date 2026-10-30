package controller

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"slices"
	"sort"
	"strings"
	"testing"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/yaml"

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
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	scheme := setupScheme()
	policy, binding := shippedDispatcherJobPolicy(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, chartProfileNamespace(agent), policy, binding}, objs...)...).
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

// Job mode needs the chart's signal that it rendered the Job admission
// policy. Without it there are no Job templates, params or Jobs Role, so the
// dispatcher's unattended tasks run on the pool; and the signal going away
// removes what a pass with it rendered.
func TestNoJobModeWithoutTheAdmissionPolicy(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	rendered := []struct {
		obj  client.Object
		name string
	}{
		{&corev1.PodTemplate{}, dispatchJobTemplatePrefix + "tse"},
		{&corev1.PodTemplate{}, dispatchJobIndexName},
		{&rbacv1.Role{}, a2aDispatcherJobsRoleName(agent)},
		{&rbacv1.RoleBinding{}, a2aDispatcherJobsRoleName(agent)},
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("with the signal: %v / %v", err, profileErr)
	}
	for _, o := range rendered {
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: o.name}, o.obj); err != nil {
			t.Fatalf("precondition: %T %s not rendered with the signal: %v", o.obj, o.name, err)
		}
	}

	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("without the signal: %v / %v", err, profileErr)
	}
	for _, o := range rendered {
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: o.name}, o.obj); !apierrors.IsNotFound(err) {
			t.Errorf("%T %s survived the signal going away: %v", o.obj, o.name, err)
		}
	}
	cfg, err := desiredDispatchConfigWith(agent, profiles, dispatchJobTemplates(agent, profiles, false))
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range cfg.Profiles {
		if p.JobTemplate != nil {
			t.Errorf("profile %s has a Job template without the signal", p.Name)
		}
	}
	if cfg.JobLabels != nil {
		t.Errorf("jobLabels %v without the signal", cfg.JobLabels)
	}
}

// The cross-module fixture for Job mode: the dispatch config with an
// unattended profile's Job template and the Job labels, which a2a/dispatch
// builds a Job from (TestTheRenderedJobConfigBuildsAJob).
const dispatchJobConfigFixturePath = "../../../a2a/dispatch/testdata/rendered-dispatch-config-jobs.json"

func TestRenderedDispatchJobFixture(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	cm, err := buildA2ADispatchConfigMapWith(agent, profiles, dispatchJobTemplates(agent, profiles, false))
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchJobConfigFixturePath, cm.Data[a2aDispatchConfigKey])
}

// The policy admits a Job as an account the index lists and holds it to
// that account's template only if the template is there; a template with no
// index admits any account no template runs as. So after every write the
// operator makes in the profile namespace, while the dispatcher can create
// Jobs (its Jobs RoleBinding exists) the index must exist, and every
// account it lists must have a template running as it. Once the binding is
// gone the order is free. Checked through a
// first render, a profile's ServiceAccount changing, a tier dropped, a
// profile added, the policy signal going away, and the dispatcher removed.
func TestTheJobPolicyHoldsAfterEveryOperatorWrite(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	scheme := setupScheme()
	var c client.Client
	writes := 0
	check := func(op string, obj client.Object) {
		if obj.GetNamespace() != pns {
			return
		}
		writes++
		ctx := context.Background()
		binding := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}) == nil
		var index corev1.PodTemplate
		indexed := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index) == nil
		if binding && !indexed {
			t.Errorf("after %s %T %s: the Jobs RoleBinding exists with no index, so the templates admit any account none runs as", op, obj, obj.GetName())
		}
		if !binding || !indexed {
			return // the dispatcher can't create a Job: nothing to admit
		}
		var all corev1.PodTemplateList
		if err := c.List(ctx, &all, client.InNamespace(pns)); err != nil {
			t.Fatal(err)
		}
		running := map[string]bool{}
		for _, pt := range all.Items {
			if strings.HasPrefix(pt.Name, dispatchJobTemplatePrefix) {
				running[pt.Template.Spec.ServiceAccountName] = true
			}
		}
		if listed := index.Annotations[dispatchJobAccountsAnnotation]; listed != "" {
			for _, sa := range strings.Split(listed, ",") {
				if !running[sa] {
					t.Errorf("after %s %T %s: the index lists %s, which no template runs as, so a Job as %s is held to nothing", op, obj, obj.GetName(), sa, sa)
				}
			}
		}
	}
	ssa := fakeServerSideApplyInterceptors()
	policy, binding := shippedDispatcherJobPolicy(t)
	c = fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				err := ssa.Patch(ctx, cl, obj, patch, opts...)
				check("apply", obj)
				return err
			},
			Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				err := cl.Create(ctx, obj, opts...)
				check("create", obj)
				return err
			},
			Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				err := cl.Delete(ctx, obj, opts...)
				check("delete", obj)
				return err
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	reconcile := func(step string, profiles ...agentv1alpha1.AgentProfile) {
		t.Helper()
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("%s: %v / %v", step, err, profileErr)
		}
		if !dispatcherJobPolicyRendered() {
			return
		}
		// Settled, the index lists exactly the accounts the templates run
		// as, so every unattended profile's Jobs are admitted.
		var want []string
		for _, tmpl := range dispatchJobTemplates(agent, profiles, false) {
			want = append(want, tmpl.Spec.ServiceAccountName)
		}
		sort.Strings(want)
		var index corev1.PodTemplate
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
			t.Fatalf("%s: the index: %v", step, err)
		}
		if got := index.Annotations[dispatchJobAccountsAnnotation]; got != strings.Join(want, ",") {
			t.Errorf("%s: the index lists %q, want %q", step, got, strings.Join(want, ","))
		}
	}
	own := func(name, sa string) agentv1alpha1.AgentProfile {
		return testAgentProfile(agent.Namespace, name, func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
			p.Spec.Identity.ServiceAccountName = sa
		})
	}

	reconcile("first render", own("a", "custom-a"), unattendedProfile(agent, "b"))
	reconcile("a's ServiceAccount changes", own("a", "custom-a2"), unattendedProfile(agent, "b"))
	b := unattendedProfile(agent, "b")
	b.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	reconcile("b drops the unattended tier", own("a", "custom-a2"), b)
	reconcile("c is added", own("a", "custom-a2"), b, unattendedProfile(agent, "c"))
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	reconcile("the policy signal goes away", own("a", "custom-a2"), b, unattendedProfile(agent, "c"))
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	reconcile("the signal is back", own("a", "custom-a2"), unattendedProfile(agent, "c"))
	if err := r.deleteDispatcherPodsRBAC(ctx, agent); err != nil {
		t.Fatalf("the dispatcher removed: %v", err)
	}
	if writes < 20 {
		t.Fatalf("only %d writes in the profile namespace were checked; the sequence didn't exercise the params", writes)
	}
}

// shippedDispatcherJobPolicy is the Job admission policy and its binding
// from the shipped YAML, typed, for a fake client to hold.
func shippedDispatcherJobPolicy(t *testing.T) (*admissionregistrationv1.ValidatingAdmissionPolicy, *admissionregistrationv1.ValidatingAdmissionPolicyBinding) {
	t.Helper()
	raw, err := os.ReadFile(dispatcherJobPolicyFile)
	if err != nil {
		t.Fatal(err)
	}
	var policy *admissionregistrationv1.ValidatingAdmissionPolicy
	var binding *admissionregistrationv1.ValidatingAdmissionPolicyBinding
	for _, doc := range strings.Split(string(raw), "\n---\n") {
		var meta metav1.TypeMeta
		if err := yaml.Unmarshal([]byte(doc), &meta); err != nil {
			t.Fatal(err)
		}
		switch meta.Kind {
		case "ValidatingAdmissionPolicy":
			policy = &admissionregistrationv1.ValidatingAdmissionPolicy{}
			if err := yaml.UnmarshalStrict([]byte(doc), policy); err != nil {
				t.Fatal(err)
			}
		case "ValidatingAdmissionPolicyBinding":
			binding = &admissionregistrationv1.ValidatingAdmissionPolicyBinding{}
			if err := yaml.UnmarshalStrict([]byte(doc), binding); err != nil {
				t.Fatal(err)
			}
		}
	}
	if policy == nil || binding == nil {
		t.Fatalf("%s holds no policy or no binding", dispatcherJobPolicyFile)
	}
	return policy, binding
}
