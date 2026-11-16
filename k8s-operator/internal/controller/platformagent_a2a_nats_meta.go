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
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The JetStream meta group of a multi-server bus, and the one repair the
// operator makes to it (#5708).
//
// A fresh three-server bus starts its servers in turn (OrderedReady), and a
// member reports ready before a meta leader exists (a2aNATSReadinessPath), so
// the first two elect a leader and the third joins after. Twice in CI (GKE
// Standard) the third never joined the meta group although every server held
// its routes: the leader then refused every R3 stream ("no suitable peers for
// placement", with no discard reason, which in nats-server 2.10's
// selectPeerGroup means the meta group itself was short), and the provision
// Job retried until the run timed out.
//
// So before the provision Job is created, every server's /jsz is read: the
// group is formed when a quorum of servers report one leader and that leader
// lists every other server as a current peer. Until then the Job is held and
// the A2ABusMetaGroup condition names the server outside. A server still
// outside a2aBusMetaHealAfter later is restarted once, under guards: exactly
// one server outside, the rest a quorum on their own, and the stray holding
// no stream or consumer (so it holds no only copy of anything). The restart is
// recorded on the StatefulSet before the pod is deleted, so it happens once
// per server per bus, never in a loop; a server still outside after its one
// restart keeps the condition, and is left for a person.
const (
	a2aBusMetaConditionType = "A2ABusMetaGroup"
	a2aBusMetaOutsideReason = "ServerOutsideMetaGroup"
	// a2aBusMetaUnknownReason is the condition while some server's view
	// cannot be read: nothing is held or repaired on it, but the condition
	// says which server, so a failing run stays diagnosable.
	a2aBusMetaUnknownReason = "MetaGroupUnknown"
	// a2aBusMetaDownReason: a bus server's pod is absent, being deleted, or
	// not Running, so provisioning waits for it.
	a2aBusMetaDownReason = "ServerNotRunning"
	// a2aBusMetaRestartedReason is the Warning Event on the one restart.
	a2aBusMetaRestartedReason = "A2ABusServerRestarted"
	// a2aBusMetaRestartedAnnotation, on the bus StatefulSet, lists the pods
	// restarted for being outside the meta group, comma-separated. Written by
	// its own patch, not the StatefulSet's server-side apply, so the apply
	// leaves it; a new bus is a new StatefulSet and starts without it.
	a2aBusMetaRestartedAnnotation = "kubeagents.x-k8s.io/a2a-meta-restarted"
	// a2aBusMetaHealAfter is how long a server stays outside before its one
	// restart: past the few seconds a joining server takes (measured on
	// Autopilot: under one second), and well inside the provision Job's
	// usual budget.
	a2aBusMetaHealAfter = 3 * time.Minute
	a2aBusJszPath       = "/jsz"
	// a2aBusJszMaxBytes caps one /jsz read: the summary carries no stream
	// detail, a few kilobytes.
	a2aBusJszMaxBytes = 1 << 20
)

