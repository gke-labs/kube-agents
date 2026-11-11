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
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A newly bound AgentProfile is held out of the dispatcher until every bus
// server has loaded the profile's config entry: the gateway's publish deny on
// the profile's supervisor subjects (gatewayProfileSupervisorDeny). The bus
// config carries every bound profile as soon as it is bound, as before; the
// dispatch config, the dispatcher's identity-map grant and the agent card
// carry a profile only once the bus has loaded it. A server still on a config
// without the deny would otherwise let the gateway publish on a supervisor
// subject the dispatcher already listens on.
//
// What every server has loaded is read from objects that already exist, so it
// survives an operator restart:
//
//   - On a cluster every profile change rolls the servers
//     (a2aConfigRolloutHash), and the pod template carries the profile list it
//     was rendered with (a2aConfigProfilesAnnotation). Each server runs one
//     StatefulSet revision, recorded in its controller-revision-hash label, and
//     a server still to be created will run status.updateRevision. The loaded
//     set is the profiles every one of those revisions lists.
//   - A single server reloads in place (planA2ABusReload). The config Secret
//     carries the profile list it renders, and while the server may still run
//     an earlier config (a reload pending, or a restart or roll unfinished) it
//     also carries the profiles that earlier config had
//     (a2aBusPreviousProfilesAnnotation). The loaded set is the two lists'
//     intersection until the server is on the new config.
//
// A profile is held only on its way in. One the dispatch config already lists
// live, or whose card is already on the directory, stays where it is, so a
// bus that can't be read for a moment never pulls a working profile.
const (
	// a2aConfigProfilesAnnotation lists the profiles whose config entry a
	// render of the bus config carries: sorted, comma-separated. It rides a
	// cluster's pod template, so every StatefulSet revision records it, and
	// the config Secret.
	a2aConfigProfilesAnnotation = "kubeagents.x-k8s.io/a2a-config-profiles"

	// a2aBusPreviousProfilesAnnotation is on a single server's config Secret
	// while the server may still run an earlier config: the profiles it is
	// known to have loaded before this config was written.
	a2aBusPreviousProfilesAnnotation = "kubeagents.x-k8s.io/a2a-reload-previous-profiles"

	// a2aConfigProfilesSeparator joins the names in both annotations. A
	// profile's name is a DNS-1123 label, which never contains it.
	a2aConfigProfilesSeparator = ","

	// reasonAgentProfileBusConfigPending is the CardPublished reason while
	// a newly bound profile waits for every bus server to load its config.
	reasonAgentProfileBusConfigPending = "BusConfigPending"
)

// encodeA2AConfigProfiles is the annotation value for names: sorted, once each.
func encodeA2AConfigProfiles(names []string) string {
	out := slices.Clone(names)
	slices.Sort(out)
	return strings.Join(slices.Compact(out), a2aConfigProfilesSeparator)
}

// decodeA2AConfigProfiles reads an annotation value back into a set.
func decodeA2AConfigProfiles(value string) map[string]bool {
	set := map[string]bool{}
	for name := range strings.SplitSeq(value, a2aConfigProfilesSeparator) {
		if name != "" {
			set[name] = true
		}
	}
	return set
}

// a2aLoadedProfiles is what every bus server has loaded: all of any
// profile list (no bus server exists, so none runs an older config), or the
// named profiles.
type a2aLoadedProfiles struct {
	all   bool
	names map[string]bool
}

// has reports whether every bus server has loaded the profile's config.
func (l a2aLoadedProfiles) has(name string) bool { return l.all || l.names[name] }

// loadedNames is a2aLoadedProfiles of exactly names.
func loadedNames(names map[string]bool) a2aLoadedProfiles {
	return a2aLoadedProfiles{names: names}
}

// a2aNoBusServer: no StatefulSet, so no server can be on an older config;
// the first one starts on a config carrying every bound profile.
var a2aNoBusServer = a2aLoadedProfiles{all: true}

// intersectProfileSets is the names in both a and b.
func intersectProfileSets(a, b map[string]bool) map[string]bool {
	out := map[string]bool{}
	for name := range a {
		if b[name] {
			out[name] = true
		}
	}
	return out
}

