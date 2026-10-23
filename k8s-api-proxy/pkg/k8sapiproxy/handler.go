package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httputil"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apiserver/pkg/endpoints/request"
)

type contextKey int

const (
	clusterIDContextKey contextKey = iota
	requestInfoContextKey
	callerWantsPartialContextKey
)

// NewHandler constructs the Kubernetes API proxy HTTP handler.
func NewHandler(cfg Config) (http.Handler, error) {
	if cfg.Upstream == nil {
		return nil, errors.New("k8sapiproxy: Config.Upstream is required")
	}
	if cfg.Authenticator == nil {
		return nil, errors.New("k8sapiproxy: Config.Authenticator is required")
	}
	mode, err := ParseMode(string(cfg.Mode))
	if err != nil {
		return nil, err
	}
	defaultCluster := cfg.DefaultClusterID
	if defaultCluster == "" {
		defaultCluster = "in-cluster"
	}

	metrics := cfg.Metrics
	if metrics == nil {
		metrics = NewMetrics()
	}

	auditSink := cfg.AuditSink
	if auditSink == nil {
		auditSink = NewJSONAuditSink(os.Stdout)
	}

	factory := NewRequestInfoFactory()

	h := &proxyHandler{
		authenticator:            cfg.Authenticator,
		upstream:                 cfg.Upstream,
		defaultClusterID:         defaultCluster,
		mode:                     mode,
		redactSecrets:            cfg.RedactSecrets,
		allowUpgradeSubresources: cfg.AllowUpgradeSubresources,
		metrics:                  metrics,
		tracerProvider:           cfg.TracerProvider,
		auditSink:                auditSink,
		factory:                  factory,
	}

	h.reverseProxy = &httputil.ReverseProxy{
		FlushInterval: -1,
		Director: func(req *http.Request) {
			if req.URL.Scheme == "" {
				req.URL.Scheme = "https"
			}
			if req.URL.Host == "" {
				req.URL.Host = "kubernetes.default.svc"
			}
		},
		Transport: roundTripperFunc(func(req *http.Request) (*http.Response, error) {
			clusterID, _ := req.Context().Value(clusterIDContextKey).(string)
			upCtx, upSpan := startUpstreamSpan(req.Context(), h.tracerProvider, clusterID, req.Method)
			defer upSpan.End()

			resp, err := h.upstream.RoundTripCluster(upCtx, clusterID, req.WithContext(upCtx))
			if err != nil {
				upSpan.RecordError(err)
				upSpan.SetStatus(codes.Error, err.Error())
				return nil, err
			}
			upSpan.SetAttributes(attribute.Int("http.response.status_code", resp.StatusCode))
			if resp.StatusCode >= http.StatusInternalServerError {
				upSpan.SetStatus(codes.Error, http.StatusText(resp.StatusCode))
			} else {
				upSpan.SetStatus(codes.Ok, "")
			}
			return resp, nil
		}),
		ModifyResponse: func(resp *http.Response) error {
			if !h.redactSecrets {
				return nil
			}
			info, _ := resp.Request.Context().Value(requestInfoContextKey).(*request.RequestInfo)
			if IsCoreV1Secret(info) {
				callerWantsPartial, _ := resp.Request.Context().Value(callerWantsPartialContextKey).(bool)
				ScrubSecretResponse(resp, info, callerWantsPartial)
				h.metrics.recordSecretRedaction(SecretActionPayloadScrubbed)
			}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			WriteStatusError(
				w,
				http.StatusBadGateway,
				metav1.StatusReasonInternalError,
				fmt.Sprintf("kube-proxy upstream transport error: %v", err),
			)
		},
	}

	return h, nil
}

type proxyHandler struct {
	authenticator            CallerAuthenticator
	upstream                 UpstreamTransport
	defaultClusterID         string
	mode                     Mode
	redactSecrets            bool
	allowUpgradeSubresources bool
	metrics                  *Metrics
	tracerProvider           trace.TracerProvider
	auditSink                AuditSink
	factory                  *request.RequestInfoFactory
	reverseProxy             *httputil.ReverseProxy
}

