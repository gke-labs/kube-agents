package mcpbridge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
	"github.com/gke-labs/kube-agents/a2a/mcpproto"
)

// Tool names exposed by the kube-agents MCP bridge.
const (
	ToolDiscoverDeployments = "discover_deployments"
	ToolConnectDeployment   = "connect_deployment"
	ToolConnectionStatus    = "connection_status"
	ToolSendMessage         = "send_message"
	ToolManageSession       = "manage_session"
	ToolTaskControl         = "task_control"
	ToolReadTopics          = "read_topics"
	ToolInspectPR           = "inspect_pr"
	ToolPollEvents          = "poll_events"
)

const (
	jsonRPCVersion              = mcpproto.JSONRPCVersion
	defaultMCPProtocolVersion   = "2024-11-05"
	mcpServerName               = "kube-agents"
	mcpServerVersion            = "0.5.0"
	remoteStopDrainTimeout      = 6 * time.Second
	methodInitialize            = mcpproto.MethodInitialize
	methodInitialized           = mcpproto.NotificationInitialized
	methodCancelledNotification = mcpproto.NotificationCancelled
	methodProgressNotification  = mcpproto.NotificationProgress
	methodPing                  = mcpproto.MethodPing
	methodToolsList             = mcpproto.MethodToolsList
	methodToolsCall             = mcpproto.MethodToolsCall
	methodResourcesList         = mcpproto.MethodResourcesList
	methodResourcesRead         = mcpproto.MethodResourcesRead
	methodPromptsList           = mcpproto.MethodPromptsList
	resourceURIStatus           = "kube-agents://status"
	resourceURIDeployments      = "kube-agents://deployments"
	resourceURITopics           = "kube-agents://topics"
	resourceURITasks            = "kube-agents://tasks"
	resourceURIEvents           = "kube-agents://events"
	mimeTypeMarkdown            = "text/markdown"
	contentTypeText             = mcpproto.ContentTypeText
	contentTypeJSON             = "application/json"
	contentTypeNDJSON           = "application/x-ndjson"
	headerProtocolVersion       = "MCP-Protocol-Version"
	rpcErrParseError            = mcpproto.ErrParseError
	rpcErrInvalidRequest        = mcpproto.ErrInvalidRequest
	rpcErrMethodNotFound        = mcpproto.ErrMethodNotFound
	rpcErrInvalidParams         = mcpproto.ErrInvalidParams
	rpcErrInternal              = mcpproto.ErrInternal
	scannerInitialBufSize       = 64 * 1024
	scannerMaxBufSize           = 16 * 1024 * 1024
	actionList                  = "list"
	actionNew                   = "new"
	actionSwitch                = "switch"
	actionSessionOn             = "session_on"
	actionSessionStartAlias     = "start"
	actionSessionOff            = "session_off"
	actionSessionEndAlias       = "end"
	actionDelete                = "delete"
	actionRemoveAlias           = "remove"
	actionClearKilled           = "clear_killed"
	actionPruneAlias            = "prune"
	actionHelp                  = "help"
	actionTranscript            = "transcript"
	actionDebugOn               = "debug_on"
	actionDebugOff              = "debug_off"
	asyncReconnectTimeout       = 60 * time.Second
	keepAliveInitialDelay       = 500 * time.Millisecond
	keepAliveTickInterval       = 15 * time.Second
	keepAliveProbeTimeout       = 5 * time.Second
	remoteMCPPath               = "/mcp"
	remoteMCPSwarmPath          = "/mcp/swarm"
	remoteMCPHealthPath         = "/healthz"
	StreamingStepContinuePrefix = "⏳ **[STREAMING_STEP_CONTINUE]**"
	// staleStreamingTaskAge is how long a tracked task no call has touched is kept.
	staleStreamingTaskAge = 10 * time.Minute
)

type (
	rpcRequest  = mcpproto.Request
	rpcResponse = mcpproto.Response
	rpcError    = mcpproto.Error

	// ToolDefinition describes a single tool exposed by the MCP server.
	ToolDefinition = mcpproto.ToolDefinition
	// TextContent represents a text content block inside an MCP tool result.
	TextContent = mcpproto.TextContent
)

// CallToolResult is the result of a tools/call invocation.
type CallToolResult struct {
	Content        []TextContent `json:"content"`
	IsError        bool          `json:"isError,omitempty"`
	CustomTitle    string        `json:"-"`
	CustomMarkdown string        `json:"-"`
}

// Resource describes an MCP resource URI exposed by the server.
type Resource struct {
	URI         string `json:"uri"`
	Name        string `json:"name"`
	Description string `json:"description,omitempty"`
	MIMEType    string `json:"mimeType,omitempty"`
}

// CallMeta carries per-call MCP request metadata (_meta).
type CallMeta struct {
	ProgressToken  any
	ConversationID string
	ArtifactsDir   string
	Raw            map[string]any
}

// UnmarshalJSON decodes _meta fields from standard and vendor-namespaced keys.
func (m *CallMeta) UnmarshalJSON(data []byte) error {
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	m.Raw = raw
	if tok, ok := raw["progressToken"]; ok {
		m.ProgressToken = tok
	}
	for k, v := range raw {
		s, ok := v.(string)
		if !ok || strings.TrimSpace(s) == "" {
			continue
		}
		switch {
		case k == "conversation_id" || strings.HasSuffix(k, "/conversation_id"):
			m.ConversationID = strings.TrimSpace(s)
		case k == "artifacts_dir" || strings.HasSuffix(k, "/artifacts_dir"):
			m.ArtifactsDir = strings.TrimSpace(s)
		}
	}
	return nil
}

// HostIntegration allows an IDE or host wrapper to attach host-specific capabilities
// (such as swarm UI projection, subagent lineage tracking, and push notification wakeups)
// without coupling the workstation MCP bridge to any proprietary host environment.
type HostIntegration interface {
	AugmentToolSchemas(defs []ToolDefinition) []ToolDefinition
	OnBeforeToolCall(ctx context.Context, srv *Server, name string, rawArgs json.RawMessage, meta CallMeta)
	ResolveConversation(ctx context.Context, srv *Server, convArg string, rawArgs json.RawMessage) (ConversationEntry, bool)
	OnConnectDeployment(ctx context.Context, srv *Server, prof *DeploymentProfile, conv ConversationEntry, rawArgs json.RawMessage, meta CallMeta)
	HandleManageSessionAction(ctx context.Context, srv *Server, action, alias, description string, rawArgs json.RawMessage, meta CallMeta) (handled bool, text, title, md string, err error)
	HandlePollEvents(ctx context.Context, srv *Server, rawArgs json.RawMessage) (string, error)
	OnAfterToolCall(ctx context.Context, srv *Server, name string, isStreamingStep bool, text, customMarkdown string) (string, string)
	OnKeepAliveTick(ctx context.Context, srv *Server) (skipAutoConnect bool)
	IsCascadeCanceled(ctx context.Context, srv *Server, hostConvID, artifactsDir string) bool
	Close()
}

// ServerOptions configures an MCP Server instance.
type ServerOptions struct {
	Store *ConfigStore
	// Cluster supplies Kubernetes discovery and port-forwarding; nil disables them.
	Cluster    ClusterAccess
	Runner     CommandRunner
	APIBaseURL string
	Host       HostIntegration
}

type gatewayBridgeSession struct {
	mu          sync.Mutex
	profile     DeploymentProfile
	tunnel      Tunnel
	baseURL     string
	bearerToken string
	httpClient  *http.Client
	closed      bool
}

func validateDirectMCPURL(rawURL string) (string, error) {
	trimmed := strings.TrimSpace(rawURL)
	if trimmed == "" {
		return "", errors.New("empty direct MCP URL")
	}
	u, err := url.Parse(trimmed)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", fmt.Errorf("invalid direct MCP URL %q: must be an https:// URL or a loopback http:// URL", rawURL)
	}
	scheme := strings.ToLower(u.Scheme)
	host := strings.ToLower(u.Hostname())
	// The URL is the server's base: requests append remoteMCPPath and its siblings. The
	// endpoint the docs name (".../mcp") is accepted too, as its base, not posted to /mcp/mcp.
	base := strings.TrimRight(trimmed, "/")
	if u.RawQuery == "" && u.Fragment == "" && strings.HasSuffix(strings.TrimRight(u.Path, "/"), remoteMCPPath) {
		base = strings.TrimRight(strings.TrimSuffix(base, remoteMCPPath), "/")
	}
	switch scheme {
	case "https":
		return base, nil
	case "http":
		if host == "127.0.0.1" || host == "localhost" || host == "::1" {
			return base, nil
		}
		return "", fmt.Errorf("refusing cleartext direct MCP URL %q on non-loopback host %q; use https:// or a loopback address", rawURL, host)
	default:
		return "", fmt.Errorf("unsupported direct MCP URL scheme %q in %q; use https:// or loopback http://", u.Scheme, rawURL)
	}
}

