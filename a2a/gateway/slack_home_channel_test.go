package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
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
// conversations.info, a fake clientset holds the record ConfigMap, and a
// temporary directory stands in for the operator's projection mounted in the
// pod. goSession runs inline (syncSessions), so every learn has finished when
// the call returns. The learner holds no home: the file decides whether a
// mention is a candidate, and the notify route reads the same file.

const (
	homeTestNamespace = "kubeagents-system"
	homeTestRecord    = "platform-agent-home-channel"
	homeTestImage     = "ghcr.io/gke-labs/kube-agents/a2a-gateway:new"
	homeTestOldImage  = "ghcr.io/gke-labs/kube-agents/a2a-gateway:old"
)

var configMapsGVR = schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}

// newHomeLearnAdapter is a Slack adapter with the learner wired over a record
// holding data (nil for an empty record) and a projection directory whose
// file is empty: the state after the operator mounted the projection and
// admitted nothing yet. writeHomeFile and removeHomeDir change it.
func newHomeLearnAdapter(t *testing.T, data map[string]string) (*SlackAdapter, *fakeSlackAPI, *k8sfake.Clientset, string) {
	t.Helper()
	api := &fakeSlackAPI{team: "T1", conversations: map[string]*slack.Channel{}}
	a, _ := newUXSlackAdapter(api)
	cs := k8sfake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: homeTestRecord, Namespace: homeTestNamespace, ResourceVersion: "1"},
		Data:       data,
	})
	dir := filepath.Join(t.TempDir(), "chat-home")
	a.SetHomeLearner(cs, homeTestNamespace, homeTestRecord, homeTestImage, dir)
	writeHomeFile(t, dir, "")
	t.Cleanup(a.closeHomeLearn)
	return a, api, cs, dir
}

// writeHomeFile is the kubelet's refresh of the projection: the file holds
// value (the operator admitted it) or nothing.
func writeHomeFile(t *testing.T, dir, value string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, homeFileKey), []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}

// removeHomeDir is a pod with no projection mounted: an operator older than it.
func removeHomeDir(t *testing.T, dir string) {
	t.Helper()
	if err := os.RemoveAll(dir); err != nil {
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
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> what is running?", "10.0", "", true)

	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0AA1" || data[homeRecordKeySetBy] != "U1" || data[homeRecordKeyWriter] != homeTestImage {
		t.Fatalf("record = %v", data)
	}
	if _, stamped := data["reader"]; stamped {
		t.Errorf("record carries a reader stamp: %v", data)
	}
	if _, err := time.Parse(time.RFC3339, data[homeRecordKeySetAt]); err != nil {
		t.Errorf("setAt %q: %v", data[homeRecordKeySetAt], err)
	}
	if len(api.posted) != 1 || !strings.Contains(api.posted[0].text, "<#C0AA1>") || api.posted[0].channel != "C0AA1" || api.posted[0].thread != "10.0" {
		t.Errorf("posts = %+v, want one line naming the channel in the mention's thread", api.posted)
	}
	if api.conversationInfoCalls != 1 {
		t.Errorf("conversations.info calls = %d, want 1", api.conversationInfoCalls)
	}
	// The patch is data-only with the read resourceVersion as its precondition.
	for _, act := range cs.Actions() {
		if act.GetVerb() != "patch" {
			continue
		}
		var body map[string]json.RawMessage
		if err := json.Unmarshal(act.(k8stesting.PatchAction).GetPatch(), &body); err != nil {
			t.Fatal(err)
		}
		if _, ok := body["data"]; !ok || !strings.Contains(string(body["metadata"]), `"resourceVersion":"1"`) {
			t.Errorf("patch = %s, want data keys under a resourceVersion precondition", act.(k8stesting.PatchAction).GetPatch())
		}
	}
}

