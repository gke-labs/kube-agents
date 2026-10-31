package dispatch

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The reason codes a dispatcher terminal starts with (contract, answer 4;
// addendum 1 adds the required-parameter check under parameters-invalid).
// PROVISIONAL until the contract settles them.
const (
	ReasonTierNotAllowed    = "tier-not-allowed"
	ReasonVerifierRefused   = "verifier-refused"
	ReasonParametersInvalid = "parameters-invalid"
	ReasonQueueTimeout      = "queue-timeout"
	ReasonNoWorker          = "no-worker"
	ReasonLeaseExpired      = "lease-expired"
	ReasonDeadlineExceeded  = "deadline-exceeded"
	ReasonStopped           = "stopped"
)

// waitBreaker is why a submission held back by a breaker is shown waiting.
const waitBreaker = "breaker-open"

const (
	// terminalEnvelopePrefix makes the dispatcher's terminal for a task one
	// envelope id, so a terminal re-published after a redelivery (the first
	// was published but the ack never landed) is dropped by the stream's
	// duplicate window rather than stored twice.
	terminalEnvelopePrefix = "env-dispatch-terminal-"
	// noticeRole is the A2A role of the terminal's message.
	noticeRole = "agent"
	// noticeTextKind is the A2A part kind of its text.
	noticeTextKind = "text"
	// noticeReasonPrefix and noticeDetailSeparator are the executors'
	// terminal-message convention, `reason: <token>[ - detail]`, which the
	// gateway's relay already parses (gateway/relay.go reasonToken).
	noticeReasonPrefix    = "reason: "
	noticeDetailSeparator = " - "
)

// noticeText is the terminal's message: the reason code as its first token,
// in the executors' `reason: <token>[ - detail]` form, so the gateway (which
// renders it) keys on it as it does on an executor's.
func noticeText(reason, detail string) string {
	if detail == "" {
		return noticeReasonPrefix + reason
	}
	return noticeReasonPrefix + reason + noticeDetailSeparator + detail
}

// publishTerminal is the dispatcher's one write to a task: a final status
// update on its `.supervisor` subject (lib.TaskSupervisorSubject, contract
// addendum 6), `rejected` for a submission refused at admission (nothing
// ran) or `failed` once a worker had it. `.supervisor`, not `.events`, so
// `.events` stays the executor's alone and replay can tell the two apart.
// Every terminal the dispatcher writes goes through here.
func (d *Dispatcher) publishTerminal(ctx context.Context, profile string, sub *lib.Envelope, state lib.TaskState, reason, detail string) error {
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    sub.TaskID,
		ContextID: sub.ContextID,
		Status: lib.TaskStatus{State: state, Message: &lib.Message{
			Role: noticeRole, MessageID: "msg-" + nuid.Next(),
			TaskID: sub.TaskID, ContextID: sub.ContextID,
			Parts: []lib.Part{{Kind: noticeTextKind, Text: noticeText(reason, detail)}},
		}},
		Final: true,
	})
	if err != nil {
		return err
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: selfSession, AgentType: selfAgentType},
		sub.TaskID, sub.ContextID, sub.CorrelationID, payload,
		lib.WithEnvelopeID(terminalEnvelopePrefix+sub.TaskID))
	if err != nil {
		return fmt.Errorf("build the %s terminal: %w", reason, err)
	}
	return d.bus.Publish(ctx, lib.TaskSupervisorSubject(profile, sub.TaskID), env)
}
