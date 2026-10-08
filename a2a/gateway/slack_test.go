package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/slack-go/slack/socketmode"
)

// fakeSlackAPI fakes the five Web API calls the adapter makes; tests assert
// on what was posted/updated.
type fakeSlackAPI struct {
	// team is the team id auth.test answers with.
	team     string
	posted   []struct{ channel, thread, text string }
	updated  []struct{ channel, ts, text string }
	members  []string
	cursor   string
	openedIM string
}

func (f *fakeSlackAPI) AuthTestContext(context.Context) (*slack.AuthTestResponse, error) {
	return &slack.AuthTestResponse{UserID: "UBOT", User: "kage", TeamID: f.team}, nil
}

func (f *fakeSlackAPI) PostMessage(channelID string, options ...slack.MsgOption) (string, string, error) {
	_, values, err := slack.UnsafeApplyMsgOptions("tok", channelID, "https://slack.example/api/", options...)
	if err != nil {
		return "", "", err
	}
	f.posted = append(f.posted, struct{ channel, thread, text string }{
		values.Get("channel"), values.Get("thread_ts"), values.Get("text"),
	})
	return channelID, "999.001", nil
}

func (f *fakeSlackAPI) UpdateMessage(channelID, timestamp string, options ...slack.MsgOption) (string, string, string, error) {
	_, values, err := slack.UnsafeApplyMsgOptions("tok", channelID, "https://slack.example/api/", options...)
	if err != nil {
		return "", "", "", err
	}
	f.updated = append(f.updated, struct{ channel, ts, text string }{
		values.Get("channel"), timestamp, values.Get("text"),
	})
	return channelID, timestamp, "", nil
}

func (f *fakeSlackAPI) GetUsersInConversation(params *slack.GetUsersInConversationParameters) ([]string, string, error) {
	return f.members, f.cursor, nil
}

func (f *fakeSlackAPI) OpenConversation(params *slack.OpenConversationParameters) (*slack.Channel, bool, bool, error) {
	ch := &slack.Channel{}
	ch.ID = f.openedIM
	return ch, false, false, nil
}

func newTestSlackAdapter(api *fakeSlackAPI) *SlackAdapter {
	return &SlackAdapter{api: api, log: slog.Default(), botUserID: "UBOT",
		sessionThreads: map[string]bool{}, sessionExpiresAt: map[string]time.Time{}, seen: map[string]bool{},
		now: time.Now}
}

// fakeClock pins a SlackAdapter's clock to a value the test moves by hand,
// so an expiry is a fact the test arranged and not a sleep raced against
// the runner. TTLs are then comfortable and no statement has to run inside
// one.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func newFakeClock(a *SlackAdapter) *fakeClock {
	c := &fakeClock{t: time.Date(2026, 9, 30, 12, 0, 0, 0, time.UTC)}
	a.now = c.now
	return c
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *fakeClock) advance(d time.Duration) time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
	return c.t
}

// countingLookup is a SessionLookup a test wires as a.sessions. It answers
// from held (absent means false, nil) or fail (an error for that key), with
// until as the bound on every true (the zero value is a true that never
// expires, for tests that are not about expiry), and records every
// conversation it was asked about. The registry read is the one synchronous
// round trip isSessionThread makes on the event pump's own goroutine, so
// tests assert on how often it happens, not only on its answer. Mutexed
// because the gateway tests drive it beside real workers.
type countingLookup struct {
	mu    sync.Mutex
	held  map[string]bool
	until time.Time
	fail  map[string]error
	asked []string
}

func (l *countingLookup) lookup(_ context.Context, conversation string) (bool, time.Time, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.asked = append(l.asked, conversation)
	if err := l.fail[conversation]; err != nil {
		return false, time.Time{}, err
	}
	if !l.held[conversation] {
		return false, time.Time{}, nil
	}
	return true, l.until, nil
}

func (l *countingLookup) calls() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.asked)
}

// noSessions is a SessionLookup that holds nothing: the registry of a
// gateway that has started no task anywhere, which is what a cache miss
// asks in the adapter-only tests.
func noSessions(context.Context, string) (bool, time.Time, error) { return false, time.Time{}, nil }

// stalledSlackStub is a Slack Web API stub that parks every request until
// unblock is called, then answers invalid_auth (one of the four errors
// socketmode does not retry). unblock is idempotent and is also registered
// as a cleanup AHEAD of srv.Close, so a t.Fatal anywhere in the test releases
// the parked request before Close waits on it. Without that ordering a
// failing test hung in Close for the package timeout — ten minutes and a
// "blocked in Close after 5 seconds" line — instead of printing its Fatalf.
func stalledSlackStub(t *testing.T) (srv *httptest.Server, unblock func()) {
	t.Helper()
	release := make(chan struct{})
	var once sync.Once
	unblock = func() { once.Do(func() { close(release) }) }
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_auth"}`))
	}))
	t.Cleanup(func() { unblock(); srv.Close() })
	return srv, unblock
}

// TestSlackWebAPIClientIsBounded: a Post through the real constructor against
// a server that never answers returns by slackAPITimeout instead of parking.
// Driven through newSlackAdapter, not slackHTTPClient, because the claim is
// about the client the constructor hands slack-go: deleting the
// OptionHTTPClient line leaves the helper intact and the adapter unbounded.
// The extra options carry an unbounded OptionHTTPClient on purpose, so the
// test also holds the constructor to applying its own client last. The
// production bound is pinned as well, since the test shortens it.
func TestSlackWebAPIClientIsBounded(t *testing.T) {
	if slackAPITimeout <= 0 || slackAPITimeout > time.Minute {
		t.Fatalf("slackAPITimeout = %v, want a bound within a minute", slackAPITimeout)
	}
	orig := slackAPITimeout
	slackAPITimeout = 300 * time.Millisecond
	t.Cleanup(func() { slackAPITimeout = orig })

	srv, _ := stalledSlackStub(t)
	a := newSlackAdapter("xoxb-stub", "xapp-stub", slog.Default(),
		slack.OptionAPIURL(srv.URL+"/"), slack.OptionHTTPClient(&http.Client{}))
	done := make(chan error, 1)
	go func() {
		_, err := a.Post("slack:C1/1.0", "hello")
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Post against a server that never answers returned nil")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Post parked past the bound: the constructor did not hand slack-go the bounded client")
	}
}

// TestSlackDMMentionIsStripped: the bot's handle is addressing in a DM too,
// where Slack's composer offers it by autocomplete. "<@UBOT> stop" must reach
// the gateway as "stop" so the cancel affordance matches, and a bare mention
// in a DM is not a turn, as it is not in a channel.
func TestSlackDMMentionIsStripped(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	got, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", "<@UBOT> stop", "1.0", ""))
	if !ok || got.Text != "stop" || got.Conversation != "slack:dm/D1" {
		t.Fatalf("DM with a mention: delivered=%v text=%q conv=%q, want text \"stop\"", ok, got.Text, got.Conversation)
	}
	if _, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", "<@UBOT>", "2.0", "")); ok {
		t.Fatal("a bare mention in a DM has nothing to run")
	}
	got, ok = a.inbound(context.Background(), slackMsg("im", "D1", "U1", "plain ask", "3.0", ""))
	if !ok || got.Text != "plain ask" {
		t.Fatalf("DM without a mention: delivered=%v text=%q", ok, got.Text)
	}
}

// TestSlackRunAuthTestHonoursCancel: auth.test is the one Web API call Run
// makes before the pump exists. Against a server that never answers, a
// cancelled ctx must still bring Run back — that is SIGTERM during a
// stalled boot, which previously waited on the kubelet to kill the pod.
func TestSlackRunAuthTestHonoursCancel(t *testing.T) {
	srv, _ := stalledSlackStub(t)
	a := newSlackAdapter("xoxb-stub", "xapp-stub", slog.Default(), slack.OptionAPIURL(srv.URL+"/"))

	ctx, cancel := context.WithCancel(context.Background())
	returned := make(chan error, 1)
	go func() { returned <- a.Run(ctx, func(InboundMessage) {}) }()

	select {
	case err := <-returned:
		t.Fatalf("Run returned before cancel against a stalled auth.test: %v", err)
	case <-time.After(200 * time.Millisecond):
	}
	cancel()
	select {
	case err := <-returned:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Run returned %v after cancel, want a context.Canceled-wrapped auth.test error", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after cancel: auth.test is not under ctx")
	}
}

// TestEmptyPrincipalMapBootNotice: an empty map is a WARN on Discord, whose
// identity join IS the map and which drops every sender without it. On Slack
// the map is an optional override, so an empty one is an INFO that says
// listed senders are attributed by member id. gchat never reads it.
func TestEmptyPrincipalMapBootNotice(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	for _, tc := range []struct {
		backend string
		notice  string
		level   slog.Level
		found   bool
	}{
		{slackBackend, "the Slack principal map is empty", slog.LevelInfo, true},
		{discordBackend, "principal map is empty; every discord message", slog.LevelWarn, true},
		{gchatBackend, "principal map is empty", 0, false},
	} {
		t.Run(tc.backend, func(t *testing.T) {
			client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-"+tc.backend), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
			if err != nil {
				t.Fatalf("gateway client: %v", err)
			}
			t.Cleanup(client.Close)
			logs := &recordingHandler{}
			cfg := &Config{
				NATSURL:          url,
				PrincipalMapPath: filepath.Join(t.TempDir(), "no-such-map"),
				DefaultAddressee: "platform",
				IdleTTL:          30 * time.Minute,
				AttributionSalt:  []byte("test-salt"),
			}
			if _, err := New(Options{Client: client, Adapter: newFakeAdapter(), Config: cfg, Backend: tc.backend, Logger: slog.New(logs)}); err != nil {
				t.Fatalf("New: %v", err)
			}
			lvl, found := logs.level(tc.notice)
			if found != tc.found {
				t.Fatalf("backend %q: empty-map notice found=%t, want %t", tc.backend, found, tc.found)
			}
			if found && lvl != tc.level {
				t.Fatalf("backend %q: empty-map notice at %v, want %v", tc.backend, lvl, tc.level)
			}
		})
	}
}

// TestSlackMarkNeverDowngrades: a true in sessionThreads is the gateway's
// word (TaskStarted) or the registry's, and the only false that can follow
// it is a registry read that was in flight when it landed. That read must
// not win.
func TestSlackMarkNeverDowngrades(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.markSessionThread("C1/1.0", true)
	a.markSessionThread("C1/1.0", false)
	if !a.sessionThreads["C1/1.0"] {
		t.Fatal("a stale false overwrote a true")
	}
	a.markSessionThread("C1/2.0", false)
	a.markSessionThread("C1/2.0", true)
	if !a.sessionThreads["C1/2.0"] {
		t.Fatal("a true must still overwrite a cached false (the un-poison)")
	}
	a.markSessionThread("C1/3.0", false)
	if v, ok := a.sessionThreads["C1/3.0"]; !ok || v {
		t.Fatalf("a fresh false must be recorded; got ok=%v v=%v", ok, v)
	}
	if n := len(a.threadsOrder); n != 3 {
		t.Errorf("threadsOrder = %d entries, want 3: one slot per key, refusals included", n)
	}
}

// TestSlackGatewayDoesNotAdoptOnATasklessTurn: a session record is minted
// for any verified turn, so a mapped user's "@bot stop" with nothing running
// in someone else's thread leaves a record and starts no task. The registry
// lookup must answer false for it, or the thread is adopted the way
// TaskStarted never adopted it. An ask afterwards starts a task and does.
func TestSlackGatewayDoesNotAdoptOnATasklessTurn(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:jayanti\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack-stop"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)

	a := newTestSlackAdapter(&fakeSlackAPI{})
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
		// The CR-level gate is not under test here; the map is.
		SlackAllowAllUsers: true,
	}
	g, err := New(Options{Client: client, Adapter: a, Config: cfg, Backend: slackBackend, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	// A verified "stop" with nothing running: routed, answered, no task.
	g.handleInbound(InboundMessage{Conversation: "slack:C1/200.1", Kind: "group", AuthorID: "U1", MessageID: "4.0", Text: "stop"})
	rec, err := g.reg.Get(ctx, "slack:C1/200.1")
	if err != nil || rec == nil {
		t.Fatalf("the verified turn should have minted a record: rec=%v err=%v", rec, err)
	}
	if rec.ActiveTask != nil || len(rec.Tasks) != 0 {
		t.Fatalf("a stop with nothing running started a task: %+v", rec)
	}
	if held, _, err := a.sessions(ctx, "slack:C1/200.1"); err != nil || held {
		t.Fatalf("the lookup adopted a thread no task ever started in: held=%v err=%v", held, err)
	}
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "anyway, lunch?", "5.0", "200.1")); ok {
		t.Fatal("an unmentioned reply in a thread with a record but no task must not be a turn")
	}

	// The ask that does start a task adopts the thread, cache wiped or not.
	g.handleInbound(InboundMessage{Conversation: "slack:C1/200.1", Kind: "group", AuthorID: "U1", MessageID: "6.0", Text: "drain node 3"})
	if held, _, err := a.sessions(ctx, "slack:C1/200.1"); err != nil || !held {
		t.Fatalf("a started task must make the lookup answer true: held=%v err=%v", held, err)
	}
	a.mu.Lock()
	a.sessionThreads = map[string]bool{}
	a.threadsOrder = nil
	a.mu.Unlock()
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "stop", "7.0", "200.1")); !ok {
		t.Fatal("after a task started, the unmentioned follow-up must deliver from the registry")
	}
}

// TestSideDoorForwardsObserverAndLookupToASlackPrimary: with the inject door
// armed beside Slack (a dev or eval install), the gateway's TaskStarted and
// SetSessionLookup reach the composite, and the composite has to pass them
// on to the Slack primary or no thread is ever marked and the registry never
// reaches the adapter. The door's own conversations stay the door's.
func TestSideDoorForwardsObserverAndLookupToASlackPrimary(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	door, err := NewInjectAdapter("127.0.0.1:0", "side-door-test-token", time.Minute, nil)
	if err != nil {
		t.Fatal(err)
	}
	composite := WithSideDoor(a, door, nil)

	observer, ok := composite.(TaskObserver)
	if !ok {
		t.Fatal("the composite must implement TaskObserver")
	}
	observer.TaskStarted("slack:C1/9.0", "task-chat")
	if !a.sessionThreads["C1/9.0"] {
		t.Fatal("TaskStarted for a chat conversation did not reach the Slack primary")
	}
	observer.TaskStarted(injectKeyPrefix+"case-1", "task-door")
	if n := len(a.sessionThreads); n != 1 {
		t.Fatalf("the door's TaskStarted reached the Slack primary; sessionThreads = %v", a.sessionThreads)
	}
	observer.TaskAccepted("slack:C1/9.0", "task-chat")
	observer.CancelPublished("slack:C1/9.0", "task-chat")
	observer.TaskTerminal("slack:C1/9.0", "task-chat", lib.StateCompleted, TerminalFromExecutor, "")

	sink, ok := composite.(SessionLookupSink)
	if !ok {
		t.Fatal("the composite must implement SessionLookupSink")
	}
	sink.SetSessionLookup(func(context.Context, string) (bool, time.Time, error) { return true, time.Time{}, nil }, 7*time.Minute)
	a.mu.Lock()
	forwarded := a.sessions != nil
	ttl := a.sessionTTL
	a.mu.Unlock()
	if !forwarded {
		t.Fatal("SetSessionLookup did not reach the Slack primary")
	}
	if ttl != 7*time.Minute {
		t.Fatalf("the idle TTL did not reach the Slack primary: sessionTTL = %v, want 7m", ttl)
	}
}