// connectGatewayBridge connects to prof. saved holds every saved deployment profile, so
// EnvMCPTokenFor can tell whether the KUBE_AGENTS_MCP_TOKEN override is prof's and prof still
// points at that profile's saved target.
func connectGatewayBridge(ctx context.Context, cluster ClusterAccess, prof DeploymentProfile, saved map[string]DeploymentProfile) (*gatewayBridgeSession, error) {
	direct := strings.TrimSpace(prof.DirectMCPURL)
	var tun Tunnel
	var baseURL string
	var bearer string

	if direct != "" {
		validatedURL, err := validateDirectMCPURL(direct)
		if err != nil {
			return nil, err
		}
		baseURL = validatedURL
		// Never populate or attach cluster-fetched Secret tokens to a DirectMCPURL; only use
		// an explicit in-memory token or the KUBE_AGENTS_MCP_TOKEN override that is prof's.
		bearer = FirstNonEmptyStr(strings.TrimSpace(prof.MCPToken), EnvMCPTokenFor(prof, saved))
	} else {
		if strings.TrimSpace(prof.MCPToken) == "" {
			if envTok := EnvMCPTokenFor(prof, saved); envTok != "" {
				prof.MCPToken = envTok
			} else if prof.KubeContext != "" && prof.Namespace != "" {
				if err := cluster.PopulateDeploymentCredentials(ctx, &prof); err != nil {
					return nil, err
				}
			}
		}
		bearer = strings.TrimSpace(prof.MCPToken)
		pf, err := cluster.StartGatewayPortForward(ctx, prof)
		if err != nil {
			return nil, err
		}
		tun = pf
		baseURL = pf.MCPURL()
	}

	httpClient := &http.Client{}
	if st, ok := tun.(StreamTunnel); ok {
		// Every connection is a stream the tunnel opens through the kube API server: no proxy,
		// no DNS, no local socket.
		httpClient = &http.Client{Transport: &http.Transport{
			Proxy:               nil,
			DialContext:         st.DialContext,
			MaxIdleConnsPerHost: 4,
			IdleConnTimeout:     90 * time.Second,
		}}
	}
	return &gatewayBridgeSession{
		profile:     prof,
		tunnel:      tun,
		baseURL:     baseURL,
		bearerToken: bearer,
		httpClient:  httpClient,
	}, nil
}

func (g *gatewayBridgeSession) Profile() DeploymentProfile {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.profile
}

func (g *gatewayBridgeSession) UpdateProfile(p DeploymentProfile) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if p.MCPToken == "" {
		p.MCPToken = g.profile.MCPToken
	}
	g.profile = p
}

func (g *gatewayBridgeSession) IsHealthy() bool {
	if g == nil {
		return false
	}
	g.mu.Lock()
	closed := g.closed
	baseURL := g.baseURL
	g.mu.Unlock()
	return !closed && baseURL != ""
}

func (g *gatewayBridgeSession) WaitForHealthy(ctx context.Context, timeout time.Duration) bool {
	if !g.IsHealthy() {
		return false
	}
	probeCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, g.baseURL+remoteMCPHealthPath, nil)
	if err != nil {
		return false
	}
	resp, err := g.httpClient.Do(req)
	if err != nil {
		// The probe's own timeout is a tunnel failure; the caller's cancel is not.
		if tun := g.currentTunnel(); tun != nil && ctx.Err() == nil {
			tun.Restart()
		}
		return false
	}
	_ = resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// currentTunnel is the session's tunnel, or nil once Close has taken it. Read it once into a
// local: Close may nil the field between a check and a call.
func (g *gatewayBridgeSession) currentTunnel() Tunnel {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.tunnel
}

func (g *gatewayBridgeSession) Close() {
	if g == nil {
		return
	}
	g.mu.Lock()
	if g.closed {
		g.mu.Unlock()
		return
	}
	g.closed = true
	tun := g.tunnel
	g.tunnel = nil
	g.mu.Unlock()
	if g.httpClient != nil {
		g.httpClient.CloseIdleConnections()
	}
	if tun != nil {
		tun.Close()
	}
}

