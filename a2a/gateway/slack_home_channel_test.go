package gateway

import (
	"context"
	"encoding/json"
	"errors"
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
// every learn and adopt has finished when the call returns.

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
	a.SetHomeLearner(setter, cs, homeTestNamespace, homeTestRecord, homeTestImage)
	return a, api, cs, setter
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
	a.AdoptHomeRecord(context.Background())
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
			a.AdoptHomeRecord(context.Background())
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
			a.AdoptHomeRecord(context.Background())
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
	a.AdoptHomeRecord(context.Background())
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
	a.AdoptHomeRecord(context.Background())
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
	a.AdoptHomeRecord(context.Background())
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
	a.AdoptHomeRecord(context.Background())
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

// failOnce fails the next configmaps action of verb once; later ones pass.
func failOnce(cs *k8sfake.Clientset, verb string) {
	failed := false
	cs.PrependReactor(verb, "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if failed {
			return false, nil, nil
		}
		failed = true
		return true, nil, errors.New("apiserver unavailable")
	})
}

func TestSlackHomeRefusalThatDidNotLandIsWrittenAtTheNextRefusal(t *testing.T) {
	for _, verb := range []string{"get", "patch"} {
		t.Run("the record "+verb+" fails", func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, nil)
			api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
			a.AdoptHomeRecord(context.Background())
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
	a.AdoptHomeRecord(context.Background())
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
	b.AdoptHomeRecord(context.Background())
	if got := bsetter.all(); len(got) != 1 || got[0] != "C0BB2" || b.homeLearn.armed {
		t.Errorf("next boot: SetHome %v, armed %v; want [C0BB2] and not armed", got, b.homeLearn.armed)
	}
}

// TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel: while a
// channel is recorded, slack.refused describes that channel. A candidate
// refused for another reason writes nothing, whether the boot's own refusal
// write landed or failed; the next channel that passes replaces the record.
func TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel(t *testing.T) {
	cases := []struct {
		name          string
		bootWriteOK   bool
		refusedAfter  string
		patchesBefore int
	}{
		{"the boot refusal landed", true, homeRefusedArchived, 1},
		{"the boot refusal write failed", false, "", 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, setter := newHomeLearnAdapter(t, map[string]string{
				homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyReader: homeTestOldImage,
			})
			api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
			api.conversations["C0BB2"] = homeChannelInfo("C0BB2", func(c *slack.Channel) { c.IsExtShared = true })
			api.conversations["C0CC3"] = homeChannelInfo("C0CC3", nil)
			if !tc.bootWriteOK {
				failOnce(cs, "patch")
			}
			a.AdoptHomeRecord(context.Background())
			if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != tc.refusedAfter || !a.homeLearn.armed {
				t.Fatalf("after boot: record %v, armed %v; want refused=%q and armed", data, a.homeLearn.armed, tc.refusedAfter)
			}
			// A mention in a shared channel: refused, and the record still
			// describes the recorded channel.
			homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> hi", "120.0", "", true)
			data := homeRecordData(t, cs)
			if data[homeRecordKeyRefused] != tc.refusedAfter || data[homeRecordKeySlack] != "C0AA1" || homePatches(cs) != tc.patchesBefore || len(setter.all()) != 0 {
				t.Fatalf("after a refused candidate: record %v, patches %d, SetHome %v; want refused=%q beside C0AA1, %d patches and none",
					data, homePatches(cs), setter.all(), tc.refusedAfter, tc.patchesBefore)
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
		{"check that cannot be made: adopted without a re-stamp",
			map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeyReader: homeTestOldImage}, nil, errors.New("ratelimited"), []string{"C0AA1"},
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
			a.AdoptHomeRecord(context.Background())
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

func TestSlackHomeUnreadableRecordLeavesTheLearnOff(t *testing.T) {
	a, _, cs, setter := newHomeLearnAdapter(t, nil)
	cs.PrependReactor("get", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("forbidden")
	})
	a.AdoptHomeRecord(context.Background())
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "80.0", "", true)
	if a.homeLearn.armed || len(setter.all()) != 0 || homePatches(cs) != 0 {
		t.Errorf("armed %v, SetHome %v, patches %d; want the learn off", a.homeLearn.armed, setter.all(), homePatches(cs))
	}
}

func TestSlackHomeWriteInFlightJoinsShutdown(t *testing.T) {
	a, _, cs, setter := newHomeLearnAdapter(t, nil)
	a.AdoptHomeRecord(context.Background())
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
	waited := make(chan struct{})
	go func() {
		a.homeLearn.wg.Wait()
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
	a.AdoptHomeRecord(context.Background())
	a.closeHomeLearn()
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> too late", "95.0", "", true)
	if homePatches(cs) != 0 || len(setter.all()) != 0 || api.conversationInfoCalls != 0 {
		t.Errorf("after shutdown: patches %d, SetHome %v, conversations.info %d; want none",
			homePatches(cs), setter.all(), api.conversationInfoCalls)
	}
	a.AdoptHomeRecord(context.Background())
	if api.conversationInfoCalls != 0 {
		t.Error("an adopt started after shutdown")
	}
}
