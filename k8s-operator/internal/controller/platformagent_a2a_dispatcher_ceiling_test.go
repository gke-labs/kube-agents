/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// ceilingProfiles is n profiles at two tiers with topic grants: the fleet-scale
// shape, in which the dispatcher's entry grows about 1.15 KB per profile.
func ceilingProfiles(agent *agentv1alpha1.PlatformAgent, n int) []agentv1alpha1.AgentProfile {
	profiles := make([]agentv1alpha1.AgentProfile, 0, n)
	for i := 0; i < n; i++ {
		name := fmt.Sprintf("cluster-%04d", i)
		profiles = append(profiles, testAgentProfile(agent.Namespace, name, func(p *agentv1alpha1.AgentProfile) {
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
			p.Spec.Bus.PublishTopics = []string{"agent." + name + ".findings"}
			p.Spec.Bus.SubscribeTopics = []string{"shared.blueprint"}
		}))
	}
	return profiles
}

// TestTheDispatcherCeiling: the dispatcher's identity entry is measured as a
// shard carries it. Well under the budget the condition says so; at four
// fifths of it the condition warns, naming the profile count and #5381; past
// what the live profiles may take, the newest profiles are refused and the
// dispatcher serves the rest, with every shard under the budget
// (gke-labs/kube-agents#5381).
func TestTheDispatcherCeiling(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	agent := authMapTestAgent()
	for _, tc := range []struct {
		profiles int
		reason   string
		status   metav1.ConditionStatus
	}{
		{100, dispatcherCeilingReasonWithin, metav1.ConditionTrue},
		{600, dispatcherCeilingReasonNear, metav1.ConditionTrue},
		{800, dispatcherCeilingReasonRefused, metav1.ConditionFalse},
	} {
		t.Run(fmt.Sprint(tc.profiles), func(t *testing.T) {
			profiles := ceilingProfiles(agent, tc.profiles)
			cond, over, err := a2aDispatcherCeiling(agent, profiles, nil)
			if err != nil {
				t.Fatal(err)
			}
			if cond.Type != dispatcherCeilingCondition || cond.Reason != tc.reason || cond.Status != tc.status || over {
				t.Fatalf("at %d profiles: %s/%s over=%v, want %s/%s", tc.profiles, cond.Status, cond.Reason, over, tc.status, tc.reason)
			}
			if tc.reason != dispatcherCeilingReasonWithin && !strings.Contains(cond.Message, dispatcherCeilingIssue) {
				t.Errorf("the message doesn't name %s: %q", dispatcherCeilingIssue, cond.Message)
			}
			refused := a2aDispatcherCeilingRefused(agent, profiles)
			if (len(refused) > 0) != (tc.reason == dispatcherCeilingReasonRefused) {
				t.Errorf("at %d profiles %d are refused", tc.profiles, len(refused))
			}
			doc, err := renderA2AAuthMap(agent, profiles)
			if err != nil {
				t.Fatal(err)
			}
			users := map[string]bool{}
			for _, id := range doc.Identities {
				users[id.User] = true
			}
			if !users[a2aDispatcherUser] {
				t.Errorf("at %d profiles the dispatcher isn't in the map; past the ceiling it serves the bound profiles", tc.profiles)
			}
			for _, name := range refused {
				for user := range users {
					if strings.HasPrefix(user, "profile-"+name+"-") {
						t.Errorf("refused profile %s still has identity %s in the map", name, user)
					}
				}
			}
			cms, _, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
			if err != nil {
				t.Fatal(err)
			}
			for _, cm := range cms {
				if len(cm.Data[a2aAuthMapKey]) > a2aAuthMapShardByteBudget {
					t.Errorf("a shard is %d bytes, over the budget", len(cm.Data[a2aAuthMapKey]))
				}
			}
		})
	}
}

// The cut measures each profile's share on its own; the shares have to add up
// to the entry the map writes for the same profiles, or the cut is wrong.
func TestTheDispatcherProfileSharesAddUp(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	agent := authMapTestAgent()
	profiles := ceilingProfiles(agent, 40)
	profiles[3].Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	sorted := sortedOldestFirst(profiles)
	ptrs := make([]*agentv1alpha1.AgentProfile, len(sorted))
	for i := range sorted {
		ptrs[i] = &sorted[i]
	}
	base, shares, err := a2aDispatcherProfileShares(agent, ptrs)
	if err != nil {
		t.Fatal(err)
	}
	want := base
	for _, s := range shares {
		want += s
	}
	entry, _ := a2aDispatcherMapEntry(agent, profiles, nil)
	got, err := a2aAuthMapEntryBytes(entry)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Errorf("the entry is %d bytes, base plus shares %d", got, want)
	}
}