func (g *gatewayBridgeSession) callRemoteTool(
	ctx context.Context,
	toolName string,
	rawArgs json.RawMessage,
	meta CallMeta,
	snap PersistentConfig,
	onProgress func(step int, msg string),
) (string, string, string, error) {
	var argsAny any = map[string]any{}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &argsAny)
	}
	// Only the called conversation's entry goes upstream, never the rest of the local table
	// (the in-cluster server reads nothing from _meta today).
	metaMap := map[string]any{}
	if alias := calledConversation(argsAny); alias != "" {
		if entry, ok := snap.Conversations[alias]; ok {
			one := map[string]ConversationEntry{alias: entry}
			metaMap["conversations"] = one
			metaMap["kube-agents/conversations"] = one
		}
	}
	for k, v := range meta.Raw {
		metaMap[k] = v
	}
	if meta.ConversationID != "" {
		metaMap["conversation_id"] = meta.ConversationID
	}
	if meta.ArtifactsDir != "" {
		metaMap["artifacts_dir"] = meta.ArtifactsDir
	}
	if meta.ProgressToken != nil {
		metaMap["progressToken"] = meta.ProgressToken
	} else {
		metaMap["progressToken"] = "bridge-progress"
	}

	rpcPayload, err := json.Marshal(map[string]any{
		"jsonrpc": jsonRPCVersion,
		"id":      1,
		"method":  methodToolsCall,
		"params": map[string]any{
			"name":      toolName,
			"arguments": argsAny,
			"_meta":     metaMap,
		},
	})
	if err != nil {
		return "", "", "", err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.baseURL+remoteMCPPath, bytes.NewReader(rpcPayload))
	if err != nil {
		return "", "", "", err
	}
	req.Header.Set("Content-Type", contentTypeJSON)
	req.Header.Set("Accept", contentTypeNDJSON)
	if versions := mcpproto.SupportedProtocolVersions(); len(versions) > 0 {
		req.Header.Set(headerProtocolVersion, versions[0])
	}
	if g.bearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+g.bearerToken)
	}

	resp, err := g.httpClient.Do(req)
	if err != nil {
		// The caller's own cancel or deadline says nothing about the tunnel, and restarting it
		// would reset every other stream on the shared connection (a concurrent stop among them).
		if tun := g.currentTunnel(); tun != nil && ctx.Err() == nil {
			tun.Restart()
		}
		return "", "", "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", "", "", fmt.Errorf("a2a-mcp HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var finalResp rpcResponse
	gotFinal := false
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, scannerInitialBufSize), scannerMaxBufSize)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var frame struct {
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			ID     json.RawMessage `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *rpcError       `json:"error"`
		}
		if json.Unmarshal([]byte(line), &frame) != nil {
			continue
		}
		if frame.Method == methodProgressNotification && onProgress != nil {
			var prog struct {
				Progress int    `json:"progress"`
				Message  string `json:"message"`
			}
			if json.Unmarshal(frame.Params, &prog) == nil && prog.Message != "" {
				onProgress(prog.Progress, prog.Message)
			}
			continue
		}
		if len(frame.ID) > 0 && string(frame.ID) != "null" {
			_ = json.Unmarshal([]byte(line), &finalResp)
			gotFinal = true
		}
	}
	if err := scanner.Err(); err != nil && !gotFinal {
		return "", "", "", fmt.Errorf("reading a2a-mcp response: %w", err)
	}
	if !gotFinal {
		return "", "", "", fmt.Errorf("empty JSON-RPC response from a2a-mcp")
	}
	if finalResp.Error != nil {
		return "", "", "", fmt.Errorf("%s (code %d)", finalResp.Error.Message, finalResp.Error.Code)
	}
	rawRes, err := json.Marshal(finalResp.Result)
	if err != nil {
		return "", "", "", err
	}
	var toolRes mcpproto.CallToolResult
	if err := json.Unmarshal(rawRes, &toolRes); err != nil {
		return "", "", "", err
	}
	text := ""
	if len(toolRes.Content) > 0 {
		text = toolRes.Content[0].Text
	}
	var customTitle, customMarkdown string
	var customEnv struct {
		Custom bool   `json:"_custom_tool_result"`
		Title  string `json:"title"`
		Result string `json:"result"`
		MD     string `json:"markdown"`
	}
	if strings.HasPrefix(strings.TrimSpace(text), "{") && json.Unmarshal([]byte(text), &customEnv) == nil && customEnv.Custom {
		text = customEnv.Result
		customTitle = customEnv.Title
		customMarkdown = customEnv.MD
	}
	if toolRes.IsError {
		return text, customTitle, customMarkdown, fmt.Errorf("%s", text)
	}
	return text, customTitle, customMarkdown, nil
}

// calledConversation is the local conversation alias a forwarded call names: its conversation
// argument, or manage_session's alias.
func calledConversation(args any) string {
	m, _ := args.(map[string]any)
	for _, key := range []string{"conversation", "alias"} {
		if v, _ := m[key].(string); strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func (g *gatewayBridgeSession) queryRemoteSwarm(ctx context.Context, swarmReq any, swarmResp any) error {
	payload, err := json.Marshal(swarmReq)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.baseURL+remoteMCPSwarmPath, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", contentTypeJSON)
	if g.bearerToken != "" {
		req.Header.Set("Authorization", "Bearer "+g.bearerToken)
	}
	resp, err := g.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("a2a-mcp /mcp/swarm HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.NewDecoder(resp.Body).Decode(swarmResp)
}

// Server implements the kube-agents Model Context Protocol (MCP) thin bridge over stdio.
type Server struct {
	store      *ConfigStore
	cluster    ClusterAccess
	runner     CommandRunner
	apiBaseURL string
	host       HostIntegration

	mu        sync.Mutex
	connectMu sync.Mutex
	// bgWG tracks the remote task stops issued when a streaming call is cancelled, so the
	// stdio entry point can drain them before the process exits. handlerWG tracks ServeStdio's
	// in-flight request handlers, which issue those stops as they return, so a signal can wait
	// for them first.
	bgWG      sync.WaitGroup
	handlerWG sync.WaitGroup
	session   *gatewayBridgeSession
	// override is the target of the last connect_deployment that overrode its deployment's
	// kube_context or namespace, nil when the last one did not. Such a connection is never
	// saved, so a reconnect goes back to override rather than to the store's active deployment;
	// it lasts for the life of the process.
	override             *DeploymentProfile
	inFlightCancels      map[string]context.CancelFunc
	activeStreamingTasks map[string]*activeStreamingTask
	writeMu              sync.Mutex
	notifyOut            io.Writer
}

type activeStreamingTask struct {
	TaskID       string
	ConvAlias    string
	HostConvID   string
	ArtifactsDir string
	InFlight     bool
	CancelCall   context.CancelFunc
	// BridgeCancelled marks an in-flight call the bridge cancelled itself and sent (or is
	// sending) the stop for, so the cancelled call does not send a second one.
	BridgeCancelled bool
	StartedAt       time.Time
	UpdatedAt       time.Time
}

type callMetaContextKey struct{}
type progressCallbackKey struct{}

// WithCallMeta attaches CallMeta to ctx.
func WithCallMeta(ctx context.Context, meta CallMeta) context.Context {
	return context.WithValue(ctx, callMetaContextKey{}, meta)
}

// CallMetaFromContext returns the CallMeta attached to ctx, if any.
func CallMetaFromContext(ctx context.Context) CallMeta {
	if ctx == nil {
		return CallMeta{}
	}
	if m, ok := ctx.Value(callMetaContextKey{}).(CallMeta); ok {
		return m
	}
	return CallMeta{}
}

// WithProgressCallback attaches a progress callback to ctx for streaming tool progress updates.
func WithProgressCallback(ctx context.Context, fn func(step int, message string)) context.Context {
	return context.WithValue(ctx, progressCallbackKey{}, fn)
}

func progressCallbackFromContext(ctx context.Context) func(step int, message string) {
	if ctx == nil {
		return nil
	}
	if fn, ok := ctx.Value(progressCallbackKey{}).(func(int, string)); ok {
		return fn
	}
	return nil
}

// NewServer constructs a new kube-agents MCP bridge server.
func NewServer(opts ServerOptions) (*Server, error) {
	store := opts.Store
	if store == nil {
		var err error
		store, err = NewConfigStore("")
		if err != nil {
			return nil, err
		}
	}
	cluster := opts.Cluster
	if cluster == nil {
		cluster = noClusterAccess{}
	}
	return &Server{
		store:                store,
		cluster:              cluster,
		runner:               opts.Runner,
		apiBaseURL:           opts.APIBaseURL,
		host:                 opts.Host,
		inFlightCancels:      make(map[string]context.CancelFunc),
		activeStreamingTasks: make(map[string]*activeStreamingTask),
	}, nil
}

// Store returns the server's ConfigStore.
func (s *Server) Store() *ConfigStore {
	if s == nil {
		return nil
	}
	return s.store
}

// Cluster returns the server's ClusterAccess implementation.
func (s *Server) Cluster() ClusterAccess {
	if s == nil {
		return noClusterAccess{}
	}
	return s.cluster
}

// SessionProfile returns the active connected DeploymentProfile, if any.
func (s *Server) SessionProfile() (DeploymentProfile, bool) {
	if s == nil {
		return DeploymentProfile{}, false
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	if sess == nil {
		return DeploymentProfile{}, false
	}
	return sess.Profile(), true
}

// UpdateSessionProfile updates the in-memory profile on the active session.
func (s *Server) UpdateSessionProfile(prof DeploymentProfile) {
	if s == nil {
		return
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	if sess != nil {
		sess.UpdateProfile(prof)
	}
}

// IsSessionHealthy reports whether the server has a healthy active session.
func (s *Server) IsSessionHealthy() bool {
	if s == nil {
		return false
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	return sess != nil && sess.IsHealthy()
}

// EnsureConnected ensures an active session to the current deployment is established.
func (s *Server) EnsureConnected(ctx context.Context) error {
	_, err := s.ensureConnected(ctx)
	return err
}

// CallRemoteTool invokes toolName on the connected in-cluster a2a-mcp server. rawArgs are in the
// bridge's argument names: they get the same gating (a refused action or mode is refused before
// connecting) and the same translation to the server's names as the bridge's own tools.
func (s *Server) CallRemoteTool(ctx context.Context, toolName string, rawArgs json.RawMessage, meta CallMeta, onProgress func(int, string)) (string, string, string, error) {
	fwd, err := s.upstreamArgs(ctx, toolName, rawArgs)
	if err != nil {
		return "", "", "", err
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	return sess.callRemoteTool(ctx, toolName, fwd, meta, s.store.Snapshot(), onProgress)
}

// QueryRemoteSwarm posts swarmReq to the connected in-cluster a2a-mcp /mcp/swarm endpoint.
func (s *Server) QueryRemoteSwarm(ctx context.Context, swarmReq any, swarmResp any) error {
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()
	if sess == nil || !sess.IsHealthy() {
		return errNotConnected
	}
	return sess.queryRemoteSwarm(ctx, swarmReq, swarmResp)
}

// Close shuts down the active bridge session and any attached HostIntegration.
func (s *Server) Close() {
	s.mu.Lock()
	for id, cancel := range s.inFlightCancels {
		cancel()
		delete(s.inFlightCancels, id)
	}
	for id, st := range s.activeStreamingTasks {
		if st != nil && st.CancelCall != nil {
			st.CancelCall()
		}
		delete(s.activeStreamingTasks, id)
	}
	sess := s.session
	s.session = nil
	host := s.host
	s.mu.Unlock()
	if sess != nil {
		sess.Close()
	}
	if host != nil {
		host.Close()
	}
}

func normalizeRPCID(raw json.RawMessage) string {
	s := strings.TrimSpace(string(raw))
	return strings.Trim(s, `"`)
}

// isAsyncRPCMethod reports whether a request must be served off the stdio read loop so that
// ping and notifications/cancelled keep flowing while it runs.
func isAsyncRPCMethod(method string) bool {
	switch method {
	case methodToolsCall, methodResourcesRead:
		return true
	default:
		return false
	}
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
	go s.runKeepAliveLoop(loopCtx)

	var wg sync.WaitGroup
	defer func() {
		cancelLoop()
		wg.Wait()
	}()

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
		rawBytes := []byte(line)
		var peek rpcRequest
		if json.Unmarshal(rawBytes, &peek) == nil && isAsyncRPCMethod(peek.Method) && len(peek.ID) > 0 && string(peek.ID) != "null" {
			reqKey := normalizeRPCID(peek.ID)
			reqCtx, cancelReq := context.WithCancel(loopCtx)
			s.mu.Lock()
			if s.inFlightCancels == nil {
				s.inFlightCancels = make(map[string]context.CancelFunc)
			}
			s.inFlightCancels[reqKey] = cancelReq
			s.mu.Unlock()

			wg.Add(1)
			s.handlerWG.Add(1)
			go func(rCtx context.Context, rCancel context.CancelFunc, key string, payload []byte) {
				defer wg.Done()
				defer s.handlerWG.Done()
				defer func() {
					rCancel()
					s.mu.Lock()
					delete(s.inFlightCancels, key)
					s.mu.Unlock()
				}()
				resp := s.HandleJSONRPC(rCtx, payload)
				if len(resp) == 0 {
					return
				}
				s.writeMu.Lock()
				_, _ = fmt.Fprintf(w, "%s\n", resp)
				s.writeMu.Unlock()
			}(reqCtx, cancelReq, reqKey, rawBytes)
			continue
		}

		resp := s.HandleJSONRPC(ctx, rawBytes)
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

// RunCLI parses CLI flags (--config, --version) and runs the stdio MCP server with clean signal handling.
func RunCLI(args []string, cluster ClusterAccess) error {
	return RunCLIWithOptions(args, ServerOptions{Cluster: cluster})
}

// RunCLIWithOptions parses CLI flags (--config, --version) and runs the stdio MCP server using opts.
func RunCLIWithOptions(args []string, opts ServerOptions) error {
	fs := flag.NewFlagSet("mcp", flag.ContinueOnError)
	configPath := fs.String("config", "", "path to persistent MCP configuration file (default: ~/.config/kube-agents/mcp-config.json)")
	versionMode := fs.Bool("version", false, "print kube-agents-mcp version and exit")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *versionMode || (fs.NArg() > 0 && fs.Arg(0) == "version") {
		fmt.Printf("%s %s\n", mcpServerName, mcpServerVersion)
		return nil
	}
	if opts.Store == nil {
		store, err := NewConfigStore(*configPath)
		if err != nil {
			return err
		}
		opts.Store = store
	}
	rpcOut := os.Stdout
	os.Stdout = os.Stderr
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})))

	srv, err := NewServer(opts)
	if err != nil {
		return err
	}
	defer srv.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return srv.serveStdioUntilDone(ctx, os.Stdin, rpcOut)
}

