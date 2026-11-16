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
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/utils/ptr"
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

// TestTheExecTierHoldsItsLongestCommand pins the exec pods' own grace period:
// RollingUpdate keeps another replica serving while one rolls, so the exec
// role drains for as long as the longest command it admits, and the broker's
// bounded 45s would cut a command still running at the SIGKILL.
// tests/test_credential_proxy_drain_budget.py holds the drain's own terms
// under this value.
func TestTheExecTierHoldsItsLongestCommand(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(2)))
	broker := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	dep := buildCredentialProxyExecTierDeployment(agent, broker)

	grace := dep.Spec.Template.Spec.TerminationGracePeriodSeconds
	if grace == nil {
		t.Fatalf("the exec pod sets no terminationGracePeriodSeconds; the kubelet's %ds default cuts the drain", kubernetesDefaultTerminationGraceSeconds)
	}
	if *grace != credentialProxyExecTierTerminationGraceSeconds {
		t.Errorf("exec terminationGracePeriodSeconds = %d, want credentialProxyExecTierTerminationGraceSeconds (%d)", *grace, credentialProxyExecTierTerminationGraceSeconds)
	}
	longest := credentialProxyCommandTimeoutSeconds + credentialProxyCommandSlotWaitSeconds
	if want := longest + credentialProxyExecTierStopMarginSeconds; credentialProxyExecTierTerminationGraceSeconds != want {
		t.Errorf("credentialProxyExecTierTerminationGraceSeconds = %d, want the command timeout, the slot wait and the stop margin (%d)", credentialProxyExecTierTerminationGraceSeconds, want)
	}
	if got := broker.Spec.Template.Spec.TerminationGracePeriodSeconds; got == nil || *got != credentialProxyTerminationGraceSeconds {
		t.Errorf("broker terminationGracePeriodSeconds = %v, want credentialProxyTerminationGraceSeconds (%d) left as it was", got, credentialProxyTerminationGraceSeconds)
	}
}

// TestTheExecTierProbeFindsADrainQuickly pins the exec pods' readiness probe:
// a draining exec pod fails /healthz, and three failures 3s apart take it out
// of the Service, so at one replica callers fall back to the stateful broker in
// about 9s. The broker keeps its own probe.
func TestTheExecTierProbeFindsADrainQuickly(t *testing.T) {
	agent := execTierTestAgent(ptr.To(int32(1)))
	broker := buildCredentialProxyDeployment(agent, brokerTestPolicyHash)
	brokerBefore := broker.DeepCopy()
	dep := buildCredentialProxyExecTierDeployment(agent, broker)

	probe := func(d *appsv1.Deployment) *corev1.Probe {
		for _, c := range d.Spec.Template.Spec.Containers {
			if c.Name == credentialProxyContainerName {
				return c.ReadinessProbe
			}
		}
		return nil
	}
	execProbe, brokerProbe := probe(dep), probe(broker)
	if execProbe == nil || brokerProbe == nil {
		t.Fatalf("readiness probes: exec %v, broker %v; want both set", execProbe, brokerProbe)
	}
	got := [3]int32{execProbe.PeriodSeconds, execProbe.TimeoutSeconds, execProbe.FailureThreshold}
	want := [3]int32{credentialProxyExecTierReadinessPeriodSeconds, credentialProxyExecTierReadinessTimeoutSeconds,
		credentialProxyExecTierReadinessFailureThreshold}
	if got != want {
		t.Errorf("exec readiness period/timeout/threshold = %v, want %v", got, want)
	}
	if execProbe.TimeoutSeconds >= execProbe.PeriodSeconds {
		t.Errorf("exec readiness timeout %ds is not inside its %ds period", execProbe.TimeoutSeconds, execProbe.PeriodSeconds)
	}
	if !equality.Semantic.DeepEqual(brokerProbe, probe(brokerBefore)) {
		t.Errorf("building the exec tier changed the broker's probe: %+v", brokerProbe)
	}
	if execProbe.HTTPGet == nil || execProbe.HTTPGet.Path != "/healthz" {
		t.Errorf("exec readiness probe = %+v, want the /healthz the draining runtime fails", execProbe.HTTPGet)
	}
}
