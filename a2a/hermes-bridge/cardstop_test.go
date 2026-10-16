package hermesbridge

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// stopStore is a session-kv server standing in for the pod's: it records each
// request and answers with status and body, after hold is closed when set.
type stopStore struct {
	mu     sync.Mutex
	srv    *httptest.Server
	status int
	body   string
	hold   chan struct{}
	in     chan struct{} // closed at the first request
	inOnce sync.Once
	posts  []stopPost
	// onPost, when set, runs inside each request before the answer.
	onPost func(stopPost)
}

type stopPost struct {
	path, auth string
	body       cardStopRequest
}

// stopAnswer is a session-kv answer with two cards stopped and one settled,
// in PR 1's shape, the fields the bridge does not read included.
const stopAnswer = `{"session_id":"x","board":true,
 "stopped":[{"id":"t_1","title":"check node pools","prior_status":"running","via":"session","worker":{"pid":7}},
            {"id":"t_2","title":"list clusters","prior_status":"todo","via":"fanout","worker":null}],
 "settled":[{"id":"t_0","title":"earlier ask","status":"done"}],
 "failed":[],"rounds":1,"truncated":false,"deadline":false}`

func newStopStore(t *testing.T, status int, body string) *stopStore {
	t.Helper()
	s := &stopStore{status: status, body: body, in: make(chan struct{})}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body cardStopRequest
		_ = json.Unmarshal(raw, &body)
		s.mu.Lock()
		s.posts = append(s.posts, stopPost{path: r.Method + " " + r.URL.Path, auth: r.Header.Get("Authorization"), body: body})
		hold, status, answer, onPost := s.hold, s.status, s.body, s.onPost
		post := s.posts[len(s.posts)-1]
		s.mu.Unlock()
		if onPost != nil {
			onPost(post)
		}
		s.inOnce.Do(func() { close(s.in) })
		if hold != nil {
			select {
			case <-hold:
			case <-r.Context().Done():
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, answer)
	}))
	t.Cleanup(s.srv.Close)
	return s
}

// holdAnswers makes the store hold every answer until the returned release.
func (s *stopStore) holdAnswers(t *testing.T) func() {
	t.Helper()
	ch := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(ch) }) }
	s.mu.Lock()
	s.hold = ch
	s.mu.Unlock()
	t.Cleanup(release) // before the server's Close, which waits for its handlers
	return release
}

// answer sets what the store answers from the next request on.
func (s *stopStore) answer(status int, body string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status, s.body = status, body
}

func (s *stopStore) seen() []stopPost {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]stopPost(nil), s.posts...)
}

// withStore points a bridge's session-kv at store.
func withStore(store *stopStore) func(*Config) {
	return func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" }
}

