package k8sapiproxy

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

const (
	gkeHost  = "gke_my-proj_us-central1_host-cluster"
	gkeFleet = "gke_my-proj_us-central1-a_fleet-01"
)

func TestParseMode(t *testing.T) {
	for raw, want := range map[string]Mode{
		"":                  ModeReadOnlyWithDryRun,
		"read-only":         ModeReadOnly,
		"read-only-dry-run": ModeReadOnlyWithDryRun,
		"read-write":        ModeReadWrite,
	} {
		got, err := ParseMode(raw)
		if err != nil || got != want {
			t.Errorf("ParseMode(%q) = %q, %v; want %q", raw, got, err, want)
		}
	}
	if _, err := ParseMode("READ-ONLY"); err == nil {
		t.Error("ParseMode accepted a mode in the wrong case")
	}
}

func TestSplitClusterPath(t *testing.T) {
	tests := []struct {
		name        string
		target      string
		wantCluster string
		wantPath    string
		wantErr     bool
		wantMsg     string // a substring of the refusal, so a case can't pass for another rule's reason
	}{
		// No prefix is refused (#4739): kubectl before 1.34 drops the kubeconfig server's
		// /clusters/<ctx> base path on `get --raw`, and a default cluster answered that
		// fleet read with the host's data.
		{name: "no prefix", target: "/api/v1/pods", wantErr: true, wantMsg: "name a cluster"},
		{name: "no prefix, discovery", target: "/apis/metrics.k8s.io/v1beta1/nodes", wantErr: true, wantMsg: "name a cluster"},
		{name: "no prefix, root", target: "/", wantErr: true, wantMsg: "name a cluster"},
		{name: "gke prefix", target: "/clusters/" + gkeFleet + "/api/v1/namespaces/default/pods", wantCluster: gkeFleet, wantPath: "/api/v1/namespaces/default/pods"},
		{name: "in-cluster prefix", target: "/clusters/in-cluster/apis/apps/v1/deployments", wantCluster: InClusterID, wantPath: "/apis/apps/v1/deployments"},
		{name: "domain-scoped gke prefix", target: "/clusters/gke_example.com:proj_us-central1_c/api/v1/pods", wantCluster: "gke_example.com:proj_us-central1_c", wantPath: "/api/v1/pods"},
		{name: "kind prefix", target: "/clusters/kind-dev.1/version", wantCluster: "kind-dev.1", wantPath: "/version"},
		{name: "prefix alone is the root", target: "/clusters/in-cluster", wantCluster: InClusterID, wantPath: "/"},
		{name: "encoded slash", target: "/clusters/in-cluster/api/v1/namespaces/default%2fsecrets/pods", wantErr: true, wantMsg: "percent-encoded"},
		{name: "encoded upper-case slash", target: "/clusters/in-cluster/api/v1/namespaces/default%2Fsecrets/pods", wantErr: true, wantMsg: "percent-encoded"},
		{name: "encoded dots", target: "/clusters/in-cluster/api/v1/namespaces/%2e%2e/secrets", wantErr: true, wantMsg: "percent-encoded"},
		{name: "encoded backslash", target: "/clusters/in-cluster/api/v1/namespaces/a%5cb/pods", wantErr: true, wantMsg: "percent-encoded"},
		{name: "encoded NUL", target: "/clusters/in-cluster/api/v1/namespaces/a%00/pods", wantErr: true, wantMsg: "control characters"},
		{name: "encoded control character", target: "/clusters/in-cluster/api/v1/namespaces/a%0a/pods", wantErr: true, wantMsg: "control characters"},
		{name: "dot-dot", target: "/clusters/in-cluster/api/v1/namespaces/default/pods/../../secrets", wantErr: true, wantMsg: "dot segments"},
		{name: "single dot", target: "/clusters/in-cluster/api/v1/./pods", wantErr: true, wantMsg: "dot segments"},
		{name: "double slash", target: "//clusters/in-cluster/api/v1/pods", wantErr: true, wantMsg: "empty path segments"},
		{name: "inner double slash", target: "/clusters/in-cluster/api/v1//pods", wantErr: true, wantMsg: "empty path segments"},
		{name: "missing cluster ID", target: "/clusters/", wantErr: true, wantMsg: "missing cluster ID"},
		{name: "invalid cluster ID", target: "/clusters/bad$cluster/api/v1/pods", wantErr: true, wantMsg: "invalid cluster ID"},
		{name: "gke ID with too few parts", target: "/clusters/gke_proj_cluster/api", wantErr: true, wantMsg: "invalid cluster ID"},
		{name: "gke project with a dot and no domain", target: "/clusters/gke_example.com_us-central1_c/api", wantErr: true, wantMsg: "invalid cluster ID"},
		{name: "upper-case cluster ID", target: "/clusters/kind-Dev/api", wantErr: true, wantMsg: "invalid cluster ID"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.ParseRequestURI(tc.target)
			if err != nil {
				t.Fatalf("parse %q: %v", tc.target, err)
			}
			cluster, apiPath, perr := SplitClusterPath(u)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("accepted (cluster=%q, path=%q)", cluster, apiPath)
				}
				if perr.Code != http.StatusBadRequest || perr.RejectionReason != ReasonInvalidRoute {
					t.Errorf("refusal = %d %q, want 400 %q", perr.Code, perr.RejectionReason, ReasonInvalidRoute)
				}
				if !strings.Contains(perr.Error(), tc.wantMsg) {
					t.Errorf("refusal %q, want it to say %q", perr.Error(), tc.wantMsg)
				}
				return
			}
			if perr != nil {
				t.Fatalf("refused: %v", perr)
			}
			if cluster != tc.wantCluster || apiPath != tc.wantPath {
				t.Errorf("got (%q, %q), want (%q, %q)", cluster, apiPath, tc.wantCluster, tc.wantPath)
			}
		})
	}
	if _, _, perr := SplitClusterPath(&url.URL{Path: "api"}); perr == nil {
		t.Error("accepted a relative path")
	}
	if _, _, perr := SplitClusterPath(nil); perr == nil {
		t.Error("accepted a nil URL")
	}
}

