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
	"errors"
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

	// agentProfileTierLabel is the tier an operator-created ServiceAccount in
	// the profile namespace is for.
	agentProfileTierLabel = "kubeagents.x-k8s.io/agentprofile-tier"

	// agentProfileUIDAnnotation marks an operator-created ServiceAccount in
	// the profile namespace with its profile's UID, the ownership test that
	// stands in for an owner reference across namespaces
	// (agentProfileOwnsServiceAccount).
	agentProfileUIDAnnotation = "kubeagents.x-k8s.io/agentprofile-uid"

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
	reasonAgentProfileInvalid         = "InvalidProfile"
	reasonAgentProfileModeUnknown     = "ModeNotRecognized"
	reasonAgentProfileSANotFound      = "ServiceAccountNotFound"
	reasonAgentProfilePublished       = "Published"
	reasonAgentProfileBusUnconfigured = "OperatorBusIdentityUnconfigured"
	reasonAgentProfileBusError        = "BusUnavailable"
	reasonAgentProfileIdentityMissing = "IdentityNotRendered"
	reasonAgentProfileNamespaceAbsent = "ProfileNamespaceAbsent"
)

// AgentProfileReconciler reconciles AgentProfile objects.
type AgentProfileReconciler struct {
	client.Client
	Scheme *runtime.Scheme
	// APIReader reads Namespaces uncached: a cached read would start a
	// cluster-wide Namespace informer. Nil falls back to the client (tests).
	APIReader client.Reader
	// Cards is the directory. Nil means the default NATS publisher.
	Cards cardPublisher
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentprofiles/finalizers,verbs=update

// Reconcile renders one profile.
func (r *AgentProfileReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	log := logf.FromContext(ctx)

	// The namespace-only request SetupWithManager sends when a PlatformAgent
	// changes and no profile is left to reconcile: drop the bus connection
	// held for that namespace, which nothing else would reach.
	if req.Name == "" {
		r.forgetBus(req.Namespace)
		return ctrl.Result{}, nil
	}

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
				// down; freeze the profile's objects with it. A deletion
				// still runs: the frozen bus is running, so the tombstone
				// can be tried, and holding the finalizer would hang the
				// delete until the operator is rolled forward.
				if !profile.DeletionTimestamp.IsZero() {
					return r.finalize(ctx, &profile, agent)
				}
				return ctrl.Result{}, r.writeStatus(ctx, &profile, agent, "", reasonAgentProfileModeUnknown, modeErr.Error(), reasonAgentProfileModeUnknown, modeErr.Error(), false)
			}
		}
		// No bus to talk to: drop any connection held for this namespace's
		// agent rather than let it reconnect to a dead Service forever. Not
		// under version skew above, where the bus is frozen and still
		// running, and a deletion still needs the connection for its
		// tombstone.
		r.forgetBus(profile.Namespace)
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
	// The single ServiceAccount #2469 created in the agent's namespace,
	// before per-tier identities moved to the profile namespace.
	if err := r.deleteOwnServiceAccount(ctx, &profile); err != nil {
		return ctrl.Result{}, err
	}
	resolution := resolveAgentProfileIdentities(agent, all.Items)[profile.Name]
	profilesNS := profileNamespaceOf(&profile)
	if resolution.refused == nil {
		foreign, err := agentProfileServiceAccountIsForeign(ctx, r.Client, &profile)
		if err != nil {
			return ctrl.Result{}, err
		}
		if foreign != "" {
			resolution = agentProfileResolution{refused: fmt.Errorf("ServiceAccount %s/%s already exists and is not this profile's; the operator does not adopt it", profilesNS, foreign), reason: reasonAgentProfileRefused}
		}
	}

	if resolution.refused != nil {
		if err := r.deleteProfileServiceAccounts(ctx, &profile, profilesNS, nil); err != nil {
			return ctrl.Result{}, err
		}
		cardReason, cardMsg, busErr := r.withdrawCard(ctx, agent, &profile)
		if err := r.writeStatus(ctx, &profile, agent, "", resolution.reason, resolution.refused.Error(), cardReason, cardMsg, false); err != nil {
			return ctrl.Result{}, err
		}
		return r.busResult(busErr)
	}

	// The profile namespace is the chart's (agentprofile_namespace.go). Until
	// it exists, carrying the chart's markers, there is nowhere to render the
	// ServiceAccounts, so the profile has no identity and no card.
	ready, err := profileNamespaceReady(ctx, r.apiReader(), agent)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !ready {
		cardReason, cardMsg, busErr := r.withdrawCard(ctx, agent, &profile)
		msg := "the profile namespace " + profilesNS + " does not exist or lacks the chart's markers; the chart creates it on a mode: next install"
		if err := r.writeStatus(ctx, &profile, agent, "", reasonAgentProfileNamespaceAbsent, msg, cardReason, cardMsg, false); err != nil {
			return ctrl.Result{}, err
		}
		return r.busResult(busErr)
	}

	idReason, idMsg := reasonAgentProfileRendered, "ServiceAccounts "+resolution.serviceAccountList()+" in "+profilesNS+" and their bus identities are rendered"
	if profile.Spec.Identity.ServiceAccountName == "" {
		keep := map[string]bool{}
		for _, id := range resolution.identities {
			keep[id.serviceAccount] = true
			err := r.ensureServiceAccount(ctx, &profile, profilesNS, id)
			if errors.Is(err, errForeignServiceAccount) {
				cardReason, cardMsg, busErr := r.withdrawCard(ctx, agent, &profile)
				if err := r.writeStatus(ctx, &profile, agent, "", reasonAgentProfileRefused, err.Error(), cardReason, cardMsg, false); err != nil {
					return ctrl.Result{}, err
				}
				return r.busResult(busErr)
			}
			if err != nil {
				return ctrl.Result{}, err
			}
		}
		// A tier the profile no longer allows loses its ServiceAccount.
		if err := r.deleteProfileServiceAccounts(ctx, &profile, profilesNS, keep); err != nil {
			return ctrl.Result{}, err
		}
	} else {
		if err := r.deleteProfileServiceAccounts(ctx, &profile, profilesNS, nil); err != nil {
			return ctrl.Result{}, err
		}
		var sa corev1.ServiceAccount
		named := profile.Spec.Identity.ServiceAccountName
		err := r.Get(ctx, types.NamespacedName{Namespace: profilesNS, Name: named}, &sa)
		switch {
		case apierrors.IsNotFound(err):
			idReason, idMsg = reasonAgentProfileSANotFound, "spec.identity.serviceAccountName "+named+" does not exist in "+profilesNS+"; the bus identity is rendered and no pod can present a token for it until it does"
		case err != nil:
			return ctrl.Result{}, err
		}
	}

	cardReason, cardMsg, busErr := r.publishCard(ctx, agent, &profile)
	if busErr != nil {
		log.Info("could not reconcile the agent card", "profile", profile.Name, "error", busErr.Error())
	}
	if err := r.writeStatus(ctx, &profile, agent, resolution.serviceAccountList(), idReason, idMsg, cardReason, cardMsg, idReason == reasonAgentProfileRendered); err != nil {
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
	// With or without a bound agent: a profile deleted after its agent still
	// has ServiceAccounts in the profile namespace, which outlives the agent.
	if err := r.deleteProfileServiceAccounts(ctx, p, profileNamespaceOf(p), nil); err != nil {
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
	// The ServiceAccounts in the profile namespace carry no owner reference,
	// so the garbage collector won't remove them with the profile.
	if err := r.deleteProfileServiceAccounts(ctx, p, profileNamespaceOf(p), nil); err != nil {
		return ctrl.Result{}, err
	}
	controllerutil.RemoveFinalizer(p, agentProfileFinalizer)
	return ctrl.Result{}, r.Update(ctx, p)
}

func (r *AgentProfileReconciler) cards() cardPublisher {
	if r.Cards == nil {
		r.Cards = newNATSCardPublisher(r.Client)
	}
	return r.Cards
}

// publishCard makes the directory entry the card the profile renders, writing
// only when the entry is missing, a tombstone, or a different card.
func (r *AgentProfileReconciler) publishCard(ctx context.Context, agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (string, string, error) {
	if !isDNS1123LabelToken(p.Name) {
		return reasonAgentProfileIdentityMissing, "the profile's name is not a subject token, so it has no directory entry", nil
	}
	if _, _, ok := operatorBusPrincipal(); !ok {
		return reasonAgentProfileBusUnconfigured, "the operator's " + operatorNamespaceEnvVar + " and " + operatorServiceAccountEnvVar + " are unset or are not a namespace and ServiceAccount name, so it has no bus identity to publish cards with", nil
	}
	want := desiredAgentCard(p)
	entry, err := r.cards().Read(ctx, agent, p.Name)
	if err != nil {
		return reasonAgentProfileBusError, err.Error(), err
	}
	if !entry.current(want) {
		if err := r.cards().Publish(ctx, agent, p.Name, &want); err != nil {
			return reasonAgentProfileBusError, err.Error(), err
		}
	}
	// The profile's durables, once the dispatcher that reads them exists:
	// one per (allowed tier, kind), and a dropped tier's deleted.
	if a2aDispatcherEnabled() {
		if err := r.cards().SyncDurables(ctx, agent, p.Name, desiredProfileDurables(p)); err != nil {
			// Drop the connection: callout grants are fixed when it opens,
			// and one opened before the callout served the map that grants
			// the operator its durables would be refused forever. The retry
			// reconnects under the map the callout serves now.
			r.forgetBus(agent.Namespace)
			return reasonAgentProfileBusError, "the agent card is published, but its durables are not: " + err.Error(), err
		}
	}
	return reasonAgentProfilePublished, "the agent card is on " + agentCardSubject(p.Name), nil
}

// withdrawCard makes the directory entry a tombstone if anything but a
// tombstone is there: a card, or an entry that does not decode. An absent entry
// stays absent: a tombstone for a card nobody published says nothing. A profile
// whose name is not a subject token has no directory subject at all, so there
// is nothing to read or withdraw (and the operator's grant could not reach it).
func (r *AgentProfileReconciler) withdrawCard(ctx context.Context, agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (string, string, error) {
	if !isDNS1123LabelToken(p.Name) {
		return reasonAgentProfileIdentityMissing, "the profile's name is not a subject token, so it has no directory entry", nil
	}
	if _, _, ok := operatorBusPrincipal(); !ok {
		return reasonAgentProfileBusUnconfigured, "the operator has no bus identity; it published no card to withdraw", nil
	}
	entry, err := r.cards().Read(ctx, agent, p.Name)
	if err != nil {
		return reasonAgentProfileBusError, err.Error(), err
	}
	if entry.present && entry.kind != a2aKindAgentClosed {
		if err := r.cards().Publish(ctx, agent, p.Name, nil); err != nil {
			return reasonAgentProfileBusError, err.Error(), err
		}
	}
	// A profile with no identity has no queue: its durables go with its
	// card, held submissions and all. Only the ones this operator made.
	if a2aDispatcherEnabled() {
		if err := r.cards().SyncDurables(ctx, agent, p.Name, nil); err != nil {
			r.forgetBus(agent.Namespace) // as in publishCard
			return reasonAgentProfileBusError, "the card is withdrawn, but the durables are not deleted: " + err.Error(), err
		}
	}
	return reasonAgentProfileIdentityMissing, "no card is published while the profile's identity is not rendered", nil
}

// errForeignServiceAccount is ensureServiceAccount's refusal: a ServiceAccount
// under the operator-created name exists and is not this profile's.
var errForeignServiceAccount = errors.New("a ServiceAccount under the operator-created name already exists and is not this profile's; the operator does not adopt it")

// ensureServiceAccount renders the operator-created ServiceAccount: owned by
// the profile, no token automount, and no RoleBinding anywhere. Its token
// exists to authenticate to the bus, through a projected volume the pod names.
//
// It creates rather than applies. Server-side apply onto an existing object
// merges, so an apply would adopt a ServiceAccount someone created by hand
// (with whatever RoleBindings they gave it) whenever it landed between the
// foreignness check and the write. Create refuses that case outright with
// AlreadyExists, and an existing ServiceAccount is only ever updated when the
// profile already controls it.
func (r *AgentProfileReconciler) ensureServiceAccount(ctx context.Context, p *agentv1alpha1.AgentProfile, ns string, id agentProfileTierIdentity) error {
	sa := &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      id.serviceAccount,
			Namespace: ns,
			Labels: map[string]string{
				labelPartOf:           a2aPartOf,
				a2aComponentLabel:     agentProfileComponent,
				agentProfileNameLabel: p.Name,
				agentProfileTierLabel: id.tier,
			},
			Annotations: map[string]string{agentProfileUIDAnnotation: string(p.UID)},
		},
		AutomountServiceAccountToken: ptr.To(false),
	}
	var existing corev1.ServiceAccount
	err := r.Get(ctx, types.NamespacedName{Namespace: ns, Name: id.serviceAccount}, &existing)
	if apierrors.IsNotFound(err) {
		err = r.Create(ctx, sa)
		if apierrors.IsAlreadyExists(err) {
			// A cached NotFound can trail this reconciler's own create (the
			// status write requeues at once), so ask the API server whose
			// it is before calling it foreign.
			if err := r.apiReader().Get(ctx, types.NamespacedName{Namespace: ns, Name: id.serviceAccount}, &existing); err != nil {
				return err
			}
			if !agentProfileOwnsServiceAccount(&existing, p) {
				return errForeignServiceAccount
			}
			return nil
		}
		return err
	}
	if err != nil {
		return err
	}
	if !agentProfileOwnsServiceAccount(&existing, p) {
		return errForeignServiceAccount
	}
	// Ours: keep it as rendered, forced, so a hand edit is put back rather
	// than conflicting on every reconcile. The same accepted risk as before
	// (decided on gke-labs#2469): the apply goes by name after a cached
	// ownership check, so a ServiceAccount deleted and re-created under this
	// name inside the informer lag could be adopted; doing that already takes
	// ServiceAccount create in the namespace.
	return r.Patch(ctx, sa, client.Apply, client.FieldOwner(agentProfileComponent), client.ForceOwnership)
}

// deleteProfileServiceAccounts removes this profile's ServiceAccounts in the
// profile namespace except the ones keep names, and only ones it owns
// (agentProfileOwnsServiceAccount): a ServiceAccount spec.identity names, or
// one someone else created, is left alone. Each delete is held to the UID it
// checked, so one re-created inside the informer lag is not removed.
func (r *AgentProfileReconciler) deleteProfileServiceAccounts(ctx context.Context, p *agentv1alpha1.AgentProfile, ns string, keep map[string]bool) error {
	var list corev1.ServiceAccountList
	if err := r.List(ctx, &list, client.InNamespace(ns), client.MatchingLabels{agentProfileNameLabel: p.Name}); err != nil {
		return err
	}
	for i := range list.Items {
		sa := &list.Items[i]
		if keep[sa.Name] || !agentProfileOwnsServiceAccount(sa, p) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, sa, client.Preconditions{UID: &sa.UID})); err != nil {
			return err
		}
	}
	return nil
}

