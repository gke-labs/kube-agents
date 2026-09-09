package gateway

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
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
	tokenPath := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenPath, []byte("tok"), 0o600); err != nil {
		t.Fatal(err)
	}
	a, err := NewGoogleChatAdapter("http://relay.invalid", tokenPath, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	return a
}

// inbound is classify with the reason collapsed to a bool — the shape most
// of the table tests below want.
func (a *GoogleChatAdapter) inbound(ev *gchatEvent) (InboundMessage, bool) {
	msg, reason := a.classify(ev)
	return msg, reason == ""
}

func gchatMsg(spaceName, spaceType, threadingState, threadName, msgName, text, argumentText, senderEmail, senderType string) *gchatEvent {
	ev := &gchatEvent{Type: "MESSAGE"}
	ev.Space.Name = spaceName
	ev.Space.SpaceType = spaceType
	ev.Space.SpaceThreadingState = threadingState
	ev.Message.Name = msgName
	ev.Message.Text = text
	if argumentText != "" {
		ev.Message.ArgumentText = &argumentText
	}
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

// TestGchatBareMentionShapes: Google's documented shape for a mention-only
// message is an EMPTY argumentText (stripping the mention leaves nothing),
// which JSON cannot distinguish from the field being absent unless the
// decoder keeps the difference. All three shapes — absent (DM, no mention),
// present-empty and present-blank (bare mention) — must do the right thing,
// or a bare @Kage becomes a task whose ask is the raw mention text.
func TestGchatBareMentionShapes(t *testing.T) {
	a := newTestGchatAdapter(t)

	absent := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M20", "hi", "", "u1@example.com", "HUMAN")
	if got, ok := a.inbound(absent); !ok || got.Text != "hi" {
		t.Errorf("absent argumentText must fall back to text: %+v ok=%v", got, ok)
	}

	empty := gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M21", "@Kage", "", "u2@example.com", "HUMAN")
	emptyArg := ""
	empty.Message.ArgumentText = &emptyArg
	if _, ok := a.inbound(empty); ok {
		t.Error("present-but-empty argumentText is a bare mention and must drop")
	}

	blank := gchatMsg("spaces/S1", "SPACE", "", "spaces/S1/threads/T9", "spaces/S1/messages/M22", "@Kage", " ", "u2@example.com", "HUMAN")
	if _, ok := a.inbound(blank); ok {
		t.Error("present-but-blank argumentText is a bare mention and must drop")
	}
}

// A GROUP_CHAT surface never supports reply threading, whatever thread name
// the event happens to carry (every message in an unthreaded space carries
// its own thread resource) — binding those threads would fragment the group
// chat into one session per ask.
func TestGchatGroupChatBindsTheSpaceEvenWithAThreadName(t *testing.T) {
	a := newTestGchatAdapter(t)
	ev := gchatMsg("spaces/G1", "GROUP_CHAT", "", "spaces/G1/threads/perMsg", "spaces/G1/messages/M23", "go", "", "u2@example.com", "HUMAN")
	got, ok := a.inbound(ev)
	if !ok || got.Conversation != "gchat:space/spaces/G1" {
		t.Errorf("GROUP_CHAT with a per-message thread bound %q, want the space", got.Conversation)
	}
}

// The seen map dedupes Pub/Sub redelivery, whose window is bounded — the map
// must be too, or a long-lived gateway leaks one entry per message forever.
func TestGchatSeenMapIsBounded(t *testing.T) {
	a := newTestGchatAdapter(t)
	for i := 0; i <= gchatSeenCap; i++ {
		ev := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", fmt.Sprintf("spaces/D1/messages/M%d", i), "x", "", "u1@example.com", "HUMAN")
		if _, ok := a.inbound(ev); !ok {
			t.Fatalf("message %d expected to deliver", i)
		}
	}
	// The first message has been evicted: a redelivery of it is a (wrong but
	// bounded) second turn, and the map holds no more than the cap.
	first := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M0", "x", "", "u1@example.com", "HUMAN")
	if _, ok := a.inbound(first); !ok {
		t.Error("the oldest entry should have been evicted at the cap")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.seen) > gchatSeenCap {
		t.Errorf("seen holds %d entries, cap is %d", len(a.seen), gchatSeenCap)
	}
}

func TestGchatOpenDirectAcceptsAPrefixedUserResource(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces/findDirectMessage"] = map[string]any{"name": "spaces/D9"}
	a := newTestGchatAdapterWithRelay(t, f)
	if _, err := a.OpenDirect("users/12345"); err != nil {
		t.Fatal(err)
	}
	if got := f.call(0).arguments["name"]; got != "users/12345" {
		t.Errorf("findDirectMessage name = %v; a users/-prefixed id must not be double-prefixed", got)
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

// Under app credentials Chat withholds member emails from
// spaces.members.list and refuses the email alias in user resource names —
// both measured live on 2026-09-09 against a real DM space. The adapter
// bridges the two identities from what it does see: every inbound event
// carries the sender's immutable users/{id} AND the asserted email.
func TestGchatRosterResolvesLearnedIDsToEmails(t *testing.T) {
	f := newFakeChatRelay(t)
	// The shape Chat actually returned: name and type, no email.
	f.responses["spaces.members/list"] = map[string]any{
		"memberships": []any{
			map[string]any{"member": map[string]any{"name": "users/100000000000000000042", "displayName": "Brian Naylor", "type": "HUMAN"}},
			map[string]any{"member": map[string]any{"name": "users/2", "type": "HUMAN"}},
		},
	}
	a := newTestGchatAdapterWithRelay(t, f)

	ev := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "THREADED_MESSAGES", "spaces/D1/threads/T1", "spaces/D1/messages/M1", "hi", "", "bnaylor@example.com", "HUMAN")
	ev.Message.Sender.Name = "users/100000000000000000042"
	if _, ok := a.inbound(ev); !ok {
		t.Fatal("inbound expected")
	}
	ids, complete, err := a.Roster("gchat:dm/spaces/D1")
	if err != nil || !complete {
		t.Fatalf("roster: %v complete=%v", err, complete)
	}
	if len(ids) != 2 || ids[0] != "bnaylor@example.com" || ids[1] != "users/2" {
		t.Errorf("roster = %v; a member who has spoken must resolve to the email the requester was verified as, the rest stay ids", ids)
	}
}

func TestGchatOpenDirectUsesTheLearnedIDForAKnownEmail(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces/findDirectMessage"] = map[string]any{"name": "spaces/D9"}
	a := newTestGchatAdapterWithRelay(t, f)

	ev := gchatMsg("spaces/S1", "SPACE", "THREADED_MESSAGES", "spaces/S1/threads/T1", "spaces/S1/messages/M1", "@Kage hi", " hi", "U1@example.com", "HUMAN")
	ev.Message.Sender.Name = "users/777"
	if _, ok := a.inbound(ev); !ok {
		t.Fatal("inbound expected")
	}
	// Case-insensitive on the email: the lookup key is not the principal.
	conv, err := a.OpenDirect("u1@example.com")
	if err != nil || conv != "gchat:dm/spaces/D9" {
		t.Fatalf("openDirect = %q, %v", conv, err)
	}
	if f.call(0).arguments["name"] != "users/777" {
		t.Errorf("findDirectMessage must use the immutable id Chat accepts under app auth, got %+v", f.call(0).arguments)
	}
}

// realGchatFixture loads one captured payload from testdata/gchat and
// base64-encodes it the way the relay hands events over. Every file there
// is real traffic from the Chat app in bnaylor-kagents-dev (2026-09-09),
// scrubbed only of identity fields and the one-time redirect token; field
// set, key order and every other byte are as Chat published them.
func realGchatFixture(t *testing.T, name string) (raw []byte, b64 string) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "gchat", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw, base64.StdEncoding.EncodeToString(raw)
}

// TestGchatRealAddonPayloads pins the wire shape Chat actually publishes for
// an app configured through the Workspace add-on surface: no top-level
// type, the message under chat.messagePayload. The first build of this
// adapter read only the legacy layout and acked every one of these away
// without a log line — the test exists so that cannot recur.
func TestGchatRealAddonPayloads(t *testing.T) {
	cases := []struct {
		file    string
		text    string
		msgName string
	}{
		{"addon-dm-plain.json", "Hi, here's some traffic", "spaces/_c2fixtureAA/messages/BP9QzqU063c.BP9QzqU063c"},
		// In a DM a typed "@app" is plain text to Chat: argumentText equals
		// text, nothing is stripped and no mention annotation is attached,
		// so it is delivered verbatim rather than treated as a bare mention.
		{"addon-dm-bare-mention-text.json", "@bkd-test", "spaces/_c2fixtureAA/messages/-IovU-AWTYU.-IovU-AWTYU"},
		{"addon-dm-mention-with-text.json", "@bkd-test what is your name", "spaces/_c2fixtureAA/messages/jhqZwM-35cA.jhqZwM-35cA"},
		{"addon-dm-hello.json", "hello", "spaces/_c2fixtureAA/messages/wh-7cP8oL1U.wh-7cP8oL1U"},
		// A reply inside a DM thread: threadReply true, the parent's thread
		// name. A DM binds the whole space, so it lands in the same session.
		{"addon-dm-thread-reply.json", "@bkd-test thread reply", "spaces/_c2fixtureAA/messages/jhqZwM-35cA.iuosuA3Wo-0"},
	}
	for _, c := range cases {
		t.Run(c.file, func(t *testing.T) {
			a := newTestGchatAdapter(t)
			_, b64 := realGchatFixture(t, c.file)
			ev, err := decodeGchatEvent(b64)
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			if ev.shape != gchatShapeAddon || ev.Type != "MESSAGE" {
				t.Fatalf("shape=%q type=%q", ev.shape, ev.Type)
			}
			msg, reason := a.classify(ev)
			if reason != "" {
				t.Fatalf("dropped: %s", reason)
			}
			if msg.Conversation != "gchat:dm/spaces/_c2fixtureAA" || msg.Kind != "dm" ||
				msg.AuthorID != "sender@example.com" || msg.MessageID != c.msgName || msg.Text != c.text {
				t.Errorf("normalized = %+v", msg)
			}
			// The learned id↔email pair comes from the same event.
			if a.userIDs["users/100000000000000000042"] != "sender@example.com" {
				t.Errorf("userIDs = %v", a.userIDs)
			}
			// A conversation key minted from a real space name survives
			// KV tokenization (the id starts with an underscore).
			if tok := kvKey(msg.Conversation); strings.ContainsAny(tok[len("sessions."):], "./: ") {
				t.Errorf("kvKey(%q) = %q leaks non-token characters", msg.Conversation, tok)
			}
		})
	}
}

// An add-on event that is not a message — a membership change, a button —
// decodes, is not a turn, and the reason names the payload that arrived.
func TestGchatAddonNonMessagePayloadDropsWithReason(t *testing.T) {
	a := newTestGchatAdapter(t)
	raw := `{"commonEventObject":{"hostApp":"CHAT"},"chat":{"user":{"name":"users/1","email":"u1@example.com","type":"HUMAN"},"eventTime":"2026-09-09T15:00:00Z","addedToSpacePayload":{"space":{"name":"spaces/S1","spaceType":"SPACE"},"interactionAdd":true}}}`
	ev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString([]byte(raw)))
	if err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(ev); !strings.Contains(reason, "addedToSpacePayload") {
		t.Errorf("reason = %q; must name the payload that arrived", reason)
	}
}

