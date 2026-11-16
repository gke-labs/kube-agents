package controller

import (
	"context"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/event"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The end-tier-drain lever (#5850): an administrator ends a draining tier's
// running work in one step.

func withEndTierDrain(tiers string) func(*agentv1alpha1.AgentProfile) {
	return func(p *agentv1alpha1.AgentProfile) {
		if p.Annotations == nil {
			p.Annotations = map[string]string{}
		}
		p.Annotations[endTierDrainAnnotation] = tiers
	}
}

// A tier the annotation names, that the profile drains, ends within the grace
// and is marked ending in the dispatch config; a tier it doesn't name drains
// on for the window.
func TestTheEndTierDrainAnnotationEndsADrainWithinTheGrace(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive, "nightly": tierModeUnattended})
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall", "nightly"} })
	prev := appliedEntry(t, appliedConfig(t, agent, cat, nil, &was), "tse")
	now := time.Now()
	owned := map[string]string{"read": tierModeInteractive, "oncall": tierModeInteractive, "nightly": tierModeUnattended}

	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} }, withEndTierDrain("oncall"))
	v := decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if !v.ending["oncall"] || v.ending["nightly"] {
		t.Fatalf("ending %v, want oncall alone", v.ending)
	}
	if end := v.draining["oncall"]; end.After(now.Add(endTierDrainGrace)) {
		t.Errorf("an ended drain runs until %v, past the grace", end)
	}
	if end := v.draining["nightly"]; end.Before(now.Add(a2aDrainingWindow - time.Minute)) {
		t.Errorf("a drain nobody ended was shortened to %v", end)
	}
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{*narrowedCopy(&is, v)})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]bool{}
	for _, d := range cfg.Profiles[0].DrainingTiers {
		got[d.Tier] = d.Ending
	}
	if !got["oncall"] || got["nightly"] {
		t.Errorf("dispatch config ending flags %v, want oncall alone", got)
	}
}

// Once the applied config marks a tier ending, the end is carried from it,
// annotation or not, and is never pushed later; a passed end drops the tier.
func TestAnEndedDrainIsCarriedFromTheAppliedConfig(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	prev := appliedEntry(t, appliedConfig(t, agent, cat, nil, &was), "tse")
	now := time.Now()
	prev.Tiers = []string{"read"}
	prev.DrainingTiers = []a2aDrainingTier{{Tier: "oncall", Mode: tierModeInteractive, Until: now.Add(time.Minute), Ending: true}}
	owned := map[string]string{"read": tierModeInteractive, "oncall": tierModeInteractive}

	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	v := decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if !v.ending["oncall"] || !v.appliedEnding["oncall"] {
		t.Fatalf("ending %v applied %v, want oncall carried", v.ending, v.appliedEnding)
	}
	if !v.draining["oncall"].Equal(now.Add(time.Minute)) {
		t.Errorf("the carried end moved: %v", v.draining["oncall"])
	}
	prev.DrainingTiers[0].Until = now.Add(-time.Second)
	v = decideTiers(&is, cat, nil)
	decideDrains(&is, &v, prev, owned, cat, now)
	if _, ok := v.draining["oncall"]; ok {
		t.Error("an ended drain past its end still drains")
	}
}

// A live tier the annotation names is never ended, and a tier re-added after
// its drain was ended and then dropped again drains anew, for the window, not
// ended: the applied config lists it live in between, so nothing carries.
func TestTheLeverNeverCutsALiveTierOrALaterDrain(t *testing.T) {
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	now := time.Now()
	owned := map[string]string{"read": tierModeInteractive, "oncall": tierModeInteractive}
	live := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} }, withEndTierDrain("oncall"))
	prevLive := appliedEntry(t, appliedConfig(t, agent, cat, nil, &live), "tse")
	v := decideTiers(&live, cat, nil)
	decideDrains(&live, &v, prevLive, owned, cat, now)
	if len(v.ending) != 0 || len(v.draining) != 0 {
		t.Fatalf("a live tier was drained or ended: draining %v ending %v", v.drainingTiers(), v.ending)
	}
	// Re-dropped with no annotation (consumed, or stripped while live).
	again := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	v = decideTiers(&again, cat, nil)
	decideDrains(&again, &v, prevLive, owned, cat, now)
	if v.ending["oncall"] {
		t.Error("a new drain inherited an old end")
	}
	if end := v.draining["oncall"]; end.Before(now.Add(a2aDrainingWindow - time.Minute)) {
		t.Errorf("a new drain got %v, want the full window", end)
	}
}