// a2aBusJszClient reads the bus servers' /jsz. It follows no redirect: a
// pod's answer must not steer the operator's GET elsewhere.
var a2aBusJszClient = &http.Client{
	Transport:     &http.Transport{Proxy: nil},
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// a2aBusServerView is one bus server's own report of the meta group, read
// from its /jsz. Err is set when it could not be read.
type a2aBusServerView struct {
	Pod         string
	Leader      string
	ClusterSize int
	// Replicas is the meta group's other members as the server lists them;
	// nats-server fills it on the leader only.
	Replicas  []a2aBusMetaPeer
	Streams   int
	Consumers int
	Err       error
	// Down: the ordinal's pod is absent, being deleted, or not Running, so
	// there was nothing to read (readA2ABusServerViews).
	Down bool
}

type a2aBusMetaPeer struct {
	Name    string
	Current bool
	Offline bool
}

// a2aBusMetaVerdict is what the views say about the meta group. Down lists
// servers not running (absent, being deleted, or not Running): the bus isn't
// up, so provisioning waits, and none is restarted on that account. Known is
// false when a running server could not be read: then nothing is held or
// repaired on its account.
type a2aBusMetaVerdict struct {
	Known      bool
	Formed     bool
	Leader     string
	Outside    []string
	Unreadable []string
	Down       []string
}

// a2aBusMetaState is what a pass found, carried to the status writers.
// Checked with no message removes the condition. Checked is false only on a
// pass that leaves the condition as it is.
type a2aBusMetaState struct {
	checked bool
	reason  string
	message string
}

// readA2ABusServerViews reads each server's /jsz at its pod IP. The bus pods
// have no per-pod DNS names (the StatefulSet's serviceName is the client
// Service), and the client Service would answer from one server at random.
// Pods come from the uncached reader, and only ones this StatefulSet controls
// are read. An ordinal with no pod, a pod being deleted, or one not Running
// is a view marked Down rather than left out: right after the one restart the
// stray is terminating, and a verdict that dropped it would read as unknown
// and let the provision Job through on a group still short.
func (r *PlatformAgentReconciler) readA2ABusServerViews(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet) ([]a2aBusServerView, error) {
	if r.busServerViews != nil {
		return r.busServerViews(ctx, agent, sts)
	}
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(sts.Namespace), client.MatchingLabels{"app": a2aNATSName(agent)}); err != nil {
		return nil, err
	}
	ordinals := map[string]bool{}
	if sts.Spec.Replicas != nil {
		for i := int32(0); i < *sts.Spec.Replicas; i++ {
			ordinals[sts.Name+"-"+strconv.Itoa(int(i))] = true
		}
	}
	seen := map[string]bool{}
	var views []a2aBusServerView
	for i := range pods.Items {
		pod := &pods.Items[i]
		// Only the StatefulSet's own ordinal pods: a pod that copies its
		// labels and owner reference adds no view.
		if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID || !ordinals[pod.Name] {
			continue
		}
		seen[pod.Name] = true
		if pod.DeletionTimestamp != nil || pod.Status.Phase != corev1.PodRunning {
			views = append(views, a2aBusServerView{Pod: pod.Name, Down: true})
			continue
		}
		views = append(views, readA2ABusServerView(ctx, pod))
	}
	for name := range ordinals {
		if !seen[name] {
			views = append(views, a2aBusServerView{Pod: name, Down: true})
		}
	}
	slices.SortFunc(views, func(a, b a2aBusServerView) int { return strings.Compare(a.Pod, b.Pod) })
	return views, nil
}

func readA2ABusServerView(ctx context.Context, pod *corev1.Pod) a2aBusServerView {
	view := a2aBusServerView{Pod: pod.Name}
	if pod.Status.PodIP == "" {
		view.Err = fmt.Errorf("pod %s has no IP yet", pod.Name)
		return view
	}
	ctx, cancel := context.WithTimeout(ctx, a2aBusVarzTimeout)
	defer cancel()
	url := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(a2aNATSMonitorPort)) + a2aBusJszPath
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		view.Err = err
		return view
	}
	resp, err := a2aBusJszClient.Do(req)
	if err != nil {
		view.Err = err
		return view
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		view.Err = fmt.Errorf("%s answered %s", url, resp.Status)
		return view
	}
	resp.Body = io.NopCloser(io.LimitReader(resp.Body, a2aBusJszMaxBytes))
	return decodeA2ABusServerView(pod.Name, resp)
}

func decodeA2ABusServerView(podName string, resp *http.Response) a2aBusServerView {
	var jsz struct {
		Streams   int `json:"streams"`
		Consumers int `json:"consumers"`
		Meta      *struct {
			Leader      string `json:"leader"`
			ClusterSize int    `json:"cluster_size"`
			Replicas    []struct {
				Name    string `json:"name"`
				Current bool   `json:"current"`
				Offline bool   `json:"offline"`
			} `json:"replicas"`
		} `json:"meta_cluster"`
	}
	view := a2aBusServerView{Pod: podName}
	if err := json.NewDecoder(resp.Body).Decode(&jsz); err != nil {
		view.Err = fmt.Errorf("decoding %s's /jsz: %w", podName, err)
		return view
	}
	view.Streams, view.Consumers = jsz.Streams, jsz.Consumers
	if jsz.Meta != nil {
		view.Leader, view.ClusterSize = jsz.Meta.Leader, jsz.Meta.ClusterSize
		for _, p := range jsz.Meta.Replicas {
			view.Replicas = append(view.Replicas, a2aBusMetaPeer{Name: p.Name, Current: p.Current, Offline: p.Offline})
		}
	}
	return view
}