// Both wire shapes through Run: a legacy event and a real add-on event are
// each delivered exactly once and acked.
func TestGchatRunDeliversBothWireShapes(t *testing.T) {
	f := newFakeChatRelay(t)
	legacy := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M1", "legacy hi", "", "u1@example.com", "HUMAN")
	_, addon := realGchatFixture(t, "addon-dm-hello.json")
	acked, _ := f.serveEvents([]map[string]any{
		{"receipt": "r1", "data": b64GchatEvent(t, legacy), "messageId": "1"},
		{"receipt": "r2", "data": addon, "messageId": "2"},
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
		if settled == 2 {
			break
		}
		select {
		case <-deadline:
			t.Fatalf("acks = %v after 5s", *acked)
		case <-time.After(10 * time.Millisecond):
		}
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("Run returned %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 || got[0].Text != "legacy hi" || got[1].Text != "hello" || got[1].Conversation != "gchat:dm/spaces/_c2fixtureAA" {
		t.Errorf("delivered = %+v", got)
	}
}

// A DM space is threaded, and a reply belongs in the thread the ask was made
// in — measured live: an answer to a question asked inside a DM thread landed
// top-level. The session stays the whole space (the key carries no thread);
// only where the reply renders follows the latest inbound message.
func TestGchatDMRepliesFollowTheLatestAskThread(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/_c2fixtureAA/messages/R1"}
	a := newTestGchatAdapterWithRelay(t, f)

	// Nothing seen yet: a DM post is top-level.
	if _, err := a.Post("gchat:dm/spaces/_c2fixtureAA", "hello"); err != nil {
		t.Fatal(err)
	}
	if body := f.call(0).arguments["body"].(map[string]any); body["thread"] != nil {
		t.Errorf("first post threaded with nothing seen: %+v", body)
	}

	_, b64 := realGchatFixture(t, "addon-dm-thread-reply.json")
	ev, err := decodeGchatEvent(b64)
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(ev)
	if reason != "" || msg.Conversation != "gchat:dm/spaces/_c2fixtureAA" {
		t.Fatalf("classify: %+v %q", msg, reason)
	}
	if _, err := a.Post(msg.Conversation, "answer"); err != nil {
		t.Fatal(err)
	}
	c := f.call(1)
	body := c.arguments["body"].(map[string]any)
	thread, _ := body["thread"].(map[string]any)
	if thread["name"] != "spaces/_c2fixtureAA/threads/jhqZwM-35cA" || c.arguments["messageReplyOption"] != gchatReplyOption {
		t.Errorf("reply must follow the ask's thread: %+v", c.arguments)
	}
}

// Under default mode a progress artifact changes nothing on the rolling line,
// and an unchanged line must not be re-edited: each edit is a Chat
// messages.patch, and re-sending the same line per artifact is the
// rate-limit stampede the relay's coalescing exists to avoid.
func TestGchatDefaultDisplayModeDoesNotReEditAnUnchangedLine(t *testing.T) {
	r := startGchatRig(t, nil, true, func(c *Config) { c.DisplayMode = "default" })
	conv := "gchat:spaces/S1/threads/T3"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "u1@example.com", MessageID: "spaces/S1/messages/M2", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
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
	for _, step := range []string{"step one", "step two", "step three"} {
		if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: step}}}); err != nil {
			t.Fatal(err)
		}
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "done"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "done" {
				return true
			}
		}
		return false
	})
	working := 0
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "working") {
			working++
		}
	}
	if working != 1 {
		t.Fatalf("the unchanged working line was edited %d times; three progress artifacts must not re-send it", working)
	}
}

