package gateway

import (
	"net/http"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const testTeamsTypingInterval = 10 * time.Millisecond

func typingTeamsAdapter(t *testing.T) (*TeamsAdapter, *fakeTeamsRelay) {
	t.Helper()
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	a.typingInterval = testTeamsTypingInterval
	return a, relay
}

func teamsTypingCount(relay *fakeTeamsRelay) int {
	relay.mu.Lock()
	defer relay.mu.Unlock()
	return len(relay.typing)
}

// teamsTypingSettled waits until no typing post has arrived for several
// intervals and returns the count it settled at.
func teamsTypingSettled(t *testing.T, relay *fakeTeamsRelay) int {
	t.Helper()
	last, quiet := teamsTypingCount(relay), 0
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); {
		time.Sleep(5 * testTeamsTypingInterval)
		now := teamsTypingCount(relay)
		if now == last {
			if quiet++; quiet >= 3 {
				return now
			}
			continue
		}
		last, quiet = now, 0
	}
	t.Fatalf("typing posts never settled; last count %d", last)
	return 0
}

// A task starting in a conversation turns the typing indicator on there, as
// a typing activity posted to that conversation and posted again while the
// task runs; the task ending turns it off.
func TestTeamsTypesWhileATaskRuns(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	m := NewMetrics()
	a.SetMetrics(m)
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	waitFor(t, "the indicator posted more than once", func() bool { return teamsTypingCount(relay) >= 3 })
	relay.mu.Lock()
	first := relay.typing[0]
	relay.mu.Unlock()
	payload, _ := first["payload"].(map[string]any)
	if first["method"] != http.MethodPost || first["endpoint"] != teamsConversationsPath+testTeamsChannel+teamsActivitiesSuffix || payload["type"] != teamsTypingActivityType || len(payload) != 1 {
		t.Fatalf("typing post %v, want a typing activity posted to the conversation", first)
	}
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	settled := teamsTypingSettled(t, relay)
	time.Sleep(10 * testTeamsTypingInterval)
	if now := teamsTypingCount(relay); now != settled {
		t.Fatalf("typing posts went on after the task ended: %d then %d", settled, now)
	}
	if n := teamsCallCount(relay); n != 0 {
		t.Fatalf("the indicator posted %d messages, want none", n)
	}
	// A typing post creates no message, so it is counted with the
	// passthrough's other calls and not among the creates.
	if got := histogramCount(t, m.teamsAPIDuration.WithLabelValues(metricsLabelOther, teamsCallOK)); got < 3 {
		t.Errorf("typing posts counted as other = %d, want every one", got)
	}
	if got := histogramCount(t, m.teamsAPIDuration.WithLabelValues(teamsAPIMethodCreate, teamsCallOK)); got != 0 {
		t.Errorf("typing posts counted as creates = %d, want none", got)
	}
}

// The indicator stays on until the conversation's last task ends, and a
// task ending in another conversation does not stop it.
func TestTeamsKeepsTypingUntilTheLastTaskEnds(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	a.TaskStarted(conversation, "task-2")
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskTerminal("teams:19:another@thread.tacv2", "task-2", lib.StateCompleted, TerminalFromExecutor, "")
	before := teamsTypingCount(relay)
	waitFor(t, "the indicator kept posting for the second task", func() bool { return teamsTypingCount(relay) >= before+3 })
	a.TaskTerminal(conversation, "task-2", lib.StateCanceled, TerminalFromExecutor, "")
	settled := teamsTypingSettled(t, relay)
	time.Sleep(10 * testTeamsTypingInterval)
	if now := teamsTypingCount(relay); now != settled {
		t.Fatalf("typing posts went on after the last task ended: %d then %d", settled, now)
	}
}

// A task that never reports an end does not keep the indicator on for
// ever: the loop stops at its bound, and the end arriving late is harmless.
func TestTeamsStopsTypingAtItsBound(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	a.typingMax = 8 * testTeamsTypingInterval
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 1 })
	settled := teamsTypingSettled(t, relay)
	if settled > 20 {
		t.Fatalf("%d typing posts before the bound stopped them, want about 8", settled)
	}
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskStarted(conversation, "task-2")
	waitFor(t, "a new task turned the indicator on again", func() bool { return teamsTypingCount(relay) > settled })
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
}

