package mcp

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	// MCPPath is the HTTP JSON-RPC 2.0 endpoint for Model Context Protocol calls.
	MCPPath = "/mcp"
	// MCPSwarmPath is the HTTP endpoint that computes a projected SwarmSnapshot
	// and drains pending push events for a remote workstation bridge.
	MCPSwarmPath = "/mcp/swarm"
	// MCPEventsPath is the Server-Sent Events (SSE) stream that notifies a
	// connected workstation bridge whenever bus state or push events change.
	MCPEventsPath = "/mcp/events"
	// MCPHealthPath is the unauthenticated health probe endpoint.
	MCPHealthPath = "/healthz"

	contentTypeJSON          = "application/json"
	contentTypeNDJSON        = "application/x-ndjson"
	contentTypeEventStream   = "text/event-stream"
	headerContentType        = "Content-Type"
	headerAccept             = "Accept"
	headerAuthorization      = "Authorization"
	headerCacheControl       = "Cache-Control"
	headerConnection         = "Connection"
	maxHTTPBodyBytes         = 4 << 20
	sseKeepAliveInterval     = 15 * time.Second
	httpReadHeaderTimeout    = 10 * time.Second
	httpIdleTimeout          = 60 * time.Second
	httpShutdownGrace        = 5 * time.Second
	remoteSwarmBootstrapWait = 2 * time.Second
	remoteSwarmTaskListLimit = 200
	gatewayBusUserName       = "gateway"
)

// InClusterOptions configures an in-cluster MCP Server attached to a local/cluster NATS URL.
type InClusterOptions struct {
	NATSURL           string
	BusUser           string
	BusPassword       string
	ObserverPassword  string
	Namespace         string
	PlatformAgentName string
	ClusterName       string
	GitHubRepo        string
	ConfigPath        string
}

// RemoteSwarmRequest is sent by a local workstation bridge to POST /mcp/swarm to synchronize
// conversation state, compute the projected SwarmSnapshot against the gateway's live
// BusSession, and drain pending push events.
type RemoteSwarmRequest struct {
	RootConversationID string                         `json:"rootConversationId"`
	Deployment         DeploymentProfile              `json:"deployment"`
	Conversations      map[string]ConversationEntry   `json:"conversations,omitempty"`
	ActiveConversation string                         `json:"activeConversation,omitempty"`
	Subagents          map[string]ConversationLineage `json:"subagents,omitempty"`
	Ledger             BoardLedger                    `json:"ledger"`
	ExistingBoardLines []string                       `json:"existingBoardLines,omitempty"`
	DrainEventsFor     string                         `json:"drainEventsFor,omitempty"`
}

// RemoteSwarmResponse is returned by POST /mcp/swarm to a local workstation bridge.
type RemoteSwarmResponse struct {
	Connected          bool                         `json:"connected"`
	MigratedSchema     bool                         `json:"migratedSchema,omitempty"`
	Deployment         DeploymentProfile            `json:"deployment"`
	Snapshot           SwarmSnapshot                `json:"snapshot"`
	Ledger             BoardLedger                  `json:"ledger"`
	Conversations      map[string]ConversationEntry `json:"conversations,omitempty"`
	ActiveConversation string                       `json:"activeConversation,omitempty"`
	PendingEvents      []PushEvent                  `json:"pendingEvents,omitempty"`
}

// NewInClusterServer initializes an MCP Server configured to dial the cluster's NATS bus
// directly (without local ~/.kube/config discovery or local Swarm file writes).
func NewInClusterServer(ctx context.Context, opts InClusterOptions) (*Server, error) {
	if strings.TrimSpace(opts.NATSURL) == "" {
		return nil, errors.New("NATSURL is required for NewInClusterServer")
	}
	cfgPath := strings.TrimSpace(opts.ConfigPath)
	var store *ConfigStore
	if cfgPath != "" {
		var err error
		store, err = NewConfigStore(cfgPath)
		if err != nil {
			return nil, err
		}
	} else {
		store = NewMemoryConfigStore()
	}
	ns := firstNonEmptyStr(strings.TrimSpace(opts.Namespace), directProfileDefaultNS)
	paName := firstNonEmptyStr(strings.TrimSpace(opts.PlatformAgentName), directProfileDefaultPAName)
	name := firstNonEmptyStr(strings.TrimSpace(opts.ClusterName), paName)
	busUser := firstNonEmptyStr(strings.TrimSpace(opts.BusUser), defaultBusUser)

	prof := DeploymentProfile{
		Name:              name,
		Namespace:         ns,
		PlatformAgentName: paName,
		ClusterName:       opts.ClusterName,
		GitHubRepo:        opts.GitHubRepo,
		DirectNATSURL:     strings.TrimSpace(opts.NATSURL),
		BusUser:           busUser,
		BusAuthValue:      opts.BusPassword,
		ObserverAuthValue: opts.ObserverPassword,
		Ready:             true,
		LastDiscoveredAt:  time.Now().UTC(),
	}
	if err := store.UpsertDeployments([]DeploymentProfile{prof}); err != nil {
		return nil, err
	}
	_, _ = store.SetActiveDeployment(prof.Name)

	srv, err := NewServer(ServerOptions{
		Store: store,
	})
	if err != nil {
		return nil, err
	}
	if ctx != nil && strings.TrimSpace(opts.BusPassword) != "" {
		_, _ = srv.ensureConnected(ctx)
	}
	return srv, nil
}

