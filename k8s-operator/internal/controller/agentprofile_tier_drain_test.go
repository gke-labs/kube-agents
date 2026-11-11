package controller

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// appliedConfig is the dispatch config the operator would have applied for
// profiles, as they rendered then, with edit applied to the result.
func appliedConfig(t *testing.T, agent *agentv1alpha1.PlatformAgent, cat tierCatalog, edit func(*a2aDispatchConfig), profiles ...*agentv1alpha1.AgentProfile) *corev1.ConfigMap {
	t.Helper()
	var narrowed []agentv1alpha1.AgentProfile
	for _, p := range profiles {
		narrowed = append(narrowed, *narrowedCopy(p, decideTiers(p, cat, nil)))
	}
	cfg, err := desiredDispatchConfig(agent, narrowed)
	if err != nil {
		t.Fatal(err)
	}
	if edit != nil {
		edit(&cfg)
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aDispatchConfigKey: string(raw)},
	}
}

func appliedEntry(t *testing.T, cm *corev1.ConfigMap, name string) *a2aDispatchProfile {
	t.Helper()
	var cfg a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &cfg); err != nil {
		t.Fatal(err)
	}
	for i := range cfg.Profiles {
		if cfg.Profiles[i].Name == name {
			return &cfg.Profiles[i]
		}
	}
	t.Fatalf("no applied entry for %s", name)
	return nil
}

// A tier the applied config lists that the render drops drains from now,
// for the window; one already draining keeps its end, clamped to the window,
// until it passes; one rendered again stops draining.
func TestADroppedTierDrainsForTheWindow(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive, "nightly": tierModeUnattended})
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall", "nightly"} })
	prev := appliedEntry(t, appliedConfig(t, agent, cat, nil, &was), "tse")
	now := time.Now()

	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	owned := map[string]string{"read": tierModeInteractive, "oncall": tierModeInteractive, "nightly": tierModeUnattended,
		"old": tierModeInteractive, "odd": "admin"}
	v := decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if got := v.drainingTiers(); !slices.Equal(got, []string{"nightly", "oncall"}) {
		t.Fatalf("draining %v, want nightly and oncall", got)
	}
	if v.modes["nightly"] != tierModeUnattended || v.modes["oncall"] != tierModeInteractive {
		t.Errorf("modes %v", v.modes)
	}
	if end := v.draining["oncall"]; end.Before(now.Add(a2aDrainingWindow-time.Second)) || end.After(now.Add(a2aDrainingWindow)) {
		t.Errorf("oncall drains until %v, want the window from now", end)
	}

	// Already draining: the end is kept, a forged one past the window is
	// clamped, and a passed one is gone.
	prev.Tiers = []string{"read"}
	prev.UnattendedTier = ""
	prev.DrainingTiers = []a2aDrainingTier{
		{Tier: "oncall", Mode: tierModeInteractive, Until: now.Add(time.Hour)},
		{Tier: "nightly", Mode: tierModeUnattended, Until: now.Add(100 * time.Hour)},
		{Tier: "old", Mode: tierModeInteractive, Until: now.Add(-time.Minute)},
		{Tier: "bad-name", Mode: tierModeInteractive, Until: now.Add(time.Hour)},
		{Tier: "odd", Mode: "admin", Until: now.Add(time.Hour)},
	}
	v = decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if got := v.drainingTiers(); !slices.Equal(got, []string{"nightly", "oncall"}) {
		t.Fatalf("draining %v, want nightly and oncall", got)
	}
	if !v.draining["oncall"].Equal(now.Add(time.Hour)) {
		t.Errorf("oncall's end moved: %v", v.draining["oncall"])
	}
	if v.draining["nightly"].After(now.Add(a2aDrainingWindow)) {
		t.Errorf("a forged end past the window was kept: %v", v.draining["nightly"])
	}

	// Listed again: it renders and stops draining.
	back := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	v = decideTiers(&back, cat, nil)
	decideDrains(&back, &v, prev, owned, cat, now)
	if _, ok := v.draining["oncall"]; ok || !slices.Contains(v.renderable, "oncall") {
		t.Errorf("a re-listed tier: renderable %v, draining %v", v.renderable, v.drainingTiers())
	}
}

