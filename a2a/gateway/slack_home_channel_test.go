package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/slack-go/slack"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// The learned home channel (slack_home_channel.go): a fake Slack API answers
// conversations.info, a fake clientset holds the record ConfigMap, and a fake
// setter stands in for the Notifier. goSession runs inline (syncSessions), so
// every learn and the reconciler's first pass have finished when the call
// returns; the reconcile loop waits on a timer the test owns (homeClock), so
// a later pass runs only when the test ticks it.

const (
	homeTestNamespace = "kubeagents-system"
	homeTestRecord    = "platform-agent-home-channel"
	homeTestImage     = "ghcr.io/gke-labs/kube-agents/a2a-gateway:new"
	homeTestOldImage  = "ghcr.io/gke-labs/kube-agents/a2a-gateway:old"
)

var configMapsGVR = schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}

type fakeHomeSetter struct {
	mu    sync.Mutex
	homes []string
}

func (f *fakeHomeSetter) SetHome(home string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.homes = append(f.homes, home)
	return nil
}

func (f *fakeHomeSetter) all() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.homes...)
}

// newHomeLearnAdapter is a Slack adapter with the learner wired over a record
// holding data (nil for an empty record).
func newHomeLearnAdapter(t *testing.T, data map[string]string) (*SlackAdapter, *fakeSlackAPI, *k8sfake.Clientset, *fakeHomeSetter) {
	t.Helper()
	api := &fakeSlackAPI{team: "T1", conversations: map[string]*slack.Channel{}}
	a, _ := newUXSlackAdapter(api)
	cs := k8sfake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: homeTestRecord, Namespace: homeTestNamespace, ResourceVersion: "1"},
		Data:       data,
	})
	setter := &fakeHomeSetter{}
	// A timer that never fires: the loop parks until the test ticks it
	// through a homeClock, or until Cleanup closes the learner.
	a.homeLearn.after = func(time.Duration) <-chan time.Time { return nil }
	a.SetHomeLearner(setter, cs, homeTestNamespace, homeTestRecord, homeTestImage)
	t.Cleanup(func() { closeHomeLearnBounded(t, a) })
	return a, api, cs, setter
}

// homeClock is the reconcile loop's timer under the test's control: after
// records every wait the loop asked for and answers the shared tick channel,
// so a send on tick releases exactly one pass.
type homeClock struct {
	mu    sync.Mutex
	waits []time.Duration
	tick  chan time.Time
}

func newHomeClock(a *SlackAdapter) *homeClock {
	c := &homeClock{tick: make(chan time.Time)}
	a.homeLearn.after = c.after
	return c
}

func (c *homeClock) after(d time.Duration) <-chan time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.waits = append(c.waits, d)
	return c.tick
}

func (c *homeClock) all() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.waits...)
}

// parked returns once the loop waits on the clock (it starts on its own
// goroutine after the first pass), with the number of waits asked so far.
func (c *homeClock) parked(t *testing.T) int {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if n := len(c.all()); n > 0 {
			return n
		}
		if time.Now().After(deadline) {
			t.Fatal("the reconcile loop never waited on the clock")
		}
		time.Sleep(time.Millisecond)
	}
}

// pass releases the loop's next pass and returns once it has finished, which
// is when the loop asks for its next wait.
func (c *homeClock) pass(t *testing.T) {
	t.Helper()
	n := c.parked(t)
	select {
	case c.tick <- time.Time{}:
	case <-time.After(5 * time.Second):
		t.Fatal("the reconcile loop did not take the tick")
	}
	deadline := time.Now().Add(5 * time.Second)
	for len(c.all()) <= n {
		if time.Now().After(deadline) {
			t.Fatal("the pass did not finish")
		}
		time.Sleep(time.Millisecond)
	}
}

// homeGets counts the record reads.
func homeGets(cs *k8sfake.Clientset) int {
	n := 0
	for _, act := range cs.Actions() {
		if act.GetVerb() == "get" && act.GetResource() == configMapsGVR {
			n++
		}
	}
	return n
}

// closeHomeLearnBounded is closeHomeLearn with a deadline, so a loop that
// does not stop fails the test instead of hanging the package.
func closeHomeLearnBounded(t *testing.T, a *SlackAdapter) {
	t.Helper()
	closed := make(chan struct{})
	go func() { a.closeHomeLearn(); close(closed) }()
	select {
	case <-closed:
	case <-time.After(10 * time.Second):
		t.Fatal("closeHomeLearn did not return: the reconcile loop did not stop")
	}
}

// replaceRecord rewrites the record's data by hand, as kubectl edit would.
func replaceRecord(t *testing.T, cs *k8sfake.Clientset, data map[string]string) {
	t.Helper()
	obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
	if err != nil {
		t.Fatal(err)
	}
	cm := obj.(*corev1.ConfigMap)
	cm.Data = data
	if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
		t.Fatal(err)
	}
}

// homeChannelInfo is a conversations.info answer: a channel the bot is in,
// changed by mut.
func homeChannelInfo(id string, mut func(*slack.Channel)) *slack.Channel {
	ch := &slack.Channel{IsMember: true}
	ch.ID = id
	if mut != nil {
		mut(ch)
	}
	return ch
}

