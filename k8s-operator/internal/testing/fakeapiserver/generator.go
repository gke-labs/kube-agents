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

package fakeapiserver

import (
	"encoding/binary"
	"fmt"
	"hash/fnv"
	"strconv"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
)

const (
	// generatorEpoch is the instant every generated timestamp counts from;
	// fixed so two generators with one seed produce byte-identical objects.
	generatorEpoch = "2026-10-01T00:00:00Z"
	// Spacing of the generated timestamps per index, so a dataset spans
	// hours like a real cluster's rather than landing on one instant.
	nodeAgeStep  = 90 * time.Minute
	podAgeStep   = 45 * time.Second
	eventAgeStep = 2 * time.Second

	// The shape of the fleet the names follow: namespaces, workloads per
	// namespace, node pools, zones. Spread by modulo so the distributions are
	// realistic without a random source.
	namespaceCount        = 25
	workloadsPerNamespace = 8
	nodePoolCount         = 4
	zoneCount             = 3
	containerName         = "app"
	nodePoolFormat        = "pool-%d"
	zoneFormat            = "us-central1-%c"
	zoneFirstSuffix       = 'a'
	region                = "us-central1"
	clusterNameFormat     = "gke-prod-%08x"
	namespaceFormat       = "team-%d"
	workloadFormat        = "%s-svc-%d"
	workloadPrefix        = "workload"
	replicaSetFormat      = "%s-%s"
	podNameFormat         = "%s-%s"
	nodeNameFormat        = "gke-prod-%s-%08x-%04x"
	eventNameFormat       = "%s.%016x"
	instanceTypeFormat    = "n2-standard-%d"
	instanceTypeCPUs      = 8
	imageFormat           = "europe-docker.pkg.dev/example/app/%s:2026.10.%02d-%07x"
	imageDayCount         = 28
	podIPFormat           = "10.%d.%d.%d"
	nodeIPFormat          = "10.128.%d.%d"
	podCIDRFormat         = "10.%d.%d.0/24"
	providerIDFormat      = "gce://example-project/%s/%s"
	octetCount            = 256
	kubeletVersion        = "v1.34.1-gke.1000000"
	osImage               = "Container-Optimized OS from Google"
	kernelVersion         = "6.6.72+"
	containerRuntime      = "containerd://2.0.4"
	architecture          = "amd64"
	operatingSystem       = "linux"
	bootDiskType          = "pd-balanced"
	machineFamily         = "n2"
	nodeCPU               = "8"
	nodeMemory            = "32886432Ki"
	nodeAllocatableCPU    = "7910m"
	nodeAllocatableMemory = "29297376Ki"
	nodePods              = "110"
	nodeEphemeralStorage  = "98831908Ki"
	nodeImageCount        = 8
	nodeImageSizeBase     = 48_000_000
	nodeImageSizeStep     = 17_000_000
	podCPURequest         = "250m"
	podMemoryRequest      = "256Mi"
	podCPULimit           = "1"
	podMemoryLimit        = "512Mi"
	podPort               = 8080
	podTerminationGrace   = 30
	podPriority           = 0
	probePath             = "/healthz"
	probePeriodSeconds    = 10
	probeTimeoutSeconds   = 1
	probeFailureThreshold = 3
	serviceAccountName    = "default"
	schedulerName         = "default-scheduler"
	podTemplateHashLength = 10
	podSuffixLength       = 5
	uidFormat             = "%08x-%04x-4%03x-%04x-%012x"
	hashFieldUID          = "uid"
	hashFieldName         = "name"
	hashFieldShape        = "shape"
	hashFieldCount        = "count"
	hashFieldTemplate     = "template"
	hashKindNode          = "node"
	hashKindPod           = "pod"
	hashKindEvent         = "event"
	hashKindReplicaSet    = "replicaset"
	hashKindCluster       = "cluster"
	repeatCountSpan       = 50
	fieldsTypeV1          = "FieldsV1"
	apiVersionV1          = "v1"
	managerKubelet        = "kubelet"
	managerController     = "kube-controller-manager"
	managerScheduler      = "kube-scheduler"
	managerNodeController = "node-controller"
	subresourceStatus     = "status"
	subresourceBinding    = "binding"
	eventFieldPathFormat  = "spec.containers{%s}"
	eventKindPod          = "Pod"
	ownerKindReplicaSet   = "ReplicaSet"
	ownerAPIVersion       = "apps/v1"
	labelApp              = "app"
	labelPodTemplateHash  = "pod-template-hash"
	kubeletPort           = 10250
	externalIPFirstOctet  = 34
	podFSGroup            = 1000
	restartCountSpan      = 3
	uidClockMask          = 0xfff
	uidNodeMask           = 0xffffffffffff
	imageTagMask          = 0xfffffff

	// The managedFields payloads, as the API server records them for a
	// Deployment-owned Pod (its controller, the scheduler's binding, the
	// kubelet's status) and a GKE node (kubelet, node controller, the
	// controller manager's taints). Verbatim shapes, so the bytes a decoder
	// has to carry per object are the bytes a real cluster sends.
	podControllerFields  = `{"f:metadata":{"f:generateName":{},"f:labels":{".":{},"f:app":{},"f:pod-template-hash":{}},"f:ownerReferences":{".":{},"k:{\"uid\":\"%s\"}":{}}},"f:spec":{"f:containers":{"k:{\"name\":\"app\"}":{".":{},"f:env":{".":{},"k:{\"name\":\"POD_NAMESPACE\"}":{".":{},"f:name":{},"f:valueFrom":{".":{},"f:fieldRef":{}}}},"f:image":{},"f:imagePullPolicy":{},"f:livenessProbe":{".":{},"f:failureThreshold":{},"f:httpGet":{".":{},"f:path":{},"f:port":{},"f:scheme":{}},"f:periodSeconds":{},"f:successThreshold":{},"f:timeoutSeconds":{}},"f:name":{},"f:ports":{".":{},"k:{\"containerPort\":8080,\"protocol\":\"TCP\"}":{".":{},"f:containerPort":{},"f:name":{},"f:protocol":{}}},"f:readinessProbe":{".":{},"f:failureThreshold":{},"f:httpGet":{".":{},"f:path":{},"f:port":{},"f:scheme":{}},"f:periodSeconds":{},"f:successThreshold":{},"f:timeoutSeconds":{}},"f:resources":{".":{},"f:limits":{".":{},"f:cpu":{},"f:memory":{}},"f:requests":{".":{},"f:cpu":{},"f:memory":{}}},"f:securityContext":{".":{},"f:allowPrivilegeEscalation":{},"f:capabilities":{".":{},"f:drop":{}},"f:runAsNonRoot":{}},"f:terminationMessagePath":{},"f:terminationMessagePolicy":{},"f:volumeMounts":{".":{},"k:{\"mountPath\":\"/var/cache/app\"}":{".":{},"f:mountPath":{},"f:name":{}}}}},"f:dnsPolicy":{},"f:enableServiceLinks":{},"f:restartPolicy":{},"f:schedulerName":{},"f:securityContext":{".":{},"f:fsGroup":{},"f:seccompProfile":{".":{},"f:type":{}}},"f:serviceAccountName":{},"f:terminationGracePeriodSeconds":{},"f:volumes":{".":{},"k:{\"name\":\"cache\"}":{".":{},"f:emptyDir":{},"f:name":{}}}}}`
	podBindingFields     = `{"f:spec":{"f:nodeName":{}}}`
	podStatusFields      = `{"f:status":{"f:conditions":{"k:{\"type\":\"ContainersReady\"}":{".":{},"f:lastProbeTime":{},"f:lastTransitionTime":{},"f:status":{},"f:type":{}},"k:{\"type\":\"Initialized\"}":{".":{},"f:lastProbeTime":{},"f:lastTransitionTime":{},"f:status":{},"f:type":{}},"k:{\"type\":\"PodReadyToStartContainers\"}":{".":{},"f:lastProbeTime":{},"f:lastTransitionTime":{},"f:status":{},"f:type":{}},"k:{\"type\":\"Ready\"}":{".":{},"f:lastProbeTime":{},"f:lastTransitionTime":{},"f:status":{},"f:type":{}}},"f:containerStatuses":{},"f:hostIP":{},"f:hostIPs":{},"f:phase":{},"f:podIP":{},"f:podIPs":{".":{},"k:{\"ip\":\"%s\"}":{".":{},"f:ip":{}}},"f:startTime":{}}}`
	nodeKubeletFields    = `{"f:metadata":{"f:annotations":{".":{},"f:csi.volume.kubernetes.io/nodeid":{},"f:node.gke.io/last-applied-node-labels":{},"f:volumes.kubernetes.io/controller-managed-attach-detach":{}},"f:labels":{".":{},"f:beta.kubernetes.io/arch":{},"f:beta.kubernetes.io/instance-type":{},"f:beta.kubernetes.io/os":{},"f:cloud.google.com/gke-boot-disk":{},"f:cloud.google.com/gke-container-runtime":{},"f:cloud.google.com/gke-cpu-scaling-level":{},"f:cloud.google.com/gke-logging-variant":{},"f:cloud.google.com/gke-max-pods-per-node":{},"f:cloud.google.com/gke-nodepool":{},"f:cloud.google.com/gke-os-distribution":{},"f:cloud.google.com/gke-provisioning":{},"f:cloud.google.com/gke-stack-type":{},"f:cloud.google.com/machine-family":{},"f:cloud.google.com/private-node":{},"f:failure-domain.beta.kubernetes.io/region":{},"f:failure-domain.beta.kubernetes.io/zone":{},"f:kubernetes.io/arch":{},"f:kubernetes.io/hostname":{},"f:kubernetes.io/os":{},"f:node.kubernetes.io/instance-type":{},"f:topology.gke.io/zone":{},"f:topology.kubernetes.io/region":{},"f:topology.kubernetes.io/zone":{}}},"f:spec":{"f:providerID":{}},"f:status":{"f:addresses":{".":{},"k:{\"type\":\"ExternalIP\"}":{".":{},"f:address":{},"f:type":{}},"k:{\"type\":\"Hostname\"}":{".":{},"f:address":{},"f:type":{}},"k:{\"type\":\"InternalIP\"}":{".":{},"f:address":{},"f:type":{}}},"f:allocatable":{".":{},"f:cpu":{},"f:ephemeral-storage":{},"f:hugepages-1Gi":{},"f:hugepages-2Mi":{},"f:memory":{},"f:pods":{}},"f:capacity":{".":{},"f:cpu":{},"f:ephemeral-storage":{},"f:hugepages-1Gi":{},"f:hugepages-2Mi":{},"f:memory":{},"f:pods":{}},"f:conditions":{".":{},"k:{\"type\":\"DiskPressure\"}":{".":{},"f:lastHeartbeatTime":{},"f:lastTransitionTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}},"k:{\"type\":\"MemoryPressure\"}":{".":{},"f:lastHeartbeatTime":{},"f:lastTransitionTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}},"k:{\"type\":\"PIDPressure\"}":{".":{},"f:lastHeartbeatTime":{},"f:lastTransitionTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}},"k:{\"type\":\"Ready\"}":{".":{},"f:lastHeartbeatTime":{},"f:lastTransitionTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}}},"f:daemonEndpoints":{"f:kubeletEndpoint":{"f:Port":{}}},"f:images":{},"f:nodeInfo":{"f:architecture":{},"f:bootID":{},"f:containerRuntimeVersion":{},"f:kernelVersion":{},"f:kubeProxyVersion":{},"f:kubeletVersion":{},"f:machineID":{},"f:operatingSystem":{},"f:osImage":{},"f:systemUUID":{}}}}`
	nodeControllerFields = `{"f:metadata":{"f:annotations":{"f:node.alpha.kubernetes.io/ttl":{}}},"f:spec":{"f:podCIDR":{},"f:podCIDRs":{".":{},"v:\"%s\"":{}}},"f:status":{"f:conditions":{"k:{\"type\":\"NetworkUnavailable\"}":{".":{},"f:lastHeartbeatTime":{},"f:lastTransitionTime":{},"f:message":{},"f:reason":{},"f:status":{},"f:type":{}}}}}`
	nodeTaintFields      = `{"f:metadata":{"f:labels":{"f:node.kubernetes.io/instance-type":{}}},"f:spec":{"f:taints":{}}}`
	eventKubeletFields   = `{"f:count":{},"f:firstTimestamp":{},"f:involvedObject":{},"f:lastTimestamp":{},"f:message":{},"f:reason":{},"f:reportingComponent":{},"f:reportingInstance":{},"f:source":{"f:component":{},"f:host":{}},"f:type":{}}`
)

