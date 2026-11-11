package controller

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
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
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)}, objs...)...).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	return &PlatformAgentReconciler{Client: c, Scheme: scheme}, c
}

// jobTestNATSCABundle is the agent's NATS CA bundle, which the warm pools
// (reconcileProfilePools) copy into the profile namespace on every pass.
func jobTestNATSCABundle(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aNATSCACertKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"},
	}
}

// The index carries no template, so it goes to the API server without a
// template key at all: sent as null, the CRD's object schema refuses it, and
// no profile gets Job mode (found live on GKE 1.35; envtest's 1.37 server
// let the null through, so this checks the bytes sent).
func TestTheIndexIsSentWithoutATemplate(t *testing.T) {
	raw, err := json.Marshal(buildDispatchJobIndexFor(a2aTestAgent(), []string{"agentprofile-tse-unattended"}))
	if err != nil {
		t.Fatal(err)
	}
	var obj struct {
		Spec map[string]json.RawMessage `json:"spec"`
	}
	if err := json.Unmarshal(raw, &obj); err != nil {
		t.Fatal(err)
	}
	if tmpl, ok := obj.Spec["template"]; ok {
		t.Errorf("the index is sent with template %s; want no template key", tmpl)
	}
	if _, ok := obj.Spec["serviceAccounts"]; !ok {
		t.Errorf("the index is sent without its serviceAccounts: %s", raw)
	}
	// With no accounts it still sends a list: an empty spec, applied over
	// an index that had accounts, is pruned to null and refused (also found
	// live).
	raw, err = json.Marshal(buildDispatchJobIndexFor(a2aTestAgent(), nil))
	if err != nil {
		t.Fatal(err)
	}
	obj.Spec = nil
	if err := json.Unmarshal(raw, &obj); err != nil {
		t.Fatal(err)
	}
	if got := string(obj.Spec["serviceAccounts"]); got != "[]" {
		t.Errorf("an index with no accounts is sent with serviceAccounts %q; want []", got)
	}
	// A template param sends no serviceAccounts key: a null is refused as
	// not an array (found live too).
	tmpl := &corev1.PodTemplateSpec{Spec: corev1.PodSpec{ServiceAccountName: "agentprofile-tse-unattended"}}
	param, err := buildDispatchJobTemplate(a2aTestAgent(), "tse", tmpl, tmpl)
	if err != nil {
		t.Fatal(err)
	}
	raw, err = json.Marshal(param)
	if err != nil {
		t.Fatal(err)
	}
	obj.Spec = nil
	if err := json.Unmarshal(raw, &obj); err != nil {
		t.Fatal(err)
	}
	if sa, ok := obj.Spec["serviceAccounts"]; ok {
		t.Errorf("a template param is sent with serviceAccounts %s; want no key", sa)
	}
}

// indexAccounts is an index param's accounts, nil-safe.
func indexAccounts(p *agentv1alpha1.DispatchJobTemplate) []string {
	if p.Spec.ServiceAccounts == nil {
		return nil
	}
	return *p.Spec.ServiceAccounts
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
	var ref *a2aDispatchJobTemplateRef
	for _, p := range cfg.Profiles {
		switch {
		case p.Name == "tse":
			ref = p.JobTemplateRef
		case p.JobTemplateRef != nil:
			t.Errorf("profile %s, which doesn't allow the unattended tier, has a Job template", p.Name)
		}
	}
	if ref == nil {
		t.Fatal("the unattended profile names no Job template in the dispatch config")
	}
	if ref.Name != dispatchJobTemplatePrefix+"tse" {
		t.Errorf("the config names %s, want the policy's param for tse", ref.Name)
	}
	inConfig := dispatchJobTemplates(agent, profiles, false)["tse"]
	if strings.Contains(cm.Data[a2aDispatchConfigKey], "containers") {
		t.Error("the dispatch config carries a pod template; it should name the param instead")
	}

	var param agentv1alpha1.DispatchJobTemplate
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &param); err != nil {
		t.Fatalf("the policy's param for tse: %v", err)
	}
	held, ok := dispatchJobParamTemplate(&param)
	if !ok {
		t.Fatalf("the param holds no template: %s", param.Spec.Template.Raw)
	}
	if got := param.Annotations[dispatchJobTemplateHashAnnotation]; got == "" || got != ref.Hash {
		t.Errorf("the param carries rendering %q, the config names %q: the dispatcher would hold every task", got, ref.Hash)
	}
	// The fake client's dry run doesn't default, so the param equals the
	// config's template here; on a real API server it is that, defaulted.
	if !apiequality.Semantic.DeepEqual(held, *inConfig) {
		t.Errorf("the param's template differs from the dispatch config's:\nparam  %+v\nconfig %+v", held, *inConfig)
	}
	if held.Spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Errorf("restartPolicy %q: a PodTemplate defaults it to Always, which a Job pod refuses", held.Spec.RestartPolicy)
	}
	// The Job's worker serves its one task and exits, rather than wait for
	// a next one that never comes until the task deadline.
	if got := envOf(held.Spec.Containers[0])["PROFILE_WORKER_ONE_SHOT"]; got != "1" {
		t.Errorf("the Job's worker has PROFILE_WORKER_ONE_SHOT %q, want 1: a pool worker would outlive its task", got)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "reader"}, &agentv1alpha1.DispatchJobTemplate{}); !apierrors.IsNotFound(err) {
		t.Errorf("a read-only profile has a Job template param: %v", err)
	}
	var index agentv1alpha1.DispatchJobTemplate
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
		t.Fatalf("the policy's index: %v", err)
	}
	if got, want := strings.Join(indexAccounts(&index), ","), inConfig.Spec.ServiceAccountName; got != want || want == "" {
		t.Errorf("the index lists %q, want the template's account %q", got, want)
	}
	for _, pt := range []*agentv1alpha1.DispatchJobTemplate{&param, &index} {
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
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &agentv1alpha1.DispatchJobTemplate{}); !apierrors.IsNotFound(err) {
		t.Errorf("the param outlived the profile's unattended tier: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
		t.Fatal(err)
	}
	if got := indexAccounts(&index); len(got) != 0 {
		t.Errorf("the index still lists %q", got)
	}
}

