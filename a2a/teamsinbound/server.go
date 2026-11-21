package teamsinbound

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Paths and bounds of the two listeners.
const (
	// EventsPath is where Microsoft is told to deliver: the path the admin
	// registers, under their host, as the bot's messaging endpoint.
	EventsPath = "/api/v1/teams/events"
	// relayEventsPath is the broker's intake route.
	relayEventsPath = "/v1/chat/teams/events"
	// HealthzPath and ReadyzPath are the probes; MetricsPath the collector's.
	HealthzPath = "/healthz"
	ReadyzPath  = "/readyz"
	MetricsPath = "/metrics"
	// maxBodyBytes bounds an activity. Teams messages are small; a card or
	// an attachment reference is tens of KiB at most, and the broker caps
	// its own intake too. Past this the request is refused unread.
	maxBodyBytes = 256 << 10
	// relayTimeout bounds the hand-off to the broker. Bot Framework waits
	// fifteen seconds for the endpoint; this leaves room for the token
	// check and the answer.
	relayTimeout = 5 * time.Second
	// bearerScheme is the Authorization scheme Microsoft uses.
	bearerScheme = "Bearer "
	// The challenges a refusal answers with, and the body an acceptance
	// answers with. Neither is a credential.
	challengeNoBearer     = `Bearer realm="teams-inbound"` // #nosec G101 -- a WWW-Authenticate challenge, not a credential
	challengeInvalidToken = `Bearer error="invalid_token"` // #nosec G101 -- a WWW-Authenticate challenge, not a credential
	acceptedBody          = `{"status":"accepted"}`
	// metricsNamespace prefixes the receiver's metrics.
	metricsNamespace = "kubeagents_teams_inbound"
)

// Outcomes a request can have, as the requests_total metric labels them.
const (
	outcomeAccepted  = "accepted"
	outcomeRefused   = "refused"
	outcomeUnchecked = "unchecked"
	outcomeRelay     = "relay_failed"
	outcomeQueueFull = "queue_full"
)

// Reasons a request is refused, as the refusals_total metric labels them.
// Coarse by design: a spike in any one is what an operator acts on.
const (
	reasonMethod   = "method"
	reasonBearer   = "no_bearer"
	reasonBody     = "body"
	reasonToken    = "token"
	reasonTenant   = "tenant"
	reasonChannel  = "channel"
	reasonOversize = "oversize"
)

// msteamsChannel is the one channel this receiver serves; an activity from
// another Bot Framework channel is refused.
const msteamsChannel = "msteams"

// Server is the receiver: the public handler Microsoft reaches and the
// status handler the probes and collector reach.
type Server struct {
	cfg      Config
	verifier *Verifier
	relay    *http.Client
	log      *slog.Logger
	registry *prometheus.Registry
	requests *prometheus.CounterVec
	refusals *prometheus.CounterVec
}

// NewServer wires a receiver from its config. The verifier is created here
// so a caller cannot run the handler without one.
func NewServer(cfg Config, log *slog.Logger) *Server {
	if log == nil {
		log = slog.Default()
	}
	reg := prometheus.NewRegistry()
	requests := prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: metricsNamespace, Name: "requests_total",
		Help: "Requests to the Teams inbound endpoint by outcome.",
	}, []string{"outcome"})
	refusals := prometheus.NewCounterVec(prometheus.CounterOpts{
		Namespace: metricsNamespace, Name: "refusals_total",
		Help: "Requests refused before reaching the broker, by reason.",
	}, []string{"reason"})
	reg.MustRegister(requests, refusals)
	for _, o := range []string{outcomeAccepted, outcomeRefused, outcomeUnchecked, outcomeRelay, outcomeQueueFull} {
		requests.WithLabelValues(o)
	}
	for _, r := range []string{reasonMethod, reasonBearer, reasonBody, reasonToken, reasonTenant, reasonChannel, reasonOversize} {
		refusals.WithLabelValues(r)
	}
	return &Server{
		cfg:      cfg,
		verifier: NewVerifier(cfg.AppID, cfg.OpenIDConfigURL),
		relay:    &http.Client{Timeout: relayTimeout},
		log:      log,
		registry: reg,
		requests: requests,
		refusals: refusals,
	}
}

// Verifier is the server's token verifier, for Warm at startup.
func (s *Server) Verifier() *Verifier { return s.verifier }

// Handler is the public listener: EventsPath and nothing else.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(EventsPath, s.handleEvents)
	return mux
}

