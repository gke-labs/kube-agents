package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The learned Google Chat home space (gchat_home_channel.go): the fake relay
// answers spaces.get, a fake clientset holds the record ConfigMap, and a
// temporary directory stands in for the operator's projection mounted in the
// pod. The learn runs on its own goroutine; gchatHomeMention waits for it.

const (
	gchatSpacesGet     = "spaces/get"
	gchatMessageCreate = "spaces.messages/create"
)

// gchatNamedSpace is a spaces.get answer for a named space the app is in.
func gchatNamedSpace(name string) map[string]any {
	return map[string]any{"name": name, "spaceType": "SPACE"}
}

// newGchatHomeAdapter is a Chat adapter with the learner wired over a record
// holding data (nil for an empty record) and a projection directory whose
// gchat file is empty: the operator mounted the projection and admitted
// nothing yet. Every spaces.get answers a named space until a test queues or
// sets another answer.
func newGchatHomeAdapter(t *testing.T, data map[string]string) (*GoogleChatAdapter, *fakeChatRelay, *k8sfake.Clientset, string) {
	t.Helper()
	f := newFakeChatRelay(t)
	f.responses[gchatSpacesGet] = gchatNamedSpace("spaces/AAA")
	f.responses[gchatMessageCreate] = map[string]any{"name": "spaces/AAA/messages/LINE"}
	a := newTestGchatAdapterWithRelay(t, f)
	cs := k8sfake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: homeTestRecord, Namespace: homeTestNamespace, ResourceVersion: "1"},
		Data:       data,
	})
	dir := filepath.Join(t.TempDir(), "chat-home")
	a.SetHomeLearner(cs, homeTestNamespace, homeTestRecord, homeTestImage, dir)
	writeGchatHomeFile(t, dir, "")
	t.Cleanup(a.closeHomeLearn)
	return a, f, cs, dir
}

func writeGchatHomeFile(t *testing.T, dir, value string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, gchatHomeFileKey), []byte(value), 0o644); err != nil {
		t.Fatal(err)
	}
}

// gchatHomeMention drives one typed message through classify and the
// gateway's verdict, as Run and handleInbound do, and waits for the learn.
func gchatHomeMention(t *testing.T, a *GoogleChatAdapter, ev *gchatEvent, verified bool) {
	t.Helper()
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("message %s was not a turn: %s", ev.Message.Name, reason)
	}
	a.InboundVerified(msg.Conversation, msg.MessageID, verified)
	a.homeLearn.wg.Wait()
}

// gchatSpaceMention is a top-level mention in a threaded named space.
func gchatSpaceMention(space, thread, message, text string) *gchatEvent {
	ev := gchatMsg(space, "SPACE", "THREADED_MESSAGES", space+"/threads/"+thread, space+"/messages/"+message, "@agent "+text, text, "u1@example.com", "HUMAN")
	ev.Message.Sender.Name = "users/111"
	return ev
}

func relayCalls(f *fakeChatRelay, key string) []relayCall {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []relayCall
	for _, c := range f.calls {
		if strings.Join(c.resource, ".")+"/"+c.method == key {
			out = append(out, c)
		}
	}
	return out
}

func TestGchatHomeLearnsFromTheFirstSpaceMention(t *testing.T) {
	a, f, cs, _ := newGchatHomeAdapter(t, nil)
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)

	data := homeRecordData(t, cs)
	if data[homeRecordKeyGchat] != "spaces/AAA" || data[homeRecordKeyGchatSetBy] != "users/111" || data[homeRecordKeyWriter] != homeTestImage || data[homeRecordKeyGchatSetAt] == "" {
		t.Fatalf("record = %v, want gchat=spaces/AAA set by users/111 with the writer and a time", data)
	}
	if _, slack := data[homeRecordKeySlack]; slack {
		t.Errorf("a Chat learn wrote Slack's key: %v", data)
	}
	gets := relayCalls(f, gchatSpacesGet)
	if len(gets) != 1 || gets[0].arguments["name"] != "spaces/AAA" {
		t.Errorf("spaces.get calls = %+v, want one for spaces/AAA", gets)
	}
	posts := relayCalls(f, gchatMessageCreate)
	if len(posts) != 1 {
		t.Fatalf("posts = %+v, want the one line", posts)
	}
	body := posts[0].arguments["body"].(map[string]any)
	if posts[0].arguments["parent"] != "spaces/AAA" || body["text"] != gchatHomeLearnedLine || body["thread"].(map[string]any)["name"] != "spaces/AAA/threads/T1" {
		t.Errorf("the line = %+v, want it in the learning thread of spaces/AAA", posts[0])
	}
}

