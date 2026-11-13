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
	"io"
	"net/http"
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
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	metaN0 = "test-agent-a2a-nats-0"
	metaN1 = "test-agent-a2a-nats-1"
	metaN2 = "test-agent-a2a-nats-2"
)

// leaderView is the leader's /jsz: it alone lists the other members.
func leaderView(pod string, peers ...a2aBusMetaPeer) a2aBusServerView {
	return a2aBusServerView{Pod: pod, Leader: pod, ClusterSize: 3, Replicas: peers}
}

func followerView(pod, leader string) a2aBusServerView {
	return a2aBusServerView{Pod: pod, Leader: leader, ClusterSize: 3}
}

func current(pod string) a2aBusMetaPeer { return a2aBusMetaPeer{Name: pod, Current: true} }

func formedViews() []a2aBusServerView {
	return []a2aBusServerView{
		leaderView(metaN0, current(metaN1), current(metaN2)),
		followerView(metaN1, metaN0),
		followerView(metaN2, metaN0),
	}
}

// lateJoinerViews is CI's shape (#5708): nats-2 holds its routes but never
// reached the meta leader, which the first two elected before it started.
func lateJoinerViews() []a2aBusServerView {
	return []a2aBusServerView{
		leaderView(metaN0, current(metaN1)),
		followerView(metaN1, metaN0),
		{Pod: metaN2, ClusterSize: 3},
	}
}

func TestTheMetaGroupVerdict(t *testing.T) {
	for _, tc := range []struct {
		name                       string
		views                      []a2aBusServerView
		known, formed              bool
		leader                     string
		outside, unreadableContain []string
	}{
		{"formed", formedViews(), true, true, metaN0, nil, nil},
		{"a late joiner the leader doesn't list", lateJoinerViews(), true, false, metaN0, []string{metaN2}, nil},
		{"a member the leader lists but not as current", []a2aBusServerView{
			leaderView(metaN0, current(metaN1), a2aBusMetaPeer{Name: metaN2}),
			followerView(metaN1, metaN0), followerView(metaN2, metaN0),
		}, true, false, metaN0, []string{metaN2}, nil},
		{"a member the leader lists as offline", []a2aBusServerView{
			leaderView(metaN0, current(metaN1), a2aBusMetaPeer{Name: metaN2, Current: true, Offline: true}),
			followerView(metaN1, metaN0), followerView(metaN2, metaN0),
		}, true, false, metaN0, []string{metaN2}, nil},
		{"a server that names another leader", []a2aBusServerView{
			leaderView(metaN0, current(metaN1), current(metaN2)),
			followerView(metaN1, metaN0), followerView(metaN2, metaN2),
		}, true, false, metaN0, []string{metaN2}, nil},
		{"no leader a quorum agrees on", []a2aBusServerView{
			{Pod: metaN0}, followerView(metaN1, metaN0), {Pod: metaN2},
		}, true, false, "", []string{metaN0, metaN1, metaN2}, nil},
		{"an unreadable server", []a2aBusServerView{
			leaderView(metaN0, current(metaN1)), followerView(metaN1, metaN0), {Pod: metaN2, Err: errors.New("refused")},
		}, false, false, "", nil, []string{metaN2}},
		{"a server not running", formedViews()[:2], false, false, "", nil, []string{"1 of 3 servers not running"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			v := judgeA2ABusMeta(tc.views, 3)
			if v.Known != tc.known || v.Formed != tc.formed || v.Leader != tc.leader || !slices.Equal(v.Outside, tc.outside) {
				t.Errorf("verdict %+v, want known=%v formed=%v leader=%q outside=%v", v, tc.known, tc.formed, tc.leader, tc.outside)
			}
			for _, u := range tc.unreadableContain {
				if !slices.Contains(v.Unreadable, u) {
					t.Errorf("unreadable %v lacks %q", v.Unreadable, u)
				}
			}
		})
	}
}

