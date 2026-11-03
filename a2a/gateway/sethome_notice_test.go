package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// HomeNotice makes the generic fake a HomeNoticer, so a test can show the
// gateway does not answer a typed /sethome on a backend that is not Slack.
func (a *fakeAdapter) HomeNotice(string) string { return a.homeNotice }

// plainAdapter hides every optional extension of the adapter it wraps: the
// gateway sees an Adapter and nothing more.
type plainAdapter struct{ Adapter }

// TestSetHomeNoticeAnswersATypedSethomeOnSlack: a typed /sethome or
// /hermes sethome on Slack is answered in place by the adapter's notice,
// whatever the task state, and starts no task and steers none.
func TestSetHomeNoticeAnswersATypedSethomeOnSlack(t *testing.T) {
	d := startSlackDMRig(t)
	// Not armed: the field decides.
	d.adapter.events <- slackMsg("im", "D1", "U1", "/sethome", "100.1", "")
	d.awaitPost(t, "the not-armed notice", "D1", "100.1", slackHomeLineNotArmed+" "+slackHomeNoticeNoSet)
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none: a typed /sethome became a turn", n)
	}
	// Armed with a learned home: the notice names it.
	dir := t.TempDir()
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: homeTestRecord, Namespace: homeTestNamespace}, Data: map[string]string{homeRecordKeySlack: "C0HOME1"}}
	d.adapter.SetHomeLearner(k8sfake.NewSimpleClientset(cm), homeTestNamespace, homeTestRecord, homeTestImage, dir)
	writeHomeFile(t, dir, "C0HOME1\n")
	d.adapter.SetHome(func(string) (bool, bool) { return true, false }, func(context.Context) (*lib.Board, error) { return &lib.Board{}, nil })
	d.adapter.events <- slackMsg("im", "D1", "U1", "/hermes sethome", "100.2", "")
	d.awaitPost(t, "the notice naming the home", "D1", "100.2", fmt.Sprintf(slackHomeNoticeHome, "C0HOME1")+" "+slackHomeNoticeMove)
	// In a thread with a running task: the notice posts there and the task
	// is neither steered nor stopped.
	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the fleet", "101.1", "")
	first := d.awaitTasks(t, 1)[0]
	d.adapter.events <- slackMsg("im", "D1", "U1", "/sethome", "101.5", "101.1")
	d.awaitPost(t, "the notice in the busy thread", "D1", "101.1", slackHomeNoticeMove)
	if envs := inSubjectEnvelopes(t, d.r.url, "platform"); len(envs) != 1 || envs[0].TaskID != first.TaskID {
		t.Fatalf("%d envelopes on platform, want the turn alone: the typed /sethome steered or stopped it", len(envs))
	}
}

// TestSetHomeNoticeStaysOffOtherBackends: on a backend that is not Slack a
// typed /sethome is the turn it was, HomeNoticer or not.
func TestSetHomeNoticeStaysOffOtherBackends(t *testing.T) {
	r := startRig(t)
	r.adapter.homeNotice = "a notice the gateway must not post here"
	conv := "discord:g1/sethome-3"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "sh-3", Text: "/sethome", TopLevel: true}
	waitFor(t, "the turn", func() bool { return messagesOn(t, r, "platform") == 1 })
	if shown(r, r.adapter.homeNotice)() {
		t.Fatal("the notice posted on a backend that is not Slack")
	}
}

// TestSetHomeNoticeFallsThroughWithoutTheExtension: on Slack, an adapter
// without HomeNotice passes a typed /sethome on as the turn it was; nothing
// is dropped.
func TestSetHomeNoticeFallsThroughWithoutTheExtension(t *testing.T) {
	d := startSlackDMRigWithAdapter(t, nil, nil, func(a *slackEventAdapter, _ string) Adapter { return plainAdapter{a} })
	d.adapter.events <- slackMsg("im", "D1", "U1", "/sethome", "100.1", "")
	d.awaitTasks(t, 1)
	for _, p := range d.posts() {
		if strings.Contains(p.text, slackHomeNoticeNoSet) || strings.Contains(p.text, slackHomeNoticeMove) {
			t.Fatalf("a notice posted with no HomeNoticer on the adapter: %q", p.text)
		}
	}
}

// TestSetHomeNoticeReachesThroughTheProductionStack: the deployed gateway
// drives the Slack adapter through a MultiAdapter beside the console (cmd/
// gateway/main.go), so the extension is read through the mux for the
// conversation's backend; a typed /sethome still gets the notice and no task.
func TestSetHomeNoticeReachesThroughTheProductionStack(t *testing.T) {
	d := startSlackDMRigWithAdapter(t, nil, nil, func(a *slackEventAdapter, url string) Adapter {
		console, err := NewConsoleAdapter(url, nil, slog.Default())
		if err != nil {
			t.Fatalf("console adapter: %v", err)
		}
		t.Cleanup(console.Close)
		m, err := NewMultiAdapter(slackBackend, "console", map[string]Adapter{slackBackend: a, "console": console}, nil)
		if err != nil {
			t.Fatal(err)
		}
		return m
	})
	d.adapter.events <- slackMsg("im", "D1", "U1", "/sethome", "100.1", "")
	d.awaitPost(t, "the notice through the mux", "D1", "100.1", slackHomeLineNotArmed+" "+slackHomeNoticeNoSet)
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none: the typed /sethome became a turn through the production stack", n)
	}
}

// TestSetHomeNoticeReachesThroughTheSideDoorStack: with a door armed the
// deployed stack is the side-door wrapper over the mux; the extension is
// still asked for the Slack conversation and the notice posts.
func TestSetHomeNoticeReachesThroughTheSideDoorStack(t *testing.T) {
	d := startSlackDMRigWithAdapter(t, nil, nil, func(a *slackEventAdapter, url string) Adapter {
		console, err := NewConsoleAdapter(url, nil, slog.Default())
		if err != nil {
			t.Fatalf("console adapter: %v", err)
		}
		t.Cleanup(console.Close)
		m, err := NewMultiAdapter(slackBackend, "console", map[string]Adapter{slackBackend: a, "console": console}, nil)
		if err != nil {
			t.Fatal(err)
		}
		door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
		if err != nil {
			t.Fatal(err)
		}
		return WithSideDoors(m, []DoorSpec{InjectDoorSpec(door)}, nil)
	})
	d.adapter.events <- slackMsg("im", "D1", "U1", "/hermes sethome", "100.1", "")
	d.awaitPost(t, "the notice through the side door and the mux", "D1", "100.1", slackHomeLineNotArmed+" "+slackHomeNoticeNoSet)
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none: the typed /hermes sethome became a turn through the side-door stack", n)
	}
}
