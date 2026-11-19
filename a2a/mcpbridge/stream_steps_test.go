package mcpbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestParseStepStreamingReadsAutoOnOffAndRefusesTheRest(t *testing.T) {
	for in, want := range map[string]StepStreaming{
		"": StepStreamingAuto, "auto": StepStreamingAuto, " ON ": StepStreamingOn, "off": StepStreamingOff, "Off": StepStreamingOff,
	} {
		got, err := ParseStepStreaming(in)
		if err != nil || got != want {
			t.Errorf("ParseStepStreaming(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"true", "false", "1", "no"} {
		if got, err := ParseStepStreaming(in); err == nil {
			t.Errorf("ParseStepStreaming(%q) = %q, want an error", in, got)
		}
	}
	if _, err := NewServer(ServerOptions{Store: newTestStore(t), StepStreaming: "sometimes"}); err == nil {
		t.Error("NewServer took an unknown step-streaming setting")
	}
}

// What reaches the install's send_message and task_control as stream_steps, for each setting,
// host _meta and per-call value. "absent" means the bridge added none, so the install decides
// from _meta (it streams when conversation_id is there).
func TestStepStreamingDecidesTheForwardedStreamSteps(t *testing.T) {
	forwarded := make(chan map[string]any, 1)
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		var params struct {
			Arguments map[string]any `json:"arguments"`
		}
		_ = json.Unmarshal(req.Params, &params)
		forwarded <- params.Arguments
		w.Header().Set("Content-Type", contentTypeJSON)
		_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: "ok"}}}))
	}))
	defer gw.Close()
	sess := &gatewayBridgeSession{baseURL: gw.URL, httpClient: &http.Client{}}

	conv := CallMeta{ConversationID: "conv-1"}
	token := CallMeta{ProgressToken: "tok-1"}
	cases := []struct {
		name string
		mode StepStreaming
		meta CallMeta
		args string
		want any
	}{
		{"auto, conversation_id: streams", StepStreamingAuto, conv, `{"text":"hi"}`, true},
		{"unset is auto", "", conv, `{"text":"hi"}`, true},
		{"auto, progress token only: install decides", StepStreamingAuto, token, `{"text":"hi"}`, "absent"},
		{"auto, nothing: install decides", StepStreamingAuto, CallMeta{}, `{"text":"hi"}`, "absent"},
		{"off, conversation_id: one plain answer", StepStreamingOff, conv, `{"text":"hi"}`, false},
		{"off, nothing: still says false", StepStreamingOff, CallMeta{}, `{"text":"hi"}`, false},
		{"on, nothing: streams", StepStreamingOn, CallMeta{}, `{"text":"hi"}`, true},
		{"on, progress token: streams", StepStreamingOn, token, `{"text":"hi"}`, true},
		{"per-call false wins over on", StepStreamingOn, conv, `{"text":"hi","stream_steps":false}`, false},
		{"per-call true wins over off", StepStreamingOff, conv, `{"text":"hi","stream_steps":true}`, true},
	}
	for _, tc := range cases {
		for _, tool := range []string{ToolSendMessage, ToolTaskControl} {
			t.Run(tc.name+"/"+tool, func(t *testing.T) {
				meta := tc.meta
				meta.StepStreaming = tc.mode
				if _, _, _, err := sess.callRemoteTool(context.Background(), tool, json.RawMessage(tc.args), meta, PersistentConfig{}, nil); err != nil {
					t.Fatal(err)
				}
				args := <-forwarded
				got, has := args["stream_steps"]
				if !has {
					got = "absent"
				}
				if got != tc.want {
					t.Fatalf("stream_steps = %v, want %v (args %v)", got, tc.want, args)
				}
			})
		}
	}

	// Other tools never get stream_steps, whatever the setting.
	meta := conv
	meta.StepStreaming = StepStreamingOff
	if _, _, _, err := sess.callRemoteTool(context.Background(), ToolPollEvents, json.RawMessage(`{}`), meta, PersistentConfig{}, nil); err != nil {
		t.Fatal(err)
	}
	if args := <-forwarded; args != nil {
		if _, has := args["stream_steps"]; has {
			t.Fatalf("poll_events got stream_steps: %v", args)
		}
	}
}

