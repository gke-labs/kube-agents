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
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The auth callout refuses an identity map it can't take (empty, unparseable
// or invalid) and keeps serving the one it had, which can be wider than what
// the operator just rendered (#4487). Its /status says so (lastError), and
// this reads it: from every Ready callout pod, by pod IP, since each replica
// has its own store and one behind the Service could hide another's refusal.
// The result is the A2AIdentityMapApplied condition.
const (
	a2aIdentityMapAppliedConditionType = "A2AIdentityMapApplied"
	a2aIdentityMapServing              = "Serving"
	a2aIdentityMapRefused              = "Refused"
	a2aIdentityMapPending              = "Pending"
	a2aIdentityMapStatusUnreadable     = "StatusUnreadable"
	a2aIdentityMapNoReadyCallout       = "NoReadyCallout"

	// a2aCalloutStatusPath is authcallout.StatusPath.
	a2aCalloutStatusPath = "/status"
	// a2aCalloutStatusTimeout bounds one pod's status read, dial included.
	a2aCalloutStatusTimeout = 2 * time.Second
	// a2aCalloutStatusMaxBytes bounds the body read: the answer names every
	// user the map covers, so it grows with the install, but not past this.
	a2aCalloutStatusMaxBytes = 1 << 20
	// a2aCalloutRefusalMaxChars caps the refusal quoted in the condition.
	a2aCalloutRefusalMaxChars = 512
)

// a2aIdentityMapRemoved is the state on a pass with the A2A stack off.
var a2aIdentityMapRemoved = calloutMapState{}

// calloutStatusAnswer is the part of the callout's /status the operator
// reads (authcallout.Status).
type calloutStatusAnswer struct {
	Version   string `json:"version"`
	LastError string `json:"lastError"`
	Pending   string `json:"pending"`
}

// calloutStatusReader reads one callout pod's /status at addr (host:port).
type calloutStatusReader interface {
	Read(ctx context.Context, addr string) (calloutStatusAnswer, error)
}

// httpCalloutStatus is the reader the reconciler uses: the usage scrape's
// stance (no proxy, no redirects, a short timeout and a bounded body).
type httpCalloutStatus struct{ client *http.Client }