// submitCardStop publishes the gateway's card-stop control: a submission whose
// message is the cardStop request part alone, from conversation when it is
// set, with authority (the honest capability when nil).
func submitCardStop(t *testing.T, c *lib.Client, taskID, contextID, conversation string, authority json.RawMessage) *lib.Envelope {
	t.Helper()
	if authority == nil {
		authority = authorityFor(t, mintFor(t, c, taskID, "platform"))
	}
	if conversation != "" {
		var auth map[string]any
		if err := json.Unmarshal(authority, &auth); err != nil {
			t.Fatal(err)
		}
		auth["audience"] = map[string]any{"conversation": conversation, "kind": "dm"}
		raw, err := json.Marshal(auth)
		if err != nil {
			t.Fatal(err)
		}
		authority = raw
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		cardStopPayload(t, taskID, contextID), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(authority))
	if err != nil {
		t.Fatalf("control envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("control publish: %v", err)
	}
	return env
}

// cardStopPayload is a message carrying the cardStop request part alone.
func cardStopPayload(t *testing.T, taskID, contextID string) json.RawMessage {
	t.Helper()
	part, err := lib.CardStopPart(lib.CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(lib.Message{Role: "user", MessageID: "msg-" + nuid.Next(),
		Parts: []lib.Part{part}, TaskID: taskID, ContextID: contextID})
	if err != nil {
		t.Fatal(err)
	}
	return payload
}

// resultCardStop is the cardStop part and the text on the task's result.
func resultCardStop(t *testing.T, task *lib.Task) (lib.CardStop, string) {
	t.Helper()
	a := task.Artifact(lib.ArtifactResult)
	if a == nil {
		t.Fatal("no result artifact")
	}
	c, ok := lib.CardStopOf(a.Parts)
	if !ok || c.CardStopResult == nil {
		t.Fatalf("result parts = %s, want a cardStop result", mustMarshal(t, a.Parts))
	}
	return c, joinTexts(a.Parts)
}

func joinTexts(parts []lib.Part) string {
	var texts []string
	for _, p := range parts {
		if p.Kind == "text" {
			texts = append(texts, p.Text)
		}
	}
	return strings.Join(texts, "")
}

func mustMarshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// The control: one POST to the conversation's session on session-kv, with
// its bearer, the context id and the gateway's conversation key, and the
// answer as the completed task's result, text and cardStop part. No turn
// reaches the Hermes API server, and no route is recorded.
func TestCardStop_StopsTheConversationsCardsWithNoTurn(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, withStore(store))
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-stop", "ctx-stop", "slack:dm/D123", nil)
	task := waitTerminal(t, c, "task-stop")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed; terminal: %q", task.State, terminalText(t, url, "task-stop"))
	}
	got, text := resultCardStop(t, task)
	want := lib.CardStopResult{
		Stopped: []lib.CardStopCard{{ID: "t_1", Title: "check node pools", PriorStatus: "running"},
			{ID: "t_2", Title: "list clusters", PriorStatus: "todo"}},
		Settled: []lib.CardStopCard{{ID: "t_0", Title: "earlier ask"}},
	}
	if mustMarshal(t, got.CardStopResult) != mustMarshal(t, want) {
		t.Fatalf("cardStop = %s, want %s", mustMarshal(t, got.CardStopResult), mustMarshal(t, want))
	}
	if wantText := "stopped 2 cards; 1 card had already finished"; text != wantText {
		t.Fatalf("text = %q, want %q", text, wantText)
	}
	posts := store.seen()
	wantPost := stopPost{path: "POST /v1/sessions/a2a-ctx-stop/cards/stop", auth: "Bearer kv-key",
		body: cardStopRequest{ContextID: "ctx-stop", Conversation: "slack:dm/D123"}}
	if len(posts) != 1 || posts[0] != wantPost {
		t.Fatalf("session-kv requests = %+v, want only %+v", posts, wantPost)
	}
	if n := len(stub.seen()); n != 0 {
		t.Fatalf("Hermes API requests = %d, want 0: a card stop is no turn", n)
	}
	if trail := eventTrail(t, replayEvents(t, url, "task-stop")); !slices.Contains(trail, string(lib.StateWorking)) {
		t.Fatalf("events = %v, want working before the terminal", trail)
	}
}

// A context id that cannot be a session id verbatim maps to the hashed id
// the conversation's turns run under, and the raw context id goes in the
// body for session-kv to check against it. A conversation the notify route
// does not serve sends no conversation key.
func TestCardStop_AHashedContextStopsTheHashedSession(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, `{"session_id":"x","board":false,"stopped":[],"settled":[],"failed":[]}`)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, withStore(store))
	c := gatewayClient(t, url)

	const ctxID = "ctx.dotted"
	sum := sha256.Sum256([]byte(ctxID))
	session := "a2a-h-" + hex.EncodeToString(sum[:])[:apiHashedSessionHexLen]
	if session != apiSessionID(ctxID) {
		t.Fatalf("test's hashed id %q disagrees with apiSessionID's %q", session, apiSessionID(ctxID))
	}
	submitCardStop(t, c, "task-hashed", ctxID, "inject:eval/run-1", nil)
	task := waitTerminal(t, c, "task-hashed")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed; terminal: %q", task.State, terminalText(t, url, "task-hashed"))
	}
	got, text := resultCardStop(t, task)
	if len(got.Stopped) != 0 || len(got.Settled) != 0 || text != "stopped 0 cards" {
		t.Fatalf("result = %s %q, want an empty stop", mustMarshal(t, got), text)
	}
	wantPost := stopPost{path: "POST /v1/sessions/" + session + "/cards/stop", auth: "Bearer kv-key",
		body: cardStopRequest{ContextID: ctxID}}
	if posts := store.seen(); len(posts) != 1 || posts[0] != wantPost {
		t.Fatalf("session-kv requests = %+v, want only %+v", posts, wantPost)
	}
}

