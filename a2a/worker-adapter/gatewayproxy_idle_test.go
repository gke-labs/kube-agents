package workeradapter

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func proxyWithIdle(t *testing.T, h http.Handler, idle time.Duration) string {
	t.Helper()
	up := httptest.NewServer(h)
	t.Cleanup(up.Close)
	p, err := StartGatewayProxyWith(up.URL, slog.New(slog.NewTextHandler(io.Discard, nil)), GatewayProxyOptions{StreamIdle: idle})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	return p.URL
}

// TestAStalledAnswerBreaksTheConnectionAfterTheIdleBound: the gateway sends
// headers and a chunk, then nothing. After StreamIdle the proxy breaks the
// connection, so the harness's read fails within the bound rather than at
// the task deadline.
func TestAStalledAnswerBreaksTheConnectionAfterTheIdleBound(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	proxy := proxyWithIdle(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, sseChunk)
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}), 300*time.Millisecond)
	client := &http.Client{Timeout: 10 * time.Second}
	start := time.Now()
	resp, err := client.Post(proxy+"/v1beta/models/m:streamGenerateContent", "application/json", strings.NewReader("{}"))
	if err == nil {
		_, err = io.ReadAll(resp.Body)
		resp.Body.Close()
	}
	took := time.Since(start)
	if err == nil {
		t.Fatal("the stalled answer read to a clean end; want the connection broken")
	}
	if took > 5*time.Second {
		t.Errorf("the break took %v; want it soon after the 300ms idle bound", took)
	}
}

// TestASteadyAnswerOutlastsTheIdleBound: chunks that keep coming, each well
// inside the bound, run longer than the bound in total and arrive whole.
func TestASteadyAnswerOutlastsTheIdleBound(t *testing.T) {
	proxy := proxyWithIdle(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for i := 0; i < 6; i++ {
			_, _ = io.WriteString(w, sseChunk)
			w.(http.Flusher).Flush()
			time.Sleep(100 * time.Millisecond)
		}
		_, _ = io.WriteString(w, sseFinish)
	}), 300*time.Millisecond)
	resp, err := http.Post(proxy+"/v1beta/models/m:streamGenerateContent", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil || !strings.HasSuffix(string(b), sseFinish) {
		t.Errorf("read %d bytes, err %v; want the whole answer", len(b), err)
	}
}

// TestTheStreamIdleDefault: a zero StreamIdle means the default, the
// install's agents' stale-stream bound.
func TestTheStreamIdleDefault(t *testing.T) {
	if DefaultGatewayProxyStreamIdle != 120*time.Second {
		t.Errorf("default %v, want 120s", DefaultGatewayProxyStreamIdle)
	}
}
