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

// teamsTypingQuiet fails if a typing post arrives over several intervals.
func teamsTypingQuiet(t *testing.T, relay *fakeTeamsRelay, when string) {
	t.Helper()
	before := teamsTypingCount(relay)
	time.Sleep(10 * testTeamsTypingInterval)
	if now := teamsTypingCount(relay); now != before {
		t.Fatalf("typing posts %s: %d then %d", when, before, now)
	}
}

// The hand-off: nothing is posted while the first agent's task runs, since
// its line shows the work; once it has ended with the ask still open, the
// indicator is posted to the conversation and posted again, until the ask
// settles.
func TestTeamsTypesWhileAnAskIsOpenPastItsTask(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	m := NewMetrics()
	a.SetMetrics(m)
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	a.MarkChanged(conversation, "investigate", "")
	teamsTypingQuiet(t, relay, "while the task was running")
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	waitFor(t, "the indicator posted more than once", func() bool { return teamsTypingCount(relay) >= 3 })
	relay.mu.Lock()
	first := relay.typing[0]
	relay.mu.Unlock()
	payload, _ := first["payload"].(map[string]any)
	if first["method"] != http.MethodPost || first["endpoint"] != teamsConversationsPath+testTeamsChannel+teamsActivitiesSuffix || payload["type"] != teamsTypingActivityType || len(payload) != 1 {
		t.Fatalf("typing post %v, want a typing activity posted to the conversation", first)
	}
	a.MarkChanged("teams:19:another@thread.tacv2", "", "")
	before := teamsTypingCount(relay)
	waitFor(t, "another conversation's mark left the indicator on", func() bool { return teamsTypingCount(relay) >= before+2 })
	a.MarkChanged(conversation, "", "")
	teamsTypingSettled(t, relay)
	teamsTypingQuiet(t, relay, "went on after the ask settled")
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

// A task that answers on its own line never shows the indicator, whichever
// of its end and its mark's settle arrives first: Teams would leave it on
// screen under the answer.
func TestTeamsDoesNotTypeForATaskThatAnswers(t *testing.T) {
	for name, end := range map[string]func(a *TeamsAdapter, c string){
		"the task ends, then the mark settles": func(a *TeamsAdapter, c string) {
			a.TaskTerminal(c, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
			a.MarkChanged(c, "", "")
		},
		"the mark settles, then the task ends": func(a *TeamsAdapter, c string) {
			a.MarkChanged(c, "", "")
			a.TaskTerminal(c, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
		},
	} {
		t.Run(name, func(t *testing.T) {
			a, relay := typingTeamsAdapter(t)
			conversation := "teams:" + testTeamsChannel
			a.TaskStarted(conversation, "task-1")
			a.MarkChanged(conversation, "investigate", "")
			time.Sleep(5 * testTeamsTypingInterval)
			end(a, conversation)
			time.Sleep(10 * testTeamsTypingInterval)
			if n := teamsTypingCount(relay); n != 0 {
				t.Fatalf("%d typing posts for a task that answered, want none", n)
			}
			a.mu.Lock()
			left := len(a.typing)
			a.mu.Unlock()
			if left != 0 {
				t.Fatalf("%d conversations still remembered after the work ended, want none", left)
			}
		})
	}
}

// A second ask while card work is open pauses the indicator for as long as
// its own task runs, and the indicator comes back after it.
func TestTeamsPausesTypingWhileALaterTaskRuns(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 2 })
	a.TaskStarted(conversation, "task-2")
	teamsTypingSettled(t, relay)
	teamsTypingQuiet(t, relay, "while the later task was running")
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
	before := teamsTypingCount(relay)
	waitFor(t, "the indicator came back", func() bool { return teamsTypingCount(relay) >= before+2 })
	a.MarkChanged(conversation, "", "")
}

// An ask that never settles does not keep the indicator on for ever: the
// loop stops at its bound, and a later task in the conversation starts it
// again.
func TestTeamsStopsTypingAtItsBound(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	a.typingMax = 8 * testTeamsTypingInterval
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 1 })
	settled := teamsTypingSettled(t, relay)
	if settled > 20 {
		t.Fatalf("%d typing posts before the bound stopped them, want about 8", settled)
	}
	a.TaskStarted(conversation, "task-2")
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
	waitFor(t, "a later task turned the indicator on again", func() bool { return teamsTypingCount(relay) > settled })
	a.MarkChanged(conversation, "", "")
}

