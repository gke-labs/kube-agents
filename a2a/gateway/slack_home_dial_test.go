package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/slack-go/slack/slackevents"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// dialBoard is the fleet fixture with a dial: U1 an admin, one kind in two
// repositories, the first with 3 of 5 clean merges.
func dialBoard(admins ...string) *lib.Board {
	b := homeBoardFixture()
	b.Dial = &lib.BoardDial{
		Enabled: true, Level: lib.DialLevelPropose, SetBy: "U1", SetAt: homeNow.Unix(), Admins: admins,
		Kinds: []lib.BoardDialKind{{Kind: "no-pdb", Title: "Disruption budgets", Environment: "dev", Repos: []lib.BoardDialRepo{
			{Repo: "org/infra", RepoKey: "github.com/org/infra", Clean: 3, Needed: 5, Days: 1, DaysNeeded: 2, State: lib.DialStateOff},
			{Repo: "org/apps", RepoKey: "github.com/org/apps", Clean: 5, Needed: 5, Days: 2, DaysNeeded: 2, Ready: true,
				State: lib.DialStatePending, PendingPR: &lib.BoardDialPR{Number: 35, URL: "https://github.com/org/apps/pull/35"}},
		}}},
	}
	return b
}

// dialBlocks is the last published view's blocks from the dial's header on,
// as Slack receives them.
func dialBlocks(t *testing.T, api *fakeSlackAPI) []map[string]any {
	t.Helper()
	if len(api.views) == 0 {
		t.Fatal("nothing was published")
	}
	raw, err := json.Marshal(api.views[len(api.views)-1].view.Blocks)
	if err != nil {
		t.Fatal(err)
	}
	var blocks []map[string]any
	if err := json.Unmarshal(raw, &blocks); err != nil {
		t.Fatal(err)
	}
	for i, b := range blocks {
		if txt, ok := b["text"].(map[string]any); ok && b["type"] == "header" && txt["text"] == slackDialTitle {
			return blocks[i:]
		}
	}
	return nil
}

// dialText is the dial's blocks as JSON text, for substring checks, with
// <, > and & as written.
func dialText(t *testing.T, blocks []map[string]any) string {
	t.Helper()
	var buf strings.Builder
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(blocks); err != nil {
		t.Fatal(err)
	}
	return buf.String()
}

func openHome(a *SlackAdapter, member string) {
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: member, Tab: "home"})
}

func TestSlackHomeDialDrawsNothingWithoutAnEnabledDial(t *testing.T) {
	for name, d := range map[string]*lib.BoardDial{"missing": nil, "off": {Enabled: false, Level: lib.DialLevelPropose, Admins: []string{"U1"}}} {
		t.Run(name, func(t *testing.T) {
			b := homeBoardFixture()
			b.Dial = d
			a, api, _ := homeAdapter(t, b, nil)
			openHome(a, "U1")
			if got := dialBlocks(t, api); got != nil {
				t.Errorf("a dial that is %s drew a panel: %v", name, got)
			}
		})
	}
}

