package mcpproto

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestARequestRoundTrips(t *testing.T) {
	for _, frame := range []string{
		`{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"inspect_pr","arguments":{"number":3}}}`,
		`{"jsonrpc":"2.0","id":"a","method":"ping"}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
	} {
		var req Request
		if err := json.Unmarshal([]byte(frame), &req); err != nil {
			t.Fatalf("%s: %v", frame, err)
		}
		out, err := json.Marshal(req)
		if err != nil {
			t.Fatal(err)
		}
		if !jsonEqual(t, frame, string(out)) {
			t.Errorf("round trip changed the frame:\n in  %s\n out %s", frame, out)
		}
	}
}

// Byte for byte, so a reordered field or a changed tag is a failing test: the
// server's frames were these bytes before the types moved here.
func TestTheFramesAreTheseBytes(t *testing.T) {
	for _, c := range []struct {
		v    any
		want string
	}{
		{Request{JSONRPC: JSONRPCVersion, Method: NotificationInitialized}, `{"jsonrpc":"2.0","method":"notifications/initialized"}`},
		{Request{JSONRPC: JSONRPCVersion, ID: json.RawMessage(`1`), Method: MethodToolsCall, Params: json.RawMessage(`{"name":"x"}`)}, `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}`},
		{Response{JSONRPC: JSONRPCVersion, ID: json.RawMessage(`1`), Result: map[string]any{}}, `{"jsonrpc":"2.0","id":1,"result":{}}`},
		{Response{JSONRPC: JSONRPCVersion, ID: json.RawMessage(`null`), Error: &Error{Code: ErrParseError, Message: "m"}}, `{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"m"}}`},
		{ToolDefinition{Name: "n", Description: "d", InputSchema: map[string]any{}}, `{"name":"n","description":"d","inputSchema":{}}`},
		{CallToolResult{Content: []TextContent{{Type: ContentTypeText, Text: "t"}}, IsError: true}, `{"content":[{"type":"text","text":"t"}],"isError":true}`},
	} {
		got, err := json.Marshal(c.v)
		if err != nil {
			t.Fatalf("%T: %v", c.v, err)
		}
		if string(got) != c.want {
			t.Errorf("%T marshals\n %s\nwant\n %s", c.v, got, c.want)
		}
	}
}

func TestAResponseCarriesAResultOrAnErrorAndAlwaysAnID(t *testing.T) {
	ok, _ := json.Marshal(Response{JSONRPC: JSONRPCVersion, ID: json.RawMessage(`7`), Result: CallToolResult{Content: []TextContent{{Type: ContentTypeText, Text: "hi"}}}})
	if !jsonEqual(t, `{"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"hi"}]}}`, string(ok)) {
		t.Errorf("result response = %s", ok)
	}
	bad, _ := json.Marshal(Response{JSONRPC: JSONRPCVersion, ID: json.RawMessage(`null`), Error: &Error{Code: ErrMethodNotFound, Message: "unknown method"}})
	if !jsonEqual(t, `{"jsonrpc":"2.0","id":null,"error":{"code":-32601,"message":"unknown method"}}`, string(bad)) {
		t.Errorf("error response = %s", bad)
	}

	var back Response
	if err := json.Unmarshal(bad, &back); err != nil || back.Error == nil || back.Error.Code != ErrMethodNotFound || back.Result != nil {
		t.Errorf("error response did not round-trip: %+v, %v", back, err)
	}
}

func TestToolShapesKeepTheirWireNames(t *testing.T) {
	def, _ := json.Marshal(ToolDefinition{Name: "n", Description: "d", InputSchema: map[string]any{"type": "object"}})
	if !jsonEqual(t, `{"name":"n","description":"d","inputSchema":{"type":"object"}}`, string(def)) {
		t.Errorf("tool definition = %s", def)
	}
	failed, _ := json.Marshal(CallToolResult{Content: []TextContent{{Type: ContentTypeText, Text: "x"}}, IsError: true})
	if !jsonEqual(t, `{"content":[{"type":"text","text":"x"}],"isError":true}`, string(failed)) {
		t.Errorf("failed result = %s", failed)
	}
	var back CallToolResult
	if err := json.Unmarshal(failed, &back); err != nil || !back.IsError || back.Content[0].Text != "x" {
		t.Errorf("failed result did not round-trip: %+v, %v", back, err)
	}
}

// The values are the protocol's: a client and server that disagree on one
// cannot talk, so a change here is a wire change.
func TestTheWireConstantsAreTheProtocols(t *testing.T) {
	got := map[string]any{
		"jsonrpc": JSONRPCVersion, "initialize": MethodInitialize, "ping": MethodPing,
		"tools/list": MethodToolsList, "tools/call": MethodToolsCall,
		"resources/list": MethodResourcesList, "resources/read": MethodResourcesRead, "prompts/list": MethodPromptsList, "text": ContentTypeText,
		"notifications/initialized": NotificationInitialized, "notifications/cancelled": NotificationCancelled,
		"notifications/progress": NotificationProgress,
		"-32700":                 ErrParseError, "-32600": ErrInvalidRequest, "-32601": ErrMethodNotFound,
		"-32602": ErrInvalidParams, "-32603": ErrInternal,
	}
	want := map[string]any{
		"jsonrpc": "2.0", "initialize": "initialize", "ping": "ping",
		"tools/list": "tools/list", "tools/call": "tools/call",
		"resources/list": "resources/list", "resources/read": "resources/read", "prompts/list": "prompts/list", "text": "text",
		"notifications/initialized": "notifications/initialized", "notifications/cancelled": "notifications/cancelled",
		"notifications/progress": "notifications/progress",
		"-32700":                 -32700, "-32600": -32600, "-32601": -32601, "-32602": -32602, "-32603": -32603,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("constants = %v, want %v", got, want)
	}
}

func TestSupportedProtocolVersionsIsNewestFirstAndACopy(t *testing.T) {
	v := SupportedProtocolVersions()
	if len(v) == 0 || v[0] != "2025-06-18" {
		t.Fatalf("versions = %v, want 2025-06-18 first", v)
	}
	v[0] = "mutated"
	if SupportedProtocolVersions()[0] != "2025-06-18" {
		t.Error("a caller's edit to the returned slice reached the package's list")
	}
}

func jsonEqual(t *testing.T, a, b string) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal([]byte(a), &x); err != nil {
		t.Fatalf("%s: %v", a, err)
	}
	if err := json.Unmarshal([]byte(b), &y); err != nil {
		t.Fatalf("%s: %v", b, err)
	}
	return reflect.DeepEqual(x, y)
}
