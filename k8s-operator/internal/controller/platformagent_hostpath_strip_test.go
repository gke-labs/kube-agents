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
	"regexp"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The tests in this file are for #1671: a hostPath on spec.deployment.extraVolumes
// or .sidecarVolumes was refused by the admission webhook and nowhere else, and
// the chart ships the webhook off. They assert the render-side layer: the volume
// and every mount naming it are out of the Pod template, the CR's own slices are
// not edited, the operator's own /tmp mount survives a user hostPath claiming
// that path, a non-hostPath volume of the same shape is untouched, and the drop
// is reported on status by the passes that rendered a template -- as a claim
// about that template, qualified while the roll carrying it is unfinished.
//
// The condition type and reason are read off the controller's own constants,
// as the EventWatcher cases in platformagent_controller_test.go read theirs.
// They were spelled as literals here while the first render cases were being
// run against a controller without the fix, to watch them fail; that is not
// reproducible in place any more, because the message-budget, condition-gate
// and rollout cases reach hostPathDroppedMessage, hostPathExtraVolumesField,
// hostPathDroppedEntryEllipsis and the oldPodsPossible constants, none of
// which exist without the fix, so the file as a whole does not compile there.
// Reproducing that failing run now means taking the render cases over on
// their own, and the literals bought nothing once it did.
//
// The cases written after the fix were checked the other way round, by mutating
// the fixed controller and watching which of them failed:
// TestRenderDropsAHostPathMountedAtTmpKeepsTmpScratch by moving the mount filter
// in buildBaseContainers back below dropTmpScratchIfClaimed, and
// TestTheDroppedVolumeConditionFollowsTheGatewayRollout by dropping the roll
// test in updateStatusReady a term at a time.

const (
	// Fixture names. The paths are chosen so a test that finds one in the
	// rendered Pod is unambiguous about which entry leaked.
	hostPathFixtureExtraVolume   = "host-root"
	hostPathFixtureExtraPath     = "/"
	hostPathFixtureSidecarVolume = "host-sock"
	hostPathFixtureSidecarPath   = "/var/run/docker.sock"
	hostPathFixtureSidecarName   = "user-sidecar"
	hostPathFixtureInitName      = "user-init"
	hostPathFixtureEmptyDirExtra = "extra-scratch"
	hostPathFixtureEmptyDirSide  = "sidecar-scratch"
	hostPathFixtureMountPath     = "/mnt/host"
	hostPathFixtureScratchPath   = "/mnt/scratch"
	// The /tmp fixture, for the ordering case below. Host path and mount path
	// differ so a failure message says which of the two it found.
	hostPathFixtureTmpVolume = "host-tmp"
	hostPathFixtureTmpHost   = "/var/tmp"
	hostPathFixtureTmpMount  = "/tmp"
	// conditionMessageMaxLength is the cap the CRD schema puts on a condition
	// message (`maxLength: 32768` under status.conditions[].message in
	// config/crd/bases/kubeagents.x-k8s.io_platformagents.yaml, from
	// metav1.Condition's own marker). A message over it does not fail this
	// condition alone: the API server refuses the whole status subresource
	// write, so Ready, the phase and every other condition go with it.
	conditionMessageMaxLength = 32768
	// The flood fixture: enough author-chosen characters that listing every
	// entry would run past that cap.
	floodedHostPathCount   = 64
	floodedHostPathNameLen = 1024
)

// hostPathOverflowCountPattern reads the count back out of the message's
// overflow clause, so the test does not hard-code how many entries the budget
// happens to fit.
var hostPathOverflowCountPattern = regexp.MustCompile(`and (\d+) more`)

// hostPathAgent is a CR carrying one hostPath on each list, mounted from the
// agent container (extraVolumeMounts), a user sidecar and a user init container,
// beside an emptyDir of the same shape on each list so the tests can tell a
// filter that drops hostPath from one that drops everything.
func hostPathAgent() *agentv1alpha1.PlatformAgent {
	agent := brokerPodAgent()
	agent.Spec.Deployment = hostPathDeploymentSpec()
	return agent
}

// hostPathDeploymentSpec is the spec.deployment the fixture carries, on its own
// so the envtest case can put it on a CR the CRD's own validation admits.
func hostPathDeploymentSpec() *agentv1alpha1.DeploymentSpec {
	return &agentv1alpha1.DeploymentSpec{
		ExtraVolumes: []corev1.Volume{
			{Name: hostPathFixtureExtraVolume, VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: hostPathFixtureExtraPath}}},
			{Name: hostPathFixtureEmptyDirExtra, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		},
		ExtraVolumeMounts: []corev1.VolumeMount{
			{Name: hostPathFixtureExtraVolume, MountPath: hostPathFixtureMountPath},
			{Name: hostPathFixtureEmptyDirExtra, MountPath: hostPathFixtureScratchPath},
		},
		SidecarVolumes: []corev1.Volume{
			{Name: hostPathFixtureSidecarVolume, VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: hostPathFixtureSidecarPath}}},
			{Name: hostPathFixtureEmptyDirSide, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
		},
		Sidecars: []corev1.Container{{
			Name:  hostPathFixtureSidecarName,
			Image: "sidecar:test",
			VolumeMounts: []corev1.VolumeMount{
				{Name: hostPathFixtureSidecarVolume, MountPath: hostPathFixtureMountPath},
				{Name: hostPathFixtureEmptyDirSide, MountPath: hostPathFixtureScratchPath},
			},
		}},
		InitContainers: []corev1.Container{{
			Name:  hostPathFixtureInitName,
			Image: "init:test",
			VolumeMounts: []corev1.VolumeMount{
				{Name: hostPathFixtureExtraVolume, MountPath: hostPathFixtureMountPath},
			},
		}},
	}
}