// A PodTemplate under a param's name that isn't this agent's is refused,
// not overwritten, and the refusal stops the Jobs Role from being granted
// (params before the Role).
func TestAForeignTemplateUnderAParamNameIsRefused(t *testing.T) {
	agent := a2aTestAgent()
	foreign := &agentv1alpha1.DispatchJobTemplate{ObjectMeta: metav1.ObjectMeta{
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
	var got agentv1alpha1.DispatchJobTemplate
	if err := c.Get(ctx, client.ObjectKeyFromObject(foreign), &got); err != nil || got.Labels["x"] != "y" {
		t.Errorf("the foreign template was touched: %v %v", err, got.Labels)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.Role{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Jobs Role was granted over a refused param: %v", err)
	}
}

// Param custody: no Role the dispatcher renders grants a podtemplate verb
// or a write on the policy's params, so neither the dispatcher nor a worker
// can change them. The Jobs Role is exactly jobs, with no update or patch,
// and get on the params, by which the dispatcher reads the template its
// config names.
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
			if slices.Contains(rule.Resources, "podtemplates") || slices.Contains(rule.Resources, "*") ||
				(slices.Contains(rule.Resources, "dispatchjobtemplates") && !slices.Equal(rule.Verbs, []string{"get"})) {
				t.Errorf("Role %s/%s grants %v on %v", role.Namespace, role.Name, rule.Verbs, rule.Resources)
			}
		}
	}
	want := []rbacv1.PolicyRule{
		{APIGroups: []string{"batch"}, Resources: []string{"jobs"}, Verbs: []string{"create", "get", "list", "watch", "delete"}},
		{APIGroups: []string{"kubeagents.x-k8s.io"}, Resources: []string{"dispatchjobtemplates"}, Verbs: []string{"get"}},
	}
	if got := buildA2ADispatcherJobsRole(agent).Rules; !reflect.DeepEqual(got, want) {
		t.Fatalf("the Jobs Role's rules are %+v, want exactly %+v", got, want)
	}
}

// Job mode needs the chart's signal that it rendered the Job admission
// policy. Without it there are no Job templates, params or Jobs Role, so the
// dispatcher's unattended tasks have no worker; and the signal going away
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
		{&agentv1alpha1.DispatchJobTemplate{}, dispatchJobTemplatePrefix + "tse"},
		{&agentv1alpha1.DispatchJobTemplate{}, dispatchJobIndexName},
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
	cfg, err := desiredDispatchConfigWith(agent, profiles, dispatchJobTemplates(agent, profiles, false), nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range cfg.Profiles {
		if p.JobTemplateRef != nil {
			t.Errorf("profile %s has a Job template without the signal", p.Name)
		}
	}
	if cfg.JobLabels != nil {
		t.Errorf("jobLabels %v without the signal", cfg.JobLabels)
	}
}

// The cross-module fixtures for Job mode: the dispatch config naming an
// unattended profile's Job template, with the Job labels, and the
// DispatchJobTemplate it names, which a2a/dispatch builds a Job from
// (TestTheRenderedJobConfigBuildsAJob). The param's template is the
// rendering itself; on an API server it is that, defaulted.
const (
	dispatchJobConfigFixturePath = "../../../a2a/dispatch/testdata/rendered-dispatch-config-jobs.json"
	dispatchJobParamFixturePath  = "../../../a2a/dispatch/testdata/rendered-dispatch-job-template.json"
)

func TestRenderedDispatchJobFixture(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	templates := dispatchJobTemplates(agent, profiles, false)
	cm, err := buildA2ADispatchConfigMapWith(agent, profiles, templates, nil)
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchJobConfigFixturePath, cm.Data[a2aDispatchConfigKey])
	param, err := buildDispatchJobTemplate(agent, "tse", templates["tse"], templates["tse"])
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.MarshalIndent(param, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchJobParamFixturePath, string(raw)+"\n")
}