// TestSlackHomeTheFileDecidesWhetherAMentionIsACandidate: a mention learns
// only while the projection names no home. A file that names a channel (the
// operator admitted one) learns nothing from a mention elsewhere, at the cost
// of one conversations.info on the home, which Slack still accepts; a file
// holding something that is not a channel id (a hand edit the operator
// rewrites) and a directory that is not mounted (an operator older than the
// projection) learn nothing with no Slack call; none writes. An empty file
// learns.
func TestSlackHomeTheFileDecidesWhetherAMentionIsACandidate(t *testing.T) {
	cases := []struct {
		name  string
		setup func(t *testing.T, dir string)
		learn bool
		calls int
	}{
		{"the file names a channel", func(t *testing.T, dir string) { writeHomeFile(t, dir, "C0HOME\n") }, false, 1},
		{"the file holds something that is not a channel id", func(t *testing.T, dir string) { writeHomeFile(t, dir, "general") }, false, 0},
		{"the directory is not mounted", func(t *testing.T, dir string) { removeHomeDir(t, dir) }, false, 0},
		{"the file is empty", func(t *testing.T, dir string) { writeHomeFile(t, dir, " \n") }, true, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, dir := newHomeLearnAdapter(t, nil)
			api.conversations["C0HOME"] = homeChannelInfo("C0HOME", nil)
			tc.setup(t, dir)
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "15.0", "", true)
			learned := homeRecordData(t, cs)[homeRecordKeySlack] == "C0AA1"
			if learned != tc.learn || api.conversationInfoCalls != tc.calls || (homePatches(cs) != 0) != tc.learn {
				t.Errorf("learned %v, conversations.info %d, patches %d; want learn=%v, calls=%d", learned, api.conversationInfoCalls, homePatches(cs), tc.learn, tc.calls)
			}
		})
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
		{"a mention on an adapter with no learner wired", func(t *testing.T, a *SlackAdapter) {
			a.homeLearn.record.client = nil
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> early", "27.0", "", true)
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, _ := newHomeLearnAdapter(t, nil)
			tc.run(t, a)
			if homePatches(cs) != 0 || api.conversationInfoCalls != 0 || len(api.posted) != 0 {
				t.Errorf("patches %d, conversations.info %d, posts %d; want none",
					homePatches(cs), api.conversationInfoCalls, len(api.posted))
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
			a, api, cs, _ := newHomeLearnAdapter(t, nil)
			if tc.info != nil {
				api.conversations["C0AA1"] = tc.info
			}
			api.conversationErr = tc.err
			homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "30.0", "", true)
			data := homeRecordData(t, cs)
			if data[homeRecordKeyRefused] != tc.reason || data[homeRecordKeySlack] != "" || len(api.posted) != 0 {
				t.Fatalf("record %v, posts %d; want refused=%s and nothing else", data, len(api.posted), tc.reason)
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
			if _, stale := data[homeRecordKeyRefused]; stale || data[homeRecordKeySlack] != "C0BB2" {
				t.Errorf("record after the learn = %v, want slack=C0BB2 and no refusal", data)
			}
		})
	}
}

func TestSlackHomeGroupDirectMessageIsRefusedWithoutACall(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	homeMention(t, a, slackChannelTypeMpIM, "G0MP1", "U1", "<@UBOT> hi all", "40.0", "", true)
	if api.conversationInfoCalls != 0 || homePatches(cs) != 0 {
		t.Fatalf("mpim: conversations.info %d, patches %d; want none", api.conversationInfoCalls, homePatches(cs))
	}
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here", "41.0", "", true)
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0BB2" {
		t.Errorf("the next channel mention did not learn: %v", data)
	}
}

func TestSlackHomeCheckThatCannotBeMadeIsRetried(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	api.conversationErr = errors.New("ratelimited")
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "50.0", "", true)
	if homePatches(cs) != 0 || len(api.posted) != 0 {
		t.Fatalf("after a failed check: patches %d, posts %d", homePatches(cs), len(api.posted))
	}
	api.conversationErr = nil
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi again", "51.0", "", true)
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0AA1" {
		t.Errorf("the retry did not learn: %v", data)
	}
}

// TestSlackHomeAdoptsAWinnerThatWroteFirst: a write the record changed under
// (409) reads again; a channel another writer recorded is kept after the same
// check, nothing else is written, and no line is posted for it.
func TestSlackHomeAdoptsAWinnerThatWroteFirst(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	conflicts := 0
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		var body struct {
			Data map[string]*string `json:"data"`
		}
		if err := json.Unmarshal(action.(k8stesting.PatchAction).GetPatch(), &body); err != nil {
			return true, nil, err
		}
		if _, learn := body.Data[homeRecordKeySlack]; !learn || conflicts > 0 {
			return false, nil, nil
		}
		conflicts++
		obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
		if err != nil {
			return true, nil, err
		}
		cm := obj.(*corev1.ConfigMap)
		cm.Data = map[string]string{homeRecordKeySlack: "C0WIN9", homeRecordKeyWriter: homeTestOldImage}
		if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
			return true, nil, err
		}
		return true, nil, apierrors.NewConflict(configMapsGVR.GroupResource(), homeTestRecord, errors.New("the record changed"))
	})
	api.conversations["C0WIN9"] = homeChannelInfo("C0WIN9", nil)
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "60.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0WIN9" || data[homeRecordKeyWriter] != homeTestOldImage || data[homeRecordKeySetBy] != "" {
		t.Errorf("record = %v, want the winner kept as written", data)
	}
	if conflicts != 1 || homePatches(cs) != 1 {
		t.Errorf("conflicts %d, patches %d; want one conflict and no write after it", conflicts, homePatches(cs))
	}
	if len(api.posted) != 0 {
		t.Errorf("posts = %+v, want no line for a channel another writer recorded", api.posted)
	}
}

