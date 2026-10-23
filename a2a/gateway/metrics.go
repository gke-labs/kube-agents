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
// scraping it, and the operator's usage poller, are admitted to a port that
// serves counters and nothing else.
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

	// taskTerminalsName counts every task terminal by state and source; the
	// operator's usage poller reads it under failed and rejected as the
	// next-mode half of status.usage's chatTurnsErroredTotal
	// (docs/designs/usage-counters-producer.md).
	taskTerminalsName       = "task_terminals_total"
	gchatEventsReceivedName = "gchat_events_received_total"
	gchatPullsName          = "gchat_pulls_total"
	chatMessagesName        = "chat_messages_total"
	notifyMarksDroppedName  = "notify_marks_dropped_total"
	// messagesInboundName counts the chat messages the gateway accepted as
	// turns, by platform: the next-mode half of status.usage's
	// chatMessagesInboundTotal, which the chat_metrics plugin counts for the
	// Hermes-side ingress (docs/designs/usage-counters-producer.md).
	messagesInboundName = "messages_inbound_total"
	// sessionsName counts the conversations the gateway minted a session
	// record for, by platform: the next-mode chat half of status.usage's
	// sessionsTotal, beside the watcher's triage sessions and the
	// session_store plugin's count for the Hermes-side ingress
	// (docs/designs/usage-counters-producer.md).
	sessionsName = "sessions_total"
	// processStartTimeName is the gauge the operator's usage poller reads to
	// tell a gateway that restarted in place from a counter that fell for
	// another reason: the Go process collector's name, but captured once at
	// start and never re-derived, as the broker, the event watcher and the
	// chat_metrics plugin serve it (docs/designs/usage-counters-producer.md,
	// "Resets"). Unprefixed: it is the process's, not the gateway's.
	processStartTimeName = "process_start_time_seconds"

	// The label names. Every value under them comes from a closed list
	// below, never from a task, a conversation or an executor's text, so
	// nothing a chat user or an executor sends can grow the series set.
	labelState    = "state"
	labelSource   = "source"
	labelOutcome  = "outcome"
	labelPlatform = "platform"
	// The platform label's values: the chat backends, under the names the
	// operator's activeInterfaces vocabulary and the chat_metrics plugin use
	// for the ones they share, so one series name reads the same on either
	// ingress. The doors (inject, a2a, a2a-google) carry programs' turns, not
	// chat, and are not counted.
	platformGoogleChat = "googlechat"
	platformSlack      = "slack"
	platformDiscord    = "discord"
	platformConsole    = "console"

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

	// The chat-message outcomes (spec-a2a-payloads.md, "Chat messages"):
	// rendered, laid out by the backend; text-only, a backend that renders
	// none posted the fallback; refused, a backend that renders them could
	// not hold this one or its API refused it, and the fallback posted;
	// invalid, the message broke a rule and was dropped whole (or, for a
	// mark alone, the mark was), the fallback standing.
	chatRendered = "rendered"
	chatTextOnly = "text_only"
	chatRefused  = "refused"
	chatInvalid  = "invalid"

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
	metricsChatOutcomes    = []string{chatRendered, chatTextOnly, chatRefused, chatInvalid}
	metricsPlatforms       = []string{platformGoogleChat, platformSlack, platformDiscord, platformConsole}
	// metricsBackendPlatforms maps each chat backend to its platform label; a
	// backend outside it is a door and is not counted.
	metricsBackendPlatforms = map[string]string{
		gchatBackend:   platformGoogleChat,
		slackBackend:   platformSlack,
		discordBackend: platformDiscord,
		consoleBackend: platformConsole,
	}
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
	chatMessages        *prometheus.CounterVec
	notifyMarksDropped  prometheus.Counter
	messagesInbound     *prometheus.CounterVec
	sessions            *prometheus.CounterVec
	// processStart is processStartTimeName, set once in NewMetrics and constant
	// for the life of the process by construction.
	processStart prometheus.Gauge
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
		chatMessages: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      chatMessagesName,
			Help:      "kube-agents.chat/v1 messages the gateway handed a chat adapter or dropped, counted per hand-over, by outcome: rendered (laid out by the backend), text_only (a backend that renders none posted the fallback), refused (the backend could not hold it or its API refused it; the fallback posted), invalid (it broke a rule and was dropped; the fallback posted, or for a bad mark alone the mark was dropped).",
		}, []string{labelOutcome}),
		notifyMarksDropped: prometheus.NewCounter(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      notifyMarksDroppedName,
			Help:      "Conversation notify marks dropped because no chain could be named for them (the notify carried no update key bound to one) while a chain was running in the conversation.",
		}),
		messagesInbound: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      messagesInboundName,
			Help:      "Chat messages the gateway accepted as turns, by platform (googlechat, slack, discord, console, other), counted before the sender is verified; a door's turns are not chat and are not counted.",
		}, []string{labelPlatform}),
		sessions: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: metricsNamespace,
			Subsystem: metricsSubsystem,
			Name:      sessionsName,
			Help:      "Conversations the gateway minted a session for, by the platform of the first message (googlechat, slack, discord, console, other); a conversation is counted once, and a door's are not chat and are not counted.",
		}, []string{labelPlatform}),
		processStart: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: processStartTimeName,
			Help: "Start time of the process since unix epoch in seconds, captured once at start.",
		}),
	}
	m.processStart.Set(float64(time.Now().UnixNano()) / float64(time.Second))
	m.registry.MustRegister(m.taskTerminals, m.gchatEventsReceived, m.gchatPulls, m.chatMessages, m.notifyMarksDropped, m.messagesInbound, m.sessions, m.processStart)
	for _, outcome := range metricsChatOutcomes {
		m.chatMessages.WithLabelValues(outcome)
	}
	for _, platform := range append(append([]string(nil), metricsPlatforms...), metricsLabelOther) {
		m.messagesInbound.WithLabelValues(platform)
		m.sessions.WithLabelValues(platform)
	}
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