func (h *proxyHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	if r.URL.Path == "/_kube_proxy/healthz" && (r.Method == http.MethodGet || r.Method == http.MethodHead) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
		h.metrics.recordRequest("healthz", "healthz", metricsLabelNone, h.mode, false, OutcomeAllowed, http.StatusOK, time.Since(start))
		return
	}

	h.metrics.incInFlight()
	defer h.metrics.decInFlight()

	dryRun := HasStrictDryRunAll(r)
	userAgent := r.UserAgent()
	spanCtx, span := startRequestSpan(h.tracerProvider, r, h.mode)
	r = r.WithContext(spanCtx)

	caller, err := h.authenticator.AuthenticateRequest(r)
	if err != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(r, nil)
		WriteStatusError(
			w,
			http.StatusUnauthorized,
			metav1.StatusReasonUnauthorized,
			fmt.Sprintf("kube-proxy caller authentication failed: %v", err),
		)
		duration := time.Since(start)
		h.metrics.recordRejection(ReasonUnauthenticated, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedAuth, http.StatusUnauthorized, duration)
		finishRequestSpan(span, OutcomeRejectedAuth, ReasonUnauthenticated, http.StatusUnauthorized)
		logAuditEvent(r.Context(), h.auditSink, auditEntry{
			httpMethod: r.Method,
			path:       auditPathForRequest(r, ""),
			userAgent:  userAgent,
			mode:       h.mode,
			dryRun:     dryRun,
			outcome:    OutcomeRejectedAuth,
			reason:     ReasonUnauthenticated,
			statusCode: http.StatusUnauthorized,
			duration:   duration,
			span:       span,
		})
		return
	}

	clusterID, cleanPath, policyErr := NormalizeAndExtractCluster(r.URL.RawPath, r.URL.Path, h.defaultClusterID)
	if policyErr != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(r, nil)
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		duration := time.Since(start)
		h.metrics.recordRejection(policyErr.RejectionReason, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedRoute, policyErr.Code, duration)
		finishRequestSpan(span, OutcomeRejectedRoute, policyErr.RejectionReason, policyErr.Code)
		logAuditEvent(r.Context(), h.auditSink, auditEntry{
			caller:     caller,
			httpMethod: r.Method,
			path:       auditPathForRequest(r, ""),
			userAgent:  userAgent,
			mode:       h.mode,
			dryRun:     dryRun,
			outcome:    OutcomeRejectedRoute,
			reason:     policyErr.RejectionReason,
			statusCode: policyErr.Code,
			duration:   duration,
			span:       span,
		})
		return
	}

	outReq := r.Clone(r.Context())
	outReq.URL.Path = cleanPath
	outReq.URL.RawPath = ""

	info, err := h.factory.NewRequestInfo(outReq)
	if err != nil {
		verbLabel, resLabel, subLabel := ClassifyRequestLabels(outReq, nil)
		WriteStatusError(
			w,
			http.StatusBadRequest,
			metav1.StatusReasonBadRequest,
			fmt.Sprintf("kube-proxy failed to parse Kubernetes API request path %q: %v", cleanPath, err),
		)
		duration := time.Since(start)
		h.metrics.recordRejection(ReasonInvalidRoute, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedRoute, http.StatusBadRequest, duration)
		finishRequestSpan(span, OutcomeRejectedRoute, ReasonInvalidRoute, http.StatusBadRequest)
		logAuditEvent(r.Context(), h.auditSink, auditEntry{
			caller:     caller,
			clusterID:  clusterID,
			httpMethod: r.Method,
			path:       cleanPath,
			userAgent:  userAgent,
			mode:       h.mode,
			dryRun:     dryRun,
			outcome:    OutcomeRejectedRoute,
			reason:     ReasonInvalidRoute,
			statusCode: http.StatusBadRequest,
			duration:   duration,
			span:       span,
		})
		return
	}

	verbLabel, resLabel, subLabel := ClassifyRequestLabels(outReq, info)
	secretRedacted := h.redactSecrets && IsCoreV1Secret(info)
	annotateRequestSpanInfo(span, clusterID, info, dryRun, secretRedacted)

	if policyErr := EvaluatePolicyWithOptions(h.mode, h.redactSecrets, h.allowUpgradeSubresources, outReq, info); policyErr != nil {
		WriteStatusError(w, policyErr.Code, policyErr.Reason, policyErr.Message)
		duration := time.Since(start)
		h.metrics.recordRejection(policyErr.RejectionReason, verbLabel, resLabel, subLabel, h.mode)
		h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, OutcomeRejectedPolicy, policyErr.Code, duration)
		finishRequestSpan(span, OutcomeRejectedPolicy, policyErr.RejectionReason, policyErr.Code)
		logAuditEvent(r.Context(), h.auditSink, auditEntry{
			caller:         caller,
			clusterID:      clusterID,
			httpMethod:     r.Method,
			path:           cleanPath,
			userAgent:      userAgent,
			info:           info,
			mode:           h.mode,
			dryRun:         dryRun,
			secretRedacted: secretRedacted,
			outcome:        OutcomeRejectedPolicy,
			reason:         policyErr.RejectionReason,
			statusCode:     policyErr.Code,
			duration:       duration,
			span:           span,
		})
		return
	}

	_, isUpgradeSub := upgradeSubresources[strings.ToLower(info.Subresource)]
	preserveUpgrade := h.mode == ModeReadWrite && h.allowUpgradeSubresources && info.IsResourceRequest && isUpgradeSub
	SanitizeRequestHeadersWithOptions(outReq.Header, preserveUpgrade)
	AppendCallerUserAgent(outReq.Header, caller)

	var callerWantsPartial bool
	if secretRedacted {
		var acceptRewritten bool
		callerWantsPartial, acceptRewritten = RewriteSecretRequestHeadersWithStatus(outReq, info)
		if acceptRewritten {
			h.metrics.recordSecretRedaction(SecretActionAcceptHeaderRewritten)
		}
	}

	ctx := context.WithValue(outReq.Context(), clusterIDContextKey, clusterID)
	ctx = context.WithValue(ctx, requestInfoContextKey, info)
	ctx = context.WithValue(ctx, callerWantsPartialContextKey, callerWantsPartial)
	outReq = outReq.WithContext(ctx)

	rec := &statusRecorder{ResponseWriter: w, statusCode: http.StatusOK}
	h.reverseProxy.ServeHTTP(rec, outReq)

	outcome := OutcomeAllowed
	if rec.statusCode >= http.StatusInternalServerError {
		outcome = OutcomeUpstreamError
	}
	duration := time.Since(start)
	h.metrics.recordRequest(verbLabel, resLabel, subLabel, h.mode, dryRun, outcome, rec.statusCode, duration)
	finishRequestSpan(span, outcome, ReasonNone, rec.statusCode)
	logAuditEvent(r.Context(), h.auditSink, auditEntry{
		caller:         caller,
		clusterID:      clusterID,
		httpMethod:     r.Method,
		path:           cleanPath,
		userAgent:      userAgent,
		info:           info,
		mode:           h.mode,
		dryRun:         dryRun,
		secretRedacted: secretRedacted,
		outcome:        outcome,
		reason:         ReasonNone,
		statusCode:     rec.statusCode,
		duration:       duration,
		span:           span,
	})
}

type statusRecorder struct {
	http.ResponseWriter
	statusCode  int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.wroteHeader {
		r.statusCode = code
		r.wroteHeader = true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.wroteHeader {
		r.statusCode = http.StatusOK
		r.wroteHeader = true
	}
	return r.ResponseWriter.Write(b)
}

func (r *statusRecorder) Flush() {
	if flusher, ok := r.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (r *statusRecorder) Unwrap() http.ResponseWriter {
	return r.ResponseWriter
}

type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}