// No drain for a profile on a named ServiceAccount (it serves one tier), or
// one left with no live tier (it leaves the install and drains as a profile).
func TestNoTierDrainForANamedIdentityOrAProfileWithNoLiveTier(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	prev := appliedEntry(t, appliedConfig(t, agent, cat, nil, &was), "tse")

	named := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{"read"}
		p.Spec.Identity.ServiceAccountName = "mine"
	})
	owned := map[string]string{"read": tierModeInteractive, "oncall": tierModeInteractive}
	v := decideTiers(&named, cat, nil)
	decideDrains(&named, &v, prev, owned, cat, time.Now())
	if len(v.draining) != 0 {
		t.Errorf("a named identity drains %v", v.drainingTiers())
	}
	none := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"ghost"} })
	v = decideTiers(&none, cat, nil)
	decideDrains(&none, &v, prev, owned, cat, time.Now())
	if len(v.draining) != 0 {
		t.Errorf("a profile with no live tier drains %v", v.drainingTiers())
	}
}

// The profile reconciler keeps a dropped tier's ServiceAccount and durables
// while it drains, and the card stops advertising it at once; the condition
// says it drains. Once the drain has passed, they go.
func TestTheProfileReconcilerKeepsADrainingTiersIdentityButNotItsCard(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	oncall := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: agent.Namespace}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	cm := appliedConfig(t, agent, cat, nil, &was)
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	// The ServiceAccount the earlier pass made for oncall, which a drain
	// requires.
	ownSA := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "agentprofile-tse-oncall", Namespace: profileNamespaceName(agent),
		Labels:      map[string]string{agentProfileNameLabel: "tse", agentProfileTierLabel: "oncall", agentProfileTierModeLabel: tierModeInteractive},
		Annotations: map[string]string{agentProfileUIDAnnotation: string(is.UID)},
	}}
	h := newProfileHarness(t, agent, &is, oncall, cm, ownSA)
	h.reconcile(agent.Namespace, "tse")

	ns := profileNamespaceName(agent)
	if _, ok := h.serviceAccount(ns, "agentprofile-tse-oncall"); !ok {
		t.Error("the draining tier's ServiceAccount is gone")
	}
	if e := h.dir.entries["tse"]; !slices.Equal(e.card.Tiers, []string{"read"}) {
		t.Errorf("card tiers %v, want [read]", e.card.Tiers)
	}
	got := h.profile(agent.Namespace, "tse")
	if !slices.Equal(got.Status.RenderedTiers, []string{"read"}) {
		t.Errorf("renderedTiers %v, want [read]", got.Status.RenderedTiers)
	}
	if c := condition(got, agentv1alpha1.AgentProfileConditionTiersRendered); !strings.Contains(c.Message, "draining") || !strings.Contains(c.Message, "oncall until") {
		t.Errorf("TiersRendered message %q, want the drain named", c.Message)
	}

	// The drain has passed.
	cm.Data[a2aDispatchConfigKey] = appliedConfig(t, agent, cat, func(c *a2aDispatchConfig) {
		for i := range c.Profiles {
			c.Profiles[i].Tiers = []string{"read"}
			c.Profiles[i].DrainingTiers = []a2aDrainingTier{{Tier: "oncall", Mode: tierModeInteractive, Until: time.Now().Add(-time.Second)}}
		}
	}, &was).Data[a2aDispatchConfigKey]
	if err := h.c.Update(t.Context(), cm); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "tse")
	if _, ok := h.serviceAccount(ns, "agentprofile-tse-oncall"); ok {
		t.Error("the ServiceAccount outlived the drain")
	}
}

