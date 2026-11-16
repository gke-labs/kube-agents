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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// migrateRetiredMode has nothing to do on a CR that already says "next" or
// carries no mode at all: no patch, no Event, and the caller goes on with
// the pass.
func TestMigrateRetiredModeIsANoOpOnNextAndNil(t *testing.T) {
	for name, mode := range map[string]*string{"next": ptr.To(agentv1alpha1.PlatformAgentModeNext), "nil": nil} {
		t.Run(name, func(t *testing.T) {
			agent := a2aTestAgent()
			agent.Spec.Mode = mode
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).Build()
			events := record.NewFakeRecorder(4)
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: events}

			migrated, err := r.migrateRetiredMode(context.Background(), agent)
			if err != nil || migrated {
				t.Fatalf("migrateRetiredMode = (%v, %v), want (false, nil)", migrated, err)
			}
			if got := eventsOf(events); len(got) != 0 {
				t.Errorf("events = %v, want none", got)
			}
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatal(err)
			}
			if (stored.Spec.Mode == nil) != (mode == nil) || (mode != nil && *stored.Spec.Mode != *mode) {
				t.Errorf("spec.mode = %v after the no-op, want %v unchanged", stored.Spec.Mode, mode)
			}
		})
	}
}

// A stored object that admission now refuses (the validating webhook checks
// the whole object on the migration's patch) is not stopped by the migration:
// the patch error is a Warning Event, the pass goes on, and spec.mode stays
// as stored until an accepted write sets it.
func TestMigrateRetiredModeContinuesWhenAdmissionRefusesThePatch(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Mode = ptr.To("today")
	scheme := setupScheme()
	refused := apierrors.NewInvalid(schema.GroupKind{Group: agentv1alpha1.GroupVersion.Group, Kind: "PlatformAgent"}, agent.Name, nil)
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(interceptor.Funcs{
		Patch: func(context.Context, client.WithWatch, client.Object, client.Patch, ...client.PatchOption) error {
			return refused
		},
	}).Build()
	events := record.NewFakeRecorder(4)
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: events}
	migrated, err := r.migrateRetiredMode(context.Background(), agent)
	if err != nil || migrated {
		t.Fatalf("migrateRetiredMode = (%v, %v) on a refused patch, want (false, nil) so the pass goes on", migrated, err)
	}
	got := eventsOf(events)
	if len(got) != 1 || !strings.Contains(got[0], modeMigrationRefusedReason) || !strings.Contains(got[0], "today") {
		t.Errorf("events = %v, want one %s Warning naming the stored value", got, modeMigrationRefusedReason)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if stored.Spec.Mode == nil || *stored.Spec.Mode != "today" {
		t.Errorf("spec.mode = %v after the refused patch, want today unchanged", stored.Spec.Mode)
	}
}

// Against a real API server serving the branch's CRD: a PlatformAgent stored
// with the retired spec.mode "today" (the enum still admits it, so the object
// stays valid across the CRD apply) is patched to "next" by the reconciler's
// first pass, which says so in a ModeMigrated Event; and a PlatformAgent that
// omits the field reads back as "next" through the schema default alone.
func TestAStoredTodayModeIsMigratedToNextEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()
	const (
		namespace = "mode-migration"
		retired   = "today"
	)
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	newAgent := func(name string, mode *string) *agentv1alpha1.PlatformAgent {
		return &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace},
			Spec: agentv1alpha1.PlatformAgentSpec{
				Mode: mode,
				Harness: &agentv1alpha1.HarnessSpec{
					ProjectID:   envtestHarnessProject,
					Location:    envtestHarnessLocation,
					ClusterName: envtestHarnessCluster,
				},
			},
		}
	}
	fetch := func(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
			t.Fatalf("reading %s back: %v", agent.Name, err)
		}
		return got
	}

	t.Run("a stored today is patched to next on the first pass", func(t *testing.T) {
		agent := newAgent("stored-today", ptr.To(retired))
		if err := cl.Create(ctx, agent); err != nil {
			t.Fatalf("creating the PlatformAgent with spec.mode %q: %v", retired, err)
		}
		if got := fetch(agent); got.Spec.Mode == nil || *got.Spec.Mode != retired {
			t.Fatalf("precondition: the API server stored spec.mode = %v, want %q (the enum must still admit it)", got.Spec.Mode, retired)
		}

		events := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: events}
		if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}); err != nil {
			t.Fatalf("Reconcile (first pass): %v", err)
		}

		got := fetch(agent)
		if got.Spec.Mode == nil || *got.Spec.Mode != agentv1alpha1.PlatformAgentModeNext {
			t.Errorf("spec.mode = %v after the first pass, want %q", got.Spec.Mode, agentv1alpha1.PlatformAgentModeNext)
		}
		var migrated bool
		for _, e := range eventsOf(events) {
			if strings.Contains(e, modeMigratedReason) && strings.Contains(e, retired) && strings.Contains(e, agentv1alpha1.PlatformAgentModeNext) {
				migrated = true
			}
		}
		if !migrated {
			t.Errorf("no %s Event naming %q and %q was emitted", modeMigratedReason, retired, agentv1alpha1.PlatformAgentModeNext)
		}

		// The second pass finds nothing to migrate: no further Event, and the
		// pass goes on to add the finalizer.
		if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}); err != nil {
			t.Fatalf("Reconcile (second pass): %v", err)
		}
		for _, e := range eventsOf(events) {
			if strings.Contains(e, modeMigratedReason) {
				t.Errorf("the second pass emitted %q again on a CR already at %q", e, agentv1alpha1.PlatformAgentModeNext)
			}
		}
		if got := fetch(agent); len(got.Finalizers) == 0 {
			t.Errorf("the second pass added no finalizer; the migration return short-circuited more than the one pass")
		}
	})

	t.Run("an absent field reads back as next", func(t *testing.T) {
		agent := newAgent("no-mode", nil)
		if err := cl.Create(ctx, agent); err != nil {
			t.Fatalf("creating the PlatformAgent without spec.mode: %v", err)
		}
		if got := fetch(agent); got.Spec.Mode == nil || *got.Spec.Mode != agentv1alpha1.PlatformAgentModeNext {
			t.Errorf("spec.mode = %v on a CR that omitted it, want %q from the CRD default", got.Spec.Mode, agentv1alpha1.PlatformAgentModeNext)
		}
	})
}
