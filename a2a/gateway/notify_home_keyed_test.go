package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// A keyed home-channel request: the event watcher's alert, posted under
// alert-<session> as it starts a thread, and the triage card's plan, question
// and triage edit posted under their keys on that thread. The key is one
// message per home thread: a later request with it edits the message, laid
// out or as text, and one that finds no message there posts anew.

// fakeHomePoster lays home posts out and edits them (notifyChatPoster and
// notifyChatEditor over fakeNotifyPoster): chats records the chat message of
// each laid-out post or edit, edits each edit as (space, message, text).
// refuseLayout answers every laid-out post or edit with ErrLayoutRefused;
// failEdits every edit with ErrMessageGone.
type fakeHomePoster struct {
	*fakeNotifyPoster
	chats        []*chatmsg.Message
	edits        []notifyPost
	editCalls    int
	refuseLayout bool
	failEdits    bool
}

func (f *fakeHomePoster) PostNotifyMessage(space, thread string, m OutMessage) (string, string, error) {
	if f.refuseLayout {
		return "", "", fmt.Errorf("%w: fake", ErrLayoutRefused)
	}
	f.chats = append(f.chats, m.Chat)
	return f.PostNotify(space, thread, m.Text)
}

func (f *fakeHomePoster) EditNotifyMessage(space, _, message string, m OutMessage) error {
	f.editCalls++
	if m.Chat != nil && f.refuseLayout {
		return ErrLayoutRefused
	}
	if f.failEdits {
		return ErrMessageGone
	}
	f.edits = append(f.edits, notifyPost{space, message, m.Text})
	f.chats = append(f.chats, m.Chat)
	return nil
}

// fakeHomeLayoutOnly lays home posts out but edits nothing.
type fakeHomeLayoutOnly struct{ *fakeNotifyPoster }

func (f *fakeHomeLayoutOnly) PostNotifyMessage(space, thread string, m OutMessage) (string, string, error) {
	return f.PostNotify(space, thread, m.Text)
}

const testHomeThread = testHome + "/threads/T1"

