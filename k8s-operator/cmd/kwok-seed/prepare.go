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

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

const (
	// kwokNodeAnnotation marks a Node as one kwok-controller manages: its
	// node-initialize stage fills the status and node-heartbeat keeps Ready.
	// kwokctl starts the controller with --manage-all-nodes, but a controller
	// started with --manage-nodes-with-annotation-selector picks nodes by
	// this mark, so the seeded nodes carry it either way.
	kwokNodeAnnotation      = "kwok.x-k8s.io/node"
	kwokNodeAnnotationValue = "fake"
	// namespaceFormat renders the prefix and a zero-padded index (team-007);
	// three digits keep the 500 namespaces of the largest profile in order.
	namespaceFormat = "%s-%03d"
	// serviceNameFormat renders the Service index; five digits hold 4,000.
	serviceNameFormat = "svc-%05d"
	servicePortName   = "http"
	servicePort       = 80
	// serviceTargetPort is the generator's container port, so the
	// EndpointSlices kube-controller-manager builds point at a port the
	// seeded Pods declare.
	serviceTargetPort = 8080
	// appLabel is the generator's workload label on every Pod; the Service
	// selector matches it so EndpointSlices resolve to seeded Pods.
	appLabel = "app"
)

// layout is the namespace plan every prepared object follows. The generator
// spreads its objects over a fixed 25 namespaces; a profile asks for N, so
// the seeder remaps by index: the i-th Pod and Service land in namespace
// i mod N, and an Event lands where the Pod it concerns did.
type layout struct {
	prefix     string
	namespaces int
	// pods is the profile's Pod count, the modulus the generator's Event
	// takes its Pod index by (see fakeapiserver.Generator.Event).
	pods int
}

// namespaceFor is the namespace of the i-th Pod or Service.
func (l layout) namespaceFor(i int) string {
	return fmt.Sprintf(namespaceFormat, l.prefix, i%l.namespaces)
}

// eventNamespace is the namespace of the i-th Event: that of the Pod it
// concerns, which the generator picks as i mod pods. An Event about the
// Pod's ReplicaSet follows the same mapping, since the ReplicaSet shares the
// Pod's namespace.
func (l layout) eventNamespace(i int) string {
	return l.namespaceFor(i % l.pods)
}

// namespaceNames lists every namespace of the layout in index order.
func (l layout) namespaceNames() []string {
	names := make([]string, l.namespaces)
	for i := range names {
		names[i] = l.namespaceFor(i)
	}
	return names
}

// clearServerFields drops what the API server assigns on create: a
// resourceVersion must be empty, and the UID and creation timestamp are
// overwritten, so sending the generator's only adds bytes.
func clearServerFields(meta *metav1.ObjectMeta) {
	meta.UID = ""
	meta.ResourceVersion = ""
	meta.CreationTimestamp = metav1.Time{}
}

// prepareNode makes a generated Node one kwok-controller adopts: the
// annotation marks it, and the conditions are cleared so the node-initialize
// stage (which only matches a node without a Ready condition) runs and the
// heartbeat stage keeps it Ready. The rest of the status stays: a Node is the
// one kind the API server accepts a status for on create (the kubelet
// registers itself that way), kwok fills capacity, allocatable, addresses and
// nodeInfo only when they are absent, and keeping the generator's values
// (8 CPUs, 32 GiB, a GKE kubelet version, eight images) keeps the bytes a
// listing of these nodes costs close to a GKE node's. Labels, annotations and
// spec (podCIDR, providerID) stay too.
func prepareNode(node *corev1.Node) {
	if node.Annotations == nil {
		node.Annotations = map[string]string{}
	}
	node.Annotations[kwokNodeAnnotation] = kwokNodeAnnotationValue
	clearServerFields(&node.ObjectMeta)
	node.Status.Conditions = nil
	node.Status.Phase = ""
}

// preparePod moves the i-th generated Pod into its layout namespace and
// drops what would get it deleted or rejected: the owner references point
// at ReplicaSets that do not exist, which the garbage collector treats as
// dangling and collects; the status is the server's to set (kwok's pod-ready
// stage marks it Running). spec.nodeName stays, naming a seeded Node.
func (l layout) preparePod(pod *corev1.Pod, i int) {
	pod.Namespace = l.namespaceFor(i)
	pod.OwnerReferences = nil
	clearServerFields(&pod.ObjectMeta)
	pod.Status = corev1.PodStatus{}
}

// prepareEvent moves the i-th generated Event into the namespace of the
// Pod it concerns. The API server rejects a core/v1 Event whose namespace
// differs from its involvedObject's, so both move together; count,
// timestamps, type, reason, message, source and reporting fields stay.
func (l layout) prepareEvent(ev *corev1.Event, i int) {
	ns := l.eventNamespace(i)
	ev.Namespace = ns
	ev.InvolvedObject.Namespace = ns
	clearServerFields(&ev.ObjectMeta)
}

// serviceFor is the i-th Service: a ClusterIP in the layout namespace of i
// selecting app, the workload label of the i-th generated Pod, so the
// endpoint slice controller has seeded Pods to resolve it to.
func (l layout) serviceFor(i int, app string) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf(serviceNameFormat, i),
			Namespace: l.namespaceFor(i),
			Labels:    map[string]string{appLabel: app},
		},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: map[string]string{appLabel: app},
			Ports: []corev1.ServicePort{{
				Name:       servicePortName,
				Protocol:   corev1.ProtocolTCP,
				Port:       servicePort,
				TargetPort: intstr.FromInt32(serviceTargetPort),
			}},
		},
	}
}
