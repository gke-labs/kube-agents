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
