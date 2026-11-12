package controller

import (
	"context"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func catalogWith(extra map[string]string) tierCatalog {
	cat := builtinTierCatalog()
	for k, v := range extra {
		cat[k] = v
	}
	return cat
}

func profileListing(tiers ...string) *agentv1alpha1.AgentProfile {
	p := testAgentProfile("test-ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = tiers })
	return &p
}

// A listed name with no tier renders nothing; the rest render.
func TestAnUnknownTierRendersNothingAndTheRestRender(t *testing.T) {
	v := decideTiers(profileListing("read", "oncall"), builtinTierCatalog(), nil)
	if !slices.Equal(v.renderable, []string{"read"}) || !slices.Equal(v.unknown, []string{"oncall"}) || v.reason() != reasonTierUnknown {
		t.Fatalf("verdict %+v", v)
	}
	v = decideTiers(profileListing("read", "oncall"), catalogWith(map[string]string{"oncall": tierModeInteractive}), nil)
	if !slices.Equal(v.renderable, []string{"read", "oncall"}) || len(v.unknown) != 0 || v.reason() != reasonTiersRendered {
		t.Fatalf("verdict with oncall defined %+v", v)
	}
}

// Two unattended-mode tiers: the incumbent keeps serving and the newcomer is
// refused; a profile that rendered neither renders neither.
func TestASecondUnattendedTierKeepsTheIncumbent(t *testing.T) {
	cat := catalogWith(map[string]string{"nightly": tierModeUnattended})
	v := decideTiers(profileListing("read", "unattended", "nightly"), cat, []string{"read", "unattended"})
	if !slices.Equal(v.renderable, []string{"read", "unattended"}) || !slices.Equal(v.conflict, []string{"nightly"}) {
		t.Fatalf("incumbent unattended: %+v", v)
	}
	v = decideTiers(profileListing("read", "unattended", "nightly"), cat, []string{"read", "nightly"})
	if !slices.Equal(v.renderable, []string{"read", "nightly"}) || !slices.Equal(v.conflict, []string{"unattended"}) {
		t.Fatalf("incumbent nightly: %+v", v)
	}
	v = decideTiers(profileListing("read", "unattended", "nightly"), cat, nil)
	if !slices.Equal(v.renderable, []string{"read"}) || len(v.conflict) != 2 || v.reason() != reasonTierModeConflict {
		t.Fatalf("a new profile listing two: %+v", v)
	}
}

// A forged renderedTiers entry for a tier the profile never lists, or that
// the catalog doesn't hold, creates nothing: rendered only picks among listed,
// known tiers.
func TestAForgedRenderedTierCreatesNothing(t *testing.T) {
	cat := catalogWith(map[string]string{"nightly": tierModeUnattended})
	v := decideTiers(profileListing("read"), cat, []string{"read", "nightly", "admin"})
	if !slices.Equal(v.renderable, []string{"read"}) || len(v.unknown) != 0 || len(v.conflict) != 0 {
		t.Fatalf("forged rendered tiers changed the verdict: %+v", v)
	}
	if _, ok := v.modes["nightly"]; ok {
		t.Error("a forged tier got a mode")
	}
}

// With status dropped (a restore), the tiers rendered are derived once from
// the profile's own ServiceAccounts, so the incumbent stays incumbent. One
// carrying another profile's UID, or a copied label, confers nothing.
func TestRenderedTiersAreDerivedFromOwnServiceAccountsWhenStatusIsGone(t *testing.T) {
	p := profileListing("read", "unattended", "nightly")
	p.UID = types.UID("uid-tse")
	ns := profileNamespaceOf(p)
	own := func(name, uid string) *corev1.ServiceAccount {
		return &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: ns,
			Labels:      map[string]string{agentProfileNameLabel: "tse"},
			Annotations: map[string]string{agentProfileUIDAnnotation: uid},
		}}
	}
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(
		own("agentprofile-tse-read", "uid-tse"),
		own("agentprofile-tse-unattended", "uid-tse"),
		own("agentprofile-tse-nightly", "uid-someone-else"), // copied label, wrong UID
	).Build()
	owned, err := ownedTierServiceAccounts(context.Background(), c, p)
	got := renderedTiersOf(p, owned)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got, []string{"read", "unattended"}) {
		t.Fatalf("derived %v, want [read unattended]", got)
	}
	v := decideTiers(p, catalogWith(map[string]string{"nightly": tierModeUnattended}), got)
	if !slices.Equal(v.renderable, []string{"read", "unattended"}) {
		t.Fatalf("after a restore the incumbent lost: %+v", v)
	}
	p.Status.RenderedTiers = []string{"read"}
	if got := renderedTiersOf(p, nil); !slices.Equal(got, []string{"read"}) {
		t.Errorf("a present status was overridden by derivation: %v", got)
	}
}

