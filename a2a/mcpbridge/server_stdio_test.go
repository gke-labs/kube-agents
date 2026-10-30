package mcpbridge

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const stdioTestWait = 5 * time.Second

type blockingGateway struct {
	srv         *httptest.Server
	blockTool   string
	release     chan struct{}
	started     chan struct{}
	stops       chan map[string]any
	calls       chan map[string]any
	startedOnce sync.Once
	releaseOnce sync.Once
	// sendReply, when set, is the text send_message answers with.
	sendReply string
}

func (g *blockingGateway) Release() {
	g.releaseOnce.Do(func() { close(g.release) })
}

func startBlockingGateway(t *testing.T, blockTool string) *blockingGateway {
	t.Helper()
	g := &blockingGateway{
		blockTool: blockTool,
		release:   make(chan struct{}),
		started:   make(chan struct{}),
		stops:     make(chan map[string]any, 8),
		calls:     make(chan map[string]any, 16),
	}
	g.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case remoteMCPHealthPath:
			w.Header().Set("Content-Type", contentTypeJSON)
			_, _ = w.Write([]byte(`{"status":"ok"}`))
		case remoteMCPPath:
			body, _ := io.ReadAll(r.Body)
			var req rpcRequest
			_ = json.Unmarshal(body, &req)
			var params struct {
				Name      string         `json:"name"`
				Arguments map[string]any `json:"arguments"`
			}
			_ = json.Unmarshal(req.Params, &params)
			select {
			case g.calls <- map[string]any{"name": params.Name, "arguments": params.Arguments}:
			default:
			}
			w.Header().Set("Content-Type", contentTypeNDJSON)
			w.WriteHeader(http.StatusOK)
			if params.Name == g.blockTool {
				prog, _ := json.Marshal(map[string]any{
					"jsonrpc": jsonRPCVersion,
					"method":  methodProgressNotification,
					"params":  map[string]any{"progressToken": "bridge-progress", "progress": 1, "message": "working"},
				})
				_, _ = fmt.Fprintf(w, "%s\n", prog)
				if f, ok := w.(http.Flusher); ok {
					f.Flush()
				}
				g.startedOnce.Do(func() { close(g.started) })
				select {
				case <-g.release:
				case <-r.Context().Done():
					return
				}
			}
			if params.Name == ToolTaskControl {
				if act, _ := params.Arguments["action"].(string); strings.EqualFold(act, "stop") {
					g.stops <- params.Arguments
				}
			}
			if act, _ := params.Arguments["action"].(string); params.Name == ToolManageSession && act == "delete" && params.Arguments["alias"] == "ghost" {
				// The server's answer to a delete of a name it does not hold (a2a/mcp sessions.go).
				_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{
					Content: []TextContent{{Type: contentTypeText, Text: "no conversation named ghost"}}, IsError: true,
				}))
				return
			}
			if act, _ := params.Arguments["action"].(string); params.Name == ToolManageSession && act == "delte" {
				// The server's answer to an action it does not have.
				_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{
					Content: []TextContent{{Type: contentTypeText, Text: "unknown action \"delte\""}}, IsError: true,
				}))
				return
			}
			reply := fmt.Sprintf("gateway ran %s", params.Name)
			if params.Name == ToolSendMessage && g.sendReply != "" {
				reply = g.sendReply
			}
			_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{
				Content: []TextContent{{Type: contentTypeText, Text: reply}},
			}))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(g.srv.Close)
	t.Cleanup(g.Release)
	return g
}

func newStdioTestServer(t *testing.T, gw *blockingGateway) *Server {
	t.Helper()
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{{
		Name:              testClusterName,
		Namespace:         testNamespace,
		PlatformAgentName: testPAName,
		DirectMCPURL:      gw.srv.URL,
		Ready:             true,
	}}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.SetActiveDeployment(testClusterName); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	return srv
}

type stdioFrame struct {
	ID     json.RawMessage `json:"id"`
	Method string          `json:"method"`
	Result struct {
		IsError  bool          `json:"isError"`
		Content  []TextContent `json:"content"`
		Contents []struct {
			URI  string `json:"uri"`
			Text string `json:"text"`
		} `json:"contents"`
	} `json:"result"`
	Error *rpcError `json:"error"`
}

type stdioHarness struct {
	t      *testing.T
	stdin  *io.PipeWriter
	stdout *io.PipeWriter
	lines  chan string
	served chan error
	seen   map[string]stdioFrame
}