func homeRecordData(t *testing.T, cs *k8sfake.Clientset) map[string]string {
	t.Helper()
	cm, err := cs.CoreV1().ConfigMaps(homeTestNamespace).Get(context.Background(), homeTestRecord, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return cm.Data
}

func homePatches(cs *k8sfake.Clientset) int {
	n := 0
	for _, act := range cs.Actions() {
		if act.GetVerb() == "patch" && act.GetResource() == configMapsGVR {
			n++
		}
	}
	return n
}

// homeMention drives one typed message through the inbound path and the
// gateway's verdict, as Run and handleInbound do.
func homeMention(t *testing.T, a *SlackAdapter, channelType, channel, user, text, ts, threadTS string, verified bool) {
	t.Helper()
	ev := slackMsg(channelType, channel, user, text, ts, threadTS)
	msg, ok := a.inbound(context.Background(), ev)
	if !ok {
		t.Fatalf("message %q in %s was not a turn", text, channel)
	}
	a.notePending(msg)
	a.noteHomeCandidate(msg, ev)
	a.InboundVerified(msg.Conversation, msg.MessageID, verified)
}

func TestSlackHomeLearnsFromTheFirstChannelMention(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	if !a.homeLearn.armed {
		t.Fatal("an empty record did not arm the learner")
	}
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> what is running?", "10.0", "", true)

	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0AA1" || data[homeRecordKeySetBy] != "U1" ||
		data[homeRecordKeyWriter] != homeTestImage || data[homeRecordKeyReader] != homeTestImage {
		t.Fatalf("record = %v", data)
	}
	if _, err := time.Parse(time.RFC3339, data[homeRecordKeySetAt]); err != nil {
		t.Errorf("setAt %q: %v", data[homeRecordKeySetAt], err)
	}
	if got := setter.all(); len(got) != 1 || got[0] != "C0AA1" {
		t.Errorf("SetHome calls = %v, want [C1]", got)
	}
	if len(api.posted) != 1 || api.posted[0].channel != "C0AA1" || api.posted[0].thread != "10.0" {
		t.Fatalf("posts = %+v, want one line in the learning thread", api.posted)
	}
	if line := api.posted[0].text; !strings.Contains(line, "<#C0AA1>") || strings.Contains(line, "U1") {
		t.Errorf("line = %q: must name the channel and never the member", line)
	}
	if api.conversationInfoCalls != 1 {
		t.Errorf("conversations.info calls = %d, want 1", api.conversationInfoCalls)
	}

	// A second mention, in another channel: learned once, written once.
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U2", "<@UBOT> and now?", "11.0", "", true)
	if homePatches(cs) != 1 || len(setter.all()) != 1 || len(api.posted) != 1 {
		t.Errorf("after a second mention: patches %d, SetHome %v, posts %d; want 1, [C1], 1",
			homePatches(cs), setter.all(), len(api.posted))
	}
}

func TestSlackHomeIsNeverSetByTheseTurns(t *testing.T) {
	cases := []struct {
		name string
		run  func(t *testing.T, a *SlackAdapter)
	}{
		{"a direct message", func(t *testing.T, a *SlackAdapter) {
			homeMention(t, a, slackChannelTypeIM, "D0DM1", "U1", "hello", "20.0", "", true)
		}},
		{"a thread reply", func(t *testing.T, a *SlackAdapter) {
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> in a thread", "21.0", "19.0", true)
		}},
		{"an unaddressed reply under the bot's post", func(t *testing.T, a *SlackAdapter) {
			ev := slackMsg(slackChannelTypeChannel, "C0AA1", "U1", "lunch?", "22.0", "19.0")
			msg, ok := a.inboundEvent(context.Background(), ev, "UBOT")
			if !ok || !msg.Unaddressed {
				t.Fatalf("bot-rooted reply = (%+v, %v)", msg, ok)
			}
			a.notePending(msg)
			a.noteHomeCandidate(msg, ev)
			a.InboundVerified(msg.Conversation, msg.MessageID, true)
		}},
		{"a command", func(t *testing.T, a *SlackAdapter) {
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> stop", "23.0", "", true)
		}},
		{"an unverified sender", func(t *testing.T, a *SlackAdapter) {
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U9", "<@UBOT> hi", "24.0", "", false)
		}},
		{"a click", func(t *testing.T, a *SlackAdapter) {
			conv := slackConversationID(slackChannelTypeChannel, "C0AA1", "25.0")
			a.mu.Lock()
			a.addPendingLocked(slackPendingKey(conv, "25.1"), slackPending{conversation: conv, author: "U1",
				text: "Option A", click: true, homeType: slackChannelTypeChannel})
			a.mu.Unlock()
			a.InboundVerified(conv, "25.1", true)
		}},
		{"a shortcut", func(t *testing.T, a *SlackAdapter) {
			conv := slackConversationID(slackChannelTypeChannel, "C0AA1", "26.0")
			a.mu.Lock()
			a.addPendingLocked(slackPendingKey(conv, "26.0"), slackPending{conversation: conv, author: "U1",
				text: "is payments down?", shortcut: true, clicker: "jay", homeType: slackChannelTypeChannel})
			a.mu.Unlock()
			a.InboundVerified(conv, "26.0", true)
		}},
		{"a mention before the boot read armed the learner", func(t *testing.T, a *SlackAdapter) {
			a.homeLearn.armed = false
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> early", "27.0", "", true)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, nil)
			a.RunHomeReconciler(context.Background())
			tc.run(t, a)
			if homePatches(cs) != 0 || len(setter.all()) != 0 || api.conversationInfoCalls != 0 {
				t.Errorf("patches %d, SetHome %v, conversations.info %d; want none",
					homePatches(cs), setter.all(), api.conversationInfoCalls)
			}
		})
	}
}

func TestSlackHomeRefusesAChannelItCannotPostIn(t *testing.T) {
	cases := []struct {
		name   string
		info   *slack.Channel
		err    error
		reason string
	}{
		{"shared", homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsExtShared = true }), nil, homeRefusedShared},
		{"pending shared", homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsPendingExtShared = true }), nil, homeRefusedShared},
		{"archived", homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true }), nil, homeRefusedArchived},
		{"not a member", homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsMember = false }), nil, homeRefusedNotMember},
		{"not found", nil, slack.SlackErrorResponse{Err: slackChannelNotFound}, homeRefusedNotMember},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, nil)
			if tc.info != nil {
				api.conversations["C0AA1"] = tc.info
			}
			api.conversationErr = tc.err
			a.RunHomeReconciler(context.Background())
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "30.0", "", true)
			data := homeRecordData(t, cs)
			if data[homeRecordKeyRefused] != tc.reason || data[homeRecordKeySlack] != "" || len(setter.all()) != 0 || len(api.posted) != 0 {
				t.Fatalf("record %v, SetHome %v, posts %d; want refused=%s and nothing else",
					data, setter.all(), len(api.posted), tc.reason)
			}
			// The same refusal again is a log line, not a write.
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U2", "<@UBOT> again", "31.0", "", true)
			if homePatches(cs) != 1 {
				t.Errorf("patches after a second refusal = %d, want 1", homePatches(cs))
			}
			// A channel the bot can post in still learns, and clears the refusal.
			api.conversationErr = nil
			homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here then", "32.0", "", true)
			data = homeRecordData(t, cs)
			if data[homeRecordKeySlack] != "C0BB2" || data[homeRecordKeyRefused] != "" {
				t.Errorf("record after the learn = %v, want slack=C2 and no refusal", data)
			}
			if _, stale := data[homeRecordKeyRefused]; stale {
				t.Errorf("slack.refused still present: %v", data)
			}
		})
	}
}