// The dispatch config lists a draining tier apart from the live ones, keeps
// its durables so what is queued at it is refused, and the identities and
// pools keep it.
func TestTheDispatchConfigListsADrainingTierApart(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	v := decideTiers(&is, cat, nil)
	until := time.Now().Add(time.Hour).Truncate(time.Second).UTC()
	v.draining = map[string]time.Time{"oncall": until}
	v.modes["oncall"] = tierModeInteractive
	n := narrowedCopy(&is, v)
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{*n})
	if err != nil {
		t.Fatal(err)
	}
	p := cfg.Profiles[0]
	if !slices.Equal(p.Tiers, []string{"read"}) {
		t.Errorf("live tiers %v, want [read]", p.Tiers)
	}
	if len(p.DrainingTiers) != 1 || p.DrainingTiers[0] != (a2aDrainingTier{Tier: "oncall", Mode: tierModeInteractive, Until: until}) {
		t.Errorf("draining %+v", p.DrainingTiers)
	}
	var durables []string
	for _, d := range p.Durables {
		durables = append(durables, d.Name)
	}
	if !slices.Contains(durables, profileDurableName("tse", "oncall", "child")) {
		t.Errorf("durables %v, want the draining tier's", durables)
	}
	var held []string
	for _, d := range desiredProfileDurables(n) {
		held = append(held, d.name)
	}
	if !slices.Contains(held, profileDurableName("tse", "oncall", "top")) {
		t.Errorf("the profile's durables %v, want the draining tier's kept", held)
	}
	res := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{*n})["tse"]
	var tiers []string
	for _, id := range res.identities {
		tiers = append(tiers, id.tier)
	}
	if !slices.Contains(tiers, "oncall") || !interactiveTier(n, "oncall") {
		t.Errorf("identities %v; the draining tier's identity and pool are kept", tiers)
	}
}

// A draining unattended-mode tier beside a new one (the planner's case): the
// Job template moves to the new tier's ServiceAccount, the old tier keeps its
// own for the Jobs still running, and the dispatch config starts nothing new
// at the old one (it drains).
func TestADrainingUnattendedTierBesideANewOne(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"nightly": tierModeUnattended, "weekly": tierModeUnattended})
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "nightly"} })
	prev := appliedEntry(t, appliedConfig(t, agent, cat, nil, &was), "tse")
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "weekly"} })
	v := decideTiers(&is, cat, []string{"read", "nightly"})
	decideDrains(&is, &v, prev, map[string]string{"read": tierModeInteractive, "nightly": tierModeUnattended}, cat, time.Now())
	if len(v.conflict) != 0 || !slices.Contains(v.renderable, "weekly") {
		t.Fatalf("verdict %+v: the new unattended tier must render, the old one isn't listed", v)
	}
	n := narrowedCopy(&is, v)
	if got := unattendedTierOf(n); got != "weekly" {
		t.Errorf("the Job template's tier is %q, want weekly", got)
	}
	tmpl := buildProfileJobTemplate(agent, n, false)
	if want := agentProfileServiceAccountName(n, "weekly"); tmpl.Spec.ServiceAccountName != want {
		t.Errorf("the Job template runs as %q, want %q", tmpl.Spec.ServiceAccountName, want)
	}
	res := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{*n})["tse"]
	var sas []string
	for _, id := range res.identities {
		sas = append(sas, id.serviceAccount)
	}
	if !slices.Contains(sas, agentProfileServiceAccountName(n, "nightly")) {
		t.Errorf("ServiceAccounts %v: the draining tier's running Jobs need theirs", sas)
	}
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{*n})
	if err != nil {
		t.Fatal(err)
	}
	p := cfg.Profiles[0]
	if p.UnattendedTier != "weekly" || slices.Contains(p.Tiers, "nightly") || len(p.DrainingTiers) != 1 || p.DrainingTiers[0].Tier != "nightly" || p.DrainingTiers[0].Mode != tierModeUnattended {
		t.Errorf("dispatch profile %+v: weekly live and unattended, nightly draining", p)
	}
}

