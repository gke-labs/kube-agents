package gateway

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestGchatConversationIDRoundTrip(t *testing.T) {
	cases := []struct {
		name                  string
		space, thread, kind   string
		want                  string
		wantSpace, wantThread string
	}{
		{"dm binds the space", "spaces/AAA", "spaces/AAA/threads/BBB", "dm", "gchat:dm/spaces/AAA", "spaces/AAA", ""},
		{"threaded space binds the thread", "spaces/AAA", "spaces/AAA/threads/BBB", "group", "gchat:spaces/AAA/threads/BBB", "spaces/AAA", "spaces/AAA/threads/BBB"},
		{"unthreaded space binds the space", "spaces/CCC", "", "group", "gchat:space/spaces/CCC", "spaces/CCC", ""},
	}
	for _, c := range cases {
		got := gchatConversationID(c.space, c.thread, c.kind)
		if got != c.want {
			t.Errorf("%s: gchatConversationID(%q,%q,%q) = %q, want %q", c.name, c.space, c.thread, c.kind, got, c.want)
			continue
		}
		space, thread, ok := gchatSpaceThread(got)
		if !ok || space != c.wantSpace || thread != c.wantThread {
			t.Errorf("%s: gchatSpaceThread(%q) = %q,%q,%v want %q,%q,true", c.name, got, space, thread, ok, c.wantSpace, c.wantThread)
		}
	}
	for _, bad := range []string{"discord:1/2", "slack:C1/1.0", "gchat:", "gchat:dm/", "gchat:space/", "gchat:threads/BBB", "gchat:spaces/AAA", "gchat:spaces/AAA/messages/M"} {
		if _, _, ok := gchatSpaceThread(bad); ok {
			t.Errorf("gchatSpaceThread(%q) parsed; must refuse", bad)
		}
	}
}

func newTestGchatAdapter(t *testing.T) *GoogleChatAdapter {
	t.Helper()
	return &GoogleChatAdapter{log: slog.Default(), seen: map[string]bool{}}
}

func gchatMsg(spaceName, spaceType, threadingState, threadName, msgName, text, argumentText, senderEmail, senderType string) *gchatEvent {
	ev := &gchatEvent{Type: "MESSAGE"}
	ev.Space.Name = spaceName
	ev.Space.SpaceType = spaceType
	ev.Space.SpaceThreadingState = threadingState
	ev.Message.Name = msgName
	ev.Message.Text = text
	ev.Message.ArgumentText = argumentText
	ev.Message.Thread.Name = threadName
	ev.Message.Sender.Email = senderEmail
	ev.Message.Sender.Type = senderType
	return ev
}

