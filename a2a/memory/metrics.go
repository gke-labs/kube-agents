package memory

import (
	"net/http"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const (
	metricsNamespace = "kubeagents"
	metricsSubsystem = "curator"

	outcomesTotalName    = "outcomes_total"
	recordsGaugeName     = "records"
	graduationsTotalName = "graduations_total"
	queriesTotalName     = "queries_total"
	guardChecksTotalName = "guard_checks_total"
	processStartTimeName = "process_start_time_seconds"

	labelKind    = "kind"
	labelStatus  = "status"
	labelType    = "type"
	labelVerdict = "verdict"

	outcomeKindOther = "other"

	outcomeStatusProcessed = "processed"
	outcomeStatusDuplicate = "duplicate"
	outcomeStatusIgnored   = "ignored"
	outcomeStatusError     = "error"

	queryStatusHit   = "hit"
	queryStatusEmpty = "empty"
	queryStatusError = "error"

	guardVerdictAllow = "allow"
	guardVerdictWarn  = "warn"
	guardVerdictBlock = "block"

	metricsMaxRequestsInFlight = 16
	metricsScrapeTimeout       = 5 * time.Second
)

var (
	metricsOutcomeKinds = []string{
		string(OutcomeProposed),
		string(OutcomeApproved),
		string(OutcomeModifiedMerged),
		string(OutcomeResolvedMerged),
		string(OutcomePRMergedPersists),
		string(OutcomeRefused),
		string(OutcomeRetired),
		outcomeKindOther,
	}
	metricsOutcomeStatuses = []string{
		outcomeStatusProcessed,
		outcomeStatusDuplicate,
		outcomeStatusIgnored,
		outcomeStatusError,
	}
	metricsRecordTypes = []RecordType{
		RecordTypeDecision,
		RecordTypeLesson,
	}
	metricsRecordStatuses = []RecordStatus{
		StatusProposed,
		StatusActive,
		StatusRejected,
		StatusSuperseded,
		StatusRetired,
	}
	metricsQueryStatuses = []string{
		queryStatusHit,
		queryStatusEmpty,
		queryStatusError,
	}
	metricsGuardVerdicts = []string{
		guardVerdictAllow,
		guardVerdictWarn,
		guardVerdictBlock,
	}

	repoMetricsMu sync.RWMutex
	repoMetrics   = map[string][]*curatorMetrics{}
)

type curatorMetrics struct {
	registry     *prometheus.Registry
	outcomes     *prometheus.CounterVec
	graduations  prometheus.Counter
	queries      *prometheus.CounterVec
	guardChecks  *prometheus.CounterVec
	processStart prometheus.Gauge
}

type recordsCollector struct {
	repoDir string
	desc    *prometheus.Desc
}

func (rc *recordsCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- rc.desc
}

func (rc *recordsCollector) Collect(ch chan<- prometheus.Metric) {
	type key struct {
		rtype  RecordType
		status RecordStatus
	}
	counts := make(map[key]int, len(metricsRecordTypes)*len(metricsRecordStatuses))
	if records, err := LoadRecords(rc.repoDir); err == nil {
		for _, rec := range records {
			if !slices.Contains(metricsRecordTypes, rec.Meta.Type) || !slices.Contains(metricsRecordStatuses, rec.Meta.Status) {
				continue
			}
			counts[key{rtype: rec.Meta.Type, status: rec.Meta.Status}]++
		}
	}
	for _, rtype := range metricsRecordTypes {
		for _, status := range metricsRecordStatuses {
			ch <- prometheus.MustNewConstMetric(
				rc.desc,
				prometheus.GaugeValue,
				float64(counts[key{rtype: rtype, status: status}]),
				string(rtype),
				string(status),
			)
		}
	}
}

func newCuratorMetrics(repoDir string) *curatorMetrics {
	cleanDir := filepath.Clean(repoDir)
	m := &curatorMetrics{
		registry: prometheus.NewRegistry(),
		outcomes: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      outcomesTotalName,
			Help:      "Outcome events processed by the Curator, by outcome kind and processing status.",
		}, []string{labelKind, labelStatus}),
		graduations: prometheus.NewCounter(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      graduationsTotalName,
			Help:      "Recurring cluster lessons graduated into fleet-wide decision proposals.",
		}),
		queries: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      queriesTotalName,
			Help:      "Memory queries answered by the Curator, by status (hit, empty, error).",
		}, []string{labelStatus}),
		guardChecks: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      guardChecksTotalName,
			Help:      "OKF guard evaluations against changed files, by verdict (allow, warn, block).",
		}, []string{labelVerdict}),
		processStart: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: processStartTimeName,
			Help: "Start time of the process since unix epoch in seconds, captured once at start.",
		}),
	}
	m.processStart.Set(float64(time.Now().Unix()))

	recCollector := &recordsCollector{
		repoDir: cleanDir,
		desc: prometheus.NewDesc(
			prometheus.BuildFQName(metricsNamespace, metricsSubsystem, recordsGaugeName),
			"Current OKF records in the Curator store, by record type and lifecycle status.",
			[]string{labelType, labelStatus},
			nil,
		),
	}

	m.registry.MustRegister(
		m.outcomes,
		recCollector,
		m.graduations,
		m.queries,
		m.guardChecks,
		m.processStart,
	)

	for _, kind := range metricsOutcomeKinds {
		for _, status := range metricsOutcomeStatuses {
			m.outcomes.WithLabelValues(kind, status)
		}
	}
	for _, status := range metricsQueryStatuses {
		m.queries.WithLabelValues(status)
	}
	for _, verdict := range metricsGuardVerdicts {
		m.guardChecks.WithLabelValues(verdict)
	}

	repoMetricsMu.Lock()
	repoMetrics[cleanDir] = append(repoMetrics[cleanDir], m)
	repoMetricsMu.Unlock()
	return m
}

