package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"golang.org/x/net/netutil"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gateway's Prometheus surface: a metrics-only TCP listener of its own,
// copied from the credential broker's (CREDENTIAL_PROXY_METRICS_PORT in
// agents/platform/scripts/credential_proxy.py), so that the collector
// scraping it is admitted to a port that serves counters and nothing else.
// The doors keep their own loopback listeners and never serve these series.
// The operator renders A2A_METRICS_PORT from the constant that also declares
// the container port and the collector's ingress rule
// (a2aGatewayMetricsPort in platformagent_a2a_manifests.go); unset means no
// listener, which is what an older operator that declares no port gets.
const (
	// MetricsPath is the one path the metrics listener serves.
	MetricsPath = "/metrics"

	// metricsNamespace and metricsSubsystem prefix every series, so the
	// gateway's sit beside the broker's kubeagents_* names and cannot be
	// mistaken for them.
	metricsNamespace = "kubeagents"
	metricsSubsystem = "a2a_gateway"

	taskTerminalsName       = "task_terminals_total"
	gchatEventsReceivedName = "gchat_events_received_total"
	gchatPullsName          = "gchat_pulls_total"

	// The label names. Every value under them comes from a closed list
	// below, never from a task, a conversation or an executor's text, so
	// nothing a chat user or an executor sends can grow the series set.
	labelState   = "state"
	labelSource  = "source"
	labelOutcome = "outcome"

	// metricsLabelOther is the bucket for a value outside a closed list.
	// observeTaskTerminal sees only the four final states and the four
	// TerminalSources today; a state or source added later lands here
	// rather than minting a series nobody documented.
	metricsLabelOther = "other"

	// The gchat pull outcomes: a pull that returned an event, one that
	// returned nothing, and one that failed (the relay refused or did not
	// answer, or the pull was never made or could not be read).
	gchatPullEvents = "events"
	gchatPullEmpty  = "empty"
	gchatPullFailed = "failed"

	// metricsMaxConnections is the broker's METRICS_MAX_CONNECTIONS: at
	// most this many connections are open at once, so a peer that reaches
	// the port cannot spend the gateway's memory one idle connection at a
	// time. A connection past it waits in the kernel's accept queue until
	// one closes. metricsMaxRequestsInFlight holds concurrent renders to the
	// same number, so it cannot fire while the listener caps connections at
	// 16 (each connection serves one request at a time); it is kept so the
	// handler stays bounded if it is ever served without that listener.
	metricsMaxConnections      = 16
	metricsMaxRequestsInFlight = metricsMaxConnections
	// metricsConnectionDeadline is the broker's
	// METRICS_CONNECTION_DEADLINE_SECONDS: how long a peer may take to send
	// a request, and how long the reply may take to write, so no
	// connection holds one of the slots above for longer whatever the peer
	// sends. metricsScrapeTimeout bounds one render inside it; the registry
	// holds a few dozen series, so a render that takes that long is a stuck
	// process.
	metricsConnectionDeadline = 10 * time.Second
	metricsScrapeTimeout      = 5 * time.Second
	// metricsIdleTimeout closes a kept-alive connection between scrapes,
	// which frees its slot; the collector scrapes every 30s and reconnects
	// for free.
	metricsIdleTimeout = 10 * time.Second
	// metricsShutdownGrace is how long the listener waits for an in-flight
	// scrape once the gateway is stopping.
	metricsShutdownGrace = 5 * time.Second
)

// metricsTerminalStates and metricsTerminalSources are the closed lists the
// task-terminal counter's labels are drawn from. Every combination, other
// included, is created at zero, so a dashboard's rate() over a state that
// has never happened reads 0 rather than no data.
var (
	metricsTerminalStates = []lib.TaskState{
		lib.StateCompleted, lib.StateFailed, lib.StateCanceled, lib.StateRejected,
	}
	metricsTerminalSources = []TerminalSource{TerminalFromExecutor, TerminalFromSupervisor, TerminalFromGateway, TerminalNeverStarted}
	metricsGchatOutcomes   = []string{gchatPullEvents, gchatPullEmpty, gchatPullFailed}
)

// Metrics is the gateway's counters and the private registry that serves
// them. Private rather than prometheus.DefaultRegisterer: a test builds as
// many gateways as it likes, each with counters that start at zero, and the
// listener serves these series and no process or runtime collector nobody
// asked for. A nil *Metrics is valid and counts nothing, so an adapter built
// without one (a test, an embedder) needs no guard at each call.
type Metrics struct {
	registry            *prometheus.Registry
	taskTerminals       *prometheus.CounterVec
	gchatEventsReceived prometheus.Counter
	gchatPulls          *prometheus.CounterVec
}

