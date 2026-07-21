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

// Command k8s-event-watcher is the v2.6 semi-autonomous-triage sidecar.
// It watches Kubernetes Events via a client-go informer, filters to a
// configured allow-list of Event.Reason values, dedupes duplicates
// within a rolling window, and POSTs matched events to a core-agent
// daemon's per-incident session endpoint. See
// docs/k8s-event-agent-design.md for the full design.
package main

import "time"

// EventKey uniquely identifies an incident for dedup purposes: the
// (involvedObject.uid, reason) pair. Same pod + same failure mode =
// same incident, regardless of how many event objects the k8s API
// emits about it.
type EventKey struct {
	UID    string
	Reason string
}

// TriageEvent is the internal representation the filter + dedup +
// injector layers pass around. Derived from *corev1.Event by watcher.go
// but carries no k8s.io/api types itself so unit tests can construct
// it without a fake clientset.
type TriageEvent struct {
	Key           EventKey
	Namespace     string
	KindOfObject  string
	Name          string
	Container     string
	Message       string
	FirstSeen     time.Time
	LastSeen      time.Time
	ControllerRef string
	Node          string
	Labels        map[string]string
	// Count is the k8s Event's own repeat-count field (how many times
	// the source recorded this same event). The sidecar's own dedup
	// counter is separate — see dedup.go.
	Count int
	Type  string
}

// InjectPayload is the JSON body POSTed to
// /sessions/<sid>/inject.message. Field names and casing mirror the
// design doc's "Inject payload shape" section verbatim so playbook
// skills can pattern-match against them.
type InjectPayload struct {
	Kind         string         `json:"kind"`
	Reason       string         `json:"reason"`
	Namespace    string         `json:"namespace"`
	KindOfObject string         `json:"kind_of_object"`
	Name         string         `json:"name"`
	Container    string         `json:"container,omitempty"`
	UID          string         `json:"uid"`
	Message      string         `json:"message"`
	Count        int            `json:"count"`
	FirstSeen    time.Time      `json:"first_seen"`
	LastSeen     time.Time      `json:"last_seen"`
	Cluster      string         `json:"cluster"`
	Context      PayloadContext `json:"context"`
	Type         string         `json:"type"`
}

// PayloadContext is the nested "context" object on InjectPayload.
type PayloadContext struct {
	ControllerRef string            `json:"controller_ref,omitempty"`
	Node          string            `json:"node,omitempty"`
	Labels        map[string]string `json:"labels,omitempty"`
}

// injectKind is the constant we stamp on every payload's "kind"
// field. Skills match against this to distinguish k8s-triggered
// injects from other signal sources (Cloud Monitoring, PagerDuty,
// etc.) that would use different constants when they ship.
const (
	injectKindEvent    = "k8s-event"
	injectKindFollowup = "k8s-event-followup"
)
