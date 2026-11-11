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
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bus-stream check (#4290): the operator reads the exporter's stream list,
// calls a provisioned stream missing only on evidence, and re-runs the
// provision Job, within limits, when one is.

// exporterBody renders what the exporter serves for one server: the
// server-level series when jsz is true, a stream series per name, a consumer
// series, comments, and an overlong line of another family, which the reader
// must skip.
func exporterBody(jsz bool, metaLeader string, streams ...string) string {
	var b strings.Builder
	b.WriteString("# HELP gnatsd_varz_connections connections\n# TYPE gnatsd_varz_connections gauge\n")
	b.WriteString(`gnatsd_varz_connections{server_id="S1"} 4` + "\n")
	if jsz {
		labels := fmt.Sprintf(`server_id="S1",server_name="nats-0",cluster="bus",domain="",meta_leader=%q,is_meta_leader="false"`, metaLeader)
		b.WriteString("# TYPE jetstream_server_total_streams gauge\n")
		fmt.Fprintf(&b, "jetstream_server_total_streams{%s} %d\n", labels, len(streams))
		fmt.Fprintf(&b, "jetstream_server_jetstream_disabled{%s} 0\n", labels)
		for _, name := range streams {
			fmt.Fprintf(&b, "jetstream_stream_total_bytes{%s,account=\"A2A\",account_name=\"A2A\",account_id=\"A2A\",stream_name=%q,stream_leader=\"nats-0\",is_stream_leader=\"true\",stream_raft_group=\"S-R3\"} 786\n", labels, name)
			fmt.Fprintf(&b, "jetstream_consumer_num_pending{%s,stream_name=%q,consumer_name=\"c-%s\"} 0\n", labels, name, name)
		}
	}
	b.WriteString("jetstream_consumer_desc{consumer_desc=\"" + strings.Repeat("x", busStreamsMaxLineBytes+10) + "\"} 1\n")
	return b.String()
}

func TestTheExporterBodyIsReadForTheStreamList(t *testing.T) {
	reading, err := foldBusStreamsBody(strings.NewReader(exporterBody(true, "nats-1", "TASKS", "KV_runtime-state")))
	if err != nil {
		t.Fatalf("fold: %v", err)
	}
	if !reading.jsz || reading.disabled || reading.metaLeader != "nats-1" {
		t.Errorf("reading = %+v, want jsz read, enabled, meta leader nats-1", reading)
	}
	if len(reading.streams) != 2 || !reading.streams["TASKS"] || !reading.streams["KV_runtime-state"] {
		t.Errorf("streams = %v, want TASKS and KV_runtime-state", reading.streams)
	}
}

// The jsz collector writes nothing for a server whose /jsz read failed, so a
// body with only the varz series has read no stream list: that must not read
// as "every stream missing".
func TestABodyWithoutTheServerSeriesHasNoStreamList(t *testing.T) {
	reading, err := foldBusStreamsBody(strings.NewReader(exporterBody(false, "")))
	if err != nil {
		t.Fatalf("fold: %v", err)
	}
	if reading.jsz {
		t.Errorf("reading = %+v, want no jsz", reading)
	}
	if v := busStreamsVerdictFor(1, []busServerReading{reading}); v.known {
		t.Errorf("verdict = %+v, want unknown", v)
	}
}

func TestADisabledServerIsRead(t *testing.T) {
	body := strings.Replace(exporterBody(true, "", "TASKS"), "jetstream_server_jetstream_disabled{", "jetstream_server_jetstream_disabledX{", 1) +
		`jetstream_server_jetstream_disabled{server_id="S1"} 1` + "\n"
	reading, err := foldBusStreamsBody(strings.NewReader(body))
	if err != nil {
		t.Fatalf("fold: %v", err)
	}
	if !reading.disabled {
		t.Errorf("reading = %+v, want disabled", reading)
	}
}

func TestAWantedLineThatIsTooLongOrBrokenFailsTheScrape(t *testing.T) {
	for name, body := range map[string]string{
		"too long": `jetstream_stream_total_bytes{stream_name="` + strings.Repeat("T", busStreamsMaxLineBytes) + `"} 1` + "\n",
		"broken":   `jetstream_stream_total_bytes{stream_name="TASKS" 1` + "\n",
	} {
		if _, err := foldBusStreamsBody(strings.NewReader(body)); err == nil {
			t.Errorf("%s: fold succeeded, want an error", name)
		}
	}
}

func readingOf(metaLeader string, streams ...string) busServerReading {
	r := busServerReading{jsz: true, metaLeader: metaLeader, streams: map[string]bool{}}
	for _, name := range streams {
		r.streams[name] = true
	}
	return r
}

func allStreamsBut(drop ...string) []string {
	var out []string
	for _, name := range a2aProvisionedStreams {
		if !slices.Contains(drop, name) {
			out = append(out, name)
		}
	}
	return out
}

func TestTheVerdictCountsOnlyServersThatReadTheirStreamList(t *testing.T) {
	all := a2aProvisionedStreams
	for _, tc := range []struct {
		name        string
		replicas    int
		readings    []busServerReading
		wantKnown   bool
		wantMissing []string
	}{
		{"one server, all there", 1, []busServerReading{readingOf("", all...)}, true, nil},
		{"one server, one gone", 1, []busServerReading{readingOf("", allStreamsBut("KV_runtime-state")...)}, true, []string{"KV_runtime-state"}},
		{"one server, every stream gone", 1, []busServerReading{readingOf("")}, true, slices.Sorted(slices.Values(all))},
		{"one server, no answer", 1, nil, false, nil},
		{"one server, JetStream off", 1, []busServerReading{{jsz: true, disabled: true}}, false, nil},
		{"cluster, all answer", 3, []busServerReading{readingOf("n0", all...), readingOf("n0", all...), readingOf("n0", all...)}, true, nil},
		{"cluster, one down", 3, []busServerReading{readingOf("n0", allStreamsBut("TASKS")...), readingOf("n0", allStreamsBut("TASKS")...)}, true, []string{"TASKS"}},
		{"cluster, two down", 3, []busServerReading{readingOf("n0")}, false, nil},
		{"cluster, a follower lacks one", 3, []busServerReading{readingOf("n0", all...), readingOf("n0", allStreamsBut("TASKS")...), readingOf("n0", all...)}, true, nil},
		{"cluster, no meta leader", 3, []busServerReading{readingOf(""), readingOf(""), readingOf("n0", all...)}, false, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := busStreamsVerdictFor(tc.replicas, tc.readings)
			if v.known != tc.wantKnown || !slices.Equal(v.missing, tc.wantMissing) {
				t.Errorf("verdict = %+v, want known=%v missing=%v", v, tc.wantKnown, tc.wantMissing)
			}
		})
	}
}