func renderHostPathPod(t *testing.T, agent *agentv1alpha1.PlatformAgent) corev1.PodSpec {
	t.Helper()
	return buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true}).Spec.Template.Spec
}

// mustFindContainer is findContainer (platformagent_manifests_test.go) with
// the miss turned into a failure; hasVolume is the broker split test's.
func mustFindContainer(t *testing.T, pod corev1.PodSpec, name string) corev1.Container {
	t.Helper()
	c, ok := findContainer(pod, name)
	if !ok {
		t.Fatalf("no container named %q in the rendered Pod", name)
	}
	return c
}

func hasMount(mounts []corev1.VolumeMount, name string) bool {
	for _, m := range mounts {
		if m.Name == name {
			return true
		}
	}
	return false
}

// assertNoDanglingMounts is the API-server rule the render has to satisfy:
// every volumeMount on every container names a volume the Pod declares.
func assertNoDanglingMounts(t *testing.T, pod corev1.PodSpec) {
	t.Helper()
	declared := make(map[string]bool, len(pod.Volumes))
	for _, v := range pod.Volumes {
		declared[v.Name] = true
	}
	for _, c := range append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...) {
		for _, m := range c.VolumeMounts {
			if !declared[m.Name] {
				t.Errorf("container %q mounts %q at %s, and the Pod declares no such volume: the API server rejects this Deployment", c.Name, m.Name, m.MountPath)
			}
		}
	}
}

func assertNoHostPathVolumes(t *testing.T, volumes []corev1.Volume) {
	t.Helper()
	for _, v := range volumes {
		if v.HostPath != nil {
			t.Errorf("volume %q reached the Pod with hostPath %s", v.Name, v.HostPath.Path)
		}
	}
}

func TestRenderDropsAHostPathExtraVolumeAndItsMounts(t *testing.T) {
	pod := renderHostPathPod(t, hostPathAgent())

	assertNoHostPathVolumes(t, pod.Volumes)
	if hasVolume(pod.Volumes, hostPathFixtureExtraVolume) {
		t.Errorf("extraVolumes entry %q is in the Pod", hostPathFixtureExtraVolume)
	}
	agentContainer := mustFindContainer(t, pod, "platform-agent")
	if hasMount(agentContainer.VolumeMounts, hostPathFixtureExtraVolume) {
		t.Errorf("the agent container still mounts %q", hostPathFixtureExtraVolume)
	}
	if !hasMount(agentContainer.VolumeMounts, hostPathFixtureEmptyDirExtra) {
		t.Errorf("the agent container lost its emptyDir mount %q; only the hostPath's mount should go", hostPathFixtureEmptyDirExtra)
	}
	// The dashboard container takes extraVolumeMounts too, and on main it was
	// the third place the hostPath mount landed.
	dashboard := mustFindContainer(t, pod, "platform-agent-dashboard")
	if hasMount(dashboard.VolumeMounts, hostPathFixtureExtraVolume) {
		t.Errorf("the dashboard container still mounts %q", hostPathFixtureExtraVolume)
	}
	initContainer := mustFindContainer(t, pod, hostPathFixtureInitName)
	if hasMount(initContainer.VolumeMounts, hostPathFixtureExtraVolume) {
		t.Errorf("the user init container still mounts %q", hostPathFixtureExtraVolume)
	}
	assertNoDanglingMounts(t, pod)
}

func TestRenderDropsAHostPathSidecarVolumeAndItsMounts(t *testing.T) {
	pod := renderHostPathPod(t, hostPathAgent())

	if hasVolume(pod.Volumes, hostPathFixtureSidecarVolume) {
		t.Errorf("sidecarVolumes entry %q is in the Pod", hostPathFixtureSidecarVolume)
	}
	sidecar := mustFindContainer(t, pod, hostPathFixtureSidecarName)
	if hasMount(sidecar.VolumeMounts, hostPathFixtureSidecarVolume) {
		t.Errorf("the user sidecar still mounts %q", hostPathFixtureSidecarVolume)
	}
	if !hasMount(sidecar.VolumeMounts, hostPathFixtureEmptyDirSide) {
		t.Errorf("the user sidecar lost its emptyDir mount %q; only the hostPath's mount should go", hostPathFixtureEmptyDirSide)
	}
	assertNoDanglingMounts(t, pod)
}

// The control: the same lists with no hostPath render exactly as before, and
// the emptyDir beside each dropped entry survives with its mounts.
func TestRenderKeepsANonHostPathVolumeOfTheSameShape(t *testing.T) {
	agent := hostPathAgent()
	pod := renderHostPathPod(t, agent)
	for _, name := range []string{hostPathFixtureEmptyDirExtra, hostPathFixtureEmptyDirSide} {
		if !hasVolume(pod.Volumes, name) {
			t.Errorf("emptyDir volume %q was dropped with the hostPath entries", name)
		}
	}

	agent.Spec.Deployment.ExtraVolumes = agent.Spec.Deployment.ExtraVolumes[1:]
	agent.Spec.Deployment.ExtraVolumeMounts = agent.Spec.Deployment.ExtraVolumeMounts[1:]
	agent.Spec.Deployment.SidecarVolumes = agent.Spec.Deployment.SidecarVolumes[1:]
	agent.Spec.Deployment.Sidecars[0].VolumeMounts = agent.Spec.Deployment.Sidecars[0].VolumeMounts[1:]
	agent.Spec.Deployment.InitContainers[0].VolumeMounts = nil
	clean := renderHostPathPod(t, agent)
	if !hasVolume(clean.Volumes, hostPathFixtureEmptyDirExtra) || !hasVolume(clean.Volumes, hostPathFixtureEmptyDirSide) {
		t.Errorf("a CR with no hostPath lost a volume: %v", clean.Volumes)
	}
	if !hasMount(mustFindContainer(t, clean, "platform-agent").VolumeMounts, hostPathFixtureEmptyDirExtra) {
		t.Errorf("a CR with no hostPath lost the agent container's extraVolumeMounts entry")
	}
	if !hasMount(mustFindContainer(t, clean, hostPathFixtureSidecarName).VolumeMounts, hostPathFixtureEmptyDirSide) {
		t.Errorf("a CR with no hostPath lost the sidecar's mount")
	}
	assertNoDanglingMounts(t, clean)
}

