package kube

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/httpstream"
)

// fakeStream is one side of an in-memory port-forward stream.
type fakeStream struct {
	headers http.Header
	r       *io.PipeReader
	w       *io.PipeWriter
}

func (s *fakeStream) Read(p []byte) (int, error)  { return s.r.Read(p) }
func (s *fakeStream) Write(p []byte) (int, error) { return s.w.Write(p) }
func (s *fakeStream) Close() error                { return s.w.Close() }
func (s *fakeStream) Reset() error {
	_ = s.r.CloseWithError(errors.New("stream reset"))
	return s.w.CloseWithError(errors.New("stream reset"))
}
func (s *fakeStream) Headers() http.Header { return s.headers }
func (s *fakeStream) Identifier() uint32   { return 0 }

// fakeConnection is an httpstream.Connection whose streams' far ends arrive on remote.
type fakeConnection struct {
	remote    chan *fakeStream
	closeOnce sync.Once
	closed    chan bool
}

func newFakeConnection() *fakeConnection {
	return &fakeConnection{remote: make(chan *fakeStream, 8), closed: make(chan bool)}
}

func (c *fakeConnection) CreateStream(headers http.Header) (httpstream.Stream, error) {
	select {
	case <-c.closed:
		return nil, errors.New("connection closed")
	default:
	}
	toRemote, fromLocal := io.Pipe()
	toLocal, fromRemote := io.Pipe()
	h := headers.Clone()
	c.remote <- &fakeStream{headers: h, r: toRemote, w: fromRemote}
	return &fakeStream{headers: h, r: toLocal, w: fromLocal}, nil
}
func (c *fakeConnection) Close() error {
	c.closeOnce.Do(func() { close(c.closed) })
	return nil
}
func (c *fakeConnection) CloseChan() <-chan bool             { return c.closed }
func (c *fakeConnection) SetIdleTimeout(time.Duration)       {}
func (c *fakeConnection) RemoveStreams(...httpstream.Stream) {}

func nextRemote(t *testing.T, c *fakeConnection) *fakeStream {
	t.Helper()
	select {
	case s := <-c.remote:
		return s
	case <-time.After(5 * time.Second):
		t.Fatal("no stream was created")
		return nil
	}
}

func fakeTunnel(conns ...*fakeConnection) (*PortForwardTunnel, *int) {
	calls := 0
	var mu sync.Mutex
	return newPortForwardTunnel(func(context.Context) (httpstream.Connection, int, error) {
		mu.Lock()
		defer mu.Unlock()
		c := conns[min(calls, len(conns)-1)]
		calls++
		return c, 8089, nil
	}), &calls
}

func TestDialContextCarriesTheConnectionOnPortForwardStreams(t *testing.T) {
	fc := newFakeConnection()
	tun, _ := fakeTunnel(fc)
	conn, err := tun.DialContext(context.Background(), "tcp", "a2a-mcp:80")
	if err != nil {
		t.Fatalf("DialContext: %v", err)
	}
	defer conn.Close()
	errS, dataS := nextRemote(t, fc), nextRemote(t, fc)
	if errS.headers.Get(corev1.StreamType) != corev1.StreamTypeError || dataS.headers.Get(corev1.StreamType) != corev1.StreamTypeData {
		t.Fatalf("stream types %q, %q; want error then data", errS.headers.Get(corev1.StreamType), dataS.headers.Get(corev1.StreamType))
	}
	for _, h := range []http.Header{errS.headers, dataS.headers} {
		if h.Get(corev1.PortHeader) != "8089" || h.Get(corev1.PortForwardRequestIDHeader) != errS.headers.Get(corev1.PortForwardRequestIDHeader) {
			t.Fatalf("stream headers %v; want port 8089 and one request ID for the pair", h)
		}
	}
	go func() { _, _ = conn.Write([]byte("ping")) }()
	buf := make([]byte, 4)
	if _, err := io.ReadFull(dataS, buf); err != nil || string(buf) != "ping" {
		t.Fatalf("the pod side read %q, %v; want ping", buf, err)
	}
	go func() { _, _ = dataS.Write([]byte("pong")) }()
	if _, err := io.ReadFull(conn, buf); err != nil || string(buf) != "pong" {
		t.Fatalf("the bridge side read %q, %v; want pong", buf, err)
	}
	if conn.RemoteAddr().String() != "a2a-mcp" || conn.LocalAddr().Network() != streamTunnelNetwork {
		t.Fatalf("addresses %v %v; want the tunnel's, not a socket's", conn.LocalAddr(), conn.RemoteAddr())
	}
}

func TestDialContextReturnsTheErrorStreamsMessage(t *testing.T) {
	fc := newFakeConnection()
	tun, _ := fakeTunnel(fc)
	conn, err := tun.DialContext(context.Background(), "tcp", "a2a-mcp:80")
	if err != nil {
		t.Fatalf("DialContext: %v", err)
	}
	defer conn.Close()
	errS := nextRemote(t, fc)
	nextRemote(t, fc)
	_, _ = errS.Write([]byte("error forwarding port 8089 to pod mcp-1: connection refused"))
	_ = errS.Close()
	_, err = conn.Read(make([]byte, 1))
	if err == nil || !strings.Contains(err.Error(), "connection refused") {
		t.Fatalf("Read after an error-stream message: %v; want that message", err)
	}
}

func TestDialContextReopensAClosedConnection(t *testing.T) {
	first, second := newFakeConnection(), newFakeConnection()
	tun, calls := fakeTunnel(first, second)
	if _, err := tun.DialContext(context.Background(), "tcp", ""); err != nil {
		t.Fatal(err)
	}
	_ = first.Close()
	if _, err := tun.DialContext(context.Background(), "tcp", ""); err != nil {
		t.Fatal(err)
	}
	if *calls != 2 {
		t.Fatalf("connector called %d times; want a new connection after the first closed", *calls)
	}
	nextRemote(t, second)
	tun.Close()
	if _, err := tun.DialContext(context.Background(), "tcp", ""); !errors.Is(err, errTunnelClosed) {
		t.Fatalf("DialContext after Close: %v; want errTunnelClosed", err)
	}
}

func TestTunnelURLIsNotALocalListener(t *testing.T) {
	tun, _ := fakeTunnel(newFakeConnection())
	u, err := url.Parse(tun.MCPURL())
	if err != nil {
		t.Fatal(err)
	}
	if h := u.Hostname(); h == "127.0.0.1" || h == "localhost" || h == "::1" || u.Port() != "" {
		t.Fatalf("MCPURL %q names a local port; the kube path must open no listener", tun.MCPURL())
	}
}
