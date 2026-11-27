package workeradapter

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeGateway records what reached it and answers each call with a short SSE
// stream.
type fakeGateway struct {
	mu    sync.Mutex
	calls []recordedCall
}

type recordedCall struct {
	method, path, query, host, auth string
	hdr                             http.Header
	body                            []byte
}

func (g *fakeGateway) handler(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	g.mu.Lock()
	g.calls = append(g.calls, recordedCall{r.Method, r.URL.Path, r.URL.RawQuery, r.Host, r.Header.Get("Authorization"), r.Header.Clone(), body})
	g.mu.Unlock()
	w.Header().Set("Content-Type", "text/event-stream")
	for i := 1; i <= 3; i++ {
		finish := ""
		if i == 3 {
			finish = `,"finishReason":"STOP"`
		}
		fmt.Fprintf(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"%d\"}]}%s}]}\n\n", i, finish)
		w.(http.Flusher).Flush()
	}
}

func (g *fakeGateway) seen() []recordedCall {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]recordedCall(nil), g.calls...)
}

func startProxyFor(t *testing.T, gateway string) *GatewayProxy {
	t.Helper()
	p, err := StartGatewayProxy(gateway, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return p
}

// TestTheGatewayProxyCarriesAModelCallAsItCame: a Gemini-format model call
// reaches the gateway with its method, path, query and body unchanged, under
// the gateway URL's own path, with no credential added, and its streamed
// answer comes back chunk by chunk.
func TestTheGatewayProxyCarriesAModelCallAsItCame(t *testing.T) {
	g := &fakeGateway{}
	up := httptest.NewServer(http.HandlerFunc(g.handler))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL+"/llm")

	if u, _ := url.Parse(p.URL); u.Hostname() != "127.0.0.1" {
		t.Fatalf("proxy URL %s, want 127.0.0.1", p.URL)
	}
	body := `{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}`
	resp, err := http.Post(p.URL+"/v1beta/models/model-default:streamGenerateContent?alt=sse", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var chunks []string
	sc := bufio.NewScanner(resp.Body)
	for sc.Scan() {
		if line := sc.Text(); strings.HasPrefix(line, "data: ") {
			chunks = append(chunks, line)
		}
	}
	if len(chunks) != 3 {
		t.Errorf("chunks %q, want 3", chunks)
	}
	calls := g.seen()
	if len(calls) != 1 {
		t.Fatalf("gateway saw %d calls, want 1", len(calls))
	}
	c := calls[0]
	upHost := strings.TrimPrefix(up.URL, "http://")
	if c.method != http.MethodPost || c.path != "/llm/v1beta/models/model-default:streamGenerateContent" || c.query != "alt=sse" || string(c.body) != body || c.host != upHost {
		t.Errorf("gateway saw %+v", c)
	}
	if c.auth != "" {
		t.Errorf("the proxy added a credential %q; it passes calls through as they came", c.auth)
	}
}

// TestTheGatewayProxyIsNotAnOpenProxy: an absolute-form request, another
// Host, a path outside the model calls, a climbing path and a method other
// than GET or POST are refused, and none reaches the gateway.
func TestTheGatewayProxyIsNotAnOpenProxy(t *testing.T) {
	g := &fakeGateway{}
	up := httptest.NewServer(http.HandlerFunc(g.handler))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL)
	addr := strings.TrimPrefix(p.URL, "http://")

	raw := func(req string) int {
		t.Helper()
		conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
		if _, err := io.WriteString(conn, req); err != nil {
			t.Fatal(err)
		}
		resp, err := http.ReadResponse(bufio.NewReader(conn), nil)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	for _, c := range []struct {
		name string
		req  string
		want int
	}{
		{"absolute form", "GET http://example.com/v1beta/models/x HTTP/1.1\r\nHost: example.com\r\n\r\n", http.StatusForbidden},
		{"another host", "GET /v1beta/models/x HTTP/1.1\r\nHost: example.com\r\n\r\n", http.StatusForbidden},
		{"admin route", "GET /key/list HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"anthropic route", "POST /v1/messages HTTP/1.1\r\nHost: " + addr + "\r\nContent-Length: 0\r\n\r\n", http.StatusNotFound},
		{"climbing path", "GET /v1beta/models/../../key/list HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"encoded climb", "GET /v1beta/models/%2e%2e/%2e%2e/key/list HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"encoded slash", "GET /v1beta/models/m%2F..%2Fx:generateContent HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"double encoded", "GET /v1beta/models/%252e%252e:generateContent HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"double slash", "GET /v1beta/models//m:generateContent HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"another call", "GET /v1beta/models/m:embedContent HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"model list", "GET /v1beta/models/ HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusNotFound},
		{"upgrade", "GET /v1beta/models/m:generateContent HTTP/1.1\r\nHost: " + addr + "\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n", http.StatusBadRequest},
		{"delete", "DELETE /v1beta/models/x:generateContent HTTP/1.1\r\nHost: " + addr + "\r\n\r\n", http.StatusMethodNotAllowed},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := raw(c.req); got != c.want {
				t.Errorf("status %d, want %d", got, c.want)
			}
		})
	}
	if calls := g.seen(); len(calls) != 0 {
		t.Errorf("refused requests reached the gateway: %+v", calls)
	}
}

// TestTheGatewayProxyBoundsWhatPassesThrough: a request body over the cap is
// refused before the gateway sees it, and a response over the cap is cut off.
func TestTheGatewayProxyBoundsWhatPassesThrough(t *testing.T) {
	g := &fakeGateway{}
	up := httptest.NewServer(http.HandlerFunc(g.handler))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL)

	big := bytes.Repeat([]byte("x"), gatewayProxyMaxRequestBytes+1)
	resp, err := http.Post(p.URL+"/v1beta/models/m:generateContent", "application/json", bytes.NewReader(big))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Errorf("an oversized request got %d, want 413", resp.StatusCode)
	}
	if calls := g.seen(); len(calls) != 0 {
		t.Errorf("the oversized request reached the gateway (%d calls)", len(calls))
	}

	huge := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		chunk := bytes.Repeat([]byte("y"), 1<<20)
		for i := 0; i < gatewayProxyMaxResponseBytes>>20+4; i++ {
			if _, err := w.Write(chunk); err != nil {
				return
			}
		}
	}))
	t.Cleanup(huge.Close)
	hp := startProxyFor(t, huge.URL)
	resp, err = http.Post(hp.URL+"/v1beta/models/m:generateContent", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	n, _ := io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if n > gatewayProxyMaxResponseBytes || n < gatewayProxyMaxResponseBytes-(1<<20) {
		t.Errorf("the response passed %d bytes; want it cut off at the %d cap", n, gatewayProxyMaxResponseBytes)
	}

	// A whole answer of exactly the cap passes.
	head, tail := `{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"`, `"}]}}]}`
	exactBody := head + strings.Repeat("z", gatewayProxyMaxResponseBytes-len(head)-len(tail)) + tail
	exact := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, exactBody)
	}))
	t.Cleanup(exact.Close)
	ep := startProxyFor(t, exact.URL)
	resp, err = http.Post(ep.URL+"/v1beta/models/m:generateContent", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	n, err = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	if err != nil || n != gatewayProxyMaxResponseBytes {
		t.Errorf("a response of exactly the cap passed %d bytes, err %v; want all %d", n, err, gatewayProxyMaxResponseBytes)
	}

	// A chunked body, with no length to refuse up front, is cut at the cap.
	pr, pw := io.Pipe()
	go func() {
		chunk := bytes.Repeat([]byte("c"), 1<<20)
		for i := 0; i <= gatewayProxyMaxRequestBytes>>20; i++ {
			if _, err := pw.Write(chunk); err != nil {
				return
			}
		}
		pw.Close()
	}()
	req, _ := http.NewRequest(http.MethodPost, p.URL+"/v1beta/models/m:generateContent", pr)
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Errorf("an oversized chunked request got %d, want 413", resp.StatusCode)
	}
}

