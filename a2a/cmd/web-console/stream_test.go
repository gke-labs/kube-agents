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

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// A turn as Hermes streams it: frames in the shapes
// _handle_session_chat_stream writes, with a keepalive comment between.
const hermesTurnStream = `event: run.started
data: {"user_message": {"role": "user", "content": "check pods"}, "seq": 1}

event: tool.progress
data: {"tool_name": "_thinking", "delta": "The user wants pod health.\n\n   I should   list pods   first."}

event: tool.started
data: {"tool_name": "kubectl_get", "preview": "pods -n   kubeagents-system", "args": {"token": "do-not-forward"}}

: keepalive

event: tool.completed
data: {"tool_name": "kubectl_get", "preview": "NAME READY STATUS\nweb-1 1/1 Running"}

event: assistant.delta
data: {"delta": "All "}

event: assistant.delta
data: {"delta": "pods "}

event: assistant.completed
data: {"content": "All pods are healthy.", "completed": true}

event: run.completed
data: {"completed": true}

event: done
data: {}

`

type relayed struct {
	name string
	data map[string]any
}

func readRelay(t *testing.T, body string) []relayed {
	t.Helper()
	var out []relayed
	var cur relayed
	scanner := bufio.NewScanner(strings.NewReader(body))
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "event: "):
			cur.name = strings.TrimPrefix(line, "event: ")
		case strings.HasPrefix(line, "data: "):
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &cur.data); err != nil {
				t.Fatalf("relay data %q: %v", line, err)
			}
		case line == "" && cur.name != "":
			out = append(out, cur)
			cur = relayed{}
		}
	}
	return out
}

func streamReq(body string) *http.Request {
	req := chatReq(body)
	req.URL.Path = "/api/chat/stream"
	return req
}