// metaRig is a three-server bus StatefulSet and its pods, owned by it.
type metaRig struct {
	t     *testing.T
	agent *agentv1alpha1.PlatformAgent
	sts   *appsv1.StatefulSet
	cl    client.Client
	r     *PlatformAgentReconciler
	rec   *record.FakeRecorder
}

func newMetaRig(t *testing.T, edit func(*metaRig, []client.Object) []client.Object) *metaRig {
	t.Helper()
	agent := a2aTestAgent()
	agent.Status.BusReplicas = 3
	sts := buildA2ANATSStatefulSet(agent, "h")
	sts.UID = "sts-uid"
	m := &metaRig{t: t, agent: agent, sts: sts}
	objs := []client.Object{agent, sts}
	for i, name := range []string{metaN0, metaN1, metaN2} {
		objs = append(objs, &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: agent.Namespace, UID: types.UID("pod-uid-" + string(rune('0'+i))),
			Labels:          map[string]string{"app": a2aNATSName(agent)},
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(sts, appsv1.SchemeGroupVersion.WithKind("StatefulSet"))},
		}})
	}
	if edit != nil {
		objs = edit(m, objs)
	}
	m.cl = fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build()
	m.rec = record.NewFakeRecorder(16)
	m.r = &PlatformAgentReconciler{Client: m.cl, Scheme: setupScheme(), Recorder: m.rec}
	return m
}

func (m *metaRig) podExists(name string) bool {
	return m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: name}, &corev1.Pod{}) == nil
}

func (m *metaRig) restartedRecord() []string {
	live := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), live); err != nil {
		m.t.Fatal(err)
	}
	return a2aBusMetaRestarted(live)
}

func (m *metaRig) heal(views []a2aBusServerView, since, now time.Time) bool {
	m.t.Helper()
	live := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), live); err != nil {
		m.t.Fatal(err)
	}
	did, err := m.r.healA2ABusMeta(context.Background(), m.agent, live, views, judgeA2ABusMeta(views, 3), since, now)
	if err != nil {
		m.t.Fatal(err)
	}
	return did
}

// The one restart: past the bound, with exactly one server outside, the rest
// a quorum and the stray holding nothing, the StatefulSet records the
// restart before the pod is deleted, and a Warning says so.
func TestAServerLeftOutsideTheMetaGroupIsRestartedOnce(t *testing.T) {
	m := newMetaRig(t, nil)
	now := time.Now()
	if !m.heal(lateJoinerViews(), now.Add(-a2aBusMetaHealAfter), now) {
		t.Fatal("no restart with every guard met")
	}
	if m.podExists(metaN2) {
		t.Error("the stray's pod was not deleted")
	}
	if !m.podExists(metaN0) || !m.podExists(metaN1) {
		t.Error("a member's pod was deleted")
	}
	if got := m.restartedRecord(); !slices.Equal(got, []string{metaN2}) {
		t.Errorf("restart record %v, want [%s]", got, metaN2)
	}
	select {
	case e := <-m.rec.Events:
		if !strings.Contains(e, a2aBusMetaRestartedReason) || !strings.Contains(e, metaN2) {
			t.Errorf("event %q", e)
		}
	default:
		t.Error("no Warning for the restart")
	}
}

// Never twice for one server: a stray still outside after its restart is
// left, whatever the clock says.
func TestARestartedServerIsNeverRestartedAgain(t *testing.T) {
	m := newMetaRig(t, func(m *metaRig, objs []client.Object) []client.Object {
		m.sts.Annotations = map[string]string{a2aBusMetaRestartedAnnotation: metaN2}
		return objs
	})
	now := time.Now()
	if m.heal(lateJoinerViews(), now.Add(-time.Hour), now) {
		t.Fatal("restarted a server the record says was restarted")
	}
	if !m.podExists(metaN2) {
		t.Error("the stray's pod was deleted a second time")
	}
}