// TestTheGatewayProxyDropsWhatACallerMayNotSay: only the headers a model
// call needs reach the gateway, so no credential or attribution a caller
// sets does, named or not; a key in the query is dropped, and the rest of
// the query passes.
func TestTheGatewayProxyDropsWhatACallerMayNotSay(t *testing.T) {
	g := &fakeGateway{}
	up := httptest.NewServer(http.HandlerFunc(g.handler))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL)
	req, _ := http.NewRequest(http.MethodPost, p.URL+"/v1beta/models/m:streamGenerateContent?alt=sse&key=caller-secret", strings.NewReader("{}"))
	for _, h := range []string{"Authorization", "X-Api-Key", "Api-Key", "X-Goog-Api-Key", "X-Litellm-Tags", "X-Litellm-End-User-Id", "Cookie", "Ocp-Apim-Subscription-Key", "X-Anything-Else"} {
		req.Header.Set(h, "caller-secret")
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	calls := g.seen()
	if len(calls) != 1 {
		t.Fatalf("gateway saw %d calls, want 1", len(calls))
	}
	c := calls[0]
	if c.query != "alt=sse" {
		t.Errorf("query %q, want alt=sse alone", c.query)
	}
	for h, v := range c.hdr {
		if strings.Contains(strings.Join(v, ","), "caller-secret") {
			t.Errorf("header %s reached the gateway", h)
		}
	}
	if c.hdr.Get("Content-Type") != "application/json" {
		t.Errorf("Content-Type %q did not pass through", c.hdr.Get("Content-Type"))
	}
}

