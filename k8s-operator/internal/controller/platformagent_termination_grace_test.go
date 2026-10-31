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
	"regexp"
	"strconv"
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

// supervisorShutdownTimeoutPattern reads the drain budget leader_elect.py gives
// Hermes above one replica, and supervisorAPITimeoutPattern the timeout on each
// of the calls that clear the label and release the lease after it, from the
// copy embedded in the operator.
var (
	supervisorShutdownTimeoutPattern = regexp.MustCompile(`(?m)^GATEWAY_SHUTDOWN_TIMEOUT_SECONDS = (\d+)$`)
	supervisorAPITimeoutPattern      = regexp.MustCompile(`(?m)^LEASE_API_TIMEOUT_SECONDS = (\d+)$`)
)

// supervisorReleaseCalls is how many API calls follow the drain: the label
// clear, then the lease read and replace.
const supervisorReleaseCalls = 3

func supervisorConstant(t *testing.T, pattern *regexp.Regexp, name string) int64 {
	t.Helper()
	m := pattern.FindStringSubmatch(leaderElectScript)
	if m == nil {
		t.Fatalf("leader_elect.py no longer declares %s as an integer constant", name)
	}
	value, err := strconv.ParseInt(m[1], 10, 64)
	if err != nil {
		t.Fatalf("%s = %q: %v", name, m[1], err)
	}
	return value
}

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

// Above one replica leader_elect.py supervises Hermes and SIGKILLs it after
// its own budget. That budget has to cover Hermes' stop path, and the budget
// plus the calls that clear the leader label and release the lease have to end
// before the kubelet's grace does, or the supervisor is killed before the
// release and a standby waits out the lease.
func TestSupervisorShutdownBudgetNestsInsideGracePeriod(t *testing.T) {
	budget := supervisorConstant(t, supervisorShutdownTimeoutPattern, "GATEWAY_SHUTDOWN_TIMEOUT_SECONDS")
	apiTimeout := supervisorConstant(t, supervisorAPITimeoutPattern, "LEASE_API_TIMEOUT_SECONDS")
	if budget < hermesDefaultCronDrainSeconds+hermesCronDrainCleanupReserveSecs {
		t.Errorf("GATEWAY_SHUTDOWN_TIMEOUT_SECONDS = %d, want >= %d (cron drain + cleanup reserve)",
			budget, hermesDefaultCronDrainSeconds+hermesCronDrainCleanupReserveSecs)
	}
	if total := budget + supervisorReleaseCalls*apiTimeout; total >= gatewayTerminationGracePeriodSeconds {
		t.Errorf("GATEWAY_SHUTDOWN_TIMEOUT_SECONDS + %d x LEASE_API_TIMEOUT_SECONDS = %d, want < gatewayTerminationGracePeriodSeconds (%d)",
			supervisorReleaseCalls, total, gatewayTerminationGracePeriodSeconds)
	}
}
