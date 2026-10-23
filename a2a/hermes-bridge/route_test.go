package hermesbridge

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

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
// the platform, the gateway's conversation key, the context id and the ask's kind, under the
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
		body: conversationRoute{Platform: "slack", Conversation: "slack:dm/D123", ContextID: "ctx-route", Ask: askQuestion, ChatType: "dm"}}
	if len(puts) != 1 || puts[0] != want {
		t.Fatalf("route PUTs = %+v, want %+v", puts, want)
	}
}

// The gateway's requester principal (hmac:<32-hex>) and audience kind ("dm" or
// "group") ride the route PUT so the memory provider can isolate personal
// memory per user and disable personal memory in group conversations.
func TestAPI_TheTurnRecordsRequesterPrincipalAndAudienceKind(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		writeCompletion(w, c.sessionID, "done")
	})
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	var auth map[string]any
	if err := json.Unmarshal(authorityFor(t, mintFor(t, c, "task-id-dm", "platform")), &auth); err != nil {
		t.Fatal(err)
	}
	auth["audience"] = map[string]any{"conversation": "gchat:spaces/DM1", "kind": "dm"}
	auth["requester"] = map[string]any{"principal": "hmac:0123456789abcdef0123456789abcdef"}
	raw, err := json.Marshal(auth)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, "task-id-dm", "ctx-id-dm", "corr-task-id-dm",
		messagePayload(t, "task-id-dm", "ctx-id-dm", "remember my cluster"), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(raw))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", "task-id-dm"), env); err != nil {
		t.Fatal(err)
	}
	task := waitTerminal(t, c, "task-id-dm")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed", task.State)
	}
	puts := store.seen()
	want := routePut{
		path: "PUT /v1/sessions/a2a-ctx-id-dm/route",
		auth: "Bearer kv-key",
		body: conversationRoute{
			Platform:     "google_chat",
			Conversation: "gchat:spaces/DM1",
			ContextID:    "ctx-id-dm",
			Ask:          askQuestion,
			UserID:       "hmac:0123456789abcdef0123456789abcdef",
			ChatType:     "dm",
		},
	}
	if len(puts) != 1 || puts[0] != want {
		t.Fatalf("route PUTs = %+v, want %+v", puts, want)
	}
}

// A Google-authenticated A2A door conversation (backend: "a2a-google",
// verifiedBy: "a2a-google-token") that carries a verified requester principal
// records an identity-only route (Platform: "") for memory isolation without
// marking a chat notify route as recorded or adding a route-lost note if the
// PUT fails.
func TestAPI_GoogleA2AConversationWithRequesterRecordsIdentityOnly(t *testing.T) {
	_, url := startServer(t)
	store := newRouteStore(t, http.StatusOK)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
	c := gatewayClient(t, url)

	var auth map[string]any
	if err := json.Unmarshal(authorityFor(t, mintFor(t, c, "task-a2a-google-id", "platform")), &auth); err != nil {
		t.Fatal(err)
	}
	auth["audience"] = map[string]any{"conversation": "a2a:ctx-google-id", "kind": "dm"}
	auth["requester"] = map[string]any{
		"principal":  "hmac:fedcba9876543210fedcba9876543210",
		"backend":    "a2a-google",
		"verifiedBy": "a2a-google-token",
	}
	raw, err := json.Marshal(auth)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, "task-a2a-google-id", "ctx-google-id", "corr-google-id",
		messagePayload(t, "task-a2a-google-id", "ctx-google-id", "hello"), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(raw))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", "task-a2a-google-id"), env); err != nil {
		t.Fatal(err)
	}
	task := waitTerminal(t, c, "task-a2a-google-id")
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; strings.Contains(got, routeLostNote) {
		t.Fatalf("answer = %q carries the route-lost note for a Google A2A door", got)
	}
	puts := store.seen()
	want := routePut{
		path: "PUT /v1/sessions/a2a-ctx-google-id/route",
		auth: "Bearer kv-key",
		body: conversationRoute{
			Conversation: "a2a:ctx-google-id",
			ContextID:    "ctx-google-id",
			UserID:       "hmac:fedcba9876543210fedcba9876543210",
			ChatType:     "dm",
		},
	}
	if len(puts) != 1 || puts[0] != want {
		t.Fatalf("route PUTs = %+v, want %+v", puts, want)
	}
}