func TestRenderDropsAnUnmountedHostPathVolumeWithoutADanglingMount(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		ExtraVolumes: []corev1.Volume{
			{Name: hostPathFixtureExtraVolume, VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: hostPathFixtureExtraPath}}},
		},
	}
	pod := renderHostPathPod(t, agent)
	assertNoHostPathVolumes(t, pod.Volumes)
	if hasVolume(pod.Volumes, hostPathFixtureExtraVolume) {
		t.Errorf("unmounted hostPath volume %q is in the Pod", hostPathFixtureExtraVolume)
	}
	for _, c := range append(append([]corev1.Container{}, pod.InitContainers...), pod.Containers...) {
		if hasMount(c.VolumeMounts, hostPathFixtureExtraVolume) {
			t.Errorf("container %q mounts %q, which nothing in the spec asked for", c.Name, hostPathFixtureExtraVolume)
		}
	}
	assertNoDanglingMounts(t, pod)
}

// TestRenderDropsAHostPathMountedAtTmpKeepsTmpScratch pins the order of two
// filters inside buildBaseContainers that a rebase could swap without failing
// anything else.
//
// The agent and dashboard containers get an operator-owned emptyDir at /tmp
// (tmpScratchVolumeName), and dropTmpScratchIfClaimed takes it away when the
// CR's own mounts already claim that path -- two mounts on one mountPath make
// the Deployment unappliable. The hostPath mount filter therefore has to run
// first: once the /tmp mount is out of the list, dropTmpScratchIfClaimed sees
// nothing claiming /tmp and leaves the emptyDir in place.
//
// Run the other way round, the render drops the hostPath mount *and* the
// emptyDir, and both containers come up with no /tmp at all -- on a
// readOnlyRootFilesystem image whose entrypoint runs with HOME=/tmp, which is
// a crash loop under a VolumesDropped condition reporting only the hostPath.
// Nothing else in this file reaches the interaction: the other fixtures mount
// at /mnt/*, and the tmp-scratch cases in platformagent_manifests_test.go
// declare no hostPath, so the filter there is a no-op over an empty set.
func TestRenderDropsAHostPathMountedAtTmpKeepsTmpScratch(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		ExtraVolumes: []corev1.Volume{{
			Name:         hostPathFixtureTmpVolume,
			VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: hostPathFixtureTmpHost}},
		}},
		ExtraVolumeMounts: []corev1.VolumeMount{
			{Name: hostPathFixtureTmpVolume, MountPath: hostPathFixtureTmpMount},
		},
	}
	pod := renderHostPathPod(t, agent)

	assertNoHostPathVolumes(t, pod.Volumes)
	if hasVolume(pod.Volumes, hostPathFixtureTmpVolume) {
		t.Errorf("extraVolumes entry %q is in the Pod", hostPathFixtureTmpVolume)
	}
	if !hasVolume(pod.Volumes, tmpScratchVolumeName) {
		t.Errorf("the %s emptyDir went with the hostPath; the Pod declares %v", tmpScratchVolumeName, pod.Volumes)
	}

	// Both containers take extraVolumeMounts and both carry the emptyDir, so
	// both have to be checked: the two dropTmpScratchIfClaimed calls are on
	// separate lines and a rebase can reorder one without the other.
	for _, name := range []string{"platform-agent", "platform-agent-dashboard"} {
		c := mustFindContainer(t, pod, name)
		if hasMount(c.VolumeMounts, hostPathFixtureTmpVolume) {
			t.Errorf("container %q still mounts the hostPath %q", name, hostPathFixtureTmpVolume)
		}
		owners := []string{}
		for _, m := range c.VolumeMounts {
			if m.MountPath == hostPathFixtureTmpMount {
				owners = append(owners, m.Name)
			}
		}
		switch {
		case len(owners) == 0:
			t.Errorf("container %q has no %s mount: the hostPath mount was filtered after dropTmpScratchIfClaimed read the list, so the %s emptyDir went with it. This container runs read-only-rootfs with HOME=%s.",
				name, hostPathFixtureTmpMount, tmpScratchVolumeName, hostPathFixtureTmpMount)
		case len(owners) > 1:
			t.Errorf("container %q mounts %s twice, from %v; the API server rejects the Deployment", name, hostPathFixtureTmpMount, owners)
		case owners[0] != tmpScratchVolumeName:
			t.Errorf("container %q serves %s from volume %q, want the operator's %s emptyDir", name, hostPathFixtureTmpMount, owners[0], tmpScratchVolumeName)
		}
	}
	assertNoDanglingMounts(t, pod)
}

