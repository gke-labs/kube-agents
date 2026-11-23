package hermesbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A pushed report, attached to the reply under it. The relay
// (session_kv_server.relay_cron_report) stores every scheduled report it
// posts, and the kanban notifier (kanban_notifier.store_incident_report) a
// card's report that invites a reply, against the thread it landed in, in
// session-kv's `incidents` table, so a reply there is answered by an agent
// that has the report. Under
// spec.mode today that read is the Hermes gateway's incident_context hook
// (agents/platform/plugins/incident_context); under next a reply arrives on
// the A2A gateway and reaches Hermes through this executor, which never runs
// that hook. So before a task's first turn the bridge makes the same lookup,
// at the same server, and frames the report the same way. First, though,
// the report the A2A gateway relays on the submission (a pushedReport part,
// lib.PushedReportOf), which needs no store; the lookup serves a reply the
// relay did not.
const (
	// incidentPath is session-kv's lookup, by the chat and thread a report
	// was stored against.
	incidentPath = "/v1/incidents/by-thread"
	// incidentTimeout bounds one lookup, as the hook's does; the store is
	// local and the turn waits on it.
	incidentTimeout = 2 * time.Second
	// incidentReplyOpen and incidentReplyClose fence the report, and
	// incidentReplyUser leads the user's words: lib's report frame, which
	// the a2a-worker uses too. incident_context._reply_text spells all three
	// the same way, and TestIncidentFrame_MatchesTheIncidentContextHook
	// reads that file to keep lib in step.
	incidentReplyOpen  = lib.ReportFrameOpen
	incidentReplyClose = lib.ReportFrameClose
	incidentReplyUser  = lib.ReportFrameUser
	// The thread shapes a chat conversation key carries. A Slack home-channel
	// thread is slack:<channel>/<ts>, stored as (channel, ts); a Google Chat
	// thread is gchat:spaces/<s>/threads/<t>, or gchat:dm/spaces/<s>/threads/<t>
	// when the home is a DM space, stored either way as (spaces/<s>, the
	// thread name). A Slack DM, a whole Google Chat DM or a bare space has no
	// stored report of this shape. The DM and thread shapes are lib's
	// (SlackDMKeyPrefix, GchatDMKeyPrefix, GchatThreadsSep). A Teams key is
	// the conversation id, and only a channel thread carries ";messageid="
	// (lib.TeamsThreadToken), so a key without it is shared by every message
	// in that conversation (lib.WholeConversationKey).
	incidentSlackPrefix = "slack:"
	incidentGchatPrefix = "gchat:"
	incidentTeamsPrefix = lib.TeamsKeyPrefix
	// incidentReplyTaskSep joins a console conversation's key and the task
	// of the turn that filed a card in the row the notifier stores for the
	// card's report (kanban_event_routing.CONSOLE_THREAD_TASK_SEP).
	incidentReplyTaskSep = "#"
)

// incidentTokens are the template tokens a stored report is stripped of
// before it is spliced ahead of the user's words (lib.ReportTokens), here so
// the hook test can compare them with incident_context._TOKENS.
var incidentTokens = func() []struct {
	pattern     *regexp.Regexp
	replacement string
} {
	var out []struct {
		pattern     *regexp.Regexp
		replacement string
	}
	for _, t := range lib.ReportTokens() {
		out = append(out, struct {
			pattern     *regexp.Regexp
			replacement string
		}{t.Pattern, t.Replacement})
	}
	return out
}()

// errNoIncident is a lookup that found no stored report: nothing is attached
// and nothing is wrong.
var errNoIncident = errors.New("no report stored for this thread")

// incidentAddress is one (chat_id, thread_id) a report may be stored under.
type incidentAddress struct {
	chatID, threadID string
}