func TestTheReprovisionRecordRateLimits(t *testing.T) {
	now := busStreamsTestNow
	at := func(ago time.Duration) busReprovisionAttempt { return busReprovisionAttempt{At: now.Add(-ago)} }
	for _, tc := range []struct {
		name     string
		attempts []busReprovisionAttempt
		want     bool
		next     time.Time
	}{
		{"none", nil, true, time.Time{}},
		{"one inside the gap", []busReprovisionAttempt{at(10 * time.Minute)}, false, now.Add(5 * time.Minute)},
		{"one past the gap", []busReprovisionAttempt{at(20 * time.Minute)}, true, time.Time{}},
		{"three in the window", []busReprovisionAttempt{at(5 * time.Hour), at(3 * time.Hour), at(time.Hour)}, false, now.Add(time.Hour)},
		{"three, one past the window", []busReprovisionAttempt{at(7 * time.Hour), at(3 * time.Hour), at(time.Hour)}, true, time.Time{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ok, next := busReprovisionRecord{Attempts: tc.attempts}.allows(now)
			if ok != tc.want || !next.Equal(tc.next) {
				t.Errorf("allows = %v, %v; want %v, %v", ok, next, tc.want, tc.next)
			}
		})
	}
}

var busStreamsTestNow = time.Date(2026, 11, 10, 12, 0, 0, 0, time.UTC)

const busStreamsTestAgentUID = types.UID("agent-uid-1")

// stubBusStreamSource answers every scrape from bodies, keyed by address; a
// missing key is a failed scrape.
type stubBusStreamSource struct {
	bodies map[string]string
	calls  int
}

func (s *stubBusStreamSource) Scrape(_ context.Context, addr string) (busServerReading, error) {
	s.calls++
	body, ok := s.bodies[addr]
	if !ok {
		return busServerReading{}, errors.New("connection refused")
	}
	return foldBusStreamsBody(strings.NewReader(body))
}

type busStreamsRig struct {
	t        *testing.T
	cl       client.Client
	poller   *BusStreamPoller
	source   *stubBusStreamSource
	recorder *record.FakeRecorder
	deleted  []string
	now      time.Time
	patchErr error
	// podDeleteErr, when set, fails every pod delete.
	podDeleteErr error
}

func busStreamsAgent(replicas int) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.UID = busStreamsTestAgentUID
	agent.Status.BusReplicas = int32(replicas)
	agent.Status.Conditions = []metav1.Condition{{
		Type: busProvisionedConditionType, Status: metav1.ConditionTrue, Reason: "ProvisionJobComplete",
		LastTransitionTime: metav1.NewTime(busStreamsTestNow.Add(-24 * time.Hour)),
	}}
	return agent
}

var busStreamsJobSerial int

func busStreamsJob(agent *agentv1alpha1.PlatformAgent, created time.Time, state batchv1.JobConditionType) *batchv1.Job {
	job := buildA2AProvisionJob(agent)
	busStreamsJobSerial++
	job.UID = types.UID(fmt.Sprintf("job-%d", busStreamsJobSerial))
	job.CreationTimestamp = metav1.NewTime(created)
	job.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent", Name: agent.Name,
		UID: agent.UID, Controller: ptr.To(true),
	}}
	if state != "" {
		job.Status.Conditions = []batchv1.JobCondition{{Type: state, Status: corev1.ConditionTrue}}
	}
	return job
}

const busStreamsTestStatefulSetUID = types.UID("sts-uid-1")

func busStreamsStatefulSet(agent *agentv1alpha1.PlatformAgent, replicas int32, ready int32) *appsv1.StatefulSet {
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{
			Name: a2aNATSName(agent), Namespace: agent.Namespace, Generation: 2, UID: busStreamsTestStatefulSetUID,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent", Name: agent.Name,
				UID: agent.UID, Controller: ptr.To(true),
			}},
		},
		Spec: appsv1.StatefulSetSpec{Replicas: ptr.To(replicas)},
		Status: appsv1.StatefulSetStatus{
			ObservedGeneration: 2, Replicas: replicas, ReadyReplicas: ready, UpdatedReplicas: replicas,
			CurrentRevision: "r1", UpdateRevision: "r1",
		},
	}
}

func busStreamsPod(agent *agentv1alpha1.PlatformAgent, ordinal int) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("%s-%d", a2aNATSName(agent), ordinal), Namespace: agent.Namespace,
			Labels: map[string]string{"app": a2aNATSName(agent), appsv1.ControllerRevisionHashLabelKey: "r1"},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: "apps/v1", Kind: "StatefulSet", Name: a2aNATSName(agent),
				UID: busStreamsTestStatefulSetUID, Controller: ptr.To(true),
			}},
		},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{
			Name:  a2aNATSExporterContainerName,
			Ports: []corev1.ContainerPort{{Name: a2aNATSMetricsPortName, ContainerPort: a2aNATSMetricsPort}},
		}}},
		Status: corev1.PodStatus{
			PodIP:             fmt.Sprintf("10.0.0.%d", ordinal+1),
			ContainerStatuses: []corev1.ContainerStatus{{Name: a2aNATSExporterContainerName, Ready: true}},
		},
	}
}

func busStreamsAddr(ordinal int) string { return fmt.Sprintf("10.0.0.%d:7777", ordinal+1) }

func clientPod(agent *agentv1alpha1.PlatformAgent, name string, selector map[string]string) *corev1.Pod {
	return &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: selector, UID: types.UID(name)}}
}

func controlledBy(owner client.Object, kind, apiVersion string) []metav1.OwnerReference {
	return []metav1.OwnerReference{{APIVersion: apiVersion, Kind: kind, Name: owner.GetName(), UID: owner.GetUID(), Controller: ptr.To(true)}}
}

