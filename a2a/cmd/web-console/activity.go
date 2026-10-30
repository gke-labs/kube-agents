/*
Copyright 2026 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// The recent activity list: what kind each session is, a one-line summary for
// the kinds the cluster opened itself, and a read-only transcript.
//
// Content is shown only for sessions the cluster or this console opened: event
// triage, scheduled checks, and the console's own sessions. A Slack or Google
// Chat session is listed by title and never read. Both checks key on Hermes'
// session source, which only the ingress that created the session sets.

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	// Session kinds the page groups the list by.
	kindEventTriage = "event_triage"
	kindScheduled   = "scheduled"
	kindConsole     = "console"
	kindChat        = "chat"
	kindOther       = "other"

	// Message authors the transcript and message poll tag each row with.
	authorUser         = "user"
	authorAgent        = "agent"
	authorEventWatcher = "event_watcher"
	authorScheduler    = "scheduler"
	authorKanban       = "kanban"

	// sourceAPIServer is the Hermes source of a session created through the
	// gateway API: the event watcher's triage sessions, scheduled checks, and
	// this console's. Slack and Google Chat sessions carry their own source.
	sourceAPIServer = "api_server"

	// titlePrefix is what session_kv_server.py's _create_gateway_session puts
	// before the session ID in the title of every session it opens: the
	// title is exactly "Triage <session id>".
	titlePrefix = "Triage "

	// summaryTimeout bounds the summaries one session list request computes.
	// A session not summarized in time is listed without one and tried again
	// on the next refresh.
	summaryTimeout = 4 * time.Second
	// summaryConcurrency bounds how many sessions are summarized at once,
	// each with up to two Hermes calls.
	summaryConcurrency = 4
	// summaryFirstPageLimit is how many of a session's oldest messages are
	// read to find its first user message.
	summaryFirstPageLimit = 5
	// summaryMaxRunes bounds the subject and latest lines.
	summaryMaxRunes = 140
	ellipsis        = "…"
	// summaryCacheMaxEntries bounds the summary cache. Past it, entries for
	// sessions the current request did not list are dropped. The recent list
	// and the two channels list different sessions, so a smaller cap would
	// evict one list's entries on every refresh of another.
	summaryCacheMaxEntries = 500

	// transcriptMaxRows is how many text rows a transcript returns, newest
	// kept, oldest first.
	transcriptMaxRows = 200
	transcriptTimeout = 5 * time.Second
	// scheduledReportMarker prefixes user messages posted by a scheduled relay
	// turn into an existing cron-... session, so later sub-daily runs are
	// distinguished from a console user's reply.
	scheduledReportMarker = "[scheduled]"
)

var (
	// transcriptIDPattern is the charset a transcript request may name. It
	// covers the IDs the event watcher, the scheduler and this console mint,
	// and refuses anything that could change the upstream path.
	transcriptIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$`)

	// The session IDs session_kv_server.py mints. create_session makes an
	// event triage ID from eight hex digits of a UUID; _session_id makes a
	// scheduled ID from cron-<slug>-<YYYYMMDD>. Under spec.mode: next,
	// hermes-bridge creates chat sessions over the gateway API with IDs
	// prefixed a2a-ctx-. (Follow-up #3928 tracks unifying the scheduled
	// session producer contract across spec.mode: next.)
	eventTriageIDPattern      = regexp.MustCompile(`^k8s-evt-[0-9a-f]{8}$`)
	scheduledIDPattern        = regexp.MustCompile(`^cron-[a-z0-9_-]{1,80}-[0-9]{8}$`)
	a2aBridgeSessionIDPattern = regexp.MustCompile(`^a2a-ctx-[A-Za-z0-9_.:-]+$`)

	// cardTitlePattern finds the card title an event triage prompt spells
	// out ("- `title`: `Triage ns/Kind/name (Reason) on cluster`"; see
	// _agent_query in session_kv_server.py). Every triage prompt opens with
	// the same sentence, so that title is the line that tells posts apart.
	cardTitlePattern = regexp.MustCompile("(?m)^- `title`: `([^`\n]+)`")

	// Event watcher and kanban notification field patterns.
	eventResourcePattern  = regexp.MustCompile(`(?m)^-\s+\*{0,2}(?:Resource|Object):\*{0,2}\s*(.+)$`)
	eventReasonPattern    = regexp.MustCompile(`(?m)^-\s+\*{0,2}(?:Event Reason|What):\*{0,2}\s*(.+)$`)
	eventWarningPattern   = regexp.MustCompile(`(?m)^-\s+\*{0,2}(?:Warning Message|Who):\*{0,2}\s*(.+)$`)
	kanbanHeaderPattern   = regexp.MustCompile(`^\[kanban\] Task (t_[0-9a-zA-Z_-]+) ([^\r\n]+?)\.?\s*(?:\r?\n|$)`)
	kanbanTaskIDPattern   = regexp.MustCompile(`^t_[0-9a-zA-Z_-]{1,40}$`)
	kanbanTitlePattern    = regexp.MustCompile(`(?m)^Title:\s*(.+)$`)
	kanbanAssigneePattern = regexp.MustCompile(`(?m)^Assignee:\s*(.+)$`)
	kanbanResultPattern   = regexp.MustCompile(`(?ms)\n(?:Result|Reason):\s*(.+?)(?:\n\nThis is an automatic task-status notification|\z)`)
	titleClusterPattern   = regexp.MustCompile(`\bon\s+([a-z0-9-]+)$`)
	gkeLocationSuffix     = regexp.MustCompile(`^(.+)-[a-z]{2,}-[a-z]+[0-9](?:-[a-z])?$`)

	// summaryKinds are the kinds a channel reads content for.
	summaryKinds = map[string]bool{kindEventTriage: true, kindScheduled: true}
)

// classifySession sorts a session into the list's groups. The console's own
// IDs come first, then any session whose source is not the gateway API (a
// chat platform), so a Slack session can never be read as event triage
// because of its title. An empty source is unknown and lands in Other. An
// event triage or scheduled session needs both an ID in the shape
// session_kv_server.py mints and the exact title it gives that ID, so a
// session whose title only starts like one is Other.
func classifySession(id, source, title string) string {
	switch {
	case sessionIDPattern.MatchString(id):
		return kindConsole
	case source == "":
		return kindOther
	case source != sourceAPIServer:
		return kindChat
	case a2aBridgeSessionIDPattern.MatchString(id):
		return kindChat
	case title != titlePrefix+id:
		return kindOther
	case eventTriageIDPattern.MatchString(id):
		return kindEventTriage
	case scheduledIDPattern.MatchString(id):
		return kindScheduled
	default:
		return kindOther
	}
}

// sessionSummary is the list row's subject and latest-reply lines.
//
// Replies counts the assistant rows with text among the session's newest
// messagesPageLimit messages, so it is exact for a short session and a floor
// for a long one.
type sessionSummary struct {
	Subject string   `json:"subject"`
	Latest  string   `json:"latest"`
	Replies int      `json:"replies"`
	Skills  []string `json:"skills,omitempty"`
	// Agents are the kanban assignees that reported into the session, in
	// the page's short form ("Cluster agent · name", "Platform agent").
	Agents []string `json:"agents,omitempty"`
}

type summaryEntry struct {
	messageCount int
	summary      sessionSummary
}

// summaryCache maps a session ID to the summary computed at a message count.
// A refresh where the count has not moved costs no Hermes call.
type summaryCache struct {
	mu      sync.Mutex
	entries map[string]summaryEntry
}

// attachSummaries fills Summary on the sessions whose kind summaryKinds
// lists, from the cache where the message count still matches and from
// Hermes otherwise. Once the cache holds more than summaryCacheMaxEntries,
// entries for sessions this call did not list are dropped.
func (s *server) attachSummaries(parent context.Context, sessions []recentSession) {
	ctx, cancel := context.WithTimeout(parent, summaryTimeout)
	defer cancel()

	type job struct {
		index int
		count int
	}
	var jobs []job
	listed := map[string]bool{}
	s.summaries.mu.Lock()
	for i := range sessions {
		sess := &sessions[i]
		listed[sess.ID] = true
		if !summaryKinds[sess.Kind] || sess.MessageCount == nil {
			continue
		}
		if e, ok := s.summaries.entries[sess.ID]; ok && e.messageCount == *sess.MessageCount {
			sum := e.summary
			sess.Summary = &sum
			continue
		}
		jobs = append(jobs, job{index: i, count: *sess.MessageCount})
	}
	if len(s.summaries.entries) > summaryCacheMaxEntries {
		for id := range s.summaries.entries {
			if !listed[id] {
				delete(s.summaries.entries, id)
			}
		}
	}
	s.summaries.mu.Unlock()

	sem := make(chan struct{}, summaryConcurrency)
	var wg sync.WaitGroup
	for _, j := range jobs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				return
			}
			defer func() { <-sem }()
			sid := sessions[j.index].ID
			sum, ok := s.summarize(ctx, sid)
			if !ok {
				return
			}
			sessions[j.index].Summary = &sum
			s.summaries.mu.Lock()
			s.summaries.entries[sid] = summaryEntry{messageCount: j.count, summary: sum}
			s.summaries.mu.Unlock()
		}()
	}
	wg.Wait()
}

// summarize reads a session's first user message and its last assistant
// message with text. ok is false when either read fails, so a failure is not
// cached as an empty summary.
func (s *server) summarize(ctx context.Context, sid string) (sessionSummary, bool) {
	var out sessionSummary
	first, failure := s.fetchMessagesPage(ctx, fmt.Sprintf("/api/sessions/%s/messages?order=oldest&limit=%d&offset=0",
		url.PathEscape(sid), summaryFirstPageLimit))
	if failure != nil {
		return out, false
	}
	kind := kindOther
	switch {
	case eventTriageIDPattern.MatchString(sid):
		kind = kindEventTriage
	case scheduledIDPattern.MatchString(sid):
		kind = kindScheduled
	}
	var skills []string
	for _, m := range first {
		_, mSkills := extractMessageToolActivity(m)
		skills = mergeSkills(skills, mSkills...)
		if hasText(m) {
			skills = mergeSkills(skills, extractTextSkills(kind, sid, *m.Content, nil)...)
		}
		if out.Subject == "" && m.Role == roleUser && hasText(m) {
			out.Subject = summarySubject(*m.Content)
		}
	}
	latest, failure := s.fetchMessagesPage(ctx, fmt.Sprintf("/api/sessions/%s/messages?order=latest&limit=%d&offset=0",
		url.PathEscape(sid), messagesPageLimit))
	if failure != nil {
		return out, false
	}
	var agents []string
	for i := len(latest) - 1; i >= 0; i-- {
		_, mSkills := extractMessageToolActivity(latest[i])
		skills = mergeSkills(skills, mSkills...)
		if hasText(latest[i]) {
			skills = mergeSkills(skills, extractTextSkills(kind, sid, *latest[i].Content, nil)...)
			if latest[i].Role == roleUser {
				if card, ok := s.parseKanbanCard(*latest[i].Content); ok && card.Assignee != "" {
					agents = mergeSkills(agents, card.Assignee)
				}
			}
		}
		if latest[i].Role != roleAssistant || !hasText(latest[i]) {
			continue
		}
		if out.Replies == 0 {
			out.Latest = summaryLine(*latest[i].Content)
		}
		out.Replies++
	}
	out.Skills = skills
	out.Agents = agents
	return out, true
}

// stripScheduledMarker removes a leading [scheduled] marker line written by a
// scheduled relay turn and reports whether it was present.
func stripScheduledMarker(text string) (string, bool) {
	trimmed := strings.TrimLeft(text, " \t\r\n")
	if rest, ok := strings.CutPrefix(trimmed, scheduledReportMarker); ok {
		if rest == "" || rest[0] == '\n' || rest[0] == '\r' {
			return strings.TrimLeft(rest, "\r\n"), true
		}
	}
	return text, false
}

// summarySubject is the card title a triage prompt names, or else the first
// line of the message.
func summarySubject(text string) string {
	body, _ := stripScheduledMarker(text)
	if m := cardTitlePattern.FindStringSubmatch(body); m != nil {
		return summaryLine(m[1])
	}
	return summaryLine(body)
}

// summaryLine is the first non-blank line of a message, cut to
// summaryMaxRunes.
func summaryLine(text string) string {
	line := ""
	for _, l := range strings.Split(text, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			line = l
			break
		}
	}
	if utf8.RuneCountInString(line) <= summaryMaxRunes {
		return line
	}
	runes := []rune(line)
	return strings.TrimSpace(string(runes[:summaryMaxRunes])) + ellipsis
}

// hermesSession is the part of Hermes' GET /api/sessions/{id} the transcript
// and reply checks read: {"object": "hermes.session", "session": {...}}.
type hermesSession struct {
	ID         string   `json:"id"`
	Source     string   `json:"source"`
	Title      string   `json:"title"`
	LastActive *float64 `json:"last_active"`
}

type transcriptResponse struct {
	SessionID    string           `json:"session_id"`
	Title        string           `json:"title"`
	Kind         string           `json:"kind"`
	ChatPlatform string           `json:"chat_platform,omitempty"`
	LatestID     int64            `json:"latest_id"`
	Messages     []sessionMessage `json:"messages"`
}

// handleTranscript returns the newest transcriptMaxRows text rows of a
// session, oldest first, for the kinds the page may open: the console's own
// sessions, and event triage or scheduled sessions the gateway API created.
// Every other session is refused with 403, after a lookup that reads only
// its source and title.
func (s *server) handleTranscript(w http.ResponseWriter, r *http.Request) {
	sid := r.PathValue("id")
	if !transcriptIDPattern.MatchString(sid) {
		writeError(w, http.StatusBadRequest, "invalid_session_id", "Session ID has characters a session ID does not use.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), transcriptTimeout)
	defer cancel()
	sess, failure := s.lookupSession(ctx, sid)
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return
	}
	kind := classifySession(sid, sess.Source, sess.Title)
	if kind != kindConsole && kind != kindEventTriage && kind != kindScheduled {
		writeError(w, http.StatusForbidden, "transcript_not_allowed",
			"The console shows transcripts only for event triage, scheduled checks and its own sessions.")
		return
	}
	textRows := func(rows []hermesMessage) bool {
		n := 0
		for _, m := range rows {
			if hasText(m) {
				n++
			}
		}
		return n >= transcriptMaxRows
	}
	rows, reachedStart, failure := s.pageMessages(ctx, sid, 0, textRows)
	if failure != nil {
		writeError(w, failure.status, failure.code, failure.message)
		return
	}
	out := transcriptResponse{SessionID: sid, Title: sess.Title, Kind: kind, Messages: []sessionMessage{}}
	if kind != kindConsole {
		out.ChatPlatform = s.cfg.ChatPlatform
	}
	type annotatedText struct {
		msg    hermesMessage
		steps  []messageStep
		skills []string
	}
	var textMessages []annotatedText
	var pendingSteps []messageStep
	var pendingSkills []string
	for _, m := range rows {
		if m.ID > out.LatestID {
			out.LatestID = m.ID
		}
		steps, skills := extractMessageToolActivity(m)
		if len(steps) > 0 && len(pendingSteps) < maxStepsPerTurn {
			rem := maxStepsPerTurn - len(pendingSteps)
			if len(steps) > rem {
				steps = steps[:rem]
			}
			pendingSteps = append(pendingSteps, steps...)
		}
		pendingSkills = mergeSkills(pendingSkills, skills...)
		if hasText(m) {
			ann := annotatedText{msg: m}
			if m.Role == roleAssistant {
				ann.steps = foldDelegations(pendingSteps)
				ann.skills = pendingSkills
				pendingSteps = nil
				pendingSkills = nil
			}
			textMessages = append(textMessages, ann)
		}
	}
	truncatedHead := !reachedStart
	if truncatedHead && len(rows) > 0 {
		firstPage, _ := s.fetchMessagesPage(ctx, fmt.Sprintf("/api/sessions/%s/messages?order=oldest&limit=1&offset=0", url.PathEscape(sid)))
		if len(firstPage) > 0 && rows[0].ID <= firstPage[0].ID {
			truncatedHead = false
		}
	}
	if len(textMessages) > transcriptMaxRows {
		textMessages = textMessages[len(textMessages)-transcriptMaxRows:]
		truncatedHead = true
	}
	seenUser := truncatedHead
	for _, item := range textMessages {
		firstUser := false
		if item.msg.Role == roleUser && !seenUser {
			firstUser = true
			seenUser = true
		}
		built := s.buildSessionMessage(kind, sid, firstUser, item.msg)
		if len(item.steps) > 0 {
			built.Steps = item.steps
		}
		if len(item.skills) > 0 {
			built.Skills = mergeSkills(built.Skills, item.skills...)
		}
		out.Messages = append(out.Messages, built)
	}
	writeJSON(w, http.StatusOK, out)
}

// buildSessionMessage tags a user or assistant row with its author and
// extracts a card for automated prompts (event watcher, scheduler, or kanban
// task completion).
func (s *server) buildSessionMessage(kind, sid string, isFirstUser bool, m hermesMessage) sessionMessage {
	content := *m.Content
	msg := sessionMessage{
		ID:        m.ID,
		Role:      m.Role,
		Author:    authorAgent,
		Content:   content,
		Timestamp: m.Timestamp,
	}
	if m.Role != roleUser {
		msg.Skills = extractTextSkills(kind, sid, content, nil)
		return msg
	}
	if card, ok := s.parseKanbanCard(content); ok {
		card.Skills = extractTextSkills(kind, sid, content, card)
		msg.Author = authorKanban
		msg.Card = card
		msg.Skills = card.Skills
		return msg
	}
	if isFirstUser && kind == kindEventTriage {
		msg.Author = authorEventWatcher
		card := parseEventWatcherCard(content)
		card.Skills = extractTextSkills(kind, sid, content, card)
		msg.Card = card
		msg.Skills = card.Skills
		return msg
	}
	if kind == kindScheduled {
		if body, marked := stripScheduledMarker(content); marked || isFirstUser || s.cfg.ChatPlatform != "" {
			if marked {
				msg.Content = body
			}
			msg.Author = authorScheduler
			card := &messageCard{Subject: summarySubject(msg.Content)}
			card.Skills = extractTextSkills(kind, sid, msg.Content, card)
			msg.Card = card
			msg.Skills = card.Skills
			return msg
		}
	}
	msg.Author = authorUser
	msg.Skills = extractTextSkills(kind, sid, content, nil)
	return msg
}

func parseEventWatcherCard(text string) *messageCard {
	card := &messageCard{Subject: summarySubject(text)}
	if strings.Contains(text, "**Who:**") || strings.Contains(text, "**What:**") || strings.Contains(text, "**Object:**") {
		card.Kind = "drift"
	}
	if m := eventResourcePattern.FindStringSubmatch(text); m != nil {
		card.Resource = strings.TrimSpace(m[1])
	}
	if m := eventReasonPattern.FindStringSubmatch(text); m != nil {
		card.Reason = strings.TrimSpace(m[1])
	}
	if m := eventWarningPattern.FindStringSubmatch(text); m != nil {
		card.Warning = strings.TrimSpace(m[1])
	}
	return card
}

func (s *server) parseKanbanCard(text string) (*messageCard, bool) {
	m := kanbanHeaderPattern.FindStringSubmatch(strings.TrimSpace(text))
	if m == nil {
		return nil, false
	}
	card := &messageCard{
		TaskID: m[1],
		Status: m[2],
	}
	if tm := kanbanTitlePattern.FindStringSubmatch(text); tm != nil {
		card.Title = strings.TrimSpace(tm[1])
	}
	if am := kanbanAssigneePattern.FindStringSubmatch(text); am != nil {
		card.Assignee = s.shortenAssignee(strings.TrimSpace(am[1]), card.Title)
	}
	if rm := kanbanResultPattern.FindStringSubmatch(text); rm != nil {
		card.Summary = strings.TrimSpace(rm[1])
	}
	return card, true
}

func (s *server) shortenAssignee(raw, title string) string {
	name := strings.TrimPrefix(strings.TrimSpace(raw), "@")
	if name == "platform" {
		return "Platform agent"
	}
	rest, ok := strings.CutPrefix(name, "cluster-")
	if !ok || rest == "" {
		return name
	}
	if tm := titleClusterPattern.FindStringSubmatch(strings.TrimSpace(title)); tm != nil {
		if cluster := tm[1]; cluster != "" && strings.Contains(rest, cluster) {
			return "Cluster agent · " + cluster
		}
	}
	if s.cfg.ProjectID != "" {
		if trimmed, ok := strings.CutPrefix(rest, s.cfg.ProjectID+"-"); ok && trimmed != "" {
			rest = trimmed
		}
	}
	if s.cfg.Location != "" {
		if trimmed, ok := strings.CutSuffix(rest, "-"+s.cfg.Location); ok && trimmed != "" {
			rest = trimmed
		}
	} else if lm := gkeLocationSuffix.FindStringSubmatch(rest); lm != nil && lm[1] != "" {
		rest = lm[1]
	}
	return "Cluster agent · " + rest
}

// lookupSession reads one session's metadata from Hermes.
func (s *server) lookupSession(ctx context.Context, sid string) (hermesSession, *upstreamFailure) {
	var out struct {
		Session hermesSession `json:"session"`
	}
	resp, err := s.hermes(ctx, http.MethodGet, "/api/sessions/"+url.PathEscape(sid), nil)
	if err != nil {
		return out.Session, &upstreamFailure{http.StatusBadGateway, "agent_unreachable", "Could not reach the agent gateway: " + err.Error()}
	}
	defer drainAndClose(resp)
	if resp.StatusCode == http.StatusNotFound {
		return out.Session, &upstreamFailure{http.StatusNotFound, "session_not_found", "The agent has no record of this session."}
	}
	if resp.StatusCode != http.StatusOK {
		return out.Session, &upstreamFailure{http.StatusBadGateway, "agent_error", upstreamErrorDetail(resp)}
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxUpstreamBodyBytes)).Decode(&out); err != nil {
		return out.Session, &upstreamFailure{http.StatusBadGateway, "agent_bad_response", "Agent gateway returned an unreadable session."}
	}
	return out.Session, nil
}

// knownPlatformSkills lists the built-in Kube-Agents platform and cluster
// skills so references in tool calls, scheduled audit IDs, and kanban task
// bodies or results can be surfaced as structured skill badges.
// TestKnownPlatformSkillsCoverTheSkillsTree fails when a directory under
// agents/*/skills/ is missing here.
var knownPlatformSkills = []string{
	"gke-workload-troubleshooting",
	"gke-stall-detection",
	"gke-load-bearing-change",
	"gke-compute-classes",
	"gke-cluster-autoscaler",
	"gke-workload-scaling",
	"gke-reliability",
	"gke-storage",
	"gke-networking",
	"gke-service-networking",
	"gke-platform-security",
	"gke-workload-security",
	"gke-workload-identity",
	"gke-observability",
	"gke-alert-configuration",
	"gke-cost-analysis",
	"gke-cost-optimization",
	"gke-upgrades",
	"gke-backup-dr",
	"gke-basics",
	"gke-golden-path",
	"gke-productionize",
	"gke-app-onboarding",
	"gke-manifest-generation",
	"gke-multitenancy",
	"gke-cluster-creation",
	"gke-batch-hpc",
	"gke-inference",
	"gke-tpu-dynamic-slices-monitoring",
	"gke-tpu-metrics-monitoring",
	"gke-ai-troubleshooting-handle-disruption-gpu-tpu",
	"gke-ai-troubleshooting-jobset-interruption",
	"gke-ai-troubleshooting-tpu-vbar-oom",
	"fleet-audit",
	"fleet-audit-reports",
	"fleet-upgrade-verification",
	"gce-compute-fleet-audit",
	"gcp-networking-fabric-audit",
	"gcp-config-connector",
	"capacity-obtainability",
	"cluster-agent-lifecycle",
	"kube-agents-observability",
	"manage-cluster",
	"workload-rebalancing",
	"submit-suggestion",
	"version-control",
	"pr-conversation",
	"inspect-repository",
	"github-issue-resolver",
	"gke-rightsizing-checks",
	"gcp-terraform",
	"gitops-helm-chart",
	"kube-agents-feedback",
	"upgrade-readiness-brief",
	"support-escalation-bundle",
	"topology-drift",
}

