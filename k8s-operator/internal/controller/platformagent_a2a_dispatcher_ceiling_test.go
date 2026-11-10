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
	"fmt"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
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
// it the condition refuses, and the map leaves the dispatcher out rather than
// write a shard the API server refuses (gke-labs/kube-agents#5381).
func TestTheDispatcherCeiling(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aProfileDispatcherEnvVar, "")
	agent := authMapTestAgent()
	for _, tc := range []struct {
		profiles int
		reason   string
		status   metav1.ConditionStatus
		inMap    bool
	}{
		{100, dispatcherCeilingReasonWithin, metav1.ConditionTrue, true},
		{600, dispatcherCeilingReasonNear, metav1.ConditionTrue, true},
		{800, dispatcherCeilingReasonOver, metav1.ConditionFalse, false},
	} {
		t.Run(fmt.Sprint(tc.profiles), func(t *testing.T) {
			profiles := ceilingProfiles(agent, tc.profiles)
			cond, over, err := a2aDispatcherCeiling(agent, profiles, nil)
			if err != nil {
				t.Fatal(err)
			}
			if cond.Type != dispatcherCeilingCondition || cond.Reason != tc.reason || cond.Status != tc.status || over == tc.inMap {
				t.Fatalf("at %d profiles: %s/%s over=%v, want %s/%s", tc.profiles, cond.Status, cond.Reason, over, tc.status, tc.reason)
			}
			if !strings.Contains(cond.Message, fmt.Sprintf("for %d profiles", tc.profiles)) {
				t.Errorf("the message doesn't name the profile count: %q", cond.Message)
			}
			if tc.reason != dispatcherCeilingReasonWithin && !strings.Contains(cond.Message, dispatcherCeilingIssue) {
				t.Errorf("the message doesn't name %s: %q", dispatcherCeilingIssue, cond.Message)
			}
			if tc.reason == dispatcherCeilingReasonOver && !strings.Contains(cond.Message, "operator.profileDispatcher.enabled: false") {
				t.Errorf("the refusal doesn't say how to switch the dispatcher off: %q", cond.Message)
			}
			doc, err := renderA2AAuthMap(agent, profiles)
			if err != nil {
				t.Fatal(err)
			}
			var users []string
			for _, id := range doc.Identities {
				users = append(users, id.User)
			}
			if slices.Contains(users, a2aDispatcherUser) != tc.inMap {
				t.Errorf("at %d profiles the dispatcher is in the map: %v, want %v", tc.profiles, !tc.inMap, tc.inMap)
			}
			if !tc.inMap {
				cms, _, err := buildA2AAuthMapConfigMaps(agent, profiles, nil)
				if err != nil {
					t.Fatal(err)
				}
				for _, cm := range cms {
					if len(cm.Data[a2aAuthMapKey]) > a2aAuthMapShardByteBudget {
						t.Errorf("past the ceiling a shard is still %d bytes", len(cm.Data[a2aAuthMapKey]))
					}
				}
			}
		})
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

// Past the ceiling the dispatcher step runs no dispatcher: what a pass under
// it rendered is torn down, as though the dispatcher were switched off.
func TestPastTheCeilingTheDispatcherStepTearsItDown(t *testing.T) {
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
		t.Fatalf("under the ceiling: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, key, &appsv1.Deployment{}); err != nil {
		t.Fatalf("precondition: no dispatcher under the ceiling: %v", err)
	}
	over := ceilingProfiles(agent, 800)
	if _, isOver, err := a2aDispatcherCeiling(agent, over, nil); err != nil || !isOver {
		t.Fatalf("800 profiles read as within the ceiling (%v); the fixture no longer crosses it", err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, over, true); err != nil || profileErr != nil {
		t.Fatalf("past the ceiling: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, key, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the dispatcher's Deployment survived the ceiling: %v", err)
	}
}