// annotateA2ABusProfiles puts the bus config's profile list on a cluster's
// pod template. A single server's template leaves it out: there the list
// reaches the server by a reload, and a template change would restart it.
func annotateA2ABusProfiles(agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet, profiles []string) {
	if a2aNATSReplicas(agent) <= 1 {
		return
	}
	if sts.Spec.Template.Annotations == nil {
		sts.Spec.Template.Annotations = map[string]string{}
	}
	sts.Spec.Template.Annotations[a2aConfigProfilesAnnotation] = encodeA2AConfigProfiles(profiles)
}

// a2aBusLoadedProfiles is what every bus server has loaded, read through
// reader (uncached: the Secret, StatefulSet, pods and ControllerRevisions are
// all outside the manager's cache). With no bus StatefulSet there is no
// server to wait for (a2aNoBusServer). A bus this operator may not read
// (a ClusterRole older than the image: the RBAC self-check names the missing
// grant) has loaded nothing, which holds new profiles and nothing else.
func a2aBusLoadedProfiles(ctx context.Context, reader client.Reader, agent *agentv1alpha1.PlatformAgent) (a2aLoadedProfiles, error) {
	sts := &appsv1.StatefulSet{}
	err := reader.Get(ctx, types.NamespacedName{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts)
	if apierrors.IsNotFound(err) {
		return a2aNoBusServer, nil
	}
	var loaded map[string]bool
	if err == nil {
		if a2aNATSReplicas(agent) > 1 {
			loaded, err = a2aClusterLoadedProfiles(ctx, reader, sts)
		} else {
			loaded, err = a2aServerLoadedProfiles(ctx, reader, agent)
		}
	}
	if apierrors.IsForbidden(err) {
		return loadedNames(map[string]bool{}), nil
	}
	if err != nil {
		return a2aLoadedProfiles{}, err
	}
	return loadedNames(loaded), nil
}

// a2aClusterLoadedProfiles reads a cluster's loaded set off its revisions:
// every revision a server runs, and the update revision any server yet to
// be created will run. A server pod with no revision label, or a revision
// that is missing, not the StatefulSet's, or doesn't decode, loads nothing.
func a2aClusterLoadedProfiles(ctx context.Context, reader client.Reader, sts *appsv1.StatefulSet) (map[string]bool, error) {
	revisions := map[string]bool{}
	if sts.Status.UpdateRevision != "" {
		revisions[sts.Status.UpdateRevision] = true
	}
	ordinals := a2aNATSMaxReplicas
	if sts.Spec.Replicas != nil {
		ordinals = max(ordinals, int(*sts.Spec.Replicas))
	}
	for i := range ordinals {
		pod := &corev1.Pod{}
		if err := reader.Get(ctx, types.NamespacedName{Name: sts.Name + "-" + strconv.Itoa(i), Namespace: sts.Namespace}, pod); err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			return nil, err
		}
		if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID {
			continue
		}
		revision := pod.Labels[appsv1.ControllerRevisionHashLabelKey]
		if revision == "" {
			return map[string]bool{}, nil
		}
		revisions[revision] = true
	}
	if len(revisions) == 0 {
		return map[string]bool{}, nil
	}
	var loaded map[string]bool
	for name := range revisions {
		rev := &appsv1.ControllerRevision{}
		if err := reader.Get(ctx, types.NamespacedName{Name: name, Namespace: sts.Namespace}, rev); err != nil {
			if apierrors.IsNotFound(err) {
				return map[string]bool{}, nil
			}
			return nil, err
		}
		if !metav1.IsControlledBy(rev, sts) {
			return map[string]bool{}, nil
		}
		names, ok := controllerRevisionProfiles(rev)
		if !ok {
			return map[string]bool{}, nil
		}
		if loaded == nil {
			loaded = names
		} else {
			loaded = intersectProfileSets(loaded, names)
		}
	}
	return loaded, nil
}