func startStdioHarness(t *testing.T, srv *Server) *stdioHarness {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	h := &stdioHarness{
		t:      t,
		stdin:  inW,
		stdout: outW,
		lines:  make(chan string, 64),
		served: make(chan error, 1),
		seen:   make(map[string]stdioFrame),
	}
	go func() {
		sc := bufio.NewScanner(outR)
		sc.Buffer(make([]byte, scannerInitialBufSize), scannerMaxBufSize)
		for sc.Scan() {
			h.lines <- sc.Text()
		}
		close(h.lines)
	}()
	go func() {
		err := srv.ServeStdio(context.Background(), inR, outW)
		_ = outW.Close()
		h.served <- err
	}()
	t.Cleanup(func() { _ = inW.Close() })
	return h
}

func (h *stdioHarness) send(id int, method string, params map[string]any) {
	h.t.Helper()
	frame := map[string]any{"jsonrpc": jsonRPCVersion, "id": id, "method": method}
	if params != nil {
		frame["params"] = params
	}
	raw, err := json.Marshal(frame)
	if err != nil {
		h.t.Fatal(err)
	}
	written := make(chan error, 1)
	go func() {
		_, err := fmt.Fprintf(h.stdin, "%s\n", raw)
		written <- err
	}()
	select {
	case err := <-written:
		if err != nil {
			h.t.Fatalf("write %s to stdin: %v", method, err)
		}
	case <-time.After(stdioTestWait):
		h.t.Fatalf("ServeStdio did not read the %s frame within %v: the stdio read loop is stalled", method, stdioTestWait)
	}
}

func (h *stdioHarness) await(id int, timeout time.Duration) stdioFrame {
	h.t.Helper()
	key := fmt.Sprint(id)
	if f, ok := h.seen[key]; ok {
		return f
	}
	deadline := time.After(timeout)
	for {
		select {
		case line, ok := <-h.lines:
			if !ok {
				h.t.Fatalf("stdout closed before a response for id %d arrived", id)
			}
			var f stdioFrame
			if err := json.Unmarshal([]byte(line), &f); err != nil {
				h.t.Fatalf("non-JSON frame on stdout: %q", line)
			}
			if len(f.ID) == 0 || string(f.ID) == "null" {
				continue
			}
			if normalizeRPCID(f.ID) == key {
				return f
			}
			h.seen[normalizeRPCID(f.ID)] = f
		case <-deadline:
			h.t.Fatalf("no response for id %d within %v", id, timeout)
		}
	}
}

func (h *stdioHarness) awaitServed(timeout time.Duration) error {
	h.t.Helper()
	select {
	case err := <-h.served:
		return err
	case <-time.After(timeout):
		h.t.Fatalf("ServeStdio did not return within %v after stdin was closed", timeout)
		return nil
	}
}

func waitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(stdioTestWait):
		t.Fatalf("timed out waiting for %s", what)
	}
}