var (
	skillPathPattern = regexp.MustCompile(`/skills/([a-z0-9][a-z0-9_-]{1,63})\b`)
	skillNameJSONPat = regexp.MustCompile(`"name"\s*:\s*"([a-z0-9][a-z0-9_-]{1,63})"`)
	cronSkillSlugPat = regexp.MustCompile(`^cron-(?:platform[-_])?([a-z0-9_-]+)-[0-9]{8}$`)
)

type hermesToolCallItem struct {
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

func parseHermesToolCalls(raw json.RawMessage) []hermesToolCallItem {
	if len(raw) == 0 {
		return nil
	}
	var items []hermesToolCallItem
	if err := json.Unmarshal(raw, &items); err == nil {
		return items
	}
	var encoded string
	if err := json.Unmarshal(raw, &encoded); err == nil && encoded != "" {
		_ = json.Unmarshal([]byte(encoded), &items)
	}
	return items
}

// toolKanbanCreate is the tool a routing turn calls to hand work to a card.
// The step it yields is a delegation; its result row carries the task ID.
const toolKanbanCreate = "kanban_create"

func extractMessageToolActivity(m hermesMessage) ([]messageStep, []string) {
	var steps []messageStep
	var skills []string
	for _, tc := range parseHermesToolCalls(m.ToolCalls) {
		name := clip(tc.Function.Name, statusFragmentRunes)
		if name == "" {
			continue
		}
		detail := previewToolCallArgs(name, tc.Function.Arguments)
		sk := detectSkillFromToolCall(name, tc.Function.Arguments)
		if sk != "" {
			skills = mergeSkills(skills, sk)
		}
		skills = mergeSkills(skills, findKnownSkillsInText(tc.Function.Arguments)...)
		st := messageStep{
			Tool:   name,
			Title:  "Finished " + name,
			Detail: detail,
			Skill:  sk,
		}
		if name == toolKanbanCreate {
			st.Delegation = true
			st.Title = "Delegated to a kanban card"
			var args struct {
				Title    string `json:"title"`
				Assignee string `json:"assignee"`
			}
			_ = json.Unmarshal([]byte(tc.Function.Arguments), &args)
			st.TaskTitle = clip(args.Title, statusFragmentRunes)
			st.Assignee = clip(args.Assignee, statusFragmentRunes)
		}
		steps = append(steps, st)
	}
	if m.Role == roleTool && m.Content != nil {
		if m.ToolName != nil && *m.ToolName == toolKanbanCreate {
			if id := kanbanCreateTaskID(*m.Content); id != "" {
				steps = append(steps, messageStep{Tool: toolKanbanCreate, Delegation: true, TaskID: id, Title: "Card " + id + " filed"})
			}
		}
	}
	return steps, skills
}

// kanbanCreateTaskID reads the task_id out of a kanban_create result row.
func kanbanCreateTaskID(content string) string {
	var out struct {
		OK     bool   `json:"ok"`
		TaskID string `json:"task_id"`
	}
	if err := json.Unmarshal([]byte(content), &out); err != nil || !out.OK {
		return ""
	}
	if !kanbanTaskIDPattern.MatchString(out.TaskID) {
		return ""
	}
	return out.TaskID
}

// foldDelegations merges each kanban_create call with the result row that
// follows it, so one step carries the card title, assignee and task ID.
func foldDelegations(steps []messageStep) []messageStep {
	out := steps[:0:0]
	for _, st := range steps {
		if st.Delegation && st.TaskID != "" && st.TaskTitle == "" {
			for i := len(out) - 1; i >= 0; i-- {
				if out[i].Delegation && out[i].TaskID == "" {
					out[i].TaskID = st.TaskID
					out[i].Title = "Delegated to card " + st.TaskID
					st.Tool = ""
					break
				}
			}
			if st.Tool == "" {
				continue
			}
		}
		out = append(out, st)
	}
	return out
}

func previewToolCallArgs(toolName, rawArgs string) string {
	trimmed := strings.TrimSpace(rawArgs)
	if trimmed == "" || trimmed == "{}" {
		return ""
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(trimmed), &parsed); err == nil {
		for _, k := range []string{"name", "title", "task_id", "command", "query", "assignee", "target"} {
			if v, ok := parsed[k].(string); ok && strings.TrimSpace(v) != "" {
				return clip(k+": "+v, statusFragmentRunes)
			}
		}
	}
	return clip(trimmed, statusFragmentRunes)
}

func detectSkillFromToolCall(toolName, previewOrArgs string) string {
	if toolName == "skill_view" {
		if m := skillNameJSONPat.FindStringSubmatch(previewOrArgs); m != nil {
			return m[1]
		}
		for _, sk := range knownPlatformSkills {
			if mentionsSkill(previewOrArgs, sk) {
				return sk
			}
		}
	}
	if m := skillPathPattern.FindStringSubmatch(previewOrArgs); m != nil {
		return m[1]
	}
	for _, sk := range knownPlatformSkills {
		if mentionsSkill(previewOrArgs, sk) {
			return sk
		}
	}
	return ""
}

// mentionsSkill reports whether text names skill as a whole name: the bytes
// on either side of the match cannot be part of a skill name, so
// "fleet-audit" is not found inside "gce-compute-fleet-audit" or
// "fleet-audit-reports".
func mentionsSkill(text, skill string) bool {
	for start := 0; start < len(text); {
		i := strings.Index(text[start:], skill)
		if i < 0 {
			return false
		}
		i += start
		end := i + len(skill)
		if (i == 0 || !isSkillNameByte(text[i-1])) && (end == len(text) || !isSkillNameByte(text[end])) {
			return true
		}
		start = i + 1
	}
	return false
}

// isSkillNameByte reports whether b can appear in a skill directory name.
func isSkillNameByte(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9') || b == '-' || b == '_'
}

// longestSkillMatches drops every skill that is part of another skill in
// the list, so a slug holding "gce-compute-fleet-audit" yields that skill
// and not also "fleet-audit".
func longestSkillMatches(skills []string) []string {
	var out []string
	for _, sk := range skills {
		inside := false
		for _, other := range skills {
			if other != sk && strings.Contains(other, sk) {
				inside = true
				break
			}
		}
		if !inside {
			out = append(out, sk)
		}
	}
	return out
}

func findKnownSkillsInText(text string) []string {
	if text == "" {
		return nil
	}
	var out []string
	for _, m := range skillPathPattern.FindAllStringSubmatch(text, -1) {
		if len(m) > 1 {
			out = mergeSkills(out, m[1])
		}
	}
	lower := strings.ToLower(text)
	for _, sk := range knownPlatformSkills {
		if mentionsSkill(lower, sk) {
			out = mergeSkills(out, sk)
		}
	}
	return out
}

func extractTextSkills(kind, sid, text string, card *messageCard) []string {
	var out []string
	if m := cronSkillSlugPat.FindStringSubmatch(sid); m != nil {
		slug := strings.ReplaceAll(m[1], "_", "-")
		var slugSkills []string
		for _, sk := range knownPlatformSkills {
			if strings.Contains(slug, sk) {
				slugSkills = append(slugSkills, sk)
			}
		}
		out = mergeSkills(out, longestSkillMatches(slugSkills)...)
		if sk, ok := cronJobSkillAliases[slug]; ok {
			out = mergeSkills(out, sk)
		}
	}
	out = mergeSkills(out, findKnownSkillsInText(text)...)
	// The topic rules below infer the skill a triage drew on from the
	// incident's own words. They run only on incident text: an event triage
	// session, or a card's fields. Plain chat gets only explicit references.
	if kind != kindEventTriage && card == nil {
		return out
	}
	combined := strings.ToLower(text)
	if card != nil {
		combined += " " + strings.ToLower(card.Subject+" "+card.Title+" "+card.Reason+" "+card.Warning+" "+card.Summary)
	}
	if kind == kindEventTriage || (card != nil && strings.HasPrefix(strings.ToLower(card.Title), "triage ")) {
		out = mergeSkills(out, "gke-workload-troubleshooting")
	}
	for _, rule := range skillTopicRules {
		if rule.pattern.MatchString(combined) {
			out = mergeSkills(out, rule.skill)
		}
	}
	return out
}

// cronJobSkillAliases name the skill a built-in scheduled job runs when the
// job's slug is not the skill's name: the stall watch files cards that run
// gke-stall-detection, and the board health check reads the kube-agents
// install itself.
var cronJobSkillAliases = map[string]string{
	"stall-watch":         "gke-stall-detection",
	"kanban-board-health": "kube-agents-observability",
}

// skillTopicRules map incident vocabulary to the skill that covers it.
var skillTopicRules = []struct {
	skill   string
	pattern *regexp.Regexp
}{
	{"gke-compute-classes", regexp.MustCompile(`\b(computeclass|compute-class|nodepoolautocreation|crdmisconfigured)\b`)},
	{"gke-storage", regexp.MustCompile(`\b(persistentvolumeclaims?|storageclass|unbound immediate)\b`)},
	{"gke-workload-scaling", regexp.MustCompile(`\b(oomkilled|out of memory|vpa|hpa|horizontalpodautoscaler|verticalpodautoscaler)\b`)},
	{"gke-stall-detection", regexp.MustCompile(`\b(stalled controllers?|available=false)\b`)},
	{"gke-cluster-autoscaler", regexp.MustCompile(`\b(failedscheduling|0/\d+ nodes are available|cluster autoscaler)\b`)},
	{"cluster-agent-lifecycle", regexp.MustCompile(`\b(kubeconfig|re-pinned|pinned to the wrong zone)\b`)},
	{"submit-suggestion", regexp.MustCompile(`\b(gitops|pull request|option a)\b`)},
}

func mergeSkills(existing []string, added ...string) []string {
	for _, a := range added {
		a = strings.TrimSpace(a)
		if a == "" {
			continue
		}
		found := false
		for _, e := range existing {
			if e == a {
				found = true
				break
			}
		}
		if !found {
			existing = append(existing, a)
		}
	}
	return existing
}