// deploymentClient is one TASKS client as the cluster has it: the CR's
// Deployment, its ReplicaSet, and one pod of that ReplicaSet named pod.
func deploymentClient(agent *agentv1alpha1.PlatformAgent, name, pod string, selector map[string]string) []client.Object {
	dep := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, UID: types.UID(name + "-dep"),
		OwnerReferences: controlledBy(agent, "PlatformAgent", agentv1alpha1.GroupVersion.String())}}
	rs := &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Name: name + "-5d9c", Namespace: agent.Namespace, UID: types.UID(name + "-rs"), Labels: selector,
		OwnerReferences: controlledBy(dep, "Deployment", "apps/v1")}}
	p := clientPod(agent, pod, selector)
	p.OwnerReferences = controlledBy(rs, "ReplicaSet", "apps/v1")
	return []client.Object{dep, rs, p}
}

func newBusStreamsRig(t *testing.T, replicas int, objs ...client.Object) *busStreamsRig {
	t.Helper()
	// The operator's own identity, which the fence admits it by.
	t.Setenv(operatorNamespaceEnvVar, "kube-agents-system")
	t.Setenv(operatorServiceAccountEnvVar, "kube-agents-operator")
	agent := busStreamsAgent(replicas)
	rig := &busStreamsRig{t: t, recorder: record.NewFakeRecorder(32), now: busStreamsTestNow, source: &stubBusStreamSource{bodies: map[string]string{}}}
	all := []client.Object{agent, busStreamsStatefulSet(agent, int32(replicas), int32(replicas))}
	for i := 0; i < replicas; i++ {
		all = append(all, busStreamsPod(agent, i))
	}
	all = append(all, deploymentClient(agent, a2aGatewayName(agent), "test-agent-a2a-gateway-abc", a2aGatewayPodSelector(agent))...)
	all = append(all, deploymentClient(agent, agent.Name+gatewayAppSuffix, "test-agent-gateway-xyz", gatewayPodSelector(agent))...)
	all = append(all, objs...)
	rig.cl = fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(all...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				if _, isPod := obj.(*corev1.Pod); isPod && rig.podDeleteErr != nil {
					return rig.podDeleteErr
				}
				rig.deleted = append(rig.deleted, obj.GetName())
				return c.Delete(ctx, obj, opts...)
			},
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if rig.patchErr != nil {
					return rig.patchErr
				}
				return c.Patch(ctx, obj, patch, opts...)
			},
		}).
		Build()
	r := &PlatformAgentReconciler{Client: rig.cl, APIReader: rig.cl, Scheme: setupScheme(), Recorder: rig.recorder}
	rig.poller = &BusStreamPoller{r: r, source: rig.source, now: func() time.Time { return rig.now },
		streaks: map[types.UID]*busStreamStreak{}}
	return rig
}

// serve sets what every server's exporter answers.
func (rig *busStreamsRig) serve(replicas int, streams ...string) {
	for i := 0; i < replicas; i++ {
		leader := ""
		if replicas > 1 {
			leader = "nats-0"
		}
		rig.source.bodies[busStreamsAddr(i)] = exporterBody(true, leader, streams...)
	}
}

func (rig *busStreamsRig) agent() *agentv1alpha1.PlatformAgent {
	rig.t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := rig.cl.Get(context.Background(), types.NamespacedName{Namespace: "test-ns", Name: "test-agent"}, agent); err != nil {
		rig.t.Fatalf("get agent: %v", err)
	}
	return agent
}

func (rig *busStreamsRig) poll() {
	rig.t.Helper()
	if err := rig.poller.pollAgent(context.Background(), rig.agent()); err != nil {
		rig.t.Fatalf("poll: %v", err)
	}
	rig.now = rig.now.Add(busStreamsPollInterval)
}

func (rig *busStreamsRig) condition() *metav1.Condition {
	return meta.FindStatusCondition(rig.agent().Status.Conditions, busStreamsPresentConditionType)
}

func (rig *busStreamsRig) jobExists() bool {
	agent := rig.agent()
	err := rig.cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: buildA2AProvisionJob(agent).Name}, &batchv1.Job{})
	return err == nil
}

// recreateJob stands for the reconcile creating the Job again, and its run.
func (rig *busStreamsRig) recreateJob(state batchv1.JobConditionType) {
	rig.t.Helper()
	if err := rig.cl.Create(context.Background(), busStreamsJob(rig.agent(), rig.now, state)); err != nil {
		rig.t.Fatalf("recreate job: %v", err)
	}
}

func (rig *busStreamsRig) events() []string {
	var out []string
	for {
		select {
		case e := <-rig.recorder.Events:
			out = append(out, e)
		default:
			return out
		}
	}
}

func TestABusWithEveryStreamReadsPresent(t *testing.T) {
	for _, replicas := range []int{1, 3} {
		t.Run(fmt.Sprintf("busReplicas %d", replicas), func(t *testing.T) {
			agent := busStreamsAgent(replicas)
			rig := newBusStreamsRig(t, replicas, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
			rig.serve(replicas, a2aProvisionedStreams...)
			rig.poll()
			if c := rig.condition(); c == nil || c.Status != metav1.ConditionTrue || c.Reason != busStreamsReasonAllPresent {
				t.Fatalf("condition = %+v, want True/AllPresent", c)
			}
			if len(rig.deleted) != 0 {
				t.Errorf("deleted %v on a healthy bus", rig.deleted)
			}
		})
	}
}

// The 10-29 case: a bus that lost a KV bucket after provisioning. Two polls
// say nothing; the third deletes the completed Job, records the attempt, and
// says so. No client is restarted: the bucket has no fixed durable.
func TestAMissingBucketIsConfirmedOnThreePollsThenTheJobRunsAgain(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("KV_runtime-state")...)
	rig.poll()
	rig.poll()
	if c := rig.condition(); c != nil || len(rig.deleted) != 0 {
		t.Fatalf("after two polls: condition %+v, deleted %v; want neither", c, rig.deleted)
	}
	rig.poll()
	if rig.jobExists() {
		t.Fatalf("the completed provision Job is still there after a confirmed loss")
	}
	c := rig.condition()
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != busStreamsReasonReprovisioning || !strings.Contains(c.Message, "KV_runtime-state") {
		t.Fatalf("condition = %+v, want False/Reprovisioning naming KV_runtime-state", c)
	}
	record := readBusReprovisionRecord(rig.agent())
	if len(record.Attempts) != 1 || !slices.Equal(record.Attempts[0].Missing, []string{"KV_runtime-state"}) {
		t.Errorf("record = %+v, want one attempt for KV_runtime-state", record)
	}
	if events := rig.events(); len(events) != 1 || !strings.Contains(events[0], busStreamsMissingEventReason) {
		t.Errorf("events = %v, want one %s", events, busStreamsMissingEventReason)
	}

	// The reconcile recreates the Job and it completes; the bucket is back.
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("condition after the re-run = %+v, want True", c)
	}
	if slices.Contains(rig.deleted, "test-agent-a2a-gateway-abc") || slices.Contains(rig.deleted, "test-agent-gateway-xyz") {
		t.Errorf("deleted %v: no client holds a durable on a KV bucket", rig.deleted)
	}
	if record := readBusReprovisionRecord(rig.agent()); !record.Attempts[0].Settled || record.Attempts[0].Restarted {
		t.Errorf("record = %+v, want settled, not restarted", record)
	}
}