// Shared-credential doors (console with nats-grant, inject with inject-bearer,
// and static-token A2A with a2a-bearer) do not record a user identity route,
// so the memory provider treats the a2a-* session as an interactive
// unattributable DM rather than pooling or allowing self-asserted access to
// personal memory.
func TestAPI_SharedCredentialDoorsDoNotRecordUserRoute(t *testing.T) {
	cases := []struct {
		name         string
		taskID       string
		ctxID        string
		conversation string
		backend      string
		verifiedBy   string
	}{
		{
			name:         "console",
			taskID:       "task-console",
			ctxID:        "ctx-console",
			conversation: "console:tok-1",
			backend:      "console",
			verifiedBy:   "nats-grant",
		},
		{
			name:         "inject",
			taskID:       "task-inject",
			ctxID:        "ctx-inject",
			conversation: "inject:eval/run-id",
			backend:      "inject",
			verifiedBy:   "inject-bearer",
		},
		{
			name:         "a2a-bearer",
			taskID:       "task-a2a-bearer",
			ctxID:        "ctx-a2a-bearer",
			conversation: "a2a:ctx-a2a-bearer",
			backend:      "a2a",
			verifiedBy:   "a2a-bearer",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, url := startServer(t)
			store := newRouteStore(t, http.StatusOK)
			stub := newAPIStub(t, nil)
			startAPIBridge(t, url, stub, func(c *Config) { c.RouteURL, c.RouteKey = store.srv.URL, "kv-key" })
			c := gatewayClient(t, url)

			var auth map[string]any
			if err := json.Unmarshal(authorityFor(t, mintFor(t, c, tc.taskID, "platform")), &auth); err != nil {
				t.Fatal(err)
			}
			auth["audience"] = map[string]any{"conversation": tc.conversation, "kind": "dm"}
			auth["requester"] = map[string]any{
				"principal":  "hmac:fedcba9876543210fedcba9876543210",
				"backend":    tc.backend,
				"verifiedBy": tc.verifiedBy,
			}
			raw, err := json.Marshal(auth)
			if err != nil {
				t.Fatal(err)
			}
			env, err := lib.NewMessageEnvelope(gatewayParty, tc.taskID, tc.ctxID, "corr-"+tc.taskID,
				messagePayload(t, tc.taskID, tc.ctxID, "hello"), lib.WithTo(lib.Party{Session: "platform"}), lib.WithAuthority(raw))
			if err != nil {
				t.Fatal(err)
			}
			if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", tc.taskID), env); err != nil {
				t.Fatal(err)
			}
			task := waitTerminal(t, c, tc.taskID)
			if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; strings.Contains(got, routeLostNote) {
				t.Fatalf("answer = %q carries the route-lost note for %s door", got, tc.name)
			}
			if puts := store.seen(); len(puts) != 0 {
				t.Fatalf("route PUTs = %+v, want none", puts)
			}
		})
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

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// routeEnvelope is a submission whose gateway authority names conversation.
func routeEnvelope(t *testing.T, contextID, conversation string) *lib.Envelope {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"audience": map[string]any{"conversation": conversation}})
	if err != nil {
		t.Fatal(err)
	}
	return &lib.Envelope{ContextID: contextID, Authority: raw}
}

// A remembered record older than routeMemoryTTL is not trusted: session-kv
// may have pruned it, so a failed PUT then is reported as a lost route.
func TestRecordRoute_AStaleMemoryIsNotTrusted(t *testing.T) {
	store := newRouteStore(t, http.StatusOK)
	b := &Bridge{cfg: Config{RouteURL: store.srv.URL, RouteKey: "kv-key", Logger: discardLogger()}, routeClient: http.DefaultClient}
	env := routeEnvelope(t, "ctx-old", "slack:dm/D9")
	now := time.Unix(1_000_000, 0)
	routeNow = func() time.Time { return now }
	t.Cleanup(func() { routeNow = time.Now })

	if err := b.recordRoute(testCtx(t), "a2a-ctx-old", env); err != nil {
		t.Fatalf("first record: %v", err)
	}
	store.setStatus(http.StatusInternalServerError)
	now = now.Add(routeMemoryTTL - time.Minute)
	if err := b.recordRoute(testCtx(t), "a2a-ctx-old", env); err != nil {
		t.Fatalf("a failed PUT inside the TTL = %v, want nil: the earlier record stands", err)
	}
	now = now.Add(2 * time.Minute)
	if err := b.recordRoute(testCtx(t), "a2a-ctx-old", env); err == nil {
		t.Fatal("a failed PUT past the TTL = nil, want the error: the store may have pruned the record")
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