// An unattended profile that leaves the install drains with no Job
// template, even with a template still rendered under its name: nothing new
// is dispatched for a draining profile, so it has nothing to stamp a Job
// from, and the applied config's template is not carried forward.
func TestADrainingProfileHasNoJobTemplate(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	templates := dispatchJobTemplates(agent, profiles, false)
	applied, err := desiredDispatchConfigWith(agent, profiles, templates, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(applied.Profiles) != 1 || applied.Profiles[0].JobTemplateRef == nil {
		t.Fatalf("precondition: the live profile renders %+v, want one with a Job template", applied.Profiles)
	}
	body, err := json.Marshal(applied)
	if err != nil {
		t.Fatal(err)
	}
	var decoded a2aDispatchConfig
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatal(err)
	}
	draining := drainingDispatchProfiles(&decoded, map[string]bool{}, time.Now())
	if len(draining) != 1 || draining[0].JobTemplateRef != nil {
		t.Fatalf("the profile that left drains as %+v, want one entry with no Job template", draining)
	}
	cfg, err := desiredDispatchConfigWith(agent, nil, templates, draining)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.Profiles) != 1 || cfg.Profiles[0].DrainingUntil.IsZero() || cfg.Profiles[0].JobTemplateRef != nil {
		t.Errorf("the draining profile renders %+v, want draining with no Job template", cfg.Profiles)
	}
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
		var index agentv1alpha1.DispatchJobTemplate
		indexed := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index) == nil
		if binding && !indexed {
			t.Errorf("after %s %T %s: the Jobs RoleBinding exists with no index, so the templates admit any account none runs as", op, obj, obj.GetName())
		}
		if !binding || !indexed {
			return // the dispatcher can't create a Job: nothing to admit
		}
		var all agentv1alpha1.DispatchJobTemplateList
		if err := c.List(ctx, &all, client.InNamespace(pns)); err != nil {
			t.Fatal(err)
		}
		running := map[string]bool{}
		for _, pt := range all.Items {
			if strings.HasPrefix(pt.Name, dispatchJobTemplatePrefix) {
				if held, ok := dispatchJobParamTemplate(&pt); ok {
					running[held.Spec.ServiceAccountName] = true
				}
			}
		}
		{
			for _, sa := range indexAccounts(&index) {
				if !running[sa] {
					t.Errorf("after %s %T %s: the index lists %s, which no template runs as, so a Job as %s is held to nothing", op, obj, obj.GetName(), sa, sa)
				}
			}
		}
	}
	ssa := fakeServerSideApplyInterceptors()
	policy, binding := shippedDispatcherJobPolicy(t)
	c = fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)).
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
		var index agentv1alpha1.DispatchJobTemplate
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
			t.Fatalf("%s: the index: %v", step, err)
		}
		if got := strings.Join(indexAccounts(&index), ","); got != strings.Join(want, ",") {
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
	// a's account moves to d in one pass: d sorts after a, so a is
	// rewritten first.
	reconcile("a's account moves to d", own("a", "custom-a3"), b, unattendedProfile(agent, "c"), own("d", "custom-a2"))
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	reconcile("the policy signal goes away", own("a", "custom-a3"), b, unattendedProfile(agent, "c"), own("d", "custom-a2"))
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	reconcile("the signal is back", own("a", "custom-a3"), unattendedProfile(agent, "c"))
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

// Job mode needs the policy in the cluster as the chart renders it, not
// only the chart's signal: with the binding gone or loosened in any of the
// ways the policy's guarantee rests on, there are no Job templates in the
// config and no Jobs Role: a Role an earlier pass granted is revoked, and
// Jobs already running stay.
func TestJobModeNeedsThePolicyInPlace(t *testing.T) {
	for name, loosen := range map[string]func(*admissionregistrationv1.ValidatingAdmissionPolicy, *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (keepPolicy, keepBinding bool){
		"the binding gone": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, _ *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			return true, false
		},
		"the policy gone": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, _ *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			return false, true
		},
		"the policy failing open": func(p *admissionregistrationv1.ValidatingAdmissionPolicy, _ *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			p.Spec.FailurePolicy = ptr.To(admissionregistrationv1.Ignore)
			return true, true
		},
		"not scoped to kube-agents' namespaces": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			delete(b.Spec.MatchResources.NamespaceSelector.MatchLabels, labelPartOf)
			return true, true
		},
		"narrowed by an extra namespace label": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.MatchResources.NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] = "nobody"
			return true, true
		},
		"narrowed by a namespace expression": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.MatchResources.NamespaceSelector.MatchExpressions = []metav1.LabelSelectorRequirement{{Key: "x", Operator: metav1.LabelSelectorOpExists}}
			return true, true
		},
		"params of a built-in kind": func(p *admissionregistrationv1.ValidatingAdmissionPolicy, _ *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			p.Spec.ParamKind = &admissionregistrationv1.ParamKind{APIVersion: "v1", Kind: "PodTemplate"}
			return true, true
		},
		"the binding auditing only": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.ValidationActions = []admissionregistrationv1.ValidationAction{admissionregistrationv1.Audit}
			return true, true
		},
		"no params allowed": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.ParamRef.ParameterNotFoundAction = ptr.To(admissionregistrationv1.AllowAction)
			return true, true
		},
		"params by another label": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.ParamRef.Selector = &metav1.LabelSelector{MatchLabels: map[string]string{"x": "y"}}
			return true, true
		},
		"not scoped to profile namespaces": func(_ *admissionregistrationv1.ValidatingAdmissionPolicy, b *admissionregistrationv1.ValidatingAdmissionPolicyBinding) (bool, bool) {
			b.Spec.MatchResources = nil
			return true, true
		},
	} {
		t.Run(name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, c := jobTemplateTestReconciler(t, agent)
			ctx := context.Background()
			profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
			pns := profileNamespaceName(agent)
			if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
				t.Fatalf("in place: %v / %v", err, profileErr)
			}
			if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); err != nil {
				t.Fatalf("precondition: no Jobs RoleBinding with the policy in place: %v", err)
			}
			policy, binding := &admissionregistrationv1.ValidatingAdmissionPolicy{}, &admissionregistrationv1.ValidatingAdmissionPolicyBinding{}
			if err := c.Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, policy); err != nil {
				t.Fatal(err)
			}
			if err := c.Get(ctx, types.NamespacedName{Name: dispatcherJobPolicyName}, binding); err != nil {
				t.Fatal(err)
			}
			keepPolicy, keepBinding := loosen(policy, binding)
			for obj, keep := range map[client.Object]bool{policy: keepPolicy, binding: keepBinding} {
				var err error
				if keep {
					err = c.Update(ctx, obj)
				} else {
					err = c.Delete(ctx, obj)
				}
				if err != nil {
					t.Fatal(err)
				}
			}
			running := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "dispatch-running", Namespace: pns, Labels: dispatchJobLabels(agent)}}
			if err := c.Create(ctx, running); err != nil {
				t.Fatal(err)
			}
			if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
				t.Fatalf("loosened: %v / %v", err, profileErr)
			}
			for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
				if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, obj); !apierrors.IsNotFound(err) {
					t.Errorf("the Jobs %T survived %s: %v", obj, name, err)
				}
			}
			// Revoked, not torn down: the policy holds only creates, and a
			// Job running now was held to its template when it was made.
			if err := c.Get(ctx, client.ObjectKeyFromObject(running), &batchv1.Job{}); err != nil {
				t.Errorf("a running dispatcher Job went with %s: %v", name, err)
			}
			var cm corev1.ConfigMap
			if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, &cm); err != nil {
				t.Fatal(err)
			}
			if strings.Contains(cm.Data[a2aDispatchConfigKey], `"jobTemplate"`) {
				t.Errorf("the dispatch config still carries a Job template with %s", name)
			}
		})
	}
}