// TASKS has the two fixed durables, so its recovery ends with the A2A gateway
// and the agent pod restarted, once.
func TestARecreatedTasksStreamRestartsItsClientsOnce(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.events()
	rig.poll()
	for _, pod := range []string{"test-agent-a2a-gateway-abc", "test-agent-gateway-xyz"} {
		if !slices.Contains(rig.deleted, pod) {
			t.Errorf("deleted %v, want %s restarted", rig.deleted, pod)
		}
	}
	before := len(rig.deleted)
	rig.poll()
	if len(rig.deleted) != before {
		t.Errorf("a second healthy poll deleted %v", rig.deleted[before:])
	}
}

// A re-run that completes without bringing the stream back stops the
// re-runs: the condition says why, and the Job is left alone.
func TestAReRunThatDoesNotRestoreStops(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("DIRECTORY")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	for i := 0; i < 6; i++ {
		rig.poll()
	}
	if !rig.jobExists() {
		t.Fatalf("the Job was deleted again after a re-run that did not restore")
	}
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionFalse || c.Reason != busStreamsReasonDidNotRestore {
		t.Fatalf("condition = %+v, want False/%s", c, busStreamsReasonDidNotRestore)
	}
}

func TestReRunsAreRateLimited(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	// An attempt ten minutes before the third poll, for another set, whose
	// Job is older than it: the gap applies.
	live := rig.agent()
	live.Annotations = map[string]string{busStreamsReprovisionAnnotation: fmt.Sprintf(`{"attempts":[{"at":%q,"missing":["TOPICS-STATE"],"settled":true}]}`,
		busStreamsTestNow.Add(-10*time.Minute).Format(time.RFC3339))}
	if err := rig.cl.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	rig.now = busStreamsTestNow.Add(-2 * busStreamsPollInterval)
	rig.serve(1, allStreamsBut("PROFILE_SUBMITS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if !rig.jobExists() {
		t.Fatalf("the Job was deleted inside the rate limit")
	}
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonMissing || !strings.Contains(c.Message, "next re-run is allowed") {
		t.Errorf("condition = %+v, want StreamsMissing with the next allowed time", c)
	}
}

func TestAFailedJobIsNotReRun(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobFailed))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if !rig.jobExists() {
		t.Fatalf("a failed Job was deleted")
	}
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionFalse || c.Reason != busStreamsReasonMissing || !strings.Contains(c.Message, "failed") {
		t.Errorf("condition = %+v, want False/StreamsMissing naming the failed Job", c)
	}
}

// Missing sets that differ poll to poll confirm only what every poll found.
func TestOnlyWhatEveryPollFoundMissingIsConfirmed(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	for _, gone := range [][]string{{"TASKS", "DIRECTORY"}, {"DIRECTORY"}, {"DIRECTORY", "KV_cap"}} {
		rig.serve(1, allStreamsBut(gone...)...)
		rig.poll()
	}
	record := readBusReprovisionRecord(rig.agent())
	if len(record.Attempts) != 1 || !slices.Equal(record.Attempts[0].Missing, []string{"DIRECTORY"}) {
		t.Errorf("record = %+v, want one attempt for DIRECTORY alone", record)
	}
}

// No verdict while the bus rolls, converts or provisions, and the run starts
// again afterwards: two misses, a rolling poll, two misses is not three.
func TestNoVerdictWhileTheBusIsNotAtRest(t *testing.T) {
	for _, tc := range []struct {
		name  string
		upset func(rig *busStreamsRig)
		reset func(rig *busStreamsRig)
	}{
		{"a server not ready", func(rig *busStreamsRig) {
			rig.setStatefulSet(busStreamsStatefulSet(rig.agent(), 1, 0))
		}, func(rig *busStreamsRig) {
			rig.setStatefulSet(busStreamsStatefulSet(rig.agent(), 1, 1))
		}},
		{"converting to three servers", func(rig *busStreamsRig) {
			agent := rig.agent()
			agent.Status.BusReplicas = 3
			if err := rig.cl.Status().Update(context.Background(), agent); err != nil {
				rig.t.Fatal(err)
			}
		}, func(rig *busStreamsRig) {
			agent := rig.agent()
			agent.Status.BusReplicas = 1
			if err := rig.cl.Status().Update(context.Background(), agent); err != nil {
				rig.t.Fatal(err)
			}
		}},
		{"the provision Job running", func(rig *busStreamsRig) {
			rig.replaceJob(batchv1.JobConditionType(""))
		}, func(rig *busStreamsRig) {
			rig.replaceJob(batchv1.JobComplete)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := busStreamsAgent(1)
			rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
			rig.serve(1, allStreamsBut("TASKS")...)
			rig.poll()
			rig.poll()
			tc.upset(rig)
			rig.poll()
			tc.reset(rig)
			rig.poll()
			rig.poll()
			if len(rig.deleted) != 0 {
				t.Errorf("deleted %v across an interrupted run", rig.deleted)
			}
		})
	}
}

func (rig *busStreamsRig) setStatefulSet(sts *appsv1.StatefulSet) {
	rig.t.Helper()
	live := &appsv1.StatefulSet{}
	if err := rig.cl.Get(context.Background(), client.ObjectKeyFromObject(sts), live); err != nil {
		rig.t.Fatal(err)
	}
	live.Status = sts.Status
	if err := rig.cl.Status().Update(context.Background(), live); err != nil {
		rig.t.Fatal(err)
	}
}

