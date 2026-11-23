package gateway

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startProfileRigWithLogs is startProfileRig with the gateway's log kept in
// r.logs, so a test can read the audit line a refusal writes.
func startProfileRigWithLogs(t *testing.T) *rig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, func(c *Config) { c.ScopeFile = scope; c.Namespace = "kubeagents-system" },
		slog.New(slog.NewTextHandler(io.MultiWriter(os.Stderr, logs), nil)))
	r.logs = logs
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams, YieldToPinned: true})
	return r
}

// steersInto reports whether any message envelope other than the opening
// one reached the task's input subject: a steer that got through.
func steersInto(t *testing.T, r *rig, addressee string, origin *lib.Envelope) bool {
	t.Helper()
	for _, e := range inSubjectEnvelopes(t, r.url, addressee) {
		if e.TaskID == origin.TaskID && e.EnvelopeID != origin.EnvelopeID && e.Kind == lib.KindMessage {
			return true
		}
	}
	return false
}

// A typed message into a running task that a confirm started is refused,
// not steered: the task holds ChangeRequested and may write, so what it is
// asked to do can't change while it runs (#5884). The author is not
// recorded, the conversation is told, and an audit line names the rule.
func TestASteerIntoAChangeRequestedTaskIsRefused(t *testing.T) {
	r := startProfileRigWithLogs(t)
	conv := "discord:steer-cr/1"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-a", Text: "open the PR", Intent: IntentConfirm}
	origin := r.awaitTask(t, "platform")
	waitForRecord(t, r, conv, "the confirmed entry", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.ChangeRequested
	})

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-b", Text: "and change the replicas too"}
	waitFor(t, "the refusal", postedContaining(r, noticeSteerChangeRequested))
	waitFor(t, "the audit line", loggedContaining(r, "steer refused", "rule="+ruleSteerChangeRequested, origin.TaskID))
	if steersInto(t, r, "platform", origin) {
		t.Fatal("the steer reached the change-requested task")
	}
	if got := steerAuthors(t, r, conv, origin.TaskID); len(got) != 0 {
		t.Fatalf("a refused steer recorded its author: %+v", got)
	}
}

// The console door is refused the same way. The console's human and its MCP
// server publish on the same .in subject as the same author, so a frame from
// either is a console steer here, and neither reaches a change-requested task.
func TestAConsoleSteerIntoAChangeRequestedTaskIsRefused(t *testing.T) {
	r := startProfileRigWithLogs(t)
	conv := "console:tab-steer-cr"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m-a", Text: "open the PR", Intent: IntentConfirm}
	origin := r.awaitTask(t, "platform")
	waitForRecord(t, r, conv, "the confirmed entry", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.ChangeRequested
	})

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", Backend: consoleBackend, AuthorID: consoleAuthor, MessageID: "m-b", Text: "also delete the old branch"}
	waitFor(t, "the refusal", postedContaining(r, noticeSteerChangeRequested))
	waitFor(t, "the audit line", loggedContaining(r, "steer refused", "rule="+ruleSteerChangeRequested, origin.TaskID))
	if steersInto(t, r, "platform", origin) {
		t.Fatal("a console steer reached the change-requested task")
	}
	if got := steerAuthors(t, r, conv, origin.TaskID); len(got) != 0 {
		t.Fatalf("a refused console steer recorded its author: %+v", got)
	}
}

// A choice click while a confirmed task runs is a steer too, and is refused
// the same way: the gateway can't tell who on a door clicked, so a
// change-requested task takes no mid-run answer of any kind (the spec says
// so). An input-required answer takes the same path and is refused alike.
func TestAChoiceClickIntoAChangeRequestedTaskIsRefused(t *testing.T) {
	r := startProfileRigWithLogs(t)
	conv := "discord:steer-cr/4"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-a", Text: "open the PR", Intent: IntentConfirm}
	origin := r.awaitTask(t, "platform")
	waitForRecord(t, r, conv, "the confirmed entry", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.ChangeRequested
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-b", Text: "use main", Click: true}
	waitFor(t, "the refusal", postedContaining(r, noticeSteerChangeRequested))
	if steersInto(t, r, "platform", origin) {
		t.Fatal("a click reached the change-requested task")
	}
}

// A task a typed message started holds no ChangeRequested, and a steer into
// it still goes through.
func TestASteerIntoATaskWithoutChangeRequestedGoes(t *testing.T) {
	r := startProfileRigWithLogs(t)
	conv := "discord:steer-cr/3"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-a", Text: "how is the fleet?"}
	origin := r.awaitTask(t, "platform")
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m-b", Text: "include costs"}
	waitFor(t, "the steer", func() bool { return steersInto(t, r, "platform", origin) })
	if loggedContaining(r, "steer refused")() {
		t.Fatal("a steer into a task without ChangeRequested was refused")
	}
}
