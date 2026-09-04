package lib

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/nats-io/nats.go/jetstream"
)

// TasksStream is the JetStream stream holding a2a.tasks.> (provisioned by the
// deployment, W2).
const TasksStream = "TASKS"

// Task is the A2A Task materialized by folding a task's event stream —
// tasks/get with no live executor required.
type Task struct {
	ID            string
	ContextID     string
	CorrelationID string
	State         TaskState
	Final         bool
	StatusHistory []TaskState
	Artifacts     []Artifact
	// PostFinalDropped counts events that arrived after the final event and
	// were dropped from the fold (assertion 10): surfaced as a warning and a
	// metric by the caller, never allowed to disturb the terminal state or
	// kill the fold.
	PostFinalDropped int
}

// Artifact returns the merged artifact with the given name, or nil.
func (t *Task) Artifact(name string) *Artifact {
	for i := range t.Artifacts {
		if t.Artifacts[i].Name == name {
			return &t.Artifacts[i]
		}
	}
	return nil
}

// FoldTask folds a task's events (status-update and artifact-update
// envelopes, in stream order) into a Task. Events after the final one are
// dropped and counted in PostFinalDropped (assertion 10) — the caller
// surfaces them as a warning and a metric; the fold survives.
func FoldTask(taskID string, events []*Envelope) (*Task, error) {
	task := &Task{ID: taskID}
	for _, env := range events {
		if env.TaskID != taskID {
			return nil, &ProtocolError{Msg: fmt.Sprintf("event for task %q on task %q's stream", env.TaskID, taskID)}
		}
		if task.Final {
			// Assertion 10: nothing follows the final event. The violation is
			// surfaced (warning + metric, by the caller) and the event
			// dropped; the fold survives - a hostile post-final write must
			// not revoke tasks/get.
			task.PostFinalDropped++
			continue
		}
		if task.CorrelationID == "" {
			task.CorrelationID = env.CorrelationID
		}
		if task.ContextID == "" {
			task.ContextID = env.ContextID
		}
		switch env.Kind {
		case KindStatusUpdate:
			var s StatusUpdate
			if err := json.Unmarshal(env.Payload, &s); err != nil {
				return nil, &ProtocolError{Msg: fmt.Sprintf("malformed status-update %s: %v", env.EnvelopeID, err)}
			}
			if s.TaskID != "" && s.TaskID != taskID {
				return nil, &ProtocolError{Msg: fmt.Sprintf("status-update %s payload names task %q inside task %q", env.EnvelopeID, s.TaskID, taskID)}
			}
			task.State = s.Status.State
			task.Final = s.Final
			task.StatusHistory = append(task.StatusHistory, s.Status.State)
		case KindArtifactUpdate:
			var a ArtifactUpdate
			if err := json.Unmarshal(env.Payload, &a); err != nil {
				return nil, &ProtocolError{Msg: fmt.Sprintf("malformed artifact-update %s: %v", env.EnvelopeID, err)}
			}
			if a.TaskID != "" && a.TaskID != taskID {
				return nil, &ProtocolError{Msg: fmt.Sprintf("artifact-update %s payload names task %q inside task %q", env.EnvelopeID, a.TaskID, taskID)}
			}
			task.mergeArtifact(a)
		default:
			return nil, &ProtocolError{Msg: fmt.Sprintf("kind %q on an events subject", env.Kind)}
		}
	}
	return task, nil
}

// mergeArtifact applies one artifact-update: append chunks extend the
// artifact's parts per A2A chunking rules, otherwise the update replaces or
// introduces the artifact.
func (t *Task) mergeArtifact(u ArtifactUpdate) {
	key := u.Artifact.ArtifactID
	if key == "" {
		key = u.Artifact.Name
	}
	for i := range t.Artifacts {
		k := t.Artifacts[i].ArtifactID
		if k == "" {
			k = t.Artifacts[i].Name
		}
		if k == key {
			if u.Append {
				t.Artifacts[i].Parts = append(t.Artifacts[i].Parts, u.Artifact.Parts...)
			} else {
				t.Artifacts[i] = u.Artifact
			}
			return
		}
	}
	t.Artifacts = append(t.Artifacts, u.Artifact)
}

