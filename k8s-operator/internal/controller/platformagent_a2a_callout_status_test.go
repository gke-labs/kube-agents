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
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeCalloutStatus answers by pod IP; an IP it has no answer for is an error.
type fakeCalloutStatus map[string]calloutStatusAnswer

func (f fakeCalloutStatus) Read(_ context.Context, addr string) (calloutStatusAnswer, error) {
	host, _, _ := net.SplitHostPort(addr)
	if a, ok := f[host]; ok {
		return a, nil
	}
	return calloutStatusAnswer{}, errors.New("connection refused")
}

func statusCalloutPod(agent *agentv1alpha1.PlatformAgent, name, ip string, ready bool) *corev1.Pod {
	status := corev1.ConditionFalse
	if ready {
		status = corev1.ConditionTrue
	}
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aCalloutPodSelector(agent)},
		Status:     corev1.PodStatus{PodIP: ip, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: status}}},
	}
}

// The callout pods' /status, read from each Ready pod and compared with the
// map the operator rendered (#4487): one refusal anywhere is Refused; else
// an older version or a re-render in progress is Pending; else Serving.
// Ready pods none of which answers is StatusUnreadable; none Ready is
// NoReadyCallout.
func TestTheCalloutPodsStatusSaysWhetherTheRenderedMapWasTaken(t *testing.T) {
	const rendered = "v2"
	for _, tc := range []struct {
		name       string
		pods       []*corev1.Pod
		answers    fakeCalloutStatus
		wantStatus metav1.ConditionStatus
		wantReason string
		wantIn     string
	}{
		{"every pod serves the render", []*corev1.Pod{nil, nil}, fakeCalloutStatus{"10.0.0.1": {Version: rendered}, "10.0.0.2": {Version: rendered}},
			metav1.ConditionTrue, a2aIdentityMapServing, rendered},
		{"one pod refused it", []*corev1.Pod{nil, nil}, fakeCalloutStatus{"10.0.0.1": {Version: rendered}, "10.0.0.2": {Version: "v1", LastError: "shard 0: identity map has no entries"}},
			metav1.ConditionFalse, a2aIdentityMapRefused, "callout-b refused identity map v2 and serves version \"v1\" in its place: shard 0: identity map has no entries"},
		{"a pod still on the old map", []*corev1.Pod{nil, nil}, fakeCalloutStatus{"10.0.0.1": {Version: rendered}, "10.0.0.2": {Version: "v1"}},
			metav1.ConditionFalse, a2aIdentityMapPending, `callout-b serves identity map "v1", not yet v2`},
		{"a pod mid-way through shards", []*corev1.Pod{nil, nil}, fakeCalloutStatus{"10.0.0.1": {Version: rendered, Pending: "shard 1 not arrived"}, "10.0.0.2": {Version: rendered}},
			metav1.ConditionFalse, a2aIdentityMapPending, "shard 1 not arrived"},
		{"Ready pods that don't answer", []*corev1.Pod{nil, nil}, fakeCalloutStatus{},
			metav1.ConditionUnknown, a2aIdentityMapStatusUnreadable, "connection refused"},
		{"no Ready pod", []*corev1.Pod{nil}, fakeCalloutStatus{},
			metav1.ConditionUnknown, a2aIdentityMapNoReadyCallout, "no auth callout pod is Ready"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := a2aTestAgent()
			r, cl, _ := a2aGateTestReconciler(t, agent)
			r.calloutStatus = tc.answers
			ctx := context.Background()
			for i := range tc.pods {
				pod := statusCalloutPod(agent, fmt.Sprintf("callout-%c", 'a'+i), fmt.Sprintf("10.0.0.%d", i+1), tc.wantReason != a2aIdentityMapNoReadyCallout)
				if err := cl.Create(ctx, pod); err != nil {
					t.Fatal(err)
				}
				if err := cl.Status().Update(ctx, pod); err != nil {
					t.Fatal(err)
				}
			}
			got := r.readCalloutMapState(ctx, agent, rendered)
			if got.status != tc.wantStatus || got.reason != tc.wantReason || !strings.Contains(got.message, tc.wantIn) {
				t.Errorf("state %+v, want %s/%s with %q", got, tc.wantStatus, tc.wantReason, tc.wantIn)
			}
		})
	}
}

// A long refusal is quoted capped, so the condition stays readable and
// under the API's message limit.
func TestARefusalIsQuotedCapped(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconciler(t, agent)
	r.calloutStatus = fakeCalloutStatus{"10.0.0.1": {Version: "v1", LastError: strings.Repeat("x", 5000)}}
	ctx := context.Background()
	pod := statusCalloutPod(agent, "callout-a", "10.0.0.1", true)
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if err := cl.Status().Update(ctx, pod); err != nil {
		t.Fatal(err)
	}
	got := r.readCalloutMapState(ctx, agent, "v2")
	if got.reason != a2aIdentityMapRefused || len(got.message) > 1024 || !strings.HasSuffix(got.message, "…") {
		t.Errorf("a 5000-byte refusal gave a %d-byte message ending %q", len(got.message), got.message[max(0, len(got.message)-8):])
	}
}

// The HTTP reader GETs /status and decodes the fields the operator uses;
// a non-200 answer is an error, not an empty status.
func TestTheHTTPCalloutStatusReaderDecodesStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case a2aCalloutStatusPath:
			_, _ = w.Write([]byte(`{"version":"v1","serving":true,"users":["a"],"lastError":"shard 0: no entries","shards":1}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	addr := strings.TrimPrefix(srv.URL, "http://")
	got, err := newHTTPCalloutStatus().Read(context.Background(), addr)
	if err != nil || got.Version != "v1" || got.LastError != "shard 0: no entries" {
		t.Fatalf("Read = %+v, %v", got, err)
	}

	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) }))
	defer bad.Close()
	if _, err := newHTTPCalloutStatus().Read(context.Background(), strings.TrimPrefix(bad.URL, "http://")); err == nil {
		t.Errorf("a 503 read as a status")
	}
}

// Through the reconcile: the condition lands on the PlatformAgent's status,
// and turning StatusUnreadable is a Warning once, not on every pass.
func TestTheReconcileSetsIdentityMapAppliedAndWarnsOnceWhenUnreadable(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(32)
	r.Recorder = rec
	r.calloutStatus = fakeCalloutStatus{} // every Ready pod refuses the connection
	ctx := context.Background()
	pod := statusCalloutPod(agent, "callout-a", "10.0.0.1", true)
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if err := cl.Status().Update(ctx, pod); err != nil {
		t.Fatal(err)
	}
	reconcileTwice(t, ctx, r, req)
	reconcileTwice(t, ctx, r, req)

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	c := meta.FindStatusCondition(got.Status.Conditions, a2aIdentityMapAppliedConditionType)
	if c == nil || c.Status != metav1.ConditionUnknown || c.Reason != a2aIdentityMapStatusUnreadable {
		t.Fatalf("condition %+v, want Unknown/%s", c, a2aIdentityMapStatusUnreadable)
	}
	warnings := 0
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, a2aIdentityMapStatusUnreadable) {
			warnings++
		}
	}
	if warnings != 1 {
		t.Errorf("%d %s warnings over four passes, want one", warnings, a2aIdentityMapStatusUnreadable)
	}
}
