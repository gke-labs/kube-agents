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
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, sock, slog.Default()) }()
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
	go func() { _ = ServeDelegateMCP(ctx, inR, outW, sock, slog.Default()) }()
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
		_ = ServeDelegateMCP(ctx, inR, outW, filepath.Join(dir, "absent.sock"), slog.Default())
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
