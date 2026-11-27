package k8sapiproxy

import (
	"errors"
	"fmt"
	"net/http"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	// ReasonAuthorizeRefused is the rejection reason of a Config.Authorize refusal that names
	// none of its own.
	ReasonAuthorizeRefused RejectionReason = "authorize.refused"

	// authorizeRefusedMessage is what the caller reads when Config.Authorize refuses without
	// an AuthorizeError that carries a message. The hook's own error text can describe the
	// embedder's setup, so it is not the caller's to read.
	authorizeRefusedMessage = messagePrefix + "the caller is not authorized for this request." + boundaryNotice

	// authorizePanicFormat is the audit event's error for a hook that panicked. The panic
	// value goes there and never to the caller.
	authorizePanicFormat = "authorize hook panicked: %v"
)

// errAuthorizeNilError stands in the audit event for a hook that returned a nil
// *AuthorizeError as its error: a non-nil error, so a refusal, with nothing to read from.
var errAuthorizeNilError = errors.New("authorize hook returned a nil *AuthorizeError")

// AuthorizeAttributes is what Config.Authorize decides on: one authenticated request, after
// its route and its API classification are known.
type AuthorizeAttributes struct {
	// Request is the caller's request as the authenticator left it: its headers include the
	// ones the handler strips before forwarding (Config.ExtraCallerHeaders among them), and
	// its URL still has the /clusters/<id> prefix. Its Body is empty: the body is the
	// upstream's to read. Changing the request does not change what is forwarded.
	Request *http.Request
	// Caller is the identity the authenticator returned; never nil. It is the hook's own
	// copy: changing it changes neither the audit event nor the caller's partition.
	Caller *CallerIdentity
	// ClusterID is the cluster the path names, or Config.DefaultClusterID for a path that
	// names none.
	ClusterID string
	// RequestInfo is the API server's classification of the request (verb, API group,
	// resource, namespace, name; Path is the API path without the /clusters/<id> prefix). It
	// is the hook's own copy, Parts included.
	RequestInfo *request.RequestInfo
}

// AuthorizeError is the refusal a Config.Authorize hook returns to choose what the audit
// record and the caller are told. The handler finds it with errors.As, so it may be wrapped.
type AuthorizeError struct {
	// Reason is the refusal's stable id in the audit record (rejection_reason) and the trace:
	// one of a small fixed set the embedder defines, never text built from the request.
	// ReasonAuthorizeRefused when empty. The rejection metric counts a reason the package
	// does not define under its catch-all label, so the set of series stays closed.
	Reason RejectionReason
	// Message is sent to the caller as the Status message, verbatim, so it must hold nothing
	// the caller should not read. A fixed message when empty.
	Message string
}

// Error is safe on a nil receiver: a hook that returns a nil *AuthorizeError as an error
// has returned a non-nil error, which the handler and the audit sink go on to read.
func (e *AuthorizeError) Error() string {
	if e == nil || e.Message == "" {
		return authorizeRefusedMessage
	}
	return e.Message
}

// runAuthorize calls the hook and returns its refusal, nil when it admits the request, with
// the cause for the audit event. The cause is the hook's error text in an error of the
// handler's own, so nothing downstream calls a method on a value the hook made: an error
// whose Error panics, such as a nil pointer of the embedder's type, would otherwise panic
// the audit sink and leave the refusal unrecorded. A hook that panics, in its own code or
// in its error's methods, has not admitted anything: the panic becomes a refusal recorded
// like any other.
func runAuthorize(hook func(AuthorizeAttributes) error, attrs AuthorizeAttributes) (perr *PolicyError, cause error) {
	defer func() {
		if p := recover(); p != nil {
			perr, cause = newAuthorizeRefusal(), fmt.Errorf(authorizePanicFormat, p)
		}
	}()
	err := hook(attrs)
	if err == nil {
		return nil, nil
	}
	refusal := newAuthorizeRefusal()
	var aerr *AuthorizeError
	if errors.As(err, &aerr) {
		if aerr == nil {
			return refusal, errAuthorizeNilError
		}
		if aerr.Reason != "" {
			refusal.RejectionReason = aerr.Reason
		}
		if aerr.Message != "" {
			refusal.Message = aerr.Message
		}
	}
	return refusal, errors.New(err.Error())
}

// newAuthorizeRefusal is the 403 a Config.Authorize refusal gets unless an AuthorizeError
// says otherwise. Only an AuthorizeError's fields reach the caller; any other error's text
// stays in the audit event.
func newAuthorizeRefusal() *PolicyError {
	return &PolicyError{
		Code:            http.StatusForbidden,
		Reason:          metav1.StatusReasonForbidden,
		RejectionReason: ReasonAuthorizeRefused,
		Message:         authorizeRefusedMessage,
	}
}
