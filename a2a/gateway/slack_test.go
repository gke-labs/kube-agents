package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/slack-go/slack/socketmode"
)

// fakeSlackAPI fakes the six Web API calls the adapter makes; tests assert
// on what was posted/updated and canned replies drive the root check.
type fakeSlackAPI struct {
	replies  map[string][]slack.Message // channel+"/"+threadTS -> msgs, root first
	posted   []struct{ channel, thread, text string }
	updated  []struct{ channel, ts, text string }
	members  []string
	cursor   string
	openedIM string
	// repliesCalls counts conversations.replies reads. That call is the one
	// synchronous Web API round trip made on the event pump's own goroutine,
	// so tests assert on how often it happens, not only on its answer.
	repliesCalls int
}

func (f *fakeSlackAPI) AuthTest() (*slack.AuthTestResponse, error) {
	return &slack.AuthTestResponse{UserID: "UBOT", User: "kage"}, nil
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

// GetConversationRepliesContext honours ctx the way the real client does —
// a cancelled or expired context comes back as ctx.Err() and no messages —
// so tests can drive the shutdown and timeout paths of isSessionThread.
func (f *fakeSlackAPI) GetConversationRepliesContext(ctx context.Context, params *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error) {
	f.repliesCalls++
	if err := ctx.Err(); err != nil {
		return nil, false, "", err
	}
	return f.replies[params.ChannelID+"/"+params.Timestamp], false, "", nil
}

func newTestSlackAdapter(api *fakeSlackAPI) *SlackAdapter {
	return &SlackAdapter{api: api, log: slog.Default(), botUserID: "UBOT",
		sessionThreads: map[string]bool{}, seen: map[string]bool{}}
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
// always; channel messages only when they mention the bot (the ask roots
// the session thread); thread replies when they mention the bot or the
// thread is already a session thread (session threads carry every message —
// the parity with Discord's bot-created threads).
//
// The cases run in order against one adapter, because the rule is stateful:
// a mention in a thread makes that thread a session thread for the cases
// after it. 200.1 is the thread the bot is pulled into mid-conversation and
// 300.1 the one it is never addressed in, and they are separate threads for
// exactly that reason.
func TestSlackInboundAffordanceRule(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/100.1": {{Msg: slack.Msg{Text: "<@UBOT> check the nodes", User: "U1"}}},
		"C1/200.1": {{Msg: slack.Msg{Text: "lunch?", User: "U2"}}},
		"C1/300.1": {{Msg: slack.Msg{Text: "anyone seen the changelog?", User: "U2"}}},
	}}
	a := newTestSlackAdapter(api)

	cases := []struct {
		name string
		m    *slackevents.MessageEvent
		want bool
		conv string
		kind string
		text string
	}{
		{"dm delivers", slackMsg("im", "D1", "U1", "hi", "1.0", ""), true, "slack:dm/D1", "dm", "hi"},
		{"channel without mention drops", slackMsg("channel", "C1", "U1", "hello", "2.0", ""), false, "", "", ""},
		{"channel mention roots a thread on the ask", slackMsg("channel", "C1", "U1", "<@UBOT> do a thing", "3.5", ""), true, "slack:C1/3.5", "group", "do a thing"},
		{"display-name mention form strips", slackMsg("channel", "C1", "U1", "<@UBOT|kage> do it", "3.6", ""), true, "slack:C1/3.6", "group", "do it"},
		{"thread reply with mention delivers", slackMsg("channel", "C1", "U1", "<@UBOT> and this", "4.0", "200.1"), true, "slack:C1/200.1", "group", "and this"},
		// The mention above minted a session on slack:C1/200.1, so that
		// thread now carries every message — the follow-up the user expects
		// to be able to steer or stop with.
		{"unmentioned follow-up in an adopted thread delivers", slackMsg("channel", "C1", "U1", "stop", "4.5", "200.1"), true, "slack:C1/200.1", "group", "stop"},
		{"reply in bot-rooted thread delivers unmentioned", slackMsg("channel", "C1", "U3", "steer it", "5.0", "100.1"), true, "slack:C1/100.1", "group", "steer it"},
		{"reply in plain thread drops", slackMsg("channel", "C1", "U3", "chatter", "6.0", "300.1"), false, "", "", ""},
		{"bare mention drops", slackMsg("channel", "C1", "U1", "<@UBOT>", "7.0", ""), false, "", "", ""},
		// Slack transmits &, < and > entity-encoded; the ask must reach the
		// executor as the user typed it.
		{"entities decode in a dm", slackMsg("im", "D1", "U1", "get pods -n foo &amp;&amp; describe node &lt;name&gt;", "10.0", ""), true, "slack:dm/D1", "dm", "get pods -n foo && describe node <name>"},
		{"entities decode after the mention strip", slackMsg("channel", "C1", "U1", "<@UBOT> scale web if cpu &gt; 80%", "11.0", ""), true, "slack:C1/11.0", "group", "scale web if cpu > 80%"},
		{"entities decode in a thread steer", slackMsg("channel", "C1", "U3", "and &lt;this&gt; too", "12.0", "100.1"), true, "slack:C1/100.1", "group", "and <this> too"},
	}
	for _, c := range cases {
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
		t.Error("non-empty subtypes must drop")
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

// TestSlackTurnSubtypes: thread_broadcast is a steer with "also send to
// channel" checked and file_share is an ask with an attachment — both are
// genuine turns and must not vanish silently. Edits stay dropped.
func TestSlackTurnSubtypes(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/100.1": {{Msg: slack.Msg{Text: "<@UBOT> check the nodes", User: "U1"}}},
	}}
	a := newTestSlackAdapter(api)

	broadcast := slackMsg("channel", "C1", "U2", "also try the east cluster", "8.0", "100.1")
	broadcast.SubType = "thread_broadcast"
	if _, ok := a.inbound(context.Background(), broadcast); !ok {
		t.Error("thread_broadcast reply in a bot-rooted thread must deliver")
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

// TestSlackEmptyTurnSkipsRootLookup: an attachment-only or whitespace-only
// reply is dropped either way, so it must not pay for the thread-root read
// first. That read runs on the event pump's goroutine under
// slackRepliesTimeout, and the next envelope's ack waits behind it — a
// two-second stall spent to discard the message. The control case — a reply
// with text, in a thread of its OWN — proves the guard still reads when the
// answer matters.
//
// The control's thread is separate on purpose. Sharing 300.1 with the empty
// cases made the final count assertion worthless: with the guard removed the
// attachment case does the read and caches the answer, the whitespace and
// control cases then both hit that cache, and repliesCalls lands on exactly
// the 1 the test wanted. It passed on a cache hit while claiming to prove a
// read. In its own uncached thread the control has to spend the call, so the
// same "want 1" now reads 2 the moment the guard goes.
func TestSlackEmptyTurnSkipsRootLookup(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/300.1": {{Msg: slack.Msg{Text: "<@UBOT> watch the rollout", User: "U1"}}},
		"C1/310.1": {{Msg: slack.Msg{Text: "<@UBOT> and this one too", User: "U1"}}},
	}}
	a := newTestSlackAdapter(api)

	// A file_share reply with no caption, in a thread nothing has cached.
	attachment := slackMsg("channel", "C1", "U2", "", "301.0", "300.1")
	attachment.SubType = "file_share"
	if _, ok := a.inbound(context.Background(), attachment); ok {
		t.Error("an empty reply is not a turn")
	}
	if api.repliesCalls != 0 {
		t.Errorf("empty reply made %d conversations.replies calls, want 0", api.repliesCalls)
	}

	whitespace := slackMsg("channel", "C1", "U2", "   \n\t ", "302.0", "300.1")
	if _, ok := a.inbound(context.Background(), whitespace); ok {
		t.Error("a whitespace-only reply is not a turn")
	}
	if api.repliesCalls != 0 {
		t.Errorf("whitespace reply made %d conversations.replies calls, want 0", api.repliesCalls)
	}

	// Control: a different thread, uncached by anything above, with text.
	// The read must happen, and the reply must deliver.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U2", "steer it", "313.0", "310.1")); !ok {
		t.Error("an unmentioned reply in a bot-rooted thread must deliver")
	}
	if api.repliesCalls != 1 {
		t.Errorf("made %d conversations.replies calls, want 1 — only the control should read", api.repliesCalls)
	}
}

