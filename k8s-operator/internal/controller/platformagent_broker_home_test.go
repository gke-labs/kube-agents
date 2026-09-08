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

// TestTheBrokerSubprocessHomeIsPodLocal is the whole point of this file.
//
// The command policy closed `--kuberc`, and it then turned out that kubectl
// honours $HOME/.kube/kuberc with no flag present. That default path is closed
// by `KUBECTL_KUBERC=false` in the executor environment (asserted in
// test_credential_proxy.py) and, underneath it, by the broker's HOME living on
// a volume nothing else can write. The second half was accidental: nothing
// asserted it, so a plausible rearrangement of the mounts — pointing
// CREDENTIAL_PROXY_STATE_DIR at a shared claim so the kubeconfig cache survives
// a restart, say — would have removed it silently. This is that assertion.
//
// It checks which volume backs the path rather than comparing paths as strings.
// Container mounts shadow, so the volume is the fact and the path arithmetic is
// not.
//
// The broker having a Pod of its own makes this cheaper to state than it was
// when it was a sidecar: there is no shared filesystem left to be off, so the
// property is simply that every private path is backed by a Pod-local emptyDir
// and no persistent claim is mounted at all.
func TestTheBrokerSubprocessHomeIsPodLocal(t *testing.T) {
	agent := brokerPodAgent()
	spec := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec

	container, found := findContainer(spec, "envoy-credential-proxy")
	if !found {
		t.Fatal("no envoy-credential-proxy container in the broker Pod: the layout moved and this test is now asserting nothing")
	}
	broker := &container
	stateDir, found := brokerEnvValue(broker.Env, "CREDENTIAL_PROXY_STATE_DIR")
	if !found {
		t.Fatal("the broker has no CREDENTIAL_PROXY_STATE_DIR, so its HOME is wherever the image's default is")
	}
	// credential_proxy.py: home_dir = state_dir / "home", and HOME is set to it
	// for every proxied subprocess.
	subprocessHome := path.Join(stateDir, "home")

	homeMount := mountCovering(broker, subprocessHome)
	if homeMount == nil {
		t.Fatalf("nothing mounts %s: the subprocess HOME is on the container's writable layer", subprocessHome)
	}
	if homeMount.Name != "credential-proxy-state" {
		t.Errorf("the subprocess HOME %s comes from volume %q, not the broker's private state volume", subprocessHome, homeMount.Name)
	}

	// No persistent claim on this Pod at all. A claim is the one volume kind
	// the agent Pod also mounts, and on a shared filesystem it would be the
	// same bytes — which is the arrangement the separate Pod exists to end.
	for _, volume := range spec.Volumes {
		if volume.PersistentVolumeClaim != nil {
			t.Errorf("volume %s is a PersistentVolumeClaim: the broker Pod shares no filesystem with anything", volume.Name)
		}
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
			t.Errorf("volume %s (%s) is no longer a Pod-local emptyDir: %+v", name, role, volume.VolumeSource)
		}
	}
}
