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
	mu                 sync.Mutex
	sessions           map[string]bool
	messages           map[string][]map[string]any
	nextID             int64
	lastQuery          string
	chats              []map[string]any
	reply              string
	postTurnWake       string
	chatDelay          time.Duration
	chatCode           int
	createDelay        time.Duration
	dropSessionOnFirst bool
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
		createDelay := f.createDelay
		f.mu.Unlock()
		if createDelay > 0 {
			time.Sleep(createDelay)
		}
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
		if f.dropSessionOnFirst {
			f.dropSessionOnFirst = false
			delete(f.sessions, sid)
		}
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
		if !f.sessions[sid] {
			hermesError(w, http.StatusNotFound, "Session not found: "+sid)
			return
		}
		// Same paging as Hermes with order=latest: offset counts back from
		// the newest message, and the page comes back oldest first.
		// When order is omitted or not latest, Hermes serves insertion order.
		all := f.messages[sid]
		limit, offset := len(all), 0
		if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil {
			limit = v
		}
		if v, err := strconv.Atoi(r.URL.Query().Get("offset")); err == nil {
			offset = v
		}
		var page []map[string]any
		if r.URL.Query().Get("order") == "latest" {
			end := max(len(all)-offset, 0)
			page = all[max(end-limit, 0):end]
		} else {
			start := min(offset, len(all))
			end := min(start+limit, len(all))
			page = all[start:end]
		}
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
	for _, sid := range []string{"", "k8s-evt-abc", "web-console-../../x", "web-console-ABC", "../api/sessions"} {
		raw := `{"message":"hi","session_id":` + strconv.Quote(sid) + `}`
		req := httptest.NewRequest(http.MethodPost, "http://localhost:8080/api/chat", strings.NewReader(raw))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set(consoleHeader, consoleHeaderValue)
		rec := serve(h, req)
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
	fake := newFakeHermes()
	upstream := httptest.NewServer(fake)
	t.Cleanup(upstream.Close)
	srv := newServer(config{HermesURL: upstream.URL, APIServerKey: testKey, ClusterName: "c1"})
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
	html := string(page)
	for _, tc := range []struct {
		name    string
		pattern string
	}{
		{"main grid row", `(?s)\bmain\s*\{[^}]*grid-template-rows:\s*minmax\(0,\s*1fr\);`},
		{"main min-height", `(?s)\bmain\s*\{[^}]*min-height:\s*0;`},
		{"chat-section min-height", `(?s)\.chat-section\s*\{[^}]*min-height:\s*0;`},
		{"sidebar min-height", `(?s)\.sidebar\s*\{[^}]*min-height:\s*0;`},
		{"sidebar-content scroll", `(?s)\.sidebar-content\s*\{[^}]*overflow-y:\s*auto;`},
	} {
		if !regexp.MustCompile(tc.pattern).MatchString(html) {
			t.Errorf("index.html is missing %s (%s)", tc.name, tc.pattern)
		}
	}
}

// TestPageSettleAndWakeLogic executes the embedded script from static/index.html
// in Node's vm module against stubbed DOM and fetch globals, verifying:
//  1. A failed settle fetch keeps the mark at its pre-turn value and queues the
//     turn in unsettledTurns so the next poll shows any pre-turn or mid-turn
//     wakes and advances the mark past replyId without replaying the turn's own
//     assistant reply as an "update".
//  1b. Two consecutive failed settles (with or without replyId) queue both
//      turns and settle them in order on the next poll or next settle fetch
//      without replaying either turn's reply.
//  2. Results that landed before the turn, multiple card wakes that landed
//     mid-turn (followed by intermediate assistant rows of the typed turn), and
//     results that landed after the reply are each shown once while the typed
//     turn's own assistant messages are not duplicated, including when the
//     turn's final reply is empty.
//  3. renderMarkdown formats line-opening fenced code blocks (including empty
//     fenced blocks) while keeping mid-line backticks and literal **bold**
//     markers inside inline code spans intact.
//  4. First turn on a fresh tab mints the session and sets the mark to 0
//     before the request, so a dropped connection preserves the session and
//     displays the late reply on the next poll.
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
  const elements = new Map();
  const makeEl = () => {
    const listeners = {};
    return {
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
      listeners,
      addEventListener(type, fn) {
        (listeners[type] ||= []).push(fn);
      },
      appendChild() {},
      remove() {},
    };
  };
  const ctx = {
    console,
    crypto,
    Date,
    Number,
    String,
    Uint8Array,
    Array,
    JSON,
    encodeURIComponent,
    setInterval() {},
    sessionStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k),
    },
    document: {
      getElementById: id => {
        if (!elements.has(id)) elements.set(id, makeEl());
        return elements.get(id);
      },
      createElement: () => makeEl(),
      querySelectorAll: () => [],
    },
    fetch: fetchImpl,
    appended,
    store,
    elements,
  };
  vm.createContext(ctx);
  vm.runInContext(scriptSource + "\n;appendMessage = (role, text, tag) => { appended.push({role, text, tag}); return { remove() {} }; };", ctx);
  return ctx;
}