// The stop waits for its own session's turn, the one the gateway just
// cancelled, and stops nothing until it ends; another conversation's stop
// does not wait on it.
func TestCardStop_WaitsForItsOwnSessionsTurnOnly(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	stub := newAPIStub(t, nil)
	b := startAPIBridge(t, url, stub, withStore(store))
	release := b.turns.acquire(testCtx(t), "a2a-ctx-held")
	if release == nil {
		t.Fatal("could not take the session's turn")
	}
	var once sync.Once
	t.Cleanup(func() { once.Do(release) })
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-held", "ctx-held", "", nil)
	waitFor(t, 10*time.Second, "the stop waiting on its session", func() bool {
		b.turns.mu.Lock()
		defer b.turns.mu.Unlock()
		slot, ok := b.turns.slots["a2a-ctx-held"]
		return ok && slot.refs == 2
	})
	submitCardStop(t, c, "task-free", "ctx-free", "", nil)
	if task := waitTerminal(t, c, "task-free"); task.State != lib.StateCompleted {
		t.Fatalf("other conversation's stop = %s, want completed while the held session waits", task.State)
	}
	for _, p := range store.seen() {
		if strings.Contains(p.path, "a2a-ctx-held") {
			t.Fatalf("the held session's cards were stopped while its turn was held: %+v", p)
		}
	}
	once.Do(release)
	if task := waitTerminal(t, c, "task-held"); task.State != lib.StateCompleted {
		t.Fatalf("held session's stop = %s, want completed once its turn ended", task.State)
	}
	if n := len(store.seen()); n != 2 {
		t.Fatalf("session-kv requests = %d, want 2", n)
	}
}

// A turn that does not end inside the wait fails the stop, unavailable, and
// nothing is stopped behind it.
func TestCardStop_ATurnThatDoesNotEndFailsTheStop(t *testing.T) {
	prev := cardStopTurnWait
	cardStopTurnWait = 300 * time.Millisecond
	t.Cleanup(func() { cardStopTurnWait = prev })
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	b := startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	release := b.turns.acquire(testCtx(t), "a2a-ctx-busy")
	if release == nil {
		t.Fatal("could not take the session's turn")
	}
	t.Cleanup(release)
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-busy", "ctx-busy", "", nil)
	task := waitTerminal(t, c, "task-busy")
	want := "reason: card-stop-unavailable - the session's turn did not end within 300ms; no card was stopped"
	if task.State != lib.StateFailed || terminalReason(t, task) != want {
		t.Fatalf("terminal = %s %q, want failed %q", task.State, terminalReason(t, task), want)
	}
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests = %d, want 0", n)
	}
}