// TestGchatInboundNormalization pins which events become turns and how they
// normalize. Google Chat itself gates delivery — a Chat app receives a space
// message only when mentioned, and every DM — so unlike Discord and Slack
// there is no mention affordance to re-derive here; what the adapter owns is
// the surface binding (thread vs space vs DM) and the mention-stripped text.
func TestGchatInboundNormalization(t *testing.T) {
	a := newTestGchatAdapter(t)
	cases := []struct {
		name string
		ev   *gchatEvent
		want bool
		conv string
		kind string
		text string
	}{
		{"dm delivers on the space", gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "spaces/D1/threads/T1", "spaces/D1/messages/M1", "hi", "", "u1@example.com", "HUMAN"),
			true, "gchat:dm/spaces/D1", "dm", "hi"},
		{"legacy DM type field delivers", func() *gchatEvent {
			ev := gchatMsg("spaces/D2", "", "", "", "spaces/D2/messages/M2", "hi", "", "u1@example.com", "HUMAN")
			ev.Space.Type = "DM"
			return ev
		}(), true, "gchat:dm/spaces/D2", "dm", "hi"},
		{"unclassifiable space defaults to group, not dm", gchatMsg("spaces/S9", "", "", "", "spaces/S9/messages/M10", "x", "", "u2@example.com", "HUMAN"),
			true, "gchat:space/spaces/S9", "group", "x"},
		{"threaded space binds the thread and strips the mention", gchatMsg("spaces/S1", "SPACE", "THREADED_MESSAGES", "spaces/S1/threads/T9", "spaces/S1/messages/M3", "@Kage check the nodes", " check the nodes", "u2@example.com", "HUMAN"),
			true, "gchat:spaces/S1/threads/T9", "group", "check the nodes"},
		{"unthreaded space binds the space", gchatMsg("spaces/S2", "SPACE", "UNTHREADED_MESSAGES", "spaces/S2/threads/T2", "spaces/S2/messages/M4", "@Kage do it", " do it", "u2@example.com", "HUMAN"),
			true, "gchat:space/spaces/S2", "group", "do it"},
		{"group chat with no thread binds the space", gchatMsg("spaces/S3", "GROUP_CHAT", "", "", "spaces/S3/messages/M5", "@Kage go", " go", "u2@example.com", "HUMAN"),
			true, "gchat:space/spaces/S3", "group", "go"},
		{"non-message event drops", &gchatEvent{Type: "ADDED_TO_SPACE"}, false, "", "", ""},
		{"bot sender drops", gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M6", "x", "", "app@example.com", "BOT"),
			false, "", "", ""},
		{"missing email drops", gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M7", "x", "", "", "HUMAN"),
			false, "", "", ""},
		{"bare mention drops", gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M8", "@Kage", "  ", "u2@example.com", "HUMAN"),
			false, "", "", ""},
		{"missing space name drops", gchatMsg("", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M9", "x", "", "u2@example.com", "HUMAN"),
			false, "", "", ""},
		{"missing message name drops", gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "", "x", "", "u2@example.com", "HUMAN"),
			false, "", "", ""},
	}
	for _, c := range cases {
		got, ok := a.inbound(c.ev)
		if ok != c.want {
			t.Errorf("%s: delivered=%v want %v", c.name, ok, c.want)
			continue
		}
		if ok && (got.Conversation != c.conv || got.Kind != c.kind || got.Text != c.text ||
			got.AuthorID != c.ev.Message.Sender.Email || got.MessageID != c.ev.Message.Name) {
			t.Errorf("%s: got %+v", c.name, got)
		}
	}
}

func TestGchatInboundDeduplicates(t *testing.T) {
	a := newTestGchatAdapter(t)
	dup := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M1", "once", "", "u1@example.com", "HUMAN")
	if _, ok := a.inbound(dup); !ok {
		t.Fatal("first delivery expected")
	}
	if _, ok := a.inbound(dup); ok {
		t.Error("Pub/Sub is at-least-once; a duplicate message name must drop")
	}
}

// fakeChatRelay is an httptest server speaking the credential proxy's chat
// relay contract: POST /v1/chat/api with {resource, method, arguments} and a
// canned {"response": ...} per method, recording every call and the bearer
// token it arrived with.
type fakeChatRelay struct {
	t         *testing.T
	srv       *httptest.Server
	mu        sync.Mutex
	calls     []relayCall
	responses map[string]any // "spaces.messages/create" -> response body
	tokens    []string
}

type relayCall struct {
	resource  []string
	method    string
	arguments map[string]any
}