func (rig *busStreamsRig) replaceJob(state batchv1.JobConditionType) {
	rig.t.Helper()
	agent := rig.agent()
	existing := &batchv1.Job{}
	if err := rig.cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: buildA2AProvisionJob(agent).Name}, existing); err != nil {
		rig.t.Fatal(err)
	}
	existing.Status.Conditions = nil
	if state != "" {
		existing.Status.Conditions = []batchv1.JobCondition{{Type: state, Status: corev1.ConditionTrue}}
	}
	if err := rig.cl.Status().Update(context.Background(), existing); err != nil {
		rig.t.Fatal(err)
	}
}

func TestAnExporterThatWillNotAnswerReadsUnreachableOnTheThirdPoll(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.source.bodies[busStreamsAddr(0)] = exporterBody(false, "")
	rig.poll()
	rig.poll()
	if c := rig.condition(); c != nil {
		t.Fatalf("condition after two polls = %+v, want none", c)
	}
	rig.poll()
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionUnknown || c.Reason != busStreamsReasonExporterUnreachable {
		t.Fatalf("condition = %+v, want Unknown/%s", c, busStreamsReasonExporterUnreachable)
	}
	if len(rig.deleted) != 0 {
		t.Errorf("deleted %v without a verdict", rig.deleted)
	}
}

func TestAnOmittedExporterReadsDetectionUnavailableAndIsNotScraped(t *testing.T) {
	t.Setenv(a2aNATSImageEnvVar, "mirror.example/nats:2.11")
	t.Setenv(a2aNATSExporterImageEnvVar, "")
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1)
	rig.poll()
	c := rig.condition()
	if c == nil || c.Status != metav1.ConditionUnknown || c.Reason != busStreamsReasonDetectionUnavailable || !strings.Contains(c.Message, a2aNATSExporterImageEnvVar) {
		t.Fatalf("condition = %+v, want Unknown/%s naming %s", c, busStreamsReasonDetectionUnavailable, a2aNATSExporterImageEnvVar)
	}
	if rig.source.calls != 0 || len(rig.deleted) != 0 {
		t.Errorf("scraped %d times and deleted %v with the exporter omitted", rig.source.calls, rig.deleted)
	}
	if busStreamsMissingCondition(rig.agent()) != nil {
		t.Errorf("an Unknown condition counts toward Degraded")
	}
}

func TestTheConditionGoesWhenTheBusIsNotProvisioned(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	live := rig.agent()
	meta.RemoveStatusCondition(&live.Status.Conditions, busProvisionedConditionType)
	if err := rig.cl.Status().Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	rig.poll()
	if c := rig.condition(); c != nil {
		t.Errorf("condition = %+v on an unprovisioned bus, want none", c)
	}
}

func TestAFalseConditionMakesTheCRDegraded(t *testing.T) {
	agent := busStreamsAgent(1)
	agent.Status.Conditions = append(agent.Status.Conditions, metav1.Condition{
		Type: busStreamsPresentConditionType, Status: metav1.ConditionFalse, Reason: busStreamsReasonMissing, Message: "Missing: TASKS",
	})
	if c := busStreamsMissingCondition(agent); c == nil || c.Message != "Missing: TASKS" {
		t.Errorf("busStreamsMissingCondition = %+v, want the False condition", c)
	}
	agent.Spec.Mode = ptr.To("today")
	if c := busStreamsMissingCondition(agent); c != nil {
		t.Errorf("busStreamsMissingCondition = %+v under today, want nil", c)
	}
}

// The list selects by a label any pod in the namespace can carry, and what a
// pod serves decides whether the Job is deleted and the gateway restarted. A
// pod the bus StatefulSet doesn't control, one on an old revision, or one on
// the host network is not read, so a stand-in claiming every stream is gone
// moves nothing.
func TestOnlyTheBusStatefulSetsOwnPodsAreRead(t *testing.T) {
	for _, tc := range []struct {
		name  string
		alter func(pod *corev1.Pod)
	}{
		{"not controlled by the StatefulSet", func(pod *corev1.Pod) { pod.OwnerReferences = nil }},
		{"controlled by another StatefulSet", func(pod *corev1.Pod) { pod.OwnerReferences[0].UID = "other" }},
		{"an old revision", func(pod *corev1.Pod) { pod.Labels[appsv1.ControllerRevisionHashLabelKey] = "r0" }},
		{"on the host network", func(pod *corev1.Pod) { pod.Spec.HostNetwork = true }},
		// Owner, revision and all copied: only the name gives it away.
		{"a copy under another name", func(*corev1.Pod) {}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := busStreamsAgent(1)
			stray := busStreamsPod(agent, 5)
			stray.Name = "stray"
			tc.alter(stray)
			rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete), stray)
			rig.serve(1, a2aProvisionedStreams...)
			rig.source.bodies[busStreamsAddr(5)] = exporterBody(true, "")
			// The real server stops answering, so only the stray pod could.
			delete(rig.source.bodies, busStreamsAddr(0))
			for i := 0; i < 4; i++ {
				rig.poll()
			}
			if len(rig.deleted) != 0 {
				t.Errorf("deleted %v on a stray pod's word", rig.deleted)
			}
			if c := rig.condition(); c == nil || c.Reason != busStreamsReasonExporterUnreachable {
				t.Errorf("condition = %+v, want ExporterUnreachable: the stray pod is not a server", c)
			}
		})
	}
}

// A StatefulSet of the bus's name this CR doesn't control gives no verdict.
func TestAStatefulSetTheCRDoesNotControlGivesNoVerdict(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	live := &appsv1.StatefulSet{}
	if err := rig.cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, live); err != nil {
		t.Fatal(err)
	}
	live.OwnerReferences = nil
	if err := rig.cl.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	rig.serve(1)
	for i := 0; i < 4; i++ {
		rig.poll()
	}
	if len(rig.deleted) != 0 || rig.source.calls != 0 {
		t.Errorf("scraped %d times and deleted %v under a StatefulSet the CR does not control", rig.source.calls, rig.deleted)
	}
}