// A task that never reports its end is forgotten at the bound, so it does
// not keep the indicator off for the asks that follow.
func TestTeamsForgetsATaskThatNeverEnds(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	a.typingMax = 8 * testTeamsTypingInterval
	conversation := "teams:" + testTeamsChannel
	a.TaskStarted(conversation, "task-1")
	a.MarkChanged(conversation, "investigate", "")
	time.Sleep(12 * testTeamsTypingInterval)
	a.TaskStarted(conversation, "task-2")
	a.TaskTerminal(conversation, "task-2", lib.StateCompleted, TerminalFromExecutor, "")
	waitFor(t, "the indicator posted after the forgotten task", func() bool { return teamsTypingCount(relay) >= 2 })
	a.MarkChanged(conversation, "", "")
}

// A typing post the broker refuses is dropped and the next one still goes.
func TestTeamsTypingSurvivesARefusedPost(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	relay.mu.Lock()
	relay.apiStatus = http.StatusBadGateway
	relay.mu.Unlock()
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator kept posting", func() bool { return teamsTypingCount(relay) >= 3 })
	a.MarkChanged(conversation, "", "")
}

// The indicator stops when the open ask settles or waits on the person.
func TestTeamsStopsTypingWhenTheAskIsNoLongerOpen(t *testing.T) {
	for name, release := range map[string]func(a *TeamsAdapter, conversation string){
		"the ask cleared":       func(a *TeamsAdapter, c string) { a.MarkChanged(c, "", "") },
		"waiting on the person": func(a *TeamsAdapter, c string) { a.MarkChanged(c, "investigate", markSettleBlocked) },
	} {
		t.Run(name, func(t *testing.T) {
			a, relay := typingTeamsAdapter(t)
			conversation := "teams:" + testTeamsChannel
			a.MarkChanged(conversation, "investigate", "")
			waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 3 })
			release(a, conversation)
			teamsTypingSettled(t, relay)
			teamsTypingQuiet(t, relay, "went on after the release")
		})
	}
}

// A settled or blocked mark starts nothing, and neither does a task with
// no open ask.
func TestTeamsDoesNotTypeWithoutAnOpenAsk(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "", markSettleFailed)
	a.MarkChanged(conversation, "investigate", markSettleBlocked)
	a.TaskStarted(conversation, "task-1")
	time.Sleep(5 * testTeamsTypingInterval)
	a.TaskTerminal(conversation, "task-1", lib.StateCompleted, TerminalFromExecutor, "")
	time.Sleep(10 * testTeamsTypingInterval)
	if n := teamsTypingCount(relay); n != 0 {
		t.Fatalf("%d typing posts with no open ask, want none", n)
	}
}

// The work resuming after the person answered turns the indicator on
// again.
func TestTeamsTypesAgainWhenTheWorkResumes(t *testing.T) {
	a, relay := typingTeamsAdapter(t)
	conversation := "teams:" + testTeamsChannel
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator posted", func() bool { return teamsTypingCount(relay) >= 1 })
	a.MarkChanged(conversation, "investigate", markSettleBlocked)
	settled := teamsTypingSettled(t, relay)
	a.MarkChanged(conversation, "investigate", "")
	waitFor(t, "the indicator came back", func() bool { return teamsTypingCount(relay) >= settled+2 })
	a.MarkChanged(conversation, "", "")
}