// A typing post the broker refuses is dropped and the next one still goes.
func TestTeamsTypingSurvivesARefusedPost(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	relay.mu.Lock()
	relay.apiStatus = http.StatusBadGateway
	relay.mu.Unlock()
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	waitFor(t, "the indicator kept posting", func() bool { return teamsTypingCount(relay) >= 3 })
	a.TaskTerminal(conversation, "task-1", lib.StateFailed, TerminalFromExecutor, "")
}

// Card work the gateway cannot see keeps the indicator on: an ask still
// open after its task ended holds it (the session mark), until the ask
// settles or waits on the person.
func TestTeamsKeepsTypingWhileTheAskIsOpen(t *testing.T) {
	for name, release := range map[string]func(a *TeamsAdapter, conversation string){
		"the ask cleared":       func(a *TeamsAdapter, c string) { a.MarkChanged(c, "", "") },
		"waiting on the person": func(a *TeamsAdapter, c string) { a.MarkChanged(c, "investigate", markSettleBlocked) },
	} {
		t.Run(name, func(t *testing.T) {
			a, relay := typingTeamsAdapter(t)
			conversation := "teams:" + testTeamsChannel
			a.TaskStarted(conversation, "task-1")
			a.MarkChanged(conversation, "investigate", "")
			a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
			before := teamsTypingCount(relay)
			waitFor(t, "the indicator kept posting past the task's end", func() bool { return teamsTypingCount(relay) >= before+3 })
			release(a, conversation)
			settled := teamsTypingSettled(t, relay)
			time.Sleep(10 * testTeamsTypingInterval)
			if now := teamsTypingCount(relay); now != settled {
				t.Fatalf("typing posts went on after the release: %d then %d", settled, now)
			}
		})
	}
}

// A mark with no ask on a conversation nothing holds starts nothing.
func TestTeamsDoesNotTypeForASettledMark(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "", markSettleFailed)
	a.MarkChanged(conversation, "investigate", markSettleBlocked)
	time.Sleep(10 * testTeamsTypingInterval)
	if n := teamsTypingCount(relay); n != 0 {
		t.Fatalf("%d typing posts for a settled or blocked mark, want none", n)
	}
}

// A task that asks the person a question is still running, and the mark
// says the work waits on them: the indicator pauses there, and comes back
// when the mark says the work is on again.
func TestTeamsDoesNotTypeWhileARunningTaskWaitsOnThePerson(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 2 })
	a.MarkChanged(conversation, "investigate", markSettleBlocked)
	settled := teamsTypingSettled(t, relay)
	time.Sleep(10 * testTeamsTypingInterval)
	if now := teamsTypingCount(relay); now != settled {
		t.Fatalf("typing posts went on while the work waited on the person: %d then %d", settled, now)
	}
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator came back", func() bool { return teamsTypingCount(relay) >= settled+2 })
	a.MarkChanged(conversation, "", "")
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
}

// The bound runs from the newest hold: a second ask late in the first's
// time is not cut off when the first's would have ended.
func TestTeamsTypingBoundRunsFromTheNewestHold(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	a.typingMax = 12 * testTeamsTypingInterval
	conversation := "teams:" + testTeamsChannel
	start := time.Now()
	a.TaskStarted(conversation, "task-1")
	time.Sleep(8 * testTeamsTypingInterval)
	a.TaskStarted(conversation, "task-2")
	// Past the first hold's bound, inside the second's.
	time.Sleep(time.Until(start.Add(15 * testTeamsTypingInterval)))
	before := teamsTypingCount(relay)
	waitFor(t, "the indicator went on for the second task", func() bool { return teamsTypingCount(relay) >= before+2 })
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
}

// The person's answer to the agent's question starts a task while the mark
// still says the work waits on them: the indicator comes back with it.
func TestTeamsTypesAgainWhenThePersonAnswers(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	a.MarkChanged(conversation, "investigate", markSettleBlocked)
	settled := teamsTypingSettled(t, relay)
	a.TaskStarted(conversation, "task-2")
	waitFor(t, "the indicator came back with the answer's task", func() bool { return teamsTypingCount(relay) >= settled+2 })
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
}