// ComputeRemoteSwarmState merges the caller's conversations into the server's store,
// queries the live BusSession for tasks/topics/agent-cards/session-pods, computes the
// projected SwarmSnapshot, and drains any pending push events for req.DrainEventsFor.
func (s *Server) ComputeRemoteSwarmState(ctx context.Context, req RemoteSwarmRequest) RemoteSwarmResponse {
	if s.store != nil && (len(req.Conversations) > 0 || req.ActiveConversation != "") {
		s.store.MergeRemoteConversations(req.Conversations, req.ActiveConversation)
	}

	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	if sess == nil || !sess.IsHealthy() {
		if ensured, err := s.ensureConnected(ctx); err == nil {
			sess = ensured
		}
	}

	snapCfg := s.store.Snapshot()
	dep := req.Deployment
	connected := false
	var cards map[string]map[string]any
	var tasks []*TaskRecord
	var topics []TopicSnapshot

	if sess != nil {
		sess.WaitForDeepBootstrap(remoteSwarmBootstrapWait)
		s.refreshActiveSessionPods(ctx)
		sessDep := sess.Profile()
		if dep.Name == "" {
			dep = sessDep
		} else {
			dep.ActiveSessionPods = sessDep.ActiveSessionPods
			dep.SessionPodsProbedAt = sessDep.SessionPodsProbedAt
			if dep.GitHubRepo == "" {
				dep.GitHubRepo = sessDep.GitHubRepo
			}
		}
		connected = sess.nc != nil && sess.nc.IsConnected()
		cards = sess.AgentCards()
		tasks = sess.ListTasks("", false, remoteSwarmTaskListLimit)
		topics = sess.ListTopics()

		updatedTokens := make(map[string]bool)
		for _, pod := range dep.ActiveSessionPods {
			token := strings.TrimPrefix(strings.TrimSpace(pod.SessionKey), consoleConvPrefix)
			addr := firstNonEmptyStr(strings.TrimSpace(pod.Addressee), strings.TrimSpace(pod.Name))
			if token == "" || addr == "" {
				continue
			}
			updatedTokens[token] = true
			for alias, c := range snapCfg.Conversations {
				if c.Token == token && c.SessionWorkerID != addr {
					s.store.SetConversationSessionWorkerID(alias, addr)
					c.SessionWorkerID = addr
					snapCfg.Conversations[alias] = c
				}
			}
		}
		for _, t := range tasks {
			if isSessionWorkerTask(t) {
				token := strings.TrimPrefix(t.Conversation, consoleConvPrefix)
				if updatedTokens[token] {
					continue
				}
				updatedTokens[token] = true
				for alias, c := range snapCfg.Conversations {
					if c.Token == token && c.SessionWorkerID != t.Addressee {
						s.store.SetConversationSessionWorkerID(alias, t.Addressee)
						c.SessionWorkerID = t.Addressee
						snapCfg.Conversations[alias] = c
					}
				}
			}
		}
	} else if activeDep, ok := s.store.ActiveDeployment(); ok && dep.Name == "" {
		dep = activeDep
	}

	prevLedger := req.Ledger
	if prevLedger.EventSeqs == nil {
		prevLedger.EventSeqs = make(map[string]int)
	}
	if prevLedger.Authors == nil {
		prevLedger.Authors = make(map[string]bool)
	}
	if prevLedger.NextSeq <= 0 {
		prevLedger.NextSeq = 1
	}
	migrated := prevLedger.SchemaVersion > 0 && prevLedger.SchemaVersion < swarmLedgerSchema
	if prevLedger.SchemaVersion == 0 && len(prevLedger.EventSeqs) > 0 {
		migrated = true
	}
	if migrated {
		prevLedger = BoardLedger{
			SchemaVersion: swarmLedgerSchema,
			NextSeq:       1,
			EventSeqs:     make(map[string]int),
			Authors:       make(map[string]bool),
		}
	}

	input := SwarmProjectionInput{
		RootConversationID: req.RootConversationID,
		Deployment:         dep,
		Connected:          connected,
		Conversations:      snapCfg.Conversations,
		Subagents:          req.Subagents,
		AgentCards:         cards,
		Tasks:              tasks,
		Topics:             topics,
		Now:                time.Now().UTC(),
	}
	swarmSnap, nextLedger := BuildSwarmState(input, prevLedger)
	if !migrated && len(req.ExistingBoardLines) > 0 && boardHasOrphanedManagedAuthor(req.ExistingBoardLines, prevLedger, swarmSnap.Members) {
		migrated = true
		prevLedger = BoardLedger{
			SchemaVersion: swarmLedgerSchema,
			NextSeq:       1,
			EventSeqs:     make(map[string]int),
			Authors:       make(map[string]bool),
		}
		swarmSnap, nextLedger = BuildSwarmState(input, prevLedger)
	}

	var drained []PushEvent
	if s.store != nil {
		drained, _ = s.store.DrainPendingEventsForAGY(strings.TrimSpace(req.DrainEventsFor))
	}

	return RemoteSwarmResponse{
		Connected:          connected,
		MigratedSchema:     migrated,
		Deployment:         dep,
		Snapshot:           swarmSnap,
		Ledger:             nextLedger,
		Conversations:      snapCfg.Conversations,
		ActiveConversation: snapCfg.ActiveConversation,
		PendingEvents:      drained,
	}
}