func TestServeStdioStdinCloseCancelsInFlightToolCall(t *testing.T) {
	gw := startBlockingGateway(t, ToolSendMessage)
	srv := newStdioTestServer(t, gw)
	h := startStdioHarness(t, srv)

	h.send(7, methodToolsCall, map[string]any{
		"name":      ToolSendMessage,
		"arguments": map[string]any{"message": "hello", "conversation": "stdio-test"},
	})
	waitClosed(t, gw.started, "the gateway to receive send_message")

	h.send(8, methodPing, nil)
	if f := h.await(8, stdioTestWait); f.Error != nil {
		t.Fatalf("ping while send_message in flight: %+v", f.Error)
	}

	start := time.Now()
	if err := h.stdin.Close(); err != nil {
		t.Fatal(err)
	}
	if err := h.awaitServed(stdioTestWait); err != nil {
		t.Fatalf("ServeStdio returned error on clean stdin EOF: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("ServeStdio took %v to return after stdin EOF", elapsed)
	}

	resp := h.await(7, stdioTestWait)
	if !resp.Result.IsError || len(resp.Result.Content) == 0 {
		t.Fatalf("expected an isError tools/call result for the cancelled send_message, got %+v", resp)
	}
	if txt := resp.Result.Content[0].Text; strings.Contains(txt, "empty JSON-RPC response") || !strings.Contains(txt, "context canceled") {
		t.Fatalf("expected the cancelled stream to be reported as a context error, got: %s", txt)
	}

	select {
	case stop := <-gw.stops:
		if conv, _ := stop["conversation"].(string); conv != "stdio-test" {
			t.Fatalf("remote stop targeted conversation %q, want stdio-test (args: %v)", conv, stop)
		}
	case <-time.After(stdioTestWait):
		t.Fatal("the cancelled send_message did not issue a remote task_control stop")
	}
}

func TestServeStdioResourcesReadDoesNotBlockReadLoop(t *testing.T) {
	gw := startBlockingGateway(t, ToolConnectionStatus)
	srv := newStdioTestServer(t, gw)
	h := startStdioHarness(t, srv)

	h.send(1, methodResourcesRead, map[string]any{"uri": resourceURIStatus})
	waitClosed(t, gw.started, "the gateway to receive connection_status")

	h.send(2, methodPing, nil)
	if f := h.await(2, stdioTestWait); f.Error != nil {
		t.Fatalf("ping while resources/read in flight: %+v", f.Error)
	}
	if _, ok := h.seen["1"]; ok {
		t.Fatal("resources/read answered before the gateway released it")
	}

	gw.Release()
	read := h.await(1, stdioTestWait)
	if read.Error != nil {
		t.Fatalf("resources/read failed: %+v", read.Error)
	}
	if len(read.Result.Contents) != 1 || read.Result.Contents[0].URI != resourceURIStatus ||
		!strings.Contains(read.Result.Contents[0].Text, "gateway ran "+ToolConnectionStatus) {
		t.Fatalf("unexpected resources/read result: %+v", read.Result)
	}

	// kube-agents://tasks sends task_control list, which the in-cluster server does not offer yet:
	// the read is refused without a gateway call.
	h.send(3, methodResourcesRead, map[string]any{"uri": resourceURITasks})
	tasksRead := h.await(3, stdioTestWait)
	if tasksRead.Error == nil || !strings.Contains(tasksRead.Error.Message, "not supported by the in-cluster server yet") {
		t.Fatalf("resources/read tasks: want a not-supported error, got %+v", tasksRead)
	}

	if err := h.stdin.Close(); err != nil {
		t.Fatal(err)
	}
	if err := h.awaitServed(stdioTestWait); err != nil {
		t.Fatalf("ServeStdio returned error on clean stdin EOF: %v", err)
	}
}

func TestCallRemoteToolReportsTransportErrorMidStream(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", contentTypeNDJSON)
		w.WriteHeader(http.StatusOK)
		_, _ = fmt.Fprintf(w, "%s\n", `{"jsonrpc":"2.0","method":"notifications/progress","params":{"progressToken":"bridge-progress","progress":1,"message":"half way"}}`)
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		panic(http.ErrAbortHandler)
	}))
	defer srv.Close()

	sess := &gatewayBridgeSession{baseURL: srv.URL, httpClient: &http.Client{}}
	var progress []string
	_, _, _, err := sess.callRemoteTool(context.Background(), ToolSendMessage, nil, CallMeta{}, PersistentConfig{}, func(_ int, msg string) {
		progress = append(progress, msg)
	})
	if err == nil {
		t.Fatal("expected an error when the gateway drops the connection mid-stream")
	}
	if !strings.Contains(err.Error(), "reading a2a-mcp response") || strings.Contains(err.Error(), "empty JSON-RPC response") {
		t.Fatalf("expected the transport error to be surfaced, got: %v", err)
	}
	if len(progress) != 1 || progress[0] != "half way" {
		t.Fatalf("expected the progress frame before the drop to be delivered, got %v", progress)
	}
}

func TestValidateDirectMCPURLRejectsNonLoopbackCleartext(t *testing.T) {
	if _, err := validateDirectMCPURL("http://192.0.2.10:8089"); err == nil {
		t.Fatal("expected non-loopback http:// DirectMCPURL to be rejected")
	}
	if _, err := validateDirectMCPURL("http://127.0.0.1:8089"); err != nil {
		t.Fatalf("expected loopback http://127.0.0.1:8089 to be accepted, got %v", err)
	}
	if _, err := validateDirectMCPURL("https://mcp.example.com/mcp"); err != nil {
		t.Fatalf("expected https:// DirectMCPURL to be accepted, got %v", err)
	}
}

// A directMcpUrl written as the endpoint the docs name (".../mcp") reaches POST /mcp, not
// /mcp/mcp: the gateway 404s every other path.
func TestDirectMCPURLEndingInMCPIsPostedToMCP(t *testing.T) {
	for _, suffix := range []string{"", "/", "/mcp", "/mcp/"} {
		gw, auth := envTokenGateway(t)
		srv := envTokenServer(t, gw.URL+suffix, "direct")
		t.Setenv(EnvMCPToken, envTokenValue)
		connectAuth(t, srv, auth, "direct")
		if res := srv.CallTool(context.Background(), ToolConnectionStatus, json.RawMessage(`{}`)); res.IsError {
			t.Fatalf("directMcpUrl %s%s: connection_status: %+v", gw.URL, suffix, res)
		}
	}
	if got, err := validateDirectMCPURL("https://mcp/mcp"); err != nil || got != "https://mcp" {
		t.Fatalf("validateDirectMCPURL(https://mcp/mcp) = %q, %v; want https://mcp", got, err)
	}
	if got, err := validateDirectMCPURL("https://mcp"); err != nil || got != "https://mcp" {
		t.Fatalf("validateDirectMCPURL(https://mcp) = %q, %v; want the host named mcp kept", got, err)
	}
}

type failingClusterAccess struct {
	noClusterAccess
	discoverEntries []DeploymentProfile
	discoverErr     error
	tunnelErr       error
}

