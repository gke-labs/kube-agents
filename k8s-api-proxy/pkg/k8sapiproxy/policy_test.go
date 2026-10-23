package k8sapiproxy

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNormalizeAndExtractCluster(t *testing.T) {
	tests := []struct {
		name             string
		rawPath          string
		urlPath          string
		defaultClusterID string
		wantCluster      string
		wantPath         string
		wantErr          bool
	}{
		{
			name:             "default cluster root path",
			urlPath:          "/api/v1/pods",
			defaultClusterID: "gke_my-proj_us-central1_host-cluster",
			wantCluster:      "gke_my-proj_us-central1_host-cluster",
			wantPath:         "/api/v1/pods",
		},
		{
			name:             "explicit gke cluster prefix",
			urlPath:          "/clusters/gke_my-proj_us-central1_fleet-01/api/v1/namespaces/default/pods",
			defaultClusterID: "in-cluster",
			wantCluster:      "gke_my-proj_us-central1_fleet-01",
			wantPath:         "/api/v1/namespaces/default/pods",
		},
		{
			name:             "explicit in-cluster prefix",
			urlPath:          "/clusters/in-cluster/apis/apps/v1/deployments",
			defaultClusterID: "in-cluster",
			wantCluster:      "in-cluster",
			wantPath:         "/apis/apps/v1/deployments",
		},
		{
			name:        "reject percent-encoded slash",
			rawPath:     "/api/v1/namespaces/default%2fsecrets/pods",
			urlPath:     "/api/v1/namespaces/default/secrets/pods",
			wantErr:     true,
		},
		{
			name:        "reject dot-dot traversal",
			urlPath:     "/api/v1/namespaces/default/pods/../../secrets",
			wantErr:     true,
		},
		{
			name:        "reject single dot segment",
			urlPath:     "/api/v1/./pods",
			wantErr:     true,
		},
		{
			name:        "reject double slash",
			urlPath:     "//api/v1/pods",
			wantErr:     true,
		},
		{
			name:        "reject invalid cluster ID",
			urlPath:     "/clusters/bad$cluster/api/v1/pods",
			wantErr:     true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			cluster, cleanPath, err := NormalizeAndExtractCluster(tc.rawPath, tc.urlPath, tc.defaultClusterID)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil (cluster=%q, path=%q)", cluster, cleanPath)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if cluster != tc.wantCluster {
				t.Errorf("cluster = %q, want %q", cluster, tc.wantCluster)
			}
			if cleanPath != tc.wantPath {
				t.Errorf("cleanPath = %q, want %q", cleanPath, tc.wantPath)
			}
		})
	}
}

