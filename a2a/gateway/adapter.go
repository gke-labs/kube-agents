// Package gateway implements the chatops gateway: adapters, a session
// manager, and a bus client. It is deterministic code — no prompt, no tools,
// nothing to inject into (spec-chatops-gateway.md, "The gateway holds no
// model"). The judgment the demo gateway exercised lives in the executors.
package gateway

import (
	"context"
	"encoding/json"
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
// that does not implement it sees no change at all, which is Discord and
// Google Chat — a human reads the chat, so the chat text is the whole
// interface. The Slack adapter implements it for TaskStarted and TaskTerminal, because
// a task starting in a thread is what makes that thread a session thread and
// the adapter has to know (see SessionLookup).
//
// The inject backend is the case that needs more. Its caller is a program: it
// posts a message and has to know which task that started and when that task
// ended, and the only other way to learn either is to parse the rendered chat
// text — "⏳ submitted…" and "✅ **completed**" — which is presentation and is
// free to change. Passing the two ids the gateway already has in hand costs
// nothing and makes the eval transport independent of how the relay words
// itself.
//
// Every method is called on one of the conversation's own workers (the
// inbox worker for what a turn does -- TaskStarted, TaskAccepted,
// CancelPublished, and TaskTerminal from the heal; the relay queue for a
// relayed TaskTerminal) while the session lock is held, so an implementation
// must not block: record and return.
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
// DeliverableObserver is told the text a completed task delivered, once,
// before the relay posts it. The relay posts a result in chat-sized chunks
// (g.post, discordChunk), so an adapter whose caller is a program cannot
// rebuild the deliverable from the posts it sees; this hands it over whole,
// and the heal path hands over the artifact the stream carries. Optional,
// like the observers above: the gateway asserts for it on the adapter it
// drives, and the side-door composite forwards it to the owning door.
type DeliverableObserver interface {
	TaskDelivered(conversation, taskID, result string)
}

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
	// The relay, the heal and the read route's fold report it, each from
	// the subject the terminal arrived on (terminalSourceOf under
	// relayBatch, healActiveTask, probeConversation), so one terminal is
	// attributed the same way on every path. It is not an
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
// taskID names the task whose stream to read; "" reads the record's active
// task. A caller that names one reads that task whether or not the record
// still holds it as active -- the relay clears the active task when it
// posts the terminal, and a finished run's trace is read after that.
//
// Called off the adapter's own request goroutine, never from a gateway
// worker; it costs one KV read and one replay of the task's stream, so a
// caller bounds ctx.
type ConversationProbe func(ctx context.Context, conversation, taskID string) (ConversationState, error)

// ProbeSink is the optional extension an Adapter implements to receive the
// gateway's ConversationProbe. The gateway type asserts for it in New, the
// way it asserts for TaskObserver at each call site; an adapter that does
// not implement it is never offered one.
type ProbeSink interface {
	SetProbe(ConversationProbe)
}

// SessionLookup answers whether the gateway has started a task in a
// conversation and the session is not idle past the idle TTL, and, with a
// true, the moment that answer stops being trustworthy. A running task
// counts regardless of age: the conversation is a session for as long as
// the task runs. Otherwise it is one only while its record shows activity
// within the TTL.
//
// A record alone is not the answer, in either direction. One is minted for
// any verified turn, including a "stop" with nothing running, so its
// existence does not say a task started; and the reap keeps the record --
// it deletes the idle session's pod and leaves the record, past tasks
// included, and nothing deletes records at all -- so its existence does not
// say the session is live either. The lookup reads the two facts the record
// does carry, the active task and the last activity, and answers from
// those. Running means what it means to the reap: an active task that is
// not detached.
//
// It is a PURE READ of the session registry, like ConversationProbe: no
// lock, no heal, no post, no write, one KV read.
//
// It exists because an adapter can be asked whether a conversation is one
// the gateway is in before it has been told so on this process, or after it
// has stopped being one. The Slack adapter's session-thread cache is process
// memory: a thread the gateway adopted mid-conversation -- a verified
// sender's mentioned ask in someone else's thread -- is forgotten on a
// restart or a cache eviction, and its unmentioned follow-ups ("stop") have
// no other source to be recognised from. The registry is the source of
// truth for which conversations the gateway is in, so a cold cache asks it
// first, and a positive entry that has expired asks it again.
//
// until is the registry's own bound on a true, handed back so the adapter's
// cache expires when the answer does and not later: for an idle-bounded
// session it is the last activity plus the TTL, the instant the registry
// would begin answering false; for a running task, which has no bound, it
// is a TTL from now, when the adapter asks again and is answered again. A
// cache that stamped its own clock instead would hold a true for up to a
// TTL past the registry's word whenever the message that made it ask moved
// no activity -- one the gateway refused, an unmapped sender's. With a
// false, until is the zero time and means nothing.
//
// The TTL is still handed over beside the lookup (SessionLookupSink): a
// true the adapter writes on its own word (TaskStarted) has no registry
// answer to take a bound from, and expires a TTL after the start.
//
// Called on the adapter's own event goroutine under the adapter's own
// bound, never from a gateway worker.
type SessionLookup func(ctx context.Context, conversation string) (held bool, until time.Time, err error)

// SessionLookupSink is the optional extension an Adapter implements to
// receive the gateway's SessionLookup and the idle TTL it is bounded by.
// Wired in New the way ProbeSink is; an adapter that does not implement it
// is never offered either.
type SessionLookupSink interface {
	SetSessionLookup(lookup SessionLookup, idleTTL time.Duration)
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
	// TaskID is the task this read describes: the one the caller named,
	// else the record's active task. Active is whether the record holds
	// that task as its active one; SubmittedAt and the active-task fields
	// after it describe the active task and are zero for a named task the
	// record has released.
	// Detached is one the gateway has already published a cancel for.
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
	// ReachedWorking is whether the stream ever showed `working`, whatever
	// ExecutorState shows now. Two events can land between a caller's
	// reads -- working, then input-required -- and a caller deciding "a
	// model ran" from the states it saw would file a run that happened as
	// one that never started. Read off the fold's history, not its head.
	ReachedWorking bool
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
	// Activity is the task's tool-call trace as the stream holds it: the
	// data part of every part of the activity artifact, in arrival order
	// (stream order for an executor that appends to one artifact id),
	// each one the executor's own JSON record of one tool invocation. Set
	// whenever the task's stream was read, final or not, and non-nil then
	// even with no calls -- nil means the stream was not read (no active
	// task, or the read failed), and a caller telling "this executor
	// called nothing" from "nobody looked" needs the two kept apart. The
	// relay deliberately never posts it; this is the only way a caller
	// sees it. Progress is the last text part of the progress artifact,
	// which is the line the relay's rolling edit shows.
	Activity []json.RawMessage
	Progress string
}