// StatusHandler is the status listener: probes and metrics.
func (s *Server) StatusHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc(HealthzPath, func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc(ReadyzPath, func(w http.ResponseWriter, _ *http.Request) {
		if s.verifier.Ready() {
			w.WriteHeader(http.StatusOK)
			return
		}
		http.Error(w, "Microsoft's signing keys have not been read yet", http.StatusServiceUnavailable)
	})
	mux.Handle(MetricsPath, promhttp.HandlerFor(s.registry, promhttp.HandlerOpts{}))
	return mux
}

// handleEvents is the one public route. The order is the containment: the
// method and the bearer's presence, the body bound, the token, the channel,
// the tenant, and only then the broker. A refusal says why in one plain
// sentence and never echoes the token or the body.
func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		s.refuse(w, http.StatusMethodNotAllowed, reasonMethod, "only POST is served here")
		return
	}
	authz := r.Header.Get("Authorization")
	if !strings.HasPrefix(authz, bearerScheme) || strings.TrimSpace(authz[len(bearerScheme):]) == "" {
		w.Header().Set("WWW-Authenticate", challengeNoBearer)
		s.refuse(w, http.StatusUnauthorized, reasonBearer, "the request carries no bearer token")
		return
	}
	token := strings.TrimSpace(authz[len(bearerScheme):])
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			s.refuse(w, http.StatusRequestEntityTooLarge, reasonOversize, "the activity is larger than this receiver accepts")
			return
		}
		s.refuse(w, http.StatusBadRequest, reasonBody, "the activity could not be read")
		return
	}
	act, err := decodeActivity(body)
	if err != nil {
		s.refuse(w, http.StatusBadRequest, reasonBody, "the activity is not a JSON object")
		return
	}
	if err := s.verifier.Verify(r.Context(), token, act); err != nil {
		if errors.Is(err, ErrUnchecked) {
			s.requests.WithLabelValues(outcomeUnchecked).Inc()
			s.log.Warn("teams inbound: a request could not be checked", "err", err)
			http.Error(w, err.Error(), http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("WWW-Authenticate", challengeInvalidToken)
		s.refuse(w, http.StatusUnauthorized, reasonToken, err.Error())
		return
	}
	if act.ChannelID != msteamsChannel {
		s.refuse(w, http.StatusForbidden, reasonChannel, "this receiver serves Microsoft Teams activities only")
		return
	}
	if !strings.EqualFold(act.tenantID(), s.cfg.TenantID) {
		s.refuse(w, http.StatusForbidden, reasonTenant, "the activity is not from the tenant this install serves")
		return
	}
	status, err := s.forward(r.Context(), body)
	switch {
	case err != nil:
		s.requests.WithLabelValues(outcomeRelay).Inc()
		s.log.Error("teams inbound: the broker did not take the activity", "err", err)
		http.Error(w, "the activity could not be handed to the install", http.StatusBadGateway)
	case status == http.StatusAccepted:
		s.requests.WithLabelValues(outcomeAccepted).Inc()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = w.Write([]byte(acceptedBody))
	case status == http.StatusServiceUnavailable:
		s.requests.WithLabelValues(outcomeQueueFull).Inc()
		s.log.Warn("teams inbound: the broker's Teams queue is full; the activity was dropped")
		http.Error(w, "the install's Teams queue is full; retry shortly", http.StatusServiceUnavailable)
	default:
		s.requests.WithLabelValues(outcomeRelay).Inc()
		s.log.Error("teams inbound: the broker answered unexpectedly", "status", status)
		http.Error(w, "the activity could not be handed to the install", http.StatusBadGateway)
	}
}

// refuse answers a refusal and counts it.
func (s *Server) refuse(w http.ResponseWriter, status int, reason, msg string) {
	s.requests.WithLabelValues(outcomeRefused).Inc()
	s.refusals.WithLabelValues(reason).Inc()
	http.Error(w, msg, status)
}

// forward posts the activity to the broker's Teams intake under the pod's
// projected ServiceAccount token, read fresh because the kubelet rotates it.
// It returns the broker's status; the broker answers 202 when queued and 503
// when its queue is full, and anything else is a fault.
func (s *Server) forward(ctx context.Context, body []byte) (int, error) {
	token, err := os.ReadFile(s.cfg.TokenPath)
	if err != nil {
		return 0, fmt.Errorf("the pod's ServiceAccount token could not be read: %w", err)
	}
	ctx, cancel := context.WithTimeout(ctx, relayTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.RelayURL+relayEventsPath, bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", bearerScheme+strings.TrimSpace(string(token)))
	resp, err := s.relay.Do(req)
	if err != nil {
		return 0, err
	}
	defer func() { _ = resp.Body.Close() }()
	// Drain the small JSON the broker answers with so the connection is reused.
	var drained json.RawMessage
	_ = json.NewDecoder(io.LimitReader(resp.Body, maxBodyBytes)).Decode(&drained)
	return resp.StatusCode, nil
}
