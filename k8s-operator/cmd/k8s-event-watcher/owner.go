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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

const (
	// podMetadataAccept asks for one Pod's ObjectMeta as JSON, never its spec
	// or status. The broker's watcher route admits a named Pod GET only under
	// this Accept (watcher_policy.evaluate_k8s), and relays only the fields
	// read here (watcher_policy.project_pod_metadata): the controller
	// ownerReference and, for a ReplicaSet, the pod-template-hash label. The
	// in-cluster client reads the ObjectMeta whole, and this file reads the
	// same fields of it.
	podMetadataAccept = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
	// ownerLookupTimeout bounds the one GET an admitted Pod event costs. The
	// lookup runs inline before the inject, so a slow API server delays one
	// incident by at most this; a lookup that gives up sends the event with no
	// controller_ref, and session_kv folds it by name as before.
	ownerLookupTimeout = 3 * time.Second
	// ownerCacheTTL and ownerCacheMax bound the per-Pod memo. A Pod's
	// controller does not change for the Pod's life, so the TTL only ages out
	// Pods that are gone; the cap keeps a churning cluster from growing it.
	ownerCacheTTL = time.Hour
	ownerCacheMax = 4096
	// podTemplateHashLabel is the label a Deployment's ReplicaSet stamps on its
	// Pods; the ReplicaSet is named "<deployment>-<hash>", so the Deployment
	// is the ReplicaSet's name less that suffix, with no ReplicaSet read.
	podTemplateHashLabel = "pod-template-hash"
	kindReplicaSet       = "ReplicaSet"
	kindDeployment       = "Deployment"
	// ownerFailureBackoff is how long a cluster's lookups are skipped after
	// one fails. A cluster whose Pod reads time out would otherwise hold
	// every admitted Pod event there for ownerLookupTimeout in turn, and a
	// broker or identity that refuses them would log one line per event.
	// A 404 is not a failure: the Pod is gone, which says nothing of the
	// cluster's other Pods.
	ownerFailureBackoff = time.Minute
)

// foldableControllerKinds are the controllers whose Pods are one workload.
// Any other controller reference — a static Pod's Node above all, which
// would put every static Pod on a node into one incident — is read as none.
var foldableControllerKinds = map[string]bool{
	kindReplicaSet: true,
	"StatefulSet":  true,
	"DaemonSet":    true,
	"Job":          true,
}

// errNoRESTClient is the lookup's answer on a client that cannot make a raw
// request.
var errNoRESTClient = errors.New("the cluster client has no REST transport")

// ownerResolver fills a Pod event's controller_ref: "<Kind>/<name>" of the
// workload that owns the Pod, in the Pod's namespace, or "" when the Pod has
// no controller it folds on, is gone, or the lookup failed. session_kv folds the Pods of one
// workload into one incident on it (_fold_key). One per cluster, beside that
// cluster's dispatcher, reading through the cluster's own client.
type ownerResolver struct {
	client  kubernetes.Interface
	timeout time.Duration
	now     func() time.Time

	mu sync.Mutex
	// cache is keyed on namespace, name and UID, the Pod the read was for:
	// an event's UID alone is the event's word, and a UID named in one
	// namespace must not set the answer for a Pod in another.
	cache boundedEntries[string]
	// skipUntil is when lookups resume after a failure (ownerFailureBackoff).
	skipUntil time.Time
}

func newOwnerResolver(client kubernetes.Interface) *ownerResolver {
	return &ownerResolver{
		client:  client,
		timeout: ownerLookupTimeout,
		now:     time.Now,
		cache:   newBoundedEntries[string](ownerCacheTTL, ownerCacheMax),
	}
}

// podMeta is the part of a PartialObjectMetadata the resolver reads.
type podMeta struct {
	Metadata struct {
		Labels          map[string]string `json:"labels"`
		OwnerReferences []struct {
			Kind       string `json:"kind"`
			Name       string `json:"name"`
			Controller *bool  `json:"controller"`
		} `json:"ownerReferences"`
	} `json:"metadata"`
}

// ControllerRef returns the Pod's controller as "<Kind>/<name>", or "".
// A lookup that fails is not remembered, so the next event for the Pod asks
// again; one that answers, including "no controller", is.
func (o *ownerResolver) ControllerRef(ctx context.Context, namespace, name, uid string) string {
	if o == nil || o.client == nil || namespace == "" || name == "" {
		return ""
	}
	now := o.now()
	key := namespace + "/" + name + "/" + uid
	o.mu.Lock()
	ref, _, cached := o.cache.lookup(key, now)
	skipping := now.Before(o.skipUntil)
	o.mu.Unlock()
	if uid != "" && cached {
		return ref
	}
	if skipping {
		return ""
	}
	ref, err := o.lookup(ctx, namespace, name)
	if apierrors.IsNotFound(err) {
		// The Pod was deleted before its event was dispatched: no controller
		// to read, and nothing wrong with the cluster's reads.
		return ""
	}
	if err != nil {
		o.mu.Lock()
		o.skipUntil = now.Add(ownerFailureBackoff)
		o.mu.Unlock()
		log.Printf("owner lookup for pod %s/%s: %v; sending events without controller_ref for %s", namespace, name, err, ownerFailureBackoff)
		return ""
	}
	if uid != "" {
		o.mu.Lock()
		o.cache.store(key, ref, now, now)
		o.mu.Unlock()
	}
	return ref
}

func (o *ownerResolver) lookup(ctx context.Context, namespace, name string) (string, error) {
	// A client with no REST transport (client-go's fake clientset returns a
	// typed nil) cannot read; it answers like a failed lookup, not a panic.
	rc := o.client.CoreV1().RESTClient()
	if typed, ok := rc.(*rest.RESTClient); rc == nil || (ok && typed == nil) {
		return "", errNoRESTClient
	}
	cctx, cancel := context.WithTimeout(ctx, o.timeout)
	defer cancel()
	raw, err := rc.Get().
		AbsPath("/api/v1/namespaces", namespace, "pods", name).
		SetHeader(acceptHeader, podMetadataAccept).
		DoRaw(cctx)
	if err != nil {
		return "", err
	}
	var meta podMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return "", fmt.Errorf("decode: %w", err)
	}
	for _, ref := range meta.Metadata.OwnerReferences {
		if ref.Controller == nil || !*ref.Controller || ref.Kind == "" || ref.Name == "" {
			continue
		}
		if !foldableControllerKinds[ref.Kind] {
			return "", nil
		}
		if ref.Kind == kindReplicaSet {
			// Only a Deployment's ReplicaSet folds: one with no matching
			// pod-template-hash (a bare ReplicaSet, or another controller's,
			// such as an Argo Rollout's, which labels its own hash) reads as
			// none, so its Pods fold by name as they did before.
			hash := meta.Metadata.Labels[podTemplateHashLabel]
			if hash == "" || !strings.HasSuffix(ref.Name, "-"+hash) {
				return "", nil
			}
			return kindDeployment + "/" + strings.TrimSuffix(ref.Name, "-"+hash), nil
		}
		return ref.Kind + "/" + ref.Name, nil
	}
	return "", nil
}