// Case 1: failed settle fetch keeps the mark at 4 so pre-turn wakes are not
// skipped, then the next poll settles the turn to replyId (8) without replaying
// the turn's own assistant reply as an "update".
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
      json: async () => ({
        latest_id: 8,
        messages: [
          { id: 5, role: "user", content: "[kanban] card done" },
          { id: 6, role: "assistant", content: "pre-turn wake during outage" },
          { id: 7, role: "user", content: "again" },
          { id: 8, role: "assistant", content: "pods are healthy" },
        ],
      }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  pollCalls = [];
  await vm.runInContext("settleAfterReply('again', 'pods are healthy', 6, 8)", ctx);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "4", "failed settle must not advance mark past unseen rows");
  await vm.runInContext("pollMessages()", ctx);
  assert.ok(pollCalls[1].endsWith("?after=4"), "next poll must request ?after=4, got " + pollCalls[1]);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "8", "poll after failed settle must advance mark to replyId");
  assert.equal(
    JSON.stringify(ctx.appended),
    JSON.stringify([{ role: "agent", text: "pre-turn wake during outage", tag: "update" }]),
    "pre-turn wake must be shown and turn reply must not be replayed"
  );
}

// Case 1b: two consecutive turns whose post-turn reads fail, followed by a poll,
// and an unsettled turn A followed by a succeeding settleAfterReply on turn B:
// neither turn's reply or intermediate working note is replayed as an "update".
{
  let pollCalls = [];
  const ctx = makeContext(async (url) => {
    if (!String(url).includes("/messages")) {
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    }
    pollCalls.push(String(url));
    if (pollCalls.length <= 2) {
      return { ok: false, status: 502, text: async () => "bad gateway" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 10,
        messages: [
          { id: 5, role: "user", content: "a" },
          { id: 6, role: "assistant", content: "ra" },
          { id: 7, role: "user", content: "b" },
          { id: 8, role: "assistant", content: "rb" },
          { id: 9, role: "user", content: "[kanban] card done" },
          { id: 10, role: "assistant", content: "delegated result after outage" },
        ],
      }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  pollCalls = [];
  await vm.runInContext("settleAfterReply('a', 'ra', 4, undefined)", ctx);
  await vm.runInContext("settleAfterReply('b', 'rb', 6, undefined)", ctx);
  await vm.runInContext("pollMessages()", ctx);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "10");
  assert.equal(
    JSON.stringify(ctx.appended),
    JSON.stringify([{ role: "agent", text: "delegated result after outage", tag: "update" }]),
    "two failed settles followed by poll must not replay either turn reply"
  );

  let callCount = 0;
  const ctx2 = makeContext(async (url) => {
    if (!String(url).includes("/messages")) {
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    }
    callCount++;
    if (callCount === 1) {
      return { ok: false, status: 502, text: async () => "bad gateway" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 9,
        messages: [
          { id: 5, role: "user", content: "a" },
          { id: 6, role: "assistant", content: "ra" },
          { id: 7, role: "user", content: "b" },
          { id: 8, role: "assistant", content: "checking..." },
          { id: 9, role: "assistant", content: "rb" },
        ],
      }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  callCount = 0;
  await vm.runInContext("settleAfterReply('a', 'ra', 4, undefined)", ctx2);
  await vm.runInContext("settleAfterReply('b', 'rb', 6, 9)", ctx2);
  assert.equal(ctx2.store.get("kube-agents-web-console-mark"), "9");
  assert.deepEqual(ctx2.appended, [], "settling pending turn A before turn B must not replay B's working note or reply");

  // Stale poll started before a turn that resolves while settleAfterReply is
  // waiting must be discarded rather than settling unsettledTurns against
  // pre-turn data and advancing the mark past unseen rows.
  let resolveStalePoll;
  let resolveSettle;
  let msgCalls = 0;
  const ctx3 = makeContext(async (url) => {
    if (String(url) === "/api/chat") {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          session_id: "web-console-" + "a".repeat(32),
          reply: "turn reply",
          before_id: 6,
          reply_id: 8,
        }),
      };
    }
    if (!String(url).includes("/messages")) {
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    }
    msgCalls++;
    if (msgCalls === 1) {
      // Initial pre-turn poll started on script load.
      return new Promise(r => { resolveStalePoll = r; });
    }
    return new Promise(r => { resolveSettle = r; });
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  ctx3.appended.length = 0;
  const turnPromise = vm.runInContext("submitMessage('typed turn')", ctx3);
  for (let i = 0; i < 20 && !resolveSettle; i++) await new Promise(r => setImmediate(r));
  assert.ok(resolveSettle, "settleAfterReply fetch must be in flight");
  resolveStalePoll({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 6,
      messages: [
        { id: 5, role: "user", content: "[kanban] card done" },
        { id: 6, role: "assistant", content: "pre-turn wake result" },
      ],
    }),
  });
  await new Promise(r => setImmediate(r));
  resolveSettle({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 8,
      messages: [
        { id: 5, role: "user", content: "[kanban] card done" },
        { id: 6, role: "assistant", content: "pre-turn wake result" },
        { id: 7, role: "user", content: "typed turn" },
        { id: 8, role: "assistant", content: "turn reply" },
      ],
    }),
  });
  await turnPromise;
  assert.equal(ctx3.store.get("kube-agents-web-console-mark"), "8");
  assert.ok(
    ctx3.appended.some(m => m.role === "agent" && m.text === "pre-turn wake result" && m.tag === "update"),
    "stale pre-turn poll must not drop pre-turn wake result: " + JSON.stringify(ctx3.appended)
  );

  // Also verify seq !== turnSeq on its own: when a turn's settleAfterReply
  // fails and submitMessage finishes (clearing turnInFlight while leaving the
  // turn in unsettledTurns), a pre-turn poll resolving afterwards must still
  // be discarded rather than settling the turn against pre-turn rows.
  let resolveLateStalePoll = null;
  let lateMsgCalls = 0;
  const ctx3b = makeContext(async (url) => {
    if (String(url) === "/api/chat") {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          session_id: "web-console-" + "a".repeat(32),
          reply: "turn reply",
          before_id: 6,
          reply_id: 8,
        }),
      };
    }
    if (!String(url).includes("/messages")) {
      return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
    }
    lateMsgCalls++;
    if (lateMsgCalls === 1) {
      return new Promise(r => { resolveLateStalePoll = r; });
    }
    if (lateMsgCalls === 2) {
      return { ok: false, status: 502, text: async () => "bad gateway" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        latest_id: 8,
        messages: [
          { id: 5, role: "user", content: "[kanban] card done" },
          { id: 6, role: "assistant", content: "pre-turn wake result" },
          { id: 7, role: "user", content: "typed turn" },
          { id: 8, role: "assistant", content: "turn reply" },
        ],
      }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "a".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  ctx3b.appended.length = 0;
  await vm.runInContext("submitMessage('typed turn')", ctx3b);
  assert.equal(ctx3b.store.get("kube-agents-web-console-mark"), "4");
  resolveLateStalePoll({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 6,
      messages: [
        { id: 5, role: "user", content: "[kanban] card done" },
        { id: 6, role: "assistant", content: "pre-turn wake result" },
      ],
    }),
  });
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  assert.equal(
    ctx3b.store.get("kube-agents-web-console-mark"),
    "4",
    "stale pre-turn poll resolving after turn finishes must be dropped by seq !== turnSeq"
  );
  assert.ok(
    !ctx3b.appended.some(m => m.tag === "update"),
    "stale pre-turn poll must not settle pending turn early: " + JSON.stringify(ctx3b.appended)
  );
  await vm.runInContext("pollMessages()", ctx3b);
  assert.equal(ctx3b.store.get("kube-agents-web-console-mark"), "8");
  assert.equal(
    ctx3b.appended.filter(m => m.role === "agent" && m.text === "pre-turn wake result" && m.tag === "update").length,
    1,
    "clean poll after discarded stale poll must show pre-turn wake once: " + JSON.stringify(ctx3b.appended)
  );
}