// consumeEndTierDrain keeps only what it may still act on: a tier the applied
// config marks ending is consumed (Normal event), a live tier and a tier not
// draining are stripped (Warning events), a tier draining but not yet ending
// stays. A conflict leaves the annotation for the next pass.
func TestTheLeverIsConsumedAfterActing(t *testing.T) {
	agent := a2aTestAgent()
	p := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} },
		withEndTierDrain("oncall,pending,read,typo"))
	v := tierVerdict{
		renderable:    []string{"read"},
		draining:      map[string]time.Time{"oncall": time.Now(), "pending": time.Now().Add(time.Hour)},
		appliedEnding: map[string]bool{"oncall": true},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(&p).Build()
	rec := record.NewFakeRecorder(10)
	r := &AgentProfileReconciler{Client: cl, Scheme: scheme, Recorder: rec}
	if err := r.consumeEndTierDrain(context.Background(), &p, v); err != nil {
		t.Fatal(err)
	}
	stored := &agentv1alpha1.AgentProfile{}
	if err := cl.Get(context.Background(), types.NamespacedName{Name: p.Name, Namespace: p.Namespace}, stored); err != nil {
		t.Fatal(err)
	}
	if got := stored.Annotations[endTierDrainAnnotation]; got != "pending" {
		t.Errorf("annotation %q after consuming, want only the tier still to act on", got)
	}
	var events []string
	for len(rec.Events) > 0 {
		events = append(events, <-rec.Events)
	}
	joined := strings.Join(events, "\n")
	for _, want := range []string{"Normal TierDrainEnded", "oncall", "Warning EndTierDrainIgnored", "read", "typo"} {
		if !strings.Contains(joined, want) {
			t.Errorf("events %q lack %q", events, want)
		}
	}

	// Nothing left to drop: no write.
	writes := 0
	quiet := fake.NewClientBuilder().WithScheme(scheme).WithObjects(stored.DeepCopy()).WithInterceptorFuncs(interceptor.Funcs{
		Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			writes++
			return c.Patch(ctx, obj, patch, opts...)
		},
	}).Build()
	r = &AgentProfileReconciler{Client: quiet, Scheme: scheme}
	if err := r.consumeEndTierDrain(context.Background(), stored, v); err != nil || writes != 0 {
		t.Errorf("an annotation with nothing to drop: err %v, %d writes", err, writes)
	}

	// A conflict: no error, and the object is left as it was read.
	fresh := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} },
		withEndTierDrain("oncall,pending,read,typo"))
	conflict := fake.NewClientBuilder().WithScheme(scheme).WithObjects(&fresh).WithInterceptorFuncs(interceptor.Funcs{
		Patch: func(context.Context, client.WithWatch, client.Object, client.Patch, ...client.PatchOption) error {
			return apierrors.NewConflict(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "agentprofiles"}, p.Name, nil)
		},
	}).Build()
	again := fresh.DeepCopy()
	r = &AgentProfileReconciler{Client: conflict, Scheme: scheme}
	if err := r.consumeEndTierDrain(context.Background(), again, v); err != nil {
		t.Fatalf("a conflict returned %v; the next pass should simply try again", err)
	}
	if again.Annotations[endTierDrainAnnotation] != "oncall,pending,read,typo" {
		t.Errorf("after a conflict the in-memory annotation is %q", again.Annotations[endTierDrainAnnotation])
	}
}

