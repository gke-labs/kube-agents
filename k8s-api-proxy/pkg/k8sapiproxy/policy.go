package k8sapiproxy

import (
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/endpoints/request"
)

// Mode controls which Kubernetes API verbs the proxy permits.
type Mode string

const (
	// ModeReadOnly permits get, list and watch on resources, plus the discovery paths.
	ModeReadOnly Mode = "read-only"
	// ModeReadOnlyWithDryRun also permits create, update and patch carrying dryRun=All, so
	// `kubectl --dry-run=server` validates a change without persisting it. kube-agents will
	// run in this mode.
	ModeReadOnlyWithDryRun Mode = "read-only-dry-run"
	// ModeReadWrite permits create, update, patch, delete and deletecollection, for platforms
	// that authorize writes. Credential minting and pod placement stay refused, and stream
	// upgrades unless Policy.AllowUpgradeSubresources is set.
	ModeReadWrite Mode = "read-write"
)

// DefaultMode is the mode an empty setting selects.
const DefaultMode = ModeReadOnlyWithDryRun

// ParseMode validates raw, returning DefaultMode for an empty string.
func ParseMode(raw string) (Mode, error) {
	switch m := Mode(raw); m {
	case "":
		return DefaultMode, nil
	case ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite:
		return m, nil
	default:
		return "", fmt.Errorf("invalid proxy mode %q: expected %q, %q or %q", raw, ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite)
	}
}

// RejectionReason classifies a refusal. The values are a closed vocabulary, so they can be
// metric labels and trace attributes.
type RejectionReason string

const (
	ReasonInvalidRoute             RejectionReason = "invalid_route"
	ReasonInvalidMode              RejectionReason = "invalid_mode"
	ReasonProtocolUpgradeBlocked   RejectionReason = "protocol_upgrade_blocked"
	ReasonDiscoveryPathBlocked     RejectionReason = "discovery_path_blocked"
	ReasonAuthResourceBlocked      RejectionReason = "auth_resource_blocked"
	ReasonResourceBlocked          RejectionReason = "resource_blocked"
	ReasonSecretSubresourceBlocked RejectionReason = "secret_subresource_blocked"
	ReasonSecretWriteBlocked       RejectionReason = "secret_write_blocked"
	ReasonSubresourceBlocked       RejectionReason = "subresource_blocked"
	ReasonDryRunRequired           RejectionReason = "dry_run_required"
	ReasonVerbBlocked              RejectionReason = "verb_blocked"
)

// PolicyError is a refusal, carrying what the handler needs to write a metav1.Status.
type PolicyError struct {
	Code            int
	Reason          metav1.StatusReason
	RejectionReason RejectionReason
	Message         string
}

func (e *PolicyError) Error() string {
	return e.Message
}

// InClusterID names the cluster the proxy runs in. A path with no /clusters/{id} prefix and
// no configured default targets it.
const InClusterID = "in-cluster"

const (
	clustersSegment = "clusters"
	messagePrefix   = "kube-proxy rejected request: "
	policyPrefix    = "kube-proxy policy blocked "

	// boundaryNotice ends every policy refusal, so the agent stops rather than retrying.
	boundaryNotice = " This is a permission boundary, not an error to work around."
	// readOnlyNotice replaces boundaryNotice on a refused write in a read-only mode. The
	// markers are the ones deploy/docker/patches/permission_refusal_stop.py reads, and the
	// policy verb is one of refusalVerbs.
	readOnlyNotice  = " This is a permission boundary, not an error to work around: the change is refused however it is attempted.\npolicy rule: kubernetes.read-only"
	policyVerbLabel = "\npolicy verb: "

	dryRunParam = "dryRun"
	dryRunAll   = "All"
)

// Request verbs, as RequestInfoFactory names them.
const (
	verbGet              = "get"
	verbList             = "list"
	verbWatch            = "watch"
	verbCreate           = "create"
	verbUpdate           = "update"
	verbPatch            = "patch"
	verbDelete           = "delete"
	verbDeleteCollection = "deletecollection"
)