// session-kv refusing, or no session-kv configured, fails the stop with
// card-stop-unavailable and the cause, which is the gateway's cue for its
// fallback notice.
func TestCardStop_NoAnswerIsUnavailable(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusServiceUnavailable, `{"detail":"Hermes' kanban module is not importable"}`)
	startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-503", "ctx-503", "", nil)
	task := waitTerminal(t, c, "task-503")
	want := `reason: card-stop-unavailable - HTTP 503: {"detail":"Hermes' kanban module is not importable"}`
	if task.State != lib.StateFailed || terminalReason(t, task) != want {
		t.Fatalf("terminal = %s %q, want failed %q", task.State, terminalReason(t, task), want)
	}
	if task.Artifact(lib.ArtifactResult) != nil {
		t.Fatal("a failed stop carries a result artifact")
	}

	_, url2 := startServer(t)
	startAPIBridge(t, url2, newAPIStub(t, nil), nil)
	c2 := gatewayClient(t, url2)
	submitCardStop(t, c2, "task-nostore", "ctx-nostore", "", nil)
	task = waitTerminal(t, c2, "task-nostore")
	want = "reason: card-stop-unavailable - no route store configured"
	if task.State != lib.StateFailed || terminalReason(t, task) != want {
		t.Fatalf("terminal with no RouteURL = %s %q, want failed %q", task.State, terminalReason(t, task), want)
	}
}

// The control passes the capability check every task passes: one minted for
// somebody else is rejected, and nothing is stopped.
func TestCardStop_IsRefusedWithoutItsCapability(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)

	const taskID = "task-stop-cap"
	submitCardStop(t, c, taskID, "ctx-stop-cap", "", authorityFor(t, mintFor(t, c, taskID, "chat-vole-somebody-else")))
	refuseAndFold(t, url, c, taskID)
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests = %d, want 0 for a refused control", n)
	}
}

// A follow-up on a running stop is refused task-ending: the stop runs no turn
// to queue it behind.
func TestCardStop_AFollowUpIsRefusedTaskEnding(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	release := store.holdAnswers(t)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, withStore(store))
	c := gatewayClient(t, url)

	origin := submitCardStop(t, c, "task-stop-steer", "ctx-stop-steer", "", nil)
	<-store.in
	steer := sendSteer(t, c, origin, "and also this")
	waitFor(t, 10*time.Second, "the follow-up's notice", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	n := steerNotices(t, url, origin.TaskID)[0]
	if n.Steer != lib.SteerRefused || n.Reason != lib.SteerReasonTaskEnding || n.EnvelopeID != steer.EnvelopeID {
		t.Fatalf("notice = %+v, want refused task-ending for %s", n.SteerNotice, steer.EnvelopeID)
	}
	release()
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if n := len(stub.seen()); n != 0 {
		t.Fatalf("Hermes API requests = %d, want 0", n)
	}
}

// A cancel ends a stop that is waiting on session-kv: canceled, as any task.
func TestCardStop_CancelEndsTheStop(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	store.holdAnswers(t)
	startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)

	origin := submitCardStop(t, c, "task-stop-cancel", "ctx-stop-cancel", "", nil)
	<-store.in
	publishCancel(t, c, origin)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateCanceled || terminalReason(t, task) != "reason: canceled-by-request" {
		t.Fatalf("terminal = %s %q, want canceled-by-request", task.State, terminalReason(t, task))
	}
}

// The cardStop part on a follow-up to an ordinary task is no control: the
// follow-up has no text and is refused no-text, and nothing is stopped.
func TestCardStop_ThePartOnAFollowUpIsIgnored(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	stub, in, release := holdingStub(t)
	startAPIBridge(t, url, stub, withStore(store))
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-plain", "ctx-plain", "how many nodes")
	<-in
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty, cardStopPayload(t, origin.TaskID, origin.ContextID),
		lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(origin.Authority))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", origin.TaskID), steer); err != nil {
		t.Fatal(err)
	}
	waitFor(t, 10*time.Second, "the follow-up's notice", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	if n := steerNotices(t, url, origin.TaskID)[0]; n.Steer != lib.SteerRefused || n.Reason != lib.SteerReasonNoText {
		t.Fatalf("notice = %+v, want refused no-text", n.SteerNotice)
	}
	release()
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests = %d, want 0: a follow-up is never a control", n)
	}
}

