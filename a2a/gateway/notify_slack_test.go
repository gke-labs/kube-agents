package gateway

import (
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The chat.notify route on Slack: the same Notifier as Chat's, built with
// Slack's home check and thread rule, and SlackAdapter.PostNotify as the
// poster.

const testSlackHome = "C0HOME"

func newTestSlackNotifier(t *testing.T, p notifyPoster) *Notifier {
	t.Helper()
	n, err := NewSlackNotifier(p, testSlackHome, nil)
	if err != nil {
		t.Fatalf("NewSlackNotifier: %v", err)
	}
	return n
}

func TestSlackNotifyRefusesAHomeThatIsNotAChannel(t *testing.T) {
	for _, home := range []string{"", "D0DM", "general", "#ops", "c0lower", "C", "spaces/AAA", "C0HOME/1.2"} {
		if _, err := NewSlackNotifier(&fakeNotifyPoster{}, home, nil); err == nil {
			t.Errorf("home %q was accepted", home)
		}
	}
	for _, home := range []string{"C0HOME", "G0PRIVATE"} {
		if _, err := NewSlackNotifier(&fakeNotifyPoster{}, home, nil); err != nil {
			t.Errorf("home %q was refused: %v", home, err)
		}
	}
}

func TestSlackNotifyUsesItsOwnSubject(t *testing.T) {
	n := newTestSlackNotifier(t, &fakeNotifyPoster{})
	if n.subject != lib.NotifySubjectSlack {
		t.Errorf("subject = %q, want %q", n.subject, lib.NotifySubjectSlack)
	}
	if lib.NotifySubjects[lib.NotifyPlatformSlack] != lib.NotifySubjectSlack {
		t.Errorf("the CLI cannot find Slack's subject: %v", lib.NotifySubjects)
	}
}

// TestSlackNotifyPostsOnlyIntoTheHomeChannel: a new thread and a reply both
// land in the home channel, whatever the request names, because a Slack
// thread ts names no channel. The answer carries the thread root's ts.
func TestSlackNotifyPostsOnlyIntoTheHomeChannel(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: "1700000000.000100"}
	n := newTestSlackNotifier(t, p)
	first := serveJSON(t, n, lib.NotifyRequest{Text: "drift on prod-eu"})
	if first.Error != "" || first.ThreadID != "1700000000.000100" {
		t.Fatalf("new thread: %+v", first)
	}
	reply := serveJSON(t, n, lib.NotifyRequest{Text: "and on prod-us", Thread: first.ThreadID})
	if reply.Error != "" || reply.ThreadID != first.ThreadID {
		t.Fatalf("reply: %+v", reply)
	}
	for _, post := range p.all() {
		if post.space != testSlackHome {
			t.Errorf("posted into %q, want the home channel %q", post.space, testSlackHome)
		}
	}
}

func TestSlackNotifyRefusesAThreadThatIsNotATS(t *testing.T) {
	p := &fakeNotifyPoster{}
	n := newTestSlackNotifier(t, p)
	for _, thread := range []string{"C0OTHER/1.2", "spaces/AAA/threads/B", "1700000000", ".5", "1.", "abc.def", "1.2.3", " 1.2"} {
		got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Thread: thread})
		if got.Error == "" {
			t.Errorf("thread %q was accepted", thread)
		}
	}
	if len(p.all()) != 0 {
		t.Errorf("a refused request posted: %v", p.all())
	}
}

// TestSlackAdapterPostNotify: the adapter posts into the channel it is
// given, threads on a ts when given one, and reports the new message's ts as
// the thread when it started one.
func TestSlackAdapterPostNotify(t *testing.T) {
	api := &fakeSlackAPI{}
	a := newTestSlackAdapter(api)
	msg, thread, err := a.PostNotify(testSlackHome, "", "**alert**: drift")
	if err != nil || msg != "999.001" || thread != "999.001" {
		t.Fatalf("top-level: msg=%q thread=%q err=%v, want the new ts as both", msg, thread, err)
	}
	msg, thread, err = a.PostNotify(testSlackHome, "1700000000.000100", "follow-up")
	if err != nil || thread != "1700000000.000100" {
		t.Fatalf("reply: msg=%q thread=%q err=%v", msg, thread, err)
	}
	if len(api.posted) != 2 || api.posted[0].channel != testSlackHome || api.posted[0].thread != "" ||
		api.posted[1].thread != "1700000000.000100" {
		t.Fatalf("posted = %+v", api.posted)
	}
	if !strings.Contains(api.posted[0].text, "*alert*") {
		t.Errorf("text %q was not translated to mrkdwn", api.posted[0].text)
	}
	if _, _, err := a.PostNotify("D0DM", "", "x"); err == nil {
		t.Error("PostNotify accepted a DM channel")
	}
}

var _ notifyPoster = (*SlackAdapter)(nil)
