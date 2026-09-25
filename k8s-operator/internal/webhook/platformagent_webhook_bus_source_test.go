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

package webhook

import (
	"context"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The source half of the bus-token reservation at admission. The name half
// (validateReservedVolumeName) refuses a volume CALLED a2a-bus-token and
// nothing else, so a projected serviceAccountToken for the bus audience under
// any other name, or a mount of one of the Secrets the operator renders with
// bus credentials in them, was admitted. Each refusal has to land on the field that matched and name
// the audience or the Secret, because a bare "forbidden" on the volume leaves
// the author guessing which of the volume's sources did it.
//
// Literals rather than the api package's constants for the audience and the
// Secret name, so this file compiles against a tree that has neither and
// fails there on behaviour rather than on a missing symbol.
func TestBusCredentialSourcesAreRefusedOnBothVolumeLists(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{}
	const agentName = "platform-agent"
	const credsSecret = "platform-agent-a2a-nats-creds"

	tokenFor := func(aud string) corev1.VolumeProjection {
		return corev1.VolumeProjection{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{Audience: aud, Path: "token"}}
	}
	projected := func(sources ...corev1.VolumeProjection) corev1.VolumeSource {
		return corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{Sources: sources}}
	}
	secretVolume := func(name string) corev1.VolumeSource {
		return corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: name}}
	}
	agentWith := func(list string, vol corev1.Volume) *agentv1alpha1.PlatformAgent {
		dep := &agentv1alpha1.DeploymentSpec{}
		switch list {
		case "extraVolumes":
			dep.ExtraVolumes = []corev1.Volume{vol}
		case "sidecarVolumes":
			// A neutral entry first, so the index in the path is exercised.
			dep.SidecarVolumes = []corev1.Volume{
				{Name: "scratch", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				vol,
			}
		default:
			t.Fatalf("unknown list %q", list)
		}
		return &agentv1alpha1.PlatformAgent{
			ObjectMeta: metav1.ObjectMeta{Name: agentName, Namespace: "kubeagents-system"},
			Spec:       agentv1alpha1.PlatformAgentSpec{AgentSpec: agentv1alpha1.AgentSpec{Deployment: dep}},
		}
	}

	refused := []struct {
		name     string
		vol      corev1.Volume
		field    string // below the list element
		mentions string
	}{
		{"bus audience under another name", corev1.Volume{Name: "innocuous-cache", VolumeSource: projected(tokenFor("a2a-bus"))},
			".projected.sources[0].serviceAccountToken.audience", `"a2a-bus"`},
		{"bus audience as the second source", corev1.Volume{Name: "bundle", VolumeSource: projected(tokenFor("vault"), tokenFor("a2a-bus"))},
			".projected.sources[1].serviceAccountToken.audience", `"a2a-bus"`},
		{"the creds Secret as a secret volume", corev1.Volume{Name: "cache", VolumeSource: secretVolume(credsSecret)},
			".secret.secretName", `"` + credsSecret + `"`},
		{"the nats.conf Secret as a secret volume", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-a2a-nats-config")},
			".secret.secretName", `"platform-agent-a2a-nats-config"`},
		{"the callout keys Secret as a secret volume", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-a2a-callout-keys")},
			".secret.secretName", `"platform-agent-a2a-callout-keys"`},
		{"the creds Secret as a projected source", corev1.Volume{Name: "bundle", VolumeSource: projected(
			corev1.VolumeProjection{Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: credsSecret}}})},
			".projected.sources[0].secret.name", `"` + credsSecret + `"`},
	}
	for _, list := range []string{"extraVolumes", "sidecarVolumes"} {
		index := "[0]"
		if list == "sidecarVolumes" {
			index = "[1]"
		}
		for _, tc := range refused {
			t.Run(list+"/"+tc.name, func(t *testing.T) {
				_, err := val.ValidateCreate(ctx, agentWith(list, tc.vol))
				wantField := "spec.deployment." + list + index + tc.field
				assertFieldError(t, err, wantField)
				statusErr, ok := err.(*apierrors.StatusError)
				if !ok {
					t.Fatalf("expected *apierrors.StatusError, got %T: %v", err, err)
				}
				for _, cause := range statusErr.ErrStatus.Details.Causes {
					if cause.Field != wantField {
						continue
					}
					if !strings.Contains(cause.Message, tc.mentions) {
						t.Errorf("the refusal on %s does not name %s; the author cannot tell which source did it: %q", wantField, tc.mentions, cause.Message)
					}
					if strings.Contains(cause.Message, "reserved") {
						t.Errorf("the refusal on %s reads as the name reservation; this volume is not called a2a-bus-token: %q", wantField, cause.Message)
					}
				}
			})
		}
	}

	// Not too wide. A serviceAccountToken projection is a real capability
	// (issue #1667 rules out refusing them all as A2), and a Secret volume is
	// how a sidecar gets its own credential.
	accepted := []struct {
		name string
		vol  corev1.Volume
	}{
		{"a projection for another audience", corev1.Volume{Name: "vault-token", VolumeSource: projected(tokenFor("vault"))}},
		{"a projection for the API server's own audience", corev1.Volume{Name: "sa-token", VolumeSource: projected(tokenFor(""))}},
		{"another Secret", corev1.Volume{Name: "tls", VolumeSource: secretVolume("my-tls")}},
		{"another agent's creds Secret", corev1.Volume{Name: "cache", VolumeSource: secretVolume("other-agent-a2a-nats-creds")}},
		{"a projected source for another Secret", corev1.Volume{Name: "bundle", VolumeSource: projected(
			corev1.VolumeProjection{Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: "my-tls"}}})}},
	}
	for _, list := range []string{"extraVolumes", "sidecarVolumes"} {
		for _, tc := range accepted {
			t.Run(list+"/accepts "+tc.name, func(t *testing.T) {
				if _, err := val.ValidateCreate(ctx, agentWith(list, tc.vol)); err != nil {
					t.Errorf("a volume that cannot deliver the bus credential was refused: %v", err)
				}
			})
		}
	}
}
