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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const testKey = "test-api-key"

// fakeHermes models the parts of the Hermes gateway API the console calls, in
// the shapes hermes-agent v2026.9.14 (tags.env) returns them from
// gateway/platforms/api_server.py: POST /api/sessions answers 201 with
// {"object":"hermes.session","session":{...}} or 409 when the ID exists;
// POST /api/sessions/{id}/chat takes {"message": ...}, answers 404 for an
// unknown session and otherwise
// {"object":"hermes.session.chat.completion","message":{"role":"assistant","content":...}};
// GET /api/sessions/{id}/messages answers 404 for an unknown session and
// otherwise {"object":"list","data":[{"id":<int>,"role":...,"content":...}]}
// in insertion order; errors are OpenAI-shaped {"error":{"message":...}}.
// Every route requires the bearer key, as the agent's auth sidecar does.
type fakeHermes struct {
	mu        sync.Mutex
	sessions  map[string]bool
	messages  map[string][]map[string]any
	nextID    int64
	lastQuery string
	// lastListQuery is the query of the latest GET /api/sessions.
	lastListQuery string
	chats         []map[string]any
	reply         string
	chatDelay     time.Duration
	chatCode      int
	// seeded holds sessions the console did not open, by ID, with their
	// source and title, as the event watcher or a chat platform creates them.
	seeded map[string]map[string]any
	// messageGets counts GET .../messages calls per session.
	messageGets map[string]int
	// stream is the raw SSE body POST .../chat/stream answers with.
	stream string
	// streamCode, when set, is the status the stream route answers with
	// instead of a stream.
	streamCode int
	streams    int
	// creates counts POST /api/sessions calls.
	creates int
	// streamGate, when set, holds the stream after `stream` is written;
	// streamTail is written once it closes. streamEnd records how the
	// stream ended: "finished" when the tail was written to a reader still
	// there, "abandoned" when the reader left first.
	streamGate   chan struct{}
	streamTail   string
	streamEnd    string
	postTurnWake string
	chatEntered  chan struct{}
	chatGate     chan struct{}
	// createDelay holds POST /api/sessions; dropSessionOnFirst forgets the
	// session on the next chat, as a replaced agent pod would.
	createDelay        time.Duration
	dropSessionOnFirst bool
	// toolName and toolContent, when set, replace the default tool row
	// written during POST .../chat. onChat, when set, is called after the
	// initial turn rows are recorded.
	toolName    string
	toolContent string
	onChat      func(sid string)
}

func newFakeHermes() *fakeHermes {
	return &fakeHermes{
		sessions: map[string]bool{}, messages: map[string][]map[string]any{}, reply: "pods are healthy",
		seeded: map[string]map[string]any{}, messageGets: map[string]int{},
	}
}

// seed adds a session another caller opened.
func (f *fakeHermes) seed(id, source, title string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sessions[id] = true
	f.seeded[id] = map[string]any{"id": id, "source": source, "title": title}
}

// post appends a message to a session, as a turn or a card's wake does.
// Callers hold f.mu. A nil content models a tool-call-only assistant row.
func (f *fakeHermes) post(sid, role string, content any) int64 {
	return f.postTool(sid, role, "", content)
}

func (f *fakeHermes) postTool(sid, role, toolName string, content any) int64 {
	f.nextID++
	row := map[string]any{"id": f.nextID, "session_id": sid, "role": role, "content": content}
	if toolName != "" {
		row["tool_name"] = toolName
	}
	f.messages[sid] = append(f.messages[sid], row)
	return f.nextID
}

// deliver models a delegated card's result landing on a session after the
// turn that filed it has returned.
func (f *fakeHermes) deliver(sid, content string) int64 {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.post(sid, "user", "[kanban] card completed")
	return f.post(sid, "assistant", content)
}

func (f *fakeHermes) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != "Bearer "+testKey {
		hermesError(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/health":
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	case r.Method == http.MethodGet && r.URL.Path == "/api/sessions":
		f.mu.Lock()
		f.lastListQuery = r.URL.RawQuery
		data := []map[string]any{{"id": "k8s-evt-00000abc", "title": "Triage k8s-evt-00000abc", "source": "api_server", "preview": "secret text"}}
		for id := range f.sessions {
			row := map[string]any{"id": id, "title": "Web console", "source": "api_server", "message_count": len(f.messages[id])}
			for k, v := range f.seeded[id] {
				row[k] = v
			}
			// Hermes filters on source when asked, as list_sessions_rich does.
			if want := r.URL.Query().Get("source"); want != "" && row["source"] != want {
				continue
			}
			data = append(data, row)
		}
		f.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
	case r.Method == http.MethodPost && r.URL.Path == "/api/sessions":
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		createDelay := f.createDelay
		f.mu.Unlock()
		if createDelay > 0 {
			time.Sleep(createDelay)
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		f.creates++
		if f.sessions[body["session_id"]] {
			hermesError(w, http.StatusConflict, "Session already exists")
			return
		}
		f.sessions[body["session_id"]] = true
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "hermes.session", "session": map[string]any{"id": body["session_id"]}})
	case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/api/sessions/") && strings.HasSuffix(r.URL.Path, "/chat/stream"):
		sid := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/sessions/"), "/chat/stream")
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		known := f.sessions[sid]
		f.streams++
		stream, code, delay := f.stream, f.streamCode, f.chatDelay
		f.mu.Unlock()
		if !known {
			hermesError(w, http.StatusNotFound, "Session not found: "+sid)
			return
		}
		if code != 0 {
			hermesError(w, code, "upstream refused")
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		time.Sleep(delay)
		f.mu.Lock()
		if f.onChat != nil {
			f.onChat(sid)
		}
		f.mu.Unlock()
		_, _ = w.Write([]byte(stream))
		f.mu.Lock()
		gate, tail := f.streamGate, f.streamTail
		f.mu.Unlock()
		if gate == nil {
			return
		}
		w.(http.Flusher).Flush()
		end := "abandoned"
		select {
		case <-gate:
			if _, err := w.Write([]byte(tail)); err == nil && r.Context().Err() == nil {
				w.(http.Flusher).Flush()
				end = "finished"
			}
		case <-r.Context().Done():
		}
		f.mu.Lock()
		f.streamEnd = end
		f.mu.Unlock()
	case r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/api/sessions/") && strings.HasSuffix(r.URL.Path, "/chat"):
		sid := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/sessions/"), "/chat")
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		if f.dropSessionOnFirst {
			f.dropSessionOnFirst = false
			delete(f.sessions, sid)
		}
		known := f.sessions[sid]
		f.chats = append(f.chats, body)
		delay, code, reply := f.chatDelay, f.chatCode, f.reply
		entered, gate := f.chatEntered, f.chatGate
		f.chatEntered, f.chatGate = nil, nil
		f.mu.Unlock()
		if entered != nil {
			close(entered)
		}
		if gate != nil {
			<-gate
		}
		if !known {
			hermesError(w, http.StatusNotFound, "Session not found")
			return
		}
		if _, ok := body["message"].(string); !ok {
			hermesError(w, http.StatusBadRequest, "Missing 'message' field")
			return
		}
		time.Sleep(delay)
		if code != 0 {
			hermesError(w, code, "upstream refused")
			return
		}
		f.mu.Lock()
		f.post(sid, "user", body["message"])
		f.post(sid, "assistant", nil)
		toolName, toolContent, onChat := f.toolName, f.toolContent, f.onChat
		if toolContent == "" {
			toolContent = "pod list"
		}
		f.postTool(sid, "tool", toolName, toolContent)
		f.post(sid, "assistant", reply)
		if f.postTurnWake != "" {
			f.post(sid, "user", "[kanban] card completed")
			f.post(sid, "assistant", f.postTurnWake)
		}
		f.mu.Unlock()
		if onChat != nil {
			onChat(sid)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"object":     "hermes.session.chat.completion",
			"session_id": sid,
			"message":    map[string]string{"role": "assistant", "content": reply},
		})
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/sessions/") && strings.HasSuffix(r.URL.Path, "/messages"):
		sid := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/sessions/"), "/messages")
		f.mu.Lock()
		defer f.mu.Unlock()
		f.lastQuery = r.URL.RawQuery
		f.messageGets[sid]++
		if !f.sessions[sid] {
			hermesError(w, http.StatusNotFound, "Session not found: "+sid)
			return
		}
		// Same paging as Hermes. With order=latest, offset counts back from
		// the newest message; with order=oldest, forward from the first.
		// Either way the page comes back oldest first.
		all := f.messages[sid]
		limit, offset := len(all), 0
		if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil {
			limit = v
		}
		if v, err := strconv.Atoi(r.URL.Query().Get("offset")); err == nil {
			offset = v
		}
		var page []map[string]any
		if r.URL.Query().Get("order") == "oldest" {
			start := min(offset, len(all))
			page = all[start:min(start+limit, len(all))]
		} else {
			end := max(len(all)-offset, 0)
			page = all[max(end-limit, 0):end]
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "session_id": sid, "data": page})
	case r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/api/sessions/") && !strings.Contains(strings.TrimPrefix(r.URL.Path, "/api/sessions/"), "/"):
		sid := strings.TrimPrefix(r.URL.Path, "/api/sessions/")
		f.mu.Lock()
		defer f.mu.Unlock()
		if !f.sessions[sid] {
			hermesError(w, http.StatusNotFound, "Session not found: "+sid)
			return
		}
		session := map[string]any{"id": sid, "source": "api_server", "title": "Web console"}
		for k, v := range f.seeded[sid] {
			session[k] = v
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "hermes.session", "session": session})
	default:
		hermesError(w, http.StatusNotFound, "no route")
	}
}

