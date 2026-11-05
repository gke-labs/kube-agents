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
	appsv1 "k8s.io/api/apps/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aFlaggedPod is a standing workload the operator renders into the agent's
// namespace only while an operator env flag is on. No golden sets the flag, so
// neither the session quota's headroom nor the chart's footprint.yaml, both
// sized from the goldens, can see it.
type a2aFlaggedPod struct {
	// name is its entry in the chart's files/flagged-pods.yaml.
	name string
	// env is the operator flag that renders it.
	env string
	// enabled reads env as the operator does.
	enabled func() bool
	// build renders its Deployment, for the pods it adds and their size.
	build func(*agentv1alpha1.PlatformAgent) *appsv1.Deployment
}

// a2aFlaggedPods is every such workload. The session quota's standing pods
// (a2aStandingPods) add each one's replicas while its flag is on, and the chart's quota preflight counts the
// same list from files/flagged-pods.yaml when operator.extraEnv sets the flag
// (TestTheChartCountsEveryFlaggedPod holds the two equal, in sizes too).
//
// A feature that adds a standing pod behind a flag goes here, and leaves when
// it's on by default: from then on the goldens render it, footprint.yaml
// counts it, and a2aStandingPods has to count its builder with the
// unconditional ones (TestA2AStandingPodsMatchTheNextModeGolden). Profile work
// doesn't: the warm workers and the Job-mode pods run in the profile
// namespace, under the pod quota the operator derives for it from the warm
// pools and the profile ceiling, not in this namespace or its quota.
var a2aFlaggedPods = []a2aFlaggedPod{
	{name: "mcp", env: a2aMCPDoorEnvVar, enabled: a2aMCPDoorEnabled, build: buildA2AMCPDeployment},                          // #3869
	{name: "dispatcher", env: a2aDispatcherImageEnvVar, enabled: a2aDispatcherEnabled, build: buildA2ADispatcherDeployment}, // #4808
}

// a2aFlaggedStandingPods is the pods the flagged workloads add to the namespace
// now, and none while every flag is off. Each rolls Recreate, so a roll needs
// no surge slot beyond its replicas.
func a2aFlaggedStandingPods(agent *agentv1alpha1.PlatformAgent) int64 {
	var n int64
	for _, w := range a2aFlaggedPods {
		if w.enabled() {
			n += int64(ptr.Deref(w.build(agent).Spec.Replicas, 1))
		}
	}
	return n
}
