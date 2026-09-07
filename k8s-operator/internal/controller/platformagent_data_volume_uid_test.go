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
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// #1244 and #1144: a credential-proxy sidecar under a second uid in the agent
// pod could not traverse a 0700 profile home under /opt/data/profiles that the
// agent had created, so a proxied kubectl returned "kubeconfig is unreadable".
// #913 removed both halves — the credential runtime is a pod of its own and no
// longer opens the file — and this is what stops the split coming back
// unnoticed: every pod the operator renders that mounts the agent's data claim
// runs its containers as one uid, so the mode a home is created with never
// decides whether another process can read it.

const (
	// The claim buildPVC lays down and buildDefaultVolumes mounts.
	agentDataClaimSuffix = "-data"
	// The workload the walk must reach, or it proves nothing: the gateway is
	// the pod that mounts the claim today.
	gatewayWorkloadSuffix = "-gateway"
)

// renderedPodTemplate is one pod template with the workload it came from, so a
// failure names something a reader can go and look at.
type renderedPodTemplate struct {
	workload string
	spec     corev1.PodSpec
}

// renderEveryWorkloadPod renders every pod-bearing workload the operator builds
// for one CR.
//
// The builders are called directly rather than through the reconciler's gates,
// so a workload an install would not create today — the shell sandbox is
// experimental, the A2A pods are dark outside mode next — is still walked. What
// is being asserted is what the builder would render if the gate opened.
func renderEveryWorkloadPod(agent *agentv1alpha1.PlatformAgent) []renderedPodTemplate {
	opts := renderOptions{imageVolumeSupported: true}
	var pods []renderedPodTemplate

	// The gateway comes in one shape or the other, never both, and
	// useStatefulSet is the reconciler's own choice between them.
	if useStatefulSet(agent) {
		gateway := buildStatefulSet(agent, "c", "f", "s", "p", nil, opts)
		pods = append(pods, renderedPodTemplate{gateway.Name, gateway.Spec.Template.Spec})
	} else {
		gateway := buildDeployment(agent, "c", "f", "s", "p", nil, opts)
		pods = append(pods, renderedPodTemplate{gateway.Name, gateway.Spec.Template.Spec})
	}

	for _, deployment := range []*appsv1.Deployment{
		buildCredentialProxyDeployment(agent, "p"),
		buildA2AGatewayDeployment(agent),
	} {
		pods = append(pods, renderedPodTemplate{deployment.Name, deployment.Spec.Template.Spec})
	}
	for _, statefulSet := range []*appsv1.StatefulSet{
		buildShellSandboxStatefulSet(agent, agent.Name+"-sandbox-keys", "http://credential-proxy:8080", "s"),
		buildA2ANATSStatefulSet(agent, "c"),
	} {
		pods = append(pods, renderedPodTemplate{statefulSet.Name, statefulSet.Spec.Template.Spec})
	}
	job := buildA2AProvisionJob(agent)
	return append(pods, renderedPodTemplate{job.Name, job.Spec.Template.Spec})
}

// volumesBackedByClaim names the pod's volumes that resolve to claimName.
//
// A claim reference is the only volume source looked at, because it is the only
// one the agent's data volume uses: buildDefaultVolumes names the claim buildPVC
// created. The sandbox and NATS StatefulSets reach their own storage the other
// way, through a volumeClaimTemplate, and the claim the StatefulSet controller
// derives from one is `<template>-<statefulset>-<ordinal>` — never a claim the
// operator named itself.
func volumesBackedByClaim(spec corev1.PodSpec, claimName string) map[string]bool {
	names := map[string]bool{}
	for _, volume := range spec.Volumes {
		if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ClaimName == claimName {
			names[volume.Name] = true
		}
	}
	return names
}

// effectiveRunAsUser is the uid the kubelet gives a container: its own override
// where it has one, the pod default otherwise. nil means neither says, which
// leaves the image to decide and is a failure here rather than a pass.
func effectiveRunAsUser(spec corev1.PodSpec, container corev1.Container) *int64 {
	if container.SecurityContext != nil && container.SecurityContext.RunAsUser != nil {
		return container.SecurityContext.RunAsUser
	}
	if spec.SecurityContext != nil {
		return spec.SecurityContext.RunAsUser
	}
	return nil
}

func dataVolumeTestAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
}