// TestMuxForwardsObserverAndLookupToASlackPrimary: in production the Slack
// adapter sits behind the console mux, alone or with the inject door above
// it, so the gateway's TaskStarted and SetSessionLookup reach the mux first.
// Dropping them there leaves no thread ever marked and the registry never
// wired, and every unmentioned reply in a session thread drops.
func TestMuxForwardsObserverAndLookupToASlackPrimary(t *testing.T) {
	for _, withDoor := range []bool{false, true} {
		t.Run(map[bool]string{false: "mux on top", true: "door above the mux"}[withDoor], func(t *testing.T) {
			a := newTestSlackAdapter(&fakeSlackAPI{})
			mux, err := NewMultiAdapter(slackBackend, consoleBackend, map[string]Adapter{slackBackend: a, consoleBackend: newFakeAdapter()}, nil)
			if err != nil {
				t.Fatal(err)
			}
			var top Adapter = mux
			if withDoor {
				door, err := NewInjectAdapter("127.0.0.1:0", "side-door-test-token", time.Minute, nil)
				if err != nil {
					t.Fatal(err)
				}
				top = WithSideDoor(mux, door, nil)
			}
			observer, ok := top.(TaskObserver)
			if !ok {
				t.Fatal("the stack must implement TaskObserver")
			}
			observer.TaskStarted("slack:C1/9.0", "task-chat")
			observer.TaskStarted("console:tab-1", "task-console")
			if len(a.sessionThreads) != 1 || !a.sessionThreads["C1/9.0"] {
				t.Fatalf("TaskStarted did not reach the Slack adapter alone: sessionThreads = %v", a.sessionThreads)
			}
			observer.TaskAccepted("slack:C1/9.0", "task-chat")
			observer.CancelPublished("slack:C1/9.0", "task-chat")
			observer.TaskTerminal("slack:C1/9.0", "task-chat", lib.StateCompleted, TerminalFromExecutor, "")
			observer.TaskStarted("noprefix", "task-x")

			sink, ok := top.(SessionLookupSink)
			if !ok {
				t.Fatal("the stack must implement SessionLookupSink")
			}
			sink.SetSessionLookup(func(context.Context, string) (bool, time.Time, error) { return true, time.Time{}, nil }, 7*time.Minute)
			a.mu.Lock()
			forwarded, ttl := a.sessions != nil, a.sessionTTL
			a.mu.Unlock()
			if !forwarded || ttl != 7*time.Minute {
				t.Fatalf("SetSessionLookup did not reach the Slack adapter: forwarded=%v ttl=%v", forwarded, ttl)
			}
		})
	}
}

// TestSlackSessionMarkExpiresOnTheIdleTTL: a true in sessionThreads is not
// forever. It is stamped when written, and once the stamp is sessionTTL old
// the next unmentioned reply re-asks the registry and takes its answer,
// false included -- the one path a true gives way. A fresh TaskStarted
// marks the thread again; a registry that still holds the session restamps
// the entry, so it is asked once per TTL and not once per reply.
func TestSlackSessionMarkExpiresOnTheIdleTTL(t *testing.T) {
	ctx := context.Background()
	a := newTestSlackAdapter(&fakeSlackAPI{})
	clock := newFakeClock(a)
	reg := &countingLookup{}
	a.sessions = reg.lookup
	const ttl = 10 * time.Minute
	a.sessionTTL = ttl
	const key = "C1/9.0"
	cached := func() (v, ok bool, exp time.Time, stamped bool) {
		a.mu.Lock()
		defer a.mu.Unlock()
		v, ok = a.sessionThreads[key]
		exp, stamped = a.sessionExpiresAt[key]
		return v, ok, exp, stamped
	}

	a.TaskStarted("slack:"+key, "task-1")
	if _, _, exp, stamped := cached(); !stamped || !exp.Equal(clock.now().Add(ttl)) {
		t.Fatalf("TaskStarted must write a true that expires a TTL from now: stamped=%v exp=%v", stamped, exp)
	}
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "and the other one", "10.0", "9.0")); !ok {
		t.Fatal("an unmentioned reply inside the TTL must deliver")
	}
	if n := reg.calls(); n != 0 {
		t.Fatalf("a fresh true consulted the registry %d times, want 0", n)
	}

	clock.advance(ttl)
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "still there?", "11.0", "9.0")); ok {
		t.Fatal("an unmentioned reply past the TTL, with the registry answering false, must not be a turn")
	}
	if n := reg.calls(); n != 1 {
		t.Fatalf("the expired true consulted the registry %d times, want 1", n)
	}
	if v, ok, _, stamped := cached(); !ok || v || stamped {
		t.Fatalf("after the registry's false the cache must hold false and no expiry: v=%v ok=%v stamped=%v", v, ok, stamped)
	}

	// A new task in the thread: marked again, and the mark answers alone.
	a.TaskStarted("slack:"+key, "task-2")
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "thanks", "12.0", "9.0")); !ok {
		t.Fatal("after a fresh TaskStarted the unmentioned reply must deliver again")
	}
	if n := reg.calls(); n != 1 {
		t.Fatalf("a re-marked thread consulted the registry (%d calls, want still 1)", n)
	}

	// The registry still holds the session when the mark expires: the reply
	// delivers, the entry takes the bound the registry handed back, and the
	// next reply inside that bound does not ask again. The bound is the
	// registry's own, not now+TTL: the registry answers by LastActivity+TTL,
	// and the message that made the adapter ask may have moved no activity
	// (an unmapped sender's, refused by the gateway), so an entry stamped
	// from the adapter's clock would outlive the registry's answer by up to
	// a TTL. Here the registry says "true, for another 3 minutes".
	expired := clock.advance(ttl)
	until := expired.Add(3 * time.Minute)
	reg.mu.Lock()
	reg.held = map[string]bool{"slack:" + key: true}
	reg.until = until
	reg.mu.Unlock()
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "one more", "13.0", "9.0")); !ok {
		t.Fatal("past the TTL with the registry answering true, the reply must deliver")
	}
	if n := reg.calls(); n != 2 {
		t.Fatalf("the second expiry consulted the registry %d times in all, want 2", n)
	}
	if v, ok, exp, stamped := cached(); !ok || !v || !stamped || !exp.Equal(until) {
		t.Fatalf("a positive registry answer must expire the entry at the registry's until, not now+TTL: v=%v ok=%v exp=%v want %v", v, ok, exp, until)
	}
	clock.advance(2 * time.Minute)
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "and another", "14.0", "9.0")); !ok {
		t.Fatal("a reply inside the registry's bound must deliver")
	}
	if n := reg.calls(); n != 2 {
		t.Fatalf("a true inside the registry's bound consulted the registry again (%d calls, want still 2)", n)
	}
	// At the registry's bound -- 3 minutes on, well inside a TTL from the
	// adapter's own stamp -- the entry is expired and the registry is asked
	// again. This is the assertion that separates "expires at until" from
	// "expires at now+TTL".
	clock.advance(time.Minute)
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "still?", "15.0", "9.0")); !ok {
		t.Fatal("at the registry's bound with the registry still answering true, the reply must deliver")
	}
	if n := reg.calls(); n != 3 {
		t.Fatalf("the entry must expire at the registry's until: registry consulted %d times in all, want 3", n)
	}
}

// TestSlackExpiredMarkForceYieldsToAFreshTrue: the force path exists for an
// entry that was expired when the registry was asked. If a TaskStarted
// restamped it while that read was in flight, the read's false is the stale
// read the never-downgrade rule is for, and it must not win.
func TestSlackExpiredMarkForceYieldsToAFreshTrue(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	clock := newFakeClock(a)
	const ttl = 10 * time.Minute
	a.sessionTTL = ttl
	a.markSessionThread("C1/1.0", true)
	clock.advance(ttl)
	a.mu.Lock()
	expired := a.markExpiredLocked("C1/1.0")
	a.mu.Unlock()
	if !expired {
		t.Fatal("the mark should have expired")
	}
	a.markSessionThread("C1/1.0", true)           // the TaskStarted that raced the read
	a.setMark("C1/1.0", false, true, time.Time{}) // the read's answer, arriving late
	if !a.sessionThreads["C1/1.0"] {
		t.Fatal("a forced false overwrote a true that was restamped during the read")
	}
	// And with nothing racing, the forced false lands.
	clock.advance(ttl)
	a.setMark("C1/1.0", false, true, time.Time{})
	if v := a.sessionThreads["C1/1.0"]; v {
		t.Fatal("a forced false on an entry still expired must land")
	}
}

// TestSlackTaskTerminalExpiresTheMarkSoTheNextReplyReasks: while a task runs
// the cached positive carries a re-ask cadence, not a real bound. When the
// task ends the entry is made due, so the next unmentioned reply asks the
// registry and takes its LastActivity-based bound instead of riding the
// cadence past it. A DM key expires nothing.
func TestSlackTaskTerminalExpiresTheMarkSoTheNextReplyReasks(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	clock := newFakeClock(a)
	reg := &countingLookup{held: map[string]bool{}}
	a.sessions, a.sessionTTL = reg.lookup, 10*time.Minute

	a.TaskStarted("slack:C1/9.0", "task-1")
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "status?", "9.1", "9.0")); !ok || reg.calls() != 0 {
		t.Fatalf("during the task a reply must deliver from cache: delivered=%v lookups=%d", ok, reg.calls())
	}

	clock.advance(2 * time.Minute)
	a.TaskTerminal("slack:C1/9.0", "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	reg.held["slack:C1/9.0"] = true
	reg.until = clock.now().Add(10 * time.Minute)
	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "now prod too", "9.2", "9.0"))
	if !ok || got.Conversation != "slack:C1/9.0" || reg.calls() != 1 {
		t.Fatalf("after the terminal the reply must re-ask and deliver: delivered=%v conv=%q lookups=%d", ok, got.Conversation, reg.calls())
	}
	a.mu.Lock()
	exp := a.sessionExpiresAt["C1/9.0"]
	a.mu.Unlock()
	if !exp.Equal(reg.until) {
		t.Fatalf("after the re-ask the entry must carry the registry's bound: exp=%v want %v", exp, reg.until)
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "thanks", "9.3", "9.0")); !ok || reg.calls() != 1 {
		t.Fatalf("within the new bound the reply is served from cache: delivered=%v lookups=%d", ok, reg.calls())
	}

	// A DM is not tracked, and a thread with no positive has nothing to expire.
	a.TaskTerminal("slack:dm/D1", "task-dm", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskTerminal("slack:C1/77.0", "task-x", lib.StateCompleted, TerminalFromExecutor, "")
	a.mu.Lock()
	_, tracked := a.sessionExpiresAt["C1/77.0"]
	a.mu.Unlock()
	if tracked {
		t.Fatal("TaskTerminal on an untracked thread must record nothing")
	}
}

// TestSlackTaskTerminalWithoutALookupExpiresNothing: an embedder that offers
// no lookup gets a true that never expires (there is nothing to re-ask), and
// TaskTerminal must honour that rather than make the entry due and turn the
// next reply into a drop.
func TestSlackTaskTerminalWithoutALookupExpiresNothing(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.TaskStarted("slack:C1/9.0", "task-1")
	a.TaskTerminal("slack:C1/9.0", "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "and then?", "9.1", "9.0")); !ok {
		t.Fatal("with no lookup wired, a session thread must keep carrying replies after its task ends")
	}
}

// recordingHandler captures log records so a test can assert on the LEVEL a
// message came out at, not only on its text — the shutdown-path filters are
// entirely about level, and a test that only matched the words would pass
// against the WARN-on-every-SIGTERM behaviour they exist to remove. Mutexed
// because the pump logs from its own goroutine.
type recordingHandler struct {
	mu      sync.Mutex
	records []slog.Record
}

func (h *recordingHandler) Enabled(context.Context, slog.Level) bool { return true }

func (h *recordingHandler) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.records = append(h.records, r.Clone())
	return nil
}

func (h *recordingHandler) WithAttrs([]slog.Attr) slog.Handler { return h }
func (h *recordingHandler) WithGroup(string) slog.Handler      { return h }

// has reports whether any record carries the attribute key with value.
func (h *recordingHandler) has(key, value string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, r := range h.records {
		found := false
		r.Attrs(func(a slog.Attr) bool {
			if a.Key == key && a.Value.String() == value {
				found = true
				return false
			}
			return true
		})
		if found {
			return true
		}
	}
	return false
}

// level reports the level of the first record whose message contains sub.
func (h *recordingHandler) level(sub string) (slog.Level, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, r := range h.records {
		if strings.Contains(r.Message, sub) {
			return r.Level, true
		}
	}
	return 0, false
}

// slackEnvelope builds the socketmode.Event shape a real EventsAPI delivery
// has, Request and all — the field TestSlackRunAwaitsPumpGoroutine leaves nil
// and so routes around the ack entirely.
func slackEnvelope(envelopeID string, m *slackevents.MessageEvent) socketmode.Event {
	return socketmode.Event{
		Type:    socketmode.EventTypeEventsAPI,
		Request: &socketmode.Request{EnvelopeID: envelopeID},
		Data: slackevents.EventsAPIEvent{
			Type:       slackevents.CallbackEvent,
			InnerEvent: slackevents.EventsAPIInnerEvent{Data: m},
		},
	}
}

func slackMsg(channelType, channel, user, text, ts, threadTS string) *slackevents.MessageEvent {
	return &slackevents.MessageEvent{
		ChannelType: channelType, Channel: channel, User: user,
		Text: text, TimeStamp: ts, ThreadTimeStamp: threadTS,
	}
}

// TestSlackRefusesAnotherWorkspacesMember: a message whose sender belongs
// to another workspace (Slack Connect) is not a turn, whatever the
// allowlist says; a sender of ours in a shared channel, and any message
// that names no sender workspace, still is.
func TestSlackRefusesAnotherWorkspacesMember(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.teamID = "T0URS"
	ctx := context.Background()
	foreign := slackMsg("im", "D1", "UGUEST", "drain node 4", "1.0", "")
	foreign.UserTeam = "T0THER"
	if _, ok := a.inbound(ctx, foreign); ok {
		t.Error("a DM from another workspace's member was delivered as a turn")
	}
	mention := slackMsg("channel", "C1", "UGUEST", "<@UBOT> drain node 4", "2.0", "")
	mention.UserTeam = "T0THER"
	if _, ok := a.inbound(ctx, mention); ok {
		t.Error("a mention from another workspace's member in a shared channel was delivered as a turn")
	}
	ours := slackMsg("channel", "C1", "U1", "<@UBOT> how is the fleet?", "3.0", "")
	ours.UserTeam = "T0URS"
	if _, ok := a.inbound(ctx, ours); !ok {
		t.Error("our own member's mention in a shared channel was refused")
	}
	if _, ok := a.inbound(ctx, slackMsg("im", "D2", "U2", "hello", "4.0", "")); !ok {
		t.Error("a DM that names no sender workspace was refused")
	}
	// A shape that names the sender's workspace only in the message's own
	// team field is still checked.
	teamOnly := slackMsg("im", "D4", "UGUEST", "drain node 4", "4.5", "")
	teamOnly.Message = &slack.Msg{Team: "T0THER"}
	logs := &recordingHandler{}
	a.log = slog.New(logs)
	if _, ok := a.inbound(ctx, teamOnly); ok {
		t.Error("a message naming another workspace in its team field alone was delivered")
	}
	if !logs.has("messageTeam", "T0THER") {
		t.Error("the refusal's log line does not name the workspace from the team field")
	}
	a.log = slog.Default()
	// A guest's reply in a thread the gateway already holds as a session
	// thread, which needs no mention, and a guest's file share, take the
	// same refusal.
	a.sessionThreads["C1/100.1"] = true
	reply := slackMsg("channel", "C1", "UGUEST", "and node 5", "6.0", "100.1")
	reply.UserTeam = "T0THER"
	if _, ok := a.inbound(ctx, reply); ok {
		t.Error("another workspace's member steered a session thread")
	}
	share := slackMsg("im", "D5", "UGUEST", "see attached", "7.0", "")
	share.SubType, share.UserTeam = "file_share", "T0THER"
	if _, ok := a.inbound(ctx, share); ok {
		t.Error("another workspace's member's file share was delivered")
	}
	// No team id from auth.test: a message that names a sender workspace
	// cannot be shown to be ours, so it is refused.
	a.teamID = ""
	unknown := slackMsg("im", "D3", "U3", "hello", "5.0", "")
	unknown.UserTeam = "T0URS"
	if _, ok := a.inbound(ctx, unknown); ok {
		t.Error("with no team id known, a message naming a sender workspace was delivered")
	}
}

