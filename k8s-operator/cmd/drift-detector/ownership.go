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
	"fmt"
	"sort"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

const (
	// fieldPrefix marks a named field in the FieldsV1 encoding: {"f:spec":
	// {"f:replicas":{}}} is spec.replicas. Every key in the tree carries one of
	// the four prefixes here, so a key without any of them is a shape this
	// parser does not know rather than a field literally called "spec".
	fieldPrefix = "f:"

	// keyPrefix, valuePrefix and indexPrefix mark the three ways FieldsV1
	// addresses one entry of a list: by its merge key ({"k:{\"name\":\"app\"}"}),
	// by its literal value for a list of scalars, and by position for an atomic
	// list. They are rendered into the path rather than skipped, because
	// "spec.containers[name=app].image" says which container drifted and
	// "spec.containers.image" does not.
	keyPrefix   = "k:"
	valuePrefix = "v:"
	indexPrefix = "i:"

	// selfMarker is the FieldsV1 key for "the containing field itself is
	// owned", as opposed to something beneath it. It appears as a sibling of
	// the children and is not a path component of its own.
	selfMarker = "."

	// pathJoiner separates the components of a rendered field path.
	pathJoiner = "."

	// listKeyOpen and listKeyClose wrap a list entry's selector in a rendered
	// path, giving "containers[name=app]" rather than a bare JSON blob.
	listKeyOpen  = "["
	listKeyClose = "]"

	// listKeyPairJoiner separates the parts of a compound merge key, which is
	// rare but legal: a list keyed on both port and protocol renders as
	// "ports[port=443,protocol=TCP]".
	listKeyPairJoiner = ","

	// reportedPathSeparator separates the field paths in a rendered claim. It
	// is the same character as listKeyPairJoiner and a different thing: one
	// separates paths in a list, the other separates key-value pairs inside a
	// single path, and a change to either is not a change to the other.
	reportedPathSeparator = ","

	// listKeyAssign separates a merge key's field from its value.
	listKeyAssign = "="

	// subresourceSeparator separates a manager's name from the subresource it
	// wrote through, giving "kubelet.status". Declared rather than reusing
	// pathJoiner, which it happens to equal: that one separates components of a
	// field path, and a change to how field paths render is not a change to how
	// a manager is named.
	subresourceSeparator = "."

	// maxReportedPaths caps how many field paths one manager contributes to a
	// log line. A manager that owns a whole object owns hundreds of leaves --
	// the GitOps controller of a Deployment routinely owns more than 200 -- and
	// a drift line carrying all of them is unreadable and expensive to emit at
	// the volumes in the README. The cap is on the rendered output only; the
	// ownership decision reads every path.
	maxReportedPaths = 12

	// pathOverflowSuffix is appended when maxReportedPaths truncates, so a
	// truncated list is never mistaken for a complete one.
	pathOverflowSuffix = "..."
)

// fieldOwner is one manager's claim on an object: who they are, when they last
// wrote, and which field paths they own.
//
// This is metav1.ManagedFieldsEntry with the FieldsV1 blob decoded into paths,
// which is the only form in which it can be compared to anything or printed.
type fieldOwner struct {
	// Manager is the field manager name the client sent -- "kubectl-edit",
	// "argocd-controller". It is self-declared and unverified, exactly like the
	// user agent in an audit record, so it identifies a tool and never a person.
	Manager string

	// Operation is "Apply" (server-side apply) or "Update" (everything else).
	// A client doing Update rather than Apply still gets an entry, but the
	// ownership is coarser, which is the caveat the design doc records about
	// attribution quality outside Server-Side Apply.
	Operation string

	// Subresource is set when the claim came through one -- a write to
	// "status" is a separate entry from a write to the object. Empty for the
	// object itself.
	Subresource string

	// UpdatedAt is when this manager last wrote. Zero when the API server did
	// not record one, which is legal: the field is a pointer upstream.
	UpdatedAt time.Time

	// Paths are the dotted field paths this manager owns, sorted. Empty when
	// the entry carried no FieldsV1 blob or one this parser could not read --
	// see ownership, which keeps such an entry rather than dropping it.
	Paths []string
}

// String renders the claim for a log line, truncating the path list at
// maxReportedPaths.
func (o fieldOwner) String() string {
	paths := o.Paths
	suffix := ""
	if len(paths) > maxReportedPaths {
		paths = paths[:maxReportedPaths]
		suffix = pathOverflowSuffix
	}
	name := o.Manager
	if o.Subresource != "" {
		name += subresourceSeparator + o.Subresource
	}
	return fmt.Sprintf("%s(%s)=[%s%s]", name, o.Operation, strings.Join(paths, reportedPathSeparator), suffix)
}