// TestGchatHomeNeverLearnsFromTheseTurns: a direct message, a group chat, a
// reply inside a thread, a command and an unverified sender are no
// candidates; none reads the record or calls Chat, bar the group chat's
// refusal, which makes no call either.
func TestGchatHomeNeverLearnsFromTheseTurns(t *testing.T) {
	cases := []struct {
		name     string
		ev       func() *gchatEvent
		verified bool
	}{
		{"a direct message", func() *gchatEvent {
			ev := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "spaces/D1/threads/T1", "spaces/D1/messages/M1", "hello", "", "u1@example.com", "HUMAN")
			return ev
		}, true},
		{"a legacy-typed direct message", func() *gchatEvent {
			ev := gchatMsg("spaces/D2", "", "", "spaces/D2/threads/T1", "spaces/D2/messages/M1", "hello", "", "u1@example.com", "HUMAN")
			ev.Space.Type = "DM"
			return ev
		}, true},
		{"a group chat", func() *gchatEvent {
			return gchatMsg("spaces/G1", "GROUP_CHAT", "", "spaces/G1/threads/T1", "spaces/G1/messages/M1", "@agent hello", "hello", "u1@example.com", "HUMAN")
		}, true},
		{"a reply inside a thread", func() *gchatEvent {
			ev := gchatSpaceMention("spaces/AAA", "T1", "M2", "and the pods?")
			ev.Message.ThreadReply = true
			return ev
		}, true},
		{"a command", func() *gchatEvent { return gchatSpaceMention("spaces/AAA", "T1", "M3", "/stop") }, true},
		{"an unverified sender", func() *gchatEvent { return gchatSpaceMention("spaces/AAA", "T1", "M4", "hello") }, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, f, cs, _ := newGchatHomeAdapter(t, nil)
			gchatHomeMention(t, a, tc.ev(), tc.verified)
			if data := homeRecordData(t, cs); len(data) != 0 || homePatches(cs) != 0 {
				t.Errorf("record = %v (patches %d), want nothing written", data, homePatches(cs))
			}
			if gets, posts := relayCalls(f, gchatSpacesGet), relayCalls(f, gchatMessageCreate); len(gets) != 0 || len(posts) != 0 {
				t.Errorf("Chat calls: gets %+v posts %+v, want none", gets, posts)
			}
			a.mu.Lock()
			pending := len(a.homePending)
			a.mu.Unlock()
			if pending != 0 {
				t.Errorf("%d marks left after the verdict", pending)
			}
		})
	}
}

// TestGchatHomeLearnArmedOnlyWithNoCRHomeAndPolicyAuto: the gate cmd/gateway
// reads. A home channel on the CR, policy manual, no record or no switch
// each disarm it, and Slack's CR home says nothing about Chat's.
func TestGchatHomeLearnArmedOnlyWithNoCRHomeAndPolicyAuto(t *testing.T) {
	armed := func() *Config {
		return &Config{GchatRelayURL: "http://relay", HomeChannelRecord: homeTestRecord, HomeChannelLearn: true}
	}
	cases := []struct {
		name string
		mut  func(*Config)
		want bool
	}{
		{"armed", func(*Config) {}, true},
		{"policy auto spelled out", func(c *Config) { c.GchatHomePolicy = "auto" }, true},
		{"a Slack home on the CR is not Chat's", func(c *Config) { c.SlackBotToken = "xoxb"; c.SlackHomeChannel = "C0AA1"; c.SlackHomePolicy = "manual" }, true},
		{"a CR home channel", func(c *Config) { c.GchatHomeChannel = "spaces/CR" }, false},
		{"a malformed CR home channel is still the CR's", func(c *Config) { c.GchatHomeChannel = "general" }, false},
		{"policy manual", func(c *Config) { c.GchatHomePolicy = "manual" }, false},
		{"no record named", func(c *Config) { c.HomeChannelRecord = "" }, false},
		{"the switch off", func(c *Config) { c.HomeChannelLearn = false }, false},
		{"Chat not on the gateway", func(c *Config) { c.GchatRelayURL = "" }, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c := armed()
			tc.mut(c)
			if got := c.GchatHomeLearnArmed(); got != tc.want {
				t.Errorf("GchatHomeLearnArmed() = %v, want %v", got, tc.want)
			}
			if got := c.HomeLearnArmedFor(BackendGchat); got != tc.want {
				t.Errorf("HomeLearnArmedFor(gchat) = %v, want %v", got, tc.want)
			}
		})
	}
	if armed().HomeLearnArmedFor(BackendDiscord) {
		t.Error("Discord learns no home")
	}
}