// serveStdioUntilDone runs ServeStdio until stdin closes or ctx ends (SIGINT/SIGTERM), and
// either way waits, at most remoteStopDrainTimeout in all, for the remote stops that cancelled
// calls issue, so the turns they were waiting on do not keep running in the cluster. A signal
// is an orderly shutdown and returns nil, so the process exits 0.
func (s *Server) serveStdioUntilDone(ctx context.Context, r io.Reader, w io.Writer) error {
	served := make(chan error, 1)
	go func() { served <- s.ServeStdio(ctx, r, w) }()
	select {
	case err := <-served:
		s.waitBackground(remoteStopDrainTimeout)
		if ctx.Err() != nil && errors.Is(err, ctx.Err()) {
			// ServeStdio saw the signal first, reading a frame queued with it.
			return nil
		}
		return err
	case <-ctx.Done():
		// ServeStdio stays blocked reading stdin. The cancelled calls' handlers queue their
		// remote stops on bgWG as they return, so wait for the handlers, then for the stops.
		deadline := time.Now().Add(remoteStopDrainTimeout)
		waitGroupUntil(&s.handlerWG, deadline)
		waitGroupUntil(&s.bgWG, deadline)
		return nil
	}
}

func (s *Server) waitBackground(timeout time.Duration) {
	if s == nil {
		return
	}
	waitGroupUntil(&s.bgWG, time.Now().Add(timeout))
}

// waitGroupUntil waits for wg, or until deadline.
func waitGroupUntil(wg *sync.WaitGroup, deadline time.Time) {
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case <-done:
	case <-timer.C:
	}
}

func (s *Server) emitProgress(progressToken any, step int, message string) {
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
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.notifyOut != nil {
		_, _ = fmt.Fprintf(s.notifyOut, "%s\n", frame)
	}
}

// HandleJSONRPC processes a single JSON-RPC 2.0 request frame and returns the marshaled response.
func (s *Server) HandleJSONRPC(ctx context.Context, raw []byte) []byte {
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return marshalRPCError(json.RawMessage("null"), rpcErrParseError, "invalid JSON-RPC frame")
	}
	if len(req.ID) == 0 || string(req.ID) == "null" {
		if req.Method == methodCancelledNotification {
			var cancelParams struct {
				RequestID json.RawMessage `json:"requestId"`
			}
			if len(req.Params) > 0 && json.Unmarshal(req.Params, &cancelParams) == nil {
				reqKey := normalizeRPCID(cancelParams.RequestID)
				s.mu.Lock()
				cancelFn := s.inFlightCancels[reqKey]
				s.mu.Unlock()
				if cancelFn != nil {
					cancelFn()
				}
			}
			return nil
		}
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
			supported := mcpproto.SupportedProtocolVersions()
			if slices.Contains(supported, initParams.ProtocolVersion) {
				protoVer = initParams.ProtocolVersion
			} else if len(supported) > 0 {
				protoVer = supported[0]
			}
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
			Meta      CallMeta        `json:"_meta"`
		}
		if err := json.Unmarshal(req.Params, &callParams); err != nil || callParams.Name == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, "tools/call requires tool name")
		}
		if s.host != nil {
			s.host.OnBeforeToolCall(ctx, s, callParams.Name, callParams.Arguments, callParams.Meta)
		}
		callCtx := WithCallMeta(ctx, callParams.Meta)
		if callParams.Meta.ProgressToken != nil {
			token := callParams.Meta.ProgressToken
			callCtx = WithProgressCallback(callCtx, func(step int, message string) {
				s.emitProgress(token, step, message)
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

// CallTool invokes a named MCP tool with JSON arguments.
func (s *Server) CallTool(ctx context.Context, name string, rawArgs json.RawMessage) CallToolResult {
	var text, customTitle, customMarkdown string
	var err error

	switch name {
	case ToolDiscoverDeployments:
		text, err = s.handleDiscoverDeployments(ctx, rawArgs)
	case ToolConnectDeployment:
		text, err = s.handleConnectDeployment(ctx, rawArgs)
	case ToolConnectionStatus:
		text, err = s.handleConnectionStatus(ctx)
	case ToolSendMessage:
		text, customTitle, customMarkdown, err = s.handleSendMessage(ctx, rawArgs)
	case ToolManageSession:
		text, customTitle, customMarkdown, err = s.handleManageSession(ctx, rawArgs)
	case ToolTaskControl:
		text, customTitle, customMarkdown, err = s.handleTaskControl(ctx, rawArgs)
	case ToolReadTopics:
		text, err = s.handleReadTopics(ctx, rawArgs)
	case ToolInspectPR:
		text, err = s.handleInspectPR(ctx, rawArgs)
	case ToolPollEvents:
		if s.host != nil {
			text, err = s.host.HandlePollEvents(ctx, s, rawArgs)
		} else {
			return CallToolResult{
				Content: []TextContent{{Type: contentTypeText, Text: fmt.Sprintf("Unknown tool %q", name)}},
				IsError: true,
			}
		}
	default:
		return CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: fmt.Sprintf("Unknown tool %q", name)}},
			IsError: true,
		}
	}

	isStreamingStep := strings.HasPrefix(text, StreamingStepContinuePrefix)
	if s.host != nil {
		text, customMarkdown = s.host.OnAfterToolCall(ctx, s, name, isStreamingStep, text, customMarkdown)
	}

	if err != nil {
		msg := fmt.Sprintf("❌ **Error in `%s`**: %v", name, err)
		if text != "" && text != err.Error() {
			msg = text + "\n\n" + msg
		}
		return CallToolResult{
			Content: []TextContent{{Type: contentTypeText, Text: msg}},
			IsError: true,
		}
	}
	return CallToolResult{
		Content:        []TextContent{{Type: contentTypeText, Text: text}},
		CustomTitle:    customTitle,
		CustomMarkdown: customMarkdown,
	}
}