// eventShape is one kind of Event a cluster records, with the component that
// records it and the message it carries; generated Events cycle through them
// by hash so a backlog mixes the routine (Pulled, Started) with the ones the
// watcher's filter keeps (BackOff, Unhealthy, FailedScheduling).
type eventShape struct {
	reason    string
	eventType string
	component string
	// nodeHost says whether the recorder is a kubelet (Source.Host set).
	nodeHost bool
	// repeats says whether the Event is one the recorder counts up.
	repeats bool
	message string
}

// eventShapes is the table generated Events cycle through; the index is
// hash(seed, event, i) modulo its length.
var eventShapes = []eventShape{
	{reason: "Pulled", eventType: corev1.EventTypeNormal, component: managerKubelet, nodeHost: true, message: "Successfully pulled image %q in 1.284s (1.284s including waiting). Image size: 184729384 bytes."},
	{reason: "Created", eventType: corev1.EventTypeNormal, component: managerKubelet, nodeHost: true, message: "Created container: " + containerName},
	{reason: "Started", eventType: corev1.EventTypeNormal, component: managerKubelet, nodeHost: true, message: "Started container " + containerName},
	{reason: "Scheduled", eventType: corev1.EventTypeNormal, component: schedulerName, message: "Successfully assigned %s to %s"},
	{reason: "Pulling", eventType: corev1.EventTypeNormal, component: managerKubelet, nodeHost: true, message: "Pulling image %q"},
	{reason: "Killing", eventType: corev1.EventTypeNormal, component: managerKubelet, nodeHost: true, message: "Stopping container " + containerName},
	{reason: "BackOff", eventType: corev1.EventTypeWarning, component: managerKubelet, nodeHost: true, repeats: true, message: "Back-off restarting failed container " + containerName + " in pod %s"},
	{reason: "Unhealthy", eventType: corev1.EventTypeWarning, component: managerKubelet, nodeHost: true, repeats: true, message: "Readiness probe failed: HTTP probe failed with statuscode: 503"},
	{reason: "FailedScheduling", eventType: corev1.EventTypeWarning, component: schedulerName, repeats: true, message: "0/%d nodes are available: %d Insufficient cpu. preemption: 0/%d nodes are available: %d No preemption victims found for incoming pod."},
	{reason: "SuccessfulCreate", eventType: corev1.EventTypeNormal, component: "replicaset-controller", message: "Created pod: %s"},
}