func recordGuardCheckForRepoDirs(records []Record, verdict string) {
	seen := map[string]bool{}
	for _, r := range records {
		if r.repoDir != "" {
			seen[r.repoDir] = true
		}
	}
	if len(seen) == 0 {
		return
	}
	repoMetricsMu.RLock()
	var targets []*curatorMetrics
	for dir := range seen {
		targets = append(targets, repoMetrics[dir]...)
	}
	repoMetricsMu.RUnlock()
	for _, m := range targets {
		m.recordGuardCheck(verdict)
	}
}

func normalizeOutcomeKindLabel(kind OutcomeKind) string {
	k := string(kind)
	if slices.Contains(metricsOutcomeKinds, k) {
		return k
	}
	return outcomeKindOther
}

func (m *curatorMetrics) recordOutcome(kind OutcomeKind, status string) {
	if m == nil {
		return
	}
	if !slices.Contains(metricsOutcomeStatuses, status) {
		status = outcomeStatusError
	}
	m.outcomes.WithLabelValues(normalizeOutcomeKindLabel(kind), status).Inc()
}

func (m *curatorMetrics) recordGraduations(n int) {
	if m == nil || n <= 0 {
		return
	}
	m.graduations.Add(float64(n))
}

func (m *curatorMetrics) recordQuery(status string) {
	if m == nil {
		return
	}
	if !slices.Contains(metricsQueryStatuses, status) {
		status = queryStatusError
	}
	m.queries.WithLabelValues(status).Inc()
}

func normalizeGuardVerdictLabel(verdict string) string {
	switch verdict {
	case guardVerdictBlock:
		return guardVerdictBlock
	case guardVerdictWarn:
		return guardVerdictWarn
	default:
		return guardVerdictAllow
	}
}

func (m *curatorMetrics) recordGuardCheck(verdict string) {
	if m == nil {
		return
	}
	m.guardChecks.WithLabelValues(normalizeGuardVerdictLabel(verdict)).Inc()
}

func (m *curatorMetrics) handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{
		MaxRequestsInFlight: metricsMaxRequestsInFlight,
		Timeout:             metricsScrapeTimeout,
	})
}

// MetricsHandler serves the Curator's dedicated Prometheus registry in text format.
func (c *Curator) MetricsHandler() http.Handler {
	if c == nil || c.metrics == nil {
		return promhttp.HandlerFor(prometheus.NewRegistry(), promhttp.HandlerOpts{})
	}
	return c.metrics.handler()
}
