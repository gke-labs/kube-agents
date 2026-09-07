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
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strings"
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
// unnoticed: every container the operator itself puts on a pod mounting the
// agent's data claim runs as one uid, so the mode a home is created with never
// decides whether another process can read it.
//
// "the operator itself" is the limit, and it is a real one. A container the CR
// supplies under spec.deployment.sidecars or spec.deployment.initContainers is
// appended to the gateway pod with its securityContext untouched
// (buildPodTemplateSpec), and the data volume is pod-scoped. One that names no
// runAsUser inherits the pod default and is covered; one that sets its own
// mounts the claim under a second uid and reproduces #1244 on that install.
// Nothing in the operator refuses it, no fixture here renders it, and this file
// does not claim otherwise — refusing that field would be a behaviour change,
// and what #1244 settled on was a test rather than a change of behaviour.

const (
	// The claim buildPVC lays down and buildDefaultVolumes mounts.
	agentDataClaimSuffix = "-data"
	// The workload the walk must reach, or it proves nothing: the gateway is
	// the pod that mounts the claim today.
	gatewayWorkloadSuffix = "-gateway"
	// The walk itself, named for the source-level assertion below that every
	// pod-bearing builder in the package appears inside it.
	dataVolumeWalkFunc = "renderEveryWorkloadPod"
)

// renderedPodTemplate is one pod template with the builder and workload it came
// from, so a failure names something a reader can go and look at.
type renderedPodTemplate struct {
	builder  string
	workload string
	spec     corev1.PodSpec
}

// renderEveryWorkloadPod renders every pod-bearing workload the operator builds
// for one CR. TestTheWalkCallsEveryPodBearingBuilder is what keeps that "every"
// true as builders are added.
//
// The builders are called directly rather than through the reconciler's gates,
// so a workload an install would not create today — the shell sandbox is
// experimental, the A2A pods are dark outside mode next — is still walked. What
// is being asserted is what the builder would render if the gate opened. Both
// gateway shapes are rendered for the same reason: useStatefulSet picks one of
// them per CR, and the one it did not pick is still a pod this operator build
// can create.
func renderEveryWorkloadPod(agent *agentv1alpha1.PlatformAgent) []renderedPodTemplate {
	opts := renderOptions{imageVolumeSupported: true}
	gatewayDeployment := buildDeployment(agent, "c", "f", "s", "p", nil, opts)
	credentialProxy := buildCredentialProxyDeployment(agent, "p")
	a2aGateway := buildA2AGatewayDeployment(agent)
	gatewayStatefulSet := buildStatefulSet(agent, "c", "f", "s", "p", nil, opts)
	sandbox := buildShellSandboxStatefulSet(agent, agent.Name+"-sandbox-keys", "http://credential-proxy:8080", "s")
	nats := buildA2ANATSStatefulSet(agent, "c")
	provision := buildA2AProvisionJob(agent)

	pods := []renderedPodTemplate{
		{"buildDeployment", gatewayDeployment.Name, gatewayDeployment.Spec.Template.Spec},
		{"buildCredentialProxyDeployment", credentialProxy.Name, credentialProxy.Spec.Template.Spec},
		{"buildA2AGatewayDeployment", a2aGateway.Name, a2aGateway.Spec.Template.Spec},
		{"buildA2AProvisionJob", provision.Name, provision.Spec.Template.Spec},
	}
	for builder, statefulSet := range map[string]*appsv1.StatefulSet{
		"buildStatefulSet":             gatewayStatefulSet,
		"buildShellSandboxStatefulSet": sandbox,
		"buildA2ANATSStatefulSet":      nats,
	} {
		pods = append(pods, renderedPodTemplate{builder, statefulSet.Name, statefulSet.Spec.Template.Spec})
	}
	sort.Slice(pods, func(i, j int) bool { return pods[i].builder < pods[j].builder })
	return pods
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
// leaves the image to decide and is a failure wherever this is asserted on.
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

// dataVolumeEveryComponentAgent turns on what the default CR leaves off: mode
// next for the A2A pods, the shell sandbox, and a third claim on the gateway
// pod for the walk to tell apart from the agent's own.
//
// Two storages, because only one of them reaches the pod. Replicas above 1
// beside RWO storage is what makes useStatefulSet true, and
// buildCustomStorageVolumes then skips every RWO storage: the StatefulSet
// reaches it through a volumeClaimTemplate, which is not a claim volume source
// and so is invisible to volumesBackedByClaim. That skip is keyed on the CR
// rather than on the calling builder, so it applies to the one pod template
// both gateway builders share. `shared` is ReadWriteMany, is not skipped, and
// is what actually puts a second claim name in front of the filter.
func dataVolumeEveryComponentAgent() *agentv1alpha1.PlatformAgent {
	agent := dataVolumeTestAgent()
	agent.Spec.Mode = ptr.To(string(ModeNext))
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(true)},
		},
	}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))},
		Storages: []agentv1alpha1.StorageSpec{
			{
				Name:        "scratch",
				AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
				StorageSize: "5Gi",
				MountPath:   "/scratch",
			},
			{
				Name:        "shared",
				AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany},
				StorageSize: "5Gi",
				MountPath:   "/shared",
			},
		},
	}
	return agent
}