// createCount returns how many sessions callers asked the fake to create.
func (f *fakeHermes) createCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.creates
}

// gets returns how many message reads a session has had.
func (f *fakeHermes) gets(sid string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.messageGets[sid]
}

// state returns copies of what the fake has recorded, under its lock.
func (f *fakeHermes) state() (map[string]bool, []map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	sessions := map[string]bool{}
	for k, v := range f.sessions {
		sessions[k] = v
	}
	return sessions, append([]map[string]any(nil), f.chats...)
}

func hermesError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"message": msg}})
}

func setup(t *testing.T) (*fakeHermes, http.Handler) {
	t.Helper()
	fake, s := setupServer(t)
	return fake, s.routes()
}

// setupServer is setup for a test that changes a server field first.
func setupServer(t *testing.T) (*fakeHermes, *server) {
	t.Helper()
	fake := newFakeHermes()
	upstream := httptest.NewServer(fake)
	t.Cleanup(upstream.Close)
	cfg := config{
		HermesURL:    upstream.URL,
		APIServerKey: testKey,
		ClusterName:  "c1",
	}
	return fake, newServer(cfg)
}

// defaultTestSessionID is the page-minted ID chatReq adds to a body that
// names no session, since the console no longer mints one.
const defaultTestSessionID = "web-console-0123456789abcdef0123456789abcdef"

func chatReq(body string) *http.Request {
	var parsed map[string]any
	if err := json.Unmarshal([]byte(body), &parsed); err == nil {
		if _, has := parsed["session_id"]; !has {
			parsed["session_id"] = defaultTestSessionID
			if b, err := json.Marshal(parsed); err == nil {
				body = string(b)
			}
		}
	}
	req := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/chat", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(consoleHeader, consoleHeaderValue)
	return req
}

func serve(h http.Handler, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decode[T any](t *testing.T, rec *httptest.ResponseRecorder) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
		t.Fatalf("decode %q: %v", rec.Body.String(), err)
	}
	return v
}

func TestChatUsesHermesContract(t *testing.T) {
	fake, h := setup(t)
	rec := serve(h, chatReq(`{"message":"check pods"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	got := decode[chatResponse](t, rec)
	if got.Reply != "pods are healthy" {
		t.Errorf("reply = %q, want the assistant message content", got.Reply)
	}
	if !sessionIDPattern.MatchString(got.SessionID) {
		t.Errorf("session id %q does not carry the console prefix", got.SessionID)
	}
	if got.BeforeID != 0 || got.ReplyID != 4 {
		t.Errorf("turn IDs = (before=%d, reply=%d), want (0, 4)", got.BeforeID, got.ReplyID)
	}
	second := decode[chatResponse](t, serve(h, chatReq(`{"message":"again","session_id":"`+got.SessionID+`"}`)))
	if second.BeforeID != 4 || second.ReplyID != 8 {
		t.Errorf("second turn IDs = (before=%d, reply=%d), want (4, 8)", second.BeforeID, second.ReplyID)
	}
	// When a delegated wake lands between the turn's assistant row (12) and
	// the post-turn replyMessageID read (rows 13 and 14), ReplyID still names
	// the turn's own assistant row (12) so the page shows row 14 as an update.
	fake.mu.Lock()
	fake.postTurnWake = "delegated card finished right after turn"
	fake.mu.Unlock()
	third := decode[chatResponse](t, serve(h, chatReq(`{"message":"third","session_id":"`+got.SessionID+`"}`)))
	if third.BeforeID != 8 || third.ReplyID != 12 {
		t.Errorf("third turn IDs with post-turn wake = (before=%d, reply=%d), want (8, 12)", third.BeforeID, third.ReplyID)
	}
	_, chats := fake.state()
	if len(chats) != 3 || chats[0]["message"] != "check pods" {
		t.Fatalf("upstream chat body = %v, want {\"message\": \"check pods\"}", chats)
	}
	if _, has := chats[0]["content"]; has {
		t.Errorf("upstream chat body still sends a content field: %v", chats[0])
	}
}

func TestEachNewChatGetsItsOwnSession(t *testing.T) {
	_, h := setup(t)
	sidA := "web-console-" + strings.Repeat("a", 32)
	sidB := "web-console-" + strings.Repeat("b", 32)
	a := decode[chatResponse](t, serve(h, chatReq(`{"message":"one","session_id":"`+sidA+`"}`)))
	b := decode[chatResponse](t, serve(h, chatReq(`{"message":"two","session_id":"`+sidB+`"}`)))
	if a.SessionID != sidA || b.SessionID != sidB || a.SessionID == b.SessionID {
		t.Fatalf("two browser tabs with distinct client-minted IDs were not kept distinct: a=%q b=%q", a.SessionID, b.SessionID)
	}
	c := decode[chatResponse](t, serve(h, chatReq(`{"message":"three","session_id":"`+a.SessionID+`"}`)))
	if c.SessionID != a.SessionID || c.BeforeID != a.ReplyID {
		t.Errorf("a named console session was not reused: got session=%q before=%d, want %q before=%d", c.SessionID, c.BeforeID, a.SessionID, a.ReplyID)
	}
}

func TestChatRefusesForeignSessionIDs(t *testing.T) {
	fake, h := setup(t)
	for _, sid := range []string{"", "web-console-../../x", "web-console-ABC", "../api/sessions"} {
		raw := `{"message":"hi","session_id":` + strconv.Quote(sid) + `}`
		rec := serve(h, chatReq(raw))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("session %q: status %d, want 400", sid, rec.Code)
		}
	}
	if _, chats := fake.state(); len(chats) != 0 {
		t.Errorf("a refused session still reached Hermes: %v", chats)
	}
	if fake.createCount() != 0 {
		t.Errorf("a refused session created %d sessions", fake.createCount())
	}
}

func TestChatRepliesIntoAnAgentSession(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-0000000a", "api_server", "Triage k8s-evt-0000000a")
	fake.seed("cron-daily-report-20261006", "api_server", "Triage cron-daily-report-20261006")
	for _, sid := range []string{"k8s-evt-0000000a", "cron-daily-report-20261006"} {
		rec := serve(h, chatReq(`{"message":"is it fixed?","session_id":"`+sid+`"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status %d: %s", sid, rec.Code, rec.Body.String())
		}
		if got := decode[chatResponse](t, rec); got.SessionID != sid {
			t.Errorf("reply went to %q, want %q", got.SessionID, sid)
		}
	}
	if fake.createCount() != 0 {
		t.Errorf("a reply into an agent session created %d sessions", fake.createCount())
	}
}

func TestChatRefusesAgentSessionRepliesWhenChatPlatformIsEnabled(t *testing.T) {
	fake, s := setupServer(t)
	s.cfg.ChatPlatform = "Slack"
	h := s.routes()
	fake.seed("k8s-evt-0000000a", "api_server", "Triage k8s-evt-0000000a")
	fake.seed("cron-daily-report-20261006", "api_server", "Triage cron-daily-report-20261006")
	fake.mu.Lock()
	fake.post("k8s-evt-0000000a", "user", "triage this")
	fake.mu.Unlock()

	status := decode[statusResponse](t, serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/status", nil)))
	if status.ChatPlatform != "Slack" {
		t.Errorf("status.ChatPlatform = %q, want Slack", status.ChatPlatform)
	}
	tr := decode[transcriptResponse](t, serve(h, transcriptReq("k8s-evt-0000000a")))
	if tr.ChatPlatform != "Slack" {
		t.Errorf("transcript.ChatPlatform = %q, want Slack", tr.ChatPlatform)
	}
	for _, sid := range []string{"k8s-evt-0000000a", "cron-daily-report-20261006"} {
		for _, req := range []*http.Request{
			chatReq(`{"message":"is it fixed?","session_id":"` + sid + `"}`),
			streamReq(`{"message":"is it fixed?","session_id":"` + sid + `"}`),
		} {
			rec := serve(h, req)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("%s on %s: status %d, want 403: %s", req.URL.Path, sid, rec.Code, rec.Body.String())
			}
			got := decode[errorResponse](t, rec)
			if got.Error != "session_not_allowed" || !strings.Contains(got.Detail, "reply in Slack") {
				t.Errorf("%s on %s: error = %+v, want session_not_allowed naming Slack", req.URL.Path, sid, got)
			}
		}
	}
	if _, chats := fake.state(); len(chats) != 0 {
		t.Errorf("a refused agent-session reply still reached Hermes: %v", chats)
	}
	// Console's own threads remain writable when Slack is enabled.
	own := serve(h, chatReq(`{"message":"check pods"}`))
	if own.Code != http.StatusOK {
		t.Errorf("console thread turn with ChatPlatform set: status %d: %s", own.Code, own.Body.String())
	}
}

func TestChatRefusesChatPlatformAndUnknownSessions(t *testing.T) {
	fake, h := setup(t)
	fake.seed("slack-thread-1", "slack", "Triage k8s-evt-0000000b")
	fake.seed("api-other", "api_server", "Something else")
	cases := map[string]int{
		"slack-thread-1":   http.StatusForbidden,
		"api-other":        http.StatusForbidden,
		"k8s-evt-0000dead": http.StatusNotFound,
	}
	for sid, want := range cases {
		rec := serve(h, chatReq(`{"message":"hi","session_id":"`+sid+`"}`))
		if rec.Code != want {
			t.Errorf("%s: status %d, want %d: %s", sid, rec.Code, want, rec.Body.String())
		}
	}
	if _, chats := fake.state(); len(chats) != 0 {
		t.Errorf("a refused session still reached Hermes: %v", chats)
	}
	if fake.createCount() != 0 {
		t.Errorf("a refused session created %d sessions", fake.createCount())
	}
}

func TestChatDoesNotRecreateAnAgentSession(t *testing.T) {
	fake, h := setup(t)
	fake.seed("k8s-evt-0000000a", "api_server", "Triage k8s-evt-0000000a")
	// The session vanishes between the lookup and the turn, as when the
	// agent pod is replaced mid-request: the lookup still sees it.
	fake.mu.Lock()
	fake.chatCode = http.StatusNotFound
	fake.mu.Unlock()
	rec := serve(h, chatReq(`{"message":"hi","session_id":"k8s-evt-0000000a"}`))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status %d, want 404: %s", rec.Code, rec.Body.String())
	}
	if got := decode[errorResponse](t, rec); got.Error != "session_not_found" || got.SessionID != "k8s-evt-0000000a" {
		t.Errorf("error body = %+v", got)
	}
	if fake.createCount() != 0 {
		t.Errorf("an agent session was recreated (%d creates)", fake.createCount())
	}
}

