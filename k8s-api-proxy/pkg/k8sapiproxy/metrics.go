package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"golang.org/x/net/netutil"
	"k8s.io/apiserver/pkg/endpoints/request"
)

const (
	// MetricsPath is the HTTP path served by the metrics-only listener.
	MetricsPath = "/metrics"

	metricsNamespace = "kubeagents"
	metricsSubsystem = "kube_proxy"

	requestsTotalName           = "requests_total"
	rejectedRequestsTotalName   = "rejected_requests_total"
	secretsRedactedTotalName    = "secrets_redacted_total"
	requestDurationSecondsName  = "request_duration_seconds"
	inFlightRequestsName        = "in_flight_requests"
	processStartTimeSecondsName = "process_start_time_seconds"

	labelVerb       = "verb"
	labelResource   = "resource"
	labelSubresource = "subresource"
	labelMode       = "mode"
	labelDryRun     = "dry_run"
	labelOutcome    = "outcome"
	labelStatusCode = "status_code"
	labelReason     = "reason"
	labelAction     = "action"

	// OutcomeAllowed indicates that the request passed policy checks and received a non-5xx upstream response.
	OutcomeAllowed = "allowed"
	// OutcomeRejectedAuth indicates that caller authentication rejected the request.
	OutcomeRejectedAuth = "rejected_auth"
	// OutcomeRejectedPolicy indicates that the L7 Kubernetes API policy rejected the request.
	OutcomeRejectedPolicy = "rejected_policy"
	// OutcomeRejectedRoute indicates that URL normalization or RequestInfo parsing rejected the request.
	OutcomeRejectedRoute = "rejected_route"
	// OutcomeUpstreamError indicates that upstream transport failed or returned a 5xx response.
	OutcomeUpstreamError = "upstream_error"

	// SecretActionAcceptHeaderRewritten counts requests where Accept was rewritten to PartialObjectMetadata.
	SecretActionAcceptHeaderRewritten = "accept_header_rewritten"
	// SecretActionPayloadScrubbed counts responses where Secret JSON payload scrubbing ran.
	SecretActionPayloadScrubbed = "payload_scrubbed"

	metricsLabelOther = "other"
	metricsLabelNone  = "none"

	metricsPortMin              = 1
	metricsPortMax              = 65535
	metricsMaxConnections       = 16
	metricsMaxRequestsInFlight  = metricsMaxConnections
	metricsConnectionDeadline   = 10 * time.Second
	metricsScrapeTimeout        = 5 * time.Second
	metricsIdleTimeout          = 10 * time.Second
	metricsShutdownGrace        = 5 * time.Second
)

var (
	knownVerbs = map[string]struct{}{
		"get":              {},
		"list":             {},
		"watch":            {},
		"create":           {},
		"update":           {},
		"patch":            {},
		"delete":           {},
		"deletecollection": {},
		"discovery":        {},
		"healthz":          {},
	}

	knownResources = map[string]struct{}{
		"pods":                      {},
		"nodes":                     {},
		"services":                  {},
		"deployments":               {},
		"replicasets":               {},
		"statefulsets":              {},
		"daemonsets":                {},
		"jobs":                      {},
		"cronjobs":                  {},
		"configmaps":                {},
		"secrets":                   {},
		"events":                    {},
		"namespaces":                {},
		"ingresses":                 {},
		"networkpolicies":           {},
		"persistentvolumeclaims":    {},
		"persistentvolumes":         {},
		"serviceaccounts":           {},
		"roles":                     {},
		"rolebindings":              {},
		"clusterroles":              {},
		"clusterrolebindings":       {},
		"customresourcedefinitions": {},
		"horizontalpodautoscalers":  {},
		"poddisruptionbudgets":      {},
		"endpoints":                 {},
		"endpointslices":            {},
		"leases":                    {},
		"tokenreviews":              {},
		"selfsubjectreviews":        {},
		"subjectaccessreviews":      {},
		"selfsubjectaccessreviews":  {},
		"localsubjectaccessreviews": {},
		"selfsubjectrulesreviews":   {},
		"discovery":                 {},
		"healthz":                   {},
		"unknown":                   {},
	}

	knownSubresources = map[string]struct{}{
		"none":                {},
		"log":                 {},
		"status":              {},
		"scale":               {},
		"ephemeralcontainers": {},
		"exec":                {},
		"attach":              {},
		"portforward":         {},
		"proxy":               {},
		"token":               {},
		"eviction":            {},
		"binding":             {},
		"approval":            {},
	}

	knownRejectionReasons = map[RejectionReason]struct{}{
		ReasonUnauthenticated:          {},
		ReasonProtocolUpgradeBlocked:   {},
		ReasonInvalidRoute:             {},
		ReasonDiscoveryPathBlocked:     {},
		ReasonAuthResourceBlocked:      {},
		ReasonSecretSubresourceBlocked: {},
		ReasonSubresourceBlocked:       {},
		ReasonDryRunRequired:           {},
		ReasonVerbBlocked:              {},
	}

	knownOutcomes = map[string]struct{}{
		OutcomeAllowed:        {},
		OutcomeRejectedAuth:   {},
		OutcomeRejectedPolicy: {},
		OutcomeRejectedRoute:  {},
		OutcomeUpstreamError:  {},
	}

	durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30}
)

