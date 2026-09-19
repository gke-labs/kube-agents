// Package gateway implements the chatops gateway: adapters, a session
// manager, and a bus client. It is deterministic code — no prompt, no tools,
// nothing to inject into (spec-chatops-gateway.md, "The gateway holds no
// model"). The judgment the demo gateway exercised lives in the executors.
package gateway

import (
	"context"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// InboundMessage is one chat message, normalized across backends. AuthorID is
// the sender's id as the backend's own identity mechanism reported it — the
// immutable snowflake over Discord's authenticated gateway websocket, the
// Google-asserted email on Google Chat, where the email IS the id
// (spec-chatops-gateway.md, "The Google Chat adapter"). Verification —
// against the principal map, or the allowlist — happens in the session
// manager; adapters never see principals.
type InboundMessage struct {
	// Conversation is the backend-qualified conversation id — the session key
	// (eg discord:1234/5678). A channel or space is not a session; a
	// conversation in it is.
	Conversation string
	// Kind is "dm" or "group".
	Kind string
	// AuthorID is the sender id in the backend's own identity vocabulary.
	AuthorID string
	// MessageID is the backend-native message id, recorded against the
	// correlationId in the ingress log so the audit chain runs chat message ->
	// correlationId -> every hop -> change.
	MessageID string
	// Text is the message content.
	Text string

	// Backend names the ingress this message arrived through, and empty
	// means the gateway's own configured backend. It exists because one
	// gateway can now have two ingresses: the inject side door may be armed
	// beside a real backend, and attribution is a property of the message
	// rather than of the process. A message that says nothing is attributed
	// to the configured backend, which is every chat backend's case.
	Backend string

	// Intent, when set, is a control action the backend expressed directly
	// rather than a message to be understood. Empty is an ordinary message.
	Intent string
	// TaskID, when set beside Intent, names the task the control action is
	// about. Empty means the conversation's active task, which is every chat
	// backend's case. A program that was answered with a task id names it,
	// so its cancel still reaches the bus when the record has released the
	// task in the meantime (the never-started heal runs before routing, and
	// a submission nobody took is still on the in subject for a bridge that
	// binds later to run).
	TaskID string
}

// IntentCancel is the only Intent today: stop the conversation's running
// task. The chat backends have no way to express it -- a human types "stop"
// and the text matcher reads it -- but a program should not have to spell a
// phrase to reach a control path, and a phrase that changes meaning is a
// class of bug an API does not have. The inject door has an explicit route
// for it, and it lands on the bus as kind: cancel exactly as the text route
// does.
const IntentCancel = "cancel"

// Adapter is the five-operation backend interface from the gateway design:
// inbound message with verified sender, conversation and thread identity
// (both carried on InboundMessage), roster read, post-to-conversation, and
// openDirect. If a backend leaks backend-isms through this interface, that is
// a bug in the interface.
type Adapter interface {
	// Run delivers inbound messages to handler until ctx is done. The adapter
	// only delivers messages whose sender the backend itself authenticated.
	Run(ctx context.Context, handler func(InboundMessage)) error

	// Post writes text to a conversation and returns the backend message id,
	// used by the rolling progress line's Edit.
	Post(conversation, text string) (messageID string, err error)

	// Edit replaces the text of a previously posted message — the rolling
	// progress line edits one message as progress artifacts arrive, at zero
	// model cost.
	Edit(conversation, messageID, text string) error

	// Roster returns the members of a conversation in the same vocabulary
	// as AuthorID (so a member who is also the requester matches), and
	// whether the list is complete. The session manager pseudonymizes and
	// caps it; adapters return it raw.
	Roster(conversation string) (ids []string, complete bool, err error)

	// OpenDirect returns a DM conversation id for a backend user — the
	// DM-switch primitive. The gateway ships the primitive; the classifier
	// that decides to use it comes later. Until then everything posts to the
	// room it came from.
	OpenDirect(userID string) (conversation string, err error)
}

// TaskObserver is the optional extension an Adapter implements when it has to
// answer questions ABOUT a task rather than only render one. The gateway type
// asserts for it and calls it where it mints and retires tasks; an adapter
// that does not implement it sees no change at all, which is every chat
// backend — a human reads the chat, so the chat text is the whole interface.
//
// The inject backend is the case that needs more. Its caller is a program: it
// posts a message and has to know which task that started and when that task
// ended, and the only other way to learn either is to parse the rendered chat
// text — "⏳ submitted…" and "✅ **completed**" — which is presentation and is
// free to change. Passing the two ids the gateway already has in hand costs
// nothing and makes the eval transport independent of how the relay words
// itself.
//
// Both methods are called on the conversation's own worker (the inbox queue
// for TaskStarted, the relay queue for TaskTerminal) while the session lock is
// held, so an implementation must not block: record and return.
type TaskObserver interface {
	// TaskStarted names the task a turn on this conversation minted. Called
	// after the id exists and BEFORE the placeholder is posted, so a caller
	// watching for both sees the id first and never has to guess whether a
	// post belongs to the task it just submitted.
	TaskStarted(conversation, taskID string)

	// TaskTerminal names the state a task ended in, who says so, and the
	// executor's own reason for it -- the text of the terminal status
	// event's message, which the bridge and the worker adapter write as
	// `reason: <token>[ - detail]`, or "" when the terminal carried none.
	// Called after the relay has posted the deliverable and edited the
	// rolling line, so a caller that sees this has already seen everything
	// the conversation received for that task. The reason is passed through
	// verbatim: whether a token names the persona's failure or the
	// executor's own (bridge-shutdown, spawn-failed) is the caller's
	// classification, made against the executor's definitions.
	TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string)

	// TaskAccepted says the submission for a task this conversation started
	// is on the task's `in` subject. Called after the publish returns and
	// before the session pod, if any, is created.
	//
	// It exists because TaskStarted is announced before the placeholder and
	// before the publish, so the id alone does not mean an executor can ever
	// see the ask: startTask's publish may fail, and then it edits the
	// placeholder, clears ActiveTask and returns, leaving nothing on any
	// subject. A program that took the id as the answer would wait out its
	// whole budget for a terminal no executor can publish. A caller that
	// wants the two ends of a task still watches TaskStarted and
	// TaskTerminal; a caller that wants to know whether its submission was
	// taken watches this.
	TaskAccepted(conversation, taskID string)

	// CancelPublished says a kind:cancel envelope for taskID is on the
	// task's `in` subject. Called after the publish returns, on both cancel
	// routes (the active task's and a named task the conversation no longer
	// holds).
	//
	// Signalled on success only, like TaskAccepted, and read against the end
	// of the turn: the gateway's answer to a cancel it did not publish is a
	// posted line saying why -- the conversation never held the task, the
	// task predates the record's correlation ids, the publish failed -- and
	// a program that had to tell those from a success would be matching on
	// that prose. Absent at the turn's end, the cancel did not go.
	CancelPublished(conversation, taskID string)
}

