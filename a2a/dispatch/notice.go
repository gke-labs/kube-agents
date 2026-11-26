package dispatch

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"

	"github.com/nats-io/nats.go"
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
	// terminal was refused by the server's permissions until
	// DroppedTerminalGiveUp (giveUpTerminal).
	// The dispatcher can't write it on the task (the write is what failed);
	// it keys the Error log and the health line's supervisorGiveUps. Nothing
	// delivers it to the gateway, so no conversation hears it; the Error
	// log is an alert line (terminalUnpublishedAlert, #6884).
	ReasonTerminalUnpublished = "terminal-unpublished"
)

// waitBreaker is why a submission held back by a breaker is shown waiting.
const waitBreaker = "breaker-open"

// waitHandOffFailed is why a submission whose hand-off failed (a Job that
// couldn't be launched or read, a lease that couldn't be written) is shown
// waiting.
const waitHandOffFailed = "hand-off-failed"

const (
	// terminalEnvelopePrefix starts every dispatcher terminal's envelope id.
	// On the five-token form the rest is fixed per task (terminalEnvelopeID),
	// so a terminal re-published after a redelivery (the first was published
	// but the ack never landed) is dropped by the stream's duplicate window
	// rather than stored twice. On the six-token form the rest is random per
	// retry loop (sixTokenTerminalID), so no other writer can store a message
	// under it first.
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

// terminalEnvelopeID is the envelope id of a task's dispatcher terminal on
// the five-token form.
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
// Every terminal the dispatcher writes goes through here. r is the caller's
// retry state for this terminal, kept across its attempts (nil for a single
// attempt).
//
// A refused write is returned as it is: there is no second form to fall back
// to. On a latched install (the six-token form) readers past the install's
// cutoff don't take a five-token terminal as a profile task's, so a terminal
// written there would free the slot while the requester never sees the end.
// The callers retry it, bounded on a latched install, and then settle the task
// loudly without one (giveUpTerminal). A failure during which the server
// refused the publish for its permissions (an async "Permissions Violation for
// Publish to" the subject, seen on the connection while the attempt ran) wraps
// errSupervisorPublishRefused: only that counts toward the bound, so an outage
// (a disconnect, no responders, a timeout with no refusal) is retried for as
// long as it lasts.
//
// A duplicate ack is the write landing. The stream's duplicate window is
// keyed on the envelope id alone, whatever the subject, so on the six-token
// form the id is drawn at random (sixTokenTerminalID), kept in r while
// attempts succeed and drawn again after any that fails: no other writer can
// store a message under an id it can't predict, or one it saw on a send that
// wasn't stored, so a duplicate can only be this terminal's own earlier send
// (#6883). A predictable id would let a message planted under it first, on a
// subject readers don't take as this task's terminal, swallow the real one.
// The cost: a dispatcher that restarts between the publish and the settle
// re-publishes under a new id, so the subject can hold a second identical
// final status, which readers fold past (the first final ends the fold).
func (d *Dispatcher) publishTerminal(ctx context.Context, r *terminalRefusal, hk holdKey, sub *lib.Envelope, state lib.TaskState, reason, detail string) error {
	if r == nil {
		r = &terminalRefusal{}
	}
	subject := d.src.Config().SupervisorSubjectFor(hk.profile, sub.TaskID)
	id := terminalEnvelopeID(hk)
	if subject != lib.TaskSupervisorSubject(hk.profile, sub.TaskID) {
		if r.id == "" {
			var err error
			if r.id, err = sixTokenTerminalID(); err != nil {
				return fmt.Errorf("draw the %s terminal's envelope id: %w", reason, err)
			}
		}
		id = r.id
	}
	env, err := terminalEnvelope(sub, state, reason, detail, lib.WithEnvelopeID(id))
	if err != nil {
		return fmt.Errorf("build the %s terminal: %w", reason, err)
	}
	c := d.busFor(hk.profile)
	refused := d.refusals.expect(c, subject)
	_, _, err = d.publishAckOn(ctx, c, subject, env)
	if err != nil {
		// A failed send may still have reached the subject's core
		// subscribers without being stored (a store timeout, a quorum
		// loss, a full stream), and one of them could plant the id it saw
		// before the retry. The retry draws a new one: the id is never
		// reused once it may have been seen unstored. A failed send that
		// was stored after all (its ack lost) leaves a second identical
		// final status, which readers fold past.
		r.id = ""
	}
	if refused() && err != nil {
		return fmt.Errorf("%w: %w", errSupervisorPublishRefused, err)
	}
	return err
}

// sixTokenTerminalID is a six-token terminal's envelope id: the prefix and
// 128 bits from crypto/rand, which no other writer can predict.
func sixTokenTerminalID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return terminalEnvelopePrefix + hex.EncodeToString(b[:]), nil
}