func newHTTPCalloutStatus() *httpCalloutStatus {
	return &httpCalloutStatus{client: &http.Client{
		Timeout: a2aCalloutStatusTimeout,
		Transport: &http.Transport{
			Proxy:             nil,
			DialContext:       (&net.Dialer{Timeout: a2aCalloutStatusTimeout}).DialContext,
			DisableKeepAlives: true,
		},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

func (h *httpCalloutStatus) Read(ctx context.Context, addr string) (calloutStatusAnswer, error) {
	var answer calloutStatusAnswer
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+a2aCalloutStatusPath, nil)
	if err != nil {
		return answer, err
	}
	resp, err := h.client.Do(req)
	if err != nil {
		return answer, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return answer, fmt.Errorf("status %d", resp.StatusCode)
	}
	err = json.NewDecoder(io.LimitReader(resp.Body, a2aCalloutStatusMaxBytes)).Decode(&answer)
	return answer, err
}

// calloutMapState is what the callout pods said about the map this pass:
// the condition's status, reason and message. a2aIdentityMapRemoved, the
// zero value, says the A2A stack is off and the condition goes.
type calloutMapState struct {
	status  metav1.ConditionStatus
	reason  string
	message string
}

// readCalloutMapState reads every Ready callout pod's /status and compares
// it with rendered, the identity map version this pass rendered. The reads
// run side by side under one deadline, so a hung callout costs a pass
// a2aCalloutStatusTimeout, not that per pod. A pod with a refusal standing
// makes it Refused, whatever the others say. Else a Ready pod that didn't
// answer makes it StatusUnreadable, since that pod may be the one holding a
// refusal, which is a Warning; else a pod on another version, or mid-way
// through shards, makes it Pending; else Serving. No Ready pod at all is
// NoReadyCallout, which the callout's readiness and A2ACalloutRolloutStalled
// already report.
func (r *PlatformAgentReconciler) readCalloutMapState(ctx context.Context, agent *agentv1alpha1.PlatformAgent, rendered string) calloutMapState {
	reader := r.calloutStatus
	if reader == nil {
		reader = newHTTPCalloutStatus()
	}
	unreadable := func(why string) calloutMapState {
		return calloutMapState{status: metav1.ConditionUnknown, reason: a2aIdentityMapStatusUnreadable,
			message: "an auth callout pod's status could not be read (" + why + "), so whether identity map " + rendered +
				" was taken or refused is unknown; the callout keeps serving its previous map when it refuses one"}
	}
	pods := &corev1.PodList{}
	if err := r.podReader().List(ctx, pods, client.InNamespace(agent.Namespace), client.MatchingLabels(a2aCalloutPodSelector(agent))); err != nil {
		return unreadable("listing the callout pods: " + err.Error())
	}
	var ready []*corev1.Pod
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Status.PodIP != "" && podIsReady(pod) && pod.DeletionTimestamp == nil {
			ready = append(ready, pod)
		}
	}
	if len(ready) == 0 {
		return calloutMapState{status: metav1.ConditionUnknown, reason: a2aIdentityMapNoReadyCallout,
			message: "no auth callout pod is Ready, so none can say whether identity map " + rendered + " was taken"}
	}
	slices.SortFunc(ready, func(a, b *corev1.Pod) int { return strings.Compare(a.Name, b.Name) })
	type result struct {
		answer calloutStatusAnswer
		err    error
	}
	results := make([]result, len(ready))
	rctx, cancel := context.WithTimeout(ctx, a2aCalloutStatusTimeout)
	defer cancel()
	var wg sync.WaitGroup
	for i, pod := range ready {
		wg.Add(1)
		go func(i int, addr string) {
			defer wg.Done()
			a, err := reader.Read(rctx, addr)
			results[i] = result{a, err}
		}(i, net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(a2aCalloutStatusPort)))
	}
	wg.Wait()
	var pending, readErr string
	for i, pod := range ready {
		res := results[i]
		switch {
		case res.err != nil:
			if readErr == "" {
				readErr = pod.Name + ": " + res.err.Error()
			}
		case res.answer.LastError != "":
			return calloutMapState{status: metav1.ConditionFalse, reason: a2aIdentityMapRefused,
				message: fmt.Sprintf("auth callout pod %s refused identity map %s and serves version %q in its place: %s",
					pod.Name, rendered, res.answer.Version, truncateForCondition(res.answer.LastError, a2aCalloutRefusalMaxChars))}
		case pending == "" && (res.answer.Version != rendered || res.answer.Pending != ""):
			pending = fmt.Sprintf("auth callout pod %s serves identity map %q, not yet %s", pod.Name, res.answer.Version, rendered)
			if res.answer.Pending != "" {
				pending += " (" + truncateForCondition(res.answer.Pending, a2aCalloutRefusalMaxChars) + ")"
			}
		}
	}
	switch {
	case readErr != "":
		return unreadable(readErr)
	case pending != "":
		return calloutMapState{status: metav1.ConditionFalse, reason: a2aIdentityMapPending, message: pending}
	}
	return calloutMapState{status: metav1.ConditionTrue, reason: a2aIdentityMapServing,
		message: fmt.Sprintf("every Ready auth callout pod serves identity map %s", rendered)}
}

// unsettled reports whether the state is one the next pass should look at
// again soon: a map still loading, or pods that didn't answer. Nothing the
// operator watches changes when a callout takes a map.
func (s *calloutMapState) unsettled() bool {
	return s != nil && (s.reason == a2aIdentityMapPending || s.reason == a2aIdentityMapStatusUnreadable)
}

// truncateForCondition caps s at n bytes, on a rune boundary, marking a cut.
func truncateForCondition(s string, n int) string {
	if len(s) <= n {
		return s
	}
	cut := n
	for cut > 0 && !utf8RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}

func utf8RuneStart(b byte) bool { return b&0xC0 != 0x80 }

func podIsReady(pod *corev1.Pod) bool {
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

// setA2AIdentityMapAppliedCondition writes state onto the agent. A nil state
// (no map rendered this pass) leaves the condition as it is; an empty one
// (the A2A stack is off, a2aIdentityMapRemoved) removes it.
func setA2AIdentityMapAppliedCondition(agent *agentv1alpha1.PlatformAgent, state *calloutMapState, now metav1.Time) {
	if state == nil {
		return
	}
	if state.reason == "" {
		meta.RemoveStatusCondition(&agent.Status.Conditions, a2aIdentityMapAppliedConditionType)
		return
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               a2aIdentityMapAppliedConditionType,
		Status:             state.status,
		Reason:             state.reason,
		Message:            state.message,
		ObservedGeneration: agent.Generation,
		LastTransitionTime: now,
	})
}

// a2aIdentityMapAppliedConditionCurrent reports whether agent already carries
// state's condition, so a pass with nothing new writes nothing.
func a2aIdentityMapAppliedConditionCurrent(agent *agentv1alpha1.PlatformAgent, state *calloutMapState) bool {
	if state == nil {
		return true
	}
	c := meta.FindStatusCondition(agent.Status.Conditions, a2aIdentityMapAppliedConditionType)
	if state.reason == "" {
		return c == nil
	}
	return c != nil && c.Status == state.status && c.Reason == state.reason && c.Message == state.message &&
		c.ObservedGeneration == agent.Generation
}