// TestSlackHomeALearnAfterARefusalClearsTheRefusal: a stale slack.refused
// beside a channel the check passes (an older gateway's write, or a hand
// edit that set slack and left the key) blocks the operator's admission;
// the next mention adopts the channel and clears it, with no line posted.
func TestSlackHomeALearnAfterARefusalClearsTheRefusal(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, map[string]string{
		homeRecordKeySlack: "C0AA1", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived,
	})
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", nil)
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> hi", "65.0", "", true)
	data := homeRecordData(t, cs)
	if _, stale := data[homeRecordKeyRefused]; stale || data[homeRecordKeySlack] != "C0AA1" {
		t.Errorf("record = %v, want C0AA1 kept and the refusal cleared", data)
	}
	if homePatches(cs) != 1 || len(api.posted) != 0 {
		t.Errorf("patches %d, posts %d; want one clearing write and no line", homePatches(cs), len(api.posted))
	}
}

// TestSlackHomeTheWindowAfterALearnAdoptsSilently: until the kubelet refreshes
// the projection, the file is still empty, so the next mention is a candidate
// too; it finds the recorded channel, keeps it and posts no second line.
func TestSlackHomeTheWindowAfterALearnAdoptsSilently(t *testing.T) {
	a, api, cs, dir := newHomeLearnAdapter(t, nil)
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "70.0", "", true)
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U2", "<@UBOT> and here", "71.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0AA1" || data[homeRecordKeySetBy] != "U1" || homePatches(cs) != 1 {
		t.Errorf("record %v, patches %d; want the first learn kept and no second write", data, homePatches(cs))
	}
	if len(api.posted) != 1 {
		t.Errorf("posts = %+v, want the one learned line", api.posted)
	}
	// The projection caught up: a mention elsewhere is not a candidate any
	// more; it costs one conversations.info on the home, which still passes.
	writeHomeFile(t, dir, "C0AA1\n")
	calls := api.conversationInfoCalls
	homeMention(t, a, slackChannelTypeChannel, "C0CC3", "U3", "<@UBOT> later", "72.0", "", true)
	if api.conversationInfoCalls != calls+1 || homePatches(cs) != 1 {
		t.Errorf("after the file caught up: conversations.info %d (was %d), patches %d; want one check of the home and no write", api.conversationInfoCalls, calls, homePatches(cs))
	}
}

func TestSlackHomeWriteErrorLearnsNothing(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	broken := true
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if broken {
			return true, nil, errors.New("apiserver unavailable")
		}
		return false, nil, nil
	})
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "80.0", "", true)
	if len(api.posted) != 0 || homeRecordData(t, cs)[homeRecordKeySlack] != "" {
		t.Fatalf("after a write error: posts %d, record %v; want nothing learned", len(api.posted), homeRecordData(t, cs))
	}
	broken = false
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "81.0", "", true)
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0AA1" {
		t.Errorf("the retry did not learn: %v", data)
	}
}

func TestSlackHomeRefusalThatDidNotLandIsWrittenAtTheNextRefusal(t *testing.T) {
	for _, verb := range []string{"get", "patch"} {
		t.Run("the record "+verb+" fails", func(t *testing.T) {
			a, api, cs, _ := newHomeLearnAdapter(t, nil)
			api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
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
			if homePatches(cs) != patches {
				t.Errorf("after a third refusal: patches %d (was %d)", homePatches(cs), patches)
			}
		})
	}
}

