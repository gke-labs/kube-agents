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

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// In its own file because it names the helpers the fix adds; the pod-level
// test beside it compiles against a tree without them.

// TestTheNameHalfAndTheSourceHalfAreTwoDifferentChecks pins that the by-source
// strip is what does the work here and not the by-name one: with the reserved
// name nowhere in the CR, the name-keyed helpers return their input unchanged
// and the source-keyed ones do the dropping. Measured on the helpers rather
// than the pod, so a regression that re-keyed the source strip on the name
// fails here with the helper named.
func TestTheNameHalfAndTheSourceHalfAreTwoDifferentChecks(t *testing.T) {
	agent := a2aTestAgent()
	agent.Spec.Deployment = busSourceDeploymentSpec()
	for _, list := range [][]corev1.Volume{agent.Spec.Deployment.SidecarVolumes, agent.Spec.Deployment.ExtraVolumes} {
		if got := a2aStripBusTokenVolume(list); len(got) != len(list) {
			t.Errorf("the name strip dropped %d of %d volumes with no reserved name present", len(list)-len(got), len(list))
		}
	}
	dropped := a2aBusCredentialVolumeNames(agent)
	want := map[string]bool{busSourceStolenProjection: true, busSourceStolenSecret: true, busSourceStolenBundle: true}
	if len(dropped) != len(want) {
		t.Fatalf("a2aBusCredentialVolumeNames = %v, want %v", dropped, want)
	}
	for name := range want {
		if !dropped[name] {
			t.Errorf("a2aBusCredentialVolumeNames misses %q", name)
		}
	}
	if kept := a2aStripBusCredentialSources(agent.Spec.Deployment.SidecarVolumes, agent.Name); len(kept) != 1 || kept[0].Name != busSourceKeptProjection {
		t.Errorf("sidecarVolumes after the source strip = %v, want only %s", kept, busSourceKeptProjection)
	}
	if kept := a2aStripBusCredentialSources(agent.Spec.Deployment.ExtraVolumes, agent.Name); len(kept) != 1 || kept[0].Name != busSourceKeptSecret {
		t.Errorf("extraVolumes after the source strip = %v, want only %s", kept, busSourceKeptSecret)
	}
	// Empty set, unchanged input: a clean CR renders the bytes it always did.
	clean := a2aTestAgent()
	clean.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Sidecars: busSourceDeploymentSpec().Sidecars}
	if got := a2aBusCredentialVolumeNames(clean); got != nil {
		t.Errorf("a CR with no user volumes drops %v", got)
	}
	in := clean.Spec.Deployment.Sidecars
	if out := stripContainerMountsNamed(in, nil); &out[0] != &in[0] {
		t.Error("stripContainerMountsNamed copied the containers with nothing to drop; a clean CR should render its own slices")
	}
}
