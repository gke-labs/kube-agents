package mcpbridge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"sync/atomic"
	"testing"
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
	l     *pipeListener
	dials atomic.Int32
}

func (t *pipeTunnel) MCPURL() string { return "http://a2a-mcp" }
func (t *pipeTunnel) Restart()       {}
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