func (f failingClusterAccess) DiscoverDeployments(context.Context, string) ([]DeploymentProfile, error) {
	return f.discoverEntries, f.discoverErr
}

func (f failingClusterAccess) PopulateDeploymentCredentials(context.Context, *DeploymentProfile) error {
	return nil
}

func (f failingClusterAccess) StartGatewayPortForward(context.Context, DeploymentProfile) (Tunnel, error) {
	return nil, f.tunnelErr
}

func TestConnectFailureDoesNotStampLastConnectedAt(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{{
		Name:              testClusterName,
		KubeContext:       testKubeContext,
		Namespace:         testNamespace,
		PlatformAgentName: testPAName,
		Ready:             true,
	}}); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{
		Store: store,
		Cluster: failingClusterAccess{
			tunnelErr: errors.New("spdy dial refused"),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()

	res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(`{"deployment":"`+testClusterName+`"}`))
	if !res.IsError {
		t.Fatalf("expected connect_deployment to fail when tunnel fails, got %+v", res)
	}
	dep, ok := store.ActiveDeployment()
	if !ok {
		t.Fatal("expected deployment profile to remain in store")
	}
	if !dep.LastConnectedAt.IsZero() {
		t.Fatalf("LastConnectedAt was stamped (%v) despite tunnel failure", dep.LastConnectedAt)
	}
}

func TestSignalDrainsTheRemoteStopsOfCancelledCalls(t *testing.T) {
	gw := startBlockingGateway(t, ToolSendMessage)
	srv := newStdioTestServer(t, gw)
	inR, inW := io.Pipe()
	t.Cleanup(func() { _ = inW.Close() })
	// ctx stands in for signal.NotifyContext: cancelling it is the SIGINT/SIGTERM.
	ctx, signal := context.WithCancel(context.Background())
	served := make(chan error, 1)
	go func() { served <- srv.serveStdioUntilDone(ctx, inR, io.Discard) }()
	frame, _ := json.Marshal(map[string]any{
		"jsonrpc": jsonRPCVersion, "id": 1, "method": methodToolsCall,
		"params": map[string]any{"name": ToolSendMessage, "arguments": map[string]any{"message": "look at prod", "conversation": "c1"}},
	})
	go func() { _, _ = fmt.Fprintf(inW, "%s\n", frame) }()
	select {
	case <-gw.started:
	case <-time.After(stdioTestWait):
		t.Fatal("the gateway never received the send_message")
	}
	signal()
	select {
	case err := <-served:
		if err != nil {
			t.Fatalf("a signal shutdown returned %v; want nil, so the process exits 0", err)
		}
	case <-time.After(stdioTestWait):
		t.Fatal("the bridge did not return after the signal")
	}
	// Returning is the process exit: the stop must already have gone out.
	select {
	case stop := <-gw.stops:
		if stop["conversation"] != "c1" {
			t.Fatalf("the drained stop named conversation %v; want c1", stop["conversation"])
		}
	default:
		t.Fatal("the bridge returned on a signal before the cancelled send_message's remote stop went out")
	}
}

// A signal that arrives with a frame already queued may reach ServeStdio first; that is the
// same orderly shutdown and returns nil too.
func TestSignalWithAQueuedFrameReturnsNil(t *testing.T) {
	srv := newStdioTestServer(t, startBlockingGateway(t, ""))
	ctx, signal := context.WithCancel(context.Background())
	signal()
	frame, _ := json.Marshal(map[string]any{"jsonrpc": jsonRPCVersion, "id": 1, "method": "ping"})
	if err := srv.serveStdioUntilDone(ctx, strings.NewReader(string(frame)+"\n"), io.Discard); err != nil {
		t.Fatalf("a signal shutdown with a queued frame returned %v; want nil", err)
	}
}

// A scan that reached some contexts cleanly (no install there) and failed others is not "could
// not scan any kubeconfig context": both bridge paths say what was found and what failed.
func TestPartlyFailedScanIsNotReportedAsNoContextScanned(t *testing.T) {
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: failingClusterAccess{
		discoverEntries: []DeploymentProfile{ContextProbeFailure("ctxB", errors.New("Unauthorized"))},
	}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	check := func(path, got string) {
		t.Helper()
		if strings.Contains(got, "could not scan any kubeconfig context") || !strings.Contains(got, "no kube-agents deployment found") || !strings.Contains(got, "ctxB: Unauthorized") {
			t.Fatalf("%s: %q; want no deployment found in the scanned contexts, and ctxB's failure", path, got)
		}
	}
	_, err = srv.ensureConnected(context.Background())
	check("ensureConnected", fmt.Sprint(err))
	res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(`{"deployment":"x"}`))
	check("connect_deployment", res.Content[0].Text)
}
