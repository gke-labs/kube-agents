package k8sapiproxy

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"os"
	"slices"
	"time"

	"go.opentelemetry.io/otel/trace"
	"k8s.io/apiserver/pkg/endpoints/request"
)

// AuditEventType is the structured event_type value emitted on every Kubernetes API audit log record.
const AuditEventType = "k8s_api_audit"

// AuditEvent describes one Kubernetes API request processed by the proxy.
// Request bodies, response bodies, and authentication tokens are excluded.
type AuditEvent struct {
	Timestamp       time.Time
	ClusterID       string
	Caller          *CallerIdentity
	HTTPMethod      string
	Path            string
	UserAgent       string
	Verb            string
	APIGroup        string
	APIVersion      string
	Resource        string
	Subresource     string
	Namespace       string
	Name            string
	Mode            Mode
	DryRun          bool
	SecretRedacted  bool
	Outcome         string
	RejectionReason RejectionReason
	StatusCode      int
	Duration        time.Duration
	TraceID         string
	SpanID          string
}

// AuditSink records structured Kubernetes API audit events.
// Implementations must be safe for concurrent use by multiple goroutines.
type AuditSink interface {
	Record(ctx context.Context, event AuditEvent)
}

// AuditSinkFunc adapts a function to the AuditSink interface.
type AuditSinkFunc func(ctx context.Context, event AuditEvent)

// Record calls f(ctx, event).
func (f AuditSinkFunc) Record(ctx context.Context, event AuditEvent) {
	if f != nil {
		f(ctx, event)
	}
}

type noopAuditSink struct{}

func (noopAuditSink) Record(context.Context, AuditEvent) {}

// NoopAuditSink returns an AuditSink that discards all audit events.
func NoopAuditSink() AuditSink {
	return noopAuditSink{}
}

type multiAuditSink struct {
	sinks []AuditSink
}

func (m multiAuditSink) Record(ctx context.Context, event AuditEvent) {
	for _, s := range m.sinks {
		s.Record(ctx, cloneAuditEvent(event))
	}
}

// NewMultiAuditSink returns an AuditSink that forwards each AuditEvent to all non-nil sinks.
func NewMultiAuditSink(sinks ...AuditSink) AuditSink {
	filtered := make([]AuditSink, 0, len(sinks))
	for _, s := range sinks {
		if s != nil {
			filtered = append(filtered, s)
		}
	}
	if len(filtered) == 0 {
		return NoopAuditSink()
	}
	if len(filtered) == 1 {
		return filtered[0]
	}
	return multiAuditSink{sinks: filtered}
}

