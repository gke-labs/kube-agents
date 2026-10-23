package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

var homeNow = time.Date(2026, 10, 16, 13, 2, 0, 0, time.UTC)

func homeBoardFixture() *lib.Board {
	return &lib.Board{
		GeneratedAt: homeNow,
		Clusters: []lib.BoardCluster{
			{Name: "seeded-a", Findings: 7, Severity: "critical", Top: "payments-api crashlooping (OOM) in seeded-debug"},
			{Name: "seeded-b", Findings: 5, Severity: "major", Top: "control plane one minor behind REGULAR"},
			{Name: "seeded-c", Findings: 0},
		},
		Running: []lib.BoardCard{{Title: "Diagnose the payments-api crashloop", Cluster: "seeded-a", StartedAt: homeNow}},
		PRs: []lib.BoardPR{{URL: "https://github.com/acme/fleet-config/pull/41", Number: 41,
			Title: "Raise payments-api memory limit to 512Mi", OpenedAt: homeNow}},
		Audit: &lib.BoardAudit{Name: "Security & RBAC audit", Findings: 7, Critical: 2,
			IssueURL: "https://github.com/acme/fleet-config/issues/231", Issue: 231, FinishedAt: homeNow},
	}
}

// homeAdapter is a UX adapter with the Home tab armed: U1 and UX are on the
// allowlist, UX in another workspace; reads counts board reads.
func homeAdapter(t *testing.T, board *lib.Board, boardErr error) (*SlackAdapter, *fakeSlackAPI, *int) {
	t.Helper()
	api := &fakeSlackAPI{users: map[string]*slack.User{
		"U1": {ID: "U1", TeamID: "T1"}, "U2": {ID: "U2", TeamID: "T1"}, "UX": {ID: "UX", TeamID: "T9"}}}
	a, _ := newUXSlackAdapter(api)
	a.workspaceURL = "https://acme.slack.com/"
	var mu sync.Mutex
	reads := 0
	// U1 is listed; UX is admitted by allow-all only, so its workspace counts.
	a.SetHome(func(m string) (bool, bool) { return m == "U1" || m == "UX", m == "UX" }, func(context.Context) (*lib.Board, error) {
		mu.Lock()
		defer mu.Unlock()
		reads++
		return board, boardErr
	})
	return a, api, &reads
}

// viewText flattens a published view into one line per block's text.
func viewText(t *testing.T, v slack.HomeTabViewRequest) string {
	t.Helper()
	raw, err := json.Marshal(v.Blocks)
	if err != nil {
		t.Fatal(err)
	}
	var blocks []map[string]any
	if err := json.Unmarshal(raw, &blocks); err != nil {
		t.Fatal(err)
	}
	var lines []string
	for _, b := range blocks {
		line := b["type"].(string) + ":"
		if txt, ok := b["text"].(map[string]any); ok {
			line += " " + txt["text"].(string)
		}
		if els, ok := b["elements"].([]any); ok {
			for _, e := range els {
				line += " " + e.(map[string]any)["text"].(string)
			}
		}
		if acc, ok := b["accessory"].(map[string]any); ok {
			line += " [" + acc["text"].(map[string]any)["text"].(string) + " " + acc["url"].(string) + "]"
		}
		lines = append(lines, line)
	}
	return strings.Join(lines, "\n")
}

func TestSlackHomeCountsTheWeeksRatingsWhenFeedbackIsOn(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if got := viewText(t, api.views[0].view); strings.Contains(got, "This week") {
		t.Errorf("feedback off: the tab drew a ratings line:\n%s", got)
	}
	rec := &fakeRecorder{up: 4, down: 1}
	a.SetFeedbackRecorder(rec)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	got := viewText(t, api.views[1].view)
	if !strings.HasSuffix(got, "divider:\ncontext: This week: 4 👍, 1 👎") {
		t.Errorf("the tab must end with the week's ratings:\n%s", got)
	}
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if rec.tallies != 1 {
		t.Errorf("tally read %d times for two opens inside its freshness, want 1", rec.tallies)
	}
}

func TestSlackHomeLeavesOutATallyItCouldNotRead(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.SetFeedbackRecorder(&fakeRecorder{tallyErr: errors.New("bucket gone")})
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	got := viewText(t, api.views[0].view)
	if strings.Contains(got, "This week") || !strings.Contains(got, "header: Fleet") {
		t.Errorf("a failed tally must leave the line out and the rest of the tab drawn:\n%s", got)
	}
}