// incidentAddresses are the rows a reply in conversation key, answered by
// Hermes session sessionID, may be a reply to, in lookup order:
// (sessionID, key#replyTo) when the reply names the task whose report it
// answers, the notifier's key for a card's report posted into a console
// conversation; the thread the key names, where the relay stores a
// scheduled report and the notifier the triage card's report under an
// alert; then (sessionID, key), the notifier's key for a card's report
// posted into a Slack, Google Chat or Teams conversation. A console reply naming
// no task does not reach the console row, whose key carries the task.
//
// A key that names a whole conversation rather than a thread takes none:
// a Google Chat space with no thread, a thread-less Slack DM, a Teams
// personal or group chat (any Teams key without a channel thread's
// ;messageid= suffix), and the thread-less Google Chat DM key, which is the legacy and openDirect shape
// (a Google Chat DM ask keys its own thread). Every later message
// under such a key shares it and the session, so a row stored under it
// would be spliced ahead of every question for the row's life, and the
// first card's report would answer a reply to the second's. The hook never
// had this, since it looks up the message's own thread.
func incidentAddresses(key, sessionID, replyTo string) []incidentAddress {
	key = strings.TrimSpace(key)
	if key == "" || lib.WholeConversationKey(key) {
		return nil
	}
	var out []incidentAddress
	if replyTo != "" && sessionID != "" {
		out = append(out, incidentAddress{chatID: sessionID, threadID: key + incidentReplyTaskSep + replyTo})
	}
	if thread, ok := incidentThread(key); ok {
		out = append(out, thread)
	}
	if sessionID != "" {
		out = append(out, incidentAddress{chatID: sessionID, threadID: key})
	}
	return out
}

// incidentThread is the stored address of a home-channel thread's post, when
// key names one.
func incidentThread(key string) (incidentAddress, bool) {
	if !lib.ThreadKeyedConversation(key) {
		return incidentAddress{}, false
	}
	switch {
	case strings.HasPrefix(key, lib.SlackDMKeyPrefix):
		// A thread in a Slack DM is one conversation per thread, but no
		// home-channel post was stored for it.
		return incidentAddress{}, false
	case strings.HasPrefix(key, incidentSlackPrefix):
		channel, ts, ok := strings.Cut(key[len(incidentSlackPrefix):], "/")
		if !ok || channel == "" || ts == "" {
			return incidentAddress{}, false
		}
		return incidentAddress{chatID: channel, threadID: ts}, true
	case strings.HasPrefix(key, incidentGchatPrefix):
		// A thread in a DM home is keyed gchat:dm/<thread name>; the relay
		// stores it by the thread name alone, as in any space.
		thread := strings.TrimPrefix(strings.TrimPrefix(key, lib.GchatDMKeyPrefix), incidentGchatPrefix)
		space, _, ok := strings.Cut(thread, lib.GchatThreadsSep)
		if !ok || space == "" {
			return incidentAddress{}, false
		}
		return incidentAddress{chatID: space, threadID: thread}, true
	case strings.HasPrefix(key, incidentTeamsPrefix):
		// A Teams channel thread is its own conversation id; the relay
		// stores the row under (thread, thread), since _thread_chat_id has
		// no chat to split off a Teams id.
		thread := key[len(incidentTeamsPrefix):]
		if !strings.Contains(strings.ToLower(thread), lib.TeamsThreadToken) {
			return incidentAddress{}, false
		}
		return incidentAddress{chatID: thread, threadID: thread}, true
	}
	return incidentAddress{}, false
}

