// Package gateway implements the chatops gateway: adapters, a session
// manager, and a bus client. It is deterministic code — no prompt, no tools,
// nothing to inject into (spec-chatops-gateway.md, "The gateway holds no
// model"). The judgment the demo gateway exercised lives in the executors.
package gateway

import "context"

// InboundMessage is one chat message, normalized across backends. AuthorID is
// the backend-native immutable id of the sender as the backend's own identity
// mechanism reported it — for Discord, the authenticated gateway websocket.
// Verification against the principal map happens in the session manager;
// adapters never see principals.
type InboundMessage struct {
	// Conversation is the backend-qualified conversation id — the session key
	// (eg discord:1234/5678). A channel or space is not a session; a
	// conversation in it is.
	Conversation string
	// Kind is "dm" or "group".
	Kind string
	// AuthorID is the backend-native immutable sender id.
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

	// Roster returns the backend-native member ids of a conversation and
	// whether the list is complete. The session manager pseudonymizes and
	// caps it; adapters return it raw.
	Roster(conversation string) (ids []string, complete bool, err error)

	// OpenDirect returns a DM conversation id for a backend user — the
	// DM-switch primitive. The gateway ships the primitive; the classifier
	// that decides to use it comes later. Until then everything posts to the
	// room it came from.
	OpenDirect(userID string) (conversation string, err error)
}
