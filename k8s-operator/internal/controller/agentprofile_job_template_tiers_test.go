package controller

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"sort"
	"testing"
	"time"

	"context"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/version"
	fakediscovery "k8s.io/client-go/discovery/fake"
	clienttesting "k8s.io/client-go/testing"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// dispatchJobTemplateHash is a profile's rendered (not defaulted) template's
// hash. The param and the config's reference both carry it, from the one
// rendering, so a param the config has moved past doesn't match. Defaulting
// is the API server's and is left out: it is the same object the policy
// holds either way.
func dispatchJobTemplateHash(rendered *corev1.PodTemplateSpec) string {
	raw, err := json.Marshal(rendered)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])[:dispatchJobTemplateHashLength]
}

// unattendedJobTemplateSets is templates, each a profile's unattended-mode
// template by profile name, as dispatchJobTemplates returns them: each in the
// profile's own param.
func unattendedJobTemplateSets(templates map[string]*corev1.PodTemplateSpec) map[string]profileJobTemplates {
	out := map[string]profileJobTemplates{}
	for profile, t := range templates {
		out[profile] = profileJobTemplates{dispatchJobTemplatePrefix + profile: t}
	}
	return out
}

// paramsOf is the param names in a profile's template set, sorted.
func paramsOf(set profileJobTemplates) []string {
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// Under Job mode every profile has a template at each live tier it has an
// identity at (#5646): a read-only profile's read tier, where its
// permit-carrying tasks run, and both of a two-tier profile's, the
// unattended-mode tier's in the profile's own param. A draining tier has
// none, so no new Job starts there (#5638). Each runs as its own tier's
// ServiceAccount, labelled with its tier.
func TestEveryLiveTierHasItsOwnJobTemplate(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	until := time.Now().Add(time.Hour)
	profiles := []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "reader"),
		unattendedProfile(agent, "tse"),
		testAgentProfile(agent.Namespace, "narrowing", func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
			p.Annotations = map[string]string{drainingTiersAnnotation: fmt.Sprintf("%s=%d", agentv1alpha1.AgentProfileTierUnattended, until.Unix())}
		}),
	}
	got := dispatchJobTemplates(agent, profiles, false, "")
	want := map[string][]string{
		"reader":    {dispatchJobTierTemplatePrefix + "reader.read"},
		"tse":       {dispatchJobTemplatePrefix + "tse", dispatchJobTierTemplatePrefix + "tse.read"},
		"narrowing": {dispatchJobTierTemplatePrefix + "narrowing.read"},
	}
	for i := range profiles {
		p := &profiles[i]
		if names := paramsOf(got[p.Name]); !slices.Equal(names, want[p.Name]) {
			t.Errorf("profile %s renders %v, want %v", p.Name, names, want[p.Name])
		}
		for name, tmpl := range got[p.Name] {
			tier := tmpl.Labels[agentProfileTierLabel]
			if dispatchJobParamName(p, tier) != name || tmpl.Spec.ServiceAccountName != agentProfileServiceAccountName(p, tier) || tmpl.Labels[agentProfileNameLabel] != p.Name {
				t.Errorf("%s holds tier %s's template under %s as %s", name, tier, tmpl.Labels[agentProfileNameLabel], tmpl.Spec.ServiceAccountName)
			}
		}
	}
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "")
	if got := dispatchJobTemplates(agent, profiles, false, ""); got != nil {
		t.Errorf("without the policy signal: %v, want none", got)
	}
}

// No two (profile, tier) pairs share a param name: the dot between them is
// in neither (a profile "a" at a Tier resource "b-c", and a profile "a-b" at
// "c", would otherwise both be ...-a-b-c), so the policy never holds one's
// Jobs to the other's template.
func TestNoTwoProfileTierPairsShareAParamName(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	a := testAgentProfile(agent.Namespace, "a", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{"b-c"}
		p.Spec.Identity.ServiceAccountName = "a-own"
	})
	ab := testAgentProfile(agent.Namespace, "a-b", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"c"} })
	if x, y := dispatchJobParamName(&a, "b-c"), dispatchJobParamName(&ab, "c"); x == y {
		t.Fatalf("(a, b-c) and (a-b, c) share the param %s", x)
	}
	got := dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{a, ab}, false, "")
	if len(got["a"]) != 1 || len(got["a-b"]) != 1 {
		t.Errorf("rendered a %v, a-b %v, want one template each", paramsOf(got["a"]), paramsOf(got["a-b"]))
	}
}

