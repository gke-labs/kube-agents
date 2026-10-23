package k8sapiproxy

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/endpoints/request"
)

var validClusterIDPattern = regexp.MustCompile(`^(in-cluster|gke_[a-z0-9-]+_[a-z0-9-]+_[a-z0-9-]+|kind-[a-z0-9._-]+)\z`)

// RejectionReason classifies why a request was refused by kube-proxy.
// Every value comes from a closed vocabulary for Prometheus labels and OpenTelemetry attributes.
type RejectionReason string

const (
	ReasonNone                     RejectionReason = ""
	ReasonUnauthenticated          RejectionReason = "unauthenticated"
	ReasonProtocolUpgradeBlocked   RejectionReason = "protocol_upgrade_blocked"
	ReasonInvalidRoute             RejectionReason = "invalid_route"
	ReasonDiscoveryPathBlocked     RejectionReason = "discovery_path_blocked"
	ReasonAuthResourceBlocked      RejectionReason = "auth_resource_blocked"
	ReasonSecretSubresourceBlocked RejectionReason = "secret_subresource_blocked"
	ReasonSubresourceBlocked       RejectionReason = "subresource_blocked"
	ReasonDryRunRequired           RejectionReason = "dry_run_required"
	ReasonVerbBlocked              RejectionReason = "verb_blocked"
)

// PolicyError represents an HTTP error returned by the proxy policy engine.
type PolicyError struct {
	Code            int
	Reason          metav1.StatusReason
	RejectionReason RejectionReason
	Message         string
}

func (e *PolicyError) Error() string {
	return e.Message
}

