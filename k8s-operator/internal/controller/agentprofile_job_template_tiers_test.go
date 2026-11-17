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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

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
	got := dispatchJobTemplates(agent, profiles, false)
	want := map[string][]string{
		"reader":    {dispatchJobTierTemplatePrefix + "reader-read"},
		"tse":       {dispatchJobTemplatePrefix + "tse", dispatchJobTierTemplatePrefix + "tse-read"},
		"narrowing": {dispatchJobTierTemplatePrefix + "narrowing-read"},
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
	if got := dispatchJobTemplates(agent, profiles, false); got != nil {
		t.Errorf("without the policy signal: %v, want none", got)
	}
}

// Two profiles whose params would share a name (a profile "a" at a Tier
// resource "b-c", and a profile "a-b" at "c") get no templates at all: the
// policy holds a Job to a param by its ServiceAccount, so one of them would
// run from the other's template. Their permit-carrying tasks are refused
// change-needs-job; every other profile keeps its own.
func TestProfilesWhoseParamNamesCollideGetNoJobTemplates(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	a := testAgentProfile(agent.Namespace, "a", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{"b-c"}
		p.Spec.Identity.ServiceAccountName = "a-own"
	})
	ab := testAgentProfile(agent.Namespace, "a-b", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"c"} })
	profiles := []agentv1alpha1.AgentProfile{a, ab, testAgentProfile(agent.Namespace, "reader")}
	if dispatchJobParamName(&profiles[0], "b-c") != dispatchJobParamName(&profiles[1], "c") {
		t.Fatal("precondition: the two params' names don't collide")
	}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	if resolved["a"].refused != nil || resolved["a-b"].refused != nil {
		t.Skipf("the identity resolver refuses one of them already: %v / %v", resolved["a"].refused, resolved["a-b"].refused)
	}
	got := dispatchJobTemplates(agent, profiles, false)
	if got["a"] != nil || got["a-b"] != nil {
		t.Errorf("colliding profiles kept templates: a %v, a-b %v", paramsOf(got["a"]), paramsOf(got["a-b"]))
	}
	if got["reader"] == nil {
		t.Error("an uninvolved profile lost its template")
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
	before := dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{p}, false)["tse"]
	read := dispatchJobTierTemplatePrefix + "tse-read"
	changed := profileJobTemplates{}
	for name, tmpl := range before {
		changed[name] = tmpl.DeepCopy()
	}
	changed[read].Spec.Containers[0].Resources.Requests[corev1.ResourceCPU] = resource.MustParse("3")
	if dispatchJobTemplatesHash(before) == dispatchJobTemplatesHash(changed) {
		t.Error("a change to the read tier's template left the profile's hash as it was")
	}
	if dispatchJobTemplatesHash(before) != dispatchJobTemplatesHash(dispatchJobTemplates(agent, []agentv1alpha1.AgentProfile{p}, false)["tse"]) {
		t.Error("the same rendering hashes differently twice")
	}
	if ref := dispatchJobTemplateRef("tse", before); ref == nil || !ref.PerTier || ref.Hash != dispatchJobTemplatesHash(before) {
		t.Errorf("the ref %+v doesn't name the profile's one hash, per tier", ref)
	}
	if ref := dispatchJobTemplateRef("tse", nil); ref != nil {
		t.Errorf("a profile with no template has ref %+v", ref)
	}
}