// TestSlackConnectedRecordsTheTeam: auth.test's team id is what the
// workspace check compares against.
func TestSlackConnectedRecordsTheTeam(t *testing.T) {
	api := &fakeSlackAPI{team: "T0URS"}
	a := newTestSlackAdapter(api)
	auth, err := api.AuthTestContext(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	a.connected(auth)
	if a.teamID != "T0URS" {
		t.Errorf("teamID = %q, want auth.test's T0URS", a.teamID)
	}
	// No team id: the check fails closed, and says so once at WARN.
	logs := &recordingHandler{}
	b := newTestSlackAdapter(&fakeSlackAPI{})
	b.log = slog.New(logs)
	b.connected(&slack.AuthTestResponse{UserID: "UBOT"})
	if lvl, found := logs.level("auth.test returned no team id"); !found || lvl != slog.LevelWarn {
		t.Errorf("no team id: warning found=%t level=%v, want WARN", found, lvl)
	}
}

func TestSlackPostThreadsAndTranslates(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	ts, err := a.Post("slack:C1/100.1", "⚙️ **working**")
	if err != nil || ts != "999.001" {
		t.Fatalf("post: ts=%q err=%v", ts, err)
	}
	p := api.posted[0]
	if p.channel != "C1" || p.thread != "100.1" || p.text != "⚙️ *working*" {
		t.Errorf("post = %+v", p)
	}
	if _, err := a.Post("slack:dm/D1", "hi"); err != nil {
		t.Fatal(err)
	}
	if api.posted[1].thread != "" {
		t.Error("DM posts must not set thread_ts")
	}
	if _, err := a.Post("discord:1/2", "x"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestSlackEditTranslates(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	if err := a.Edit("slack:C1/100.1", "100.2", "✅ **completed**"); err != nil {
		t.Fatal(err)
	}
	u := api.updated[0]
	if u.channel != "C1" || u.ts != "100.2" || u.text != "✅ *completed*" {
		t.Errorf("update = %+v", u)
	}
	if err := a.Edit("nonsense", "1", "x"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestSlackRosterReadsChannelMembers(t *testing.T) {
	api := &fakeSlackAPI{members: []string{"U1", "U2"}}
	a := newTestSlackAdapter(api)
	ids, complete, err := a.Roster("slack:C1/100.1")
	if err != nil || !complete || len(ids) != 2 {
		t.Fatalf("roster = %v %v %v", ids, complete, err)
	}
	api.cursor = "more"
	if _, complete, _ = a.Roster("slack:C1/100.1"); complete {
		t.Error("a next cursor means the roster is incomplete")
	}
	if _, _, err := a.Roster("discord:1/2"); err == nil {
		t.Error("malformed conversation must error")
	}
}

func TestSlackOpenDirect(t *testing.T) {
	api := &fakeSlackAPI{openedIM: "D9"}
	a := newTestSlackAdapter(api)
	conv, err := a.OpenDirect("U1")
	if err != nil || conv != "slack:dm/D9" {
		t.Fatalf("openDirect = %q, %v", conv, err)
	}
}

// TestSlackInboundAffordanceRule pins which messages become turns: DMs
// always; channel messages only when they mention the bot (the ask's own ts
// is the thread the session will live in); thread replies when they mention
// the bot or the thread is already a session thread (session threads carry
// every message — the parity with Discord's bot-created threads).
//
// The cases run in order against one adapter, because the rule is stateful:
// a task starting in a thread (TaskStarted, which the before hooks call
// where the gateway would) makes it a session thread for the cases after
// it, and nothing else does — a channel ask's own thread is no exception.
// 100.1 is the thread a channel ask rooted, 200.1 the one the bot is pulled
// into mid-conversation, and 300.1 the one it is never addressed in. The
// first two are one rule; the third is a separate thread for exactly that
// reason. The registry answers false throughout, so a cache miss drops.
func TestSlackInboundAffordanceRule(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.sessions = noSessions

	// rooted and adopted are the gateway's side of the two threads below:
	// the channel ask that rooted 100.1 and the mentioned ask in someone
	// else's 200.1 were each verified and started a task, and TaskStarted
	// is how the adapter learns that thread is now a session thread. Each
	// runs before the first case that needs it.
	rooted := func() { a.TaskStarted("slack:C1/100.1", "task-0") }
	adopted := func() { a.TaskStarted("slack:C1/200.1", "task-1") }
	cases := []struct {
		name   string
		before func()
		m      *slackevents.MessageEvent
		want   bool
		conv   string
		kind   string
		text   string
	}{
		{"dm delivers", nil, slackMsg("im", "D1", "U1", "hi", "1.0", ""), true, "slack:dm/D1", "dm", "hi"},
		{"channel without mention drops", nil, slackMsg("channel", "C1", "U1", "hello", "2.0", ""), false, "", "", ""},
		{"channel mention roots a thread on the ask", nil, slackMsg("channel", "C1", "U1", "<@UBOT> do a thing", "3.5", ""), true, "slack:C1/3.5", "group", "do a thing"},
		{"display-name mention form strips", nil, slackMsg("channel", "C1", "U1", "<@UBOT|kage> do it", "3.6", ""), true, "slack:C1/3.6", "group", "do it"},
		{"thread reply with mention delivers", nil, slackMsg("channel", "C1", "U1", "<@UBOT> and this", "4.0", "200.1"), true, "slack:C1/200.1", "group", "and this"},
		// The mention above minted a session on slack:C1/200.1 and the
		// gateway started a task there (adopted), so that thread now
		// carries every message — the follow-up the user expects to be able
		// to steer or stop with.
		{"unmentioned follow-up in an adopted thread delivers", adopted, slackMsg("channel", "C1", "U1", "stop", "4.5", "200.1"), true, "slack:C1/200.1", "group", "stop"},
		// The channel ask that rooted 100.1 is not in this table because it
		// would change nothing: a channel mention is a turn, and marks no
		// thread. The gateway starting the task there does (rooted).
		{"reply in a thread a channel ask rooted drops until a task starts", nil, slackMsg("channel", "C1", "U3", "too early", "4.8", "100.1"), false, "", "", ""},
		{"reply in bot-rooted thread delivers unmentioned once a task started", rooted, slackMsg("channel", "C1", "U3", "steer it", "5.0", "100.1"), true, "slack:C1/100.1", "group", "steer it"},
		{"reply in plain thread drops", nil, slackMsg("channel", "C1", "U3", "chatter", "6.0", "300.1"), false, "", "", ""},
		{"bare mention drops", nil, slackMsg("channel", "C1", "U1", "<@UBOT>", "7.0", ""), false, "", "", ""},
		// Slack transmits &, < and > entity-encoded; the ask must reach the
		// executor as the user typed it.
		{"entities decode in a dm", nil, slackMsg("im", "D1", "U1", "get pods -n foo &amp;&amp; describe node &lt;name&gt;", "10.0", ""), true, "slack:dm/D1", "dm", "get pods -n foo && describe node <name>"},
		{"entities decode after the mention strip", nil, slackMsg("channel", "C1", "U1", "<@UBOT> scale web if cpu &gt; 80%", "11.0", ""), true, "slack:C1/11.0", "group", "scale web if cpu > 80%"},
		{"entities decode in a thread steer", nil, slackMsg("channel", "C1", "U3", "and &lt;this&gt; too", "12.0", "100.1"), true, "slack:C1/100.1", "group", "and <this> too"},
	}
	for _, c := range cases {
		if c.before != nil {
			c.before()
		}
		got, ok := a.inbound(context.Background(), c.m)
		if ok != c.want {
			t.Errorf("%s: delivered=%v want %v", c.name, ok, c.want)
			continue
		}
		if ok && (got.Conversation != c.conv || got.Text != c.text || got.Kind != c.kind ||
			got.AuthorID != c.m.User || got.MessageID != c.m.TimeStamp) {
			t.Errorf("%s: got %+v", c.name, got)
		}
	}
}

func TestSlackInboundFilters(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	if _, ok := a.inbound(context.Background(), slackMsg("im", "D1", "UBOT", "self", "1.0", "")); ok {
		t.Error("own messages must drop")
	}
	bot := slackMsg("im", "D1", "U9", "from an app", "2.0", "")
	bot.BotID = "B123"
	if _, ok := a.inbound(context.Background(), bot); ok {
		t.Error("bot messages must drop")
	}
	edited := slackMsg("im", "D1", "U1", "edited", "3.0", "")
	edited.SubType = "message_changed"
	if _, ok := a.inbound(context.Background(), edited); ok {
		t.Error("subtypes outside the turn set (plain, thread_broadcast, file_share) must drop")
	}
	dup := slackMsg("im", "D1", "U1", "once", "4.0", "")
	if _, ok := a.inbound(context.Background(), dup); !ok {
		t.Fatal("first delivery expected")
	}
	if _, ok := a.inbound(context.Background(), dup); ok {
		t.Error("socket mode is at-least-once; a duplicate (channel,ts) must drop")
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "", "U1", "<@UBOT> x", "5.0", "")); ok {
		t.Error("empty channel must drop")
	}
	if _, ok := a.inbound(context.Background(), slackMsg("im", "D1", "", "ghost", "6.0", "")); ok {
		t.Error("empty user must drop")
	}
}

func TestSlackConversationIDRoundTrip(t *testing.T) {
	cases := []struct {
		channelType, channel, threadTS string
		want                           string
		wantChannel, wantThread        string
	}{
		{"im", "D0AB1", "", "slack:dm/D0AB1", "D0AB1", ""},
		{"channel", "C042", "1725193344.000100", "slack:C042/1725193344.000100", "C042", "1725193344.000100"},
		{"group", "G777", "1700.42", "slack:G777/1700.42", "G777", "1700.42"},
		{"mpim", "C9", "1700.43", "slack:C9/1700.43", "C9", "1700.43"},
	}
	for _, c := range cases {
		got := slackConversationID(c.channelType, c.channel, c.threadTS)
		if got != c.want {
			t.Errorf("slackConversationID(%q,%q,%q) = %q, want %q", c.channelType, c.channel, c.threadTS, got, c.want)
		}
		ch, ts, ok := slackChannelThread(got)
		if !ok || ch != c.wantChannel || ts != c.wantThread {
			t.Errorf("slackChannelThread(%q) = %q,%q,%v want %q,%q,true", got, ch, ts, ok, c.wantChannel, c.wantThread)
		}
	}
	for _, bad := range []string{"discord:1/2", "slack:", "slack:C1", "slack:C1/", "slack:dm/", "slack:/100.1"} {
		if _, _, ok := slackChannelThread(bad); ok {
			t.Errorf("slackChannelThread(%q) parsed; must refuse", bad)
		}
	}
}

// The DoD's registry round-trip: a Slack key contains '.' and '/' and ':',
// all outside the KV token charset — it must survive kvKey's tokenization
// as one token, and distinct keys must not collide through the substitution.
func TestSlackKeySurvivesKVKeyTokenization(t *testing.T) {
	key := "slack:C042/1725193344.000100"
	tok := kvKey(key)
	if !strings.HasPrefix(tok, "sessions.") {
		t.Fatalf("kvKey(%q) = %q, want sessions. prefix", key, tok)
	}
	if strings.ContainsAny(tok[len("sessions."):], "./: ") {
		t.Errorf("kvKey(%q) = %q leaks non-token characters", key, tok)
	}
	if kvKey("slack:C042/1725193344_000100") == tok {
		t.Errorf("distinct slack keys collide after sanitization")
	}
}

func TestToMrkdwn(t *testing.T) {
	cases := map[string]string{
		"⚙️ **working** — checking nodes":    "⚙️ *working* — checking nodes",
		"see [the doc](https://x.example/p)": "see <https://x.example/p|the doc>",
		"plain text":                         "plain text",
		"**a** and **b**":                    "*a* and *b*",
		// Executor text is model output; Slack control sequences in it must
		// arrive escaped, or a prompt-injected result pings the room.
		"<!channel> deploy done": "&lt;!channel&gt; deploy done",
		"ping <@U999> now":       "ping &lt;@U999&gt; now",
		"a & b < c":              "a &amp; b &lt; c",
	}
	for in, want := range cases {
		if got := toMrkdwn(in); got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestToMrkdwnConvertsProseAfterAChunkedFence: Gateway.post splits a result
// with chatChunks(text, discordChunk) and the adapter translates each chunk
// on its own, so the chunker closes a fenced block it cuts and reopens it in
// the next chunk (TestChatChunksKeepFencesBalanced). The prose after the
// block then converts -- bold and links -- rather than riding to the end of
// the chunk as the content of a fence opened by the block's orphan closer.
// The fence lines before it are untouched.
func TestToMrkdwnConvertsProseAfterAChunkedFence(t *testing.T) {
	big := "```\n" + strings.Repeat("log line\n", 300) + "```\n**Summary:** see [runbook](https://x.example/r)"
	chunks := chatChunks(big, discordChunk)
	last := chunks[len(chunks)-1]
	if !strings.HasSuffix(last, "**Summary:** see [runbook](https://x.example/r)") {
		t.Fatalf("the last chunk does not carry the summary: %q", last)
	}
	got := toMrkdwn(last)
	wantTail := "```\n*Summary:* see <https://x.example/r|runbook>"
	if !strings.HasSuffix(got, wantTail) {
		t.Errorf("the summary after the cut block was not converted:\n got %q\nwant suffix %q", got, wantTail)
	}
	fence := strings.TrimSuffix(last, "**Summary:** see [runbook](https://x.example/r)")
	if !strings.HasPrefix(got, fence) {
		t.Errorf("the fence lines before the summary were altered:\n got %q\nwant prefix %q", got, fence)
	}
}

// TestToMrkdwnRewritesBoldOnlyOnClosedPairs: the bold rewrite used to be a
// whole-string "**" -> "*", and executor output is full of "**" that is not
// bold -- a Python **kwargs, a **/*.yaml glob, a horizontal rule -- so the
// user read an altered answer. A pair is "**" on both sides of
// one-line content that starts and ends on a non-space, non-star character;
// "***" on both sides is the bold-italic form and becomes *_x_*. Anything
// else -- an opener that never closes on its line, a triple closed by a
// double, stars around spaces -- is left as written. Code spans (fenced,
// double-backtick, inline) are verbatim, and the only rewrite that reaches
// them is the control-sequence escaping, which is not a markdown rule.
func TestToMrkdwnRewritesBoldOnlyOnClosedPairs(t *testing.T) {
	cases := map[string]string{
		// The issue's rows: none of these is a bold pair.
		"**kwargs":                "**kwargs",
		"**/*.yaml":               "**/*.yaml",
		"a ** b":                  "a ** b",
		"***":                     "***",
		"def f(*args, **kwargs):": "def f(*args, **kwargs):",
		// Bold-italic is the one triple that is a pair, and a single star
		// inside a pair is emphasis inside it, not a second pair.
		"***x***":                     "*_x_*",
		"**Note: *not* recommended**": "*Note: *not* recommended*",
		// Code spans are verbatim: inline, double-backtick, fenced, and a
		// fence that never closes (it runs to the end, as CommonMark reads it).
		"`**x**`":                              "`**x**`",
		"``a ` **x**``":                        "``a ` **x**``",
		"```\n**x**\n```":                      "```\n**x**\n```",
		"```yaml\npaths: [\"**/*.yaml\"]\n```": "```yaml\npaths: [\"**/*.yaml\"]\n```",
		"```\nunclosed fence **x**":            "```\nunclosed fence **x**",
		// A pair beside a code span is still rewritten; the span is not.
		"**a** and `**b**` and **c**":                "*a* and `**b**` and *c*",
		"**bold** then ```**code**``` then **bold**": "*bold* then ```**code**``` then *bold*",
		// A pair that wraps a code span still converts (only its own stars
		// change), a link whose label holds a code span still converts, and
		// a link written inside a code span is code, not a link.
		"**`kubectl get pods`**":    "*`kubectl get pods`*",
		"see **`values.yaml`** for": "see *`values.yaml`* for",
		// And still converts when the span it wraps holds a `**` of its
		// own: the closer is sought outside code spans, so the span's
		// stars are neither a closer nor altered. The same span with no
		// pair around it, or with an opener that never closes, is as
		// written.
		"**`**kwargs`**":                            "*`**kwargs`*",
		"use **`**kwargs`** for":                    "use *`**kwargs`* for",
		"**see `a**b`**":                            "*see `a**b`*",
		"pass `**kwargs` through":                   "pass `**kwargs` through",
		"**`**kwargs` unclosed":                     "**`**kwargs` unclosed",
		"[`kubectl`](https://x.example/p)":          "<https://x.example/p|`kubectl`>",
		"run `[x](https://x.example/p)` as written": "run `[x](https://x.example/p)` as written",
		// Escaping is not a markdown rule and still reaches code: a
		// prompt-injected <!channel> in a code span must not ping the room.
		"<!channel> in `**code**`": "&lt;!channel&gt; in `**code**`",
		// One exponent is not a pair; two on a line are one to CommonMark
		// as well, and the adapter reads the markdown rather than guessing
		// at Python. Pinned so the residue is a decision, not a surprise.
		"2**8":            "2**8",
		"x = a**2 + b**2": "x = a*2 + b*2",
		// Not pairs: an opener that closes on a later line, stars around
		// spaces, a triple closed by a double and the reverse.
		"**open here\nclosed** there": "**open here\nclosed** there",
		"** x **":                     "** x **",
		"***x**":                      "***x**",
		"**x***":                      "**x***",
		// Single-star and underscore emphasis are not this rewrite's: Slack
		// reads *x* as bold and _x_ as italic, and both pass through as today.
		"*x* and _y_": "*x* and _y_",
		// A markdown link's destination is never altered: a pair whose stars
		// sit inside it is left alone, while a pair that wraps the whole
		// link still converts, since only its own stars change.
		"[doc](https://x.example/**a**/b)": "<https://x.example/**a**/b|doc>",
		"**[doc](https://x.example/p)**":   "*<https://x.example/p|doc>*",
		// A `**` inside a code span does not open a pair, and one inside a
		// link's destination does not close one: the pair is read from the
		// stars outside both, as CommonMark reads it (`a**b**` is bold b).
		"`**`a**b**":                           "`**`a*b*",
		"**x [doc](https://x.example/**a) y**": "*x <https://x.example/**a|doc> y*",
		// A bare URL is not a shape the adapter recognises (Slack auto-links
		// it), so a closed pair inside one is rewritten exactly as before.
		"see https://x.example/**a**/b": "see https://x.example/*a*/b",
	}
	for in, want := range cases {
		if got := toMrkdwn(in); got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestToMrkdwnRefusesURLShapedLabelsNamingAnotherHost: Slack renders a
// link's label, so [https://good.example](https://evil.example) read as a
// link to good.example that opened evil.example -- the pipe refusal
// (TestToMrkdwnRefusesPipesInsideLinkURLs) closes only the split-at-pipe
// route to the same display. A label that is itself URL-shaped and names a
// host other than the destination's is refused: the markdown is left as
// written, escaped, and no <...|...> is produced. The check is on the host,
// case-insensitively and without the port, for every URL a label carries
// anywhere in it, read as the label renders (emphasis and code marks and
// invisible format characters removed); a label that is prose is prose.
func TestToMrkdwnRefusesURLShapedLabelsNamingAnotherHost(t *testing.T) {
	cases := map[string]string{
		"[https://good.example](https://evil.example)":            "[https://good.example](https://evil.example)",
		"[https://good.example/path](https://evil.example/login)": "[https://good.example/path](https://evil.example/login)",
		"[http://good.example](https://evil.example)":             "[http://good.example](https://evil.example)",
		// The same host is an honest label, whatever the case or path.
		"[https://x.example/p](https://x.example/p)":     "<https://x.example/p|https://x.example/p>",
		"[HTTPS://X.EXAMPLE/p](https://x.example/q?a=1)": "<https://x.example/q?a=1|HTTPS://X.EXAMPLE/p>",
		// The URL is found anywhere in the label, so wrapping it in bold,
		// italic, a space or escaped angle brackets does not get it past
		// the check, and userinfo that reads as one host and parses as the
		// other is refused outright.
		"[**https://good.example**](https://evil.example)":          "[**https://good.example**](https://evil.example)",
		"[_https://good.example_](https://evil.example)":            "[_https://good.example_](https://evil.example)",
		"[ https://good.example](https://evil.example)":             "[ https://good.example](https://evil.example)",
		"[<https://good.example>](https://evil.example)":            "[&lt;https://good.example&gt;](https://evil.example)",
		"[https://good.example@evil.example](https://evil.example)": "[https://good.example@evil.example](https://evil.example)",
		// A mark inside the URL renders away, so the label is read without
		// it: after the scheme, splitting the scheme, or a zero-width space
		// in it. A space after the scheme leaves a claim with no host.
		"[https://**good.example**](https://evil.example)":   "[https://**good.example**](https://evil.example)",
		"[https://*good.example*](https://evil.example)":     "[https://*good.example*](https://evil.example)",
		"[https://_good.example_](https://evil.example)":     "[https://_good.example_](https://evil.example)",
		"[https://`good.example`](https://evil.example)":     "[https://`good.example`](https://evil.example)",
		"[**https**://good.example](https://evil.example)":   "[**https**://good.example](https://evil.example)",
		"[https:**//**good.example](https://evil.example)":   "[https:**//**good.example](https://evil.example)",
		"[`https`://good.example](https://evil.example)":     "[`https`://good.example](https://evil.example)",
		"[https:/\u200b/good.example](https://evil.example)": "[https:/\u200b/good.example](https://evil.example)",
		"[https:// good.example](https://evil.example)":      "[https:// good.example](https://evil.example)",
		// Every URL in the label is checked, not the first.
		"[https://evil.example or https://good.example](https://evil.example)": "[https://evil.example or https://good.example](https://evil.example)",
		// The same host, bolded, on another port, or followed by sentence
		// punctuation, is still an honest label.
		"[**https://x.example**](https://x.example/p)":        "<https://x.example/p|*https://x.example*>",
		"[https://good.example](https://good.example:8443/x)": "<https://good.example:8443/x|https://good.example>",
		"[Read https://x.example.](https://x.example)":        "<https://x.example|Read https://x.example.>",
		"[https://x.example, the docs](https://x.example/p)":  "<https://x.example/p|https://x.example, the docs>",
		// A bare hostname is not URL-shaped; that label is prose to this
		// check, the same as "the doc". So is a scheme Slack would not
		// auto-link; the check reads a URL, not a lookalike.
		"[good.example](https://evil.example)":        "<https://evil.example|good.example>",
		"[https:/good.example](https://evil.example)": "<https://evil.example|https:/good.example>",
		// A refused link beside an honest one: only the honest one converts.
		"[ok](https://x.example/p) and [https://good.example](https://evil.example)": "<https://x.example/p|ok> and [https://good.example](https://evil.example)",
	}
	for in, want := range cases {
		got := toMrkdwn(in)
		if got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
		// Belt and braces, for the URL-shaped labels: no live link to the
		// other host survives.
		if strings.Contains(in, "https://good.example") && strings.Contains(got, "<https://evil.example") {
			t.Errorf("toMrkdwn(%q) = %q produced a live link whose label names another host", in, got)
		}
	}
}

// TestToMrkdwnKeepsBalancedParenthesesInLinkURLs: the URL class used to end
// at the first ")", so a Wikipedia-style destination with a parenthesised
// segment converted to a link to a 404 with a stray ")" after it. CommonMark
// admits balanced parentheses in a destination; one level is admitted here,
// and the pipe refusal still holds inside the group.
func TestToMrkdwnKeepsBalancedParenthesesInLinkURLs(t *testing.T) {
	cases := map[string]string{
		"[Foo](https://en.wikipedia.org/wiki/Foo_(bar))":                "<https://en.wikipedia.org/wiki/Foo_(bar)|Foo>",
		"[Foo](https://en.wikipedia.org/wiki/Foo_(bar)) and (an aside)": "<https://en.wikipedia.org/wiki/Foo_(bar)|Foo> and (an aside)",
		"[a](https://x.example/p) (b)":                                  "<https://x.example/p|a> (b)",
		// Unbalanced: not a link, left as written.
		"[a](https://x.example/(p)": "[a](https://x.example/(p)",
		// A pipe inside the group is still refused.
		"[a](https://x.example/p(q|https://good.example))": "[a](https://x.example/p(q|https://good.example))",
	}
	for in, want := range cases {
		if got := toMrkdwn(in); got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestSlackTurnSubtypes: thread_broadcast is a steer with "also send to
// channel" checked and file_share is an ask with an attachment — both are
// genuine turns and must not vanish silently. Edits stay dropped.
func TestSlackTurnSubtypes(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.sessions = noSessions
	// The gateway started a task in 100.1, so it is a session thread.
	a.TaskStarted("slack:C1/100.1", "task-1")

	broadcast := slackMsg("channel", "C1", "U2", "also try the east cluster", "8.0", "100.1")
	broadcast.SubType = "thread_broadcast"
	if _, ok := a.inbound(context.Background(), broadcast); !ok {
		t.Error("thread_broadcast reply in a session thread must deliver")
	}

	file := slackMsg("im", "D1", "U1", "here is the manifest", "9.0", "")
	file.SubType = "file_share"
	if _, ok := a.inbound(context.Background(), file); !ok {
		t.Error("file_share with text must deliver")
	}
}

// TestSlackUnescaper: the decode is the exact inverse of Slack's own inbound
// escaping and nothing wider. The literal cases are the reason this is one
// strings.Replacer and not a sequence of ReplaceAll calls — a Replacer scans
// the input once and never rescans its own output, so "&amp;lt;" (what Slack
// sends for a typed "&lt;") comes back as "&lt;" instead of collapsing to
// "<" the way &amp;-then-&lt; passes would leave it.
func TestSlackUnescaper(t *testing.T) {
	cases := []struct {
		name string
		wire string
		want string
	}{
		{"plain text untouched", "restart the api deployment", "restart the api deployment"},
		{"ampersand", "get pods -n foo &amp;&amp; describe node", "get pods -n foo && describe node"},
		{"angles", "describe node &lt;name&gt;", "describe node <name>"},
		{"greater than in a condition", "scale web if cpu &gt; 80%", "scale web if cpu > 80%"},
		{"typed &lt; survives", "type &amp;lt; for a left angle", "type &lt; for a left angle"},
		{"typed &amp; survives", "write &amp;amp; not &amp;", "write &amp; not &"},
		{"typed &gt; survives", "the &amp;gt; entity", "the &gt; entity"},
		{"non-slack entities are left alone", "&copy; 2026 &#123; &nbsp;", "&copy; 2026 &#123; &nbsp;"},
		{"bare ampersand is not an entity", "cats & dogs", "cats & dogs"},
		{"round trip through the outbound escaper", slackEscaper.Replace("a & b < c > d"), "a & b < c > d"},
	}
	for _, c := range cases {
		if got := slackUnescaper.Replace(c.wire); got != c.want {
			t.Errorf("%s: slackUnescaper(%q) = %q, want %q", c.name, c.wire, got, c.want)
		}
	}
}

// TestSlackAskEchoIsNotDoubleEscaped: the inbound text becomes ActiveTask.Ask
// and formatTaskStatus echoes it back through Post -> toMrkdwn, which escapes
// again. Decoding on the way in is what keeps that one escape rather than
// two, so the user sees their own words and not "cpu &amp;gt; 80%".
func TestSlackAskEchoIsNotDoubleEscaped(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	msg, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", "scale web if cpu &gt; 80% &amp;&amp; nodes ok", "20.0", ""))
	if !ok {
		t.Fatal("dm must deliver")
	}
	ask := truncateRunes(msg.Text, askCap)
	if ask != "scale web if cpu > 80% && nodes ok" {
		t.Fatalf("ask = %q", ask)
	}
	card := formatTaskStatus(&lib.Task{ID: "t-1", State: lib.StateWorking}, ask, time.Time{})
	if _, err := a.Post(msg.Conversation, card); err != nil {
		t.Fatal(err)
	}
	wire := api.posted[0].text
	// One escape on the wire, which Slack renders back as the typed text.
	if !strings.Contains(wire, "cpu &gt; 80% &amp;&amp; nodes ok") {
		t.Errorf("status card echo = %q", wire)
	}
	if strings.Contains(wire, "&amp;gt;") || strings.Contains(wire, "&amp;amp;") {
		t.Errorf("status card echo is double-escaped: %q", wire)
	}
}

// TestSlackEmptyTurnSkipsSessionLookup: an attachment-only or whitespace-only
// reply is dropped either way, so it must not pay for the session registry
// read first. That read runs on the event pump's goroutine under
// slackSessionLookupTimeout, and the next envelope's ack waits behind it — a
// two-second stall spent to discard the message. The control case — a reply
// with text, in a thread of its OWN — proves the guard still asks when the
// answer matters.
//
// The control's thread is separate on purpose. Sharing 300.1 with the empty
// cases made the final count assertion worthless: with the guard removed the
// attachment case does the read and caches the answer, the whitespace and
// control cases then both hit that cache, and the count lands on exactly the
// 1 the test wanted. It passed on a cache hit while claiming to prove a
// read. In its own uncached thread the control has to spend the call, so the
// same "want 1" now reads 2 the moment the guard goes.
func TestSlackEmptyTurnSkipsSessionLookup(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	reg := &countingLookup{held: map[string]bool{"slack:C1/300.1": true, "slack:C1/310.1": true}}
	a.sessions = reg.lookup

	// A file_share reply with no caption, in a thread nothing has cached.
	attachment := slackMsg("channel", "C1", "U2", "", "301.0", "300.1")
	attachment.SubType = "file_share"
	if _, ok := a.inbound(context.Background(), attachment); ok {
		t.Error("an empty reply is not a turn")
	}
	if n := reg.calls(); n != 0 {
		t.Errorf("empty reply made %d session lookups, want 0", n)
	}

	whitespace := slackMsg("channel", "C1", "U2", "   \n\t ", "302.0", "300.1")
	if _, ok := a.inbound(context.Background(), whitespace); ok {
		t.Error("a whitespace-only reply is not a turn")
	}
	if n := reg.calls(); n != 0 {
		t.Errorf("whitespace reply made %d session lookups, want 0", n)
	}

	// Control: a different thread, uncached by anything above, with text.
	// The lookup must happen, and the reply must deliver.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U2", "steer it", "313.0", "310.1")); !ok {
		t.Error("an unmentioned reply in a thread the registry holds must deliver")
	}
	if n := reg.calls(); n != 1 {
		t.Errorf("made %d session lookups, want 1 — only the control should ask", n)
	}
}

// TestSlackChannelMentionRootsNothingUntilATaskStarts pins the rule that a
// channel-level mention is a turn and nothing more. It used to mark its own
// thread a session thread on the spot — the root IS the ask, so the answer
// looked free — but the adapter has verified nothing at that point, not the
// sender and not that the text is an ask, and a sender the principal map
// refuses would still have turned the thread into one whose every message
// reached the gateway. Now the thread becomes a session thread when the
// gateway starts the task there and says so (TaskStarted), the same as a
// thread someone else rooted; until then an unmentioned reply asks the
// registry, which holds nothing, and drops.
func TestSlackChannelMentionRootsNothingUntilATaskStarts(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	reg := &countingLookup{}
	a.sessions = reg.lookup

	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> drain node 3", "400.0", ""))
	if !ok || got.Conversation != "slack:C1/400.0" || got.Text != "drain node 3" {
		t.Fatalf("a channel mention is a turn keyed on its own ts: delivered=%v got=%+v", ok, got)
	}
	if v, cached := a.sessionThreads["C1/400.0"]; cached {
		t.Fatalf("the channel mention marked its own thread as %v; it must mark nothing", v)
	}
	// A bare "<@bot>" in the channel is not a turn, and marks nothing either.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT>", "410.0", "")); ok {
		t.Fatal("a bare mention has nothing to run")
	}
	if v, cached := a.sessionThreads["C1/410.0"]; cached {
		t.Fatalf("a bare channel mention marked its own thread as %v; it must mark nothing", v)
	}

	// An unmentioned reply under the ask, before any task started there:
	// the registry is asked, holds nothing, and the reply drops.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U2", "here is more", "401.0", "400.0")); ok {
		t.Fatal("an unmentioned reply under a channel mention delivered before any task started")
	}
	if n := reg.calls(); n != 1 {
		t.Fatalf("the reply made %d session lookups, want 1 — the registry is the only source", n)
	}

	// The gateway verified the sender and started the task: now the thread
	// is a session thread, and the same shape of reply delivers from cache.
	a.TaskStarted("slack:C1/400.0", "task-1")
	got, ok = a.inbound(context.Background(), slackMsg("channel", "C1", "U2", "and this", "402.0", "400.0"))
	if !ok || got.Conversation != "slack:C1/400.0" {
		t.Fatalf("an unmentioned reply after TaskStarted: delivered=%v conv=%q", ok, got.Conversation)
	}
	if n := reg.calls(); n != 1 {
		t.Errorf("the reply after TaskStarted made %d session lookups in total, want the 1 from before — TaskStarted filled the cache", n)
	}
}

// TestToMrkdwnEscapesAmpersandsInsideLinkURLs pins behaviour that reads like
// a bug and is not one: the ampersand in a link's query string goes out as
// "&amp;", inside the <url|label> form.
//
// That is what Slack asks for and what Slack itself emits. Slack's formatting
// spec names exactly three characters to entity-encode — &, < and > — with no
// carve-out for the URL portion of a control sequence, and the archived
// version of that page states the invariant from the other side: "Because the
// ampersands and angled brackets are already escaped, no further translation
// need take place (for a web-client). The server ensures that no extra
// un-escaped angled brackets or ampersands are included in the message."
// (slackhq/slack-api-docs, page_formatting.md; the live page is
// docs.slack.dev/messaging/formatting-message-text.) The rendering algorithm
// on that same page — find <(.*?)>, split on the pipe, treat the head as a
// URL — runs over the already-escaped text, so the client decodes the entity
// when it builds the href.
//
// Confirmed from the other direction by slackapi/bolt-js#2103, where an app
// posted a link containing a RAW "&" and Slack's own server normalised it to
// "&amp;" in the stored message; Slack staff labelled the resulting broken
// link a "server-side-issue" in the iOS client, not a sender error, and
// desktop and web resolved the same link correctly. Emitting a bare "&" here
// would therefore be re-escaped by Slack anyway.
//
// So: do not "fix" this by leaving the URL unescaped. Anything that stops
// escaping inside <...> also stops escaping <!channel>, which is the reason
// slackEscaper exists — see the case below.
func TestToMrkdwnEscapesAmpersandsInsideLinkURLs(t *testing.T) {
	cases := map[string]string{
		"[Trace](https://monitor.local/query?a=1&b=2)": "<https://monitor.local/query?a=1&amp;b=2|Trace>",
		"[Logs](https://x.example/l?a=1&b=2&c=3)":      "<https://x.example/l?a=1&amp;b=2&amp;c=3|Logs>",
		// A bare URL is not rewritten; Slack auto-links it. The ampersand
		// is still escaped, for the same reason.
		"see https://x.example/l?a=1&b=2": "see https://x.example/l?a=1&amp;b=2",
	}
	for in, want := range cases {
		if got := toMrkdwn(in); got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
	}
}

// TestToMrkdwnRefusesPipesInsideLinkURLs: Slack splits the <url|text> the
// link rewrite emits at its FIRST pipe. A markdown link whose URL carries a
// pipe would therefore render as a link to the text before the pipe, under
// display text the URL chose -- [Trace](https://evil.example/x|https://good.example)
// as <https://evil.example/x|https://good.example|Trace> shows "good" and
// goes to "evil". The URL class excludes the pipe, so such a link is left
// as the escaped plain markdown it arrived as and no <...|...> is produced;
// an ordinary link beside it still converts.
func TestToMrkdwnRefusesPipesInsideLinkURLs(t *testing.T) {
	cases := map[string]string{
		"[Trace](https://evil.example/x|https://good.example)": "[Trace](https://evil.example/x|https://good.example)",
		"[a](https://x.example/p|q)":                           "[a](https://x.example/p|q)",
		// The crafted link beside an honest one: only the honest one converts.
		"[ok](https://x.example/p) and [Trace](https://evil.example/x|https://good.example)": "<https://x.example/p|ok> and [Trace](https://evil.example/x|https://good.example)",
		// The same URL, with the pipe percent-encoded as a URL must carry it,
		// is an ordinary link.
		"[Trace](https://x.example/p%7Cq)": "<https://x.example/p%7Cq|Trace>",
	}
	for in, want := range cases {
		got := toMrkdwn(in)
		if got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
		if strings.Contains(in, "|") && strings.Contains(got, "|https://good.example|") {
			t.Errorf("toMrkdwn(%q) = %q produced a link whose display text the URL chose", in, got)
		}
	}
}

// TestToMrkdwnNeutralisesControlSequences is the property the escaping exists
// for and the one no link-handling change may cost us. Relayed text is
// executor output — model output — so a prompt-injected result containing
// <!channel> must reach Slack as inert characters, not as an @channel ping to
// the whole room. Same for <!here>, <!everyone>, a user mention, and a
// subteam handle. Held alongside a link in the same string, since a link fix
// is the plausible way to break it.
func TestToMrkdwnNeutralisesControlSequences(t *testing.T) {
	cases := map[string]string{
		"<!channel> deploy done":  "&lt;!channel&gt; deploy done",
		"<!here> heads up":        "&lt;!here&gt; heads up",
		"<!everyone> all hands":   "&lt;!everyone&gt; all hands",
		"<!subteam^S123|@sre> up": "&lt;!subteam^S123|@sre&gt; up",
		"ping <@U999> now":        "ping &lt;@U999&gt; now",
		"join <#C123|general>":    "join &lt;#C123|general&gt;",
		// The mixed case: a real link is rewritten, the injected control
		// sequence beside it is not.
		"<!channel> see [Trace](https://monitor.local/q?a=1&b=2)": "&lt;!channel&gt; see <https://monitor.local/q?a=1&amp;b=2|Trace>",
	}
	for in, want := range cases {
		got := toMrkdwn(in)
		if got != want {
			t.Errorf("toMrkdwn(%q) = %q, want %q", in, got, want)
		}
		// Belt and braces, independent of the table: the only control
		// sequences left on the wire are URL links. No mention, channel
		// link or special command survives as one.
		for _, opener := range []string{"<!", "<@", "<#"} {
			if strings.Contains(got, opener) {
				t.Errorf("toMrkdwn(%q) = %q leaves a live %q control sequence", in, got, opener)
			}
		}
	}
}

// TestSlackRunAwaitsPumpGoroutine: Run must not return while the event pump
// it started is still working. Before the WaitGroup it did — RunContext
// returning (an invalid token, an unrecoverable socket error) unblocked Run
// while a handler call was mid-flight, so an embedder that treats "Run
// returned" as "this adapter is finished" could tear down state the pump was
// still writing to.
//
// The sequence is forced, not timed: apps.connections.open blocks until the
// test releases it, so the pump is guaranteed to be inside handler before
// RunContext fails. Then the test asserts Run is still blocked, releases the
// handler, and reads a variable the pump wrote with no synchronisation of its
// own — under -race, only Run's wg.Wait can order that write before this
// read.
func TestSlackRunAwaitsPumpGoroutine(t *testing.T) {
	srv, unblock := stalledSlackStub(t)

	a := newTestSlackAdapter(&fakeSlackAPI{})
	// A real socketmode client, pointed at the stub: its Events channel is
	// the pump's input, and its connect fails fatally (invalid_auth is one
	// of the four errors socketmode does not retry) as soon as we release.
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))

	var pumpFinishedHandler bool // deliberately unsynchronised; see above
	entered := make(chan struct{})
	proceed := make(chan struct{})
	handler := func(InboundMessage) {
		close(entered)
		<-proceed
		pumpFinishedHandler = true
	}

	// Queue one real turn for the pump before Run starts; Events is buffered.
	a.sm.Events <- socketmode.Event{
		Type: socketmode.EventTypeEventsAPI,
		Data: slackevents.EventsAPIEvent{
			Type: slackevents.CallbackEvent,
			InnerEvent: slackevents.EventsAPIInnerEvent{
				Data: slackMsg("im", "D1", "U1", "hello", "500.0", ""),
			},
		},
	}

	returned := make(chan error, 1)
	go func() { returned <- a.Run(context.Background(), handler) }()

	select {
	case <-entered:
	case err := <-returned:
		t.Fatalf("Run returned before the pump reached the handler: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("pump never reached the handler")
	}

	// RunContext can now fail, which sends Run into its deferred cancel and
	// wait while the handler is still parked.
	unblock()
	select {
	case err := <-returned:
		t.Fatalf("Run returned with the pump still in the handler: %v", err)
	case <-time.After(250 * time.Millisecond):
	}

	close(proceed)
	select {
	case err := <-returned:
		if err == nil {
			t.Error("Run should surface the connect failure")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned; the deferred cancel and wait are out of order")
	}
	if !pumpFinishedHandler {
		t.Error("Run returned before the pump finished")
	}
}

// TestSlackPumpDropsATurnItCouldNotAck is the duplicate-turn guard. An
// envelope we failed to ack is one Slack will redeliver; handling it here as
// well turns that safe redelivery into two turns from one user message,
// because the instance that gets the redelivery has an empty alreadySeen map
// and cannot suppress it. So a failed ack must drop the turn, not log and
// carry on.
//
// The ack failure is forced without racing a context cancellation: socketmode
// refuses to write a Socket Mode response of 20KB or more (Slack silently
// drops those), so AckCtx on an oversized envelope ID fails deterministically,
// before the response ever reaches the send channel. A second, ackable
// envelope behind it proves the drop is a drop and not a dead pump — and,
// since Events is FIFO and the pump is single-threaded, seeing the second turn
// means the first was already decided.
func TestSlackPumpDropsATurnItCouldNotAck(t *testing.T) {
	srv, unblock := stalledSlackStub(t)

	logs := &recordingHandler{}
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.log = slog.New(logs)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))

	delivered := make(chan InboundMessage, 4)

	a.sm.Events <- slackEnvelope(strings.Repeat("E", 32*1024), slackMsg("im", "D1", "U1", "unacked", "600.0", ""))
	a.sm.Events <- slackEnvelope("Env-ok", slackMsg("im", "D1", "U1", "acked", "601.0", ""))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	returned := make(chan error, 1)
	go func() { returned <- a.Run(ctx, func(m InboundMessage) { delivered <- m }) }()

	select {
	case m := <-delivered:
		if m.MessageID == "600.0" {
			t.Fatalf("the unacked turn reached the handler: %+v — Slack will redeliver it, so this is the duplicate", m)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the ackable turn never reached the handler")
	}
	select {
	case m := <-delivered:
		t.Errorf("a second turn reached the handler: %+v", m)
	default:
	}

	// An ack that failed for anything other than shutdown is a real failure
	// and keeps its WARN.
	if lvl, ok := logs.level("ack failed"); !ok || lvl != slog.LevelWarn {
		t.Errorf("oversized-envelope ack logged at %v (found=%v), want WARN", lvl, ok)
	}

	cancel()
	unblock()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
}

// slackInvalidAuthServer answers every Web API call with invalid_auth, which
// socketmode's connect() treats as fatal — so RunContext gives up on the first
// attempt instead of backing off and redialling, and a Run against it returns
// in microseconds.
func slackInvalidAuthServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_auth"}`))
	}))
	t.Cleanup(srv.Close)
	return srv
}

// slackCancelledPumpRun drives one whole SlackAdapter.Run against a context
// that is already cancelled, with a single ackable DM envelope sitting in
// Events, and reports whether the pump took that envelope off the channel.
//
// The Socket Mode client is the real one, not a fake, because the behaviour
// under test is the real one's: AckCtx on a cancelled context with room in the
// 20-deep socketModeResponses buffer returns nil roughly half the time. Run's
// deferred wg.Wait means the pump goroutine has finished by the time this
// returns, so the caller's counters need no locking of their own.
func slackCancelledPumpRun(apiURL, envelopeID, ts string, logs *recordingHandler, handler func(InboundMessage)) bool {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.log = slog.New(logs)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(apiURL)))
	a.sm.Events <- slackEnvelope(envelopeID, slackMsg("im", "D1", "U1", "hello", ts, ""))

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_ = a.Run(ctx, handler)

	// Whatever RunContext pushed onto Events on its way out — connecting, and
	// possibly connection_error — carries no Request, so matching on the
	// envelope ID answers exactly one question: did the pump take OUR
	// envelope, or did its top-of-loop select take ctx.Done instead?
	//
	// Nothing but the pump reads Events, so finding our envelope still there
	// means the pump left at the select and never took it; draining the
	// channel without meeting it means the pump took it. The first version
	// of this helper returned the inverse, which made the anti-vacuity guard
	// below count the runs that skipped the code under test.
	for {
		select {
		case evt := <-a.sm.Events:
			if evt.Request != nil && evt.Request.EnvelopeID == envelopeID {
				return false
			}
		default:
			return true
		}
	}
}