// authenticateOrAdoptBearer verifies the caller's Bearer token when no static A2A_MCP_TOKEN
// was configured. If the active deployment profile does not yet have a BusAuthValue (or its
// BusSession is not connected), it adopts the caller's Bearer token as the `console` NATS
// password and verifies it by connecting the BusSession against the cluster NATS server.
func (s *Server) authenticateOrAdoptBearer(ctx context.Context, got string) bool {
	if s == nil || s.store == nil {
		return false
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	sessionHealthy := sess != nil && sess.IsHealthy()

	activeDep, ok := s.store.ActiveDeployment()
	if !ok {
		return false
	}
	curPass := strings.TrimSpace(activeDep.BusAuthValue)
	if curPass != "" {
		if subtle.ConstantTimeCompare([]byte(got), []byte(curPass)) == 1 {
			if !sessionHealthy {
				_, err := s.ensureConnected(ctx)
				return err == nil
			}
			return true
		}
		if sessionHealthy || got == "" {
			return false
		}
	}
	if got == "" {
		// Allow unauthenticated requests only when the server is already connected
		// to a local/embedded NATS instance that required no password.
		return sessionHealthy && curPass == ""
	}

	candidate := activeDep
	if candidate.BusUser == "" || candidate.BusUser == gatewayBusUserName {
		candidate.BusUser = defaultBusUser
	}
	candidate.BusAuthValue = got
	if _, err := s.connectSession(ctx, candidate); err != nil {
		return false
	}
	return true
}

// HTTPHandler returns an http.Handler serving /mcp, /mcp/swarm, /mcp/events, and /healthz.
// When bearerToken is non-empty, all routes except /healthz require Authorization: Bearer <token>.
// When bearerToken is empty, routes authenticate the caller's Bearer token against the cluster's
// `console` NATS user credentials (or allow no-auth if already connected to a no-auth embedded NATS).
func (s *Server) HTTPHandler(bearerToken string) http.Handler {
	mux := http.NewServeMux()
	token := strings.TrimSpace(bearerToken)

	requireAuth := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			got := strings.TrimSpace(strings.TrimPrefix(r.Header.Get(headerAuthorization), bearerPrefix))
			if token != "" {
				if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
					http.Error(w, "unauthorized", http.StatusUnauthorized)
					return
				}
			} else if !s.authenticateOrAdoptBearer(r.Context(), got) {
				http.Error(w, "unauthorized", http.StatusUnauthorized)
				return
			}
			next(w, r)
		}
	}

	mux.HandleFunc(http.MethodGet+" "+MCPHealthPath, func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set(headerContentType, contentTypeJSON)
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":  "ok",
			"name":    mcpServerName,
			"version": mcpServerVersion,
		})
	})

	mux.HandleFunc(http.MethodPost+" "+MCPPath, requireAuth(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, maxHTTPBodyBytes))
		if err != nil {
			http.Error(w, "failed to read request body", http.StatusBadRequest)
			return
		}
		streamNDJSON := strings.Contains(r.Header.Get(headerAccept), contentTypeNDJSON)
		reqCtx := r.Context()
		if streamNDJSON {
			w.Header().Set(headerContentType, contentTypeNDJSON)
			w.Header().Set(headerCacheControl, "no-cache")
			w.WriteHeader(http.StatusOK)
			flusher, _ := w.(http.Flusher)
			var flushFn func()
			if flusher != nil {
				flushFn = flusher.Flush
			}
			reqCtx = withRequestNotifySink(reqCtx, w, flushFn)
			resp := s.HandleJSONRPC(reqCtx, body)
			if len(resp) > 0 {
				if sink := requestNotifySinkFromContext(reqCtx); sink != nil {
					sink.mu.Lock()
					_, _ = fmt.Fprintf(w, "%s\n", resp)
					if sink.flush != nil {
						sink.flush()
					}
					sink.mu.Unlock()
				} else {
					_, _ = fmt.Fprintf(w, "%s\n", resp)
				}
			}
			return
		}

		resp := s.HandleJSONRPC(reqCtx, body)
		if len(resp) == 0 {
			w.WriteHeader(http.StatusAccepted)
			return
		}
		w.Header().Set(headerContentType, contentTypeJSON)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(resp)
	}))

	mux.HandleFunc(http.MethodPost+" "+MCPSwarmPath, requireAuth(func(w http.ResponseWriter, r *http.Request) {
		var req RemoteSwarmRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, maxHTTPBodyBytes)).Decode(&req); err != nil {
			http.Error(w, fmt.Sprintf("invalid swarm request: %v", err), http.StatusBadRequest)
			return
		}
		resp := s.ComputeRemoteSwarmState(r.Context(), req)
		w.Header().Set(headerContentType, contentTypeJSON)
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(resp)
	}))

	mux.HandleFunc(http.MethodGet+" "+MCPEventsPath, requireAuth(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set(headerContentType, contentTypeEventStream)
		w.Header().Set(headerCacheControl, "no-cache")
		w.Header().Set(headerConnection, "keep-alive")
		w.WriteHeader(http.StatusOK)
		flusher, _ := w.(http.Flusher)
		if flusher != nil {
			flusher.Flush()
		}

		subCh, unsub := s.SubscribePushEvents()
		defer unsub()

		ticker := time.NewTicker(sseKeepAliveInterval)
		defer ticker.Stop()
		for {
			select {
			case <-r.Context().Done():
				return
			case ev, ok := <-subCh:
				if !ok {
					return
				}
				raw, err := json.Marshal(ev)
				if err != nil {
					continue
				}
				if _, err := fmt.Fprintf(w, "event: push\ndata: %s\n\n", raw); err != nil {
					return
				}
				if flusher != nil {
					flusher.Flush()
				}
			case ts := <-ticker.C:
				_, err := fmt.Fprintf(w, "event: heartbeat\ndata: {\"time\":%q}\n\n", ts.UTC().Format(time.RFC3339))
				if err != nil {
					return
				}
				if flusher != nil {
					flusher.Flush()
				}
			}
		}
	}))

	return mux
}