// TestGchatHomeUnarmedAdapterMarksNothing: an adapter the operator did not
// arm (a CR home channel) keeps no mark and learns nothing.
func TestGchatHomeUnarmedAdapterMarksNothing(t *testing.T) {
	f := newFakeChatRelay(t)
	a := newTestGchatAdapterWithRelay(t, f)
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
	a.mu.Lock()
	pending := len(a.homePending)
	a.mu.Unlock()
	if pending != 0 || len(relayCalls(f, gchatSpacesGet)) != 0 {
		t.Errorf("an unarmed adapter marked %d messages and made %d checks", pending, len(relayCalls(f, gchatSpacesGet)))
	}
}

// TestGchatHomeSecondMentionDoesNotRelearn: once the file names the home, a
// mention in the home does nothing, and a mention elsewhere checks the home
// once and leaves it. In the window before the file follows, a mention
// elsewhere adopts the recorded space and posts no line.
func TestGchatHomeSecondMentionDoesNotRelearn(t *testing.T) {
	a, f, cs, dir := newGchatHomeAdapter(t, nil)
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
	if homePatches(cs) != 1 || len(relayCalls(f, gchatMessageCreate)) != 1 {
		t.Fatalf("the first mention: patches %d, posts %d; want one each", homePatches(cs), len(relayCalls(f, gchatMessageCreate)))
	}

	// The window: the record names spaces/AAA, the file is still empty.
	gchatHomeMention(t, a, gchatSpaceMention("spaces/BBB", "T2", "M2", "and here"), true)
	if data := homeRecordData(t, cs); data[homeRecordKeyGchat] != "spaces/AAA" || homePatches(cs) != 1 || len(relayCalls(f, gchatMessageCreate)) != 1 {
		t.Fatalf("a mention in the window: record %v, patches %d, posts %d; want the record adopted, nothing written or posted",
			data, homePatches(cs), len(relayCalls(f, gchatMessageCreate)))
	}

	// The file followed.
	writeGchatHomeFile(t, dir, "spaces/AAA\n")
	checks := len(relayCalls(f, gchatSpacesGet))
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T3", "M3", "again"), true)
	if got := len(relayCalls(f, gchatSpacesGet)); got != checks {
		t.Errorf("a mention in the home made %d checks, want none", got-checks)
	}
	gchatHomeMention(t, a, gchatSpaceMention("spaces/CCC", "T4", "M4", "elsewhere"), true)
	gets := relayCalls(f, gchatSpacesGet)
	if len(gets) != checks+1 || gets[len(gets)-1].arguments["name"] != "spaces/AAA" {
		t.Errorf("a mention outside the home: checks %+v, want one more, of the home", gets[checks:])
	}
	if data := homeRecordData(t, cs); data[homeRecordKeyGchat] != "spaces/AAA" || homePatches(cs) != 1 || len(relayCalls(f, gchatMessageCreate)) != 1 {
		t.Errorf("after later mentions: record %v, patches %d, posts %d; want the first learn alone", data, homePatches(cs), len(relayCalls(f, gchatMessageCreate)))
	}
}

// TestGchatHomeAdoptsAWinnerThatWroteFirst: a write the record changed under
// (409) reads again; a space another writer recorded is kept after the same
// check, nothing else is written, and no line is posted for it.
func TestGchatHomeAdoptsAWinnerThatWroteFirst(t *testing.T) {
	a, f, cs, _ := newGchatHomeAdapter(t, nil)
	conflicts := 0
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		var body struct {
			Data map[string]*string `json:"data"`
		}
		if err := json.Unmarshal(action.(k8stesting.PatchAction).GetPatch(), &body); err != nil {
			return true, nil, err
		}
		if _, learn := body.Data[homeRecordKeyGchat]; !learn || conflicts > 0 {
			return false, nil, nil
		}
		conflicts++
		obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
		if err != nil {
			return true, nil, err
		}
		cm := obj.(*corev1.ConfigMap)
		cm.Data = map[string]string{homeRecordKeyGchat: "spaces/WIN", homeRecordKeyWriter: homeTestOldImage}
		if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
			return true, nil, err
		}
		return true, nil, apierrors.NewConflict(configMapsGVR.GroupResource(), homeTestRecord, errors.New("the record changed"))
	})
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeyGchat] != "spaces/WIN" || data[homeRecordKeyWriter] != homeTestOldImage || data[homeRecordKeyGchatSetBy] != "" {
		t.Errorf("record = %v, want the winner kept as written", data)
	}
	if conflicts != 1 || homePatches(cs) != 1 {
		t.Errorf("conflicts %d, patches %d; want one conflict and no write after it", conflicts, homePatches(cs))
	}
	if posts := relayCalls(f, gchatMessageCreate); len(posts) != 0 {
		t.Errorf("posts = %+v, want none for an adopted space", posts)
	}
}

