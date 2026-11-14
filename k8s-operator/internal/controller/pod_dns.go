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
// and warm pool), the gateway, the A2A gateway, the A2A MCP server and the
// scope publisher. The session pods the gateway spawns set the same value
// (a2a/gateway/spawn.go, sessionPodNdots), and a test in each module reads
// the other's constant, so the two cannot drift apart.
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

// publicAPICallerPodNdots is the resolver's ndots on the pods that resolve
// public API hosts on every call: the broker tiers, where each gcloud call
// resolves container.googleapis.com and the token hosts, and the kube-api
// proxy. Those hosts have 2 dots, so at ndots:3 each lookup still goes
// through all six search domains first (#5609). With ndots:2 they are asked
// as written first: 2 queries, not up to 14.
//
// Only these pods get 2, for two reasons. They are the pods that resolve
// such hosts on every call: the others dial 4-dot in-cluster names, which
// ndots:3 already asks as written. And each runs one container from an
// image of this repository, where every resolver (glibc, Go, and gRPC's
// c-ares in the broker; Go in the proxy) tries the search list when a name
// with ndots or more dots does not resolve as written. So a 2-dot name that
// needs the search list (a forge host written <svc>.<ns>.svc) still resolves
// here, after one NXDOMAIN pair that goes to the upstream resolver. musl
// libc has no such fallback, and the sandbox, the workers, the session pods
// and the gateway (with its sidecars) can run a customer's image, so they
// must not go below brokerCallerPodNdots. The API server is named in full
// (inClusterAPIServer), so its lookups do not take that detour.
const publicAPICallerPodNdots = "2"

// resolverNdotsOption is the resolv.conf option these constants set.
const resolverNdotsOption = "ndots"

// brokerCallerPodDNSConfig is the dnsConfig for the brokerCallerPodNdots
// pods: the cluster's nameservers and search list (dnsPolicy ClusterFirst)
// with ndots lowered.
func brokerCallerPodDNSConfig() *corev1.PodDNSConfig {
	return podDNSConfig(brokerCallerPodNdots)
}

// publicAPICallerPodDNSConfig is the same for the publicAPICallerPodNdots
// pods.
func publicAPICallerPodDNSConfig() *corev1.PodDNSConfig {
	return podDNSConfig(publicAPICallerPodNdots)
}

func podDNSConfig(ndots string) *corev1.PodDNSConfig {
	return &corev1.PodDNSConfig{
		Options: []corev1.PodDNSConfigOption{{Name: resolverNdotsOption, Value: ptr.To(ndots)}},
	}
}
