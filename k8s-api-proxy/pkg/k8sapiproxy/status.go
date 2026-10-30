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
	if code < minStatusCode || code > maxStatusCode {
		code = http.StatusInternalServerError
	}
	body, err := json.Marshal(metav1.Status{
		TypeMeta: metav1.TypeMeta{Kind: statusKind, APIVersion: statusAPIVersion},
		Status:   metav1.StatusFailure,
		Message:  message,
		Reason:   reason,
		Code:     int32(code), // #nosec G115 -- clamped to [minStatusCode, maxStatusCode] above
	})
	if err != nil {
		http.Error(w, message, code)
		return
	}
	w.Header().Set(headerContentType, contentTypeJSON)
	w.Header().Set(headerNoSniff, noSniff)
	w.WriteHeader(code)
	_, _ = w.Write(body)
}

func writePolicyError(w http.ResponseWriter, e *PolicyError) {
	writeStatus(w, e.Code, e.Reason, e.Message)
}