// NewMetrics builds the counters on a fresh registry.
func NewMetrics() *Metrics {
	m := &Metrics{
		registry: prometheus.NewRegistry(),
		taskTerminals: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      taskTerminalsName,
			Help:      "Task terminals the gateway delivered to a conversation, by final state and by whose word it was (executor, supervisor, gateway for a task that never reached the bus, gateway-never-started for one no executor took inside the first-event grace).",
		}, []string{labelState, labelSource}),
		gchatEventsReceived: prometheus.NewCounter(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      gchatEventsReceivedName,
			Help:      "Google Chat events the gateway pulled from the credential broker's relay, before parsing or classification.",
		}),
		gchatPulls: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      gchatPullsName,
			Help:      "Pulls of the Google Chat relay, by outcome: events (one arrived), empty (none did), failed (the relay refused or did not answer, or the pull was never made or could not be read).",
		}, []string{labelOutcome}),
	}
	m.registry.MustRegister(m.taskTerminals, m.gchatEventsReceived, m.gchatPulls)
	for _, state := range append(append([]lib.TaskState(nil), metricsTerminalStates...), metricsLabelOther) {
		for _, source := range append(append([]TerminalSource(nil), metricsTerminalSources...), metricsLabelOther) {
			m.taskTerminals.WithLabelValues(string(state), string(source))
		}
	}
	for _, outcome := range metricsGchatOutcomes {
		m.gchatPulls.WithLabelValues(outcome)
	}
	return m
}

// Handler serves the registry in the Prometheus text format.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{
		MaxRequestsInFlight: metricsMaxRequestsInFlight,
		Timeout:             metricsScrapeTimeout,
	})
}

// taskTerminal counts one terminal. Its one caller is observeTaskTerminal,
// which every terminal path reaches once, relayed or declared by the gateway.
func (m *Metrics) taskTerminal(state lib.TaskState, source TerminalSource) {
	if m == nil {
		return
	}
	m.taskTerminals.WithLabelValues(metricsTerminalStateLabel(state), metricsTerminalSourceLabel(source)).Inc()
}

// gchatPull counts one pull of the Chat relay under its outcome, and the
// event it carried when it carried one.
func (m *Metrics) gchatPull(outcome string) {
	if m == nil {
		return
	}
	if outcome == gchatPullEvents {
		m.gchatEventsReceived.Inc()
	}
	m.gchatPulls.WithLabelValues(outcome).Inc()
}

// metricsTerminalStateLabel is state if the closed list has it, else other.
func metricsTerminalStateLabel(state lib.TaskState) string {
	for _, known := range metricsTerminalStates {
		if state == known {
			return string(state)
		}
	}
	return metricsLabelOther
}

// metricsTerminalSourceLabel is source if the closed list has it, else other.
func metricsTerminalSourceLabel(source TerminalSource) string {
	for _, known := range metricsTerminalSources {
		if source == known {
			return string(source)
		}
	}
	return metricsLabelOther
}

// MetricsServer is the metrics-only listener: MetricsPath and nothing else,
// on its own port, so the NetworkPolicy rule that admits the collector to it
// admits the collector to counters and nothing else.
type MetricsServer struct {
	listen  string
	metrics *Metrics
	log     *slog.Logger

	// listener, when set, is an already-bound listener Run serves on
	// instead of binding listen. Test injection only.
	listener net.Listener
}

// NewMetricsServer builds the listener for a port. It binds every interface,
// unlike the doors' loopback binds, because its caller is the collector on
// the pod network; what keeps anyone else off it is the NetworkPolicy, and
// what it serves is counters.
func NewMetricsServer(port int, metrics *Metrics, log *slog.Logger) (*MetricsServer, error) {
	if port < metricsPortMin || port > metricsPortMax {
		return nil, fmt.Errorf("metrics listener port %d is not in %d-%d", port, metricsPortMin, metricsPortMax)
	}
	if metrics == nil {
		return nil, fmt.Errorf("metrics listener needs the gateway's metrics")
	}
	if log == nil {
		log = slog.Default()
	}
	return &MetricsServer{listen: fmt.Sprintf(":%d", port), metrics: metrics, log: log}, nil
}

// Run serves MetricsPath until ctx is done. Every other path is 404, and
// every method on it but GET (and the HEAD the mux answers with it) 405.
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
