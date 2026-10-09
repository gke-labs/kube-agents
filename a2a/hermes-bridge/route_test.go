package hermesbridge

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

type routeStore struct {
	mu     sync.Mutex
	srv    *httptest.Server
	status int
	puts   []routePut
}

type routePut struct {
	path, auth string
	body       conversationRoute
}

func newRouteStore(t *testing.T, status int) *routeStore {
	t.Helper()
	s := &routeStore{status: status}
	s.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body conversationRoute
		_ = json.Unmarshal(raw, &body)
		s.mu.Lock()
		s.puts = append(s.puts, routePut{path: r.Method + " " + r.URL.Path, auth: r.Header.Get("Authorization"), body: body})
		status := s.status
		s.mu.Unlock()
		w.WriteHeader(status)
		_, _ = io.WriteString(w, `{"detail":"refused"}`)
	}))
	t.Cleanup(s.srv.Close)
	return s
}

func (s *routeStore) setStatus(status int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.status = status
}

func (s *routeStore) seen() []routePut {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]routePut(nil), s.puts...)
}

// submitFrom is submitIn with the gateway's audience: the conversation the
// task came from, as the gateway's authority block names it.
func submitFrom(t *testing.T, c *lib.Client, taskID, contextID, conversation, prompt string) {
	t.Helper()
	var auth map[string]any
	if err := json.Unmarshal(authorityFor(t, mintFor(t, c, taskID, "platform")), &auth); err != nil {
		t.Fatal(err)
	}
	auth["audience"] = map[string]any{"conversation": conversation, "kind": "dm"}
	raw, err := json.Marshal(auth)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, prompt), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(raw))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("submission publish: %v", err)
	}
}

// Before the turn, the bridge records the conversation its session answers:
// the platform, the gateway's conversation key and the context id, under the
// Hermes session id, so a card the turn files can report back there.
func TestAPI_TheTurnRecordsItsConversationRoute(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		if len(store.seen()) == 0 {
			t.Error("the turn reached the API server before the route was recorded")
		}
		writeCompletion(w, c.sessionID, "filed a card")
	})
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	submitFrom(t, c, "task-r", "ctx-route", "slack:dm/D123", "how many nodes")
	task := waitTerminal(t, c, "task-r")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; got != "filed a card" {
		t.Fatalf("answer = %q, want it untouched when the route was recorded", got)
	}
	puts := store.seen()
	want := routePut{path: "PUT /v1/sessions/a2a-ctx-route/route", auth: "Bearer kv-key",
		body: conversationRoute{Platform: "slack", Conversation: "slack:dm/D123", ContextID: "ctx-route"}}
	if len(puts) != 1 || puts[0] != want {
		t.Fatalf("route PUTs = %+v, want %+v", puts, want)
	}
}

// A route the store refuses is said in the answer: a turn that ends
// "completed" and then nothing, while the card's answer goes nowhere, is the
// failure this exists to end.
func TestAPI_ARouteThatCannotBeRecordedIsSaidInTheAnswer(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusInternalServerError)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	submitFrom(t, c, "task-lost", "ctx-lost", "gchat:spaces/A/threads/B", "how many pods")
	task := waitTerminal(t, c, "task-lost")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed: the turn runs without its route", task.State)
	}
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; !strings.HasSuffix(got, routeLostNote) {
		t.Fatalf("answer = %q, want it to end with the route-lost note", got)
	}
}

// A later task of the same session whose PUT fails, when an earlier task
// recorded the same route, loses nothing: the store still holds that route,
// so the answer carries no route-lost note.
func TestAPI_AFailedPUTOfAnAlreadyRecordedRouteIsNotALoss(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	submitFrom(t, c, "task-first", "ctx-same", "slack:dm/D123", "how many nodes")
	if task := waitTerminal(t, c, "task-first"); task.State != lib.StateCompleted {
		t.Fatalf("first task state = %s, want completed", task.State)
	}
	store.setStatus(http.StatusInternalServerError)
	submitFrom(t, c, "task-second", "ctx-same", "slack:dm/D123", "and pods")
	task := waitTerminal(t, c, "task-second")
	if task.State != lib.StateCompleted {
		t.Fatalf("second task state = %s, want completed", task.State)
	}
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; strings.Contains(got, routeLostNote) {
		t.Fatalf("answer = %q, want no route-lost note: the earlier record of the same route stands", got)
	}
	if puts := store.seen(); len(puts) != 2 {
		t.Fatalf("route PUTs = %d, want 2: the second task still tries", len(puts))
	}
}

// A conversation the notify route does not serve (the inject door, the A2A
// door) records nothing and says nothing.
func TestAPI_NoRouteIsRecordedForAConversationWithoutOne(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	submitFrom(t, c, "task-inject", "ctx-inject", "inject:eval/run-1", "hello")
	task := waitTerminal(t, c, "task-inject")
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; strings.Contains(got, routeLostNote) {
		t.Fatalf("answer = %q carries the route-lost note for a door with no route", got)
	}
	if puts := store.seen(); len(puts) != 0 {
		t.Fatalf("route PUTs = %+v, want none", puts)
	}
}
