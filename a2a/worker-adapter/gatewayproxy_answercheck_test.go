package workeradapter

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const (
	sseChunk  = "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hi\"}]}}]}\n\n"
	sseFinish = "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\" there\"}]},\"finishReason\":\"STOP\"}]}\n\n"
)

// TestAnAnswerThatEndsBadlyBreaksTheConnection: the proxy streams each
// answer through, and one that ends before it finished, or carries an
// error, ends in a broken connection (an error reading the body) rather
// than a clean end; a whole answer reads to a clean EOF, byte for byte.
// Non-2xx answers and countTokens pass unwatched.
func TestAnAnswerThatEndsBadlyBreaksTheConnection(t *testing.T) {
	for _, tc := range []struct {
		name, ctype, body, method string
		status                    int
		broken                    bool
	}{
		{name: "whole stream", ctype: "text/event-stream", body: sseChunk + sseFinish},
		{name: "whole stream, CRLF", ctype: "text/event-stream", body: strings.ReplaceAll(sseChunk+sseFinish, "\n", "\r\n")},
		{name: "whole stream then [DONE]", ctype: "text/event-stream", body: sseChunk + sseFinish + "data: [DONE]\n\n"},
		{name: "blocked prompt", ctype: "text/event-stream", body: "data: {\"promptFeedback\":{\"blockReason\":\"SAFETY\"}}\n\n"},
		{name: "cut before the finish", ctype: "text/event-stream", body: sseChunk, broken: true},
		{name: "last event not closed", ctype: "text/event-stream", body: sseChunk + strings.TrimSuffix(sseFinish, "\n"), broken: true},
		{name: "text after the finish", ctype: "text/event-stream", body: sseFinish + sseChunk, broken: true},
		{name: "error object", ctype: "text/event-stream", body: sseChunk + "data: {\"error\":{\"code\":500,\"message\":\"x\"}}\n\n" + sseFinish, broken: true},
		{name: "error beside a finish", ctype: "text/event-stream", body: "data: {\"candidates\":[{\"finishReason\":\"STOP\"}],\"error\":{\"message\":\"x\"}}\n\n", broken: true},
		{name: "event: error", ctype: "text/event-stream", body: sseChunk + "event: error\ndata: {}\n\n" + sseFinish, broken: true},
		{name: "event that doesn't parse", ctype: "text/event-stream", body: sseChunk + "data: {\"candidates\":[\n\n", broken: true},
		{name: "empty stream", ctype: "text/event-stream", body: "", broken: true},
		{name: "whole unary answer", ctype: "application/json", method: "generateContent", body: `{"candidates":[{"finishReason":"STOP"}]}`},
		{name: "unary answer with no finish", ctype: "application/json", method: "generateContent", body: `{"candidates":[{"content":{}}]}`, broken: true},
		{name: "countTokens", ctype: "application/json", method: "countTokens", body: `{"totalTokens":3}`},
		{name: "gateway error", ctype: "application/json", status: 500, body: `{"error":{"code":500}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", tc.ctype)
				if tc.status != 0 {
					w.WriteHeader(tc.status)
				}
				_, _ = io.WriteString(w, tc.body)
			}))
			t.Cleanup(up.Close)
			p := startProxyFor(t, up.URL)
			method := tc.method
			if method == "" {
				method = "streamGenerateContent"
			}
			resp, err := http.Post(p.URL+"/v1beta/models/m:"+method, "application/json", strings.NewReader("{}"))
			if err != nil {
				// A break before any of the answer was sent fails the call
				// itself, which is broken too.
				if !tc.broken {
					t.Fatal(err)
				}
				return
			}
			defer resp.Body.Close()
			b, err := io.ReadAll(resp.Body)
			if tc.broken {
				if err == nil {
					t.Errorf("read %q to a clean end; want the connection broken", b)
				}
				return
			}
			if err != nil || string(b) != tc.body {
				t.Errorf("read %q, err %v; want the answer byte for byte and a clean end", b, err)
			}
		})
	}
}
