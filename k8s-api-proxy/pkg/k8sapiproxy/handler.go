package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	authFailedMessage   = messagePrefix + "caller authentication failed"
	unclassifiedMessage = messagePrefix + "the request path could not be classified"
	upstreamFailed      = "kube-proxy could not reach cluster %q"

	// flushImmediately streams each upstream write to the caller, which watch and log
	// follow need.
	flushImmediately = -1
)

// Config configures NewHandler.
type Config struct {
	// Authenticator identifies the caller. Required.
	Authenticator CallerAuthenticator
	// Upstream forwards an admitted request to its cluster. Required.
	Upstream UpstreamTransport
	// DefaultClusterID is the cluster a path without a /clusters/{id} prefix targets;
	// InClusterID when empty.
	DefaultClusterID string
	// Policy decides which requests are forwarded. Its Mode must be set.
	Policy Policy
	// Audit receives an AuditEvent for every request; NewSlogAuditSink(Logger) when nil.
	Audit AuditSink
	// Logger receives the reverse proxy's own errors, such as a response body that failed to
	// copy; slog.Default() when nil.
	Logger *slog.Logger
}

// auditEventKey carries the request's *AuditEvent to the transport and the error handler.
type auditEventKey struct{}

type handler struct {
	authenticator    CallerAuthenticator
	defaultClusterID string
	policy           Policy
	audit            AuditSink
	factory          *request.RequestInfoFactory
	proxy            *httputil.ReverseProxy
}

// NewHandler returns the proxy's HTTP handler. Each request is authenticated, routed to a
// cluster by SplitClusterPath, classified, evaluated by cfg.Policy, stripped of the caller's
// credentials and identity headers, and forwarded through cfg.Upstream. With
// cfg.Policy.RedactSecrets, a Secret response carries no data or stringData values and no
// annotation holding a copy of them. Every refusal is a
// metav1.Status.
func NewHandler(cfg Config) (http.Handler, error) {
	if cfg.Authenticator == nil {
		return nil, errors.New("k8sapiproxy: Config.Authenticator is required")
	}
	if cfg.Upstream == nil {
		return nil, errors.New("k8sapiproxy: Config.Upstream is required")
	}
	if !cfg.Policy.Mode.valid() {
		return nil, fmt.Errorf("k8sapiproxy: invalid Config.Policy.Mode %q", cfg.Policy.Mode)
	}
	if cfg.DefaultClusterID != "" && !validClusterID.MatchString(cfg.DefaultClusterID) {
		return nil, fmt.Errorf("k8sapiproxy: invalid Config.DefaultClusterID %q", cfg.DefaultClusterID)
	}

	logger := cfg.Logger
	if logger == nil {
		logger = slog.Default()
	}
	audit := cfg.Audit
	if audit == nil {
		audit = NewSlogAuditSink(logger)
	}

	upstream := cfg.Upstream
	return &handler{
		authenticator:    cfg.Authenticator,
		defaultClusterID: cfg.DefaultClusterID,
		policy:           cfg.Policy,
		audit:            audit,
		factory:          NewRequestInfoFactory(),
		proxy: &httputil.ReverseProxy{
			// Rewrite rather than Director: the reverse proxy then also drops the caller's
			// Forwarded, X-Forwarded-For, X-Forwarded-Host and X-Forwarded-Proto headers. The
			// outbound URL keeps only the path and query, so a scheme, host or userinfo from an
			// absolute-form request URI never reaches the transport.
			Rewrite: func(pr *httputil.ProxyRequest) {
				pr.Out.Host = ""
				pr.Out.URL = &url.URL{Path: pr.In.URL.Path, RawQuery: pr.In.URL.RawQuery}
			},
			Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
				resp, err := upstream.RoundTripCluster(req.Context(), eventFrom(req).ClusterID, req)
				// Redacting here rather than in ModifyResponse keeps the plan off resp.Request,
				// which a transport may replace with a request of its own.
				if plan, ok := req.Context().Value(secretRedactionKey{}).(*secretRedaction); ok && resp != nil {
					redactSecretResponse(resp, plan)
				}
				return resp, err
			}),
			FlushInterval: flushImmediately,
			// The transport's error goes to the audit event, not to the caller: it can name
			// the upstream endpoint.
			ErrorHandler: func(w http.ResponseWriter, req *http.Request, err error) {
				ev := eventFrom(req)
				ev.Err = err
				switch {
				case req.Context().Err() != nil:
					ev.Outcome = OutcomeCanceled
				case isHijacked(w):
					// The upgrade failed after the connection was handed over, so there is no
					// response left to write.
					ev.Outcome = OutcomeAborted
					return
				default:
					ev.Outcome = OutcomeUpstreamError
				}
				writeStatus(w, http.StatusBadGateway, metav1.StatusReasonInternalError,
					fmt.Sprintf(upstreamFailed, ev.ClusterID))
			},
			ErrorLog: slog.NewLogLogger(logger.Handler(), slog.LevelError),
		},
	}, nil
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	rec := &statusRecorder{ResponseWriter: w}
	ev := &AuditEvent{HTTPMethod: r.Method, Path: r.URL.Path, UserAgent: r.UserAgent()}
	defer func() {
		// The reverse proxy panics with http.ErrAbortHandler when a response fails mid-copy;
		// the event is recorded before the panic carries on to the server.
		p := recover()
		switch {
		case p == nil:
		case p == http.ErrAbortHandler && r.Context().Err() != nil:
			ev.Outcome = OutcomeCanceled
		case p == http.ErrAbortHandler:
			ev.Outcome = OutcomeAborted
		default:
			ev.Err = fmt.Errorf("panic: %v", p)
		}
		ev.StatusCode, ev.Duration = rec.code, time.Since(start)
		h.audit.Record(r.Context(), *ev)
		if p != nil {
			panic(p)
		}
	}()
	h.serve(rec, r, ev)
}

