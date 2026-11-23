package lib

import (
	"encoding/json"
	"strings"
)

// The chat.notify wire contract, shared by the gateway that answers it
// (a2a/gateway/notify.go) and the `a2a notify` command that sends it from the
// agent container. The grants that bound who may do either are rendered by
// the operator (platformagent_a2a_identities.go).
const (
	// NotifySubjectGchat is the subject a Google Chat notify is published on.
	NotifySubjectGchat = "chat.notify.gchat"
	// NotifySubjectSlack is the subject a Slack notify is published on.
	NotifySubjectSlack = "chat.notify.slack"
	// NotifySubjectTeams is the subject a Microsoft Teams notify is published on.
	NotifySubjectTeams = "chat.notify.teams"
	// NotifySubjectConsole is the subject a console notify is published on.
	// The console has no home channel, so it serves conversation requests
	// only.
	NotifySubjectConsole = "chat.notify.console"
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
	// NotifyPlatformTeams is Microsoft Teams', as Hermes spells it (`--to teams`).
	NotifyPlatformTeams = "teams"
	// NotifyPlatformConsole is the console door's. Hermes has no console
	// platform of its own; this is the name the agent side records on a
	// console conversation's route.
	NotifyPlatformConsole = "console"
)

// NotifySubjects maps a caller's platform name to its notify subject.
var NotifySubjects = map[string]string{
	NotifyPlatformGchat:   NotifySubjectGchat,
	NotifyPlatformSlack:   NotifySubjectSlack,
	NotifyPlatformTeams:   NotifySubjectTeams,
	NotifyPlatformConsole: NotifySubjectConsole,
}

// NotifyRequest is the body of a chat.notify request.
type NotifyRequest struct {
	// Text is the message, chunked under the backend cap on the way out,
	// except on a request with an Update key, which is one message (an
	// edit reaches one): a conversation's is cut to one chunk; a home
	// thread's goes whole when its Chat is laid out, as text when it fits
	// one chunk, and otherwise chunked and unkeyed as an unkeyed post.
	Text string `json:"text"`
	// Thread, when set, is the thread to reply on. It must be a thread of
	// the home channel; empty starts a new thread there. On Google Chat it
	// is the thread's resource name. On Slack it is "<channel>/<ts>",
	// admitted only when the channel is home, or a bare ts, which names no
	// channel and is posted into the home channel; senders that know the
	// thread's channel send the qualified form.
	Thread string `json:"thread,omitempty"`
	// WaitMillis is how long the requester waits for the answer. A request
	// the gateway fails or refuses after that has nobody to tell, and the
	// requester has recorded it as possibly posted, so the gateway logs the
	// loss as an error rather than as an ordinary refusal. Zero is unknown.
	WaitMillis int64 `json:"wait_ms,omitempty"`
	// Blocks, when set, is a Slack Block Kit array posted as one message
	// with Text as its notification and fallback text (the fleet audit's
	// report card). Only a backend that renders blocks takes it; any other
	// refuses the request rather than dropping the blocks, and a request
	// with Blocks and an Update key is refused: the gateway edits chat
	// messages and text by key, not Block Kit.
	Blocks json.RawMessage `json:"blocks,omitempty"`
	// Conversation, with ContextID, aims the request at a conversation the
	// gateway holds (its session-record key, e.g. "gchat:spaces/A/threads/B")
	// rather than the home channel. The gateway posts it only when that
	// conversation's session record carries ContextID: the context id the
	// conversation's own tasks brought to the agent. Thread must be empty.
	Conversation string `json:"conversation,omitempty"`
	ContextID    string `json:"context_id,omitempty"`
	// Chat and Update carry a card's work after its task ends, on a request
	// aimed at a conversation: Chat a kube-agents.chat/v1 object, with Text
	// its fallback, laid out by a backend that renders it (Slack, Google
	// Chat) and posted as Text by any other; Update a sender-chosen key
	// whose later request edits the message the first posted in the same
	// conversation or home-channel thread. A home channel request may carry
	// Chat too (the fleet-audit or first-inventory card), laid out the same
	// way, and Update: one with no Thread binds its key to the thread it
	// starts (the event watcher's alert, which its triage edits).
	Chat   json.RawMessage `json:"chat,omitempty"`
	Update string          `json:"update,omitempty"`
	// AnswerTo, on a conversation request, says the post answers an ask:
	// it is the delivered report of the card whose completion settled every
	// card of the ask's plan, and its value is that plan's Update key, which
	// names the ask (the chain the key's first mark bound). A backend that
	// draws rating controls draws them on it (answer feedback). The CLI
	// refuses it on a home channel request, and the gateway ignores it with
	// Update, which names a message edited in place rather than an answer.
	AnswerTo string `json:"answer_to,omitempty"`
	// ReplyTo, on a request aimed at a conversation, is the id of the
	// gateway task whose turn filed the work this reports on, when the
	// sender knows it. Only the console passes it on (its frame's replyTo),
	// so a console client can join a post to the turn it reports on, and
	// the gateway keeps the post under its root for the reply that names
	// it (a2a/gateway/reports.go).
	ReplyTo string `json:"reply_to,omitempty"`
	// Report marks a conversation post as a card's report: the kanban
	// notifier sets it on the settled card's report post alone (never on a
	// progress note, heartbeat, queued or plan line), and the gateway keeps
	// a post for the reply under it only when it is set: under its task for
	// a console post, under the thread for a post into a chat thread; the
	// console frame carries it (report), so a console client offers a
	// reply under that post alone.
	Report bool `json:"report,omitempty"`
}