// TasksGet replays the task's events subject from sequence 1 on an ephemeral
// ordered consumer and folds the result — the durability payoff: no live
// executor required.
func (c *Client) TasksGet(ctx context.Context, addressee, taskID string) (*Task, error) {
	_, js := c.conn()
	subject := TaskEventsSubject(addressee, taskID)
	stream, err := js.Stream(ctx, TasksStream)
	if err != nil {
		return nil, fmt.Errorf("stream %s: %w", TasksStream, err)
	}
	// Snapshot the replay horizon first: fold what the stream holds now, and
	// terminate deterministically even while the task is still emitting.
	last, err := stream.GetLastMsgForSubject(ctx, subject)
	if err != nil {
		if errors.Is(err, jetstream.ErrMsgNotFound) {
			// No events in the retention window: the A2A answer is
			// TaskNotFound, not an empty Task indistinguishable from a broken
			// one.
			return nil, &A2AError{Code: CodeTaskNotFound, Message: fmt.Sprintf("task %q has no events in the retention window", taskID)}
		}
		return nil, fmt.Errorf("replay horizon for %s: %w", taskID, err)
	}
	cons, err := js.OrderedConsumer(ctx, TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{subject},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	if err != nil {
		return nil, fmt.Errorf("ordered consumer for %s: %w", taskID, err)
	}
	it, err := cons.Messages()
	if err != nil {
		return nil, fmt.Errorf("replay messages for %s: %w", taskID, err)
	}
	defer it.Stop()
	// it.Next does not observe ctx on its own; stopping the iterator is what
	// unblocks it, so a canceled context cannot hang the replay.
	stopWatch := context.AfterFunc(ctx, it.Stop)
	defer stopWatch()
	var events []*Envelope
	for {
		msg, err := it.Next()
		if err != nil {
			if ctx.Err() != nil {
				return nil, fmt.Errorf("replay for %s: %w", taskID, ctx.Err())
			}
			return nil, fmt.Errorf("replay next for %s: %w", taskID, err)
		}
		meta, err := msg.Metadata()
		if err != nil {
			return nil, fmt.Errorf("replay metadata for %s: %w", taskID, err)
		}
		env, err := ParseEnvelope(msg.Data())
		if err != nil {
			// A hostile or foreign write must not revoke tasks/get for the
			// task: the live path terms poison and keeps going, so replay
			// skips it the same way rather than failing the whole fold.
			c.log.Error("a2a replay skipping unparseable event", "subject", subject, "err", err)
		} else if env.Kind != KindStatusUpdate && env.Kind != KindArtifactUpdate {
			// Replay-only screen, not live parity: the live path delivers a
			// foreign kind on .events to the handler, where FoldTask surfaces
			// it as a ProtocolError. Replay's job is narrower - one foreign
			// write must not revoke tasks/get for the task.
			c.log.Error("a2a replay skipping non-event kind", "subject", subject, "kind", env.Kind)
		} else if env.TaskID != taskID {
			// The fourth poison class: a valid event for another task on this
			// subject. FoldTask would hard-error on it; the screen drops it so
			// one foreign write cannot revoke tasks/get (payload-level taskId
			// mismatches never get this far - ParseEnvelope refuses them).
			c.log.Error("a2a replay skipping event for another task", "subject", subject, "taskId", env.TaskID)
		} else if env.To != nil && env.To.Session != addressee {
			c.log.Error("a2a replay skipping to/addressee mismatch", "subject", subject, "to", env.To.Session)
		} else {
			events = append(events, env)
		}
		// Two exits: the snapshotted horizon, or nothing left pending — the
		// horizon message itself may have aged out between snapshot and
		// replay, and waiting for it then would block forever.
		if meta.Sequence.Stream >= last.Sequence || meta.NumPending == 0 {
			break
		}
	}
	task, err := FoldTask(taskID, events)
	if err != nil {
		return nil, err
	}
	if task.PostFinalDropped > 0 {
		c.protocolViolations.Add(int64(task.PostFinalDropped))
		c.log.Warn("a2a events after final dropped from fold",
			"task", taskID, "dropped", task.PostFinalDropped)
	}
	return task, nil
}

// ValidateArtifacts enforces assertion 18: a completed task carries at least
// one result artifact, and reserved names carry only their defined content —
// result is the deliverable, thinking and progress are text, activity is the
// structured tool-call trace.
func (t *Task) ValidateArtifacts() error {
	if t.State == StateCompleted && t.Artifact(ArtifactResult) == nil {
		return &ProtocolError{Msg: fmt.Sprintf("task %q completed without a result artifact", t.ID)}
	}
	for _, a := range t.Artifacts {
		switch a.Name {
		case ArtifactThinking, ArtifactProgress:
			for _, p := range a.Parts {
				if p.Kind != "text" {
					return &ProtocolError{Msg: fmt.Sprintf("artifact %q carries a %q part; reserved name is text-only", a.Name, p.Kind)}
				}
			}
		case ArtifactActivity:
			for _, p := range a.Parts {
				if p.Kind != "data" {
					return &ProtocolError{Msg: fmt.Sprintf("artifact %q carries a %q part; the tool-call trace is data parts", a.Name, p.Kind)}
				}
			}
		}
	}
	return nil
}