func TestChatRecreatesAMissingSession(t *testing.T) {
	fake, h := setup(t)
	sid := "web-console-" + strings.Repeat("a", 32)
	// First turn on a fresh tab creates the session before runTurn when
	// latestMessageID sees 404, so runTurn succeeds in one call and reports created=true.
	rec := serve(h, chatReq(`{"message":"hi","session_id":"`+sid+`"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if got := decode[chatResponse](t, rec); !got.Created {
		t.Errorf("first turn on fresh session: Created=false, want true")
	}
	if sessions, chats := fake.state(); !sessions[sid] || len(chats) != 1 {
		t.Errorf("want the fresh session created ahead of runTurn; sessions=%v chats=%d", sessions, len(chats))
	}
	// Normal turn on an existing session reports created=false.
	recExisting := serve(h, chatReq(`{"message":"follow up","session_id":"`+sid+`"}`))
	if recExisting.Code != http.StatusOK {
		t.Fatalf("status %d: %s", recExisting.Code, recExisting.Body.String())
	}
	if got := decode[chatResponse](t, recExisting); got.Created {
		t.Errorf("turn on existing session: Created=true, want false")
	}
	// If the agent pod's session store loses the session right before runTurn,
	// the 404 retry recreates the session, retries the turn once, and reports created=true.
	fake.mu.Lock()
	fake.dropSessionOnFirst = true
	fake.mu.Unlock()
	rec2 := serve(h, chatReq(`{"message":"again","session_id":"`+sid+`"}`))
	if rec2.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec2.Code, rec2.Body.String())
	}
	if got := decode[chatResponse](t, rec2); !got.Created {
		t.Errorf("recreated session after 404: Created=false, want true")
	}
	if sessions, chats := fake.state(); !sessions[sid] || len(chats) != 4 {
		t.Errorf("want the lost session recreated and the turn retried once; sessions=%v chats=%d", sessions, len(chats))
	}
}

func TestChatSessionCreateTimeoutReports502Not504(t *testing.T) {
	fake, srv := setupServer(t)
	srv.createTimeout = 40 * time.Millisecond
	h := srv.routes()

	sid := "web-console-" + strings.Repeat("d", 32)
	fake.mu.Lock()
	fake.sessions[sid] = true
	fake.dropSessionOnFirst = true
	fake.createDelay = 120 * time.Millisecond
	fake.mu.Unlock()

	rec := serve(h, chatReq(`{"message":"hi","session_id":"`+sid+`"}`))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("session-create timeout inside 404 retry: status %d, want 502 (not 504): %s", rec.Code, rec.Body.String())
	}
	got := decode[errorResponse](t, rec)
	if got.Error != "session_create_failed" {
		t.Errorf("error code = %q, want session_create_failed", got.Error)
	}
}

func TestChatSurfacesUpstreamErrorsWithoutAFallback(t *testing.T) {
	fake, h := setup(t)
	fake.mu.Lock()
	fake.chatCode = http.StatusInternalServerError
	fake.mu.Unlock()
	rec := serve(h, chatReq(`{"message":"hi"}`))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status %d, want 502", rec.Code)
	}
	got := decode[errorResponse](t, rec)
	if !strings.Contains(got.Detail, "upstream refused") {
		t.Errorf("detail %q does not carry the gateway's message", got.Detail)
	}
	if got.SessionID == "" {
		t.Errorf("error response dropped the session id, so the page would open a new session next turn")
	}

	fake.mu.Lock()
	fake.chatCode = http.StatusTooManyRequests
	fake.mu.Unlock()
	if rec := serve(h, chatReq(`{"message":"hi"}`)); rec.Code != http.StatusServiceUnavailable {
		t.Errorf("a rate-limited gateway answered %d, want 503", rec.Code)
	}
}

func TestChatRefusesAConcurrentTurnOnOneSession(t *testing.T) {
	fake, h := setup(t)
	first := decode[chatResponse](t, serve(h, chatReq(`{"message":"start"}`)))
	entered := make(chan struct{})
	gate := make(chan struct{})
	fake.mu.Lock()
	fake.chatEntered = entered
	fake.chatGate = gate
	fake.mu.Unlock()

	body := `{"message":"x","session_id":"` + first.SessionID + `"}`
	done := make(chan int)
	go func() { done <- serve(h, chatReq(body)).Code }()
	<-entered
	if code := serve(h, chatReq(body)).Code; code != http.StatusConflict {
		t.Errorf("second turn while one is in flight: status %d, want 409", code)
	}
	close(gate)
	if code := <-done; code != http.StatusOK {
		t.Errorf("first turn: status %d", code)
	}
}

func TestChatBoundsTheRequestBody(t *testing.T) {
	_, h := setup(t)
	big := `{"message":"` + strings.Repeat("a", maxRequestBodyBytes+1) + `"}`
	if rec := serve(h, chatReq(big)); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("oversized body: status %d, want 413", rec.Code)
	}
}

func TestChatRequiresTheConsoleRequestShape(t *testing.T) {
	fake, h := setup(t)

	noHeader := chatReq(`{"message":"hi"}`)
	noHeader.Header.Del(consoleHeader)
	if rec := serve(h, noHeader); rec.Code != http.StatusForbidden {
		t.Errorf("missing console header: status %d, want 403", rec.Code)
	}

	form := chatReq(`{"message":"hi"}`)
	form.Header.Set("Content-Type", "text/plain")
	if rec := serve(h, form); rec.Code != http.StatusUnsupportedMediaType {
		t.Errorf("text/plain body: status %d, want 415", rec.Code)
	}

	crossOrigin := chatReq(`{"message":"hi"}`)
	crossOrigin.Header.Set("Origin", "https://evil.example")
	if rec := serve(h, crossOrigin); rec.Code != http.StatusForbidden {
		t.Errorf("cross-origin request: status %d, want 403", rec.Code)
	}

	get := httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/chat", nil)
	if rec := serve(h, get); rec.Code == http.StatusOK {
		t.Errorf("GET /api/chat answered 200")
	}
	if _, chats := fake.state(); len(chats) != 0 {
		t.Errorf("a refused request reached Hermes: %v", chats)
	}
}

func TestNonLoopbackHostIsRefused(t *testing.T) {
	_, h := setup(t)
	for _, path := range []string{
		"/", "/api/status", "/api/sessions/recent", "/api/insights",
		"/api/sessions/" + sessionIDPrefix + strings.Repeat("a", 32) + "/messages",
		"/api/sessions/k8s-evt-00000abc/transcript", "/api/channels/alerts/posts",
	} {
		req := httptest.NewRequest(http.MethodGet, "http://rebind.attacker.example:8080"+path, nil)
		if rec := serve(h, req); rec.Code != http.StatusForbidden {
			t.Errorf("%s via a foreign Host: status %d, want 403", path, rec.Code)
		}
	}
	rebind := chatReq(`{"message":"hi"}`)
	rebind.Host = "rebind.attacker.example:8080"
	if rec := serve(h, rebind); rec.Code != http.StatusForbidden {
		t.Errorf("chat via a foreign Host: status %d, want 403", rec.Code)
	}
	for _, host := range []string{"localhost:8080", "127.0.0.1:8080", "[::1]:8080"} {
		req := httptest.NewRequest(http.MethodGet, "http://"+host+"/api/status", nil)
		if rec := serve(h, req); rec.Code != http.StatusOK {
			t.Errorf("Host %s: status %d, want 200", host, rec.Code)
		}
	}
}

// registeredRoutes reads the method and pattern of every route routes() in
// main.go registers, so the guard tests below cover routes added later
// without anyone updating a list here.
func registeredRoutes(t *testing.T) [][2]string {
	t.Helper()
	src, err := os.ReadFile("main.go")
	if err != nil {
		t.Fatal(err)
	}
	// Any registration counts: a route with another method, or none, fails
	// here rather than being skipped by the guard checks.
	pattern := regexp.MustCompile(`mux\.Handle(?:Func)?\("(?:([A-Z]+) )?([^"]+)"`)
	var routes [][2]string
	for _, m := range pattern.FindAllStringSubmatch(string(src), -1) {
		if m[1] != http.MethodGet && m[1] != http.MethodPost {
			t.Errorf("route %q registers method %q; the guard checks cover GET and POST only", m[2], m[1])
			continue
		}
		routes = append(routes, [2]string{m[1], m[2]})
	}
	if len(routes) < 10 {
		t.Fatalf("found %d routes in main.go; the pattern no longer matches routes()", len(routes))
	}
	return routes
}

// routePath fills a route pattern's {wildcards} with a value each guard
// runs before the handler sees.
func routePath(pattern string) string {
	return regexp.MustCompile(`\{[^}]+\}`).ReplaceAllString(pattern, "x")
}

// Every route but /healthz refuses a foreign Host, and every POST route
// also refuses a request without the console header, a non-JSON body, or a
// foreign Origin. The lists come from main.go, so a route that drops
// requireLocalHost or requireConsoleRequest fails here.
func TestEveryRouteHasItsGuards(t *testing.T) {
	_, h := setup(t)
	for _, r := range registeredRoutes(t) {
		method, path := r[0], routePath(r[1])
		if path == "/healthz" {
			continue
		}
		foreign := httptest.NewRequest(method, "http://rebind.attacker.example:8080"+path, strings.NewReader(`{}`))
		foreign.Header.Set("Content-Type", "application/json")
		foreign.Header.Set(consoleHeader, consoleHeaderValue)
		if rec := serve(h, foreign); rec.Code != http.StatusForbidden {
			t.Errorf("%s %s via a foreign Host: status %d, want 403", method, path, rec.Code)
		}
		if method != http.MethodPost {
			continue
		}
		noHeader := httptest.NewRequest(method, "http://localhost:8080"+path, strings.NewReader(`{}`))
		noHeader.Header.Set("Content-Type", "application/json")
		if rec := serve(h, noHeader); rec.Code != http.StatusForbidden {
			t.Errorf("%s %s without the console header: status %d, want 403", method, path, rec.Code)
		}
		plain := httptest.NewRequest(method, "http://localhost:8080"+path, strings.NewReader(`{}`))
		plain.Header.Set("Content-Type", "text/plain")
		plain.Header.Set(consoleHeader, consoleHeaderValue)
		if rec := serve(h, plain); rec.Code != http.StatusUnsupportedMediaType {
			t.Errorf("%s %s with a text/plain body: status %d, want 415", method, path, rec.Code)
		}
		crossOrigin := httptest.NewRequest(method, "http://localhost:8080"+path, strings.NewReader(`{}`))
		crossOrigin.Header.Set("Content-Type", "application/json")
		crossOrigin.Header.Set(consoleHeader, consoleHeaderValue)
		crossOrigin.Header.Set("Origin", "https://evil.example")
		if rec := serve(h, crossOrigin); rec.Code != http.StatusForbidden {
			t.Errorf("%s %s from a foreign Origin: status %d, want 403", method, path, rec.Code)
		}
	}
}

func TestHealthzAnswersAnyHost(t *testing.T) {
	_, h := setup(t)
	req := httptest.NewRequest(http.MethodGet, "http://10.0.0.5:8080/healthz", nil)
	if rec := serve(h, req); rec.Code != http.StatusOK {
		t.Errorf("kubelet probe by pod IP: status %d, want 200", rec.Code)
	}
}

func TestStatusReportsTheAgentGateway(t *testing.T) {
	_, h := setup(t)
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/status", nil))
	got := decode[statusResponse](t, rec)
	if !got.Agent.Healthy || got.Cluster != "c1" {
		t.Errorf("status = %+v", got)
	}
}

func TestRecentSessionsOmitsPreviewsAndMarksConsoleSessions(t *testing.T) {
	_, h := setup(t)
	mine := decode[chatResponse](t, serve(h, chatReq(`{"message":"hi"}`)))
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/sessions/recent", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), "secret text") {
		t.Errorf("session list leaked a message preview: %s", rec.Body.String())
	}
	got := decode[struct {
		Sessions []recentSession `json:"sessions"`
	}](t, rec)
	marked := map[string]bool{}
	for _, s := range got.Sessions {
		marked[s.ID] = s.Console
	}
	if !marked[mine.SessionID] || marked["k8s-evt-00000abc"] {
		t.Errorf("console flags wrong: %v", marked)
	}
}