// TestSlackBareMentionStillRootsTheThread pins the side effect the empty-text
// check above must not skip past. A bare "@bot" is not a turn, but it does
// root a thread, and recording that costs no API call — so the first real
// reply under it delivers from cache rather than spending
// slackRepliesTimeout re-reading a root we already saw.
func TestSlackBareMentionStillRootsTheThread(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT>", "400.0", "")); ok {
		t.Fatal("a bare mention has nothing to run")
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U2", "here is the ask", "401.0", "400.0")); !ok {
		t.Error("a reply under a bare mention must deliver: the root mentioned the bot")
	}
	// api.replies is empty, so a lookup would have answered false and
	// dropped the reply. Delivering proves it came from the cache fill.
	if api.repliesCalls != 0 {
		t.Errorf("root was re-read %d times; the bare mention should have cached it", api.repliesCalls)
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
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_auth"}`))
	}))
	defer srv.Close()

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
	close(release)
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
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_auth"}`))
	}))
	defer srv.Close()

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
	close(release)
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
	for {
		select {
		case evt := <-a.sm.Events:
			if evt.Request != nil && evt.Request.EnvelopeID == envelopeID {
				return true
			}
		default:
			return false
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
// what 9239f5df set out to do and did only about half the time.
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
	for i := 0; i < slackCancelledPumpRuns; i++ {
		slackCancelledPumpRun(srv.URL+"/", fmt.Sprintf("Env-noack-%d", i),
			fmt.Sprintf("810.%03d", i), logs, func(InboundMessage) {})
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

// TestSlackRootLookupLogLevels: the thread-root read fires on every
// unmentioned thread reply, so its failure log is the noisiest thing in the
// adapter and a WARN on every pod termination is a false positive for anything
// alerting on logs. Cancelled is demoted. DeadlineExceeded is NOT — at this
// site that is slackRepliesTimeout genuinely expiring on a slow
// conversations.replies, which cost a user their reply and is the real
// operational signal a blanket "any context error" filter would swallow.
func TestSlackRootLookupLogLevels(t *testing.T) {
	// Shutdown: the parent context is already cancelled.
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	shutdown := &recordingHandler{}
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.log = slog.New(shutdown)
	if a.isSessionThread(cancelled, "C1", "700.1") {
		t.Error("a failed lookup must report false")
	}
	if lvl, ok := shutdown.level("thread root lookup"); !ok || lvl != slog.LevelDebug {
		t.Errorf("cancelled lookup logged at %v (found=%v), want DEBUG", lvl, ok)
	}

	// The timeout, modelled with a parent whose deadline has already passed
	// so the derived context reports DeadlineExceeded rather than Canceled.
	expired, cancelExpired := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancelExpired()
	timedOut := &recordingHandler{}
	b := newTestSlackAdapter(&fakeSlackAPI{})
	b.log = slog.New(timedOut)
	if b.isSessionThread(expired, "C1", "700.2") {
		t.Error("a timed-out lookup must report false")
	}
	if lvl, ok := timedOut.level("thread root lookup"); !ok || lvl != slog.LevelWarn {
		t.Errorf("timed-out lookup logged at %v (found=%v), want WARN — slackRepliesTimeout expiring is a dropped reply", lvl, ok)
	}

	// And a plain empty answer, with no context involved at all, still warns.
	empty := &recordingHandler{}
	c := newTestSlackAdapter(&fakeSlackAPI{})
	c.log = slog.New(empty)
	if c.isSessionThread(context.Background(), "C1", "700.3") {
		t.Error("an unknown thread root must report false")
	}
	if lvl, ok := empty.level("thread root lookup"); !ok || lvl != slog.LevelWarn {
		t.Errorf("empty root lookup logged at %v (found=%v), want WARN", lvl, ok)
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
// someone else's thread, which delivers a turn keyed on that thread, and
// then follows up unmentioned — a steer, or "stop". That follow-up has to
// reach the gateway, because a session is already running there. Before the
// adapter recorded the mention, the follow-up hit the root check, the root
// read found a message with no mention, and the message was discarded
// without even a drop notice: nothing reached handleInbound.
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
			api := &fakeSlackAPI{replies: map[string][]slack.Message{
				"C1/200.1": {{Msg: slack.Msg{Text: "lunch?", User: "U2"}}},
			}}
			a := newTestSlackAdapter(api)

			got, ok := a.inbound(context.Background(), c.msg("<@UBOT> drain node 3", "4.0", "200.1"))
			if !ok || got.Conversation != "slack:C1/200.1" {
				t.Fatalf("mention in a foreign thread: delivered=%v conv=%q", ok, got.Conversation)
			}
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

// TestSlackBareMentionAdoptsForeignThread: a bare "@bot" is not a turn
// (nothing to run), but it is still the user addressing the bot in that
// thread, so the ask that follows it unmentioned is one. Same rule as the
// channel case, where a bare mention roots a thread whose later replies are
// turns; the thread being someone else's does not change it.
func TestSlackBareMentionAdoptsForeignThread(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/200.1": {{Msg: slack.Msg{Text: "lunch?", User: "U2"}}},
	}}
	a := newTestSlackAdapter(api)

	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT>", "4.0", "200.1")); ok {
		t.Fatal("a bare mention has nothing to run and must not deliver")
	}
	if api.repliesCalls != 0 {
		t.Errorf("bare mention made %d conversations.replies reads, want 0", api.repliesCalls)
	}
	got, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "drain node 3", "5.0", "200.1"))
	if !ok || got.Conversation != "slack:C1/200.1" {
		t.Fatalf("the ask after a bare mention: delivered=%v conv=%q", ok, got.Conversation)
	}
	if api.repliesCalls != 0 {
		t.Errorf("the recorded mention should have answered from cache; %d replies reads", api.repliesCalls)
	}
}

