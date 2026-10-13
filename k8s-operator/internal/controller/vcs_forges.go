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

// vcs_forges.go — the forges the credential broker serves beyond GitHub.
//
// GitHub needs nothing from here: its credential is minted per repository by
// the token minter, and the broker builds it when it is handed no forge
// configuration. A forge whose credential an administrator supplies (GitLab)
// needs two things the broker cannot find for itself: a configuration naming
// the forge, its host, the groups it may serve and where its token is, and
// the token. Both reach the broker's pod and no other: the token is a Secret
// mounted only there, and the configuration rides in the broker's policy
// ConfigMap, whose hash already rolls the broker when it changes.
//
// A GitHub-only install renders none of it, so its broker is byte-for-byte
// what it was.

import (
	"encoding/json"
	"fmt"
	"path"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// vcsForgesKey is the policy ConfigMap key, and the basename the broker
	// reads it under.
	vcsForgesKey = "vcs-forges.json"
	// vcsForgesMountPath is VCS_FORGES_CONFIG: providers.registry reads the
	// file the variable names.
	vcsForgesMountPath = "/etc/credential-proxy/" + vcsForgesKey
	vcsForgesEnv       = "VCS_FORGES_CONFIG"
	// forgeCredentialsDir holds one directory per credentialed forge, named
	// for the forge, with the token in it.
	forgeCredentialsDir = "/var/run/kube-agents/forge-credentials" // #nosec G101 -- Mount path, not a credential
	// forgeCredentialsVolumePrefix names each forge's Secret volume, by its
	// position in the rendered configuration: a forge name can be 63
	// characters, which is a volume name's whole budget.
	forgeCredentialsVolumePrefix = "vcs-forge-credentials-" // #nosec G101 -- Volume name prefix, not a credential
)

// brokerForges is the forge configuration the declaration hands the broker,
// or nil. A declaration that does not resolve hands it none, which leaves the
// broker serving GitHub as before; the reconcile status reports why.
func brokerForges(agent *agentv1alpha1.PlatformAgent) []agentv1alpha1.BrokerForge {
	if agent.Spec.Integration == nil {
		return nil
	}
	resolved, err := agent.Spec.Integration.ResolveGit()
	if err != nil {
		return nil
	}
	return resolved.BrokerForges(forgeCredentialsDir)
}

// vcsForgesJSON renders the configuration, or "" when there is none. No error
// return, for the reason scopedSAPoolJSON gives: the document is strings, which
// json.Marshal cannot fail on.
func vcsForgesJSON(agent *agentv1alpha1.PlatformAgent) string {
	forges := brokerForges(agent)
	if forges == nil {
		return ""
	}
	document, _ := json.Marshal(struct {
		Forges []agentv1alpha1.BrokerForge `json:"forges"`
	}{Forges: forges})
	return string(document)
}

// buildVCSForgesEnv points the broker at the configuration. Unset when there
// is none, which the broker reads as "GitHub only".
func buildVCSForgesEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	if vcsForgesJSON(agent) == "" {
		return nil
	}
	return []corev1.EnvVar{{Name: vcsForgesEnv, Value: vcsForgesMountPath}}
}

// buildVCSForgesVolumeMounts mounts the configuration and each forge's token.
//
// The configuration is a SubPath mount of the policy ConfigMap, so it appears
// and disappears with the key, as the scoped-SA pool's does: naming a key the
// ConfigMap does not carry leaves the container unable to start.
func buildVCSForgesVolumeMounts(agent *agentv1alpha1.PlatformAgent) []corev1.VolumeMount {
	forges := brokerForges(agent)
	if forges == nil {
		return nil
	}
	mounts := []corev1.VolumeMount{{
		Name: "credential-proxy-policy", MountPath: vcsForgesMountPath, SubPath: vcsForgesKey, ReadOnly: true,
	}}
	for i, forge := range forges {
		if forge.CredentialsSecret == "" {
			continue
		}
		mounts = append(mounts, corev1.VolumeMount{
			Name:      fmt.Sprintf("%s%d", forgeCredentialsVolumePrefix, i),
			MountPath: path.Join(forgeCredentialsDir, forge.Name),
			ReadOnly:  true,
		})
	}
	return mounts
}

// buildVCSForgesVolumes projects each forge's token out of its Secret.
//
// Only the `token` key, so nothing else an administrator keeps in the Secret
// reaches the pod. 0400, readable through the pod's fsGroup as the other
// projections are. Optional, deliberately: a missing Secret must not keep the
// broker -- and with it GitHub, chat and every brokered command -- from
// starting. The broker reads the token on every call and answers a missing
// file with FORGE_CREDENTIAL_UNAVAILABLE naming the host, and kubelet fills
// the file in once the Secret exists, with no restart.
func buildVCSForgesVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	var volumes []corev1.Volume
	for i, forge := range brokerForges(agent) {
		if forge.CredentialsSecret == "" {
			continue
		}
		volumes = append(volumes, corev1.Volume{
			Name: fmt.Sprintf("%s%d", forgeCredentialsVolumePrefix, i),
			VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{
				SecretName: forge.CredentialsSecret,
				Items: []corev1.KeyToPath{{
					Key:  agentv1alpha1.ForgeCredentialsTokenKey,
					Path: agentv1alpha1.ForgeCredentialsTokenKey,
				}},
				DefaultMode: ptr.To(int32(0o400)),
				Optional:    ptr.To(true),
			}},
		})
	}
	return volumes
}

// gatewayPolicyView is the policy ConfigMap as the gateway pod's hash
// annotation reads it: without the forge configuration, which only the broker
// mounts. Without the key -- every GitHub-only install -- it is the ConfigMap
// itself, so that annotation is what it always was.
func gatewayPolicyView(cm *corev1.ConfigMap) *corev1.ConfigMap {
	if _, ok := cm.Data[vcsForgesKey]; !ok {
		return cm
	}
	view := cm.DeepCopy()
	delete(view.Data, vcsForgesKey)
	return view
}
