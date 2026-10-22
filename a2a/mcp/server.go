package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"
)

const (
	jsonRPCVersion               = "2.0"
	defaultMCPProtocolVersion    = "2024-11-05"
	mcpServerName                = "kube-agents"
	mcpServerVersion             = "0.4.0"
	methodInitialize             = "initialize"
	methodInitialized            = "notifications/initialized"
	methodProgressNotification   = "notifications/progress"
	methodPing                   = "ping"
	methodToolsList              = "tools/list"
	methodToolsCall              = "tools/call"
	methodResourcesList          = "resources/list"
	methodResourcesRead          = "resources/read"
	methodPromptsList            = "prompts/list"
	toolDiscoverDeployments      = "discover_deployments"
	toolConnectDeployment        = "connect_deployment"
	toolConnectionStatus         = "connection_status"
	toolSendMessage              = "send_message"
	toolManageSession            = "manage_session"
	toolTaskControl              = "task_control"
	toolReadTopics               = "read_topics"
	toolInspectPR                = "inspect_pr"
	toolPollEvents               = "poll_events"
	resourceURIStatus            = "kube-agents://status"
	resourceURIDeployments       = "kube-agents://deployments"
	resourceURITopics            = "kube-agents://topics"
	resourceURITasks             = "kube-agents://tasks"
	resourceURIEvents            = "kube-agents://events"
	mimeTypeMarkdown             = "text/markdown"
	contentTypeText              = "text"
	rpcErrParseError             = -32700
	rpcErrInvalidRequest         = -32600
	rpcErrMethodNotFound         = -32601
	rpcErrInvalidParams          = -32602
	rpcErrInternal               = -32603
	scannerInitialBufSize        = 64 * 1024
	scannerMaxBufSize            = 16 * 1024 * 1024
	defaultTaskListLimit         = 12
	defaultTranscriptLookupLimit = 50
	taskSummaryPreviewChars      = 80
	actionList                   = "list"
	actionNew                    = "new"
	actionSwitch                 = "switch"
	actionSessionOn              = "session_on"
	actionSessionStartAlias      = "start"
	actionSessionOff             = "session_off"
	actionSessionEndAlias        = "end"
	actionDelete                 = "delete"
	actionRemoveAlias            = "remove"
	actionClearKilled            = "clear_killed"
	actionPruneAlias             = "prune"
	actionHelp                   = "help"
	actionTranscript             = "transcript"
	actionDebugOn                = "debug_on"
	actionDebugOff               = "debug_off"
	actionGet                    = "get"
	actionStatus                 = "status"
	actionSteer                  = "steer"
	actionStop                   = "stop"
	actionCancelAlias            = "cancel"
	actionRead                   = "read"
	actionCards                  = "cards"
	directProfileDefaultName     = "direct-nats"
	directProfileDefaultNS       = "kubeagents-system"
	directProfileDefaultPAName   = "platform-agent"
	asyncReconnectTimeout        = 60 * time.Second
	keepAliveInitialDelay        = 500 * time.Millisecond
	keepAliveTickInterval        = 15 * time.Second
	keepAliveProbeTimeout        = 5 * time.Second
	ensureConnectedHealthyWait   = 3 * time.Second
	ensureConnectedLockedWait    = 1 * time.Second
	pushEventSubChanSize         = 32
	errServerClosedMsg           = "mcp server is closed"
)

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// MCPToolDefinition describes a single MCP tool schema returned by tools/list.
type MCPToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// MCPTextContent represents a single text content block in an MCP tool response.
type MCPTextContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// MCPCallToolResult holds the content and custom rendering metadata returned by CallTool.
type MCPCallToolResult struct {
	Content        []MCPTextContent `json:"content"`
	IsError        bool             `json:"isError,omitempty"`
	CustomTitle    string           `json:"-"`
	CustomMarkdown string           `json:"-"`
}