// The containers and mount lists come off the manager's cached copy of the CR.
// A filter that edits them in place would make the second reconcile see a
// spec the author did not write, and the condition below would have nothing
// to report.
func TestRenderLeavesTheCRsOwnSlicesUntouched(t *testing.T) {
	agent := hostPathAgent()
	before := agent.DeepCopy()
	renderHostPathPod(t, agent)
	if !hasMount(agent.Spec.Deployment.Sidecars[0].VolumeMounts, hostPathFixtureSidecarVolume) {
		t.Errorf("render removed the mount from the CR's own sidecar container")
	}
	if !hasMount(agent.Spec.Deployment.InitContainers[0].VolumeMounts, hostPathFixtureExtraVolume) {
		t.Errorf("render removed the mount from the CR's own init container")
	}
	if len(agent.Spec.Deployment.ExtraVolumes) != len(before.Spec.Deployment.ExtraVolumes) ||
		len(agent.Spec.Deployment.SidecarVolumes) != len(before.Spec.Deployment.SidecarVolumes) ||
		len(agent.Spec.Deployment.ExtraVolumeMounts) != len(before.Spec.Deployment.ExtraVolumeMounts) {
		t.Errorf("render shortened one of the CR's own volume lists")
	}
}

func TestReconcileReportsADroppedHostPathVolumeAndClearsItWhenRemoved(t *testing.T) {
	agent := hostPathAgent()
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	reconcileTwice := func() {
		t.Helper()
		for pass := 0; pass < 2; pass++ {
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile pass %d failed: %v", pass, err)
			}
		}
	}
	reconcileTwice()

	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("reading the gateway Deployment: %v", err)
	}
	assertNoHostPathVolumes(t, dep.Spec.Template.Spec.Volumes)
	assertNoDanglingMounts(t, dep.Spec.Template.Spec)

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType)
	if cond == nil {
		t.Fatalf("no %s condition after rendering around a hostPath; conditions: %+v", hostPathDroppedConditionType, got.Status.Conditions)
	}
	if cond.Status != metav1.ConditionTrue || cond.Reason != hostPathDroppedReason {
		t.Errorf("%s condition = %s/%s, want True/%s", hostPathDroppedConditionType, cond.Status, cond.Reason, hostPathDroppedReason)
	}
	for _, want := range []string{
		"spec.deployment.extraVolumes[0]", hostPathFixtureExtraVolume, hostPathFixtureExtraPath,
		"spec.deployment.sidecarVolumes[0]", hostPathFixtureSidecarVolume, hostPathFixtureSidecarPath,
	} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("%s message does not name %q: %s", hostPathDroppedConditionType, want, cond.Message)
		}
	}
	if got.Status.Phase == "Degraded" {
		t.Errorf("a dropped hostPath parked the CR on Degraded; the issue asks for drop-and-continue")
	}
	t.Logf("%s: %s/%s: %s", cond.Type, cond.Status, cond.Reason, cond.Message)

	// A pass that changes nothing writes nothing. The condition is in
	// updateStatusReady's unchanged comparison for the same reason the others
	// are: a status write per pass re-enqueues the CR through the unfiltered
	// watch and reconciles it continuously.
	settledVersion := got.ResourceVersion
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile (settled) failed: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if got.ResourceVersion != settledVersion {
		t.Errorf("a reconcile with nothing to change wrote the CR (resourceVersion %s -> %s); the %s condition is causing a write per pass", settledVersion, got.ResourceVersion, hostPathDroppedConditionType)
	}

	// Removing the entries clears the condition on the next pass.
	got.Spec.Deployment.ExtraVolumes = got.Spec.Deployment.ExtraVolumes[1:]
	got.Spec.Deployment.ExtraVolumeMounts = got.Spec.Deployment.ExtraVolumeMounts[1:]
	got.Spec.Deployment.SidecarVolumes = got.Spec.Deployment.SidecarVolumes[1:]
	got.Spec.Deployment.Sidecars[0].VolumeMounts = got.Spec.Deployment.Sidecars[0].VolumeMounts[1:]
	got.Spec.Deployment.InitContainers[0].VolumeMounts = nil
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("removing the hostPath entries: %v", err)
	}
	reconcileTwice()
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType); cond != nil {
		t.Errorf("%s condition survived the removal of every hostPath entry: %+v", hostPathDroppedConditionType, cond)
	}
}

func TestReconcileWritesNoVolumesDroppedConditionWithoutAHostPath(t *testing.T) {
	agent := brokerPodAgent()
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	for pass := 0; pass < 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile pass %d failed: %v", pass, err)
		}
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType); cond != nil {
		t.Errorf("%s condition written for a CR with no hostPath: %+v", hostPathDroppedConditionType, cond)
	}
}