func TestSlackHomeDrawsTheDashboardForAnAllowedMember(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.noteAsker("slack:C1/40.0", "U1")
	a.trackQuestion("slack:C1/40.0", "1792155000.000100", OutMessage{Text: "Which checkout-gateway should I look at?",
		Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Which checkout-gateway should I look at?",
			Choices: []chatmsg.Choice{{Text: "seeded-a"}, {Text: "seeded-b"}}}})
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if len(api.views) != 1 || api.views[0].member != "U1" || api.views[0].view.Type != slack.VTHomeTab {
		t.Fatalf("views = %+v", api.views)
	}
	got := viewText(t, api.views[0].view)
	want := []string{
		"header: Fleet",
		"context: 3 clusters · updated <!date^1792155720^{time}|Oct 16 13:02 UTC>",
		"section: 🔴 *seeded-a*  payments-api crashlooping (OOM) in seeded-debug · 6 more findings",
		"section: 🟠 *seeded-b*  control plane one minor behind REGULAR · 4 more findings",
		"section: 🟢 *seeded-c*  no open findings",
		"header: Waiting on you",
		"section: Which checkout-gateway should I look at? [Open thread https://acme.slack.com/archives/C1/p1792155000000100?thread_ts=40.0&cid=C1]",
		"context: asked <!date^1792155000^{time}|Oct 16 12:50 UTC>",
		"header: Running",
		"section: ⏳  Diagnose the payments-api crashloop",
		"context: seeded-a · started <!date^1792155720^{time}|Oct 16 13:02 UTC>",
		"header: Recent pull requests",
		"section: <https://github.com/acme/fleet-config/pull/41|#41 Raise payments-api memory limit to 512Mi>",
		"context: acme/fleet-config · opened <!date^1792155720^{date_short_pretty} {time}|Oct 16 13:02 UTC>",
		"header: Last audit",
		"section: *Security &amp; RBAC audit:* 7 findings, 2 critical [Ledger issue #231 ↗ https://github.com/acme/fleet-config/issues/231]",
		"context: <!date^1792155720^{date_short_pretty} {time}|Oct 16 13:02 UTC>",
	}
	for _, line := range want {
		if !strings.Contains(got, line) {
			t.Errorf("view lacks %q; got:\n%s", line, got)
		}
	}
	if strings.Index(got, "Fleet") > strings.Index(got, "Waiting on you") || strings.Index(got, "Running") > strings.Index(got, "Recent pull requests") ||
		strings.Index(got, "Recent pull requests") > strings.Index(got, "Last audit") {
		t.Errorf("panels out of the mock's order:\n%s", got)
	}
}

// "Waiting on you" is the viewer's: a question in a conversation someone else
// asked in is not on their tab.
func TestSlackHomeWaitingIsPerViewer(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.SetHome(func(string) (bool, bool) { return true, false }, func(context.Context) (*lib.Board, error) { return homeBoardFixture(), nil })
	a.noteAsker("slack:C1/40.0", "U2")
	a.trackQuestion("slack:C1/40.0", "50.0", OutMessage{Text: "q", Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Theirs?",
		Choices: []chatmsg.Choice{{Text: "a"}}}})
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if got := viewText(t, api.views[0].view); strings.Contains(got, "Waiting on you") || strings.Contains(got, "Theirs?") {
		t.Errorf("U1's tab shows U2's question:\n%s", got)
	}
}

func TestSlackHomeShowsNoFleetToAMemberTheGatewayWouldNotAnswer(t *testing.T) {
	for _, member := range []string{"U2", "UX"} {
		t.Run(member, func(t *testing.T) {
			a, api, reads := homeAdapter(t, homeBoardFixture(), nil)
			a.homeOpened(&slackevents.AppHomeOpenedEvent{User: member, Tab: "home"})
			if len(api.views) != 1 {
				t.Fatalf("views = %d, want the one-line view", len(api.views))
			}
			if got := viewText(t, api.views[0].view); got != "section: "+slackHomeDenied {
				t.Errorf("view = %q", got)
			}
			if *reads != 0 {
				t.Errorf("board read %d times for a member who sees none of it", *reads)
			}
		})
	}
}