// The renderers see each profile narrowed to its renderable tiers, carrying
// their modes; a profile with none is left out.
func TestRenderableProfilesAreNarrowedAndCarryTheirModes(t *testing.T) {
	nightly := agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "nightly", Namespace: "test-ns"}, Spec: agentv1alpha1.TierSpec{Mode: tierModeUnattended}}
	oncall := agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: "test-ns"}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	skipped := agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "platform", Namespace: "test-ns"}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(&nightly, &oncall, &skipped).Build()
	a := testAgentProfile("test-ns", "a", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"oncall", "nightly"} })
	b := testAgentProfile("test-ns", "b", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"ghost"} })
	pl := testAgentProfile("test-ns", "pl", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"platform"} })
	got, verdicts, err := renderableAgentProfiles(context.Background(), c, "test-ns", []agentv1alpha1.AgentProfile{a, b, pl}, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Name != "a" {
		t.Fatalf("renderable %v, want a alone", got)
	}
	if !interactiveTier(&got[0], "oncall") || interactiveTier(&got[0], "nightly") || unattendedTierOf(&got[0]) != "nightly" {
		t.Errorf("modes on the narrowed copy: %q", got[0].Annotations[renderedTierModesAnnotation])
	}
	if verdicts["b"].reason() != reasonTierUnknown || verdicts["pl"].reason() != reasonTierUnknown {
		t.Errorf("ghost and a skipped Tier must be unknown: %+v %+v", verdicts["b"], verdicts["pl"])
	}
	if a.Annotations[renderedTierModesAnnotation] != "" {
		t.Error("the caller's profile was mutated; only the copy carries modes")
	}
	// A role that can't read Tiers: custom tiers fail closed.
	got, _, _ = renderableAgentProfiles(context.Background(), c, "test-ns", []agentv1alpha1.AgentProfile{a}, true)
	if len(got) != 0 {
		t.Errorf("with Tiers unreadable a custom-tier profile rendered: %v", got)
	}
}

// The profile reconciler renders, publishes and queues only the tiers that
// render. A profile listing read and an unknown tier gets read's
// ServiceAccount, a card advertising read alone and read's durables, and its
// status records read as rendered, with TiersRendered False/TierUnknown.
func TestTheProfileReconcilerRendersOnlyTheTiersThatRender(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "ghost"} })
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "tse")

	ns := profileNamespaceName(agent)
	if _, ok := h.serviceAccount(ns, "agentprofile-tse-read"); !ok {
		t.Error("read's ServiceAccount is missing")
	}
	if _, ok := h.serviceAccount(ns, "agentprofile-tse-ghost"); ok {
		t.Error("an unknown tier got a ServiceAccount")
	}
	if e := h.dir.entries["tse"]; e.kind != a2aKindAgentCard || !slices.Equal(e.card.Tiers, []string{"read"}) {
		t.Errorf("card %+v, want tiers [read]", e)
	}
	for _, d := range h.dir.durables["tse"] {
		if strings.Contains(d.name, "ghost") {
			t.Errorf("an unknown tier got durable %s", d.name)
		}
	}
	got := h.profile(agent.Namespace, "tse")
	if !slices.Equal(got.Status.RenderedTiers, []string{"read"}) {
		t.Errorf("renderedTiers %v, want [read]", got.Status.RenderedTiers)
	}
	if c := condition(got, agentv1alpha1.AgentProfileConditionTiersRendered); c.Status != metav1.ConditionFalse || c.Reason != reasonTierUnknown {
		t.Errorf("TiersRendered = %s/%s, want False/%s", c.Status, c.Reason, reasonTierUnknown)
	}
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Status != metav1.ConditionTrue {
		t.Errorf("IdentityReady = %s/%s; the known tier still renders", c.Status, c.Reason)
	}
}