// TestADroppedHostPathIsReportedOnAReconcileThatParksDegraded is the gap the
// Ready-only condition write left. Three refusals render the workload in full
// and then park the CR on Degraded (ModeNotRecognized, A2AProvisionFailed,
// ShellSandboxKeysMissing), and on those passes updateStatusReady never runs.
// The one used here is the one a chart-default install sits on indefinitely:
// the chart renders the sandbox's authorized-keys Secret only when a public
// key is supplied, and `credentials.create` is false by default — which is the
// same install the webhook is off on, so it is also the install where a
// hostPath reaches the reconcile at all. The Deployment is written without the
// volume either way; what this asserts is that the CR says so.
func TestADroppedHostPathIsReportedOnAReconcileThatParksDegraded(t *testing.T) {
	agent := hostPathAgent()
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	if err := cl.Delete(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("removing the sandbox keys Secret the fixture creates: %v", err)
	}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	for pass := 0; pass < 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile pass %d failed: %v", pass, err)
		}
	}

	// The render happened: without it there is no drop to report and the test
	// would pass against a controller that never writes the condition at all.
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("the Degraded path did not render the gateway Deployment, so this is no longer the case under test: %v", err)
	}
	assertNoHostPathVolumes(t, dep.Spec.Template.Spec.Volumes)
	assertNoDanglingMounts(t, dep.Spec.Template.Spec)

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, "Ready")
	if got.Status.Phase != "Degraded" || ready == nil || ready.Reason != reasonShellSandboxKeysMissing {
		t.Fatalf("phase=%q Ready=%+v, want Degraded/%s; the pass under test is the one that parks there", got.Status.Phase, ready, reasonShellSandboxKeysMissing)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType)
	if cond == nil {
		t.Fatalf("no %s condition on a CR parked Degraded after the render dropped a hostPath; conditions: %+v", hostPathDroppedConditionType, got.Status.Conditions)
	}
	if cond.Status != metav1.ConditionTrue || cond.Reason != hostPathDroppedReason {
		t.Errorf("%s condition = %s/%s, want True/%s", hostPathDroppedConditionType, cond.Status, cond.Reason, hostPathDroppedReason)
	}
	for _, want := range []string{
		"spec.deployment.extraVolumes[0]", hostPathFixtureExtraVolume, hostPathFixtureExtraPath,
		"spec.deployment.sidecarVolumes[0]", hostPathFixtureSidecarVolume, hostPathFixtureSidecarPath,
	} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("%s message does not name %q: %s", hostPathDroppedConditionType, want, cond.Message)
		}
	}

	// Still one write per change, not one per pass: the Degraded writer's
	// unchanged comparison has to cover the condition it now carries, or the
	// parked CR writes status on every requeue tick (#1392).
	settledVersion := got.ResourceVersion
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile (settled) failed: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if got.ResourceVersion != settledVersion {
		t.Errorf("a parked pass with nothing to change wrote the CR (resourceVersion %s -> %s)", settledVersion, got.ResourceVersion)
	}

	// And it clears on the Degraded path too, rather than standing until the
	// CR happens to reach Ready.
	got.Spec.Deployment.ExtraVolumes = got.Spec.Deployment.ExtraVolumes[1:]
	got.Spec.Deployment.ExtraVolumeMounts = got.Spec.Deployment.ExtraVolumeMounts[1:]
	got.Spec.Deployment.SidecarVolumes = got.Spec.Deployment.SidecarVolumes[1:]
	got.Spec.Deployment.Sidecars[0].VolumeMounts = got.Spec.Deployment.Sidecars[0].VolumeMounts[1:]
	got.Spec.Deployment.InitContainers[0].VolumeMounts = nil
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("removing the hostPath entries: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile (after removal) failed: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType); cond != nil {
		t.Errorf("%s condition survived the removal of every hostPath entry on the Degraded path: %+v", hostPathDroppedConditionType, cond)
	}
}

// The message is a claim about the Pod template the controller rendered, not
// about the Pod that is running. reconcileWorkload server-side-applies the
// template and returns as soon as the API server accepts it, so on a CR a
// pre-fix operator rendered with real hostPath mounts the old Pods keep them
// until the roll replaces them -- and the roll can stall. The clause saying so
// is present exactly when the caller could see the roll was unfinished.
func TestTheDroppedVolumeMessageQualifiesItselfWhileTheRollIsUnfinished(t *testing.T) {
	agent := hostPathAgent()
	settled := hostPathDroppedMessage(agent, rolloutNotKnownIncomplete)
	rolling := hostPathDroppedMessage(agent, rolloutIncomplete)

	if strings.Contains(settled, hostPathDroppedRollingClause) {
		t.Errorf("the unqualified message carries the rollout clause: %s", settled)
	}
	if !strings.Contains(rolling, hostPathDroppedRollingClause) {
		t.Errorf("the message written while the roll is unfinished does not say so: %s", rolling)
	}
	for form, msg := range map[string]string{"settled": settled, "rolling": rolling} {
		// What the controller observes is the template, so that is what the
		// message may speak about.
		if !strings.Contains(msg, "Pod template") {
			t.Errorf("the %s message does not say the claim is about the rendered Pod template: %s", form, msg)
		}
		if !strings.Contains(msg, hostPathFixtureExtraVolume) || !strings.Contains(msg, hostPathFixtureSidecarVolume) {
			t.Errorf("the %s message does not name both dropped entries: %s", form, msg)
		}
		if len(msg) > conditionMessageMaxLength {
			t.Errorf("the %s message is %d characters, over the %d the CRD schema allows", form, len(msg), conditionMessageMaxLength)
		}
	}
}