// Sizes is how many of each kind a fake cluster holds.
type Sizes struct {
	Nodes  int
	Pods   int
	Events int
}

// Generator derives a cluster's objects from a seed and an index, so a fake
// cluster holds no dataset: the i-th Node, Pod or Event is the same object
// every time it is asked for, and two generators with one seed agree. Pods
// run on the cluster's Nodes and Events concern its Pods, by index modulo the
// sizes, so the cross-references resolve inside the same cluster.
type Generator struct {
	seed  int64
	sizes Sizes
	epoch time.Time
	// cluster names the fake cluster, from the seed, in node names and
	// provider IDs.
	cluster string
}

// NewGenerator returns the generator for seed over a cluster of sizes.
func NewGenerator(seed int64, sizes Sizes) *Generator {
	epoch, err := time.Parse(time.RFC3339, generatorEpoch)
	if err != nil {
		panic(err)
	}
	g := &Generator{seed: seed, sizes: sizes, epoch: epoch}
	g.cluster = fmt.Sprintf(clusterNameFormat, uint32(g.hash(hashKindCluster, 0, hashFieldName)))
	return g
}

// Sizes is the cluster's object counts.
func (g *Generator) Sizes() Sizes { return g.sizes }

// hash is the one source of variation: FNV-1a over the seed, the kind, the
// index and the field, so each field varies independently and reproducibly.
func (g *Generator) hash(kind string, i int, field string) uint64 {
	h := fnv.New64a()
	var buf [8]byte
	binary.LittleEndian.PutUint64(buf[:], uint64(g.seed))
	_, _ = h.Write(buf[:])
	_, _ = h.Write([]byte(kind))
	binary.LittleEndian.PutUint64(buf[:], uint64(i))
	_, _ = h.Write(buf[:])
	_, _ = h.Write([]byte(field))
	return h.Sum64()
}

