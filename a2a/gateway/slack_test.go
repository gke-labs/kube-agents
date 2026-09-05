package gateway

import (
	"log/slog"
	"strings"
	"testing"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
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

func (f *fakeSlackAPI) GetConversationReplies(params *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error) {
	return f.replies[params.ChannelID+"/"+params.Timestamp], false, "", nil
}

func newTestSlackAdapter(api *fakeSlackAPI) *SlackAdapter {
	return &SlackAdapter{api: api, log: slog.Default(), botUserID: "UBOT",
		sessionRoots: map[string]bool{}, seen: map[string]bool{}}
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
// thread root did (bot-rooted threads carry every message — the parity
// with Discord's bot-created threads).
func TestSlackInboundAffordanceRule(t *testing.T) {
	api := &fakeSlackAPI{replies: map[string][]slack.Message{
		"C1/100.1": {{Msg: slack.Msg{Text: "<@UBOT> check the nodes", User: "U1"}}},
		"C1/200.1": {{Msg: slack.Msg{Text: "lunch?", User: "U2"}}},
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
		{"reply in bot-rooted thread delivers unmentioned", slackMsg("channel", "C1", "U3", "steer it", "5.0", "100.1"), true, "slack:C1/100.1", "group", "steer it"},
		{"reply in plain thread drops", slackMsg("channel", "C1", "U3", "chatter", "6.0", "200.1"), false, "", "", ""},
		{"bare mention drops", slackMsg("channel", "C1", "U1", "<@UBOT>", "7.0", ""), false, "", "", ""},
	}
	for _, c := range cases {
		got, ok := a.inbound(c.m)
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
	if _, ok := a.inbound(slackMsg("im", "D1", "UBOT", "self", "1.0", "")); ok {
		t.Error("own messages must drop")
	}
	bot := slackMsg("im", "D1", "U9", "from an app", "2.0", "")
	bot.BotID = "B123"
	if _, ok := a.inbound(bot); ok {
		t.Error("bot messages must drop")
	}
	edited := slackMsg("im", "D1", "U1", "edited", "3.0", "")
	edited.SubType = "message_changed"
	if _, ok := a.inbound(edited); ok {
		t.Error("non-empty subtypes must drop")
	}
	dup := slackMsg("im", "D1", "U1", "once", "4.0", "")
	if _, ok := a.inbound(dup); !ok {
		t.Fatal("first delivery expected")
	}
	if _, ok := a.inbound(dup); ok {
		t.Error("socket mode is at-least-once; a duplicate (channel,ts) must drop")
	}
	if _, ok := a.inbound(slackMsg("channel", "", "U1", "<@UBOT> x", "5.0", "")); ok {
		t.Error("empty channel must drop")
	}
	if _, ok := a.inbound(slackMsg("im", "D1", "", "ghost", "6.0", "")); ok {
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
	if _, ok := a.inbound(broadcast); !ok {
		t.Error("thread_broadcast reply in a bot-rooted thread must deliver")
	}

	file := slackMsg("im", "D1", "U1", "here is the manifest", "9.0", "")
	file.SubType = "file_share"
	if _, ok := a.inbound(file); !ok {
		t.Error("file_share with text must deliver")
	}
}
