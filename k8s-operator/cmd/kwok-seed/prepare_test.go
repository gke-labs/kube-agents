// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"fmt"
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/fakeapiserver"
)

// testProfile is small enough to walk every object and large enough that
// Event indexes wrap past the Pod count and Pod indexes past the Node count.
var testProfile = profile{Nodes: 10, Pods: 300, Namespaces: 5, Services: 10, Events: 500}

func testGenerator(seed int64, p profile) *fakeapiserver.Generator {
	return fakeapiserver.NewGenerator(seed, fakeapiserver.Sizes{Nodes: p.Nodes, Pods: p.Pods, Events: p.Events})
}

func testLayout(p profile) layout {
	return layout{prefix: "team", namespaces: p.Namespaces, pods: p.Pods}
}

func TestNamespaceFor(t *testing.T) {
	l := testLayout(testProfile)
	cases := []struct {
		i    int
		want string
	}{{0, "team-000"}, {4, "team-004"}, {5, "team-000"}, {299, "team-004"}, {1234, "team-004"}}
	for _, c := range cases {
		if got := l.namespaceFor(c.i); got != c.want {
			t.Errorf("namespaceFor(%d) = %q, want %q", c.i, got, c.want)
		}
	}
	wantNames := []string{"team-000", "team-001", "team-002", "team-003", "team-004"}
	if got := l.namespaceNames(); !reflect.DeepEqual(got, wantNames) {
		t.Errorf("namespaceNames() = %v, want %v", got, wantNames)
	}
	wide := layout{prefix: "scale", namespaces: 500, pods: 60000}
	if got := wide.namespaceFor(499); got != "scale-499" {
		t.Errorf("namespaceFor(499) = %q, want scale-499", got)
	}
	if got := wide.namespaceFor(500); got != "scale-000" {
		t.Errorf("namespaceFor(500) = %q, want scale-000", got)
	}
}

func TestPrepareNode(t *testing.T) {
	gen := testGenerator(7, testProfile)
	node := gen.Node(3)
	before := node.DeepCopy()
	prepareNode(node)

	if got := node.Annotations[kwokNodeAnnotation]; got != kwokNodeAnnotationValue {
		t.Errorf("annotation %s = %q, want %q", kwokNodeAnnotation, got, kwokNodeAnnotationValue)
	}
	if len(node.Status.Conditions) != 0 || node.Status.Phase != "" {
		t.Errorf("conditions and phase not cleared: %+v", node.Status)
	}
	if !reflect.DeepEqual(node.Status.Capacity, before.Status.Capacity) ||
		!reflect.DeepEqual(node.Status.Allocatable, before.Status.Allocatable) ||
		!reflect.DeepEqual(node.Status.Addresses, before.Status.Addresses) ||
		!reflect.DeepEqual(node.Status.NodeInfo, before.Status.NodeInfo) ||
		!reflect.DeepEqual(node.Status.Images, before.Status.Images) {
		t.Errorf("status fields the API server accepts on create changed: %+v", node.Status)
	}
	if node.UID != "" || node.ResourceVersion != "" || !node.CreationTimestamp.IsZero() {
		t.Errorf("server fields not cleared: uid=%q rv=%q created=%v", node.UID, node.ResourceVersion, node.CreationTimestamp)
	}
	if node.Name != gen.NodeName(3) {
		t.Errorf("name = %q, want %q", node.Name, gen.NodeName(3))
	}
	if !reflect.DeepEqual(node.Labels, before.Labels) {
		t.Errorf("labels changed: %v", node.Labels)
	}
	if !reflect.DeepEqual(node.Spec, before.Spec) {
		t.Errorf("spec changed: %+v", node.Spec)
	}
	for k, v := range before.Annotations {
		if node.Annotations[k] != v {
			t.Errorf("annotation %s = %q, want %q", k, node.Annotations[k], v)
		}
	}

	bare := gen.Node(4)
	bare.Annotations = nil
	prepareNode(bare)
	if got := bare.Annotations[kwokNodeAnnotation]; got != kwokNodeAnnotationValue {
		t.Errorf("nil annotations: %s = %q, want %q", kwokNodeAnnotation, got, kwokNodeAnnotationValue)
	}
}

func TestPreparePod(t *testing.T) {
	gen := testGenerator(7, testProfile)
	l := testLayout(testProfile)
	for _, i := range []int{0, 7, 12, 299} {
		pod := gen.Pod(i)
		before := pod.DeepCopy()
		l.preparePod(pod, i)

		if want := l.namespaceFor(i); pod.Namespace != want {
			t.Errorf("pod %d namespace = %q, want %q", i, pod.Namespace, want)
		}
		if len(pod.OwnerReferences) != 0 {
			t.Errorf("pod %d keeps ownerReferences: %v", i, pod.OwnerReferences)
		}
		if !reflect.DeepEqual(pod.Status, corev1.PodStatus{}) {
			t.Errorf("pod %d status not cleared: %+v", i, pod.Status)
		}
		if pod.UID != "" || pod.ResourceVersion != "" || !pod.CreationTimestamp.IsZero() {
			t.Errorf("pod %d server fields not cleared", i)
		}
		if pod.Name != before.Name {
			t.Errorf("pod %d name changed to %q", i, pod.Name)
		}
		if !reflect.DeepEqual(pod.Labels, before.Labels) {
			t.Errorf("pod %d labels changed: %v", i, pod.Labels)
		}
		if !reflect.DeepEqual(pod.Spec, before.Spec) {
			t.Errorf("pod %d spec changed", i)
		}
		if want := gen.NodeName(i % testProfile.Nodes); pod.Spec.NodeName != want {
			t.Errorf("pod %d nodeName = %q, want %q", i, pod.Spec.NodeName, want)
		}
	}
}