func TestSlackHomeGroupDirectMessageIsRefusedWithoutACall(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	homeMention(t, a, slackChannelTypeMpIM, "G0MP1", "U1", "<@UBOT> hi all", "40.0", "", true)
	if api.conversationInfoCalls != 0 || homePatches(cs) != 0 || len(setter.all()) != 0 {
		t.Fatalf("mpim: conversations.info %d, patches %d, SetHome %v; want none",
			api.conversationInfoCalls, homePatches(cs), setter.all())
	}
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here", "41.0", "", true)
	if got := setter.all(); len(got) != 1 || got[0] != "C0BB2" {
		t.Errorf("the next channel mention did not learn: %v", got)
	}
}

func TestSlackHomeCheckThatCannotBeMadeIsRetried(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	api.conversationErr = errors.New("ratelimited")
	a.RunHomeReconciler(context.Background())
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "50.0", "", true)
	if homePatches(cs) != 0 || len(setter.all()) != 0 || !a.homeLearn.armed {
		t.Fatalf("after a failed check: patches %d, SetHome %v, armed %v", homePatches(cs), setter.all(), a.homeLearn.armed)
	}
	api.conversationErr = nil
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi again", "51.0", "", true)
	if got := setter.all(); len(got) != 1 || got[0] != "C0AA1" {
		t.Errorf("the retry did not learn: %v", got)
	}
}

func TestSlackHomeAdoptsAWinnerThatWroteFirst(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	conflicts := 0
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		// Every write carries the read's resourceVersion as its precondition:
		// the fake tracker never checks it, so this reactor does.
		var body struct {
			Metadata struct {
				ResourceVersion string `json:"resourceVersion"`
			} `json:"metadata"`
		}
		if err := json.Unmarshal(action.(k8stesting.PatchAction).GetPatch(), &body); err != nil {
			t.Fatalf("patch body: %v", err)
		}
		if body.Metadata.ResourceVersion != "1" {
			t.Errorf("patch resourceVersion = %q, want the read's \"1\"", body.Metadata.ResourceVersion)
		}
		if conflicts > 0 {
			return false, nil, nil
		}
		conflicts++
		obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
		if err != nil {
			t.Fatal(err)
		}
		cm := obj.(*corev1.ConfigMap).DeepCopy()
		cm.Data = map[string]string{homeRecordKeySlack: "C0WIN9", homeRecordKeyWriter: homeTestOldImage}
		if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
			t.Fatal(err)
		}
		return true, nil, apierrors.NewConflict(configMapsGVR.GroupResource(), homeTestRecord, errors.New("modified"))
	})
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "60.0", "", true)
	if got := setter.all(); len(got) != 1 || got[0] != "C0WIN9" {
		t.Fatalf("SetHome = %v, want the winner C9", got)
	}
	// The winner's channel and writer stand; this image stamps itself as
	// the reader, as a boot adopt would.
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0WIN9" || data[homeRecordKeyWriter] != homeTestOldImage ||
		data[homeRecordKeyReader] != homeTestImage || data[homeRecordKeySetBy] != "" {
		t.Errorf("record = %v, want the winner kept and reader re-stamped", data)
	}
	if len(api.posted) != 1 || !strings.Contains(api.posted[0].text, "<#C0WIN9>") {
		t.Errorf("posts = %+v, want one line naming C9", api.posted)
	}
}

