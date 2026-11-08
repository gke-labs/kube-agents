package workeradapter

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fakeAdapterSocket plays the adapter's end: one JSON request per connection,
// one JSON reply, recording what it got. The recorder is mutex-guarded
// because the accept loop runs on its own goroutine while the test goroutine
// reads it back (go test -race).
func fakeAdapterSocket(t *testing.T, reply delegateReply) (string, func() []lib.DelegateRequest) {
	t.Helper()
	// A unix socket path is limited to ~104 bytes on macOS; t.TempDir()
	// nests under the test name and can blow that budget, so this uses its
	// own short directory under os.MkdirTemp instead.
	dir, err := os.MkdirTemp("", "d")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	path := filepath.Join(dir, "d.sock")
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	var mu sync.Mutex
	var got []lib.DelegateRequest
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			var req lib.DelegateRequest
			_ = json.NewDecoder(c).Decode(&req)
			mu.Lock()
			got = append(got, req)
			mu.Unlock()
			_ = json.NewEncoder(c).Encode(reply)
			c.Close()
		}
	}()
	return path, func() []lib.DelegateRequest {
		mu.Lock()
		defer mu.Unlock()
		out := make([]lib.DelegateRequest, len(got))
		copy(out, got)
		return out
	}
}

func rpc(t *testing.T, in *io.PipeWriter, out *bufio.Reader, body string) map[string]any {
	t.Helper()
	if _, err := io.WriteString(in, body+"\n"); err != nil {
		t.Fatal(err)
	}
	line, err := out.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(line), &m); err != nil {
		t.Fatalf("not json: %q", line)
	}
	return m
}

func TestDelegateMCPListsOneToolAndForwardsACall(t *testing.T) {
	sock, got := fakeAdapterSocket(t, delegateReply{OK: true, Message: "delegated; this turn ends now"})
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, sock, nil, slog.Default()) }()
	out := bufio.NewReader(outR)

	init := rpc(t, inW, out, `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}`)
	if init["result"] == nil {
		t.Fatalf("initialize: %v", init)
	}
	if _, err := io.WriteString(inW, `{"jsonrpc":"2.0","method":"notifications/initialized"}`+"\n"); err != nil {
		t.Fatal(err)
	}
	list := rpc(t, inW, out, `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`)
	tools := list["result"].(map[string]any)["tools"].([]any)
	if len(tools) != 1 || tools[0].(map[string]any)["name"] != DelegateToolName {
		t.Fatalf("tools = %v", tools)
	}
	call := rpc(t, inW, out, `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"delegate","arguments":{"addressee":"platform","text":"how is the fleet?"}}}`)
	res := call["result"].(map[string]any)
	if res["isError"] == true {
		t.Fatalf("call errored: %v", call)
	}
	content := res["content"].([]any)[0].(map[string]any)
	if !strings.Contains(content["text"].(string), "delegated") {
		t.Fatalf("content = %v", content)
	}
	if reqs := got(); len(reqs) != 1 || reqs[0].Addressee != "platform" || reqs[0].Text != "how is the fleet?" {
		t.Fatalf("adapter got %+v", reqs)
	}
}

func TestDelegateMCPReportsARefusalAsAToolError(t *testing.T) {
	sock, _ := fakeAdapterSocket(t, delegateReply{OK: false, Message: "a task is already delegated this turn"})
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, sock, nil, slog.Default()) }()
	out := bufio.NewReader(outR)
	call := rpc(t, inW, out, `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delegate","arguments":{"addressee":"platform","text":"x"}}}`)
	res := call["result"].(map[string]any)
	if res["isError"] != true || !strings.Contains(res["content"].([]any)[0].(map[string]any)["text"].(string), "already delegated") {
		t.Fatalf("want isError with the adapter's message, got %v", call)
	}
}

func TestDelegateMCPWithoutASocketIsAToolErrorNotACrash(t *testing.T) {
	dir, err := os.MkdirTemp("", "d")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		_ = ServeDelegateMCP(ctx, inR, outW, filepath.Join(dir, "absent.sock"), nil, slog.Default())
	}()
	out := bufio.NewReader(outR)
	call := rpc(t, inW, out, `{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delegate","arguments":{"addressee":"platform","text":"x"}}}`)
	if call["result"].(map[string]any)["isError"] != true {
		t.Fatalf("want isError, got %v", call)
	}
}

func TestDelegateSocketPathDefaultsAndRespectsEnv(t *testing.T) {
	t.Setenv(EnvDelegateSocket, "")
	if got := DelegateSocketPath(); got != DefaultDelegateSocket {
		t.Fatalf("default: got %q, want %q", got, DefaultDelegateSocket)
	}
	t.Setenv(EnvDelegateSocket, "/tmp/custom.sock")
	if got := DelegateSocketPath(); got != "/tmp/custom.sock" {
		t.Fatalf("env override: got %q, want /tmp/custom.sock", got)
	}
}