func newFakeChatRelay(t *testing.T) *fakeChatRelay {
	f := &fakeChatRelay{t: t, responses: map[string]any{}}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.tokens = append(f.tokens, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if r.URL.Path != "/v1/chat/api" || r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var body struct {
			Resource  []string       `json:"resource"`
			Method    string         `json:"method"`
			Arguments map[string]any `json:"arguments"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		f.calls = append(f.calls, relayCall{body.Resource, body.Method, body.Arguments})
		key := strings.Join(body.Resource, ".") + "/" + body.Method
		resp, ok := f.responses[key]
		if !ok {
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(map[string]any{"error": "Google Chat operation failed"})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"response": resp})
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeChatRelay) call(i int) relayCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	if i >= len(f.calls) {
		f.t.Fatalf("relay call %d not made; have %d", i, len(f.calls))
	}
	return f.calls[i]
}

func newTestGchatAdapterWithRelay(t *testing.T, f *fakeChatRelay) *GoogleChatAdapter {
	t.Helper()
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("tok-1"), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := NewGoogleChatAdapter(f.srv.URL, tokenPath, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	return a
}

func TestGchatPostThreadsAndTranslates(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M77"}
	a := newTestGchatAdapterWithRelay(t, f)

	id, err := a.Post("gchat:spaces/S1/threads/T9", "⚙️ **working**")
	if err != nil || id != "spaces/S1/messages/M77" {
		t.Fatalf("post: id=%q err=%v", id, err)
	}
	c := f.call(0)
	if c.method != "create" || c.arguments["parent"] != "spaces/S1" {
		t.Errorf("create = %+v", c)
	}
	body := c.arguments["body"].(map[string]any)
	if body["text"] != "⚙️ *working*" {
		t.Errorf("text = %q", body["text"])
	}
	if body["thread"].(map[string]any)["name"] != "spaces/S1/threads/T9" {
		t.Errorf("thread = %+v", body["thread"])
	}
	if c.arguments["messageReplyOption"] != "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" {
		t.Errorf("messageReplyOption = %v", c.arguments["messageReplyOption"])
	}

	if _, err := a.Post("gchat:dm/spaces/D1", "hi"); err != nil {
		t.Fatal(err)
	}
	dm := f.call(1)
	dmBody := dm.arguments["body"].(map[string]any)
	if _, hasThread := dmBody["thread"]; hasThread {
		t.Error("DM posts must not set a thread")
	}
	if _, hasOpt := dm.arguments["messageReplyOption"]; hasOpt {
		t.Error("DM posts must not set messageReplyOption")
	}

	if _, err := a.Post("discord:1/2", "x"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestGchatEditPatchesText(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/S1/messages/M77"}
	a := newTestGchatAdapterWithRelay(t, f)

	if err := a.Edit("gchat:spaces/S1/threads/T9", "spaces/S1/messages/M77", "✅ **completed**"); err != nil {
		t.Fatal(err)
	}
	c := f.call(0)
	if c.method != "patch" || c.arguments["name"] != "spaces/S1/messages/M77" || c.arguments["updateMask"] != "text" {
		t.Errorf("patch = %+v", c)
	}
	if c.arguments["body"].(map[string]any)["text"] != "✅ *completed*" {
		t.Errorf("text = %q", c.arguments["body"].(map[string]any)["text"])
	}
	if err := a.Edit("nonsense", "m", "x"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestGchatRosterReadsSpaceMembers(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.members/list"] = map[string]any{
		"memberships": []any{
			map[string]any{"member": map[string]any{"name": "users/1", "email": "u1@example.com", "type": "HUMAN"}},
			map[string]any{"member": map[string]any{"name": "users/2", "type": "HUMAN"}},
			map[string]any{"member": map[string]any{"name": "users/app", "type": "BOT"}},
		},
	}
	a := newTestGchatAdapterWithRelay(t, f)

	ids, complete, err := a.Roster("gchat:spaces/S1/threads/T9")
	if err != nil || !complete {
		t.Fatalf("roster: %v %v %v", ids, complete, err)
	}
	// Emails where the backend surfaced one (they resolve to principals),
	// the immutable users/ id where it did not, and never the app itself.
	if len(ids) != 2 || ids[0] != "u1@example.com" || ids[1] != "users/2" {
		t.Errorf("ids = %v", ids)
	}
	if f.call(0).arguments["parent"] != "spaces/S1" {
		t.Errorf("list = %+v", f.call(0))
	}

	f.responses["spaces.members/list"] = map[string]any{
		"memberships":   []any{map[string]any{"member": map[string]any{"name": "users/1", "email": "u1@example.com", "type": "HUMAN"}}},
		"nextPageToken": "more",
	}
	if _, complete, _ := a.Roster("gchat:spaces/S1/threads/T9"); complete {
		t.Error("a next page token means the roster is incomplete")
	}
	if _, _, err := a.Roster("discord:1/2"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestGchatOpenDirect(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces/findDirectMessage"] = map[string]any{"name": "spaces/D9"}
	a := newTestGchatAdapterWithRelay(t, f)

	conv, err := a.OpenDirect("u1@example.com")
	if err != nil || conv != "gchat:dm/spaces/D9" {
		t.Fatalf("openDirect = %q, %v", conv, err)
	}
	if f.call(0).arguments["name"] != "users/u1@example.com" {
		t.Errorf("findDirectMessage = %+v", f.call(0))
	}
}

func TestGchatOpenDirectFallsBackToSetup(t *testing.T) {
	f := newFakeChatRelay(t)
	// No findDirectMessage response canned: the relay answers 502, the
	// adapter falls back to spaces.setup.
	f.responses["spaces/setup"] = map[string]any{"name": "spaces/D10"}
	a := newTestGchatAdapterWithRelay(t, f)

	conv, err := a.OpenDirect("u1@example.com")
	if err != nil || conv != "gchat:dm/spaces/D10" {
		t.Fatalf("openDirect = %q, %v", conv, err)
	}
}

// The relay authenticates callers by projected ServiceAccount token, and the
// kubelet rotates that file — the adapter must read it per request, not once.
func TestGchatRelayTokenIsReadPerRequest(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("tok-1"), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := NewGoogleChatAdapter(f.srv.URL, tokenPath, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.Post("gchat:dm/spaces/D1", "one"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tokenPath, []byte("tok-2"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Post("gchat:dm/spaces/D1", "two"); err != nil {
		t.Fatal(err)
	}
	if f.tokens[0] != "tok-1" || f.tokens[1] != "tok-2" {
		t.Errorf("tokens = %v; a rotated projected token must be picked up", f.tokens)
	}
}

func TestToGchatText(t *testing.T) {
	cases := map[string]string{
		"⚙️ **working** — checking nodes":    "⚙️ *working* — checking nodes",
		"see [the doc](https://x.example/p)": "see <https://x.example/p|the doc>",
		"plain text":                         "plain text",
		"**a** and **b**":                    "*a* and *b*",
		// Executor text is model output; Chat parses <users/…> mentions out
		// of message text, so an injected ping-all must arrive defanged —
		// visibly, not with invisible characters.
		"<users/all> deploy done": "< users/all> deploy done",
		"ping <users/123> now":    "ping < users/123> now",
	}
	for in, want := range cases {
		if got := toGchatText(in); got != want {
			t.Errorf("toGchatText(%q) = %q, want %q", in, got, want)
		}
	}
}

// serveEvents arms the fake relay's event routes: GET /v1/chat/a2a/events
// pops one envelope per poll, POST …/ack and …/nack record receipts.
func (f *fakeChatRelay) serveEvents(envelopes []map[string]any) (acked, nacked *[]string) {
	var acks, nacks []string
	acked, nacked = &acks, &nacks
	prev := f.srv.Config.Handler
	f.srv.Config.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/v1/chat/a2a/events":
			var ev map[string]any
			if len(envelopes) > 0 {
				ev, envelopes = envelopes[0], envelopes[1:]
			}
			f.mu.Unlock()
			json.NewEncoder(w).Encode(map[string]any{"event": ev})
			return
		case r.Method == http.MethodPost && r.URL.Path == "/v1/chat/a2a/events/ack":
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			acks = append(acks, body["receipt"])
			f.mu.Unlock()
			json.NewEncoder(w).Encode(map[string]any{"settled": true})
			return
		case r.Method == http.MethodPost && r.URL.Path == "/v1/chat/a2a/events/nack":
			var body map[string]string
			json.NewDecoder(r.Body).Decode(&body)
			nacks = append(nacks, body["receipt"])
			f.mu.Unlock()
			json.NewEncoder(w).Encode(map[string]any{"settled": true})
			return
		}
		f.mu.Unlock()
		prev.ServeHTTP(w, r)
	})
	return acked, nacked
}

func b64GchatEvent(t *testing.T, ev *gchatEvent) string {
	t.Helper()
	raw, err := json.Marshal(ev)
	if err != nil {
		t.Fatal(err)
	}
	return base64.StdEncoding.EncodeToString(raw)
}

// TestGchatRunDeliversAcksAndSwallowsPoison pins the pull loop: a turn is
// delivered and acked; a non-turn event is acked without delivery; a
// malformed payload is ACKED, not nacked — the legacy seam's recorded hole
// is a poison message that is never settled and redelivers forever
// (tests/integration/test_seam_chat_ingress.py), and this adapter must not
// replicate it.
func TestGchatRunDeliversAcksAndSwallowsPoison(t *testing.T) {
	f := newFakeChatRelay(t)
	turn := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M1", "hi", "", "u1@example.com", "HUMAN")
	nonTurn := &gchatEvent{Type: "ADDED_TO_SPACE"}
	acked, nacked := f.serveEvents([]map[string]any{
		{"receipt": "r1", "data": b64GchatEvent(t, turn), "messageId": "1"},
		{"receipt": "r2", "data": "not!!!base64", "messageId": "2"},
		{"receipt": "r3", "data": b64GchatEvent(t, nonTurn), "messageId": "3"},
	})
	a := newTestGchatAdapterWithRelay(t, f)

	var mu sync.Mutex
	var got []InboundMessage
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- a.Run(ctx, func(m InboundMessage) {
			mu.Lock()
			got = append(got, m)
			mu.Unlock()
		})
	}()

	deadline := time.After(5 * time.Second)
	for {
		f.mu.Lock()
		settled := len(*acked)
		f.mu.Unlock()
		if settled == 3 {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("acks = %v nacks = %v after 5s", *acked, *nacked)
		case <-time.After(10 * time.Millisecond):
		}
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("Run returned %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || got[0].Conversation != "gchat:dm/spaces/D1" || got[0].Text != "hi" {
		t.Errorf("delivered = %+v", got)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(*nacked) != 0 {
		t.Errorf("nacked = %v; poison must be acked away, not redelivered forever", *nacked)
	}
}

// startGchatRig assembles a gateway with the gchat backend semantics — no
// mapping table; identity resolution is the identity function gated by the
// allowlist — on the embedded server, with the fake adapter standing in for
// the Chat relay.
func startGchatRig(t *testing.T, allowed []string, allowAll bool, opts ...func(*Config)) *rig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	adapter := newFakeAdapter()
	cfg := &Config{
		NATSURL:            url,
		DefaultAddressee:   "platform",
		IdleTTL:            30 * time.Minute,
		AttributionSalt:    []byte("test-salt"),
		GchatAllowedUsers:  allowed,
		GchatAllowAllUsers: allowAll,
	}
	for _, o := range opts {
		o(cfg)
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "gchat"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()

	return &rig{g: g, adapter: adapter, client: client, bus: bus, url: url}
}

// TestGchatTurnCarriesVerifiedEmailPrincipal is the identity property the
// backend exists for: the Google-asserted email IS the principal — hashed
// identically as principal and as backend subject, so the cross-surface
// audit join holds — and verifiedBy names the mechanism, not the backend.
func TestGchatTurnCarriesVerifiedEmailPrincipal(t *testing.T) {
	r := startGchatRig(t, []string{"U1@Example.com"}, false)
	r.adapter.inbox <- InboundMessage{
		Conversation: "gchat:spaces/S1/threads/T1", Kind: "group",
		AuthorID: "u1@example.com", MessageID: "spaces/S1/messages/M1", Text: "how is the fleet?",
	}

	origin := r.awaitTask(t, "platform")
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatalf("authority block: %v", err)
	}
	want := NewPseudonymizer([]byte("test-salt")).Hash("u1@example.com")
	if auth.Requester.Principal != want {
		t.Errorf("principal = %q, want the hashed email %q", auth.Requester.Principal, want)
	}
	if auth.Requester.Subject != want {
		t.Errorf("subject = %q, want the same hash — the email is both the backend id and the principal", auth.Requester.Subject)
	}
	if auth.Requester.Backend != "gchat" || auth.Requester.VerifiedBy != "chat-event-topic-iam" {
		t.Errorf("requester = %+v", auth.Requester)
	}
}

// TestGchatUnlistedSenderDropsVisiblyOnce: the allowlist is the ingress gate
// the legacy path already has, and the drop is observable — one notice per
// sender, naming the sender's own id so the admin knows what to add. (The
// sender's own id in the sender's own conversation is not an oracle; the
// email is already on the message above the notice.)
func TestGchatUnlistedSenderDropsVisiblyOnce(t *testing.T) {
	r := startGchatRig(t, []string{"u1@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T1"
	for _, id := range []string{"spaces/S1/messages/M1", "spaces/S1/messages/M2"} {
		r.adapter.inbox <- InboundMessage{
			Conversation: conv, Kind: "group",
			AuthorID: "intruder@example.com", MessageID: id, Text: "do a thing",
		}
	}
	waitFor(t, "the unverified-sender notice", func() bool {
		return len(r.adapter.postTexts()) >= 1
	})
	time.Sleep(200 * time.Millisecond)
	posts := r.adapter.postTexts()
	if len(posts) != 1 {
		t.Fatalf("posts = %v, want exactly one notice for two messages", posts)
	}
	if !strings.Contains(posts[0], "can't verify") || !strings.Contains(posts[0], "intruder@example.com") {
		t.Errorf("notice %q should say what happened and which id to add", posts[0])
	}
	if !strings.Contains(posts[0], "allowed users list") {
		t.Errorf("notice %q should name the gchat remedy, not the principal map", posts[0])
	}
	if got := len(inSubjectEnvelopes(t, r.url, "platform")); got != 0 {
		t.Errorf("%d task envelopes published for an unlisted sender", got)
	}
}

func TestGchatAllowAllResolvesAnySender(t *testing.T) {
	r := startGchatRig(t, nil, true)
	r.adapter.inbox <- InboundMessage{
		Conversation: "gchat:dm/spaces/D1", Kind: "dm",
		AuthorID: "anyone@example.com", MessageID: "spaces/D1/messages/M1", Text: "hello",
	}
	origin := r.awaitTask(t, "platform")
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	if auth.Requester.Principal != NewPseudonymizer([]byte("test-salt")).Hash("anyone@example.com") {
		t.Errorf("requester = %+v", auth.Requester)
	}
}

// TestGchatDefaultDisplayModeQuietsProgressNarration: the existing Chat
// integration's default-vs-debug split (GoogleChatSpec.Mode), honoured by
// this relay rather than reinvented. Under default the rolling line carries
// the state but never the turn-by-turn narration; the result still posts.
// Debug — the gateway's historical behaviour, and the zero value — is pinned
// by TestReplyRelayAndRollingProgressLine.
func TestGchatDefaultDisplayModeQuietsProgressNarration(t *testing.T) {
	r := startGchatRig(t, nil, true, func(c *Config) { c.DisplayMode = "default" })
	conv := "gchat:spaces/S1/threads/T2"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "u1@example.com", MessageID: "spaces/S1/messages/M1", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "reading the fleet"}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "state edit", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "working") {
				return true
			}
		}
		return false
	})
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "the fleet is fine" {
				return true
			}
		}
		return false
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "reading the fleet") {
			t.Fatalf("default mode leaked progress narration into the rolling line: %q", e)
		}
	}
}

// The DoD's registry round-trip: a gchat key contains ':' and '/', both
// outside the KV token charset — it must survive kvKey's tokenization as one
// token, and distinct keys must not collide through the substitution.
func TestGchatKeySurvivesKVKeyTokenization(t *testing.T) {
	key := "gchat:spaces/AAAqqq/threads/BBBrrr"
	tok := kvKey(key)
	if !strings.HasPrefix(tok, "sessions.") {
		t.Fatalf("kvKey(%q) = %q, want sessions. prefix", key, tok)
	}
	if strings.ContainsAny(tok[len("sessions."):], "./: ") {
		t.Errorf("kvKey(%q) = %q leaks non-token characters", key, tok)
	}
	if kvKey("gchat:spaces/AAAqqq_threads/BBBrrr") == tok {
		t.Errorf("distinct gchat keys collide after sanitization")
	}
}
