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
	"path"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
)

// The broker's private volumes and why each one has to stay private.
//
//	credential-proxy-state   $HOME for every proxied subprocess. kubectl reads
//	                         $HOME/.kube/kuberc with no flag at all, and a kuberc
//	                         can set `as`, so a writable HOME is caller-supplied
//	                         impersonation past an argv the policy found nothing
//	                         to refuse. See credential_proxy.py's environment.
//	credential-proxy-runtime The backend Unix socket. Reaching that socket is
//	                         reaching the credentials, past Envoy and past the
//	                         whole command policy; the 0600 mode on it assumes
//	                         nothing else has the directory.
var brokerPrivateVolumes = map[string]string{
	"credential-proxy-state":   "the proxied subprocess HOME",
	"credential-proxy-runtime": "the backend socket directory",
}

// Where each of those has to land in the broker container.
//
// The paths are pinned as literals, not derived, because
// tests/e2e/operator/credential_isolation_e2e_test.py hard-codes them to assert
// that the AGENT container mounts neither. An "X is not mounted" check goes
// trivially true when X moves, and passes — so if these paths change without
// that file changing, this test is the thing that says so. /var/lib is already
// pinned at platformagent_manifests_test.go:611 and :974; /var/run was pinned
// nowhere until here.
var brokerPrivateMountPaths = map[string]string{
	"credential-proxy-state":   "/var/lib/credential-proxy",
	"credential-proxy-runtime": "/var/run/credential-proxy",
}

// pathIsWithin reports whether child is at or below parent. Both are absolute
// container paths, so `path` rather than `filepath`: the operator renders Linux
// paths whatever the host running the test is.
func pathIsWithin(child, parent string) bool {
	child, parent = path.Clean(child), path.Clean(parent)
	return child == parent || strings.HasPrefix(child, strings.TrimSuffix(parent, "/")+"/")
}

// mountCovering returns the VolumeMount that supplies target inside container:
// the one whose MountPath is the longest ancestor of target. Longest wins
// because that is what the kubelet does — a mount nested inside another shadows
// it — and picking the shortest would report the PVC for a path the emptyDir
// actually backs.
func mountCovering(container *corev1.Container, target string) *corev1.VolumeMount {
	var best *corev1.VolumeMount
	for index := range container.VolumeMounts {
		mount := &container.VolumeMounts[index]
		if !pathIsWithin(target, mount.MountPath) {
			continue
		}
		if best == nil || len(path.Clean(mount.MountPath)) > len(path.Clean(best.MountPath)) {
			best = mount
		}
	}
	return best
}

func volumeNamed(volumes []corev1.Volume, name string) *corev1.Volume {
	for index := range volumes {
		if volumes[index].Name == name {
			return &volumes[index]
		}
	}
	return nil
}

