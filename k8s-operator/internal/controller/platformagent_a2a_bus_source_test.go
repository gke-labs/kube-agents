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
	"slices"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The source half of the bus-token reservation at render, beside
// TestUserAuthoredContainersCannotMountTheBusTokenByName, which is the name
// half and says in its own name what it does not cover.
//
// Literals for the audience and the Secret name rather than the constants, so
// these tests compile against a tree without the fix and fail there on the
// rendered pod rather than on a missing symbol. The fixture agent is
// a2aTestAgent (name test-agent), so the credentials Secret the operator
// renders for it is test-agent-a2a-nats-creds.

// busSourceFixtureNames are the user volumes the fixtures below declare. The
// two "stolen" entries carry the bus credential by source; the two "kept"
// entries are the same shapes with an innocent source and must survive, or
// the strip is taking volumes it has no claim on.
const (
	busSourceStolenProjection = "innocuous-cache"  // projected serviceAccountToken, audience a2a-bus
	busSourceStolenSecret     = "innocuous-config" // secret volume naming the creds Secret
	busSourceStolenBundle     = "innocuous-bundle" // projected secret source naming the creds Secret
	busSourceKeptProjection   = "vault-token"      // projected serviceAccountToken, audience vault
	busSourceKeptSecret       = "my-tls"           // secret volume naming another Secret
	busSourceCredsSecretName  = "test-agent-a2a-nats-creds"
)

func busSourceTokenProjection(audience string) corev1.VolumeSource {
	return corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
		ServiceAccountToken: &corev1.ServiceAccountTokenProjection{Audience: audience, Path: "token"},
	}}}}
}

func busSourceSecretVolume(name string) corev1.VolumeSource {
	return corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: name}}
}

func busSourceSecretProjection(name string) corev1.VolumeSource {
	return corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
		Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: name}},
	}}}}
}

// busSourceDeploymentSpec spreads the stolen and kept volumes over both CR
// volume lists and mounts each from every user-authored mount surface: a
// sidecar, an init container, and extraVolumeMounts (which reaches the agent
// container and the dashboard).
func busSourceDeploymentSpec() *agentv1alpha1.DeploymentSpec {
	mount := func(name string) corev1.VolumeMount {
		return corev1.VolumeMount{Name: name, MountPath: "/mnt/" + name, ReadOnly: true}
	}
	all := []corev1.VolumeMount{
		mount(busSourceStolenProjection), mount(busSourceStolenSecret), mount(busSourceStolenBundle),
		mount(busSourceKeptProjection), mount(busSourceKeptSecret),
	}
	return &agentv1alpha1.DeploymentSpec{
		Sidecars: []corev1.Container{{
			Name: "hermes-bridge", Image: "bridge:dev",
			VolumeMounts: append([]corev1.VolumeMount{{Name: "agent-data", MountPath: "/opt/data"}}, all...),
		}},
		InitContainers:    []corev1.Container{{Name: "peek", Image: "busybox", VolumeMounts: slices.Clone(all)}},
		ExtraVolumeMounts: slices.Clone(all),
		SidecarVolumes: []corev1.Volume{
			{Name: busSourceStolenProjection, VolumeSource: busSourceTokenProjection("a2a-bus")},
			{Name: busSourceKeptProjection, VolumeSource: busSourceTokenProjection("vault")},
			{Name: busSourceStolenBundle, VolumeSource: busSourceSecretProjection(busSourceCredsSecretName)},
		},
		ExtraVolumes: []corev1.Volume{
			{Name: busSourceStolenSecret, VolumeSource: busSourceSecretVolume(busSourceCredsSecretName)},
			{Name: busSourceKeptSecret, VolumeSource: busSourceSecretVolume("my-tls")},
		},
	}
}

// busSourceHolders maps a volume name to the containers (init and regular)
// that mount it.
func busSourceHolders(spec corev1.PodSpec) map[string][]string {
	holders := map[string][]string{}
	for _, c := range slices.Concat(spec.InitContainers, spec.Containers) {
		for _, m := range c.VolumeMounts {
			holders[m.Name] = append(holders[m.Name], c.Name)
		}
	}
	return holders
}