func TestStepStreamingDecidesTheResultEnvelope(t *testing.T) {
	conv := CallMeta{ConversationID: "conv-1"}
	token := CallMeta{ProgressToken: "tok-1"}
	cases := []struct {
		name string
		mode StepStreaming
		meta CallMeta
		want bool
	}{
		{"auto, conversation_id", StepStreamingAuto, conv, true},
		{"auto, progress token", StepStreamingAuto, token, true},
		{"auto, nothing", StepStreamingAuto, CallMeta{}, false},
		{"off, conversation_id", StepStreamingOff, conv, false},
		{"off, progress token", StepStreamingOff, token, false},
		{"on, nothing", StepStreamingOn, CallMeta{}, true},
	}
	for _, tc := range cases {
		meta := tc.meta
		meta.StepStreaming = tc.mode
		if got := meta.wrapsResults(); got != tc.want {
			t.Errorf("%s: wrapsResults = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// The tools/call handler copies the server's setting onto the call, whatever the host's _meta
// says: a host can't pick its own setting by sending a StepStreaming-looking key.
func TestTheHandlerCopiesTheServersSettingOntoTheCall(t *testing.T) {
	for _, mode := range []StepStreaming{StepStreamingAuto, StepStreamingOn, StepStreamingOff} {
		host := &metaRecordingHost{}
		srv, err := NewServer(ServerOptions{Store: newTestStore(t), StepStreaming: mode, Host: host})
		if err != nil {
			t.Fatal(err)
		}
		req := `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"connection_status","arguments":{},"_meta":{"conversation_id":"c","StepStreaming":"on","stepStreaming":"on"}}}`
		srv.HandleJSONRPC(context.Background(), []byte(req))
		if host.meta.StepStreaming != mode {
			t.Errorf("server %q: the call carried %q", mode, host.meta.StepStreaming)
		}
		srv.Close()
	}
}

func newTestStore(t *testing.T) *ConfigStore {
	t.Helper()
	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	return store
}

// metaRecordingHost records the CallMeta the tools/call handler hands the host.
type metaRecordingHost struct{ meta CallMeta }

func (h *metaRecordingHost) AugmentToolSchemas(defs []ToolDefinition) []ToolDefinition { return defs }
func (h *metaRecordingHost) OnBeforeToolCall(_ context.Context, _ *Server, _ string, _ json.RawMessage, meta CallMeta) {
	h.meta = meta
}
func (h *metaRecordingHost) ResolveConversation(context.Context, *Server, string, json.RawMessage) (ConversationEntry, bool) {
	return ConversationEntry{}, false
}
func (h *metaRecordingHost) OnConnectDeployment(context.Context, *Server, *DeploymentProfile, ConversationEntry, json.RawMessage, CallMeta) {
}
func (h *metaRecordingHost) HandleManageSessionAction(context.Context, *Server, string, string, string, json.RawMessage, CallMeta) (bool, string, string, string, error) {
	return false, "", "", "", nil
}
func (h *metaRecordingHost) HandlePollEvents(context.Context, *Server, json.RawMessage) (string, error) {
	return "", nil
}
func (h *metaRecordingHost) OnAfterToolCall(_ context.Context, _ *Server, _ string, _ bool, text, md string) (string, string) {
	return text, md
}
func (h *metaRecordingHost) OnKeepAliveTick(context.Context, *Server) bool { return false }
func (h *metaRecordingHost) IsCascadeCanceled(context.Context, *Server, string, string) bool {
	return false
}
func (h *metaRecordingHost) Close() {}

// initialize logs the host's clientInfo and capability names once, so a host that needs its own
// step-streaming default can be told apart from the logs.
func TestInitializeLogsTheClientAndItsCapabilities(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	defer slog.SetDefault(prev)

	srv, err := NewServer(ServerOptions{Store: newTestStore(t), StepStreaming: StepStreamingOff})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()
	srv.HandleJSONRPC(context.Background(), []byte(`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","clientInfo":{"name":"antigravity-client","version":"v1.0.0"},"capabilities":{"roots":{},"elicitation":{}}}}`))
	got := buf.String()
	for _, want := range []string{"msg=\"mcp client\"", "name=antigravity-client", "version=v1.0.0", "capabilities=\"[elicitation roots]\"", "stepStreaming=off"} {
		if !strings.Contains(got, want) {
			t.Errorf("initialize log %q lacks %s", got, want)
		}
	}
}
