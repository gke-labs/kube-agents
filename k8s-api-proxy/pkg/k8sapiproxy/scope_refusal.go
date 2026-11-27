package k8sapiproxy

import "net/http"

// client-go's discovery client (kubectl cluster-info, kubectl api-resources, the first
// request of most kubectl commands) asks /api and /apis for an aggregated discovery
// document and can't decode a JSON Status there: it reports the refusal as "unknown". A
// text/plain body it does carry: for a 403 the body text becomes the error's message
// (client-go rest/request.go, isTextResponse).
const contentTypeTextPlain = "text/plain; charset=utf-8"

// scopeRefusalReasons are the install-wide cluster refusals answered before the request
// is classified (spec.scope.exclude.clusters and spec.hold).
var scopeRefusalReasons = map[RejectionReason]bool{
	ReasonExcludedCluster:   true,
	ReasonExcludeUnreadable: true,
	ReasonHeldCluster:       true,
	ReasonHoldUnreadable:    true,
}

// refuseScope answers a scope refusal: as plain text on a discovery path (isDiscoveryPath:
// /api, /api/<v>, /apis, /apis/<g>, /apis/<g>/<v>, /version, /openapi/...), so kubectl
// shows the message rather than "unknown", and as the usual JSON Status elsewhere. The
// audit and the metrics read the event either way.
func refuseScope(w http.ResponseWriter, ev *AuditEvent, perr *PolicyError, apiPath string) {
	if !scopeRefusalReasons[perr.RejectionReason] {
		refuse(w, ev, perr)
		return
	}
	refuseReadably(w, ev, perr, apiPath)
}

// refuseReadably answers a 403 in the form kubectl can show on apiPath: plain text on a
// discovery path, the JSON Status elsewhere. It is for a refusal that turns the caller away
// whatever the request asks (a scope refusal, a Config.Authorize refusal), which a caller
// meets first on discovery.
func refuseReadably(w http.ResponseWriter, ev *AuditEvent, perr *PolicyError, apiPath string) {
	if !isDiscoveryPath(apiPath) {
		refuse(w, ev, perr)
		return
	}
	ev.Outcome, ev.Reason = OutcomeRefused, perr.RejectionReason
	w.Header().Set(headerContentType, contentTypeTextPlain)
	w.Header().Set(headerNoSniff, noSniff)
	w.WriteHeader(perr.Code)
	_, _ = w.Write([]byte(perr.Message + "\n")) // #nosec G705 -- served as text/plain with nosniff, never HTML
}
