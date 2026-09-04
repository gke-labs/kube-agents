package lib

import (
	"context"
	"encoding/json"
	"fmt"
)

// TaskExecution is the executor side of one task: it binds the originating
// message's taskId, contextId, and correlationId so every event the task
// emits carries them (assertion 15), with correlationId copied verbatim,
// never re-minted (assertion 14).
type TaskExecution struct {
	c             *Client
	from          Party
	addressee     string
	taskID        string
	contextID     string
	correlationID string
}

// NewTaskExecution derives an execution from the originating kind:message
// envelope. addressee is the executor's own name — the profile or session
// token its task subjects carry (0.4); the worker gets it from its PROFILE
// env, the gateway from the session it spawned.
func (c *Client) NewTaskExecution(origin *Envelope, from Party, addressee string) (*TaskExecution, error) {
	if origin == nil {
		return nil, &ProtocolError{Msg: "nil originating envelope"}
	}
	if addressee == "" {
		return nil, &ProtocolError{Msg: "task execution requires the executor's addressee token"}
	}
	if origin.To != nil && origin.To.Session != addressee {
		return nil, &ProtocolError{Msg: fmt.Sprintf("originating message addressed to %q, executor is %q", origin.To.Session, addressee)}
	}
	if origin.Kind != KindMessage {
		return nil, &ProtocolError{Msg: fmt.Sprintf("task execution originates from a kind:message envelope, got %q", origin.Kind)}
	}
	if origin.TaskID == "" || origin.ContextID == "" || origin.CorrelationID == "" {
		return nil, &ProtocolError{Msg: "originating message missing taskId, contextId, or correlationId"}
	}
	return &TaskExecution{
		c:             c,
		from:          from,
		addressee:     addressee,
		taskID:        origin.TaskID,
		contextID:     origin.ContextID,
		correlationID: origin.CorrelationID,
	}, nil
}

// StatusEnvelope builds a status-update event for this task.
func (x *TaskExecution) StatusEnvelope(state TaskState, final bool, opts ...EnvelopeOption) (*Envelope, error) {
	payload, err := json.Marshal(StatusUpdate{
		TaskID:    x.taskID,
		ContextID: x.contextID,
		Status:    TaskStatus{State: state},
		Final:     final,
	})
	if err != nil {
		return nil, err
	}
	return NewStatusUpdateEnvelope(x.from, x.taskID, x.contextID, x.correlationID, payload, opts...)
}

// ArtifactEnvelope builds an artifact-update event for this task.
func (x *TaskExecution) ArtifactEnvelope(a Artifact, opts ...EnvelopeOption) (*Envelope, error) {
	payload, err := json.Marshal(ArtifactUpdate{
		TaskID:    x.taskID,
		ContextID: x.contextID,
		Artifact:  a,
	})
	if err != nil {
		return nil, err
	}
	return NewArtifactUpdateEnvelope(x.from, x.taskID, x.contextID, x.correlationID, payload, opts...)
}

// PublishStatus builds and publishes a status-update on the task's events
// subject.
func (x *TaskExecution) PublishStatus(ctx context.Context, state TaskState, final bool, opts ...EnvelopeOption) error {
	env, err := x.StatusEnvelope(state, final, opts...)
	if err != nil {
		return err
	}
	return x.c.Publish(ctx, TaskEventsSubject(x.addressee, x.taskID), env)
}

// PublishArtifact builds and publishes an artifact-update on the task's
// events subject.
func (x *TaskExecution) PublishArtifact(ctx context.Context, a Artifact, opts ...EnvelopeOption) error {
	env, err := x.ArtifactEnvelope(a, opts...)
	if err != nil {
		return err
	}
	return x.c.Publish(ctx, TaskEventsSubject(x.addressee, x.taskID), env)
}