// slackCancelledPumpRuns is how many shutdowns the two tests below each drive,
// and the count is the whole reason they are trustworthy. One run cannot be
// enough: with ctx already done AND an envelope already buffered, both cases
// of the pump's top-of-loop select are ready, and a Go select picks uniformly
// at random among ready cases — so about half of all runs leave at the select
// and never reach the code under test, and a one-shot test would go green
// against the broken version every other time it ran. Fifty independent runs
// make "no turn was handled" and "no ack was attempted" deterministic to about
// 2^-50. That is also why the branches are not pinned by pre-filling the
// response buffer instead: pre-filling forces AckCtx to FAIL, and the branch
// that matters here is the one where it succeeds.
const slackCancelledPumpRuns = 50

// TestSlackPumpStartsNoTurnOnACancelledContext is the shutdown-race guard —
// what the first shutdown fix in this branch set out to do and did only about
// half the time.
//
// The trap: AckCtx returning nil never meant Slack has the ack. It means the
// response was QUEUED. SendCtx races ctx.Done against a send into the 20-deep
// socketModeResponses channel, runResponseSender keeps that channel drained so
// in production there is always room, and once ctx is cancelled both cases are
// ready and the runtime picks at random — measured against slack-go v0.29.0,
// 483 of 1000 such calls came back nil. Each of those used to fall straight
// through to the handler and start or steer a task on an instance that is
// exiting, while runResponseSender — whose select has the same shape — left
// without flushing the queued ack. Slack redelivers to a new instance whose
// alreadySeen map is empty, and one user message becomes two agent sessions
// doing real work.
//
// So the contract is: a cancelled pump reaches no handler, whatever AckCtx
// says. Two guards in Run enforce it — the ctx.Err() re-check after the Events
// receive, and the ctx.Err() re-check after a successful ack — and this test
// asserts the contract rather than either guard.
// TestSlackPumpDoesNotAckOnACancelledContext below pins the first one alone.
func TestSlackPumpStartsNoTurnOnACancelledContext(t *testing.T) {
	srv := slackInvalidAuthServer(t)
	logs := &recordingHandler{}

	// handled is written by each run's pump goroutine and read here; Run's
	// deferred wg.Wait orders every write before this function sees it.
	handled, took := 0, 0
	for i := 0; i < slackCancelledPumpRuns; i++ {
		if slackCancelledPumpRun(srv.URL+"/", fmt.Sprintf("Env-cancel-%d", i),
			fmt.Sprintf("800.%03d", i), logs, func(InboundMessage) { handled++ }) {
			took++
		}
	}
	if handled != 0 {
		t.Errorf("a cancelled pump handled %d of %d turns; every one is a duplicate waiting to happen, because nothing flushed the ack and Slack will redeliver the envelope",
			handled, slackCancelledPumpRuns)
	}
	// Without this the test could pass for the wrong reason: if every run
	// happened to leave at the top-of-loop select, nothing below it ran.
	if took == 0 {
		t.Errorf("not one of the %d runs took the envelope off Events, so the code under test never executed", slackCancelledPumpRuns)
	}
	t.Logf("%d of %d cancelled pumps took the envelope past the top-of-loop select", took, slackCancelledPumpRuns)
}