// The tool offers parameters, optional, as a map of names to strings, and a
// call's parameters reach the adapter on the request.
func TestDelegateMCPOffersAndForwardsParameters(t *testing.T) {
	sock, got := fakeAdapterSocket(t, delegateReply{OK: true, Message: "delegated; this turn ends now"})
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, sock, nil, slog.Default()) }()
	out := bufio.NewReader(outR)

	list := rpc(t, inW, out, `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`)
	schema := list["result"].(map[string]any)["tools"].([]any)[0].(map[string]any)["inputSchema"].(map[string]any)
	params, ok := schema["properties"].(map[string]any)["parameters"].(map[string]any)
	if !ok || params["type"] != "object" || params["additionalProperties"].(map[string]any)["type"] != "string" {
		t.Fatalf("parameters property = %v, want an object of strings", params)
	}
	for _, r := range schema["required"].([]any) {
		if r == "parameters" {
			t.Fatal("parameters is required; platform takes none")
		}
	}

	call := rpc(t, inW, out, `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"delegate","arguments":{"addressee":"cluster","text":"why is web crashing?","parameters":{"project":"p1","location":"us-east1","cluster":"dev"}}}}`)
	if res := call["result"].(map[string]any); res["isError"] == true {
		t.Fatalf("call errored: %v", call)
	}
	reqs := got()
	want := map[string]string{"project": "p1", "location": "us-east1", "cluster": "dev"}
	if len(reqs) != 1 || len(reqs[0].Parameters) != len(want) {
		t.Fatalf("adapter got %+v", reqs)
	}
	for k, v := range want {
		if reqs[0].Parameters[k] != v {
			t.Fatalf("adapter got parameters %v, want %v", reqs[0].Parameters, want)
		}
	}
}

// addresseeOf lists the tools over a fresh server and returns the delegate
// tool's addressee description.
func addresseeOf(t *testing.T, targets *lib.DelegateTargets) string {
	t.Helper()
	inR, inW := io.Pipe()
	outR, outW := io.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, "/nonexistent.sock", targets, slog.Default()) }()
	list := rpc(t, inW, bufio.NewReader(outR), `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`)
	tool := list["result"].(map[string]any)["tools"].([]any)[0].(map[string]any)
	props := tool["inputSchema"].(map[string]any)["properties"].(map[string]any)
	return props["addressee"].(map[string]any)["description"].(string)
}

// With a profile on the turn's list, the tool names it and its blurb, not
// platform alone: a model told "only platform" sends "use e2e5" to platform.
func TestDelegateMCPNamesTheTurnsTargets(t *testing.T) {
	desc := addresseeOf(t, &lib.DelegateTargets{Targets: []lib.DelegateTarget{
		{Name: "platform", Description: "the platform agent"},
		{Name: "e2e5", Description: "reads one cluster's workloads"},
	}})
	for _, want := range []string{`"platform" (the platform agent)`, `"e2e5" (reads one cluster's workloads)`} {
		if !strings.Contains(desc, want) {
			t.Errorf("addressee description %q lacks %q", desc, want)
		}
	}
	if strings.Contains(desc, "only") {
		t.Errorf("addressee description %q still says only", desc)
	}
}

func TestDelegateMCPWithNoListNamesPlatformAndWithAnEmptyOneNamesNone(t *testing.T) {
	if desc := addresseeOf(t, nil); !strings.Contains(desc, `"platform"`) {
		t.Errorf("no list: %q, want platform named", desc)
	}
	if desc := addresseeOf(t, &lib.DelegateTargets{Targets: []lib.DelegateTarget{}}); !strings.Contains(desc, "No agent is available") {
		t.Errorf("empty list: %q, want none named", desc)
	}
}

// The adapter carries the submission's part to the server's env inside
// --mcp-config, and the server reads it back; a submission with none leaves
// the argument alone.
func TestTheTurnsTargetsReachTheMCPServer(t *testing.T) {
	want := lib.DelegateTargets{Targets: []lib.DelegateTarget{{Name: "platform"}, {Name: "e2e5", Description: "reads"}}}
	part, err := lib.DelegateTargetsPart(want)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "use e2e5"}, part}})
	cfg := `{"mcpServers":{"a2a":{"command":"worker-adapter","args":["mcp"]}}}`
	argv := []string{"claude", MCPConfigFlag, cfg, "-p"}

	out, rewrote := withDelegateTargets(argv, &lib.Envelope{Payload: payload})
	if !rewrote || argv[2] != cfg {
		t.Fatalf("rewrote=%v, input modified=%v", rewrote, argv[2] != cfg)
	}
	var parsed map[string]map[string]map[string]any
	if err := json.Unmarshal([]byte(out[2]), &parsed); err != nil {
		t.Fatal(err)
	}
	raw := parsed[MCPServersKey][DelegateMCPServer][MCPEnvKey].(map[string]any)[EnvDelegateTargets].(string)
	t.Setenv(EnvDelegateTargets, raw)
	if got := DelegateTargetsFromEnv(slog.Default()); got == nil || !reflect.DeepEqual(*got, want) {
		t.Fatalf("server read %+v, want %+v", got, want)
	}

	bare, _ := json.Marshal(lib.Message{Role: "user", Parts: []lib.Part{{Kind: "text", Text: "hi"}}})
	if out, rewrote := withDelegateTargets(argv, &lib.Envelope{Payload: bare}); rewrote || !reflect.DeepEqual(out, argv) {
		t.Fatalf("no part: rewrote=%v out=%v", rewrote, out)
	}
	t.Setenv(EnvDelegateTargets, `{"targets":[{"name":"a.b"}]}`)
	if got := DelegateTargetsFromEnv(slog.Default()); got != nil {
		t.Fatalf("an invalid list read as %+v, want none", got)
	}
}