// InboundObserver is the optional extension an Adapter implements when it has
// to know what became of a message it handed over, rather than only what the
// conversation received. Every chat backend ignores both: a human reads the
// room and knows whether they were answered.
//
// A program cannot. It needs the two facts the transcript does not carry.
//
// The gateway drops a message whose sender it cannot verify, and tells the
// sender once per sender rather than once per message: a person who has been
// told they are unknown does not need the same line under every attempt. For
// a person that is right, and for a program it is a silence that looks
// exactly like a turn still running. MessageDropped hands the adapter the
// fact itself, on every drop; the notice's own dedupe stays the gateway's.
//
// TurnFinished says the turn is over, which is the only thing that makes "the
// gateway answered without starting a task" knowable rather than guessed. A
// door watching the transcript alone cannot tell a turn that answered and
// stopped from one that has posted something on its way to minting a task --
// the heal posts twice before startTask announces - and a guess that lands
// the wrong way answers the caller "nothing started" while the task it did
// start runs unobserved.
//
// Both are called on the conversation's inbox worker, like TaskStarted:
// record and return.
type InboundObserver interface {
	MessageDropped(conversation, authorID string)
	TurnFinished(conversation string)
}

// TerminalSource says whose word a terminal is. It exists because "the task
// failed" and "the gateway could not start the task" are the same TaskState
// and mean opposite things to a caller deciding whether it has an answer: the
// first is what the executor did with the ask, the second is that no executor
// ever saw it. A chat user reads the difference out of the posted text; a
// program cannot, and an eval that confuses them scores an outage as the
// agent's failure.
type TerminalSource string