// uid is a UUID-shaped identifier for the i-th object of kind.
func (g *Generator) uid(kind string, i int) types.UID {
	a := g.hash(kind, i, hashFieldUID)
	b := g.hash(kind, i, hashFieldUID+hashFieldName)
	return types.UID(fmt.Sprintf(uidFormat, uint32(a), uint16(a>>32), uint16(a>>48)&uidClockMask, uint16(b), b>>16&uidNodeMask))
}

func (g *Generator) at(step time.Duration, i int) metav1.Time {
	return metav1.NewTime(g.epoch.Add(step * time.Duration(i)))
}

func (g *Generator) zone(i int) string {
	return fmt.Sprintf(zoneFormat, zoneFirstSuffix+rune(i%zoneCount))
}

func (g *Generator) nodePool(i int) string {
	return fmt.Sprintf(nodePoolFormat, i%nodePoolCount)
}

// NodeName is the name of the i-th Node.
func (g *Generator) NodeName(i int) string {
	return fmt.Sprintf(nodeNameFormat, g.nodePool(i), uint32(g.hash(hashKindNode, i%nodePoolCount, hashFieldName)), uint16(g.hash(hashKindNode, i, hashFieldName)))
}

func (g *Generator) namespace(i int) string {
	return fmt.Sprintf(namespaceFormat, i%namespaceCount)
}