// Metrics holds the Prometheus collectors and private registry for kube-proxy.
type Metrics struct {
	registry         *prometheus.Registry
	requestsTotal    *prometheus.CounterVec
	rejectedTotal    *prometheus.CounterVec
	secretsRedacted  *prometheus.CounterVec
	requestDuration  *prometheus.HistogramVec
	inFlightRequests prometheus.Gauge
	processStart     prometheus.Gauge
}

// NewMetrics creates a new Metrics instance with an isolated Prometheus registry.
func NewMetrics() *Metrics {
	reg := prometheus.NewRegistry()
	m := &Metrics{
		registry: reg,
		requestsTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      requestsTotalName,
			Help:      "Total Kubernetes API requests processed by kube-proxy, labeled by verb, resource, subresource, mode, dry_run, outcome, and status_code.",
		}, []string{labelVerb, labelResource, labelSubresource, labelMode, labelDryRun, labelOutcome, labelStatusCode}),
		rejectedTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      rejectedRequestsTotalName,
			Help:      "Total Kubernetes API requests rejected by kube-proxy before reaching kube-apiserver, labeled by reason, verb, resource, subresource, and mode.",
		}, []string{labelReason, labelVerb, labelResource, labelSubresource, labelMode}),
		secretsRedacted: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      secretsRedactedTotalName,
			Help:      "Total core/v1 Secret redaction operations performed by kube-proxy, labeled by action.",
		}, []string{labelAction}),
		requestDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      requestDurationSecondsName,
			Help:      "Duration of Kubernetes API requests processed by kube-proxy in seconds, labeled by verb, resource, and outcome.",
			Buckets:   durationBuckets,
		}, []string{labelVerb, labelResource, labelOutcome}),
		inFlightRequests: prometheus.NewGauge(prometheus.GaugeOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      inFlightRequestsName,
			Help:      "Number of Kubernetes API requests currently in flight through kube-proxy.",
		}),
		processStart: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: processStartTimeSecondsName,
			Help: "Start time of the process since unix epoch in seconds, captured once at start.",
		}),
	}
	m.processStart.Set(float64(time.Now().UnixNano()) / float64(time.Second))
	reg.MustRegister(
		m.requestsTotal,
		m.rejectedTotal,
		m.secretsRedacted,
		m.requestDuration,
		m.inFlightRequests,
		m.processStart,
	)
	m.secretsRedacted.WithLabelValues(SecretActionAcceptHeaderRewritten)
	m.secretsRedacted.WithLabelValues(SecretActionPayloadScrubbed)
	return m
}

// Handler returns an HTTP handler that serves the Prometheus exposition format.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{
		MaxRequestsInFlight: metricsMaxRequestsInFlight,
		Timeout:             metricsScrapeTimeout,
	})
}

func (m *Metrics) incInFlight() {
	if m == nil {
		return
	}
	m.inFlightRequests.Inc()
}

func (m *Metrics) decInFlight() {
	if m == nil {
		return
	}
	m.inFlightRequests.Dec()
}

func (m *Metrics) recordSecretRedaction(action string) {
	if m == nil {
		return
	}
	switch action {
	case SecretActionAcceptHeaderRewritten, SecretActionPayloadScrubbed:
		m.secretsRedacted.WithLabelValues(action).Inc()
	default:
		m.secretsRedacted.WithLabelValues(metricsLabelOther).Inc()
	}
}

func (m *Metrics) recordRequest(verb, resource, subresource string, mode Mode, dryRun bool, outcome string, statusCode int, duration time.Duration) {
	if m == nil {
		return
	}
	vLabel := sanitizeVerbLabel(verb)
	rLabel := sanitizeResourceLabel(resource)
	sLabel := sanitizeSubresourceLabel(subresource)
	mLabel := sanitizeModeLabel(mode)
	dLabel := "false"
	if dryRun {
		dLabel = "true"
	}
	oLabel := sanitizeOutcomeLabel(outcome)
	cLabel := sanitizeStatusCodeLabel(statusCode)

	m.requestsTotal.WithLabelValues(vLabel, rLabel, sLabel, mLabel, dLabel, oLabel, cLabel).Inc()
	m.requestDuration.WithLabelValues(vLabel, rLabel, oLabel).Observe(duration.Seconds())
}