// The chart's signal gone (a deliberate gate-off) tears Job mode down: the
// Jobs Role, the dispatcher's running Jobs and the params all go.
func TestGatingJobModeOffTearsItDown(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	running := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "dispatch-running", Namespace: pns, Labels: dispatchJobLabels(agent)}}
	if err := c.Create(ctx, running); err != nil {
		t.Fatal(err)
	}
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("gated off: %v / %v", err, profileErr)
	}
	for _, o := range []struct {
		obj client.Object
		key types.NamespacedName
	}{
		{&rbacv1.RoleBinding{}, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}},
		{&batchv1.Job{}, client.ObjectKeyFromObject(running)},
		{&agentv1alpha1.DispatchJobTemplate{}, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}},
	} {
		if err := c.Get(ctx, o.key, o.obj); !apierrors.IsNotFound(err) {
			t.Errorf("%T %s survived the gate-off: %v", o.obj, o.key.Name, err)
		}
	}
}

// Where the DispatchJobTemplate kind isn't served (a helm upgrade doesn't
// apply crds/, and the chart then renders no policy), tearing Job mode down
// finds no templates to delete rather than failing every reconcile.
func TestTheJobTeardownToleratesAnUnservedTemplateKind(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).
		WithInterceptorFuncs(interceptor.Funcs{List: func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*agentv1alpha1.DispatchJobTemplateList); ok {
				return &meta.NoKindMatchError{GroupKind: schema.GroupKind{Group: "kubeagents.x-k8s.io", Kind: "DispatchJobTemplate"}}
			}
			return cl.List(ctx, list, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	if err := r.deleteDispatcherJobResources(context.Background(), agent); err != nil {
		t.Errorf("teardown without the template kind: %v", err)
	}
}

// The Job template carries what GKE Autopilot's mutating admission would
// otherwise fill in on the Job, so the policy's comparison holds there (found
// live): every container a request and a limit for cpu, memory and
// ephemeral-storage, the worker's cpu and memory from the profile, and the
// architecture toleration, once.
func TestTheJobTemplateSettlesWhatAutopilotFillsIn(t *testing.T) {
	agent := a2aTestAgent()
	p := unattendedProfile(agent, "tse")
	tmpl := buildProfileJobTemplate(agent, &p, false)
	all := append(append([]corev1.Container{}, tmpl.Spec.Containers...), tmpl.Spec.InitContainers...)
	if len(tmpl.Spec.InitContainers) == 0 {
		t.Fatal("the template has no init container, so this checks only the worker")
	}
	for _, c := range all {
		for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory, corev1.ResourceEphemeralStorage} {
			if _, ok := c.Resources.Requests[name]; !ok {
				t.Errorf("container %s has no %s request: Autopilot would default it, and the Job would no longer equal its template", c.Name, name)
			}
			if _, ok := c.Resources.Limits[name]; !ok {
				t.Errorf("container %s has no %s limit", c.Name, name)
			}
		}
	}
	w := tmpl.Spec.Containers[0].Resources
	if !w.Requests.Cpu().Equal(p.Spec.Resources.Requests.CPU) || !w.Limits.Memory().Equal(p.Spec.Resources.Limits.Memory) {
		t.Errorf("the worker's resources %+v aren't the profile's %+v", w, p.Spec.Resources)
	}
	n := 0
	for _, tol := range tmpl.Spec.Tolerations {
		if tol == jobArchToleration {
			n++
		}
	}
	if n != 1 || len(tmpl.Spec.Tolerations) != 1 {
		t.Errorf("tolerations %+v, want the architecture's alone, once", tmpl.Spec.Tolerations)
	}
	// The worker's ephemeral-storage is the builder's, as a warm pool's pod
	// has it: the pod's disk-backed emptyDirs (they count against the pod's
	// ceiling), plus its log allowance. The settling leaves it be.
	need := profileWorkerEphemeralStorage(tmpl.Spec.Volumes)
	for _, got := range []resource.Quantity{tmpl.Spec.Containers[0].Resources.Requests[corev1.ResourceEphemeralStorage], tmpl.Spec.Containers[0].Resources.Limits[corev1.ResourceEphemeralStorage]} {
		if !got.Equal(need) {
			t.Errorf("the worker's ephemeral-storage %s isn't its emptyDirs plus its log allowance, %s", got.String(), need.String())
		}
	}
	// A container a builder leaves bare is settled too, from the defaults.
	bare := corev1.PodSpec{Containers: []corev1.Container{{Name: "worker"}}, InitContainers: []corev1.Container{{Name: "stage"}}}
	settleForPlatformMutation(&bare, &p)
	if got := bare.InitContainers[0].Resources.Requests[corev1.ResourceEphemeralStorage]; got.String() != "1Gi" {
		t.Errorf("a bare init container's ephemeral-storage request is %q, want Autopilot's default 1Gi", got.String())
	}
}

