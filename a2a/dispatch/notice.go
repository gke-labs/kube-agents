package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The reason codes a dispatcher terminal starts with (contract, answer 4;
// addendum 1 adds the required-parameter check under parameters-invalid).
// PROVISIONAL until the contract settles them.
const (
	ReasonTierNotAllowed = "tier-not-allowed"
	// ReasonTierWithdrawn refuses a submission queued at a tier its profile
	// drains (lib.DispatchProfile.DrainingTiers, #3659): the tier left the
	// profile after the submission was queued.
	ReasonTierWithdrawn     = "tier-withdrawn"
	ReasonVerifierRefused   = "verifier-refused"
	ReasonParametersInvalid = "parameters-invalid"
	ReasonQueueTimeout      = "queue-timeout"
	ReasonNoWorker          = "no-worker"
	ReasonLeaseExpired      = "lease-expired"
	ReasonDeadlineExceeded  = "deadline-exceeded"
	ReasonStopped           = "stopped"
	// ReasonChangeNeedsJob refuses a task whose confirmed change would put
	// a write permit on its credential while its tier has no Job runner: a
	// permit-carrying task runs in its own one-pod Job, never on a pool pod.
	ReasonChangeNeedsJob = "change-needs-job"
	// ReasonTierEnded ends a task whose tier an administrator ended the
	// drain of (lib.DrainingTier.Ending, #5850), held or with its Job still
	// starting (#6134): canceled, not failed.
	ReasonTierEnded = "tier-ended"
	// The re-check of a child whose parent is an AgentProfile task (#5686):
	// the gateway checks the same at mint, and the dispatcher holds a child
	// an older gateway sent, or one that breaks either rule, to them.
	// ReasonParentUnchecked: the child carries no parent scope, so no gateway
	// checked its chain and scope.
	ReasonParentUnchecked = "parent-unchecked"
	// ReasonParentCycle: the child's profile is already on its parent's
	// chain, the chain head's one self-nest aside.
	ReasonParentCycle = "parent-cycle"
	// ReasonOutsideParentScope: the child's target lies outside its parent's
	// scope.
	ReasonOutsideParentScope = "outside-parent-scope"
	// ReasonScriptNotAllowed: the submission's trigger names a script its
	// profile's spec.scripts doesn't list (#5622).
	ReasonScriptNotAllowed = "script-not-allowed"
	// ReasonTerminalUnpublished names a profile task settled with no
	// terminal on the bus, on a latched install (#5301): its six-token
	// terminal was refused until DroppedTerminalGiveUp, or taken as a
	// duplicate of a message that isn't it and refused again under a fresh
	// id (giveUpTerminal). The dispatcher can't write it on the task (the
	// write is what failed); it keys the Error log and the health line's
	// supervisorGiveUps, and the gateway reads it as one plain line wherever
	// a settle names it.
	ReasonTerminalUnpublished = "terminal-unpublished"
)

// waitBreaker is why a submission held back by a breaker is shown waiting.
const waitBreaker = "breaker-open"

// waitHandOffFailed is why a submission whose hand-off failed (a Job that
// couldn't be launched or read, a lease that couldn't be written) is shown
// waiting.
const waitHandOffFailed = "hand-off-failed"

const (
	// terminalEnvelopePrefix makes the dispatcher's terminal for a task one
	// envelope id (terminalEnvelopeID), so a terminal re-published after a
	// redelivery (the first was published but the ack never landed) is
	// dropped by the stream's duplicate window rather than stored twice.
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

// terminalEnvelopeID is the one envelope id of a task's dispatcher terminal.
// The TASKS stream's duplicate window is keyed on it alone, whatever the
// subject, and a task id is unique only within its (profile, tier, kind)
// durable, so it carries all four: a second profile's terminal for the same
// task id is its own message, not a duplicate of the first's. The parts are
// subject tokens, so none holds a dot and the dotted id is unambiguous.
func terminalEnvelopeID(hk holdKey) string {
	return terminalEnvelopePrefix + hk.profile + "." + hk.tier + "." + hk.kind + "." + hk.taskID
}

// publishTerminal is the dispatcher's one write to a task: a final status
// update on its `.supervisor` subject, the five-token
// lib.TaskSupervisorSubject or the six-token lib.ProfileSupervisorSubject as
// the dispatch config says (DispatchConfig.SupervisorSubjectFor; contract
// addendum 6, #5301), `rejected` for a submission refused at admission (nothing
// ran) or `failed` once a worker had it. `.supervisor`, not `.events`, so
// `.events` stays the executor's alone and replay can tell the two apart.
// Every terminal the dispatcher writes goes through here.
//
// A refused write is returned as it is: there is no second form to fall back
// to. On a latched install (the six-token form) readers past the install's
// cutoff don't take a five-token terminal as a profile task's, so a terminal
// written there would free the slot while the requester never sees the end.
// The callers retry it, bounded on a latched install, and then settle the task
// loudly without one (giveUpTerminal).
//
// On the six-token form a duplicate ack is checked rather than taken as the
// write landing (confirmDuplicateTerminal): the envelope id is fixed per task,
// and the stream's duplicate window is keyed on the id alone, whatever the
// subject, so a message stored first under the same id on another subject
// would otherwise swallow the real terminal.
func (d *Dispatcher) publishTerminal(ctx context.Context, hk holdKey, sub *lib.Envelope, state lib.TaskState, reason, detail string) error {
	env, err := terminalEnvelope(sub, state, reason, detail, lib.WithEnvelopeID(terminalEnvelopeID(hk)))
	if err != nil {
		return fmt.Errorf("build the %s terminal: %w", reason, err)
	}
	subject := d.src.Config().SupervisorSubjectFor(hk.profile, sub.TaskID)
	_, duplicate, err := d.busFor(hk.profile).PublishAck(ctx, subject, env)
	if err != nil || !duplicate || subject == lib.TaskSupervisorSubject(hk.profile, sub.TaskID) {
		return err
	}
	return d.confirmDuplicateTerminal(ctx, hk, sub, subject, state, reason, detail)
}

// terminalEnvelope is the terminal publishTerminal writes, in state, with
// reason and detail as its one text part.
func terminalEnvelope(sub *lib.Envelope, state lib.TaskState, reason, detail string, opts ...lib.EnvelopeOption) (*lib.Envelope, error) {
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
		return nil, err
	}
	return lib.NewStatusUpdateEnvelope(lib.Party{Session: selfSession, AgentType: selfAgentType},
		sub.TaskID, sub.ContextID, sub.CorrelationID, payload, opts...)
}

// errTerminalUnpublished is a terminal the stream took as a duplicate of a
// message that is not this task's terminal, whose one re-publish under a
// fresh id did not land either. The callers settle the task without a
// terminal at once (giveUpTerminal): retrying the fixed id meets the same
// duplicate for as long as the window lasts.
var errTerminalUnpublished = errors.New("the terminal was taken as a duplicate and no terminal is stored on the six-token supervisor subject")

// confirmDuplicateTerminal answers a duplicate ack for a six-token terminal.
// A duplicate is usually this dispatcher's own earlier write (a retry after an
// ack that never arrived, a redelivery after a restart), and then the
// six-token subject holds a final status from the dispatcher. The terminal's
// envelope id is predictable, though, and the duplicate window is keyed on
// the id alone, so another writer could have stored a message under it first
// on a subject readers don't take as this task's terminal. So the subject is
// read with a direct get: a final status from the dispatcher there is the
// write landing. With none, the terminal is published once more under a fresh
// id, which no earlier message can hold; if that fails too, or is again a
// duplicate, errTerminalUnpublished.
//
// A read that fails (no grant for the direct get, a stream without direct
// gets, a connection mid-reconnect) can't tell the two apart, and is not
// taken as the write landing: a writer able to plant the id could otherwise
// win by making the read fail. It takes the re-publish too. The cost is an
// ordinary retry whose first write did land getting a second identical
// terminal on the subject, which a reader folds past (the first final ends
// the fold).
func (d *Dispatcher) confirmDuplicateTerminal(ctx context.Context, hk holdKey, sub *lib.Envelope, subject string, state lib.TaskState, reason, detail string) error {
	log := d.log.With("profile", hk.profile, "tier", hk.tier, "kind", hk.kind, "task", hk.taskID, "subject", subject)
	stored, err := d.storedTerminal(ctx, hk.profile, subject, sub.TaskID)
	switch {
	case err != nil:
		log.Warn("a profile task's terminal was taken as a duplicate and the six-token subject can't be read to confirm it; publishing it once under a fresh id", "err", err)
	case stored:
		return nil
	default:
		log.Warn("a profile task's terminal was taken as a duplicate, but no terminal is stored on the six-token supervisor subject; publishing it once under a fresh id")
	}
	env, err := terminalEnvelope(sub, state, reason, detail)
	if err != nil {
		return fmt.Errorf("build the %s terminal: %w", reason, err)
	}
	pctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), kvOpTimeout)
	defer cancel()
	_, duplicate, err := d.busFor(hk.profile).PublishAck(pctx, subject, env)
	switch {
	case err != nil:
		return fmt.Errorf("%w; the re-publish under a fresh id failed: %v", errTerminalUnpublished, err)
	case duplicate:
		return fmt.Errorf("%w; the re-publish under a fresh id was a duplicate too", errTerminalUnpublished)
	}
	return nil
}

// storedTerminal reports whether the newest message on a task's six-token
// supervisor subject is the dispatcher's final status for the task, by a
// direct get on TASKS (no consumer, no stream info). Nothing stored is false;
// an error is the read failing.
func (d *Dispatcher) storedTerminal(ctx context.Context, profile, subject, taskID string) (bool, error) {
	rctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), stopCheckTimeout)
	defer cancel()
	msg, err := d.busFor(profile).Conn().RequestWithContext(rctx, directGetPrefix+lib.TasksStream+"."+subject, nil)
	if err != nil {
		return false, err
	}
	switch s := msg.Header.Get("Status"); s {
	case "":
	case "404":
		return false, nil
	default:
		return false, fmt.Errorf("direct get on %s: status %s %s", lib.TasksStream, s, msg.Header.Get("Description"))
	}
	if got := msg.Header.Get("Nats-Subject"); got != subject {
		return false, nil
	}
	env, err := lib.ParseEnvelope(msg.Data)
	if err != nil {
		return false, nil
	}
	return env.TaskID == taskID && env.From.Session == selfSession && lib.IsFinalStatus(env), nil
}