// chatMessage counts one chat message under its outcome, one of
// metricsChatOutcomes; the callers pass only those constants.
func (m *Metrics) chatMessage(outcome string) {
	if m == nil {
		return
	}
	m.chatMessages.WithLabelValues(outcome).Inc()
}

// notifyMarkDropped counts one conversation notify mark dropped for want of
// a chain to name (applyNotifyMark).
func (m *Metrics) notifyMarkDropped() {
	if m == nil {
		return
	}
	m.notifyMarksDropped.Inc()
}

// messageInbound counts one accepted chat message under its platform.
func (m *Metrics) messageInbound(platform string) {
	if m == nil {
		return
	}
	m.messagesInbound.WithLabelValues(platform).Inc()
}

// sessionMinted counts one conversation whose session record this gateway
// created, under its platform. Its one caller is mintSession, on the create
// that won; the loser of a mint race adopts the winner's record and counts
// nothing, so two replicas racing first contact count one session.
func (m *Metrics) sessionMinted(platform string) {
	if m == nil {
		return
	}
	m.sessions.WithLabelValues(platform).Inc()
}

// metricsPlatformLabel is the platform label for a message that arrived
// through backend, and false for a door, whose turns are not chat. A chat
// backend added later without a row lands under other rather than minting
// a series nobody documented.
func metricsPlatformLabel(backend string) (string, bool) {
	if platform, ok := metricsBackendPlatforms[backend]; ok {
		return platform, true
	}
	switch backend {
	case injectBackend, a2aBackend, a2aGoogleBackend:
		return "", false
	}
	return metricsLabelOther, true
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
// admits the collector and the operator to counters and nothing else.
type MetricsServer struct {
	listen  string
	metrics *Metrics
	log     *slog.Logger

	// listener, when set, is an already-bound listener Run serves on
	// instead of binding listen. Test injection only.
	listener net.Listener
}

// NewMetricsServer builds the listener for a port. It binds every interface,
// unlike the doors' loopback binds, because its callers, the collector and
// the operator's poller, are on the pod network; what keeps anyone else off
// it is the NetworkPolicy, and
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