func TestRecentSessionsReportsAnUnreachableGateway(t *testing.T) {
	cfg := config{HermesURL: "http://127.0.0.1:1"}
	h := newServer(cfg).routes()
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/sessions/recent", nil))
	if rec.Code != http.StatusBadGateway {
		t.Errorf("unreachable gateway: status %d, want 502 so the page shows an error, not an empty list", rec.Code)
	}
}

func TestIndexIsServed(t *testing.T) {
	_, h := setup(t)
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://localhost:8080/", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "<html") {
		t.Errorf("index: status %d", rec.Code)
	}
}

func TestHostOnly(t *testing.T) {
	for in, want := range map[string]string{
		"localhost:8080": "localhost", "LOCALHOST": "localhost", "[::1]:8080": "::1", "127.0.0.1": "127.0.0.1",
	} {
		if got := hostOnly(in); got != want {
			t.Errorf("hostOnly(%q) = %q, want %q", in, got, want)
		}
	}
}

func messagesReq(sid, after string) *http.Request {
	target := "http://localhost:8080/api/sessions/" + sid + "/messages"
	if after != "" {
		target += "?after=" + after
	}
	return httptest.NewRequest(http.MethodGet, target, nil)
}

func TestMessagesReturnsADelegatedResultAfterTheTurnsReply(t *testing.T) {
	fake, h := setup(t)
	turn := decode[chatResponse](t, serve(h, chatReq(`{"message":"audit the fleet"}`)))

	// The page's first poll after a reply sees the turn's own messages.
	first := decode[sessionMessagesResponse](t, serve(h, messagesReq(turn.SessionID, "")))
	if len(first.Messages) != 2 ||
		first.Messages[0].Role != roleUser || first.Messages[0].Content != "audit the fleet" ||
		first.Messages[1].Role != roleAssistant || first.Messages[1].Content != turn.Reply {
		t.Fatalf("first poll = %+v, want the typed message then the reply (no tool or empty assistant rows)", first)
	}
	if first.LatestID != first.Messages[1].ID {
		t.Errorf("latest_id = %d, want the reply's ID %d", first.LatestID, first.Messages[1].ID)
	}
	if fake.lastQuery != "order=latest&limit=50&offset=0" {
		t.Errorf("upstream query = %q, want the newest page", fake.lastQuery)
	}

	resultID := fake.deliver(turn.SessionID, "audit finished: 2 findings")
	next := decode[sessionMessagesResponse](t, serve(h, messagesReq(turn.SessionID, strconv.FormatInt(first.LatestID, 10))))
	if len(next.Messages) != 2 || next.Messages[0].Role != roleUser ||
		next.Messages[1].Content != "audit finished: 2 findings" || next.Messages[1].ID != resultID {
		t.Fatalf("poll after the mark = %+v, want the wake and the delegated result", next)
	}
	if next.LatestID != resultID {
		t.Errorf("latest_id = %d, want %d", next.LatestID, resultID)
	}

	idle := decode[sessionMessagesResponse](t, serve(h, messagesReq(turn.SessionID, strconv.FormatInt(next.LatestID, 10))))
	if len(idle.Messages) != 0 || idle.LatestID != next.LatestID {
		t.Errorf("poll with nothing new = %+v, want no messages and the same mark", idle)
	}
}

func TestMessagesPagesBackPastALongTurnToTheMark(t *testing.T) {
	fake, h := setup(t)
	turn := decode[chatResponse](t, serve(h, chatReq(`{"message":"audit the fleet"}`)))
	mark := decode[sessionMessagesResponse](t, serve(h, messagesReq(turn.SessionID, ""))).LatestID

	// A result lands, then a tool-heavy turn writes more rows than one page.
	resultID := fake.deliver(turn.SessionID, "audit finished: 2 findings")
	fake.mu.Lock()
	for i := 0; i < 3*messagesPageLimit; i++ {
		fake.post(turn.SessionID, "tool", "pod list")
	}
	fake.mu.Unlock()

	got := decode[sessionMessagesResponse](t, serve(h, messagesReq(turn.SessionID, strconv.FormatInt(mark, 10))))
	if len(got.Messages) != 2 || got.Messages[1].ID != resultID {
		t.Fatalf("poll = %+v, want the result written before the long turn", got.Messages)
	}
	if got.LatestID <= resultID {
		t.Errorf("latest_id = %d, want past the turn's rows", got.LatestID)
	}
}

func TestMessagesRefusesForeignSessionIDs(t *testing.T) {
	fake, h := setup(t)
	for _, sid := range []string{"k8s-evt-00000abc", sessionIDPrefix + "xyz", sessionIDPrefix + strings.Repeat("A", 32)} {
		if rec := serve(h, messagesReq(sid, "")); rec.Code != http.StatusBadRequest {
			t.Errorf("session %q: status %d, want 400", sid, rec.Code)
		}
	}
	if fake.lastQuery != "" {
		t.Errorf("a refused poll reached Hermes")
	}
}

func TestMessagesRefusesABadMark(t *testing.T) {
	_, h := setup(t)
	sid := sessionIDPrefix + strings.Repeat("b", 32)
	for _, after := range []string{"-1", "abc", "1.5"} {
		if rec := serve(h, messagesReq(sid, after)); rec.Code != http.StatusBadRequest {
			t.Errorf("after=%q: status %d, want 400", after, rec.Code)
		}
	}
}