func TestTheRestartGuards(t *testing.T) {
	now := time.Now()
	past := now.Add(-a2aBusMetaHealAfter)
	// A leader a quorum agrees on (nats-0 and nats-1 both name it) that
	// lists neither other server as current: two outside, so the rest are
	// not a quorum without them.
	twoOutside := []a2aBusServerView{leaderView(metaN0), followerView(metaN1, metaN0), {Pod: metaN2, ClusterSize: 3}}
	holding := lateJoinerViews()
	holding[2].Streams = 1
	consuming := lateJoinerViews()
	consuming[2].Consumers = 1
	memberHolding := lateJoinerViews()
	memberHolding[0].Streams = 1
	loading := lateJoinerViews()
	loading[2].ClusterSize = 0
	noLeader := []a2aBusServerView{{Pod: metaN0}, {Pod: metaN1}, {Pod: metaN2}}
	for _, tc := range []struct {
		name  string
		views []a2aBusServerView
		since time.Time
	}{
		{"before the bound", lateJoinerViews(), now.Add(-a2aBusMetaHealAfter + time.Second)},
		{"no since (not yet recorded outside)", lateJoinerViews(), time.Time{}},
		{"two servers outside (no quorum without them)", twoOutside, past},
		{"the stray holds a stream", holding, past},
		{"the stray holds a consumer", consuming, past},
		{"a member holds a stream (not a fresh bus)", memberHolding, past},
		{"the stray's meta node hasn't started (still loading its store)", loading, past},
		{"no agreed leader", noLeader, past},
		{"formed", formedViews(), past},
		{"unknown", append(lateJoinerViews()[:2], a2aBusServerView{Pod: metaN2, Err: errors.New("x")}), past},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := newMetaRig(t, nil)
			if m.heal(tc.views, tc.since, now) {
				t.Fatal("restarted with a guard unmet")
			}
			for _, pod := range []string{metaN0, metaN1, metaN2} {
				if !m.podExists(pod) {
					t.Errorf("%s was deleted", pod)
				}
			}
			if rec := m.restartedRecord(); len(rec) != 0 {
				t.Errorf("restart recorded %v", rec)
			}
		})
	}
}

// A pod of the stray's name that this StatefulSet doesn't control is never
// deleted.
func TestAForeignPodIsNeverRestarted(t *testing.T) {
	m := newMetaRig(t, func(m *metaRig, objs []client.Object) []client.Object {
		for _, o := range objs {
			if o.GetName() == metaN2 {
				o.SetOwnerReferences(nil)
			}
		}
		return objs
	})
	now := time.Now()
	if m.heal(lateJoinerViews(), now.Add(-a2aBusMetaHealAfter), now) {
		t.Fatal("restarted a pod the StatefulSet doesn't control")
	}
	if !m.podExists(metaN2) {
		t.Error("the foreign pod was deleted")
	}
}

// The provision path's check: hold while the group isn't formed, with the
// condition naming the server; no hold when formed; fail open, naming the
// unreadable server, when a view can't be read.
func TestTheMetaGroupCheckHoldsProvisioningUntilFormed(t *testing.T) {
	for _, tc := range []struct {
		name        string
		views       []a2aBusServerView
		hold        bool
		reason      string
		msgContains string
	}{
		{"formed", formedViews(), false, "", ""},
		{"a server outside", lateJoinerViews(), true, a2aBusMetaOutsideReason, metaN2 + " is outside the JetStream meta group led by " + metaN0},
		{"unreadable", append(lateJoinerViews()[:2], a2aBusServerView{Pod: metaN2, Err: errors.New("refused")}), false, a2aBusMetaUnknownReason, "verdict unknown: " + metaN2 + " unreadable"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := newMetaRig(t, nil)
			views := tc.views
			m.r.busServerViews = func(context.Context, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet) ([]a2aBusServerView, error) {
				return views, nil
			}
			hold, state, err := m.r.checkA2ABusMeta(context.Background(), m.agent, m.sts, 3, time.Now())
			if err != nil {
				t.Fatal(err)
			}
			if hold != tc.hold || !state.checked || state.reason != tc.reason || !strings.Contains(state.message, tc.msgContains) {
				t.Errorf("hold %v state %+v; want hold %v reason %q message containing %q", hold, state, tc.hold, tc.reason, tc.msgContains)
			}
		})
	}
}