func TestSlackHomeWriteErrorLearnsNothing(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	broken := true
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if broken {
			return true, nil, errors.New("apiserver unavailable")
		}
		return false, nil, nil
	})
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "70.0", "", true)
	if len(setter.all()) != 0 || len(api.posted) != 0 || !a.homeLearn.armed {
		t.Fatalf("after a write error: SetHome %v, posts %d, armed %v; want nothing adopted", setter.all(), len(api.posted), a.homeLearn.armed)
	}
	broken = false
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "71.0", "", true)
	if got := setter.all(); len(got) != 1 || got[0] != "C0AA1" {
		t.Errorf("the retry did not learn: %v", got)
	}
}

// failTimes fails the next n configmaps actions of verb; later ones pass.
func failTimes(cs *k8sfake.Clientset, verb string, n int) {
	cs.PrependReactor(verb, "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if n == 0 {
			return false, nil, nil
		}
		n--
		return true, nil, errors.New("apiserver unavailable")
	})
}

// failOnce fails the next configmaps action of verb once; later ones pass.
func failOnce(cs *k8sfake.Clientset, verb string) { failTimes(cs, verb, 1) }

func TestSlackHomeRefusalThatDidNotLandIsWrittenAtTheNextRefusal(t *testing.T) {
	for _, verb := range []string{"get", "patch"} {
		t.Run("the record "+verb+" fails", func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, nil)
			api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
			a.RunHomeReconciler(context.Background())
			failOnce(cs, verb)
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "100.0", "", true)
			if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != "" {
				t.Fatalf("record after the failed %s = %v, want no refusal yet", verb, data)
			}
			// The same refusal again: the reason reaches the record this time.
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U2", "<@UBOT> again", "101.0", "", true)
			if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != homeRefusedArchived {
				t.Fatalf("record after the retry = %v, want refused=%s", data, homeRefusedArchived)
			}
			// And once it is there, a third refusal is a log line.
			patches := homePatches(cs)
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U3", "<@UBOT> once more", "102.0", "", true)
			if homePatches(cs) != patches || len(setter.all()) != 0 || !a.homeLearn.armed {
				t.Errorf("after a third refusal: patches %d (was %d), SetHome %v, armed %v", homePatches(cs), patches, setter.all(), a.homeLearn.armed)
			}
		})
	}
}

func TestSlackHomeRefusedRecordIsReplacedByTheNextMention(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, map[string]string{
		homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage,
	})
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
	a.RunHomeReconciler(context.Background())
	if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != homeRefusedArchived || data[homeRecordKeySlack] != "C0AA1" || len(setter.all()) != 0 {
		t.Fatalf("after boot: record %v, SetHome %v; want refused=%s on the recorded channel and nothing adopted", data, setter.all(), homeRefusedArchived)
	}
	if !a.homeLearn.armed {
		t.Fatal("a refused recorded channel left the learner unarmed")
	}
	// A mention in the refused channel: a log line, no second write, the
	// record keeps describing the recorded channel.
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "110.0", "", true)
	if homePatches(cs) != 1 || len(setter.all()) != 0 {
		t.Fatalf("after a mention in the refused channel: patches %d, SetHome %v; want 1 and none", homePatches(cs), setter.all())
	}
	// A mention in a channel that passes replaces it, as it would fill an
	// empty record.
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here then", "111.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0BB2" || data[homeRecordKeySetBy] != "U1" ||
		data[homeRecordKeyWriter] != homeTestImage || data[homeRecordKeyReader] != homeTestImage {
		t.Fatalf("record after the replacement = %v", data)
	}
	if _, stale := data[homeRecordKeyRefused]; stale {
		t.Errorf("slack.refused still present: %v", data)
	}
	if got := setter.all(); len(got) != 1 || got[0] != "C0BB2" {
		t.Errorf("SetHome = %v, want [C0BB2]", got)
	}
	if len(api.posted) != 1 || !strings.Contains(api.posted[0].text, "<#C0BB2>") || api.posted[0].thread != "111.0" {
		t.Errorf("posts = %+v, want one line naming C0BB2 in the learning thread", api.posted)
	}
	if a.homeLearn.armed {
		t.Error("the learner stayed armed after the replacement")
	}
	// The next boot adopts the replacement without a write.
	b, bapi, _, bsetter := newHomeLearnAdapter(t, data)
	bapi.conversations["C0BB2"] = homeChannelInfo("C0BB2", nil)
	b.RunHomeReconciler(context.Background())
	if got := bsetter.all(); len(got) != 1 || got[0] != "C0BB2" || b.homeLearn.armed {
		t.Errorf("next boot: SetHome %v, armed %v; want [C0BB2] and not armed", got, b.homeLearn.armed)
	}
}

// TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel: while a
// channel is recorded, slack.refused describes that channel. A candidate
// refused for another reason writes nothing, whether the first pass's own
// refusal write landed or the next pass landed it; the next channel that
// passes replaces the record.
func TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel(t *testing.T) {
	cases := []struct {
		name          string
		bootWriteOK   bool
		patchesBefore int
	}{
		{"the first pass's refusal landed", true, 1},
		{"the first pass's refusal write failed; the next pass landed it", false, 2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, map[string]string{
				homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage,
			})
			api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
			api.conversations["C0BB2"] = homeChannelInfo("C0BB2", func(c *slack.Channel) { c.IsExtShared = true })
			api.conversations["C0CC3"] = homeChannelInfo("C0CC3", nil)
			clock := newHomeClock(a)
			if !tc.bootWriteOK {
				failOnce(cs, "patch")
			}
			a.RunHomeReconciler(context.Background())
			if !tc.bootWriteOK {
				// A failed pass arms nothing; the retry lands the refusal.
				if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != "" || a.homeLearn.armed {
					t.Fatalf("after the failed write: record %v, armed %v; want no refusal and not armed", data, a.homeLearn.armed)
				}
				clock.pass(t)
			}
			if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != homeRefusedArchived || !a.homeLearn.armed {
				t.Fatalf("after the refusal: record %v, armed %v; want refused=archived and armed", data, a.homeLearn.armed)
			}
			// A mention in a shared channel: refused, and the record still
			// describes the recorded channel.
			homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> hi", "120.0", "", true)
			data := homeRecordData(t, cs)
			if data[homeRecordKeyRefused] != homeRefusedArchived || data[homeRecordKeySlack] != "C0AA1" || homePatches(cs) != tc.patchesBefore || len(setter.all()) != 0 {
				t.Fatalf("after a refused candidate: record %v, patches %d, SetHome %v; want refused=archived beside C0AA1, %d patches and none",
					data, homePatches(cs), setter.all(), tc.patchesBefore)
			}
			// A channel that passes replaces the recorded one and clears the
			// refusal, as before.
			homeMention(t, a, slackChannelTypeChannel, "C0CC3", "U1", "<@UBOT> here then", "121.0", "", true)
			data = homeRecordData(t, cs)
			if _, stale := data[homeRecordKeyRefused]; stale || data[homeRecordKeySlack] != "C0CC3" {
				t.Errorf("record after the replacement = %v, want slack=C0CC3 and no refusal", data)
			}
			if got := setter.all(); len(got) != 1 || got[0] != "C0CC3" {
				t.Errorf("SetHome = %v, want [C0CC3]", got)
			}
		})
	}
}

func TestSlackHomeBootAdopt(t *testing.T) {
	valid := map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived}
	cases := []struct {
		name     string
		data     map[string]string
		info     *slack.Channel
		err      error
		wantHome []string
		wantData map[string]string
		patches  int
		armed    bool
	}{
		{"valid record: adopted, reader re-stamped, refusal cleared", valid, nil, nil, []string{"C0AA1"},
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestImage}, 1, false},
		{"record this image already stands behind: no write",
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestImage, homeRecordKeyReader: homeTestImage}, nil, nil,
			[]string{"C0AA1"}, map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestImage, homeRecordKeyReader: homeTestImage}, 0, false},
		{"archived channel: refused, nothing adopted, the learner armed",
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage},
			homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true }), nil, nil,
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived}, 1, true},
		{"malformed id: refused without a call, the learner armed",
			map[string]string{homeRecordKeySlack: "general"}, nil, nil, nil,
			map[string]string{homeRecordKeySlack: "general", homeRecordKeyRefused: homeRefusedMalformed}, 1, true},
		{"check that cannot be made: nothing adopted, nothing armed, the pass is retried",
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyReader: homeTestOldImage}, nil, errors.New("ratelimited"), nil,
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyReader: homeTestOldImage}, 0, false},
		{"empty record: the learner is armed", nil, nil, nil, nil, map[string]string{}, 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, tc.data)
			if tc.info != nil {
				api.conversations[tc.info.ID] = tc.info
			}
			api.conversationErr = tc.err
			a.RunHomeReconciler(context.Background())
			if got := setter.all(); strings.Join(got, ",") != strings.Join(tc.wantHome, ",") {
				t.Errorf("SetHome = %v, want %v", got, tc.wantHome)
			}
			data := homeRecordData(t, cs)
			for k, v := range tc.wantData {
				if data[k] != v {
					t.Errorf("record[%s] = %q, want %q (record %v)", k, data[k], v, data)
				}
			}
			for k := range data {
				if _, want := tc.wantData[k]; !want {
					t.Errorf("record carries %s=%q, not wanted (record %v)", k, data[k], data)
				}
			}
			if homePatches(cs) != tc.patches || a.homeLearn.armed != tc.armed {
				t.Errorf("patches %d armed %v, want %d %v", homePatches(cs), a.homeLearn.armed, tc.patches, tc.armed)
			}
			if strings.HasPrefix(tc.name, "malformed id") && api.conversationInfoCalls != 0 {
				t.Errorf("conversations.info called %d times for a malformed id", api.conversationInfoCalls)
			}
		})
	}
}