func TestMessagesReportsAMissingSession(t *testing.T) {
	_, h := setup(t)
	rec := serve(h, messagesReq(sessionIDPrefix+strings.Repeat("c", 32), ""))
	if rec.Code != http.StatusNotFound {
		t.Errorf("unknown session: status %d, want 404", rec.Code)
	}
}

func TestEveryResponseForbidsFraming(t *testing.T) {
	_, h := setup(t)
	reqs := []*http.Request{
		httptest.NewRequest(http.MethodGet, "http://localhost:8080/", nil),
		httptest.NewRequest(http.MethodGet, "http://localhost:8080/api/status", nil),
		httptest.NewRequest(http.MethodGet, "http://10.0.0.5:8080/healthz", nil),
		httptest.NewRequest(http.MethodGet, "http://rebind.attacker.example:8080/", nil),
		chatReq(`{"message":"hi"}`),
	}
	for _, req := range reqs {
		rec := serve(h, req)
		if got := rec.Header().Get("Content-Security-Policy"); got != cspFrameAncestorsNone {
			t.Errorf("%s %s: Content-Security-Policy = %q", req.Method, req.URL, got)
		}
		if got := rec.Header().Get("X-Frame-Options"); got != xFrameOptionsDeny {
			t.Errorf("%s %s: X-Frame-Options = %q", req.Method, req.URL, got)
		}
	}
}