// Subresources the policy decides by name.
const (
	subStatus              = "status"
	subScale               = "scale"
	subLog                 = "log"
	subEviction            = "eviction"
	subApproval            = "approval"
	subBinding             = "binding"
	subToken               = "token"
	subEphemeralContainers = "ephemeralcontainers"
)

const (
	coreGroup        = ""
	coreVersion      = "v1"
	secretsResource  = "secrets"
	bindingsResource = "bindings"
)

// kubectl commands named in the `policy verb:` marker.
const (
	kubectlReplace            = "replace"
	kubectlScale              = "scale"
	kubectlDrain              = "drain"
	kubectlCertificateApprove = "certificate approve"
)

// refusalVerbs are the kubectl verbs the policy names in a read-only refusal. Each is in
// permission_refusal_stop.py's MUTATING_VERBS or MUTATING_SUBVERBS; a refusal whose verb is
// not here, such as a refused read, ends with boundaryNotice instead.
var refusalVerbs = sets.New(
	verbCreate, kubectlReplace, verbPatch, verbDelete, kubectlScale, kubectlDrain, kubectlCertificateApprove,
)

// validClusterID accepts InClusterID, a kubeconfig context name for GKE
// (gke_{project}_{location}_{cluster}, where a legacy domain-scoped project is
// {domain}:{name}, as the installer admits) and one for kind (kind-{name}).
var validClusterID = regexp.MustCompile(`^(in-cluster|gke_([a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z0-9-]+_[a-z0-9-]+_[a-z0-9-]+|kind-[a-z0-9._-]+)\z`)

// discoverySegment is an API group or version name.
var discoverySegment = regexp.MustCompile(`^[a-z0-9]([-a-z0-9.]*[a-z0-9])?\z`)

const (
	apiPrefix          = "api"
	apisPrefix         = "apis"
	openAPIV3Prefix    = "/openapi/v3/"
	groupDiscoveryLen  = 2 // /api/{version} or /apis/{group}
	versionDiscoverLen = 3 // /apis/{group}/{version}
)

var discoveryPaths = sets.New(
	"/api", "/apis", "/version", "/openapi/v2", "/openapi/v3", "/healthz", "/livez", "/readyz",
)

var readVerbs = sets.New(verbGet, verbList, verbWatch)

// dryRunVerbs are the verbs the API server honours dryRun=All on.
var dryRunVerbs = sets.New(verbCreate, verbUpdate, verbPatch)

var readWriteVerbs = sets.New(verbGet, verbList, verbWatch, verbCreate, verbUpdate, verbPatch, verbDelete, verbDeleteCollection)

// upgradeSubresources stream over a protocol upgrade, or reach a pod, node or service directly.
var upgradeSubresources = sets.New("exec", "attach", "portforward", "proxy")

// mintingSubresources mint credentials, change who a pod runs as or where it runs, or approve
// a certificate. Refused in every mode.
var mintingSubresources = sets.New(subEphemeralContainers, subBinding, subToken, subApproval)

// placementResources reach the same storage as pods/binding through a top-level resource, so
// they are refused in every mode with it.
var placementResources = map[string]sets.Set[string]{
	coreGroup: sets.New(bindingsResource),
}

// reviewResources answer "who am I" and "may I", by group. Refused in every mode: the answers
// describe the proxy's identity, not the caller's, and a token review validates a token.
var reviewResources = map[string]sets.Set[string]{
	"authentication.k8s.io": sets.New("tokenreviews", "selfsubjectreviews"),
	"authorization.k8s.io": sets.New(
		"subjectaccessreviews", "selfsubjectaccessreviews", "localsubjectaccessreviews", "selfsubjectrulesreviews",
	),
}

// NewRequestInfoFactory returns the classifier the API server itself uses, so the proxy and
// the API server agree on the verb, resource and subresource of a request.
func NewRequestInfoFactory() *request.RequestInfoFactory {
	return &request.RequestInfoFactory{
		APIPrefixes:          sets.NewString(apiPrefix, apisPrefix),
		GrouplessAPIPrefixes: sets.NewString(apiPrefix),
	}
}