// A profile's one hash covers every tier's rendering: a change to its read
// tier's template alone moves the hash the config names and every param of
// the profile carries, so no param from an older rendering is read.
func TestTheProfilesHashMovesWithAnyTiersTemplate(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	p := unattendedProfile(agent, "tse")
	before := dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{p}, false, "")["tse"]
	read := dispatchJobTierTemplatePrefix + "tse.read"
	changed := profileJobTemplates{}
	for name, tmpl := range before {
		changed[name] = tmpl.DeepCopy()
	}
	changed[read].Spec.Containers[0].Resources.Requests[corev1.ResourceCPU] = resource.MustParse("3")
	if dispatchJobTemplatesHash(before) == dispatchJobTemplatesHash(changed) {
		t.Error("a change to the read tier's template left the profile's hash as it was")
	}
	if dispatchJobTemplatesHash(before) != dispatchJobTemplatesHash(dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{p}, false, "")["tse"]) {
		t.Error("the same rendering hashes differently twice")
	}
	if ref := dispatchJobTemplateRef("tse", before); ref == nil || !ref.PerTier || ref.Hash != dispatchJobTemplatesHash(before) {
		t.Errorf("the ref %+v doesn't name the profile's one hash, per tier", ref)
	}
	if ref := dispatchJobTemplateRef("tse", nil); ref != nil {
		t.Errorf("a profile with no template has ref %+v", ref)
	}
}

// A param already current (its rendering, labels and defaulting API server)
// costs no dry run and no apply on the next pass: with a template per tier
// that is two calls per param per pass saved. A param whose template was
// edited, or an API server upgrade, has it defaulted and written again.
func TestACurrentParamIsNotDefaultedOrAppliedAgain(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	scheme := setupScheme()
	policy, binding := shippedDispatcherJobPolicy(t)
	var dryRuns, applies int
	ssa := fakeServerSideApplyInterceptors()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent), policy, binding, jobTestNATSCABundle(agent)).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if pt, ok := obj.(*agentv1alpha1.DispatchJobTemplate); ok && isDispatchJobTemplateParam(pt.Name) {
					applies++
				}
				return ssa.Patch(ctx, cl, obj, patch, opts...)
			},
			Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				if _, ok := obj.(*corev1.PodTemplate); ok {
					dryRuns++
				}
				return cl.Create(ctx, obj, opts...)
			},
		}).Build()
	disco := &fakediscovery.FakeDiscovery{Fake: &clienttesting.Fake{}, FakedServerVersion: &version.Info{GitVersion: "v1.36.0"}}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, DiscoveryClient: disco}
	ctx := context.Background()
	profiles := []agentv1alpha1.AgentProfile{unattendedProfile(agent, "tse"), testAgentProfile(agent.Namespace, "reader")}
	pass := func(step string) (int, int) {
		t.Helper()
		dryRuns, applies = 0, 0
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("%s: %v / %v", step, err, profileErr)
		}
		return dryRuns, applies
	}
	if d, a := pass("first"); d != 3 || a != 3 {
		t.Fatalf("first pass: %d dry runs, %d applies, want 3 and 3 (three params)", d, a)
	}
	if d, a := pass("again"); d != 0 || a != 0 {
		t.Errorf("a pass with every param current: %d dry runs, %d applies, want none", d, a)
	}
	// An edited template is written again.
	var pt agentv1alpha1.DispatchJobTemplate
	key := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: dispatchJobTierTemplatePrefix + "reader.read"}
	if err := c.Get(ctx, key, &pt); err != nil {
		t.Fatal(err)
	}
	pt.Spec.Template.Raw = []byte(`{"spec":{"containers":[{"name":"x","image":"evil"}]}}`)
	if err := c.Update(ctx, &pt); err != nil {
		t.Fatal(err)
	}
	if d, a := pass("edited"); d != 1 || a != 1 {
		t.Errorf("one edited param: %d dry runs, %d applies, want 1 and 1", d, a)
	}
	// Another API server defaults again.
	disco.FakedServerVersion = &version.Info{GitVersion: "v1.37.0"}
	if d, a := pass("upgraded"); d != 3 || a != 3 {
		t.Errorf("after an API server upgrade: %d dry runs, %d applies, want 3 and 3", d, a)
	}
	// An unknown server always defaults.
	r.DiscoveryClient = nil
	if d, a := pass("unknown"); d != 3 || a != 3 {
		t.Errorf("with no API server version: %d dry runs, %d applies, want 3 and 3", d, a)
	}
}