// TestEveryPodMountingTheAgentDataClaimRunsAsOneUID walks the rendered
// workloads and holds the agent's data volume to a single uid.
//
// Driven by the volume rather than by one pod template, which is what reaches
// the pods that must *not* mount it: the A2A pods at uid 1000, the credential
// runtime in its own pod, and the shell sandbox, which sets no uid at all
// because sshd forks as root and drops to its own unprivileged user in-process.
// Any of them acquiring the claim fails here — the sandbox through the
// no-runAsUser branch below, the others on the uid.
func TestEveryPodMountingTheAgentDataClaimRunsAsOneUID(t *testing.T) {
	shapes := []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		// The claims other than the agent's own that the gateway pod must
		// carry, asserted rather than assumed. What this walk does beyond
		// reading a pod's security context is discriminate — reject a volume
		// backed by some other claim, and every container that mounts only
		// such a volume — and a fixture that quietly stops rendering a second
		// claim leaves that discrimination unexercised with nothing saying so.
		otherClaims []string
	}{
		{"default", dataVolumeTestAgent(), []string{"system-metadata"}},
		{"every component", dataVolumeEveryComponentAgent(), []string{"system-metadata", "shared"}},
	}

	for _, shape := range shapes {
		t.Run(shape.name, func(t *testing.T) {
			claim := shape.agent.Name + agentDataClaimSuffix
			gatewayReached := false
			mountingContainers := 0
			gatewayOtherClaims := map[string]bool{}
			rejectedMounts := 0

			for _, pod := range renderEveryWorkloadPod(shape.agent) {
				volumes := volumesBackedByClaim(pod.spec, claim)
				if len(volumes) == 0 {
					continue
				}
				isGateway := pod.workload == shape.agent.Name+gatewayWorkloadSuffix
				if isGateway {
					gatewayReached = true
				}

				otherVolumes := map[string]bool{}
				for _, volume := range pod.spec.Volumes {
					pvc := volume.PersistentVolumeClaim
					if pvc == nil || pvc.ClaimName == claim {
						continue
					}
					otherVolumes[volume.Name] = true
					if isGateway {
						gatewayOtherClaims[pvc.ClaimName] = true
					}
				}

				// The fsGroup is what makes the claim's contents reachable to
				// the group at all; without it the volume arrives owned by
				// whichever uid wrote it and root's group.
				podSC := pod.spec.SecurityContext
				if podSC == nil || podSC.FSGroup == nil || *podSC.FSGroup != agentFSGroup {
					t.Errorf("%s (%s) mounts %s but does not carry fsGroup %d: %#v", pod.workload, pod.builder, claim, agentFSGroup, podSC)
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
					for _, mount := range container.VolumeMounts {
						if otherVolumes[mount.Name] {
							rejectedMounts++
							break
						}
					}
					if !mountsClaim {
						continue
					}
					mountingContainers++

					user := effectiveRunAsUser(pod.spec, container)
					if user == nil {
						t.Errorf("container %s in %s (%s) mounts %s with no runAsUser at either level, so the image picks the uid",
							container.Name, pod.workload, pod.builder, claim)
						continue
					}
					if *user != sandboxUID {
						t.Errorf("container %s in %s (%s) mounts %s as uid %d; a second uid on this claim is #1244, which needs %d",
							container.Name, pod.workload, pod.builder, claim, *user, sandboxUID)
					}
				}
			}

			if !gatewayReached {
				t.Errorf("the walk never found the gateway pod mounting %s; either the claim was renamed or this assertion no longer reaches the pod it is about", claim)
			}
			if mountingContainers == 0 {
				t.Errorf("no container mounted %s, so this walk asserted nothing", claim)
			}
			for _, other := range shape.otherClaims {
				if !gatewayOtherClaims[other] {
					t.Errorf("the gateway pod carries no volume backed by the %s claim, so the walk was never handed a second claim to reject; it saw %v", other, gatewayOtherClaims)
				}
			}
			if rejectedMounts == 0 {
				t.Errorf("no container mounted a claim other than %s, so nothing here told the agent's claim apart from another one", claim)
			}
		})
	}
}