// SplitClusterPath validates u's path and splits off an optional /clusters/{id} prefix,
// returning the target cluster and the API server path. A path without the prefix targets
// defaultClusterID, or InClusterID when that is empty. Encoded separators, dot segments,
// empty segments, backslashes and control characters are refused rather than cleaned, so
// the path the policy classifies is the path the API server receives.
func SplitClusterPath(u *url.URL, defaultClusterID string) (clusterID, apiPath string, err *PolicyError) {
	if u == nil || !strings.HasPrefix(u.Path, "/") {
		return "", "", invalidRoute("path must start with '/'")
	}
	for _, p := range []string{u.RawPath, u.Path} {
		lower := strings.ToLower(p)
		if strings.Contains(lower, "%2f") || strings.Contains(lower, "%2e") || strings.Contains(lower, "%5c") || strings.Contains(lower, "%00") {
			return "", "", invalidRoute("percent-encoded separators, dots and NUL are not permitted in the path")
		}
		if strings.Contains(p, "//") {
			return "", "", invalidRoute("empty path segments ('//') are not permitted")
		}
		if strings.ContainsFunc(p, func(c rune) bool { return c == '\\' || c < ' ' || c == '\x7f' }) {
			return "", "", invalidRoute("backslashes and control characters are not permitted in the path")
		}
	}

	segments := strings.Split(strings.TrimPrefix(u.Path, "/"), "/")
	for _, seg := range segments {
		if seg == "." || seg == ".." {
			return "", "", invalidRoute("dot segments ('.' or '..') are not permitted")
		}
	}

	if segments[0] == clustersSegment {
		if len(segments) < 2 || segments[1] == "" {
			return "", "", invalidRoute("missing cluster ID after /clusters/")
		}
		if !validClusterID.MatchString(segments[1]) {
			return "", "", invalidRoute(fmt.Sprintf("invalid cluster ID %q", segments[1]))
		}
		return segments[1], "/" + strings.Join(segments[2:], "/"), nil
	}

	clusterID = strings.TrimSpace(defaultClusterID)
	if clusterID == "" {
		clusterID = InClusterID
	}
	return clusterID, u.Path, nil
}

// Policy decides which requests reach the API server.
type Policy struct {
	Mode Mode
	// RedactSecrets keeps Secret values out of what the Secrets API returns. Besides the
	// response redaction the handler applies, it refuses Secret subresources, and Secret update
	// and patch in every mode: a dry-run patch can copy a value into a field that is returned
	// unredacted, a JSON patch `test` op answers whether a guess is right, and a validation
	// error can echo a copied value back. It does not cover a value a workload exposes: in
	// ModeReadWrite a caller can create a pod that prints a Secret and read its log.
	RedactSecrets bool
	// AllowUpgradeSubresources permits exec, attach, portforward and proxy in ModeReadWrite.
	// Ignored in the read-only modes.
	AllowUpgradeSubresources bool
}