// A profile that allows the unattended tier and whose requests break GKE
// Autopilot's memory-per-vCPU bounds would have its Jobs refused on Autopilot
// (found live: Autopilot raised 250m/4Gi to 616m). The operator says so
// before the first task: False JobResourcesFit on the profile, and a Warning
// Event on the PlatformAgent.
func TestAProfileOutsideAutopilotsRatioIsFlagged(t *testing.T) {
	agent := a2aTestAgent()
	with := func(cpu, mem string, tiers ...string) agentv1alpha1.AgentProfile {
		p := unattendedProfile(agent, "tse")
		p.Spec.Resources.Requests.CPU, p.Spec.Resources.Requests.Memory = resource.MustParse(cpu), resource.MustParse(mem)
		if len(tiers) > 0 {
			p.Spec.AllowedTiers = tiers
		}
		return p
	}
	for name, c := range map[string]struct {
		p    agentv1alpha1.AgentProfile
		flag bool
	}{
		"within (250m, 512Mi)":           {with("250m", "512Mi"), false},
		"too much memory (250m, 4Gi)":    {with("250m", "4Gi"), true},
		"too little memory (2, 1Gi)":     {with("2", "1Gi"), true},
		"at the upper bound (1, 6.5Gi)":  {with("1", "6656Mi"), false},
		"read only, no Jobs (250m, 4Gi)": {with("250m", "4Gi", agentv1alpha1.AgentProfileTierRead), false},
	} {
		if got := autopilotRatioProblem(&c.p) != ""; got != c.flag {
			t.Errorf("%s: flagged %v, want %v", name, got, c.flag)
		}
	}

	scheme := setupScheme()
	p := with("250m", "4Gi")
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(&p).WithStatusSubresource(&agentv1alpha1.AgentProfile{}).Build()
	ar := &AgentProfileReconciler{Client: cl, Scheme: scheme}
	ctx := context.Background()
	if err := ar.writeStatus(ctx, &p, agent, nil, "", reasonAgentProfileRendered, "", reasonAgentProfilePublished, "", true); err != nil {
		t.Fatal(err)
	}
	cond := meta.FindStatusCondition(p.Status.Conditions, agentv1alpha1.AgentProfileConditionJobResourcesFit)
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != reasonAgentProfileOutsideAutopilotRatio {
		t.Errorf("JobResourcesFit = %+v, want False %s", cond, reasonAgentProfileOutsideAutopilotRatio)
	}
	fine := p.DeepCopy()
	fine.Spec.Resources.Requests.Memory = resource.MustParse("512Mi")
	if err := ar.writeStatus(ctx, fine, agent, nil, "", reasonAgentProfileRendered, "", reasonAgentProfilePublished, "", true); err != nil {
		t.Fatal(err)
	}
	if cond := meta.FindStatusCondition(fine.Status.Conditions, agentv1alpha1.AgentProfileConditionJobResourcesFit); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Errorf("JobResourcesFit for a fitting profile = %+v, want True", cond)
	}

	r, _ := jobTemplateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(10)
	r.Recorder = rec
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{p}, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	var events []string
	for len(rec.Events) > 0 {
		events = append(events, <-rec.Events)
	}
	if !slices.ContainsFunc(events, func(e string) bool {
		return strings.Contains(e, reasonProfileJobResources) && strings.Contains(e, "tse")
	}) {
		t.Errorf("events %v, want a %s Warning naming tse", events, reasonProfileJobResources)
	}
}

