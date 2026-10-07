/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// POST /api/chat/stream: one turn, with a live status line while it runs.
//
// It takes the same request and applies the same checks as POST /api/chat.
// It calls Hermes' POST /api/sessions/{id}/chat/stream and relays a reduced
// SSE stream to the page: `status` events carrying one short plain-text line
// ("Running kubectl_get: pods -n x"), then one `reply` event with the full
// reply, or one `error` event in /api/chat's error shape. A status line can
// carry a tool's name with Hermes' short preview of its arguments, or an
// excerpt of the model's reasoning. Each line is collapsed and cut here; full
// tool arguments and tool output are not forwarded.
//
// Hermes interrupts a run when the client reading its stream goes away
// (_drain_session_stream_task_on_disconnect). So the handler reads Hermes'
// stream on a context of its own, bounded by streamTurnCeiling, not by the
// browser's request. When the browser leaves, the handler keeps reading to
// `done` and holds the session's in-flight claim until then, so the run
// finishes and no second turn starts on top of it. Only the ceiling cuts a
// run short, and the error then says the run was interrupted.

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	contentTypeEventStream = "text/event-stream"

	// The events the page reads.
	eventStatus = "status"
	eventStep   = "step"
	eventDelta  = "delta"
	eventReply  = "reply"
	eventError  = "error"

	stepKindTool     = "tool"
	stepKindThinking = "thinking"
	stepStateRunning = "running"
	stepStateDone    = "done"
	stepStateFailed  = "failed"

	// maxStepsPerTurn bounds how many step rows one turn sends to the page.
	maxStepsPerTurn = 50

	// The Hermes events the relay reads; see _handle_session_chat_stream in
	// hermes-agent's gateway/platforms/api_server.py.
	hermesRunStarted         = "run.started"
	hermesToolStarted        = "tool.started"
	hermesToolCompleted      = "tool.completed"
	hermesToolFailed         = "tool.failed"
	hermesToolProgress       = "tool.progress"
	hermesAssistantDelta     = "assistant.delta"
	hermesAssistantCompleted = "assistant.completed"
	hermesEventError         = "error"
	hermesDone               = "done"
	// hermesThinkingTool is the tool name Hermes gives reasoning progress.
	hermesThinkingTool = "_thinking"

	sseEventPrefix = "event:"
	sseDataPrefix  = "data:"
	sseComment     = ":"
	sseKeepalive   = ": keepalive\n\n"

	// statusFragmentRunes bounds a tool preview or a reasoning excerpt
	// inside a status line, and statusLineRunes the whole line.
	statusFragmentRunes = 120
	statusLineRunes     = 160

	// streamTurnCeiling bounds how long the console reads one turn's stream,
	// whether or not the browser is still there. When it fires, the console
	// closes the stream and Hermes interrupts the run.
	streamTurnCeiling = 15 * time.Minute
	// streamWriteGrace is how long past the ceiling the page's response may
	// stay open, so the final error event can still be written.
	streamWriteGrace = 30 * time.Second

	statusSending = "Sending your message to the agent"
	statusStarted = "The agent started working"
	statusWriting = "Writing the reply"
	errNoReply    = "The agent's stream ended without a reply."
)

// hermesStreamEvent is the part of a Hermes stream event's data the relay
// reads.
type hermesStreamEvent struct {
	ToolName string `json:"tool_name"`
	Preview  string `json:"preview"`
	Delta    string `json:"delta"`
	Content  string `json:"content"`
	Message  string `json:"message"`
}

type statusEvent struct {
	Text string `json:"text"`
}

// stepEvent is one row in the turn's collapsible steps block. A tool call
// sends one with state "running" on tool.started and a second with the same ID
// and state "done" or "failed" when the tool finishes, keeping the argument
// preview from tool.started so tool output never reaches the page.
type stepEvent struct {
	ID     string `json:"id"`
	Kind   string `json:"kind"`
	Title  string `json:"title"`
	Detail string `json:"detail,omitempty"`
	State  string `json:"state"`
}

type deltaEvent struct {
	Text string `json:"text"`
}

// sseWriter writes events to the page and flushes each one. Once the browser
// has gone (its request context ended, or a write failed) it drops every
// later event, so the relay can keep reading Hermes' stream to its end.
type sseWriter struct {
	w    http.ResponseWriter
	rc   *http.ResponseController
	page context.Context
	gone bool
}

func (e *sseWriter) write(frame string) {
	if e.gone || e.page.Err() != nil {
		e.gone = true
		return
	}
	if _, err := io.WriteString(e.w, frame); err != nil {
		e.gone = true
		return
	}
	if e.rc.Flush() != nil {
		e.gone = true
	}
}

