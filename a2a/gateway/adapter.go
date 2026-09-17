// Package gateway implements the chatops gateway: adapters, a session
// manager, and a bus client. It is deterministic code — no prompt, no tools,
// nothing to inject into (spec-chatops-gateway.md, "The gateway holds no
// model"). The judgment the demo gateway exercised lives in the executors.
package gateway

import (
	"context"

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
}

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

	// TaskTerminal names the state a task ended in, and who says so. Called
	// after the relay has posted the deliverable and edited the rolling line,
	// so a caller that sees this has already seen everything the conversation
	// received for that task.
	TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource)
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
)