// The terminal edit is gated the same way as the rolling line: under default
// a canceled or failed task ends on its state, not on the last narration.
func TestGchatDefaultDisplayModeQuietsTheTerminalLineToo(t *testing.T) {
	r := startGchatRig(t, nil, true, func(c *Config) { c.DisplayMode = "default" })
	conv := "gchat:spaces/S1/threads/T4"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "u1@example.com", MessageID: "spaces/S1/messages/M3", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "SECRET-NARRATION"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "terminal edit", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "canceled") {
				return true
			}
		}
		return false
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "SECRET-NARRATION") {
			t.Fatalf("default mode leaked narration into the terminal line: %q", e)
		}
	}
}

// The allowlist is matched case-insensitively on the asserted address in
// both directions: entries are folded at build time, and the author is
// folded at lookup, so Google varying the case of an email never drops a
// listed sender. (The principal itself stays case-preserved; see
// resolvePrincipal.)
func TestGchatAllowlistFoldsTheAuthorCase(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	r.adapter.inbox <- InboundMessage{Conversation: "gchat:dm/spaces/D5", Kind: "dm", AuthorID: "User@Example.com", MessageID: "spaces/D5/messages/M1", Text: "hello"}
	origin := r.awaitTask(t, "platform")
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	if auth.Requester.Principal != NewPseudonymizer([]byte("test-salt")).Hash("User@Example.com") {
		t.Errorf("principal must be the asserted address as delivered, case preserved: %+v", auth.Requester)
	}
}