// A read of the policy or its binding that fails for any reason but
// NotFound or Forbidden (a timeout, throttling, an upgrade) says nothing
// either way: the operator changes nothing, rather than tear down Job mode
// and the unattended tasks running on it, and reports the error.
func TestATransientPolicyReadChangesNothing(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	scheme := setupScheme()
	policy, binding := shippedDispatcherJobPolicy(t)
	ca := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace}, Data: map[string]string{"ca.crt": "x"}}
	var failing error
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding, ca).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: fakeServerSideApplyInterceptors().Patch,
			Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
				if _, isBinding := obj.(*admissionregistrationv1.ValidatingAdmissionPolicyBinding); isBinding && failing != nil {
					return failing
				}
				return cl.Get(ctx, key, obj, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	failing = apierrors.NewServerTimeout(admissionregistrationv1.Resource("validatingadmissionpolicybindings"), "get", 1)
	if _, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err == nil {
		t.Error("a timed-out read of the binding was swallowed; want it reported")
	}
	for _, o := range []struct {
		obj  client.Object
		name string
	}{
		{&rbacv1.RoleBinding{}, a2aDispatcherJobsRoleName(agent)},
		{&agentv1alpha1.DispatchJobTemplate{}, dispatchJobTemplatePrefix + "tse"},
	} {
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: o.name}, o.obj); err != nil {
			t.Errorf("%T %s went on a transient read failure: %v", o.obj, o.name, err)
		}
	}

	// Forbidden is no transient: the operator's role lacks the get grant,
	// so the policy can't be checked, and Job mode goes.
	failing = apierrors.NewForbidden(admissionregistrationv1.Resource("validatingadmissionpolicybindings"), dispatcherJobPolicyName, errors.New("no grant"))
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("forbidden: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Jobs RoleBinding outlived a Forbidden read of the binding: %v", err)
	}
}

// The binding selects profile namespaces by the label whose absence makes
// the profile namespace not ready, so Job mode can't stand there: the
// dispatcher's Jobs Role goes, rather than outlive the policy's scope.
func TestANotReadyProfileNamespaceRevokesJobMode(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); err != nil {
		t.Fatalf("precondition: no Jobs RoleBinding: %v", err)
	}
	running := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "dispatch-running", Namespace: pns, Labels: dispatchJobLabels(agent)}}
	if err := c.Create(ctx, running); err != nil {
		t.Fatal(err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, false); err != nil || profileErr != nil {
		t.Fatalf("not ready: %v / %v", err, profileErr)
	}
	// Revoked, not torn down: a not-ready namespace may be a transient
	// failure reading it, and the unattended tasks running there stay.
	if err := c.Get(ctx, client.ObjectKeyFromObject(running), &batchv1.Job{}); err != nil {
		t.Errorf("a running dispatcher Job went with the namespace's readiness: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &agentv1alpha1.DispatchJobTemplate{}); err != nil {
		t.Errorf("the param went with the namespace's readiness: %v", err)
	}
	for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, obj); !apierrors.IsNotFound(err) {
			t.Errorf("the Jobs %T outlived the profile namespace's readiness: %v", obj, err)
		}
	}
}

// A profile namespace the policy's binding doesn't select (here without
// part-of: kube-agents) is not held to the policy, so the dispatcher gets
// no Jobs Role there and the config no templates, though the namespace is
// otherwise ready.
func TestAProfileNamespaceThePolicyDoesNotSelectGetsNoJobMode(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); err != nil {
		t.Fatalf("precondition: no Jobs RoleBinding: %v", err)
	}
	var ns corev1.Namespace
	if err := c.Get(ctx, types.NamespacedName{Name: pns}, &ns); err != nil {
		t.Fatal(err)
	}
	delete(ns.Labels, labelPartOf)
	if err := c.Update(ctx, &ns); err != nil {
		t.Fatal(err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("unselected: %v / %v", err, profileErr)
	}
	for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
		if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, obj); !apierrors.IsNotFound(err) {
			t.Errorf("the Jobs %T stayed in a namespace the policy doesn't select: %v", obj, err)
		}
	}
	var cm corev1.ConfigMap
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, &cm); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(cm.Data[a2aDispatchConfigKey], `"jobTemplate"`) {
		t.Error("the dispatch config names a Job template in a namespace the policy doesn't select")
	}
}