// TestSlackMentionUnpoisonsCachedFalse: the root check caches its answer, so
// an unmentioned reply that arrives BEFORE the bot is pulled into the thread
// leaves a false behind. A later mention has to overwrite it, or the thread
// is dropped for the life of the cache entry — including the "stop" for the
// session that mention started.
func TestSlackMentionUnpoisonsCachedFalse(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/200.1": {{Msg: slack.Msg{Text: "lunch?", User: "U2"}}},
	}}
	a := newTestSlackAdapter(api)

	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U3", "chatter", "3.0", "200.1")); ok {
		t.Fatal("chatter in a thread the bot is not in must drop")
	}
	if v, cached := a.sessionThreads["C1/200.1"]; !cached || v {
		t.Fatalf("want a cached false for the thread; cached=%v value=%v", cached, v)
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> drain node 3", "4.0", "200.1")); !ok {
		t.Fatal("mention in the thread must deliver")
	}
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "stop", "5.0", "200.1")); !ok {
		t.Fatal("the cached false outlived the session it silenced")
	}
	// One entry, one eviction slot: the overwrite must not double-book the
	// ring or the cache would evict short of its cap.
	if n := len(a.threadsOrder); n != 1 {
		t.Errorf("threadsOrder = %d entries, want 1", n)
	}
}
