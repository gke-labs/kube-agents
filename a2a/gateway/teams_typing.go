package gateway

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The typing indicator on Teams (spec-chatops-gateway.md, "The Teams
// adapter"): while a task the gateway started is running in a conversation,
// the adapter keeps Teams's own "is typing" line on there. Teams draws no
// reaction on the asker's message and no Working… status, and a status line
// or a plan sitting unchanged in the thread does not read as work going on.
//
// The gateway names each task as it starts and as it ends (TaskObserver),
// and the conversation's session mark whenever it changes (MarkObserver):
// an ask whose chain has ended but still carries its kind is card work
// still open, which the gateway cannot see any other way, and a blocked
// settle is work waiting on the person. So the indicator is held by each
// running task and by an open ask that is not blocked, as Slack holds its
// Working… status (slackSession.wanted), and it is not posted at all while
// the settle is blocked: "is typing" under a question the person has to
// answer would say the opposite of what is true. All of these calls arrive under
// the session lock, so they only record and return; one loop per
// conversation posts a `typing` activity through the broker's passthrough,
// again every teamsTypingInterval because Teams clears the indicator a few
// seconds after each, until the conversation's last hold is released or
// teamsTypingMax has passed since the newest hold was added. A post that
// fails is dropped: the indicator is a courtesy, and the next tick tries
// again.
const (
	teamsTypingActivityType = "typing"
	// teamsTypingInterval is how often the indicator is posted again;
	// Teams shows one for about three seconds.
	teamsTypingInterval = 3 * time.Second
	// teamsTypingRequestTimeout bounds one post, under the interval, so a
	// stalled broker holds one request per conversation and not a queue.
	teamsTypingRequestTimeout = 2 * time.Second
	// teamsTypingMax ends the loop for a conversation whose task never
	// reports an end, so no indicator outlives its work by more than this.
	// It runs from the newest hold, so a second ask late in the first's
	// ten minutes gets its own.
	teamsTypingMax = 10 * time.Minute
	// teamsTypingMarkHold is the hold an open ask keeps beside the task
	// ids; no task id has this shape.
	teamsTypingMarkHold = "mark:open-ask"
)

// teamsTyping is one conversation's holds (its running tasks, and its open
// ask), the channel that stops its loop, when the loop gives up, and whether
// the work is waiting on the person, which pauses the posts.
type teamsTyping struct {
	holds    map[string]struct{}
	stop     chan struct{}
	deadline time.Time
	blocked  bool
}

var (
	_ TaskObserver = (*TeamsAdapter)(nil)
	_ MarkObserver = (*TeamsAdapter)(nil)
)

// TaskStarted starts the conversation's indicator with its first task
// (TaskObserver).
func (a *TeamsAdapter) TaskStarted(conversation, taskID string) {
	a.holdTyping(conversation, taskID)
}

// TaskTerminal stops the conversation's indicator with its last task
// (TaskObserver).
func (a *TeamsAdapter) TaskTerminal(conversation, taskID string, _ lib.TaskState, _ TerminalSource, _ string) {
	a.releaseTyping(conversation, taskID)
}

// MarkChanged holds the indicator while the conversation's ask is open and
// not waiting on the person, and releases it otherwise (MarkObserver).
func (a *TeamsAdapter) MarkChanged(conversation string, ask, settle string) {
	blocked := settle == markSettleBlocked
	if ask != "" && !blocked {
		a.holdTyping(conversation, teamsTypingMarkHold)
	} else {
		a.releaseTyping(conversation, teamsTypingMarkHold)
	}
	a.mu.Lock()
	if t := a.typing[conversation]; t != nil {
		t.blocked = blocked
	}
	a.mu.Unlock()
}

// holdTyping adds a hold on conversation's indicator and starts its loop
// with the first.
func (a *TeamsAdapter) holdTyping(conversation, hold string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil {
		t = &teamsTyping{holds: map[string]struct{}{}, stop: make(chan struct{})}
		a.typing[conversation] = t
		go a.keepTyping(conversation, t)
	}
	if _, held := t.holds[hold]; !held {
		t.holds[hold] = struct{}{}
		t.deadline = time.Now().Add(a.typingMax)
	}
}

// releaseTyping drops a hold on conversation's indicator and stops its
// loop with the last.
func (a *TeamsAdapter) releaseTyping(conversation, hold string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil {
		return
	}
	delete(t.holds, hold)
	if len(t.holds) == 0 {
		close(t.stop)
		delete(a.typing, conversation)
	}
}

// TaskAccepted and CancelPublished change nothing the indicator shows
// (TaskObserver).
func (a *TeamsAdapter) TaskAccepted(string, string)    {}
func (a *TeamsAdapter) CancelPublished(string, string) {}

// keepTyping posts the indicator for conversation until t is stopped or its
// deadline passes; at the deadline it forgets t, so a late release finds
// nothing to stop, and a later hold starts the indicator again. While the
// work waits on the person the loop goes on and posts nothing.
func (a *TeamsAdapter) keepTyping(conversation string, t *teamsTyping) {
	tick := time.NewTicker(a.typingInterval)
	defer tick.Stop()
	for {
		a.mu.Lock()
		expired, blocked := !time.Now().Before(t.deadline), t.blocked
		if expired && a.typing[conversation] == t {
			delete(a.typing, conversation)
		}
		a.mu.Unlock()
		select {
		case <-t.stop:
			return
		default:
		}
		if expired {
			return
		}
		if !blocked {
			a.postTyping(conversation)
		}
		select {
		case <-t.stop:
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