// A params pass that fails (here a foreign index under the index's name,
// after a good pass) revokes the dispatcher's Jobs Role and renders the
// dispatch config without Job templates, so the dispatcher doesn't hand
// unattended tasks to a Job it can't create.
func TestAFailedParamsPassRevokesJobMode(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	pns := profileNamespaceName(agent)
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	var index agentv1alpha1.DispatchJobTemplate
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobIndexName}, &index); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(ctx, &index); err != nil {
		t.Fatal(err)
	}
	foreign := &agentv1alpha1.DispatchJobTemplate{ObjectMeta: metav1.ObjectMeta{Name: dispatchJobIndexName, Namespace: pns, Labels: map[string]string{"x": "y"}}}
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true)
	if err != nil {
		t.Fatal(err)
	}
	var refused errForeignDispatchJobParam
	if !errors.As(profileErr, &refused) {
		t.Errorf("profileErr = %v, want the foreign-param refusal", profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); !apierrors.IsNotFound(err) {
		t.Errorf("the Jobs RoleBinding outlived a failed params pass: %v", err)
	}
	var cm corev1.ConfigMap
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, &cm); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(cm.Data[a2aDispatchConfigKey], `"jobTemplate"`) {
		t.Error("the dispatch config still names a Job template after a failed params pass")
	}
}

// A profile that allows only the unattended tier has no warm pool, but its
// Job pods mount the NATS CA bundle all the same: with Job mode on the copy
// is in the profile namespace, and it was there before the first Job template
// or the Jobs grant was written. With Job mode gated off, nothing mounts it
// and it goes.
func TestAnUnattendedOnlyProfileGetsTheCABundleForItsJobs(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	scheme := setupScheme()
	policy, binding := shippedDispatcherJobPolicy(t)
	pns := profileNamespaceName(agent)
	bundleKey := types.NamespacedName{Namespace: pns, Name: a2aNATSCABundleName(agent)}
	apply := fakeServerSideApplyInterceptors()
	var early []string
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				_, param := obj.(*agentv1alpha1.DispatchJobTemplate)
				_, grant := obj.(*rbacv1.RoleBinding)
				if (param || grant && obj.GetName() == a2aDispatcherJobsRoleName(agent)) && obj.GetNamespace() == pns {
					if err := cl.Get(ctx, bundleKey, &corev1.ConfigMap{}); err != nil {
						early = append(early, obj.GetName())
					}
				}
				return apply.Patch(ctx, cl, obj, patch, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	p := unattendedProfile(agent, "nightly")
	p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	profiles := []agentv1alpha1.AgentProfile{p}
	for pass := 1; pass <= 2; pass++ {
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("pass %d: %v / %v", pass, err, profileErr)
		}
		if err := c.Get(ctx, bundleKey, &corev1.ConfigMap{}); err != nil {
			t.Fatalf("pass %d: no CA bundle in the profile namespace for the Job pods to mount: %v", pass, err)
		}
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "nightly"}, &agentv1alpha1.DispatchJobTemplate{}); err != nil {
		t.Fatalf("the unattended-only profile has no Job template: %v", err)
	}
	if len(early) > 0 {
		t.Errorf("written before the CA bundle was copied: %v", early)
	}

	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("gate off: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, bundleKey, &corev1.ConfigMap{}); !apierrors.IsNotFound(err) {
		t.Errorf("the CA bundle outlived Job mode with no pool to mount it: %v", err)
	}
}

