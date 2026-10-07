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
	streamGate chan struct{}
	streamTail string
	streamEnd  string
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
		known := f.sessions[sid]
		f.chats = append(f.chats, body)
		delay, code, reply := f.chatDelay, f.chatCode, f.reply
		f.mu.Unlock()
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

func chatReq(body string) *http.Request {
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
	_, chats := fake.state()
	if len(chats) != 1 || chats[0]["message"] != "check pods" {
		t.Fatalf("upstream chat body = %v, want {\"message\": \"check pods\"}", chats)
	}
	if _, has := chats[0]["content"]; has {
		t.Errorf("upstream chat body still sends a content field: %v", chats[0])
	}
}

func TestEachNewChatGetsItsOwnSession(t *testing.T) {
	_, h := setup(t)
	a := decode[chatResponse](t, serve(h, chatReq(`{"message":"one"}`)))
	b := decode[chatResponse](t, serve(h, chatReq(`{"message":"two"}`)))
	if a.SessionID == b.SessionID {
		t.Fatalf("two browsers without a session were handed the same session %q", a.SessionID)
	}
	c := decode[chatResponse](t, serve(h, chatReq(`{"message":"three","session_id":"`+a.SessionID+`"}`)))
	if c.SessionID != a.SessionID {
		t.Errorf("a named console session was not reused: got %q want %q", c.SessionID, a.SessionID)
	}
}

func TestChatRefusesForeignSessionIDs(t *testing.T) {
	fake, h := setup(t)
	for _, sid := range []string{"web-console-../../x", "web-console-ABC", "../api/sessions"} {
		body, _ := json.Marshal(chatRequest{Message: "hi", SessionID: sid})
		rec := serve(h, chatReq(string(body)))
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
	rec := serve(h, chatReq(`{"message":"hi","session_id":"`+sid+`"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %s", rec.Code, rec.Body.String())
	}
	if sessions, chats := fake.state(); !sessions[sid] || len(chats) != 2 {
		t.Errorf("want the session recreated and the turn retried once; sessions=%v chats=%d", sessions, len(chats))
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
	fake.mu.Lock()
	fake.chatDelay = 200 * time.Millisecond
	fake.mu.Unlock()

	body := `{"message":"x","session_id":"` + first.SessionID + `"}`
	done := make(chan int)
	go func() { done <- serve(h, chatReq(body)).Code }()
	time.Sleep(50 * time.Millisecond)
	if code := serve(h, chatReq(body)).Code; code != http.StatusConflict {
		t.Errorf("second turn while one is in flight: status %d, want 409", code)
	}
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
	toggle = toggle[:strings.Index(toggle, "\n    }\n")]
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
	start := strings.Index(page, "    "+selector+" {")
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
