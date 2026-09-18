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
	"slices"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// busSourceEnvtestNamespace is the CR under test's namespace; the name is
// envtestAgentName (test-agent), which is what the fixture's Secret name
// test-agent-a2a-nats-creds is built from.
const busSourceEnvtestNamespace = "bus-source-strip"

// TestABusCredentialSourceStaysOutOfTheDeploymentWithoutTheWebhookEnvtest is
// the chart-default install against a real API server: the operator's CRDs
// and no admission webhook. The CR is `mode: next` and carries, on a sidecar,
// a projected serviceAccountToken for the bus audience under an innocent
// name and a mount of the credentials Secret. Both are admitted unchallenged,
// and the assertion is on what the controller then wrote: a gateway
// Deployment the API server accepted, with neither volume, neither mount,
// and the operator's own a2a-bus-token projection still on platform-agent.
// Against a controller without the fix the same Deployment carries both,
// which is the issue's "renders intact" measured on an API server rather
// than on a struct.
func TestABusCredentialSourceStaysOutOfTheDeploymentWithoutTheWebhookEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()

	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: busSourceEnvtestNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: busSourceEnvtestNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Mode: ptr.To("next"),
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
			AgentSpec: agentv1alpha1.AgentSpec{Deployment: &agentv1alpha1.DeploymentSpec{
				Sidecars: []corev1.Container{{
					Name: "hermes-bridge", Image: "bridge:dev",
					VolumeMounts: []corev1.VolumeMount{
						{Name: busSourceStolenProjection, MountPath: "/var/run/secrets/a2a-bus", ReadOnly: true},
						{Name: busSourceStolenSecret, MountPath: "/var/run/secrets/creds", ReadOnly: true},
						{Name: busSourceKeptProjection, MountPath: "/var/run/secrets/vault", ReadOnly: true},
					},
				}},
				SidecarVolumes: []corev1.Volume{
					{Name: busSourceStolenProjection, VolumeSource: busSourceTokenProjection("a2a-bus")},
					{Name: busSourceStolenSecret, VolumeSource: busSourceSecretVolume(busSourceCredsSecretName)},
					{Name: busSourceKeptProjection, VolumeSource: busSourceTokenProjection("vault")},
				},
			}},
		},
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating the PlatformAgent (the API server should admit it; envtest runs no webhook): %v", err)
	}
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}

	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	// The first pass adds the finalizer and returns; the second renders. The
	// workload is applied before the A2A stack is reconciled, so an error out
	// of the bus provisioning (no kubelet runs its Job here) is logged and the
	// Deployment is still what the assertions read.
	for _, pass := range []string{"finalizer", "render"} {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Logf("Reconcile (%s) returned %v; reading the Deployment the pass applied before it", pass, err)
		}
	}

	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, client.ObjectKey{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("the API server holds no gateway Deployment after the render pass: %v", err)
	}
	pod := dep.Spec.Template.Spec
	holders := busSourceHolders(pod)

	for _, v := range pod.Volumes {
		switch {
		case v.Name == busSourceStolenProjection:
			t.Errorf("the accepted Deployment carries volume %q projecting audience %q", v.Name, v.Projected.Sources[0].ServiceAccountToken.Audience)
		case v.Name == busSourceStolenSecret:
			t.Errorf("the accepted Deployment carries volume %q mounting Secret %q", v.Name, v.Secret.SecretName)
		}
	}
	for _, name := range []string{busSourceStolenProjection, busSourceStolenSecret} {
		if got := holders[name]; len(got) != 0 {
			t.Errorf("containers %v in the accepted Deployment mount %q", got, name)
		}
	}

	// The operator's own volume is the control: still there, still the
	// projection, still on platform-agent alone.
	i := slices.IndexFunc(pod.Volumes, func(v corev1.Volume) bool { return v.Name == a2aBusTokenVolume })
	if i < 0 {
		t.Fatalf("the accepted Deployment has no %s volume; the strip reached the operator's own projection", a2aBusTokenVolume)
	}
	if own := pod.Volumes[i]; own.Projected == nil || own.Projected.Sources[0].ServiceAccountToken == nil ||
		own.Projected.Sources[0].ServiceAccountToken.Audience != "a2a-bus" {
		t.Errorf("the operator's %s volume is not the a2a-bus token projection: %+v", a2aBusTokenVolume, own)
	}
	if got := holders[a2aBusTokenVolume]; len(got) != 1 || got[0] != "platform-agent" {
		t.Errorf("%s is mounted by %v, want exactly [platform-agent]", a2aBusTokenVolume, got)
	}
	// And the innocent projection beside them is the author's.
	if !slices.Contains(holders[busSourceKeptProjection], "hermes-bridge") {
		t.Errorf("the sidecar lost its %q mount (holders %v); the strip is wider than the two routes", busSourceKeptProjection, holders[busSourceKeptProjection])
	}

	// No dangling mounts anywhere: the API server accepted this Deployment,
	// but say so explicitly for the transcript.
	declared := map[string]bool{}
	for _, v := range pod.Volumes {
		declared[v.Name] = true
	}
	for name, cs := range holders {
		if !declared[name] {
			t.Errorf("containers %v mount %q, which the pod does not declare", cs, name)
		}
	}
	stolen := 0
	for _, name := range []string{busSourceStolenProjection, busSourceStolenSecret} {
		if declared[name] {
			stolen++
		}
	}
	t.Logf("volumes=%d bus-credential user volumes in Deployment=%d operator's %s mounted by %v",
		len(pod.Volumes), stolen, a2aBusTokenVolume, holders[a2aBusTokenVolume])
}
