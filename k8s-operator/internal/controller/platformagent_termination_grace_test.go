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

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's stop path waits up to agent.cron_drain_timeout (30s) for an
// in-flight cron run and then keeps a 10s cleanup reserve. A grace period under
// that sum lets the kubelet SIGKILL the gateway mid-cleanup, which leaves the
// cron job wedged at last_status=running. These are Hermes defaults, not
// values this repo sets, so they are restated here rather than read.
const (
	hermesDefaultCronDrainSeconds     = 30
	hermesCronDrainCleanupReserveSecs = 10
)

func TestGatewayTerminationGracePeriodCoversStopPath(t *testing.T) {
	if gatewayTerminationGracePeriodSeconds < hermesDefaultCronDrainSeconds+hermesCronDrainCleanupReserveSecs {
		t.Fatalf("gatewayTerminationGracePeriodSeconds = %d, want >= %d (cron drain %ds + cleanup reserve %ds)",
			gatewayTerminationGracePeriodSeconds,
			hermesDefaultCronDrainSeconds+hermesCronDrainCleanupReserveSecs,
			hermesDefaultCronDrainSeconds, hermesCronDrainCleanupReserveSecs)
	}
}

func TestGatewayPodTemplateSetsTerminationGracePeriod(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}

	dep := buildDeployment(agent, "", "", "", "", nil, renderOptions{})
	got := dep.Spec.Template.Spec.TerminationGracePeriodSeconds
	if got == nil {
		t.Fatalf("Deployment pod template leaves terminationGracePeriodSeconds unset (Kubernetes default 30s)")
	}
	if *got != gatewayTerminationGracePeriodSeconds {
		t.Errorf("Deployment terminationGracePeriodSeconds = %d, want %d", *got, gatewayTerminationGracePeriodSeconds)
	}

	sts := buildStatefulSet(agent, "", "", "", "", nil, renderOptions{})
	got = sts.Spec.Template.Spec.TerminationGracePeriodSeconds
	if got == nil || *got != gatewayTerminationGracePeriodSeconds {
		t.Errorf("StatefulSet terminationGracePeriodSeconds = %v, want %d", got, gatewayTerminationGracePeriodSeconds)
	}
}
