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

// forge_credentials.go — how a self-managed forge reaches the credential proxy.
//
// A GitHub token is minted per repository by the token minter. A self-managed
// forge's token is one an administrator stored in a Secret, named by the
// forge's credentialsRef. The operator mounts that Secret into the credential
// proxy's Pod and nowhere else, and tells the proxy which forges exist and
// where each token file is through one environment variable,
// CREDENTIAL_PROXY_FORGES, which agents/platform/scripts/providers/registry.py
// reads. The agent's Pod never mounts these volumes, and
// validateExtraVolumeMounts refuses a CR that tries to.

import (
	"encoding/json"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// credentialProxyForgesEnv carries the declared forges to the proxy as a
	// JSON list.
	credentialProxyForgesEnv = "CREDENTIAL_PROXY_FORGES"
	// forgeTokenMountRoot holds one directory per self-managed forge, named
	// for the forge, each holding the token file.
	forgeTokenMountRoot = "/var/run/secrets/kubeagents/forges" // #nosec G101 -- Mount path, not a credential
	// forgeTokenFileName is the file the Secret's token key is projected to.
	forgeTokenFileName = "token" // #nosec G101 -- File name, not a credential
	// forgeTokenVolumePrefix starts the name of every forge token volume. The
	// suffix is the forge's position in spec.integration.forges, since a
	// forge name of up to 63 characters would overflow a volume name.
	forgeTokenVolumePrefix = agentv1alpha1.ForgeCredentialVolumePrefix // #nosec G101 -- Volume name prefix, not a credential
	// forgeTokenFileMode is owner-read only; the proxy Pod's fsGroup is what
	// makes it readable to the proxy, as for the other projected tokens.
	forgeTokenFileMode = int32(0400)
)

// forgeDeclaration is one entry of CREDENTIAL_PROXY_FORGES.
type forgeDeclaration struct {
	Name      string `json:"name"`
	Provider  string `json:"provider"`
	Host      string `json:"host"`
	Scheme    string `json:"scheme"`
	Port      int32  `json:"port,omitempty"`
	TokenFile string `json:"tokenFile,omitempty"`
}

// forgeCredential is one self-managed forge's mounted token.
type forgeCredential struct {
	forge  *agentv1alpha1.ResolvedForge
	volume string
	dir    string
}

func (c forgeCredential) tokenFile() string {
	return c.dir + "/" + forgeTokenFileName
}

// selfManagedForgeCredentials lists the valid self-managed forges the CR
// declares, each with the volume and directory its token is mounted at. A
// forge validation refuses is left out: its problem is reported on the CR,
// and mounting a Secret for a forge the agent will not build serves nothing.
func selfManagedForgeCredentials(agent *agentv1alpha1.PlatformAgent) []forgeCredential {
	if agent == nil || agent.Spec.Integration == nil {
		return nil
	}
	resolved, err := agent.Spec.Integration.ResolveGit()
	if err != nil || resolved == nil {
		return nil
	}
	var out []forgeCredential
	for _, f := range resolved.SelfManagedForges() {
		out = append(out, forgeCredential{
			forge:  f,
			volume: fmt.Sprintf("%s%d", forgeTokenVolumePrefix, f.Index),
			dir:    forgeTokenMountRoot + "/" + f.Name,
		})
	}
	return out
}

// buildForgeDeclarationsEnv renders CREDENTIAL_PROXY_FORGES, or nothing when
// no self-managed forge is declared, so an install without one renders the
// same Pod it did before. When a valid GitHub forge is also declared alongside
// a self-managed forge, it is included in CREDENTIAL_PROXY_FORGES so the
// broker's registry builds both GitHubForge and the self-managed forge(s).
func buildForgeDeclarationsEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	credentials := selfManagedForgeCredentials(agent)
	if len(credentials) == 0 {
		return nil
	}
	declarations := make([]forgeDeclaration, 0, len(credentials)+1)
	if resolved, err := agent.Spec.Integration.ResolveGit(); err == nil && resolved != nil {
		if gh := resolved.PrimaryForge(agentv1alpha1.GitProviderGitHub); gh != nil {
			declarations = append(declarations, forgeDeclaration{
				Name:     gh.Name,
				Provider: agentv1alpha1.GitProviderGitHub,
				Host:     "github.com",
				Scheme:   agentv1alpha1.DefaultForgeScheme,
			})
		}
	}
	for _, c := range credentials {
		declarations = append(declarations, forgeDeclaration{
			Name:      c.forge.Name,
			Provider:  c.forge.Provider,
			Host:      strings.ToLower(c.forge.Host),
			Scheme:    c.forge.EffectiveScheme(),
			Port:      c.forge.Port,
			TokenFile: c.tokenFile(),
		})
	}
	raw, err := json.Marshal(declarations)
	if err != nil {
		// A slice of flat string and integer structs always marshals.
		return nil
	}
	return []corev1.EnvVar{{Name: credentialProxyForgesEnv, Value: string(raw)}}
}

// buildForgeCredentialVolumes projects each self-managed forge's Secret, the
// token key only. The Secret is optional: a missing one leaves that forge's
// calls failing as unauthenticated, rather than holding back the whole
// credential proxy and every other forge with it.
func buildForgeCredentialVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	var volumes []corev1.Volume
	for _, c := range selfManagedForgeCredentials(agent) {
		volumes = append(volumes, corev1.Volume{
			Name: c.volume,
			VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
				DefaultMode: ptr.To(forgeTokenFileMode),
				Sources: []corev1.VolumeProjection{{Secret: &corev1.SecretProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: c.forge.CredentialsSecret},
					Items:                []corev1.KeyToPath{{Key: agentv1alpha1.ForgeTokenSecretKey, Path: forgeTokenFileName}},
					Optional:             ptr.To(true),
				}}},
			}},
		})
	}
	return volumes
}

// buildForgeCredentialMounts mounts each forge token volume read-only at the
// directory CREDENTIAL_PROXY_FORGES names. A directory mount rather than a
// SubPath one, so a rotated Secret reaches the file without a restart.
func buildForgeCredentialMounts(agent *agentv1alpha1.PlatformAgent) []corev1.VolumeMount {
	var mounts []corev1.VolumeMount
	for _, c := range selfManagedForgeCredentials(agent) {
		mounts = append(mounts, corev1.VolumeMount{Name: c.volume, MountPath: c.dir, ReadOnly: true})
	}
	return mounts
}

// isForgeCredentialVolume reports whether a volume name is one of the forge
// token volumes, which the agent container must never mount.
func isForgeCredentialVolume(name string) bool {
	return agentv1alpha1.IsForgeCredentialVolumeName(name)
}
