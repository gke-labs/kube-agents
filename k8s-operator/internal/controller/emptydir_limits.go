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

// The sizeLimits of the operator's scratch emptyDirs that had none. Without a
// sizeLimit a runaway write fills the node's ephemeral storage and the kubelet
// evicts whichever pod it ranks worst; with one, the eviction is this pod, for
// a stated reason, at a predictable threshold (tmpScratchVolumeName argues the
// same). Each is the larger of sixteen times the volume's peak over seven days
// on the dev installs (Cloud Monitoring kubernetes.io/pod/volume/used_bytes,
// to 2026-10-22) and a floor by role: 1Mi for a key or marker mount, 64Mi for a
// component's own small state, 1Gi for a workspace a process writes into.
//
// A container that writes one of these volumes requests its sizeLimit plus
// emptyDirLogsAllowance as ephemeral storage, with the limit equal to it: a
// namespace ResourceQuota on requests.ephemeral-storage refuses a pod with a
// container that lacks a request, a LimitRange defaulting a smaller limit would
// refuse a request with no limit of its own, and Autopilot sets the limit equal
// to the request anyway.
const (
	// The allowance for a writing container's own logs and writable layer on
	// top of the volume it writes. The largest measured among those containers
	// is 2.2 MiB (the a2a session worker).
	emptyDirLogsAllowance = "64Mi"

	// A plugin's staged payload: written once by the stage-<plugin> init
	// container, then mounted read-only, so a payload over the limit fails at
	// the pod's first start, never mid-task. The shipped plugins' payloads are
	// 73,728 bytes (pubsub-platform) and 28,076 bytes (gke-stockout-investigator).
	// A plugin carries what a persona does (skills, code, config), so it is
	// sized from the largest such payload in the tree, agents/platform at
	// 20 MiB: sixteen times that, rounded up.
	pluginStagingSizeLimit = "512Mi"
	// pluginStagingEphemeralRequest is pluginStagingSizeLimit plus emptyDirLogsAllowance.
	pluginStagingEphemeralRequest = "576Mi"

	// fluent-bit buffers in memory (Mem_Buf_Limit, no storage.path) and keeps
	// only its tail databases here: 8.0 MiB peak across 214 gateway pods. Its
	// container already requests 1Gi of ephemeral storage.
	fluentBitStateSizeLimit = "128Mi"

	// The a2a provision Job's HOME, for the nats CLI's context directory: a few
	// files of a few KB. The Job runs for minutes on a bus restart or upgrade,
	// between samples, so it has no measured peak; it takes the small-state floor.
	a2aProvisionTmpSizeLimit = "64Mi"
	// a2aProvisionEphemeralRequest is a2aProvisionTmpSizeLimit plus emptyDirLogsAllowance.
	a2aProvisionEphemeralRequest = "128Mi"

	// The shell sandbox's ~/.hermes block: mounted empty and read-only, so
	// nothing writes it (4 KiB peak across 11 sandbox pods).
	shellSandboxHermesHomeSizeLimit = "1Mi"

	// The staged ssh client key: one key file (8 KiB peak across 187 gateway pods).
	shellSandboxClientKeySizeLimit = "1Mi"
	// shellSandboxClientKeyEphemeralRequest is shellSandboxClientKeySizeLimit
	// plus emptyDirLogsAllowance, for the init container that writes the copy.
	shellSandboxClientKeyEphemeralRequest = "65Mi"
)