// Through the AgentProfile reconciler: the annotation stays while the applied
// config hasn't yet marked the tier ending, and is consumed once it has.
func TestTheProfileReconcilerConsumesTheLeverOnceApplied(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	oncall := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: agent.Namespace}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	cm := appliedConfig(t, agent, cat, nil, &was)
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} }, withEndTierDrain("oncall"))
	ownSA := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "agentprofile-tse-oncall", Namespace: profileNamespaceName(agent),
		Labels:      map[string]string{agentProfileNameLabel: "tse", agentProfileTierLabel: "oncall", agentProfileTierModeLabel: tierModeInteractive},
		Annotations: map[string]string{agentProfileUIDAnnotation: string(is.UID)},
	}}
	h := newProfileHarness(t, agent, &is, oncall, cm, ownSA)
	h.reconcile(agent.Namespace, "tse")
	if got := h.profile(agent.Namespace, "tse").Annotations[endTierDrainAnnotation]; got != "oncall" {
		t.Fatalf("annotation %q before the applied config carries the end; want it kept", got)
	}

	// The PlatformAgent pass applies the drain, ended.
	cm.Data[a2aDispatchConfigKey] = appliedConfig(t, agent, cat, func(c *a2aDispatchConfig) {
		for i := range c.Profiles {
			c.Profiles[i].Tiers = []string{"read"}
			c.Profiles[i].DrainingTiers = []a2aDrainingTier{{Tier: "oncall", Mode: tierModeInteractive, Until: time.Now().Add(time.Minute), Ending: true}}
		}
	}, &was).Data[a2aDispatchConfigKey]
	if err := h.c.Update(t.Context(), cm); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "tse")
	if got, ok := h.profile(agent.Namespace, "tse").Annotations[endTierDrainAnnotation]; ok {
		t.Errorf("annotation %q after the applied config carried the end; want it consumed", got)
	}
	if _, ok := h.serviceAccount(profileNamespaceName(agent), "agentprofile-tse-oncall"); !ok {
		t.Error("the ending tier's ServiceAccount went before its shortened end")
	}
}

// Upgrade and rollback: a dispatch config with no tier ending serializes as
// before (no "ending" key), and one written by an older operator, without
// the field, decodes as not ending.
func TestTheEndingFieldIsAbsentUntilUsed(t *testing.T) {
	d := a2aDrainingTier{Tier: "oncall", Mode: tierModeInteractive, Until: time.Unix(1700000000, 0).UTC()}
	raw, err := json.Marshal(d)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "ending") {
		t.Errorf("a drain nobody ended serializes %s; want the pre-#5850 shape", raw)
	}
	var old a2aDrainingTier
	if err := json.Unmarshal([]byte(`{"tier":"oncall","mode":"interactive","until":"2023-11-14T22:13:20Z"}`), &old); err != nil || old.Ending {
		t.Errorf("an older config decodes as %+v (err %v); want not ending", old, err)
	}
}

// The PlatformAgent pass is asked back at the earliest drain's end, so an
// ended drain's teardown follows its shortened end, not the next resync.
func TestThePassComesBackAtTheEarliestDrainEnd(t *testing.T) {
	now := time.Now()
	mk := func(ends string) agentv1alpha1.AgentProfile {
		return agentv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Annotations: map[string]string{drainingTiersAnnotation: ends}}}
	}
	unix := func(d time.Duration) string { return strconv.FormatInt(now.Add(d).Unix(), 10) }
	got := earliestDrainEnd([]agentv1alpha1.AgentProfile{mk("oncall=" + unix(time.Hour)), mk("nightly=" + unix(90*time.Second) + ",x=" + unix(2*time.Hour))}, now)
	if got < 80*time.Second || got > 100*time.Second {
		t.Errorf("earliestDrainEnd = %s, want about the 90s drain", got)
	}
	if got := earliestDrainEnd([]agentv1alpha1.AgentProfile{mk("")}, now); got != 0 {
		t.Errorf("no drain: %s, want 0", got)
	}
	if got := earliestDrainEnd([]agentv1alpha1.AgentProfile{mk("old=" + unix(-time.Minute))}, now); got != time.Second {
		t.Errorf("a passed end: %s, want the floor", got)
	}
}

