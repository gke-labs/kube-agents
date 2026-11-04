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
)

// kubernetesDefaultTerminationGraceSeconds is what the kubelet waits between
// SIGTERM and SIGKILL when a pod sets nothing.
const kubernetesDefaultTerminationGraceSeconds = 30

// TestTheBrokerPodHoldsItsDrain pins the grace period that lets the broker's
// SIGTERM drain finish, and the Recreate strategy it is sized for: the drain
// runs while no other broker serves, so it is bounded, not as long as the
// longest command. tests/test_credential_proxy_drain_budget.py holds the
// drain's own terms under this value.
func TestTheBrokerPodHoldsItsDrain(t *testing.T) {
	dep := buildCredentialProxyDeployment(brokerPodAgent(), brokerTestPolicyHash)

	grace := dep.Spec.Template.Spec.TerminationGracePeriodSeconds
	if grace == nil {
		t.Fatalf("the broker pod sets no terminationGracePeriodSeconds; the kubelet's %ds default cuts the drain", kubernetesDefaultTerminationGraceSeconds)
	}
	if *grace != credentialProxyTerminationGraceSeconds {
		t.Errorf("terminationGracePeriodSeconds = %d, want credentialProxyTerminationGraceSeconds (%d)", *grace, credentialProxyTerminationGraceSeconds)
	}
	if *grace <= kubernetesDefaultTerminationGraceSeconds {
		t.Errorf("terminationGracePeriodSeconds = %d, not above the %ds default the drain outlasts", *grace, kubernetesDefaultTerminationGraceSeconds)
	}
	if got := dep.Spec.Strategy.Type; got != appsv1.RecreateDeploymentStrategyType {
		t.Errorf("strategy = %q, want Recreate: the bounded drain is sized for a pod no other broker serves beside", got)
	}
}
