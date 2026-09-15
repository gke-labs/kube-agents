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
)

func TestParseResourceName(t *testing.T) {
	tests := []struct {
		name         string
		resourceName string
		want         ResourceRef
	}{
		{
			name:         "namespaced core resource",
			resourceName: "core/v1/namespaces/default/services/web",
			want:         ResourceRef{Group: "", Version: "v1", Namespace: "default", Resource: "services", Name: "web"},
		},
		{
			name:         "namespaced grouped resource",
			resourceName: "apps/v1/namespaces/prod/deployments/checkout",
			want:         ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "checkout"},
		},
		{
			name:         "namespaced resource subresource",
			resourceName: "apps/v1/namespaces/prod/deployments/checkout/scale",
			want:         ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "checkout", Subresource: "scale"},
		},
		{
			name:         "cluster scoped core resource",
			resourceName: "core/v1/nodes/gke-pool-1-abc",
			want:         ResourceRef{Group: "", Version: "v1", Resource: "nodes", Name: "gke-pool-1-abc"},
		},
		{
			name:         "cluster scoped grouped resource",
			resourceName: "rbac.authorization.k8s.io/v1/clusterroles/cluster-admin",
			want:         ResourceRef{Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "clusterroles", Name: "cluster-admin"},
		},
		{
			name:         "cluster scoped subresource",
			resourceName: "core/v1/nodes/gke-pool-1-abc/status",
			want:         ResourceRef{Group: "", Version: "v1", Resource: "nodes", Name: "gke-pool-1-abc", Subresource: "status"},
		},
		{
			// The namespace object itself, not an object inside it.
			name:         "namespace object",
			resourceName: "core/v1/namespaces/payments",
			want:         ResourceRef{Group: "", Version: "v1", Resource: "namespaces", Name: "payments"},
		},
		{
			// Same shape as a namespaced create; told apart by the closed set
			// in namespaceSubresources.
			name:         "namespace object subresource",
			resourceName: "core/v1/namespaces/payments/finalize",
			want:         ResourceRef{Group: "", Version: "v1", Resource: "namespaces", Name: "payments", Subresource: "finalize"},
		},
		{
			name:         "namespaced create without a name",
			resourceName: "core/v1/namespaces/default/pods",
			want:         ResourceRef{Group: "", Version: "v1", Namespace: "default", Resource: "pods"},
		},
		{
			name:         "cluster scoped create without a name",
			resourceName: "rbac.authorization.k8s.io/v1/clusterrolebindings",
			want:         ResourceRef{Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "clusterrolebindings"},
		},
		{
			// The lease traffic the sink carve-out targets, kept here because
			// a human-made lease write still reaches the detector.
			name:         "coordination lease",
			resourceName: "coordination.k8s.io/v1/namespaces/kube-node-lease/leases/gke-pool-1-abc",
			want:         ResourceRef{Group: "coordination.k8s.io", Version: "v1", Namespace: "kube-node-lease", Resource: "leases", Name: "gke-pool-1-abc"},
		},
		{
			name:         "custom resource",
			resourceName: "networking.gke.io/v1/namespaces/prod/managedcertificates/web-cert",
			want:         ResourceRef{Group: "networking.gke.io", Version: "v1", Namespace: "prod", Resource: "managedcertificates", Name: "web-cert"},
		},
		{
			// A namespace called "namespaces" is legal and would break a parser
			// that matched the segment anywhere rather than at the scope position.
			name:         "resource named like the scope segment",
			resourceName: "core/v1/namespaces/default/namespaces/odd",
			want:         ResourceRef{Group: "", Version: "v1", Namespace: "default", Resource: "namespaces", Name: "odd"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseResourceName(tc.resourceName)
			if err != nil {
				t.Fatalf("parseResourceName(%q) returned error: %v", tc.resourceName, err)
			}
			if got != tc.want {
				t.Errorf("parseResourceName(%q)\n got %+v\nwant %+v", tc.resourceName, got, tc.want)
			}
		})
	}
}

func TestParseResourceNameErrors(t *testing.T) {
	tests := []struct {
		name         string
		resourceName string
		wantErr      error
	}{
		{
			name:         "empty is a skip not a failure",
			resourceName: "",
			wantErr:      errNoResourceName,
		},
		{
			name:         "group and version alone name nothing",
			resourceName: "apps/v1",
		},
		{
			name:         "single segment",
			resourceName: "apps",
		},
		{
			name:         "too many trailing segments",
			resourceName: "apps/v1/namespaces/prod/deployments/checkout/scale/extra",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, err := parseResourceName(tc.resourceName)
			if err == nil {
				t.Fatalf("parseResourceName(%q) succeeded, want error", tc.resourceName)
			}
			if tc.wantErr != nil && !errors.Is(err, tc.wantErr) {
				t.Errorf("parseResourceName(%q) error = %v, want %v", tc.resourceName, err, tc.wantErr)
			}
		})
	}
}

func TestResourceRefString(t *testing.T) {
	tests := []struct {
		name string
		ref  ResourceRef
		want string
	}{
		{
			name: "namespaced object",
			ref:  ResourceRef{Namespace: "prod", Resource: "deployments", Name: "checkout"},
			want: "prod/deployments/checkout",
		},
		{
			name: "cluster scoped object",
			ref:  ResourceRef{Resource: "nodes", Name: "gke-pool-1-abc"},
			want: "nodes/gke-pool-1-abc",
		},
		{
			name: "subresource",
			ref:  ResourceRef{Namespace: "prod", Resource: "deployments", Name: "checkout", Subresource: "scale"},
			want: "prod/deployments/checkout/scale",
		},
		{
			name: "create without a name",
			ref:  ResourceRef{Namespace: "default", Resource: "pods"},
			want: "default/pods",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.ref.String(); got != tc.want {
				t.Errorf("String() = %q, want %q", got, tc.want)
			}
		})
	}
}