// And the clause is wired to the gateway workload rather than to a constant.
// updateStatusReady reads the roll off the workload it already fetches, on two
// terms: the workload controller has not observed the applied template yet, or
// it has and still counts Pods that are not on it. Either one means Pods from
// an earlier revision -- which a pre-fix operator may have rendered with these
// volumes really mounted -- can still be running.
func TestTheDroppedVolumeConditionFollowsTheGatewayRollout(t *testing.T) {
	cases := []struct {
		name string
		// replicas > 1 over RWO storage is what puts the gateway on a
		// StatefulSet, whose ordered roll has the same window.
		statefulSet bool
		generation  int64
		observed    int64
		replicas    int32
		updated     int32
		rolling     bool
	}{
		{
			// The apply has landed and the workload controller has written
			// nothing back yet, so its counts describe the template before it
			// and read fully rolled out while every Pod is still old.
			name:       "applied template not observed yet",
			generation: 4, observed: 3, replicas: 3, updated: 3,
			rolling: true,
		},
		{
			// Mid-roll: one Pod on the new template, two on the old one.
			name:       "replicas from an earlier revision still counted",
			generation: 4, observed: 4, replicas: 3, updated: 1,
			rolling: true,
		},
		{
			name:       "fully rolled out",
			generation: 4, observed: 4, replicas: 3, updated: 3,
			rolling: false,
		},
		{
			name:        "statefulset mid-roll",
			statefulSet: true,
			generation:  4, observed: 4, replicas: 3, updated: 1,
			rolling: true,
		},
		{
			name:        "statefulset fully rolled out",
			statefulSet: true,
			generation:  4, observed: 4, replicas: 3, updated: 3,
			rolling: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := hostPathAgent()
			gatewayMeta := metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace, Generation: tc.generation}
			var gateway client.Object = &appsv1.Deployment{
				ObjectMeta: gatewayMeta,
				Status: appsv1.DeploymentStatus{
					ObservedGeneration: tc.observed,
					Replicas:           tc.replicas,
					UpdatedReplicas:    tc.updated,
					ReadyReplicas:      tc.replicas,
				},
			}
			if tc.statefulSet {
				agent.Spec.Deployment.Availability = &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(tc.replicas)}
				agent.Spec.Deployment.Storages = []agentv1alpha1.StorageSpec{{
					Name:        "gateway-data",
					MountPath:   "/srv/gateway-data",
					AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
				}}
				if !useStatefulSet(agent) {
					t.Fatalf("this case is meant to take the StatefulSet path and does not")
				}
				gateway = &appsv1.StatefulSet{
					ObjectMeta: gatewayMeta,
					Status: appsv1.StatefulSetStatus{
						ObservedGeneration: tc.observed,
						Replicas:           tc.replicas,
						UpdatedReplicas:    tc.updated,
						ReadyReplicas:      tc.replicas,
					},
				}
			}

			scheme := setupScheme()
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(agent, gateway).
				WithStatusSubresource(agent).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
			ctx := context.Background()

			if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent)); err != nil {
				t.Fatalf("updateStatusReady failed: %v", err)
			}
			cond := meta.FindStatusCondition(agent.Status.Conditions, hostPathDroppedConditionType)
			if cond == nil {
				t.Fatalf("no %s condition; conditions: %+v", hostPathDroppedConditionType, agent.Status.Conditions)
			}
			if got := strings.Contains(cond.Message, hostPathDroppedRollingClause); got != tc.rolling {
				t.Errorf("rollout clause present = %v, want %v, at generation %d with observedGeneration %d and %d of %d replicas updated: %s",
					got, tc.rolling, tc.generation, tc.observed, tc.updated, tc.replicas, cond.Message)
			}

			// The clause has to be inside the unchanged comparison as well as
			// inside the write. It rides in the message, which the comparison
			// already covers -- but a comparison that recomputed the message
			// without it would differ from the written one on every pass, and
			// a CR sitting mid-roll would write status on every 30s requeue
			// tick and wake itself through the unfiltered watch (#1392).
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			settledVersion := stored.ResourceVersion
			if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent)); err != nil {
				t.Fatalf("updateStatusReady (settled) failed: %v", err)
			}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			if stored.ResourceVersion != settledVersion {
				t.Errorf("a second pass over unchanged state wrote the CR (resourceVersion %s -> %s); the %s message and the comparison disagree",
					settledVersion, stored.ResourceVersion, hostPathDroppedConditionType)
			}
		})
	}
}

// The same qualification on the Degraded path. The three refusals that land
// there below the render have the identical window -- the pass applied a
// template and the apply returns before the Pods carrying the hostPath are
// gone -- and unlike updateStatusReady this writer holds no workload object,
// so it reads the gateway back to answer. It is a cache read: SetupWithManager
// Owns both workload kinds. An earlier round of this change left the Degraded
// wording unqualified on the theory that the read would be an API request per
// parked pass, which is the one wording that can claim a security property the
// cluster does not have.
func TestTheDegradedPathQualifiesTheDroppedVolumeMessageWhileTheRollIsUnfinished(t *testing.T) {
	cases := []struct {
		name string
		// replicas > 1 over RWO storage is what puts the gateway on a
		// StatefulSet, whose ordered roll has the same window.
		statefulSet bool
		// noGateway drops the workload entirely: a writer that cannot read the
		// roll has to qualify, not to assume the roll is done.
		noGateway  bool
		generation int64
		observed   int64
		replicas   int32
		updated    int32
		rolling    bool
	}{
		{
			name:       "applied template not observed yet",
			generation: 4, observed: 3, replicas: 3, updated: 3,
			rolling: true,
		},
		{
			name:       "replicas from an earlier revision still counted",
			generation: 4, observed: 4, replicas: 3, updated: 1,
			rolling: true,
		},
		{
			name:       "fully rolled out",
			generation: 4, observed: 4, replicas: 3, updated: 3,
			rolling: false,
		},
		{
			name:        "statefulset mid-roll",
			statefulSet: true,
			generation:  4, observed: 4, replicas: 3, updated: 1,
			rolling: true,
		},
		{
			name:      "no gateway to read the roll from",
			noGateway: true,
			rolling:   true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := hostPathAgent()
			gatewayMeta := metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace, Generation: tc.generation}
			var gateway client.Object = &appsv1.Deployment{
				ObjectMeta: gatewayMeta,
				Status: appsv1.DeploymentStatus{
					ObservedGeneration: tc.observed,
					Replicas:           tc.replicas,
					UpdatedReplicas:    tc.updated,
					ReadyReplicas:      tc.replicas,
				},
			}
			if tc.statefulSet {
				agent.Spec.Deployment.Availability = &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(tc.replicas)}
				agent.Spec.Deployment.Storages = []agentv1alpha1.StorageSpec{{
					Name:        "gateway-data",
					MountPath:   "/srv/gateway-data",
					AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
				}}
				if !useStatefulSet(agent) {
					t.Fatalf("this case is meant to take the StatefulSet path and does not")
				}
				gateway = &appsv1.StatefulSet{
					ObjectMeta: gatewayMeta,
					Status: appsv1.StatefulSetStatus{
						ObservedGeneration: tc.observed,
						Replicas:           tc.replicas,
						UpdatedReplicas:    tc.updated,
						ReadyReplicas:      tc.replicas,
					},
				}
			}

			scheme := setupScheme()
			builder := fake.NewClientBuilder().
				WithScheme(scheme).
				WithStatusSubresource(agent).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors())
			if tc.noGateway {
				builder = builder.WithObjects(agent)
			} else {
				builder = builder.WithObjects(agent, gateway)
			}
			cl := builder.Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
			ctx := context.Background()

			if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", workloadRendered); err != nil {
				t.Fatalf("updateStatusDegraded failed: %v", err)
			}
			cond := meta.FindStatusCondition(agent.Status.Conditions, hostPathDroppedConditionType)
			if cond == nil {
				t.Fatalf("no %s condition on a CR parked Degraded after a pass that rendered; conditions: %+v", hostPathDroppedConditionType, agent.Status.Conditions)
			}
			if got := strings.Contains(cond.Message, hostPathDroppedRollingClause); got != tc.rolling {
				t.Errorf("rollout clause present = %v, want %v, at generation %d with observedGeneration %d and %d of %d replicas updated: %s",
					got, tc.rolling, tc.generation, tc.observed, tc.updated, tc.replicas, cond.Message)
			}

			// Whatever the clause says, it has to say the same thing twice:
			// the roll state rides in the message the unchanged comparison
			// covers, and a comparison that recomputed it differently would
			// write status on every 30s requeue of a parked CR (#1392).
			stored := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			settledVersion := stored.ResourceVersion
			if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", workloadRendered); err != nil {
				t.Fatalf("updateStatusDegraded (settled) failed: %v", err)
			}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
				t.Fatalf("reading the PlatformAgent back: %v", err)
			}
			if stored.ResourceVersion != settledVersion {
				t.Errorf("a second parked pass over unchanged state wrote the CR (resourceVersion %s -> %s); the %s message and the comparison disagree",
					settledVersion, stored.ResourceVersion, hostPathDroppedConditionType)
			}
		})
	}
}