func TestSlackHomeDialGivesAnAdminTheLevelRepoPickerAndRequest(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U1"), nil)
	openHome(a, "U1")
	blocks := dialBlocks(t, api)
	got := dialText(t, blocks)
	for _, want := range []string{
		`"type":"radio_buttons"`, `"action_id":"kage_dial.level"`,
		`"type":"static_select"`, `"action_id":"kage_dial.repo"`,
		`"value":"no-pdb|dev|github.com/org/infra"`, `"value":"no-pdb|dev|github.com/org/apps"`,
		"org/infra · 3 of 5 clean merges · off",
		`"action_id":"kage_dial.request"`, "Request auto-merge",
		"Set to Propose fixes by <@U1>",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("the admin's dial lacks %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "kage_dial.stop") {
		t.Errorf("an off repository drew Stop:\n%s", got)
	}
	// The radio starts at the stored level.
	var initial string
	for _, b := range blocks {
		if acc, ok := b["accessory"].(map[string]any); ok && acc["type"] == "radio_buttons" {
			initial, _ = acc["initial_option"].(map[string]any)["value"].(string)
		}
	}
	if initial != lib.DialLevelPropose {
		t.Errorf("the radio starts at %q, want %q", initial, lib.DialLevelPropose)
	}
}

func TestSlackHomeDialLeavesATooLongRepositoryOutOfThePicker(t *testing.T) {
	b := dialBoard("U1")
	long := "github.com/" + strings.Repeat("o", 39) + "/" + strings.Repeat("r", 100)
	b.Dial.Kinds[0].Repos = append(b.Dial.Kinds[0].Repos, lib.BoardDialRepo{Repo: "long", RepoKey: long, Needed: 5})
	a, api, _ := homeAdapter(t, b, nil)
	openHome(a, "U1")
	got := dialText(t, dialBlocks(t, api))
	if strings.Contains(got, long) || !strings.Contains(got, "static_select") {
		t.Errorf("a value over Slack's 150 characters must be left out of the picker, the rest kept:\n%s", got)
	}
}

func TestSlackHomeDialSkipsATooLongFirstRepositoryEverywhere(t *testing.T) {
	b := dialBoard("U1")
	long := "github.com/" + strings.Repeat("o", 39) + "/" + strings.Repeat("r", 100)
	rest := b.Dial.Kinds[0].Repos[:1]
	b.Dial.Kinds[0].Repos = append([]lib.BoardDialRepo{{Repo: "long", RepoKey: long, Needed: 5}}, rest...)
	a, api, _ := homeAdapter(t, b, nil)
	openHome(a, "U1")
	got := dialText(t, dialBlocks(t, api))
	if strings.Contains(got, long) || !strings.Contains(got, "org/infra · 3 of 5 clean merges · off") {
		t.Errorf("a first repository too long for Slack must be skipped in the row and the button, not only the picker:\n%s", got)
	}
}

func TestSlackHomeDialRepoPickSurvivesAnExpiredViewer(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U1"), nil)
	openHome(a, "U1")
	a.home.mu.Lock()
	a.home.viewers["U1"] = time.Now().Add(-2 * slackHomeViewerTTL)
	a.home.mu.Unlock()
	a.dialClick(dialClickPayload("U1", "4.2"), slackDialActionRepo, "", "no-pdb|dev|github.com/org/apps")
	if got := dialText(t, dialBlocks(t, api)); !strings.Contains(got, "org/apps · 5 of 5 clean merges · Pending") {
		t.Errorf("a pick made after the tab's viewer entry expired was dropped:\n%s", got)
	}
}

func TestSlackHomeDialIsReadOnlyForAMemberWhoIsNotAnAdmin(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U9"), nil)
	openHome(a, "U1")
	got := dialText(t, dialBlocks(t, api))
	if !strings.Contains(got, "*Level:* Propose fixes") || !strings.Contains(got, "org/infra · 3 of 5 clean merges · off") {
		t.Errorf("a member who is not an admin must see the level and progress:\n%s", got)
	}
	for _, control := range []string{"radio_buttons", "kage_dial.request", "kage_dial.stop", "kage_dial.level"} {
		if strings.Contains(got, control) {
			t.Errorf("a member who is not an admin was drawn %s:\n%s", control, got)
		}
	}
}

func TestSlackHomeDialLinesForEachState(t *testing.T) {
	cases := []struct {
		name string
		repo lib.BoardDialRepo
		want string
	}{
		{"partway", lib.BoardDialRepo{Clean: 3, Needed: 5, DaysNeeded: 2, State: lib.DialStateOff}, "3 of 5 clean merges · off"},
		{"capped", lib.BoardDialRepo{Clean: 8, Needed: 5, Days: 3, DaysNeeded: 2, Ready: true, State: lib.DialStateOn}, "5 of 5 clean merges · auto-merges"},
		{"short of days", lib.BoardDialRepo{Clean: 5, Needed: 5, Days: 1, DaysNeeded: 2, State: lib.DialStateOff}, "5 of 5 clean merges, on 1 of 2 separate days · off"},
		{"ready says ready", lib.BoardDialRepo{Clean: 5, Needed: 5, Days: 1, DaysNeeded: 2, Ready: true, State: lib.DialStateOff}, "5 of 5 clean merges · off"},
		{"pending", lib.BoardDialRepo{Clean: 5, Needed: 5, Days: 2, DaysNeeded: 2, Ready: true, State: lib.DialStatePending,
			PendingPR: &lib.BoardDialPR{Number: 35, URL: "https://github.com/org/apps/pull/35"}}, "Pending: <https://github.com/org/apps/pull/35|rules PR #35>"},
		{"pending with no url", lib.BoardDialRepo{Clean: 5, Needed: 5, State: lib.DialStatePending, PendingPR: &lib.BoardDialPR{Number: 35, URL: "javascript:x"}}, "Pending: rules PR"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := slackDialProgressLine(c.repo) + slackHomeDot + slackDialStateLine(c.repo); !strings.HasSuffix(got, c.want) {
				t.Errorf("got %q, want %q", got, c.want)
			}
		})
	}
}

