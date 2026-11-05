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
	"os"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// transientPodsChartPath is the chart's copy of the pods the operator starts
// on a schedule, which its quota preflight counts.
const transientPodsChartPath = "../../../charts/kube-agents/files/transient-pods.yaml"

// TestTheChartCountsTheScopePublishersJobPod holds the chart's scopePublisher
// entry to the operator (#4816): the Job's pod summed over its containers,
// one pod at a time, and the same scope fields giving it something to
// resolve, read the same way the operator reads A2A_GATEWAY_POD_POLICY. No
// golden declares such a scope, so this is what keeps a resize or a new scope
// field from passing the preflight and then having the Job's pod refused.
func TestTheChartCountsTheScopePublishersJobPod(t *testing.T) {
	raw, err := os.ReadFile(transientPodsChartPath)
	if err != nil {
		t.Fatalf("read the chart's transient pods: %v", err)
	}
	var file struct {
		ScopePublisher struct {
			Pods                         int64    `json:"pods"`
			ScopeMembers                 []string `json:"scopeMembers"`
			CPUMillisRequest             int64    `json:"cpuMillisRequest"`
			CPUMillisLimit               int64    `json:"cpuMillisLimit"`
			MemoryBytesRequest           int64    `json:"memoryBytesRequest"`
			MemoryBytesLimit             int64    `json:"memoryBytesLimit"`
			EphemeralStorageBytesRequest int64    `json:"ephemeralStorageBytesRequest"`
			EphemeralStorageBytesLimit   int64    `json:"ephemeralStorageBytesLimit"`
		} `json:"scopePublisher"`
	}
	if err := yaml.UnmarshalStrict(raw, &file); err != nil {
		t.Fatalf("parse %s: %v", transientPodsChartPath, err)
	}
	c := file.ScopePublisher

	// One at a time: decideScopePublish never starts a Job while another is
	// active, and each Job runs one pod and never retries.
	job := buildScopePublisherJob(scopePublisherAgent(), time.Unix(0, 0), "hash")
	if c.Pods != 1 {
		t.Errorf("the chart counts %d scope publisher pods; the operator runs one at a time", c.Pods)
	}
	if p := job.Spec.Parallelism; p != nil && *p != 1 {
		t.Errorf("parallelism = %d; the chart counts one pod", *p)
	}
	if b := job.Spec.BackoffLimit; b == nil || *b != 0 {
		t.Errorf("backoffLimit = %v; a retry pod beside a failed one is more than the chart counts", b)
	}
	pod := job.Spec.Template.Spec
	if len(pod.InitContainers) != 0 {
		t.Fatalf("%d init containers; the chart's figures are the containers' sum", len(pod.InitContainers))
	}
	req, lim := corev1.ResourceList{}, corev1.ResourceList{}
	for _, ctr := range pod.Containers {
		for _, side := range []struct{ sum, add corev1.ResourceList }{{req, ctr.Resources.Requests}, {lim, ctr.Resources.Limits}} {
			for name, q := range side.add {
				total := side.sum[name]
				total.Add(q)
				side.sum[name] = total
			}
		}
		if ctr.Resources.Limits.Cpu().IsZero() || ctr.Resources.Limits.Memory().IsZero() {
			t.Errorf("container %s declares no CPU or memory limit; a limits quota refuses it", ctr.Name)
		}
	}
	for _, f := range []struct {
		name      string
		got, want int64
	}{
		{"cpuMillisRequest", req.Cpu().MilliValue(), c.CPUMillisRequest},
		{"cpuMillisLimit", lim.Cpu().MilliValue(), c.CPUMillisLimit},
		{"memoryBytesRequest", req.Memory().Value(), c.MemoryBytesRequest},
		{"memoryBytesLimit", lim.Memory().Value(), c.MemoryBytesLimit},
		{"ephemeralStorageBytesRequest", req.StorageEphemeral().Value(), c.EphemeralStorageBytesRequest},
		{"ephemeralStorageBytesLimit", lim.StorageEphemeral().Value(), c.EphemeralStorageBytesLimit},
	} {
		if f.got != f.want {
			t.Errorf("%s = %d in the operator, %d in the chart", f.name, f.got, f.want)
		}
	}

	// The scope fields: every list in ScopeSpec, set alone, gives the Job
	// something to resolve exactly when the chart lists it.
	scopeType := reflect.TypeOf(agentv1alpha1.ScopeSpec{})
	seen := 0
	for i := 0; i < scopeType.NumField(); i++ {
		field := scopeType.Field(i)
		if field.Type != reflect.TypeOf([]string(nil)) {
			continue
		}
		jsonName := strings.Split(field.Tag.Get("json"), ",")[0]
		scope := agentv1alpha1.ScopeSpec{}
		reflect.ValueOf(&scope).Elem().Field(i).Set(reflect.ValueOf([]string{"x"}))
		agent := scopePublisherAgent()
		agent.Spec.Scope = &scope
		listed := slices.Contains(c.ScopeMembers, jsonName)
		if got := scopeHasResolvableMembers(agent); got != listed {
			t.Errorf("scope.%s alone: the operator resolves it = %v, the chart lists it = %v", jsonName, got, listed)
		}
		if listed {
			seen++
		}
	}
	if seen != len(c.ScopeMembers) {
		t.Errorf("the chart lists %v, of which %d are list fields of ScopeSpec", c.ScopeMembers, seen)
	}

	// The policy flag, read as the chart reads a literal: "true" only.
	agent := scopePublisherAgent()
	for v, on := range map[string]bool{"true": true, "": false, "false": false, "TRUE": false, " true": false} {
		t.Setenv(a2aGatewayPodPolicyEnvVar, v)
		if got := scopePublisherMayRun(agent, true); got != on {
			t.Errorf("%s=%q: the operator may run the Job = %v; the chart counts it = %v", a2aGatewayPodPolicyEnvVar, v, got, on)
		}
	}
}