func (e *sseWriter) send(name string, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		return
	}
	e.write(fmt.Sprintf("event: %s\ndata: %s\n\n", name, b))
}

func (e *sseWriter) keepalive() { e.write(sseKeepalive) }

func (s *server) handleChatStream(w http.ResponseWriter, r *http.Request) {
	sid, msg, agentSession, ok := s.beginTurn(w, r)
	if !ok {
		return
	}
	// Released when Hermes' stream ends, which may be after the browser left.
	defer s.release(sid)

	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), s.streamCeiling)
	defer cancel()

	w.Header().Set("Content-Type", contentTypeEventStream)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	out := &sseWriter{w: w, rc: http.NewResponseController(w), page: r.Context()}
	// The server's WriteTimeout fits /api/chat's turnTimeout. A stream may
	// run to the ceiling, so this response gets a later write deadline.
	_ = out.rc.SetWriteDeadline(time.Now().Add(s.streamCeiling + streamWriteGrace))
	out.send(eventStatus, statusEvent{Text: statusSending})

	resp, err := s.openTurnStream(ctx, sid, msg)
	if errors.Is(err, errSessionNotFound) && !agentSession {
		// Same recovery as /api/chat: the agent pod lost the session. A
		// session the cluster opened is never recreated here.
		if err = s.createSession(ctx, sid); err == nil {
			resp, err = s.openTurnStream(ctx, sid, msg)
		}
	}
	if err != nil {
		out.send(eventError, s.streamFailure(err, sid))
		return
	}
	defer drainAndClose(resp)

	reply, err := relayTurnStream(ctx, resp.Body, out)
	if out.gone {
		log.Printf(logPrefix+"session %s: the browser left during the turn; read the agent's stream to its end (err=%v)", sid, err)
		return
	}
	if err != nil {
		out.send(eventError, s.streamFailure(err, sid))
		return
	}
	out.send(eventReply, chatResponse{SessionID: sid, Reply: reply})
}

// streamFailure is turnFailure's body, except when the turn ceiling fired:
// the console then closed Hermes' stream, which interrupts the run.
func (s *server) streamFailure(err error, sid string) errorResponse {
	if errors.Is(err, context.DeadlineExceeded) {
		return errorResponse{Error: "turn_interrupted", SessionID: sid,
			Detail: fmt.Sprintf("The agent did not finish within %s, so the console stopped reading the turn and the agent interrupted it. Ask again, or ask for a smaller piece of the work.", s.streamCeiling)}
	}
	_, body := turnFailure(err, sid)
	return body
}

// openTurnStream starts a streamed turn. An error status arrives before the
// stream starts, as JSON, and maps to the same errors runTurn returns.
func (s *server) openTurnStream(ctx context.Context, sid, msg string) (*http.Response, error) {
	resp, err := s.hermes(ctx, http.MethodPost, "/api/sessions/"+url.PathEscape(sid)+"/chat/stream", map[string]string{"message": msg})
	if err != nil {
		return nil, err
	}
	if resp.StatusCode == http.StatusOK {
		return resp, nil
	}
	defer drainAndClose(resp)
	if resp.StatusCode == http.StatusNotFound {
		return nil, errSessionNotFound
	}
	return nil, &upstreamError{status: resp.StatusCode, detail: upstreamErrorDetail(resp)}
}

