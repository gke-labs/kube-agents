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
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ManagerCacheOptions is the cache the manager is built with. It is a
// function rather than a literal in main.go so that what the operator holds
// in memory has somewhere to be documented and somewhere to be tested
// (manager_cache_test.go); that main.go passes it at all is pinned
// separately, in cmd's manager_wiring_test.go.
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
// cache entirely — Pods through ManagerClientOptions below, Secrets and the
// A2A bookkeeping objects through APIReader — and to hold less of what
// remains, below.
func ManagerCacheOptions() cache.Options {
	return cache.Options{
		// managedFields is the largest part of most cached objects and
		// nothing in this operator reads it: the one consumer of a
		// managedFields ownership claim is the drift detector
		// (cmd/drift-detector), a separate process with its own live client.
		// Stripping it on the way into the store is upstream's own
		// suggestion for this, and it is safe for the read-modify-write
		// paths here because ObjectMeta.ManagedFields is omitempty: a nil
		// slice is left out of the request body, and the server keeps its own
		// entries for a field the request does not carry. (It keeps them for
		// an explicit empty list too — clearing the field takes a list holding
		// one empty entry — so neither shape resets ownership from here.)
		// Annotations are left as they are for the opposite reason —
		// kubectl.kubernetes.io/last-applied-configuration is the other bulk
		// field, but an Update built from a cached copy without it would
		// delete it from the live object.
		DefaultTransform: cache.TransformStripManagedFields(),
	}
}

// ManagerClientOptions is the client the manager hands its reconcilers, and
// it is where "this operator does not cache Pods" stops being a convention.
//
// Nothing watches Pods, so the informer exists only if something reads one
// through the cache, and the four reads that do are written to go live
// (podReader, UsageCounterPoller.reader) with a test apiece. That pins the
// four; it does not pin the fifth. DisableFor makes the delegating client
// answer every Pod read from the API server whoever writes it, so a reader
// added later cannot start a cluster-wide Pod informer by accident, and
// podReader's fallback to the cached client — the shape tests supply — is
// live in this process too.
//
// It does not make the informer impossible: a Watches() or Owns() on Pods
// bypasses the client entirely and would start one. Nothing here does that,
// and the reason not to is in podReader's comment.
func ManagerClientOptions() client.Options {
	return client.Options{
		Cache: &client.CacheOptions{
			DisableFor: []client.Object{&corev1.Pod{}},
		},
	}
}
