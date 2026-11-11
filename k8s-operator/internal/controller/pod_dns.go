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
	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
)

// brokerCallerPodNdots is the resolver's ndots on the pods that dial the
// broker and the kube-api proxy: the shell sandbox, the profile workers (Job
// and warm pool) and the broker tiers. The session pods the gateway spawns
// set the same value (a2a/gateway/spawn.go, sessionPodNdots); keep the two in
// step.
//
// Kubernetes' default is ndots:5, so the 4-dot service names the operator
// renders (CREDENTIAL_PROXY_URL, KUBE_PROXY_URL: <svc>.<ns>.svc.cluster.local)
// go through all six search domains first: up to 14 A/AAAA queries per
// lookup, 12 of them NXDOMAIN (#5590). With ndots:3 a name with three or more
// dots is asked as written first, so these lookups take 2 queries.
//
// Why 3 and not 2: a name with fewer dots than ndots keeps today's search
// list order. The 2-dot service names these pods use (NATS_URL's
// <nats>.<ns>.svc, kubernetes.default.svc, a forge's <svc>.<ns>.svc) must
// stay that way, because musl libc does not fall back to the search list for
// a name with ndots or more dots, and a worker can run a customer's harness
// image. ndots:2 would also send one NXDOMAIN pair per such lookup to the
// upstream resolver. The cost: 2-dot API hosts (container.googleapis.com)
// keep the search list expansion.
//
// A trailing dot in the rendered URLs would also stop the expansion, but
// Python's ssl module sends the dot in the TLS server name, and a Go TLS
// server (k8s-api-proxy) refuses that handshake.
const brokerCallerPodNdots = "3"

// resolverNdotsOption is the resolv.conf option brokerCallerPodNdots sets.
const resolverNdotsOption = "ndots"

// brokerCallerPodDNSConfig is the dnsConfig for those pods: the cluster's
// nameservers and search list (dnsPolicy ClusterFirst) with ndots lowered.
func brokerCallerPodDNSConfig() *corev1.PodDNSConfig {
	return &corev1.PodDNSConfig{
		Options: []corev1.PodDNSConfigOption{{Name: resolverNdotsOption, Value: ptr.To(brokerCallerPodNdots)}},
	}
}