// A server restarted once and still outside is named as left for a person.
func TestTheConditionSaysWhenARestartedServerIsStillOutside(t *testing.T) {
	msg := a2aBusMetaMessage(judgeA2ABusMeta(lateJoinerViews(), 3), []string{metaN2})
	if !strings.Contains(msg, "Restarted once already and still outside: "+metaN2) || !strings.Contains(msg, "left for a person") {
		t.Errorf("message %q", msg)
	}
}

// The condition: a pass that didn't look changes nothing; a new reason
// starts a new period, so the restart's clock counts from the first pass
// that saw a server outside; removed once formed.
func TestTheMetaGroupConditionAndItsClock(t *testing.T) {
	agent := a2aTestAgent()
	t0 := metav1.NewTime(time.Now().Add(-time.Hour))
	setA2ABusMetaCondition(agent, a2aBusMetaState{checked: true, reason: a2aBusMetaUnknownReason, message: "verdict unknown: x unreadable"}, t0)
	if !a2aBusMetaSince(agent).IsZero() {
		t.Error("an unknown verdict starts the restart clock")
	}
	t1 := metav1.NewTime(time.Now())
	outside := a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: "y is outside"}
	setA2ABusMetaCondition(agent, outside, t1)
	if since := a2aBusMetaSince(agent); !since.Equal(t1.Time.Truncate(time.Second)) && !since.Equal(t1.Time) {
		t.Errorf("since %v, want the first outside pass %v, not the unknown one %v", since, t1, t0)
	}
	if !a2aBusMetaConditionCurrent(agent, outside) || !a2aBusMetaConditionCurrent(agent, a2aBusMetaState{}) {
		t.Error("an unchanged or unchecked pass reads as a change")
	}
	setA2ABusMetaCondition(agent, a2aBusMetaState{}, metav1.Now())
	if meta.FindStatusCondition(agent.Status.Conditions, a2aBusMetaConditionType) == nil {
		t.Error("an unchecked pass removed the condition")
	}
	setA2ABusMetaCondition(agent, a2aBusMetaState{checked: true}, metav1.Now())
	if meta.FindStatusCondition(agent.Status.Conditions, a2aBusMetaConditionType) != nil {
		t.Error("the condition survives a formed group")
	}
}

// The /jsz decoder reads the fields nats-server 2.10 writes.
func TestTheJszMetaViewDecodes(t *testing.T) {
	body := `{"streams":2,"consumers":1,"meta_cluster":{"name":"platform-agent-a2a-nats","leader":"` + metaN0 + `","cluster_size":3,
		"replicas":[{"name":"` + metaN1 + `","current":true,"active":1},{"name":"` + metaN2 + `","current":false,"offline":true}]}}`
	v := decodeA2ABusServerView(metaN0, &http.Response{Body: io.NopCloser(strings.NewReader(body))})
	if v.Err != nil || v.Leader != metaN0 || v.ClusterSize != 3 || v.Streams != 2 || v.Consumers != 1 || len(v.Replicas) != 2 ||
		!v.Replicas[0].Current || v.Replicas[1].Current || !v.Replicas[1].Offline {
		t.Errorf("decoded %+v", v)
	}
	if bad := decodeA2ABusServerView(metaN0, &http.Response{Body: io.NopCloser(strings.NewReader("not json"))}); bad.Err == nil {
		t.Error("a body that isn't JSON decoded")
	}
}

