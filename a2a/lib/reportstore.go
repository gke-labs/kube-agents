package lib

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"
)

// A pushed report at rest: one row per (chat_id, thread_id) in the gateway's
// session-state bucket, under ReportKeyPrefix, so a reply under the report
// still finds it after the process that posted it is gone. The gateway is
// the only reader and writer of these rows, as of every value in that
// bucket. Gateways older than the rows read only "sessions.>" and exact
// keys there, so they never see one.
//
// The address mirrors the hermes-bridge's session-kv incident row
// (a2a/hermes-bridge/incident.go, incidentAddresses): the same
// (chat_id, thread_id) a home-channel thread's report is stored under there
// is the one it is stored under here, so a report written by either is
// found by the same lookup order.
const (
	// ReportKeyPrefix leads every report row's key. One token, beside
	// "sessions.", "tasks." and "refused.", so a filtered listing of any of
	// those never returns a report row, and "report.>" lists them all.
	ReportKeyPrefix = "report."
	// ReportKeysFilter lists every report row.
	ReportKeysFilter = ReportKeyPrefix + ">"
	// ReportReplyTaskSep joins a console conversation's key and the task a
	// report is on, as the thread id of the row kept for it: the bridge's
	// incidentReplyTaskSep (kanban_event_routing.CONSOLE_THREAD_TASK_SEP).
	ReportReplyTaskSep = "#"
)

// The sources a report row names. A writer replaces only a row its own
// source wrote, so two writers sharing an address can't overwrite each
// other's reports. Further sources are added beside these.
const (
	// ReportSourceRelay is the gateway's relay of the reports the notify
	// route posted (a2a/gateway/reports.go).
	ReportSourceRelay = "relay"
)

// ReportRecord is one report row's value.
type ReportRecord struct {
	// Text is the report as it was posted, cut to PushedReportTextCap.
	Text string `json:"text"`
	// Source is the writer that stored the row (ReportSource*).
	Source string `json:"source"`
	// PostedAt is when the report was posted; it encodes as RFC 3339, with
	// the fraction of a second when there is one.
	PostedAt time.Time `json:"posted_at"`
	// Conversation is the gateway conversation key the report was posted
	// into.
	Conversation string `json:"conversation"`
	// Root is the task the report was on, the root of its chain; "" when
	// the report named none.
	Root string `json:"root"`
	// ChatID and ThreadID are the row's address (ReportAddress), stored so
	// a read can check the row it got is the one it asked for.
	ChatID   string `json:"chat_id"`
	ThreadID string `json:"thread_id"`
	// Epoch is the gateway term that wrote the row, which fences a deposed
	// gateway's write as a session record's epoch does. Zero when the
	// writer runs unelected.
	Epoch uint64 `json:"epoch,omitempty"`
}

// ReportAddress is one (chat_id, thread_id) a report may be stored under.
type ReportAddress struct {
	ChatID, ThreadID string
}

// Key is the row key the address is stored under (ReportKey).
func (a ReportAddress) Key() string {
	return ReportKey(a.ChatID, a.ThreadID)
}

// ReportKey is the session-state key of the report row stored under
// (chatID, threadID): ReportKeyPrefix and the hex SHA-256 of the two joined
// by a NUL, which neither carries. A KV key allows only [-/_=.a-zA-Z0-9]
// with no empty token, and a conversation key carries ':' and may carry
// '.', so the address is hashed rather than spelled.
func ReportKey(chatID, threadID string) string {
	sum := sha256.Sum256([]byte(chatID + "\x00" + threadID))
	return ReportKeyPrefix + hex.EncodeToString(sum[:])
}

// ReportAddresses are the rows a reply in conversationKey may be under, in
// lookup order. With hermesSessionID set it is the bridge's
// incidentAddresses exactly: (hermesSessionID, key#replyTo) when the reply
// names a task, the thread the key names (ReportThreadAddress), then
// (hermesSessionID, key). With none, as on the gateway, which has no Hermes
// session, it is (key, key#replyTo) when the reply names a task, then the
// thread.
//
// A key that names a whole conversation rather than a thread
// (WholeConversationKey) takes none, as in the bridge: every message under
// it shares the key, so a row stored there would ride every later question.
func ReportAddresses(conversationKey, hermesSessionID, replyTo string) []ReportAddress {
	key := strings.TrimSpace(conversationKey)
	if key == "" || WholeConversationKey(key) {
		return nil
	}
	chat := hermesSessionID
	if chat == "" {
		chat = key
	}
	var out []ReportAddress
	if replyTo != "" {
		out = append(out, ReportAddress{ChatID: chat, ThreadID: key + ReportReplyTaskSep + replyTo})
	}
	if thread, ok := ReportThreadAddress(key); ok {
		out = append(out, thread)
	}
	if hermesSessionID != "" {
		out = append(out, ReportAddress{ChatID: hermesSessionID, ThreadID: key})
	}
	return out
}

// ReportThreadAddress is the stored address of a home-channel thread's
// report, when key names one: the bridge's incidentThread. A Slack
// home-channel thread slack:<channel>/<ts> is (channel, ts); a Google Chat
// thread gchat:spaces/<s>/threads/<t>, or gchat:dm/spaces/<s>/threads/<t> in
// a DM home, is (spaces/<s>, the thread name) either way; a Teams channel
// thread is (its conversation id, the same id). A thread in a Slack DM has
// none: no home-channel post is stored for one.
func ReportThreadAddress(key string) (ReportAddress, bool) {
	key = strings.TrimSpace(key)
	if !ThreadKeyedConversation(key) {
		return ReportAddress{}, false
	}
	switch {
	case strings.HasPrefix(key, SlackDMKeyPrefix):
		return ReportAddress{}, false
	case strings.HasPrefix(key, reportSlackPrefix):
		channel, ts, ok := strings.Cut(key[len(reportSlackPrefix):], "/")
		if !ok || channel == "" || ts == "" {
			return ReportAddress{}, false
		}
		return ReportAddress{ChatID: channel, ThreadID: ts}, true
	case strings.HasPrefix(key, reportGchatPrefix):
		thread := strings.TrimPrefix(strings.TrimPrefix(key, GchatDMKeyPrefix), reportGchatPrefix)
		space, _, ok := strings.Cut(thread, GchatThreadsSep)
		if !ok || space == "" {
			return ReportAddress{}, false
		}
		return ReportAddress{ChatID: space, ThreadID: thread}, true
	case strings.HasPrefix(key, TeamsKeyPrefix):
		thread := key[len(TeamsKeyPrefix):]
		if !strings.Contains(strings.ToLower(thread), TeamsThreadToken) {
			return ReportAddress{}, false
		}
		return ReportAddress{ChatID: thread, ThreadID: thread}, true
	}
	return ReportAddress{}, false
}

// The chat backends' key prefixes ReportThreadAddress splits, as the
// bridge spells them (incidentSlackPrefix, incidentGchatPrefix).
const (
	reportSlackPrefix = "slack:"
	reportGchatPrefix = "gchat:"
)