// TestGchatHomeConflictFromSlacksLearnIsRetried: both backends write the one
// record. A 409 caused by Slack's learn landing first leaves Chat's key
// empty, so the second attempt writes it, and Slack's keys stand.
func TestGchatHomeConflictFromSlacksLearnIsRetried(t *testing.T) {
	a, f, cs, _ := newGchatHomeAdapter(t, nil)
	conflicts := 0
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if conflicts > 0 {
			return false, nil, nil
		}
		conflicts++
		obj, err := cs.Tracker().Get(configMapsGVR, homeTestNamespace, homeTestRecord)
		if err != nil {
			return true, nil, err
		}
		cm := obj.(*corev1.ConfigMap)
		cm.Data = map[string]string{homeRecordKeySlack: "C0AA1", homeRecordKeySetBy: "U1", homeRecordKeyRefused: homeRefusedArchived}
		if err := cs.Tracker().Update(configMapsGVR, cm, homeTestNamespace); err != nil {
			return true, nil, err
		}
		return true, nil, apierrors.NewConflict(configMapsGVR.GroupResource(), homeTestRecord, errors.New("the record changed"))
	})
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
	data := homeRecordData(t, cs)
	if data[homeRecordKeyGchat] != "spaces/AAA" || data[homeRecordKeySlack] != "C0AA1" || data[homeRecordKeySetBy] != "U1" || data[homeRecordKeyRefused] != homeRefusedArchived {
		t.Errorf("record = %v, want Chat's space beside Slack's keys untouched", data)
	}
	if len(relayCalls(f, gchatMessageCreate)) != 1 {
		t.Errorf("the line was not posted once after the retry")
	}
}