// judgeA2ABusMeta reads the views for a bus of replicas servers. The leader is
// the one a quorum of the servers report; a server is outside when it reports
// another or none, or when the leader does not list it as a current, online
// peer. A server not running while another is holds, ahead of everything
// else; none running, a missing view (fewer views than replicas) or an
// unreadable server makes the verdict unknown.
func judgeA2ABusMeta(views []a2aBusServerView, replicas int) a2aBusMetaVerdict {
	var down []string
	for _, v := range views {
		if v.Down {
			down = append(down, v.Pod)
		}
	}
	// With no server running at all there is nothing to judge, and the bus's
	// rollout hold (a2aProvisionJobWaitsForBus) covers a bus that hasn't
	// started; that stays unknown, below. Some servers up and one not is the
	// window the restart opens, or a server that can't start: that holds.
	if len(down) > 0 && len(down) < len(views) {
		return a2aBusMetaVerdict{Known: true, Down: down}
	}
	if len(down) == len(views) && len(views) > 0 {
		return a2aBusMetaVerdict{Unreadable: []string{fmt.Sprintf("%d of %d servers not running", len(down), replicas)}}
	}
	var unreadable []string
	for _, v := range views {
		if v.Err != nil {
			unreadable = append(unreadable, v.Pod)
		}
	}
	if len(views) < replicas {
		unreadable = append(unreadable, fmt.Sprintf("%d of %d servers not running", replicas-len(views), replicas))
	}
	if len(unreadable) > 0 {
		return a2aBusMetaVerdict{Unreadable: unreadable}
	}
	votes := map[string]int{}
	for _, v := range views {
		if v.Leader != "" {
			votes[v.Leader]++
		}
	}
	quorum := replicas/2 + 1
	leader := ""
	for name, n := range votes {
		if n >= quorum {
			leader = name
		}
	}
	verdict := a2aBusMetaVerdict{Known: true, Leader: leader}
	if leader == "" {
		for _, v := range views {
			verdict.Outside = append(verdict.Outside, v.Pod)
		}
		return verdict
	}
	var leaderView *a2aBusServerView
	for i := range views {
		if views[i].Pod == leader {
			leaderView = &views[i]
		}
	}
	for _, v := range views {
		if v.Pod == leader {
			continue
		}
		if v.Leader != leader || leaderView == nil || !leaderListsCurrent(leaderView, v.Pod) {
			verdict.Outside = append(verdict.Outside, v.Pod)
		}
	}
	if leaderView == nil {
		// The leader the quorum names is not one of this bus's pods: a meta
		// group this StatefulSet does not own. Nothing here is formed.
		return a2aBusMetaVerdict{Known: true, Leader: leader, Outside: verdict.Outside}
	}
	verdict.Formed = len(verdict.Outside) == 0
	return verdict
}

func leaderListsCurrent(leader *a2aBusServerView, pod string) bool {
	for _, p := range leader.Replicas {
		if p.Name == pod {
			return p.Current && !p.Offline
		}
	}
	return false
}

// a2aBusMetaMessage is the condition's message for a verdict that is not
// formed; restarted lists the outside servers already restarted once.
func a2aBusMetaMessage(v a2aBusMetaVerdict, restarted []string) string {
	if v.Leader == "" {
		return fmt.Sprintf("no JetStream meta leader is agreed by a quorum of the bus's servers (%s); stream provisioning waits",
			strings.Join(v.Outside, ", "))
	}
	var once []string
	for _, pod := range v.Outside {
		if slices.Contains(restarted, pod) {
			once = append(once, pod)
		}
	}
	msg := fmt.Sprintf("%s outside the JetStream meta group led by %s; stream provisioning waits",
		strings.Join(v.Outside, ", ")+verbFor(len(v.Outside)), v.Leader)
	if len(once) > 0 {
		msg += fmt.Sprintf(". Restarted once already and still outside: %s; the operator restarts a server only once, so it is left for a person (delete the pod, or wipe the bus)",
			strings.Join(once, ", "))
	}
	return msg
}