func TestPrepareEventFollowsItsPod(t *testing.T) {
	gen := testGenerator(7, testProfile)
	l := testLayout(testProfile)
	replicaSetEvents := 0
	for i := range testProfile.Events {
		before := gen.Event(i)
		ev := gen.Event(i)
		l.prepareEvent(ev, i)
		podIndex := i % testProfile.Pods
		pod := gen.Pod(podIndex)
		l.preparePod(pod, podIndex)

		if ev.Namespace != pod.Namespace {
			t.Fatalf("event %d namespace = %q, pod %d namespace = %q", i, ev.Namespace, podIndex, pod.Namespace)
		}
		if ev.InvolvedObject.Namespace != ev.Namespace {
			t.Fatalf("event %d involvedObject.namespace = %q, event namespace = %q", i, ev.InvolvedObject.Namespace, ev.Namespace)
		}
		switch ev.InvolvedObject.Kind {
		case "Pod":
			if ev.InvolvedObject.Name != pod.Name {
				t.Fatalf("event %d involves pod %q, want %q", i, ev.InvolvedObject.Name, pod.Name)
			}
		case "ReplicaSet":
			replicaSetEvents++
		default:
			t.Fatalf("event %d involves a %s", i, ev.InvolvedObject.Kind)
		}
		if ev.UID != "" || ev.ResourceVersion != "" || !ev.CreationTimestamp.IsZero() {
			t.Fatalf("event %d server fields not cleared", i)
		}
		if ev.Name != before.Name || ev.Count != before.Count || ev.Type != before.Type || ev.Reason != before.Reason ||
			ev.Message != before.Message || ev.Source != before.Source || ev.ReportingController != before.ReportingController ||
			!ev.FirstTimestamp.Equal(&before.FirstTimestamp) || !ev.LastTimestamp.Equal(&before.LastTimestamp) {
			t.Fatalf("event %d lost a field it should keep:\n got %+v\nwant %+v", i, ev, before)
		}
	}
	if replicaSetEvents == 0 {
		t.Error("no ReplicaSet event among the generated ones; the mapping for them went untested")
	}
}

func TestServiceFor(t *testing.T) {
	gen := testGenerator(7, testProfile)
	l := testLayout(testProfile)
	for _, i := range []int{0, 3, 9} {
		pod := gen.Pod(i)
		l.preparePod(pod, i)
		app := pod.Labels[appLabel]
		if app == "" {
			t.Fatalf("pod %d carries no %s label", i, appLabel)
		}
		svc := l.serviceFor(i, app)

		if want := fmt.Sprintf("svc-%05d", i); svc.Name != want {
			t.Errorf("service %d name = %q, want %q", i, svc.Name, want)
		}
		if svc.Namespace != pod.Namespace {
			t.Errorf("service %d namespace = %q, pod namespace = %q", i, svc.Namespace, pod.Namespace)
		}
		if !reflect.DeepEqual(svc.Spec.Selector, map[string]string{"app": app}) {
			t.Errorf("service %d selector = %v, want app=%s", i, svc.Spec.Selector, app)
		}
		if svc.Spec.Type != corev1.ServiceTypeClusterIP {
			t.Errorf("service %d type = %q", i, svc.Spec.Type)
		}
		if len(svc.Spec.Ports) != 1 || svc.Spec.Ports[0].Port != 80 || svc.Spec.Ports[0].TargetPort.IntValue() != 8080 || svc.Spec.Ports[0].Protocol != corev1.ProtocolTCP {
			t.Errorf("service %d ports = %+v, want one 80 -> 8080 TCP", i, svc.Spec.Ports)
		}
	}
}

func TestPreparedObjectsAreDeterministic(t *testing.T) {
	l := testLayout(testProfile)
	prepared := func(seed int64) (*corev1.Node, *corev1.Pod, *corev1.Event, *corev1.Service) {
		gen := testGenerator(seed, testProfile)
		node := gen.Node(2)
		prepareNode(node)
		pod := gen.Pod(42)
		l.preparePod(pod, 42)
		ev := gen.Event(377)
		l.prepareEvent(ev, 377)
		svc := l.serviceFor(5, gen.Pod(5).Labels[appLabel])
		return node, pod, ev, svc
	}
	n1, p1, e1, s1 := prepared(7)
	n2, p2, e2, s2 := prepared(7)
	if !reflect.DeepEqual(n1, n2) || !reflect.DeepEqual(p1, p2) || !reflect.DeepEqual(e1, e2) || !reflect.DeepEqual(s1, s2) {
		t.Error("two generators with one seed prepared different objects")
	}
	_, other, _, _ := prepared(8)
	if other.Name == p1.Name {
		t.Error("a different seed produced the same pod name")
	}
}