// podBearingKinds are the workload kinds whose spec carries a pod template. A
// builder returning one of these renders containers, and containers are what
// the walk above is about.
var podBearingKinds = map[string]bool{
	"Pod":         true,
	"Deployment":  true,
	"StatefulSet": true,
	"DaemonSet":   true,
	"ReplicaSet":  true,
	"Job":         true,
	"CronJob":     true,
}

// TestTheWalkCallsEveryPodBearingBuilder ties renderEveryWorkloadPod's list to
// the package, so the walk cannot narrow silently.
//
// Without it the list is seven names somebody typed: a builder added later
// renders a pod nothing here reaches, and a new workload mounting the agent's
// claim under its image's own uid ships green. This reads the package source
// instead and requires every function returning a pod-bearing kind to be called
// inside the walk. It sees this directory only — no pod template is built
// anywhere else in k8s-operator/ today, and one built outside it would need
// this assertion widened along with the walk.
func TestTheWalkCallsEveryPodBearingBuilder(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("reading the package directory: %v", err)
	}

	fset := token.NewFileSet()
	builders := map[string]string{}
	var walk *ast.FuncDecl
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, ".go") {
			continue
		}
		file, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatalf("parsing %s: %v", name, err)
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Recv != nil {
				continue
			}
			if strings.HasSuffix(name, "_test.go") {
				if fn.Name.Name == dataVolumeWalkFunc {
					walk = fn
				}
				continue
			}
			if kind := podBearingResultKind(fn); kind != "" {
				builders[fn.Name.Name] = kind
			}
		}
	}

	if walk == nil {
		t.Fatalf("%s not found in this package; the assertion below has nothing to check", dataVolumeWalkFunc)
	}
	if len(builders) == 0 {
		t.Fatal("no pod-bearing builder found in this package, so this assertion passed vacuously")
	}

	called := map[string]bool{}
	ast.Inspect(walk, func(node ast.Node) bool {
		if call, ok := node.(*ast.CallExpr); ok {
			if ident, ok := call.Fun.(*ast.Ident); ok {
				called[ident.Name] = true
			}
		}
		return true
	})

	names := make([]string, 0, len(builders))
	for builder := range builders {
		names = append(names, builder)
	}
	sort.Strings(names)
	for _, builder := range names {
		if !called[builder] {
			t.Errorf("%s renders a %s, and %s does not call it — its pod never reaches the uid assertion",
				builder, builders[builder], dataVolumeWalkFunc)
		}
	}
}

// podBearingResultKind names the workload kind a function returns, or "" for a
// function that returns none. Every result is considered, so a builder that
// returns an error alongside its object still counts.
func podBearingResultKind(fn *ast.FuncDecl) string {
	if fn.Type.Results == nil {
		return ""
	}
	for _, result := range fn.Type.Results.List {
		star, ok := result.Type.(*ast.StarExpr)
		if !ok {
			continue
		}
		selector, ok := star.X.(*ast.SelectorExpr)
		if !ok {
			continue
		}
		if podBearingKinds[selector.Sel.Name] {
			return selector.Sel.Name
		}
	}
	return ""
}
