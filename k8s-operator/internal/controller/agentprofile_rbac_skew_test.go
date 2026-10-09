package controller

import (
	"context"
	"strings"
	"testing"

	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// An image deployed ahead of its ClusterRole: the CRD is there, the role has
// no agentprofiles rules. The probe names exactly those denials, so the
// informer gates can skip them, and a denial on anything else is not one.
func TestAgentProfileAccessDeniedNamesOnlyAgentProfileGrants(t *testing.T) {
	if got := AgentProfileAccessDenied(nil); got != nil {
		t.Errorf("a nil checker (tests, the golden harness) denies %v, want nothing", got)
	}

	checker := NewRBACChecker((&fakeAuthorizer{deny: func(a *authorizationv1.ResourceAttributes) bool {
		return a.Resource == "agentprofiles" && a.Verb == "list"
	}}).reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("Probe: %v", err)
	}
	got := AgentProfileAccessDenied(checker)
	if len(got) != 1 || !strings.Contains(got[0], "list agentprofiles") {
		t.Errorf("AgentProfileAccessDenied = %v, want the one list denial", got)
	}

	other := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews())
	if _, err := other.Probe(context.Background()); err != nil {
		t.Fatalf("Probe: %v", err)
	}
	if got := AgentProfileAccessDenied(other); len(got) != 0 {
		t.Errorf("a PDB denial reads as an AgentProfile denial: %v", got)
	}
}

// With the watch skipped for a role that cannot read AgentProfiles, the
// identity map renders without them. Listing anyway would hang the worker on a
// cached informer that never syncs, or fail every next render on the 403; the
// fake stands in with the 403.
func TestAnUnreadableAgentProfileKindIsNotListed(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	build := func(unreadable bool) *PlatformAgentReconciler {
		scheme := setupScheme()
		funcs := fakeServerSideApplyInterceptors()
		funcs.List = func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			if _, ok := list.(*agentv1alpha1.AgentProfileList); ok {
				return errors.NewForbidden(schema.GroupResource{Group: agentv1alpha1.GroupVersion.Group, Resource: "agentprofiles"}, "", nil)
			}
			return cl.List(ctx, list, opts...)
		}
		cl := fake.NewClientBuilder().
			WithScheme(scheme).
			WithObjects(agent.DeepCopy()).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
			WithInterceptorFuncs(funcs).
			Build()
		return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, agentProfilesUnreadable: unreadable}
	}

	// The control: with the flag clear the render does list, and the 403 is
	// what it hits.
	if _, err := build(false).reconcileA2A(context.Background(), agent.DeepCopy()); err == nil || !strings.Contains(err.Error(), "failed to list AgentProfiles") {
		t.Fatalf("with the kind readable, reconcileA2A = %v; want the list's 403, or this test proves nothing", err)
	}
	if _, err := build(true).reconcileA2A(context.Background(), agent.DeepCopy()); err != nil && strings.Contains(err.Error(), "AgentProfiles") {
		t.Errorf("with the kind unreadable, reconcileA2A still listed it: %v", err)
	}
}

// The denial is decided by RBAC alone, so it makes AgentProfiles unreadable
// whether or not the CRD is installed at boot. An operator booted with an old
// role and no CRD, which then gets the CRD applied, must still not list the
// kind.
func TestADeniedRoleMakesAgentProfilesUnreadableWithOrWithoutTheCRD(t *testing.T) {
	noMatch := &meta.NoKindMatchError{GroupKind: schema.GroupKind{Group: agentv1alpha1.GroupVersion.Group, Kind: "AgentProfile"}}
	denied := []string{"list agentprofiles.kubeagents.x-k8s.io"}
	for _, tc := range []struct {
		name              string
		mapErr            error
		denied            []string
		watch, unreadable bool
	}{
		{"CRD installed, role current", nil, nil, true, false},
		{"CRD installed, role denied", nil, denied, false, true},
		{"CRD missing, role denied", noMatch, denied, false, true},
		{"CRD missing, role current", noMatch, nil, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			watch, unreadable := agentProfileWatchPlan(tc.mapErr, tc.denied)
			if watch != tc.watch || unreadable != tc.unreadable {
				t.Errorf("agentProfileWatchPlan = watch %v, unreadable %v; want %v, %v", watch, unreadable, tc.watch, tc.unreadable)
			}
		})
	}
}