// Case 2: pre-turn result, two mid-turn wakes (including a multi-row wake and
// an intermediate assistant row before the first wake), and a post-reply result,
// even when replyId points at the post-reply wake row (id 16) rather than the
// turn's own reply row (id 14), plus an empty-reply turn with a mid-turn wake.
{
  let settleReady = false;
  const messages = [
    { id: 5, role: "user", content: "[kanban] card 1 done" },
    { id: 6, role: "assistant", content: "pre-turn delegated result" },
    { id: 7, role: "user", content: "check cluster" },
    { id: 8, role: "assistant", content: "intermediate note before wake" },
    { id: 9, role: "user", content: "[kanban] card 2 done mid-turn" },
    { id: 10, role: "assistant", content: "first mid-turn wake result" },
    { id: 11, role: "assistant", content: "first mid-turn wake second row" },
    { id: 12, role: "user", content: "[kanban] card 3 done mid-turn" },
    { id: 13, role: "assistant", content: "second mid-turn wake result" },
    { id: 14, role: "assistant", content: "final turn reply" },
    { id: 15, role: "user", content: "[kanban] card 4 done" },
    { id: 16, role: "assistant", content: "post-reply delegated result" },
  ];
  const ctx = makeContext(async (url) => {
    if (!String(url).includes("/messages") || !settleReady) {
      return { ok: false, status: 503, text: async () => "" };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({ latest_id: 16, messages }),
    };
  }, {
    "kube-agents-web-console-session": "web-console-" + "b".repeat(32),
    "kube-agents-web-console-mark": "4",
  });
  settleReady = true;
  await vm.runInContext("settleAfterReply('check cluster', 'final turn reply', 4, 16)", ctx);
  assert.equal(
    JSON.stringify(ctx.appended),
    JSON.stringify([
      { role: "agent", text: "pre-turn delegated result", tag: "update" },
      { role: "agent", text: "first mid-turn wake result", tag: "update" },
      { role: "agent", text: "first mid-turn wake second row", tag: "update" },
      { role: "agent", text: "second mid-turn wake result", tag: "update" },
      { role: "agent", text: "post-reply delegated result", tag: "update" },
    ]),
    "all delegated results (pre-turn, both mid-turn wakes including multi-row wake, post-reply) must be shown once"
  );
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "16");

  // Empty final reply (row 11 filtered out of /messages) with a mid-turn wake at row 10.
  const emptyReplyCtx = makeContext(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      latest_id: 11,
      messages: [
        { id: 7, role: "user", content: "check" },
        { id: 8, role: "assistant", content: "working note" },
        { id: 9, role: "user", content: "[kanban] card done" },
        { id: 10, role: "assistant", content: "wake result on empty-reply turn" },
      ],
    }),
  }), {
    "kube-agents-web-console-session": "web-console-" + "b".repeat(32),
    "kube-agents-web-console-mark": "6",
  });
  await vm.runInContext("settleAfterReply('check', '', 6, 11)", emptyReplyCtx);
  assert.equal(
    JSON.stringify(emptyReplyCtx.appended),
    JSON.stringify([{ role: "agent", text: "wake result on empty-reply turn", tag: "update" }]),
    "mid-turn wake on an empty-reply turn must be shown once"
  );
  assert.equal(emptyReplyCtx.store.get("kube-agents-web-console-mark"), "11");
}