// TestSlackHomeReconcilerRetriesUntilTheRecordAgrees: a pass that fails (the
// read, the check, the refusal write or the re-stamp) is tried again after a
// wait that doubles from homeRetryMin to homeRetryMax, and a pass that
// succeeds is followed by the steady interval; nothing is armed before a read
// and a check succeed.
func TestSlackHomeReconcilerRetriesUntilTheRecordAgrees(t *testing.T) {
	recorded := func() map[string]string {
		return map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage}
	}
	archived := func(c *slack.Channel) { c.IsArchived = true }
	cases := []struct {
		name       string
		data       map[string]string
		info       *slack.Channel
		arrange    func(api *fakeSlackAPI, cs *k8sfake.Clientset)
		failing    int      // passes that fail before the one that converges
		armedEarly []string // SetHome calls allowed before that pass: a checked channel whose stamp is what failed
		wantHome   []string
		wantData   map[string]string
		wantArmed  bool
	}{
		{"the read fails three times", recorded(), homeChannelInfo("C0AA1", nil),
			func(_ *fakeSlackAPI, cs *k8sfake.Clientset) { failTimes(cs, "get", 3) }, 3, nil, []string{"C0AA1"},
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestImage}, false},
		{"the check fails twice", recorded(), homeChannelInfo("C0AA1", nil),
			func(api *fakeSlackAPI, _ *k8sfake.Clientset) { api.conversationErrs = 2 }, 2, nil, []string{"C0AA1"},
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestImage}, false},
		{"the refusal write fails once", recorded(), homeChannelInfo("C0AA1", archived),
			func(_ *fakeSlackAPI, cs *k8sfake.Clientset) { failOnce(cs, "patch") }, 1, nil, nil,
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived}, true},
		{"the re-stamp fails once", recorded(), homeChannelInfo("C0AA1", nil),
			func(_ *fakeSlackAPI, cs *k8sfake.Clientset) { failOnce(cs, "patch") }, 1, []string{"C0AA1"}, []string{"C0AA1"},
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestImage}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, tc.data)
			api.conversations[tc.info.ID] = tc.info
			tc.arrange(api, cs)
			clock := newHomeClock(a)
			a.RunHomeReconciler(context.Background())
			wantWaits := []time.Duration{}
			for i, wait := 0, homeRetryMin; i < tc.failing; i++ {
				wantWaits = append(wantWaits, wait)
				wait = min(wait*2, homeRetryMax)
			}
			wantWaits = append(wantWaits, homeReconcileEvery)
			// Before the pass that converges: nothing armed, unless the
			// channel was read and checked and only its stamp failed.
			for i := 1; i < tc.failing; i++ {
				clock.pass(t)
			}
			if got := setter.all(); strings.Join(got, ",") != strings.Join(tc.armedEarly, ",") {
				t.Fatalf("SetHome %v before a pass succeeded, want %v", got, tc.armedEarly)
			}
			if tc.failing > 0 {
				clock.pass(t)
			}
			if got := clock.all(); !slices.Equal(got, wantWaits) {
				t.Errorf("waits = %v, want %v", got, wantWaits)
			}
			if got := setter.all(); strings.Join(got, ",") != strings.Join(tc.wantHome, ",") {
				t.Errorf("SetHome = %v, want %v", got, tc.wantHome)
			}
			data := homeRecordData(t, cs)
			for k, v := range tc.wantData {
				if data[k] != v {
					t.Errorf("record[%s] = %q, want %q (record %v)", k, data[k], v, data)
				}
			}
			if a.homeLearn.armed != tc.wantArmed {
				t.Errorf("armed = %v, want %v", a.homeLearn.armed, tc.wantArmed)
			}
			// Settled: the next pass is one read, no Slack call, no write.
			calls, patches := api.conversationInfoCalls, homePatches(cs)
			clock.pass(t)
			if api.conversationInfoCalls != calls || homePatches(cs) != patches {
				t.Errorf("a settled pass made %d Slack calls and %d writes", api.conversationInfoCalls-calls, homePatches(cs)-patches)
			}
		})
	}
}

// TestSlackHomeReconcilerFollowsAHandEdit: a record edited while the gateway
// runs is acted on at the next pass, as a boot would: a channel that passes
// is adopted and stamped, one that is refused is marked and the learner armed.
func TestSlackHomeReconcilerFollowsAHandEdit(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", nil)
	api.conversations["C0BB2"] = homeChannelInfo("C0BB2", func(c *slack.Channel) { c.IsArchived = true })
	clock := newHomeClock(a)
	a.RunHomeReconciler(context.Background())
	if !a.homeLearn.armed {
		t.Fatal("an empty record did not arm the learner")
	}
	replaceRecord(t, cs, map[string]string{homeRecordKeySlack: "C0AA1"})
	clock.pass(t)
	if got := setter.all(); len(got) != 1 || got[0] != "C0AA1" || a.homeLearn.armed {
		t.Fatalf("after the edit to a good channel: SetHome %v, armed %v; want [C0AA1] and not armed", got, a.homeLearn.armed)
	}
	if data := homeRecordData(t, cs); data[homeRecordKeyReader] != homeTestImage || data[homeRecordKeyWriter] != homeTestImage {
		t.Errorf("record after the adopt = %v, want reader and writer stamped", data)
	}
	// The stamp is edited away: the next pass puts it back without a call.
	replaceRecord(t, cs, map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestImage, homeRecordKeyReader: "someone-else"})
	calls := api.conversationInfoCalls
	clock.pass(t)
	if data := homeRecordData(t, cs); data[homeRecordKeyReader] != homeTestImage {
		t.Errorf("record after the reader edit = %v, want the reader re-stamped", data)
	}
	if api.conversationInfoCalls != calls+1 || len(setter.all()) != 1 {
		t.Errorf("the re-stamp pass made %d Slack calls and %d SetHome calls; want 1 and 0 more", api.conversationInfoCalls-calls, len(setter.all())-1)
	}
	// Edited to an archived channel: refused, and the learner armed again.
	replaceRecord(t, cs, map[string]string{homeRecordKeySlack: "C0BB2"})
	clock.pass(t)
	data := homeRecordData(t, cs)
	if data[homeRecordKeyRefused] != homeRefusedArchived || !a.homeLearn.armed || len(setter.all()) != 1 {
		t.Errorf("after the edit to an archived channel: record %v, armed %v, SetHome %v; want refused=archived, armed, no new SetHome", data, a.homeLearn.armed, setter.all())
	}
}