// hostPathFloodDeploymentSpec is a spec.deployment carrying count hostPath
// volumes whose names and paths are long enough that listing them all would
// run past the 32768 characters the CRD schema allows a condition message.
// Author-chosen strings, both of them, and nothing bounds either.
func hostPathFloodDeploymentSpec(count, nameLen int) *agentv1alpha1.DeploymentSpec {
	spec := &agentv1alpha1.DeploymentSpec{}
	for i := 0; i < count; i++ {
		// Distinct names: extraVolumes is a list-map keyed on name, so the API
		// server refuses a CR that repeats one.
		suffix := "-" + strconv.Itoa(i)
		spec.ExtraVolumes = append(spec.ExtraVolumes, corev1.Volume{
			Name: strings.Repeat("v", nameLen) + suffix,
			VolumeSource: corev1.VolumeSource{
				HostPath: &corev1.HostPathVolumeSource{Path: "/" + strings.Repeat("p", nameLen) + suffix},
			},
		})
	}
	return spec
}

func TestTheDroppedVolumeMessageStaysUnderTheConditionCap(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Deployment = hostPathFloodDeploymentSpec(floodedHostPathCount, floodedHostPathNameLen)
	// rolloutIncomplete because it is the longer of the two forms: the cap has
	// to hold for the message as it is at its widest.
	msg := hostPathDroppedMessage(agent, rolloutIncomplete)

	if len(msg) > conditionMessageMaxLength {
		t.Errorf("message is %d characters, over the %d the CRD schema allows: every status write on this CR fails, not just this condition", len(msg), conditionMessageMaxLength)
	}
	if !strings.Contains(msg, "spec.deployment.extraVolumes[0]") {
		t.Errorf("message does not name the first entry, which is the one the author has to find: %s", msg)
	}
	// Every entry it did not list has to be accounted for, or the message is
	// a shorter lie rather than a shorter report.
	listed := strings.Count(msg, hostPathExtraVolumesField+"[")
	match := hostPathOverflowCountPattern.FindStringSubmatch(msg)
	if match == nil {
		t.Fatalf("message lists %d of %d entries and does not say how many it left out: %s", listed, floodedHostPathCount, msg)
	}
	rest, err := strconv.Atoi(match[1])
	if err != nil {
		t.Fatalf("unreadable overflow count %q: %v", match[1], err)
	}
	if listed+rest != floodedHostPathCount {
		t.Errorf("message lists %d entries and counts %d more, which is %d of %d", listed, rest, listed+rest, floodedHostPathCount)
	}
	t.Logf("%d entries of %d characters each rendered a %d-character message listing %d of them", floodedHostPathCount, floodedHostPathNameLen, len(msg), listed)
}

// One entry can be longer on its own than the message may be, so the budget
// has to cut inside an entry rather than only between entries.
func TestASingleOversizedHostPathEntryIsTruncated(t *testing.T) {
	agent := brokerPodAgent()
	agent.Spec.Deployment = hostPathFloodDeploymentSpec(1, 64*1024)
	msg := hostPathDroppedMessage(agent, rolloutIncomplete)

	if len(msg) > conditionMessageMaxLength {
		t.Errorf("message is %d characters, over the %d the CRD schema allows", len(msg), conditionMessageMaxLength)
	}
	if !strings.Contains(msg, "spec.deployment.extraVolumes[0]") {
		t.Errorf("a truncated message still has to name the field the entry is on: %s", msg)
	}
	if !strings.Contains(msg, hostPathDroppedEntryEllipsis) {
		t.Errorf("a truncated entry is not marked as truncated: %s", msg)
	}
}

