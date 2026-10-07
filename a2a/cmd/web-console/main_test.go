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
	chats     []map[string]any
	reply     string
	chatDelay time.Duration
	chatCode  int
}

func newFakeHermes() *fakeHermes {
	return &fakeHermes{sessions: map[string]bool{}, messages: map[string][]map[string]any{}, reply: "pods are healthy"}
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
		data := []map[string]any{{"id": "k8s-evt-abc", "title": "Triage k8s-evt-abc", "source": "api_server", "preview": "secret text"}}
		for id := range f.sessions {
			data = append(data, map[string]any{"id": id, "title": "Web console", "source": "api_server"})
		}
		f.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "data": data})
	case r.Method == http.MethodPost && r.URL.Path == "/api/sessions":
		var body map[string]string
		_ = json.NewDecoder(r.Body).Decode(&body)
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.sessions[body["session_id"]] {
			hermesError(w, http.StatusConflict, "Session already exists")
			return
		}
		f.sessions[body["session_id"]] = true
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "hermes.session", "session": map[string]any{"id": body["session_id"]}})
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
		if !f.sessions[sid] {
			hermesError(w, http.StatusNotFound, "Session not found: "+sid)
			return
		}
		// Same paging as Hermes with order=latest: offset counts back from
		// the newest message, and the page comes back oldest first.
		all := f.messages[sid]
		limit, offset := len(all), 0
		if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil {
			limit = v
		}
		if v, err := strconv.Atoi(r.URL.Query().Get("offset")); err == nil {
			offset = v
		}
		end := max(len(all)-offset, 0)
		page := all[max(end-limit, 0):end]
		_ = json.NewEncoder(w).Encode(map[string]any{"object": "list", "session_id": sid, "data": page})
	default:
		hermesError(w, http.StatusNotFound, "no route")
	}
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
	fake := newFakeHermes()
	upstream := httptest.NewServer(fake)
	t.Cleanup(upstream.Close)
	cfg := config{
		HermesURL:    upstream.URL,
		APIServerKey: testKey,
		ClusterName:  "c1",
	}
	return fake, newServer(cfg).routes()
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
	if got.BeforeID != 0 || got.ReplyID != 4 {
		t.Errorf("turn IDs = (before=%d, reply=%d), want (0, 4)", got.BeforeID, got.ReplyID)
	}
	second := decode[chatResponse](t, serve(h, chatReq(`{"message":"again","session_id":"`+got.SessionID+`"}`)))
	if second.BeforeID != 4 || second.ReplyID != 8 {
		t.Errorf("second turn IDs = (before=%d, reply=%d), want (4, 8)", second.BeforeID, second.ReplyID)
	}
	_, chats := fake.state()
	if len(chats) != 2 || chats[0]["message"] != "check pods" {
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
	for _, sid := range []string{"k8s-evt-abc", "web-console-../../x", "web-console-ABC", "../api/sessions"} {
		body, _ := json.Marshal(chatRequest{Message: "hi", SessionID: sid})
		rec := serve(h, chatReq(string(body)))
		if rec.Code != http.StatusBadRequest {
			t.Errorf("session %q: status %d, want 400", sid, rec.Code)
		}
	}
	if _, chats := fake.state(); len(chats) != 0 {
		t.Errorf("a refused session still reached Hermes: %v", chats)
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
	for _, path := range []string{"/", "/api/status", "/api/sessions/recent", "/api/sessions/" + sessionIDPrefix + strings.Repeat("a", 32) + "/messages"} {
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
	if !marked[mine.SessionID] || marked["k8s-evt-abc"] {
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
	for _, sid := range []string{"k8s-evt-abc", sessionIDPrefix + "xyz", sessionIDPrefix + strings.Repeat("A", 32)} {
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
	for _, rule := range []string{"grid-template-rows: minmax(0, 1fr);", "min-height: 0;"} {
		if !strings.Contains(string(page), rule) {
			t.Errorf("index.html is missing %q", rule)
		}
	}
}

// TestPageSettleAndWakeLogic executes the embedded script from static/index.html
// in Node's vm module against stubbed DOM and fetch globals, verifying:
//  1. A failed settle fetch still advances the mark to replyId so the next
//     poll does not replay the turn's own assistant reply as an "update".
//  2. Results that landed before the turn, a card wake that landed mid-turn
//     (followed by an intermediate assistant row of the typed turn), and
//     results that landed after the reply are each shown once while the typed
//     turn's own assistant messages are not duplicated.
func TestPageSettleAndWakeLogic(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node is not in PATH")
	}
	cmd := exec.Command(node, "--input-type=module", "-e", `
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync("static/index.html", "utf8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(match, "static/index.html has no <script> block");
const scriptSource = match[1];

function makeContext(fetchImpl, initialStorage = {}) {
  const store = new Map(Object.entries(initialStorage));
  const appended = [];
  const makeEl = () => ({
    className: "",
    innerHTML: "",
    innerText: "",
    value: "",
    title: "",
    hidden: false,
    disabled: false,
    scrollTop: 0,
    scrollHeight: 0,
    dataset: {},
    addEventListener() {},
    appendChild() {},
    remove() {},
  });
  const ctx = {
    console,
    Date,
    Number,
    String,
    JSON,
    encodeURIComponent,
    setInterval() {},
    sessionStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    document: {
      getElementById: () => makeEl(),
      createElement: () => makeEl(),
      querySelectorAll: () => [],
    },
    fetch: fetchImpl,
    appended,
    store,
  };
  vm.createContext(ctx);
  vm.runInContext(scriptSource + "\n;appendMessage = (role, text, tag) => { appended.push({role, text, tag}); return { remove() {} }; };", ctx);
  return ctx;
}

// Case 1: failed settle fetch advances lastSeenId to replyId so the next poll
// does not replay the turn's own assistant reply as an "update".
{
  let pollCalls = [];
  const ctx = makeContext(async (url) => {
    if (!String(url).includes("/messages")) {
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    }
    pollCalls.push(String(url));
    if (pollCalls.length === 1) {
      return { ok: false, status: 502, text: async () => "bad gateway" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ latest_id: 8, messages: [] }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  pollCalls = [];
  await vm.runInContext("settleAfterReply('again', 'pods are healthy', 4, 8)", ctx);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "8", "failed settle must advance mark to replyId");
  await vm.runInContext("pollMessages()", ctx);
  assert.ok(pollCalls[1].endsWith("?after=8"), "next poll must request ?after=8, got " + pollCalls[1]);
  assert.deepEqual(ctx.appended, [], "turn reply must not be replayed as an update");
}

// Case 2: pre-turn result, mid-turn wake (with a subsequent intermediate
// assistant row on the typed turn), and post-reply result.
{
  let settleReady = false;
  const messages = [
    { id: 5, role: "user", content: "[kanban] card 1 done" },
    { id: 6, role: "assistant", content: "pre-turn delegated result" },
    { id: 7, role: "user", content: "check cluster" },
    { id: 8, role: "assistant", content: "intermediate note before wake" },
    { id: 9, role: "user", content: "[kanban] card 2 done mid-turn" },
    { id: 10, role: "assistant", content: "mid-turn wake result" },
    { id: 11, role: "assistant", content: "intermediate note after wake" },
    { id: 12, role: "assistant", content: "final turn reply" },
    { id: 13, role: "user", content: "[kanban] card 3 done" },
    { id: 14, role: "assistant", content: "post-reply delegated result" },
  ];
  const ctx = makeContext(async (url) => {
    if (!String(url).includes("/messages") || !settleReady) {
      return { ok: false, status: 503, text: async () => "" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ latest_id: 14, messages }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "b".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  settleReady = true;
  await vm.runInContext("settleAfterReply('check cluster', 'final turn reply', 4, 12)", ctx);
  assert.equal(
    JSON.stringify(ctx.appended),
    JSON.stringify([
      { role: "agent", text: "pre-turn delegated result", tag: "update" },
      { role: "agent", text: "mid-turn wake result", tag: "update" },
      { role: "agent", text: "post-reply delegated result", tag: "update" },
    ]),
    "only delegated results (pre-turn, mid-turn wake, post-reply) should be shown as updates"
  );
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "14");
}
`)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("node page test failed: %v\n%s", err, out)
	}
}