func TestSplitClusterPathWithDefault(t *testing.T) {
	tests := []struct {
		name        string
		target      string
		defaultID   string
		wantCluster string
		wantPath    string
		wantErr     bool
		wantMsg     string
	}{
		{name: "bare resource path", target: "/api/v1/namespaces/default/pods", defaultID: InClusterID, wantCluster: InClusterID, wantPath: "/api/v1/namespaces/default/pods"},
		{name: "bare discovery path", target: "/apis/metrics.k8s.io/v1beta1/nodes", defaultID: InClusterID, wantCluster: InClusterID, wantPath: "/apis/metrics.k8s.io/v1beta1/nodes"},
		{name: "bare root path", target: "/", defaultID: InClusterID, wantCluster: InClusterID, wantPath: "/"},
		{name: "explicit in-cluster prefix still strips", target: "/clusters/in-cluster/api/v1/pods", defaultID: InClusterID, wantCluster: InClusterID, wantPath: "/api/v1/pods"},
		{name: "explicit fleet prefix overrides default", target: "/clusters/" + gkeFleet + "/api/v1/pods", defaultID: InClusterID, wantCluster: gkeFleet, wantPath: "/api/v1/pods"},
		{name: "invalid default cluster ID", target: "/api/v1/pods", defaultID: "bad$default", wantErr: true, wantMsg: "invalid default cluster ID"},
		{name: "missing cluster ID after /clusters/", target: "/clusters/", defaultID: InClusterID, wantErr: true, wantMsg: "missing cluster ID"},
		{name: "invalid explicit cluster ID", target: "/clusters/bad$cluster/api/v1/pods", defaultID: InClusterID, wantErr: true, wantMsg: "invalid cluster ID"},
		{name: "encoded slash on bare path", target: "/api/v1/namespaces/default%2fsecrets/pods", defaultID: InClusterID, wantErr: true, wantMsg: "percent-encoded"},
		{name: "encoded dots on bare path", target: "/api/v1/namespaces/%2e%2e/secrets", defaultID: InClusterID, wantErr: true, wantMsg: "percent-encoded"},
		{name: "dot-dot on bare path", target: "/api/v1/namespaces/default/pods/../../secrets", defaultID: InClusterID, wantErr: true, wantMsg: "dot segments"},
		{name: "double slash on bare path", target: "//api/v1/pods", defaultID: InClusterID, wantErr: true, wantMsg: "empty path segments"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			u, err := url.ParseRequestURI(tc.target)
			if err != nil {
				t.Fatalf("parse %q: %v", tc.target, err)
			}
			cluster, apiPath, perr := SplitClusterPathWithDefault(u, tc.defaultID)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("accepted (cluster=%q, path=%q)", cluster, apiPath)
				}
				if perr.Code != http.StatusBadRequest || perr.RejectionReason != ReasonInvalidRoute {
					t.Errorf("refusal = %d %q, want 400 %q", perr.Code, perr.RejectionReason, ReasonInvalidRoute)
				}
				if !strings.Contains(perr.Error(), tc.wantMsg) {
					t.Errorf("refusal %q, want it to say %q", perr.Error(), tc.wantMsg)
				}
				return
			}
			if perr != nil {
				t.Fatalf("refused: %v", perr)
			}
			if cluster != tc.wantCluster || apiPath != tc.wantPath {
				t.Errorf("got (%q, %q), want (%q, %q)", cluster, apiPath, tc.wantCluster, tc.wantPath)
			}
		})
	}
	if cluster, apiPath, perr := SplitClusterPathWithDefault(&url.URL{Path: ""}, InClusterID); perr != nil || cluster != InClusterID || apiPath != "/" {
		t.Errorf("empty URL path with default = (%q, %q, %v), want (%q, \"/\", nil)", cluster, apiPath, perr, InClusterID)
	}
	if _, _, perr := SplitClusterPathWithDefault(&url.URL{Path: "api"}, InClusterID); perr == nil {
		t.Error("accepted a relative path")
	}
	if _, _, perr := SplitClusterPathWithDefault(nil, InClusterID); perr == nil {
		t.Error("accepted a nil URL")
	}
}

