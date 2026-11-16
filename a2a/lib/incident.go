package lib

// The incident route: the gateway's write of a scheduled report into the
// agent pod's incident store, so a reply in the thread the report landed in
// is answered with the report in hand (a2a/hermes-bridge/incident.go reads
// it back as <untrusted_report>). The gateway publishes an IncidentKeep on
// IncidentSubject; the Hermes bridge, the one process beside the agent that
// holds the session KV's key, checks the thread is one of a channel this
// install's chat backend serves (its home channel; whether the gateway in
// fact posted in that thread is the gateway's word), stores it with
// POST /v1/incidents, and
// answers an IncidentKeepResult on the request's subject under
// IncidentReplyPrefix. Named and shaped as the board route is
// (BoardSubject): a request the gateway alone sends and the bridge alone
// answers, in the chat namespace, with a reply namespace of its own.
const (
	// IncidentSubject is the subject an incident write is published on.
	IncidentSubject = "chat.incident"
	// IncidentReplyPrefix is the namespace an incident write's reply
	// subject sits in: only the bridge may publish there and only the
	// gateway may read, for the board reply's reason.
	IncidentReplyPrefix = "chat.incident.reply.gateway."
)

// IncidentKeep is one report to keep against the thread it was posted in.
// Platform is a NotifyPlatform name; ChatID the channel or space; ThreadID
// the thread as the platform answered the post (a Slack ts, bare or
// channel-qualified, or a Google Chat thread name). The bridge stores it
// under the address its own lookup reads.
type IncidentKeep struct {
	Platform string `json:"platform"`
	ChatID   string `json:"chat_id"`
	ThreadID string `json:"thread_id"`
	Report   string `json:"report"`
}

// IncidentKeepResult is the bridge's answer: OK, or why the report was not
// kept.
type IncidentKeepResult struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}