// A profile whose only tier is unknown renders nothing at all.
func TestAProfileWithOnlyAnUnknownTierRendersNothing(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"ghost"} })
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "tse")
	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-tse-ghost"); ok {
		t.Error("an unknown tier got a ServiceAccount")
	}
	if _, ok := h.dir.entries["tse"]; ok {
		t.Error("a profile with no renderable tier published a card")
	}
	got := h.profile(agent.Namespace, "tse")
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Status != metav1.ConditionFalse || c.Reason != reasonTierUnknown {
		t.Errorf("IdentityReady = %s/%s, want False/%s", c.Status, c.Reason, reasonTierUnknown)
	}
	if len(got.Status.RenderedTiers) != 0 {
		t.Errorf("renderedTiers %v, want none", got.Status.RenderedTiers)
	}
}

// The dispatch config names a profile's unattended-mode tier when it is a Tier
// resource, and leaves it unset for the built-in (which the dispatcher knows),
// so existing configs don't change.
func TestTheDispatchConfigNamesACustomUnattendedTier(t *testing.T) {
	agent := a2aTestAgent()
	nightly := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "nightly"} })
	builtin := testAgentProfile(agent.Namespace, "aud", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "unattended"} })
	cat := catalogWith(map[string]string{"nightly": tierModeUnattended})
	n := narrowedCopy(&nightly, decideTiers(&nightly, cat, nil))
	b := narrowedCopy(&builtin, decideTiers(&builtin, cat, nil))
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{*n, *b})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for _, p := range cfg.Profiles {
		got[p.Name] = p.UnattendedTier
	}
	if got["tse"] != "nightly" || got["aud"] != "" {
		t.Errorf("unattendedTier = %v, want tse:nightly and aud unset", got)
	}
}

// A draining profile keeps its own unattended-mode tier in the dispatch
// config, so the dispatcher still cleans up that tier's Jobs and counts its
// adopted tasks as unattended; one that isn't among the entry's tiers is not
// carried.
func TestADrainingProfileKeepsItsUnattendedTier(t *testing.T) {
	now := time.Now()
	applied := &a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: "gone", Tiers: []string{"read", "nightly"}, UnattendedTier: "nightly"},
		{Name: "odd", Tiers: []string{"read"}, UnattendedTier: "weekly"},
	}}
	got := drainingDispatchProfiles(applied, map[string]bool{}, now)
	byName := map[string]a2aDispatchProfile{}
	for _, p := range got {
		byName[p.Name] = p
	}
	if byName["gone"].UnattendedTier != "nightly" {
		t.Errorf("gone drains with unattendedTier %q, want nightly", byName["gone"].UnattendedTier)
	}
	if byName["odd"].UnattendedTier != "" {
		t.Errorf("odd carried unattendedTier %q, which isn't one of its tiers", byName["odd"].UnattendedTier)
	}
}

// A hand-made ServiceAccount under a refused tier's name doesn't refuse the
// profile: the operator renders nothing for that tier, so nothing collides.
// The profile's renderable tiers render as usual.
func TestAForeignServiceAccountForARefusedTierDoesNotRefuseTheProfile(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "ghost"} })
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "agentprofile-tse-ghost", Namespace: profileNamespaceName(agent)}}
	h := newProfileHarness(t, agent, &p, foreign)
	h.reconcile(agent.Namespace, "tse")
	got := h.profile(agent.Namespace, "tse")
	if c := condition(got, agentv1alpha1.AgentProfileConditionIdentityReady); c.Status != metav1.ConditionTrue {
		t.Errorf("IdentityReady = %s/%s: %s; the foreign ServiceAccount is for a tier that doesn't render", c.Status, c.Reason, c.Message)
	}
	bound, err := boundAgentProfiles(context.Background(), h.c, agent, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(bound) != 1 || bound[0].Name != "tse" {
		t.Errorf("bound profiles %v, want tse", bound)
	}
}

// A refused map keeps the operator re-checking: the refusal can clear inside
// the callout alone, once the tiers file arrives.
func TestARefusedMapIsUnsettled(t *testing.T) {
	if !(&calloutMapState{reason: a2aIdentityMapRefused}).unsettled() {
		t.Error("a refused map is settled; nothing would re-read it when the callout takes it")
	}
	if (&calloutMapState{reason: a2aIdentityMapServing}).unsettled() {
		t.Error("a served map is unsettled")
	}
}