// The pod at a server's own name counts only when the StatefulSet controls it,
// on its current revision, off the host network: a pod recreated by someone
// else under that name, or a stale one, is not a server.
func TestTheServersOwnPodMustBeTheStatefulSets(t *testing.T) {
	for _, tc := range []struct {
		name  string
		alter func(pod *corev1.Pod)
	}{
		{"not controlled by the StatefulSet", func(pod *corev1.Pod) { pod.OwnerReferences = nil }},
		{"an old revision", func(pod *corev1.Pod) { pod.Labels[appsv1.ControllerRevisionHashLabelKey] = "r0" }},
		{"on the host network", func(pod *corev1.Pod) { pod.Spec.HostNetwork = true }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := busStreamsAgent(1)
			rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
			pod := &corev1.Pod{}
			if err := rig.cl.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent) + "-0"}, pod); err != nil {
				t.Fatal(err)
			}
			tc.alter(pod)
			if err := rig.cl.Update(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			rig.serve(1)
			for i := 0; i < 4; i++ {
				rig.poll()
			}
			if len(rig.deleted) != 0 || rig.source.calls != 0 {
				t.Errorf("scraped %d times and deleted %v", rig.source.calls, rig.deleted)
			}
		})
	}
}

// A StatefulSet still on three servers while the CR resolves to one is
// mid-conversion: no verdict, even with every server saying TASKS is gone.
func TestNoVerdictWhileTheServerCountDiffersFromTheCRs(t *testing.T) {
	agent := busStreamsAgent(3)
	rig := newBusStreamsRig(t, 3, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	live := rig.agent()
	live.Status.BusReplicas = 1
	if err := rig.cl.Status().Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	// The server count is in the Job's spec, so the CR's new count names a new
	// Job; give it a completed one, so the server count alone holds the verdict.
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(3, allStreamsBut("TASKS")...)
	for i := 0; i < 4; i++ {
		rig.poll()
	}
	if len(rig.deleted) != 0 {
		t.Errorf("deleted %v mid-conversion", rig.deleted)
	}
}

// Polls with nothing to judge by count in a row only: a poll the gate skips
// starts the count again.
func TestAGatedPollRestartsTheUnreachableCount(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.source.bodies[busStreamsAddr(0)] = exporterBody(false, "")
	rig.poll()
	rig.poll()
	rig.setStatefulSet(busStreamsStatefulSet(rig.agent(), 1, 0))
	rig.poll()
	rig.setStatefulSet(busStreamsStatefulSet(rig.agent(), 1, 1))
	rig.poll()
	if c := rig.condition(); c != nil {
		t.Errorf("condition = %+v, want none: the gated poll broke the run", c)
	}
}

// Through Reconcile: a False BusStreamsPresent makes the CR Degraded with its
// message, and a True one clears it.
func TestTheReconcileReportsMissingBusStreamsAsDegraded(t *testing.T) {
	r, cl, req := settledStuckPodReconciler(t, nil)
	ctx := context.Background()
	set := func(status metav1.ConditionStatus, reason, message string) {
		t.Helper()
		agent := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, agent); err != nil {
			t.Fatal(err)
		}
		meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{Type: busStreamsPresentConditionType, Status: status, Reason: reason, Message: message})
		if err := cl.Status().Update(ctx, agent); err != nil {
			t.Fatal(err)
		}
		busGatePass(t, ctx, r, req, "bus streams "+reason)
	}
	degraded := func() *metav1.Condition {
		t.Helper()
		agent := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, agent); err != nil {
			t.Fatal(err)
		}
		return meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	}
	set(metav1.ConditionFalse, busStreamsReasonMissing, "Missing from the A2A bus on 3 polls in a row: TASKS.")
	if c := degraded(); c == nil || c.Status != metav1.ConditionTrue || c.Reason != conditionReasonA2ABusStreamsMissing || !strings.Contains(c.Message, "TASKS") {
		t.Fatalf("Degraded = %+v, want True/%s naming TASKS", c, conditionReasonA2ABusStreamsMissing)
	}
	set(metav1.ConditionTrue, busStreamsReasonAllPresent, "All present.")
	if c := degraded(); c != nil {
		t.Errorf("Degraded = %+v after the streams came back, want none", c)
	}
}

// A stream restored once and lost again later is re-provisioned again: the
// stop applies to a re-run that did not restore, not to every later loss.
func TestASecondLossAfterARestoreIsReProvisioned(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	rig.now = rig.now.Add(7 * 24 * time.Hour)
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if rig.jobExists() {
		t.Fatalf("the second loss was not re-provisioned; condition %+v", rig.condition())
	}
}

// The re-run is the Job with another UID, however its creation time, which
// the API server keeps in whole seconds, compares with the attempt's.
func TestAReRunInTheSameSecondStillCounts(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.now = rig.now.Add(700 * time.Millisecond)
	rig.poll()
	attemptAt := readBusReprovisionRecord(rig.agent()).Attempts[0].At
	if err := rig.cl.Create(context.Background(), busStreamsJob(rig.agent(), attemptAt.Truncate(time.Second), batchv1.JobComplete)); err != nil {
		t.Fatal(err)
	}
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if !slices.Contains(rig.deleted, "test-agent-a2a-gateway-abc") {
		t.Errorf("deleted %v: a re-run created in the attempt's second restarted nothing", rig.deleted)
	}
}

// TASKS back while another stream isn't: its readers restart now, and the
// other stream stops the re-runs.
func TestTasksBackRestartsItsClientsEvenWithAnotherStreamStillMissing(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS", "KV_cap")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, allStreamsBut("KV_cap")...)
	rig.poll()
	if !slices.Contains(rig.deleted, "test-agent-a2a-gateway-abc") || !slices.Contains(rig.deleted, "test-agent-gateway-xyz") {
		t.Errorf("deleted %v, want both TASKS readers restarted", rig.deleted)
	}
	rig.poll()
	rig.poll()
	rig.poll()
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonDidNotRestore || !strings.Contains(c.Message, busStreamsReprovisionAnnotation) {
		t.Errorf("condition = %+v, want %s naming the annotation to clear", c, busStreamsReasonDidNotRestore)
	}
}

// The record is written before the Job is deleted: a record that can't be
// written leaves the Job alone, so the limits can't be lost.
func TestARecordThatCannotBeWrittenLeavesTheJobAlone(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.patchErr = errors.New("admission webhook refused the update")
	if err := rig.poller.pollAgent(context.Background(), rig.agent()); err == nil {
		t.Fatalf("poll succeeded with the record unwritable")
	}
	if !rig.jobExists() {
		t.Errorf("the Job was deleted without a record of it")
	}
}

func TestAnOperatorWithoutItsIdentityReadsDetectionUnavailable(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	t.Setenv(operatorNamespaceEnvVar, "")
	rig.serve(1)
	rig.poll()
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonDetectionUnavailable || rig.source.calls != 0 {
		t.Errorf("condition = %+v after %d scrapes, want DetectionUnavailable and none", c, rig.source.calls)
	}
}