// The cli executor runs no conversation session, so it has nothing to stop
// and says so; nothing is spawned.
func TestCardStop_TheCLIExecutorIsUnsupported(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	marker := t.TempDir() + "/spawned"
	_, _ = startBridgeConfig(t, Config{
		NATSURL:      url,
		Command:      script(t, "touch "+marker),
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
		Scope:        capability.NamespaceScope(""),
		RouteURL:     store.srv.URL,
		RouteKey:     "kv-key",
	}, nil)
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-stop-cli", "ctx-stop-cli", "", nil)
	task := waitTerminal(t, c, "task-stop-cli")
	if task.State != lib.StateFailed || !strings.HasPrefix(terminalReason(t, task), "reason: card-stop-unsupported - ") {
		t.Fatalf("terminal = %s %q, want failed card-stop-unsupported", task.State, terminalReason(t, task))
	}
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests = %d, want 0", n)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("the cli executor spawned hermes for a card stop")
	}
}

// The result part is bounded: a list past cardStopListMax is cut and says
// listsCut, not truncated, which is session-kv's word for cards left open; a
// title past cardStopTitleMax runes is cut to that many, its "…" included.
// The text counts what session-kv answered, not the cut list.
func TestCardStopResult_IsBounded(t *testing.T) {
	var r cardStopResponse
	long := strings.Repeat("é", cardStopTitleMax+5)
	for i := 0; i < cardStopListMax+1; i++ {
		r.Stopped = append(r.Stopped, struct {
			ID          string `json:"id"`
			Title       string `json:"title"`
			PriorStatus string `json:"prior_status"`
		}{ID: "t", Title: long, PriorStatus: "running"})
	}
	got := cardStopResult(&r)
	if len(got.Stopped) != cardStopListMax || !got.ListsCut || got.Truncated {
		t.Fatalf("result = %d stopped, listsCut %v, truncated %v; want %d, true, false",
			len(got.Stopped), got.ListsCut, got.Truncated, cardStopListMax)
	}
	if title := got.Stopped[0].Title; title != strings.Repeat("é", cardStopTitleMax-1)+"…" {
		t.Fatalf("title = %q (%d runes), want %d runes, the ellipsis included", title, len([]rune(title)), cardStopTitleMax)
	}
	if text := cardStopText(&r); text != "stopped 201 cards" {
		t.Fatalf("text = %q, want the answer's count and no claim that cards are still open", text)
	}
}

// A session-kv that does not answer inside Config.CardStopTimeout fails the
// stop, unavailable, rather than holding the control open.
func TestCardStop_ASlowAnswerFailsAtTheTimeout(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	store.holdAnswers(t)
	startAPIBridge(t, url, newAPIStub(t, nil), func(c *Config) {
		withStore(store)(c)
		c.CardStopTimeout = 300 * time.Millisecond
	})
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-slow", "ctx-slow", "", nil)
	task := waitTerminal(t, c, "task-slow")
	if task.State != lib.StateFailed || !strings.HasPrefix(terminalReason(t, task), "reason: card-stop-unavailable - ") ||
		!strings.Contains(terminalReason(t, task), "deadline exceeded") {
		t.Fatalf("terminal = %s %q, want failed card-stop-unavailable at the deadline", task.State, terminalReason(t, task))
	}
}

// What the stop could not promise is said plainly, in the text and in the
// part's counts: a card it could not archive, an archive still running at
// session-kv's deadline, a card stopped mid-claim whose worker may still
// start, and cards left open past one stop.
func TestCardStop_WhatTheStopCannotPromiseIsSaid(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, `{"session_id":"x","board":true,
 "stopped":[{"id":"t_1","title":"a","prior_status":"running"},{"id":"t_2","title":"b","prior_status":"ready"}],
 "settled":[],
 "failed":[{"id":"t_3","title":"c","error":"database is locked"}],
 "pending":[{"id":"t_4","title":"d"},{"id":"t_5","title":"e"}],
 "unresolved":[{"id":"t_2","title":"b"}],
 "errors":["re-scan 2: database is locked"],
 "rounds":2,"truncated":true,"deadline":true}`)
	startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-partial", "ctx-partial", "", nil)
	task := waitTerminal(t, c, "task-partial")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed; terminal: %q", task.State, terminalText(t, url, "task-partial"))
	}
	got, text := resultCardStop(t, task)
	if got.Failed != 1 || got.Pending != 2 || got.Unresolved != 1 || !got.Truncated || !got.Deadline || got.ListsCut {
		t.Fatalf("cardStop = %s, want failed 1, pending 2, unresolved 1, truncated, deadline, lists whole", mustMarshal(t, got))
	}
	want := "stopped 2 cards; a worker may still start for 1 of them, claimed just before the stop; " +
		"1 card could not be stopped; " +
		"2 cards still being stopped when the stop ran out of time, so they may still be running; " +
		"the conversation had more cards than one stop takes, so some may still be open"
	if text != want {
		t.Fatalf("text = %q, want %q", text, want)
	}
}