func (s *Server) handleDiscoverDeployments(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		KubeContext string `json:"kube_context"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	discovered, discErr := s.cluster.DiscoverDeployments(ctx, args.KubeContext)
	if s.store != nil {
		switch {
		case strings.TrimSpace(args.KubeContext) == "" && (discErr == nil || len(discovered) > 0):
			// A clean full scan that found nothing (no contexts, or no installs left) still
			// prunes; unreachable contexts come back as probe failures and keep their profiles.
			_ = s.store.SyncDiscoveredDeployments(discovered)
		case len(discovered) > 0:
			_ = s.store.UpsertDeployments(discovered)
		}
	}
	var all []DeploymentProfile
	activeName := ""
	if s.store != nil {
		snap := s.store.Snapshot()
		activeName = snap.ActiveDeployment
		for _, p := range snap.Deployments {
			all = append(all, p)
		}
		sort.Slice(all, func(i, j int) bool {
			if all[i].Ready != all[j].Ready {
				return all[i].Ready
			}
			return all[i].Name < all[j].Name
		})
		all = append(all, ProbeFailures(discovered)...)
	} else {
		all = discovered
	}
	if discErr != nil && len(all) == 0 {
		return "", discErr
	}
	out := FormatDeploymentsMarkdown(all, activeName)
	if discErr != nil && len(discovered) == 0 {
		out = fmt.Sprintf("⚠️ **Live kubeconfig scan failed**: %v (showing saved deployment profiles below).\n\n%s", discErr, out)
	}
	return out, nil
}

func (s *Server) handleConnectDeployment(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		Deployment  string `json:"deployment"`
		KubeContext string `json:"kube_context"`
		Namespace   string `json:"namespace"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}

	prof, err := s.store.SelectDeployment(args.Deployment)
	if errors.Is(err, errDeploymentAmbiguous) {
		// Discovery cannot make an ambiguous name unambiguous; say which deployments it matches.
		return "", err
	}
	if err != nil {
		discovered, dErr := s.cluster.DiscoverDeployments(ctx, args.KubeContext)
		deployments := Deployments(discovered)
		if len(discovered) > 0 {
			_ = s.store.UpsertDeployments(discovered)
		}
		if len(deployments) > 0 {
			prof, err = s.store.SelectDeployment(args.Deployment)
		} else if dErr == nil && len(discovered) > 0 {
			dErr = NewPartialDiscoveryError(ProbeFailures(discovered))
		}
		if err != nil {
			if dErr != nil {
				return "", fmt.Errorf("%w (discovery error: %v)", err, dErr)
			}
			return "", err
		}
	}
	// kube_context filters discovery by case-insensitive substring; a profile whose context it
	// already matches keeps that full context name, which is the one the kubeconfig knows.
	// Otherwise it is resolved to a kubeconfig context with the same matcher, and it and
	// namespace override the saved target for this connection only: it is not saved, does not
	// change which deployment is active, and gets no env token (EnvMCPTokenFor).
	saved := prof
	if kc := strings.TrimSpace(args.KubeContext); kc != "" && !strings.Contains(strings.ToLower(prof.KubeContext), strings.ToLower(kc)) {
		resolved, err := s.resolveKubeContextOverride(kc)
		if err != nil {
			return "", err
		}
		prof.KubeContext = resolved
	}
	if strings.TrimSpace(args.Namespace) != "" {
		prof.Namespace = strings.TrimSpace(args.Namespace)
	}

	conv := s.ConversationFor(ctx, "", rawArgs)
	meta := CallMetaFromContext(ctx)
	if s.host != nil {
		s.host.OnConnectDeployment(ctx, s, &prof, conv, rawArgs, meta)
	}
	EnrichDeploymentVersionStatus(ctx, &prof)

	sess, err := s.connectSession(ctx, prof, sameConnectionTarget(prof, saved))
	if err != nil {
		return "", err
	}

	if text, _, _, rErr := sess.callRemoteTool(ctx, ToolConnectionStatus, nil, meta, s.store.Snapshot(), nil); rErr == nil && text != "" {
		return text, nil
	}
	return fmt.Sprintf("## 🔌 Connected to Kube-Agents Deployment `%s`\n\n- **Namespace / PlatformAgent**: `%s/%s`\n- **MCP Endpoint**: `%s`\n- **Active Conversation**: **`%s`** (token `console:%s`)\n",
		prof.Name, prof.Namespace, prof.PlatformAgentName, sess.baseURL, conv.Alias, conv.Token), nil
}

// resolveKubeContextOverride is the kubeconfig context a connect_deployment kube_context
// override names: an exact context name, else the one context MatchKubeContexts selects with
// it, as discovery's filter would. Matching several or none is refused. A ClusterAccess that
// cannot list contexts gets kc as given.
func (s *Server) resolveKubeContextOverride(kc string) (string, error) {
	lister, ok := s.cluster.(KubeContextLister)
	if !ok {
		return kc, nil
	}
	names, err := lister.KubeContexts()
	if err != nil {
		return "", fmt.Errorf("resolving kube_context %q: %w", kc, err)
	}
	for _, name := range names {
		if strings.TrimSpace(name) == kc {
			return kc, nil
		}
	}
	matches := MatchKubeContexts(names, kc)
	switch len(matches) {
	case 1:
		return matches[0], nil
	case 0:
		return "", fmt.Errorf("kube_context %q matches no kubeconfig context", kc)
	default:
		return "", fmt.Errorf("kube_context %q matches more than one kubeconfig context: %s; name one of them", kc, strings.Join(matches, ", "))
	}
}

func (s *Server) handleConnectionStatus(ctx context.Context) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	text, _, _, cErr := sess.callRemoteTool(ctx, ToolConnectionStatus, nil, CallMetaFromContext(ctx), s.store.Snapshot(), nil)
	if cErr != nil {
		prof := sess.Profile()
		conv := s.ReadOnlyConversationFor(ctx, "")
		return fmt.Sprintf("## 🔌 Kube-Agents MCP Status\n\n- **Active Deployment**: **`%s`** (`%s/%s`)\n- **MCP Endpoint**: `%s`\n- **Active Conversation**: **`%s`** (`console:%s`)\n- **Remote Status Warning**: %v\n",
			prof.Name, prof.Namespace, prof.PlatformAgentName, sess.baseURL, conv.Alias, conv.Token, cErr), nil
	}
	return text, nil
}