// publishAckOn is c.PublishAck, or the test hook publishAck in its place.
func (d *Dispatcher) publishAckOn(ctx context.Context, c *lib.Client, subject string, env *lib.Envelope) (uint64, bool, error) {
	if d.publishAck != nil {
		return d.publishAck(ctx, c, subject, env)
	}
	return c.PublishAck(ctx, subject, env)
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

// errSupervisorPublishRefused marks a terminal publish that failed while the
// server refused the publish on its subject for the connection's permissions
// (publishRefusals): the one failure terminalGivenUp's bound counts.
var errSupervisorPublishRefused = errors.New("the server refused the publish on the supervisor subject (permissions violation)")

// publishRefusals hears, per connection, the server refusing a publish for
// the connection's permissions. nats.go gets such a refusal as an async
// error, "Permissions Violation for Publish to <subject>" (the publish call
// itself only times out, its ack never coming), which reaches the
// connection's error handler and so lib.Client.OnAsyncError. A terminal
// publish brackets its attempt with expect, so the bound in terminalGivenUp
// counts a failure only when the server said it was a refusal of that
// subject. The server answers a refused PUB with its -ERR at once, long
// before the attempt's ack timeout, so the refusal is heard before the
// attempt returns. Only subjects with an attempt in flight are kept.
type publishRefusals struct {
	mu      sync.Mutex
	watched map[*lib.Client]bool
	// inFlight is each (connection, subject) with an attempt running.
	inFlight map[refusalKey]*refusalCount
}

type refusalKey struct {
	c       *lib.Client
	subject string
}

type refusalCount struct {
	attempts int
	refused  uint64
}

// expect brackets one publish on subject over c: the func it returns, called
// once when the attempt is over, reports whether the server refused a publish
// on subject over c meanwhile. A nil c (a test's bare dispatcher) never
// reports one.
func (p *publishRefusals) expect(c *lib.Client, subject string) (refused func() bool) {
	if c == nil {
		return func() bool { return false }
	}
	p.watch(c)
	k := refusalKey{c, subject}
	p.mu.Lock()
	n := p.inFlight[k]
	if n == nil {
		n = &refusalCount{}
		p.inFlight[k] = n
	}
	n.attempts++
	before := n.refused
	p.mu.Unlock()
	return func() bool {
		p.mu.Lock()
		defer p.mu.Unlock()
		n.attempts--
		if n.attempts == 0 {
			delete(p.inFlight, k)
		}
		return n.refused > before
	}
}

// watch hooks c's async errors, once per connection.
func (p *publishRefusals) watch(c *lib.Client) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.watched[c] {
		return
	}
	if p.watched == nil {
		p.watched = map[*lib.Client]bool{}
		p.inFlight = map[refusalKey]*refusalCount{}
	}
	p.watched[c] = true
	c.OnAsyncError(func(err error) { p.observe(c, err) })
}

// observe counts err against its subject if it is a publish permission
// refusal with an attempt in flight.
func (p *publishRefusals) observe(c *lib.Client, err error) {
	subject, ok := refusedPublishSubject(err)
	if !ok {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if n := p.inFlight[refusalKey{c, subject}]; n != nil {
		n.refused++
	}
}

// publishViolationPrefix is the server's text for a refused publish, before
// the quoted subject (nats-server client.go, pubPermissionViolation).
const publishViolationPrefix = "Permissions Violation for Publish to "

// refusedPublishSubject is the subject a publish permission violation names.
// A refused reply subject ("... Publish with Reply of ...") or a subscribe
// refusal is not one.
func refusedPublishSubject(err error) (string, bool) {
	if !errors.Is(err, nats.ErrPermissionViolation) {
		return "", false
	}
	_, quoted, ok := strings.Cut(err.Error(), publishViolationPrefix)
	if !ok {
		return "", false
	}
	subject, uerr := strconv.Unquote(strings.TrimSpace(quoted))
	if uerr != nil {
		return "", false
	}
	return subject, true
}