// dataVolumeStatefulSetAgent is the other shape of the gateway workload: RWO
// custom storage at more than one replica, which is what useStatefulSet keys
// on, plus mode next and the shell sandbox so the walk renders every pod.
func dataVolumeStatefulSetAgent() *agentv1alpha1.PlatformAgent {
	agent := dataVolumeTestAgent()
	agent.Spec.Mode = ptr.To(string(ModeNext))
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(true)},
		},
	}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))},
		Storages: []agentv1alpha1.StorageSpec{{
			Name:        "scratch",
			AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			StorageSize: "5Gi",
			MountPath:   "/scratch",
		}},
	}
	return agent
}

// TestEveryPodMountingTheAgentDataClaimRunsAsOneUID walks the rendered
// workloads and holds the agent's data volume to a single uid.
//
// Driven by the volume rather than by one pod template, which is what reaches
// the pods that must *not* mount it — the shell sandbox at uid 1000, the A2A
// pods at uid 1000, the credential runtime in its own pod. Any of them
// acquiring the claim without also acquiring uid 10000 fails here.
func TestEveryPodMountingTheAgentDataClaimRunsAsOneUID(t *testing.T) {
	shapes := []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
	}{
		{"default", dataVolumeTestAgent()},
		{"statefulset shape, mode next", dataVolumeStatefulSetAgent()},
	}

	for _, shape := range shapes {
		t.Run(shape.name, func(t *testing.T) {
			claim := shape.agent.Name + agentDataClaimSuffix
			gatewayReached := false
			mountingContainers := 0

			for _, pod := range renderEveryWorkloadPod(shape.agent) {
				volumes := volumesBackedByClaim(pod.spec, claim)
				if len(volumes) == 0 {
					continue
				}
				if pod.workload == shape.agent.Name+gatewayWorkloadSuffix {
					gatewayReached = true
				}

				// The fsGroup is what makes the claim's contents reachable to
				// the group at all; without it the volume arrives owned by
				// whichever uid wrote it and root's group.
				podSC := pod.spec.SecurityContext
				if podSC == nil || podSC.FSGroup == nil || *podSC.FSGroup != agentFSGroup {
					t.Errorf("%s mounts %s but does not carry fsGroup %d: %#v", pod.workload, claim, agentFSGroup, podSC)
				}

				// Init containers included: a native sidecar lives in
				// InitContainers, so a walk of Containers alone never sees one.
				all := append(append([]corev1.Container{}, pod.spec.InitContainers...), pod.spec.Containers...)
				for _, container := range all {
					mountsClaim := false
					for _, mount := range container.VolumeMounts {
						if volumes[mount.Name] {
							mountsClaim = true
							break
						}
					}
					if !mountsClaim {
						continue
					}
					mountingContainers++

					user := effectiveRunAsUser(pod.spec, container)
					if user == nil {
						t.Errorf("container %s in %s mounts %s with no runAsUser at either level, so the image picks the uid",
							container.Name, pod.workload, claim)
						continue
					}
					if *user != sandboxUID {
						t.Errorf("container %s in %s mounts %s as uid %d; a second uid on this claim is #1244, which needs %d",
							container.Name, pod.workload, claim, *user, sandboxUID)
					}
				}
			}

			if !gatewayReached {
				t.Errorf("the walk never found the gateway pod mounting %s; either the claim was renamed or this assertion no longer reaches the pod it is about", claim)
			}
			if mountingContainers == 0 {
				t.Errorf("no container mounted %s, so this walk asserted nothing", claim)
			}
		})
	}
}

// TestTheStatefulSetShapeIsTheOneTheOperatorWouldPick keeps the two CR shapes
// above apart. useStatefulSet is what picks the gateway workload, in the
// reconciler and in the walk alike, so two shapes it answers the same way leave
// one of buildDeployment and buildStatefulSet unwalked without failing.
func TestTheStatefulSetShapeIsTheOneTheOperatorWouldPick(t *testing.T) {
	if !useStatefulSet(dataVolumeStatefulSetAgent()) {
		t.Error("dataVolumeStatefulSetAgent no longer selects the StatefulSet workload shape")
	}
	if useStatefulSet(dataVolumeTestAgent()) {
		t.Error("the default CR now selects the StatefulSet workload shape, so the two shapes above are one")
	}
}