// TestSlackPumpDoesNotAckOnACancelledContext pins the first guard on its own:
// the ctx.Err() re-check immediately after the Events receive, which is what
// defeats the pseudo-random select. A pump that is already cancelled must not
// so much as ATTEMPT the ack — an ack queued now goes into a buffer whose
// drain goroutine is exiting, so it is at best a no-op, and at worst the thing
// that makes the pump believe the turn is safe to run.
//
// Both shutdown log lines are checked because an attempted ack announces
// itself one way or the other: Canceled from AckCtx logs "ack abandoned", and
// a nil arriving on a dead context logs "ack queued but not flushed". Over
// fifty runs, an ack attempted at all produces one of them.
func TestSlackPumpDoesNotAckOnACancelledContext(t *testing.T) {
	srv := slackInvalidAuthServer(t)
	logs := &recordingHandler{}
	took := 0
	for i := 0; i < slackCancelledPumpRuns; i++ {
		if slackCancelledPumpRun(srv.URL+"/", fmt.Sprintf("Env-noack-%d", i),
			fmt.Sprintf("810.%03d", i), logs, func(InboundMessage) {}) {
			took++
		}
	}
	// Absence-only assertions pass when nothing ran; this is the one test
	// that pins the receive-site re-check on its own, so it needs the same
	// guard as its sibling.
	if took == 0 {
		t.Errorf("not one of the %d runs took the envelope off Events, so the receive-site ctx check never executed", slackCancelledPumpRuns)
	}
	if lvl, ok := logs.level("ack abandoned"); ok {
		t.Errorf("a cancelled pump attempted an ack and had it refused (logged at %v); the receive is not re-checking ctx", lvl)
	}
	if lvl, ok := logs.level("ack queued but not flushed"); ok {
		t.Errorf("a cancelled pump queued an ack nothing will flush (logged at %v); the receive is not re-checking ctx", lvl)
	}
}