func verbFor(n int) string {
	if n == 1 {
		return " is"
	}
	return " are"
}

// a2aBusMetaRestarted reads the StatefulSet's record of restarted pods.
func a2aBusMetaRestarted(sts *appsv1.StatefulSet) []string {
	raw := strings.TrimSpace(sts.Annotations[a2aBusMetaRestartedAnnotation])
	if raw == "" {
		return nil
	}
	return strings.Split(raw, ",")
}

// healA2ABusMeta restarts the one server outside the meta group, once, when
// every guard holds. since is when the condition first said a server was
// outside (zero when it does not yet). It returns whether it restarted one.
func (r *PlatformAgentReconciler) healA2ABusMeta(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet, views []a2aBusServerView, v a2aBusMetaVerdict, since, now time.Time) (bool, error) {
	// Exactly one outside, with an agreed leader: the verdict then has every
	// other server naming that leader and listed by it as current, so the
	// rest are a quorum without the stray.
	if !v.Known || v.Formed || v.Leader == "" || len(v.Outside) != 1 || since.IsZero() || now.Sub(since) < a2aBusMetaHealAfter {
		return false, nil
	}
	// Bring-up only: never on a bus that has been provisioned, nor while any
	// server holds a stream or consumer. A server recovering its store from
	// disk reports none until it has (and no meta group either), so a running
	// bus's server can't be taken for an empty one.
	if busProvisioned(agent) {
		return false, nil
	}
	for _, view := range views {
		if view.Streams != 0 || view.Consumers != 0 {
			return false, nil
		}
	}
	stray := v.Outside[0]
	if slices.Contains(a2aBusMetaRestarted(sts), stray) {
		return false, nil
	}
	var strayView *a2aBusServerView
	for i := range views {
		if views[i].Pod == stray {
			strayView = &views[i]
		}
	}
	// The stray's own meta node must have started (it reports the group's
	// size), which nats-server does only after recovering its store: a
	// server still loading reports neither.
	if strayView == nil || strayView.ClusterSize == 0 {
		return false, nil
	}
	pod := &corev1.Pod{}
	if err := r.podReader().Get(ctx, client.ObjectKey{Namespace: sts.Namespace, Name: stray}, pod); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	if owner := metav1.GetControllerOf(pod); owner == nil || owner.UID != sts.UID || pod.DeletionTimestamp != nil {
		return false, nil
	}
	// The record first, so a pass that dies between the two never restarts
	// the server a second time.
	restarted := append(a2aBusMetaRestarted(sts), stray)
	patch := client.MergeFrom(sts.DeepCopy())
	if sts.Annotations == nil {
		sts.Annotations = map[string]string{}
	}
	sts.Annotations[a2aBusMetaRestartedAnnotation] = strings.Join(restarted, ",")
	if err := r.Patch(ctx, sts, patch); err != nil {
		return false, err
	}
	uid := pod.UID
	if err := r.Delete(ctx, pod, client.Preconditions{UID: &uid}); err != nil {
		if errors.IsNotFound(err) || errors.IsConflict(err) {
			// The pod went, or was replaced, without us: that is a restart.
			return false, nil
		}
		// Not restarted: take the record back (best effort), so the
		// condition doesn't tell a person it was.
		undo := client.MergeFrom(sts.DeepCopy())
		sts.Annotations[a2aBusMetaRestartedAnnotation] = strings.Join(restarted[:len(restarted)-1], ",")
		if sts.Annotations[a2aBusMetaRestartedAnnotation] == "" {
			delete(sts.Annotations, a2aBusMetaRestartedAnnotation)
		}
		_ = r.Patch(ctx, sts, undo)
		return false, err
	}
	r.recordEvent(agent, corev1.EventTypeWarning, a2aBusMetaRestartedReason,
		fmt.Sprintf("restarted bus server %s once: it stayed outside the JetStream meta group led by %s for %s, holding no stream or consumer, with the other servers a quorum without it (#5708)",
			stray, v.Leader, now.Sub(since).Truncate(time.Second)))
	logf.FromContext(ctx).Info("restarted an A2A bus server outside the JetStream meta group, once",
		"pod", stray, "leader", v.Leader, "outsideFor", now.Sub(since).Truncate(time.Second))
	return true, nil
}

