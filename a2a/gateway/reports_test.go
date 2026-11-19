package gateway

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A report is found by the root it was on; a root nothing was posted on,
// and an unknown conversation, find nothing.
func TestPushedReportsByRoot(t *testing.T) {
	var p pushedReports
	p.put("console:tab-1", "task-a", "report a")
	p.put("console:tab-1", "task-b", "report b")
	if text, ok := p.byRoot("console:tab-1", "task-a"); !ok || text != "report a" {
		t.Fatalf("byRoot = %q %v, want report a", text, ok)
	}
	if _, ok := p.byRoot("console:tab-1", "task-c"); ok {
		t.Fatal("a root nothing was posted on was found")
	}
	if _, ok := p.byRoot("console:tab-2", "task-a"); ok {
		t.Fatal("an unknown conversation found a report")
	}
}

// Past the per-conversation cap the oldest report is forgotten, and nothing
// finds it by its root; two reports on one root give the newer.
func TestPushedReportsCapEvictsTheOldest(t *testing.T) {
	var p pushedReports
	for i := 1; i <= pushedReportsPerConversation+1; i++ {
		p.put("slack:C1/1.1", fmt.Sprintf("task-%d", i), fmt.Sprintf("report %d", i))
	}
	if _, ok := p.byRoot("slack:C1/1.1", "task-1"); ok {
		t.Fatal("the oldest report outlived the cap")
	}
	if text, ok := p.byRoot("slack:C1/1.1", "task-2"); !ok || text != "report 2" {
		t.Fatalf("the second report = %q %v, want kept", text, ok)
	}
	p.put("slack:C1/1.1", "task-9", "report 9, again")
	if text, _ := p.byRoot("slack:C1/1.1", "task-9"); text != "report 9, again" {
		t.Fatalf("two reports on one root gave %q, want the newer", text)
	}
}

// Past the byte budget the conversation least recently posted a report to
// is forgotten first, whole, and the budget holds.
func TestPushedReportsByteBudgetForgetsTheStalestConversation(t *testing.T) {
	var p pushedReports
	report := strings.Repeat("x", lib.PushedReportTextCap)
	n := pushedReportsBytesMax/lib.PushedReportTextCap + 1
	for i := 0; i < n; i++ {
		p.put(fmt.Sprintf("console:tab-%d", i), "task-1", report)
	}
	if _, ok := p.byRoot("console:tab-0", "task-1"); ok {
		t.Fatal("the first conversation survived the budget")
	}
	if _, ok := p.byRoot(fmt.Sprintf("console:tab-%d", n-1), "task-1"); !ok {
		t.Fatal("the newest conversation was forgotten")
	}
	if p.bytes > pushedReportsBytesMax {
		t.Fatalf("kept %d bytes, budget %d", p.bytes, pushedReportsBytesMax)
	}
	p.put("console:tab-1", "task-2", "a late report")
	p.put("console:tab-2", "task-2", report)
	if _, ok := p.byRoot("console:tab-1", "task-2"); !ok {
		t.Fatal("the conversation posted to last was forgotten before an older one")
	}
}

// A report past the cap is cut and says so.
func TestPushedReportsCutsAtTheCap(t *testing.T) {
	var p pushedReports
	p.put("console:tab-1", "task-b", strings.Repeat("x", lib.PushedReportTextCap+100))
	text, _ := p.byRoot("console:tab-1", "task-b")
	if len(text) > lib.PushedReportTextCap || !strings.HasSuffix(text, pushedReportCutNote) {
		t.Fatalf("cut report: %d bytes, ends %q", len(text), text[len(text)-40:])
	}
}

// A reply that names a task reads the report kept under the root of its
// chain, so a client echoing a child's id finds the report kept under the
// root; one naming no task reads nothing.
func TestRelayedReportIsReadByTheChainsRoot(t *testing.T) {
	g := &Gateway{}
	rec := &SessionRecord{Key: "console:tab-k", Tasks: []TaskRef{
		{ID: "task-root"},
		{ID: "task-child", Role: taskRoleChild, ParentTaskID: "task-root", RootTaskID: "task-root"},
	}}
	g.reports.put(rec.Key, "task-root", "the helper's report")
	for _, id := range []string{"task-root", "task-child"} {
		got, ok := g.relayedReport(rec, id)
		if !ok || got != (lib.PushedReport{TaskID: "task-root", Text: "the helper's report"}) {
			t.Fatalf("by %s: %+v %v, want the report under the root", id, got, ok)
		}
	}
	if _, ok := g.relayedReport(rec, "task-other"); ok {
		t.Fatal("an unknown task found a report")
	}
	if _, ok := g.relayedReport(rec, ""); ok {
		t.Fatal("a turn naming no task read a report")
	}
}

