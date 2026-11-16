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

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The single-replica gateway is not the autoscaler's to evict for
// consolidation (#6040): one ScaleDown took the only gateway down mid-deploy.
func TestTheGatewayPodIsNotEvictedForConsolidation(t *testing.T) {
	pt := buildPodTemplateSpec(newTestPlatformAgent(), "", "", "", "", nil, renderOptions{})
	if got := pt.Annotations[gatewaySafeToEvictAnnotation]; got != "false" {
		t.Errorf("%s = %q, want \"false\"", gatewaySafeToEvictAnnotation, got)
	}
}

// spec.deployment.podAnnotations wins, so an install that wants the
// consolidation back can say "true".
func TestPodAnnotationsCanOptTheGatewayBackIntoConsolidation(t *testing.T) {
	agent := newTestPlatformAgent()
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		PodAnnotations: map[string]string{gatewaySafeToEvictAnnotation: "true"},
	}
	pt := buildPodTemplateSpec(agent, "", "", "", "", nil, renderOptions{})
	if got := pt.Annotations[gatewaySafeToEvictAnnotation]; got != "true" {
		t.Errorf("%s = %q with podAnnotations saying \"true\"; the CR's own value must win", gatewaySafeToEvictAnnotation, got)
	}
}