func (g *Generator) workload(i int) string {
	return fmt.Sprintf(workloadFormat, workloadPrefix, (i/namespaceCount)%workloadsPerNamespace)
}

func (g *Generator) replicaSet(i int) (name string, uid types.UID) {
	index := i % (namespaceCount * workloadsPerNamespace)
	hash := strconv.FormatUint(g.hash(hashKindReplicaSet, index, hashFieldTemplate), 36)
	if len(hash) > podTemplateHashLength {
		hash = hash[:podTemplateHashLength]
	}
	return fmt.Sprintf(replicaSetFormat, g.workload(i), hash), g.uid(hashKindReplicaSet, index)
}

// PodName is the name of the i-th Pod, and its namespace.
func (g *Generator) PodName(i int) (name, namespace string) {
	rs, _ := g.replicaSet(i)
	suffix := strconv.FormatUint(g.hash(hashKindPod, i, hashFieldName), 36)
	if len(suffix) > podSuffixLength {
		suffix = suffix[:podSuffixLength]
	}
	return fmt.Sprintf(podNameFormat, rs, suffix), g.namespace(i)
}

func (g *Generator) podIP(i int) string {
	return fmt.Sprintf(podIPFormat, (i/(octetCount*octetCount))%octetCount, (i/octetCount)%octetCount, i%octetCount)
}

func (g *Generator) nodeIP(i int) string {
	return fmt.Sprintf(nodeIPFormat, (i/octetCount)%octetCount, i%octetCount)
}

func (g *Generator) image(i int) string {
	return fmt.Sprintf(imageFormat, g.workload(i), 1+i%imageDayCount, uint32(g.hash(hashKindPod, i, hashFieldTemplate))&imageTagMask)
}

func managedField(manager, operation, subresource string, at metav1.Time, fields string) metav1.ManagedFieldsEntry {
	return metav1.ManagedFieldsEntry{
		Manager:     manager,
		Operation:   metav1.ManagedFieldsOperationType(operation),
		APIVersion:  apiVersionV1,
		Time:        &at,
		FieldsType:  fieldsTypeV1,
		FieldsV1:    &metav1.FieldsV1{Raw: []byte(fields)},
		Subresource: subresource,
	}
}

