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
// under the session lock, so both calls only record and return; one loop per
// conversation posts a `typing` activity through the broker's passthrough,
// again every teamsTypingInterval because Teams clears the indicator a few
// seconds after each, until the conversation's last task ends or
// teamsTypingMax has passed. A post that fails is dropped: the indicator is
// a courtesy, and the next tick tries again.
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
	teamsTypingMax = 10 * time.Minute
)

// teamsTyping is one conversation's running tasks and the channel that
// stops its loop.
type teamsTyping struct {
	tasks map[string]struct{}
	stop  chan struct{}
}

var _ TaskObserver = (*TeamsAdapter)(nil)

// TaskStarted starts the conversation's indicator with its first task
// (TaskObserver).
func (a *TeamsAdapter) TaskStarted(conversation, taskID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil {
		t = &teamsTyping{tasks: map[string]struct{}{}, stop: make(chan struct{})}
		a.typing[conversation] = t
		go a.keepTyping(conversation, t)
	}
	t.tasks[taskID] = struct{}{}
}

// TaskTerminal stops the conversation's indicator with its last task
// (TaskObserver).
func (a *TeamsAdapter) TaskTerminal(conversation, taskID string, _ lib.TaskState, _ TerminalSource, _ string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	t := a.typing[conversation]
	if t == nil {
		return
	}
	delete(t.tasks, taskID)
	if len(t.tasks) == 0 {
		close(t.stop)
		delete(a.typing, conversation)
	}
}

// TaskAccepted and CancelPublished change nothing the indicator shows
// (TaskObserver).
func (a *TeamsAdapter) TaskAccepted(string, string)    {}
func (a *TeamsAdapter) CancelPublished(string, string) {}

// keepTyping posts the indicator for conversation until t is stopped or
// teamsTypingMax passes; at the bound it forgets t, so a late TaskTerminal
// finds nothing to stop.
func (a *TeamsAdapter) keepTyping(conversation string, t *teamsTyping) {
	bound := time.NewTimer(a.typingMax)
	defer bound.Stop()
	tick := time.NewTicker(a.typingInterval)
	defer tick.Stop()
	for {
		select {
		case <-t.stop:
			return
		default:
		}
		a.postTyping(conversation)
		select {
		case <-t.stop:
			return
		case <-bound.C:
			a.mu.Lock()
			if a.typing[conversation] == t {
				delete(a.typing, conversation)
			}
			a.mu.Unlock()
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