func TestValidateURLPath(t *testing.T) {
	for _, tc := range []struct {
		name    string
		path    string
		rawPath string
		wantMsg string
	}{
		{name: "clean path and empty rawPath", path: "/api/v1/pods"},
		{name: "literal dot-dot in rawPath with clean path", path: "/api/v1/pods", rawPath: "/api/v1/../secrets", wantMsg: "dot segments"},
		{name: "literal dot-dot in rawPath with empty path", rawPath: "/api/v1/../secrets", wantMsg: "dot segments"},
		{name: "literal single dot in rawPath", path: "/api/v1/pods", rawPath: "/api/v1/./pods", wantMsg: "dot segments"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateURLPath(tc.path, tc.rawPath)
			if tc.wantMsg == "" {
				if err != nil {
					t.Fatalf("ValidateURLPath(%q, %q) = %v, want nil", tc.path, tc.rawPath, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("ValidateURLPath(%q, %q) = nil, want error containing %q", tc.path, tc.rawPath, tc.wantMsg)
			}
			if !strings.Contains(err.Error(), tc.wantMsg) {
				t.Errorf("ValidateURLPath(%q, %q) = %q, want substring %q", tc.path, tc.rawPath, err.Error(), tc.wantMsg)
			}
		})
	}
}

type policyCase struct {
	name    string
	method  string
	target  string
	headers map[string]string
	// want maps each mode to the expected outcome: "" for allowed, or the rejection reason.
	want map[Mode]RejectionReason
}

func every(r RejectionReason) map[Mode]RejectionReason {
	return map[Mode]RejectionReason{ModeReadOnly: r, ModeReadOnlyWithDryRun: r, ModeReadWrite: r}
}

func onlyReadWrite(r RejectionReason) map[Mode]RejectionReason {
	return map[Mode]RejectionReason{ModeReadOnly: r, ModeReadOnlyWithDryRun: r, ModeReadWrite: ""}
}

var upgradeHeaders = map[string]string{"Connection": "Upgrade", "Upgrade": "SPDY/3.1"}

func TestPolicyEvaluate(t *testing.T) {
	tests := []policyCase{
		{name: "discovery /api", method: http.MethodGet, target: "/api", want: every("")},
		{name: "discovery /api/v1", method: http.MethodGet, target: "/api/v1", want: every("")},
		{name: "discovery /apis/apps", method: http.MethodGet, target: "/apis/apps", want: every("")},
		{name: "discovery /apis/apps/v1", method: http.MethodGet, target: "/apis/apps/v1", want: every("")},
		{name: "discovery HEAD /version", method: http.MethodHead, target: "/version", want: every("")},
		{name: "openapi v3 document", method: http.MethodGet, target: "/openapi/v3/apis/apps/v1", want: every("")},
		{name: "openapi v3 bare prefix with slash", method: http.MethodGet, target: "/openapi/v3/", want: every(ReasonDiscoveryPathBlocked)},
		{name: "metrics", method: http.MethodGet, target: "/metrics", want: every("")},
		{name: "HEAD /metrics", method: http.MethodHead, target: "/metrics", want: every("")},
		{name: "POST to /metrics", method: http.MethodPost, target: "/metrics", want: every(ReasonDiscoveryPathBlocked)},
		{name: "pprof", method: http.MethodGet, target: "/debug/pprof", want: every(ReasonDiscoveryPathBlocked)},
		{name: "POST to discovery", method: http.MethodPost, target: "/api", want: every(ReasonDiscoveryPathBlocked)},
		{name: "upgrade on discovery", method: http.MethodGet, target: "/api", headers: upgradeHeaders, want: every(ReasonProtocolUpgradeBlocked)},

		{name: "list pods", method: http.MethodGet, target: "/api/v1/namespaces/default/pods", want: every("")},
		{name: "watch events", method: http.MethodGet, target: "/api/v1/namespaces/default/events?watch=true", want: every("")},
		{name: "follow logs", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=true", want: every("")},
		{name: "get scale", method: http.MethodGet, target: "/apis/apps/v1/namespaces/default/deployments/web/scale", want: every("")},
		{name: "list secrets", method: http.MethodGet, target: "/api/v1/namespaces/default/secrets", want: every("")},
		{name: "watch through the path", method: http.MethodGet, target: "/api/v1/watch/namespaces/default/pods", want: every("")},
		{name: "watch secrets", method: http.MethodGet, target: "/api/v1/namespaces/default/secrets?watch=true", want: every("")},
		{name: "upgrade on a list", method: http.MethodGet, target: "/api/v1/namespaces/default/pods", headers: upgradeHeaders, want: every(ReasonProtocolUpgradeBlocked)},
		{name: "upgrade token among others", method: http.MethodGet, target: "/api/v1/pods", headers: map[string]string{"Connection": "keep-alive, Upgrade"}, want: every(ReasonProtocolUpgradeBlocked)},

		{name: "exec GET", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/exec?command=sh", want: every(ReasonSubresourceBlocked)},
		{name: "exec POST", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/exec?command=sh", headers: upgradeHeaders, want: every(ReasonSubresourceBlocked)},
		{name: "attach", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/attach", want: every(ReasonSubresourceBlocked)},
		{name: "portforward", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/portforward", want: every(ReasonSubresourceBlocked)},
		{name: "service proxy", method: http.MethodGet, target: "/api/v1/namespaces/default/services/s/proxy/", want: every(ReasonSubresourceBlocked)},
		{name: "node proxy", method: http.MethodGet, target: "/api/v1/nodes/n/proxy/metrics", want: every(ReasonSubresourceBlocked)},
		{name: "exec in another case", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/Exec", want: every(ReasonSubresourceBlocked)},
		{name: "serviceaccount token", method: http.MethodPost, target: "/api/v1/namespaces/default/serviceaccounts/default/token", want: every(ReasonSubresourceBlocked)},
		{name: "ephemeral containers", method: http.MethodPatch, target: "/api/v1/namespaces/default/pods/p/ephemeralcontainers", want: every(ReasonSubresourceBlocked)},
		{name: "pod binding", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/binding", want: every(ReasonSubresourceBlocked)},
		{name: "top-level bindings", method: http.MethodPost, target: "/api/v1/namespaces/default/bindings", want: every(ReasonResourceBlocked)},
		{name: "top-level bindings dry run", method: http.MethodPost, target: "/api/v1/namespaces/default/bindings?dryRun=All", want: every(ReasonResourceBlocked)},
		{name: "CSR approval", method: http.MethodPut, target: "/apis/certificates.k8s.io/v1/certificatesigningrequests/c/approval", want: every(ReasonSubresourceBlocked)},

		{name: "tokenreviews", method: http.MethodPost, target: "/apis/authentication.k8s.io/v1/tokenreviews", want: every(ReasonAuthResourceBlocked)},
		{name: "selfsubjectreviews", method: http.MethodPost, target: "/apis/authentication.k8s.io/v1/selfsubjectreviews", want: every(ReasonAuthResourceBlocked)},
		{name: "selfsubjectaccessreviews", method: http.MethodPost, target: "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", want: every(ReasonAuthResourceBlocked)},
		{name: "localsubjectaccessreviews", method: http.MethodPost, target: "/apis/authorization.k8s.io/v1/namespaces/default/localsubjectaccessreviews", want: every(ReasonAuthResourceBlocked)},

		{name: "create without dryRun", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: ReasonDryRunRequired, ModeReadWrite: ""}},
		{name: "create with dryRun=All", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All&fieldManager=kubectl",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
		{name: "server-side apply with dryRun=All", method: http.MethodPatch, target: "/apis/apps/v1/namespaces/default/deployments/web?dryRun=All&fieldManager=kubectl",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
		{name: "replace with dryRun=All", method: http.MethodPut, target: "/apis/apps/v1/namespaces/default/deployments/web?dryRun=All",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
		{name: "duplicate dryRun", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All&dryRun=",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: ReasonDryRunRequired, ModeReadWrite: ""}},
		{name: "dryRun with a semicolon", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All;x=1",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: ReasonDryRunRequired, ModeReadWrite: ""}},
		{name: "dryRun in the wrong case", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments?dryrun=All",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: ReasonDryRunRequired, ModeReadWrite: ""}},
		{name: "delete with dryRun=All", method: http.MethodDelete, target: "/api/v1/namespaces/default/pods/p?dryRun=All", want: onlyReadWrite(ReasonVerbBlocked)},
		{name: "unknown HTTP method", method: http.MethodOptions, target: "/api/v1/namespaces/default/pods", want: every(ReasonVerbBlocked)},
		{name: "deletecollection", method: http.MethodDelete, target: "/api/v1/namespaces/default/pods", want: onlyReadWrite(ReasonVerbBlocked)},

		{name: "status patch without dryRun", method: http.MethodPatch, target: "/api/v1/namespaces/default/pods/p/status", want: onlyReadWrite(ReasonSubresourceBlocked)},
		{name: "status patch with dryRun=All", method: http.MethodPatch, target: "/api/v1/nodes/n/status?dryRun=All",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonSubresourceBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
		{name: "scale without dryRun", method: http.MethodPatch, target: "/apis/apps/v1/namespaces/default/deployments/web/scale", want: onlyReadWrite(ReasonSubresourceBlocked)},
		{name: "eviction", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/eviction", want: onlyReadWrite(ReasonSubresourceBlocked)},
		{name: "write to pod log", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/log", want: every(ReasonSubresourceBlocked)},
		{name: "resize", method: http.MethodPatch, target: "/api/v1/namespaces/default/pods/p/resize", want: onlyReadWrite(ReasonSubresourceBlocked)},

		// RedactSecrets is on in this table.
		{name: "secret subresource read", method: http.MethodGet, target: "/api/v1/namespaces/default/secrets/s/status", want: every(ReasonSecretSubresourceBlocked)},
		{name: "secret dry-run JSON patch", method: http.MethodPatch, target: "/api/v1/namespaces/default/secrets/s?dryRun=All", want: every(ReasonSecretWriteBlocked)},
		{name: "secret dry-run replace", method: http.MethodPut, target: "/api/v1/namespaces/default/secrets/s?dryRun=All", want: every(ReasonSecretWriteBlocked)},
		{name: "secret create with dryRun=All", method: http.MethodPost, target: "/api/v1/namespaces/default/secrets?dryRun=All",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
		{name: "secret delete", method: http.MethodDelete, target: "/api/v1/namespaces/default/secrets/s", want: onlyReadWrite(ReasonVerbBlocked)},
		{name: "secret-named CRD in another group", method: http.MethodPatch, target: "/apis/example.com/v1/namespaces/default/secrets/s?dryRun=All",
			want: map[Mode]RejectionReason{ModeReadOnly: ReasonVerbBlocked, ModeReadOnlyWithDryRun: "", ModeReadWrite: ""}},
	}

	factory := NewRequestInfoFactory()
	for _, tc := range tests {
		for _, mode := range []Mode{ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite} {
			want, ok := tc.want[mode]
			if !ok {
				t.Fatalf("%s: no expectation for %s", tc.name, mode)
			}
			t.Run(tc.name+"/"+string(mode), func(t *testing.T) {
				req := httptest.NewRequest(tc.method, tc.target, nil)
				for k, v := range tc.headers {
					req.Header.Set(k, v)
				}
				info, err := factory.NewRequestInfo(req)
				if err != nil {
					t.Fatalf("NewRequestInfo(%q): %v", tc.target, err)
				}
				before := req.URL.String()
				perr := Policy{Mode: mode, RedactSecrets: true}.Evaluate(req, info)
				var got RejectionReason
				if perr != nil {
					got = perr.RejectionReason
				}
				if got != want {
					t.Errorf("got %q, want %q (info=%+v, err=%v)", got, want, info, perr)
				}
				if req.URL.String() != before {
					t.Errorf("Evaluate rewrote the URL: %q -> %q", before, req.URL.String())
				}
			})
		}
	}
}

func TestPolicyWithoutRedaction(t *testing.T) {
	factory := NewRequestInfoFactory()
	for _, tc := range []struct{ method, target string }{
		{http.MethodPatch, "/api/v1/namespaces/default/secrets/s?dryRun=All"},
		{http.MethodPut, "/api/v1/namespaces/default/secrets/s?dryRun=All"},
		{http.MethodGet, "/api/v1/namespaces/default/secrets/s/status"},
	} {
		req := httptest.NewRequest(tc.method, tc.target, nil)
		info, _ := factory.NewRequestInfo(req)
		if perr := (Policy{Mode: ModeReadOnlyWithDryRun}).Evaluate(req, info); perr != nil {
			t.Errorf("%s %s refused with redaction off: %v", tc.method, tc.target, perr)
		}
	}
}

func TestPolicyUpgradeSubresources(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/p/exec?command=sh", nil)
	for k, v := range upgradeHeaders {
		req.Header.Set(k, v)
	}
	info, _ := NewRequestInfoFactory().NewRequestInfo(req)
	if perr := (Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}).Evaluate(req, info); perr != nil {
		t.Errorf("exec refused in read-write with upgrades allowed: %v", perr)
	}
	for _, mode := range []Mode{ModeReadOnly, ModeReadOnlyWithDryRun} {
		if perr := (Policy{Mode: mode, AllowUpgradeSubresources: true}).Evaluate(req, info); perr == nil {
			t.Errorf("exec allowed in %s", mode)
		}
	}

	// The opt-in covers the upgrade subresources only, not an upgrade on any other request.
	for _, target := range []string{"/api/v1/namespaces/default/pods", "/api/v1/namespaces/default/pods/p/log"} {
		plain := httptest.NewRequest(http.MethodGet, target, nil)
		for k, v := range upgradeHeaders {
			plain.Header.Set(k, v)
		}
		plainInfo, _ := NewRequestInfoFactory().NewRequestInfo(plain)
		perr := Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}.Evaluate(plain, plainInfo)
		if perr == nil || perr.RejectionReason != ReasonProtocolUpgradeBlocked {
			t.Errorf("upgrade on %s: got %v, want %q", target, perr, ReasonProtocolUpgradeBlocked)
		}
	}

	tokenReq := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/serviceaccounts/sa/token", nil)
	tokenInfo, _ := NewRequestInfoFactory().NewRequestInfo(tokenReq)
	if perr := (Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}).Evaluate(tokenReq, tokenInfo); perr == nil {
		t.Error("token minting allowed with upgrades allowed")
	}
}

func TestPolicyFailsClosed(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api", nil)
	info, _ := NewRequestInfoFactory().NewRequestInfo(req)
	for _, mode := range []Mode{"", "READ-WRITE"} {
		perr := Policy{Mode: mode}.Evaluate(req, info)
		if perr == nil || perr.RejectionReason != ReasonInvalidMode || perr.Code != http.StatusInternalServerError {
			t.Errorf("mode %q: got %v, want a 500 %q", mode, perr, ReasonInvalidMode)
		}
	}
	if perr := (Policy{Mode: ModeReadWrite}).Evaluate(req, nil); perr == nil {
		t.Error("allowed a request with no RequestInfo")
	}
}

// The read-only markers are what deploy/docker/patches/permission_refusal_stop.py reads to
// stop the agent after a refused write, and the verb must be one it counts as mutating.
func TestRefusalMarkers(t *testing.T) {
	tests := []struct {
		method, target, verb string
	}{
		{http.MethodDelete, "/api/v1/namespaces/default/pods/p", "delete"},
		{http.MethodDelete, "/api/v1/namespaces/default/pods", "delete"},
		{http.MethodPut, "/apis/apps/v1/namespaces/default/deployments/web", "replace"},
		{http.MethodPatch, "/apis/apps/v1/namespaces/default/deployments/web", "patch"},
		{http.MethodPost, "/apis/apps/v1/namespaces/default/deployments", "create"},
		{http.MethodPatch, "/apis/apps/v1/namespaces/default/deployments/web/scale", "scale"},
		{http.MethodPost, "/api/v1/namespaces/default/pods/p/eviction", "drain"},
		{http.MethodPut, "/apis/certificates.k8s.io/v1/certificatesigningrequests/c/approval", "certificate approve"},
		{http.MethodPatch, "/api/v1/namespaces/default/secrets/s?dryRun=All", "patch"},
		{http.MethodPost, "/api/v1/namespaces/default/serviceaccounts/sa/token", "create"},
	}
	factory := NewRequestInfoFactory()
	for _, tc := range tests {
		req := httptest.NewRequest(tc.method, tc.target, nil)
		info, _ := factory.NewRequestInfo(req)
		perr := Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true}.Evaluate(req, info)
		if perr == nil {
			t.Fatalf("%s %s allowed", tc.method, tc.target)
		}
		for _, marker := range []string{"refused however it is attempted", "\npolicy rule: kubernetes.read-only", "\npolicy verb: " + tc.verb + "\x00"} {
			if !strings.Contains(perr.Message+"\x00", marker) {
				t.Errorf("%s %s: message %q lacks %q", tc.method, tc.target, perr.Message, strings.TrimSuffix(marker, "\x00"))
			}
		}
		rw := Policy{Mode: ModeReadWrite, RedactSecrets: true}.Evaluate(req, info)
		if rw != nil && strings.Contains(rw.Message, "kubernetes.read-only") {
			t.Errorf("%s %s: read-write refusal carries the read-only rule: %q", tc.method, tc.target, rw.Message)
		}
	}
}

// A refused read, or a request with no verb, is not a refused change, so it carries no
// read-only markers for the refusal hook to count.
func TestRefusalWithoutMarkers(t *testing.T) {
	tests := []struct{ method, target string }{
		{http.MethodGet, "/api/v1/namespaces/default/pods/p/ephemeralcontainers"},
		{http.MethodGet, "/api/v1/namespaces/default/pods/p/exec?command=sh"},
		{http.MethodOptions, "/api/v1/namespaces/default/pods"},
		{http.MethodGet, "/debug/pprof"},
		{http.MethodPost, "/metrics"},
		{http.MethodGet, "/apis/certificates.k8s.io/v1/certificatesigningrequests/c/approval"},
		{http.MethodOptions, "/apis/certificates.k8s.io/v1/certificatesigningrequests/c/approval"},
	}
	factory := NewRequestInfoFactory()
	for _, tc := range tests {
		req := httptest.NewRequest(tc.method, tc.target, nil)
		info, _ := factory.NewRequestInfo(req)
		perr := Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true}.Evaluate(req, info)
		if perr == nil {
			t.Fatalf("%s %s allowed", tc.method, tc.target)
		}
		if strings.Contains(perr.Message, "policy rule:") || strings.Contains(perr.Message, "policy verb:") {
			t.Errorf("%s %s: refusal carries read-only markers: %q", tc.method, tc.target, perr.Message)
		}
		if !strings.Contains(perr.Message, "permission boundary") {
			t.Errorf("%s %s: refusal lacks the boundary notice: %q", tc.method, tc.target, perr.Message)
		}
	}
}

func TestPolicyDenyStreaming(t *testing.T) {
	factory := NewRequestInfoFactory()
	tests := []struct {
		name    string
		method  string
		target  string
		headers map[string]string
		want    RejectionReason
	}{
		{name: "list pods allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods", want: ""},
		{name: "get pod allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p", want: ""},
		{name: "watch=false allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=false", want: ""},
		{name: "watch=FALSE allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=FALSE", want: ""},
		{name: "watch=0 allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=0", want: ""},
		{name: "pod log finite allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?tailLines=20", want: ""},
		{name: "pod log follow=false allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=false", want: ""},
		{name: "pod log follow=FALSE allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=FALSE", want: ""},
		{name: "pod log follow=0 allowed", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=0", want: ""},

		{name: "watch=true blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=true", want: ReasonStreamingBlocked},
		{name: "watch=1 blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=1", want: ReasonStreamingBlocked},
		{name: "watch=yes blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=yes", want: ReasonStreamingBlocked},
		{name: "bare watch key blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch", want: ReasonStreamingBlocked},
		{name: "empty watch= blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=", want: ReasonStreamingBlocked},
		{name: "duplicate watch=false&watch=true blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=false&watch=true", want: ReasonStreamingBlocked},
		{name: "semicolon watch smuggle blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?limit=5;watch=true", want: ReasonStreamingBlocked},
		{name: "percent-encoded watch key blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?%77atch=true", want: ReasonStreamingBlocked},
		{name: "unicode long-s watch=falſe blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=fal%C5%BFe", want: ReasonStreamingBlocked},
		{name: "padded watch=+false+ blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods?watch=+false+", want: ReasonStreamingBlocked},
		{name: "single-resource named watch blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p?watch=true", want: ReasonStreamingBlocked},
		{name: "legacy /api/v1/watch/pods blocked", method: http.MethodGet, target: "/api/v1/watch/pods", want: ReasonStreamingBlocked},
		{name: "legacy /api/v1/watch/namespaces/default/pods blocked", method: http.MethodGet, target: "/api/v1/watch/namespaces/default/pods", want: ReasonStreamingBlocked},
		{name: "legacy /apis/apps/v1/watch/deployments blocked", method: http.MethodGet, target: "/apis/apps/v1/watch/namespaces/default/deployments", want: ReasonStreamingBlocked},
		{name: "secrets watch blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/secrets?watch=true", want: ReasonStreamingBlocked},

		{name: "pod log follow=true blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=true", want: ReasonStreamingBlocked},
		{name: "pod log follow=1 blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=1", want: ReasonStreamingBlocked},
		{name: "pod log bare follow blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow", want: ReasonStreamingBlocked},
		{name: "pod log empty follow= blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=", want: ReasonStreamingBlocked},
		{name: "pod log duplicate follow=false&follow=true blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=false&follow=true", want: ReasonStreamingBlocked},
		{name: "pod log semicolon follow blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?tailLines=5;follow=true", want: ReasonStreamingBlocked},
		{name: "pod log percent-encoded follow key blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?%66ollow=true", want: ReasonStreamingBlocked},
		{name: "pod log unicode long-s follow=falſe blocked", method: http.MethodGet, target: "/api/v1/namespaces/default/pods/p/log?follow=fal%C5%BFe", want: ReasonStreamingBlocked},

		{name: "exec blocked even in read-write with AllowUpgradeSubresources", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/exec?command=sh", headers: upgradeHeaders, want: ReasonSubresourceBlocked},
		{name: "portforward blocked even in read-write with AllowUpgradeSubresources", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/portforward", headers: upgradeHeaders, want: ReasonSubresourceBlocked},
	}

	for _, tc := range tests {
		for _, mode := range []Mode{ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite} {
			t.Run(tc.name+"/"+string(mode), func(t *testing.T) {
				req := httptest.NewRequest(tc.method, tc.target, nil)
				for k, v := range tc.headers {
					req.Header.Set(k, v)
				}
				info, err := factory.NewRequestInfo(req)
				if err != nil {
					t.Fatalf("NewRequestInfo(%q): %v", tc.target, err)
				}
				pol := Policy{
					Mode:                     mode,
					RedactSecrets:            true,
					AllowUpgradeSubresources: true,
					DenyStreaming:            true,
				}
				perr := pol.Evaluate(req, info)
				var got RejectionReason
				if perr != nil {
					got = perr.RejectionReason
					if perr.Code != http.StatusForbidden {
						t.Errorf("code = %d, want 403", perr.Code)
					}
					if !strings.Contains(perr.Message, "permission boundary") {
						t.Errorf("message %q lacks boundary notice", perr.Message)
					}
					if strings.Contains(perr.Message, "policy rule:") {
						t.Errorf("streaming refusal carries read-only markers: %q", perr.Message)
					}
				}
				if got != tc.want {
					t.Errorf("got %q, want %q (err=%v)", got, tc.want, perr)
				}
			})
		}
	}
}
