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
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The AgentProfile reconciler ("profile" is the A2A AgentProfile resource, not a
// Hermes profile directory). Per profile it renders two of the three things the
// spec says must agree: the ServiceAccount the profile's pods run as, and the
// agent card on the directory. The third, the callout identity entry, is the
// PlatformAgent reconciler's (agentprofile_platformagent.go), because it lives
// in the one identity map that reconciler already owns.
//
// Everything is dark unless the PlatformAgent in the profile's namespace runs
// `spec.mode: next`. On today, the reconciler removes what it rendered and
// renders nothing.
//
// Deletion is finalizer-ordered: the tombstone is published before the
// finalizer comes off, so a profile never disappears from the cluster while its
// card still routes traffic to it. The card is level-triggered: every reconcile
// reads the directory and republishes when the entry is missing, tombstoned or
// stale, and a periodic resync covers a directory that lost the entry with no
// Kubernetes event to say so.

const (
	// agentProfileFinalizer holds a profile until its tombstone is published.
	agentProfileFinalizer = "kubeagents.x-k8s.io/agentprofile-finalizer"

	// agentProfileComponent labels what the operator renders for a profile.
	agentProfileComponent = "agentprofile"

	// agentProfileNameLabel names the profile on what it renders.
	agentProfileNameLabel = "kubeagents.x-k8s.io/agentprofile"

	// agentProfileResync re-reads the directory for live profiles.
	agentProfileResync = 5 * time.Minute

	// agentProfileBusRetry is the requeue after a bus error.
	agentProfileBusRetry = 30 * time.Second
)

// Condition reasons.
const (
	reasonAgentProfileRendered        = "Rendered"
	reasonAgentProfileNotNext         = "ModeNotNext"
	reasonAgentProfileNoAgent         = "NoPlatformAgent"
	reasonAgentProfileManyAgents      = "MultiplePlatformAgents"
	reasonAgentProfileRefused         = "ServiceAccountRefused"
	reasonAgentProfileSANotFound      = "ServiceAccountNotFound"
	reasonAgentProfilePublished       = "Published"
	reasonAgentProfileBusUnconfigured = "OperatorBusIdentityUnconfigured"
	reasonAgentProfileBusError        = "BusUnavailable"
	reasonAgentProfileIdentityMissing = "IdentityNotRendered"
)

// AgentProfileReconciler reconciles AgentProfile objects.
type AgentProfileReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	// Cards is the directory. Nil means the default NATS publisher.
	Cards cardPublisher
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles/finalizers,verbs=update

// Reconcile renders one profile.
func (r *AgentProfileReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	var profile agentv1alpha1.AgentProfile
	if err := r.Get(ctx, req.NamespacedName, &profile); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	agent, reason, msg, err := r.boundAgent(ctx, profile.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	if agent == nil || !a2aStackRendering(agent) {
		if agent != nil {
			reason, msg = reasonAgentProfileNotNext, "the PlatformAgent "+agent.Name+" does not run spec.mode: next; an AgentProfile renders nothing until it does"
			if _, modeErr := resolveMode(agent); modeErr != nil {
				// Version skew freezes the bus rather than tearing it
				// down; freeze the profile's objects with it.
				return ctrl.Result{}, r.writeStatus(ctx, &profile, agent, "", reason, modeErr.Error(), reason, modeErr.Error(), false)
			}
		}
		return ctrl.Result{}, r.renderNothing(ctx, &profile, agent, reason, msg)
	}

	if !profile.DeletionTimestamp.IsZero() {
		return r.finalize(ctx, &profile, agent)
	}
	if controllerutil.AddFinalizer(&profile, agentProfileFinalizer) {
		if err := r.Update(ctx, &profile); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	var all agentv1alpha1.AgentProfileList
	if err := r.List(ctx, &all, client.InNamespace(profile.Namespace)); err != nil {
		return ctrl.Result{}, err
	}
	resolution := resolveAgentProfileIdentities(agent, all.Items)[profile.Name]

	if resolution.refused != nil {
		if err := r.deleteOwnServiceAccount(ctx, &profile); err != nil {
			return ctrl.Result{}, err
		}
		cardReason, cardMsg, busErr := r.withdrawCard(ctx, agent, &profile)
		if err := r.writeStatus(ctx, &profile, agent, "", reasonAgentProfileRefused, resolution.refused.Error(), cardReason, cardMsg, false); err != nil {
			return ctrl.Result{}, err
		}
		return r.busResult(busErr)
	}

	idReason, idMsg := reasonAgentProfileRendered, "ServiceAccount "+resolution.serviceAccount+" and its bus identity are rendered"
	if profile.Spec.Identity.ServiceAccountName == "" {
		if err := r.applyServiceAccount(ctx, &profile, resolution.serviceAccount); err != nil {
			return ctrl.Result{}, err
		}
	} else {
		if err := r.deleteOwnServiceAccount(ctx, &profile); err != nil {
			return ctrl.Result{}, err
		}
		var sa corev1.ServiceAccount
		err := r.Get(ctx, types.NamespacedName{Namespace: profile.Namespace, Name: resolution.serviceAccount}, &sa)
		switch {
		case apierrors.IsNotFound(err):
			idReason, idMsg = reasonAgentProfileSANotFound, "spec.identity.serviceAccountName "+resolution.serviceAccount+" does not exist; the bus identity is rendered and no pod can present a token for it until it does"
		case err != nil:
			return ctrl.Result{}, err
		}
	}

	cardReason, cardMsg, busErr := r.publishCard(ctx, agent, &profile)
	if busErr != nil {
		log.Info("could not reconcile the agent card", "profile", profile.Name, "error", busErr.Error())
	}
	if err := r.writeStatus(ctx, &profile, agent, resolution.serviceAccount, idReason, idMsg, cardReason, cardMsg, idReason == reasonAgentProfileRendered); err != nil {
		return ctrl.Result{}, err
	}
	if busErr != nil {
		return r.busResult(busErr)
	}
	return ctrl.Result{RequeueAfter: agentProfileResync}, nil
}

// busResult turns a bus failure into a timed retry rather than an error: the
// bus being down is a state to wait out, not a reconcile bug, and an error
// would back off exponentially past the point the bus came back.
func (r *AgentProfileReconciler) busResult(busErr error) (ctrl.Result, error) {
	if busErr != nil {
		return ctrl.Result{RequeueAfter: agentProfileBusRetry}, nil
	}
	return ctrl.Result{RequeueAfter: agentProfileResync}, nil
}

// boundAgent finds the profile's PlatformAgent: the only one in its namespace.
func (r *AgentProfileReconciler) boundAgent(ctx context.Context, namespace string) (*agentv1alpha1.PlatformAgent, string, string, error) {
	var agents agentv1alpha1.PlatformAgentList
	if err := r.List(ctx, &agents, client.InNamespace(namespace)); err != nil {
		return nil, "", "", err
	}
	switch len(agents.Items) {
	case 0:
		return nil, reasonAgentProfileNoAgent, "no PlatformAgent in namespace " + namespace + "; an AgentProfile binds to the one in its namespace", nil
	case 1:
		return &agents.Items[0], "", "", nil
	default:
		return nil, reasonAgentProfileManyAgents, fmt.Sprintf("%d PlatformAgents in namespace %s; an AgentProfile binds to the one in its namespace and cannot choose", len(agents.Items), namespace), nil
	}
}

// renderNothing is the today path: remove what this reconciler rendered and
// release the finalizer. There is no bus to tombstone on, and on a flip to
// today the PlatformAgent reconciler tears down the directory with the bus.
func (r *AgentProfileReconciler) renderNothing(ctx context.Context, p *agentv1alpha1.AgentProfile, agent *agentv1alpha1.PlatformAgent, reason, msg string) error {
	if err := r.deleteOwnServiceAccount(ctx, p); err != nil {
		return err
	}
	if !p.DeletionTimestamp.IsZero() {
		if controllerutil.RemoveFinalizer(p, agentProfileFinalizer) {
			return r.Update(ctx, p)
		}
		return nil
	}
	return r.writeStatus(ctx, p, agent, "", reason, msg, reason, msg, false)
}

// finalize publishes the tombstone, then releases the finalizer. A bus that
// cannot take the tombstone holds the finalizer and retries: releasing it
// would leave a card routing traffic to a profile that no longer exists. With
// no operator bus identity configured the operator never published a card, so
// there is nothing to withdraw.
func (r *AgentProfileReconciler) finalize(ctx context.Context, p *agentv1alpha1.AgentProfile, agent *agentv1alpha1.PlatformAgent) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(p, agentProfileFinalizer) {
		return ctrl.Result{}, nil
	}
	if _, _, ok := operatorBusPrincipal(); ok {
		if _, msg, busErr := r.withdrawCard(ctx, agent, p); busErr != nil {
			logf.FromContext(ctx).Info("holding the AgentProfile finalizer until its tombstone is published", "profile", p.Name, "reason", msg)
			return ctrl.Result{RequeueAfter: agentProfileBusRetry}, nil
		}
	}
	controllerutil.RemoveFinalizer(p, agentProfileFinalizer)
	return ctrl.Result{}, r.Update(ctx, p)
}

func (r *AgentProfileReconciler) cards() cardPublisher {
	if r.Cards == nil {
		r.Cards = newNATSCardPublisher()
	}
	return r.Cards
}

// publishCard makes the directory entry the card the profile renders, writing
// only when the entry is missing, a tombstone, or a different card.
func (r *AgentProfileReconciler) publishCard(ctx context.Context, agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (string, string, error) {
	if _, _, ok := operatorBusPrincipal(); !ok {
		return reasonAgentProfileBusUnconfigured, "the operator was deployed without " + operatorNamespaceEnvVar + " and " + operatorServiceAccountEnvVar + ", so it has no bus identity to publish cards with", nil
	}
	want := desiredAgentCard(p)
	entry, err := r.cards().Read(ctx, agent, p.Name)
	if err != nil {
		return reasonAgentProfileBusError, err.Error(), err
	}
	if entry.current(want) {
		return reasonAgentProfilePublished, "the agent card is on " + agentCardSubject(p.Name), nil
	}
	if err := r.cards().Publish(ctx, agent, p.Name, &want); err != nil {
		return reasonAgentProfileBusError, err.Error(), err
	}
	return reasonAgentProfilePublished, "the agent card is on " + agentCardSubject(p.Name), nil
}

// withdrawCard makes the directory entry a tombstone if it is a card. An
// absent entry stays absent: a tombstone for a card nobody published says
// nothing.
func (r *AgentProfileReconciler) withdrawCard(ctx context.Context, agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (string, string, error) {
	if _, _, ok := operatorBusPrincipal(); !ok {
		return reasonAgentProfileBusUnconfigured, "the operator has no bus identity; it published no card to withdraw", nil
	}
	entry, err := r.cards().Read(ctx, agent, p.Name)
	if err != nil {
		return reasonAgentProfileBusError, err.Error(), err
	}
	if entry.present && entry.kind == a2aKindAgentCard {
		if err := r.cards().Publish(ctx, agent, p.Name, nil); err != nil {
			return reasonAgentProfileBusError, err.Error(), err
		}
	}
	return reasonAgentProfileIdentityMissing, "no card is published while the profile's identity is not rendered", nil
}

// applyServiceAccount renders the operator-created ServiceAccount: owned by the
// profile, no token automount, and no RoleBinding anywhere. Its token exists to
// authenticate to the bus, through a projected volume the pod names.
func (r *AgentProfileReconciler) applyServiceAccount(ctx context.Context, p *agentv1alpha1.AgentProfile, name string) error {
	sa := &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: p.Namespace,
			Labels: map[string]string{
				labelPartOf:           a2aPartOf,
				a2aComponentLabel:     agentProfileComponent,
				agentProfileNameLabel: p.Name,
			},
		},
		AutomountServiceAccountToken: ptr.To(false),
	}
	if err := ctrl.SetControllerReference(p, sa, r.Scheme); err != nil {
		return err
	}
	return r.Patch(ctx, sa, client.Apply, client.FieldOwner(agentProfileComponent), client.ForceOwnership)
}

// deleteOwnServiceAccount removes the ServiceAccount the operator created for
// this profile, and only that: a ServiceAccount not controlled by the profile is
// someone else's, including one spec.identity names.
func (r *AgentProfileReconciler) deleteOwnServiceAccount(ctx context.Context, p *agentv1alpha1.AgentProfile) error {
	var sa corev1.ServiceAccount
	err := r.Get(ctx, types.NamespacedName{Namespace: p.Namespace, Name: agentProfileServiceAccountPrefix + p.Name}, &sa)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if !metav1.IsControlledBy(&sa, p) {
		return nil
	}
	return client.IgnoreNotFound(r.Delete(ctx, &sa))
}

// writeStatus records both conditions.
func (r *AgentProfileReconciler) writeStatus(ctx context.Context, p *agentv1alpha1.AgentProfile, agent *agentv1alpha1.PlatformAgent, sa, idReason, idMsg, cardReason, cardMsg string, identityReady bool) error {
	before := p.Status.DeepCopy()
	p.Status.ObservedGeneration = p.Generation
	p.Status.ServiceAccountName = sa
	p.Status.AgentRef = ""
	if agent != nil {
		p.Status.AgentRef = agent.Name
	}
	meta.SetStatusCondition(&p.Status.Conditions, metav1.Condition{
		Type:               agentv1alpha1.AgentProfileConditionIdentityReady,
		Status:             conditionStatus(identityReady),
		Reason:             idReason,
		Message:            idMsg,
		ObservedGeneration: p.Generation,
	})
	meta.SetStatusCondition(&p.Status.Conditions, metav1.Condition{
		Type:               agentv1alpha1.AgentProfileConditionCardPublished,
		Status:             conditionStatus(cardReason == reasonAgentProfilePublished),
		Reason:             cardReason,
		Message:            cardMsg,
		ObservedGeneration: p.Generation,
	})
	if equalAgentProfileStatus(before, &p.Status) {
		return nil
	}
	return r.Status().Update(ctx, p)
}

func conditionStatus(ok bool) metav1.ConditionStatus {
	if ok {
		return metav1.ConditionTrue
	}
	return metav1.ConditionFalse
}

// equalAgentProfileStatus compares two statuses ignoring condition transition
// times, which SetStatusCondition preserves when nothing else changed.
func equalAgentProfileStatus(a, b *agentv1alpha1.AgentProfileStatus) bool {
	if a.ObservedGeneration != b.ObservedGeneration || a.AgentRef != b.AgentRef || a.ServiceAccountName != b.ServiceAccountName || len(a.Conditions) != len(b.Conditions) {
		return false
	}
	for _, c := range b.Conditions {
		old := meta.FindStatusCondition(a.Conditions, c.Type)
		if old == nil || old.Status != c.Status || old.Reason != c.Reason || old.Message != c.Message || old.ObservedGeneration != c.ObservedGeneration {
			return false
		}
	}
	return true
}

// SetupWithManager registers the reconciler. A PlatformAgent change (a mode
// flip above all) re-reconciles every profile in its namespace, and so does any
// profile change, because two profiles naming one ServiceAccount resolve
// relative to each other.
func (r *AgentProfileReconciler) SetupWithManager(mgr ctrl.Manager) error {
	enqueueProfilesIn := func(ctx context.Context, namespace string) []reconcile.Request {
		var list agentv1alpha1.AgentProfileList
		if err := mgr.GetClient().List(ctx, &list, client.InNamespace(namespace)); err != nil {
			return nil
		}
		reqs := make([]reconcile.Request, 0, len(list.Items))
		for _, p := range list.Items {
			reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: p.Namespace, Name: p.Name}})
		}
		return reqs
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.AgentProfile{}).
		Owns(&corev1.ServiceAccount{}).
		Watches(
			&agentv1alpha1.PlatformAgent{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				return enqueueProfilesIn(ctx, obj.GetNamespace())
			}),
			builder.WithPredicates(predicate.GenerationChangedPredicate{}),
		).
		Watches(
			&agentv1alpha1.AgentProfile{},
			handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, obj client.Object) []reconcile.Request {
				return enqueueProfilesIn(ctx, obj.GetNamespace())
			}),
			builder.WithPredicates(predicate.GenerationChangedPredicate{}),
		).
		Named("agentprofile").
		Complete(r)
}
