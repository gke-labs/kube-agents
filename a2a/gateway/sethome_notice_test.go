package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestSetHomeNoticeAnswersATypedSethomeOnSlack: a typed /sethome or
// /hermes sethome from an admitted member is answered by the Slack adapter
// in the thread it was typed in, before the text becomes a turn: no task
// starts, a running task is neither steered nor stopped.
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
	d.adapter.events <- slackMsg("im", "D1", "U1", "/hermes sethome", "100.2", "")
	d.awaitPost(t, "the notice naming the home", "D1", "100.2", "Home channel now: `C0HOME1`")
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

// TestSetHomeNoticeLearnsNoHomeAndAdmitsByTheList: a first top-level
// "@bot /sethome" in a channel on an armed learner with no home learns
// nothing and gets the notice; a member the list does not admit gets no
// notice and starts no task.
func TestSetHomeNoticeLearnsNoHomeAndAdmitsByTheList(t *testing.T) {
	d := startSlackDMRig(t)
	dir := t.TempDir()
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: homeTestRecord, Namespace: homeTestNamespace}}
	cs := k8sfake.NewSimpleClientset(cm)
	d.adapter.SetHomeLearner(cs, homeTestNamespace, homeTestRecord, homeTestImage, dir)
	writeHomeFile(t, dir, "")
	d.adapter.events <- slackMsg("channel", "C0NEW2", "U1", "<@UBOT> /sethome", "200.1", "")
	d.awaitPost(t, "the no-home notice in the channel thread", "C0NEW2", "200.1", slackHomeLineNoHome)
	if data, _ := cs.CoreV1().ConfigMaps(homeTestNamespace).Get(context.Background(), homeTestRecord, metav1.GetOptions{}); len(data.Data) != 0 {
		t.Fatalf("the typed /sethome learned a home: %v", data.Data)
	}
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none", n)
	}
	// A member the list does not admit: the text goes on as a turn, which
	// the gateway's own admission refuses; no notice, no task.
	before := len(d.posts())
	d.adapter.events <- slackMsg("im", "D2", "U2", "/sethome", "201.1", "")
	waitFor(t, "the gateway to refuse the turn", func() bool { return len(d.posts()) > before })
	for _, p := range d.posts()[before:] {
		if p.channel == "D2" && (p.text == toMrkdwn(slackHomeLineNotArmed+" "+slackHomeNoticeNoSet) || p.text == toMrkdwn(slackHomeLineNoHome+" "+slackHomeNoticeMove)) {
			t.Fatalf("a member the list does not admit got the notice: %q", p.text)
		}
	}
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none", n)
	}
}

// TestSetHomeNoticeLeavesAnOpenQuestionOpen: a typed /sethome in a thread
// whose task has a question open is answered in the thread and is no
// answer to the question: it stays open and its card is not rewritten.
func TestSetHomeNoticeLeavesAnOpenQuestionOpen(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	a.SetHome(func(string) (bool, bool) { return true, false }, func(context.Context) (*lib.Board, error) { return &lib.Board{}, nil })
	conv := "slack:C1/40.0"
	a.noteQuestion(conv, slackQuestion{channel: "C1", ts: "50.0", text: "Which one?", blocks: json.RawMessage(uxQuestionBlocks)})
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> /sethome", "55.0", "40.0")); ok {
		t.Fatal("a typed /sethome in the question's thread became a turn")
	}
	if n := len(api.posted); n != 1 || api.posted[0].thread != "40.0" || !strings.Contains(api.posted[0].text, slackHomeNoticeNoSet) {
		t.Fatalf("posts = %+v, want the notice in the thread", api.posted)
	}
	a.mu.Lock()
	_, open := a.uxLocked().questions[conv]
	a.mu.Unlock()
	if !open || len(api.updated) != 0 {
		t.Fatalf("question open %v, card rewrites %d; want the question untouched", open, len(api.updated))
	}
	// A plain reply in the same thread is the turn a /sethome is not.
	if _, ok := a.inbound(context.Background(), slackMsg("channel", "C1", "U1", "<@UBOT> B", "56.0", "40.0")); !ok {
		t.Fatal("a plain mentioned reply in the thread was not a turn")
	}
}

// TestSetHomeNoticeNeedsNoUserLookup: under allow-all, the documented
// default, a users.info that fails changes nothing: admission is the
// gateway's own rule on the author, so the notice posts and no task starts.
func TestSetHomeNoticeNeedsNoUserLookup(t *testing.T) {
	d := startSlackDMRigWith(t, nil, func(c *Config) {
		c.SlackAllowedUsers = nil
		c.SlackAllowAllUsers = true
	})
	d.adapter.events <- slackMsg("im", "D1", "U1", "/sethome", "100.1", "")
	d.awaitPost(t, "the notice with users.info down", "D1", "100.1", slackHomeNoticeNoSet)
	if n := len(d.tasks(t)); n != 0 {
		t.Fatalf("%d tasks, want none: a failed users.info let the typed /sethome through as a turn", n)
	}
}

// TestSetHomeNoticeWithoutAnAdmissionRuleIsATurn: an adapter the gateway has
// handed no admission rule answers nothing; the typed command is the turn
// it was, for the gateway to admit or refuse.
func TestSetHomeNoticeWithoutAnAdmissionRuleIsATurn(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	if _, ok := a.inbound(context.Background(), slackMsg("im", "D1", "U1", "/sethome", "100.1", "")); !ok {
		t.Fatal("with no admission rule the typed /sethome was not a turn")
	}
	if len(api.posted) != 0 {
		t.Fatalf("posts = %+v, want none", api.posted)
	}
}