// ownership decomposes an object's metadata.managedFields into one fieldOwner
// per entry, in the order the API server returned them.
//
// An entry whose FieldsV1 blob is absent is kept with no paths rather than
// dropped. Dropping it would make an object look unowned, and "nobody owns this
// field" is the answer that makes a human change look like drift nobody has
// reconciled -- the false positive this whole join exists to avoid. Kept, it
// shows up in the log as a manager with an empty path list, which is visibly a
// gap in the data rather than an absence of claims.
//
// A blob that is present and does not decode is handled the same way, but that
// is defence rather than a path anything reaches today: the client converts the
// API server's response through unstructured, whose managedFields conversion
// drops an entry it cannot decode before this function is called. fieldPaths
// still returns nil for one, so if that conversion ever changes the entry
// survives here instead of disappearing.
func ownership(obj *unstructured.Unstructured) []fieldOwner {
	entries := obj.GetManagedFields()
	if len(entries) == 0 {
		return nil
	}

	owners := make([]fieldOwner, 0, len(entries))
	for _, entry := range entries {
		owner := fieldOwner{
			Manager:     entry.Manager,
			Operation:   string(entry.Operation),
			Subresource: entry.Subresource,
		}
		if entry.Time != nil {
			owner.UpdatedAt = entry.Time.Time
		}
		if entry.FieldsV1 != nil {
			owner.Paths = fieldPaths(entry.FieldsV1.Raw)
		}
		owners = append(owners, owner)
	}
	return owners
}

// fieldPaths flattens a FieldsV1 blob into sorted dotted paths.
//
// Returns nil for a blob that does not decode. That is indistinguishable here
// from an entry that owns nothing, which is why ownership keeps the entry
// either way: the caller sees a manager with no paths and can tell that from
// the object having no managers at all.
func fieldPaths(raw []byte) []string {
	if len(raw) == 0 {
		return nil
	}
	var tree map[string]any
	if err := json.Unmarshal(raw, &tree); err != nil {
		return nil
	}

	var paths []string
	walkFields(tree, "", &paths)
	sort.Strings(paths)
	return paths
}

// walkFields recurses the FieldsV1 tree, appending a path for every leaf.
//
// A leaf is an empty object: {"f:replicas":{}} means replicas is owned and has
// nothing beneath it. A non-empty object is a branch, and its own path is
// emitted only if it carries the selfMarker, because owning "spec.template" as
// a whole and owning one field inside it are different claims.
func walkFields(tree map[string]any, prefix string, out *[]string) {
	for key, child := range tree {
		if key == selfMarker {
			// The containing field itself, not a child of it. At the root this
			// would be the whole object, which no manager claims in practice;
			// emitting the empty prefix would render as "".
			if prefix != "" {
				*out = append(*out, prefix)
			}
			continue
		}

		path := joinPath(prefix, renderKey(key))
		nested, ok := child.(map[string]any)
		if !ok || len(nested) == 0 {
			// A leaf. The !ok arm covers a value that is not an object at all,
			// which the encoding does not produce but a hand-written or
			// truncated blob can; recording the path is more useful than
			// silently skipping it.
			*out = append(*out, path)
			continue
		}
		walkFields(nested, path, out)
	}
}

// renderKey turns one FieldsV1 key into its path component.
//
// An unrecognised prefix is returned whole rather than stripped or dropped. A
// new addressing form on Kubernetes' side then shows up in the log looking
// wrong, which is a bug report; stripping it would silently produce a path that
// reads as valid and is not.
func renderKey(key string) string {
	switch {
	case strings.HasPrefix(key, fieldPrefix):
		return strings.TrimPrefix(key, fieldPrefix)
	case strings.HasPrefix(key, keyPrefix):
		return renderListKey(strings.TrimPrefix(key, keyPrefix))
	case strings.HasPrefix(key, valuePrefix):
		return listKeyOpen + strings.TrimPrefix(key, valuePrefix) + listKeyClose
	case strings.HasPrefix(key, indexPrefix):
		return listKeyOpen + strings.TrimPrefix(key, indexPrefix) + listKeyClose
	default:
		return key
	}
}

// renderListKey turns a merge-key selector, which is a JSON object, into
// "[name=app]". The raw JSON is returned when it does not decode, for the
// reason renderKey returns an unknown prefix whole.
func renderListKey(raw string) string {
	var fields map[string]any
	if err := json.Unmarshal([]byte(raw), &fields); err != nil {
		return listKeyOpen + raw + listKeyClose
	}

	// Sorted, because Go randomises map iteration and an unsorted compound key
	// would render differently on each run -- which would make two log lines
	// about the same field look like two different fields.
	names := make([]string, 0, len(fields))
	for name := range fields {
		names = append(names, name)
	}
	sort.Strings(names)

	pairs := make([]string, 0, len(names))
	for _, name := range names {
		pairs = append(pairs, fmt.Sprintf("%s%s%v", name, listKeyAssign, fields[name]))
	}
	return listKeyOpen + strings.Join(pairs, listKeyPairJoiner) + listKeyClose
}

// joinPath appends a component to a dotted path, without a leading separator
// at the root.
//
// A list selector attaches to the field it indexes rather than hanging off it:
// "spec.containers[name=app]", not "spec.containers.[name=app]". The three
// list-addressing forms all render starting with listKeyOpen, which is what
// distinguishes them from a named field here.
func joinPath(prefix, component string) string {
	if prefix == "" {
		return component
	}
	if strings.HasPrefix(component, listKeyOpen) {
		return prefix + component
	}
	return prefix + pathJoiner + component
}
