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
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	// kubernetesAuditService is the serviceName Cloud Audit Logs stamps on
	// Kubernetes API calls. The sink filter already restricts to
	// resource.type="k8s_cluster", so an entry carrying a different service is
	// a sink whose filter no longer matches this detector: understood, not
	// actionable, and acked -- but counted and logged, because a subscription
	// where every message takes that path is a misconfiguration and not a quiet
	// cluster.
	kubernetesAuditService = "k8s.io"

	// kubernetesClusterResourceType is the monitored-resource type the sink
	// filter selects on. An entry carrying some other non-empty type reached
	// the topic from outside that filter.
	kubernetesClusterResourceType = "k8s_cluster"

	// methodNameSeparator splits a Cloud Audit Logs methodName
	// ("io.k8s.apps.v1.deployments.patch") into its components. The verb is
	// the last one.
	methodNameSeparator = "."
)

// errNotKubernetesAudit reports an entry that is not a Kubernetes API audit
// record. Distinct from a parse failure: the message was understood, it is
// simply not ours, so it is acked and dropped rather than redelivered.
var errNotKubernetesAudit = errors.New("entry is not a Kubernetes audit record")

// logEntry is the subset of a Cloud Logging LogEntry the detector reads. The
// full entry carries request and response bodies that can run to megabytes;
// decoding only these fields keeps the hot path off them.
type logEntry struct {
	InsertID     string       `json:"insertId"`
	Timestamp    time.Time    `json:"timestamp"`
	Resource     logResource  `json:"resource"`
	ProtoPayload auditPayload `json:"protoPayload"`
}

// logResource is the monitored resource the entry describes. For
// resource.type="k8s_cluster" the labels identify the cluster, which the
// detector needs because the sink is project-wide: one topic carries every
// cluster in the project.
type logResource struct {
	Type   string `json:"type"`
	Labels struct {
		ProjectID   string `json:"project_id"`
		Location    string `json:"location"`
		ClusterName string `json:"cluster_name"`
	} `json:"labels"`
}

// auditPayload is the subset of google.cloud.audit.AuditLog the detector reads.
type auditPayload struct {
	ServiceName        string `json:"serviceName"`
	MethodName         string `json:"methodName"`
	ResourceName       string `json:"resourceName"`
	AuthenticationInfo struct {
		PrincipalEmail string `json:"principalEmail"`
	} `json:"authenticationInfo"`
	RequestMetadata struct {
		CallerSuppliedUserAgent string `json:"callerSuppliedUserAgent"`
	} `json:"requestMetadata"`
}

// AuditRecord is one mutating Kubernetes API call, decomposed into the fields
// the rest of the detector works from. It carries no Cloud Logging types, so
// T2's classifier and T3's attribution join can be tested without a message.
type AuditRecord struct {
	// InsertID is Cloud Logging's own unique id for the entry. Pub/Sub
	// delivers at least once, so this is what a dedup layer keys on.
	InsertID string

	// Principal is the authenticated caller: a user, a "system:" controller,
	// or a service account. T2 classifies on this.
	Principal string

	// MethodName is the full audit method, "io.k8s.apps.v1.deployments.patch".
	MethodName string

	// Verb is the trailing component of MethodName -- create, patch, update,
	// delete. T3 short-circuits the live-object lookup on delete.
	Verb string

	// UserAgent is the caller-supplied user agent. It is a hint, not evidence:
	// anything can set it, so T2 classifies on Principal and uses this only to
	// explain a decision to a human.
	UserAgent string

	// Resource identifies the object the call touched.
	Resource ResourceRef

	// Cluster, Project, and Location identify which cluster in the fleet the
	// call landed on. A GKE cluster name is unique only within a project and
	// location, so all three are needed to reach it again in T3.
	Cluster  string
	Project  string
	Location string

	// Timestamp is when the API server recorded the call.
	Timestamp time.Time
}

// parseAuditEntry decodes one Pub/Sub message body into an AuditRecord.
//
// The error tells the caller what to do with the message. errNotKubernetesAudit
// and errNoResourceName mean the entry was understood and is not actionable:
// ack it. Anything else means the payload did not have the shape this detector
// expects, which is a change on Google's side or a bug here, and nacking makes
// it loud rather than silently dropping drift.
func parseAuditEntry(data []byte) (AuditRecord, error) {
	var entry logEntry
	if err := json.Unmarshal(data, &entry); err != nil {
		return AuditRecord{}, fmt.Errorf("decode log entry: %w", err)
	}

	// An absent serviceName is not a message from another service -- it is a
	// message whose protoPayload this parser could not find. json.Unmarshal
	// zeroes what it cannot match, so a renamed or restructured payload decodes
	// without error and arrives here looking exactly like an empty one. Treating
	// that as a skip would ack every message on the subscription while logging
	// nothing, which is the one failure mode the nack path exists to prevent.
	if entry.ProtoPayload.ServiceName == "" {
		return AuditRecord{}, errors.New("entry has no protoPayload.serviceName: the payload shape is not the one this parser expects")
	}
	if entry.ProtoPayload.ServiceName != kubernetesAuditService {
		return AuditRecord{}, fmt.Errorf("%w: serviceName %q", errNotKubernetesAudit, entry.ProtoPayload.ServiceName)
	}
	if entry.Resource.Type != "" && entry.Resource.Type != kubernetesClusterResourceType {
		return AuditRecord{}, fmt.Errorf("%w: resource.type %q", errNotKubernetesAudit, entry.Resource.Type)
	}

	ref, err := parseResourceName(entry.ProtoPayload.ResourceName)
	if err != nil {
		return AuditRecord{}, err
	}

	return AuditRecord{
		InsertID:   entry.InsertID,
		Principal:  entry.ProtoPayload.AuthenticationInfo.PrincipalEmail,
		MethodName: entry.ProtoPayload.MethodName,
		Verb:       methodVerb(entry.ProtoPayload.MethodName),
		UserAgent:  entry.ProtoPayload.RequestMetadata.CallerSuppliedUserAgent,
		Resource:   ref,
		Cluster:    entry.Resource.Labels.ClusterName,
		Project:    entry.Resource.Labels.ProjectID,
		Location:   entry.Resource.Labels.Location,
		Timestamp:  entry.Timestamp,
	}, nil
}

// methodVerb returns the trailing component of a Cloud Audit Logs methodName,
// which is the Kubernetes verb. Returns the whole string when there is no
// separator, so an unexpected shape surfaces in the log rather than as "".
func methodVerb(methodName string) string {
	idx := strings.LastIndex(methodName, methodNameSeparator)
	if idx < 0 || idx == len(methodName)-1 {
		return methodName
	}
	return methodName[idx+1:]
}