// The newest profiles are the ones refused, by creation time with name
// breaking ties, so a pass never reshuffles which; deleting an older profile
// readmits the oldest refused one.
func TestTheCeilingRefusesTheNewestProfiles(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	agent := authMapTestAgent()
	profiles := ceilingProfiles(agent, 800)
	start := metav1.Now()
	for i := range profiles {
		// Created in reverse name order, so creation time, not name, decides.
		profiles[i].CreationTimestamp = metav1.NewTime(start.Add(time.Duration(len(profiles)-i) * time.Second))
	}
	refused := a2aDispatcherCeilingRefused(agent, profiles)
	if len(refused) == 0 {
		t.Fatal("800 profiles fit; the fixture no longer crosses the ceiling")
	}
	newest := sortedOldestFirst(profiles)
	newest = newest[len(newest)-len(refused):]
	for i, p := range newest {
		if refused[i] != p.Name {
			t.Fatalf("refused %v, want the newest %d", refused[:min(3, len(refused))], len(refused))
		}
	}
	// Shuffled input, same answer.
	shuffled := append([]agentv1alpha1.AgentProfile(nil), profiles...)
	slices.Reverse(shuffled)
	if again := a2aDispatcherCeilingRefused(agent, shuffled); !slices.Equal(again, refused) {
		t.Error("the refused set depends on list order")
	}
	// Deleting the oldest profile readmits the oldest refused one.
	oldest := sortedOldestFirst(profiles)[0].Name
	var rest []agentv1alpha1.AgentProfile
	for _, p := range profiles {
		if p.Name != oldest {
			rest = append(rest, p)
		}
	}
	if after := a2aDispatcherCeilingRefused(agent, rest); slices.Contains(after, refused[0]) {
		t.Errorf("deleting the oldest profile didn't readmit %s", refused[0])
	}
}

// The condition is written on the way out, and a move into NearBudget or
// OverBudget is a Warning event once, not on every pass.
func TestTheDispatcherCeilingWarnsOncePerTransition(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	rec := record.NewFakeRecorder(16)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: rec}
	ctx := context.Background()
	write := func(reason string, status metav1.ConditionStatus, msg string) {
		t.Helper()
		if err := r.syncDispatcherCeiling(ctx, agent, &metav1.Condition{Type: dispatcherCeilingCondition, Status: status, Reason: reason, Message: msg}); err != nil {
			t.Fatal(err)
		}
	}
	events := func() int { return len(rec.Events) }

	write(dispatcherCeilingReasonWithin, metav1.ConditionTrue, "a")
	if events() != 0 {
		t.Fatalf("WithinBudget recorded %d events", events())
	}
	write(dispatcherCeilingReasonNear, metav1.ConditionTrue, "b")
	write(dispatcherCeilingReasonNear, metav1.ConditionTrue, "c")
	if events() != 1 {
		t.Fatalf("two NearBudget passes recorded %d events, want 1", events())
	}
	write(dispatcherCeilingReasonOver, metav1.ConditionFalse, "d")
	if got := <-rec.Events; !strings.Contains(got, "Warning DispatcherNearBudget") {
		t.Errorf("first event %q", got)
	}
	if got := <-rec.Events; !strings.Contains(got, "Warning DispatcherOverBudget") {
		t.Errorf("second event %q", got)
	}
	if c := meta.FindStatusCondition(agent.Status.Conditions, dispatcherCeilingCondition); c == nil || c.Reason != dispatcherCeilingReasonOver {
		t.Errorf("condition %+v, want OverBudget", c)
	}
	if err := r.syncDispatcherCeiling(ctx, agent, dispatcherCeilingRemoved); err != nil {
		t.Fatal(err)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, dispatcherCeilingCondition) != nil {
		t.Error("switched off, the condition stayed")
	}
}