// HTTPServer wraps an *http.Server serving the MCP HTTP/SSE endpoints.
type HTTPServer struct {
	listen      string
	srv         *Server
	bearerToken string
	log         *slog.Logger

	mu       sync.Mutex
	listener net.Listener
}

// NewHTTPServer constructs an HTTPServer for listen (e.g. ":8089" or "127.0.0.1:8089").
func NewHTTPServer(listen string, srv *Server, bearerToken string, log *slog.Logger) (*HTTPServer, error) {
	addr := strings.TrimSpace(listen)
	if addr == "" {
		return nil, errors.New("mcp HTTP listener requires a non-empty listen address")
	}
	if srv == nil {
		return nil, errors.New("mcp HTTP listener requires a non-nil *mcp.Server")
	}
	if log == nil {
		log = slog.Default()
	}
	return &HTTPServer{
		listen:      addr,
		srv:         srv,
		bearerToken: bearerToken,
		log:         log,
	}, nil
}

// Addr returns the bound listener address once Run has started listening, or the configured listen string.
func (h *HTTPServer) Addr() string {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.listener != nil {
		return h.listener.Addr().String()
	}
	return h.listen
}

// Run binds the TCP listener and serves MCP HTTP requests until ctx is canceled.
func (h *HTTPServer) Run(ctx context.Context) error {
	ln, err := net.Listen(tcpNetwork, h.listen)
	if err != nil {
		return fmt.Errorf("mcp HTTP listener on %s: %w", h.listen, err)
	}
	h.mu.Lock()
	h.listener = ln
	h.mu.Unlock()

	httpSrv := &http.Server{
		Handler:           h.srv.HTTPHandler(h.bearerToken),
		ReadHeaderTimeout: httpReadHeaderTimeout,
		IdleTimeout:       httpIdleTimeout,
	}
	h.log.Info("mcp HTTP server listening", "address", ln.Addr().String(), "path", MCPPath)

	errs := make(chan error, 1)
	go func() {
		serveErr := httpSrv.Serve(ln)
		if errors.Is(serveErr, http.ErrServerClosed) {
			serveErr = nil
		}
		errs <- serveErr
	}()

	select {
	case err := <-errs:
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), httpShutdownGrace)
		defer cancel()
		_ = httpSrv.Shutdown(shutdownCtx)
		return nil
	}
}
