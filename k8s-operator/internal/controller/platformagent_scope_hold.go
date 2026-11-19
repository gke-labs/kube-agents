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

// spec.scope.hold rides in a ConfigMap of its own, never in <agent>-config:
// that one is in the agent pod's config-hash, and a hold must take effect
// without rolling anything. No pod template hashes this ConfigMap; kube-proxy
// and the credential broker mount it as a directory (no subPath, so the
// kubelet swaps the file in place) and stat it on every request.
const (
	scopeHoldConfigMapSuffix = "-scope-hold"
	scopeHoldKey             = "hold.json"
	scopeHoldVolumeName      = "platform-agent-scope-hold-vol"
	// scopeHoldDir sits beside brokerScopeDir, not inside it: the runtime
	// cannot create a mount point inside a read-only ConfigMap volume.
	scopeHoldDir = "/etc/kube-agents/hold"

	kubeProxyHoldFileEnvVar       = "KUBE_PROXY_HOLD_FILE"
	credentialProxyHoldFileEnvVar = "CREDENTIAL_PROXY_HOLD_FILE" // #nosec G101 -- Environment variable name, not a credential

	scopeHoldFileMode int32 = 0444
)

// scopeHoldDeclaration is hold.json: the CRD's own keys, so kube-proxy and
// the broker read the field as an operator wrote it. An empty hold renders
// "{}".
type scopeHoldDeclaration struct {
	All      bool                            `json:"all,omitempty"`
	Clusters []agentv1alpha1.ScopeClusterRef `json:"clusters,omitempty"`
	Reason   string                          `json:"reason,omitempty"`
}

// renderScopeHoldJSON renders spec.scope.hold, clusters sorted so an
// unchanged hold renders the same bytes.
func renderScopeHoldJSON(agent *agentv1alpha1.PlatformAgent) string {
	decl := scopeHoldDeclaration{}
	if scope := agent.Spec.Scope; scope != nil && scope.Hold != nil {
		decl.All = scope.Hold.All
		decl.Reason = scope.Hold.Reason
		decl.Clusters = append(decl.Clusters, scope.Hold.Clusters...)
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
		manifestsLog.Error(err, "rendering spec.scope.hold failed; rendering an unreadable hold")
		return ""
	}
	return string(out) + "\n"
}

func scopeHoldConfigMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + scopeHoldConfigMapSuffix
}

// buildScopeHoldConfigMap is rendered on every install, "{}" when nothing is
// held, so the readers never meet a missing file on an operator that knows
// the field.
func buildScopeHoldConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      scopeHoldConfigMapName(agent),
			Namespace: agent.Namespace,
		},
		Data: map[string]string{scopeHoldKey: renderScopeHoldJSON(agent)},
	}
}

func (r *PlatformAgentReconciler) reconcileScopeHoldConfigMap(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	cm := buildScopeHoldConfigMap(agent)
	if err := ctrl.SetControllerReference(agent, cm, r.Scheme); err != nil {
		return err
	}
	return r.applyManaged(ctx, agent, cm)
}

// scopeHoldVolume projects hold.json at scopeHoldDir. Optional, so a pod
// that starts before the operator first writes the ConfigMap mounts an empty
// directory; the readers refuse every cluster on a configured file they
// cannot read until the kubelet fills it in.
func scopeHoldVolume(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: scopeHoldVolumeName,
		VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: scopeHoldConfigMapName(agent)},
			Items:                []corev1.KeyToPath{{Key: scopeHoldKey, Path: scopeHoldKey}},
			Optional:             ptr.To(true),
			DefaultMode:          ptr.To(scopeHoldFileMode),
		}},
	}
}

func scopeHoldMount() corev1.VolumeMount {
	return corev1.VolumeMount{Name: scopeHoldVolumeName, MountPath: scopeHoldDir, ReadOnly: true}
}

func scopeHoldFilePath() string {
	return path.Join(scopeHoldDir, scopeHoldKey)
}

// scopeForDeclaration is spec.scope as scope.json and the shell sandbox see
// it: without the hold, and nil for a block that carries nothing but a hold
// (and the cap the API server defaults onto every present block). A hold
// patched onto a CR with no scope block must not turn "no declaration" into
// "an empty declaration", which the reconcile reads as the one that drops
// projects, nor move the agent pod's config-hash.
func scopeForDeclaration(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.ScopeSpec {
	scope := agent.Spec.Scope
	if scope == nil || scope.Hold == nil {
		return scope
	}
	rest := scope.DeepCopy()
	rest.Hold = nil
	holdOnly := len(rest.Projects)+len(rest.Folders)+len(rest.Organizations)+len(rest.SharedVpcHosts)+len(rest.MetricsScopes) == 0 &&
		rest.Exclude == nil &&
		(rest.MaxProjects == nil || *rest.MaxProjects == defaultScopeMaxProjects)
	if holdOnly {
		return nil
	}
	return rest
}