// profileServiceAccountRequest maps a ServiceAccount in a profile namespace,
// labelled with a profile's name, to that profile in the agent's namespace.
// Anything else maps to nothing. A label someone copied only costs a
// reconcile, which checks ownership by UID.
func profileServiceAccountRequest(obj client.Object) []reconcile.Request {
	name := obj.GetLabels()[agentProfileNameLabel]
	agentNS, ok := agentNamespaceOfProfileServiceAccount(obj.GetNamespace())
	if name == "" || !ok {
		return nil
	}
	return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: agentNS, Name: name}}}
}

func (r *AgentProfileReconciler) apiReader() client.Reader {
	if r.APIReader != nil {
		return r.APIReader
	}
	return r.Client
}

// forgetBus drops the bus connection held for an agent in namespace, if the
// directory is the NATS publisher (tests substitute their own).
func (r *AgentProfileReconciler) forgetBus(namespace string) {
	if f, ok := r.Cards.(interface{ forget(namespace string) }); ok {
		f.forget(namespace)
	}
}

// deleteOwnServiceAccount removes the ServiceAccount #2469 created for this
// profile in the agent's namespace, before per-tier identities in the profile
// namespace replaced it, and only that: a ServiceAccount not controlled by the profile is
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
	// The ownership verdict is on the cached copy, so the delete is held to
	// that copy's UID: a ServiceAccount deleted and re-created under the same
	// name inside the informer lag is someone else's, and the precondition
	// fails rather than deleting it. The next pass reads the new one, which
	// is not controlled by the profile, and leaves it.
	return client.IgnoreNotFound(r.Delete(ctx, &sa, client.Preconditions{UID: &sa.UID}))
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

