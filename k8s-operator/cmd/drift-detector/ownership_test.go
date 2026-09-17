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
	"slices"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

// managedFieldsObject builds an object carrying the given entries, which is the
// only input ownership reads.
func managedFieldsObject(entries ...metav1.ManagedFieldsEntry) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetManagedFields(entries)
	return obj
}

// entry builds one managedFields entry with a FieldsV1 blob written as JSON.
func entry(manager, operation, subresource, fieldsJSON string, at *time.Time) metav1.ManagedFieldsEntry {
	e := metav1.ManagedFieldsEntry{
		Manager:     manager,
		Operation:   metav1.ManagedFieldsOperationType(operation),
		Subresource: subresource,
	}
	if fieldsJSON != "" {
		e.FieldsV1 = &metav1.FieldsV1{Raw: []byte(fieldsJSON)}
	}
	if at != nil {
		e.Time = &metav1.Time{Time: *at}
	}
	return e
}

func TestFieldPathsRendersEveryAddressingForm(t *testing.T) {
	for _, tc := range []struct {
		name  string
		blob  string
		paths []string
	}{
		{
			name:  "named fields nest into a dotted path",
			blob:  `{"f:spec":{"f:replicas":{}}}`,
			paths: []string{"spec.replicas"},
		},
		{
			name: "a merge key renders as a selector, not as a path component",
			blob: `{"f:spec":{"f:containers":{"k:{\"name\":\"app\"}":{"f:image":{}}}}}`,
			// Without the selector this would be "spec.containers.image",
			// which does not say which container drifted.
			paths: []string{"spec.containers[name=app].image"},
		},
		{
			name: "a selector attaches to the field it indexes, at every depth",
			blob: `{"f:spec":{"f:containers":{"k:{\"name\":\"app\"}":{"f:ports":{"k:{\"containerPort\":80}":{}}}}}}`,
			// The bug this guards is a path joiner that treats a selector as
			// an ordinary component: "spec.containers.[name=app].ports.[...]".
			paths: []string{"spec.containers[name=app].ports[containerPort=80]"},
		},
		{
			name:  "a compound merge key renders both parts, sorted",
			blob:  `{"f:spec":{"f:ports":{"k:{\"protocol\":\"TCP\",\"port\":443}":{}}}}`,
			paths: []string{"spec.ports[port=443,protocol=TCP]"},
		},
		{
			name:  "a scalar list entry is addressed by value",
			blob:  `{"f:spec":{"f:finalizers":{"v:\"kubernetes\"":{}}}}`,
			paths: []string{`spec.finalizers["kubernetes"]`},
		},
		{
			name:  "an atomic list entry is addressed by index",
			blob:  `{"f:spec":{"f:rules":{"i:0":{}}}}`,
			paths: []string{"spec.rules[0]"},
		},
		{
			name: "the self marker claims the containing field, not a child of it",
			blob: `{"f:spec":{"f:template":{".":{},"f:spec":{}}}}`,
			// Owning spec.template whole and owning one field inside it are
			// different claims, so both appear.
			paths: []string{"spec.template", "spec.template.spec"},
		},
		{
			name:  "the self marker at the root emits nothing, rather than an empty path",
			blob:  `{".":{},"f:spec":{}}`,
			paths: []string{"spec"},
		},
		{
			name: "an unrecognised prefix is kept whole",
			blob: `{"z:novel":{}}`,
			// Stripping it would produce a path that reads as valid and is
			// not; kept, it shows up in a log line as a bug report.
			paths: []string{"z:novel"},
		},
		{
			name:  "a blob that is not an object at that level still yields its path",
			blob:  `{"f:spec":{"f:replicas":3}}`,
			paths: []string{"spec.replicas"},
		},
		{
			name:  "paths come back sorted",
			blob:  `{"f:spec":{},"f:metadata":{},"f:status":{}}`,
			paths: []string{"metadata", "spec", "status"},
		},
		{
			name:  "an empty blob yields nothing",
			blob:  "",
			paths: nil,
		},
		{
			name:  "an undecodable blob yields nothing rather than panicking",
			blob:  `{"f:spec":`,
			paths: nil,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := fieldPaths([]byte(tc.blob))
			if !slices.Equal(got, tc.paths) {
				t.Errorf("fieldPaths(%s) = %v, want %v", tc.blob, got, tc.paths)
			}
		})
	}
}

func TestRenderListKeyFallsBackToRawJSON(t *testing.T) {
	// A selector that does not decode is rendered whole for the same reason an
	// unknown prefix is: a wrong-looking path is a bug report, a silently
	// plausible one is not.
	if got, want := renderListKey(`{"name":`), `[{"name":]`; got != want {
		t.Errorf("renderListKey = %q, want %q", got, want)
	}
}