// A Tier deleted while a profile drains it stays in the tiers file at the
// mode the drain decided, until the drain ends; a built-in is never added.
func TestADeletedTierStaysInTheTiersFileWhileItDrains(t *testing.T) {
	is := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	v := decideTiers(&is, builtinTierCatalog(), nil)
	v.draining = map[string]time.Time{"oncall": time.Now().Add(time.Hour), "unattended": time.Now().Add(time.Hour)}
	v.modes["oncall"], v.modes["unattended"] = tierModeInteractive, tierModeUnattended
	got := keptDrainingTiers([]agentv1alpha1.AgentProfile{*narrowedCopy(&is, v)})
	want := []a2aTierEntry{{Name: "oncall", Mode: tierModeInteractive}}
	if !slices.Equal(got, want) {
		t.Fatalf("kept %v, want %v", got, want)
	}
	cm, err := buildA2ATiersConfigMap(a2aTestAgent(), []agentv1alpha1.Tier{
		{ObjectMeta: metav1.ObjectMeta{Name: "oncall"}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}},
	}, got...)
	if err != nil {
		t.Fatal(err)
	}
	if body := cm.Data[a2aTiersKey]; strings.Count(body, `"oncall"`) != 1 {
		t.Errorf("tiers file %s, want oncall once", body)
	}
}

// The applied config only says which tiers left and when. A tier the profile
// has no ServiceAccount of its own for (never rendered, or a forged entry)
// doesn't drain; nor does a legacy tier name; a deleted Tier drains at the
// mode its ServiceAccount recorded, whatever the config claims, and not at
// all when the ServiceAccount recorded none.
func TestADrainTakesNothingButTimingFromTheAppliedConfig(t *testing.T) {
	now := time.Now()
	is := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	prev := &a2aDispatchProfile{Name: "tse", Tiers: []string{"read", "forged", "platform", "gone", "bare"},
		DrainingTiers: []a2aDrainingTier{{Tier: "gone", Mode: tierModeInteractive, Until: now.Add(time.Hour)}}}
	owned := map[string]string{"read": tierModeInteractive, "platform": tierModeInteractive, "gone": tierModeUnattended, "bare": ""}
	cat := builtinTierCatalog()
	v := decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if got := v.drainingTiers(); !slices.Equal(got, []string{"gone"}) {
		t.Fatalf("draining %v, want gone alone", got)
	}
	if v.modes["gone"] != tierModeUnattended {
		t.Errorf("gone drains at %q, want the ServiceAccount's unattended, not the config's interactive", v.modes["gone"])
	}
}

// With status lost, a draining unattended tier listed again beside the live
// one isn't counted as an incumbent, so the live one keeps serving rather
// than both conflicting.
func TestTheStatusLostFallbackIgnoresADrainingTier(t *testing.T) {
	cat := catalogWith(map[string]string{"nightly": tierModeUnattended, "weekly": tierModeUnattended})
	p := testAgentProfile("ns", "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "weekly", "nightly"} })
	owned := map[string]string{"read": tierModeInteractive, "weekly": tierModeUnattended, "nightly": tierModeUnattended}
	prev := a2aDispatchProfile{Name: "tse", Tiers: []string{"read", "weekly"}, UnattendedTier: "weekly",
		DrainingTiers: []a2aDrainingTier{{Tier: "nightly", Mode: tierModeUnattended, Until: time.Now().Add(time.Hour)}}}
	v := decideTiers(&p, cat, renderedTiersOf(&p, withoutDraining(owned, prev)))
	if !slices.Contains(v.renderable, "weekly") || !slices.Equal(v.conflict, []string{"nightly"}) {
		t.Errorf("verdict %+v: weekly keeps serving, nightly conflicts", v)
	}
}

// A tier whose drain has ended, its Tier deleted and its ServiceAccount gone,
// still has its durables found: the applied config still lists it.
func TestADurableProbeIncludesTheAppliedConfigsTiers(t *testing.T) {
	agent := a2aTestAgent()
	cat := builtinTierCatalog()
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	cm := appliedConfig(t, agent, cat, func(c *a2aDispatchConfig) {
		for i := range c.Profiles {
			c.Profiles[i].DrainingTiers = []a2aDrainingTier{{Tier: "oncall", Mode: tierModeInteractive, Until: time.Now().Add(-time.Minute)}}
		}
	}, &was)
	h := newProfileHarness(t, agent, &was, cm)
	probe, err := h.r.durableProbeTiers(t.Context(), agent, &was)
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(probe, "oncall") {
		t.Errorf("probe %v, want the ended drain's tier", probe)
	}
}