// Node is the i-th Node: a GKE node as the API server returns it, labels,
// annotations, capacity, conditions, images and three managedFields entries.
func (g *Generator) Node(i int) *corev1.Node {
	name := g.NodeName(i)
	created := g.at(nodeAgeStep, i)
	zone := g.zone(i)
	instanceType := fmt.Sprintf(instanceTypeFormat, instanceTypeCPUs)
	podCIDR := fmt.Sprintf(podCIDRFormat, (i/octetCount)%octetCount, i%octetCount)
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name:              name,
			UID:               g.uid(hashKindNode, i),
			CreationTimestamp: created,
			Labels: map[string]string{
				"beta.kubernetes.io/arch":                  architecture,
				"beta.kubernetes.io/instance-type":         instanceType,
				"beta.kubernetes.io/os":                    operatingSystem,
				"cloud.google.com/gke-boot-disk":           bootDiskType,
				"cloud.google.com/gke-container-runtime":   "containerd",
				"cloud.google.com/gke-cpu-scaling-level":   nodeCPU,
				"cloud.google.com/gke-logging-variant":     "DEFAULT",
				"cloud.google.com/gke-max-pods-per-node":   nodePods,
				"cloud.google.com/gke-nodepool":            g.nodePool(i),
				"cloud.google.com/gke-os-distribution":     "cos",
				"cloud.google.com/gke-provisioning":        "standard",
				"cloud.google.com/gke-stack-type":          "IPV4",
				"cloud.google.com/machine-family":          machineFamily,
				"cloud.google.com/private-node":            "false",
				"failure-domain.beta.kubernetes.io/region": region,
				"failure-domain.beta.kubernetes.io/zone":   zone,
				"kubernetes.io/arch":                       architecture,
				"kubernetes.io/hostname":                   name,
				"kubernetes.io/os":                         operatingSystem,
				"node.kubernetes.io/instance-type":         instanceType,
				"topology.gke.io/zone":                     zone,
				"topology.kubernetes.io/region":            region,
				"topology.kubernetes.io/zone":              zone,
			},
			Annotations: map[string]string{
				"container.googleapis.com/instance_id":                   strconv.FormatUint(g.hash(hashKindNode, i, hashFieldUID)>>1, 10),
				"csi.volume.kubernetes.io/nodeid":                        fmt.Sprintf(`{"pd.csi.storage.gke.io":"projects/example-project/zones/%s/instances/%s"}`, zone, name),
				"node.alpha.kubernetes.io/ttl":                           "0",
				"node.gke.io/last-applied-node-labels":                   "cloud.google.com/gke-boot-disk=" + bootDiskType + ",cloud.google.com/gke-nodepool=" + g.nodePool(i),
				"volumes.kubernetes.io/controller-managed-attach-detach": "true",
			},
			ManagedFields: []metav1.ManagedFieldsEntry{
				managedField(managerKubelet, string(metav1.ManagedFieldsOperationUpdate), subresourceStatus, created, nodeKubeletFields),
				managedField(managerNodeController, string(metav1.ManagedFieldsOperationUpdate), "", created, fmt.Sprintf(nodeControllerFields, podCIDR)),
				managedField(managerController, string(metav1.ManagedFieldsOperationUpdate), "", created, nodeTaintFields),
			},
		},
		Spec: corev1.NodeSpec{
			PodCIDR:    podCIDR,
			PodCIDRs:   []string{podCIDR},
			ProviderID: fmt.Sprintf(providerIDFormat, zone, name),
		},
		Status: corev1.NodeStatus{
			Capacity: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse(nodeCPU),
				corev1.ResourceMemory:           resource.MustParse(nodeMemory),
				corev1.ResourcePods:             resource.MustParse(nodePods),
				corev1.ResourceEphemeralStorage: resource.MustParse(nodeEphemeralStorage),
			},
			Allocatable: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse(nodeAllocatableCPU),
				corev1.ResourceMemory:           resource.MustParse(nodeAllocatableMemory),
				corev1.ResourcePods:             resource.MustParse(nodePods),
				corev1.ResourceEphemeralStorage: resource.MustParse(nodeEphemeralStorage),
			},
			Conditions: []corev1.NodeCondition{
				{Type: corev1.NodeNetworkUnavailable, Status: corev1.ConditionFalse, Reason: "RouteCreated", Message: "NodeController create implicit route", LastHeartbeatTime: created, LastTransitionTime: created},
				{Type: corev1.NodeMemoryPressure, Status: corev1.ConditionFalse, Reason: "KubeletHasSufficientMemory", Message: "kubelet has sufficient memory available", LastHeartbeatTime: created, LastTransitionTime: created},
				{Type: corev1.NodeDiskPressure, Status: corev1.ConditionFalse, Reason: "KubeletHasNoDiskPressure", Message: "kubelet has no disk pressure", LastHeartbeatTime: created, LastTransitionTime: created},
				{Type: corev1.NodePIDPressure, Status: corev1.ConditionFalse, Reason: "KubeletHasSufficientPID", Message: "kubelet has sufficient PID available", LastHeartbeatTime: created, LastTransitionTime: created},
				{Type: corev1.NodeReady, Status: corev1.ConditionTrue, Reason: "KubeletReady", Message: "kubelet is posting ready status", LastHeartbeatTime: created, LastTransitionTime: created},
			},
			Addresses: []corev1.NodeAddress{
				{Type: corev1.NodeInternalIP, Address: g.nodeIP(i)},
				{Type: corev1.NodeExternalIP, Address: fmt.Sprintf(podIPFormat, externalIPFirstOctet, (i/octetCount)%octetCount, i%octetCount)},
				{Type: corev1.NodeHostName, Address: name},
			},
			DaemonEndpoints: corev1.NodeDaemonEndpoints{KubeletEndpoint: corev1.DaemonEndpoint{Port: kubeletPort}},
			NodeInfo: corev1.NodeSystemInfo{
				MachineID:               fmt.Sprintf("%016x%016x", g.hash(hashKindNode, i, hashFieldUID), g.hash(hashKindNode, i, hashFieldName)),
				SystemUUID:              string(g.uid(hashKindNode, i)),
				BootID:                  string(g.uid(hashKindNode, i+1)),
				KernelVersion:           kernelVersion,
				OSImage:                 osImage,
				ContainerRuntimeVersion: containerRuntime,
				KubeletVersion:          kubeletVersion,
				KubeProxyVersion:        kubeletVersion,
				OperatingSystem:         operatingSystem,
				Architecture:            architecture,
			},
		},
	}
	for n := 0; n < nodeImageCount; n++ {
		node.Status.Images = append(node.Status.Images, corev1.ContainerImage{
			Names:     []string{g.image(i + n)},
			SizeBytes: int64(nodeImageSizeBase + n*nodeImageSizeStep),
		})
	}
	return node
}

