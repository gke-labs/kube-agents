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
	"errors"
	"testing"
	"time"
)

// humanPatchEntry is a Kubernetes audit LogEntry as the Log Router sink
// delivers it: a human patching a Deployment with kubectl. Trimmed to the
// fields the detector reads plus a couple it must tolerate and ignore.
const humanPatchEntry = `{
  "insertId": "abc123",
  "logName": "projects/example-project/logs/cloudaudit.googleapis.com%2Factivity",
  "timestamp": "2026-09-14T10:30:00.123456Z",
  "severity": "NOTICE",
  "resource": {
    "type": "k8s_cluster",
    "labels": {
      "project_id": "example-project",
      "location": "us-central1",
      "cluster_name": "prod-1"
    }
  },
  "protoPayload": {
    "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
    "serviceName": "k8s.io",
    "methodName": "io.k8s.apps.v1.deployments.patch",
    "resourceName": "apps/v1/namespaces/prod/deployments/checkout",
    "authenticationInfo": {"principalEmail": "engineer@example.com"},
    "requestMetadata": {
      "callerIp": "203.0.113.10",
      "callerSuppliedUserAgent": "kubectl/v1.32.0 (linux/amd64) kubernetes/a1b2c3d"
    },
    "authorizationInfo": [{"granted": true, "permission": "io.k8s.apps.v1.deployments.patch"}],
    "request": {"spec": {"replicas": 5}},
    "response": {"metadata": {"resourceVersion": "9001"}}
  }
}`

func TestParseAuditEntryHumanPatch(t *testing.T) {
	got, err := parseAuditEntry([]byte(humanPatchEntry))
	if err != nil {
		t.Fatalf("parseAuditEntry returned error: %v", err)
	}

	wantTime, err := time.Parse(time.RFC3339Nano, "2026-09-14T10:30:00.123456Z")
	if err != nil {
		t.Fatalf("fixture timestamp is not parseable: %v", err)
	}

	want := AuditRecord{
		InsertID:   "abc123",
		Principal:  "engineer@example.com",
		MethodName: "io.k8s.apps.v1.deployments.patch",
		Verb:       "patch",
		UserAgent:  "kubectl/v1.32.0 (linux/amd64) kubernetes/a1b2c3d",
		Resource: ResourceRef{
			Group:     "apps",
			Version:   "v1",
			Namespace: "prod",
			Resource:  "deployments",
			Name:      "checkout",
		},
		Cluster:   "prod-1",
		Project:   "example-project",
		Location:  "us-central1",
		Timestamp: wantTime,
	}

	if got != want {
		t.Errorf("parseAuditEntry()\n got %+v\nwant %+v", got, want)
	}
}

// The five fields T1 is accepted on, checked by name so a regression says which
// one went missing rather than dumping two structs.
func TestParseAuditEntryPopulatesAcceptanceFields(t *testing.T) {
	rec, err := parseAuditEntry([]byte(humanPatchEntry))
	if err != nil {
		t.Fatalf("parseAuditEntry returned error: %v", err)
	}

	fields := map[string]string{
		"principalEmail":          rec.Principal,
		"methodName":              rec.MethodName,
		"resourceName":            rec.Resource.String(),
		"callerSuppliedUserAgent": rec.UserAgent,
	}
	for name, value := range fields {
		if value == "" {
			t.Errorf("%s is empty", name)
		}
	}
	if rec.Timestamp.IsZero() {
		t.Error("timestamp is zero")
	}
}

func TestParseAuditEntrySkips(t *testing.T) {
	tests := []struct {
		name    string
		entry   string
		wantErr error
	}{
		{
			name: "non-Kubernetes service is acked and dropped",
			entry: `{"protoPayload":{"serviceName":"compute.googleapis.com",
			         "methodName":"v1.compute.instances.insert",
			         "resourceName":"projects/x/zones/y/instances/z"}}`,
			wantErr: errNotKubernetesAudit,
		},
		{
			name: "call that names no object is acked and dropped",
			entry: `{"protoPayload":{"serviceName":"k8s.io",
			         "methodName":"io.k8s.authorization.v1.subjectaccessreviews.create",
			         "resourceName":""}}`,
			wantErr: errNoResourceName,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parseAuditEntry([]byte(tc.entry))
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("parseAuditEntry() error = %v, want %v", err, tc.wantErr)
			}
		})
	}
}

// Malformed input must be distinguishable from a skip, because the two get
// opposite treatment on the subscription: skips ack, failures nack.
func TestParseAuditEntryMalformedIsNotASkip(t *testing.T) {
	_, err := parseAuditEntry([]byte(`{"protoPayload": not json`))
	if err == nil {
		t.Fatal("parseAuditEntry succeeded on malformed JSON, want error")
	}
	if errors.Is(err, errNotKubernetesAudit) || errors.Is(err, errNoResourceName) {
		t.Errorf("malformed JSON reported as a skip (%v); it must nack instead", err)
	}
}

// json.Unmarshal zeroes fields it cannot match, so a payload whose shape
// changed decodes without error and reaches the parser looking like an empty
// one. If that were treated as a skip, every message on the subscription would
// be acked and dropped, and the log would be indistinguishable from a cluster
// with no drift. It has to nack.
func TestParseAuditEntryUnrecognisablePayloadIsNotASkip(t *testing.T) {
	tests := []struct {
		name  string
		entry string
	}{
		{name: "protoPayload absent", entry: `{"insertId":"x","resource":{"type":"k8s_cluster"}}`},
		{name: "protoPayload renamed", entry: `{"insertId":"x","protoPayloadV2":{"serviceName":"k8s.io"}}`},
		{name: "protoPayload empty", entry: `{"insertId":"x","protoPayload":{}}`},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parseAuditEntry([]byte(tc.entry))
			if err == nil {
				t.Fatal("parseAuditEntry succeeded on an unrecognisable payload, want error")
			}
			if errors.Is(err, errNotKubernetesAudit) || errors.Is(err, errNoResourceName) {
				t.Errorf("unrecognisable payload reported as a skip (%v); it must nack instead", err)
			}
		})
	}
}

// The sink filter selects resource.type="k8s_cluster". Anything else on the
// topic came from outside that filter.
func TestParseAuditEntryWrongResourceTypeIsASkip(t *testing.T) {
	entry := `{"resource":{"type":"gce_instance"},
	           "protoPayload":{"serviceName":"k8s.io",
	                           "resourceName":"apps/v1/namespaces/prod/deployments/checkout"}}`
	if _, err := parseAuditEntry([]byte(entry)); !errors.Is(err, errNotKubernetesAudit) {
		t.Errorf("parseAuditEntry() error = %v, want errNotKubernetesAudit", err)
	}
}

func TestMethodVerb(t *testing.T) {
	tests := []struct {
		methodName string
		want       string
	}{
		{"io.k8s.apps.v1.deployments.patch", "patch"},
		{"io.k8s.core.v1.pods.delete", "delete"},
		{"io.k8s.coordination.v1.leases.update", "update"},
		{"io.k8s.core.v1.pods.binding.create", "create"},
		{"unexpected", "unexpected"},
		{"trailing.", "trailing."},
	}

	for _, tc := range tests {
		t.Run(tc.methodName, func(t *testing.T) {
			if got := methodVerb(tc.methodName); got != tc.want {
				t.Errorf("methodVerb(%q) = %q, want %q", tc.methodName, got, tc.want)
			}
		})
	}
}
