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

// A CR may not mount a Secret the operator writes inference gateway caller
// keys into: a container holding another class's key would be admitted by the
// gateway as that class. Every class's key Secret and the caller-key map are
// refused, as a secret volume and as a projected source, on both volume lists;
// another agent's key Secrets and unrelated Secrets are not.
//
// Literal Secret names, so a rename of the operator's names fails here on
// behaviour rather than compiling past it.
func TestInferenceKeySecretsAreRefusedOnBothVolumeLists(t *testing.T) {
	ctx := context.Background()
	val := &PlatformAgentCustomValidator{}
	const agentName = "platform-agent"

	secretVolume := func(name string) corev1.VolumeSource {
		return corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: name}}
	}
	projectedSecret := func(names ...string) corev1.VolumeSource {
		var sources []corev1.VolumeProjection
		for _, n := range names {
			sources = append(sources, corev1.VolumeProjection{Secret: &corev1.SecretProjection{LocalObjectReference: corev1.LocalObjectReference{Name: n}}})
		}
		return corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{Sources: sources}}
	}
	agentWith := func(list string, vol corev1.Volume) *agentv1alpha1.PlatformAgent {
		dep := &agentv1alpha1.DeploymentSpec{}
		switch list {
		case "extraVolumes":
			dep.ExtraVolumes = []corev1.Volume{vol}
		case "sidecarVolumes":
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
		name   string
		vol    corev1.Volume
		field  string
		secret string
	}{
		{"the agent key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-agent")},
			".secret.secretName", "platform-agent-inference-key-agent"},
		{"the session key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-a2a-session")},
			".secret.secretName", "platform-agent-inference-key-a2a-session"},
		{"the curator key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-curator")},
			".secret.secretName", "platform-agent-inference-key-curator"},
		{"the probe key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-gateway-probe")},
			".secret.secretName", "platform-agent-inference-key-gateway-probe"},
		{"a tier key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-tier-unattended")},
			".secret.secretName", "platform-agent-inference-key-tier-unattended"},
		{"the caller-key map", corev1.Volume{Name: "cache", VolumeSource: secretVolume("litellm-caller-keys")},
			".secret.secretName", "litellm-caller-keys"},
		{"a key as a second projected source", corev1.Volume{Name: "bundle", VolumeSource: projectedSecret("my-tls", "platform-agent-inference-key-agent")},
			".projected.sources[1].secret.name", "platform-agent-inference-key-agent"},
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
					if cause.Field == wantField && !strings.Contains(cause.Message, `"`+tc.secret+`"`) {
						t.Errorf("the refusal on %s does not name Secret %s: %q", wantField, tc.secret, cause.Message)
					}
				}
			})
		}
	}

	accepted := []struct {
		name string
		vol  corev1.Volume
	}{
		{"another Secret", corev1.Volume{Name: "tls", VolumeSource: secretVolume("my-tls")}},
		{"another agent's key", corev1.Volume{Name: "cache", VolumeSource: secretVolume("other-agent-inference-key-agent")}},
		{"the bare prefix", corev1.Volume{Name: "cache", VolumeSource: secretVolume("platform-agent-inference-key-")}},
		{"a projected source for another Secret", corev1.Volume{Name: "bundle", VolumeSource: projectedSecret("my-tls")}},
	}
	for _, list := range []string{"extraVolumes", "sidecarVolumes"} {
		for _, tc := range accepted {
			t.Run(list+"/accepted/"+tc.name, func(t *testing.T) {
				if _, err := val.ValidateCreate(ctx, agentWith(list, tc.vol)); err != nil {
					t.Errorf("refused: %v", err)
				}
			})
		}
	}
}