// Case 3: renderMarkdown handles column-0 fences, indented list fences,
// indented closers, four-backtick fences, empty fenced blocks, mid-line
// backticks, double-backtick inline code spans quoting a single backtick,
// and literal **bold** markers inside inline code spans.
{
  const ctx = makeContext(async () => ({ ok: false, status: 503, text: async () => "" }));
  const fenced = vm.runInContext("renderMarkdown('Before\\n\\x60\\x60\\x60yaml\\nkind: Pod\\n\\x60\\x60\\x60\\nAfter')", ctx);
  assert.equal(fenced, "<p>Before</p><pre><code>kind: Pod</code></pre><p>After</p>");
  const emptyFence = vm.runInContext("renderMarkdown('Run:\\n\\x60\\x60\\x60\\n\\x60\\x60\\x60\\nThen apply.')", ctx);
  assert.equal(emptyFence, "<p>Run:</p><pre><code></code></pre><p>Then apply.</p>");
  const codeWithStars = vm.runInContext("renderMarkdown('Pass \\x60**kwargs\\x60 to **func**.')", ctx);
  assert.equal(codeWithStars, "<p>Pass <code>**kwargs</code> to <strong>func</strong>.</p>");
  const boldWrappingCode = vm.runInContext("renderMarkdown('**Run \\x60kubectl rollout restart\\x60 next**')", ctx);
  assert.equal(boldWrappingCode, "<p><strong>Run <code>kubectl rollout restart</code> next</strong></p>");
  const tildeFence = vm.runInContext("renderMarkdown('~~~yaml\\nkind: Pod\\n~~~')", ctx);
  assert.equal(tildeFence, "<pre><code>kind: Pod</code></pre>");
  const longerInnerBackticks = vm.runInContext("renderMarkdown('\\x60a\\x60\\x60b\\x60')", ctx);
  assert.equal(longerInnerBackticks, "<p><code>a\x60\x60b</code></p>");
  const spacedStars = vm.runInContext("renderMarkdown('2 ** 3 ** 4')", ctx);
  assert.equal(spacedStars, "<p>2 ** 3 ** 4</p>");
  const indentedListFence = vm.runInContext(
    "renderMarkdown('- Apply:\\n  \\x60\\x60\\x60yaml\\n  kind: Pod\\n\\n  spec: {}\\n  \\x60\\x60\\x60\\n- Done')",
    ctx
  );
  assert.equal(
    indentedListFence,
    "<p>- Apply:</p><pre><code>kind: Pod\n\nspec: {}</code></pre><p>- Done</p>"
  );
  const indentedCloser = vm.runInContext(
    "renderMarkdown('\\x60\\x60\\x60yaml\\nkind: Pod\\n  \\x60\\x60\\x60\\nAfter that, apply it.')",
    ctx
  );
  assert.equal(
    indentedCloser,
    "<pre><code>kind: Pod</code></pre><p>After that, apply it.</p>"
  );
  const fourBacktick = vm.runInContext(
    "renderMarkdown('\\x60\\x60\\x60\\x60\\n\\x60\\x60\\x60\\nnested\\n\\x60\\x60\\x60\\n\\x60\\x60\\x60\\x60')",
    ctx
  );
  assert.equal(fourBacktick, "<pre><code>\x60\x60\x60\nnested\n\x60\x60\x60</code></pre>");
  const doubleBacktickSpan = vm.runInContext(
    "renderMarkdown('Use \\x60\\x60a \\x60 b\\x60\\x60 inline.')",
    ctx
  );
  assert.equal(doubleBacktickSpan, "<p>Use <code>a \x60 b</code> inline.</p>");
  const fenceAfterRejectedOpen = vm.runInContext(
    "renderMarkdown('\\x60\\x60\\x60a\\x60b\\n\\x60\\x60\\x60\\ncode\\n\\x60\\x60\\x60\\nAfter')",
    ctx
  );
  assert.equal(
    fenceAfterRejectedOpen,
    "<p>\x60\x60\x60a\x60b</p><pre><code>code</code></pre><p>After</p>"
  );
  const crossLineCodeSpan = vm.runInContext(
    "renderMarkdown('Use \\x60kubectl\\nget pods\\x60 now.')",
    ctx
  );
  assert.equal(
    crossLineCodeSpan,
    "<p>Use <code>kubectl get pods</code> now.</p>"
  );
  const inline = vm.runInContext("renderMarkdown('Use \\x60\\x60\\x60yaml on its own line, not \\x60\\x60\\x60inline\\x60\\x60\\x60 here.')", ctx);
  assert.ok(inline.includes("Use <code>yaml on its own line, not </code>inline"), "mid-line backticks must not drop surrounding prose: " + inline);
  assert.ok(!inline.includes("<pre>"), "mid-line backticks must not open a <pre> block: " + inline);
}