func (h *handler) serve(w http.ResponseWriter, r *http.Request, ev *AuditEvent) {
	// The authenticator's error goes to the audit event, not to the caller, as the transport's
	// does: it can describe the proxy's own authentication setup.
	caller, err := h.authenticator.AuthenticateRequest(r)
	if err != nil || caller == nil {
		ev.Outcome, ev.Err = OutcomeUnauthenticated, err
		writeStatus(w, http.StatusUnauthorized, metav1.StatusReasonUnauthorized, authFailedMessage)
		return
	}
	ev.Caller = caller

	clusterID, apiPath, perr := SplitClusterPath(r.URL, h.defaultClusterID)
	if perr != nil {
		refuse(w, ev, perr)
		return
	}
	ev.ClusterID, ev.Path = clusterID, apiPath

	out := r.Clone(r.Context())
	out.URL.Path = apiPath
	out.URL.RawPath = ""

	info, err := h.factory.NewRequestInfo(out)
	if err != nil {
		ev.Outcome, ev.Reason = OutcomeRefused, ReasonInvalidRoute
		writeStatus(w, http.StatusBadRequest, metav1.StatusReasonBadRequest, unclassifiedMessage)
		return
	}
	ev.Verb, ev.APIGroup, ev.APIVersion = info.Verb, info.APIGroup, info.APIVersion
	ev.Resource, ev.Subresource, ev.Namespace, ev.Name = info.Resource, info.Subresource, info.Namespace, info.Name
	ev.DryRun = HasStrictDryRunAll(out)
	if perr := h.policy.Evaluate(out, info); perr != nil {
		refuse(w, ev, perr)
		return
	}

	sanitizeRequestHeaders(out.Header, h.policy.permitsUpgrade(info))
	ev.Outcome = OutcomeForwarded
	ctx := context.WithValue(out.Context(), auditEventKey{}, ev)
	if h.policy.RedactSecrets && isCoreV1Secret(info) {
		ctx = context.WithValue(ctx, secretRedactionKey{}, rewriteSecretRequest(out.Header, info))
	}
	out = out.WithContext(ctx)
	h.proxy.ServeHTTP(w, out)
}

func refuse(w http.ResponseWriter, ev *AuditEvent, perr *PolicyError) {
	ev.Outcome, ev.Reason = OutcomeRefused, perr.RejectionReason
	writePolicyError(w, perr)
}

func isHijacked(w http.ResponseWriter) bool {
	rec, ok := w.(*statusRecorder)
	return ok && rec.hijacked
}

// eventFrom returns the request's audit event, which ServeHTTP always sets before it
// forwards.
func eventFrom(req *http.Request) *AuditEvent {
	ev, _ := req.Context().Value(auditEventKey{}).(*AuditEvent)
	if ev == nil {
		ev = &AuditEvent{}
	}
	return ev
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
