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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// assertWriterEphemeral checks a container that writes a bounded emptyDir: it
// requests ephemeral storage and its limit equals the request, because a
// LimitRange defaulting a smaller limit refuses a request with no limit of its
// own, and the figure is the one the constant names.
func assertWriterEphemeral(t *testing.T, where string, c corev1.Container, want string) {
	t.Helper()
	req, ok := c.Resources.Requests[corev1.ResourceEphemeralStorage]
	if !ok {
		t.Errorf("%s: no ephemeral-storage request; a quota on requests.ephemeral-storage refuses the pod", where)
		return
	}
	lim, ok := c.Resources.Limits[corev1.ResourceEphemeralStorage]
	if !ok {
		t.Errorf("%s: ephemeral-storage request %s has no limit; a smaller LimitRange default refuses it", where, req.String())
		return
	}
	if req.Cmp(lim) != 0 {
		t.Errorf("%s: ephemeral-storage request %s, limit %s; want them equal", where, req.String(), lim.String())
	}
	if w := resource.MustParse(want); req.Cmp(w) != 0 {
		t.Errorf("%s: ephemeral-storage request %s, want %s", where, req.String(), want)
	}
}

// TestEmptyDirWritersRequestTheirVolumePlusLogs covers the operator's three
// containers that write a bounded emptyDir and had no ephemeral request.
func TestEmptyDirWritersRequestTheirVolumePlusLogs(t *testing.T) {
	plugin := &agentv1alpha1.AgentPlugin{
		ObjectMeta: metav1.ObjectMeta{Name: "myplugin"},
		Spec:       agentv1alpha1.AgentPluginSpec{Image: "busybox:musl"},
	}
	assertWriterEphemeral(t, "plugin staging init", buildPluginStagingInitContainer("/home/agent", plugin), pluginStagingEphemeralRequest)

	assertWriterEphemeral(t, "sandbox client-key init", buildShellSandboxClientKeyInitContainer("example.com/agent:v1"), shellSandboxClientKeyEphemeralRequest)

	job := buildA2AProvisionJob(a2aTestAgent())
	assertWriterEphemeral(t, "a2a provision container", job.Spec.Template.Spec.Containers[0], a2aProvisionEphemeralRequest)
	var tmp *corev1.Volume
	for i := range job.Spec.Template.Spec.Volumes {
		if job.Spec.Template.Spec.Volumes[i].Name == "tmp" {
			tmp = &job.Spec.Template.Spec.Volumes[i]
		}
	}
	if tmp == nil || tmp.EmptyDir == nil || tmp.EmptyDir.SizeLimit == nil || tmp.EmptyDir.SizeLimit.Cmp(resource.MustParse(a2aProvisionTmpSizeLimit)) != 0 {
		t.Errorf("a2a provision tmp volume = %+v, want an emptyDir with sizeLimit %s", tmp, a2aProvisionTmpSizeLimit)
	}
}

// TestEmptyDirWriterRequestsAreTheVolumePlusTheAllowance holds the
// hand-written request constants to their definition, so one cannot drift from
// the sizeLimit it covers.
func TestEmptyDirWriterRequestsAreTheVolumePlusTheAllowance(t *testing.T) {
	for _, tc := range []struct{ request, sizeLimit string }{
		{pluginStagingEphemeralRequest, pluginStagingSizeLimit},
		{a2aProvisionEphemeralRequest, a2aProvisionTmpSizeLimit},
		{shellSandboxClientKeyEphemeralRequest, shellSandboxClientKeySizeLimit},
	} {
		want := resource.MustParse(tc.sizeLimit)
		want.Add(resource.MustParse(emptyDirLogsAllowance))
		if got := resource.MustParse(tc.request); got.Cmp(want) != 0 {
			t.Errorf("request %s for sizeLimit %s: want %s (sizeLimit + %s)", tc.request, tc.sizeLimit, want.String(), emptyDirLogsAllowance)
		}
	}
}