// MCPResource describes a single MCP resource returned by resources/list.
type MCPResource struct {
	URI         string `json:"uri"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	MIMEType    string `json:"mimeType,omitempty"`
}

// ServerOptions configures an MCP Server instance.
type ServerOptions struct {
	Store      *ConfigStore
	Runner     CommandRunner
	APIBaseURL string
}

// Server implements the kube-agents Model Context Protocol (MCP) server.
type Server struct {
	store      *ConfigStore
	runner     CommandRunner
	apiBaseURL string

	mu          sync.Mutex
	closed      bool
	connectMu   sync.Mutex
	session     *BusSession
	writeMu     sync.Mutex
	notifyOut   io.Writer
	eventSubsMu sync.Mutex
	eventSubs   map[chan PushEvent]struct{}
}

type rpcCallToolMeta struct {
	ProgressToken       any                          `json:"progressToken,omitempty"`
	ConversationID      string                       `json:"antigravity.google/conversation_id,omitempty"`
	ArtifactsDir        string                       `json:"antigravity.google/artifacts_dir,omitempty"`
	Conversations       map[string]ConversationEntry `json:"kube-agents/conversations,omitempty"`
	LegacyConversations map[string]ConversationEntry `json:"conversations,omitempty"`
	ActiveConversation  string                       `json:"kube-agents/active_conversation,omitempty"`
	LegacyActiveConv    string                       `json:"active_conversation,omitempty"`
}

type callMetaContextKey struct{}

type notifySinkContextKey struct{}

type requestNotifySink struct {
	mu    sync.Mutex
	w     io.Writer
	flush func()
}

func withRequestNotifySink(ctx context.Context, w io.Writer, flush func()) context.Context {
	if w == nil {
		return ctx
	}
	return context.WithValue(ctx, notifySinkContextKey{}, &requestNotifySink{w: w, flush: flush})
}

func requestNotifySinkFromContext(ctx context.Context) *requestNotifySink {
	if ctx == nil {
		return nil
	}
	if sink, ok := ctx.Value(notifySinkContextKey{}).(*requestNotifySink); ok {
		return sink
	}
	return nil
}

func withCallMeta(ctx context.Context, meta rpcCallToolMeta) context.Context {
	return context.WithValue(ctx, callMetaContextKey{}, meta)
}

func callMetaFromContext(ctx context.Context) rpcCallToolMeta {
	if ctx == nil {
		return rpcCallToolMeta{}
	}
	if m, ok := ctx.Value(callMetaContextKey{}).(rpcCallToolMeta); ok {
		return m
	}
	return rpcCallToolMeta{}
}

// NewServer constructs a new kube-agents MCP server.
func NewServer(opts ServerOptions) (*Server, error) {
	store := opts.Store
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			return nil, err
		}
	}
	return &Server{
		store:      store,
		runner:     opts.Runner,
		apiBaseURL: opts.APIBaseURL,
	}, nil
}

// Close shuts down the active bus session and closes any active push-event subscriber streams.
func (s *Server) Close() {
	s.mu.Lock()
	s.closed = true
	sess := s.session
	s.session = nil
	s.mu.Unlock()
	if sess != nil {
		sess.Close()
	}
	s.eventSubsMu.Lock()
	for ch := range s.eventSubs {
		delete(s.eventSubs, ch)
		close(ch)
	}
	s.eventSubsMu.Unlock()
}

// SubscribePushEvents registers an in-memory subscriber channel that receives live PushEvent
// notifications whenever enqueuePushEvent accepts an event. The returned cancel function
// unregisters the subscriber and closes the channel.
func (s *Server) SubscribePushEvents() (<-chan PushEvent, func()) {
	ch := make(chan PushEvent, pushEventSubChanSize)
	if s == nil {
		close(ch)
		return ch, func() {}
	}
	s.mu.Lock()
	closed := s.closed
	s.mu.Unlock()
	if closed {
		close(ch)
		return ch, func() {}
	}
	s.eventSubsMu.Lock()
	if s.eventSubs == nil {
		s.eventSubs = make(map[chan PushEvent]struct{})
	}
	s.eventSubs[ch] = struct{}{}
	s.eventSubsMu.Unlock()

	var once sync.Once
	cancel := func() {
		once.Do(func() {
			s.eventSubsMu.Lock()
			if _, ok := s.eventSubs[ch]; ok {
				delete(s.eventSubs, ch)
				close(ch)
			}
			s.eventSubsMu.Unlock()
		})
	}
	return ch, cancel
}

func (s *Server) enqueuePushEvent(ev PushEvent) {
	if s == nil || s.store == nil || !s.store.OwnsConsoleConversation(ev.Conversation) {
		return
	}
	_ = s.store.AppendPendingEvents(ev)
	s.eventSubsMu.Lock()
	for ch := range s.eventSubs {
		select {
		case ch <- ev:
		default:
		}
	}
	s.eventSubsMu.Unlock()
}

func (s *Server) drainAndFormatPiggybackFor(callerID string) string {
	if s == nil || s.store == nil {
		return ""
	}
	events, err := s.store.DrainPendingEventsForAGY(callerID)
	if err != nil || len(events) == 0 {
		return ""
	}
	return piggybackHeader + FormatPushEventsMarkdownBounded(events, maxPiggybackEvents, piggybackSummaryMaxChars)
}

// ServeStdio reads newline-delimited JSON-RPC 2.0 requests from r and writes responses to w.
func (s *Server) ServeStdio(ctx context.Context, r io.Reader, w io.Writer) error {
	s.writeMu.Lock()
	s.notifyOut = w
	s.writeMu.Unlock()
	defer func() {
		s.writeMu.Lock()
		if s.notifyOut == w {
			s.notifyOut = nil
		}
		s.writeMu.Unlock()
	}()

	loopCtx, cancelLoop := context.WithCancel(ctx)
	defer cancelLoop()
	go s.runKeepAliveLoop(loopCtx)

	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, scannerInitialBufSize), scannerMaxBufSize)
	for scanner.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		resp := s.HandleJSONRPC(ctx, []byte(line))
		if len(resp) == 0 {
			continue
		}
		s.writeMu.Lock()
		_, writeErr := fmt.Fprintf(w, "%s\n", resp)
		s.writeMu.Unlock()
		if writeErr != nil {
			return writeErr
		}
	}
	return scanner.Err()
}

// emitProgress writes an MCP notifications/progress JSON-RPC frame to the active per-request
// or stdio writer when a progressToken was provided by the client.
func (s *Server) emitProgress(ctx context.Context, progressToken any, step int, message string) {
	if progressToken == nil || strings.TrimSpace(message) == "" {
		return
	}
	frame, err := json.Marshal(map[string]any{
		"jsonrpc": jsonRPCVersion,
		"method":  methodProgressNotification,
		"params": map[string]any{
			"progressToken": progressToken,
			"progress":      step,
			"total":         0,
			"message":       message,
		},
	})
	if err != nil {
		return
	}
	if sink := requestNotifySinkFromContext(ctx); sink != nil && sink.w != nil {
		sink.mu.Lock()
		_, _ = fmt.Fprintf(sink.w, "%s\n", frame)
		if sink.flush != nil {
			sink.flush()
		}
		sink.mu.Unlock()
		return
	}
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.notifyOut != nil {
		_, _ = fmt.Fprintf(s.notifyOut, "%s\n", frame)
	}
}

// HandleJSONRPC processes a single JSON-RPC 2.0 request frame and returns the marshaled response
// (or nil if the frame was a notification).
func (s *Server) HandleJSONRPC(ctx context.Context, raw []byte) []byte {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return marshalRPCError(json.RawMessage("null"), rpcErrParseError, "invalid JSON-RPC frame")
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		if strings.HasPrefix(req.Method, "notifications/") {
			return nil
		}
		if req.Method == "" {
			return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, "missing method")
		}
		return nil
	}

	switch req.Method {
	case methodInitialize:
		protoVer := defaultMCPProtocolVersion
		var initParams struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		if len(req.Params) > 0 && json.Unmarshal(req.Params, &initParams) == nil && initParams.ProtocolVersion != "" {
			protoVer = initParams.ProtocolVersion
		}
		return marshalRPCResult(req.ID, map[string]any{
			"protocolVersion": protoVer,
			"capabilities": map[string]any{
				"tools":     map[string]any{},
				"resources": map[string]any{},
			},
			"serverInfo": map[string]any{
				"name":    mcpServerName,
				"version": mcpServerVersion,
			},
		})
	case methodPing:
		return marshalRPCResult(req.ID, map[string]any{})
	case methodToolsList:
		return marshalRPCResult(req.ID, map[string]any{
			"tools": s.toolDefinitions(),
		})
	case methodToolsCall:
		var callParams struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
			Meta      rpcCallToolMeta `json:"_meta"`
		}
		if err := json.Unmarshal(req.Params, &callParams); err != nil || callParams.Name == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "tools/call requires tool name")
		}
		if len(callParams.Meta.Conversations) == 0 && len(callParams.Meta.LegacyConversations) > 0 {
			callParams.Meta.Conversations = callParams.Meta.LegacyConversations
		}
		if callParams.Meta.ActiveConversation == "" && callParams.Meta.LegacyActiveConv != "" {
			callParams.Meta.ActiveConversation = callParams.Meta.LegacyActiveConv
		}
		if s.store != nil && (len(callParams.Meta.Conversations) > 0 || callParams.Meta.ActiveConversation != "") {
			s.store.MergeRemoteConversations(callParams.Meta.Conversations, callParams.Meta.ActiveConversation)
		}
		if callParams.Meta.ConversationID != "" && s.store != nil {
			cid := strings.TrimSpace(callParams.Meta.ConversationID)
			snap := s.store.Snapshot()
			isSub := false
			rootID := cid
			if c, ok := s.store.LookupConversation(cid); ok && isSessionSubagentEntry(c) {
				isSub = true
				rootID = firstNonEmptyStr(c.ParentAGYConversationID, snap.Push.AGYConversationID, cid)
			} else if snap.Push.AGYConversationID != "" && snap.Push.AGYConversationID != cid && callParams.Name != toolConnectDeployment {
				isSub = true
				rootID = snap.Push.AGYConversationID
			}
			enabled := true
			_, _ = s.store.UpdatePushConfigWithLineage(ConversationLineage{
				ConversationID:     cid,
				RootConversationID: rootID,
				IsSubagent:         isSub,
			}, &enabled, "")
		}
		callCtx := withCallMeta(ctx, callParams.Meta)
		if callParams.Meta.ProgressToken != nil {
			token := callParams.Meta.ProgressToken
			reqCtx := callCtx
			callCtx = WithProgressCallback(callCtx, func(step int, message string) {
				s.emitProgress(reqCtx, token, step, message)
			})
		}
		res := s.CallTool(callCtx, callParams.Name, callParams.Arguments)
		if !res.IsError && len(res.Content) > 0 && (res.CustomTitle != "" || res.CustomMarkdown != "") && (callParams.Meta.ConversationID != "" || callParams.Meta.ProgressToken != nil) {
			txt := res.Content[0].Text
			md := txt
			if res.CustomMarkdown != "" {
				md = res.CustomMarkdown
			}
			envelope := map[string]any{
				"_custom_tool_result": true,
				"_version":            1,
				"result":              txt,
				"markdown":            md,
			}
			if res.CustomTitle != "" {
				envelope["title"] = res.CustomTitle
			}
			if wrapped, err := json.Marshal(envelope); err == nil {
				res.Content[0].Text = string(wrapped)
			}
		}
		return marshalRPCResult(req.ID, res)
	case methodResourcesList:
		return marshalRPCResult(req.ID, map[string]any{
			"resources": s.resourceDefinitions(),
		})
	case methodResourcesRead:
		var readParams struct {
			URI string `json:"uri"`
		}
		if err := json.Unmarshal(req.Params, &readParams); err != nil || readParams.URI == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "resources/read requires uri")
		}
		text, err := s.readResource(ctx, readParams.URI)
		if err != nil {
			return marshalRPCError(req.ID, rpcErrInvalidParams, err.Error())
		}
		return marshalRPCResult(req.ID, map[string]any{
			"contents": []map[string]any{
				{
					"uri":      readParams.URI,
					"mimeType": mimeTypeMarkdown,
					"text":     text,
				},
			},
		})
	case methodPromptsList:
		return marshalRPCResult(req.ID, map[string]any{
			"prompts": []any{},
		})
	default:
		return marshalRPCError(req.ID, rpcErrMethodNotFound, fmt.Sprintf("unknown method %q", req.Method))
	}
}

// CallTool invokes a named MCP tool with JSON arguments and appends any pending push events.
func (s *Server) CallTool(ctx context.Context, name string, rawArgs json.RawMessage) MCPCallToolResult {
	var text, customTitle, customMarkdown string
	var err error

	switch name {
	case toolDiscoverDeployments:
		text, err = s.handleDiscoverDeployments(ctx, rawArgs)
	case toolConnectDeployment:
		text, err = s.handleConnectDeployment(ctx, rawArgs)
	case toolConnectionStatus:
		text, err = s.handleConnectionStatus(ctx)
	case toolSendMessage:
		text, customTitle, customMarkdown, err = s.handleSendMessage(ctx, rawArgs)
	case toolManageSession:
		text, customTitle, customMarkdown, err = s.handleManageSession(ctx, rawArgs)
	case toolTaskControl:
		text, customTitle, customMarkdown, err = s.handleTaskControl(ctx, rawArgs)
	case toolReadTopics:
		text, err = s.handleReadTopics(ctx, rawArgs)
	case toolInspectPR:
		text, err = s.handleInspectPR(ctx, rawArgs)
	case toolPollEvents:
		text, err = s.handlePollEvents(ctx, rawArgs)
	default:
		return MCPCallToolResult{
			Content: []MCPTextContent{{Type: contentTypeText, Text: fmt.Sprintf("Unknown tool %q", name)}},
			IsError: true,
		}
	}

	// Piggyback pending push events, except (a) on poll_events, which reports them itself, and
	// (b) on intermediate STREAMING_STEP_CONTINUE frames, where a burst of unrelated events would
	// bloat the frame and derail the model's polling loop; they are delivered on the final frame.
	isStreamingStep := strings.HasPrefix(text, streamingStepContinuePrefix)
	if name != toolPollEvents && !isStreamingStep {
		callerID := strings.TrimSpace(callMetaFromContext(ctx).ConversationID)
		if piggyback := s.drainAndFormatPiggybackFor(callerID); piggyback != "" {
			text += piggyback
			if customMarkdown != "" {
				customMarkdown += piggyback
			}
		}
	}

	if err != nil {
		msg := fmt.Sprintf("❌ **Error in `%s`**: %v", name, err)
		if text != "" {
			msg = text + "\n\n" + msg
		}
		return MCPCallToolResult{
			Content: []MCPTextContent{{Type: contentTypeText, Text: msg}},
			IsError: true,
		}
	}
	return MCPCallToolResult{
		Content:        []MCPTextContent{{Type: contentTypeText, Text: text}},
		CustomTitle:    customTitle,
		CustomMarkdown: customMarkdown,
	}
}

func (s *Server) ensureConnected(ctx context.Context) (*BusSession, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, errors.New(errServerClosedMsg)
	}
	cur := s.session
	s.mu.Unlock()
	if cur != nil && cur.WaitForHealthy(ctx, ensureConnectedHealthyWait) {
		return cur, nil
	}

	s.connectMu.Lock()
	defer s.connectMu.Unlock()

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, errors.New(errServerClosedMsg)
	}
	cur = s.session
	s.mu.Unlock()
	if cur != nil && cur.WaitForHealthy(ctx, ensureConnectedLockedWait) {
		return cur, nil
	}

	prof, ok := s.store.ActiveDeployment()
	if !ok {
		return nil, errNoDeploymentsSaved
	}
	return s.connectSessionLocked(ctx, prof)
}

// ensureConnectedAsync repairs an unhealthy or missing bus session in the background without
// blocking the caller. At most one reconnect runs at a time (connectMu); callers that need the
// session synchronously still go through ensureConnected, which waits on that same lock.
func (s *Server) ensureConnectedAsync() {
	s.mu.Lock()
	closed := s.closed
	cur := s.session
	s.mu.Unlock()
	if closed || (cur != nil && cur.IsHealthy()) {
		return
	}
	if !s.connectMu.TryLock() {
		return
	}
	go func() {
		defer s.connectMu.Unlock()
		ctx, cancel := context.WithTimeout(context.Background(), asyncReconnectTimeout)
		defer cancel()
		s.mu.Lock()
		closed := s.closed
		cur := s.session
		s.mu.Unlock()
		if closed || (cur != nil && cur.WaitForHealthy(ctx, ensureConnectedLockedWait)) {
			return
		}
		prof, ok := s.store.ActiveDeployment()
		if !ok {
			return
		}
		_, _ = s.connectSessionLocked(ctx, prof)
	}()
}

func (s *Server) runKeepAliveLoop(ctx context.Context) {
	initTimer := time.NewTimer(keepAliveInitialDelay)
	select {
	case <-ctx.Done():
		initTimer.Stop()
		return
	case <-initTimer.C:
		s.keepAliveTick(ctx)
	}

	ticker := time.NewTicker(keepAliveTickInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.keepAliveTick(ctx)
		}
	}
}

// keepAliveTick runs one background maintenance pass: it auto-connects or heals the active
// deployment's NATS session when idle and refreshes /session pod liveness.
func (s *Server) keepAliveTick(ctx context.Context) {
	if s == nil || ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()

	if sess == nil || !sess.IsHealthy() {
		s.ensureConnectedAsync()
		return
	}

	probeCtx, cancel := context.WithTimeout(ctx, keepAliveProbeTimeout)
	sess.DeriveActiveSessionPods(probeCtx)
	cancel()
	if s.store != nil {
		_ = s.store.UpsertDeployments([]DeploymentProfile{sess.Profile()})
	}
}

func (s *Server) connectSession(ctx context.Context, prof DeploymentProfile) (*BusSession, error) {
	s.connectMu.Lock()
	defer s.connectMu.Unlock()
	return s.connectSessionLocked(ctx, prof)
}

func (s *Server) connectSessionLocked(ctx context.Context, prof DeploymentProfile) (*BusSession, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, errors.New(errServerClosedMsg)
	}
	s.mu.Unlock()
	newSess, err := ConnectBusSession(ctx, prof, s.enqueuePushEvent)
	if err != nil {
		return nil, err
	}
	newSess.DeriveActiveSessionPods(ctx)
	if s.store != nil {
		_ = s.store.UpsertDeployments([]DeploymentProfile{newSess.Profile()})
		newSess.EnableStepProgressPersistence(s.store.StepProgressPath())
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		newSess.Close()
		return nil, errors.New(errServerClosedMsg)
	}
	oldSess := s.session
	s.session = newSess
	s.mu.Unlock()
	if oldSess != nil {
		oldSess.Close()
	}
	return newSess, nil
}

func marshalRPCResult(id json.RawMessage, result any) []byte {
	raw, _ := json.Marshal(rpcResponse{
		JSONRPC: jsonRPCVersion,
		ID:      id,
		Result:  result,
	})
	return raw
}

func marshalRPCError(id json.RawMessage, code int, msg string) []byte {
	raw, _ := json.Marshal(rpcResponse{
		JSONRPC: jsonRPCVersion,
		ID:      id,
		Error: &rpcError{
			Code:    code,
			Message: msg,
		},
	})
	return raw
}