// WriteStatusError writes a native Kubernetes metav1.Status JSON response to w.
func WriteStatusError(w http.ResponseWriter, code int, reason metav1.StatusReason, message string) {
	status := metav1.Status{
		TypeMeta: metav1.TypeMeta{
			Kind:       "Status",
			APIVersion: "v1",
		},
		Status:  metav1.StatusFailure,
		Message: message,
		Reason:  reason,
		Code:    int32(code),
	}
	body, err := json.Marshal(status)
	if err != nil {
		http.Error(w, message, code)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(code)
	_, _ = w.Write(body)
}

// NewRequestInfoFactory constructs a Kubernetes RequestInfoFactory for standard API prefixes.
func NewRequestInfoFactory() *request.RequestInfoFactory {
	return &request.RequestInfoFactory{
		APIPrefixes:          sets.NewString("api", "apis"),
		GrouplessAPIPrefixes: sets.NewString("api"),
	}
}

// NormalizeAndExtractCluster validates the URL path against traversal attacks and extracts an optional /clusters/{clusterID} prefix.
func NormalizeAndExtractCluster(rawPath, urlPath, defaultClusterID string) (string, string, *PolicyError) {
	if urlPath == "" || !strings.HasPrefix(urlPath, "/") {
		return "", "", &PolicyError{
			Code:            http.StatusBadRequest,
			Reason:          metav1.StatusReasonBadRequest,
			RejectionReason: ReasonInvalidRoute,
			Message:         "kube-proxy rejected request: path must start with '/'",
		}
	}

	for _, candidate := range []string{rawPath, urlPath} {
		if candidate == "" {
			continue
		}
		lower := strings.ToLower(candidate)
		if strings.Contains(lower, "%2f") || strings.Contains(lower, "%2e") || strings.Contains(lower, "%00") || strings.Contains(candidate, `\`) {
			return "", "", &PolicyError{
				Code:            http.StatusBadRequest,
				Reason:          metav1.StatusReasonBadRequest,
				RejectionReason: ReasonInvalidRoute,
				Message:         "kube-proxy rejected request: percent-encoded path separators or dot segments are not permitted",
			}
		}
		if strings.Contains(candidate, "//") {
			return "", "", &PolicyError{
				Code:            http.StatusBadRequest,
				Reason:          metav1.StatusReasonBadRequest,
				RejectionReason: ReasonInvalidRoute,
				Message:         "kube-proxy rejected request: empty path segments ('//') are not permitted",
			}
		}
	}

	trimmed := strings.TrimPrefix(urlPath, "/")
	if trimmed != "" {
		segments := strings.Split(trimmed, "/")
		for _, seg := range segments {
			if seg == "." || seg == ".." {
				return "", "", &PolicyError{
					Code:            http.StatusBadRequest,
					Reason:          metav1.StatusReasonBadRequest,
					RejectionReason: ReasonInvalidRoute,
					Message:         "kube-proxy rejected request: relative dot segments ('.' or '..') are not permitted",
				}
			}
		}

		if segments[0] == "clusters" {
			if len(segments) < 2 || segments[1] == "" {
				return "", "", &PolicyError{
					Code:            http.StatusBadRequest,
					Reason:          metav1.StatusReasonBadRequest,
					RejectionReason: ReasonInvalidRoute,
					Message:         "kube-proxy rejected request: missing cluster ID after /clusters/",
				}
			}
			clusterID := segments[1]
			if !validClusterIDPattern.MatchString(clusterID) {
				return "", "", &PolicyError{
					Code:            http.StatusBadRequest,
					Reason:          metav1.StatusReasonBadRequest,
					RejectionReason: ReasonInvalidRoute,
					Message:         fmt.Sprintf("kube-proxy rejected request: invalid cluster ID %q", clusterID),
				}
			}
			remaining := "/" + strings.Join(segments[2:], "/")
			return clusterID, remaining, nil
		}
	}

	clusterID := strings.TrimSpace(defaultClusterID)
	if clusterID == "" {
		clusterID = "in-cluster"
	}
	return clusterID, urlPath, nil
}

var allowedDiscoveryPaths = map[string]struct{}{
	"/api":        {},
	"/apis":       {},
	"/version":    {},
	"/openapi/v2": {},
	"/openapi/v3": {},
	"/healthz":    {},
	"/livez":      {},
	"/readyz":     {},
}

var discoverySegmentPattern = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?\z`)

func isAllowedDiscoveryPath(p string) bool {
	if _, ok := allowedDiscoveryPaths[p]; ok {
		return true
	}
	if strings.HasPrefix(p, "/openapi/v3/") && len(p) > len("/openapi/v3/") {
		return true
	}
	trimmed := strings.TrimPrefix(p, "/")
	parts := strings.Split(trimmed, "/")
	if len(parts) == 2 && (parts[0] == "api" || parts[0] == "apis") && discoverySegmentPattern.MatchString(parts[1]) {
		return true
	}
	if len(parts) == 3 && parts[0] == "apis" && discoverySegmentPattern.MatchString(parts[1]) && discoverySegmentPattern.MatchString(parts[2]) {
		return true
	}
	return false
}

var upgradeSubresources = map[string]struct{}{
	"exec":        {},
	"attach":      {},
	"portforward": {},
	"proxy":       {},
}

var alwaysBlockedSubresources = map[string]struct{}{
	"ephemeralcontainers": {},
	"binding":             {},
	"token":               {},
	"approval":            {},
}

func permissionBoundaryVerb(info *request.RequestInfo) string {
	if info == nil {
		return ""
	}
	switch strings.ToLower(info.Subresource) {
	case "scale":
		return "scale"
	case "approval":
		return "certificate approve"
	case "eviction":
		return "evict"
	}
	switch info.Verb {
	case "update":
		return "replace"
	case "deletecollection":
		return "delete"
	default:
		return info.Verb
	}
}

func formatReadOnlyRefusalMessage(base string, mode Mode, info *request.RequestInfo) string {
	if mode != "" && mode != ModeReadOnly && mode != ModeReadOnlyWithDryRun {
		return base + " This is a permission boundary, not an error to work around."
	}
	verb := permissionBoundaryVerb(info)
	if verb == "" {
		return base + " This is a permission boundary, not an error to work around: the change is refused however it is attempted.\npolicy rule: kubernetes.read-only"
	}
	return fmt.Sprintf(
		"%s This is a permission boundary, not an error to work around: the change is refused however it is attempted.\npolicy rule: kubernetes.read-only\npolicy verb: %s",
		base,
		verb,
	)
}

// IsCoreV1Secret returns true when info targets core/v1 Secret resources.
func IsCoreV1Secret(info *request.RequestInfo) bool {
	return info != nil && info.IsResourceRequest && info.APIGroup == "" && info.APIVersion == "v1" && info.Resource == "secrets"
}

// EvaluatePolicy enforces the L7 Kubernetes API policy on r and info.
func EvaluatePolicy(mode Mode, redactSecrets bool, r *http.Request, info *request.RequestInfo) *PolicyError {
	return EvaluatePolicyWithOptions(mode, redactSecrets, false, r, info)
}

// EvaluatePolicyWithOptions enforces the L7 Kubernetes API policy on r and info,
// with optional opt-in for interactive stream-upgrade subresources in ModeReadWrite.
func EvaluatePolicyWithOptions(mode Mode, redactSecrets bool, allowUpgradeSubresources bool, r *http.Request, info *request.RequestInfo) *PolicyError {
	if !info.IsResourceRequest {
		if HasUpgradeHeader(r) {
			return &PolicyError{
				Code:            http.StatusForbidden,
				Reason:          metav1.StatusReasonForbidden,
				RejectionReason: ReasonProtocolUpgradeBlocked,
				Message:         "kube-proxy policy blocked protocol upgrade request: interactive streaming subresources are not permitted. This is a permission boundary, not an error to work around.",
			}
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			return &PolicyError{
				Code:            http.StatusForbidden,
				Reason:          metav1.StatusReasonForbidden,
				RejectionReason: ReasonDiscoveryPathBlocked,
				Message:         fmt.Sprintf("kube-proxy policy blocked HTTP method %q on non-resource path %q: only GET and HEAD are permitted on discovery paths. This is a permission boundary, not an error to work around.", r.Method, info.Path),
			}
		}
		if isAllowedDiscoveryPath(info.Path) {
			return nil
		}
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonDiscoveryPathBlocked,
			Message:         fmt.Sprintf("kube-proxy policy blocked non-resource path %q: only standard Kubernetes discovery and health paths are permitted. This is a permission boundary, not an error to work around.", info.Path),
		}
	}

	if isBlockedAuthResource(info) {
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonAuthResourceBlocked,
			Message:         fmt.Sprintf("kube-proxy policy blocked resource %q in group %q: authentication and authorization review APIs are not permitted. This is a permission boundary, not an error to work around.", info.Resource, info.APIGroup),
		}
	}

	if redactSecrets && IsCoreV1Secret(info) && info.Subresource != "" {
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonSecretSubresourceBlocked,
			Message:         fmt.Sprintf("kube-proxy policy blocked subresource %q on secrets: Secret subresources are not permitted. This is a permission boundary, not an error to work around.", info.Subresource),
		}
	}

	sub := strings.ToLower(info.Subresource)
	_, isUpgradeSub := upgradeSubresources[sub]
	upgradePermitted := mode == ModeReadWrite && allowUpgradeSubresources && isUpgradeSub

	if info.Subresource != "" {
		if _, blocked := alwaysBlockedSubresources[sub]; blocked {
			base := fmt.Sprintf("kube-proxy policy blocked subresource %q on resource %q: this subresource is not permitted.", info.Subresource, info.Resource)
			msg := base + " This is a permission boundary, not an error to work around."
			if sub == "approval" {
				msg = formatReadOnlyRefusalMessage(base, mode, info)
			}
			return &PolicyError{
				Code:            http.StatusForbidden,
				Reason:          metav1.StatusReasonForbidden,
				RejectionReason: ReasonSubresourceBlocked,
				Message:         msg,
			}
		}
		if isUpgradeSub {
			if !upgradePermitted {
				return &PolicyError{
					Code:            http.StatusForbidden,
					Reason:          metav1.StatusReasonForbidden,
					RejectionReason: ReasonSubresourceBlocked,
					Message:         fmt.Sprintf("kube-proxy policy blocked subresource %q on resource %q: this subresource is not permitted. This is a permission boundary, not an error to work around.", info.Subresource, info.Resource),
				}
			}
			return nil
		}
		if err := evaluateSubresourceByMode(mode, r, info); err != nil {
			return err
		}
	}

	if HasUpgradeHeader(r) && !upgradePermitted {
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonProtocolUpgradeBlocked,
			Message:         "kube-proxy policy blocked protocol upgrade request: interactive streaming subresources are not permitted. This is a permission boundary, not an error to work around.",
		}
	}

	switch mode {
	case ModeReadOnly:
		if info.Verb == "get" || info.Verb == "list" || info.Verb == "watch" {
			return nil
		}
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonVerbBlocked,
			Message:         formatReadOnlyRefusalMessage(fmt.Sprintf("kube-proxy policy blocked verb %q on resource %q: this proxy enforces read-only cluster access.", info.Verb, info.Resource), mode, info),
		}

	case "", ModeReadOnlyWithDryRun:
		if info.Verb == "get" || info.Verb == "list" || info.Verb == "watch" {
			return nil
		}
		if info.Verb == "create" || info.Verb == "update" || info.Verb == "patch" {
			if HasStrictDryRunAll(r) {
				enforceDryRunQuery(r)
				return nil
			}
			return &PolicyError{
				Code:            http.StatusForbidden,
				Reason:          metav1.StatusReasonForbidden,
				RejectionReason: ReasonDryRunRequired,
				Message:         formatReadOnlyRefusalMessage(fmt.Sprintf("kube-proxy policy blocked verb %q on resource %q: this proxy enforces read-only cluster access (pass --dry-run=server for non-persisted validation).", info.Verb, info.Resource), mode, info),
			}
		}
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonVerbBlocked,
			Message:         formatReadOnlyRefusalMessage(fmt.Sprintf("kube-proxy policy blocked verb %q on resource %q: this proxy enforces read-only cluster access.", info.Verb, info.Resource), mode, info),
		}

	case ModeReadWrite:
		switch info.Verb {
		case "get", "list", "watch", "create", "update", "patch", "delete", "deletecollection":
			return nil
		default:
			return &PolicyError{
				Code:            http.StatusForbidden,
				Reason:          metav1.StatusReasonForbidden,
				RejectionReason: ReasonVerbBlocked,
				Message:         fmt.Sprintf("kube-proxy policy blocked verb %q on resource %q: verb is not permitted. This is a permission boundary, not an error to work around.", info.Verb, info.Resource),
			}
		}

	default:
		return &PolicyError{
			Code:            http.StatusForbidden,
			Reason:          metav1.StatusReasonForbidden,
			RejectionReason: ReasonVerbBlocked,
			Message:         fmt.Sprintf("kube-proxy policy blocked request: unknown proxy mode %q", mode),
		}
	}
}