func (s *Server) handleSendMessage(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args map[string]any
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	if args == nil {
		args = make(map[string]any)
	}
	mode, _ := args["mode"].(string)
	if mode = strings.ToLower(strings.TrimSpace(mode)); sendModeAwaitsServer(mode) {
		return "", "", "", errSendModeNotSupported(mode)
	}
	convArg, _ := args["conversation"].(string)
	conv := s.ConversationFor(ctx, convArg, rawArgs)
	args["conversation"] = conv.Alias
	msgText, _ := args["message"].(string)
	trimmedMsg := strings.TrimSpace(msgText)
	fwdArgs, err := serverSendMessageArgs(args)
	if err != nil {
		return "", "", "", err
	}
	updatedArgs, _ := json.Marshal(fwdArgs)

	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	meta := CallMetaFromContext(ctx)
	callerID := FirstNonEmptyStr(strings.TrimSpace(conv.HostConversationID), strings.TrimSpace(meta.ConversationID))
	callCtx, cancelCall := context.WithCancel(ctx)
	defer cancelCall()

	streamKey := "send:" + FirstNonEmptyStr(callerID, conv.Alias, DefaultConvAlias)
	s.registerStreamingCallStart(streamKey, "", conv.Alias, callerID, meta.ArtifactsDir, cancelCall)
	txt, title, md, callErr := sess.callRemoteTool(callCtx, ToolSendMessage, updatedArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
	s.finishStreamingCall(streamKey, conv.Alias, callerID, meta.ArtifactsDir, txt, callCtx.Err(), callErr)
	// A /session or /session off text is a console control turn; the local session flag follows
	// it only once the server took the turn.
	// The text is read with the gateway's own matchers (chattext), so a spelling the gateway
	// takes as off or stop never turns the flag on.
	if callErr == nil {
		if rest, ok := chattext.IsSessionCommand(trimmedMsg); ok {
			switch {
			case chattext.IsSessionOff(rest):
				_, _ = s.store.SetConversationSessionMode(conv.Alias, false)
			case chattext.IsStop(rest):
				// A stop of the running turn; the session state does not change.
			default:
				_, _ = s.store.SetConversationSessionMode(conv.Alias, true)
			}
		}
	}
	return txt, title, md, callErr
}

func (s *Server) handleManageSession(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Action      string `json:"action"`
		Alias       string `json:"alias"`
		Description string `json:"description"`
		Debug       *bool  `json:"debug"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	action := strings.ToLower(strings.TrimSpace(args.Action))
	if action == "" {
		action = actionList
	}
	meta := CallMetaFromContext(ctx)

	if s.host != nil {
		if handled, txt, title, md, err := s.host.HandleManageSessionAction(ctx, s, action, args.Alias, args.Description, rawArgs, meta); handled {
			return txt, title, md, err
		}
	}
	if sessionActionAwaitsServer(action) {
		return "", "", "", errSessionActionNotSupported(action)
	}

	switch action {
	case actionNew:
		// With no alias the store would re-mint the active conversation in place, dropping
		// its session state while the server kept the conversation; a new one needs a name.
		// An alias already in use is refused for the same reason (CreateConversation).
		if strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(args.Alias), ConsoleConvPrefix)) == "" {
			return "", "", "", errors.New("manage_session new needs an alias for the new conversation")
		}
		entry, err := s.store.CreateConversation(args.Alias, args.Description)
		if err != nil {
			return "", "", "", err
		}
		return fmt.Sprintf("Created conversation **`%s`** (token `console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSwitch:
		alias := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(args.Alias), ConsoleConvPrefix))
		if alias == "" {
			return "", "", "", errors.New("manage_session switch needs the alias of the conversation to make active")
		}
		if IsDedicatedSubagentConversation(ConversationEntry{Alias: alias}) {
			// Refused before ConversationFor, which would create a conversation of that name.
			return "", "", "", fmt.Errorf("cannot switch to %q: it %w", alias, errSubagentConversation)
		}
		entry := s.ConversationFor(ctx, args.Alias, rawArgs)
		if entry.Alias != alias && entry.Token != alias {
			return "", "", "", fmt.Errorf("could not switch to conversation %q: the active conversation is still %q", alias, entry.Alias)
		}
		if IsDedicatedSubagentConversation(entry) {
			return "", "", "", fmt.Errorf("cannot switch to %q: it %w", entry.Alias, errSubagentConversation)
		}
		if active := s.store.Snapshot().ActiveConversation; active != entry.Alias {
			return "", "", "", fmt.Errorf("could not switch to conversation %q: the active conversation is still %q", entry.Alias, active)
		}
		return fmt.Sprintf("Switched active conversation to **`%s`** (token `console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSessionOn, actionSessionStartAlias:
		entry := s.ConversationFor(ctx, args.Alias, rawArgs)
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", "", "", err
		}
		fwdArgs, _ := json.Marshal(map[string]any{"action": actionSessionOn, "alias": entry.Alias})
		txt, title, md, rErr := sess.callRemoteTool(ctx, ToolManageSession, fwdArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
		if rErr != nil {
			return txt, title, md, rErr
		}
		_, _ = s.store.SetConversationSessionMode(entry.Alias, true)
		if txt != "" {
			return txt, title, md, nil
		}
		return fmt.Sprintf("Enabled persistent `/session` worker mode on **`%s`** (`console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionSessionOff, actionSessionEndAlias:
		entry := s.ConversationFor(ctx, args.Alias, rawArgs)
		sess, err := s.ensureConnected(ctx)
		if err != nil {
			return "", "", "", err
		}
		fwdArgs, _ := json.Marshal(map[string]any{"action": actionSessionOff, "alias": entry.Alias})
		txt, title, md, rErr := sess.callRemoteTool(ctx, ToolManageSession, fwdArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
		if rErr != nil {
			return txt, title, md, rErr
		}
		_, _ = s.store.SetConversationSessionMode(entry.Alias, false)
		if txt != "" {
			return txt, title, md, nil
		}
		return fmt.Sprintf("Terminated `/session` worker mode on **`%s`** (`console:%s`).\n", entry.Alias, entry.Token), "", "", nil

	case actionDelete, actionRemoveAlias:
		// Resolve the local entry first, with DeleteConversation's own matching, so the remote
		// delete names the same conversation the local one removes, and an ambiguous name
		// deletes nothing anywhere.
		// A name that resolves to nothing, or to several, is refused as an error.
		target := strings.TrimSpace(args.Alias)
		if target == "" {
			return "", "", "", errors.New("manage_session delete needs the conversation's alias or console token")
		}
		existing, found, ambiguous := s.store.ResolveConversation(target)
		if ambiguous {
			return "", "", "", fmt.Errorf("more than one conversation matches %q by name or role; name it by alias or console token", target)
		}
		if !found {
			// Not in the local table: the server may still hold the name, and its answer, or its
			// refusal, is the result.
			sess, err := s.ensureConnected(ctx)
			if err != nil {
				return "", "", "", fmt.Errorf("conversation %q is not in this bridge's list, and the in-cluster server could not be asked: %w", target, err)
			}
			fwd, _ := json.Marshal(map[string]any{"action": actionDelete, "alias": target})
			txt, title, md, rErr := sess.callRemoteTool(ctx, ToolManageSession, fwd, meta, s.store.Snapshot(), nil)
			if rErr != nil {
				return txt, title, md, rErr
			}
			return FirstNonEmptyStr(txt, fmt.Sprintf("Deleted conversation **`%s`** on the in-cluster server.\n", target)), title, md, nil
		}
		// The local entry goes first and the server's delete is best effort: it only forgets the
		// name there (a2a/mcp sessions.go), and after a server restart it no longer holds it, so a
		// failure must not keep a conversation the user asked to delete. For a session
		// conversation the failure is reported as a warning; for a plain one, which the server
		// may never have seen, it is not.
		isSession := existing.SessionMode || strings.TrimSpace(existing.SessionWorkerID) != ""
		deleted, ok := s.store.DeleteConversation(existing.Alias)
		if !ok {
			return "", "", "", fmt.Errorf("conversation %q was not found (another bridge process may have deleted it)", existing.Alias)
		}
		var remoteErr error
		fwd, _ := json.Marshal(map[string]any{"action": actionDelete, "alias": existing.Alias})
		if sess, err := s.ensureConnected(ctx); err != nil {
			remoteErr = err
		} else if _, _, _, rErr := sess.callRemoteTool(ctx, ToolManageSession, fwd, meta, s.store.Snapshot(), nil); rErr != nil {
			remoteErr = rErr
		}
		if remoteErr != nil && isSession {
			return fmt.Sprintf("Deleted session **`%s`** from this bridge.\n\n⚠️ The in-cluster server's delete failed (%v); it may still hold the name. A `/session` worker for it idles out by itself.\n", FirstNonEmptyStr(deleted.Alias, target), remoteErr), "", "", nil
		}
		return fmt.Sprintf("Deleted session **`%s`**.\n", FirstNonEmptyStr(deleted.Alias, target)), "", "", nil

	case actionDebugOn, actionDebugOff:
		enable := action == actionDebugOn
		entry := s.ReadOnlyConversationFor(ctx, args.Alias)
		if entry.Alias != "" {
			_ = s.store.SetConversationDebugMode(entry.Alias, enable)
		} else {
			_ = s.store.SetDebugMode(enable)
		}
		return fmt.Sprintf("Debug output set to `%v` for **`%s`**.\n", enable, FirstNonEmptyStr(entry.Alias, "global")), "", "", nil
	}

	// list, help and anything else go to the server, whose answer (or refusal) is the result.
	// Only list has a local stand-in, used when the server cannot answer, and it says so.
	sess, err := s.ensureConnected(ctx)
	if err == nil {
		var txt, title, md string
		txt, title, md, err = sess.callRemoteTool(ctx, ToolManageSession, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
		if err == nil {
			return txt, title, md, nil
		}
		if action != actionList {
			return txt, title, md, err
		}
	} else if action != actionList {
		return "", "", "", err
	}
	snap := s.store.Snapshot()
	aliases := make([]string, 0, len(snap.Conversations))
	for alias := range snap.Conversations {
		aliases = append(aliases, alias)
	}
	sort.Strings(aliases)
	var sb strings.Builder
	sb.WriteString("## 💬 Kube-Agents Console Conversations (this bridge's local list)\n\n")
	fmt.Fprintf(&sb, "⚠️ The in-cluster server's conversation list is not available (%v); these are the names this bridge has saved, not the server's.\n\n", err)
	for _, alias := range aliases {
		c := snap.Conversations[alias]
		fmt.Fprintf(&sb, "- **`%s`** (`console:%s`, sessionMode=`%v`)\n", alias, c.Token, c.SessionMode)
	}
	return sb.String(), "", "", nil
}

func (s *Server) handleTaskControl(ctx context.Context, rawArgs json.RawMessage) (string, string, string, error) {
	var args struct {
		Action       string `json:"action"`
		TaskID       string `json:"task_id"`
		Conversation string `json:"conversation"`
	}
	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &args)
	}
	act := strings.ToLower(strings.TrimSpace(args.Action))
	if taskActionAwaitsServer(act) {
		return "", "", "", errTaskActionNotSupported(act)
	}
	// An unreadable wait is refused here, before the connect, naming the bridge's argument.
	if _, err := serverTaskControlArgs(rawArgs, ""); err != nil {
		return "", "", "", err
	}
	taskID := strings.TrimSpace(args.TaskID)
	isStop := act == taskActionStop || act == taskActionCancel
	stopAlias := ""
	if isStop {
		// A stop of a task the bridge is not tracking is refused before the connect, as
		// CallRemoteTool's is (upstreamArgs).
		var err error
		if stopAlias, err = s.stopConversationAlias(ctx, args.Conversation, taskID); err != nil {
			return "", "", "", err
		}
	}
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", "", "", err
	}
	meta := CallMetaFromContext(ctx)
	conv := s.ReadOnlyConversationFor(ctx, args.Conversation)
	callerID := FirstNonEmptyStr(strings.TrimSpace(conv.HostConversationID), strings.TrimSpace(meta.ConversationID))

	if isStop {
		s.clearStreamingTask(taskID, stopAlias)
		fwd, err := serverTaskControlArgs(rawArgs, stopAlias)
		if err != nil {
			return "", "", "", err
		}
		return sess.callRemoteTool(ctx, ToolTaskControl, fwd, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
	}
	if rawArgs, err = serverTaskControlArgs(rawArgs, ""); err != nil {
		return "", "", "", err
	}

	if (act == taskActionGet || act == taskActionSteer) && taskID != "" {
		callCtx, cancelCall := context.WithCancel(ctx)
		defer cancelCall()
		streamKey := "task:" + taskID
		// The server stops a conversation's running task, not a task by id, so a cancelled read
		// stops the conversation that owns the task, never the caller's active one; with the
		// owner unknown, nothing is stopped.
		owner := s.taskOwnerConversation(taskID)
		s.registerStreamingCallStart(streamKey, taskID, owner, callerID, meta.ArtifactsDir, cancelCall)
		txt, title, md, callErr := sess.callRemoteTool(callCtx, ToolTaskControl, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
		s.finishStreamingCall(streamKey, owner, callerID, meta.ArtifactsDir, txt, callCtx.Err(), callErr)
		return txt, title, md, callErr
	}

	return sess.callRemoteTool(ctx, ToolTaskControl, rawArgs, meta, s.store.Snapshot(), progressCallbackFromContext(ctx))
}

func extractStreamingTaskID(text string) string {
	idx := strings.Index(text, "task-")
	if idx < 0 {
		return ""
	}
	rest := text[idx:]
	end := 0
	for end < len(rest) {
		c := rest[end]
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '-' || c == '_' {
			end++
		} else {
			break
		}
	}
	if end <= len("task-") {
		return ""
	}
	return rest[:end]
}

func (s *Server) registerStreamingCallStart(key, taskID, convAlias, hostConvID, artifactsDir string, cancelCall context.CancelFunc) {
	if s == nil || key == "" {
		return
	}
	now := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.activeStreamingTasks == nil {
		s.activeStreamingTasks = make(map[string]*activeStreamingTask)
	}
	for k, existing := range s.activeStreamingTasks {
		if existing != nil && existing.ConvAlias == convAlias && taskID != "" && existing.TaskID == taskID && k != key {
			delete(s.activeStreamingTasks, k)
		}
	}
	entry := s.activeStreamingTasks[key]
	if entry == nil {
		entry = &activeStreamingTask{
			StartedAt: now,
		}
		s.activeStreamingTasks[key] = entry
	}
	if taskID != "" {
		entry.TaskID = taskID
	}
	if convAlias != "" {
		entry.ConvAlias = convAlias
	}
	if hostConvID != "" {
		entry.HostConvID = hostConvID
	}
	if artifactsDir != "" {
		entry.ArtifactsDir = artifactsDir
	}
	entry.InFlight = true
	entry.CancelCall = cancelCall
	entry.UpdatedAt = now
}

func (s *Server) finishStreamingCall(key, convAlias, hostConvID, artifactsDir, responseText string, ctxErr, callErr error) {
	if s == nil {
		return
	}
	extractedTaskID := extractStreamingTaskID(responseText)
	isContinue := strings.HasPrefix(responseText, StreamingStepContinuePrefix) || strings.Contains(responseText, "STREAMING_STEP_CONTINUE")
	// The in-cluster server reports a turn still running as its task line (a2a/mcp writeTask).
	if ref := runningTaskRef(responseText); ref != "" {
		extractedTaskID, isContinue = ref, true
	}

	s.mu.Lock()
	if s.activeStreamingTasks == nil {
		s.activeStreamingTasks = make(map[string]*activeStreamingTask)
	}
	entry := s.activeStreamingTasks[key]
	knownTaskID := extractedTaskID
	if entry != nil && knownTaskID == "" {
		knownTaskID = entry.TaskID
	}
	delete(s.activeStreamingTasks, key)
	// The same task may be tracked under its other spelling ("task:<addressee>/<id>" from a
	// send, "task:<id>" from a get): this answer supersedes those entries too. Only the call's
	// own task and the server's running-task line count, not any "task-" the text mentions.
	// A call that failed or was cancelled brought no answer and supersedes nothing: the owner
	// recorded from the send is kept for the next stop or cancelled read.
	if ctxErr == nil && callErr == nil {
		refs := []string{runningTaskRef(responseText)}
		if entry != nil {
			refs = append(refs, entry.TaskID)
		}
		for k, st := range s.activeStreamingTasks {
			if st == nil || st.InFlight {
				continue
			}
			for _, ref := range refs {
				if sameTaskRef(st.TaskID, ref) {
					delete(s.activeStreamingTasks, k)
					break
				}
			}
		}
	}

	if ctxErr != nil {
		s.mu.Unlock()
		if entry != nil && entry.BridgeCancelled {
			return
		}
		s.bgWG.Add(1)
		go func() {
			defer s.bgWG.Done()
			s.stopRemoteTaskOnCancel(convAlias, knownTaskID)
		}()
		return
	}

	if isContinue && callErr == nil && knownTaskID != "" {
		now := time.Now()
		s.activeStreamingTasks["task:"+knownTaskID] = &activeStreamingTask{
			TaskID:       knownTaskID,
			ConvAlias:    convAlias,
			HostConvID:   hostConvID,
			ArtifactsDir: artifactsDir,
			InFlight:     false,
			StartedAt:    now,
			UpdatedAt:    now,
		}
	}
	s.mu.Unlock()
}

// taskOwnerConversation is the conversation alias a tracked streaming task was started on (a
// send_message that returned taskID still running), or "" when the bridge does not know it.
// errStopNeedsConversation refuses a stop by task id whose owning conversation the bridge does
// not know: the server stops a conversation's running task, so stopping the caller's active
// one instead could stop a different turn.
var errStopNeedsConversation = errors.New("stop needs the task's conversation: this bridge is not tracking that task; pass conversation")

// stopConversationAlias is the conversation a task_control stop goes to: the one named; with
// only a task_id, the conversation that owns that task (refused when unknown); with neither,
// the caller's bound or active conversation.
func (s *Server) stopConversationAlias(ctx context.Context, conversation, taskID string) (string, error) {
	if strings.TrimSpace(conversation) == "" && taskID != "" {
		owner := s.taskOwnerConversation(taskID)
		if owner == "" {
			return "", errStopNeedsConversation
		}
		return owner, nil
	}
	return s.ReadOnlyConversationFor(ctx, conversation).Alias, nil
}

func (s *Server) taskOwnerConversation(taskID string) string {
	if s == nil || taskID == "" {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, st := range s.activeStreamingTasks {
		if st != nil && sameTaskRef(st.TaskID, taskID) && st.ConvAlias != "" {
			return st.ConvAlias
		}
	}
	return ""
}

// runningTaskRefRegex matches the in-cluster server's line for a task still running
// (a2a/mcp writeTask): "Task `<addressee>/<id>`: **<state>** (still running; ...".
var runningTaskRefRegex = regexp.MustCompile("(?m)^Task `([^`\\s]+)`: \\*\\*[^*\\n]+\\*\\* \\(still running")

// runningTaskRef is the task reference of the first task the server reports still running in
// text, or "".
func runningTaskRef(text string) string {
	if m := runningTaskRefRegex.FindStringSubmatch(text); len(m) == 2 {
		return m[1]
	}
	return ""
}

// sameTaskRef reports whether two task references name the same task: equal, or one is the
// other's task id without its "<addressee>/" (task_control takes either form).
func sameTaskRef(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	if a == b {
		return true
	}
	_, aID, aRef := strings.Cut(a, "/")
	_, bID, bRef := strings.Cut(b, "/")
	return (aRef && !bRef && aID == b) || (bRef && !aRef && bID == a)
}

func (s *Server) clearStreamingTask(taskID, convAlias string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, entry := range s.activeStreamingTasks {
		if (taskID != "" && (k == taskID || k == "task:"+taskID || (entry != nil && sameTaskRef(entry.TaskID, taskID)))) ||
			(taskID == "" && convAlias != "" && entry != nil && entry.ConvAlias == convAlias) {
			if entry != nil && entry.InFlight && entry.CancelCall != nil {
				// The caller sends the stop; the cancelled call removes its own entry.
				entry.BridgeCancelled = true
				entry.CancelCall()
				continue
			}
			delete(s.activeStreamingTasks, k)
		}
	}
}

// stopRemoteTaskOnCancel sends stop for convAlias's running task. With convAlias unknown it
// sends nothing: the server's stop is per conversation and would fall to its default one.
func (s *Server) stopRemoteTaskOnCancel(convAlias, taskID string) {
	if s == nil || strings.TrimSpace(convAlias) == "" {
		return
	}
	stopCtx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	sess, err := s.ensureConnected(stopCtx)
	if err != nil || sess == nil {
		return
	}
	stopArgs := map[string]any{
		"action": "stop",
	}
	if strings.TrimSpace(taskID) != "" {
		stopArgs["task_id"] = strings.TrimSpace(taskID)
	}
	if strings.TrimSpace(convAlias) != "" {
		stopArgs["conversation"] = strings.TrimSpace(convAlias)
	}
	raw, _ := json.Marshal(stopArgs)
	raw, _ = serverTaskControlArgs(raw, strings.TrimSpace(convAlias))
	_, _, _, _ = sess.callRemoteTool(stopCtx, ToolTaskControl, raw, CallMeta{}, s.store.Snapshot(), nil)
	if s.host != nil {
		s.host.OnKeepAliveTick(stopCtx, s)
	}
}

// sweepStaleStreamingTasks drops tracked tasks no call has touched for staleStreamingTaskAge,
// with or without a HostIntegration, so a long-lived bridge neither grows the table nor keeps
// answering taskOwnerConversation for tasks long finished. An in-flight call's entry is left
// to the call, which removes it when it returns.
func (s *Server) sweepStaleStreamingTasks(now time.Time) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, st := range s.activeStreamingTasks {
		if st == nil || (!st.InFlight && now.Sub(st.UpdatedAt) > staleStreamingTaskAge) {
			delete(s.activeStreamingTasks, k)
		}
	}
}

func (s *Server) checkActiveStreamingTasksForCancel(ctx context.Context) {
	if s == nil || ctx.Err() != nil {
		return
	}
	s.sweepStaleStreamingTasks(time.Now())
	if s.host == nil {
		return
	}
	type candidate struct {
		key   string
		entry activeStreamingTask
	}
	var candidates []candidate

	s.mu.Lock()
	for k, st := range s.activeStreamingTasks {
		if st == nil {
			delete(s.activeStreamingTasks, k)
			continue
		}
		if st.HostConvID == "" || st.BridgeCancelled {
			continue
		}
		candidates = append(candidates, candidate{key: k, entry: *st})
	}
	s.mu.Unlock()

	for _, cand := range candidates {
		checkCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		canceled := s.host.IsCascadeCanceled(checkCtx, s, cand.entry.HostConvID, cand.entry.ArtifactsDir)
		cancel()
		if !canceled {
			continue
		}
		s.mu.Lock()
		cur := s.activeStreamingTasks[cand.key]
		if cur == nil || cur.BridgeCancelled {
			s.mu.Unlock()
			continue
		}
		cancelFn := cur.CancelCall
		taskID := cur.TaskID
		convAlias := cur.ConvAlias
		if cur.InFlight && cancelFn != nil {
			// The stop below is the only one: the cancelled call removes its own entry.
			cur.BridgeCancelled = true
		} else {
			delete(s.activeStreamingTasks, cand.key)
		}
		s.mu.Unlock()

		if cancelFn != nil {
			cancelFn()
		}
		s.stopRemoteTaskOnCancel(convAlias, taskID)
	}
}

func (s *Server) handleReadTopics(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	sess, err := s.ensureConnected(ctx)
	if err != nil {
		return "", err
	}
	txt, _, _, err := sess.callRemoteTool(ctx, ToolReadTopics, rawArgs, CallMetaFromContext(ctx), s.store.Snapshot(), nil)
	return txt, err
}

func (s *Server) handleInspectPR(ctx context.Context, rawArgs json.RawMessage) (string, error) {
	var args struct {
		PR        string `json:"pr"`
		Repo      string `json:"repo"`
		OutputDir string `json:"output_dir"`
	}
	if err := json.Unmarshal(rawArgs, &args); err != nil {
		return "", fmt.Errorf("invalid inspect_pr arguments: %w", err)
	}
	defaultRepo := strings.TrimSpace(args.Repo)
	if defaultRepo == "" && s.store != nil {
		if active, ok := s.store.ActiveDeployment(); ok {
			defaultRepo = active.GitHubRepo
		}
	}
	outDir := strings.TrimSpace(args.OutputDir)
	if outDir == "" {
		if artDir := strings.TrimSpace(CallMetaFromContext(ctx).ArtifactsDir); artDir != "" {
			if repo, num, pErr := ParsePRReference(args.PR, defaultRepo); pErr == nil {
				safeRepo := strings.ReplaceAll(repo, "/", "-")
				outDir = filepath.Join(artDir, "scratch", "prs", fmt.Sprintf("%s-pr-%d", safeRepo, num))
			}
		}
	}
	insp, err := InspectPullRequest(ctx, s.runner, s.apiBaseURL, args.PR, defaultRepo, outDir)
	if err != nil {
		return "", err
	}
	return FormatPRInspectionMarkdown(insp), nil
}

func (s *Server) ensureConnected(ctx context.Context) (*gatewayBridgeSession, error) {
	s.mu.Lock()
	cur := s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
		return cur, nil
	}

	s.connectMu.Lock()
	defer s.connectMu.Unlock()

	s.mu.Lock()
	cur = s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
		return cur, nil
	}

	if prof, ok := s.overrideTarget(); ok {
		return s.connectSessionLocked(ctx, prof, false)
	}
	prof, ok := s.store.ActiveDeployment()
	if !ok {
		discovered, err := s.cluster.DiscoverDeployments(ctx, "")
		deployments := Deployments(discovered)
		if err != nil && len(deployments) == 0 {
			return nil, fmt.Errorf("%w: %v", errNotConnected, err)
		}
		if len(deployments) == 0 {
			if len(discovered) > 0 {
				return nil, fmt.Errorf("%w: %v", errNotConnected, NewPartialDiscoveryError(ProbeFailures(discovered)))
			}
			return nil, errNoDeploymentsSaved
		}
		var uerr error
		if prof, uerr = s.store.UpsertDiscoveredForAutoConnect(discovered); uerr != nil {
			return nil, uerr
		}
	}
	return s.connectSessionLocked(ctx, prof, true)
}