// A console turn on the conversation's own session that names the task a
// kept report was on carries the report to the session pod, beside the
// replyTo part; one naming a task nothing was kept for carries the replyTo
// part alone.
func TestAConsoleReplyNamingTheRootCarriesTheRelayedReport(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	r.adapter.roster = []string{consoleAuthor}
	r.g.reports.put("console:tab-rel", "task-root", "card report: np-quartz-7 is at 92%")
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-rel", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "and the other pool?", ReplyTo: "task-root",
	}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	parts := messageParts(t, origin)
	got, found, err := lib.PushedReportOf(parts)
	if err != nil || !found || got != (lib.PushedReport{TaskID: "task-root", Text: "card report: np-quartz-7 is at 92%"}) {
		t.Fatalf("pushedReport part = %+v found=%v err=%v", got, found, err)
	}
	if rt, found, _ := lib.ReplyToOf(parts); !found || rt.TaskID != "task-root" {
		t.Fatalf("the replyTo part went missing beside the report: %+v %v", rt, found)
	}
}

func TestAConsoleReplyNamingAnUnknownTaskCarriesNoRelayedReport(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	r.adapter.roster = []string{consoleAuthor}
	r.g.reports.put("console:tab-unk", "task-a", "report a")
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-unk", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "and the other pool?", ReplyTo: "task-b",
	}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	if _, found, _ := lib.PushedReportOf(messageParts(t, r.awaitTask(t, spawn.calls()[0].Session))); found {
		t.Fatal("a reply naming an unknown task carried a report")
	}
}

// The relay rides a human turn on every route: the fixed platform route,
// where the hermes-bridge frames it, and a profile route, whose executor
// ignores a part it does not read.
func TestAPlatformTurnCarriesTheRelayedReport(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	r.adapter.roster = []string{consoleAuthor}
	r.g.reports.put("console:tab-pl", "task-root", "card report")
	r.adapter.inbox <- InboundMessage{
		Conversation: "console:tab-pl", Kind: "dm", Backend: consoleBackend,
		AuthorID: consoleAuthor, MessageID: "m1", Text: "and the other pool?", ReplyTo: "task-root",
	}
	got, found, err := lib.PushedReportOf(messageParts(t, r.awaitTask(t, "platform")))
	if err != nil || !found || got.Text != "card report" {
		t.Fatalf("platform turn: pushedReport part = %+v found=%v err=%v", got, found, err)
	}
}

func TestAProfileTurnCarriesTheRelayedReport(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	r.g.reports.put("console:tab-pr", "task-root", "card report")
	turn := profileTurn("console:tab-pr", "chat", nil)
	turn.Backend, turn.AuthorID, turn.ReplyTo = consoleBackend, consoleAuthor, "task-root"
	r.adapter.inbox <- turn
	got, found, err := lib.PushedReportOf(messageParts(t, submissionOn(t, r, "chat")))
	if err != nil || !found || got.Text != "card report" {
		t.Fatalf("profile turn: pushedReport part = %+v found=%v err=%v", got, found, err)
	}
}

// The whole chain, through the real console notifier: a report the route
// posts into a console conversation, naming the turn it answers, is kept
// by the gateway (gatewayConversations.KeepReport) and rides the next reply
// that names that turn.
func TestAReportTheConsoleRoutePostedReachesTheReplyUnderIt(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	r.adapter.roster = []string{consoleAuthor}
	ctx := context.Background()
	const conv = "console:tab-chain"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m1", Text: "list the pods"}
	origin := r.awaitTask(t, "platform")
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatalf("record: %v", err)
	}
	n := NewConsoleNotifier(&fakeFrames{}, nil)
	n.SetConversations(r.g.Conversations(r.adapter))
	const report = "card report: np-quartz-7 is at 92%"
	if got := serveJSON(t, n, lib.NotifyRequest{Text: report, Conversation: conv, ContextID: rec.ContextID, ReplyTo: origin.TaskID, Report: true}); got.Error != "" || got.MessageID == "" {
		t.Fatalf("notify reply = %+v, want a post", got)
	}
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the first turn released", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec != nil && rec.ActiveTask == nil
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m2", Text: "and the other pool?", ReplyTo: origin.TaskID}
	var reply *lib.Envelope
	waitFor(t, "the reply's submission", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage && e.TaskID != origin.TaskID {
				reply = e
				return true
			}
		}
		return false
	})
	got, found, err := lib.PushedReportOf(messageParts(t, reply))
	if err != nil || !found || got != (lib.PushedReport{TaskID: origin.TaskID, Text: report}) {
		t.Fatalf("pushedReport part = %+v found=%v err=%v, want the posted report under %s", got, found, err, origin.TaskID)
	}
}