// Every half of "at rest" holds the verdict: the spec observed, every replica
// updated, one revision.
func TestNoVerdictUntilTheStatefulSetIsAtRest(t *testing.T) {
	for _, tc := range []struct {
		name  string
		alter func(sts *appsv1.StatefulSet)
	}{
		{"spec not observed", func(sts *appsv1.StatefulSet) { sts.Status.ObservedGeneration = 1 }},
		{"a replica not updated", func(sts *appsv1.StatefulSet) { sts.Status.UpdatedReplicas = 0 }},
		{"two revisions", func(sts *appsv1.StatefulSet) { sts.Status.CurrentRevision = "r0" }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := busStreamsAgent(1)
			rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
			sts := busStreamsStatefulSet(agent, 1, 1)
			tc.alter(sts)
			rig.setStatefulSet(sts)
			rig.serve(1)
			for i := 0; i < 4; i++ {
				rig.poll()
			}
			if len(rig.deleted) != 0 || rig.source.calls != 0 {
				t.Errorf("scraped %d times and deleted %v", rig.source.calls, rig.deleted)
			}
		})
	}
}

// A judged poll ends a run of polls with nothing to judge by.
func TestAJudgedPollRestartsTheUnreachableCount(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.source.bodies[busStreamsAddr(0)] = exporterBody(false, "")
	rig.poll()
	rig.poll()
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	rig.source.bodies[busStreamsAddr(0)] = exporterBody(false, "")
	rig.poll()
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonAllPresent {
		t.Errorf("condition = %+v, want AllPresent still: the judged poll broke the run", c)
	}
}

// A CRD that predates status.busReplicas prunes it: the running StatefulSet's
// count is the bus's then, and the check still judges a one-server bus.
func TestABusWhoseCountTheStatusDoesNotRecordIsStillJudged(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	live := rig.agent()
	live.Status.BusReplicas = 0
	if err := rig.cl.Status().Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	// The Job's name follows the stream replicas, so give the CR as it now
	// reads a completed Job of its own.
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonAllPresent {
		t.Errorf("condition = %+v, want AllPresent on a one-server bus whose count the status does not record", c)
	}
}

// The provision Job is found by its labels and owner, not by the name the
// reconcile would render from the CR as stored, which can differ from the
// render the reconcile made (a pruned status.busReplicas): any provision Job
// of the CR's counts, the newest deciding and a running one gating.
func TestTheProvisionJobIsFoundByItsLabelsNotItsName(t *testing.T) {
	agent := busStreamsAgent(1)
	job := busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete)
	job.Name = agent.Name + a2aProvisionJobNameInfix + "0ther000"
	rig := newBusStreamsRig(t, 1, job)
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if !slices.Contains(rig.deleted, job.Name) {
		t.Errorf("deleted %v, want the CR's completed provision Job %s", rig.deleted, job.Name)
	}
}

func TestAnyRunningProvisionJobGatesTheVerdict(t *testing.T) {
	agent := busStreamsAgent(1)
	running := busStreamsJob(agent, busStreamsTestNow.Add(-2*time.Hour), "")
	running.Name = agent.Name + a2aProvisionJobNameInfix + "running0"
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete), running)
	rig.serve(1, allStreamsBut("TASKS")...)
	for i := 0; i < 4; i++ {
		rig.poll()
	}
	if len(rig.deleted) != 0 {
		t.Errorf("deleted %v while a provision Job was running", rig.deleted)
	}
}

// A provision-labelled Job the CR doesn't control is not its provision Job:
// with no Job of its own, the CR's poll waits for the reconcile.
func TestAProvisionJobTheCRDoesNotControlIsIgnored(t *testing.T) {
	agent := busStreamsAgent(1)
	foreign := busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete)
	foreign.OwnerReferences = nil
	rig := newBusStreamsRig(t, 1, foreign)
	rig.serve(1, allStreamsBut("TASKS")...)
	for i := 0; i < 4; i++ {
		rig.poll()
	}
	if len(rig.deleted) != 0 {
		t.Errorf("deleted %v: a Job the CR does not control", rig.deleted)
	}
}

// The newest of the CR's provision Jobs decides: an older failed run beside a
// newer completed one doesn't hold the re-run.
func TestTheNewestProvisionJobDecides(t *testing.T) {
	agent := busStreamsAgent(1)
	older := busStreamsJob(agent, busStreamsTestNow.Add(-3*time.Hour), batchv1.JobFailed)
	older.Name = agent.Name + a2aProvisionJobNameInfix + "older000"
	newer := busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete)
	rig := newBusStreamsRig(t, 1, older, newer)
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if !slices.Contains(rig.deleted, newer.Name) {
		t.Errorf("deleted %v, want the newest, completed Job %s re-run", rig.deleted, newer.Name)
	}
}

// A stop for names a re-run did not bring back lasts only until they are back:
// fixed by hand, the bus reads AllPresent, and a later loss of the same name
// is re-provisioned rather than stopped.
func TestAStopIsLiftedOnceItsStreamsAreBack(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("DIRECTORY")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	for i := 0; i < 4; i++ {
		rig.poll()
	}
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonDidNotRestore {
		t.Fatalf("condition = %+v, want %s before the fix", c, busStreamsReasonDidNotRestore)
	}

	// Fixed by hand.
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionTrue || c.Reason != busStreamsReasonAllPresent {
		t.Fatalf("condition after the fix = %+v, want True/AllPresent", c)
	}
	if last := readBusReprovisionRecord(rig.agent()).last(); last == nil || !last.Restored {
		t.Fatalf("record's last attempt = %+v, want it marked restored once its streams were back", last)
	}

	// Lost again, past the gap but inside the window the attempt is kept in.
	rig.now = rig.now.Add(time.Hour)
	rig.serve(1, allStreamsBut("DIRECTORY")...)
	rig.poll()
	rig.poll()
	rig.poll()
	if rig.jobExists() {
		t.Fatalf("the later loss was stopped by the lifted stop; condition %+v", rig.condition())
	}
	if c := rig.condition(); c == nil || c.Reason != busStreamsReasonReprovisioning {
		t.Errorf("condition = %+v, want %s", c, busStreamsReasonReprovisioning)
	}
}