// Past the ceiling the dispatcher keeps serving the bound profiles; only the
// draining profiles' grants can push the entry past the budget, and then the
// dispatcher step tears it down rather than leave it without a map entry.
func TestTheDispatcherOutlivesTheCeilingAndOnlyDrainingTearsItDown(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatcherName(agent)}
	// Rendered first with no profiles, as its sibling tests do (with some, the
	// step also copies the bus CA bundle, which this fixture doesn't carry).
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: no dispatcher: %v", err)
	}
	over := ceilingProfiles(agent, 800)
	if len(a2aDispatcherCeilingRefused(agent, over)) == 0 {
		t.Fatal("800 profiles fit; the fixture no longer crosses the ceiling")
	}
	if _, isOver, err := a2aDispatcherCeiling(agent, over, nil); err != nil || isOver {
		t.Fatalf("with the newest refused, the entry still reads as over the budget (%v)", err)
	}
	// Draining profiles past the reserve the cut leaves them, each with the
	// deadline drainingDispatchProfiles gives every one it returns.
	until := time.Now().Add(a2aDrainingWindow).Truncate(time.Second).UTC()
	var draining []a2aDispatchProfile
	for i := 0; i < 600; i++ {
		draining = append(draining, a2aDispatchProfile{Name: fmt.Sprintf("gone-%04d", i), Tiers: []string{agentv1alpha1.AgentProfileTierRead}, DrainingUntil: until})
	}
	if _, isOver, err := a2aDispatcherCeiling(agent, over, draining); err != nil || !isOver {
		t.Fatalf("600 draining profiles don't push the entry over (%v); the fixture no longer reaches the backstop", err)
	}
	if profileErr, err := r.reconcileA2ADispatcherDraining(ctx, agent, over, true, draining); err != nil || profileErr != nil {
		t.Fatalf("past the budget: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, key, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the dispatcher's Deployment survived an entry past the budget: %v", err)
	}
	// The teardown lasts as long as the drains do, not one pass: the
	// dispatch config keeps the draining profiles, with deadlines, so the
	// next pass reads the same backstop and the dispatcher stays down.
	next, err := r.dispatchDraining(ctx, agent, over)
	if err != nil {
		t.Fatalf("reading the draining profiles back: %v", err)
	}
	if len(next) != len(draining) {
		t.Fatalf("the pass after the teardown reads %d draining profiles, want %d: the dispatcher would come back while they still drain", len(next), len(draining))
	}
	if _, isOver, err := a2aDispatcherCeiling(agent, over, next); err != nil || !isOver {
		t.Errorf("the pass after the teardown reads as under the budget (%v); the dispatcher would flap", err)
	}
	cm := &corev1.ConfigMap{}
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		t.Fatalf("the teardown deleted the dispatch config: %v", err)
	}
	var applied a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &applied); err != nil {
		t.Fatal(err)
	}
	live := map[string]bool{}
	for _, p := range over {
		live[p.Name] = true
	}
	// Once the drains run out the entry fits and the dispatcher comes back.
	if gone := drainingDispatchProfiles(&applied, live, time.Now().Add(a2aDrainingWindow+time.Minute)); len(gone) != 0 {
		t.Errorf("%d draining profiles outlast the drain window, so the teardown would never end", len(gone))
	}
}

// A profile refused for a foreign ServiceAccount takes no room under the
// ceiling on either side: the PlatformAgent renders without it, so the
// AgentProfile controller has to cut without it too, or the profile at the
// boundary is rendered by one and refused by the other.
func TestBothControllersCutAtTheSameProfile(t *testing.T) {
	withOperatorBusPrincipal(t)
	withDispatcher(t)
	agent := a2aTestAgent()
	profiles := ceilingProfiles(agent, 800)
	start := metav1.Now()
	for i := range profiles {
		profiles[i].CreationTimestamp = metav1.NewTime(start.Add(time.Duration(i) * time.Second))
	}
	oldest := &profiles[0]
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{
		Namespace: profileNamespaceOf(oldest),
		Name:      agentProfileServiceAccountName(oldest, oldest.AllowedTierList()[0]),
	}}
	refused := a2aDispatcherCeilingRefused(agent, profiles[1:])
	if len(refused) == 0 {
		t.Fatal("800 profiles fit; the fixture no longer crosses the ceiling")
	}
	// The newest profile the PlatformAgent binds, which counting the foreign
	// profile would push out.
	boundary := profiles[len(profiles)-len(refused)-1].Name
	if !slices.Contains(a2aDispatcherCeilingRefused(agent, profiles), boundary) {
		t.Fatalf("%s is bound even with the foreign profile counted; the fixture no longer tests the boundary", boundary)
	}
	objs := []client.Object{agent, foreign}
	for i := range profiles {
		objs = append(objs, &profiles[i])
	}
	h := newProfileHarness(t, objs...)
	h.reconcile(agent.Namespace, boundary)
	if c := condition(h.profile(agent.Namespace, boundary), agentv1alpha1.AgentProfileConditionIdentityReady); c.Reason == reasonAgentProfileDispatcherCeiling {
		t.Errorf("the AgentProfile controller refused %s, which the PlatformAgent renders: %s", boundary, c.Message)
	}
}