// A deadline with nothing pending still says the stop did not finish.
func TestCardStopText_ADeadlineWithNothingPending(t *testing.T) {
	r := cardStopResponse{Deadline: true}
	if got, want := cardStopText(&r), "stopped 0 cards; the stop ran out of time before it finished"; got != want {
		t.Fatalf("text = %q, want %q", got, want)
	}
}

// Every answer the bridge cannot use fails the stop unavailable with the
// cause: a refusal, an unreadable body, one over the read cap, and a bridge
// with no session-kv key, which sends nothing.
func TestCardStop_AnswersItCannotUseAreUnavailable(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)
	for i, tc := range []struct {
		status int
		body   string
		want   string
	}{
		{http.StatusBadRequest, `{"detail":"context_id is not this session's"}`,
			`reason: card-stop-unavailable - HTTP 400: {"detail":"context_id is not this session's"}`},
		{http.StatusUnauthorized, `{"detail":"Invalid API key"}`, `reason: card-stop-unavailable - HTTP 401: {"detail":"Invalid API key"}`},
		{http.StatusOK, `not json`, "reason: card-stop-unavailable - unreadable answer: "},
		{http.StatusOK, `{"stopped":[],"pad":"` + strings.Repeat("x", cardStopResponseCap) + `"}`,
			"reason: card-stop-unavailable - the answer is over 1048576 bytes"},
	} {
		store.answer(tc.status, tc.body)
		id := fmt.Sprintf("task-bad-%d", i)
		submitCardStop(t, c, id, "ctx-"+id, "", nil)
		task := waitTerminal(t, c, id)
		if task.State != lib.StateFailed || !strings.HasPrefix(terminalReason(t, task), tc.want) {
			t.Errorf("answer %d %.40q: terminal = %s %q, want failed %q", tc.status, tc.body, task.State, terminalReason(t, task), tc.want)
		}
	}

	_, url2 := startServer(t)
	store2 := newStopStore(t, http.StatusOK, stopAnswer)
	startAPIBridge(t, url2, newAPIStub(t, nil), func(c *Config) { c.RouteURL = store2.srv.URL })
	c2 := gatewayClient(t, url2)
	submitCardStop(t, c2, "task-nokey", "ctx-nokey", "", nil)
	task := waitTerminal(t, c2, "task-nokey")
	if want := "reason: card-stop-unavailable - SESSION_KV_API_KEY is not set"; task.State != lib.StateFailed || terminalReason(t, task) != want {
		t.Fatalf("terminal with no key = %s %q, want failed %q", task.State, terminalReason(t, task), want)
	}
	if n := len(store2.seen()); n != 0 {
		t.Fatalf("session-kv requests with no key = %d, want 0", n)
	}
}

// The session's turn is held for the whole POST: no turn can start in the
// session while session-kv archives its cards.
func TestCardStop_TheTurnIsHeldAcrossThePOST(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	b := startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	var heldDuringPost atomic.Bool
	store.mu.Lock()
	store.onPost = func(stopPost) {
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		if release := b.turns.acquire(ctx, "a2a-ctx-hold-post"); release != nil {
			release()
			return
		}
		heldDuringPost.Store(true)
	}
	store.mu.Unlock()
	c := gatewayClient(t, url)

	submitCardStop(t, c, "task-hold-post", "ctx-hold-post", "", nil)
	if task := waitTerminal(t, c, "task-hold-post"); task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if !heldDuringPost.Load() {
		t.Fatal("the session's turn was free while session-kv archived its cards")
	}
}

