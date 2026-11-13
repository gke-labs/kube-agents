package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The install's tier table as a file (#3659, Tier PR 2a). The operator renders
// every Tier resource in the agent's namespace into one ConfigMap,
// <agent>-a2a-tiers, key tiers.json, and mounts it into the three components
// that check a grant's tier: the capability verifier, the gateway (its mint
// validates every grant) and the auth callout (a profile entry may name any
// profile tier). Each reads it with capability.StartTierFile and re-reads it on
// change. The file can only add profile tiers at one of the two built-in verb
// sets: the built-in and legacy tiers are compiled in and can't be redefined,
// and a missing or bad file serves the compiled-in table.
//
// Who can change it: the ConfigMap is the operator's, rendered only from the
// Tier resources, and it sits in the install's namespace. Anyone who can edit
// objects in that namespace already controls the install, so the file adds
// no trust beyond what writing a Tier resource takes.
const (
	a2aTiersKey       = "tiers.json"
	a2aTiersVolume    = "a2a-tiers"
	a2aTiersMountPath = "/etc/a2a-tiers"
	// a2aTiersFileEnvVar is capability.TiersFileEnv; this module can't import
	// the a2a module.
	a2aTiersFileEnvVar = "A2A_TIERS_FILE"
)

func a2aTiersName(agent *agentv1alpha1.PlatformAgent) string { return agent.Name + "-a2a-tiers" }

// a2aTiersFile is the file's shape, which capability.ParseTierFile reads.
type a2aTiersFile struct {
	Tiers []a2aTierEntry `json:"tiers"`
}

type a2aTierEntry struct {
	Name string `json:"name"`
	Mode string `json:"mode"`
}

// buildA2ATiersConfigMap renders tiers, sorted by name, into the ConfigMap.
// A tier being deleted is left out.
func buildA2ATiersConfigMap(agent *agentv1alpha1.PlatformAgent, tiers []agentv1alpha1.Tier) (*corev1.ConfigMap, error) {
	f := a2aTiersFile{Tiers: []a2aTierEntry{}}
	for _, t := range tiers {
		if t.DeletionTimestamp != nil {
			continue
		}
		f.Tiers = append(f.Tiers, a2aTierEntry{Name: t.Name, Mode: string(t.Spec.Mode)})
	}
	sort.Slice(f.Tiers, func(i, j int) bool { return f.Tiers[i].Name < f.Tiers[j].Name })
	raw, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return nil, err
	}
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aTiersName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "tiers"),
		},
		Data: map[string]string{a2aTiersKey: string(raw) + "\n"},
	}, nil
}

// applyA2ATiers renders the Tier resources in the agent's namespace into the
// tiers ConfigMap. With the Tier CRD not installed, or unreadable, it renders
// an empty list, which serves the compiled-in tiers.
func (r *PlatformAgentReconciler) applyA2ATiers(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	var list agentv1alpha1.TierList
	// A role that can't read Tiers lists nothing: a cached List would start
	// an informer that never syncs and hold the reconcile (#1009's case).
	if !r.tiersUnreadable {
		if err := r.List(ctx, &list, client.InNamespace(agent.Namespace)); err != nil && !isCRDNotInstalledError(err) {
			return fmt.Errorf("failed to list Tiers: %w", err)
		}
	}
	cm, err := buildA2ATiersConfigMap(agent, list.Items)
	if err != nil {
		return err
	}
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return err
	}
	if err := r.applyManaged(ctx, agent, cm); err != nil {
		return fmt.Errorf("failed to apply the tiers ConfigMap: %w", err)
	}
	return nil
}

// a2aTiersVolumeAndMount is the tiers file's volume and mount. The volume is
// optional, so a pod that starts before the ConfigMap exists runs on the
// compiled-in tiers and picks the file up when it appears, and the mount is
// the whole directory (no subPath), so an edit reaches the file without a
// restart.
func a2aTiersVolumeAndMount(agent *agentv1alpha1.PlatformAgent) (corev1.Volume, corev1.VolumeMount, corev1.EnvVar) {
	return corev1.Volume{
			Name: a2aTiersVolume,
			VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: a2aTiersName(agent)},
				Optional:             ptr.To(true),
			}},
		},
		corev1.VolumeMount{Name: a2aTiersVolume, MountPath: a2aTiersMountPath, ReadOnly: true},
		corev1.EnvVar{Name: a2aTiersFileEnvVar, Value: a2aTiersMountPath + "/" + a2aTiersKey}
}

// withA2ATiersFile mounts the tiers file into the named container of spec
// and points its A2A_TIERS_FILE at it.
func withA2ATiersFile(agent *agentv1alpha1.PlatformAgent, spec *corev1.PodSpec, container string) {
	vol, mount, env := a2aTiersVolumeAndMount(agent)
	for i := range spec.Containers {
		if spec.Containers[i].Name != container {
			continue
		}
		spec.Containers[i].VolumeMounts = append(spec.Containers[i].VolumeMounts, mount)
		spec.Containers[i].Env = append(spec.Containers[i].Env, env)
		spec.Volumes = append(spec.Volumes, vol)
		return
	}
}