// TestGchatHomeRefusedCandidates: what spaces.get says of a candidate. A
// definite refusal is written as its word and nothing is posted; a check
// that cannot be made writes nothing.
func TestGchatHomeRefusedCandidates(t *testing.T) {
	cases := []struct {
		name   string
		answer any
		reason string
	}{
		{"the app is not in the space", fakeChatAPIError{status: http.StatusNotFound, reason: "Not Found"}, homeRefusedNotMember},
		{"the app is forbidden the space", fakeChatAPIError{status: http.StatusForbidden, reason: "Forbidden"}, homeRefusedNotMember},
		{"people outside the organization are admitted", map[string]any{"name": "spaces/AAA", "spaceType": "SPACE", "externalUserAllowed": true}, homeRefusedShared},
		{"the name is a direct message", map[string]any{"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}, homeRefusedDirectMessage},
		{"the name is a group chat", map[string]any{"name": "spaces/AAA", "spaceType": "GROUP_CHAT"}, homeRefusedDirectMessage},
		{"Chat is unavailable", fakeChatAPIError{status: http.StatusServiceUnavailable, reason: "Unavailable"}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, f, cs, _ := newGchatHomeAdapter(t, nil)
			f.responses[gchatSpacesGet] = tc.answer
			gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
			data := homeRecordData(t, cs)
			if data[homeRecordKeyGchat] != "" || data[homeRecordKeyGchatRefused] != tc.reason || len(relayCalls(f, gchatMessageCreate)) != 0 {
				t.Errorf("record = %v, posts %d; want gchat.refused=%q, no space, no line", data, len(relayCalls(f, gchatMessageCreate)), tc.reason)
			}
			// The next space that passes is learned and clears the refusal.
			f.responses[gchatSpacesGet] = gchatNamedSpace("spaces/BBB")
			gchatHomeMention(t, a, gchatSpaceMention("spaces/BBB", "T2", "M2", "here then"), true)
			data = homeRecordData(t, cs)
			if _, stale := data[homeRecordKeyGchatRefused]; stale || data[homeRecordKeyGchat] != "spaces/BBB" {
				t.Errorf("record after the next mention = %v, want gchat=spaces/BBB and no refusal", data)
			}
		})
	}
}

func TestGchatHomeCheckRefusesAMalformedNameWithoutACall(t *testing.T) {
	a, f, _, _ := newGchatHomeAdapter(t, nil)
	for _, id := range []string{"", "general", "spaces/", "spaces/AAA/threads/T1", "spaces/" + strings.Repeat("A", gchatHomeSpaceMax)} {
		if refused, err := a.checkHomeSpace(context.Background(), id); refused != homeRefusedMalformed || err != nil {
			t.Errorf("checkHomeSpace(%q) = (%q, %v), want malformed", id, refused, err)
		}
	}
	if gets := relayCalls(f, gchatSpacesGet); len(gets) != 0 {
		t.Errorf("a malformed name reached Chat: %+v", gets)
	}
}

// TestGchatHomeFileAbsentLearnsNothing: a projection with no Chat file (an
// operator older than Chat's learn, which renders the shared switch for
// Slack) or no directory at all learns nothing and calls nothing.
func TestGchatHomeFileAbsentLearnsNothing(t *testing.T) {
	for _, name := range []string{"the file is missing", "the directory is missing"} {
		t.Run(name, func(t *testing.T) {
			a, f, cs, dir := newGchatHomeAdapter(t, nil)
			if name == "the file is missing" {
				os.Remove(filepath.Join(dir, gchatHomeFileKey))
				writeHomeFile(t, dir, "")
			} else {
				removeHomeDir(t, dir)
			}
			gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
			if homePatches(cs) != 0 || len(relayCalls(f, gchatSpacesGet)) != 0 {
				t.Errorf("patches %d, checks %d; want none", homePatches(cs), len(relayCalls(f, gchatSpacesGet)))
			}
			if home, present := a.LearnedHome(); home != "" || present {
				t.Errorf("LearnedHome() = (%q, %v), want absent", home, present)
			}
		})
	}
}

// TestGchatHomeLearnedHomeReadsTheProjection: the notify route's home source.
func TestGchatHomeLearnedHomeReadsTheProjection(t *testing.T) {
	a, _, _, dir := newGchatHomeAdapter(t, nil)
	cases := []struct {
		value, want string
	}{
		{"", ""},
		{" spaces/HOME\n", "spaces/HOME"},
		{"general\n", ""},
		{"spaces/HOME/threads/T1\n", ""},
		{"C0AA1\n", ""},
	}
	for _, tc := range cases {
		writeGchatHomeFile(t, dir, tc.value)
		if got, present := a.LearnedHome(); got != tc.want || !present {
			t.Errorf("LearnedHome() with %q = (%q, %v), want (%q, true)", tc.value, got, present, tc.want)
		}
	}
}

// TestGchatNotifyPostsIntoTheLearnedSpace: the notify route with no CR home
// follows the adapter's home source and verifier. No home while the file
// names none; once the operator projects the learned space, a home post
// lands in it after one spaces.get, a thread of it is a thread of home, and
// a thread of another space is refused. A space Chat refuses at post time is
// written to gchat.refused and receives nothing.
func TestGchatNotifyPostsIntoTheLearnedSpace(t *testing.T) {
	a, f, cs, dir := newGchatHomeAdapter(t, nil)
	n, err := NewGchatNotifier(a, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	n.SetHomeSource(a.LearnedHome)
	n.SetHomeVerifier(a.VerifyHomeForPost)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "drift"}); got.Error != notifyNoHome {
		t.Fatalf("a home post before the learn = %+v, want %q", got, notifyNoHome)
	}

	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "hello"), true)
	writeGchatHomeFile(t, dir, homeRecordData(t, cs)[homeRecordKeyGchat]+"\n")
	f.responses[gchatMessageCreate] = map[string]any{"name": "spaces/AAA/messages/N1", "thread": map[string]any{"name": "spaces/AAA/threads/N1"}}
	before := len(relayCalls(f, gchatMessageCreate))
	checks := len(relayCalls(f, gchatSpacesGet))
	got := serveJSON(t, n, lib.NotifyRequest{Text: "drift detected"})
	if got.Error != "" {
		t.Fatalf("a home post after the learn = %+v", got)
	}
	posts := relayCalls(f, gchatMessageCreate)[before:]
	if len(posts) != 1 || posts[0].arguments["parent"] != "spaces/AAA" || posts[0].arguments["body"].(map[string]any)["text"] != "drift detected" {
		t.Fatalf("posts = %+v, want one into spaces/AAA", posts)
	}
	if got := len(relayCalls(f, gchatSpacesGet)) - checks; got != 1 {
		t.Errorf("the post made %d checks, want one", got)
	}
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "reply", Thread: "spaces/AAA/threads/N1"}); got.Error != "" {
		t.Errorf("a thread of the learned space = %+v", got)
	}
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "reply", Thread: "spaces/OTHER/threads/N1"}); got.Error == "" {
		t.Error("a thread of another space was posted")
	}

	// The app is removed from the space after the learn.
	f.responses[gchatSpacesGet] = fakeChatAPIError{status: http.StatusNotFound, reason: "Not Found"}
	before = len(relayCalls(f, gchatMessageCreate))
	got = serveJSON(t, n, lib.NotifyRequest{Text: "drift again"})
	if want := "home channel refused: " + homeRefusedNotMember; got.Error != want {
		t.Errorf("a post into a refused home = %+v, want %q", got, want)
	}
	if len(relayCalls(f, gchatMessageCreate)) != before {
		t.Error("a refused home received a post")
	}
	if data := homeRecordData(t, cs); data[homeRecordKeyGchatRefused] != homeRefusedNotMember || data[homeRecordKeyGchat] != "spaces/AAA" {
		t.Errorf("record = %v, want gchat.refused=not-member beside spaces/AAA", data)
	}
}