// TestAFailedGatewayCallLogsNoQuery: when the gateway can't be reached, the
// log line names the path and the cause, not the outbound URL's query.
func TestAFailedGatewayCallLogsNoQuery(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	dead := "http://" + ln.Addr().String()
	ln.Close()
	var buf bytes.Buffer
	p, err := StartGatewayProxy(dead, slog.New(slog.NewTextHandler(&buf, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	resp, err := http.Post(p.URL+"/v1beta/models/m:generateContent?alt=QUERYMARK", "application/json", strings.NewReader("{\"BODYMARK\":1}"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status %d, want 502", resp.StatusCode)
	}
	if out := buf.String(); strings.Contains(out, "QUERYMARK") || strings.Contains(out, "BODYMARK") || !strings.Contains(out, "gateway proxy") {
		t.Errorf("log %q: want the failure, without the query or body", out)
	}
}

// TestTheGatewayProxyRefusesAGatewayURLItCantUse: a URL that isn't plain
// http(s) to a host, or carries credentials, a query or a fragment, is
// refused, and nothing is left listening.
func TestTheGatewayProxyRefusesAGatewayURLItCantUse(t *testing.T) {
	for _, u := range []string{"", "inference-gateway", "ftp://gw", "http://", "http://user:pw@gw", "http://gw?x=1", "http://gw#f"} {
		if p, err := StartGatewayProxy(u, nil); err == nil {
			_ = p.Close()
			t.Errorf("gateway URL %q accepted", u)
		}
	}
}

// TestAClosedGatewayProxyStopsListening: Close frees the port.
func TestAClosedGatewayProxyStopsListening(t *testing.T) {
	up := httptest.NewServer(http.HandlerFunc((&fakeGateway{}).handler))
	t.Cleanup(up.Close)
	p, err := StartGatewayProxy(up.URL, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Close(); err != nil {
		t.Fatal(err)
	}
	if conn, err := net.DialTimeout("tcp", strings.TrimPrefix(p.URL, "http://"), time.Second); err == nil {
		// A dial to a loopback port nothing listens on can, rarely, pick
		// that port as its own source and connect to itself. That is no
		// listener.
		self := conn.LocalAddr().String() == conn.RemoteAddr().String()
		conn.Close()
		if !self {
			t.Error("the proxy still accepts connections after Close")
		}
	}
}