// Wired into the provision path: on a rolled-out three-server bus, a server
// outside the meta group holds the provision Job and the pass carries the
// condition; once the group is formed the Job is created.
func TestTheProvisionJobWaitsForTheMetaGroup(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	views := lateJoinerViews()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8),
		busServerViews: func(context.Context, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet) ([]a2aBusServerView, error) {
			return views, nil
		}}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	reportCalloutStatus(t, ctx, cl, agent, 2, 2, 2)
	reportBusRolledOut(t, ctx, cl, agent)
	jobKey := client.ObjectKey{Name: buildA2AProvisionJob(agent).Name, Namespace: agent.Namespace}

	state, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	if !state.jobHeld || !strings.Contains(state.busMeta.message, metaN2) || state.busMeta.reason != a2aBusMetaOutsideReason {
		t.Errorf("held %v, meta %+v; want held, naming %s", state.jobHeld, state.busMeta, metaN2)
	}
	if err := cl.Get(ctx, jobKey, &batchv1.Job{}); err == nil {
		t.Error("the provision Job was created with a server outside the meta group")
	}

	views = formedViews()
	if state, err = r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if state.jobHeld || state.busMeta.message != "" || !state.busMeta.checked {
		t.Errorf("held %v, meta %+v; want created, with the condition cleared", state.jobHeld, state.busMeta)
	}
	if err := cl.Get(ctx, jobKey, &batchv1.Job{}); err != nil {
		t.Errorf("the provision Job was not created once the meta group formed: %v", err)
	}

	// The pass that created the Job may fail later and never write the clear,
	// leaving a ServerOutsideMetaGroup condition: once the Job exists, any
	// pass clears it, whatever the views say now.
	setA2ABusMetaCondition(agent, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: "stale"}, metav1.Now())
	views = lateJoinerViews()
	if state, err = r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if !state.busMeta.checked || state.busMeta.message != "" {
		t.Errorf("meta %+v with the Job existing; want the condition cleared", state.busMeta)
	}
}

// Right after the one restart the stray is terminating, then absent, then
// Pending: a server not running while the others are holds provisioning
// (never unknown, which would let the Job through on a group still short),
// and is never restarted on that account. No server running at all stays
// unknown: the bus's rollout hold covers a bus that hasn't started.
func TestAServerNotRunningHoldsProvisioningAndIsNeverRestarted(t *testing.T) {
	down := []a2aBusServerView{leaderView(metaN0, current(metaN1)), followerView(metaN1, metaN0), {Pod: metaN2, Down: true}}
	v := judgeA2ABusMeta(down, 3)
	if !v.Known || v.Formed || !slices.Equal(v.Down, []string{metaN2}) {
		t.Errorf("one down: verdict %+v, want known, not formed, %s down", v, metaN2)
	}
	all := []a2aBusServerView{{Pod: metaN0, Down: true}, {Pod: metaN1, Down: true}, {Pod: metaN2, Down: true}}
	if v := judgeA2ABusMeta(all, 3); v.Known || !slices.Contains(v.Unreadable, "3 of 3 servers not running") {
		t.Errorf("all down: verdict %+v, want unknown", v)
	}

	m := newMetaRig(t, nil)
	m.r.busServerViews = func(context.Context, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet) ([]a2aBusServerView, error) {
		return down, nil
	}
	// Long past the restart's bound, with the condition's clock started.
	setA2ABusMetaCondition(m.agent, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: "earlier"}, metav1.NewTime(time.Now().Add(-time.Hour)))
	hold, state, err := m.r.checkA2ABusMeta(context.Background(), m.agent, m.sts, 3, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if !hold || state.reason != a2aBusMetaDownReason || !strings.Contains(state.message, metaN2+" is not running") {
		t.Errorf("hold %v state %+v; want held, %s naming %s", hold, state, a2aBusMetaDownReason, metaN2)
	}
	if !m.podExists(metaN2) || len(m.restartedRecord()) != 0 {
		t.Error("a server not running was restarted")
	}
}