// TestSlackHomeRefusedRecordIsReplacedByTheNextMention: a recorded channel
// the check now refuses (archived since it was learned; the operator's file
// is empty because the record carries the refusal) is replaced by the next
// mention that passes, as an empty record would be filled.
func TestSlackHomeRefusedRecordIsReplacedByTheNextMention(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, map[string]string{
		homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived,
	})
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
	// A mention in the refused channel: a log line, no write, the record
	// keeps describing the recorded channel.
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> hi", "110.0", "", true)
	if homePatches(cs) != 0 || len(api.posted) != 0 {
		t.Fatalf("after a mention in the refused channel: patches %d, posts %d; want none", homePatches(cs), len(api.posted))
	}
	// A mention in a channel that passes replaces it.
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> here then", "111.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0BB2" || data[homeRecordKeySetBy] != "U1" || data[homeRecordKeyWriter] != homeTestImage {
		t.Fatalf("record after the replacement = %v", data)
	}
	if _, stale := data[homeRecordKeyRefused]; stale {
		t.Errorf("slack.refused still present: %v", data)
	}
	if len(api.posted) != 1 || !strings.Contains(api.posted[0].text, "<#C0BB2>") || api.posted[0].thread != "111.0" {
		t.Errorf("posts = %+v, want one line naming C0BB2 in the learning thread", api.posted)
	}
}

// TestSlackHomeAHomeRefusedSinceTheLearnIsReplacedByTheNextMention: the file
// names a home, Slack refuses it now (archived since it was learned), and a
// mention in a channel that passes replaces it; the file alone never ends
// the learn for a home Slack no longer accepts.
func TestSlackHomeAHomeRefusedSinceTheLearnIsReplacedByTheNextMention(t *testing.T) {
	a, api, cs, dir := newHomeLearnAdapter(t, map[string]string{
		homeRecordKeySlack: "C0OLD1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage,
	})
	writeHomeFile(t, dir, "C0OLD1\n")
	api.conversations["C0OLD1"] = homeChannelInfo("C0OLD1", func(c *slack.Channel) { c.IsArchived = true })
	homeMention(t, a, slackChannelTypeChannel, "C0NEW2", "U1", "<@UBOT> here now", "120.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0NEW2" || data[homeRecordKeySetBy] != "U1" {
		t.Fatalf("record after a mention with the home archived = %v, want C0NEW2 by U1", data)
	}
	if _, stale := data[homeRecordKeyRefused]; stale {
		t.Errorf("slack.refused present after the replacement: %v", data)
	}
	if len(api.posted) != 1 || !strings.Contains(api.posted[0].text, "<#C0NEW2>") || api.posted[0].thread != "120.0" {
		t.Errorf("posts = %+v, want one line naming C0NEW2 in the learning thread", api.posted)
	}
	// With the home still accepted, a mention elsewhere changes nothing.
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	writeHomeFile(t, dir, "C0NEW2\n")
	before := homePatches(cs)
	homeMention(t, a, slackChannelTypeChannel, "C0THR3", "U1", "<@UBOT> and here", "121.0", "", true)
	if homePatches(cs) != before || len(api.posted) != 1 {
		t.Errorf("a mention with an accepted home wrote or posted: patches %d->%d, posts %d", before, homePatches(cs), len(api.posted))
	}
}

// TestSlackHomeVerifyHomeForPostWritesTheRefusalBesideTheRecord: the
// post-time check. A refused home is written to slack.refused once while
// the record names it; a record that names another channel is not
// relabelled; a check that cannot be made writes nothing; a healthy home
// writes nothing.
func TestSlackHomeVerifyHomeForPostWritesTheRefusalBesideTheRecord(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage})
	ctx := context.Background()
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsExtShared = true })
	api.conversations["C0BB2"] = homeChannelInfo("C0BB2", func(c *slack.Channel) { c.IsArchived = true })
	api.conversations["C0OK3"] = homeChannelInfo("C0OK3", nil)
	if refused, err := a.VerifyHomeForPost(ctx, "C0BB2"); refused != homeRefusedArchived || err != nil || homePatches(cs) != 0 {
		t.Fatalf("a channel the record does not name: refused=%q err=%v patches=%d; want archived and no write", refused, err, homePatches(cs))
	}
	if refused, err := a.VerifyHomeForPost(ctx, "C0AA1"); refused != homeRefusedShared || err != nil {
		t.Fatalf("the shared home: refused=%q err=%v", refused, err)
	}
	if data := homeRecordData(t, cs); data[homeRecordKeyRefused] != homeRefusedShared || data[homeRecordKeySlack] != "C0AA1" || homePatches(cs) != 1 {
		t.Fatalf("record after the refusal = %v (patches %d), want slack.refused=shared beside C0AA1 in one write", data, homePatches(cs))
	}
	a.VerifyHomeForPost(ctx, "C0AA1")
	if homePatches(cs) != 1 {
		t.Errorf("the same refusal was written twice")
	}
	// A check that cannot be made: the fake answers conversationErr for a
	// channel it does not name.
	api.conversationErr = errors.New("429")
	if refused, err := a.VerifyHomeForPost(ctx, "C0XX9"); refused != "" || err == nil || homePatches(cs) != 1 {
		t.Errorf("a check that cannot be made: refused=%q err=%v patches=%d", refused, err, homePatches(cs))
	}
	api.conversationErr = nil
	if refused, err := a.VerifyHomeForPost(ctx, "C0OK3"); refused != "" || err != nil || homePatches(cs) != 1 {
		t.Errorf("a healthy home: refused=%q err=%v patches=%d", refused, err, homePatches(cs))
	}
}

// TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel: while a
// channel is recorded, slack.refused describes that channel. A candidate
// refused for another reason writes nothing; the next channel that passes
// replaces the record.
func TestSlackHomeRefusedCandidateNeverRelabelsTheRecordedChannel(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, map[string]string{
		homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestOldImage, homeRecordKeyRefused: homeRefusedArchived,
	})
	api.conversations["C0AA1"] = homeChannelInfo("C0AA1", func(c *slack.Channel) { c.IsArchived = true })
	api.conversations["C0BB2"] = homeChannelInfo("C0BB2", func(c *slack.Channel) { c.IsExtShared = true })
	api.conversations["C0CC3"] = homeChannelInfo("C0CC3", nil)
	homeMention(t, a, slackChannelTypeChannel, "C0BB2", "U1", "<@UBOT> hi", "120.0", "", true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeyRefused] != homeRefusedArchived || data[homeRecordKeySlack] != "C0AA1" || homePatches(cs) != 0 {
		t.Fatalf("after a refused candidate: record %v, patches %d; want the record untouched", data, homePatches(cs))
	}
	homeMention(t, a, slackChannelTypeChannel, "C0CC3", "U1", "<@UBOT> here then", "121.0", "", true)
	data = homeRecordData(t, cs)
	if _, stale := data[homeRecordKeyRefused]; stale || data[homeRecordKeySlack] != "C0CC3" {
		t.Errorf("record after the replacement = %v, want slack=C0CC3 and no refusal", data)
	}
}

// TestSlackHomeLearnedHomeReadsTheProjection: the notify route's home source.
func TestSlackHomeLearnedHomeReadsTheProjection(t *testing.T) {
	a, _, _, dir := newHomeLearnAdapter(t, nil)
	cases := []struct {
		name    string
		setup   func()
		want    string
		present bool
	}{
		{"empty", func() { writeHomeFile(t, dir, "") }, "", true},
		{"a channel, trimmed", func() { writeHomeFile(t, dir, " C0HOME\n") }, "C0HOME", true},
		{"a private channel", func() { writeHomeFile(t, dir, "G0PRIV\n") }, "G0PRIV", true},
		{"not a channel id", func() { writeHomeFile(t, dir, "general\n") }, "", true},
		{"a direct message id", func() { writeHomeFile(t, dir, "D0DM1\n") }, "", true},
		{"the file is gone", func() { os.Remove(filepath.Join(dir, homeFileKey)) }, "", false},
		{"the directory is gone", func() { removeHomeDir(t, dir) }, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tc.setup()
			got, present := a.LearnedHome()
			if got != tc.want || present != tc.present {
				t.Errorf("LearnedHome() = (%q, %v), want (%q, %v)", got, present, tc.want, tc.present)
			}
		})
	}
}

func TestSlackHomeWriteInFlightJoinsShutdown(t *testing.T) {
	a, _, cs, _ := newHomeLearnAdapter(t, nil)
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
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0AA1" {
		t.Errorf("record = %v, want the learn written", data)
	}
}

func TestSlackHomeNoLearnStartsAfterShutdown(t *testing.T) {
	a, api, cs, _ := newHomeLearnAdapter(t, nil)
	a.closeHomeLearn()
	homeMention(t, a, slackChannelTypeChannel, "C0AA1", "U1", "<@UBOT> too late", "95.0", "", true)
	if homePatches(cs) != 0 || api.conversationInfoCalls != 0 {
		t.Errorf("after shutdown: patches %d, conversations.info %d; want none", homePatches(cs), api.conversationInfoCalls)
	}
}
