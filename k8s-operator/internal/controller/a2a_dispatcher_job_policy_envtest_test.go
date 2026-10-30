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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// The policy source the chart's copy is generated from, relative to this package.
var dispatcherJobPolicyFile = filepath.Join("..", "..", "config", "admission", "a2a-dispatcher-job-policy.yaml")

// The Job a2a/dispatch builds from the operator's rendered config, generated
// by its TestTheBuiltJobMatchesTheAdmissionFixture.
var dispatcherJobFixture = filepath.Join("testdata", "a2a-dispatcher-job.json")

// TestTheDispatcherJobPolicyHoldsJobsToTheRenderedTemplate runs the shipped
// policy in a real API server against params the operator's own builders
// render: a Job the dispatcher creates is admitted only as its profile's
// template, and every way out of it is denied by the rule that names it.
// Missing params deny; PodTemplates without the operator's labels are not
// params; the dispatcher can't write params; other requesters are not
// selected.
func TestTheDispatcherJobPolicyHoldsJobsToTheRenderedTemplate(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}})
	// Labelled as the chart labels it: the binding selects profile
	// namespaces by it.
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: pns,
		Labels: map[string]string{profileNamespaceComponentLabel: profileNamespaceComponent}}})
	// The dispatcher's Jobs Role and binding, as the operator renders them.
	mustCreate(ctx, t, admin, buildA2ADispatcherJobsRole(agent))
	mustCreate(ctx, t, admin, buildA2ADispatcherJobsRoleBinding(agent))
	dispatcher := impersonatedClient(t, cfg, agent.Namespace, a2aDispatcherName(agent))

	tse := buildProfileJobTemplate(agent, ptr.To(unattendedProfile(agent, "tse")), false)

	// Before any operator param: PodTemplates without its labels, one
	// posing as the index and listing tse's account, one as tse's template.
	// Neither is selected, so the Job finds no param and is denied.
	foreignIndex := buildDispatchJobIndex(agent, map[string]*corev1.PodTemplateSpec{"tse": tse})
	foreignIndex.Labels = map[string]string{"x": "y"}
	foreignTemplate := buildDispatchJobTemplate(agent, "tse", tse)
	foreignTemplate.Labels = nil
	mustCreate(ctx, t, admin, foreignIndex)
	mustCreate(ctx, t, admin, foreignTemplate)
	applyDispatcherJobPolicy(ctx, t, admin)
	deadline := time.Now().Add(gatewayPodPolicyActivation)
	for n := 0; ; n++ {
		err := dispatcher.Create(ctx, dispatcherJob(fmt.Sprintf("activation-%d", n), tse, nil))
		if apierrors.IsForbidden(err) || apierrors.IsInvalid(err) {
			if !strings.Contains(err.Error(), "no params found") {
				t.Errorf("with only unlabelled PodTemplates: refused for %q, want no params found", err)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the policy was not enforced within %s (last create: %v)", gatewayPodPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}
	for _, obj := range []client.Object{foreignIndex, foreignTemplate} {
		if err := admin.Delete(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}

	// The cases that hold even for a template that says otherwise: each
	// gets a profile whose rendered template carries the mutation, so the
	// equality rule passes and the static rule is what denies.
	const (
		scheduling = "may not set tolerations"
		restricted = "must meet Pod Security restricted"
		jobShape   = "must have backoffLimit 0"
		equality   = "must have exactly the pod spec"
		podLabels  = "must carry exactly the pod labels"
		podAnnots  = "must carry exactly the pod annotations"
		jobMeta    = "must carry the dispatcher's marker and its agent's instance label"
		index      = "must run as a profile ServiceAccount the operator rendered"
	)
	main := func(s *corev1.PodSpec) *corev1.Container { return &s.Containers[0] }
	static := map[string]struct {
		mutate func(*corev1.PodSpec)
		rule   string
	}{
		"tolerations": {func(s *corev1.PodSpec) {
			s.Tolerations = []corev1.Toleration{{Key: "k", Operator: corev1.TolerationOpExists}}
		}, scheduling},
		"a nodeSelector": {func(s *corev1.PodSpec) { s.NodeSelector = map[string]string{"pool": "gpu"} }, scheduling},
		"affinity": {func(s *corev1.PodSpec) {
			s.Affinity = &corev1.Affinity{NodeAffinity: &corev1.NodeAffinity{RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
				NodeSelectorTerms: []corev1.NodeSelectorTerm{{MatchExpressions: []corev1.NodeSelectorRequirement{{Key: "k", Operator: corev1.NodeSelectorOpExists}}}},
			}}}
		}, scheduling},
		"a priorityClassName": {func(s *corev1.PodSpec) { s.PriorityClassName = "system-node-critical" }, scheduling},
		"another scheduler":   {func(s *corev1.PodSpec) { s.SchedulerName = "my-scheduler" }, scheduling},
		"a hostPath volume": {func(s *corev1.PodSpec) {
			s.Volumes = append(s.Volumes, corev1.Volume{Name: "h", VolumeSource: corev1.VolumeSource{HostPath: &corev1.HostPathVolumeSource{Path: "/"}}})
		}, restricted},
		"a Secret volume": {func(s *corev1.PodSpec) {
			s.Volumes = append(s.Volumes, corev1.Volume{Name: "s", VolumeSource: corev1.VolumeSource{Secret: &corev1.SecretVolumeSource{SecretName: "x"}}})
		}, restricted},
		"host network":        {func(s *corev1.PodSpec) { s.HostNetwork = true }, restricted},
		"automount on":        {func(s *corev1.PodSpec) { s.AutomountServiceAccountToken = ptr.To(true) }, restricted},
		"runAsNonRoot unset":  {func(s *corev1.PodSpec) { s.SecurityContext.RunAsNonRoot = nil }, restricted},
		"unconfined seccomp":  {func(s *corev1.PodSpec) { s.SecurityContext.SeccompProfile.Type = corev1.SeccompProfileTypeUnconfined }, restricted},
		"an added capability": {func(s *corev1.PodSpec) { main(s).SecurityContext.Capabilities.Add = []corev1.Capability{"SYS_ADMIN"} }, restricted},
		"a privileged init container": {func(s *corev1.PodSpec) {
			s.InitContainers = append(s.InitContainers, corev1.Container{Name: "evil", Image: "busybox",
				SecurityContext: &corev1.SecurityContext{Privileged: ptr.To(true)}})
		}, restricted},
		"privilege escalation": {func(s *corev1.PodSpec) {
			main(s).SecurityContext.AllowPrivilegeEscalation = ptr.To(true)
		}, restricted},
	}
	templates := map[string]*corev1.PodTemplateSpec{"tse": tse}
	staticProfile := map[string]string{}
	names := make([]string, 0, len(static))
	for name := range static {
		names = append(names, name)
	}
	sort.Strings(names)
	for i, name := range names {
		profile := fmt.Sprintf("s%d", i)
		staticProfile[name] = profile
		tmpl := buildProfileJobTemplate(agent, ptr.To(unattendedProfile(agent, profile)), false)
		static[name].mutate(&tmpl.Spec)
		templates[profile] = tmpl
	}
	for profile, tmpl := range templates {
		mustCreate(ctx, t, admin, buildDispatchJobTemplate(agent, profile, tmpl))
	}
	mustCreate(ctx, t, admin, buildDispatchJobIndex(agent, templates))

	// The policy's params come from an informer; wait until the rendered
	// template is admitted.
	deadline = time.Now().Add(gatewayPodPolicyActivation)
	for {
		err := dispatcher.Create(ctx, dispatcherJob("rendered", tse, nil))
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the rendered template was not admitted within %s: %v", gatewayPodPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}

	// The Job a2a/dispatch's buildJob makes from the operator's rendered
	// config, generated there (TestTheBuiltJobMatchesTheAdmissionFixture).
	built, err := os.ReadFile(dispatcherJobFixture)
	if err != nil {
		t.Fatal(err)
	}
	var builtJob batchv1.Job
	if err := json.Unmarshal(built, &builtJob); err != nil {
		t.Fatal(err)
	}
	if err := dispatcher.Create(ctx, &builtJob); err != nil {
		t.Errorf("the Job the dispatcher builds (%s): refused, want admitted: %v", dispatcherJobFixture, err)
	}

	// Each case its own Job name, so one wrongly admitted doesn't make
	// the next refused as already existing.
	for i, name := range names {
		c := static[name]
		err := dispatcher.Create(ctx, dispatcherJob(fmt.Sprintf("static-%d", i), templates[staticProfile[name]], nil))
		if err == nil {
			t.Errorf("%s (in its template too): admitted, want refused", name)
			continue
		}
		if !strings.Contains(err.Error(), c.rule) {
			t.Errorf("%s (in its template too): refused for %q, want the rule %q", name, err, c.rule)
		}
	}

	// Off the template, the Job alone changed.
	denied := map[string]struct {
		pod  func(*corev1.PodTemplateSpec)
		job  func(*batchv1.Job)
		rule string
	}{
		"another image":  {pod: func(p *corev1.PodTemplateSpec) { main(&p.Spec).Image = "registry.example/evil:1" }, rule: equality},
		"a runtimeClass": {pod: func(p *corev1.PodTemplateSpec) { p.Spec.RuntimeClassName = ptr.To("runc") }, rule: equality},
		"other resources": {pod: func(p *corev1.PodTemplateSpec) {
			main(&p.Spec).Resources.Limits = corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("64")}
		}, rule: equality},
		"another runAsUser": {pod: func(p *corev1.PodTemplateSpec) { p.Spec.SecurityContext.RunAsUser = ptr.To(int64(4242)) }, rule: equality},
		"an extra env var": {pod: func(p *corev1.PodTemplateSpec) {
			main(&p.Spec).Env = append(main(&p.Spec).Env, corev1.EnvVar{Name: "X", Value: "y"})
		}, rule: equality},
		"an extra emptyDir": {pod: func(p *corev1.PodTemplateSpec) {
			p.Spec.Volumes = append(p.Spec.Volumes, corev1.Volume{Name: "e", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		}, rule: equality},
		"an extra pod label": {pod: func(p *corev1.PodTemplateSpec) { p.Labels["app"] = "evil" }, rule: podLabels},
		"a template label dropped": {pod: func(p *corev1.PodTemplateSpec) {
			for k := range p.Labels {
				delete(p.Labels, k)
				return
			}
		}, rule: podLabels},
		"a pod annotation": {pod: func(p *corev1.PodTemplateSpec) {
			p.Annotations = map[string]string{"container.apparmor.security.beta.kubernetes.io/worker": "unconfined"}
		}, rule: podAnnots},
		"a manual selector": {job: func(j *batchv1.Job) {
			j.Spec.ManualSelector = ptr.To(true)
			j.Spec.Selector = &metav1.LabelSelector{MatchLabels: j.Spec.Template.Labels}
		}, rule: jobShape},
		"a podFailurePolicy that ignores failures": {job: func(j *batchv1.Job) {
			j.Spec.PodFailurePolicy = &batchv1.PodFailurePolicy{Rules: []batchv1.PodFailurePolicyRule{{
				Action:      batchv1.PodFailurePolicyActionIgnore,
				OnExitCodes: &batchv1.PodFailurePolicyOnExitCodesRequirement{Operator: batchv1.PodFailurePolicyOnExitCodesOpNotIn, Values: []int32{0}},
			}}}
		}, rule: jobShape},
		"indexed completion": {job: func(j *batchv1.Job) {
			j.Spec.CompletionMode = ptr.To(batchv1.IndexedCompletion)
			j.Spec.Completions = ptr.To(int32(1))
		}, rule: jobShape},
		"suspended":                      {job: func(j *batchv1.Job) { j.Spec.Suspend = ptr.To(true) }, rule: jobShape},
		"no dispatcher marker label":     {job: func(j *batchv1.Job) { delete(j.Labels, dispatchJobLabel) }, rule: jobMeta},
		"the marker label false":         {job: func(j *batchv1.Job) { j.Labels[dispatchJobLabel] = "false" }, rule: jobMeta},
		"another agent's instance label": {job: func(j *batchv1.Job) { j.Labels["app.kubernetes.io/instance"] = "other-ns-other-agent" }, rule: jobMeta},
		"a finalizer":                    {job: func(j *batchv1.Job) { j.Finalizers = []string{"example.com/keep"} }, rule: jobMeta},
		"an unlisted ServiceAccount":     {pod: func(p *corev1.PodTemplateSpec) { p.Spec.ServiceAccountName = "agentprofile-other-unattended" }, rule: index},
		"the default ServiceAccount":     {pod: func(p *corev1.PodTemplateSpec) { p.Spec.ServiceAccountName = "default" }, rule: index},
		"backoffLimit 1":                 {job: func(j *batchv1.Job) { j.Spec.BackoffLimit = ptr.To(int32(1)) }, rule: jobShape},
		"backoffLimit unset":             {job: func(j *batchv1.Job) { j.Spec.BackoffLimit = nil }, rule: jobShape},
		"parallelism 2":                  {job: func(j *batchv1.Job) { j.Spec.Parallelism = ptr.To(int32(2)) }, rule: jobShape},
		"completions 2":                  {job: func(j *batchv1.Job) { j.Spec.Completions = ptr.To(int32(2)) }, rule: jobShape},
		"no activeDeadlineSeconds":       {job: func(j *batchv1.Job) { j.Spec.ActiveDeadlineSeconds = nil }, rule: jobShape},
		"no ttlSecondsAfterFinished":     {job: func(j *batchv1.Job) { j.Spec.TTLSecondsAfterFinished = nil }, rule: jobShape},
	}
	if len(tse.Labels) == 0 {
		t.Fatal("the rendered template has no pod labels, so the label cases prove nothing")
	}
	n := 0
	for name, c := range denied {
		tmpl := tse.DeepCopy()
		if c.pod != nil {
			c.pod(tmpl)
		}
		n++
		err := dispatcher.Create(ctx, dispatcherJob(fmt.Sprintf("denied-%d", n), tmpl, c.job))
		if err == nil {
			t.Errorf("%s: admitted, want refused", name)
			continue
		}
		if !strings.Contains(err.Error(), c.rule) {
			t.Errorf("%s: refused for %q, want the rule %q", name, err, c.rule)
		}
	}
	// No ServiceAccount at all, without the projected tokens: the API server
	// refuses those on a pod with no ServiceAccount before admission runs.
	bare := tse.DeepCopy()
	bare.Spec.ServiceAccountName = ""
	bare.Spec.Volumes = nil
	for i := range bare.Spec.Containers {
		bare.Spec.Containers[i].VolumeMounts = nil
	}
	for i := range bare.Spec.InitContainers {
		bare.Spec.InitContainers[i].VolumeMounts = nil
	}
	if err := dispatcher.Create(ctx, dispatcherJob("no-account", bare, nil)); err == nil || !strings.Contains(err.Error(), "not the namespace default") {
		t.Errorf("a Job with no ServiceAccount: %v, want refused as the namespace default", err)
	}

	// Param custody: the dispatcher can't write a PodTemplate.
	evil := buildDispatchJobTemplate(agent, "evil", tse)
	if err := dispatcher.Create(ctx, evil); !apierrors.IsForbidden(err) {
		t.Errorf("the dispatcher creating a PodTemplate: %v, want Forbidden", err)
	}

	// Not selected: a Job in a namespace that isn't a profile namespace,
	// which has no params. Params are found before matchConditions run, so
	// an unscoped binding denies it ("no params found"): every Job in the
	// cluster, the operator's own provisioning Job among them (found live
	// on GKE Autopilot).
	if err := admin.Create(ctx, dispatcherJob("elsewhere", tse, func(j *batchv1.Job) { j.Namespace = agent.Namespace })); err != nil {
		t.Errorf("a Job outside the profile namespace was refused: %v", err)
	}

	// Not selected: the same out-of-shape Job from a requester that is not
	// a dispatcher.
	tmpl := tse.DeepCopy()
	main(&tmpl.Spec).Image = "registry.example/evil:1"
	if err := admin.Create(ctx, dispatcherJob("admin", tmpl, nil)); err != nil {
		t.Errorf("a non-dispatcher requester's Job was refused: %v", err)
	}
}

// dispatcherJob is a Job as the dispatcher stamps it from a template: a name,
// its marker label, the shape the policy's static rule requires, and the
// template as it is. mutate, when not nil, is applied last.
func dispatcherJob(name string, tmpl *corev1.PodTemplateSpec, mutate func(*batchv1.Job)) *batchv1.Job {
	j := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns-profiles", Labels: dispatchJobLabels(a2aTestAgent())},
		Spec: batchv1.JobSpec{
			BackoffLimit:            ptr.To(int32(0)),
			ActiveDeadlineSeconds:   ptr.To(int64(3600)),
			TTLSecondsAfterFinished: ptr.To(int32(300)),
			Template:                *tmpl.DeepCopy(),
		},
	}
	if mutate != nil {
		mutate(j)
	}
	return j
}

func applyDispatcherJobPolicy(ctx context.Context, t *testing.T, c client.Client) {
	t.Helper()
	raw, err := os.ReadFile(dispatcherJobPolicyFile)
	if err != nil {
		t.Fatal(err)
	}
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), len(raw))
	for {
		u := &unstructured.Unstructured{}
		if err := decoder.Decode(&u.Object); err != nil {
			if errors.Is(err, io.EOF) {
				return
			}
			t.Fatal(err)
		}
		if len(u.Object) == 0 {
			continue
		}
		if err := c.Create(ctx, u); err != nil {
			t.Fatalf("apply %s %s: %v", u.GetKind(), u.GetName(), err)
		}
	}
}