// overrideTarget is the in-memory target of the last overridden connect_deployment, if any.
func (s *Server) overrideTarget() (DeploymentProfile, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.override == nil {
		return DeploymentProfile{}, false
	}
	return *s.override, true
}

func (s *Server) ensureConnectedAsync() {
	s.mu.Lock()
	cur := s.session
	s.mu.Unlock()
	if cur != nil && cur.IsHealthy() {
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
		cur := s.session
		s.mu.Unlock()
		if cur != nil && cur.IsHealthy() {
			return
		}
		prof, overridden := s.overrideTarget()
		if !overridden {
			var ok bool
			if prof, ok = s.store.ActiveDeployment(); !ok {
				return
			}
		}
		if _, err := s.connectSessionLocked(ctx, prof, !overridden); err == nil && s.host != nil {
			s.host.OnKeepAliveTick(ctx, s)
		}
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
	cancelWatchTicker := time.NewTicker(1 * time.Second)
	defer cancelWatchTicker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-cancelWatchTicker.C:
			s.checkActiveStreamingTasksForCancel(ctx)
		case <-ticker.C:
			s.keepAliveTick(ctx)
		}
	}
}

func (s *Server) keepAliveTick(ctx context.Context) {
	if s == nil || ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	sess := s.session
	s.mu.Unlock()

	skipAutoConnect := false
	if s.host != nil {
		skipAutoConnect = s.host.OnKeepAliveTick(ctx, s)
	}
	if !skipAutoConnect && (sess == nil || !sess.IsHealthy()) {
		s.ensureConnectedAsync()
	}
}

