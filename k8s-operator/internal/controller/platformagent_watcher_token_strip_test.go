// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"sort"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func watcherAudienceVolume(name string) corev1.Volume {
	return corev1.Volume{Name: name, VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
		Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
			Audience: credentialProxyWatcherAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
		}}},
	}}}
}

// TestNoCRSurfaceHandsAContainerTheWatcherToken covers all five user-authored
// mount surfaces, by name and by source: a sidecar and an init container
// mounting the reserved volume; a sidecar volume and an extra volume that
// project the watcher audience under other names, each mounted (the second
// through extraVolumeMounts, into platform-agent). In the rendered pod the only
// container mounting a watcher-audience volume is agent-api-auth, and an
// ordinary user volume and its mounts survive.
func TestNoCRSurfaceHandsAContainerTheWatcherToken(t *testing.T) {
	agent := federatedTestAgent()
	ordinary := corev1.Volume{Name: "scratch", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Sidecars: []corev1.Container{{
			Name: "helper", Image: "helper:1",
			VolumeMounts: []corev1.VolumeMount{
				{Name: watcherTokenVolume, MountPath: "/w"},
				{Name: "sneaky-sidecar", MountPath: "/s"},
				{Name: "scratch", MountPath: "/scratch"},
			},
		}},
		InitContainers: []corev1.Container{{
			Name: "prep", Image: "prep:1",
			VolumeMounts: []corev1.VolumeMount{{Name: watcherTokenVolume, MountPath: "/w"}},
		}},
		SidecarVolumes:    []corev1.Volume{watcherAudienceVolume("sneaky-sidecar"), ordinary},
		ExtraVolumes:      []corev1.Volume{watcherAudienceVolume("sneaky-extra")},
		ExtraVolumeMounts: []corev1.VolumeMount{{Name: "sneaky-extra", MountPath: "/e"}},
	}
	spec := buildPodTemplateSpec(agent, "h", "h", "h", "h", nil, renderOptions{}).Spec

	watcherVolumes := map[string]bool{}
	names := map[string]bool{}
	for _, v := range spec.Volumes {
		names[v.Name] = true
		if v.Name == watcherTokenVolume || projectsWatcherToken(v) {
			watcherVolumes[v.Name] = true
		}
	}
	if len(watcherVolumes) != 1 || !watcherVolumes[watcherTokenVolume] {
		t.Errorf("watcher-audience volumes in the pod = %v, want the operator's %s alone", watcherVolumes, watcherTokenVolume)
	}
	var holders []string
	scratchMounted := false
	for _, c := range append(append([]corev1.Container{}, spec.InitContainers...), spec.Containers...) {
		for _, m := range c.VolumeMounts {
			if watcherVolumes[m.Name] {
				holders = append(holders, c.Name)
			}
			if c.Name == "helper" && m.Name == "scratch" {
				scratchMounted = true
			}
			if !names[m.Name] {
				t.Errorf("%s mounts %q, which is not a pod volume", c.Name, m.Name)
			}
		}
	}
	sort.Strings(holders)
	if len(holders) != 1 || holders[0] != "agent-api-auth" {
		t.Errorf("containers mounting the watcher token = %v, want [agent-api-auth]", holders)
	}
	if !names["scratch"] || !scratchMounted {
		t.Errorf("an ordinary user volume and its mount must survive the strip")
	}
}
