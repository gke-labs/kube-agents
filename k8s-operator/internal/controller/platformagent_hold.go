/*
Copyright 2026 Google LLC.

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
	"path"
	"sort"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// spec.hold rides in a ConfigMap of its own, never in <agent>-config:
// that one is in the agent pod's config-hash, and a hold must take effect
// without rolling anything. No pod template hashes this ConfigMap; kube-proxy
// and the credential broker mount it as a directory (no subPath, so the
// kubelet swaps the file in place) and stat it on every request.
const (
	holdConfigMapSuffix = "-hold"
	holdKey             = "hold.json"
	holdVolumeName      = "platform-agent-hold-vol"
	// holdDir sits beside brokerScopeDir, not inside it: the runtime
	// cannot create a mount point inside a read-only ConfigMap volume.
	holdDir = "/etc/kube-agents/hold"

	kubeProxyHoldFileEnvVar       = "KUBE_PROXY_HOLD_FILE"
	credentialProxyHoldFileEnvVar = "CREDENTIAL_PROXY_HOLD_FILE" // #nosec G101 -- Environment variable name, not a credential

	holdFileMode int32 = 0444
)

// holdDeclaration is hold.json: the CRD's own keys, so kube-proxy and
// the broker read the field as an operator wrote it. An empty hold renders
// "{}".
type holdDeclaration struct {
	All      bool                            `json:"all,omitempty"`
	Clusters []agentv1alpha1.ScopeClusterRef `json:"clusters,omitempty"`
	Reason   string                          `json:"reason,omitempty"`
}

// renderHoldJSON renders spec.hold, clusters sorted so an
// unchanged hold renders the same bytes.
func renderHoldJSON(agent *agentv1alpha1.PlatformAgent) string {
	decl := holdDeclaration{}
	if hold := agent.Spec.Hold; hold != nil {
		decl.All = hold.All
		decl.Reason = hold.Reason
		decl.Clusters = append(decl.Clusters, hold.Clusters...)
	}
	sort.Slice(decl.Clusters, func(i, j int) bool {
		a, b := decl.Clusters[i], decl.Clusters[j]
		if a.ProjectID != b.ProjectID {
			return a.ProjectID < b.ProjectID
		}
		if a.Location != b.Location {
			return a.Location < b.Location
		}
		return a.ClusterName < b.ClusterName
	})
	out, err := json.Marshal(decl)
	if err != nil {
		// A bool, a string and a list of string triples cannot fail to
		// marshal. If they ever do, an unparseable file is the safe render:
		// both readers refuse every cluster on it (fail-closed).
		manifestsLog.Error(err, "rendering spec.hold failed; rendering an unreadable hold")
		return ""
	}
	return string(out) + "\n"
}

func holdConfigMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + holdConfigMapSuffix
}

// buildHoldConfigMap is rendered on every install, "{}" when nothing is
// held, so the readers never meet a missing file on an operator that knows
// the field.
func buildHoldConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      holdConfigMapName(agent),
			Namespace: agent.Namespace,
		},
		Data: map[string]string{holdKey: renderHoldJSON(agent)},
	}
}

func (r *PlatformAgentReconciler) reconcileHoldConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	cm := buildHoldConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, cm)
}

// holdVolume projects hold.json at holdDir. Optional, so a pod
// that starts before the operator first writes the ConfigMap mounts an empty
// directory; the readers refuse every cluster on a configured file they
// cannot read until the kubelet fills it in.
func holdVolume(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: holdVolumeName,
		VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: holdConfigMapName(agent)},
			Items:                []corev1.KeyToPath{{Key: holdKey, Path: holdKey}},
			Optional:             ptr.To(true),
			DefaultMode:          ptr.To(holdFileMode),
		}},
	}
}

func holdMount() corev1.VolumeMount {
	return corev1.VolumeMount{Name: holdVolumeName, MountPath: holdDir, ReadOnly: true}
}

func holdFilePath() string {
	return path.Join(holdDir, holdKey)
}