// TestSlackHomeReconcilerPassAndLearnConvergeOnTheLaterChannel: a pass that
// read one channel and a learn that wrote another in the same seconds end
// with the route and the record on the learn's channel, whichever SetHome
// ran last. Without the route in the settled compare, the pass's re-stamp
// conflict left the route on its channel and every later pass read-only.
func TestSlackHomeReconcilerPassAndLearnConvergeOnTheLaterChannel(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", nil)
	api.conversations["C0BB2"] = homeChannelInfo("C0BB2", nil)
	clock := newHomeClock(a)
	a.RunHomeReconciler(context.Background())
	replaceRecord(t, cs, map[string]string{homeRecordKeySlack: "C0AA1"})
	// While the pass checks C0AA1: the record is cleared by hand and a
	// mention learns C0BB2, inline on the pass's goroutine.
	var hookErr error
	interleaved := false
	api.infoHook = func(channel string) {
		if channel != "C0AA1" || interleaved {
			return
		}
		interleaved = true
		obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
		if err != nil {
			hookErr = err
			return
		}
		cm := obj.(*corev1.ConfigMap)
		cm.Data = nil
		if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
			hookErr = err
			return
		}
		ev := slackMsg(slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here", "130.0", "")
		msg, ok := a.inbound(context.Background(), ev)
		if !ok {
			hookErr = errors.New("the mention was not a turn")
			return
		}
		a.notePending(msg)
		a.noteHomeCandidate(msg, ev)
		a.InboundVerified(msg.Conversation, msg.MessageID, true)
	}
	// The pass's re-stamp then meets the record the learn wrote: a conflict,
	// as the resourceVersion precondition answers on a real API server.
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		var body struct {
			Data map[string]*string `json:"data"`
		}
		if err := json.Unmarshal(action.(k8stesting.PatchAction).GetPatch(), &body); err != nil {
			return true, nil, err
		}
		if _, learnWrite := body.Data[homeRecordKeySlack]; learnWrite || !interleaved {
			return false, nil, nil
		}
		return true, nil, apierrors.NewConflict(configMapsGVR.GroupResource(), homeTestRecord, errors.New("the record changed"))
	})
	clock.pass(t)
	if hookErr != nil {
		t.Fatal(hookErr)
	}
	if got := setter.all(); !slices.Equal(got, []string{"C0BB2", "C0AA1"}) {
		t.Fatalf("SetHome during the overlap = %v, want [C0BB2 C0AA1]", got)
	}
	// The retry pass arms the record's channel.
	clock.pass(t)
	got := setter.all()
	if len(got) != 3 || got[2] != "C0BB2" {
		t.Fatalf("SetHome after the retry = %v, want a third call for C0BB2", got)
	}
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0BB2" || data[homeRecordKeyReader] != homeTestImage {
		t.Errorf("record = %v, want C0BB2 stamped by this image", data)
	}
	// And settled: a further pass is one read.
	calls, patches := api.conversationInfoCalls, homePatches(cs)
	clock.pass(t)
	if api.conversationInfoCalls != calls || homePatches(cs) != patches || len(setter.all()) != 3 {
		t.Errorf("a settled pass made %d Slack calls, %d writes, %d SetHome calls", api.conversationInfoCalls-calls, homePatches(cs)-patches, len(setter.all())-3)
	}
}