// agentProfileRequestsIn is what a PlatformAgent or AgentProfile event in a
// namespace enqueues: every profile there, or, with none left, the
// namespace-only request that tells Reconcile to drop the namespace's bus
// connection.
func agentProfileRequestsIn(ctx context.Context, c client.Reader, namespace string) []reconcile.Request {
	var list agentv1alpha1.AgentProfileList
	if err := c.List(ctx, &list, client.InNamespace(namespace)); err != nil {
		return nil
	}
	if len(list.Items) == 0 {
		return []reconcile.Request{{NamespacedName: types.NamespacedName{Namespace: namespace}}}
	}
	reqs := make([]reconcile.Request, 0, len(list.Items))
	for _, p := range list.Items {
		reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: p.Namespace, Name: p.Name}})
	}
	return reqs
}

// SetupWithManager registers the reconciler. A PlatformAgent change (a mode
// flip above all) re-reconciles every profile in its namespace, and so does any
// profile change, because two profiles naming one ServiceAccount resolve
// relative to each other.
func (r *AgentProfileReconciler) SetupWithManager(mgr ctrl.Manager) error {
	enqueueProfilesIn := func(ctx context.Context, namespace string) []reconcile.Request {
		return agentProfileRequestsIn(ctx, mgr.GetClient(), namespace)
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.AgentProfile{}).
		// The legacy agent-namespace ServiceAccount (deleteOwnServiceAccount).
		Owns(&corev1.ServiceAccount{}).
		// The per-tier ServiceAccounts carry no owner reference, so they map
		// back to their profile by name label and namespace.
		Watches(
			&corev1.ServiceAccount{},
			handler.EnqueueRequestsFromMapFunc(func(_ context.Context, obj client.Object) []reconcile.Request {
				return profileServiceAccountRequest(obj)
			}),
		).
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