// a2aBusMetaSince is when the condition began saying a server is outside.
func a2aBusMetaSince(agent *agentv1alpha1.PlatformAgent) time.Time {
	c := meta.FindStatusCondition(agent.Status.Conditions, a2aBusMetaConditionType)
	if c == nil || c.Reason != a2aBusMetaOutsideReason {
		return time.Time{}
	}
	return c.LastTransitionTime.Time
}

// checkA2ABusMeta is the provision path's check, on a multi-server bus whose
// provision Job does not exist yet: whether to hold the Job, the condition
// to write, and the one restart when it is due.
func (r *PlatformAgentReconciler) checkA2ABusMeta(ctx context.Context, agent *agentv1alpha1.PlatformAgent, sts *appsv1.StatefulSet, replicas int, now time.Time) (hold bool, state a2aBusMetaState, err error) {
	views, err := r.readA2ABusServerViews(ctx, agent, sts)
	if err != nil {
		return false, a2aBusMetaState{}, err
	}
	v := judgeA2ABusMeta(views, replicas)
	if !v.Known {
		// Fail open: an unreadable server (a monitor the operator cannot
		// reach, a pod without an IP yet) holds nothing on its own. The
		// condition still says which, so a run that fails here is
		// diagnosable.
		msg := "verdict unknown: " + strings.Join(v.Unreadable, ", ") + " unreadable; stream provisioning is not held on it"
		logf.FromContext(ctx).Info("A2A bus meta group "+msg, "unreadable", v.Unreadable)
		return false, a2aBusMetaState{checked: true, reason: a2aBusMetaUnknownReason, message: msg}, nil
	}
	if len(v.Down) > 0 {
		// The bus isn't up: a server is starting, being restarted, or can't
		// be scheduled. R3 streams can't be placed without it, so the Job
		// waits; restarting is the StatefulSet's business, not this check's.
		return true, a2aBusMetaState{checked: true, reason: a2aBusMetaDownReason,
			message: strings.Join(v.Down, ", ") + verbFor(len(v.Down)) + " not running; stream provisioning waits for every server"}, nil
	}
	if v.Formed {
		return false, a2aBusMetaState{checked: true}, nil
	}
	_, err = r.healA2ABusMeta(ctx, agent, sts, views, v, a2aBusMetaSince(agent), now)
	return true, a2aBusMetaState{checked: true, reason: a2aBusMetaOutsideReason, message: a2aBusMetaMessage(v, a2aBusMetaRestarted(sts))}, err
}

func a2aBusMetaConditionCurrent(agent *agentv1alpha1.PlatformAgent, s a2aBusMetaState) bool {
	if !s.checked {
		return true
	}
	existing := meta.FindStatusCondition(agent.Status.Conditions, a2aBusMetaConditionType)
	if s.message == "" {
		return existing == nil
	}
	return existing != nil && existing.Status == metav1.ConditionFalse &&
		existing.Reason == s.reason && existing.Message == s.message
}

func setA2ABusMetaCondition(agent *agentv1alpha1.PlatformAgent, s a2aBusMetaState, now metav1.Time) {
	if !s.checked {
		return
	}
	if s.message == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aBusMetaConditionType)
		return
	}
	// A new reason or message starts a new period: SetStatusCondition keeps
	// the transition time while the status stays False, and the restart's
	// clock (a2aBusMetaSince) must count from the first pass that saw this
	// server outside, not from an earlier unknown verdict, a leaderless
	// phase, another server, or its own restart.
	if existing := meta.FindStatusCondition(agent.Status.Conditions, a2aBusMetaConditionType); existing != nil && (existing.Reason != s.reason || existing.Message != s.message) {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aBusMetaConditionType)
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               a2aBusMetaConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             s.reason,
		Message:            s.message,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}
