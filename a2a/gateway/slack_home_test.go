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
			{Name: "seeded-b", Findings: 5, Severity: "high", Top: "control plane one minor behind REGULAR"},
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
	a.SetHome(func(m string) bool { return m == "U1" || m == "UX" }, func(context.Context) (*lib.Board, error) {
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
	a.SetHome(func(string) bool { return true }, func(context.Context) (*lib.Board, error) { return homeBoardFixture(), nil })
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
	if !strings.Contains(got, "Could not read the board at") || strings.Contains(got, "no open findings") || strings.Contains(got, "clusters") {
		t.Errorf("view = %s", got)
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

func TestPRRepo(t *testing.T) {
	for in, want := range map[string]string{
		"https://github.com/acme/fleet-config/pull/41":   "acme/fleet-config",
		"https://gitlab.com/grp/proj/-/merge_requests/3": "grp/proj",
		"http://github.com/acme/x/pull/1":                "",
		"https://github.com/":                            "",
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