// The read marks an ordinal Down when its pod is being deleted, isn't
// Running, or doesn't exist, rather than leaving it out.
func TestTheReadMarksAServerNotRunningDown(t *testing.T) {
	m := newMetaRig(t, func(m *metaRig, objs []client.Object) []client.Object {
		var kept []client.Object
		for _, o := range objs {
			pod, isPod := o.(*corev1.Pod)
			switch {
			case !isPod:
				kept = append(kept, o)
			case pod.Name == metaN0:
				pod.Status.Phase = corev1.PodRunning
				pod.DeletionTimestamp = &metav1.Time{Time: time.Now()}
				pod.Finalizers = []string{"test/hold"}
				kept = append(kept, pod)
			case pod.Name == metaN1:
				pod.Status.Phase = corev1.PodPending
				kept = append(kept, pod)
			}
			// metaN2 is left out: no pod.
		}
		return kept
	})
	views, err := m.r.readA2ABusServerViews(context.Background(), m.agent, m.sts)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, v := range views {
		if v.Down {
			got = append(got, v.Pod)
		}
	}
	if !slices.Equal(got, []string{metaN0, metaN1, metaN2}) {
		t.Errorf("down %v, want every server: terminating, Pending, absent", got)
	}
}

// Never on a bus that has been provisioned: a running bus's server is
// never restarted by this, whatever its view says.
func TestNoRestartOnAProvisionedBus(t *testing.T) {
	m := newMetaRig(t, nil)
	setBusProvisionedCondition(m.agent, true, "provision-job", metav1.Now())
	now := time.Now()
	if m.heal(lateJoinerViews(), now.Add(-time.Hour), now) {
		t.Fatal("restarted a server of a provisioned bus")
	}
	if !m.podExists(metaN2) {
		t.Error("deleted a provisioned bus's server")
	}
}

// A pod under another StatefulSet's UID, or one already being deleted, is
// never restarted.
func TestOnlyALivePodOfThisStatefulSetIsRestarted(t *testing.T) {
	for _, tc := range []struct {
		name string
		edit func(o client.Object)
	}{
		{"another owner's UID", func(o client.Object) {
			o.SetOwnerReferences([]metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: "x", UID: "other-uid", Controller: ptrTrue()}})
		}},
		{"already being deleted", func(o client.Object) {
			o.SetFinalizers([]string{"test/hold"})
			o.SetDeletionTimestamp(&metav1.Time{Time: time.Now()})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := newMetaRig(t, func(m *metaRig, objs []client.Object) []client.Object {
				for _, o := range objs {
					if o.GetName() == metaN2 {
						tc.edit(o)
					}
				}
				return objs
			})
			now := time.Now()
			if m.heal(lateJoinerViews(), now.Add(-a2aBusMetaHealAfter), now) {
				t.Fatal("restarted")
			}
			if rec := m.restartedRecord(); len(rec) != 0 {
				t.Errorf("recorded %v", rec)
			}
		})
	}
}

func ptrTrue() *bool { b := true; return &b }

// The record is written before the delete: a patch that fails deletes
// nothing, and a delete that fails takes the record back.
func TestTheRestartIsRecordedBeforeTheDelete(t *testing.T) {
	now := time.Now()
	t.Run("patch fails", func(t *testing.T) {
		m := newMetaRig(t, nil)
		m.r.Client = interceptor.NewClient(m.cl.(client.WithWatch), interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				return errors.New("patch refused")
			},
		})
		live := &appsv1.StatefulSet{}
		if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), live); err != nil {
			t.Fatal(err)
		}
		views := lateJoinerViews()
		if _, err := m.r.healA2ABusMeta(context.Background(), m.agent, live, views, judgeA2ABusMeta(views, 3), now.Add(-a2aBusMetaHealAfter), now); err == nil {
			t.Error("a failed record reported no error")
		}
		if !m.podExists(metaN2) {
			t.Error("the pod was deleted although its restart couldn't be recorded")
		}
	})
	t.Run("delete fails", func(t *testing.T) {
		m := newMetaRig(t, nil)
		m.r.Client = interceptor.NewClient(m.cl.(client.WithWatch), interceptor.Funcs{
			Delete: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				return errors.New("delete refused")
			},
		})
		live := &appsv1.StatefulSet{}
		if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), live); err != nil {
			t.Fatal(err)
		}
		views := lateJoinerViews()
		if _, err := m.r.healA2ABusMeta(context.Background(), m.agent, live, views, judgeA2ABusMeta(views, 3), now.Add(-a2aBusMetaHealAfter), now); err == nil {
			t.Error("a failed delete reported no error")
		}
		if rec := m.restartedRecord(); len(rec) != 0 {
			t.Errorf("the record %v stands for a restart that didn't happen", rec)
		}
	})
}