// Ingress is at-most-once by recorded decision: the event is acked BEFORE
// the handler runs. A well-meaning "ack after the durable publish" would
// flip this to at-least-once with an in-memory dedupe, and a redelivery
// across a restart would become a duplicate task.
func TestGchatRunAcksBeforeTheHandlerRuns(t *testing.T) {
	f := newFakeChatRelay(t)
	turn := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M1", "hi", "", "u1@example.com", "HUMAN")
	acked, _ := f.serveEvents([]map[string]any{
		{"receipt": "r1", "data": b64GchatEvent(t, turn), "messageId": "1"},
	})
	a := newTestGchatAdapterWithRelay(t, f)

	ackedWhenHandled := make(chan int, 1)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		done <- a.Run(ctx, func(m InboundMessage) {
			f.mu.Lock()
			n := len(*acked)
			f.mu.Unlock()
			ackedWhenHandled <- n
		})
	}()
	select {
	case n := <-ackedWhenHandled:
		if n != 1 {
			t.Fatalf("handler ran with %d acks recorded; the event must be settled before the handler sees it", n)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("handler never ran")
	}
	cancel()
	if err := <-done; err != nil && !errors.Is(err, context.Canceled) {
		t.Fatalf("Run returned %v", err)
	}
}

// An add-on event whose message.sender omits the email is filled from
// chat.user, which names the same person.
func TestGchatAddonSenderEmailFallsBackToChatUser(t *testing.T) {
	a := newTestGchatAdapter(t)
	raw := `{"chat":{"user":{"name":"users/1","email":"u1@example.com","type":"HUMAN"},"eventTime":"2026-09-09T15:00:00Z","messagePayload":{"space":{"name":"spaces/D1","spaceType":"DIRECT_MESSAGE"},"message":{"name":"spaces/D1/messages/M1","text":"hi","argumentText":"hi","thread":{"name":"spaces/D1/threads/M1"},"sender":{"name":"users/1","type":"HUMAN"}}}}}`
	ev, err := decodeGchatEvent(base64.StdEncoding.EncodeToString([]byte(raw)))
	if err != nil {
		t.Fatal(err)
	}
	msg, reason := a.classify(ev)
	if reason != "" || msg.AuthorID != "u1@example.com" {
		t.Errorf("classify = %+v %q; the sender email must come from chat.user when message.sender lacks it", msg, reason)
	}
}

