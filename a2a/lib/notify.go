package lib

import "encoding/json"

// The chat.notify wire contract, shared by the gateway that answers it
// (a2a/gateway/notify.go) and the `a2a notify` command that sends it from the
// agent container. The grants that bound who may do either are rendered by
// the operator (platformagent_a2a_identities.go).
const (
	// NotifySubjectGchat is the subject a Google Chat notify is published on.
	NotifySubjectGchat = "chat.notify.gchat"
	// NotifySubjectSlack is the subject a Slack notify is published on.
	NotifySubjectSlack = "chat.notify.slack"
	// NotifyReplyPrefix is the namespace a notify's reply subject must sit
	// under: the gateway answers there and nowhere else, and the agent
	// principal is the one reader of it.
	NotifyReplyPrefix = "chat.notify.reply.agent."
	// NotifyPlatformGchat is the platform name a caller asks for, spelled as
	// Hermes spells it (`hermes send --to google_chat`), so the agent-side
	// callers keep one vocabulary across the today and next paths.
	NotifyPlatformGchat = "google_chat"
	// NotifyPlatformSlack is Slack's, as Hermes spells it (`--to slack`).
	NotifyPlatformSlack = "slack"
)

// NotifySubjects maps a caller's platform name to its notify subject.
var NotifySubjects = map[string]string{
	NotifyPlatformGchat: NotifySubjectGchat,
	NotifyPlatformSlack: NotifySubjectSlack,
}

// NotifyRequest is the body of a chat.notify request.
type NotifyRequest struct {
	// Text is the message, chunked under the backend cap on the way out.
	Text string `json:"text"`
	// Thread, when set, is the thread to reply on. It must be a thread of
	// the home channel; empty starts a new thread there. On Google Chat it
	// is the thread's resource name; on Slack it is the thread root's ts,
	// which names no channel, so the gateway posts it into the home
	// channel and nowhere else.
	Thread string `json:"thread,omitempty"`
	// WaitMillis is how long the requester waits for the answer. A request
	// the gateway fails or refuses after that has nobody to tell, and the
	// requester has recorded it as possibly posted, so the gateway logs the
	// loss as an error rather than as an ordinary refusal. Zero is unknown.
	WaitMillis int64 `json:"wait_ms,omitempty"`
	// Blocks, when set, is a Slack Block Kit array posted as one message
	// with Text as its notification and fallback text (the fleet audit's
	// report card). Only a backend that renders blocks takes it; any other
	// refuses the request rather than dropping the blocks.
	Blocks json.RawMessage `json:"blocks,omitempty"`
	// Conversation, with ContextID, aims the request at a conversation the
	// gateway holds (its session-record key, e.g. "gchat:spaces/A/threads/B")
	// rather than the home channel. The gateway posts it only when that
	// conversation's session record carries ContextID: the context id the
	// conversation's own tasks brought to the agent. Thread must be empty.
	Conversation string `json:"conversation,omitempty"`
	ContextID    string `json:"context_id,omitempty"`
	// Chat and Update are reserved for a card's work after its task ends:
	// Chat a kube-agents.chat/v1 object, with Text its fallback, and Update
	// a sender-chosen key whose later request edits the message the first
	// posted in the same conversation. This gateway does not read them yet;
	// a request carrying them is posted as its Text.
	Chat   json.RawMessage `json:"chat,omitempty"`
	Update string          `json:"update,omitempty"`
}

// NotifyReply is the answer: the first message posted and the thread it
// landed in, or the reason nothing (or not everything) was posted. Field
// names match what `hermes send --json` prints, so the agent-side callers
// parse one shape on either path.
type NotifyReply struct {
	MessageID string `json:"message_id,omitempty"`
	ThreadID  string `json:"thread_id,omitempty"`
	Error     string `json:"error,omitempty"`
}