// A Slack Connect guest the list names as T…:U… is drawn the dashboard, as their
// mentions are turns; the same member id from a workspace the entry does not
// name is not.
func TestSlackHomeDrawsTheDashboardForAListedGuest(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{
		"UG": {ID: "UG", TeamID: "T7"}, "UH": {ID: "UH", TeamID: "T8"}}}
	a, _ := newUXSlackAdapter(api)
	a.workspaceURL = "https://acme.slack.com/"
	a.SetAllowedGuests([]string{"T7:UG", "T7:UH"})
	a.SetHome(func(m string) (bool, bool) { return m == "U1" || m == "T7:UG" || m == "T7:UH", false },
		func(context.Context) (*lib.Board, error) { return homeBoardFixture(), nil })
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "UG", Tab: "home"})
	if len(api.views) != 1 || !strings.Contains(viewText(t, api.views[0].view), "header: Fleet") {
		t.Fatalf("a listed guest was not drawn the dashboard: %+v", api.views)
	}
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "UH", Tab: "home"})
	if len(api.views) != 2 || viewText(t, api.views[1].view) != "section: "+slackHomeDenied {
		t.Errorf("a member of a workspace the entry does not name was drawn the dashboard: %+v", api.views)
	}
}

// With guest entries on the list, a guest listed only by their bare member id
// is shown no fleet, as their messages are refused; an entry naming the
// install's own workspace admits nobody; our own listed member is still drawn.
func TestSlackHomeWithGuestEntriesChecksEveryMembersWorkspace(t *testing.T) {
	api := &fakeSlackAPI{users: map[string]*slack.User{
		"UQ": {ID: "UQ", TeamID: "T7"}, "UO": {ID: "UO", TeamID: "T1"}, "U1": {ID: "U1", TeamID: "T1"}}}
	a, _ := newUXSlackAdapter(api) // our own workspace is T1
	a.workspaceURL = "https://acme.slack.com/"
	a.SetAllowedGuests([]string{"T7:UG", "T1:UO"})
	a.SetHome(func(m string) (bool, bool) { return m == "U1" || m == "UQ" || m == "T1:UO", false },
		func(context.Context) (*lib.Board, error) { return homeBoardFixture(), nil })
	for member, want := range map[string]bool{"UQ": false, "UO": false, "U1": true} {
		a.homeOpened(&slackevents.AppHomeOpenedEvent{User: member, Tab: "home"})
		got := strings.Contains(viewText(t, api.views[len(api.views)-1].view), "header: Fleet")
		if got != want {
			t.Errorf("member %s drawn the dashboard = %v, want %v", member, got, want)
		}
	}
}

// A listed member needs no users.info, so an app without users:read still
// draws their dashboard; allow-all is what needs the workspace check.
func TestSlackHomeAListedMemberNeedsNoUserLookup(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	api.users = nil
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if got := viewText(t, api.views[0].view); !strings.Contains(got, "header: Fleet") {
		t.Errorf("listed member denied without users.info:\n%s", got)
	}
	if api.userInfoCalls != 0 {
		t.Errorf("users.info called %d times for a listed member", api.userInfoCalls)
	}
}

// Past the cap, the count is the whole fleet and a line says how many more.
func TestSlackHomeCountsTheWholeFleetPastTheCap(t *testing.T) {
	b := homeBoardFixture()
	b.ClusterCount = 40
	for len(b.Clusters) < slackHomeClustersMax+2 {
		b.Clusters = append(b.Clusters, lib.BoardCluster{Name: "extra"})
	}
	a, api, _ := homeAdapter(t, b, nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	got := viewText(t, api.views[0].view)
	if !strings.Contains(got, "context: 40 clusters ·") || !strings.Contains(got, "context: 25 more clusters") {
		t.Errorf("view:\n%s", got)
	}
}

// Under allow-all, a users.info failure decides nothing: the tab is left as
// it was, never told it is not allowed; and a republish to a member who was
// admitted when they opened it asks users.info nothing again.
func TestSlackHomeAnUndecidedMemberIsPublishedNothing(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	api.users["UX"] = &slack.User{ID: "UX", TeamID: "T1"}
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "UX", Tab: "home"})
	if len(api.views) != 1 || !strings.Contains(viewText(t, api.views[0].view), "header: Fleet") {
		t.Fatalf("views = %+v", api.views)
	}
	calls := api.userInfoCalls
	a.republishHome()
	if api.userInfoCalls != calls || len(api.views) != 2 {
		t.Errorf("republish: users.info calls %d -> %d, views %d", calls, api.userInfoCalls, len(api.views))
	}
	delete(api.users, "UX")
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "UX", Tab: "home"})
	if len(api.views) != 2 {
		t.Errorf("a failed lookup published %q", viewText(t, api.views[len(api.views)-1].view))
	}
}

// A failed read stands for a few seconds, so an outage is one request.
func TestSlackHomeCachesAFailedRead(t *testing.T) {
	a, api, reads := homeAdapter(t, nil, errors.New("nothing answers"))
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if len(api.views) != 2 || *reads != 1 {
		t.Errorf("views=%d reads=%d, want 2 views from 1 failed read", len(api.views), *reads)
	}
}