// A restart whose deletes fail stays owed: the condition doesn't go
// True/AllPresent over clients still without their TASKS consumer, and the
// next poll retries it.
func TestAFailedRestartIsRetriedBeforeTheBusReadsPresent(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)

	rig.podDeleteErr = errors.New("etcdserver: request timed out")
	for i := 0; i < 2; i++ {
		if err := rig.poller.pollAgent(context.Background(), rig.agent()); err == nil {
			t.Fatalf("poll %d succeeded with the restart failing", i+1)
		}
		rig.now = rig.now.Add(busStreamsPollInterval)
		if c := rig.condition(); c == nil || c.Status != metav1.ConditionFalse || c.Reason != busStreamsReasonReprovisioning || !strings.Contains(c.Message, "restarting the A2A gateway and the agent pod failed") {
			t.Fatalf("condition = %+v with the restart still owed, want False/%s naming the failed restart", c, busStreamsReasonReprovisioning)
		}
		if last := readBusReprovisionRecord(rig.agent()).last(); last == nil || !last.Settled || !last.RestartOwed || last.Restarted {
			t.Fatalf("record's last attempt = %+v, want settled, restart owed, not restarted", last)
		}
		if events := rig.events(); !slices.ContainsFunc(events, func(e string) bool {
			return strings.HasPrefix(e, corev1.EventTypeWarning+" "+busStreamsRestartFailedEventReason)
		}) {
			t.Fatalf("events = %v, want a Warning %s on the failed restart", events, busStreamsRestartFailedEventReason)
		}
	}

	rig.podDeleteErr = nil
	rig.poll()
	for _, pod := range []string{"test-agent-a2a-gateway-abc", "test-agent-gateway-xyz"} {
		if !slices.Contains(rig.deleted, pod) {
			t.Errorf("deleted %v, want %s restarted on the retry", rig.deleted, pod)
		}
	}
	if c := rig.condition(); c == nil || c.Status != metav1.ConditionTrue || c.Reason != busStreamsReasonAllPresent {
		t.Errorf("condition after the retry = %+v, want True/AllPresent", c)
	}
	if last := readBusReprovisionRecord(rig.agent()).last(); last == nil || !last.Restarted {
		t.Errorf("record's last attempt = %+v, want restarted", last)
	}
	before := len(rig.deleted)
	rig.poll()
	if len(rig.deleted) != before {
		t.Errorf("a poll after the restart deleted %v again", rig.deleted[before:])
	}
}

// Only each client's own pods are restarted: a pod that carries a client's app
// label without its workload controlling it is left alone, and so is a
// look-alike Deployment the CR doesn't control.
func TestOnlyTheClientsOwnPodsAreRestarted(t *testing.T) {
	agent := busStreamsAgent(1)
	stray := clientPod(agent, "test-agent-a2a-gateway-stray", a2aGatewayPodSelector(agent))
	foreignRS := &appsv1.ReplicaSet{ObjectMeta: metav1.ObjectMeta{Name: "other-rs", Namespace: agent.Namespace, UID: "other-rs", Labels: gatewayPodSelector(agent),
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: "other", UID: "other-dep", Controller: ptr.To(true)}}}}
	foreign := clientPod(agent, "test-agent-gateway-foreign", gatewayPodSelector(agent))
	foreign.OwnerReferences = controlledBy(foreignRS, "ReplicaSet", "apps/v1")
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete), stray, foreignRS, foreign)
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	for _, pod := range []string{"test-agent-a2a-gateway-abc", "test-agent-gateway-xyz"} {
		if !slices.Contains(rig.deleted, pod) {
			t.Errorf("deleted %v, want %s restarted", rig.deleted, pod)
		}
	}
	for _, pod := range []string{stray.Name, foreign.Name} {
		if slices.Contains(rig.deleted, pod) {
			t.Errorf("deleted %s, which no client workload of the CR controls", pod)
		}
	}
}

// The agent pod can run under a StatefulSet; its pods are its own directly.
func TestAStatefulSetClientsPodsAreRestarted(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	ctx := context.Background()
	// Swap the agent gateway's Deployment for a StatefulSet.
	if err := rig.cl.Delete(ctx, &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + gatewayAppSuffix, Namespace: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	sts := &appsv1.StatefulSet{ObjectMeta: metav1.ObjectMeta{Name: agent.Name + gatewayAppSuffix, Namespace: agent.Namespace, UID: "agent-gw-sts",
		OwnerReferences: controlledBy(agent, "PlatformAgent", agentv1alpha1.GroupVersion.String())}}
	pod := clientPod(agent, "test-agent-gateway-0", gatewayPodSelector(agent))
	pod.OwnerReferences = controlledBy(sts, "StatefulSet", "apps/v1")
	for _, obj := range []client.Object{sts, pod} {
		if err := rig.cl.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	rig.deleted = nil
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if !slices.Contains(rig.deleted, "test-agent-gateway-0") {
		t.Errorf("deleted %v, want the StatefulSet's pod restarted", rig.deleted)
	}
	if slices.Contains(rig.deleted, "test-agent-gateway-xyz") {
		t.Errorf("deleted the old ReplicaSet's pod, whose Deployment is gone")
	}
}

// A client Deployment of the expected name that the CR doesn't control is not
// the client's: its pods are left alone.
func TestALookAlikeDeploymentsPodsAreNotRestarted(t *testing.T) {
	agent := busStreamsAgent(1)
	rig := newBusStreamsRig(t, 1, busStreamsJob(agent, busStreamsTestNow.Add(-time.Hour), batchv1.JobComplete))
	ctx := context.Background()
	dep := &appsv1.Deployment{}
	if err := rig.cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aGatewayName(agent)}, dep); err != nil {
		t.Fatal(err)
	}
	dep.OwnerReferences = []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: "someone-else", UID: "someone-else", Controller: ptr.To(true)}}
	if err := rig.cl.Update(ctx, dep); err != nil {
		t.Fatal(err)
	}
	rig.serve(1, allStreamsBut("TASKS")...)
	rig.poll()
	rig.poll()
	rig.poll()
	rig.recreateJob(batchv1.JobComplete)
	rig.serve(1, a2aProvisionedStreams...)
	rig.poll()
	if slices.Contains(rig.deleted, "test-agent-a2a-gateway-abc") {
		t.Errorf("deleted the pod of a Deployment the CR doesn't control")
	}
	if !slices.Contains(rig.deleted, "test-agent-gateway-xyz") {
		t.Errorf("deleted %v, want the agent pod still restarted", rig.deleted)
	}
}