// Evaluate returns nil when r may be forwarded, or the refusal. info must come from
// NewRequestInfoFactory over the path SplitClusterPath returned. Evaluate does not modify r.
func (p Policy) Evaluate(r *http.Request, info *request.RequestInfo) *PolicyError {
	if r == nil || info == nil {
		return invalidRoute("the request could not be classified")
	}
	switch p.Mode {
	case ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite:
	default:
		return &PolicyError{
			Code:            http.StatusInternalServerError,
			Reason:          metav1.StatusReasonInternalError,
			RejectionReason: ReasonInvalidMode,
			Message:         fmt.Sprintf("%srequest: unknown proxy mode %q", policyPrefix, p.Mode),
		}
	}

	if !info.IsResourceRequest {
		return evaluateNonResource(r, info)
	}

	if groups, ok := reviewResources[info.APIGroup]; ok && groups.Has(info.Resource) {
		return forbidden(ReasonAuthResourceBlocked, fmt.Sprintf(
			"%sresource %q in group %q: authentication and authorization review APIs are not permitted.%s",
			policyPrefix, info.Resource, info.APIGroup, boundaryNotice))
	}

	if groups, ok := placementResources[info.APIGroup]; ok && groups.Has(info.Resource) && !readVerbs.Has(info.Verb) {
		return forbidden(ReasonResourceBlocked, fmt.Sprintf(
			"%sverb %q on resource %q: assigning a pod to a node is not permitted.%s",
			policyPrefix, info.Verb, info.Resource, boundaryNotice))
	}

	sub := strings.ToLower(info.Subresource)
	if p.RedactSecrets && isCoreV1Secret(info) {
		if sub != "" {
			return forbidden(ReasonSecretSubresourceBlocked, fmt.Sprintf(
				"%ssubresource %q on secrets: Secret subresources are not permitted.%s",
				policyPrefix, info.Subresource, boundaryNotice))
		}
		if info.Verb == verbUpdate || info.Verb == verbPatch {
			return forbidden(ReasonSecretWriteBlocked, p.refusal(fmt.Sprintf(
				"%sverb %q on secrets: this proxy redacts Secret values, and changing a Secret through it could reveal them.",
				policyPrefix, info.Verb), info))
		}
	}

	upgradePermitted := p.Mode == ModeReadWrite && p.AllowUpgradeSubresources && upgradeSubresources.Has(sub)
	switch {
	case sub == "":
	case mintingSubresources.Has(sub):
		return forbidden(ReasonSubresourceBlocked, p.refusal(fmt.Sprintf(
			"%ssubresource %q on resource %q: this subresource is not permitted.",
			policyPrefix, info.Subresource, info.Resource), info))
	case upgradeSubresources.Has(sub):
		if !upgradePermitted {
			return forbidden(ReasonSubresourceBlocked, fmt.Sprintf(
				"%ssubresource %q on resource %q: this subresource is not permitted.%s",
				policyPrefix, info.Subresource, info.Resource, boundaryNotice))
		}
		return nil
	default:
		if err := p.evaluateSubresource(r, info, sub); err != nil {
			return err
		}
	}

	if HasUpgradeHeader(r) && !upgradePermitted {
		return upgradeBlocked()
	}

	if readVerbs.Has(info.Verb) {
		return nil
	}
	switch p.Mode {
	case ModeReadOnlyWithDryRun:
		if dryRunVerbs.Has(info.Verb) {
			if HasStrictDryRunAll(r) {
				return nil
			}
			return forbidden(ReasonDryRunRequired, p.refusal(fmt.Sprintf(
				"%sverb %q on resource %q: this proxy enforces read-only cluster access (pass --dry-run=server for non-persisted validation).",
				policyPrefix, info.Verb, info.Resource), info))
		}
	case ModeReadWrite:
		if readWriteVerbs.Has(info.Verb) {
			return nil
		}
		return forbidden(ReasonVerbBlocked, fmt.Sprintf(
			"%sverb %q on resource %q: verb is not permitted.%s", policyPrefix, info.Verb, info.Resource, boundaryNotice))
	}
	return forbidden(ReasonVerbBlocked, p.refusal(fmt.Sprintf(
		"%sverb %q on resource %q: this proxy enforces read-only cluster access.", policyPrefix, info.Verb, info.Resource), info))
}

func evaluateNonResource(r *http.Request, info *request.RequestInfo) *PolicyError {
	if HasUpgradeHeader(r) {
		return upgradeBlocked()
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		return forbidden(ReasonDiscoveryPathBlocked, fmt.Sprintf(
			"%sHTTP method %q on non-resource path %q: only GET and HEAD are permitted on discovery paths.%s",
			policyPrefix, r.Method, info.Path, boundaryNotice))
	}
	if isDiscoveryPath(info.Path) {
		return nil
	}
	return forbidden(ReasonDiscoveryPathBlocked, fmt.Sprintf(
		"%snon-resource path %q: only Kubernetes discovery and health paths are permitted.%s",
		policyPrefix, info.Path, boundaryNotice))
}