func TestSlackHomeDialShowsTheRowsNoteUnderIt(t *testing.T) {
	b := dialBoard("U9")
	b.Dial.Kinds[0].Repos[0].Note = "The switch did not run: <!here> the token expired"
	a, api, _ := homeAdapter(t, b, nil)
	openHome(a, "U1")
	got := dialText(t, dialBlocks(t, api))
	if !strings.Contains(got, "The switch did not run: &lt;!here&gt; the token expired") {
		t.Errorf("the row's note is not drawn under it, escaped:\n%s", got)
	}
}

func TestSlackHomeDialPendingRowOffersStop(t *testing.T) {
	b := dialBoard("U1")
	b.Dial.Kinds[0].Repos = b.Dial.Kinds[0].Repos[1:]
	a, api, _ := homeAdapter(t, b, nil)
	openHome(a, "U1")
	got := dialText(t, dialBlocks(t, api))
	if !strings.Contains(got, `"action_id":"kage_dial.stop"`) || !strings.Contains(got, `"style":"danger"`) ||
		strings.Contains(got, "kage_dial.request") || strings.Contains(got, "static_select") {
		t.Errorf("a single pending repository must draw Stop and no picker:\n%s", got)
	}
}

// dialActor records the actions sent and answers with res or err.
type dialActor struct {
	sent []lib.BoardAction
	res  *lib.BoardActionResult
	err  error
}

func (d *dialActor) act(_ context.Context, a lib.BoardAction) (*lib.BoardActionResult, error) {
	d.sent = append(d.sent, a)
	return d.res, d.err
}

func dialClickPayload(member, actionTS string) slackClickPayload {
	var p slackClickPayload
	p.Type = slackBlockActions
	p.User.ID, p.User.TeamID = member, "T1"
	p.Container.Type = slackContainerView
	p.ActionTS = actionTS
	return p
}

func TestSlackHomeDialLevelTapGoesOverTheBoardRouteAndShowsTheAnswer(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U1"), nil)
	actor := &dialActor{res: &lib.BoardActionResult{OK: true, Message: "Set to Auto-merge proven fixes."}}
	a.SetBoardActor(actor.act)
	a.dialClick(dialClickPayload("U1", "1.1"), slackDialActionLevel, "", lib.DialLevelAutomerge)
	if len(actor.sent) != 1 {
		t.Fatalf("sent %d actions, want 1", len(actor.sent))
	}
	if want := (lib.BoardAction{Action: lib.DialActionLevel, Level: lib.DialLevelAutomerge, User: "U1", By: actor.sent[0].By}); !boardActionEqual(actor.sent[0], want) {
		t.Errorf("sent %+v, want %+v", actor.sent[0], want)
	}
	if got := dialText(t, dialBlocks(t, api)); !strings.Contains(got, "Set to Auto-merge proven fixes.") {
		t.Errorf("the answer is not drawn under the dial:\n%s", got)
	}
	// The same tap redelivered is dropped.
	a.dialClick(dialClickPayload("U1", "1.1"), slackDialActionLevel, "", lib.DialLevelAutomerge)
	if len(actor.sent) != 1 {
		t.Errorf("a redelivered tap was sent again")
	}
}

func TestSlackHomeDialRequestAndStopNameTheirRow(t *testing.T) {
	a, _, _ := homeAdapter(t, dialBoard("U1"), nil)
	actor := &dialActor{res: &lib.BoardActionResult{OK: true}}
	a.SetBoardActor(actor.act)
	a.dialClick(dialClickPayload("U1", "2.1"), slackDialActionReq, "no-pdb|dev|github.com/org/infra", "")
	a.dialClick(dialClickPayload("U1", "2.2"), slackDialActionStop, "no-pdb|dev|github.com/org/apps", "")
	want := []lib.BoardAction{
		{Action: lib.DialActionRequest, Kind: "no-pdb", Environment: "dev", RepoKey: "github.com/org/infra", User: "U1"},
		{Action: lib.DialActionStop, Kind: "no-pdb", Environment: "dev", RepoKey: "github.com/org/apps", User: "U1"},
	}
	if len(actor.sent) != len(want) {
		t.Fatalf("sent %d actions, want %d", len(actor.sent), len(want))
	}
	for i := range want {
		want[i].By = actor.sent[i].By
		if !boardActionEqual(actor.sent[i], want[i]) {
			t.Errorf("action %d: sent %+v, want %+v", i, actor.sent[i], want[i])
		}
	}
}