// relayTurnStream reads Hermes' SSE stream until done or EOF, sends a status
// line for each event worth showing, and returns the reply from
// assistant.completed. An error event with no reply becomes the error. It
// keeps reading after the browser has gone; out drops what it cannot send.
//
// Hermes reports reasoning as `_thinking` progress. Some models stream the
// reply itself that way once they start writing it, so after the first
// assistant.delta no reasoning line is shown, and none whose text the reply
// so far already starts with.
func relayTurnStream(ctx context.Context, body io.Reader, out *sseWriter) (string, error) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, bufio.MaxScanTokenSize), maxUpstreamBodyBytes)
	var (
		name, lastStatus, reply, upstreamMsg string
		data                                 []string
		written                              strings.Builder
		haveReply, done, writing             bool
		toolCounts                           = map[string]int{}
		openTools                            = map[string][]stepEvent{}
		thinkCount, stepsOpened              int
	)
	dispatch := func() {
		defer func() { name, data = "", nil }()
		if name == "" && len(data) == 0 {
			return
		}
		var ev hermesStreamEvent
		_ = json.Unmarshal([]byte(strings.Join(data, "\n")), &ev)
		switch name {
		case hermesAssistantCompleted:
			reply, haveReply = ev.Content, true
			return
		case hermesEventError:
			upstreamMsg = ev.Message
			return
		case hermesDone:
			done = true
			return
		case hermesToolStarted:
			if tool := clip(ev.ToolName, statusFragmentRunes); tool != "" && stepsOpened < maxStepsPerTurn {
				stepsOpened++
				toolCounts[ev.ToolName]++
				st := stepEvent{
					ID:     fmt.Sprintf("%s#%d", tool, toolCounts[ev.ToolName]),
					Kind:   stepKindTool,
					Title:  "Running " + tool,
					Detail: clip(ev.Preview, statusFragmentRunes),
					State:  stepStateRunning,
				}
				openTools[ev.ToolName] = append(openTools[ev.ToolName], st)
				out.send(eventStep, st)
			}
		case hermesToolCompleted, hermesToolFailed:
			if queue := openTools[ev.ToolName]; len(queue) > 0 {
				st := queue[0]
				openTools[ev.ToolName] = queue[1:]
				tool := clip(ev.ToolName, statusFragmentRunes)
				if name == hermesToolFailed {
					st.State = stepStateFailed
					st.Title = tool + " failed"
				} else {
					st.State = stepStateDone
					st.Title = "Finished " + tool
				}
				out.send(eventStep, st)
			}
		case hermesAssistantDelta:
			writing = true
			written.WriteString(ev.Delta)
			if ev.Delta != "" {
				out.send(eventDelta, deltaEvent{Text: ev.Delta})
			}
		case hermesToolProgress:
			if ev.ToolName == hermesThinkingTool && (writing || echoesReply(ev.Delta, written.String())) {
				return
			}
			if ev.ToolName == hermesThinkingTool {
				if thought := clip(ev.Delta, statusFragmentRunes); thought != "" && stepsOpened < maxStepsPerTurn {
					stepsOpened++
					thinkCount++
					out.send(eventStep, stepEvent{
						ID:     fmt.Sprintf("think#%d", thinkCount),
						Kind:   stepKindThinking,
						Title:  "Thinking",
						Detail: thought,
						State:  stepStateDone,
					})
				}
			}
		}
		text := statusLine(name, ev)
		if text == "" || text == lastStatus {
			return
		}
		lastStatus = text
		out.send(eventStatus, statusEvent{Text: text})
	}
	for !done && scanner.Scan() {
		line := scanner.Text()
		switch {
		case line == "":
			dispatch()
		case strings.HasPrefix(line, sseComment):
			out.keepalive()
		case strings.HasPrefix(line, sseEventPrefix):
			name = strings.TrimSpace(strings.TrimPrefix(line, sseEventPrefix))
		case strings.HasPrefix(line, sseDataPrefix):
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, sseDataPrefix), " "))
		}
	}
	if !done {
		dispatch()
	}
	if err := scanner.Err(); err != nil && !haveReply {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return "", fmt.Errorf("agent stream broke off: %w", err)
	}
	if haveReply {
		return reply, nil
	}
	if ctx.Err() != nil {
		return "", ctx.Err()
	}
	if upstreamMsg != "" {
		return "", errors.New(upstreamMsg)
	}
	return "", errors.New(errNoReply)
}

// echoesReply reports whether a reasoning excerpt is the start of the reply
// written so far, compared with whitespace collapsed.
func echoesReply(thought, written string) bool {
	t := strings.Join(strings.Fields(thought), " ")
	w := strings.Join(strings.Fields(written), " ")
	return t != "" && w != "" && strings.HasPrefix(w, t)
}

// statusLine is the one-line status for a Hermes event, or "" for an event
// the page does not show.
func statusLine(name string, ev hermesStreamEvent) string {
	tool := clip(ev.ToolName, statusFragmentRunes)
	var line string
	switch name {
	case hermesRunStarted:
		line = statusStarted
	case hermesToolStarted:
		line = "Running " + tool
		if preview := clip(ev.Preview, statusFragmentRunes); preview != "" {
			line += ": " + preview
		}
	case hermesToolCompleted:
		line = "Finished " + tool
	case hermesToolFailed:
		line = tool + " failed"
	case hermesToolProgress:
		if ev.ToolName != hermesThinkingTool {
			return ""
		}
		thought := clip(ev.Delta, statusFragmentRunes)
		if thought == "" {
			return ""
		}
		line = "Thinking: " + thought
	case hermesAssistantDelta:
		line = statusWriting
	default:
		return ""
	}
	return clip(line, statusLineRunes)
}

// clip collapses whitespace runs to single spaces and cuts text to max
// runes, marking a cut with an ellipsis.
func clip(text string, max int) string {
	text = strings.Join(strings.Fields(text), " ")
	if utf8.RuneCountInString(text) <= max {
		return text
	}
	runes := []rune(text)
	return strings.TrimSpace(string(runes[:max])) + ellipsis
}