// TestSlackHomeReconcilerSkipsAPassDuringALearn: a pass that finds a learn
// in flight does nothing, and the learn settles the state the next pass
// reads, so that pass is one read with no Slack call and no write.
func TestSlackHomeReconcilerSkipsAPassDuringALearn(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", nil)
	clock := newHomeClock(a)
	a.RunHomeReconciler(context.Background())
	a.syncSessions = false
	entered, release := make(chan struct{}), make(chan struct{})
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		close(entered)
		<-release
		return false, nil, nil
	})
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "140.0", "", true)
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the learn never reached the write")
	}
	// The fake clientset holds its lock while a reactor runs, so nothing
	// reads its actions until the write is released; the read count is
	// checked afterwards.
	calls := api.conversationInfoCalls
	clock.pass(t)
	if api.conversationInfoCalls != calls || len(setter.all()) != 0 {
		t.Fatalf("a pass during the learn made %d Slack calls, SetHome %v; want none", api.conversationInfoCalls-calls, setter.all())
	}
	close(release)
	deadline := time.Now().Add(5 * time.Second)
	for {
		a.homeLearn.mu.Lock()
		busy := a.homeLearn.busy
		a.homeLearn.mu.Unlock()
		if !busy && len(setter.all()) == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the learn did not finish")
		}
		time.Sleep(time.Millisecond)
	}
	// Two reads so far: the first pass's and the learn's; the skipped pass
	// made none.
	if gets := homeGets(cs); gets != 2 {
		t.Fatalf("reads = %d after the learn, want 2 (the skipped pass made none)", gets)
	}
	calls, patches := api.conversationInfoCalls, homePatches(cs)
	clock.pass(t)
	if api.conversationInfoCalls != calls || homeGets(cs) != 3 || homePatches(cs) != patches {
		t.Errorf("the pass after the learn made %d Slack calls, %d reads in total, %d writes; want 0, 3, 0", api.conversationInfoCalls-calls, homeGets(cs), homePatches(cs)-patches)
	}
}

// TestSlackHomeReconcilerBackoffResetsAfterAPassSucceeds: the wait after a
// failure starts again from homeRetryMin once a pass has succeeded.
func TestSlackHomeReconcilerBackoffResetsAfterAPassSucceeds(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage})
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", nil)
	clock := newHomeClock(a)
	failOnce(cs, "get")
	a.RunHomeReconciler(context.Background())
	clock.pass(t)
	failOnce(cs, "get")
	clock.pass(t)
	if got, want := clock.all(), []time.Duration{homeRetryMin, homeReconcileEvery, homeRetryMin}; !slices.Equal(got, want) {
		t.Errorf("waits = %v, want %v", got, want)
	}
}

// TestSlackHomeReconcilerStopsWithRun: closing the learner ends the loop's
// wait, so Run's shutdown does not sit out the steady interval.
func TestSlackHomeReconcilerStopsWithRun(t *testing.T) {
	a, _, _, _ := newHomeLearnAdapter(t, nil)
	clock := newHomeClock(a)
	a.RunHomeReconciler(context.Background())
	if n := clock.parked(t); n != 1 {
		t.Fatalf("waits = %v, want the loop parked once", clock.all())
	}
	done := make(chan struct{})
	go func() { a.closeHomeLearn(); close(done) }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("closeHomeLearn did not return while the loop waited")
	}
}

func TestSlackHomeUnreadableRecordLeavesTheLearnOff(t *testing.T) {
	a, _, cs, setter := newHomeLearnAdapter(t, nil)
	cs.PrependReactor("get", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("forbidden")
	})
	clock := newHomeClock(a)
	a.RunHomeReconciler(context.Background())
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "80.0", "", true)
	if a.homeLearn.armed || len(setter.all()) != 0 || homePatches(cs) != 0 {
		t.Errorf("armed %v, SetHome %v, patches %d; want the learn off", a.homeLearn.armed, setter.all(), homePatches(cs))
	}
	// And the loop keeps trying, capped at the steady interval.
	for i := 0; i < 5; i++ {
		clock.pass(t)
	}
	if got := clock.all(); got[len(got)-1] != homeRetryMax || got[len(got)-2] != homeRetryMax {
		t.Errorf("waits = %v, want the wait capped at %s", got, homeRetryMax)
	}
}

func TestSlackHomeWriteInFlightJoinsShutdown(t *testing.T) {
	a, _, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	a.syncSessions = false
	entered, release := make(chan struct{}), make(chan struct{})
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		close(entered)
		<-release
		return false, nil, nil
	})
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "90.0", "", true)
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the learn never reached the write")
	}
	// closeHomeLearn is what Run defers: it ends the reconcile loop's wait
	// and then waits for the write in flight.
	waited := make(chan struct{})
	go func() {
		a.closeHomeLearn()
		close(waited)
	}()
	select {
	case <-waited:
		t.Fatal("Wait returned while the write was in flight")
	case <-time.After(50 * time.Millisecond):
	}
	close(release)
	select {
	case <-waited:
	case <-time.After(5 * time.Second):
		t.Fatal("Wait did not return after the write")
	}
	if got := setter.all(); len(got) != 1 || got[0] != "C0AA1" {
		t.Errorf("SetHome = %v, want [C1]", got)
	}
}

func TestSlackHomeNoLearnStartsAfterShutdown(t *testing.T) {
	a, api, cs, setter := newHomeLearnAdapter(t, nil)
	a.RunHomeReconciler(context.Background())
	closeHomeLearnBounded(t, a)
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> too late", "95.0", "", true)
	if homePatches(cs) != 0 || len(setter.all()) != 0 || api.conversationInfoCalls != 0 {
		t.Errorf("after shutdown: patches %d, SetHome %v, conversations.info %d; want none",
			homePatches(cs), setter.all(), api.conversationInfoCalls)
	}
	a.RunHomeReconciler(context.Background())
	if api.conversationInfoCalls != 0 {
		t.Error("an adopt started after shutdown")
	}
}
