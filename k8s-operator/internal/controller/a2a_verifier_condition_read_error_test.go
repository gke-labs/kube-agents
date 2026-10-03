package controller

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	meta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
)

// A read error is not an answer about the verifier, and the difference matters
// because the condition is written on the present-while-it-holds pattern:
// "ready" is spelled by REMOVING it. So a helper that collapsed "I could not
// look" into "not not-ready" would not be neutral — it would clear a correct
// A2AVerifier=False off a settled install on one transient API error and
// report the stack healthy until the next pass put it back. Every executor
// turns an unanswered capability Check into a terminal rejected, so that is a
// window where every submission is refused and status says nothing.
//
// The three inputs are therefore three outcomes, not two.
func TestAVerifierReadErrorLeavesTheConditionAlone(t *testing.T) {
	agent := splitReadinessNextAgent()

	withGet := func(fail bool, objs ...client.Object) *PlatformAgentReconciler {
		scheme := setupScheme()
		b := fake.NewClientBuilder().
			WithScheme(scheme).
			WithObjects(append([]client.Object{agent}, objs...)...).
			WithStatusSubresource(agent)
		if fail {
			b = b.WithInterceptorFuncs(interceptor.Funcs{
				Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
					if _, ok := obj.(*appsv1.Deployment); ok && key.Name == a2aVerifierName(agent) {
						return errors.NewInternalError(context.DeadlineExceeded)
					}
					return cl.Get(ctx, key, obj, opts...)
				},
			})
		}
		cl := b.Build()
		return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	}

	t.Run("a down verifier is known and not ready", func(t *testing.T) {
		r := withGet(false, a2aVerifierWorkload(agent, 0))
		notReady, known := r.a2aVerifierNotReady(context.Background(), agent)
		if !known || !notReady {
			t.Fatalf("notReady=%v known=%v, want true/true", notReady, known)
		}
	})

	t.Run("an absent verifier is a real answer, not an error", func(t *testing.T) {
		r := withGet(false)
		notReady, known := r.a2aVerifierNotReady(context.Background(), agent)
		if !known || notReady {
			t.Fatalf("notReady=%v known=%v, want false/true: an absent Deployment is the ordinary pre-render shape", notReady, known)
		}
	})

	t.Run("a read error is not an answer", func(t *testing.T) {
		r := withGet(true, a2aVerifierWorkload(agent, 0))
		notReady, known := r.a2aVerifierNotReady(context.Background(), agent)
		if known {
			t.Fatalf("notReady=%v known=%v, want known=false", notReady, known)
		}
	})

	// The consequence, which is the thing that was actually wrong: an
	// unknown pass must neither erase a standing condition nor count as a
	// reason to write status.
	t.Run("an unknown pass preserves a standing condition", func(t *testing.T) {
		withCond := splitReadinessNextAgent()
		withCond.Status.Conditions = []metav1.Condition{{
			Type: a2aVerifierConditionType, Status: metav1.ConditionFalse,
			Reason: a2aVerifierNotReadyReason, Message: a2aVerifierNotReadyMessage,
			LastTransitionTime: metav1.Now(),
		}}
		setA2AVerifierCondition(withCond, false, false, metav1.Now())
		if cond := meta.FindStatusCondition(withCond.Status.Conditions, a2aVerifierConditionType); cond == nil {
			t.Error("a pass that could not read the verifier erased the condition; the CR now reports the stack healthy")
		}
		if !a2aVerifierConditionCurrent(withCond, false, false) {
			t.Error("an unknown pass counted as a condition change, so it would dirty status on every transient read error")
		}
	})

	// And the control: known-ready still clears it, so the preservation
	// above is not just "never removes anything".
	t.Run("a known-ready pass still clears it", func(t *testing.T) {
		withCond := splitReadinessNextAgent()
		withCond.Status.Conditions = []metav1.Condition{{
			Type: a2aVerifierConditionType, Status: metav1.ConditionFalse,
			Reason: a2aVerifierNotReadyReason, Message: a2aVerifierNotReadyMessage,
			LastTransitionTime: metav1.Now(),
		}}
		setA2AVerifierCondition(withCond, false, true, metav1.Now())
		if cond := meta.FindStatusCondition(withCond.Status.Conditions, a2aVerifierConditionType); cond != nil {
			t.Errorf("the condition outlived the fault it reports: %+v", cond)
		}
	})
}
