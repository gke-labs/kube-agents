package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/slack-go/slack/slackevents"
)

// appPost is a message as Slack delivers one posted with a user token through
// an app: the member's own user id, and the app's bot_id beside it.
func appPost(user, ts string) *slackevents.MessageEvent {
	m := slackMsg("im", "D1", user, "what is 2 + 3?", ts, "")
	m.BotID = "BDRIVER"
	return m
}

// A listed test user's app-attributed post is a turn from that user; an
// unlisted member's is still dropped as bot traffic, and the bot's own posts
// are dropped even when its member id is on the list.
func TestSlackTestUsersAppPostsAreReadAsTheirs(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	ctx := context.Background()

	if _, ok := a.inbound(ctx, appPost("UTEST", "100.1")); ok {
		t.Fatal("an app-attributed post was a turn with no test users configured")
	}

	a.SetTestUsers([]string{" UTEST ", "", "UBOT"})
	msg, ok := a.inbound(ctx, appPost("UTEST", "100.2"))
	if !ok || msg.AuthorID != "UTEST" || msg.Text != "what is 2 + 3?" {
		t.Fatalf("listed test user's app post = %+v, %v; want a turn from UTEST", msg, ok)
	}
	if _, ok := a.inbound(ctx, appPost("UOTHER", "100.3")); ok {
		t.Fatal("an unlisted member's app-attributed post was a turn")
	}
	if _, ok := a.inbound(ctx, appPost("UBOT", "100.4")); ok {
		t.Fatal("the bot's own post was a turn because its id was listed")
	}
	if _, ok := a.inbound(ctx, appPost("", "100.5")); ok {
		t.Fatal("an app post with no user was a turn")
	}
	// A plain message from the test user is a turn as before.
	if _, ok := a.inbound(ctx, slackMsg("im", "D1", "UTEST", "hi", "100.6", "")); !ok {
		t.Fatal("the test user's own message stopped being a turn")
	}
	// The dedupe still applies to a listed user's post.
	if _, ok := a.inbound(ctx, appPost("UTEST", "100.2")); ok {
		t.Fatal("a redelivered test-user post was a second turn")
	}
}

// A listed test user from another workspace is still refused: the list lifts
// the bot_id drop and nothing else.
func TestSlackTestUsersStillMeetTheWorkspaceGate(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.teamID = "T0URS"
	a.syncSessions = true
	a.SetTestUsers([]string{"UTEST"})
	m := appPost("UTEST", "200.1")
	m.UserTeam = "T0THER"
	if _, ok := a.inbound(context.Background(), m); ok {
		t.Fatal("a listed test user from another workspace was a turn")
	}
}

func TestFromEnvReadsTheSlackTestUsers(t *testing.T) {
	setBaseEnv(t)
	t.Setenv(EnvSlackTestUsers, "U1, ,U2")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatalf("FromEnv: %v", err)
	}
	if len(cfg.SlackTestUsers) != 2 || cfg.SlackTestUsers[0] != "U1" || cfg.SlackTestUsers[1] != "U2" {
		t.Fatalf("SlackTestUsers = %v", cfg.SlackTestUsers)
	}
}

// A listed test user's app post reaches the gateway as that member's message
// and meets the allowlist like any other: a test user the allowlist does not
// name is refused with the unverified-sender notice, and no task is published.
// The list lifts the adapter's bot_id drop and widens nothing past it.
func TestSlackTestUserOffTheAllowlistIsRefused(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.SetTestUsers([]string{"U2"})
	msg, ok := a.inbound(context.Background(), appPost("U2", "300.1"))
	if !ok {
		t.Fatal("the listed test user's app post was dropped by the adapter")
	}
	r := startSlackGateRig(t, []string{"U1"}, false)
	r.adapter.inbox <- msg
	waitFor(t, "the unverified-sender notice", func() bool { return len(r.adapter.postTexts()) >= 1 })
	time.Sleep(200 * time.Millisecond)
	if posts := r.adapter.postTexts(); !strings.Contains(posts[0], "can't verify") || !strings.Contains(posts[0], "U2") {
		t.Errorf("notice %q should be the unverified-sender notice naming the test user", posts[0])
	}
	if got := len(inSubjectEnvelopes(t, r.url, "platform")); got != 0 {
		t.Errorf("%d task envelopes published for a test user off the allowlist", got)
	}
}
