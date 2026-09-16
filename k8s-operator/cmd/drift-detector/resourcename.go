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
	"fmt"
	"strings"
)

const (
	// coreGroupAlias is what Cloud Audit Logs calls the Kubernetes core API
	// group in resourceName. The Kubernetes API itself calls it "", and that
	// is what a dynamic client wants, so the alias is translated away here
	// rather than at every call site in T3.
	coreGroupAlias = "core"

	// namespacesSegment marks namespace scope in a resourceName path. It is
	// also a resource in its own right -- "core/v1/namespaces/foo" is the
	// namespace object, not an object inside foo -- which is the ambiguity
	// parseResourceName exists to resolve.
	namespacesSegment = "namespaces"

	// minResourceNameSegments is the shortest path that names anything: a
	// group, a version, and a resource. Create calls stop there, because the
	// API server has not assigned a name at the point the call is audited.
	minResourceNameSegments = 3

	// pathSeparator separates resourceName segments.
	pathSeparator = "/"

	// maxRenderedRefParts is how many segments String can emit: namespace,
	// resource, name, subresource.
	maxRenderedRefParts = 4
)

// namespaceSubresources are the subresources the namespace object itself has.
//
// They are enumerated because "core/v1/namespaces/foo/status" and
// "core/v1/namespaces/foo/pods" have the same shape and mean different things:
// the first is a subresource of namespace foo, the second is a create inside
// namespace foo whose name was not yet assigned. Nothing in the path
// distinguishes them, so the trailing segment is matched against the closed set
// of subresources a namespace actually has.
//
// This is the one rule in this file not read off a specification, and it is the
// first thing to check against live fixtures in T2. A miss parses a create as a
// subresource of a namespace, which T3 would then fail to look up.
var namespaceSubresources = map[string]bool{
	"status":   true,
	"finalize": true,
}

// errNoResourceName reports an audit record that named no object. Some
// Kubernetes calls legitimately have no resourceName (subject access reviews,
// for one), so this is a skip rather than a parse failure.
var errNoResourceName = errors.New("audit record carries no resourceName")

// ResourceRef is a Kubernetes object identity decomposed from an audit record's
// resourceName, in the form T3's live-object lookup needs.
type ResourceRef struct {
	// Group is the API group, empty for the core group. See coreGroupAlias.
	Group string

	// Version is the API version the call was made against.
	Version string

	// Namespace is empty for cluster-scoped objects.
	Namespace string

	// Resource is the plural, lowercase API resource -- "deployments", not
	// "Deployment". It is deliberately not converted to a kind: the audit log
	// gives the resource, and the resource is what a dynamic client indexes by,
	// so converting would mean a RESTMapper lookup here and the reverse lookup
	// again in T3.
	Resource string

	// Name is empty on a create, whose name the API server assigns after the
	// call is audited.
	Name string

	// Subresource is "status", "scale", and so on; empty for the object itself.
	Subresource string
}

// String renders the reference the way a human reading the structured log
// would write it, for the log's human-facing field.
func (r ResourceRef) String() string {
	parts := make([]string, 0, maxRenderedRefParts)
	if r.Namespace != "" {
		parts = append(parts, r.Namespace)
	}
	parts = append(parts, r.Resource)
	if r.Name != "" {
		parts = append(parts, r.Name)
	}
	if r.Subresource != "" {
		parts = append(parts, r.Subresource)
	}
	return strings.Join(parts, pathSeparator)
}

// parseResourceName decomposes a Cloud Audit Logs resourceName into the
// components T3 needs to fetch the live object.
//
// The grammar is <group>/<version>[/namespaces/<namespace>]/<resource>[/<name>[/<subresource>]],
// with two shapes that do not read off it directly: the namespace object
// itself, handled via namespaceSubresources, and a create, which stops before
// the name.
func parseResourceName(resourceName string) (ResourceRef, error) {
	if resourceName == "" {
		return ResourceRef{}, errNoResourceName
	}

	segments := strings.Split(resourceName, pathSeparator)
	if len(segments) < minResourceNameSegments {
		return ResourceRef{}, fmt.Errorf("resourceName %q: got %d segments, want at least %d", resourceName, len(segments), minResourceNameSegments)
	}

	ref := ResourceRef{Group: segments[0], Version: segments[1]}
	if ref.Group == coreGroupAlias {
		ref.Group = ""
	}

	// Everything after the group and version. Consuming a namespace scope off
	// the front, where there is one, leaves <resource>[/<name>[/<subresource>]].
	tail := segments[2:]
	if tail[0] == namespacesSegment && len(tail) > 2 {
		ref.Namespace = tail[1]
		tail = tail[2:]

		if len(tail) == 1 && namespaceSubresources[tail[0]] {
			// What was taken for the scope is the namespace object itself:
			// "core/v1/namespaces/foo/status". Put it back.
			return ResourceRef{
				Group:       ref.Group,
				Version:     ref.Version,
				Resource:    namespacesSegment,
				Name:        ref.Namespace,
				Subresource: tail[0],
			}, nil
		}
	}

	switch len(tail) {
	case 1:
		ref.Resource = tail[0]
	case 2:
		ref.Resource, ref.Name = tail[0], tail[1]
	case 3:
		ref.Resource, ref.Name, ref.Subresource = tail[0], tail[1], tail[2]
	default:
		return ResourceRef{}, fmt.Errorf("resourceName %q: %d segments after group/version, want 1 to 3", resourceName, len(tail))
	}

	return ref, nil
}