// With no warm pool, Job mode revoked (here the profile namespace losing the
// label the policy's binding selects it by) leaves the dispatcher's running
// Jobs to finish, so the CA bundle their pods mount stays with them.
func TestARevokedJobModeKeepsTheCABundleForItsRunningJobs(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	p := unattendedProfile(agent, "nightly")
	p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	profiles := []agentv1alpha1.AgentProfile{p}
	pns := profileNamespaceName(agent)
	bundleKey := types.NamespacedName{Namespace: pns, Name: a2aNATSCABundleName(agent)}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, bundleKey, &corev1.ConfigMap{}); err != nil {
		t.Fatalf("precondition: no CA bundle in the profile namespace: %v", err)
	}
	running := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: "dispatch-running", Namespace: pns, Labels: dispatchJobLabels(agent)}}
	if err := c.Create(ctx, running); err != nil {
		t.Fatal(err)
	}
	var ns corev1.Namespace
	if err := c.Get(ctx, types.NamespacedName{Name: pns}, &ns); err != nil {
		t.Fatal(err)
	}
	delete(ns.Labels, labelPartOf)
	if err := c.Update(ctx, &ns); err != nil {
		t.Fatal(err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("revoked: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}, &rbacv1.RoleBinding{}); !apierrors.IsNotFound(err) {
		t.Fatalf("precondition: Job mode wasn't revoked: %v", err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(running), &batchv1.Job{}); err != nil {
		t.Fatalf("precondition: the running Job went with the revoke: %v", err)
	}
	if err := c.Get(ctx, bundleKey, &corev1.ConfigMap{}); err != nil {
		t.Errorf("the CA bundle went with the revoke, under a running Job: %v", err)
	}
}

// One pass reads each thing once, uncached: with Job templates and a read
// pool, the CA bundle is copied once (before the templates, not again for the
// pool); gated off, the Jobs Role is revoked once (in the revoke arm, not
// again by the sweep).
func TestAPassDoesNotRepeatItsUncachedReads(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	scheme := setupScheme()
	policy, binding := shippedDispatcherJobPolicy(t)
	pns := profileNamespaceName(agent)
	srcBundle := types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSCABundleName(agent)}
	jobsRole := types.NamespacedName{Namespace: pns, Name: a2aDispatcherJobsRoleName(agent)}
	var mu sync.Mutex
	gets := map[string]int{}
	funcs := fakeServerSideApplyInterceptors()
	funcs.Get = func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
		mu.Lock()
		gets[fmt.Sprintf("%T %s", obj, key)]++
		mu.Unlock()
		return cl.Get(ctx, key, obj, opts...)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)).
		WithInterceptorFuncs(funcs).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse")}
	count := func(obj client.Object, key types.NamespacedName) int {
		mu.Lock()
		defer mu.Unlock()
		return gets[fmt.Sprintf("%T %s", obj, key)]
	}
	reset := func() {
		mu.Lock()
		defer mu.Unlock()
		clear(gets)
	}

	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: profilePoolName("tse", agentv1alpha1.AgentProfileTierRead)}, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: no read pool: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + "tse"}, &agentv1alpha1.DispatchJobTemplate{}); err != nil {
		t.Fatalf("precondition: no Job template: %v", err)
	}
	reset()
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("second pass: %v / %v", err, profileErr)
	}
	if n := count(&corev1.ConfigMap{}, srcBundle); n != 1 {
		t.Errorf("the NATS CA bundle was read %d times in one pass, want 1", n)
	}

	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	reset()
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("gate off: %v / %v", err, profileErr)
	}
	for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
		if n := count(obj, jobsRole); n != 1 {
			t.Errorf("the Jobs %T was read %d times in the gate-off pass, want 1", obj, n)
		}
		if err := c.Get(ctx, jobsRole, obj); !apierrors.IsNotFound(err) {
			t.Errorf("the Jobs %T survived the gate-off: %v", obj, err)
		}
	}
}

// Past the tool limit with image volumes off, a profile's Job pods would never
// schedule on Autopilot, as its pool's wouldn't: it gets no Job template, the
// reconcile names it as a profile-namespace error, and the other profiles keep
// theirs. With image volumes on, it gets its template.
func TestAProfilePastTheToolLimitGetsNoJobTemplateWithoutImageVolumes(t *testing.T) {
	agent := a2aTestAgent()
	r, c := jobTemplateTestReconciler(t, agent)
	ctx := context.Background()
	pns := profileNamespaceName(agent)
	big := unattendedProfile(agent, "big")
	big.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	for i := 0; i <= profileWorkerAutopilotToolLimit; i++ {
		big.Spec.Tools = append(big.Spec.Tools, agentv1alpha1.AgentProfileTool{Name: "tool" + strconv.Itoa(i), Image: "registry.example/tool" + strconv.Itoa(i) + ":1"})
	}
	profiles := []agentv1alpha1.AgentProfile{big, unattendedProfile(agent, "small")}
	param := func(name string) error {
		return c.Get(ctx, types.NamespacedName{Namespace: pns, Name: dispatchJobTemplatePrefix + name}, &agentv1alpha1.DispatchJobTemplate{})
	}

	agent.Annotations = map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "false"}
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true)
	if err != nil || profileErr == nil {
		t.Fatalf("a profile past the tool limit should surface as a profile-namespace error: %v / %v", err, profileErr)
	}
	if want := "AgentProfile big has " + strconv.Itoa(profileWorkerAutopilotToolLimit+1) + " tools"; !strings.Contains(profileErr.Error(), want) || !strings.Contains(profileErr.Error(), "Job template") {
		t.Errorf("the error %q doesn't name the profile's Job template and its tools (%q)", profileErr, want)
	}
	if err := param("big"); !apierrors.IsNotFound(err) {
		t.Errorf("a profile past the tool limit got a Job template without image volumes: %v", err)
	}
	var cm corev1.ConfigMap
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, &cm); err != nil {
		t.Fatal(err)
	}
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatal(err)
	}
	for _, p := range cfg.Profiles {
		if p.Name == "big" && p.JobTemplateRef != nil {
			t.Error("the dispatch config names a Job template for a profile past the tool limit")
		}
	}
	if err := param("small"); err != nil {
		t.Errorf("a profile past the tool limit held up another profile's Job template: %v", err)
	}

	agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"] = "true"
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
		t.Fatalf("with image volumes: %v / %v", err, profileErr)
	}
	if err := param("big"); err != nil {
		t.Errorf("with image volumes the profile got no Job template: %v", err)
	}
}
