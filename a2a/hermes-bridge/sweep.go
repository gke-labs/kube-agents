package hermesbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// synthesizeCASAttempts bounds the horizon-read/fold/CAS-publish loop: each
// retry means a writer landed after our horizon, and three straight losses
// on a startup sweep is a fault worth surfacing, not a race worth waiting
// out.
const synthesizeCASAttempts = 3

// The in-flight registry: one KV entry per accepted task, written before
// submitted is published and deleted after the terminal event. The sweep
// reads it on startup, so a bridge death mid-task costs an honest terminal
// failed rather than a task that hangs open forever.
//
// The sweep assumes incarnations are serial - the kubelet restarts the
// sidecar container in place, and the operator's agent Deployment is
// strategy Recreate, so two bridges never run at once. Real fencing for
// overlapping executors is the stage-3 dispatcher's problem, not scaffolding
// this component grows.

func (b *Bridge) kvKey(taskID string) string {
	return "bridge." + b.cfg.Profile + "." + taskID
}

func (b *Bridge) markInFlight(ctx context.Context, taskID string) error {
	_, err := b.kv.Put(ctx, b.kvKey(taskID), []byte(b.from.Session))
	return err
}

func (b *Bridge) clearInFlight(ctx context.Context, taskID string) {
	if err := b.kv.Delete(ctx, b.kvKey(taskID)); err != nil {
		b.cfg.Logger.Warn("in-flight registry delete failed", "task", taskID, "err", err)
	}
}

// errTaskStillLive marks a sweep target whose events subject kept moving
// under the CAS - something is executing it, so the sweep must not finalize
// it and must not crash the bridge over it. The key stays for a later look.
var errTaskStillLive = errors.New("task events still advancing; leaving it alone")

// sweep finalizes tasks a prior incarnation accepted and never finished.
// Runs before the durable consumer starts, so nothing it reads is a task
// this incarnation is executing.
func (b *Bridge) sweep(ctx context.Context) error {
	// Keys (not ListKeys): the lister's channel ends silently on a
	// disconnect or cancellation, and a truncated listing here reads as "no
	// orphans" - Keys surfaces the error instead.
	all, err := b.kv.Keys(ctx)
	if err != nil && !errors.Is(err, jetstream.ErrNoKeysFound) {
		return fmt.Errorf("list in-flight keys: %w", err)
	}
	prefix := "bridge." + b.cfg.Profile + "."
	for _, key := range all {
		if !strings.HasPrefix(key, prefix) {
			continue
		}
		taskID := strings.TrimPrefix(key, prefix)
		if err := b.sweepTask(ctx, taskID); err != nil {
			if errors.Is(err, errTaskStillLive) {
				b.cfg.Logger.Warn("sweep found a task still emitting events; keeping its key",
					"task", taskID)
				continue
			}
			return fmt.Errorf("sweep task %s: %w", taskID, err)
		}
		if err := b.kv.Delete(ctx, key); err != nil {
			b.cfg.Logger.Warn("sweep key delete failed", "task", taskID, "err", err)
		}
	}
	return nil
}

func (b *Bridge) sweepTask(ctx context.Context, taskID string) error {
	task, err := b.c.TasksGet(ctx, b.cfg.Profile, taskID)
	switch {
	case isTaskNotFound(err):
		// Died between the KV put and the submitted publish: the unacked
		// submission redelivers, so there is nothing to finalize.
		return nil
	case err != nil:
		return err
	case task.Final:
		// Died between the terminal publish and the KV delete.
		return nil
	}
	b.cfg.Logger.Warn("sweeping orphaned task to terminal failed", "task", taskID, "state", task.State)
	return b.synthesizeTerminal(ctx, taskID,
		lib.StateFailed, "reason: bridge-died-without-terminal-event")
}

// synthesizeTerminal writes a terminal event on behalf of a task with no
// live executor. Compare-and-swap per the profiles spec, not read-then-write
// - and the expected sequence is read BEFORE the fold, so any event landing
// after the horizon (a dying process's flush included) fails the CAS and
// the next iteration's fold sees it. Whichever writer loses lands in the
// warn-and-drop path like any other post-final event.
func (b *Bridge) synthesizeTerminal(ctx context.Context, taskID string, state lib.TaskState, reason string) error {
	subject := lib.TaskEventsSubject(b.cfg.Profile, taskID)
	stream, err := b.js.Stream(ctx, lib.TasksStream)
	if err != nil {
		return err
	}
	for attempt := 0; attempt < synthesizeCASAttempts; attempt++ {
		// Horizon first, fold second: the CAS baseline must never be newer
		// than what the fold judged non-final.
		last, err := stream.GetLastMsgForSubject(ctx, subject)
		if err != nil {
			if errors.Is(err, jetstream.ErrMsgNotFound) {
				return nil // no events in the window; nothing to finalize
			}
			return fmt.Errorf("last event for %s: %w", taskID, err)
		}
		task, err := b.c.TasksGet(ctx, b.cfg.Profile, taskID)
		if err != nil {
			if isTaskNotFound(err) {
				return nil
			}
			return err
		}
		if task.Final {
			return nil
		}
		payload, err := json.Marshal(lib.StatusUpdate{
			TaskID:    taskID,
			ContextID: task.ContextID,
			Status: lib.TaskStatus{
				State: state,
				Message: &lib.Message{
					Role:      "agent",
					MessageID: "msg-" + nuid.Next(),
					Parts:     []lib.Part{{Kind: "text", Text: reason}},
					TaskID:    taskID,
					ContextID: task.ContextID,
				},
			},
			Final: true,
		})
		if err != nil {
			return err
		}
		env, err := lib.NewStatusUpdateEnvelope(b.from, taskID, task.ContextID, task.CorrelationID, payload)
		if err != nil {
			return err
		}
		data, err := json.Marshal(env)
		if err != nil {
			return err
		}
		_, err = b.js.Publish(ctx, subject, data,
			jetstream.WithMsgID(env.EnvelopeID),
			jetstream.WithExpectLastSequencePerSubject(last.Sequence))
		if err == nil {
			return nil
		}
		var apiErr *jetstream.APIError
		if !errors.As(err, &apiErr) || apiErr.ErrorCode != jetstream.JSErrCodeStreamWrongLastSequence {
			return fmt.Errorf("synthesize terminal for %s: %w", taskID, err)
		}
		// Lost the CAS: something wrote after the horizon. Go around - the
		// next fold sees the write, terminal or not.
	}
	return errTaskStillLive
}