// twoTierProfiles is n profiles that allow read and unattended, created a
// second apart in name order, so oldest first is name order.
func twoTierProfiles(agent *agentv1alpha1.PlatformAgent, n int) []agentv1alpha1.AgentProfile {
	profiles := ceilingProfiles(agent, n)
	start := metav1.Now()
	for i := range profiles {
		profiles[i].CreationTimestamp = metav1.NewTime(start.Add(time.Duration(i) * time.Second))
	}
	return profiles
}

// The dispatch config's shares, summed, are at least the ConfigMap the
// operator writes for the same profiles, and only a little over: a Job
// template is charged at the larger of its two renderings, and the first
// entry one comma it doesn't take (gke-labs/kube-agents#5479).
func TestTheDispatchConfigSharesCoverTheConfigMap(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "false") // no cut: measure all of them
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := twoTierProfiles(agent, 40)
	profiles[3].Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	var ptrs []*agentv1alpha1.AgentProfile
	for i := range profiles {
		ptrs = append(ptrs, &profiles[i])
	}
	base, shares, err := a2aDispatchConfigShares(agent, ptrs)
	if err != nil {
		t.Fatal(err)
	}
	estimate := base
	for _, n := range shares {
		estimate += n
	}
	largest := 0
	for _, imageVolumes := range []bool{true, false} {
		cm, err := buildA2ADispatchConfigMapWith(agent, profiles, dispatchJobTemplates(agent, profiles, imageVolumes), nil)
		if err != nil {
			t.Fatal(err)
		}
		actual := len(cm.Data[a2aDispatchConfigKey])
		if estimate < actual {
			t.Errorf("imageVolumes=%v: the shares sum to %d bytes, under the %d the ConfigMap holds", imageVolumes, estimate, actual)
		}
		largest = max(largest, actual)
	}
	// Against the larger rendering, which is what each share is charged at,
	// the sum is over by the first entry's comma alone.
	if estimate-largest > 1 {
		t.Errorf("the shares sum to %d bytes, %d over the larger ConfigMap's %d", estimate, estimate-largest, largest)
	}
}

// Under Job mode each profile that allows unattended carries its Job
// template in the dispatch config, so the config's limit comes before the
// identity's: the cut refuses the newest at the ConfigMap's limit, names
// #5479, and what the operator writes fits in one ConfigMap.
func TestUnderJobModeTheConfigLimitCutsFirst(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := twoTierProfiles(agent, 400)
	resolved := resolveAgentProfileIdentities(agent, profiles)
	refused := a2aDispatcherCeilingRefused(agent, profiles)
	if len(refused) == 0 {
		t.Fatal("400 two-tier profiles under Job mode fit; the fixture no longer crosses the config limit")
	}
	fit := len(profiles) - len(refused)
	msg := resolved[refused[0]].refused.Error()
	if !strings.Contains(msg, "ConfigMap") || !strings.Contains(msg, "#5479") {
		t.Errorf("the refusal doesn't name the config limit and #5479: %s", msg)
	}
	// What the operator writes for the bound profiles fits one ConfigMap at
	// either rendering, and at the larger it uses most of the budget: the cut
	// is safe and not far early.
	for _, imageVolumes := range []bool{true, false} {
		cm, err := buildA2ADispatchConfigMapWith(agent, profiles, dispatchJobTemplates(agent, profiles, imageVolumes), nil)
		if err != nil {
			t.Fatal(err)
		}
		n := len(cm.Data[a2aDispatchConfigKey])
		if n > a2aDispatchConfigLimitBytes {
			t.Errorf("imageVolumes=%v: the dispatch config for the %d bound profiles is %d bytes, over one ConfigMap", imageVolumes, fit, n)
		}
		if !imageVolumes && n < a2aDispatchConfigProfileBudget*9/10 {
			t.Errorf("the dispatch config for the %d bound profiles is %d bytes, under nine tenths of the %d budget: the cut is early", fit, n, a2aDispatchConfigProfileBudget)
		}
	}
	cond, over, err := a2aDispatcherCeiling(agent, profiles, nil)
	if err != nil || over {
		t.Fatalf("over=%v err=%v", over, err)
	}
	if cond.Reason != dispatcherCeilingReasonRefused || !strings.Contains(cond.Message, "ConfigMap") || !strings.Contains(cond.Message, "#5479") {
		t.Errorf("condition %s: %s", cond.Reason, cond.Message)
	}
	// Short of the cut but past four fifths of the config, the warning
	// names the config.
	near, _, err := a2aDispatcherCeiling(agent, profiles[:fit*9/10], nil)
	if err != nil {
		t.Fatal(err)
	}
	if near.Reason != dispatcherCeilingReasonNear || !strings.Contains(near.Message, "ConfigMap") {
		t.Errorf("%d profiles: %s: %s", fit*9/10, near.Reason, near.Message)
	}
}