// The page's grid row must not grow with the session list. When it did, a
// long list pushed the message box below the window with no way to scroll to
// it.
func TestPageKeepsTheMessageBoxInTheWindow(t *testing.T) {
	page, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	// Each grid's one row is bounded by the window, and every column, pane
	// and list down to the scrolling element has min-height: 0, so a long
	// feed or transcript scrolls inside its pane instead of pushing either
	// message box off the bottom of the page. The rail, top bar and banner
	// keep their height.
	for selector, rules := range map[string][]string{
		".app":         {"min-height: 0;", "grid-template-rows: minmax(0, 1fr);"},
		".rail":        {"min-height: 0;"},
		".rail-scroll": {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".workspace":   {"min-height: 0;"},
		".topbar":      {"flex-shrink: 0;"},
		".insights":    {"flex-shrink: 0;"},
		".panes":       {"flex: 1;", "min-height: 0;", "grid-template-rows: minmax(0, 1fr);"},
		".centre":      {"min-height: 0;"},
		".view":        {"min-height: 0;"},
		".feed":        {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".chat-stream": {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".side-pane":   {"min-height: 0;"},
		".side-body":   {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".about":       {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".composer":    {"flex-shrink: 0;"},
	} {
		block := cssBlock(t, string(page), selector)
		for _, rule := range rules {
			if !strings.Contains(block, rule) {
				t.Errorf("%s is missing %q", selector, rule)
			}
		}
	}
}

func TestPageNeverAsksForNotificationsOnLoad(t *testing.T) {
	page, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	// The one permission request sits in the handler of the rail button.
	if n := strings.Count(string(page), "Notification.requestPermission("); n != 1 {
		t.Fatalf("index.html requests notification permission %d times, want once", n)
	}
	toggle := string(page)[strings.Index(string(page), "async function toggleNotifications()"):]
	endIdx := strings.Index(toggle, "\n      }\n")
	if endIdx < 0 {
		endIdx = strings.Index(toggle, "\n    }\n")
	}
	toggle = toggle[:endIdx]
	if !strings.Contains(toggle, "Notification.requestPermission(") {
		t.Errorf("the permission request is not inside toggleNotifications")
	}
	if !strings.Contains(string(page), `$("btn-notify").addEventListener("click", toggleNotifications)`) {
		t.Errorf("toggleNotifications is not bound to the button's click")
	}
	for _, call := range []string{"toggleNotifications();", "toggleNotifications()\n"} {
		if strings.Contains(string(page), call) {
			t.Errorf("toggleNotifications is called outside a click handler")
		}
	}
}

// cssBlock returns the declarations of the first rule whose selector is
// exactly selector.
func cssBlock(t *testing.T, page, selector string) string {
	t.Helper()
	start := strings.Index(page, "      "+selector+" {")
	if start < 0 {
		start = strings.Index(page, "    "+selector+" {")
	}
	if start < 0 {
		t.Fatalf("index.html has no %s rule", selector)
	}
	end := strings.Index(page[start:], "}")
	return page[start : start+end]
}

func TestReplyIntoABusyAgentSessionIsRefused(t *testing.T) {
	recent := float64(time.Now().Add(-time.Minute).Unix())
	stale := float64(time.Now().Add(-agentBusyWindow - time.Minute).Unix())
	for _, tc := range []struct {
		name       string
		lastActive float64
		rows       [][2]any // role, content
		want       int
	}{
		{"unanswered user row", recent, [][2]any{{"user", "Pod crashlooping"}}, http.StatusConflict},
		{"mid-turn tool row", recent, [][2]any{{"user", "x"}, {"assistant", nil}, {"tool", "pods"}}, http.StatusConflict},
		{"answered", recent, [][2]any{{"user", "x"}, {"assistant", "Filed card t_1."}}, http.StatusOK},
		{"unanswered but stale", stale, [][2]any{{"user", "x"}}, http.StatusOK},
		{"no messages yet", recent, nil, http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fake, h := setup(t)
			sid := "k8s-evt-0000000a"
			fake.seed(sid, "api_server", "Triage "+sid)
			fake.mu.Lock()
			fake.seeded[sid]["last_active"] = tc.lastActive
			for _, r := range tc.rows {
				fake.post(sid, r[0].(string), r[1])
			}
			fake.mu.Unlock()
			for _, req := range []*http.Request{
				chatReq(`{"message":"hi","session_id":"` + sid + `"}`),
				streamReq(`{"message":"hi","session_id":"` + sid + `"}`),
			} {
				rec := serve(h, req)
				if rec.Code != tc.want {
					t.Fatalf("%s: status %d, want %d: %s", req.URL.Path, rec.Code, tc.want, rec.Body.String())
				}
				if tc.want == http.StatusConflict {
					if got := decode[errorResponse](t, rec); got.Error != "agent_busy" || got.Detail != agentBusyDetail {
						t.Errorf("busy body = %+v", got)
					}
				}
			}
			if _, chats := fake.state(); tc.want == http.StatusConflict && len(chats) != 0 {
				t.Errorf("a busy session still got a turn: %v", chats)
			}
		})
	}
}

func TestRunTurnWithoutDelegationReturnsImmediately(t *testing.T) {
	fake, s := setupServer(t)
	s.delegationPollInterval = 500 * time.Millisecond
	h := s.routes()

	start := time.Now()
	rec := serve(h, chatReq(`{"message":"what is your agent role?"}`))
	elapsed := time.Since(start)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	got := decode[chatResponse](t, rec)
	if got.Reply != "pods are healthy" {
		t.Errorf("reply = %q, want direct reply", got.Reply)
	}
	if elapsed >= 250*time.Millisecond {
		t.Errorf("direct turn without kanban_create waited %s, want no polling delay", elapsed)
	}
	// Four message reads and no poll: handleChat's existence check on the
	// page-minted ID, runTurn's beforeID read, the post-turn delegation
	// check, and replyMessageID.
	if gets := fake.gets(got.SessionID); gets != 4 {
		t.Errorf("message gets = %d, want 4 (existence + beforeID + post-turn check + reply ID)", gets)
	}
}

func TestRunTurnAwaitsDelegatedKanbanCardResult(t *testing.T) {
	t.Run("returns card summary when wake is silent", func(t *testing.T) {
		fake, s := setupServer(t)
		s.delegationPollInterval = 5 * time.Millisecond
		h := s.routes()

		fake.mu.Lock()
		fake.reply = "checking pods in kubeagents-system."
		fake.toolName = toolKanbanCreate
		fake.toolContent = `{"ok": true, "task_id": "t_1122aabb", "status": "ready"}`
		fake.onChat = func(sid string) {
			go func() {
				time.Sleep(15 * time.Millisecond)
				fake.mu.Lock()
				defer fake.mu.Unlock()
				fake.post(sid, roleUser, "[kanban] Task t_1122aabb completed.\n"+
					"Title: Check running pods in kubeagents-system\n"+
					"Assignee: @cluster-kube-agents-demo-platform-agent-host-us-central1\n"+
					"Board: default\n\n"+
					"Check the result or decide the next step.\n"+
					"Result: All 6 pods in kubeagents-system are Running (1/1 Ready) with 0 restarts.\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleAssistant, "[SILENT]")
			}()
		}
		fake.mu.Unlock()

		rec := serve(h, chatReq(`{"message":"what pods are running in kubeagents-system?"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		got := decode[chatResponse](t, rec)
		want := "All 6 pods in kubeagents-system are Running (1/1 Ready) with 0 restarts."
		if got.Reply != want {
			t.Errorf("reply = %q, want %q", got.Reply, want)
		}
		if fake.gets(got.SessionID) < 3 {
			t.Errorf("message gets = %d, want >= 3 showing polling occurred", fake.gets(got.SessionID))
		}
	})

	t.Run("prefers subsequent assistant synthesis over raw card summary", func(t *testing.T) {
		fake, s := setupServer(t)
		s.delegationPollInterval = 5 * time.Millisecond
		h := s.routes()

		fake.mu.Lock()
		fake.reply = "delegating node check."
		fake.toolName = toolKanbanCreate
		fake.toolContent = `{"ok": true, "task_id": "t_3344ccdd", "status": "ready"}`
		fake.onChat = func(sid string) {
			go func() {
				time.Sleep(15 * time.Millisecond)
				fake.mu.Lock()
				defer fake.mu.Unlock()
				fake.post(sid, roleUser, "[kanban] Task t_3344ccdd completed.\n"+
					"Title: Inspect nodes\n"+
					"Result: raw node output\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleAssistant, "All 3 nodes in platform-agent-host are Ready on v1.33.5-gke.1308000.")
			}()
		}
		fake.mu.Unlock()

		rec := serve(h, chatReq(`{"message":"what nodes are in this cluster?"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		got := decode[chatResponse](t, rec)
		want := "All 3 nodes in platform-agent-host are Ready on v1.33.5-gke.1308000."
		if got.Reply != want {
			t.Errorf("reply = %q, want %q", got.Reply, want)
		}
	})

	t.Run("returns explanation when card is blocked with subsequent assistant synthesis", func(t *testing.T) {
		fake, s := setupServer(t)
		s.delegationPollInterval = 5 * time.Millisecond
		h := s.routes()

		fake.mu.Lock()
		fake.reply = "checking container logs."
		fake.toolName = toolKanbanCreate
		fake.toolContent = `{"ok": true, "task_id": "t_5566eeff", "status": "ready"}`
		fake.onChat = func(sid string) {
			go func() {
				time.Sleep(15 * time.Millisecond)
				fake.mu.Lock()
				defer fake.mu.Unlock()
				fake.post(sid, roleUser, "[kanban] Task t_5566eeff blocked; needs attention.\n"+
					"Title: Get last 5 logs from platform-agent-gateway\n"+
					"Assignee: @cluster-gca-gke-test-ka-webconsole-pr2321-us-central1\n"+
					"Reason: The caller lacks container.pods.getLogs permission.\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleAssistant, "I couldn't fetch the logs from the platform-agent-gateway container because the service account lacks the container.pods.getLogs permission.")
			}()
		}
		fake.mu.Unlock()

		rec := serve(h, chatReq(`{"message":"show the last 5 log lines from platform-agent-gateway"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		got := decode[chatResponse](t, rec)
		want := "I couldn't fetch the logs from the platform-agent-gateway container because the service account lacks the container.pods.getLogs permission."
		if got.Reply != want {
			t.Errorf("reply = %q, want %q", got.Reply, want)
		}
	})
}

func TestResolveDelegatedTurnHandlesMultipleCardsAndSynthesisGrace(t *testing.T) {
	t.Run("waits until all delegated cards reach a terminal status", func(t *testing.T) {
		fake, s := setupServer(t)
		s.delegationPollInterval = 5 * time.Millisecond
		h := s.routes()

		fake.mu.Lock()
		fake.reply = "delegating two checks."
		fake.toolName = toolKanbanCreate
		fake.toolContent = `{"ok": true, "task_id": "t_card_1", "status": "ready"}`
		fake.onChat = func(sid string) {
			// Record a second kanban_create during the same turn.
			fake.postTool(sid, roleTool, toolKanbanCreate, `{"ok": true, "task_id": "t_card_2", "status": "ready"}`)
			go func() {
				time.Sleep(10 * time.Millisecond)
				fake.mu.Lock()
				// First card completes while second is still pending; an interim
				// assistant message after card 1 must not settle the turn early.
				fake.post(sid, roleUser, "[kanban] Task t_card_1 completed.\n"+
					"Title: Check nodes\n"+
					"Result: 3 nodes Ready.\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleAssistant, "Interim note after card 1.")
				fake.mu.Unlock()

				time.Sleep(25 * time.Millisecond)
				fake.mu.Lock()
				defer fake.mu.Unlock()
				fake.post(sid, roleUser, "[kanban] Task t_card_2 completed.\n"+
					"Title: Check pods\n"+
					"Result: 12 pods Running.\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleAssistant, "Combined synthesis: 3 nodes Ready and 12 pods Running.")
			}()
		}
		fake.mu.Unlock()

		rec := serve(h, chatReq(`{"message":"check nodes and pods"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		got := decode[chatResponse](t, rec)
		want := "Combined synthesis: 3 nodes Ready and 12 pods Running."
		if got.Reply != want {
			t.Errorf("reply = %q, want %q", got.Reply, want)
		}
	})

	t.Run("falls back to joined card summaries after synthesis grace window expires", func(t *testing.T) {
		fake, s := setupServer(t)
		s.delegationPollInterval = 5 * time.Millisecond
		h := s.routes()

		fake.mu.Lock()
		fake.reply = "delegating two checks."
		fake.toolName = toolKanbanCreate
		fake.toolContent = `{"ok": true, "task_id": "t_grace_1", "status": "ready"}`
		fake.onChat = func(sid string) {
			fake.postTool(sid, roleTool, toolKanbanCreate, `{"ok": true, "task_id": "t_grace_2", "status": "ready"}`)
			go func() {
				time.Sleep(10 * time.Millisecond)
				fake.mu.Lock()
				defer fake.mu.Unlock()
				// Both cards reach terminal status, and no assistant synthesis or
				// [SILENT] message is ever posted afterwards.
				fake.post(sid, roleUser, "[kanban] Task t_grace_1 completed.\n"+
					"Title: Check nodes\n"+
					"Result: Node summary 1\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
				fake.post(sid, roleUser, "[kanban] Task t_grace_2 completed.\n"+
					"Title: Check pods\n"+
					"Result: Pod summary 2\n\n"+
					"This is an automatic task-status notification, not a request to decompose the task again.")
			}()
		}
		fake.mu.Unlock()

		rec := serve(h, chatReq(`{"message":"check nodes and pods without synthesis"}`))
		if rec.Code != http.StatusOK {
			t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
		}
		got := decode[chatResponse](t, rec)
		want := "Node summary 1\n\nPod summary 2"
		if got.Reply != want {
			t.Errorf("reply = %q, want %q", got.Reply, want)
		}
	})
}

// ---- Page behaviour, run under Node ----
//
// The page's script is one inline <script>. pageJS pulls named top-level
// functions and single-line declarations out of it, and runPageJS runs them
// with stubs under node, so a test exercises the shipped code rather than a
// copy. Tests skip when node is not on PATH.

func pageSource(t *testing.T) string {
	t.Helper()
	page, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	return string(page)
}

// pageJS returns the source of each named top-level function (four-space
// indent, ending at the first closing brace at that indent) or one-line
// const/let declaration.
func pageJS(t *testing.T, names ...string) string {
	t.Helper()
	page := pageSource(t)
	var out strings.Builder
	for _, name := range names {
		found := false
		for _, indent := range []string{"    ", "      "} {
			for _, head := range []string{"\n" + indent + "function " + name + "(", "\n" + indent + "async function " + name + "("} {
				start := strings.Index(page, head)
				if start < 0 {
					continue
				}
				closeTok := "\n" + indent + "}\n"
				end := strings.Index(page[start+1:], closeTok)
				if end < 0 {
					t.Fatalf("function %s has no closing brace", name)
				}
				out.WriteString(page[start+1 : start+1+end+len(closeTok)])
				found = true
				break
			}
			if found {
				break
			}
		}
		if found {
			continue
		}
		for _, indent := range []string{"    ", "      "} {
			for _, head := range []string{"\n" + indent + "const " + name + " =", "\n" + indent + "let " + name + " ="} {
				start := strings.Index(page, head)
				if start < 0 {
					continue
				}
				end := strings.Index(page[start+1:], ";\n")
				if end < 0 {
					end = strings.Index(page[start+1:], "\n")
					out.WriteString(page[start+1:start+1+end] + "\n")
				} else {
					out.WriteString(page[start+1 : start+1+end+len(";\n")])
				}
				found = true
				break
			}
			if found {
				break
			}
		}
		if !found {
			t.Fatalf("index.html has no top-level %s", name)
		}
	}
	return out.String()
}

// runPageJS runs script under node and returns its trimmed stdout.
func runPageJS(t *testing.T, script string) string {
	t.Helper()
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node is not on PATH")
	}
	out, err := exec.Command(node, "-e", script).CombinedOutput()
	if err != nil {
		t.Fatalf("node: %v\n%s", err, out)
	}
	return strings.TrimSpace(string(out))
}

// pageSnippet returns page text from the first occurrence of start up to
// and including the first end after it.
func pageSnippet(t *testing.T, start, end string) string {
	t.Helper()
	page := pageSource(t)
	i := strings.Index(page, start)
	if i < 0 {
		t.Fatalf("index.html has no %q", start)
	}
	j := strings.Index(page[i:], end)
	if j < 0 {
		t.Fatalf("index.html has no %q after %q", end, start)
	}
	return page[i : i+j+len(end)]
}

func TestPageSkillPillsNeedAKnownSkill(t *testing.T) {
	script := `const SKILL_BY_NAME = { "gke-cost-analysis": { name: "gke-cost-analysis", cat: "fleet", desc: "" } };
const skillUsage = new Map([["custom-skill", {}]]);
` + pageJS(t, "escapeHtml", "skillInfo", "renderInline") + `
const pills = s => (renderInline(s).match(/class="skill-pill/g) || []).length;
console.log(JSON.stringify([pills("costs $120/month"), pills("set $pod_name first"), pills("use $gke-cost-analysis now"), pills("ran $custom-skill")]));`
	if got, want := runPageJS(t, script), "[0,0,1,1]"; got != want {
		t.Errorf("skill pills for [dollar amount, shell variable, catalog skill, reported skill] = %s, want %s", got, want)
	}
}

func TestPageCountsEachSkillAndCardOnce(t *testing.T) {
	script := `const sessionSkills = {}; const skillUsage = new Map(); const seenSpecialists = new Map();
const nowSeconds = () => 1;
` + pageJS(t, "countedSkillKeys", "noteSkills", "recordSpecialistFromCard") + `
noteSkills("s1", ["a"], "m1"); noteSkills("s1", ["a"], "m1"); noteSkills("s1", ["a"], "summary"); noteSkills("s1", ["a"], "summary");
noteSkills("s1", ["a"], "m2");
const card = { assignee: "Cluster agent (x)", task_id: "t_1", status: "completed" };
recordSpecialistFromCard(card); recordSpecialistFromCard(card);
recordSpecialistFromCard({ assignee: "Cluster agent (x)", task_id: "t_2" });
console.log(JSON.stringify([skillUsage.get("a").count, seenSpecialists.get("Cluster agent (x)").count]));`
	if got, want := runPageJS(t, script), "[3,2]"; got != want {
		t.Errorf("[skill count, specialist count] after repeated notes = %s, want %s", got, want)
	}
}

// Step ids restart at skill_view#1 on every turn (stream.go numbers them per
// turn), so the same skill read on two turns of one thread counts twice,
// while a repeated event for one step counts once.
func TestPageSkillStepsCountOncePerTurn(t *testing.T) {
	script := `const sessionSkills = {}; const skillUsage = new Map(); const inflight = {};
let clock = 100; const nowSeconds = () => clock;
const refreshSkillsBars = () => {}; const statusChanged = () => {};
` + pageJS(t, "countedSkillKeys", "noteSkills", "startInflight", "addStep") + `
const STATUS_SENDING = "sending";
startInflight("s1", "first");
addStep("s1", { id: "skill_view#1", skill: "a" }); addStep("s1", { id: "skill_view#1", skill: "a", state: "done" });
delete inflight.s1; clock = 200;
startInflight("s1", "second");
addStep("s1", { id: "skill_view#1", skill: "a" });
console.log(skillUsage.get("a").count);`
	if got, want := runPageJS(t, script), "2"; got != want {
		t.Errorf("skill count after one read on each of two turns = %s, want %s", got, want)
	}
}

func TestPageFeedbackFormKeepsTheTitle(t *testing.T) {
	script := `const FEEDBACK_KINDS = { bug: {}, feature: {}, usage: {}, question: {} };
` + pageJS(t, "isFeedbackKind", "feedbackFormArgs") + `
console.log(JSON.stringify([
  feedbackFormArgs(["form", "Login", "button", "is", "dead"]),
  feedbackFormArgs(["form", "Feature", "Dark", "mode"]),
  feedbackFormArgs(["form", "constructor"]),
  feedbackFormArgs(["form"])]));`
	want := `[["bug","Login button is dead"],["feature","Dark mode"],["bug","constructor"],["bug",""]]`
	if got := runPageJS(t, script); got != want {
		t.Errorf("feedbackFormArgs = %s, want %s", got, want)
	}
	// Switching the kind keeps a body the operator typed:
	// renderFeedbackPreview replaces only a template body.
	handler := pageSnippet(t, `$("feedback-kind-bar").addEventListener("click"`, "\n      });\n")
	if strings.Contains(handler, `.value = ""`) || strings.Contains(handler, `template = "1"`) {
		t.Errorf("the feedback kind bar resets the Details body:\n%s", handler)
	}
}

func TestPageMergeOfStoredThreadsKeepsTheLiveObjects(t *testing.T) {
	script := `let threads = [], activeId = "", view = "alerts"; const inflight = {}; const calls = [];
const parseThreads = raw => JSON.parse(raw);
const onScreen = id => view === "thread" && activeId === id;
const threadsChanged = () => {}; const selectThread = id => calls.push("select:" + id); const selectChannel = n => calls.push("channel:" + n);
` + pageJS(t, "findThread", "mergeStoredThreads") + `
const x = { id: "x", title: "", mark: 5, markKnown: true, updated: 1, unread: false, started: true };
threads = [x];
mergeStoredThreads(JSON.stringify([{ id: "x", title: "T", mark: 7, markKnown: true, updated: 2, unread: false, started: true }]));
const kept = threads[0] === x && x.mark === 7 && x.title === "T";
const busy = { id: "b", mark: 0, updated: 1 };
threads = [busy, x]; inflight.b = {};
mergeStoredThreads(JSON.stringify([{ id: "x", mark: 7, updated: 2 }]));
const busyKept = threads.includes(busy);
delete inflight.b; threads = [x]; activeId = "x"; view = "thread";
mergeStoredThreads(JSON.stringify([{ id: "y", mark: 0, updated: 3 }]));
console.log(JSON.stringify([kept, busyKept, activeId, calls]));`
	if got, want := runPageJS(t, script), `[true,true,"y",["select:y"]]`; got != want {
		t.Errorf("[same object kept, running thread kept, activeId, pane calls] = %s, want %s", got, want)
	}
}

func TestPageCapThreadsKeepsPinnedThreads(t *testing.T) {
	script := `const MAX_THREADS = 2; const inflight = {}; let activeId = ""; let pinnedIds = new Set(["old"]);
let threads = [{ id: "a", updated: 3 }, { id: "b", updated: 2 }, { id: "old", updated: 1 }];
` + pageJS(t, "capThreads") + `
capThreads();
console.log(threads.map(t => t.id).join(","));`
	if got, want := runPageJS(t, script), "a,old"; got != want {
		t.Errorf("threads after capThreads = %s, want %s", got, want)
	}
}

func TestPageOpeningAnUnreadThreadMarksNewMessages(t *testing.T) {
	script := `let activeId = "", view = ""; const shown = [];
const stream = { appendChild: el => shown.push(el) };
const $ = () => stream; const unreadSepRow = () => "SEP";
const clearStream = () => {}; const threadTitle = s => s; const noteMessageActivity = () => {};
const appendMessage = (role, text) => shown.push(text); const refreshSkillsBars = () => {};
const advanceMark = () => {}; const saveThreads = () => {}; const threadsChanged = () => {};
` + pageJS(t, "activateThread") + `
const t = { id: "s", title: "x" };
const transcript = { latest_id: 3, messages: [1, 2, 3].map(id => ({ id, role: "assistant", content: "m" + id })) };
activateThread(t, transcript, 1);
const withMark = shown.join(",");
shown.length = 0;
activateThread(t, transcript, 0);
console.log(withMark + "|" + shown.join(","));`
	if got, want := runPageJS(t, script), "m1,SEP,m2,m3|m1,m2,m3"; got != want {
		t.Errorf("rows shown = %s, want %s", got, want)
	}
}

// The poll delivers an off-screen message and moves the mark past it in the
// same pass, so the divider has to come from where unread began, not from
// the mark at the time the thread is opened.
func TestPageUnreadStartSurvivesThePollMovingTheMark(t *testing.T) {
	script := `const onScreen = () => false; const noteMessageActivity = () => {};
const saveThreads = () => {}; const nowSeconds = () => 1; const refreshDelegationPills = () => {};
` + pageJS(t, "advanceMark", "deliver", "showUpdates") + `
const t = { id: "s", mark: 1, markKnown: true, unread: false };
showUpdates(t, [2, 3].map(id => ({ id, role: "assistant", content: "m" + id })));
console.log(JSON.stringify([t.unread, t.mark, t.unreadFrom]));`
	if got, want := runPageJS(t, script), "[true,3,1]"; got != want {
		t.Errorf("[unread, mark, unreadFrom] after an off-screen poll = %s, want %s", got, want)
	}
	sel := pageSnippet(t, "async function selectThread(id) {", "activeId = id;")
	if !strings.Contains(sel, "const prevMark = t.unreadFrom || 0;") {
		t.Errorf("selectThread does not open the divider from t.unreadFrom:\n%s", sel)
	}
}

func TestPageTranscriptFollowsOnlyFromTheBottomAndLabelsLocalRows(t *testing.T) {
	script := `const rows = [];
const stream = { scrollTop: 100, scrollHeight: 1000, clientHeight: 200, appendChild: el => rows.push(el) };
const $ = () => stream; let centreLastDay = "";
const recordSpecialistFromCard = () => {}; const nowSeconds = () => 86400;
const dayLabel = s => "day" + Math.floor(s / 86400); const daySepRow = l => "sep:" + l;
const messageRow = (role, text) => role + ":" + text;
` + pageJS(t, "NEAR_BOTTOM_PX", "nearBottom", "appendTo") + `
appendTo(stream, "agent", "report");
const stayed = stream.scrollTop;
appendTo(stream, "user", "mine");
centreLastDay = "";
appendTo(stream, "agent", "old", "", undefined, null, null, { id: 7 });
console.log(JSON.stringify([stayed, stream.scrollTop, rows]));`
	if got, want := runPageJS(t, script), `[100,1000,["sep:day1","agent:report","user:mine","agent:old"]]`; got != want {
		t.Errorf("[scrollTop after agent row, after own row, rows] = %s, want %s", got, want)
	}
}

func TestPageCardOutcomeReadsMultiWordWakeHeads(t *testing.T) {
	script := pageJS(t, "CARD_DONE_STATUS", "CARD_BLOCKED_STATUS", "cardOutcomeState") + `
console.log(["completed", "gave up (retries exhausted)", "blocked; needs attention", "crashed (worker exited); dispatcher will retry", "timed out; dispatcher will retry", "in_progress"].map(cardOutcomeState).join(","));`
	if got, want := runPageJS(t, script), "done,done,blocked,running,running,running"; got != want {
		t.Errorf("card states = %s, want %s", got, want)
	}
}

func TestPageBrowserFamilyReportsEdge(t *testing.T) {
	fn := pageJS(t, "browserFamily")
	script := `const family = ua => (function () { const navigator = { userAgent: ua }; ` + fn + ` return browserFamily(); })();
console.log([
  family("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.2210.91"),
  family("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
  family("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15")].join("|"));`
	if got, want := runPageJS(t, script), "Edge 120 on Windows|Chrome 120 on Linux|Safari 17 on macOS"; got != want {
		t.Errorf("browserFamily = %s, want %s", got, want)
	}
}

func TestPageReleaseStatusLabelShowsBehindWhenPatched(t *testing.T) {
	script := pageJS(t, "releaseStatusLabel", "safeHttpUrl") + `
const behindPatched = releaseStatusLabel({ status: "behind", latest: "0.13.0", is_patched: true, applied_prs: [{ pr: 1 }] });
const latestPatched = releaseStatusLabel({ status: "latest", latest: "0.12.0", is_patched: true, applied_prs: [{ pr: 1 }] });
const safe1 = safeHttpUrl("javascript:alert(1)", "https://github.com/gke-labs/kube-agents/pull/42");
const safe2 = safeHttpUrl("https://github.com/gke-labs/kube-agents/pull/42", "#");
console.log(JSON.stringify([behindPatched.text, latestPatched.text, safe1, safe2]));`
	if got, want := runPageJS(t, script), `["0.13.0 available","1 PR applied","https://github.com/gke-labs/kube-agents/pull/42","https://github.com/gke-labs/kube-agents/pull/42"]`; got != want {
		t.Errorf("releaseStatusLabel / safeHttpUrl = %s, want %s", got, want)
	}
}

func TestPageResolveForgeNamePreservesNamedForge(t *testing.T) {
	script := `let lastRepos = { forges: [{ name: "in-cluster-gitea", provider: "gitea" }, { name: "ghe", provider: "github" }] };
` + pageJS(t, "resolveForgeName") + `
console.log(JSON.stringify([
  resolveForgeName("gitea", "in-cluster-gitea", "gitea"),
  resolveForgeName("github", "in-cluster-gitea", "gitea"),
  resolveForgeName("gitea", "", "")
]));`
	if got, want := runPageJS(t, script), `["in-cluster-gitea","ghe","in-cluster-gitea"]`; got != want {
		t.Errorf("resolveForgeName = %s, want %s", got, want)
	}
}

func TestPageStreamedRepliesDoNotPileUpArtifacts(t *testing.T) {
	script := `const SKILL_BY_NAME = {}; const skillUsage = new Map();
` + pageJS(t, "escapeHtml", "skillInfo", "renderInline", "artifactSeq", "artifactStore", "artifactIdByKey", "registerArtifact", "renderMarkdown") + "\n" +
		"const fence = \"```yaml\\na: 1\\n```\";\n" + `
const live = renderMarkdown(fence, { live: true });
const afterLive = Object.keys(artifactStore).length;
renderMarkdown(fence); renderMarkdown(fence);
console.log(JSON.stringify([live.includes("data-copy-art"), afterLive, Object.keys(artifactStore).length]));`
	if got, want := runPageJS(t, script), "[false,0,1]"; got != want {
		t.Errorf("[live render has artifact buttons, artifacts after live, after two final renders] = %s, want %s", got, want)
	}
}

func TestPageUpgradeCommandsUseDocumentedUpgradeScriptAndBundle(t *testing.T) {
	fn := pageJS(t, "UPGRADE_SCRIPT_URL", "UPGRADE_BUNDLE_URL", "upgradeCommands")
	if !strings.Contains(fn, "upgrade.sh --non-interactive") || !strings.Contains(fn, "kube-agents-{version}.tar.gz") || strings.Contains(fn, "helm upgrade") {
		t.Errorf("upgradeCommands should use the release-pinned upgrade.sh and release bundle rather than raw helm upgrade:\n%s", fn)
	}
}

// Entry points that send into or write to the centre stream must make it
// visible and must not hand a request to a thread that has a turn running.
func TestPageEntryPointsReachAVisibleIdleThread(t *testing.T) {
	ask := pageSnippet(t, `ask.addEventListener("click"`, "\n          });\n")
	if !strings.Contains(ask, `isThreadBusy(findThread(activeId))`) || !strings.Contains(ask, "newChat();") {
		t.Errorf("Ask the agent can send into a busy thread:\n%s", ask)
	}
	propose := pageJS(t, "sendPRProposalToAgent")
	if !strings.Contains(propose, `isThreadBusy(findThread(activeId))`) || !strings.Contains(propose, "newChat();") {
		t.Errorf("sendPRProposalToAgent can send into a busy or non-thread view:\n%s", propose)
	}
	palette := pageSnippet(t, `sub: c.local ? "Action" : "Prompt",`, "handleSlashCommand(c.token, \"centre\");")
	for _, want := range []string{"else newChat();", "PALETTE_STREAM_COMMANDS.has(c.local)", `view !== "thread"`} {
		if !strings.Contains(palette, want) {
			t.Errorf("palette command run is missing %q:\n%s", want, palette)
		}
	}
	escape := pageSnippet(t, "function wireAutocomplete(", `if (e.key === "Escape") {`)
	escapeArm := pageSnippet(t, escape[len(escape)-len(`if (e.key === "Escape") {`):], "return;")
	if !strings.Contains(escapeArm, "e.stopPropagation();") {
		t.Errorf("Escape in the autocomplete popup reaches the document handler:\n%s", escapeArm)
	}
}

func TestIsTerminalKanbanStatus(t *testing.T) {
	for _, s := range []string{"completed", "done", "failed", "blocked", "cancelled", "canceled", "aborted", "gave_up", "gave up (retries exhausted)"} {
		if !isTerminalKanbanStatus(s) {
			t.Errorf("isTerminalKanbanStatus(%q) = false, want true", s)
		}
	}
	for _, s := range []string{"in_progress", "ready", "timed out; dispatcher will retry", "timed_out", "crashed (worker exited); dispatcher will retry"} {
		if isTerminalKanbanStatus(s) {
			t.Errorf("isTerminalKanbanStatus(%q) = true, want false", s)
		}
	}
}

func TestPageTopbarActionsWrapInsteadOfClipping(t *testing.T) {
	page := pageSource(t)
	actions := cssBlock(t, page, ".header-actions")
	for _, rule := range []string{"flex-wrap: wrap;", "min-width: 0;", "flex-shrink: 1;"} {
		if !strings.Contains(actions, rule) {
			t.Errorf(".header-actions is missing %q", rule)
		}
	}
	topbar := cssBlock(t, page, ".topbar")
	if regexp.MustCompile(`(?m)^\s*height:`).MatchString(topbar) || !strings.Contains(topbar, "min-height: 52px;") {
		t.Errorf(".topbar must grow with wrapped actions:\n%s", topbar)
	}
}

func TestPageUsageEmailDoesNotAskForAMissingJob(t *testing.T) {
	page := pageSource(t)
	for _, text := range []string{"run the usage-report cron job", "reports/kube-agents-usage-"} {
		if strings.Contains(page, text) {
			t.Errorf("index.html still mentions %q, which this release does not ship", text)
		}
	}
}

// Every catalog entry's SKILL.md link and "Lives at" line must point at a
// file in the tree, and SKILL_CATALOG must stay in parity with knownPlatformSkills.
func TestSkillCatalogPointsAtSkillsInTheTree(t *testing.T) {
	entry := regexp.MustCompile(`\{\s*name:\s*"([a-z0-9-]+)",\s*cat:\s*"[a-z]+",\s*(?:agent:\s*"(cluster|tse)",\s*)?desc:`)
	matches := entry.FindAllStringSubmatch(pageSource(t), -1)
	if len(matches) < 40 {
		t.Fatalf("found %d SKILL_CATALOG entries; the pattern no longer matches the catalog", len(matches))
	}
	inCatalog := make(map[string]bool, len(matches))
	for _, m := range matches {
		inCatalog[m[1]] = true
		dir := "platform"
		if m[2] != "" {
			dir = m[2]
		}
		path := "../../../agents/" + dir + "/skills/" + m[1] + "/SKILL.md"
		if _, err := os.Stat(path); err != nil {
			t.Errorf("catalog skill %s: %v", m[1], err)
		}
	}
	knownSet := make(map[string]bool, len(knownPlatformSkills))
	for _, name := range knownPlatformSkills {
		knownSet[name] = true
		if !inCatalog[name] {
			t.Errorf("knownPlatformSkills entry %q is missing from SKILL_CATALOG", name)
		}
	}
	for name := range inCatalog {
		if !knownSet[name] {
			t.Errorf("SKILL_CATALOG entry %q is missing from knownPlatformSkills", name)
		}
	}
}

// Opening the Upgrade dialog before About or Feedback must still fetch the
// environment, or the ownership line reads "owned by not readable from this
// console".
func TestPageUpgradeDialogFetchesTheEnvironment(t *testing.T) {
	open := pageJS(t, "openUpgrade")
	for _, want := range []string{"if (!lastEnvironment)", "fetchEnvironment().then(", "renderUpgradeModal();"} {
		if !strings.Contains(open, want) {
			t.Errorf("openUpgrade is missing %q:\n%s", want, open)
		}
	}
}