// TestAPreRenderRefusalWritesNoVolumesDroppedCondition is the other side of
// the Degraded-path write. Four refusals return before reconcileWorkload —
// ForbiddenVolumeMount, ShellSandboxCannotBeDisabled, RuntimeClassNotFound and
// EgressAllowlistRefused — and on those passes no Pod is rendered, so the
// running workload is whatever the previous pass left. Writing the condition
// there would report a security property of a Pod this operator never wrote:
// on an install rolled forward over a CR whose Deployment a pre-fix render
// gave real hostPath mounts, `kubectl describe` would say the mounts are gone
// while they are still mounted, on every 30s requeue for as long as the
// refusal stands.
//
// The refusal used here is the one that needs no cluster state to provoke.
func TestAPreRenderRefusalWritesNoVolumesDroppedCondition(t *testing.T) {
	agent := hostPathAgent()
	agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
		ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
	}
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	for pass := 0; pass < 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("a refusal is a Degraded status, not a reconcile error (pass %d): %v", pass, err)
		}
	}

	// Nothing rendered, which is what makes this the case under test rather
	// than a variant of the Degraded case above.
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + "-gateway", Namespace: agent.Namespace}}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(dep), dep); !apierrors.IsNotFound(err) {
		t.Fatalf("the refusal rendered the gateway Deployment (err %v), so this is no longer a pre-render pass", err)
	}

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, "Ready")
	if got.Status.Phase != "Degraded" || ready == nil || ready.Reason != reasonShellSandboxCannotBeDisabled {
		t.Fatalf("phase=%q Ready=%+v, want Degraded/%s; the pass under test is the one that parks there", got.Status.Phase, ready, reasonShellSandboxCannotBeDisabled)
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType); cond != nil {
		t.Errorf("%s written on a pass that rendered no Pod: the CR now asserts the hostPath entries are out of a workload this operator never wrote: %+v", hostPathDroppedConditionType, cond)
	}
}

// TestAPreRenderRefusalLeavesAnAlreadyPresentVolumesDroppedInPlace pins the
// other half of the gate, which is not symmetric with it. A condition already
// on the CR was written by a pass that did render, and the Pod that pass wrote
// is still the one running — so a pre-render refusal leaves it exactly as it
// stands rather than clearing or refreshing it.
//
// The discriminator is a spec edit that takes the hostPath entries away at the
// same time as it provokes the refusal. Recomputing the condition there would
// remove it, and the CR would stop reporting volumes that are genuinely absent
// from the running Pod. Left in place it is stale in its wording — it names
// entries the spec no longer carries — and correct in what it asserts, which
// is the direction this condition has to err in: over-reporting a drop that
// happened, never claiming one that did not. The next pass to reach the render
// refreshes or removes it.
func TestAPreRenderRefusalLeavesAnAlreadyPresentVolumesDroppedInPlace(t *testing.T) {
	agent := hostPathAgent()
	r, cl := newSplitReconciler(t, agent)
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	for pass := 0; pass < 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile pass %d failed: %v", pass, err)
		}
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	rendered := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType)
	if rendered == nil {
		t.Fatalf("the rendering passes wrote no %s condition, so there is nothing for the refusal to preserve", hostPathDroppedConditionType)
	}
	renderedMessage := rendered.Message

	// Switch the sandbox off and take the hostPath entries out in one edit.
	got.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
		ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Enabled: ptr.To(false)},
	}
	got.Spec.Deployment.ExtraVolumes = got.Spec.Deployment.ExtraVolumes[1:]
	got.Spec.Deployment.ExtraVolumeMounts = got.Spec.Deployment.ExtraVolumeMounts[1:]
	got.Spec.Deployment.SidecarVolumes = got.Spec.Deployment.SidecarVolumes[1:]
	got.Spec.Deployment.Sidecars[0].VolumeMounts = got.Spec.Deployment.Sidecars[0].VolumeMounts[1:]
	got.Spec.Deployment.InitContainers[0].VolumeMounts = nil
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("switching the sandbox off and removing the hostPath entries: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("a refusal is a Degraded status, not a reconcile error: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	ready := meta.FindStatusCondition(got.Status.Conditions, "Ready")
	if got.Status.Phase != "Degraded" || ready == nil || ready.Reason != reasonShellSandboxCannotBeDisabled {
		t.Fatalf("phase=%q Ready=%+v, want Degraded/%s", got.Status.Phase, ready, reasonShellSandboxCannotBeDisabled)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, hostPathDroppedConditionType)
	if cond == nil {
		t.Fatalf("the refusal cleared a %s condition a rendering pass had written; the Pod that pass rendered is still the one running without those volumes", hostPathDroppedConditionType)
	}
	if cond.Status != metav1.ConditionTrue || cond.Reason != hostPathDroppedReason || cond.Message != renderedMessage {
		t.Errorf("the refusal rewrote the condition instead of leaving it: got %s/%s %q, want it untouched at %s/%s %q", cond.Status, cond.Reason, cond.Message, metav1.ConditionTrue, hostPathDroppedReason, renderedMessage)
	}

	// And a parked CR still writes once per change, not once per pass. The
	// condition is out of the Degraded writer's comparison on a pre-render
	// pass, because a term no write can satisfy would make every requeue tick
	// a status write (#1392).
	settledVersion := got.ResourceVersion
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile (settled) failed: %v", err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatalf("reading the PlatformAgent back: %v", err)
	}
	if got.ResourceVersion != settledVersion {
		t.Errorf("a parked pre-render pass with nothing to change wrote the CR (resourceVersion %s -> %s)", settledVersion, got.ResourceVersion)
	}
}