// TestSlackAckCtxQueuesAnAckOnACancelledContext is the half of the premise
// that actually describes production, and the one the pre-filled-buffer test
// below cannot show. With ROOM in socketModeResponses — the normal state,
// since runResponseSender drains it — AckCtx on a cancelled context has two
// ready cases in its select and comes back nil a large fraction of the time.
// Nil means queued, never delivered: runResponseSender exits on the same ctx
// without flushing what is in the buffer.
//
// Asserted as "not an error every single time" rather than "about half", so
// the assertion is not itself a coin toss — two hundred draws all landing on
// the error case is 2^-200 if the select is fair, and a certainty if slack-go
// has changed. Should this one ever start failing, the post-ack ctx.Err()
// guard in the pump has lost its reason to exist and can go.
func TestSlackAckCtxQueuesAnAckOnACancelledContext(t *testing.T) {
	const draws = 200
	queued := 0
	for i := 0; i < draws; i++ {
		sm := socketmode.New(slack.New("xoxb-stub"))
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if err := sm.AckCtx(ctx, "Env-1", nil); err == nil {
			queued++
		}
	}
	if queued == 0 {
		t.Errorf("AckCtx on a cancelled context with an empty response buffer errored on all %d draws; SendCtx no longer races ctx.Done against the buffered send", draws)
	}
	t.Logf("AckCtx returned nil — queued, not delivered — on %d of %d cancelled-context calls", queued, draws)
}

// TestSlackAckCtxFailsOnACancelledContext pins one premise the shutdown filter
// rests on: AckCtx really does come back context.Canceled once the context is
// done and the response channel cannot take the write. Plain Ack could not —
// it passes context.TODO(), and marshalling a forty-byte struct does not fail
// — which is why the error branch was unreachable before the switch to AckCtx.
//
// Read the pre-filled buffer below for what it is and no more. It forces the
// error branch by making ctx.Done the ONLY ready case in AckCtx's select, and
// that is not what a real shutdown looks like: in production
// runResponseSender keeps socketModeResponses drained, so the buffered send is
// ready too and AckCtx comes back nil about half the time
// (TestSlackAckCtxQueuesAnAckOnACancelledContext). This test is proof that
// "AckCtx can return Canceled", NOT proof that the shutdown path is covered —
// the coverage for that is TestSlackPumpStartsNoTurnOnACancelledContext and
// TestSlackPumpDoesNotAckOnACancelledContext.
func TestSlackAckCtxFailsOnACancelledContext(t *testing.T) {
	sm := socketmode.New(slack.New("xoxb-stub"))
	// socketModeResponses is 20 deep and its drain goroutine only runs under
	// RunContext, so twenty sends leave the buffer full and ctx.Done the only
	// ready case in AckCtx's select.
	for i := 0; i < 20; i++ {
		if err := sm.Send(socketmode.Response{EnvelopeID: "filler"}); err != nil {
			t.Fatalf("filling the response buffer: %v", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := sm.AckCtx(ctx, "Env-1", nil); !errors.Is(err, context.Canceled) {
		t.Errorf("AckCtx on a cancelled context = %v, want context.Canceled", err)
	}
}

// TestSlackSessionLookupLogLevels: the session registry read fires on every
// unmentioned reply in an uncached thread, so its failure log is the noisiest
// thing in the adapter and a WARN on every pod termination is a false
// positive for anything alerting on logs. Cancelled is demoted.
// DeadlineExceeded is NOT — at this site that is slackSessionLookupTimeout
// genuinely expiring on a slow registry read, which cost a user their reply
// and is the real operational signal a blanket "any context error" filter
// would swallow. And no failure caches: an error is an unknown, not an
// answer, and a false left behind would silence a running task's thread.
func TestSlackSessionLookupLogLevels(t *testing.T) {
	// The registry's answer is whatever the context says, the way a real
	// KV read surfaces ctx.Err() on a cancelled or expired context.
	ctxErr := func(ctx context.Context, _ string) (bool, time.Time, error) {
		if err := ctx.Err(); err != nil {
			return false, time.Time{}, fmt.Errorf("kv get: %w", err)
		}
		return false, time.Time{}, errors.New("kv unavailable")
	}
	uncached := func(t *testing.T, a *SlackAdapter, key string) {
		t.Helper()
		if v, cached := a.sessionThreads[key]; cached {
			t.Errorf("a failed lookup cached %v for %s; an error must not be cached", v, key)
		}
	}

	// Shutdown: the parent context is already cancelled.
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	shutdown := &recordingHandler{}
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.log = slog.New(shutdown)
	a.sessions = ctxErr
	if a.isSessionThread(cancelled, "C1", "700.1") {
		t.Error("a failed lookup must report false")
	}
	if lvl, ok := shutdown.level("session lookup"); !ok || lvl != slog.LevelDebug {
		t.Errorf("cancelled lookup logged at %v (found=%v), want DEBUG", lvl, ok)
	}
	uncached(t, a, "C1/700.1")

	// The timeout, modelled with a parent whose deadline has already passed
	// so the derived context reports DeadlineExceeded rather than Canceled.
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	timedOut := &recordingHandler{}
	b := newTestSlackAdapter(&fakeSlackAPI{})
	b.log = slog.New(timedOut)
	b.sessions = ctxErr
	if b.isSessionThread(expired, "C1", "700.2") {
		t.Error("a timed-out lookup must report false")
	}
	if lvl, ok := timedOut.level("session lookup"); !ok || lvl != slog.LevelWarn {
		t.Errorf("timed-out lookup logged at %v (found=%v), want WARN — slackSessionLookupTimeout expiring is a dropped reply", lvl, ok)
	}
	uncached(t, b, "C1/700.2")

	// And a plain registry error, with no context involved at all, warns.
	failed := &recordingHandler{}
	c := newTestSlackAdapter(&fakeSlackAPI{})
	c.log = slog.New(failed)
	c.sessions = ctxErr
	if c.isSessionThread(context.Background(), "C1", "700.3") {
		t.Error("a failed lookup must report false")
	}
	if lvl, ok := failed.level("session lookup"); !ok || lvl != slog.LevelWarn {
		t.Errorf("failed lookup logged at %v (found=%v), want WARN", lvl, ok)
	}
	uncached(t, c, "C1/700.3")

	// No lookup wired at all: a miss is a false, and nothing is logged —
	// there was nothing to ask, so nothing failed.
	silent := &recordingHandler{}
	d := newTestSlackAdapter(&fakeSlackAPI{})
	d.log = slog.New(silent)
	if d.isSessionThread(context.Background(), "C1", "700.4") {
		t.Error("with no lookup wired a miss must report false")
	}
	if lvl, ok := silent.level("session lookup"); ok {
		t.Errorf("with no lookup wired, a miss logged at %v; want nothing", lvl)
	}
}

// TestSlackDecodedTextDrivesTheAffordances pins a consequence of decoding the
// inbound entities that nothing else in the suite notices. normalize() drops
// every non-alphanumeric, so the entity escaping used to survive it as
// letters: "&lt;stop&gt;" normalized to "ltstopgt" and matched nothing.
// Decoded first, the same wire text normalizes to "stop" — a hard task cancel.
// Kept deliberately: the affordances should match what the user typed, not
// what Slack's transport did to it. The same shift shortens normalized text,
// so an ask can newly fall under isStatusQuery's wideMatchLenCap.
func TestSlackDecodedTextDrivesTheAffordances(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})

	// What Slack puts on the wire when a user types "<stop>".
	const stopWire = "&lt;stop&gt;"
	if got := normalize(stopWire); got != "ltstopgt" || isStop(stopWire) {
		t.Fatalf("premise: normalize(%q) = %q, isStop = %v", stopWire, got, isStop(stopWire))
	}
	msg, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", stopWire, "800.0", ""))
	if !ok {
		t.Fatal("dm must deliver")
	}
	if msg.Text != "<stop>" {
		t.Fatalf("inbound text = %q, want the decoded form", msg.Text)
	}
	if !isStop(msg.Text) {
		t.Error("a typed <stop> must cancel: the decode is what lets normalize see \"stop\"")
	}

	// The length half. Same words, entity-encoded and not.
	const pokeWire = "any update on the &lt;prod&gt; rollout &amp; the canary?"
	if isStatusQuery(pokeWire, true) {
		t.Errorf("premise: the wire form normalizes to %d chars, over the %d cap", len(normalize(pokeWire)), wideMatchLenCap)
	}
	poke, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", pokeWire, "801.0", ""))
	if !ok {
		t.Fatal("dm must deliver")
	}
	if !isStatusQuery(poke.Text, true) {
		t.Errorf("decoded %q normalizes to %d chars and must read as a status poke", poke.Text, len(normalize(poke.Text)))
	}
}

// TestSlackMidThreadMentionAdoptsThread pins the sequence that mints a
// session in a thread the bot did not root: a user mentions the bot in
// someone else's thread, which delivers a turn keyed on that thread, the
// gateway starts a task there and says so (TaskStarted), and the user then
// follows up unmentioned — a steer, or "stop". That follow-up has to reach
// the gateway, because a session is already running there. Without the
// record, the follow-up is a cache miss, the registry is asked, and the
// message is discarded without even a drop notice if the answer is no:
// nothing reaches handleInbound.
//
// The record is the gateway's TaskStarted, not the mention: the adapter
// used to record on the mention alone, before anyone had verified the
// sender, and TestSlackBareMentionInForeignThreadAdoptsNothing pins the
// other half of that change.
//
// Every shape that can carry a mention into a foreign thread is here, since
// the bug is "a session minted at a key the adapter never recorded" and a
// plain reply is only one way to reach it.
func TestSlackMidThreadMentionAdoptsThread(t *testing.T) {
	mention := func(text, ts, thread string) *slackevents.MessageEvent {
		return slackMsg("channel", "C1", "U1", text, ts, thread)
	}
	broadcast := func(text, ts, thread string) *slackevents.MessageEvent {
		m := mention(text, ts, thread)
		m.SubType = "thread_broadcast"
		return m
	}
	fileShare := func(text, ts, thread string) *slackevents.MessageEvent {
		m := mention(text, ts, thread)
		m.SubType = "file_share"
		return m
	}
	cases := []struct {
		name string
		msg  func(text, ts, thread string) *slackevents.MessageEvent
	}{
		{"plain reply", mention},
		{"thread_broadcast", broadcast},
		{"file_share", fileShare},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			a := newTestSlackAdapter(&fakeSlackAPI{})
			a.sessions = noSessions

			got, ok := a.inbound(context.Background(), c.msg("<@UBOT> drain node 3", "4.0", "200.1"))
			if !ok || got.Conversation != "slack:C1/200.1" {
				t.Fatalf("mention in a foreign thread: delivered=%v conv=%q", ok, got.Conversation)
			}
			// The gateway verified the sender and started a task on that key.
			a.TaskStarted(got.Conversation, "task-1")
			got, ok = a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "5.0", "200.1"))
			if !ok || got.Conversation != "slack:C1/200.1" {
				t.Fatalf("unmentioned follow-up in the session's own thread: delivered=%v conv=%q", ok, got.Conversation)
			}
			if got.Text != "stop" {
				t.Errorf("follow-up text = %q, want the affordance word intact", got.Text)
			}
		})
	}
}