func TestSlackHomeDialAFailedTapAsksToTryAgain(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U1"), nil)
	a.SetBoardActor((&dialActor{err: errors.New("no responders")}).act)
	a.dialClick(dialClickPayload("U1", "3.1"), slackDialActionReq, "no-pdb|dev|github.com/org/infra", "")
	if got := dialText(t, dialBlocks(t, api)); !strings.Contains(got, slackDialFailed) {
		t.Errorf("a tap that got no answer must say so:\n%s", got)
	}
	if got := slackDialAnswer(&lib.BoardActionResult{Message: "Only an install admin can change the dial.\nmore"}, nil); got != "Only an install admin can change the dial." {
		t.Errorf("session-kv's refusal is drawn as %q", got)
	}
	if got := slackDialAnswer(&lib.BoardActionResult{Message: "<!channel> & co"}, nil); strings.Contains(got, "<!channel>") {
		t.Errorf("session-kv's message must be escaped: %q", got)
	}
}

func TestSlackHomeDialRepoPickRedrawsOnlyThePressersView(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U1"), nil)
	actor := &dialActor{}
	a.SetBoardActor(actor.act)
	a.dialClick(dialClickPayload("U1", "4.1"), slackDialActionRepo, "", "no-pdb|dev|github.com/org/apps")
	if len(actor.sent) != 0 {
		t.Errorf("a repo pick sent %d actions; it changes only the view", len(actor.sent))
	}
	got := dialText(t, dialBlocks(t, api))
	if !strings.Contains(got, "org/apps · 5 of 5 clean merges · Pending") || !strings.Contains(got, "kage_dial.stop") {
		t.Errorf("the picked repository is not the one drawn:\n%s", got)
	}
	if a.dialPick("U2", "no-pdb", "dev") != "" {
		t.Errorf("one member's pick leaked to another")
	}
}

func TestSlackHomeDialRefusesATapFromAMemberWhoIsNotAnAdmin(t *testing.T) {
	a, api, _ := homeAdapter(t, dialBoard("U9"), nil)
	actor := &dialActor{res: &lib.BoardActionResult{OK: true}}
	a.SetBoardActor(actor.act)
	a.dialClick(dialClickPayload("U1", "5.1"), slackDialActionLevel, "", lib.DialLevelExplain)
	if len(actor.sent) != 0 {
		t.Errorf("a tap from a member who is not an admin was sent")
	}
	if got := dialText(t, dialBlocks(t, api)); !strings.Contains(got, slackDialNotAdmin) {
		t.Errorf("the refusal is not drawn:\n%s", got)
	}
}

func TestSlackHomeDialDropsTapsThatAreNotHomeTabActions(t *testing.T) {
	a, _, _ := homeAdapter(t, dialBoard("U1"), nil)
	actor := &dialActor{res: &lib.BoardActionResult{OK: true}}
	a.SetBoardActor(actor.act)
	inChannel := dialClickPayload("U1", "6.1")
	inChannel.Channel.ID = "C1"
	a.dialClick(inChannel, slackDialActionLevel, "", lib.DialLevelExplain)
	a.dialClick(dialClickPayload("U1", "6.2"), slackDialActionLevel, "", "everything")
	a.dialClick(dialClickPayload("U1", "6.3"), slackDialActionReq, "no-pdb", "")
	if len(actor.sent) != 0 {
		t.Errorf("sent %d actions for taps the dial did not draw: %+v", len(actor.sent), actor.sent)
	}
}

func TestSlackHomeDialRowValueRoundTrips(t *testing.T) {
	value := slackDialRowValue("no-pdb", "dev", "github.com/org/infra")
	kind, env, key, ok := slackDialRow(value)
	if !ok || kind != "no-pdb" || env != "dev" || key != "github.com/org/infra" {
		t.Errorf("slackDialRow(%q) = %q %q %q %v", value, kind, env, key, ok)
	}
	if _, _, _, ok := slackDialRow("a|b"); ok {
		t.Error("a two-part value must not read as a row")
	}
}

func boardActionEqual(a, b lib.BoardAction) bool {
	ja, _ := json.Marshal(a)
	jb, _ := json.Marshal(b)
	return string(ja) == string(jb)
}