func (s *Server) connectSession(ctx context.Context, prof DeploymentProfile, persist bool) (*gatewayBridgeSession, error) {
	s.connectMu.Lock()
	defer s.connectMu.Unlock()
	return s.connectSessionLocked(ctx, prof, persist)
}

// connectSessionLocked connects to prof and makes it the session. With persist, prof is a saved
// deployment's target: the store marks it connected and active, and any override is cleared.
// Without it, prof is an overridden target: nothing is written to the store, and prof is kept
// as the override that reconnects use.
func (s *Server) connectSessionLocked(ctx context.Context, prof DeploymentProfile, persist bool) (*gatewayBridgeSession, error) {
	var saved map[string]DeploymentProfile
	if s.store != nil {
		saved = s.store.Snapshot().Deployments
	}
	newSess, err := connectGatewayBridge(ctx, s.cluster, prof, saved)
	if err != nil {
		return nil, err
	}
	if persist && s.store != nil {
		if connectedProf, mErr := s.store.MarkDeploymentConnected(newSess.Profile()); mErr == nil {
			newSess.UpdateProfile(connectedProf)
		}
	}
	s.mu.Lock()
	oldSess := s.session
	s.session = newSess
	if persist {
		s.override = nil
	} else {
		override := prof
		s.override = &override
	}
	s.mu.Unlock()
	if oldSess != nil {
		oldSess.Close()
	}
	return newSess, nil
}

// ReadOnlyConversationFor resolves convArg or the caller's bound conversation without mutating
// ActiveConversation or minting a new entry when looking up by alias.
func (s *Server) ReadOnlyConversationFor(ctx context.Context, convArg string) ConversationEntry {
	if s == nil || s.store == nil {
		return ConversationEntry{}
	}
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		if c, ok := s.store.LookupConversation(trimmed); ok {
			return c
		}
		return ConversationEntry{
			Alias: trimmed,
			Token: strings.TrimPrefix(trimmed, ConsoleConvPrefix),
		}
	}
	meta := CallMetaFromContext(ctx)
	if cid := strings.TrimSpace(meta.ConversationID); cid != "" {
		if c, ok := s.store.LookupConversation(cid); ok {
			return c
		}
	}
	return s.store.ActiveConversation()
}

// ConversationFor resolves or creates the console conversation for convArg / rawArgs.
func (s *Server) ConversationFor(ctx context.Context, convArg string, rawArgs json.RawMessage) ConversationEntry {
	if s == nil || s.store == nil {
		return ConversationEntry{}
	}
	if s.host != nil {
		if c, ok := s.host.ResolveConversation(ctx, s, convArg, rawArgs); ok {
			return c
		}
	}
	if trimmed := strings.TrimSpace(convArg); trimmed != "" {
		if c, err := s.store.EnsureConversation(trimmed, false, ""); err == nil {
			return c
		}
	}
	return s.store.ActiveConversation()
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