// Case 4: first turn on a fresh tab mints the session and sets the mark to 0
// before the request, so a dropped connection preserves the session and
// displays the late reply on the next poll.
{
  let chatSessionId = "";
  const ctx = makeContext(async (url, opts) => {
    if (String(url) === "/api/chat") {
      chatSessionId = JSON.parse(opts.body).session_id;
      throw new Error("socket hang up");
    }
    if (String(url).includes("/messages")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          latest_id: 2,
          messages: [
            { id: 1, role: "user", content: "first question" },
            { id: 2, role: "assistant", content: "late reply after drop" },
          ],
        }),
      };
    }
    return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
  });
  ctx.appended.length = 0;
  await vm.runInContext("submitMessage('first question')", ctx);
  assert.ok(/^web-console-[0-9a-f]{32}$/.test(chatSessionId), "first turn must send a minted session ID: " + chatSessionId);
  assert.equal(ctx.store.get("kube-agents-web-console-session"), chatSessionId);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "0");
  await vm.runInContext("pollMessages()", ctx);
  assert.ok(
    ctx.appended.some(m => m.role === "agent" && m.text === "late reply after drop" && m.tag === "update"),
    "poll after dropped first turn must display late reply: " + JSON.stringify(ctx.appended)
  );
}

// Case 5: recreated session (created: true after store loss when the tab mark
// is 138 and new IDs start at 1) resets the mark so delegated results on the
// recreated session are shown, and Enter during IME composition (isComposing
// or keyCode 229) does not send the half-typed message.
{
  let msgPhase = 0;
  let chatCalls = 0;
  const sid = "web-console-" + "e".repeat(32);
  const ctx = makeContext(async (url) => {
    if (String(url) === "/api/chat") {
      chatCalls++;
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          session_id: sid,
          reply: "fresh store reply",
          reply_id: 2,
          created: true,
        }),
      };
    }
    if (String(url).includes("/messages")) {
      msgPhase++;
      if (msgPhase === 1) {
        return { ok: true, status: 200, json: async () => ({ latest_id: 138, messages: [] }) };
      }
      if (msgPhase === 2) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            latest_id: 2,
            messages: [
              { id: 1, role: "user", content: "after store wipe" },
              { id: 2, role: "assistant", content: "fresh store reply" },
            ],
          }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          latest_id: 4,
          messages: [
            { id: 3, role: "user", content: "[kanban] card done" },
            { id: 4, role: "assistant", content: "delegated result on recreated session" },
          ],
        }),
      };
    }
    return { ok: true, status: 200, text: async () => "{}", json: async () => ({}) };
  }, {
    "kube-agents-web-console-session": sid,
    "kube-agents-web-console-mark": "138",
  });
  ctx.SetImmediate = setImmediate;
  ctx.appended.length = 0;
  await vm.runInContext("submitMessage('after store wipe')", ctx);
  assert.equal(ctx.store.get("kube-agents-web-console-mark"), "2", "created:true must reset old mark 138 and settle to new reply_id 2");
  await vm.runInContext("pollMessages()", ctx);
  assert.ok(
    ctx.appended.some(m => m.role === "agent" && m.text === "delegated result on recreated session" && m.tag === "update"),
    "poll after recreated session must show delegated result at id 4: " + JSON.stringify(ctx.appended)
  );

  // IME composition check on prompt-input keydown.
  const inputEl = ctx.elements.get("prompt-input");
  const keydown = inputEl.listeners.keydown[0];
  inputEl.value = "half-typed";
  const callsBefore = chatCalls;
  keydown({ key: "Enter", shiftKey: false, isComposing: true, keyCode: 13, preventDefault() {} });
  keydown({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229, preventDefault() {} });
  assert.equal(inputEl.value, "half-typed", "IME Enter must not clear prompt-input");
  assert.equal(chatCalls, callsBefore, "IME Enter must not submit a turn");
}
`)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("node page test failed: %v\n%s", err, out)
	}
}
