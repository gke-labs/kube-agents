package k8sapiproxy

import (
	"encoding/json"
	"net/http"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	statusKind        = "Status"
	statusAPIVersion  = "v1"
	headerContentType = "Content-Type"
	contentTypeJSON   = "application/json"
	headerNoSniff     = "X-Content-Type-Options"
	noSniff           = "nosniff"
	minStatusCode     = 100
	maxStatusCode     = 599
)

// writeStatus writes a refusal as the metav1.Status kubectl expects from an API server, so
// kubectl prints message as the error.
func writeStatus(w http.ResponseWriter, code int, reason metav1.StatusReason, message string) {
	code, body := statusBody(code, reason, message)
	w.Header().Set(headerContentType, contentTypeJSON)
	w.Header().Set(headerNoSniff, noSniff)
	w.WriteHeader(code)
	_, _ = w.Write(body) // #nosec G705 -- a JSON Status served as application/json with nosniff, never HTML
}

// statusBody returns a failure metav1.Status as JSON, with code clamped to a valid HTTP status.
func statusBody(code int, reason metav1.StatusReason, message string) (int, []byte) {
	if code < minStatusCode || code > maxStatusCode {
		code = http.StatusInternalServerError
	}
	// Marshalling a metav1.Status cannot fail: it holds only strings, integers and pointers.
	body, _ := json.Marshal(metav1.Status{
		TypeMeta: metav1.TypeMeta{Kind: statusKind, APIVersion: statusAPIVersion},
		Status:   metav1.StatusFailure,
		Message:  message,
		Reason:   reason,
		Code:     int32(code), // #nosec G115 -- clamped to [minStatusCode, maxStatusCode] above
	})
	return code, body
}

func writePolicyError(w http.ResponseWriter, e *PolicyError) {
	writeStatus(w, e.Code, e.Reason, e.Message)
}