func TestOwnershipDecodesEveryEntry(t *testing.T) {
	at := time.Date(2026, 9, 16, 10, 0, 0, 0, time.UTC)
	obj := managedFieldsObject(
		entry("argocd-controller", "Apply", "", `{"f:spec":{"f:replicas":{}}}`, &at),
		entry("kubelet", "Update", "status", `{"f:status":{"f:phase":{}}}`, nil),
	)

	owners := ownership(obj)
	if len(owners) != 2 {
		t.Fatalf("ownership returned %d owners, want 2", len(owners))
	}

	if owners[0].Manager != "argocd-controller" {
		t.Errorf("owners[0].Manager = %q, want argocd-controller", owners[0].Manager)
	}
	if owners[0].Operation != "Apply" {
		t.Errorf("owners[0].Operation = %q, want Apply", owners[0].Operation)
	}
	if !owners[0].UpdatedAt.Equal(at) {
		t.Errorf("owners[0].UpdatedAt = %v, want %v", owners[0].UpdatedAt, at)
	}
	if !slices.Equal(owners[0].Paths, []string{"spec.replicas"}) {
		t.Errorf("owners[0].Paths = %v, want [spec.replicas]", owners[0].Paths)
	}

	if owners[1].Subresource != "status" {
		t.Errorf("owners[1].Subresource = %q, want status", owners[1].Subresource)
	}
	// A nil entry.Time is legal upstream; it must not become a wall-clock
	// reading, because reconciledBy treats "at or after the change" as a
	// reconcile and a defaulted now() would claim one on every lookup.
	if !owners[1].UpdatedAt.IsZero() {
		t.Errorf("owners[1].UpdatedAt = %v, want the zero time for an entry with no Time", owners[1].UpdatedAt)
	}
}

func TestOwnershipKeepsAnEntryWithNoReadableFields(t *testing.T) {
	// "Nobody owns this field" is the false positive the join exists to avoid,
	// so an entry whose blob is missing is kept with no paths rather than
	// dropped -- visibly a gap in the data, not an absence of claims.
	//
	// The other half of that rule, an entry whose blob is present and does not
	// decode, has no test because it cannot be constructed:
	// unstructured.SetManagedFields serialises the entries and drops one whose
	// FieldsV1 will not round-trip, which is the same conversion the real
	// client puts the API server's response through. ownership handles it
	// anyway, and fieldPaths' undecodable case above is what covers the
	// decoding half directly.
	owners := ownership(managedFieldsObject(entry("mystery", "Update", "", "", nil)))
	if len(owners) != 1 {
		t.Fatalf("ownership returned %d owners, want the entry kept", len(owners))
	}
	if owners[0].Manager != "mystery" {
		t.Errorf("Manager = %q, want mystery", owners[0].Manager)
	}
	if len(owners[0].Paths) != 0 {
		t.Errorf("Paths = %v, want none", owners[0].Paths)
	}
}

func TestOwnershipOnAnObjectWithNoManagedFields(t *testing.T) {
	if owners := ownership(&unstructured.Unstructured{}); owners != nil {
		t.Errorf("ownership = %v, want nil for an object nothing has ever managed", owners)
	}
}

func TestFieldOwnerStringTruncatesLongPathLists(t *testing.T) {
	paths := make([]string, maxReportedPaths+5)
	for i := range paths {
		paths[i] = "spec.field" + string(rune('a'+i))
	}
	owner := fieldOwner{Manager: "argocd-controller", Operation: "Apply", Paths: paths}

	got := owner.String()
	// The marker is its own entry in the list, not a tail on the last path: a
	// path can end in a list selector, and "containers[name=pause]..." reads as
	// a badly rendered path rather than as a truncation.
	if !strings.HasSuffix(got, ","+pathOverflowSuffix+"]") {
		t.Errorf("String() = %q, want it to end in a separated overflow marker", got)
	}
	if n := strings.Count(got, ","); n != maxReportedPaths {
		t.Errorf("String() rendered %d separators, want %d (%d paths plus the marker)",
			n, maxReportedPaths, maxReportedPaths)
	}
	if strings.Contains(got, paths[maxReportedPaths]) {
		t.Errorf("String() = %q, want the path past the cap omitted", got)
	}
}

func TestFieldOwnerStringNamesTheSubresource(t *testing.T) {
	owner := fieldOwner{Manager: "kubelet", Operation: "Update", Subresource: "status", Paths: []string{"status.phase"}}
	// A manager writing the object and the same manager writing its status are
	// two entries; the rendering has to tell them apart.
	if got, want := owner.String(), "kubelet.status(Update)=[status.phase]"; got != want {
		t.Errorf("String() = %q, want %q", got, want)
	}
}
