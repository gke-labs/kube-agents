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
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/yaml"
)

// flaggedPodsChartPath is the chart's copy of a2aFlaggedPods, which its quota
// preflight counts.
const flaggedPodsChartPath = "../../../charts/kube-agents/files/flagged-pods.yaml"

type chartFlaggedPod struct {
	Name                         string `json:"name"`
	Env                          string `json:"env"`
	EnabledWhen                  string `json:"enabledWhen"`
	Replicas                     int64  `json:"replicas"`
	CPUMillisRequest             int64  `json:"cpuMillisRequest"`
	CPUMillisLimit               int64  `json:"cpuMillisLimit"`
	MemoryBytesRequest           int64  `json:"memoryBytesRequest"`
	MemoryBytesLimit             int64  `json:"memoryBytesLimit"`
	EphemeralStorageBytesRequest int64  `json:"ephemeralStorageBytesRequest"`
	EphemeralStorageBytesLimit   int64  `json:"ephemeralStorageBytesLimit"`
}

// TestTheChartCountsEveryFlaggedPod holds files/flagged-pods.yaml to
// a2aFlaggedPods: the same workloads, each with the operator's flag, the same
// reading of it, and the pod's replicas and summed requests and limits. The
// chart can't render these pods (no golden sets the flags), so this is what
// keeps a resize, or a new flagged workload, from passing the preflight and
// then being refused by a namespace quota (#3869, #4808).
func TestTheChartCountsEveryFlaggedPod(t *testing.T) {
	raw, err := os.ReadFile(flaggedPodsChartPath)
	if err != nil {
		t.Fatalf("read the chart's flagged pods: %v", err)
	}
	var file struct {
		Pods []chartFlaggedPod `json:"pods"`
	}
	if err := yaml.UnmarshalStrict(raw, &file); err != nil {
		t.Fatalf("parse %s: %v", flaggedPodsChartPath, err)
	}
	chart := map[string]chartFlaggedPod{}
	for _, p := range file.Pods {
		chart[p.Name] = p
	}
	if len(chart) != len(file.Pods) || len(chart) != len(a2aFlaggedPods) {
		t.Fatalf("the chart lists %d flagged pods (%d distinct), the operator %d; each needs one entry on both sides",
			len(file.Pods), len(chart), len(a2aFlaggedPods))
	}
	agent := a2aTestAgent()
	for _, w := range a2aFlaggedPods {
		t.Run(w.name, func(t *testing.T) {
			c, ok := chart[w.name]
			if !ok {
				t.Fatalf("no entry %q in %s", w.name, flaggedPodsChartPath)
			}
			if c.Env != w.env {
				t.Errorf("env = %q in the chart, %q in the operator", c.Env, w.env)
			}
			// The chart's reading of the flag, against the operator's.
			var on, off []string
			switch c.EnabledWhen {
			case "true":
				on, off = []string{"true"}, []string{"", "false", "TRUE", " true"}
			case "nonBlank":
				on, off = []string{"x", " x ", "\u200b"}, []string{"", " ", "  ", "\t\n", "\u0085"}
			default:
				t.Fatalf("enabledWhen = %q; the chart reads only \"true\" and nonBlank", c.EnabledWhen)
			}
			for _, v := range off {
				t.Setenv(w.env, v)
				if w.enabled() && v == "" {
					t.Errorf("%s unset: the operator renders it. If it's on by default now, the goldens and footprint.yaml count it: drop it from a2aFlaggedPods and the chart's file, and count its builder with a2aStandingPods' unconditional ones", w.env)
				} else if w.enabled() {
					t.Errorf("%s=%q: the operator renders it, the chart (%s) doesn't count it", w.env, v, c.EnabledWhen)
				}
			}
			for _, v := range on {
				t.Setenv(w.env, v)
				if !w.enabled() {
					t.Errorf("%s=%q: the chart (%s) counts it, the operator doesn't render it", w.env, v, c.EnabledWhen)
				}
			}

			d := w.build(agent)
			if d.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
				t.Errorf("strategy = %q; the chart and the session quota count no surge pod, so it must roll Recreate", d.Spec.Strategy.Type)
			}
			if got := int64(ptr.Deref(d.Spec.Replicas, 1)); got != c.Replicas {
				t.Errorf("replicas = %d, the chart counts %d", got, c.Replicas)
			}
			pod := d.Spec.Template.Spec
			if len(pod.InitContainers) != 0 {
				t.Fatalf("%d init containers; the chart's figures are the containers' sum", len(pod.InitContainers))
			}
			var req, lim corev1.ResourceList = corev1.ResourceList{}, corev1.ResourceList{}
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
		})
	}
}

// The session quota counts each flagged workload's pods only while its flag
// is on.
func TestA2AFlaggedStandingPodsFollowTheFlags(t *testing.T) {
	agent := a2aTestAgent()
	t.Setenv(a2aMCPDoorEnvVar, "")
	t.Setenv(a2aDispatcherImageEnvVar, "")
	t.Setenv(a2aCuratorEnvVar, "")
	if got := a2aFlaggedStandingPods(agent); got != 0 {
		t.Errorf("every flag off: %d pods, want 0", got)
	}
	t.Setenv(a2aMCPDoorEnvVar, "true")
	if got := a2aFlaggedStandingPods(agent); got != 1 {
		t.Errorf("MCP on: %d pods, want 1", got)
	}
	t.Setenv(a2aDispatcherImageEnvVar, "example.com/dispatcher:1")
	if got := a2aFlaggedStandingPods(agent); got != 2 {
		t.Errorf("MCP and dispatcher on: %d pods, want 2", got)
	}
	t.Setenv(a2aCuratorEnvVar, "true")
	if got := a2aFlaggedStandingPods(agent); got != 3 {
		t.Errorf("MCP, dispatcher, and curator on: %d pods, want 3", got)
	}
	t.Setenv(a2aMCPDoorEnvVar, "")
	t.Setenv(a2aCuratorEnvVar, "")
	if got := a2aFlaggedStandingPods(agent); got != 1 {
		t.Errorf("dispatcher on: %d pods, want 1", got)
	}
}