// A failed state edit must not be remembered as sent: under default mode
// every later artifact renders the same line, so the next one is the retry.
func TestGchatDefaultDisplayModeRetriesAFailedStateEdit(t *testing.T) {
	r := startGchatRig(t, nil, true, func(c *Config) { c.DisplayMode = "default" })
	conv := "gchat:spaces/S1/threads/T5"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "u1@example.com", MessageID: "spaces/S1/messages/M4", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	r.adapter.mu.Lock()
	r.adapter.failEdits = 1
	r.adapter.mu.Unlock()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "step"}}}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line to land on the retry", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "working") {
				return true
			}
		}
		return false
	})
}

// A caller that arms the gchat relay in the Config but leaves Options.Backend
// unset must not get principal-map resolution against a Chat adapter: New
// derives the backend from the same config that selects the adapter.
func TestNewDerivesTheBackendFromTheConfig(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	cfg := &Config{
		NATSURL:            url,
		DefaultAddressee:   "platform",
		IdleTTL:            30 * time.Minute,
		AttributionSalt:    []byte("test-salt"),
		GchatRelayURL:      "http://relay.invalid",
		GchatAllowAllUsers: true,
	}
	g, err := New(Options{Client: client, Adapter: newFakeAdapter(), Config: cfg})
	if err != nil {
		t.Fatal(err)
	}
	if g.backend != gchatBackend {
		t.Fatalf("backend = %q; a gchat-armed config must select the gchat identity path", g.backend)
	}
}
