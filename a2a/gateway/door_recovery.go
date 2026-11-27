package gateway

import (
	"context"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// SetTaskLocator receives the door task index (TaskLocatorSink).
func (d *A2ADoor) SetTaskLocator(locate TaskLocator) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.locate = locate
}

// recoverTask is the read for a task this door doesn't hold for caller (a
// handover, a restart, an eviction): the door task index names the
// conversation whose record lists the task, the record must be caller's own
// conversation, and the probe's fold of the record and the stream gives the
// task's state, terminal and result. It reports whether the task is now
// held for caller (#7454).
//
// The record decides whether a task without a terminal on its stream is
// still running: only the record's active task reads as working. Anything
// else, a terminal past the stream's window or an end the record knows and
// the stream doesn't, stays a miss: the not-found answer, with mayHaveRun,
// rather than a task that reads as running forever.
//
// Ownership is the conversation key: the index must name one under caller's
// own prefix (a2a:<caller>:) before anything of the task is read. A caller
// never contains a colon (callerOf refuses one), which is what already keeps
// two callers from spelling one conversation key, so the prefix is caller's
// alone. The task's contextId is the rest of the key: the door's, which is
// what the caller sent or was given, not the record's bus contextId. A miss
// for any reason is the same miss, so the answer confirms nothing about
// another caller's ids.
func (d *A2ADoor) recoverTask(ctx context.Context, taskID, caller string) bool {
	if !lib.ValidSubjectToken(taskID) {
		// Not a task id: nothing is formatted into a KV key or a subject.
		return false
	}
	d.mu.Lock()
	locate, probe := d.locate, d.probe
	if held := d.tasks[taskID]; held != nil && held.caller != "" {
		// Held for someone else (taskFor already missed for caller).
		d.mu.Unlock()
		return false
	}
	d.mu.Unlock()
	if locate == nil || probe == nil {
		return false
	}
	key, err := locate(ctx, taskID)
	if err != nil {
		d.log.Warn("the A2A door could not look up a task it doesn't hold", "taskId", taskID, "err", err)
		return false
	}
	contextID, own := strings.CutPrefix(key, a2aConversationKey(caller, ""))
	if !own || contextID == "" || strings.ContainsRune(caller, ':') {
		return false
	}
	st, err := probe(ctx, key, taskID)
	if err != nil {
		d.log.Warn("the A2A door could not read a task it doesn't hold", "taskId", taskID, "err", err)
		return false
	}
	var state lib.TaskState
	switch {
	case st.Final:
		state = st.ExecutorState
	case st.Active && st.TaskID == taskID:
		state = st.ExecutorState
		if state == "" {
			state = lib.StateSubmitted
		}
	default:
		return false
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	task := d.tasks[taskID]
	if task != nil && task.caller != "" && task.caller != caller {
		return false
	}
	if task == nil {
		task = &a2aTask{id: taskID, created: time.Now()}
		d.taskOrder = append(d.taskOrder, taskID)
		for len(d.taskOrder) > a2aMaxTasks {
			d.evictOldestTaskLocked(task)
		}
		d.tasks[taskID] = task
	}
	task.caller, task.contextID, task.key = caller, contextID, key
	task.recovered = true
	task.updated = time.Now()
	if !task.terminal {
		// What the relay told this door since the handover, a stub with its
		// terminal, stays; otherwise the fold's word is taken.
		task.state = state
		if st.Final {
			task.terminal = true
			task.source = st.TerminalSource
			task.reason = truncateRunes(st.Reason, injectMaxEntryBytes)
		}
	}
	if st.Final && state == lib.StateCompleted && !task.delivered && st.Result != "" {
		result := st.Result
		if len(result) > a2aMaxResultBytes {
			task.resultCutFrom = len(result)
			result = truncateRunes(result, a2aMaxResultBytes)
		}
		d.chargeLocked(task, len(result)-len(task.result))
		task.result, task.delivered = result, true
	}
	conv := d.conversationLocked(key)
	if conv.caller == "" {
		conv.caller, conv.contextID = caller, contextID
	}
	if !task.terminal && conv.active == nil {
		conv.active = task
	}
	d.enforceBudgetLocked(task)
	return d.tasks[taskID] == task
}

// heldOrRecovered is taskFor, falling back to recoverTask on a miss.
func (d *A2ADoor) heldOrRecovered(ctx context.Context, taskID, caller string) (*a2aTask, bool) {
	if task, ok := d.taskFor(taskID, caller); ok {
		return task, true
	}
	if !d.recoverTask(ctx, taskID, caller) {
		return nil, false
	}
	return d.taskFor(taskID, caller)
}