const (
	// TerminalFromExecutor is a terminal that arrived on the task's event
	// stream -- the executor's own account of how the work ended.
	TerminalFromExecutor TerminalSource = "executor"
	// TerminalFromGateway is a terminal the gateway declared about a task no
	// executor could have run, because it never reached the bus. Nothing is
	// published for it: it is the gateway telling its own adapter, not a
	// claim on a stream that has never heard of the task.
	TerminalFromGateway TerminalSource = "gateway"
	// TerminalFromSupervisor is a terminal that arrived on the task's
	// supervisor subject -- the gateway's own word about an executor that
	// died or never ran, rather than the executor's account of the work.
	// The read route's fold and the heal report it: they are the two places
	// that have the subject a terminal arrived on (probeConversation,
	// healActiveTask). It is not an
	// answer either, and it is not TerminalFromGateway, which says something
	// narrower and more useful -- that this gateway could not put the task
	// on the bus at all.
	TerminalFromSupervisor TerminalSource = "supervisor"
	// TerminalNeverStarted is the never-started heal: a task that reached the
	// bus and produced nothing on its events subject inside
	// A2A_FIRST_EVENT_GRACE, which handleInbound releases on the
	// conversation's next message. Nothing is published for this one either,
	// and age is not evidence of what happened -- but for a caller it is the
	// difference between "the agent answered badly" and "no executor was
	// listening". A program that sends no further message never sees this
	// source; it reads the same window off the read route (ConversationProbe)
	// and classifies for itself.
	TerminalNeverStarted TerminalSource = "gateway-never-started"
)

// ConversationProbe is the read route's source: what the gateway's session
// record holds for one conversation and what the task's stream shows, read
// and reported with nothing changed. It is a PURE READ. It does not run the
// stale-task heal, take the session lock, post, publish or write. The
// never-started heal is a write under the per-conversation lock inside the
// keyed queue, and a read that performed it would be a second writer racing
// the next inbound message (the A2A owner's constraint on the eval
// transport's design doc); so the heal stays handleInbound's, and the read
// reports the same facts the heal would decide on -- the active task's age
// against the gateway's grace, and whether the stream holds any executor
// event -- for the caller to classify. Whether the gateway has since
// released the conversation on some turn is immaterial to that caller: it
// keys every case and repetition to a fresh conversation.
//
// Called off the adapter's own request goroutine, never from a gateway
// worker; it costs one KV read and one replay of the task's stream, so a
// caller bounds ctx.
type ConversationProbe func(ctx context.Context, conversation string) (ConversationState, error)

// ProbeSink is the optional extension an Adapter implements to receive the
// gateway's ConversationProbe. The gateway type asserts for it in New, the
// way it asserts for TaskObserver at each call site; an adapter that does
// not implement it is never offered one.
type ProbeSink interface {
	SetProbe(ConversationProbe)
}

// ConversationState is one read's answer: the record as it stands, plus the
// two gateway-wide facts a caller needs beside it.
type ConversationState struct {
	// Backend is the backend the gateway attributes messages to, and
	// InjectOnly whether that is the inject door alone -- no Discord token
	// and no Chat relay armed. Surfaced because a `mode: next` install whose
	// relay URL failed to render starts on the door and would otherwise be
	// silent about it; the gateway logs the same at start.
	Backend    string
	InjectOnly bool
	// Grace is the gateway's FirstEventGrace: the window inside which an
	// active task with nothing on its stream is legitimately pre-first-event.
	Grace time.Duration
	// Active is whether the record holds an active task; the four fields
	// after it describe that task. Detached is one the gateway has already
	// published a cancel for.
	Active      bool
	TaskID      string
	SubmittedAt time.Time
	Age         time.Duration
	Detached    bool
	// ExecutorState is the state the task's stream shows -- "" when the
	// stream has no events for it at all (no executor has touched it),
	// otherwise the latest status event's state, which for a stream that
	// does not regress is also the highest: submitted, working, or a
	// terminal. Final is whether that event was final.
	ExecutorState lib.TaskState
	Final         bool
	// The terminal, when Final -- the fold of the task's stream, which is
	// what a caller has when the record still holds a task whose relay
	// already acked its terminal (a restart or a failed record write between
	// the ack and the release). TerminalSource says whose word it is: the
	// executor's when it arrived on the task's events subject, the
	// supervisor's when it arrived on the supervisor subject. Result is the text of the
	// result artifact, and Reason the terminal's status message, which the
	// executors write as `reason: <token>[ - detail]`.
	TerminalSource TerminalSource
	Result         string
	Reason         string
}