// Pod is the i-th Pod: a Deployment-owned Pod with one container, probes,
// resources, a volume, its status, and the three managedFields entries its
// controller, the scheduler and the kubelet leave on it.
func (g *Generator) Pod(i int) *corev1.Pod {
	name, namespace := g.PodName(i)
	rsName, rsUID := g.replicaSet(i)
	created := g.at(podAgeStep, i)
	ip := g.podIP(i)
	nodeName := ""
	hostIP := ""
	if g.sizes.Nodes > 0 {
		nodeName = g.NodeName(i % g.sizes.Nodes)
		hostIP = g.nodeIP(i % g.sizes.Nodes)
	}
	templateHash := rsName[len(g.workload(i))+1:]
	grace := int64(podTerminationGrace)
	priority := int32(podPriority)
	controller := true
	nonRoot := true
	noEscalation := false
	fsGroup := int64(podFSGroup)
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:              name,
			Namespace:         namespace,
			GenerateName:      rsName + "-",
			UID:               g.uid(hashKindPod, i),
			CreationTimestamp: created,
			Labels:            map[string]string{labelApp: g.workload(i), labelPodTemplateHash: templateHash},
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: ownerAPIVersion, Kind: ownerKindReplicaSet, Name: rsName, UID: rsUID,
				Controller: &controller, BlockOwnerDeletion: &controller,
			}},
			ManagedFields: []metav1.ManagedFieldsEntry{
				managedField(managerController, string(metav1.ManagedFieldsOperationUpdate), "", created, fmt.Sprintf(podControllerFields, rsUID)),
				managedField(managerScheduler, string(metav1.ManagedFieldsOperationUpdate), subresourceBinding, created, podBindingFields),
				managedField(managerKubelet, string(metav1.ManagedFieldsOperationUpdate), subresourceStatus, created, fmt.Sprintf(podStatusFields, ip)),
			},
		},
		Spec: corev1.PodSpec{
			NodeName:                      nodeName,
			ServiceAccountName:            serviceAccountName,
			SchedulerName:                 schedulerName,
			RestartPolicy:                 corev1.RestartPolicyAlways,
			DNSPolicy:                     corev1.DNSClusterFirst,
			TerminationGracePeriodSeconds: &grace,
			Priority:                      &priority,
			EnableServiceLinks:            &controller,
			SecurityContext:               &corev1.PodSecurityContext{FSGroup: &fsGroup, SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
			Volumes:                       []corev1.Volume{{Name: "cache", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}},
			Containers: []corev1.Container{{
				Name:            containerName,
				Image:           g.image(i),
				ImagePullPolicy: corev1.PullIfNotPresent,
				Ports:           []corev1.ContainerPort{{Name: "http", ContainerPort: podPort, Protocol: corev1.ProtocolTCP}},
				Env: []corev1.EnvVar{{
					Name:      "POD_NAMESPACE",
					ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{APIVersion: apiVersionV1, FieldPath: "metadata.namespace"}},
				}},
				Resources: corev1.ResourceRequirements{
					Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(podCPURequest), corev1.ResourceMemory: resource.MustParse(podMemoryRequest)},
					Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse(podCPULimit), corev1.ResourceMemory: resource.MustParse(podMemoryLimit)},
				},
				LivenessProbe:            probe(),
				ReadinessProbe:           probe(),
				VolumeMounts:             []corev1.VolumeMount{{Name: "cache", MountPath: "/var/cache/app"}},
				TerminationMessagePath:   corev1.TerminationMessagePathDefault,
				TerminationMessagePolicy: corev1.TerminationMessageReadFile,
				SecurityContext: &corev1.SecurityContext{
					RunAsNonRoot:             &nonRoot,
					AllowPrivilegeEscalation: &noEscalation,
					Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
				},
			}},
		},
		Status: corev1.PodStatus{
			Phase:     corev1.PodRunning,
			HostIP:    hostIP,
			PodIP:     ip,
			PodIPs:    []corev1.PodIP{{IP: ip}},
			QOSClass:  corev1.PodQOSBurstable,
			StartTime: &created,
			Conditions: []corev1.PodCondition{
				{Type: corev1.PodReadyToStartContainers, Status: corev1.ConditionTrue, LastTransitionTime: created},
				{Type: corev1.PodInitialized, Status: corev1.ConditionTrue, LastTransitionTime: created},
				{Type: corev1.PodReady, Status: corev1.ConditionTrue, LastTransitionTime: created},
				{Type: corev1.ContainersReady, Status: corev1.ConditionTrue, LastTransitionTime: created},
				{Type: corev1.PodScheduled, Status: corev1.ConditionTrue, LastTransitionTime: created},
			},
			ContainerStatuses: []corev1.ContainerStatus{{
				Name:         containerName,
				Image:        g.image(i),
				ImageID:      g.image(i) + "@sha256:" + fmt.Sprintf("%016x%016x%016x%016x", g.hash(hashKindPod, i, hashFieldTemplate), g.hash(hashKindPod, i, hashFieldUID), g.hash(hashKindPod, i, hashFieldName), g.hash(hashKindPod, i, hashFieldShape)),
				ContainerID:  "containerd://" + fmt.Sprintf("%016x%016x%016x%016x", g.hash(hashKindPod, i, hashFieldShape), g.hash(hashKindPod, i, hashFieldName), g.hash(hashKindPod, i, hashFieldUID), g.hash(hashKindPod, i, hashFieldTemplate)),
				Ready:        true,
				Started:      &controller,
				RestartCount: int32(g.hash(hashKindPod, i, hashFieldCount) % restartCountSpan),
				State:        corev1.ContainerState{Running: &corev1.ContainerStateRunning{StartedAt: created}},
			}},
		},
	}
}