// NewAuditLogger constructs a structured JSON slog.Logger for Kubernetes API audit events.
// Request and response bodies and authentication credentials are never written to the audit log.
func NewAuditLogger(w io.Writer) *slog.Logger {
	if w == nil {
		w = os.Stdout
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
}

// NewJSONAuditSink constructs an AuditSink that writes newline-delimited JSON audit records to w.
// If w is nil, it writes to os.Stdout.
func NewJSONAuditSink(w io.Writer) AuditSink {
	return NewSlogAuditSink(NewAuditLogger(w))
}

type slogAuditSink struct {
	logger *slog.Logger
}

// NewSlogAuditSink wraps a structured slog.Logger as an AuditSink.
// If logger is nil, it writes JSON records to os.Stdout.
func NewSlogAuditSink(logger *slog.Logger) AuditSink {
	if logger == nil {
		logger = NewAuditLogger(os.Stdout)
	}
	return slogAuditSink{logger: logger}
}

func (s slogAuditSink) Record(ctx context.Context, event AuditEvent) {
	if s.logger == nil {
		return
	}

	attrs := make([]slog.Attr, 0, 20)
	attrs = append(attrs,
		slog.String("event_type", AuditEventType),
		slog.String("mode", sanitizeModeLabel(event.Mode)),
		slog.String("outcome", event.Outcome),
		slog.Int("status_code", event.StatusCode),
		slog.String("http_method", event.HTTPMethod),
		slog.String("path", event.Path),
		slog.String("verb", event.Verb),
		slog.String("resource", event.Resource),
		slog.Bool("dry_run", event.DryRun),
		slog.Bool("secret_redacted", event.SecretRedacted),
		slog.Int64("duration_ms", event.Duration.Milliseconds()),
	)

	if event.ClusterID != "" {
		attrs = append(attrs, slog.String("cluster", event.ClusterID))
	}
	if event.APIGroup != "" {
		attrs = append(attrs, slog.String("api_group", event.APIGroup))
	}
	if event.APIVersion != "" {
		attrs = append(attrs, slog.String("api_version", event.APIVersion))
	}
	if event.Subresource != "" {
		attrs = append(attrs, slog.String("subresource", event.Subresource))
	}
	if event.Namespace != "" {
		attrs = append(attrs, slog.String("namespace", event.Namespace))
	}
	if event.Name != "" {
		attrs = append(attrs, slog.String("name", event.Name))
	}
	if event.RejectionReason != ReasonNone {
		attrs = append(attrs, slog.String("rejection_reason", string(event.RejectionReason)))
	}
	if event.Caller != nil {
		if event.Caller.Username != "" {
			attrs = append(attrs, slog.String("caller_username", event.Caller.Username))
		}
		if event.Caller.UID != "" {
			attrs = append(attrs, slog.String("caller_uid", event.Caller.UID))
		}
		if len(event.Caller.Groups) > 0 {
			attrs = append(attrs, slog.Any("caller_groups", event.Caller.Groups))
		}
	}
	if event.UserAgent != "" {
		attrs = append(attrs, slog.String("user_agent", event.UserAgent))
	}
	if event.TraceID != "" {
		attrs = append(attrs, slog.String("trace_id", event.TraceID))
	}
	if event.SpanID != "" {
		attrs = append(attrs, slog.String("span_id", event.SpanID))
	}

	level := slog.LevelInfo
	switch event.Outcome {
	case OutcomeRejectedAuth, OutcomeRejectedRoute, OutcomeRejectedPolicy:
		level = slog.LevelWarn
	case OutcomeUpstreamError:
		level = slog.LevelError
	}

	s.logger.LogAttrs(ctx, level, "kube-proxy audit", attrs...)
}

type auditEntry struct {
	caller         *CallerIdentity
	clusterID      string
	httpMethod     string
	path           string
	userAgent      string
	info           *request.RequestInfo
	mode           Mode
	dryRun         bool
	secretRedacted bool
	outcome        string
	reason         RejectionReason
	statusCode     int
	duration       time.Duration
	span           trace.Span
}

func cloneCallerIdentity(caller *CallerIdentity) *CallerIdentity {
	if caller == nil {
		return nil
	}
	return &CallerIdentity{
		Username: caller.Username,
		UID:      caller.UID,
		Groups:   slices.Clone(caller.Groups),
	}
}

func cloneAuditEvent(event AuditEvent) AuditEvent {
	event.Caller = cloneCallerIdentity(event.Caller)
	return event
}

func logAuditEvent(ctx context.Context, sink AuditSink, entry auditEntry) {
	if sink == nil {
		return
	}

	verb := verbFromHTTPMethod(entry.httpMethod)
	resource := "unknown"
	subresource := ""
	apiGroup := ""
	apiVersion := ""
	namespace := ""
	name := ""

	if entry.info != nil {
		if !entry.info.IsResourceRequest {
			verb = "discovery"
			resource = "discovery"
		} else {
			if entry.info.Verb != "" {
				verb = entry.info.Verb
			}
			if entry.info.Resource != "" {
				resource = entry.info.Resource
			}
			subresource = entry.info.Subresource
			apiGroup = entry.info.APIGroup
			apiVersion = entry.info.APIVersion
			namespace = entry.info.Namespace
			name = entry.info.Name
		}
	}

	var traceID, spanID string
	if entry.span != nil {
		sc := entry.span.SpanContext()
		if sc.IsValid() {
			traceID = sc.TraceID().String()
			spanID = sc.SpanID().String()
		}
	}

	event := AuditEvent{
		Timestamp:       time.Now().UTC(),
		ClusterID:       entry.clusterID,
		Caller:          cloneCallerIdentity(entry.caller),
		HTTPMethod:      entry.httpMethod,
		Path:            entry.path,
		UserAgent:       entry.userAgent,
		Verb:            verb,
		APIGroup:        apiGroup,
		APIVersion:      apiVersion,
		Resource:        resource,
		Subresource:     subresource,
		Namespace:       namespace,
		Name:            name,
		Mode:            entry.mode,
		DryRun:          entry.dryRun,
		SecretRedacted:  entry.secretRedacted,
		Outcome:         entry.outcome,
		RejectionReason: entry.reason,
		StatusCode:      entry.statusCode,
		Duration:        entry.duration,
		TraceID:         traceID,
		SpanID:          spanID,
	}

	sink.Record(ctx, event)
}

func auditPathForRequest(r *http.Request, cleanPath string) string {
	if cleanPath != "" {
		return cleanPath
	}
	if r != nil && r.URL != nil {
		return r.URL.Path
	}
	return ""
}