func TestSlackHomeIgnoresOtherTabsAndAnUnarmedAdapter(t *testing.T) {
	a, api, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "messages"})
	bare, bareAPI := newUXSlackAdapter(&fakeSlackAPI{})
	_ = bareAPI
	bare.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if len(api.views) != 0 || len(bare.api.(*fakeSlackAPI).views) != 0 {
		t.Errorf("published on the Messages tab or unarmed")
	}
}

// A board that cannot be read says so, and never draws as a clean fleet.
func TestSlackHomeBoardErrorIsNotACleanFleet(t *testing.T) {
	a, api, _ := homeAdapter(t, nil, errors.New("nothing answers"))
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	got := viewText(t, api.views[0].view)
	if !strings.Contains(got, "Could not read the board at") || strings.Contains(got, "no open findings") || strings.Contains(got, "clusters") ||
		strings.Contains(got, slackHomeFirstReport) {
		t.Errorf("view = %s", got)
	}
}

// Before the first report a cluster with no open finding has not been checked,
// so it is not drawn green; the note says how to get the report until someone
// writes.
func TestSlackHomeBeforeTheFirstReportIsNotAHealthyFleet(t *testing.T) {
	for _, tc := range []struct {
		name                 string
		reportPending, first bool
		line                 string
		note                 bool
	}{
		{"nobody has written", true, true, "section: ⚪ *seeded-c*  no findings registered yet", true},
		{"written, report not delivered", true, false, "section: ⚪ *seeded-c*  no findings registered yet", false},
		{"report delivered on a clean fleet", false, false, "section: 🟢 *seeded-c*  no open findings", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			board := homeBoardFixture()
			board.FirstReportPending, board.FirstMessagePending = tc.reportPending, tc.first
			a, api, _ := homeAdapter(t, board, nil)
			a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
			got := viewText(t, api.views[0].view)
			if !strings.Contains(got, tc.line) {
				t.Errorf("view lacks %q; got:\n%s", tc.line, got)
			}
			if strings.Contains(got, slackHomeFirstReport) != tc.note {
				t.Errorf("first-report note shown = %v, want %v; got:\n%s", !tc.note, tc.note, got)
			}
			if !strings.Contains(got, "section: 🔴 *seeded-a*") {
				t.Errorf("a cluster with findings lost its dot; got:\n%s", got)
			}
		})
	}
}

