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
	"sigs.k8s.io/controller-runtime/pkg/cache"
)

// ManagerCacheOptions is the cache the manager is built with. It is a
// function rather than a literal in main.go so that what the operator holds
// in memory is pinned by a test (manager_cache_test.go) rather than by
// reading the wiring.
//
// Every informer this manager starts is cluster-wide, and that is a
// deliberate reading of the alternatives rather than an oversight (#3493):
//
//   - Scoping by namespace (cache.Options.DefaultNamespaces) is fixed at
//     process start, and a PlatformAgent is a namespaced CR an operator may
//     apply anywhere. The list would have to be configured, and an agent in a
//     namespace nobody added to it would never reconcile at all.
//   - Scoping by label (ByObject selectors) is worse than it looks here,
//     because two paths exist to notice an object the operator did NOT
//     create: the RoleBinding prune, which lists every RoleBinding in the
//     agent's namespace, and agentProfileServiceAccountIsForeign, which has
//     to see a foreign ServiceAccount under the operator's name. A selector
//     hides exactly those objects, and both paths then read "absent" and act
//     on it.
//
// What is left is to keep the kinds that grow with the cluster out of the
// cache entirely — Pods through podReader, Secrets and the A2A bookkeeping
// objects through APIReader — and to hold less of what remains, below.
func ManagerCacheOptions() cache.Options {
	return cache.Options{
		// managedFields is the largest part of most cached objects and
		// nothing in this operator reads it: the one consumer of a
		// managedFields ownership claim is the drift detector
		// (cmd/drift-detector), a separate process with its own live client.
		// Stripping it on the way into the store is upstream's own
		// suggestion for this, and it is safe for the read-modify-write
		// paths here because ObjectMeta.ManagedFields is omitempty: a nil
		// slice is left out of the request body, and an absent field leaves
		// the server's own entries alone (an explicit empty list would reset
		// them). Annotations are left as they are for the opposite reason —
		// kubectl.kubernetes.io/last-applied-configuration is the other bulk
		// field, but an Update built from a cached copy without it would
		// delete it from the live object.
		DefaultTransform: cache.TransformStripManagedFields(),
	}
}