func TestEvaluatePolicyDifferentialCorpus(t *testing.T) {
	factory := NewRequestInfoFactory()

	tests := []struct {
		name    string
		mode    Mode
		method  string
		target  string
		headers map[string]string
		wantOK  bool
	}{
		{
			name:   "discovery /api allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api",
			wantOK: true,
		},
		{
			name:   "discovery /api/v1 allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1",
			wantOK: true,
		},
		{
			name:   "discovery /apis/apps allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/apis/apps",
			wantOK: true,
		},
		{
			name:   "discovery /apis/apps/v1 allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/apis/apps/v1",
			wantOK: true,
		},
		{
			name:   "discovery /openapi/v3/apis/apps/v1 allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/openapi/v3/apis/apps/v1",
			wantOK: true,
		},
		{
			name:   "non-resource /metrics blocked",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/metrics",
			wantOK: false,
		},
		{
			name:   "non-resource /debug/pprof blocked",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/debug/pprof",
			wantOK: false,
		},
		{
			name:   "list pods allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/pods",
			wantOK: true,
		},
		{
			name:   "watch events allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/events?watch=true",
			wantOK: true,
		},
		{
			name:   "get pod logs allowed",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/pods/my-pod/log?follow=true",
			wantOK: true,
		},
		{
			name:   "pod exec GET blocked",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/pods/my-pod/exec?command=sh",
			wantOK: false,
		},
		{
			name:   "pod exec POST in read-write mode still blocked",
			mode:   ModeReadWrite,
			method: http.MethodPost,
			target: "/api/v1/namespaces/default/pods/my-pod/exec?command=sh",
			wantOK: false,
		},
		{
			name:   "pod portforward blocked in all modes",
			mode:   ModeReadWrite,
			method: http.MethodPost,
			target: "/api/v1/namespaces/default/pods/my-pod/portforward",
			wantOK: false,
		},
		{
			name:   "serviceaccount token minting blocked in all modes",
			mode:   ModeReadWrite,
			method: http.MethodPost,
			target: "/api/v1/namespaces/default/serviceaccounts/default/token",
			wantOK: false,
		},
		{
			name:   "certificatesigningrequest approval blocked in all modes",
			mode:   ModeReadWrite,
			method: http.MethodPut,
			target: "/apis/certificates.k8s.io/v1/certificatesigningrequests/my-csr/approval",
			wantOK: false,
		},
		{
			name:   "pod status patch without dryRun blocked in read-only-dry-run",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodPatch,
			target: "/api/v1/namespaces/default/pods/my-pod/status",
			wantOK: false,
		},
		{
			name:   "node status patch without dryRun blocked in read-only-dry-run",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodPatch,
			target: "/api/v1/nodes/node-1/status",
			wantOK: false,
		},
		{
			name:   "watch secrets allowed at policy layer (rewritten to PartialObjectMetadata by handler)",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/secrets?watch=true",
			wantOK: true,
		},
		{
			name:   "tokenreviews blocked in all modes",
			mode:   ModeReadWrite,
			method: http.MethodPost,
			target: "/apis/authentication.k8s.io/v1/tokenreviews",
			wantOK: false,
		},
		{
			name:   "selfsubjectaccessreviews blocked in all modes",
			mode:   ModeReadWrite,
			method: http.MethodPost,
			target: "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
			wantOK: false,
		},
		{
			name:   "create deployment without dryRun blocked in read-only-dry-run",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodPost,
			target: "/apis/apps/v1/namespaces/default/deployments",
			wantOK: false,
		},
		{
			name:   "create deployment with dryRun=All allowed in read-only-dry-run",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodPost,
			target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All&fieldManager=kubectl",
			wantOK: true,
		},
		{
			name:   "patch deployment with dryRun=All blocked in strict read-only",
			mode:   ModeReadOnly,
			method: http.MethodPatch,
			target: "/apis/apps/v1/namespaces/default/deployments/web?dryRun=All",
			wantOK: false,
		},
		{
			name:   "duplicate dryRun query parameter blocked",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodPost,
			target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All&dryRun=",
			wantOK: false,
		},
		{
			name:   "delete pod with dryRun=All blocked in read-only-dry-run",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodDelete,
			target: "/api/v1/namespaces/default/pods/my-pod?dryRun=All",
			wantOK: false,
		},
		{
			name:   "delete pod allowed in read-write mode",
			mode:   ModeReadWrite,
			method: http.MethodDelete,
			target: "/api/v1/namespaces/default/pods/my-pod",
			wantOK: true,
		},
		{
			name:   "protocol upgrade header blocked",
			mode:   ModeReadOnlyWithDryRun,
			method: http.MethodGet,
			target: "/api/v1/namespaces/default/pods",
			headers: map[string]string{
				"Connection": "Upgrade",
				"Upgrade":    "websocket",
			},
			wantOK: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(tc.method, tc.target, nil)
			for k, v := range tc.headers {
				req.Header.Set(k, v)
			}
			info, err := factory.NewRequestInfo(req)
			if err != nil {
				t.Fatalf("NewRequestInfo(%q) failed: %v", tc.target, err)
			}
			policyErr := EvaluatePolicy(tc.mode, true, req, info)
			if tc.wantOK && policyErr != nil {
				t.Errorf("expected allowed, got error: %v", policyErr)
			}
			if !tc.wantOK && policyErr == nil {
				t.Errorf("expected blocked, got allowed (info=%+v)", info)
			}
		})
	}

	t.Run("permission-boundary refusal message matches permission_refusal_stop.py markers", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodDelete, "/api/v1/namespaces/default/pods/my-pod", nil)
		info, _ := factory.NewRequestInfo(req)
		policyErr := EvaluatePolicy(ModeReadOnlyWithDryRun, true, req, info)
		if policyErr == nil {
			t.Fatalf("expected delete to be refused")
		}
		for _, wantSubstr := range []string{"refused however it is attempted", "\npolicy rule: kubernetes.read-only", "\npolicy verb: delete"} {
			if !strings.Contains(policyErr.Message, wantSubstr) {
				t.Errorf("refusal message %q missing required marker %q", policyErr.Message, wantSubstr)
			}
		}
	})

	t.Run("AllowUpgradeSubresources permits pod exec in ModeReadWrite with protocol upgrade", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/my-pod/exec?command=sh", nil)
		req.Header.Set("Connection", "Upgrade")
		req.Header.Set("Upgrade", "SPDY/3.1")
		info, _ := factory.NewRequestInfo(req)
		if err := EvaluatePolicyWithOptions(ModeReadWrite, true, false, req, info); err == nil {
			t.Errorf("expected pod exec to be blocked when allowUpgradeSubresources=false")
		}
		if err := EvaluatePolicyWithOptions(ModeReadWrite, true, true, req, info); err != nil {
			t.Errorf("expected pod exec to be allowed when allowUpgradeSubresources=true in ModeReadWrite, got: %v", err)
		}
		if err := EvaluatePolicyWithOptions(ModeReadOnlyWithDryRun, true, true, req, info); err == nil {
			t.Errorf("expected pod exec to remain blocked in ModeReadOnlyWithDryRun even when allowUpgradeSubresources=true")
		}
	})
}