// evaluateSubresource decides a subresource that neither mints credentials nor upgrades.
func (p Policy) evaluateSubresource(r *http.Request, info *request.RequestInfo, sub string) *PolicyError {
	if readVerbs.Has(info.Verb) {
		return nil
	}
	allowed := false
	switch sub {
	case subStatus, subScale:
		allowed = dryRunVerbs.Has(info.Verb) &&
			(p.Mode == ModeReadWrite || (p.Mode == ModeReadOnlyWithDryRun && HasStrictDryRunAll(r)))
	case subLog:
	case subEviction:
		allowed = p.Mode == ModeReadWrite && info.Verb == verbCreate
	default:
		allowed = p.Mode == ModeReadWrite && readWriteVerbs.Has(info.Verb)
	}
	if allowed {
		return nil
	}
	return forbidden(ReasonSubresourceBlocked, p.refusal(fmt.Sprintf(
		"%sverb %q on subresource \"%s/%s\": this subresource operation is not permitted.",
		policyPrefix, info.Verb, info.Resource, info.Subresource), info))
}

// refusal ends base with the read-only markers when a read-only mode refuses a write, or
// the plain boundary notice otherwise.
func (p Policy) refusal(base string, info *request.RequestInfo) string {
	verb := kubectlVerb(info)
	if p.Mode == ModeReadWrite || !refusalVerbs.Has(verb) {
		return base + boundaryNotice
	}
	return base + readOnlyNotice + policyVerbLabel + verb
}

// kubectlVerb names the kubectl command a refused request most likely came from, for the
// `policy verb:` marker.
func kubectlVerb(info *request.RequestInfo) string {
	if info.Verb == "" || readVerbs.Has(info.Verb) {
		return info.Verb
	}
	switch strings.ToLower(info.Subresource) {
	case subScale:
		return kubectlScale
	case subApproval:
		return kubectlCertificateApprove
	case subEviction:
		return kubectlDrain
	}
	switch info.Verb {
	case verbUpdate:
		return kubectlReplace
	case verbDeleteCollection:
		return verbDelete
	default:
		return info.Verb
	}
}

func isDiscoveryPath(p string) bool {
	if discoveryPaths.Has(p) {
		return true
	}
	if strings.HasPrefix(p, openAPIV3Prefix) && len(p) > len(openAPIV3Prefix) {
		return true
	}
	parts := strings.Split(strings.TrimPrefix(p, "/"), "/")
	switch len(parts) {
	case groupDiscoveryLen:
		return (parts[0] == apiPrefix || parts[0] == apisPrefix) && discoverySegment.MatchString(parts[1])
	case versionDiscoverLen:
		return parts[0] == apisPrefix && discoverySegment.MatchString(parts[1]) && discoverySegment.MatchString(parts[2])
	}
	return false
}

func isCoreV1Secret(info *request.RequestInfo) bool {
	return info.IsResourceRequest && info.APIGroup == coreGroup && info.APIVersion == coreVersion && info.Resource == secretsResource
}

// HasStrictDryRunAll reports whether r's query carries dryRun=All exactly once and nothing
// the API server could read differently. url.ParseQuery already rejects ';'; the explicit
// check keeps that true if parsing is ever relaxed.
func HasStrictDryRunAll(r *http.Request) bool {
	if r == nil || r.URL == nil || strings.Contains(r.URL.RawQuery, ";") {
		return false
	}
	q, err := url.ParseQuery(r.URL.RawQuery)
	if err != nil {
		return false
	}
	vals := q[dryRunParam]
	return len(vals) == 1 && vals[0] == dryRunAll
}

func forbidden(reason RejectionReason, message string) *PolicyError {
	return &PolicyError{
		Code:            http.StatusForbidden,
		Reason:          metav1.StatusReasonForbidden,
		RejectionReason: reason,
		Message:         message,
	}
}

func invalidRoute(detail string) *PolicyError {
	return &PolicyError{
		Code:            http.StatusBadRequest,
		Reason:          metav1.StatusReasonBadRequest,
		RejectionReason: ReasonInvalidRoute,
		Message:         messagePrefix + detail,
	}
}

func upgradeBlocked() *PolicyError {
	return forbidden(ReasonProtocolUpgradeBlocked,
		policyPrefix+"protocol upgrade request: interactive streaming subresources are not permitted."+boundaryNotice)
}