func (m *Metrics) recordRejection(reason RejectionReason, verb, resource, subresource string, mode Mode) {
	if m == nil {
		return
	}
	reasonLabel := metricsLabelOther
	if _, ok := knownRejectionReasons[reason]; ok {
		reasonLabel = string(reason)
	}
	vLabel := sanitizeVerbLabel(verb)
	rLabel := sanitizeResourceLabel(resource)
	sLabel := sanitizeSubresourceLabel(subresource)
	mLabel := sanitizeModeLabel(mode)

	m.rejectedTotal.WithLabelValues(reasonLabel, vLabel, rLabel, sLabel, mLabel).Inc()
}

// ClassifyRequestLabels extracts bounded verb, resource, and subresource labels from an HTTP request and optional RequestInfo.
func ClassifyRequestLabels(r *http.Request, info *request.RequestInfo) (verb, resource, subresource string) {
	if info != nil {
		if !info.IsResourceRequest {
			return "discovery", "discovery", metricsLabelNone
		}
		v := sanitizeVerbLabel(info.Verb)
		res := sanitizeResourceLabel(info.Resource)
		sub := metricsLabelNone
		if info.Subresource != "" {
			sub = sanitizeSubresourceLabel(info.Subresource)
		}
		return v, res, sub
	}

	verb = verbFromHTTPMethod(r.Method)
	return verb, "unknown", metricsLabelNone
}

func verbFromHTTPMethod(method string) string {
	switch method {
	case http.MethodGet, http.MethodHead:
		return "get"
	case http.MethodPost:
		return "create"
	case http.MethodPut:
		return "update"
	case http.MethodPatch:
		return "patch"
	case http.MethodDelete:
		return "delete"
	default:
		return metricsLabelOther
	}
}

func sanitizeVerbLabel(verb string) string {
	if _, ok := knownVerbs[verb]; ok {
		return verb
	}
	return metricsLabelOther
}

func sanitizeResourceLabel(resource string) string {
	if _, ok := knownResources[resource]; ok {
		return resource
	}
	return metricsLabelOther
}

func sanitizeSubresourceLabel(subresource string) string {
	if subresource == "" {
		return metricsLabelNone
	}
	if _, ok := knownSubresources[subresource]; ok {
		return subresource
	}
	return metricsLabelOther
}

func sanitizeModeLabel(mode Mode) string {
	switch mode {
	case ModeReadOnly, ModeReadOnlyWithDryRun, ModeReadWrite:
		return string(mode)
	case "":
		return string(ModeReadOnlyWithDryRun)
	default:
		return metricsLabelOther
	}
}

func sanitizeOutcomeLabel(outcome string) string {
	if _, ok := knownOutcomes[outcome]; ok {
		return outcome
	}
	return metricsLabelOther
}

func sanitizeStatusCodeLabel(code int) string {
	if code >= 100 && code <= 599 {
		return strconv.Itoa(code)
	}
	return metricsLabelOther
}

// MetricsServer serves GET /metrics on a dedicated TCP port.
type MetricsServer struct {
	listen   string
	metrics  *Metrics
	log      *slog.Logger
	listener net.Listener
}

// NewMetricsServer constructs a bounded HTTP server for the metrics-only listener.
func NewMetricsServer(port int, metrics *Metrics, log *slog.Logger) (*MetricsServer, error) {
	if port < metricsPortMin || port > metricsPortMax {
		return nil, fmt.Errorf("metrics listener port %d is not in %d-%d", port, metricsPortMin, metricsPortMax)
	}
	if metrics == nil {
		return nil, errors.New("metrics listener requires a non-nil Metrics instance")
	}
	if log == nil {
		log = slog.Default()
	}
	return &MetricsServer{
		listen:  fmt.Sprintf(":%d", port),
		metrics: metrics,
		log:     log,
	}, nil
}

// Run starts the metrics HTTP server and blocks until ctx is canceled.
func (s *MetricsServer) Run(ctx context.Context) error {
	mux := http.NewServeMux()
	mux.Handle(http.MethodGet+" "+MetricsPath, s.metrics.Handler())
	srv := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: metricsConnectionDeadline,
		ReadTimeout:       metricsConnectionDeadline,
		WriteTimeout:      metricsConnectionDeadline,
		IdleTimeout:       metricsIdleTimeout,
	}

	ln := s.listener
	if ln == nil {
		var err error
		ln, err = net.Listen("tcp", s.listen)
		if err != nil {
			return fmt.Errorf("metrics listener on %s: %w", s.listen, err)
		}
	}
	ln = netutil.LimitListener(ln, metricsMaxConnections)
	s.log.Info("metrics listening", "address", ln.Addr().String(), "path", MetricsPath)

	errs := make(chan error, 1)
	go func() {
		err := srv.Serve(ln)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errs <- err
	}()

	select {
	case err := <-errs:
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), metricsShutdownGrace)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
		return nil
	}
}