// The draining profiles' entries go into the dispatch config as well as their
// grants into the identity. With the live profiles at the config's cut, enough
// of them push the config past what one ConfigMap holds while the identity is
// still well under its ceiling: that is OverBudget too, and the teardown
// writes a config, with the drains alone, that fits and that the agent owns.
func TestDrainingPastTheConfigLimitTearsDownWithAConfigThatFits(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	t.Setenv(a2aDispatcherImageEnvVar, testDispatcherImage)
	agent := a2aTestAgent()
	profiles := twoTierProfiles(agent, 400)
	until := time.Now().Add(a2aDrainingWindow).Truncate(time.Second).UTC()
	var draining []a2aDispatchProfile
	for i := 0; i < 600; i++ {
		draining = append(draining, a2aDispatchProfile{Name: fmt.Sprintf("gone-%04d", i), Tiers: []string{agentv1alpha1.AgentProfileTierRead}, DrainingUntil: until})
	}
	entry, _ := a2aDispatcherMapEntry(agent, profiles, draining)
	if size, _ := a2aAuthMapEntryBytes(entry); size > a2aDispatcherCeilingBytes {
		t.Fatalf("the identity entry is %d bytes, over its own ceiling: the fixture no longer isolates the config side", size)
	}
	cond, over, err := a2aDispatcherCeiling(agent, profiles, draining)
	if err != nil || !over {
		t.Fatalf("over=%v err=%v: 600 draining entries past the config's reserve didn't read as over", over, err)
	}
	if cond.Reason != dispatcherCeilingReasonOver || !strings.Contains(cond.Message, "ConfigMap") || !strings.Contains(cond.Message, "#5479") {
		t.Errorf("condition %s: %s", cond.Reason, cond.Message)
	}

	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	if profileErr, err := r.reconcileA2ADispatcherDraining(ctx, agent, profiles, true, draining); err != nil || profileErr != nil {
		t.Fatalf("past the config limit: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatcherName(agent)}, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the dispatcher's Deployment survived a config past the limit: %v", err)
	}
	cm := &corev1.ConfigMap{}
	if err := c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		t.Fatalf("the teardown wrote no dispatch config: %v", err)
	}
	if n := len(cm.Data[a2aDispatchConfigKey]); n > a2aDispatchConfigLimitBytes {
		t.Errorf("the teardown's dispatch config is %d bytes, over one ConfigMap", n)
	}
	if !metav1.IsControlledBy(cm, agent) {
		t.Error("the teardown's dispatch config has no controller reference to the agent, so cleanup would leave it")
	}
	// The drains alone: live entries would grow it with the profile count,
	// past one ConfigMap at fleet scale, and nothing dispatches for them.
	var applied a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &applied); err != nil {
		t.Fatal(err)
	}
	for _, p := range applied.Profiles {
		if p.DrainingUntil.IsZero() {
			t.Fatalf("the teardown's dispatch config lists live profile %s; it should carry the drains alone", p.Name)
		}
	}
	next, err := r.dispatchDraining(ctx, agent, profiles)
	if err != nil || len(next) != len(draining) {
		t.Errorf("the next pass reads %d draining profiles (%v), want %d", len(next), err, len(draining))
	}
}

// The cut is cached by what decides it, a profile's spec among it, so a spec
// change moves the cut even where nothing bumps the generation.
func TestTheCutFollowsAProfileSpecChange(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	agent := a2aTestAgent()
	profiles := twoTierProfiles(agent, 800)
	before := a2aDispatcherCeilingRefused(agent, profiles)
	if len(before) == 0 {
		t.Fatal("800 profiles fit; the fixture no longer crosses the ceiling")
	}
	// The ten oldest drop a tier, so their shares shrink and newer ones fit.
	for i := 0; i < 10; i++ {
		profiles[i].Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	}
	after := a2aDispatcherCeilingRefused(agent, profiles)
	if slices.Equal(before, after) {
		t.Errorf("the cut didn't move when the ten oldest profiles dropped a tier (%d refused both times): a cached cut outlived the spec it was made for", len(after))
	}
}
