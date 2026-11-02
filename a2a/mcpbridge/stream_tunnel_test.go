package mcpbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// pipeListener hands the server ends of pipeTunnel's connections to an http.Server.
type pipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
}

func (l *pipeListener) Accept() (net.Conn, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}
func (l *pipeListener) Close() error   { close(l.closed); return nil }
func (l *pipeListener) Addr() net.Addr { return &net.UnixAddr{Name: "pipe", Net: "pipe"} }

// pipeTunnel is a StreamTunnel whose connections are in-memory pipes: no socket exists.
type pipeTunnel struct {
	l        *pipeListener
	dials    atomic.Int32
	restarts atomic.Int32
}

func (t *pipeTunnel) MCPURL() string { return "http://a2a-mcp" }
func (t *pipeTunnel) Restart()       { t.restarts.Add(1) }
func (t *pipeTunnel) Close()         {}
func (t *pipeTunnel) DialContext(context.Context, string, string) (net.Conn, error) {
	t.dials.Add(1)
	client, server := net.Pipe()
	t.l.conns <- server
	return client, nil
}

type pipeClusterAccess struct {
	noClusterAccess
	tun *pipeTunnel
}

func (c pipeClusterAccess) PopulateDeploymentCredentials(_ context.Context, prof *DeploymentProfile) error {
	prof.MCPToken = "kube-secret-token"
	return nil
}
func (c pipeClusterAccess) StartGatewayPortForward(context.Context, DeploymentProfile) (Tunnel, error) {
	return c.tun, nil
}

func TestKubePathSendsTheBearerOnlyThroughTheTunnelsDialer(t *testing.T) {
	// A proxy in the environment must not see the request either.
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("http_proxy", "http://127.0.0.1:1")
	l := &pipeListener{conns: make(chan net.Conn, 4), closed: make(chan struct{})}
	auth := make(chan string, 4)
	hs := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		auth <- r.Header.Get("Authorization")
		w.Header().Set("Content-Type", contentTypeNDJSON)
		_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: "status ok"}}}))
	})}
	go func() { _ = hs.Serve(l) }()
	t.Cleanup(func() { _ = hs.Close() })

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{{Name: "d", KubeContext: "ctx", Namespace: "ns", PlatformAgentName: "pa", Ready: true}}); err != nil {
		t.Fatal(err)
	}
	tun := &pipeTunnel{l: l}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: pipeClusterAccess{tun: tun}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	t.Setenv(EnvMCPToken, "")
	res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(`{"deployment":"d"}`))
	if res.IsError || res.Content[0].Text != "status ok" {
		t.Fatalf("connect_deployment over the stream tunnel: %+v", res)
	}
	if got := <-auth; got != "Bearer kube-secret-token" {
		t.Fatalf("the server behind the tunnel saw Authorization %q", got)
	}
	if tun.dials.Load() == 0 {
		t.Fatal("the request did not go through the tunnel's DialContext")
	}
}

func TestCallerCancellationDoesNotRestartTheSharedTunnel(t *testing.T) {
	l := &pipeListener{conns: make(chan net.Conn, 8), closed: make(chan struct{})}
	inHandler := make(chan struct{}, 8)
	hs := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		if containsBlockTool(body) {
			inHandler <- struct{}{}
			<-r.Context().Done()
			return
		}
		w.Header().Set("Content-Type", contentTypeNDJSON)
		_, _ = fmt.Fprintf(w, "%s\n", marshalRPCResult(req.ID, CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: "status ok"}}}))
	})}
	go func() { _ = hs.Serve(l) }()
	t.Cleanup(func() { _ = hs.Close() })

	store, err := NewConfigStore(filepath.Join(t.TempDir(), "mcp-config.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.UpsertDeployments([]DeploymentProfile{{Name: "d", KubeContext: "ctx", Namespace: "ns", PlatformAgentName: "pa", Ready: true}}); err != nil {
		t.Fatal(err)
	}
	tun := &pipeTunnel{l: l}
	srv, err := NewServer(ServerOptions{Store: store, Cluster: pipeClusterAccess{tun: tun}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Close)
	t.Setenv(EnvMCPToken, "")
	if res := srv.CallTool(context.Background(), ToolConnectDeployment, json.RawMessage(`{"deployment":"d"}`)); res.IsError {
		t.Fatalf("connect_deployment: %+v", res)
	}
	srv.mu.Lock()
	sess := srv.session
	srv.mu.Unlock()
	if sess == nil {
		t.Fatal("no session after connect_deployment")
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, _, _, err := sess.callRemoteTool(ctx, "block_me", nil, CallMeta{}, PersistentConfig{}, nil)
		done <- err
	}()
	select {
	case <-inHandler:
	case <-time.After(5 * time.Second):
		t.Fatal("the blocking call never reached the server")
	}
	cancel()
	if err := <-done; err == nil {
		t.Fatal("the cancelled call returned no error")
	}
	if n := tun.restarts.Load(); n != 0 {
		t.Fatalf("the caller's own cancellation restarted the shared tunnel %d time(s)", n)
	}
	if sess.WaitForHealthy(ctx, time.Second) {
		t.Fatal("a health probe on a cancelled context reported healthy")
	}
	if n := tun.restarts.Load(); n != 0 {
		t.Fatalf("a health probe on the caller's cancelled context restarted the tunnel %d time(s)", n)
	}
}

func containsBlockTool(body []byte) bool {
	var req struct {
		Params struct {
			Name string `json:"name"`
		} `json:"params"`
	}
	return json.Unmarshal(body, &req) == nil && req.Params.Name == "block_me"
}

// countingTunnel is a Tunnel that only counts restarts.
type countingTunnel struct{ restarts atomic.Int32 }

func (t *countingTunnel) MCPURL() string { return "http://127.0.0.1:1" }
func (t *countingTunnel) Restart()       { t.restarts.Add(1) }
func (t *countingTunnel) Close()         {}

func TestAFailingCallAndCloseDoNotRaceOnTheTunnel(t *testing.T) {
	// Requests fail at once (a transport with no shared state, so it adds no synchronisation) while another goroutine closes the
	// session, as a reconnect closes the one it replaces. Run with -race.
	for i := 0; i < 50; i++ {
		g := &gatewayBridgeSession{tunnel: &countingTunnel{}, baseURL: "http://127.0.0.1:1", httpClient: &http.Client{Transport: failingRoundTripper{}}}
		var wg sync.WaitGroup
		wg.Add(3)
		go func() {
			defer wg.Done()
			_, _, _, _ = g.callRemoteTool(context.Background(), "x", nil, CallMeta{}, PersistentConfig{}, nil)
		}()
		go func() { defer wg.Done(); _ = g.WaitForHealthy(context.Background(), time.Second) }()
		go func() { defer wg.Done(); g.Close() }()
		wg.Wait()
	}
}

type failingRoundTripper struct{}

func (failingRoundTripper) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("transport down")
}