// attachPushedReport returns prompt with the report the reply is under
// ahead of it, framed as untrusted data: the report the gateway relayed on
// the submission (a pushedReport part, kept from the notify post it made
// into the conversation), else the one stored in session-kv for the
// conversation. The prompt is unchanged when the submission carries no
// report and the bridge has no route store, the conversation names no
// thread, no report is stored, or the lookup fails. A failed lookup is logged and
// the turn goes on without the report, as the hook's does: a reply the
// agent has to find its way back from beats one it never answers.
func (b *Bridge) attachPushedReport(ctx context.Context, sessionID string, env *lib.Envelope, parts []lib.Part, prompt string) string {
	auth, err := parseRouteAuthority(env)
	if err != nil {
		return prompt
	}
	key := strings.TrimSpace(auth.Audience.Conversation)
	if report, ok := b.relayedReport(env, parts); ok {
		b.cfg.Logger.Info("pushed report attached from the gateway's relay",
			"task", env.TaskID, "session", sessionID, "conversation", key, "reportTask", report.TaskID, "bytes", len(report.Text))
		return incidentReply(report.Text, prompt)
	}
	if b.cfg.RouteURL == "" || strings.TrimSpace(b.cfg.RouteKey) == "" {
		return prompt
	}
	// The part is read only for a key that is looked up at all.
	replyTo := ""
	if key != "" && !lib.WholeConversationKey(key) {
		replyTo = b.incidentReplyTo(env, parts)
	}
	for _, addr := range incidentAddresses(key, sessionID, replyTo) {
		report, err := b.getIncident(ctx, addr)
		if errors.Is(err, errNoIncident) {
			continue
		}
		if err != nil {
			b.cfg.Logger.Warn("pushed report not attached; the turn runs without it",
				"task", env.TaskID, "session", sessionID, "chat", addr.chatID, "thread", addr.threadID, "err", err)
			return prompt
		}
		b.cfg.Logger.Info("pushed report attached to the reply under it",
			"task", env.TaskID, "session", sessionID, "chat", addr.chatID, "thread", addr.threadID, "bytes", len(report))
		return incidentReply(report, prompt)
	}
	return prompt
}

// relayedReport is the report the gateway put on the submission, when it
// did. A part the gateway would not have written (lib.PushedReportOf) is
// logged and read as none, so the turn looks the report up instead.
func (b *Bridge) relayedReport(env *lib.Envelope, parts []lib.Part) (lib.PushedReport, bool) {
	r, found, err := lib.PushedReportOf(parts)
	if err != nil {
		b.cfg.Logger.Warn("pushedReport part ignored; the turn looks the report up instead", "task", env.TaskID, "err", err)
		return lib.PushedReport{}, false
	}
	return r, found
}

// incidentReplyTo is the task the submission's replyTo part names, or "":
// no part, or one the gateway would not have written (lib.ReplyToOf), which
// is logged and read as none, so the turn runs on the other addresses.
func (b *Bridge) incidentReplyTo(env *lib.Envelope, parts []lib.Part) string {
	r, found, err := lib.ReplyToOf(parts)
	if err != nil {
		b.cfg.Logger.Warn("replyTo part ignored; the turn looks for its report by conversation alone", "task", env.TaskID, "err", err)
		return ""
	}
	if !found {
		return ""
	}
	return r.TaskID
}

// getIncident reads the report stored at addr, or errNoIncident.
func (b *Bridge) getIncident(ctx context.Context, addr incidentAddress) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, incidentTimeout)
	defer cancel()
	q := url.Values{"chat_id": {addr.chatID}, "thread_id": {addr.threadID}}
	target := strings.TrimRight(b.cfg.RouteURL, "/") + incidentPath + "?" + q.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.RouteKey)
	resp, err := b.routeClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return "", errNoIncident
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, routeErrorTailBytes))
		return "", fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	var body struct {
		Report string `json:"report"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return "", fmt.Errorf("decoding the report: %w", err)
	}
	if body.Report == "" {
		return "", errNoIncident
	}
	return body.Report, nil
}

// incidentReply is incident_context._reply_text (lib.FrameReport): the
// report, blunted, fenced and labelled untrusted, then the user's words,
// which have the last say.
func incidentReply(report, userText string) string {
	return lib.FrameReport(report, userText)
}

// neutralizeIncident blunts every token in incidentTokens, whatever case it
// arrived in (incident_context._neutralize, lib.NeutralizeReport).
func neutralizeIncident(text string) string {
	return lib.NeutralizeReport(text)
}
