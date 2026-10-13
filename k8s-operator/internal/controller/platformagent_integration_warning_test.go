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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Review round 3: Warnings() reached only the admission response, and the
// chart ships the webhook off -- so a credentialed forge the broker is not
// given left no trace on a default install, whose status read Ready.
func TestAnIntegrationWarningIsAnEventOnTheCR(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "pa", Namespace: "ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
			IntegrationSpec: agentv1alpha1.IntegrationSpec{Forges: []agentv1alpha1.ForgeSpec{{
				Name: "gl", Provider: agentv1alpha1.GitProviderGitLab,
				CredentialsRef: &agentv1alpha1.ForgeCredentialsRef{Name: "gl-token"},
			}}},
		}},
	}
	rec := record.NewFakeRecorder(4)
	r := &PlatformAgentReconciler{Recorder: rec}
	r.recordIntegrationWarnings(agent)
	events := drainEvents(rec)
	if len(events) != 1 {
		t.Fatalf("got %d Events, want 1: %q", len(events), events)
	}
	for _, want := range []string{corev1.EventTypeWarning, conditionReasonIntegrationWarning, "forges[0]"} {
		if !strings.Contains(events[0], want) {
			t.Errorf("Event %q does not contain %q", events[0], want)
		}
	}

	// A declaration with nothing to warn about writes nothing.
	agent.Spec.Integration.Forges[0].Namespace = "acme"
	r.recordIntegrationWarnings(agent)
	if events := drainEvents(rec); len(events) != 0 {
		t.Errorf("a clean declaration recorded %q", events)
	}
}