// controllerRevisionProfiles reads the profile list off a StatefulSet
// revision: its data is the pod template the revision stamps pods from, as a
// patch to spec.template. A revision rendered before the list was recorded
// carries none, and loads nothing.
func controllerRevisionProfiles(rev *appsv1.ControllerRevision) (map[string]bool, bool) {
	raw := rev.Data.Raw
	if len(raw) == 0 && rev.Data.Object != nil {
		var err error
		if raw, err = json.Marshal(rev.Data.Object); err != nil {
			return nil, false
		}
	}
	var data struct {
		Spec struct {
			Template struct {
				Metadata struct {
					Annotations map[string]string `json:"annotations"`
				} `json:"metadata"`
			} `json:"template"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, false
	}
	return decodeA2AConfigProfiles(data.Spec.Template.Metadata.Annotations[a2aConfigProfilesAnnotation]), true
}

// a2aServerLoadedProfiles reads a single server's loaded set off its config
// Secret: the list the Secret renders, narrowed to the previous list while a
// reload is pending or the server isn't yet settled on this config
// (a2aSingleBusSettled). A pending reload with no previous list recorded (one
// stamped before this was) loads nothing new.
func a2aServerLoadedProfiles(ctx context.Context, reader client.Reader, agent *agentv1alpha1.PlatformAgent) (map[string]bool, error) {
	live := &corev1.Secret{}
	if err := reader.Get(ctx, types.NamespacedName{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace}, live); err != nil {
		if apierrors.IsNotFound(err) {
			return map[string]bool{}, nil
		}
		return nil, err
	}
	return a2aServerLoadedFrom(ctx, reader, agent, live)
}

// a2aServerLoadedFrom is a2aServerLoadedProfiles on a Secret already read.
func a2aServerLoadedFrom(ctx context.Context, reader client.Reader, agent *agentv1alpha1.PlatformAgent, live *corev1.Secret) (map[string]bool, error) {
	loaded := decodeA2AConfigProfiles(live.Annotations[a2aConfigProfilesAnnotation])
	previous, recorded := live.Annotations[a2aBusPreviousProfilesAnnotation]
	if _, pending := live.Annotations[a2aBusReloadRequestedAnnotation]; pending {
		return intersectProfileSets(loaded, decodeA2AConfigProfiles(previous)), nil
	}
	if !recorded {
		return loaded, nil
	}
	settled, err := a2aSingleBusSettled(ctx, reader, agent)
	if err != nil {
		return nil, err
	}
	if !settled {
		return intersectProfileSets(loaded, decodeA2AConfigProfiles(previous)), nil
	}
	return loaded, nil
}

// a2aSingleBusSettled reports whether a single server can only be running
// the config Secret as it is now: the StatefulSet has observed its template
// and rolled every replica onto it, and the server pod is not terminating (a
// pod deleted for a failed reload still serves its old config until it
// exits). No StatefulSet, or no pod, runs nothing older.
func a2aSingleBusSettled(ctx context.Context, reader client.Reader, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	sts := &appsv1.StatefulSet{}
	if err := reader.Get(ctx, types.NamespacedName{Name: a2aNATSName(agent), Namespace: agent.Namespace}, sts); err != nil {
		if apierrors.IsNotFound(err) {
			return true, nil
		}
		return false, err
	}
	replicas := int32(1)
	if sts.Spec.Replicas != nil {
		replicas = *sts.Spec.Replicas
	}
	if sts.Status.ObservedGeneration < sts.Generation || sts.Status.UpdatedReplicas < replicas ||
		(sts.Status.CurrentRevision != "" && sts.Status.CurrentRevision != sts.Status.UpdateRevision) {
		return false, nil
	}
	pod := &corev1.Pod{}
	if err := reader.Get(ctx, types.NamespacedName{Name: sts.Name + a2aNATSPodOrdinalSuffix, Namespace: sts.Namespace}, pod); err != nil {
		if apierrors.IsNotFound(err) {
			return true, nil
		}
		return false, err
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID {
		return true, nil
	}
	return pod.DeletionTimestamp == nil, nil
}

// stampA2ABusPreviousProfiles records on config, a single server's config
// Secret about to be applied, the profiles the server is known to have
// loaded, for as long as it may run an earlier config: from the pass that
// changes the config until a pending reload is confirmed and the server is
// settled on it. planA2ABusReload runs first, so config already carries any
// reload it stamped. A cluster records nothing here: its revisions say it.
func (r *PlatformAgentReconciler) stampA2ABusPreviousProfiles(ctx context.Context, agent *agentv1alpha1.PlatformAgent, config *corev1.Secret) error {
	if a2aNATSReplicas(agent) > 1 {
		return nil
	}
	live := &corev1.Secret{}
	if err := r.a2aReader().Get(ctx, types.NamespacedName{Name: config.Name, Namespace: config.Namespace}, live); err != nil {
		return client.IgnoreNotFound(err)
	}
	loaded, err := a2aServerLoadedFrom(ctx, r.podReader(), agent, live)
	if err != nil {
		return err
	}
	_, reloadPending := config.Annotations[a2aBusReloadRequestedAnnotation]
	_, recorded := live.Annotations[a2aBusPreviousProfilesAnnotation]
	changed := string(live.Data[a2aNATSConfKey]) != string(config.Data[a2aNATSConfKey])
	if !changed && !reloadPending && recorded {
		settled, err := a2aSingleBusSettled(ctx, r.podReader(), agent)
		if err != nil {
			return err
		}
		recorded = !settled
	}
	if !changed && !reloadPending && !recorded {
		return nil
	}
	if config.Annotations == nil {
		config.Annotations = map[string]string{}
	}
	config.Annotations[a2aBusPreviousProfilesAnnotation] = encodeA2AConfigProfiles(setNames(loaded))
	return nil
}

// setNames is a set's members, in no particular order.
func setNames(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	return out
}

// dispatchConfigLiveProfiles is the profiles the applied dispatch config
// lists live (not draining): the ones the hold no longer applies to. None
// applied, or a body that doesn't decode (dispatchDraining warns about it),
// lists none.
func (r *PlatformAgentReconciler) dispatchConfigLiveProfiles(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (map[string]bool, error) {
	cm := &corev1.ConfigMap{}
	if err := r.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		if apierrors.IsNotFound(err) {
			return map[string]bool{}, nil
		}
		return nil, fmt.Errorf("reading the applied dispatch config for the profiles it lists: %w", err)
	}
	var applied a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &applied); err != nil {
		return map[string]bool{}, nil
	}
	live := map[string]bool{}
	for _, p := range applied.Profiles {
		if p.DrainingUntil.IsZero() {
			live[p.Name] = true
		}
	}
	return live, nil
}

// a2aDispatchableProfiles splits the bound profiles into the ones the
// dispatcher may take (every bus server has loaded the profile's config, or
// the dispatch config already lists it live) and the names of the ones
// held until the bus has.
func (r *PlatformAgentReconciler) a2aDispatchableProfiles(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) ([]agentv1alpha1.AgentProfile, []string, error) {
	if len(profiles) == 0 {
		return profiles, nil, nil
	}
	loaded, err := a2aBusLoadedProfiles(ctx, r.podReader(), agent)
	if err != nil {
		return nil, nil, fmt.Errorf("reading which profiles every A2A bus server has loaded: %w", err)
	}
	live, err := r.dispatchConfigLiveProfiles(ctx, agent)
	if err != nil {
		return nil, nil, err
	}
	dispatchable := make([]agentv1alpha1.AgentProfile, 0, len(profiles))
	var held []string
	for _, p := range profiles {
		if loaded.has(p.Name) || live[p.Name] {
			dispatchable = append(dispatchable, p)
		} else {
			held = append(held, p.Name)
		}
	}
	return dispatchable, held, nil
}

// agentProfileCardHeld reports whether the profile's card waits for the bus:
// no bus server set has loaded its config yet and the directory has no live
// card for it. A card already published stays (the profile was dispatchable
// when it went out). A directory that can't be read holds nothing here:
// publishCard meets the same failure and reports it.
func (r *AgentProfileReconciler) agentProfileCardHeld(ctx context.Context, agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (bool, error) {
	loaded, err := a2aBusLoadedProfiles(ctx, r.apiReader(), agent)
	if err != nil {
		return false, err
	}
	if loaded.has(p.Name) || !isDNS1123LabelToken(p.Name) {
		return false, nil
	}
	if _, _, ok := operatorBusPrincipal(); !ok {
		return false, nil
	}
	entry, err := r.cards().Read(ctx, agent, p.Name)
	if err != nil {
		return false, nil // publishCard meets the same error and reports it
	}
	return !entry.present || entry.kind == a2aKindAgentClosed, nil
}