// assertBrokerHomeIsOffTheSharedWorkspace is the whole point of this file, run
// against both layouts.
//
// The command policy closed `--kuberc`, and it then turned out that kubectl
// honours $HOME/.kube/kuberc with no flag present. That default path is closed
// by `KUBECTL_KUBERC=false` in the executor environment (asserted in
// test_credential_proxy.py) and, underneath it, by the broker's HOME living on
// a volume the agent cannot write. The second half was accidental: nothing
// asserted it, so a plausible rearrangement of the mounts — pointing
// CREDENTIAL_PROXY_STATE_DIR at the PVC so the kubeconfig cache survives a
// restart, say, or mounting the state volume into the agent for debugging —
// would have removed it silently. This is that assertion.
//
// It deliberately checks which volume backs the path rather than comparing the
// state directory and the workspace root as strings. Container mounts shadow,
// so a state directory lexically inside the workspace can still be backed by
// the emptyDir; the volume is the fact, the path arithmetic is not. Found by
// mutation: moving CREDENTIAL_PROXY_STATE_DIR and its mount to
// /opt/data/credential-proxy leaves this green, and correctly so — the emptyDir
// still shadows that path inside the broker container, so the agent's writes
// underneath the PVC are invisible to the broker.
//
// That layout is not harmless, though, and the reason is worth keeping here
// because it is not what this test guards. With the state dir under the
// workspace root, the broker's own HOME becomes lexically inside the
// _within_workspace containment root (credential_proxy.py), so an
// agent-supplied cwd could name it and a proxied git could run there. Nothing
// does this today and nothing proposes to; it is a reason not to, not a bug.
func assertBrokerHomeIsOffTheSharedWorkspace(t *testing.T, spec corev1.PodSpec, brokerContainer string) {
	t.Helper()

	container, found := findContainer(spec, brokerContainer)
	if !found {
		t.Fatalf("no %s container in this Pod: the layout moved and this test is now asserting nothing", brokerContainer)
	}
	broker := &container
	stateDir, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_STATE_DIR")
	if !found {
		t.Fatal("the broker has no CREDENTIAL_PROXY_STATE_DIR, so its HOME is wherever the image's default is")
	}
	workspaceRoot, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_WORKSPACE_ROOT")
	if !found {
		t.Fatal("the broker has no CREDENTIAL_PROXY_WORKSPACE_ROOT, so there is no shared workspace to be off")
	}
	// credential_proxy.py: home_dir = state_dir / "home", and HOME is set to it
	// for every proxied subprocess.
	subprocessHome := path.Join(stateDir, "home")

	homeMount := mountCovering(broker, subprocessHome)
	if homeMount == nil {
		t.Fatalf("nothing mounts %s in %s: the subprocess HOME is on the container's writable layer", subprocessHome, brokerContainer)
	}
	if homeMount.Name != "credential-proxy-state" {
		t.Errorf("the subprocess HOME %s comes from volume %q, not the broker's private state volume", subprocessHome, homeMount.Name)
	}

	// The workspace is the shared PVC by construction; assert that rather than
	// assume it, or the comparison below is between two unknowns.
	workspaceMount := mountCovering(broker, workspaceRoot)
	if workspaceMount == nil {
		t.Fatalf("nothing mounts the workspace root %s in %s", workspaceRoot, brokerContainer)
	}
	if workspaceVolume := volumeNamed(spec.Volumes, workspaceMount.Name); workspaceVolume == nil ||
		workspaceVolume.PersistentVolumeClaim == nil {
		t.Fatalf("the workspace root %s is not backed by the shared PVC (volume %q); this test no longer knows what is shared",
			workspaceRoot, workspaceMount.Name)
	}
	if homeMount.Name == workspaceMount.Name {
		t.Errorf("the subprocess HOME and the shared workspace are the same volume %q: the agent can write $HOME/.kube/kuberc",
			homeMount.Name)
	}

	for name, role := range brokerPrivateVolumes {
		volume := volumeNamed(spec.Volumes, name)
		if volume == nil {
			t.Errorf("volume %s (%s) is not on this Pod", name, role)
			continue
		}
		if wantPath := brokerPrivateMountPaths[name]; mountCovering(broker, wantPath) == nil ||
			mountCovering(broker, wantPath).Name != name {
			t.Errorf("volume %s (%s) no longer supplies %s in the broker; the e2e's "+
				"\"the agent does not mount %s\" check would now pass trivially",
				name, role, wantPath, wantPath)
		}
		if volume.EmptyDir == nil {
			// A PVC here is the specific regression: it is the one volume kind
			// the agent Pod also mounts, and on a shared filesystem it would be
			// the same bytes.
			t.Errorf("volume %s (%s) is no longer a Pod-local emptyDir: %+v", name, role, volume.VolumeSource)
		}
		for index := range spec.Containers {
			other := &spec.Containers[index]
			if other.Name == brokerContainer {
				continue
			}
			for _, mount := range other.VolumeMounts {
				if mount.Name == name {
					t.Errorf("container %s mounts %s (%s); it is meant to be reachable from the broker only",
						other.Name, name, role)
				}
			}
		}
	}
}

// TestTheBrokerSubprocessHomeIsNotOnTheSharedWorkspace, sidecar layout — the
// default install, where agent and broker are containers in one Pod and the
// only thing between them is which volumes each mounts.
func TestTheBrokerSubprocessHomeIsNotOnTheSharedWorkspace(t *testing.T) {
	pod := buildPodTemplateSpec(splitBrokerAgent(false), "c", "f", "s", "p", nil, renderOptions{})
	assertBrokerHomeIsOffTheSharedWorkspace(t, pod.Spec, "envoy-credential-proxy")
}

// TestTheSplitBrokerSubprocessHomeIsNotOnTheSharedWorkspace, split layout. The
// broker Pod mounts the same PVC, so moving the state directory onto it is if
// anything more tempting here: the broker is the only writer of its own
// kubeconfig cache and a PVC would make it survive a restart.
func TestTheSplitBrokerSubprocessHomeIsNotOnTheSharedWorkspace(t *testing.T) {
	deployment := buildCredentialBrokerDeployment(splitBrokerAgent(true), "policy-hash", defaultAgentHome)
	assertBrokerHomeIsOffTheSharedWorkspace(t, deployment.Spec.Template.Spec, "credential-broker")
}