func isBlockedAuthResource(info *request.RequestInfo) bool {
	if info.APIGroup == "authentication.k8s.io" && (info.Resource == "tokenreviews" || info.Resource == "selfsubjectreviews") {
		return true
	}
	if info.APIGroup == "authorization.k8s.io" && (info.Resource == "subjectaccessreviews" || info.Resource == "selfsubjectaccessreviews" || info.Resource == "localsubjectaccessreviews" || info.Resource == "selfsubjectrulesreviews") {
		return true
	}
	return false
}

func evaluateSubresourceByMode(mode Mode, r *http.Request, info *request.RequestInfo) *PolicyError {
	switch strings.ToLower(info.Subresource) {
	case "status", "scale":
		if info.Verb == "get" || info.Verb == "list" || info.Verb == "watch" {
			return nil
		}
		if (mode == "" || mode == ModeReadOnlyWithDryRun) && (info.Verb == "create" || info.Verb == "update" || info.Verb == "patch") && HasStrictDryRunAll(r) {
			enforceDryRunQuery(r)
			return nil
		}
		if mode == ModeReadWrite && (info.Verb == "create" || info.Verb == "update" || info.Verb == "patch") {
			return nil
		}
	case "log":
		if info.Verb == "get" || info.Verb == "list" || info.Verb == "watch" {
			return nil
		}
	case "eviction":
		if mode == ModeReadWrite && info.Verb == "create" {
			return nil
		}
	default:
		if info.Verb == "get" || info.Verb == "list" || info.Verb == "watch" {
			return nil
		}
		if mode == ModeReadWrite && (info.Verb == "create" || info.Verb == "update" || info.Verb == "patch" || info.Verb == "delete" || info.Verb == "deletecollection") {
			return nil
		}
	}
	return &PolicyError{
		Code:            http.StatusForbidden,
		Reason:          metav1.StatusReasonForbidden,
		RejectionReason: ReasonSubresourceBlocked,
		Message:         formatReadOnlyRefusalMessage(fmt.Sprintf("kube-proxy policy blocked verb %q on subresource \"%s/%s\": this subresource operation is not permitted.", info.Verb, info.Resource, info.Subresource), mode, info),
	}
}

// HasStrictDryRunAll returns true when the request query string strictly specifies dryRun=All.
func HasStrictDryRunAll(r *http.Request) bool {
	if r == nil || r.URL == nil {
		return false
	}
	if strings.Contains(r.URL.RawQuery, ";") {
		return false
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return false
	}
	vals, ok := q["dryRun"]
	return ok && len(vals) == 1 && vals[0] == "All"
}

func enforceDryRunQuery(r *http.Request) {
	if r == nil || r.URL == nil {
		return
	}
	q := r.URL.Query()
	q.Set("dryRun", "All")
	r.URL.RawQuery = q.Encode()
}