func TestChatStreamRelaysStatusAndReply(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	fake.stream = hermesTurnStream
	fake.mu.Unlock()

	rec := serve(h, streamReq(`{"message":"check pods"}`))
	if rec.Code != http.StatusOK || rec.Header().Get("Content-Type") != contentTypeEventStream {
		t.Fatalf("status %d, content type %q: %s", rec.Code, rec.Header().Get("Content-Type"), rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "do-not-forward") || strings.Contains(rec.Body.String(), "web-1") {
		t.Errorf("tool args or a completed preview reached the page: %s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), ": keepalive") {
		t.Errorf("the keepalive was not relayed")
	}
	events := readRelay(t, rec.Body.String())
	var statuses []string
	var steps []string
	var deltas []string
	for _, e := range events[:len(events)-1] {
		switch e.name {
		case eventStatus:
			statuses = append(statuses, e.data["text"].(string))
		case eventStep:
			detail, _ := e.data["detail"].(string)
			steps = append(steps, e.data["id"].(string)+":"+e.data["kind"].(string)+":"+e.data["state"].(string)+":"+e.data["title"].(string)+":"+detail)
		case eventDelta:
			deltas = append(deltas, e.data["text"].(string))
		default:
			t.Fatalf("unexpected event %q before the reply", e.name)
		}
	}
	want := []string{
		statusSending,
		statusStarted,
		"Thinking: The user wants pod health. I should list pods first.",
		"Running kubectl_get: pods -n kubeagents-system",
		"Finished kubectl_get",
		statusWriting,
	}
	if strings.Join(statuses, "|") != strings.Join(want, "|") {
		t.Errorf("status lines =\n%q\nwant\n%q", statuses, want)
	}
	wantSteps := []string{
		"think#1:thinking:done:Thinking:The user wants pod health. I should list pods first.",
		"kubectl_get#1:tool:running:Running kubectl_get:pods -n kubeagents-system",
		"kubectl_get#1:tool:done:Finished kubectl_get:pods -n kubeagents-system",
	}
	if strings.Join(steps, "|") != strings.Join(wantSteps, "|") {
		t.Errorf("steps =\n%q\nwant\n%q", steps, wantSteps)
	}
	if strings.Join(deltas, "") != "All pods " {
		t.Errorf("deltas = %q, want %q", deltas, []string{"All ", "pods "})
	}
	last := events[len(events)-1]
	if last.name != eventReply || last.data["reply"] != "All pods are healthy." || !sessionIDPattern.MatchString(last.data["session_id"].(string)) {
		t.Errorf("final event = %+v, want the reply and the session", last)
	}
	sid := last.data["session_id"].(string)
	if !newServerClaimFree(t, h, sid) {
		t.Errorf("the session's in-flight claim was not released")
	}
}

// newServerClaimFree checks the claim is gone by running a second turn on the
// same session, which a held claim refuses with 409.
func newServerClaimFree(t *testing.T, h http.Handler, sid string) bool {
	t.Helper()
	return serve(h, chatReq(`{"message":"again","session_id":"`+sid+`"}`)).Code == http.StatusOK
}

func TestChatStreamRelaysAnUpstreamError(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	fake.stream = "event: run.started\ndata: {}\n\nevent: error\ndata: {\"message\": \"model quota exhausted\"}\n\nevent: done\ndata: {}\n\n"
	fake.mu.Unlock()
	events := readRelay(t, serve(h, streamReq(`{"message":"hi"}`)).Body.String())
	last := events[len(events)-1]
	if last.name != eventError || last.data["error"] != "agent_error" ||
		!strings.Contains(last.data["detail"].(string), "model quota exhausted") || last.data["session_id"] == "" {
		t.Errorf("final event = %+v, want an agent_error carrying the message and the session", last)
	}

	fake.mu.Lock()
	fake.streamCode = http.StatusTooManyRequests
	fake.mu.Unlock()
	events = readRelay(t, serve(h, streamReq(`{"message":"hi"}`)).Body.String())
	if last := events[len(events)-1]; last.name != eventError || last.data["error"] != "agent_busy" {
		t.Errorf("rate-limited stream: final event = %+v, want agent_busy", last)
	}
}

func TestChatStreamWithNoReplyIsAnError(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	fake.stream = "event: run.started\ndata: {}\n\n"
	fake.mu.Unlock()
	events := readRelay(t, serve(h, streamReq(`{"message":"hi"}`)).Body.String())
	if last := events[len(events)-1]; last.name != eventError || !strings.Contains(last.data["detail"].(string), errNoReply) {
		t.Errorf("final event = %+v", last)
	}
}

func TestChatStreamRecreatesAMissingSession(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	fake.stream = hermesTurnStream
	fake.mu.Unlock()
	sid := sessionIDPrefix + strings.Repeat("d", 32)
	events := readRelay(t, serve(h, streamReq(`{"message":"hi","session_id":"`+sid+`"}`)).Body.String())
	if last := events[len(events)-1]; last.name != eventReply || last.data["session_id"] != sid {
		t.Errorf("final event = %+v", last)
	}
	fake.mu.Lock()
	streams := fake.streams
	fake.mu.Unlock()
	if sessions, _ := fake.state(); !sessions[sid] || streams != 2 {
		t.Errorf("want the session recreated and the stream retried once; streams=%d", streams)
	}
}

func TestChatStreamRepliesIntoAnAgentSession(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-0000000a", "api_server", "Triage k8s-evt-0000000a")
	fake.seed("slack-1", "slack", "Triage k8s-evt-0000000a")
	fake.mu.Lock()
	fake.stream = hermesTurnStream
	fake.mu.Unlock()
	events := readRelay(t, serve(h, streamReq(`{"message":"hi","session_id":"k8s-evt-0000000a"}`)).Body.String())
	if last := events[len(events)-1]; last.name != eventReply || last.data["session_id"] != "k8s-evt-0000000a" {
		t.Errorf("final event = %+v", last)
	}
	if rec := serve(h, streamReq(`{"message":"hi","session_id":"slack-1"}`)); rec.Code != http.StatusForbidden {
		t.Errorf("chat platform session: status %d, want 403", rec.Code)
	}

	// A 404 from the turn itself ends the stream; the session is not recreated.
	fake.mu.Lock()
	fake.streamCode = http.StatusNotFound
	fake.mu.Unlock()
	events = readRelay(t, serve(h, streamReq(`{"message":"hi","session_id":"k8s-evt-0000000a"}`)).Body.String())
	if last := events[len(events)-1]; last.name != eventError || last.data["error"] != "session_not_found" {
		t.Errorf("final event = %+v, want session_not_found", last)
	}
	if fake.createCount() != 0 {
		t.Errorf("an agent session was created or recreated (%d creates)", fake.createCount())
	}
}

func TestChatStreamKeepsTheChatGuards(t *testing.T) {
	fake, h := setup(t)
	noHeader := streamReq(`{"message":"hi"}`)
	noHeader.Header.Del(consoleHeader)
	if rec := serve(h, noHeader); rec.Code != http.StatusForbidden {
		t.Errorf("missing console header: status %d, want 403", rec.Code)
	}
	if rec := serve(h, streamReq(`{"message":"hi","session_id":"web-console-ABC"}`)); rec.Code != http.StatusBadRequest {
		t.Errorf("malformed console session: status %d, want 400", rec.Code)
	}
	if rec := serve(h, streamReq(`{"message":"hi","session_id":"k8s-evt-00000abc"}`)); rec.Code != http.StatusNotFound {
		t.Errorf("unknown agent session: status %d, want 404", rec.Code)
	}
	big := `{"message":"` + strings.Repeat("a", maxRequestBodyBytes+1) + `"}`
	if rec := serve(h, streamReq(big)); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: status %d, want 413", rec.Code)
	}
	rebind := streamReq(`{"message":"hi"}`)
	rebind.Host = "rebind.attacker.example:8080"
	if rec := serve(h, rebind); rec.Code != http.StatusForbidden {
		t.Errorf("foreign Host: status %d, want 403", rec.Code)
	}
	fake.mu.Lock()
	streams := fake.streams
	fake.mu.Unlock()
	if streams != 0 {
		t.Errorf("a refused request reached Hermes")
	}

	// A turn in flight on a session refuses a streamed turn on it too.
	turn := decode[chatResponse](t, serve(h, chatReq(`{"message":"start"}`)))
	srv := newServer(config{})
	if !srv.claim(turn.SessionID) || srv.claim(turn.SessionID) {
		t.Fatal("claim does not hold")
	}
	rec := httptest.NewRecorder()
	srv.routes().ServeHTTP(rec, streamReq(`{"message":"x","session_id":"`+turn.SessionID+`"}`))
	if rec.Code != http.StatusConflict {
		t.Errorf("streamed turn on a busy session: status %d, want 409", rec.Code)
	}
}

func TestClip(t *testing.T) {
	if got := clip("  a \n\t b  ", statusFragmentRunes); got != "a b" {
		t.Errorf("clip = %q", got)
	}
	long := clip(strings.Repeat("x ", statusFragmentRunes), statusFragmentRunes)
	if !strings.HasSuffix(long, ellipsis) || len([]rune(long)) > statusFragmentRunes+1 {
		t.Errorf("clip long = %q", long)
	}
}

// waitFor polls cond until it holds or the deadline passes.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

const gatedTail = "event: assistant.completed\ndata: {\"content\": \"done\"}\n\nevent: done\ndata: {}\n\n"

func TestChatStreamKeepsReadingAfterTheBrowserLeaves(t *testing.T) {
	fake, s := setupServer(t)
	h := s.routes()
	console := httptest.NewServer(h)
	t.Cleanup(console.Close)
	sid := sessionIDPrefix + strings.Repeat("e", 32)
	fake.seed(sid, "api_server", "Web console")
	gate := make(chan struct{})
	fake.mu.Lock()
	fake.stream = "event: run.started\ndata: {}\n\n"
	fake.streamGate, fake.streamTail = gate, gatedTail
	fake.mu.Unlock()

	ctx, leave := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, console.URL+"/api/chat/stream",
		strings.NewReader(`{"message":"hi","session_id":"`+sid+`"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(consoleHeader, consoleHeaderValue)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	lines := bufio.NewScanner(resp.Body)
	for lines.Scan() && !strings.Contains(lines.Text(), statusStarted) {
	}
	// The browser goes away mid-turn.
	leave()
	resp.Body.Close()
	time.Sleep(100 * time.Millisecond)

	if code := serve(h, chatReq(`{"message":"again","session_id":"`+sid+`"}`)).Code; code != http.StatusConflict {
		t.Errorf("a turn while the abandoned one still runs: status %d, want 409", code)
	}
	fake.mu.Lock()
	end := fake.streamEnd
	fake.mu.Unlock()
	if end != "" {
		t.Fatalf("the agent's stream ended (%s) before the run finished", end)
	}

	close(gate)
	waitFor(t, "the claim to be released", func() bool {
		return serve(h, chatReq(`{"message":"again","session_id":"`+sid+`"}`)).Code == http.StatusOK
	})
	fake.mu.Lock()
	end = fake.streamEnd
	fake.mu.Unlock()
	if end != "finished" {
		t.Errorf("the agent's stream ended %q, want it read to the end", end)
	}
}

// The server's WriteTimeout is sized for /api/chat. A streamed turn that
// runs longer must still reach the page.
func TestChatStreamOutlastsTheServerWriteTimeout(t *testing.T) {
	fake, s := setupServer(t)
	console := httptest.NewUnstartedServer(s.routes())
	console.Config.WriteTimeout = 200 * time.Millisecond
	console.Start()
	t.Cleanup(console.Close)
	sid := sessionIDPrefix + strings.Repeat("d", 32)
	fake.seed(sid, "api_server", "Web console")
	gate := make(chan struct{})
	fake.mu.Lock()
	fake.stream = "event: run.started\ndata: {}\n\n"
	fake.streamGate, fake.streamTail = gate, gatedTail
	fake.mu.Unlock()
	go func() {
		time.Sleep(600 * time.Millisecond)
		close(gate)
	}()

	req, _ := http.NewRequest(http.MethodPost, console.URL+"/api/chat/stream",
		strings.NewReader(`{"message":"hi","session_id":"`+sid+`"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(consoleHeader, consoleHeaderValue)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	events := readRelay(t, string(body))
	if last := events[len(events)-1]; last.name != eventReply {
		t.Errorf("final event = %+v, want the reply", last)
	}
}

func TestChatStreamCeilingInterruptsTheRun(t *testing.T) {
	fake, s := setupServer(t)
	s.streamCeiling = 300 * time.Millisecond
	sid := sessionIDPrefix + strings.Repeat("f", 32)
	fake.seed(sid, "api_server", "Web console")
	gate := make(chan struct{})
	t.Cleanup(func() { close(gate) })
	fake.mu.Lock()
	fake.stream = "event: run.started\ndata: {}\n\n"
	fake.streamGate, fake.streamTail = gate, gatedTail
	fake.mu.Unlock()

	events := readRelay(t, serve(s.routes(), streamReq(`{"message":"hi","session_id":"`+sid+`"}`)).Body.String())
	last := events[len(events)-1]
	detail, _ := last.data["detail"].(string)
	if last.name != eventError || last.data["error"] != "turn_interrupted" || !strings.Contains(detail, "interrupted") {
		t.Errorf("final event = %+v, want turn_interrupted", last)
	}
	if strings.Contains(detail, "may still be working") {
		t.Errorf("the error says the run may still be working: %q", detail)
	}
	waitFor(t, "the agent to see the stream closed", func() bool {
		fake.mu.Lock()
		defer fake.mu.Unlock()
		return fake.streamEnd == "abandoned"
	})
}

func TestChatStreamDropsReasoningThatEchoesTheReply(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	// The order seen on a cluster: the reply starts streaming, then the same
	// text arrives again as _thinking progress, then the reply.
	fake.stream = "event: run.started\ndata: {}\n\n" +
		"event: tool.progress\ndata: {\"tool_name\": \"_thinking\", \"delta\": \"Check the pods.\"}\n\n" +
		"event: assistant.delta\ndata: {\"delta\": \"There are eight pods \"}\n\n" +
		"event: tool.progress\ndata: {\"tool_name\": \"_thinking\", \"delta\": \"There are eight pods running\"}\n\n" +
		"event: assistant.completed\ndata: {\"content\": \"There are eight pods running.\"}\n\n" +
		"event: done\ndata: {}\n\n"
	fake.mu.Unlock()
	events := readRelay(t, serve(h, streamReq(`{"message":"hi"}`)).Body.String())
	var statuses []string
	for _, e := range events {
		if e.name == eventStatus {
			statuses = append(statuses, e.data["text"].(string))
		}
	}
	want := []string{statusSending, statusStarted, "Thinking: Check the pods.", statusWriting}
	if strings.Join(statuses, "|") != strings.Join(want, "|") {
		t.Errorf("status lines = %q, want %q", statuses, want)
	}
}

func TestEchoesReply(t *testing.T) {
	for _, tc := range []struct {
		thought, written string
		want             bool
	}{
		{"There are  eight", "There are eight pods", true},
		{"Check the pods", "There are eight pods", false},
		{"anything", "", false},
		{"", "text", false},
	} {
		if got := echoesReply(tc.thought, tc.written); got != tc.want {
			t.Errorf("echoesReply(%q, %q) = %v, want %v", tc.thought, tc.written, got, tc.want)
		}
	}
}

func TestChatStreamStepPairingAndCap(t *testing.T) {
	fake, h := setup(t)
	var b strings.Builder
	b.WriteString("event: run.started\ndata: {}\n\n")
	// Two concurrent calls to the same tool finish in FIFO order; the second fails.
	b.WriteString("event: tool.started\ndata: {\"tool_name\": \"kubectl_get\", \"preview\": \"pods\"}\n\n")
	b.WriteString("event: tool.started\ndata: {\"tool_name\": \"kubectl_get\", \"preview\": \"nodes\"}\n\n")
	b.WriteString("event: tool.completed\ndata: {\"tool_name\": \"kubectl_get\", \"preview\": \"secret-output\"}\n\n")
	b.WriteString("event: tool.failed\ndata: {\"tool_name\": \"kubectl_get\", \"error\": \"forbidden\"}\n\n")
	// Push past maxStepsPerTurn; updates to already-started steps still go through.
	for i := 0; i < maxStepsPerTurn+5; i++ {
		b.WriteString("event: tool.progress\ndata: {\"tool_name\": \"_thinking\", \"delta\": \"step\"}\n\n")
	}
	b.WriteString("event: assistant.completed\ndata: {\"content\": \"done\"}\n\nevent: done\ndata: {}\n\n")
	fake.mu.Lock()
	fake.stream = b.String()
	fake.mu.Unlock()

	rec := serve(h, streamReq(`{"message":"check"}`))
	if strings.Contains(rec.Body.String(), "secret-output") {
		t.Fatalf("completed tool preview leaked: %s", rec.Body.String())
	}
	events := readRelay(t, rec.Body.String())
	seenIDs := map[string]bool{}
	var updates []string
	for _, e := range events {
		if e.name != eventStep {
			continue
		}
		id := e.data["id"].(string)
		seenIDs[id] = true
		if strings.HasPrefix(id, "kubectl_get#") {
			detail, _ := e.data["detail"].(string)
			updates = append(updates, id+":"+e.data["state"].(string)+":"+detail)
		}
	}
	wantUpdates := []string{
		"kubectl_get#1:running:pods",
		"kubectl_get#2:running:nodes",
		"kubectl_get#1:done:pods",
		"kubectl_get#2:failed:nodes",
	}
	if strings.Join(updates, "|") != strings.Join(wantUpdates, "|") {
		t.Errorf("tool step updates = %q, want %q", updates, wantUpdates)
	}
	if len(seenIDs) != maxStepsPerTurn {
		t.Errorf("distinct step IDs = %d, want cap %d", len(seenIDs), maxStepsPerTurn)
	}
}