// TestSlackBareMentionInForeignThreadAdoptsNothing: a bare "@bot" inside
// someone else's thread is not a turn (nothing to run), and it does not make
// that thread a session thread either. The adapter used to record the
// thread on the mention alone — before the gateway had verified the sender
// or decided anything — so anyone who could type "<@bot>" in a thread turned
// its every later message into a delivery. Now the thread becomes a session
// thread when the gateway starts a task in it and says so (TaskStarted);
// until then an unmentioned reply is answered from the registry, which holds
// no task there, and drops.
func TestSlackBareMentionInForeignThreadAdoptsNothing(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	reg := &countingLookup{}
	a.sessions = reg.lookup

	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT>", "4.0", "200.1")); ok {
		t.Fatal("a bare mention has nothing to run and must not deliver")
	}
	if v, cached := a.sessionThreads["C1/200.1"]; cached {
		t.Fatalf("a bare mention in a foreign thread recorded the thread as %v; it must record nothing", v)
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "drain node 3", "5.0", "200.1")); ok {
		t.Fatal("an unmentioned reply after a bare mention delivered: the mention adopted the thread")
	}
	if n := reg.calls(); n != 1 {
		t.Errorf("the unmentioned reply made %d session lookups, want exactly 1 — the registry is the answer", n)
	}
	// A mentioned ask is a turn whoever rooted the thread; that has not
	// changed. What has: the thread is a session thread once the gateway
	// starts a task there, and not before.
	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> drain node 3", "6.0", "200.1"))
	if !ok || got.Conversation != "slack:C1/200.1" {
		t.Fatalf("a mentioned ask in a foreign thread: delivered=%v conv=%q", ok, got.Conversation)
	}
	a.TaskStarted(got.Conversation, "task-1")
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "7.0", "200.1")); !ok {
		t.Fatal("an unmentioned follow-up after TaskStarted must deliver")
	}
}

// TestSlackMentionUnpoisonsCachedFalse: the registry lookup caches its
// answer, so an unmentioned reply that arrives BEFORE the bot is pulled into
// the thread leaves a false behind. The gateway starting a task there
// (TaskStarted) has to overwrite it, or the thread is dropped for the life
// of the cache entry — including the "stop" for the session that task
// started.
func TestSlackMentionUnpoisonsCachedFalse(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.sessions = noSessions

	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U3", "chatter", "3.0", "200.1")); ok {
		t.Fatal("chatter in a thread the bot is not in must drop")
	}
	if v, cached := a.sessionThreads["C1/200.1"]; !cached || v {
		t.Fatalf("want a cached false for the thread; cached=%v value=%v", cached, v)
	}
	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> drain node 3", "4.0", "200.1"))
	if !ok {
		t.Fatal("mention in the thread must deliver")
	}
	// The un-poisoning is the gateway's TaskStarted, not the mention.
	a.TaskStarted(got.Conversation, "task-1")
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "5.0", "200.1")); !ok {
		t.Fatal("the cached false outlived the session it silenced")
	}
	// One entry, one eviction slot: the overwrite must not double-book the
	// ring or the cache would evict short of its cap.
	if n := len(a.threadsOrder); n != 1 {
		t.Errorf("threadsOrder = %d entries, want 1", n)
	}
}

// TestSlackSessionLookupAnswersAColdCache: the session registry is the
// source of truth for which threads the gateway has started a task in, and a
// cache miss asks it. That is what makes a session thread — the channel
// ask's own or one the gateway adopted mid-conversation — survive a restart
// of the adapter's process: with nothing else to derive an answer from,
// every follow-up would drop. A true and a false are both answers and both
// cache; an error is neither, and must not.
func TestSlackSessionLookupAnswersAColdCache(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	var asked []string
	registryDown := true
	a.sessions = func(_ context.Context, conversation string) (bool, time.Time, error) {
		asked = append(asked, conversation)
		switch conversation {
		case "slack:C1/200.1":
			return true, time.Time{}, nil
		case "slack:C1/230.1":
			// A session thread behind a registry that fails once and then
			// recovers.
			if registryDown {
				return false, time.Time{}, errors.New("kv unavailable")
			}
			return true, time.Time{}, nil
		}
		return false, time.Time{}, nil
	}

	// The registry holds the thread: delivered, and cached.
	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "5.0", "200.1"))
	if !ok || got.Conversation != "slack:C1/200.1" {
		t.Fatalf("a reply in a thread the registry holds: delivered=%v conv=%q", ok, got.Conversation)
	}
	if !a.sessionThreads["C1/200.1"] {
		t.Error("the registry's true was not cached")
	}
	if len(asked) != 1 {
		t.Errorf("the registry was consulted for %v; want once", asked)
	}

	// The registry does not hold the thread: dropped, and the false cached,
	// so the next reply in the same thread does not ask again.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "chatter", "6.0", "210.1")); ok {
		t.Fatal("a reply in a thread the registry does not hold delivered")
	}
	if v, cached := a.sessionThreads["C1/210.1"]; !cached || v {
		t.Errorf("the registry's false was not cached: cached=%v value=%v", cached, v)
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "more chatter", "7.0", "210.1")); ok {
		t.Fatal("a second reply in the same thread delivered")
	}
	if len(asked) != 2 {
		t.Errorf("the registry was consulted for %v; want the cached false to have answered the second reply", asked)
	}

	// The registry fails on a session thread: this reply drops, but the
	// false must not be cached, or the thread is silent for the rest of its
	// task once the registry is back.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "8.0", "230.1")); ok {
		t.Fatal("with the registry down, the reply has nothing to deliver on")
	}
	if v, cached := a.sessionThreads["C1/230.1"]; cached {
		t.Fatalf("a %v derived while the registry was unreachable was cached", v)
	}
	registryDown = false
	got, ok = a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "9.0", "230.1"))
	if !ok || got.Conversation != "slack:C1/230.1" {
		t.Fatalf("the next reply, with the registry back: delivered=%v conv=%q", ok, got.Conversation)
	}
	if !a.sessionThreads["C1/230.1"] {
		t.Error("the recovered registry's true was not cached")
	}
	if len(asked) != 4 {
		t.Errorf("the registry was consulted for %v; want the failing thread asked twice", asked)
	}
}

// TestSlackTaskStartedMarksOnlyThreads: TaskStarted records the thread a
// task started in, and nothing for a DM, which is the whole session and
// needs no record.
func TestSlackTaskStartedMarksOnlyThreads(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.TaskStarted("slack:dm/D1", "task-dm")
	a.TaskStarted("not-a-slack-key", "task-other")
	if n := len(a.sessionThreads); n != 0 {
		t.Fatalf("a DM or a foreign key marked %d threads, want 0: %v", n, a.sessionThreads)
	}
	a.TaskStarted("slack:C1/9.0", "task-thread")
	if !a.sessionThreads["C1/9.0"] {
		t.Fatalf("a task in a thread did not mark it: %v", a.sessionThreads)
	}
	if n := len(a.threadsOrder); n != 1 {
		t.Errorf("threadsOrder = %d entries, want 1", n)
	}
}

// TestSlackGatewayAdoptsThreadOnStartedTaskAndSurvivesRestart is the
// adopt-a-foreign-thread sequence through the real gateway rather than the
// adapter alone: New wires the registry lookup, a verified sender's mentioned
// ask in someone else's thread starts a task and TaskStarted marks the
// thread, and after the adapter's cache is wiped — a restart — the registry
// still answers for the unmentioned "stop". An unverified sender's ask in a
// fresh thread starts nothing and marks nothing.
func TestSlackGatewayAdoptsThreadOnStartedTaskAndSurvivesRestart(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:jayanti\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)

	a := newTestSlackAdapter(&fakeSlackAPI{})
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
		// U1 is listed; U9, below, is not, and is the unverified sender.
		SlackAllowedUsers: []string{"U1"},
	}
	g, err := New(Options{Client: client, Adapter: a, Config: cfg, Backend: slackBackend, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if a.sessions == nil {
		t.Fatal("New did not offer the Slack adapter the session lookup")
	}
	// Wrap the lookup New wired so the test can count how often the
	// registry is consulted; the answers are still the real registry's.
	registry := a.sessions
	var consulted int
	a.sessions = func(ctx context.Context, conversation string) (bool, time.Time, error) {
		consulted++
		return registry(ctx, conversation)
	}

	// The mentioned ask, as inbound would hand it over, driven through the
	// gateway: verified, minted, task started, and the adapter told.
	g.handleInbound(InboundMessage{Conversation: "slack:C1/200.1", Kind: "group", AuthorID: "U1", MessageID: "4.0", Text: "drain node 3"})
	marked := func() bool {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.sessionThreads["C1/200.1"]
	}
	waitFor(t, "the gateway to start a task in C1/200.1 and the adapter to mark it", marked)

	// A restart: the cache is gone, the registry is not.
	a.mu.Lock()
	a.sessionThreads = map[string]bool{}
	a.threadsOrder = nil
	a.mu.Unlock()
	got, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "stop", "5.0", "200.1"))
	if !ok || got.Conversation != "slack:C1/200.1" {
		t.Fatalf("an unmentioned follow-up after a restart: delivered=%v conv=%q — the registry did not answer", ok, got.Conversation)
	}
	if consulted != 1 {
		t.Errorf("the registry was consulted %d times for the follow-up, want 1 — the cache was empty and it is the only source", consulted)
	}

	// An unlisted sender's ask starts nothing, so it marks nothing.
	g.handleInbound(InboundMessage{Conversation: "slack:C1/300.1", Kind: "group", AuthorID: "U9", MessageID: "6.0", Text: "drain node 4"})
	a.mu.Lock()
	v, cached := a.sessionThreads["C1/300.1"]
	a.mu.Unlock()
	if cached {
		t.Fatalf("an unverified sender's ask marked its thread as %v; it must mark nothing", v)
	}
	if held, _, err := a.sessions(ctx, "slack:C1/300.1"); err != nil || held {
		t.Fatalf("the registry holds a session for the unverified sender's thread: held=%v err=%v", held, err)
	}
}

// TestSlackGatewayIdleThreadNeedsAFreshMention is the idle bound through
// the real gateway. A session thread stays one while a task runs there or
// the session has had activity within the idle TTL, and past that it needs
// a fresh mention -- whether or not its record is still in the registry,
// which it is: New without Run reaps nothing, and the reap would keep the
// record anyway. So with a short TTL: an ask starts a task and marks the
// thread; once the task is released (the relay's terminal, stood in for by
// a record write, since nothing terminates the placeholder in this rig) and
// the TTL passes, the adapter's cache has expired, the registry answers
// false off the stale LastActivity, and an unmentioned reply is not a turn.
// A mentioned ask starts a task again and the thread is back. And a task
// that keeps running keeps the thread past any TTL.
func TestSlackGatewayIdleThreadNeedsAFreshMention(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:jayanti\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack-idle"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)

	a := newTestSlackAdapter(&fakeSlackAPI{})
	const ttl = 300 * time.Millisecond
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          ttl,
		AttributionSalt:  []byte("test-salt"),
		// The CR-level gate is not under test here; the map is.
		SlackAllowAllUsers: true,
	}
	g, err := New(Options{Client: client, Adapter: a, Config: cfg, Backend: slackBackend, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if a.sessions == nil {
		t.Fatal("New did not offer the Slack adapter the session lookup")
	}
	if a.sessionTTL != ttl {
		t.Fatalf("New handed the adapter sessionTTL = %v, want the config's %v", a.sessionTTL, ttl)
	}
	const conv = "slack:C1/200.1"
	marked := func() bool {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.sessionThreads["C1/200.1"]
	}

	// The verified ask: task started, thread marked, replies carry.
	g.handleInbound(InboundMessage{Conversation: conv, Kind: "group", AuthorID: "U1", MessageID: "4.0", Text: "drain node 3"})
	waitFor(t, "the gateway to start a task in C1/200.1 and the adapter to mark it", marked)
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "and node 4 after", "5.0", "200.1")); !ok {
		t.Fatal("an unmentioned reply inside the TTL must deliver")
	}

	// The task ends. The relay clears ActiveTask on the terminal; here the
	// record is written the same way, the activity clocks untouched so that
	// they go stale on their own. The record and its task history stay.
	rec, err := g.reg.Get(ctx, conv)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("expected an active task on the record: rec=%+v err=%v", rec, err)
	}
	rec.ActiveTask = nil
	if err := g.reg.Put(ctx, rec); err != nil {
		t.Fatalf("releasing the task: %v", err)
	}
	// Released but inside the TTL: still a session thread, and the bound
	// the registry hands back is its own, the last task's activity + TTL.
	held, until, err := a.sessions(ctx, conv)
	if err != nil || !held {
		t.Fatalf("a released task inside the idle TTL must still be a session thread: held=%v err=%v", held, err)
	}
	if want := rec.LastTaskActivity.Add(ttl); !until.Equal(want) {
		t.Fatalf("the idle-bounded positive must expire at LastTaskActivity+TTL: until=%v want %v", until, want)
	}
	time.Sleep(ttl + 100*time.Millisecond)
	rec, err = g.reg.Get(ctx, conv)
	if err != nil || rec == nil || len(rec.Tasks) == 0 {
		t.Fatalf("the record must survive with its task history for this test to mean anything: rec=%+v err=%v", rec, err)
	}
	if held, _, err := a.sessions(ctx, conv); err != nil || held {
		t.Fatalf("an idle session with a record and a past task must not be a session thread: held=%v err=%v", held, err)
	}
	if got, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "stop", "6.0", "200.1")); ok {
		t.Fatalf("an unmentioned reply past the idle TTL must not be a turn; delivered %q", got.Text)
	}
	if marked() {
		t.Fatal("the expired mark must have been overwritten by the registry's false")
	}

	// A fresh mention starts a task and the thread is a session thread again.
	g.handleInbound(InboundMessage{Conversation: conv, Kind: "group", AuthorID: "U1", MessageID: "7.0", Text: "drain node 3, really"})
	waitFor(t, "the second ask to start a task and re-mark the thread", marked)
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "thanks", "8.0", "200.1")); !ok {
		t.Fatal("after a fresh mention started a task, the unmentioned reply must deliver")
	}

	// The running-task half: nothing terminates this task, and the TTL
	// passing does not end the session while it runs.
	time.Sleep(ttl + 100*time.Millisecond)
	if held, _, err := a.sessions(ctx, conv); err != nil || !held {
		t.Fatalf("a running task must keep the thread a session thread past the TTL: held=%v err=%v", held, err)
	}
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U1", "still going?", "9.0", "200.1")); !ok {
		t.Fatal("with a task running, an unmentioned reply past the TTL must deliver from the registry")
	}
}