// The sequence the gateway drives: a turn is running, the gateway cancels it
// and sends the stop. Here the stop lands first, so it is certainly waiting
// when the cancel comes: it reaches session-kv only once the cancelled turn
// has ended.
func TestCardStop_FollowsTheCancelledTurn(t *testing.T) {
	_, url := startServer(t)
	turnIn := make(chan struct{})
	var inOnce sync.Once
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		inOnce.Do(func() { close(turnIn) })
		<-r.Context().Done() // runs until the bridge ends the request
	})
	store := newStopStore(t, http.StatusOK, stopAnswer)
	b := startAPIBridge(t, url, stub, withStore(store))
	// The turn's task leaves the bridge's table when its finalize is done,
	// just before it gives the session's turn back.
	var postAfterTurn atomic.Bool
	store.mu.Lock()
	store.onPost = func(stopPost) { postAfterTurn.Store(runOf(b, "task-long") == nil) }
	store.mu.Unlock()
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-long", "ctx-seq", "check every node pool")
	<-turnIn
	submitCardStop(t, c, "task-seq-stop", "ctx-seq", "", nil)
	waitFor(t, 10*time.Second, "the stop waiting behind the turn", func() bool {
		b.turns.mu.Lock()
		defer b.turns.mu.Unlock()
		slot, ok := b.turns.slots["a2a-ctx-seq"]
		return ok && slot.refs == 2
	})
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests while the turn runs = %d, want 0", n)
	}
	publishCancel(t, c, origin)
	if task := waitTerminal(t, c, origin.TaskID); task.State != lib.StateCanceled {
		t.Fatalf("turn state = %s, want canceled", task.State)
	}
	if task := waitTerminal(t, c, "task-seq-stop"); task.State != lib.StateCompleted {
		t.Fatalf("stop state = %s, want completed; terminal: %q", task.State, terminalText(t, url, "task-seq-stop"))
	}
	if !postAfterTurn.Load() {
		t.Fatal("the stop reached session-kv while the cancelled turn still held the session")
	}
	if posts := store.seen(); len(posts) != 1 || !strings.Contains(posts[0].path, "a2a-ctx-seq") {
		t.Fatalf("session-kv requests = %+v, want one for a2a-ctx-seq", posts)
	}
}

// A control accepted once the bridge is closing is ended bridge-shutdown in
// accept and starts no goroutine: Run's wg.Wait may already be under way.
func TestCardStop_AClosingBridgeStartsNoStop(t *testing.T) {
	_, url := startServer(t)
	store := newStopStore(t, http.StatusOK, stopAnswer)
	b := startAPIBridge(t, url, newAPIStub(t, nil), withStore(store))
	c := gatewayClient(t, url)
	env, err := lib.NewMessageEnvelope(gatewayParty, "task-closing", "ctx-closing", "corr-task-closing",
		cardStopPayload(t, "task-closing", "ctx-closing"), lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, mintFor(t, c, "task-closing", "platform"))))
	if err != nil {
		t.Fatal(err)
	}
	b.mu.Lock()
	b.closing.Store(true)
	b.mu.Unlock()
	b.accept(context.Background(), env)
	task := waitTerminal(t, c, "task-closing")
	if task.State != lib.StateFailed || terminalReason(t, task) != shutdownReason {
		t.Fatalf("terminal = %s %q, want failed %q", task.State, terminalReason(t, task), shutdownReason)
	}
	time.Sleep(300 * time.Millisecond)
	if n := len(store.seen()); n != 0 {
		t.Fatalf("session-kv requests = %d, want 0: no stop starts once the bridge is closing", n)
	}
}
