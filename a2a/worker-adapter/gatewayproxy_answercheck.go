package workeradapter

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
)

// An answer that ends badly must reach the harness as a failed call. The
// core-agent harness's Gemini client (the genai SDK) treats a stream that
// ends cleanly before any finishReason, or that carries an error object, as
// a completed answer whose text is whatever arrived; it reports a failure
// only when the connection breaks. So the proxy, which streams each chunk
// through as it comes, watches the answer as it passes, and if it ends cut
// short or carries an error it breaks the connection to the harness instead
// of ending it: the read fails, ReverseProxy aborts the response, and the
// client sees an unexpected EOF, which fails its turn.
//
// What counts as whole: the last event of a streamed answer (or the body of
// a unary one) has a candidate with a finishReason, or the prompt was
// blocked (promptFeedback.blockReason). countTokens answers carry neither and
// aren't checked; nor are non-2xx answers, which the client already reads as
// errors.
var (
	errAnswerCutShort = errors.New("gateway proxy: the model's answer ended before it finished")
	errAnswerError    = errors.New("gateway proxy: the model's answer carried an error")
)

// answerChunk is the part of a GenerateContentResponse the check reads.
type answerChunk struct {
	Candidates []struct {
		FinishReason string `json:"finishReason"`
	} `json:"candidates"`
	PromptFeedback *struct {
		BlockReason string `json:"blockReason"`
	} `json:"promptFeedback"`
	Error json.RawMessage `json:"error"`
}

// verdict reads one chunk: whether it ends the answer, and whether it is an
// error (or doesn't parse, which a whole answer never does).
func (c answerChunk) verdict() (finished bool) {
	for _, cand := range c.Candidates {
		if cand.FinishReason != "" {
			return true
		}
	}
	return c.PromptFeedback != nil && c.PromptFeedback.BlockReason != ""
}

func parseChunk(data []byte) (finished bool, err error) {
	var c answerChunk
	if json.Unmarshal(data, &c) != nil {
		return false, errAnswerError
	}
	if len(c.Error) > 0 && !bytes.Equal(bytes.TrimSpace(c.Error), []byte("null")) {
		return false, errAnswerError
	}
	return c.verdict(), nil
}

// answerCheck passes the body through unchanged while it watches it.
type answerCheck struct {
	r     io.ReadCloser
	sse   bool
	buf   []byte // SSE: the event not yet closed; unary: the body so far
	whole bool   // SSE: the last closed event finished the answer
	err   error
}

// checkAnswer wraps a 2xx generation answer's body; anything else passes
// unwatched. The length is dropped so a broken answer can't end at a
// declared length the harness already reached.
func checkAnswer(resp *http.Response) {
	if resp.StatusCode < 200 || resp.StatusCode > 299 || strings.HasSuffix(resp.Request.URL.Path, ":countTokens") {
		return
	}
	resp.Body = &answerCheck{
		r:   resp.Body,
		sse: strings.HasPrefix(strings.ToLower(strings.TrimSpace(resp.Header.Get("Content-Type"))), "text/event-stream"),
	}
	resp.ContentLength = -1
	resp.Header.Del("Content-Length")
}

func (a *answerCheck) Read(p []byte) (int, error) {
	if a.err != nil {
		return 0, a.err
	}
	n, err := a.r.Read(p)
	if n > 0 {
		a.buf = append(a.buf, p[:n]...)
		if a.sse {
			if ferr := a.events(); ferr != nil {
				// This chunk carries the error: don't pass it on.
				a.err = ferr
				return 0, ferr
			}
		}
	}
	if errors.Is(err, io.EOF) {
		if ferr := a.end(); ferr != nil {
			a.err = ferr
			return n, ferr
		}
	}
	return n, err
}

// events reads every SSE event closed so far. An `event: error` or an error
// object fails the answer.
func (a *answerCheck) events() error {
	for {
		norm := a.buf
		i := bytes.Index(norm, []byte("\n\n"))
		j := bytes.Index(norm, []byte("\r\n\r\n"))
		end, skip := i, 2
		if j >= 0 && (i < 0 || j < i) {
			end, skip = j, 4
		}
		if end < 0 {
			return nil
		}
		event := a.buf[:end]
		a.buf = a.buf[end+skip:]
		var data [][]byte
		for _, line := range bytes.Split(event, []byte("\n")) {
			line = bytes.TrimSuffix(line, []byte("\r"))
			switch {
			case bytes.HasPrefix(line, []byte("event:")):
				if bytes.EqualFold(bytes.TrimSpace(line[len("event:"):]), []byte("error")) {
					return errAnswerError
				}
			case bytes.HasPrefix(line, []byte("data:")):
				v := bytes.TrimPrefix(line[len("data:"):], []byte(" "))
				if !bytes.Equal(bytes.TrimSpace(v), []byte("[DONE]")) {
					data = append(data, v)
				}
			}
		}
		if len(data) == 0 {
			continue
		}
		finished, err := parseChunk(bytes.Join(data, []byte("\n")))
		if err != nil {
			return err
		}
		a.whole = finished
	}
}

// end judges the answer once the gateway's body ends.
func (a *answerCheck) end() error {
	if a.sse {
		if len(bytes.TrimSpace(a.buf)) > 0 || !a.whole {
			return errAnswerCutShort
		}
		return nil
	}
	finished, err := parseChunk(a.buf)
	if err != nil {
		return err
	}
	if !finished {
		return errAnswerCutShort
	}
	return nil
}

func (a *answerCheck) Close() error { return a.r.Close() }
