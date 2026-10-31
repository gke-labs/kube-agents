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
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// newHomePickAdapter is the learner fixture (the record, the projected file)
// with the Home tab armed: U1 is listed, UX is admitted by allow-all from
// another workspace, U2 is not admitted. The follow-up wait is short.
func newHomePickAdapter(t *testing.T, data map[string]string) (*SlackAdapter, *fakeSlackAPI, *k8sfake.Clientset, string) {
	t.Helper()
	a, api, cs, dir := newHomeLearnAdapter(t, data)
	api.users = map[string]*slack.User{"U1": {ID: "U1", TeamID: "T1"}, "U2": {ID: "U2", TeamID: "T1"}, "UX": {ID: "UX", TeamID: "T9"}}
	a.botUserID = "UBOT"
	a.homeLearn.followUp = 20 * time.Millisecond
	a.SetHome(func(m string) (bool, bool) { return m == "U1" || m == "UX", m == "UX" },
		func(context.Context) (*lib.Board, error) { return &lib.Board{}, nil })
	return a, api, cs, dir
}

// homePickPayload is a Home tab pick as Slack sends it: a block_actions
// payload with a view container, no channel and no message.
func homePickPayload(t *testing.T, user, team, channel, actionTS string) json.RawMessage {
	t.Helper()
	return homePickPayloadIn(t, user, team, channel, actionTS, slackContainerView, "")
}

