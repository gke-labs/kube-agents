package workeradapter

import (
	"bufio"
	"compress/gzip"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// gzipSSEGateway answers with three SSE chunks, gzip-compressed when the
// call asks for gzip, flushing each and waiting for release before the last.
func gzipSSEGateway(release <-chan struct{}) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		var out io.Writer = w
		var zw *gzip.Writer
		if strings.Contains(r.Header.Get("Accept-Encoding"), "gzip") {
			w.Header().Set("Content-Encoding", "gzip")
			zw = gzip.NewWriter(w)
			out = zw
		}
		flush := func() {
			if zw != nil {
				_ = zw.Flush()
			}
			w.(http.Flusher).Flush()
		}
		_, _ = io.WriteString(out, "data: {\"n\":1}\n\n")
		flush()
		_, _ = io.WriteString(out, "data: {\"n\":2}\n\n")
		flush()
		<-release
		_, _ = io.WriteString(out, "data: {\"n\":3,\"finishReason\":\"STOP\"}\n\n")
		if zw != nil {
			_ = zw.Close()
		}
	})
}

// TestACompressedAnswerStreamsThroughTheProxy: a harness on Go's default
// transport (core-agent's genai client) asks for gzip implicitly. The proxy
// forwards Accept-Encoding and passes Content-Encoding back, so the client
// decompresses, and each chunk arrives as the gateway flushes it, before
// the answer ends: the proxy streams, it doesn't hold.
func TestACompressedAnswerStreamsThroughTheProxy(t *testing.T) {
	release := make(chan struct{})
	up := httptest.NewServer(gzipSSEGateway(release))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL)

	resp, err := http.Post(p.URL+"/v1beta/models/m:streamGenerateContent?alt=sse", "application/json", strings.NewReader("{}"))
	if err != nil {
		close(release)
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if !resp.Uncompressed {
		t.Errorf("the client got the answer still compressed (Content-Encoding %q); want its transport to have decompressed it", resp.Header.Get("Content-Encoding"))
	}
	lines := make(chan string, 8)
	go func() {
		sc := bufio.NewScanner(resp.Body)
		for sc.Scan() {
			if l := sc.Text(); strings.HasPrefix(l, "data: ") {
				lines <- l
			}
		}
		close(lines)
	}()
	for _, want := range []string{`data: {"n":1}`, `data: {"n":2}`} {
		select {
		case got := <-lines:
			if got != want {
				t.Errorf("chunk %q, want %q", got, want)
			}
		case <-time.After(5 * time.Second):
			close(release)
			t.Fatalf("chunk %q didn't arrive before the answer ended: the proxy held it", want)
		}
	}
	close(release)
	got, ok := <-lines
	if !ok || got != `data: {"n":3,"finishReason":"STOP"}` {
		t.Errorf("last chunk %q (ok %v)", got, ok)
	}
}

// TestAnExplicitGzipCallerGetsTheGatewaysEncoding: a caller that sets
// Accept-Encoding itself, and so decompresses itself, gets the gzip body
// with its Content-Encoding, intact.
func TestAnExplicitGzipCallerGetsTheGatewaysEncoding(t *testing.T) {
	release := make(chan struct{})
	close(release)
	up := httptest.NewServer(gzipSSEGateway(release))
	t.Cleanup(up.Close)
	p := startProxyFor(t, up.URL)
	req, _ := http.NewRequest(http.MethodPost, p.URL+"/v1beta/models/m:streamGenerateContent?alt=sse", strings.NewReader("{}"))
	req.Header.Set("Accept-Encoding", "gzip")
	resp, err := (&http.Client{Transport: &http.Transport{DisableCompression: true}}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.Header.Get("Content-Encoding") != "gzip" {
		t.Fatalf("Content-Encoding %q, want gzip passed back", resp.Header.Get("Content-Encoding"))
	}
	zr, err := gzip.NewReader(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	b, err := io.ReadAll(zr)
	if err != nil || !strings.HasSuffix(string(b), "data: {\"n\":3,\"finishReason\":\"STOP\"}\n\n") {
		t.Errorf("decoded %q, err %v", b, err)
	}
}