// TestUserAuthoredVolumesCannotCarryTheBusCredentialBySource is the render
// half of gke-labs#1667. A user volume that projects the bus audience under a
// name of the author's choosing, or that mounts the credentials Secret, stays
// out of the pod together with every mount that named it, on every surface a
// mount can come from. The important control is the operator's own
// projection: it is appended to the pod outside the user lists and must be
// untouched, still a projected token, still mounted by platform-agent and by
// nothing else.
func TestUserAuthoredVolumesCannotCarryTheBusCredentialBySource(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Deployment = busSourceDeploymentSpec()
	pt := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
	spec := pt.Spec

	// Precondition: the surface is up and the operator's projection is in the
	// pod, so a missing user volume below means the strip ran and not that
	// the feature is off.
	own := podVolume(pt, a2aBusTokenVolume)
	if own == nil {
		t.Fatalf("the pod carries no %s volume; nothing here measures a strip", a2aBusTokenVolume)
	}
	if own.Projected == nil || len(own.Projected.Sources) != 1 || own.Projected.Sources[0].ServiceAccountToken == nil ||
		own.Projected.Sources[0].ServiceAccountToken.Audience != "a2a-bus" {
		t.Errorf("the operator's own %s volume is not the single a2a-bus token projection: %+v; the source strip "+
			"reached the volume it exists to protect", a2aBusTokenVolume, own)
	}

	holders := busSourceHolders(spec)
	if got := holders[a2aBusTokenVolume]; len(got) != 1 || got[0] != "platform-agent" {
		t.Errorf("%s is mounted by %v, want exactly [platform-agent]; the strip either took the operator's mount "+
			"or left a second holder", a2aBusTokenVolume, got)
	}

	for _, name := range []string{busSourceStolenProjection, busSourceStolenSecret, busSourceStolenBundle} {
		if v := podVolume(pt, name); v != nil {
			t.Errorf("user volume %q reached the pod: %+v. Under a different name it is the bus credential all the "+
				"same -- the callout resolves the pod's ServiceAccount and bridge-password is a static bus user", name, v)
		}
		if got := holders[name]; len(got) != 0 {
			t.Errorf("containers %v still mount %q; the volume is gone, so this is a Deployment the API server "+
				"refuses and a reconcile that wedges with nothing in status", got, name)
		}
	}

	// Not too wide: the same shapes with an innocent source survive, and they
	// reach every container the author sent them to.
	wantHolders := []string{"peek", "platform-agent", "platform-agent-dashboard", "hermes-bridge"}
	for _, name := range []string{busSourceKeptProjection, busSourceKeptSecret} {
		if podVolume(pt, name) == nil {
			t.Errorf("the pod lost user volume %q; the strip is taking volumes it has no claim on", name)
		}
		got := holders[name]
		for _, want := range wantHolders {
			if !slices.Contains(got, want) {
				t.Errorf("%q is mounted by %v, want %s among them; the mount strip is keyed wider than the dropped set", name, got, want)
			}
		}
	}
	for _, c := range spec.Containers {
		if c.Name == "hermes-bridge" && !slices.ContainsFunc(c.VolumeMounts, func(m corev1.VolumeMount) bool { return m.Name == "agent-data" }) {
			t.Errorf("the sidecar lost its agent-data mount: %+v", c.VolumeMounts)
		}
	}

	// The CR's own slices are the manager's cached copy and must not have
	// been edited in place.
	if n := len(agent.Spec.Deployment.Sidecars[0].VolumeMounts); n != 6 {
		t.Errorf("the CR's sidecar has %d mounts after the render, want 6; the strip edited the cached CR", n)
	}
	if n := len(agent.Spec.Deployment.SidecarVolumes) + len(agent.Spec.Deployment.ExtraVolumes); n != 5 {
		t.Errorf("the CR has %d user volumes after the render, want 5; the strip edited the cached CR", n)
	}

	// Gated on the surface, like the name half: a today install has no bus,
	// so a token for its audience and a Secret that does not exist are the
	// author's business, and dropping them would be one more way to tell the
	// next stack exists.
	today := a2aTestAgent()
	today.Spec.Mode = ptr.To("today")
	today.Spec.Deployment = busSourceDeploymentSpec()
	todayPT := buildPodTemplateSpec(today, "", "", "", "", nil, renderOptions{})
	for _, name := range []string{busSourceStolenProjection, busSourceStolenSecret, busSourceStolenBundle} {
		if podVolume(todayPT, name) == nil {
			t.Errorf("a today install lost user volume %q; the source strip is not gated on the surface", name)
		}
	}
}