func newHomeKeyedNotifier(t *testing.T) (*Notifier, *fakeHomePoster) {
	t.Helper()
	p := &fakeHomePoster{fakeNotifyPoster: &fakeNotifyPoster{landsIn: testHomeThread}}
	n, err := NewGchatNotifier(p, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	return n, p
}

func homeKeyed(text, key, thread string) lib.NotifyRequest {
	return lib.NotifyRequest{Text: text, Chat: json.RawMessage(testHomeChat), Update: key, Thread: thread}
}

func TestNotifyHomeUpdateKeyEditsTheMessageItPosted(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	first := serveJSON(t, n, homeKeyed("plan", "plan-c1-1", testHomeThread))
	second := serveJSON(t, n, homeKeyed("plan, later", "plan-c1-1", testHomeThread))
	if first.Error != "" || second.Error != "" || second.MessageID != first.MessageID || second.ThreadID != testHomeThread {
		t.Fatalf("replies = %+v, %+v; want the second to name the first's message on the thread", first, second)
	}
	if posts := p.all(); len(posts) != 1 || len(p.edits) != 1 || p.edits[0].thread != first.MessageID || p.edits[0].text != "plan, later" {
		t.Fatalf("posts = %+v, edits = %+v; want one post and one edit of it", posts, p.edits)
	}
	if len(p.chats) != 2 || p.chats[1] == nil || p.chats[1].Headline == "" {
		t.Fatalf("chats = %+v, want the layout on the post and the edit", p.chats)
	}
}

func TestNotifyHomeUpdateKeyIsPerThread(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	first := serveJSON(t, n, homeKeyed("plan", "plan-c1-1", testHomeThread))
	other := serveJSON(t, n, homeKeyed("plan", "plan-c1-1", testHome+"/threads/T2"))
	if other.MessageID == first.MessageID || len(p.all()) != 2 || len(p.edits) != 0 {
		t.Fatalf("replies = %+v, %+v, posts = %d, edits = %d; want a post per thread", first, other, len(p.all()), len(p.edits))
	}
}

// The alert: a keyed text request with no thread starts one, and the triage
// that follows, keyed the same on that thread, edits it, laid out.
func TestNotifyHomeKeyedTextBindsToTheThreadItStarts(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	alert := serveJSON(t, n, lib.NotifyRequest{Text: "🚨 seeded-b: CrashLoopBackOff", Update: "alert-s1"})
	if alert.Error != "" || alert.ThreadID != testHomeThread {
		t.Fatalf("alert = %+v, want it to start the thread", alert)
	}
	triage := serveJSON(t, n, homeKeyed("triage", "alert-s1", alert.ThreadID))
	if triage.Error != "" || triage.MessageID != alert.MessageID {
		t.Fatalf("triage = %+v, want it to edit the alert %q", triage, alert.MessageID)
	}
	if posts := p.all(); len(posts) != 1 || len(p.edits) != 1 || p.edits[0].thread != alert.MessageID || len(p.chats) != 1 || p.chats[0] == nil {
		t.Fatalf("posts = %+v, edits = %+v, chats = %d; want the text alert then its laid-out edit", posts, p.edits, len(p.chats))
	}
}

func TestNotifyHomeUpdateKeyPostsAnewWhenTheEditFails(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	first := serveJSON(t, n, homeKeyed("a", "plan-c1-1", testHomeThread))
	p.failEdits = true
	p.editCalls = 0
	second := serveJSON(t, n, homeKeyed("b", "plan-c1-1", testHomeThread))
	if second.Error != "" || second.MessageID == first.MessageID || len(p.all()) != 2 {
		t.Fatalf("replies %+v, %+v, posts %d; want a second post", first, second, len(p.all()))
	}
	if p.editCalls != 1 {
		t.Fatalf("edit calls = %d; want one, since a gone message is not edited again as text", p.editCalls)
	}
	p.failEdits = false
	serveJSON(t, n, homeKeyed("c", "plan-c1-1", testHomeThread))
	if len(p.edits) != 1 || p.edits[0].thread != second.MessageID {
		t.Fatalf("edits = %+v, want the key on the second post", p.edits)
	}
}

func TestNotifyHomeKeyedRequestOnABackendWithoutEditsPostsAnew(t *testing.T) {
	p := &fakeHomeLayoutOnly{&fakeNotifyPoster{landsIn: testHomeThread}}
	n, err := NewGchatNotifier(p, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	first := serveJSON(t, n, homeKeyed("a", "plan-c1-1", testHomeThread))
	second := serveJSON(t, n, homeKeyed("b", "plan-c1-1", testHomeThread))
	if first.Error != "" || second.Error != "" || second.MessageID == first.MessageID || len(p.all()) != 2 {
		t.Fatalf("replies %+v, %+v, posts %d; want two posts", first, second, len(p.all()))
	}
}

// A keyed chat request whose layout the backend refuses posts its text,
// once and keyed, when the text fits one message, so the next request edits
// it as text.
func TestNotifyHomeKeyedChatWhoseLayoutIsRefusedIsOneTextMessage(t *testing.T) {
	p := &fakeHomePoster{fakeNotifyPoster: &fakeNotifyPoster{landsIn: testHomeThread}, refuseLayout: true}
	n, err := NewGchatNotifier(p, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	first := serveJSON(t, n, homeKeyed("triage, as text", "alert-s1", testHomeThread))
	second := serveJSON(t, n, homeKeyed("settled", "alert-s1", testHomeThread))
	if first.Error != "" || second.Error != "" || second.MessageID != first.MessageID {
		t.Fatalf("replies %+v, %+v; want the text post edited", first, second)
	}
	if posts := p.all(); len(posts) != 1 || posts[0].text != "triage, as text" || len(p.edits) != 1 || p.edits[0].text != "settled" {
		t.Fatalf("posts = %+v, edits = %+v; want one text post then a text edit", posts, p.edits)
	}
}

// A keyed request whose text does not fit one message, and whose layout
// does not land, is posted in chunks and unkeyed, as before the route read
// keys: the alert its key names is left as it was, and the next keyed
// request still edits the alert.
func TestNotifyHomeKeyedTextThatOverflowsOneMessagePostsInChunksUnkeyed(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	alert := serveJSON(t, n, lib.NotifyRequest{Text: "🚨 alert", Update: "alert-s1"})
	p.refuseLayout = true
	long := strings.Repeat("report line\n", 400)
	triage := serveJSON(t, n, homeKeyed(long, "alert-s1", alert.ThreadID))
	if triage.Error != "" || triage.MessageID == alert.MessageID || len(p.edits) != 0 {
		t.Fatalf("triage = %+v (alert %q), edits = %+v; want chunks under the alert and no edit", triage, alert.MessageID, p.edits)
	}
	chunks := len(p.all()) - 1
	var joined strings.Builder
	for _, post := range p.all()[1:] {
		joined.WriteString(post.text)
	}
	if chunks < 2 || strings.Count(joined.String(), "report line") != 400 {
		t.Fatalf("posts = %d; want the whole text in chunks, got %d lines", len(p.all()), strings.Count(joined.String(), "report line"))
	}
	p.refuseLayout = false
	settled := serveJSON(t, n, homeKeyed("settled", "alert-s1", alert.ThreadID))
	if settled.MessageID != alert.MessageID || len(p.edits) != 1 || len(p.all()) != 1+chunks {
		t.Fatalf("settled = %+v, edits = %+v, posts = %d; want the alert edited and nothing new posted", settled, p.edits, len(p.all()))
	}
	// Text alone that overflows goes the same way, keyed or not.
	overflow := serveJSON(t, n, lib.NotifyRequest{Text: long, Update: "alert-s1", Thread: alert.ThreadID})
	if overflow.Error != "" || overflow.MessageID == alert.MessageID || len(p.all()) != 1+2*chunks {
		t.Fatalf("overflow = %+v, posts = %d; want chunks again", overflow, len(p.all()))
	}
}

// A backend that lays nothing out and edits nothing posts a keyed chat
// request's text, and the next request with the key posts anew.
func TestNotifyHomeKeyedChatOnABackendWithoutLayoutPostsItsText(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHomeThread}
	n := newTestNotifier(t, p)
	first := serveJSON(t, n, homeKeyed("triage, as text", "alert-s1", testHomeThread))
	second := serveJSON(t, n, homeKeyed("settled", "alert-s1", testHomeThread))
	if first.Error != "" || second.Error != "" || second.MessageID == first.MessageID {
		t.Fatalf("replies %+v, %+v; want two posts", first, second)
	}
	if posts := p.all(); len(posts) != 2 || posts[0].text != "triage, as text" || posts[1].text != "settled" {
		t.Fatalf("posts = %+v; want the texts", posts)
	}
}

func TestNotifyRefusesAnOverlongHomeUpdateKey(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	got := serveJSON(t, n, homeKeyed("a", strings.Repeat("k", notifyUpdateKeyMax+1), testHomeThread))
	if got.Error == "" || len(p.all()) != 0 {
		t.Fatalf("reply = %+v, posts = %d; want a refusal and no post", got, len(p.all()))
	}
}

// An unkeyed home chat message posts as before: one laid-out message, no key held.
func TestNotifyUnkeyedHomeChatMessagePostsOnce(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	first := serveJSON(t, n, homeKeyed("report", "", testHomeThread))
	second := serveJSON(t, n, homeKeyed("report", "", testHomeThread))
	if first.Error != "" || second.Error != "" || second.MessageID == first.MessageID || len(p.all()) != 2 || len(p.edits) != 0 {
		t.Fatalf("replies %+v, %+v, posts %d, edits %d; want two posts", first, second, len(p.all()), len(p.edits))
	}
}

// A keyed edit of a gone message posts anew, laid out and keyed, whether or
// not the text fits one message: the message, not the layout, is what
// failed.
func TestNotifyHomeKeyedEditOfAGoneMessageWithOverflowingTextPostsAnewLaidOut(t *testing.T) {
	n, p := newHomeKeyedNotifier(t)
	first := serveJSON(t, n, homeKeyed("plan", "plan-c1-1", testHomeThread))
	p.failEdits = true
	long := strings.Repeat("report line\n", 400)
	second := serveJSON(t, n, homeKeyed(long, "plan-c1-1", testHomeThread))
	if second.Error != "" || second.MessageID == first.MessageID || len(p.all()) != 2 || len(p.chats) != 2 || p.chats[1] == nil {
		t.Fatalf("replies %+v, %+v, posts %d, chats %d; want one laid-out post anew", first, second, len(p.all()), len(p.chats))
	}
	p.failEdits = false
	third := serveJSON(t, n, homeKeyed("settled", "plan-c1-1", testHomeThread))
	if third.MessageID != second.MessageID || len(p.edits) != 1 || p.edits[0].thread != second.MessageID {
		t.Fatalf("third = %+v, edits = %+v; want the key on the second post", third, p.edits)
	}
}