func TestSlackHomeCachesTheBoardAcrossOpens(t *testing.T) {
	a, api, reads := homeAdapter(t, homeBoardFixture(), nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	if len(api.views) != 2 || *reads != 1 {
		t.Errorf("views=%d reads=%d, want 2 views from 1 read", len(api.views), *reads)
	}
}

// A change republishes to the members who opened the tab, once per burst,
// reading the board again only when a card moved.
func TestSlackHomeRepublishesToRecentViewersOnAChange(t *testing.T) {
	a, api, reads := homeAdapter(t, homeBoardFixture(), nil)
	a.homeChanged(true)
	a.home.mu.Lock()
	idle := a.home.timer == nil
	a.home.mu.Unlock()
	if !idle {
		t.Fatal("a change with no viewers armed a republish")
	}
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	a.homeChanged(false)
	a.homeChanged(true)
	a.home.mu.Lock()
	timer := a.home.timer
	a.home.mu.Unlock()
	if timer == nil || !timer.Stop() {
		t.Fatal("no republish armed")
	}
	a.republishHome()
	if len(api.views) != 2 || api.views[1].member != "U1" {
		t.Fatalf("views = %+v", api.views)
	}
	if *reads != 2 {
		t.Errorf("reads = %d; a card moving reads the board again", *reads)
	}
	a.home.mu.Lock()
	a.home.viewers["U1"] = time.Now().Add(-slackHomeViewerTTL - time.Second)
	a.home.mu.Unlock()
	a.republishHome()
	if len(api.views) != 2 {
		t.Errorf("republished to a viewer past the TTL")
	}
}

// A change that lands while a pass is running starts no second pass beside
// it; the pass rearms the timer when it ends.
func TestSlackHomeAChangeDuringAPassRearmsInsteadOfOverlapping(t *testing.T) {
	a, _, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	preset := time.AfterFunc(time.Hour, func() {})
	defer preset.Stop()
	a.SetHome(func(m string) (bool, bool) { return m == "U1", false }, func(context.Context) (*lib.Board, error) {
		a.homeChanged(true) // a card moves mid-pass
		a.home.mu.Lock()
		timer, dirty := a.home.timer, a.home.dirty
		a.home.mu.Unlock()
		if timer != preset {
			t.Error("a timer was armed mid-pass, so a second pass could run beside this one")
		}
		if !dirty {
			t.Error("the change during the pass was not marked dirty")
		}
		return homeBoardFixture(), nil
	})
	a.home.mu.Lock()
	a.home.timer = preset
	a.home.refetch = true
	a.home.mu.Unlock()
	a.republishHome()
	a.home.mu.Lock()
	rearmed := a.home.timer
	a.home.mu.Unlock()
	if rearmed == nil || rearmed == preset {
		t.Fatal("the change during the pass was dropped instead of rearming a new pass")
	}
	rearmed.Stop()
}

func TestSlackHomeMarkAndQuestionChangesArmARepublish(t *testing.T) {
	a, _, _ := homeAdapter(t, homeBoardFixture(), nil)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	a.MarkChanged("slack:C1/40.0", "a1", "")
	a.home.mu.Lock()
	timer, refetch := a.home.timer, a.home.refetch
	a.home.mu.Unlock()
	if timer == nil || !refetch {
		t.Errorf("a mark change armed timer=%v refetch=%v", timer != nil, refetch)
	}
	if timer != nil {
		timer.Stop()
	}
}

// The asker is whoever asked; a member who answers the question, by typing,
// does not take the next question over, and a question asked or answered
// arms a republish that only recomputes who is waiting.
func TestSlackHomeAnAnswerDoesNotMoveTheAsker(t *testing.T) {
	a, _, _ := homeAdapter(t, homeBoardFixture(), nil)
	conv := "slack:C1/40.0"
	ask := InboundMessage{Conversation: conv, AuthorID: "U1", MessageID: "m1", Text: "check seeded-a"}
	a.notePending(ask)
	a.InboundVerified(conv, ask.MessageID, true)
	a.homeOpened(&slackevents.AppHomeOpenedEvent{User: "U1", Tab: "home"})
	q := func(ts, headline string) {
		a.trackQuestion(conv, ts, OutMessage{Text: headline, Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: headline,
			Choices: []chatmsg.Choice{{Text: "a"}, {Text: "b"}}}})
	}
	q("50.0", "First?")
	a.home.mu.Lock()
	armed, refetch := a.home.timer != nil, a.home.refetch
	a.home.mu.Unlock()
	if !armed || refetch {
		t.Errorf("a question armed=%v refetch=%v; want a republish that does not re-read the board", armed, refetch)
	}
	answer := InboundMessage{Conversation: conv, AuthorID: "U2", MessageID: "m2", Text: "the second one"}
	a.notePending(answer)
	a.InboundVerified(conv, answer.MessageID, true)
	q("60.0", "Second?")
	if got := a.waitingOn("U1"); len(got) != 1 || got[0].text != "Second?" {
		t.Errorf("waiting on U1 = %+v; the asker moved to whoever answered", got)
	}
	if got := a.waitingOn("U2"); len(got) != 0 {
		t.Errorf("waiting on U2 = %+v", got)
	}
	a.home.mu.Lock()
	if a.home.timer != nil {
		a.home.timer.Stop()
	}
	a.home.mu.Unlock()
}

func TestPRRepo(t *testing.T) {
	for in, want := range map[string]string{
		"https://github.com/acme/fleet-config/pull/41":       "acme/fleet-config",
		"https://gitlab.com/grp/proj/-/merge_requests/3":     "grp/proj",
		"https://gitlab.com/grp/sub/proj/-/merge_requests/3": "grp/sub/proj",
		"https://github.com/acme/fleet-config/issues/2":      "",
		"http://github.com/acme/x/pull/1":                    "",
		"https://github.com/":                                "",
	} {
		if got := prRepo(in); got != want {
			t.Errorf("prRepo(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRequestBoardOverTheBus(t *testing.T) {
	s := startServer(t)
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if _, err := requestBoard(ctx, nc); err == nil || !strings.Contains(err.Error(), "bridge is not running") {
		t.Errorf("no responder: err = %v", err)
	}
	var replyTo string
	sub, err := nc.Subscribe(lib.BoardSubject, func(m *nats.Msg) {
		replyTo = m.Reply
		data, _ := json.Marshal(homeBoardFixture())
		_ = m.Respond(data)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	b, err := requestBoard(ctx, nc)
	if err != nil {
		t.Fatal(err)
	}
	if len(b.Clusters) != 3 || !strings.HasPrefix(replyTo, lib.BoardReplyPrefix) {
		t.Errorf("board = %+v reply = %q", b, replyTo)
	}
}
