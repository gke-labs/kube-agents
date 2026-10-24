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
	f.nextID++
	f.messages[sid] = append(f.messages[sid], map[string]any{"id": f.nextID, "session_id": sid, "role": role, "content": content})
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
		f.post(sid, "tool", "pod list")
		f.post(sid, "assistant", reply)
		if f.postTurnWake != "" {
			f.post(sid, "user", "[kanban] card completed")
			f.post(sid, "assistant", f.postTurnWake)
		}
		f.mu.Unlock()
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
		".app":           {"min-height: 0;", "grid-template-rows: minmax(0, 1fr);"},
		".rail":          {"min-height: 0;"},
		".rail-scroll":   {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".workspace":     {"min-height: 0;"},
		".topbar":        {"flex-shrink: 0;"},
		".insights":      {"flex-shrink: 0;"},
		".panes":         {"flex: 1;", "min-height: 0;", "grid-template-rows: minmax(0, 1fr);"},
		".centre":        {"min-height: 0;"},
		".view":          {"min-height: 0;"},
		".feed":          {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".chat-stream":   {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".side-pane":     {"min-height: 0;"},
		".side-body":     {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".about":         {"flex: 1 1 auto;", "min-height: 0;", "overflow-y: auto;"},
		".composer":      {"flex-shrink: 0;"},
		".thread-remove": {"opacity: 0;"},
	} {
		block := cssBlock(t, string(page), selector)
		for _, rule := range rules {
			if !strings.Contains(block, rule) {
				t.Errorf("%s is missing %q", selector, rule)
			}
		}
	}
	if strings.Contains(cssBlock(t, string(page), ".thread-remove"), "visibility: hidden") {
		t.Errorf(".thread-remove must not use visibility: hidden, which removes it from keyboard tab order")
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

func TestPageHarnessCoversThreadsUnreadStatusStepsTimerAndCards(t *testing.T) {
	if _, err := exec.LookPath("node"); err != nil {
		if os.Getenv("CI") != "" {
			t.Fatalf("node not available in PATH on CI: %v", err)
		}
		t.Skip("node not available in PATH")
	}
	page, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	src := string(page)
	start := strings.Index(src, "// ---- Thread state.")
	end := strings.Index(src, "// Builds one message row.")
	renderStart := strings.Index(src, "let threadsHtml = \"\";")
	renderEnd := strings.Index(src, "function showView(name)")
	compStart := strings.Index(src, "function updateComposer()")
	compEnd := strings.Index(src, "let chatPlatform =")
	paneStart := strings.Index(src, "const pane = { mode: null, id: \"\", latestId: null };")
	paneEnd := strings.Index(src, "// Slack, Google Chat and A2A bridge sessions")
	enterStart := strings.Index(src, "function onEnter(input, send)")
	enterEnd := strings.Index(src, "$(\"btn-send\").addEventListener")
	if start < 0 || end <= start || renderStart < 0 || renderEnd <= renderStart || compStart < 0 || compEnd <= compStart || paneStart < 0 || paneEnd <= paneStart || enterStart < 0 || enterEnd <= enterStart {
		t.Fatal("index.html is missing the thread-state, renderThreads, updateComposer, pane, or onEnter slice markers")
	}
	slice := src[start:end] + "\n" + src[renderStart:renderEnd] + "\n" + src[compStart:compEnd] + "\n" + src[enterStart:enterEnd]
	paneSlice := src[paneStart:paneEnd]

	cmd := exec.Command("node", "--input-type=module", "-e", `
import assert from "node:assert/strict";
import vm from "node:vm";
import crypto from "node:crypto";

const scriptSource = `+strconv.Quote(slice)+`;
const paneSource = `+strconv.Quote(paneSlice)+`;

function makeHarness(fetchImpl, initialLocal = {}, initialSession = {}) {
  const local = new Map(Object.entries(initialLocal));
  const session = new Map(Object.entries(initialSession));
  const appended = [];
  const statusCalls = [];
  let cleared = 0;
  let changed = 0;
  let selectedThread = null;
  let selectedChannel = null;
  const ctx = {
    console,
    Date,
    Math,
    Number,
    String,
    Array,
    Object,
    Set,
    JSON,
    RegExp,
    Uint8Array,
    TextDecoder,
    crypto: crypto.webcrypto,
    encodeURIComponent,
    CONSOLE_HEADER: "X-Kube-Agents-Console",
    pending: { centre: null, side: null },
    showPending: () => {},
    removePending: () => {},
    localStorage: {
      getItem: k => (local.has(k) ? local.get(k) : null),
      setItem: (k, v) => local.set(k, String(v)),
      removeItem: k => local.delete(k),
    },
    sessionStorage: {
      getItem: k => (session.has(k) ? session.get(k) : null),
      setItem: (k, v) => session.set(k, String(v)),
      removeItem: k => session.delete(k),
    },
    document: { getElementById: () => ({}), querySelectorAll: () => [] },
    $: id => ctx.document.getElementById(id),
    fetch: fetchImpl || (async () => ({ ok: false, status: 503 })),
    appendMessage: (author, text, tag, ts, card, meta) => {
      appended.push({ author, text, tag, ts, card, meta });
    },
    clearStream: () => { cleared++; },
    threadsChanged: () => { changed++; },
    statusChanged: id => { statusCalls.push(id); },
    selectThread: id => { selectedThread = id; },
    selectChannel: name => { selectedChannel = name; },
    getState: () => ({
      local,
      session,
      appended,
      statusCalls,
      cleared,
      changed,
      selectedThread,
      selectedChannel,
    }),
  };
  vm.createContext(ctx);
  vm.runInContext(scriptSource, ctx);
  return ctx;
}

// 1. Legacy sessionStorage migration into localStorage threads.
{
  const legacyId = "web-console-" + "1".repeat(32);
  const ctx = makeHarness(null, {}, {
    "kube-agents-web-console-session": legacyId,
    "kube-agents-web-console-mark": "12",
  });
  vm.runInContext("loadThreads()", ctx);
  const threads = JSON.parse(vm.runInContext("JSON.stringify(threads)", ctx));
  assert.equal(threads.length, 1);
  assert.equal(threads[0].id, legacyId);
  assert.equal(threads[0].mark, 12);
  assert.equal(threads[0].markKnown, true);
  assert.equal(ctx.getState().session.has("kube-agents-web-console-session"), false);
}

// 2. capThreads keeps at most MAX_THREADS and never drops in-flight or activeId.
{
  const ctx = makeHarness();
  const protectedId = "web-console-" + "a".repeat(32);
  vm.runInContext("addThread(" + JSON.stringify(protectedId) + ", 'protected', true); startInflight(" + JSON.stringify(protectedId) + ", 'running');", ctx);
  for (let i = 0; i < 15; i++) {
    const id = "web-console-" + i.toString(16).padStart(32, "0");
    vm.runInContext("addThread(" + JSON.stringify(id) + ", 't' + " + i + ", true)", ctx);
  }
  const ids = JSON.parse(vm.runInContext("JSON.stringify(threads.map(t => t.id))", ctx));
  assert.equal(ids.length, 12);
  assert.ok(ids.includes(protectedId), "in-flight thread must never be evicted by capThreads");
  assert.equal(vm.runInContext("removeThread(" + JSON.stringify(protectedId) + ")", ctx), false);
}

// 3. mergeStoredThreads preserves object identity, keeps local in-flight threads,
// does not advance mark on the on-screen thread, and preserves the on-screen thread
// even if evicted by another tab.
{
  const idA = "web-console-" + "a".repeat(32);
  const idB = "web-console-" + "b".repeat(32);
  const idC = "web-console-" + "c".repeat(32);
  const ctx = makeHarness();
  vm.runInContext(
    "const tA = addThread(" + JSON.stringify(idA) + ", 'A', true);" +
    "const tB = addThread(" + JSON.stringify(idB) + ", 'B', true);" +
    "const tC = addThread(" + JSON.stringify(idC) + ", 'C', true);" +
    "tA.mark = 4; tA.markKnown = true;" +
    "tB.mark = 2; tB.markKnown = true;" +
    "activeId = " + JSON.stringify(idA) + "; view = 'thread';" +
    "startInflight(" + JSON.stringify(idC) + ", 'busy');",
    ctx
  );
  // Tab 2 advanced tA.mark to 9 and tB.mark to 7, and did not know about tC.
  const tab2Raw = JSON.stringify([
    { id: idA, title: "A", mark: 9, markKnown: true, updated: 100, unread: true, started: true },
    { id: idB, title: "B", mark: 7, markKnown: true, updated: 100, unread: true, started: true },
  ]);
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(tab2Raw) + ")", ctx);
  assert.equal(vm.runInContext("findThread(" + JSON.stringify(idA) + ") === tA", ctx), true, "object identity preserved");
  assert.equal(vm.runInContext("tA.mark", ctx), 4, "on-screen thread mark must not jump ahead before pollThread renders");
  assert.equal(vm.runInContext("tA.unread", ctx), false, "on-screen thread stays read");
  assert.equal(vm.runInContext("tB.mark", ctx), 7, "background thread adopts higher mark from other tab");
  assert.equal(vm.runInContext("!!findThread(" + JSON.stringify(idC) + ")", ctx), true, "in-flight thread preserved across storage merge");

  // An on-screen thread idA missing from another tab's update (e.g. evicted by capThreads in tab B)
  // is preserved in merged threads so this tab does not jump away mid-read.
  const tab2WithoutA = JSON.stringify([
    { id: idB, title: "B", mark: 7, markKnown: true, updated: 100, unread: false, started: true },
  ]);
  vm.runInContext("delete inflight[" + JSON.stringify(idC) + "]; mergeStoredThreads(" + JSON.stringify(tab2WithoutA) + ")", ctx);
  assert.equal(vm.runInContext("!!findThread(" + JSON.stringify(idA) + ")", ctx), true, "on-screen thread preserved across storage merge");
  assert.equal(vm.runInContext("activeId", ctx), idA, "stays on active thread");
  assert.equal(ctx.getState().selectedThread, null, "selectThread not called because on-screen thread kept");

  // When a thread was not on screen, it is removed by another tab's update.
  vm.runInContext("view = 'channel'; activeId = ''; mergeStoredThreads(" + JSON.stringify(tab2WithoutA) + ")", ctx);
  assert.equal(vm.runInContext("findThread(" + JSON.stringify(idA) + ")", ctx), null, "background thread removed");

  // Retaining an on-screen thread into a 12-thread stored list still caps
  // at MAX_THREADS (12) by evicting the oldest idle thread, rather than
  // saving 13 and letting parseThreads slice by position.
  const full12 = Array.from({ length: 12 }, (_, i) => ({
    id: "web-console-" + (i + 1).toString(16).padStart(32, "0"),
    title: "T" + i,
    mark: 1,
    markKnown: true,
    updated: i === 5 ? 1 : 50 + i,
    unread: false,
    started: true,
  }));
  vm.runInContext(
    "addThread(" + JSON.stringify(idA) + ", 'Active', true);" +
    "activeId = " + JSON.stringify(idA) + "; view = 'thread';" +
    "mergeStoredThreads(" + JSON.stringify(JSON.stringify(full12)) + ");",
    ctx
  );
  const afterCap = JSON.parse(vm.runInContext("JSON.stringify(threads.map(t => t.id))", ctx));
  assert.equal(afterCap.length, 12, "mergeStoredThreads must cap at MAX_THREADS");
  assert.ok(afterCap.includes(idA), "active retained thread kept");
  assert.ok(!afterCap.includes(full12[5].id), "oldest idle thread evicted by LRU, not last by position");
  assert.ok(afterCap.includes(full12[11].id), "newest tail thread kept");

  // Two tabs viewing different older threads converge in one merge cycle
  // instead of ping-ponging storage events forever.
  const oldTab1 = "web-console-" + "91".padStart(32, "0");
  const oldTab2 = "web-console-" + "92".padStart(32, "0");
  const tab1Ctx = makeHarness();
  const tab2Ctx = makeHarness();
  const shared11 = Array.from({ length: 11 }, (_, i) => ({
    id: "web-console-" + (i + 20).toString(16).padStart(32, "0"),
    title: "S" + i,
    mark: 1,
    markKnown: true,
    updated: 200 + i,
    viewedAt: 200 + i,
    unread: false,
    started: true,
  }));
  const tab1Init = JSON.stringify([
    { id: oldTab1, title: "Old1", mark: 1, markKnown: true, updated: 1, viewedAt: 1, unread: false, started: true },
    ...shared11,
  ]);
  const tab2Init = JSON.stringify([
    { id: oldTab2, title: "Old2", mark: 1, markKnown: true, updated: 2, viewedAt: 2, unread: false, started: true },
    ...shared11,
  ]);
  vm.runInContext(
    "threads = parseThreads(" + JSON.stringify(tab1Init) + "); activeId = " + JSON.stringify(oldTab1) + "; view = 'thread';",
    tab1Ctx
  );
  vm.runInContext(
    "threads = parseThreads(" + JSON.stringify(tab2Init) + "); activeId = " + JSON.stringify(oldTab2) + "; view = 'thread';",
    tab2Ctx
  );
  // Tab 1 merges Tab 2's storage write; Tab 1 retains oldTab1 with a fresh viewedAt.
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(tab2Init) + ")", tab1Ctx);
  const tab1Saved = tab1Ctx.getState().local.get("kube-agents-web-console-threads");
  assert.ok(tab1Saved, "Tab 1 writes merged list retaining oldTab1");
  // Tab 2 merges Tab 1's write; Tab 2 retains oldTab2 and evicts an idle shared thread, keeping oldTab1.
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(tab1Saved) + ")", tab2Ctx);
  const tab2Saved = tab2Ctx.getState().local.get("kube-agents-web-console-threads");
  assert.ok(tab2Saved, "Tab 2 writes merged list retaining oldTab2 and oldTab1");
  // Tab 1 receives Tab 2's write: both oldTab1 and oldTab2 are present, so Tab 1 does not write again.
  tab1Ctx.getState().local.delete("kube-agents-web-console-threads");
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(tab2Saved) + ")", tab1Ctx);
  assert.equal(
    tab1Ctx.getState().local.has("kube-agents-web-console-threads"),
    false,
    "Two-tab storage merge must converge without endless ping-pong writes"
  );

  // Removing a thread with × in Tab 2 is not undone by Tab 1 having it on screen.
  vm.runInContext("removeThread(" + JSON.stringify(oldTab1) + ")", tab2Ctx);
  const removedList = tab2Ctx.getState().local.get("kube-agents-web-console-removed");
  const afterRemoveThreads = tab2Ctx.getState().local.get("kube-agents-web-console-threads");
  tab1Ctx.getState().local.set("kube-agents-web-console-removed", removedList);
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(afterRemoveThreads) + ")", tab1Ctx);
  assert.equal(
    vm.runInContext("findThread(" + JSON.stringify(oldTab1) + ")", tab1Ctx),
    null,
    "Thread removed in Tab 2 must not be retained by Tab 1 even when on screen"
  );
  assert.notEqual(
    vm.runInContext("activeId", tab1Ctx),
    oldTab1,
    "Tab 1 switches activeId off the removed thread"
  );
  // Deliberately re-adding the thread clears its removed tombstone.
  vm.runInContext("addThread(" + JSON.stringify(oldTab1) + ", 'Reopened', true)", tab2Ctx);
  assert.equal(
    JSON.parse(tab2Ctx.getState().local.get("kube-agents-web-console-removed")).includes(oldTab1),
    false,
    "addThread clears the thread ID from kube-agents-web-console-removed"
  );
}

// 4. settleAfterReply advances mark to replyId when fetchNewMessages fails.
{
  const id = "web-console-" + "d".repeat(32);
  let calls = 0;
  const ctx = makeHarness(async () => {
    calls++;
    if (calls === 1) return { ok: false, status: 502 };
    return { ok: true, status: 200, json: async () => ({ latest_id: 8, messages: [] }) };
  });
  vm.runInContext(
    "const t = addThread(" + JSON.stringify(id) + ", 'test', true);" +
    "t.mark = 4; t.markKnown = true; activeId = t.id; view = 'thread';",
    ctx
  );
  await vm.runInContext("settleAfterReply(t, 'again', 'pods are healthy', 4, 8)", ctx);
  assert.equal(vm.runInContext("t.mark", ctx), 8);
  await vm.runInContext("pollThread(t)", ctx);
  assert.deepEqual(ctx.getState().appended, []);
}

// 5. settleAfterReply handles pre-turn results (including another tab's user
// question while inflight[t.id] is still set), mid-turn kanban wakes with
// intermediate assistant notes on the user's own turn, and post-reply results.
{
  const id = "web-console-" + "e".repeat(32);
  const messages = [
    { id: 5, role: "user", author: "kanban", content: "[kanban] Task t_1 completed.", card: { task_id: "t_1", status: "completed", summary: "done 1" } },
    { id: 6, role: "assistant", author: "agent", content: "pre-turn delegated result" },
    { id: 7, role: "user", author: "user", content: "question from other tab" },
    { id: 8, role: "assistant", author: "agent", content: "reply to other tab" },
    { id: 9, role: "user", author: "user", content: "check cluster" },
    { id: 10, role: "assistant", author: "agent", content: "intermediate note before wake" },
    { id: 11, role: "user", author: "kanban", content: "[kanban] Task t_2 completed.", card: { task_id: "t_2", status: "completed", summary: "done 2" } },
    { id: 12, role: "assistant", author: "agent", content: "first mid-turn wake result" },
    { id: 13, role: "assistant", author: "agent", content: "intermediate note between wakes" },
    { id: 14, role: "user", author: "kanban", content: "[kanban] Task t_3 blocked; needs attention.", card: { task_id: "t_3", status: "blocked; needs attention", summary: "blocked 3" } },
    { id: 15, role: "assistant", author: "agent", content: "second mid-turn wake result" },
    { id: 16, role: "assistant", author: "agent", content: "final turn reply" },
    { id: 17, role: "user", author: "kanban", content: "[kanban] Task t_4 completed.", card: { task_id: "t_4", status: "completed", summary: "done 4" } },
    { id: 18, role: "assistant", author: "agent", content: "post-reply delegated result" },
  ];
  const ctx = makeHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ latest_id: 18, messages }),
  }));
  vm.runInContext(
    "const t = addThread(" + JSON.stringify(id) + ", 'test', true);" +
    "t.mark = 4; t.markKnown = true; activeId = t.id; view = 'thread';" +
    "startInflight(t.id, 'check cluster');",
    ctx
  );
  await vm.runInContext("settleAfterReply(t, 'check cluster', 'final turn reply', 4, 16)", ctx);
  assert.deepEqual(
    ctx.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [
      { author: "kanban", text: "[kanban] Task t_1 completed.", tag: "Specialist report" },
      { author: "agent", text: "pre-turn delegated result", tag: "update" },
      { author: "user", text: "question from other tab", tag: "You" },
      { author: "agent", text: "reply to other tab", tag: "update" },
      { author: "kanban", text: "[kanban] Task t_2 completed.", tag: "Specialist report" },
      { author: "agent", text: "first mid-turn wake result", tag: "update" },
      { author: "kanban", text: "[kanban] Task t_3 blocked; needs attention.", tag: "Specialist report" },
      { author: "agent", text: "second mid-turn wake result", tag: "update" },
      { author: "kanban", text: "[kanban] Task t_4 completed.", tag: "Specialist report" },
      { author: "agent", text: "post-reply delegated result", tag: "update" },
    ]
  );
  assert.equal(vm.runInContext("t.mark", ctx), 18);

  // Re-selecting a thread mid-turn after a kanban wake user row landed in its
  // transcript (id <= mark) still delivers the wake's assistant reply when
  // settleAfterReply runs with turnAt < 0 && replyAt >= 0.
  const idReselect = "web-console-" + "f".repeat(32);
  const ctxReselect = makeHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 14,
      messages: [
        { id: 10, role: "assistant", author: "agent", content: "wake reply after reselect" },
        { id: 11, role: "assistant", author: "agent", content: "intermediate note of user turn" },
        { id: 14, role: "assistant", author: "agent", content: "final turn reply" },
      ],
    }),
  }));
  vm.runInContext(
    "const tr = addThread(" + JSON.stringify(idReselect) + ", 'reselect', true);" +
    "activateThread(tr, { latest_id: 9, messages: [" +
    "  { id: 7, role: 'user', author: 'user', content: 'check cluster' }," +
    "  { id: 9, role: 'user', author: 'kanban', content: '[kanban] Task t_2 completed.' }" +
    "] });",
    ctxReselect
  );
  ctxReselect.getState().appended.length = 0;
  await vm.runInContext("settleAfterReply(tr, 'check cluster', 'final turn reply', 4, 14)", ctxReselect);
  assert.deepEqual(
    ctxReselect.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [{ author: "agent", text: "wake reply after reselect", tag: "update" }]
  );

  // Also cover when a background pollThread marked the kanban row (id: 9)
  // before the user submitted their turn (before_id: 9, turnAt === 0 at id: 10),
  // and the wake's assistant reply (id: 11) lands between id: 10 and id: 14.
  const idPrePolled = "web-console-" + "e".repeat(32);
  let prePollStep = 0;
  const ctxPrePolled = makeHarness(async () => {
    prePollStep++;
    if (prePollStep === 1) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          latest_id: 9,
          messages: [
            { id: 9, role: "user", author: "kanban", content: "[kanban] Task t_3 completed." },
          ],
        }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 14,
        messages: [
          { id: 10, role: "user", author: "user", content: "check" },
          { id: 11, role: "assistant", author: "agent", content: "wake reply" },
          { id: 12, role: "assistant", author: "agent", content: "intermediate note" },
          { id: 14, role: "assistant", author: "agent", content: "final" },
        ],
      }),
    };
  });
  vm.runInContext(
    "const tp = addThread(" + JSON.stringify(idPrePolled) + ", 'prepolled', true);" +
    "activateThread(tp, { latest_id: 8, messages: [] });",
    ctxPrePolled
  );
  await vm.runInContext("pollThread(tp)", ctxPrePolled);
  ctxPrePolled.getState().appended.length = 0;
  await vm.runInContext("settleAfterReply(tp, 'check', 'final', 9, 14)", ctxPrePolled);
  assert.deepEqual(
    ctxPrePolled.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [{ author: "agent", text: "wake reply", tag: "update" }]
  );

  // When a kanban wake user row lands inside midTurnSlice after any earlier wake
  // reply (or when midTurnSlice is empty after a pre-polled wake) and the wake's
  // assistant reply has not yet been written when Turn 1 settles, t.wakeOpen must
  // remain true across Turn 1 and deliver the wake's reply in Turn 2's midTurnSlice.
  const idCarry = "web-console-" + "c".repeat(32);
  let carryStep = 0;
  const ctxCarry = makeHarness(async () => {
    carryStep++;
    if (carryStep === 1) {
      // Turn 1 settleAfterReply: midTurnSlice has [11 user kanban] with no reply yet,
      // and post-reply slice is empty.
      return {
        ok: true,
        status: 200,
        json: async () => ({
          latest_id: 12,
          messages: [
            { id: 10, role: "user", author: "user", content: "turn 1" },
            { id: 11, role: "user", author: "kanban", content: "[kanban] Task t_5 completed." },
            { id: 12, role: "assistant", author: "agent", content: "turn 1 reply" },
          ],
        }),
      };
    }
    // Turn 2 settleAfterReply: the wake's assistant reply (id: 14) lands inside
    // Turn 2's midTurnSlice before Turn 2's own reply (id: 15).
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 15,
        messages: [
          { id: 13, role: "user", author: "user", content: "turn 2" },
          { id: 14, role: "assistant", author: "agent", content: "delayed wake reply from turn 1" },
          { id: 15, role: "assistant", author: "agent", content: "turn 2 reply" },
        ],
      }),
    };
  });
  vm.runInContext(
    "const tc = addThread(" + JSON.stringify(idCarry) + ", 'carry', true);" +
    "activateThread(tc, { latest_id: 9, messages: [] });",
    ctxCarry
  );
  ctxCarry.getState().appended.length = 0;
  await vm.runInContext("settleAfterReply(tc, 'turn 1', 'turn 1 reply', 9, 12)", ctxCarry);
  assert.equal(vm.runInContext("tc.wakeOpen", ctxCarry), true, "wakeOpen must stay true when midTurnSlice ends in an unanswered kanban wake");
  assert.deepEqual(
    ctxCarry.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [{ author: "kanban", text: "[kanban] Task t_5 completed.", tag: "Specialist report" }]
  );
  ctxCarry.getState().appended.length = 0;
  await vm.runInContext("settleAfterReply(tc, 'turn 2', 'turn 2 reply', 12, 15)", ctxCarry);
  assert.equal(vm.runInContext("tc.wakeOpen", ctxCarry), false, "wakeOpen closes once the wake reply lands in Turn 2");
  assert.deepEqual(
    ctxCarry.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [{ author: "agent", text: "delayed wake reply from turn 1", tag: "update" }]
  );
}

// 6. loadSeen and updateChannel: first-visit seeding across both channels even
// when one channel fails on first poll and the page reloads before it succeeds.
{
  const ctx1 = makeHarness();
  vm.runInContext("loadSeen(null)", ctx1);
  // Only alerts succeeds before reload.
  vm.runInContext("updateChannel('alerts', [{ id: 'k8s-evt-1', message_count: 2 }], true, '')", ctx1);
  const storedSeen = ctx1.getState().local.get("kube-agents-web-console-seen");
  assert.ok(storedSeen, "seen map persisted after alerts seeded");

  // Reload in a new context using storedSeen; scheduled must still seed on its first poll!
  const ctx2 = makeHarness();
  vm.runInContext("loadSeen(" + JSON.stringify(storedSeen) + ")", ctx2);
  vm.runInContext("updateChannel('scheduled', [{ id: 'cron-a-20261012', message_count: 4 }], false, '')", ctx2);
  assert.equal(vm.runInContext("channelUnread.scheduled", ctx2), 0, "scheduled must be seeded on first successful poll after partial reload");
  // A subsequent message on cron-a-20261012 makes it unread and updates pageTitle.
  vm.runInContext("updateChannel('scheduled', [{ id: 'cron-a-20261012', message_count: 5 }], false, '')", ctx2);
  assert.equal(vm.runInContext("channelUnread.scheduled", ctx2), 1);
  assert.equal(vm.runInContext("pageTitle()", ctx2), "(1) kube-agents");
}

// 7. postHeadline for empty triage, empty scheduled, and started sessions.
{
  const ctx = makeHarness();
  const emptyAlert = JSON.parse(vm.runInContext("JSON.stringify(postHeadline({ id: 'k8s-evt-1', kind: 'event_triage', message_count: 0 }))", ctx));
  assert.deepEqual(emptyAlert, {
    text: "Event k8s-evt-1: the agent has not started on this yet",
    muted: true,
    started: false,
  });
  const emptySched = JSON.parse(vm.runInContext("JSON.stringify(postHeadline({ id: 'cron-sweep-20261012', kind: 'scheduled', message_count: 1, summary: { replies: 0 } }))", ctx));
  assert.deepEqual(emptySched, {
    text: "Check cron-sweep-20261012: the agent has not started on this yet",
    muted: true,
    started: false,
  });
  const started = JSON.parse(vm.runInContext("JSON.stringify(postHeadline({ id: 'k8s-evt-1', kind: 'event_triage', message_count: 3, summary: { subject: 'Triage Pod web-1', replies: 1 } }))", ctx));
  assert.deepEqual(started, {
    text: "Triage Pod web-1",
    muted: false,
    started: true,
  });
}

// 8. startInflight, setStatus, addStep, appendDelta, and formatStatusWithTimer.
{
  const ctx = makeHarness();
  vm.runInContext(
    "const turn = startInflight('s1', 'hi');" +
    "turn.startedAt = 100; turn.lastEventAt = 100;" +
    "setStatus('s1', 'The agent started working');" +
    "turn.lastEventAt = 100;",
    ctx
  );
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 102)", ctx), "The agent started working · 2 s");
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 104)", ctx), "Waiting for the model · 4 s");

  // Running tool step stays on Running even after 3s, and a Thinking status while
  // a tool step is still running also does not switch to Waiting for the model (!runningStep guard).
  vm.runInContext(
    "addStep('s1', { id: 'kubectl_get#1', kind: 'tool', title: 'Running kubectl_get', detail: 'pods', state: 'running' });" +
    "setStatus('s1', 'Running kubectl_get: pods');" +
    "inflight.s1.lastEventAt = 104;",
    ctx
  );
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 110)", ctx), "Running kubectl_get: pods · 10 s");
  vm.runInContext("setStatus('s1', 'Thinking: checking pods'); inflight.s1.lastEventAt = 104;", ctx);
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 110)", ctx), "Thinking: checking pods · 10 s");

  // Failed tool step transitions to Waiting for the model after 3s.
  vm.runInContext(
    "addStep('s1', { id: 'kubectl_get#1', kind: 'tool', title: 'kubectl_get failed', state: 'failed' });" +
    "setStatus('s1', 'kubectl_get failed');" +
    "inflight.s1.lastEventAt = 110;",
    ctx
  );
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 111)", ctx), "kubectl_get failed · 11 s");
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 114)", ctx), "Waiting for the model · 14 s");

  // Thinking status also transitions to Waiting for the model after 3s once no tool step is running.
  vm.runInContext(
    "setStatus('s1', 'Thinking: check events');" +
    "inflight.s1.lastEventAt = 114;",
    ctx
  );
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 118)", ctx), "Waiting for the model · 18 s");

  // Once deltaText is streaming, neither Writing reply… nor a late Thinking status switches to Waiting for the model (!turn.deltaText guard).
  vm.runInContext(
    "setStatus('s1', 'Writing reply…'); appendDelta('s1', 'All '); appendDelta('s1', 'healthy.');" +
    "inflight.s1.lastEventAt = 118;",
    ctx
  );
  assert.equal(vm.runInContext("inflight.s1.deltaText", ctx), "All healthy.");
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 125)", ctx), "Writing reply… · 25 s");
  vm.runInContext("setStatus('s1', 'Thinking: late'); inflight.s1.lastEventAt = 118;", ctx);
  assert.equal(vm.runInContext("formatStatusWithTimer(inflight.s1, 125)", ctx), "Thinking: late · 25 s");
}

// 9. matchTurnMetaIndex and activateThread attach step summary only to the turn's own reply row.
{
  const ctx = makeHarness();
  const msgs = [
    { id: 1, role: "user", content: "hi" },
    { id: 2, role: "assistant", content: "turn reply" },
    { id: 3, role: "user", author: "kanban", content: "[kanban] Task t_1 completed." },
    { id: 4, role: "assistant", content: "later background wake reply" },
  ];
  assert.equal(
    vm.runInContext("matchTurnMetaIndex(" + JSON.stringify(msgs) + ", { replyId: 2, reply: 'turn reply' })", ctx),
    1
  );
  assert.equal(
    vm.runInContext("matchTurnMetaIndex(" + JSON.stringify(msgs) + ", { reply: 'turn reply' })", ctx),
    1
  );
  const tid = "web-console-" + "8".repeat(32);
  const meta = {
    steps: [{ id: "kubectl_get#1", state: "done", title: "Finished kubectl_get", detail: "pods" }],
    elapsed: 5,
    reply: "turn reply",
    replyId: 2,
  };
  vm.runInContext(
    "const tm = addThread(" + JSON.stringify(tid) + ", 'meta test', true);" +
    "lastTurnMeta[tm.id] = " + JSON.stringify(meta) + ";" +
    "activateThread(tm, { latest_id: 4, messages: " + JSON.stringify(msgs) + " });",
    ctx
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(ctx.getState().appended.map(m => m.meta))),
    [null, meta, null, null],
    "activateThread restores lastTurnMeta on the matching reply row"
  );
}

// 10. renderMarkdown and renderAuthorBody for event_watcher, scheduler, kanban, and agent with steps.
{
  const ctx = makeHarness();
  const fenced = vm.runInContext("renderMarkdown('Before\\n\\x60\\x60\\x60yaml\\nkind: Pod\\n\\x60\\x60\\x60\\nAfter')", ctx);
  assert.equal(fenced, "<p>Before</p><pre><code>kind: Pod</code></pre><p>After</p>");
  const inline = vm.runInContext("renderMarkdown('Use \\x60\\x60\\x60yaml on its own line, not \\x60\\x60\\x60inline\\x60\\x60\\x60 here.')", ctx);
  assert.ok(!inline.includes("<pre>"), "mid-line backticks must not open a <pre> block: " + inline);
  assert.equal(
    vm.runInContext("renderMarkdown('1. step\\n   \\x60\\x60\\x60yaml\\n   a: 1\\n   \\x60\\x60\\x60')", ctx),
    "<p>1. step</p><pre><code>a: 1</code></pre>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('\\x60\\x60\\x60\\x60md\\n\\x60\\x60\\x60yaml\\nx: 1\\n\\x60\\x60\\x60\\n\\x60\\x60\\x60\\x60')", ctx),
    "<pre><code>\x60\x60\x60yaml\nx: 1\n\x60\x60\x60</code></pre>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('~~~sh\\necho hi\\n~~~')", ctx),
    "<pre><code>echo hi</code></pre>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('glob \\x60a/**/b\\x60 and **bold \\x60x*y\\x60**')", ctx),
    "<p>glob <code>a/**/b</code> and <strong>bold <code>x*y</code></strong></p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('2 ** 3 * 4 = 24')", ctx),
    "<p>2 ** 3 * 4 = 24</p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('\\x60\\x60 \\x60foo\\x60 \\x60\\x60 and \\x60a\\x60\\x60b\\x60')", ctx),
    "<p><code>\x60foo\x60</code> and <code>a\x60\x60b</code></p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('\\x60\\x60\\x60a\\x60b\\n\\x60\\x60\\x60\\ncode\\n\\x60\\x60\\x60\\nAfter')", ctx),
    "<p>\x60\x60\x60a\x60b</p><pre><code>code</code></pre><p>After</p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('Use \\x60kubectl\\nget pods\\x60 now.')", ctx),
    "<p>Use <code>kubectl get pods</code> now.</p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('**All pods\\nhealthy**\\n\\n**unclosed\\n\\nnext')", ctx),
    "<p><strong>All pods<br>healthy</strong></p><p>**unclosed</p><p>next</p>"
  );
  assert.equal(
    vm.runInContext("renderMarkdown('***critical alert*** and **bold**')", ctx),
    "<p><em><strong>critical alert</strong></em> and <strong>bold</strong></p>"
  );

  const alertHtml = vm.runInContext("renderAuthorBody('event_watcher', 'raw prompt', { subject: 'Triage Pod web-1', resource: 'default/Pod/web-1', reason: 'BackOff', warning: 'CrashLoopBackOff' })", ctx);
  assert.ok(alertHtml.includes("msg-card-alert") && alertHtml.includes("default/Pod/web-1") && alertHtml.includes("Show routing prompt"));

  const schedHtml = vm.runInContext("renderAuthorBody('scheduler', 'sweep prompt', { subject: 'Run policy sweep' })", ctx);
  assert.ok(schedHtml.includes("msg-card-cron") && schedHtml.includes("Run policy sweep") && schedHtml.includes("Show scheduled prompt"));

  const kanbanHtml = vm.runInContext("renderAuthorBody('kanban', 'wake text', { task_id: 't_99', status: 'blocked; needs attention', title: 'Investigate PVC', summary: 'StorageClass missing' })", ctx);
  assert.ok(kanbanHtml.includes("msg-card-kanban") && kanbanHtml.includes("t_99") && kanbanHtml.includes("blocked; needs attention") && kanbanHtml.includes("StorageClass missing"));

  const agentHtml = vm.runInContext("renderAuthorBody('agent', 'All good.', null, { elapsed: 4, steps: [{ id: 'kubectl_get#1', state: 'done', title: 'Finished kubectl_get', detail: 'pods' }] })", ctx);
  assert.ok(agentHtml.includes("Worked for 4 s &middot; 1 step") && agentHtml.includes("Finished kubectl_get") && agentHtml.includes("<p>All good.</p>"));
}

// 11. renderThreads template signature guard and onEnter IME composition guard.
{
  const ctx = makeHarness();
  let writes = 0;
  let storedInner = "";
  const listEl = {
    get innerHTML() {
      // Simulate browser HTML serializer canonicalizing &times; to literal ×.
      return storedInner.replace(/&times;/g, "\u00d7");
    },
    set innerHTML(v) {
      writes++;
      storedInner = v;
    },
    contains: () => false,
    querySelector: () => null,
  };
  ctx.document.getElementById = id => (id === "thread-list" ? listEl : {});
  ctx.CSS = { escape: s => s };
  const tid = "web-console-" + "7".repeat(32);
  vm.runInContext("addThread(" + JSON.stringify(tid) + ", 'Chat 1', true); renderThreads();", ctx);
  assert.equal(writes, 1, "first renderThreads writes innerHTML");
  vm.runInContext("renderThreads();", ctx);
  assert.equal(writes, 1, "second renderThreads with unchanged template must skip DOM rewrite even when list.innerHTML canonicalized &times;");

  let sent = 0;
  let listener = null;
  const fakeInput = { addEventListener: (_, fn) => { listener = fn; } };
  ctx.fakeInput = fakeInput;
  ctx.onSend = () => { sent++; };
  vm.runInContext("onEnter(fakeInput, onSend)", ctx);
  listener({ key: "Enter", shiftKey: false, isComposing: true, keyCode: 13, preventDefault() {} });
  listener({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229, preventDefault() {} });
  assert.equal(sent, 0, "IME composition Enter must not submit");
  listener({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 13, preventDefault() {} });
  assert.equal(sent, 1, "plain Enter submits once");
}

// 12. reconcileTurnErrorMark advances mark past the user row on stream error so
// pollThread does not duplicate the user prompt and shows the background reply.
{
  const idErr = "web-console-" + "9".repeat(32);
  let pollStep = 0;
  const ctxErr = makeHarness(async () => {
    pollStep++;
    if (pollStep === 1) {
      // During reconcileTurnErrorMark: user row is in state.db, reply not yet written.
      return {
        ok: true,
        status: 200,
        json: async () => ({
          latest_id: 11,
          messages: [
            { id: 11, role: "user", author: "user", content: "broken stream prompt" },
          ],
        }),
      };
    }
    // Next pollThread after background turn completes on server.
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 12,
        messages: [
          { id: 12, role: "assistant", author: "agent", content: "background finished reply" },
        ],
      }),
    };
  });
  vm.runInContext(
    "const te = addThread(" + JSON.stringify(idErr) + ", '', true);" +
    "activateThread(te, { latest_id: 10, messages: [] });",
    ctxErr
  );
  ctxErr.getState().appended.length = 0;
  await vm.runInContext("reconcileTurnErrorMark(te, 'broken stream prompt')", ctxErr);
  assert.equal(vm.runInContext("te.mark", ctxErr), 11, "mark must advance to the user row id");
  await vm.runInContext("pollThread(te)", ctxErr);
  assert.deepEqual(
    ctxErr.getState().appended.map(m => ({ author: m.author, text: m.text, tag: m.tag })),
    [{ author: "agent", text: "background finished reply", tag: "update" }],
    "pollThread after stream error must show only the finished reply, never the user prompt a second time"
  );
}

// 13. busyUntil blocks removeThread, updateComposer, and submitMessage across tabs,
// survives a 409 turn_in_progress race, clears on reload once an assistant reply
// arrives, and mergeStoredThreads never resurrects a removed thread in localStorage.
{
  const idBusy = "web-console-" + "a".repeat(32);
  const tabA = makeHarness();
  let tabBFetches = 0;
  const tabB = makeHarness(async () => {
    tabBFetches++;
    return { ok: false, status: 503 };
  });
  const btnSendB = { disabled: false };
  const chipB = { disabled: false };
  tabB.document.getElementById = id => (id === "btn-send" ? btnSendB : {});
  tabB.document.querySelectorAll = sel => (sel === ".chip" ? [chipB] : []);

  vm.runInContext(
    "const tb = addThread(" + JSON.stringify(idBusy) + ", 'Busy chat', true);" +
    "tb.busyUntil = nowSeconds() + 900; startInflight(tb.id, 'hi'); saveThreads();",
    tabA
  );
  const storedBusy = tabA.getState().local.get("kube-agents-web-console-threads");
  vm.runInContext(
    "mergeStoredThreads(" + JSON.stringify(storedBusy) + ");" +
    "activeId = " + JSON.stringify(idBusy) + "; view = 'thread'; updateComposer();",
    tabB
  );
  assert.equal(btnSendB.disabled, true, "updateComposer must disable #btn-send in Tab B when busyUntil is active");
  assert.equal(chipB.disabled, true, "updateComposer must disable .chip in Tab B when busyUntil is active");
  assert.equal(
    vm.runInContext("removeThread(" + JSON.stringify(idBusy) + ")", tabB),
    false,
    "Tab B must refuse removeThread while Tab A's turn has busyUntil set"
  );
  await vm.runInContext("submitMessage('second prompt from tab B')", tabB);
  assert.equal(tabBFetches, 0, "submitMessage must refuse to send while isThreadBusy(t) is true");
  assert.ok(
    vm.runInContext("findThread(" + JSON.stringify(idBusy) + ").busyUntil > nowSeconds()", tabB),
    "blocked submitMessage must not clear busyUntil"
  );

  // Race: Tab B starts submitMessage before receiving Tab A's busyUntil, gets HTTP 409
  // turn_in_progress while merging Tab A's busyUntil, and must not zero busyUntil in finally.
  const tabRace = makeHarness(async () => {
    // Simulate Tab A's storage event landing while Tab B's request is in flight.
    vm.runInContext("mergeStoredThreads(" + JSON.stringify(storedBusy) + ")", tabRace);
    const body = "event: error\ndata: " + JSON.stringify({ error: "turn_in_progress", message: "A turn is already running." }) + "\n\n";
    let readDone = false;
    return {
      ok: true,
      status: 200,
      headers: { get: h => (h.toLowerCase() === "content-type" ? "text/event-stream" : "") },
      body: {
        getReader: () => ({
          read: async () => {
            if (readDone) return { done: true, value: undefined };
            readDone = true;
            return { done: false, value: new TextEncoder().encode(body) };
          },
        }),
      },
    };
  });
  tabRace.TextEncoder = TextEncoder;
  vm.runInContext(
    "const tr = addThread(" + JSON.stringify(idBusy) + ", 'Busy chat', true);" +
    "tr.mark = 1; tr.markKnown = true; activeId = tr.id; view = 'thread';",
    tabRace
  );
  await vm.runInContext("submitMessage('racing prompt')", tabRace);
  assert.ok(
    vm.runInContext("findThread(" + JSON.stringify(idBusy) + ").busyUntil > nowSeconds()", tabRace),
    "submitMessage finally must not clear busyUntil when server returns turn_in_progress"
  );

  // And if storage ever carries busyUntil: 0 while Tab A still has inflight[idBusy],
  // Tab A's mergeStoredThreads preserves its higher busyUntil and re-saves it to localStorage.
  const staleZeroBusy = JSON.stringify([
    { id: idBusy, title: "Busy chat", mark: 1, markKnown: true, updated: 100, busyUntil: 0, unread: false, started: true },
  ]);
  tabA.getState().local.set("kube-agents-web-console-threads", staleZeroBusy);
  vm.runInContext("mergeStoredThreads(" + JSON.stringify(staleZeroBusy) + ")", tabA);
  const resavedByA = JSON.parse(tabA.getState().local.get("kube-agents-web-console-threads"));
  assert.ok(resavedByA[0].busyUntil > 0, "Tab A must re-save its active busyUntil when storage had busyUntil: 0");

  // Reloading mid-turn leaves busyUntil in localStorage with no local inflight entry.
  // Once the assistant reply arrives via pollThread or activateThread, busyUntil is cleared.
  const tabReloadPoll = makeHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 5,
      messages: [{ id: 5, role: "assistant", author: "agent", content: "turn finished after reload" }],
    }),
  }));
  vm.runInContext(
    "mergeStoredThreads(" + JSON.stringify(storedBusy) + ");" +
    "const tp = findThread(" + JSON.stringify(idBusy) + "); tp.mark = 4; tp.markKnown = true;" +
    "activeId = tp.id; view = 'thread';",
    tabReloadPoll
  );
  assert.ok(vm.runInContext("isThreadBusy(findThread(" + JSON.stringify(idBusy) + "))", tabReloadPoll));
  await vm.runInContext("pollThread(findThread(" + JSON.stringify(idBusy) + "))", tabReloadPoll);
  assert.equal(
    vm.runInContext("findThread(" + JSON.stringify(idBusy) + ").busyUntil", tabReloadPoll),
    0,
    "showUpdates must clear busyUntil once the assistant reply arrives after a mid-turn reload"
  );

  const tabReloadActivate = makeHarness();
  vm.runInContext(
    "mergeStoredThreads(" + JSON.stringify(storedBusy) + ");" +
    "const ta = findThread(" + JSON.stringify(idBusy) + ");" +
    "activateThread(ta, { latest_id: 6, messages: [" +
    "  { id: 5, role: 'user', author: 'user', content: 'hi' }," +
    "  { id: 6, role: 'assistant', author: 'agent', content: 'done' }" +
    "] });",
    tabReloadActivate
  );
  assert.equal(
    vm.runInContext("findThread(" + JSON.stringify(idBusy) + ").busyUntil", tabReloadActivate),
    0,
    "activateThread must clear busyUntil when the transcript ends with an assistant reply"
  );

  // Even if another tab writes idBusy to REMOVED_KEY and clears THREADS_KEY while
  // Tab A has inflight[idBusy], Tab A keeps its in-memory turn without re-adding
  // idBusy to localStorage THREADS_KEY or deleting it from REMOVED_KEY.
  tabA.getState().local.set("kube-agents-web-console-removed", JSON.stringify([idBusy]));
  tabA.getState().local.set("kube-agents-web-console-threads", "[]");
  vm.runInContext("mergeStoredThreads('[]')", tabA);
  assert.ok(
    vm.runInContext("!!findThread(" + JSON.stringify(idBusy) + ")", tabA),
    "Tab A keeps in-flight thread in memory"
  );
  assert.equal(
    tabA.getState().local.get("kube-agents-web-console-threads"),
    "[]",
    "Tab A must not write a removed thread back to localStorage THREADS_KEY"
  );
  assert.equal(
    tabA.getState().local.get("kube-agents-web-console-removed"),
    JSON.stringify([idBusy]),
    "Tab A must not delete idBusy from REMOVED_KEY"
  );
}

// 14. Switching posts or threads clears unsent composer drafts (#side-input and #prompt-input).
{
  const ctx = makeHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ latest_id: 1, messages: [] }),
  }));
  const els = new Map();
  const getEl = id => {
    if (!els.has(id)) {
      els.set(id, {
        value: "",
        textContent: "",
        innerHTML: "",
        hidden: false,
        disabled: false,
        style: {},
        classList: { add() {}, remove() {}, toggle() {} },
        focus() {},
        querySelector: () => null,
        appendChild() {},
        replaceChildren() {},
        content: { cloneNode: () => ({}) },
        scrollTop: 0,
        scrollHeight: 0,
      });
    }
    return els.get(id);
  };
  ctx.document.getElementById = getEl;
  ctx.document.querySelectorAll = () => [];
  ctx.document.createElement = () => ({ appendChild() {}, classList: { add() {}, remove() {} } });
  ctx.CHANNELS = {
    alerts: { title: "# alerts", sub: "", empty: "" },
    scheduled: { title: "# scheduled", sub: "", empty: "" },
  };
  ctx.VIEW_KEY = "kube-agents-web-console-view";
  ctx.ACTIVE_KEY = "kube-agents-web-console-active";
  ctx.chatPlatform = "";
  ctx.postSubject = p => p.id;
  ctx.messageRow = () => ({});
  ctx.renderRail = () => {};
  ctx.renderFeed = () => {};
  ctx.updateSideComposer = () => {};
  ctx.showIntro = () => {};
  ctx.showView = name => { vm.runInContext("view = " + JSON.stringify(name), ctx); };
  vm.runInContext(paneSource, ctx);

  // Opening a post, typing a draft in #side-input, and switching to another post clears #side-input.
  vm.runInContext("channelPosts.alerts = [{ id: 'k8s-evt-1', kind: 'event_triage' }, { id: 'k8s-evt-2', kind: 'event_triage' }];", ctx);
  await vm.runInContext("openPost('k8s-evt-1')", ctx);
  getEl("side-input").value = "draft for evt-1";
  await vm.runInContext("openPost('k8s-evt-1')", ctx);
  assert.equal(getEl("side-input").value, "draft for evt-1", "re-clicking the same post preserves the draft");
  await vm.runInContext("openPost('k8s-evt-2')", ctx);
  assert.equal(getEl("side-input").value, "", "switching to another post must clear #side-input");

  getEl("side-input").value = "draft before close";
  vm.runInContext("closeSide()", ctx);
  assert.equal(getEl("side-input").value, "", "closing the side pane must clear #side-input");

  await vm.runInContext("openPost('k8s-evt-1')", ctx);
  getEl("side-input").value = "draft before about";
  vm.runInContext("openAbout()", ctx);
  assert.equal(getEl("side-input").value, "", "switching the side pane to About must clear #side-input");

  // Switching threads or clicking + New chat clears #prompt-input.
  const t1 = "web-console-" + "1".repeat(32);
  const t2 = "web-console-" + "2".repeat(32);
  vm.runInContext(
    "addThread(" + JSON.stringify(t1) + ", 'T1', true);" +
    "addThread(" + JSON.stringify(t2) + ", 'T2', true);",
    ctx
  );
  await vm.runInContext("selectThread(" + JSON.stringify(t1) + ")", ctx);
  getEl("prompt-input").value = "unsent draft on T1";
  await vm.runInContext("selectThread(" + JSON.stringify(t1) + ")", ctx);
  assert.equal(getEl("prompt-input").value, "unsent draft on T1", "re-selecting the same active thread preserves the draft");
  await vm.runInContext("selectThread(" + JSON.stringify(t2) + ")", ctx);
  assert.equal(getEl("prompt-input").value, "", "switching to another thread must clear #prompt-input");

  getEl("prompt-input").value = "unsent draft before newChat";
  vm.runInContext("newChat()", ctx);
  assert.equal(getEl("prompt-input").value, "", "newChat() must clear #prompt-input");
}
`)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("node page harness failed: %v\n%s", err, out)
	}
}