func probe() *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler:     corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: probePath, Port: intstr.FromInt32(podPort), Scheme: corev1.URISchemeHTTP}},
		PeriodSeconds:    probePeriodSeconds,
		TimeoutSeconds:   probeTimeoutSeconds,
		SuccessThreshold: 1,
		FailureThreshold: probeFailureThreshold,
	}
}

// Event is the i-th Event: one of eventShapes about the Pod it concerns
// (index modulo the Pod count), recorded by the kubelet of that Pod's node,
// the scheduler or a controller, with the managedFields entry recorders leave.
func (g *Generator) Event(i int) *corev1.Event {
	shape := eventShapes[g.hash(hashKindEvent, i, hashFieldShape)%uint64(len(eventShapes))]
	podIndex := 0
	if g.sizes.Pods > 0 {
		podIndex = i % g.sizes.Pods
	}
	podName, namespace := g.PodName(podIndex)
	nodeName := ""
	if g.sizes.Nodes > 0 {
		nodeName = g.NodeName(podIndex % g.sizes.Nodes)
	}
	first := g.at(eventAgeStep, i)
	last := first
	count := int32(1)
	if shape.repeats {
		count = 1 + int32(g.hash(hashKindEvent, i, hashFieldCount)%repeatCountSpan)
		last = metav1.NewTime(first.Add(eventAgeStep * time.Duration(count)))
	}
	var message string
	switch shape.reason {
	case "Pulled", "Pulling":
		message = fmt.Sprintf(shape.message, g.image(podIndex))
	case "Scheduled":
		message = fmt.Sprintf(shape.message, namespace+"/"+podName, nodeName)
	case "BackOff", "SuccessfulCreate":
		message = fmt.Sprintf(shape.message, podName)
	case "FailedScheduling":
		message = fmt.Sprintf(shape.message, g.sizes.Nodes, g.sizes.Nodes, g.sizes.Nodes, g.sizes.Nodes)
	default:
		message = shape.message
	}
	source := corev1.EventSource{Component: shape.component}
	if shape.nodeHost {
		source.Host = nodeName
	}
	involved := corev1.ObjectReference{
		Kind: eventKindPod, Namespace: namespace, Name: podName, UID: g.uid(hashKindPod, podIndex),
		APIVersion: apiVersionV1, FieldPath: fmt.Sprintf(eventFieldPathFormat, containerName),
	}
	if shape.reason == "SuccessfulCreate" {
		rsName, rsUID := g.replicaSet(podIndex)
		involved = corev1.ObjectReference{Kind: ownerKindReplicaSet, Namespace: namespace, Name: rsName, UID: rsUID, APIVersion: ownerAPIVersion}
	}
	ev := &corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:              fmt.Sprintf(eventNameFormat, involved.Name, g.hash(hashKindEvent, i, hashFieldName)),
			Namespace:         namespace,
			UID:               g.uid(hashKindEvent, i),
			CreationTimestamp: first,
			ManagedFields: []metav1.ManagedFieldsEntry{
				managedField(shape.component, string(metav1.ManagedFieldsOperationUpdate), "", first, eventKubeletFields),
			},
		},
		InvolvedObject:      involved,
		Reason:              shape.reason,
		Message:             message,
		Source:              source,
		FirstTimestamp:      first,
		LastTimestamp:       last,
		Count:               count,
		Type:                shape.eventType,
		ReportingController: shape.component,
		ReportingInstance:   source.Host,
	}
	return ev
}
