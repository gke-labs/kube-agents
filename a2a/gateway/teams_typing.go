package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The typing indicator on Teams (spec-chatops-gateway.md, "The Teams
// adapter"): while work the person's ask started is going on with nothing
// on screen that shows it, the adapter keeps Teams's own "is typing" line on
// in the conversation. Teams draws no reaction on the asker's message and no
// Working… status, and an acknowledgement or a plan sitting unchanged does
// not read as work going on.
//
// That is the hand-off: the first agent's task has ended, its line is an
// acknowledgement, and a card is working where the gateway cannot see it.
// The conversation's session mark says so (MarkObserver): an ask whose
// chain has ended but still carries its kind is card work still open, and a
// blocked settle is work waiting on the person, as Slack reads it for its
// Working… status (slackSession.wanted). So the indicator is wanted while
// the ask is open and not blocked.
//
// It is not posted while a task the gateway started is running there
// (TaskObserver): that task's own line, with its Stop button, already shows
// the work, and Teams leaves the indicator on screen for some twenty
// seconds after the last post unless a new message from the bot clears it.
// A task's answer is an edit of its line, which clears nothing, so typing
// beside a running task would sit under its answer; the hand-off's answer
// is a new message. For the same reason the first post waits one interval
// after a task ends, in case the mark settles just behind it.
//
// All of these calls arrive under the session lock, so they only record
// and return; one loop per conversation posts a `typing` activity through
// the broker's passthrough every teamsTypingInterval until the ask is no
// longer open or teamsTypingMax has passed since the ask opened, the newest
// task started or ended, or the adapter last posted or edited a message
// there (typingShown): a card that keeps showing its work keeps the
// indicator, as Slack keeps a plan until it has gone without a new note,
// and an edit after the bound ended the loop starts it again. A
// new message the adapter posts there holds the indicator off for one
// interval too (quietTyping). A post that fails is dropped: the indicator
// is a courtesy, and the next tick tries again.
const (
	teamsTypingActivityType = "typing"
	// teamsTypingInterval is how often the indicator is posted again, and
	// how long after a task's end the first post waits.
	teamsTypingInterval = 3 * time.Second
	// teamsTypingRequestTimeout bounds one post on the gateway's side,
	// under the interval, so the loop never waits on a stalled broker. The
	// broker's own call to Microsoft can outlive it.
	teamsTypingRequestTimeout = 2 * time.Second
	// teamsTypingMax ends the loop for an ask that never settles, so no
	// indicator outlives its work by more than this, and forgets a task
	// that never reports an end. It runs from the newest task's start or
	// end, so a second ask late in the first's ten minutes gets its own,
	// and from the adapter's last post or edit in the conversation, so
	// work that goes on showing itself is not cut off at ten minutes.
	teamsTypingMax = 10 * time.Minute
)

// teamsTyping is one conversation's typing state: the tasks running there
// with when each started, whether its ask is open, the channel that stops
// its loop (nil when none runs), when the loop gives up, and when a task
// last ended.
type teamsTyping struct {
	tasks      map[string]time.Time
	open       bool
	stop       chan struct{}
	deadline   time.Time
	quietUntil time.Time
}

var (
	_ TaskObserver = (*TeamsAdapter)(nil)
	_ MarkObserver = (*TeamsAdapter)(nil)
)

// TaskStarted records the task running in the conversation, which keeps
// the indicator off while its line shows the work, and restarts the bound
// of an open ask (TaskObserver). A conversation has one top-level chain at
// a time, so the new task takes the place of any earlier one: a stopped
// task whose executor never confirms is replaced by the next ask with no
// end announced, and would otherwise keep the indicator off for that ask's
// card work until the bound.
func (a *TeamsAdapter) TaskStarted(conversation, taskID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	now := time.Now()
	for c, t := range a.typing {
		a.forgetTypingLocked(c, t, now)
	}
	t := a.typingLocked(conversation)
	t.tasks = map[string]time.Time{taskID: now}
	t.deadline = now.Add(a.typingMax)
	a.startTypingLocked(conversation, t)
}

// TaskTerminal records the task's end; the indicator may go on one
// interval later if the conversation's ask is still open, with its bound
// run from here (TaskObserver).
func (a *TeamsAdapter) TaskTerminal(conversation, taskID string, _ lib.TaskState, _ TerminalSource, _ string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil {
		return
	}
	now := time.Now()
	delete(t.tasks, taskID)
	t.quietUntil = now.Add(a.typingInterval)
	if t.open {
		// A long chain that hands off gets the whole bound for its card
		// work, not what its own run left of it.
		t.deadline = now.Add(a.typingMax)
		a.startTypingLocked(conversation, t)
	}
	a.forgetTypingLocked(conversation, t, now)
}

// quietTyping holds conversation's indicator off for one interval. The
// adapter calls it around each new message it posts there: a card's answer
// is posted before its mark settles (Notifier.reply), and a typing post
// landing behind the answer would sit under it.
func (a *TeamsAdapter) quietTyping(conversation string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if t := a.typing[conversation]; t != nil {
		t.quietUntil = time.Now().Add(a.typingInterval)
	}
}

// typingShown restarts the bound of conversation's open ask. The adapter
// calls it after each message it posts or edits there: the work is still
// showing itself. An edit also starts the loop again if the bound had ended
// it, one interval later, so a plan's last edit just ahead of its answer
// posts nothing; a new message never does, since a message that follows a
// long silence is usually the answer, and a typing post would sit under it.
func (a *TeamsAdapter) typingShown(conversation string, edit bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil || !t.open {
		return
	}
	running := t.stop != nil
	if !running && !edit {
		return
	}
	now := time.Now()
	t.deadline = now.Add(a.typingMax)
	if !running {
		t.quietUntil = now.Add(a.typingInterval)
		a.startTypingLocked(conversation, t)
	}
}

// MarkChanged wants the indicator while the conversation's ask is open and
// not waiting on the person, and stops it otherwise (MarkObserver).
func (a *TeamsAdapter) MarkChanged(conversation string, ask, settle string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	now := time.Now()
	if ask != "" && settle != markSettleBlocked {
		t := a.typingLocked(conversation)
		if !t.open {
			t.open, t.deadline = true, now.Add(a.typingMax)
		}
		a.startTypingLocked(conversation, t)
		return
	}
	if t := a.typing[conversation]; t != nil {
		t.open = false
		a.stopTypingLocked(t)
		a.forgetTypingLocked(conversation, t, now)
	}
}

// typingLocked is conversation's typing state, made on first use.
func (a *TeamsAdapter) typingLocked(conversation string) *teamsTyping {
	t := a.typing[conversation]
	if t == nil {
		t = &teamsTyping{tasks: map[string]time.Time{}}
		a.typing[conversation] = t
	}
	return t
}

// startTypingLocked starts conversation's loop if its ask is open and none
// runs.
func (a *TeamsAdapter) startTypingLocked(conversation string, t *teamsTyping) {
	if !t.open || t.stop != nil {
		return
	}
	t.stop = make(chan struct{})
	go a.keepTyping(conversation, t, t.stop)
}

// stopTypingLocked stops t's loop if one runs.
func (a *TeamsAdapter) stopTypingLocked(t *teamsTyping) {
	if t.stop != nil {
		close(t.stop)
		t.stop = nil
	}
}

// forgetTypingLocked drops the tasks of t that never reported an end
// within the bound, and t itself once nothing is left to remember.
func (a *TeamsAdapter) forgetTypingLocked(conversation string, t *teamsTyping, now time.Time) {
	for id, started := range t.tasks {
		if now.Sub(started) >= a.typingMax {
			delete(t.tasks, id)
		}
	}
	if len(t.tasks) == 0 && !t.open && t.stop == nil {
		delete(a.typing, conversation)
	}
}

// TaskAccepted and CancelPublished change nothing the indicator shows
// (TaskObserver).
func (a *TeamsAdapter) TaskAccepted(string, string)    {}
func (a *TeamsAdapter) CancelPublished(string, string) {}

// keepTyping posts the indicator for conversation until stop is closed or
// t's deadline passes. At the deadline the loop ends and the ask stays
// open, so a later task starts it again. While a task is running there, or
// has ended within the last interval, the loop goes on and posts nothing.
func (a *TeamsAdapter) keepTyping(conversation string, t *teamsTyping, stop chan struct{}) {
	tick := time.NewTicker(a.typingInterval)
	defer tick.Stop()
	for {
		a.mu.Lock()
		now := time.Now()
		if t.stop != stop {
			a.mu.Unlock()
			return
		}
		if !now.Before(t.deadline) {
			t.stop = nil
			a.mu.Unlock()
			return
		}
		a.forgetTypingLocked(conversation, t, now)
		quiet := len(t.tasks) > 0 || now.Before(t.quietUntil)
		a.mu.Unlock()
		if !quiet {
			a.postTyping(conversation)
		}
		select {
		case <-stop:
			return
		case <-tick.C:
		}
	}
}

// postTyping posts one typing activity to conversation, counted with the
// passthrough's other calls (metricsLabelOther: it creates no message).
func (a *TeamsAdapter) postTyping(conversation string) {
	convID := teamsConversationFromID(conversation)
	if convID == "" {
		return
	}
	endpoint := teamsConversationsPath + convID + teamsActivitiesSuffix
	payload, err := json.Marshal(map[string]any{
		"serviceUrl": a.serviceURLFor(convID), "endpoint": endpoint, "method": http.MethodPost,
		"payload": map[string]any{"type": teamsTypingActivityType},
	})
	if err != nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), teamsTypingRequestTimeout)
	defer cancel()
	start := time.Now()
	_, relayStatus, err := a.relayPostStatus(ctx, teamsRelayAPIPath, payload)
	a.metrics.teamsAPICall(metricsLabelOther, relayStatus, teamsCarriedStatus(err), time.Since(start))
	if err != nil {
		a.log.Debug("teams typing indicator was not posted", "conversation", conversation, "err", err)
	}
}