// homePickPayloadIn is homePickPayload with the container type and the
// message channel chosen: a message click carrying the picker's action id.
func homePickPayloadIn(t *testing.T, user, team, channel, actionTS, container, msgChannel string) json.RawMessage {
	t.Helper()
	payload := map[string]any{
		"type":      "block_actions",
		"user":      map[string]any{"id": user, "team_id": team},
		"team":      map[string]any{"id": "T1"},
		"container": map[string]any{"type": container, "view_id": "V1"},
		"view":      map[string]any{"id": "V1", "type": "home"},
		"actions": []any{map[string]any{"type": "conversations_select", "action_id": slackHomePickAction,
			"block_id": "home", "selected_conversation": channel, "action_ts": actionTS}},
	}
	if msgChannel != "" {
		payload["channel"] = map[string]any{"id": msgChannel}
		payload["message"] = map[string]any{"ts": "50.0"}
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func pick(t *testing.T, a *SlackAdapter, user, team, channel, actionTS string) {
	t.Helper()
	if _, ok := a.interactive(homePickPayload(t, user, team, channel, actionTS)); ok {
		t.Fatal("a home pick became a turn")
	}
	a.homeLearn.wg.Wait()
}

// lastView is the text of the last view published to member.
func lastView(t *testing.T, api *fakeSlackAPI, member string) string {
	t.Helper()
	api.mu.Lock()
	defer api.mu.Unlock()
	for i := len(api.views) - 1; i >= 0; i-- {
		if api.views[i].member == member {
			return viewText(t, api.views[i].view)
		}
	}
	return ""
}

// anyView reports whether any view published to member carries text.
func anyView(t *testing.T, api *fakeSlackAPI, member, text string) bool {
	t.Helper()
	api.mu.Lock()
	defer api.mu.Unlock()
	for _, v := range api.views {
		if v.member == member && strings.Contains(viewText(t, v.view), text) {
			return true
		}
	}
	return false
}

func viewsFor(api *fakeSlackAPI, member string) int {
	api.mu.Lock()
	defer api.mu.Unlock()
	n := 0
	for _, v := range api.views {
		if v.member == member {
			n++
		}
	}
	return n
}

// TestSlackHomePickViewStates (V1): the picker and the line naming the home
// in every armed state; one fixed line when the learner is not armed; the
// denied view unchanged.
func TestSlackHomePickViewStates(t *testing.T) {
	a, api, _, dir := newHomePickAdapter(t, nil)
	a.publishHome("U1", false)
	text := lastView(t, api, "U1")
	if !strings.Contains(text, slackHomeTitleHome) || !strings.Contains(text, "[select "+slackHomePickPlaceholder+" "+slackHomePickAction+"]") {
		t.Fatalf("no picker in the armed view:\n%s", text)
	}
	if !strings.Contains(text, slackHomeLineNoHome) {
		t.Errorf("no-home line missing:\n%s", text)
	}
	writeHomeFile(t, dir, "C0HOME\n")
	a.publishHome("U1", true)
	if text := lastView(t, api, "U1"); !strings.Contains(text, "Home channel: <#C0HOME>") || strings.Contains(text, slackHomeLineNoHome) {
		t.Errorf("the line does not name the projected home:\n%s", text)
	}
	a.publishHome("U2", false)
	if text := lastView(t, api, "U2"); !strings.Contains(text, slackHomeDenied) || strings.Contains(text, slackHomePickAction) {
		t.Errorf("the denied view changed:\n%s", text)
	}
	// The production wait for the file is the pending TTL; the fixture
	// shortens it after SetHomeLearner.
	if d, _, _, _ := newHomeLearnAdapter(t, nil); d.homeLearn.followUp != slackHomePickPendingTTL {
		t.Errorf("followUp default = %s, want %s", d.homeLearn.followUp, slackHomePickPendingTTL)
	}
	// Not armed: no record client (the CR field set, policy manual, or an
	// operator older than the projection all leave it nil).
	b, bapi, _ := homeAdapter(t, homeBoardFixture(), nil)
	b.publishHome("U1", false)
	if text := lastView(t, bapi, "U1"); strings.Contains(text, slackHomePickAction) || !strings.Contains(text, slackHomeLineNotArmed) {
		t.Errorf("the unarmed view draws a picker or lacks the fixed line:\n%s", text)
	}
}

// TestSlackHomePickMovesTheHome (S1): a passing pick writes the record once,
// publishes "Checking" then the outcome to the member, posts the two lines,
// and the view shows the new channel before the file catches up (E7).
func TestSlackHomePickMovesTheHome(t *testing.T) {
	a, api, cs, dir := newHomePickAdapter(t, map[string]string{homeRecordKeySlack: "C0OLD1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage, homeRecordKeyRefused: homeRefusedArchived})
	writeHomeFile(t, dir, "C0OLD1\n")
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	a.publishHome("U1", false)
	a.publishHome("UX", false)
	before := viewsFor(api, "U1")
	pick(t, a, "U1", "T1", "C0NEW2", "200.1")
	data := homeRecordData(t, cs)
	if data[homeRecordKeySlack] != "C0NEW2" || data[homeRecordKeySetBy] != "U1" || data[homeRecordKeyWriter] != homeTestImage || homePatches(cs) != 1 {
		t.Fatalf("record after the pick = %v (patches %d), want C0NEW2 by U1 in one write", data, homePatches(cs))
	}
	if _, stale := data[homeRecordKeyRefused]; stale {
		t.Errorf("the move kept a stale refusal: %v", data)
	}
	if got := viewsFor(api, "U1") - before; got < 2 || !anyView(t, api, "U1", "Checking <#C0NEW2>") {
		t.Errorf("views to the picker = %d, want the Checking publish and the outcome", got)
	}
	a.home.mu.Lock()
	armed := a.home.timer != nil
	a.home.mu.Unlock()
	if !armed {
		t.Error("no republish armed for the other viewers")
	}
	if !anyView(t, api, "U1", "Recorded <#C0NEW2> as the home channel") || !anyView(t, api, "U1", "may still land in <#C0OLD1>") {
		t.Errorf("outcome status missing from the picker's views")
	}
	// The file never caught up within the test's follow-up: the last view
	// says so, and (E7) still shows the move over the lagging file.
	text := lastView(t, api, "U1")
	if !strings.Contains(text, slackHomePickNotApplied) || !strings.Contains(text, "Home channel: <#C0NEW2>") {
		t.Errorf("the last view lacks the not-applied status or the pending move:\n%s", text)
	}
	a.publishHome("UX", true)
	if text := lastView(t, api, "UX"); !strings.Contains(text, "Home channel: <#C0NEW2>") {
		t.Errorf("a second viewer does not see the move:\n%s", text)
	}
	if len(api.posted) != 2 || api.posted[0].channel != "C0NEW2" || !strings.Contains(api.posted[0].text, "set by <@U1> from the app's Home tab") ||
		api.posted[1].channel != "C0OLD1" || !strings.Contains(api.posted[1].text, "moved to <#C0NEW2> by <@U1>") {
		t.Errorf("posts = %+v, want the new-channel line and the old-home line", api.posted)
	}
	// The file catches up: the file is the source again, and nothing is
	// pending.
	writeHomeFile(t, dir, "C0NEW2\n")
	a.publishHome("U1", true)
	a.home.mu.Lock()
	pending := a.home.pending
	a.home.mu.Unlock()
	if pending != nil {
		t.Errorf("pending move kept after the file caught up: %+v", pending)
	}
}

// TestSlackHomePickNotApplied (X1 half): the file never follows; the status
// says so after the follow-up wait.
func TestSlackHomePickNotApplied(t *testing.T) {
	a, api, _, dir := newHomePickAdapter(t, nil)
	writeHomeFile(t, dir, "")
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	pick(t, a, "U1", "T1", "C0NEW2", "201.1")
	if text := lastView(t, api, "U1"); !strings.Contains(text, slackHomePickNotApplied) {
		t.Errorf("no not-applied status:\n%s", text)
	}
}

// TestSlackHomePickRefusals (E1, E4, E5, R1, the busy gate E3): no write and
// the right text, or nothing published, per case.
func TestSlackHomePickRefusals(t *testing.T) {
	cases := []struct {
		name          string
		user, team    string
		channel       string
		setup         func(a *SlackAdapter, api *fakeSlackAPI)
		wantStatus    string
		wantViews     bool
		wantInfoCalls int
	}{
		{"a foreign team id with a listed id (E1)", "U1", "T9", "C0NEW2", nil, slackHomePickGuest, true, 0},
		{"a member not admitted", "U2", "T1", "C0NEW2", nil, slackHomeDenied, true, 0},
		{"admission undecided (E5)", "UX", "T1", "C0NEW2", func(a *SlackAdapter, api *fakeSlackAPI) { delete(api.users, "UX") }, "", false, 0},
		{"the recorded channel (E4)", "U1", "T1", "C0OLD1", nil, "already the home channel", true, 1},
		{"an archived channel", "U1", "T1", "C0ARCH", func(a *SlackAdapter, api *fakeSlackAPI) {
			api.conversations["C0ARCH"] = homeChannelInfo("C0ARCH", func(c *slack.Channel) { c.IsArchived = true })
		}, "<#C0ARCH> is archived", true, 1},
		{"the app not a member", "U1", "T1", "C0OUT1", func(a *SlackAdapter, api *fakeSlackAPI) {
			api.conversations["C0OUT1"] = homeChannelInfo("C0OUT1", func(c *slack.Channel) { c.IsMember = false })
		}, "/invite <@UBOT>", true, 1},
		{"a Slack Connect channel", "U1", "T1", "C0SHR1", func(a *SlackAdapter, api *fakeSlackAPI) {
			api.conversations["C0SHR1"] = homeChannelInfo("C0SHR1", func(c *slack.Channel) { c.IsExtShared = true })
		}, slackHomePickShared, true, 1},
		{"a direct message id", "U1", "T1", "D0DM1", nil, slackHomePickMalformed, true, 0},
		{"a group direct message (A1)", "U1", "T1", "G0MPIM", func(a *SlackAdapter, api *fakeSlackAPI) {
			api.conversations["G0MPIM"] = homeChannelInfo("G0MPIM", func(c *slack.Channel) { c.IsMpIM = true })
		}, slackHomePickMalformed, true, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a, api, cs, dir := newHomePickAdapter(t, map[string]string{homeRecordKeySlack: "C0OLD1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage})
			writeHomeFile(t, dir, "C0OLD1\n")
			api.conversations["C0OLD1"] = homeChannelInfo("C0OLD1", nil)
			if tc.setup != nil {
				tc.setup(a, api)
			}
			pick(t, a, tc.user, tc.team, tc.channel, "202.1")
			if homePatches(cs) != 0 || len(api.posted) != 0 {
				t.Fatalf("patches %d, posts %d; want none", homePatches(cs), len(api.posted))
			}
			if api.conversationInfoCalls != tc.wantInfoCalls {
				t.Errorf("conversations.info calls = %d, want %d", api.conversationInfoCalls, tc.wantInfoCalls)
			}
			text := lastView(t, api, tc.user)
			if !tc.wantViews && text != "" {
				t.Fatalf("a view was published: %s", text)
			}
			if tc.wantViews && !strings.Contains(text, tc.wantStatus) {
				t.Errorf("view = %s, want %q", text, tc.wantStatus)
			}
		})
	}
}

// TestSlackHomePickConflictAndBusy (E2, E3): a 409 writes once and names the
// winner; a second pick while one runs writes nothing and says so.
func TestSlackHomePickConflictAndBusy(t *testing.T) {
	a, api, cs, dir := newHomePickAdapter(t, map[string]string{homeRecordKeySlack: "C0OLD1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage})
	writeHomeFile(t, dir, "C0OLD1\n")
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	var once sync.Once
	cs.PrependReactor("patch", "configmaps", func(action k8stesting.Action) (bool, runtime.Object, error) {
		conflicted := false
		once.Do(func() {
			conflicted = true
			cm, _ := cs.Tracker().Get(schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}, homeTestNamespace, homeTestRecord)
			obj := cm.(*corev1.ConfigMap)
			obj.Data = map[string]string{homeRecordKeySlack: "C0WIN3", homeRecordKeySetBy: "U7"}
			obj.ResourceVersion = "9"
			_ = cs.Tracker().Update(schema.GroupVersionResource{Version: "v1", Resource: "configmaps"}, obj, homeTestNamespace)
		})
		if conflicted {
			return true, nil, apierrors.NewConflict(schema.GroupResource{Resource: "configmaps"}, homeTestRecord, errors.New("changed"))
		}
		return false, nil, nil
	})
	pick(t, a, "U1", "T1", "C0NEW2", "203.1")
	if data := homeRecordData(t, cs); data[homeRecordKeySlack] != "C0WIN3" {
		t.Fatalf("the loser overwrote the winner: %v", data)
	}
	if text := lastView(t, api, "U1"); !strings.Contains(text, "Moved to <#C0WIN3> by <@U7>") {
		t.Errorf("conflict status missing:\n%s", text)
	}
	// Busy: a learn-shaped hold on the gate while a pick arrives.
	a.homeLearn.mu.Lock()
	a.homeLearn.busy = true
	a.homeLearn.mu.Unlock()
	before := homePatches(cs)
	pick(t, a, "U1", "T1", "C0NEW2", "204.1")
	a.homeLearn.mu.Lock()
	a.homeLearn.busy = false
	a.homeLearn.mu.Unlock()
	if homePatches(cs) != before || !strings.Contains(lastView(t, api, "U1"), slackHomePickBusy) {
		t.Errorf("a pick during a change wrote or said nothing: patches %d->%d, view %s", before, homePatches(cs), lastView(t, api, "U1"))
	}
}

// TestSlackHomePickAPIDownAndStaleRefusal (X1, Low): the API server not
// answering moves nothing and says so; a pick of the recorded channel beside
// a stale refusal clears the refusal and writes nothing else.
func TestSlackHomePickAPIDownAndStaleRefusal(t *testing.T) {
	a, api, cs, dir := newHomePickAdapter(t, map[string]string{homeRecordKeySlack: "C0OLD1", homeRecordKeySetBy: "U0", homeRecordKeyWriter: homeTestImage, homeRecordKeyRefused: homeRefusedArchived})
	writeHomeFile(t, dir, "")
	api.conversations["C0OLD1"] = homeChannelInfo("C0OLD1", nil)
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	down := true
	cs.PrependReactor("get", "configmaps", func(k8stesting.Action) (bool, runtime.Object, error) {
		if down {
			return true, nil, errors.New("the API server did not answer")
		}
		return false, nil, nil
	})
	pick(t, a, "U1", "T1", "C0NEW2", "209.1")
	if homePatches(cs) != 0 || !strings.Contains(lastView(t, api, "U1"), slackHomePickNoAPI) || len(api.posted) != 0 {
		t.Fatalf("API down: patches %d, posts %d, view %s", homePatches(cs), len(api.posted), lastView(t, api, "U1"))
	}
	down = false
	pick(t, a, "U1", "T1", "C0OLD1", "210.1")
	data := homeRecordData(t, cs)
	if _, stale := data[homeRecordKeyRefused]; stale || data[homeRecordKeySlack] != "C0OLD1" || homePatches(cs) != 1 {
		t.Errorf("same-channel pick: record %v, patches %d; want the refusal cleared in one write", data, homePatches(cs))
	}
	if !strings.Contains(lastView(t, api, "U1"), "already the home channel") {
		t.Errorf("same-channel status missing: %s", lastView(t, api, "U1"))
	}
}

// TestSlackHomePickDedupeAndShutdown (R2, E6): a redelivered action is one
// pick; nothing starts after the learner closed.
func TestSlackHomePickDedupeAndShutdown(t *testing.T) {
	a, api, cs, dir := newHomePickAdapter(t, nil)
	writeHomeFile(t, dir, "")
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	api.conversations["C0THR3"] = homeChannelInfo("C0THR3", nil)
	pick(t, a, "U1", "T1", "C0NEW2", "205.1")
	pick(t, a, "U1", "T1", "C0NEW2", "205.1")
	if homePatches(cs) != 1 || api.conversationInfoCalls != 1 {
		t.Fatalf("a redelivered pick ran again: patches %d, conversations.info %d", homePatches(cs), api.conversationInfoCalls)
	}
	a.closeHomeLearn()
	pick(t, a, "U1", "T1", "C0THR3", "206.1")
	if homePatches(cs) != 1 || api.conversationInfoCalls != 1 {
		t.Errorf("a pick after close ran: patches %d, conversations.info %d", homePatches(cs), api.conversationInfoCalls)
	}
}

// TestSlackHomePickNotAHomeTabAction: a message click with the picker's
// action id, or a pick with no channel, is dropped before any call.
func TestSlackHomePickNotAHomeTabAction(t *testing.T) {
	a, api, cs, _ := newHomePickAdapter(t, nil)
	api.conversations["C0NEW2"] = homeChannelInfo("C0NEW2", nil)
	a.interactive(homePickPayloadIn(t, "U1", "T1", "C0NEW2", "207.1", "message", "C1"))
	a.homeLearn.wg.Wait()
	pick(t, a, "U1", "T1", "", "208.1")
	if homePatches(cs) != 0 || api.conversationInfoCalls != 0 || len(api.views) != 0 {
		t.Errorf("a non-view action did work: patches %d, info %d, views %d", homePatches(cs), api.conversationInfoCalls, len(api.views))
	}
}