// Wired: the check restarts the stray once the condition has said it is
// outside long enough.
func TestTheCheckRestartsTheStrayWhenItsTimeIsUp(t *testing.T) {
	m := newMetaRig(t, nil)
	views := lateJoinerViews()
	m.r.busServerViews = func(context.Context, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet) ([]a2aBusServerView, error) {
		return views, nil
	}
	msg := a2aBusMetaMessage(judgeA2ABusMeta(views, 3), nil)
	setA2ABusMetaCondition(m.agent, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: msg}, metav1.NewTime(time.Now().Add(-a2aBusMetaHealAfter-time.Second)))
	live := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), live); err != nil {
		t.Fatal(err)
	}
	hold, state, err := m.r.checkA2ABusMeta(context.Background(), m.agent, live, 3, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if m.podExists(metaN2) || !hold {
		t.Errorf("the stray wasn't restarted (hold %v)", hold)
	}
	if !strings.Contains(state.message, "Restarted once already") {
		t.Errorf("the condition doesn't say it was restarted: %q", state.message)
	}
}

// A changed message starts a new period: another server outside, or the
// same one after its restart, waits its own 3 minutes.
func TestANewMessageRestartsTheClock(t *testing.T) {
	agent := a2aTestAgent()
	t0 := metav1.NewTime(time.Now().Add(-time.Hour))
	setA2ABusMetaCondition(agent, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: "nats-1 is outside"}, t0)
	t1 := metav1.NewTime(time.Now())
	setA2ABusMetaCondition(agent, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: "nats-2 is outside"}, t1)
	if since := a2aBusMetaSince(agent); since.Before(t1.Time.Add(-time.Second)) {
		t.Errorf("since %v, want the new message's %v, not the old %v", since, t1, t0)
	}
}

// Only the StatefulSet's ordinal pods are read: a pod that copies its labels
// and owner reference under another name adds no view.
func TestOnlyTheStatefulSetsOrdinalPodsAreRead(t *testing.T) {
	m := newMetaRig(t, func(m *metaRig, objs []client.Object) []client.Object {
		decoy := objs[len(objs)-1].DeepCopyObject().(*corev1.Pod)
		decoy.Name, decoy.UID = "test-agent-a2a-nats-decoy", "decoy-uid"
		return append(objs, decoy)
	})
	views, err := m.r.readA2ABusServerViews(context.Background(), m.agent, m.sts)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, v := range views {
		names = append(names, v.Pod)
	}
	if !slices.Equal(names, []string{metaN0, metaN1, metaN2}) {
		t.Errorf("read %v", names)
	}
}

// During a bus migration the meta group check doesn't run: the migration
// holds the provision Job itself, and mid-way the bus is unprovisioned with
// no streams, which the restart's guards would read as a fresh bus.
func TestTheMetaCheckWaitsOutABusMigration(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	reads := 0
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8),
		busServerViews: func(context.Context, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet) ([]a2aBusServerView, error) {
			reads++
			return lateJoinerViews(), nil
		}}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	reportCalloutStatus(t, ctx, cl, agent, 2, 2, 2)
	reportBusRolledOut(t, ctx, cl, agent)
	reads = 0
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), Phase: string(busMigrationConverting), RunID: "3-1"}
	if !busMigrationHoldsProvision(agent) {
		t.Fatal("the fixture's run doesn't hold provisioning")
	}
	state, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
	if err != nil {
		t.Fatal(err)
	}
	if !state.jobHeld {
		t.Error("the provision Job wasn't held during a migration")
	}
	if reads != 0 || state.busMeta.message != "" {
		t.Errorf("the meta check ran during a migration: %d reads, meta %+v", reads, state.busMeta)
	}
}