// Setting or changing end-tier-drain enqueues the PlatformAgent, which a
// generation-only watch wouldn't: the annotation changes no generation.
func TestTheLeverEnqueuesThePlatformAgent(t *testing.T) {
	old := &agentv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "tse", Generation: 3}}
	set := old.DeepCopy()
	set.Annotations = map[string]string{endTierDrainAnnotation: "oncall"}
	other := old.DeepCopy()
	other.Annotations = map[string]string{"unrelated": "x"}
	p := endTierDrainChanged()
	if !p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: set}) {
		t.Error("setting the lever didn't pass the predicate")
	}
	if p.Update(event.UpdateEvent{ObjectOld: old, ObjectNew: other}) {
		t.Error("an unrelated annotation passed the predicate")
	}
}

// While the lever is set the AgentProfile pass comes back within
// endTierDrainRecheck, to consume it inside the drain's grace.
func TestTheProfilePassComesBackSoonWhileTheLeverIsSet(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	oncall := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: agent.Namespace}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	cm := appliedConfig(t, agent, cat, nil, &was)
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} }, withEndTierDrain("oncall"))
	// The tier's own ServiceAccount, which a drain requires.
	ownSA := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "agentprofile-tse-oncall", Namespace: profileNamespaceName(agent),
		Labels:      map[string]string{agentProfileNameLabel: "tse", agentProfileTierLabel: "oncall", agentProfileTierModeLabel: tierModeInteractive},
		Annotations: map[string]string{agentProfileUIDAnnotation: string(is.UID)},
	}}
	h := newProfileHarness(t, agent, &is, oncall, cm, ownSA)
	res := h.reconcile(agent.Namespace, "tse")
	if res.RequeueAfter == 0 || res.RequeueAfter > endTierDrainRecheck {
		t.Errorf("RequeueAfter %s with the lever set, want at most %s", res.RequeueAfter, endTierDrainRecheck)
	}
}

// The AgentProfile pass, which tears a tier's ServiceAccounts and durables
// down, comes back just after its profile's earliest drain end rather than at
// the 5-minute resync: an ended drain is minutes long, not hours.
func TestTheProfilePassComesBackAtItsDrainEnd(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	cat := catalogWith(map[string]string{"oncall": tierModeInteractive})
	oncall := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: agent.Namespace}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}}
	was := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	cm := appliedConfig(t, agent, cat, func(c *a2aDispatchConfig) {
		for i := range c.Profiles {
			c.Profiles[i].Tiers = []string{"read"}
			c.Profiles[i].DrainingTiers = []a2aDrainingTier{{Tier: "oncall", Mode: tierModeInteractive, Until: time.Now().Add(90 * time.Second), Ending: true}}
		}
	}, &was)
	is := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read"} })
	ownSA := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Name: "agentprofile-tse-oncall", Namespace: profileNamespaceName(agent),
		Labels:      map[string]string{agentProfileNameLabel: "tse", agentProfileTierLabel: "oncall", agentProfileTierModeLabel: tierModeInteractive},
		Annotations: map[string]string{agentProfileUIDAnnotation: string(is.UID)},
	}}
	h := newProfileHarness(t, agent, &is, oncall, cm, ownSA)
	res := h.reconcile(agent.Namespace, "tse")
	if res.RequeueAfter < 60*time.Second || res.RequeueAfter > 95*time.Second {
		t.Errorf("RequeueAfter %s with a drain ending in 90s, want about 90s", res.RequeueAfter)
	}
}