// NotifyReply is the answer: the first message posted and the thread it
// landed in, or the reason nothing (or not everything) was posted. Field
// names match what `hermes send --json` prints, so the agent-side callers
// parse one shape on either path.
type NotifyReply struct {
	MessageID string `json:"message_id,omitempty"`
	ThreadID  string `json:"thread_id,omitempty"`
	Error     string `json:"error,omitempty"`
	// Retry marks a refusal that says nothing about the request: the gateway
	// is stopping, or couldn't read the conversation's record in time.
	// Nothing was posted, and the same request may land later, so a sender
	// waits rather than counting it against the destination.
	Retry bool `json:"retry,omitempty"`
	// Kept says the refusal in Error is a home post the gateway kept for the
	// digest it posts once a home channel is set: nothing was posted now, and
	// a sender that would retry or re-send should treat the post as handled.
	Kept bool `json:"kept,omitempty"`
}

// The conversation keys every top-level message of a conversation shares: a
// Google Chat space with no thread (gchat:space/...), a Google Chat DM with
// no thread (gchat:dm/... without /threads/), and a Slack DM with no thread
// (slack:dm/<id>). A report posted under such a key belongs to no one later
// message, so neither the hermes-bridge's lookup nor the gateway's relay
// puts it ahead of one.
const (
	wholeConversationGchatSpace = "gchat:space/"
	// GchatDMKeyPrefix begins a Google Chat DM conversation key, and
	// GchatThreadsSep sits between a space and its thread in one;
	// SlackDMKeyPrefix begins a Slack DM key. The hermes-bridge's lookup
	// reads the same shapes (incidentThread).
	GchatDMKeyPrefix = "gchat:dm/"
	GchatThreadsSep  = "/threads/"
	SlackDMKeyPrefix = "slack:dm/"
	// TeamsKeyPrefix begins a Microsoft Teams conversation key, whose rest is
	// the conversation id; only a channel thread's id carries
	// TeamsThreadToken, so a key without it (a personal chat, a group chat,
	// a channel's own id) is shared by every message in that conversation.
	TeamsKeyPrefix   = "teams:"
	TeamsThreadToken = ";messageid="
)

// WholeConversationKey reports whether key names a conversation every
// top-level message shares rather than one thread.
func WholeConversationKey(key string) bool {
	switch {
	case strings.HasPrefix(key, wholeConversationGchatSpace):
		return true
	case strings.HasPrefix(key, GchatDMKeyPrefix):
		return !strings.Contains(key[len(GchatDMKeyPrefix):], GchatThreadsSep)
	case strings.HasPrefix(key, SlackDMKeyPrefix):
		return !strings.Contains(key[len(SlackDMKeyPrefix):], "/")
	case strings.HasPrefix(key, TeamsKeyPrefix):
		return !strings.Contains(strings.ToLower(key[len(TeamsKeyPrefix):]), TeamsThreadToken)
	}
	return false
}

// The conversation key prefixes of the chat backends whose key names the
// thread a message is typed in, so the key alone says which posts a reply
// is under. A Teams channel thread's key is its own conversation id with
// the thread token; a console or door key is one long conversation and
// names a report by task instead.
var threadKeyedPrefixes = []string{"slack:", "gchat:", TeamsKeyPrefix}

// ThreadKeyedConversation reports whether key names one thread of a chat
// backend: a thread-keyed prefix, and not a key every top-level message
// shares (WholeConversationKey).
func ThreadKeyedConversation(key string) bool {
	if WholeConversationKey(key) {
		return false
	}
	for _, prefix := range threadKeyedPrefixes {
		if strings.HasPrefix(key, prefix) {
			return true
		}
	}
	return false
}