// TestSlackGatewayDetachedTaskIsNotARunningTask: a task the user stopped
// whose terminal never arrived is left on the record with Detached set
// (cancelTask), and the reap treats it as not running. The lookup must
// share that predicate: a detached task with stale activity does not keep
// its thread a session thread forever, while the same task not detached
// does, however stale the activity. Driven through the real gateway and
// its registry, with the record written back the way the idle test
// releases a task.
func TestSlackGatewayDetachedTaskIsNotARunningTask(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:jayanti\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack-detached"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)

	a := newTestSlackAdapter(&fakeSlackAPI{})
	const ttl = 30 * time.Minute
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          ttl,
		AttributionSalt:  []byte("test-salt"),
		// The CR-level gate is not under test here; the map is.
		SlackAllowAllUsers: true,
	}
	g, err := New(Options{Client: client, Adapter: a, Config: cfg, Backend: slackBackend, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	const conv = "slack:C1/200.1"
	marked := func() bool {
		a.mu.Lock()
		defer a.mu.Unlock()
		return a.sessionThreads["C1/200.1"]
	}
	g.handleInbound(InboundMessage{Conversation: conv, Kind: "group", AuthorID: "U1", MessageID: "4.0", Text: "drain node 3"})
	waitFor(t, "the gateway to start a task in C1/200.1 and the adapter to mark it", marked)
	rec, err := g.reg.Get(ctx, conv)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("expected an active task on the record: rec=%+v err=%v", rec, err)
	}

	// The stop went out, no terminal came back, and the activity is stale.
	rec.ActiveTask.Detached = true
	rec.LastActivity = time.Now().Add(-2 * ttl)
	rec.LastTaskActivity = rec.LastActivity
	if err := g.reg.Put(ctx, rec); err != nil {
		t.Fatalf("detaching the task: %v", err)
	}
	if held, _, err := a.sessions(ctx, conv); err != nil || held {
		t.Fatalf("a detached task with stale activity must not keep the thread a session thread: held=%v err=%v", held, err)
	}
	// A restart: the cache is gone, the registry is the only source.
	a.mu.Lock()
	a.sessionThreads = map[string]bool{}
	a.sessionExpiresAt = map[string]time.Time{}
	a.threadsOrder = nil
	a.mu.Unlock()
	if _, ok := a.inbound(ctx, slackMsg("channel", "C1", "U2", "anyone?", "5.0", "200.1")); ok {
		t.Fatal("an unmentioned reply in a thread whose only task is detached and idle must not be a turn")
	}

	// The same record, task not detached: running, and the thread stays
	// a session thread whatever the activity says.
	rec.ActiveTask.Detached = false
	if err := g.reg.Put(ctx, rec); err != nil {
		t.Fatalf("re-attaching the task: %v", err)
	}
	if held, _, err := a.sessions(ctx, conv); err != nil || !held {
		t.Fatalf("a running task must keep the thread a session thread past any TTL: held=%v err=%v", held, err)
	}
}

// startSlackGateRig is startGchatRig for the Slack backend: a principal map
// file mapping U1 and U2 (both would resolve), the Slack allowlist pair as
// given, and a fake adapter whose roster is the two of them.
func startSlackGateRig(t *testing.T, allowed []string, allowAll bool) *rig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:one\nU2 test:two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
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
	adapter.roster = []string{"U1", "U2"}
	cfg := &Config{
		NATSURL:            url,
		PrincipalMapPath:   mapFile,
		DefaultAddressee:   "platform",
		IdleTTL:            30 * time.Minute,
		AttributionSalt:    []byte("test-salt"),
		SlackAllowedUsers:  allowed,
		SlackAllowAllUsers: allowAll,
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: slackBackend})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()
	return &rig{g: g, adapter: adapter, client: client, bus: bus, url: url}
}

func slackAuthorityOf(t *testing.T, r *rig) Authority {
	t.Helper()
	origin := r.awaitTask(t, "platform")
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatalf("authority block: %v", err)
	}
	return auth
}

// TestSlackMappedAndAllowedSenderIsAdmitted: the sender passes both gates
// and resolves through the map, as before the allowlist was carried.
func TestSlackMappedAndAllowedSenderIsAdmitted(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1"}, false)
	r.adapter.inbox <- InboundMessage{Conversation: "slack:D1", Kind: "dm", AuthorID: "U1", MessageID: "1.0", Text: "how is the fleet?"}
	auth := slackAuthorityOf(t, r)
	if want := NewPseudonymizer([]byte("test-salt")).Hash("test:one"); auth.Requester.Principal != want {
		t.Errorf("principal = %q, want the mapped principal's hash %q", auth.Requester.Principal, want)
	}
}

// TestSlackMappedButDisallowedSenderDropsVisiblyOnce: Chat's rule, carried
// to Slack. A member the map resolves but spec.integration.slack.allowedUsers
// does not list is refused exactly as an unmapped sender is (the same
// once-per-sender notice, naming the sender's id), and no task is published.
// Member ids compare exactly: "u2" is not "U2".
func TestSlackMappedButDisallowedSenderDropsVisiblyOnce(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1", "u2"}, false)
	for _, id := range []string{"1.0", "2.0"} {
		r.adapter.inbox <- InboundMessage{Conversation: "slack:D2", Kind: "dm", AuthorID: "U2", MessageID: id, Text: "do a thing"}
	}
	waitFor(t, "the unverified-sender notice", func() bool { return len(r.adapter.postTexts()) >= 1 })
	time.Sleep(200 * time.Millisecond)
	posts := r.adapter.postTexts()
	if len(posts) != 1 {
		t.Fatalf("posts = %v, want exactly one notice for two messages", posts)
	}
	if !strings.Contains(posts[0], "can't verify") || !strings.Contains(posts[0], "U2") {
		t.Errorf("notice %q should be the unverified-sender notice naming the sender", posts[0])
	}
	if want := "an admin has to add you to " + unverifiedRemedyFor(slackBackend) + "."; !strings.HasSuffix(posts[0], want) {
		t.Errorf("notice %q should be the one an unmapped Slack sender gets, ending %q", posts[0], want)
	}
	if !strings.Contains(posts[0], "allowed users list") {
		t.Errorf("notice %q should name the Slack allowlist among the remedies", posts[0])
	}
	if got := len(inSubjectEnvelopes(t, r.url, "platform")); got != 0 {
		t.Errorf("%d task envelopes published for a disallowed sender", got)
	}
}

// TestSlackListedUnmappedSenderIsAttributedByMemberID: the map is an
// override, not a gate. A listed sender it does not name is admitted and
// attributed slack:<member id>, with the verifiedBy that says no map was
// consulted, and they are in their own audience under that same principal.
func TestSlackListedUnmappedSenderIsAttributedByMemberID(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1", "U9"}, false)
	r.adapter.inbox <- InboundMessage{Conversation: "slack:D9", Kind: "dm", AuthorID: "U9", MessageID: "1.0", Text: "do a thing"}
	auth := slackAuthorityOf(t, r)
	ps := NewPseudonymizer([]byte("test-salt"))
	if want := ps.Hash(slackMemberPrincipalPrefix + "U9"); auth.Requester.Principal != want {
		t.Errorf("principal = %q, want the pseudonym of %q", auth.Requester.Principal, slackMemberPrincipalPrefix+"U9")
	}
	if auth.Requester.VerifiedBy != slackMemberVerifiedBy {
		t.Errorf("verifiedBy = %q, want %q (no map was consulted)", auth.Requester.VerifiedBy, slackMemberVerifiedBy)
	}
	if !slices.Contains(auth.Audience.Roster, auth.Requester.Principal) {
		t.Errorf("the requester's principal %q is not in their own audience %v", auth.Requester.Principal, auth.Audience.Roster)
	}
}

// TestSlackPartialMapAttributesEachSenderItsOwnWay: with a map naming U1
// and not U9, U1 is attributed by the map and U9 by member id, and each
// authority block says which.
func TestSlackPartialMapAttributesEachSenderItsOwnWay(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1", "U9"}, false)
	for _, tc := range []struct{ id, principal, verifiedBy string }{
		{"U1", "test:one", slackVerifiedBy},
		{"U9", slackMemberPrincipalPrefix + "U9", slackMemberVerifiedBy},
	} {
		if got := r.g.resolvePrincipal(slackBackend, tc.id); got != tc.principal {
			t.Errorf("%s resolved to %q, want %q", tc.id, got, tc.principal)
		}
		if got := r.g.verifiedByOf(slackBackend, tc.principal); got != tc.verifiedBy {
			t.Errorf("%s: verifiedBy = %q, want %q", tc.id, got, tc.verifiedBy)
		}
	}
	r.adapter.inbox <- InboundMessage{Conversation: "slack:D1", Kind: "dm", AuthorID: "U1", MessageID: "1.0", Text: "do a thing"}
	auth := slackAuthorityOf(t, r)
	if auth.Requester.Principal != NewPseudonymizer([]byte("test-salt")).Hash("test:one") || auth.Requester.VerifiedBy != slackVerifiedBy {
		t.Errorf("the mapped sender's requester = %+v, want the map's principal and %q", auth.Requester, slackVerifiedBy)
	}
}

// TestSlackMapCannotAssertTheMemberIDPrefix: a map value carrying the
// reserved prefix is refused, so no principal a map entry made can claim to
// be a bare member id, and the mistake is a lockout, not a grant.
func TestSlackMapCannotAssertTheMemberIDPrefix(t *testing.T) {
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U5 "+slackMemberPrincipalPrefix+"U1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pm, err := LoadPrincipalMap(mapFile)
	if err != nil {
		t.Fatal(err)
	}
	g := &Gateway{pm: pm, slackAllowAll: true, log: slog.Default()}
	if got := g.resolvePrincipal(slackBackend, "U5"); got != "" {
		t.Errorf("a map value carrying %q resolved to %q; it must be refused", slackMemberPrincipalPrefix, got)
	}
	if got := g.resolvePrincipal(slackBackend, ""); got != "" {
		t.Errorf("an empty member id resolved to %q", got)
	}
	// The notice such a member gets must point at the map entry, since they
	// are already on the list.
	if remedy := unverifiedRemedyFor(slackBackend); !strings.Contains(remedy, "principal map") {
		t.Errorf("the Slack remedy %q does not name the principal map", remedy)
	}
}

// TestSlackRosterRefusesTheReservedPrefixSilently: the roster resolves every
// channel member on every turn, so a reserved-prefix map entry is refused
// there without a log line; the requester path is where it is logged.
func TestSlackRosterRefusesTheReservedPrefixSilently(t *testing.T) {
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U5 "+slackMemberPrincipalPrefix+"U1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pm, err := LoadPrincipalMap(mapFile)
	if err != nil {
		t.Fatal(err)
	}
	logs := &recordingHandler{}
	g := &Gateway{pm: pm, slackAllowAll: true, log: slog.New(logs)}
	if got := g.rosterResolver(slackBackend)("U5"); got != "" {
		t.Errorf("the roster resolved a reserved-prefix entry to %q", got)
	}
	if _, found := logs.level("reserved member-id prefix"); found {
		t.Error("the roster path logged the reserved-prefix refusal; it must be silent")
	}
	if got := g.resolvePrincipal(slackBackend, "U5"); got != "" {
		t.Errorf("the requester path resolved a reserved-prefix entry to %q", got)
	}
	if lvl, found := logs.level("reserved member-id prefix"); !found || lvl != slog.LevelError {
		t.Errorf("the requester path's refusal: found=%t level=%v, want ERROR", found, lvl)
	}
}

// TestSlackAllowAllAdmitsEverySender: allow-all lifts the list, the only
// gate: a mapped sender is attributed by the map, an unmapped one by member
// id.
func TestSlackAllowAllAdmitsEverySender(t *testing.T) {
	r := startSlackGateRig(t, nil, true)
	r.adapter.inbox <- InboundMessage{Conversation: "slack:D2", Kind: "dm", AuthorID: "U2", MessageID: "1.0", Text: "hello"}
	auth := slackAuthorityOf(t, r)
	if want := NewPseudonymizer([]byte("test-salt")).Hash("test:two"); auth.Requester.Principal != want {
		t.Errorf("principal = %q, want %q", auth.Requester.Principal, want)
	}
	if p := r.g.resolvePrincipal(slackBackend, "U9"); p != slackMemberPrincipalPrefix+"U9" {
		t.Errorf("allow-all resolved an unmapped sender to %q, want %q", p, slackMemberPrincipalPrefix+"U9")
	}
}

// TestSlackEmptyAllowlistWithoutAllowAllDropsEveryone: Chat's posture for
// the shape the operator renders from a degenerate CR list.
func TestSlackEmptyAllowlistWithoutAllowAllDropsEveryone(t *testing.T) {
	r := startSlackGateRig(t, nil, false)
	for _, id := range []string{"U1", "U2"} {
		if p := r.g.resolvePrincipal(slackBackend, id); p != "" {
			t.Errorf("%s resolved to %q with an empty allowlist and allow-all off", id, p)
		}
	}
}

// TestSlackRosterDoesNotAttributeADisallowedMember: the roster is read under
// the same gate as the requester, so a mapped member the allowlist refuses
// is recorded by backend id, not named by the principal the map would give.
func TestSlackRosterDoesNotAttributeADisallowedMember(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1"}, false)
	r.adapter.inbox <- InboundMessage{Conversation: "slack:C1/1.0", Kind: "group", AuthorID: "U1", MessageID: "1.0", Text: "how is the fleet?"}
	auth := slackAuthorityOf(t, r)
	ps := NewPseudonymizer([]byte("test-salt"))
	roster := strings.Join(auth.Audience.Roster, ",")
	if !strings.Contains(roster, ps.Hash("test:one")) {
		t.Errorf("roster %v is missing the allowed requester's principal", auth.Audience.Roster)
	}
	if strings.Contains(roster, ps.Hash("test:two")) {
		t.Errorf("roster %v names the disallowed member's mapped principal", auth.Audience.Roster)
	}
	if !strings.Contains(roster, ps.Hash("U2")) {
		t.Errorf("roster %v should carry the disallowed member by backend id", auth.Audience.Roster)
	}
	rec, err := r.g.reg.Get(context.Background(), "slack:C1/1.0")
	if err != nil || rec == nil {
		t.Fatalf("record: %v %v", rec, err)
	}
	if strings.Contains(strings.Join(rec.Roster, ","), ps.Hash("test:two")) {
		t.Errorf("session record roster %v names the disallowed member's mapped principal", rec.Roster)
	}
}

// TestSlackWorkspaceCheckReadsADecodedEvent: the workspace check on events
// decoded the way Socket Mode delivers them (json into
// slackevents.MessageEvent), not built by hand. slack-go decodes a plain
// message's top-level fields into MessageEvent.Message as well, so the
// message's own team field is read even when user_team is absent.
func TestSlackWorkspaceCheckReadsADecodedEvent(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.teamID = "T0URS"
	decode := func(raw string) *slackevents.MessageEvent {
		t.Helper()
		var m slackevents.MessageEvent
		if err := json.Unmarshal([]byte(raw), &m); err != nil {
			t.Fatalf("decoding %s: %v", raw, err)
		}
		return &m
	}
	for _, tc := range []struct {
		name, raw string
		turn      bool
	}{
		{"team only, foreign", `{"type":"message","channel":"D1","channel_type":"im","user":"UGUEST","text":"drain node 4","ts":"1.0","team":"T0THER"}`, false},
		{"user_team, foreign", `{"type":"message","channel":"D2","channel_type":"im","user":"UGUEST","text":"drain node 4","ts":"2.0","team":"T0URS","user_team":"T0THER"}`, false},
		{"ours", `{"type":"message","channel":"D3","channel_type":"im","user":"U1","text":"how is the fleet","ts":"3.0","team":"T0URS"}`, true},
		{"no workspace named", `{"type":"message","channel":"D4","channel_type":"im","user":"U1","text":"hello","ts":"4.0"}`, true},
	} {
		m := decode(tc.raw)
		if _, ok := a.inbound(context.Background(), m); ok != tc.turn {
			t.Errorf("%s: delivered=%v, want %v (Message.Team=%q, UserTeam=%q)", tc.name, ok, tc.turn, teamOf(m), m.UserTeam)
		}
	}
}

func teamOf(m *slackevents.MessageEvent) string {
	if m.Message == nil {
		return "<nil Message>"
	}
	return m.Message.Team
}