// TestGchatNotifyACRHomeIsNeverChecked: the CR's home is the install's word.
func TestGchatNotifyACRHomeIsNeverChecked(t *testing.T) {
	a, f, _, _ := newGchatHomeAdapter(t, nil)
	n, err := NewGchatNotifier(a, "spaces/CR", nil)
	if err != nil {
		t.Fatal(err)
	}
	n.SetHomeSource(a.LearnedHome)
	n.SetHomeVerifier(a.VerifyHomeForPost)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "drift"}); got.Error != "" {
		t.Fatalf("a home post into the CR's space = %+v", got)
	}
	if posts := relayCalls(f, gchatMessageCreate); len(posts) != 1 || posts[0].arguments["parent"] != "spaces/CR" {
		t.Errorf("posts = %+v, want one into spaces/CR", posts)
	}
	if gets := relayCalls(f, gchatSpacesGet); len(gets) != 0 {
		t.Errorf("the CR's home was checked: %+v", gets)
	}
}

func TestGchatHomeNoLearnStartsAfterShutdown(t *testing.T) {
	a, f, cs, _ := newGchatHomeAdapter(t, nil)
	a.closeHomeLearn()
	gchatHomeMention(t, a, gchatSpaceMention("spaces/AAA", "T1", "M1", "too late"), true)
	if homePatches(cs) != 0 || len(relayCalls(f, gchatSpacesGet)) != 0 {
		t.Errorf("after shutdown: patches %d, checks %d; want none", homePatches(cs), len(relayCalls(f, gchatSpacesGet)))
	}
}

// TestFromEnvGchatHomeLearn: the operator's env for a Chat-only gateway arms
// Chat's learn and not Slack's; its policy env disarms it.
func TestFromEnvGchatHomeLearn(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("A2A_GCHAT_RELAY_URL", "http://relay.ns.svc:8081")
	t.Setenv("A2A_HOME_CHANNEL_RECORD", homeTestRecord)
	t.Setenv("A2A_HOME_CHANNEL_LEARN", "true")
	t.Setenv("A2A_GCHAT_HOME_POLICY", "auto")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.GchatHomeLearnArmed() || cfg.HomeLearnArmed() {
		t.Fatalf("GchatHomeLearnArmed = %v, HomeLearnArmed = %v; want Chat's alone", cfg.GchatHomeLearnArmed(), cfg.HomeLearnArmed())
	}
	t.Setenv("A2A_GCHAT_HOME_POLICY", "manual")
	if cfg, err = FromEnv(); err != nil || cfg.GchatHomeLearnArmed() {
		t.Fatalf("policy manual: armed = %v, err = %v", cfg != nil && cfg.GchatHomeLearnArmed(), err)
	}
	t.Setenv("A2A_GCHAT_HOME_POLICY", "")
	t.Setenv("A2A_GCHAT_HOME_CHANNEL", "spaces/CR")
	if cfg, err = FromEnv(); err != nil || cfg.GchatHomeLearnArmed() {
		t.Fatalf("a CR home channel: armed = %v, err = %v", cfg != nil && cfg.GchatHomeLearnArmed(), err)
	}
}
