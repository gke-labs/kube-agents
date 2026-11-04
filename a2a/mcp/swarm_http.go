package mcp

// POST /mcp/swarm: the swarm projection (swarm.go) served to the workstation
// bridge (#3689: a2a/mcpbridge/server.go queryRemoteSwarm), which posts its
// conversation table, subagent lineage and board ledger and writes the answer
// into Antigravity's Swarm UI directory.
//
// The request is untrusted. It is bounded by its own body limit, its lists
// are capped before anything reads them, and its deployment block is never
// decoded: the server reads the one install it is a client of, with the
// credential it was started with, and takes neither from a client.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// SwarmPath is where the bridge asks for the swarm view.
	SwarmPath = Path + "/swarm"

	// maxSwarmBodyBytes bounds one swarm request.
	maxSwarmBodyBytes = 1 << 20
	// maxSwarmConversationIDRunes bounds an Antigravity conversation id, the
	// root's included.
	maxSwarmConversationIDRunes = 128
	// taskSubjects is every task subject on TASKS.
	taskSubjects = "a2a.tasks.>"
	// swarmReadTTL is how long one TASKS read answers every swarm call. The
	// bridge calls after every tool call, so a busy workstation then costs
	// the bus one read, and one reader slot, per window rather than per call.
	swarmReadTTL = 3 * time.Second
	// swarmDrainSlack is how many messages past the limit one read takes, for
	// those that land between the stream's state and the consumer's start.
	swarmDrainSlack = drainFetchBatch

	msgSwarmBadRequest   = "the body is not a swarm request"
	msgSwarmTooMany      = "the request names %d %s; at most %d"
	noteDeploymentIgnore = "this server never takes a deployment or credentials from a client; it reads the install it runs in"
	noteTasksPartial     = "the TASKS read was partial, so tasks may be missing: %v"
	noteTasksBusy        = "the TASKS read was skipped because this server's bus readers were busy; no tasks were read, try again in a few seconds"
	noteTasksOldest      = "TASKS holds more than %d messages since the window began; the oldest were not read, so older tasks may be missing"
	noteTasksBudget      = "the TASKS read reached its %d MiB budget before the newest messages, which it reads last, so the newest tasks and states may be missing"
	noteDroppedEntries   = "%d conversation entries in the request were dropped: an alias or token that is not valid"
	noteNotConnected     = "not connected to the bus; no tasks were read"
	noteCardsUnavailable = "the agent cards could not be read"
)

// swarmTaskWindow is how far back the TASKS read starts: 3a's roster keeps a
// conversation that long after its last task. A variable for tests.
var swarmTaskWindow = swarmRecent

// swarmTaskLimit bounds the messages one TASKS read takes: the newest this
// many on the stream. A variable for tests.
var swarmTaskLimit = maxTaskEvents

// maxSwarmPromptRunes caps the prompt kept of a submission; the projection
// shortens it further for display.
const maxSwarmPromptRunes = 4096

// maxSwarmQuestionRunes caps the text kept of a task's input-required
// question; the projection shortens it further for display.
const maxSwarmQuestionRunes = 4096

// maxSwarmRefBytes caps a conversation reference kept from a submission.
const maxSwarmRefBytes = 256

// swarmCapRunes cuts s to at most n runes.
func swarmCapRunes(s string, n int) string {
	if utf8.RuneCountInString(s) <= n {
		return s
	}
	return string([]rune(s)[:n])
}

// swarmConversationEntry is one conversation in the bridge's shape
// (#3689: a2a/mcpbridge/config.go ConversationEntry). The server reads Alias,
// Token, Description, the Antigravity ids and LastUsedAt; every other field is
// the client's own, passed back as it sent it, since the bridge replaces its
// entry with the one returned whenever they differ.
type swarmConversationEntry struct {
	Alias                    string    `json:"alias"`
	Token                    string    `json:"token"`
	SessionMode              bool      `json:"sessionMode"`
	SessionTerminated        bool      `json:"sessionTerminated,omitempty"`
	Description              string    `json:"description,omitempty"`
	HostConversationID       string    `json:"hostConversationId,omitempty"`
	ParentHostConversationID string    `json:"parentHostConversationId,omitempty"`
	SubagentRole             string    `json:"subagentRole,omitempty"`
	SessionWorkerID          string    `json:"sessionWorkerId,omitempty"`
	AutoBound                bool      `json:"autoBound,omitempty"`
	DebugMode                bool      `json:"debugMode,omitempty"`
	Disconnected             bool      `json:"disconnected,omitempty"`
	CreatedAt                time.Time `json:"createdAt"`
	LastUsedAt               time.Time `json:"lastUsedAt"`
}

// swarmLineage is one subagent's lineage (#3689:
// a2a/mcpbridge/swarm_lineage.go ConversationLineage).
type swarmLineage struct {
	ConversationID       string `json:"conversationId"`
	ParentConversationID string `json:"parentConversationId,omitempty"`
	RootConversationID   string `json:"rootConversationId"`
	SubagentType         string `json:"subagentType,omitempty"`
	SubagentRole         string `json:"subagentRole,omitempty"`
	SubagentState        string `json:"subagentState,omitempty"`
	IsSubagent           bool   `json:"isSubagent"`
}

// swarmRequest is the part of the bridge's RemoteSwarmRequest the server
// reads. Its deployment, active conversation, board lines and event drain
// are not decoded at all, and unknown fields are ignored.
type swarmRequest struct {
	RootConversationID string                            `json:"rootConversationId"`
	Conversations      map[string]swarmConversationEntry `json:"conversations"`
	Subagents          map[string]swarmLineage           `json:"subagents"`
	Ledger             BoardLedger                       `json:"ledger"`
}

// swarmResponse is the bridge's RemoteSwarmResponse, without the deployment,
// the active conversation and the schema flag, and with three fields it
// ignores: what the server did not take, did not serve, and could not read.
type swarmResponse struct {
	Connected           bool                              `json:"connected"`
	Snapshot            SwarmSnapshot                     `json:"snapshot"`
	Ledger              BoardLedger                       `json:"ledger"`
	Conversations       map[string]swarmConversationEntry `json:"conversations,omitempty"`
	PendingEvents       []json.RawMessage                 `json:"pendingEvents"`
	DeploymentIgnored   bool                              `json:"deploymentIgnored"`
	PendingEventsServed bool                              `json:"pendingEventsServed"`
	Notes               []string                          `json:"notes"`
}

// swarmHandler answers POST /mcp/swarm from the bus and the server's own
// conversation table.
type swarmHandler struct {
	bus   *NATSBus
	convs *Conversations
	now   func() time.Time // nil is time.Now

	// mu serialises TASKS reads, so concurrent calls share one read (and
	// one reader slot) rather than each taking one, and guards the cache.
	mu       sync.Mutex
	cached   swarmTaskSet
	cachedAt time.Time
}

// swarmTaskSet is one TASKS read: every task it found, before any request's
// filter, and the notes on how it went.
type swarmTaskSet struct {
	tasks []SwarmTask
	notes []string
}

func (h *swarmHandler) clock() time.Time {
	if h.now != nil {
		return h.now()
	}
	return time.Now()
}

func (h *swarmHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxSwarmBodyBytes))
	if err != nil {
		if tooLarge := (*http.MaxBytesError)(nil); errors.As(err, &tooLarge) {
			http.Error(w, msgBodyTooLarge, http.StatusRequestEntityTooLarge)
		} else {
			http.Error(w, msgSwarmBadRequest, http.StatusBadRequest)
		}
		return
	}
	var req swarmRequest
	// A JSON null decodes into the zero request without an error; it is no
	// request at all.
	if bytes.Equal(bytes.TrimSpace(body), []byte("null")) {
		http.Error(w, msgSwarmBadRequest, http.StatusBadRequest)
		return
	}
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, msgSwarmBadRequest, http.StatusBadRequest)
		return
	}
	if n := len(req.Conversations); n > maxSwarmInputConvs {
		http.Error(w, fmt.Sprintf(msgSwarmTooMany, n, "conversations", maxSwarmInputConvs), http.StatusBadRequest)
		return
	}
	if n := len(req.Subagents); n > maxSwarmInputConvs {
		http.Error(w, fmt.Sprintf(msgSwarmTooMany, n, "subagents", maxSwarmInputConvs), http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), requestBudget)
	defer cancel()
	resp := h.answer(ctx, req, h.clock())
	w.Header().Set(headerContentType, contentTypeJSON)
	_ = json.NewEncoder(w).Encode(resp)
}

// answer builds the response to a request already bounded by count. It
// projects only the conversations the request names.
func (h *swarmHandler) answer(ctx context.Context, req swarmRequest, now time.Time) swarmResponse {
	resp := swarmResponse{
		PendingEvents:     []json.RawMessage{},
		DeploymentIgnored: true,
		Notes:             []string{noteDeploymentIgnore},
	}
	entries, dropped := mergeSwarmConversations(h.convs.List(), req.Conversations)
	if dropped > 0 {
		resp.Notes = append(resp.Notes, fmt.Sprintf(noteDroppedEntries, dropped))
	}
	in := SwarmInput{Conversations: swarmConversations(entries, req.Subagents), Now: now}

	resp.Connected = h.bus.client.Conn().IsConnected()
	if resp.Connected {
		set := h.taskSet(ctx)
		resp.Notes = append(resp.Notes, set.notes...)
		in.Tasks = keepSwarmTasks(set.tasks, entries)
		if cards := h.bus.Status(ctx).AgentCards; cards != nil {
			in.Cards = cards
		} else {
			resp.Notes = append(resp.Notes, noteCardsUnavailable)
		}
	} else {
		resp.Notes = append(resp.Notes, noteNotConnected)
	}

	resp.Snapshot, resp.Ledger = BuildSwarmState(in, req.Ledger)
	// The coordinator and the platform answer for the bridge's root
	// conversation; 3a leaves their ids for the caller, who knows it. The id
	// passes the same path-safety check as every id 3a sets, since the
	// client joins it into paths.
	if root := safeConversationID(req.RootConversationID); root != "" {
		for _, name := range []string{swarmMemberCoordinator, swarmMemberPlatform} {
			if m, ok := resp.Snapshot.Members[name]; ok {
				m.ConversationID = root
				resp.Snapshot.Members[name] = m
			}
		}
	}
	if len(entries) > 0 {
		resp.Conversations = make(map[string]swarmConversationEntry, len(entries))
		for _, e := range entries {
			resp.Conversations[e.Alias] = e
		}
	}
	return resp
}

// taskSet is the TASKS read, at most swarmReadTTL old. Concurrent callers
// wait for one read and share it. A read its caller cut short, or one the
// busy readers skipped, answers that caller only.
func (h *swarmHandler) taskSet(ctx context.Context) swarmTaskSet {
	h.mu.Lock()
	defer h.mu.Unlock()
	if !h.cachedAt.IsZero() && h.clock().Sub(h.cachedAt) < swarmReadTTL {
		return h.cached
	}
	read, err := h.bus.swarmTasks(ctx, h.clock().Add(-swarmTaskWindow))
	set := swarmTaskSet{tasks: read.tasks}
	switch {
	case errors.Is(err, errReadersBusy):
		set.notes = append(set.notes, noteTasksBusy)
		return set
	case errors.Is(err, ErrReadByteBudget):
		set.notes = append(set.notes, fmt.Sprintf(noteTasksBudget, readMaxBytes>>20))
	case err != nil:
		set.notes = append(set.notes, fmt.Sprintf(noteTasksPartial, err))
	}
	if read.oldestDropped {
		set.notes = append(set.notes, fmt.Sprintf(noteTasksOldest, swarmTaskLimit))
	}
	// A read cut short, by its caller, its deadline or its byte budget,
	// answers this call only: it is not what the next caller should see.
	if err != nil || ctx.Err() != nil {
		return set
	}
	h.cached, h.cachedAt = set, h.clock()
	return set
}

// validSwarmConversationID reports whether id looks like an Antigravity
// conversation id: present, at most maxSwarmConversationIDRunes, and free of
// control characters.
func validSwarmConversationID(id string) bool {
	if id == "" || len(id) > maxSwarmConversationIDRunes*utf8.UTFMax || !utf8.ValidString(id) ||
		utf8.RuneCountInString(id) > maxSwarmConversationIDRunes {
		return false
	}
	return !strings.ContainsFunc(id, unicode.IsControl)
}

// swarmConversationToken is a conversation token as the console door takes
// it, bare or as console:<token>, or "" when it is not one.
func swarmConversationToken(raw string) string {
	t := strings.TrimPrefix(strings.TrimSpace(raw), consoleConversationPrefix)
	if len(t) > maxSwarmHandleBytes || !lib.ValidSubjectToken(t) {
		return ""
	}
	return t
}

// mergeSwarmConversations is the request's conversations, one entry per entry
// kept, in alias order. The server's table only resolves the names the request
// gives: where it holds the alias, the conversation is the server's, the one
// this server sent that name's turns to, so its token is used; otherwise the
// client's token, which must be a valid conversation token. Every other field
// is the client's, capped; the description is the client's unless it has
// none, and the last use is the later of the two. The table's other names are
// neither projected nor returned. dropped counts the entries whose alias or
// token is not valid.
func mergeSwarmConversations(server []ConversationInfo, client map[string]swarmConversationEntry) (out []swarmConversationEntry, dropped int) {
	serverByAlias := make(map[string]ConversationInfo, len(server))
	for _, c := range server {
		serverByAlias[c.Alias] = c
	}
	out = make([]swarmConversationEntry, 0, len(client))
	for alias, e := range client {
		// 3a trims an alias; one it would change is refused rather than
		// returned under a name the bridge did not send.
		if alias == "" || alias != strings.TrimSpace(alias) || len(alias) > maxSwarmAliasBytes {
			dropped++
			continue
		}
		e = cappedClientEntry(e)
		e.Alias = alias
		if s, ok := serverByAlias[alias]; ok {
			e.Token = s.Token
			if e.Description == "" {
				e.Description = s.Description
			}
			if last := s.LastUsed.UTC(); last.After(e.LastUsedAt) {
				e.LastUsedAt = last
			}
		} else if e.Token = swarmConversationToken(e.Token); e.Token == "" {
			dropped++
			continue
		}
		out = append(out, e)
	}
	slices.SortFunc(out, func(x, y swarmConversationEntry) int { return strings.Compare(x.Alias, y.Alias) })
	return out, dropped
}

// cappedClientEntry applies 3a's byte limits to a client entry's strings: one
// past its limit is dropped, not shortened, so nothing half a value is passed
// on as if it were one.
func cappedClientEntry(e swarmConversationEntry) swarmConversationEntry {
	if len(e.Description) > swarmScanBytes {
		e.Description = ""
	}
	if !validSwarmConversationID(e.HostConversationID) {
		e.HostConversationID = ""
	}
	if !validSwarmConversationID(e.ParentHostConversationID) {
		e.ParentHostConversationID = ""
	}
	if len(e.SubagentRole) > maxSwarmHandleBytes {
		e.SubagentRole = ""
	}
	if len(e.SessionWorkerID) > maxSwarmHandleBytes {
		e.SessionWorkerID = ""
	}
	e.CreatedAt, e.LastUsedAt = e.CreatedAt.UTC(), e.LastUsedAt.UTC()
	return e
}

// swarmConversations turns the merged table into 3a's input. A parent is the
// alias of the entry whose Antigravity id is this entry's parent id, or, when
// that names none, the one the subagent lineage gives as its parent.
func swarmConversations(entries []swarmConversationEntry, subagents map[string]swarmLineage) []SwarmConversation {
	aliasOfHost := make(map[string]string, len(entries))
	for _, e := range entries {
		if e.HostConversationID == "" {
			continue
		}
		if have, dup := aliasOfHost[e.HostConversationID]; !dup || e.Alias < have {
			aliasOfHost[e.HostConversationID] = e.Alias
		}
	}
	out := make([]SwarmConversation, 0, len(entries))
	for _, e := range entries {
		parent := aliasOfHost[e.ParentHostConversationID]
		if parent == "" && e.HostConversationID != "" {
			if l, ok := subagents[e.HostConversationID]; ok && validSwarmConversationID(l.ParentConversationID) {
				parent = aliasOfHost[l.ParentConversationID]
			}
		}
		if parent == e.Alias {
			parent = ""
		}
		out = append(out, SwarmConversation{
			Alias: e.Alias, Token: e.Token, Description: e.Description, Parent: parent, LastUsed: e.LastUsedAt,
			// The projection validates it again before a member carries it.
			AGYConversationID: e.HostConversationID,
		})
	}
	return out
}

// keepSwarmTasks keeps the tasks of the table's conversations, and those
// delegated from a kept task, transitively to maxLineageDepth hops.
func keepSwarmTasks(tasks []SwarmTask, entries []swarmConversationEntry) []SwarmTask {
	conversations := make(map[string]bool, len(entries))
	for _, e := range entries {
		conversations[consoleConversationPrefix+e.Token] = true
	}
	kept := make(map[string]bool, len(tasks))
	for i := range tasks {
		if conversations[tasks[i].Conversation] {
			kept[tasks[i].TaskID] = true
		}
	}
	for hop := 0; hop < maxLineageDepth; hop++ {
		grew := false
		for i := range tasks {
			if t := &tasks[i]; !kept[t.TaskID] && t.ViaTaskID != "" && kept[t.ViaTaskID] {
				kept[t.TaskID] = true
				grew = true
			}
		}
		if !grew {
			break
		}
	}
	out := make([]SwarmTask, 0, len(kept))
	for _, t := range tasks {
		if kept[t.TaskID] {
			out = append(out, t)
		}
	}
	return out
}

// swarmTaskRead is one task as the TASKS read found it.
type swarmTaskRead struct {
	ref       TaskRef
	submitted bool
	task      SwarmTask
	events    []*lib.Envelope
	// firstAt is when the read's first message of the task was stored.
	firstAt time.Time
}

// swarmRead is what one TASKS read found, and how it fell short.
type swarmRead struct {
	tasks []SwarmTask
	// oldestDropped: the read started past the stream's first message and
	// found none older than the window, so the window held more than the
	// read takes, and its oldest messages were not read.
	oldestDropped bool
}

// swarmTasks reads the newest swarmTaskLimit messages on TASKS in one drain
// on one reader slot, keeps those since since, and folds each task. A task
// whose submission is older than since has no conversation here, and is not
// kept, unless a later message on its .in (a steer) is inside the window: it
// is live, so it is kept, but with no prompt, since what the read holds is
// not its submission. A read that stops short returns what it read with the
// error that stopped it.
func (b *NATSBus) swarmTasks(ctx context.Context, since time.Time) (swarmRead, error) {
	ctx, cancel := context.WithTimeout(ctx, taskReadTimeout)
	defer cancel()
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		return swarmRead{}, err
	}
	state := s.CachedInfo().State
	if state.Msgs == 0 {
		return swarmRead{}, nil
	}
	start := state.FirstSeq
	if limit := uint64(swarmTaskLimit); state.LastSeq >= limit && state.LastSeq-limit+1 > start {
		start = state.LastSeq - limit + 1
	}
	var (
		reads       = map[TaskRef]*swarmTaskRead{}
		order       []*swarmTaskRead
		submittedAt = map[TaskRef]time.Time{} // the first .in older than the window
		sawOlder    bool
		read        swarmRead
	)
	// One drain, oldest first, within the bus reader's byte budget: a window
	// past the budget stops before its newest messages, and the error says
	// so (ErrReadByteBudget).
	derr := b.readSwarmWindow(ctx, s, start, since, reads, &order, submittedAt, &sawOlder)
	read.oldestDropped = start > state.FirstSeq && !sawOlder
	addresseeOf := make(map[string]string, len(order))
	for _, rd := range order {
		if _, dup := addresseeOf[rd.ref.TaskID]; dup {
			addresseeOf[rd.ref.TaskID] = ""
		} else {
			addresseeOf[rd.ref.TaskID] = rd.ref.Addressee
		}
	}
	read.tasks = make([]SwarmTask, 0, len(order))
	for _, rd := range order {
		if !rd.submitted {
			continue
		}
		t := rd.task
		if len(rd.events) > 0 {
			if folded, err := lib.FoldTask(rd.ref.TaskID, rd.events); err == nil {
				t.Task = folded
			}
		}
		if t.ViaTaskID != "" {
			t.ViaAddressee = addresseeOf[t.ViaTaskID]
		}
		read.tasks = append(read.tasks, t)
	}
	return read, derr
}

// readSwarmWindow is one drain of TASKS from start, filling reads, order,
// submittedAt and sawOlder as swarmTasks describes. What it keeps is bounded
// by the drain's byte budget; a submission's conversation reference and
// prompt are capped as well.
func (b *NATSBus) readSwarmWindow(ctx context.Context, s jetstream.Stream, start uint64, since time.Time,
	reads map[TaskRef]*swarmTaskRead, order *[]*swarmTaskRead, submittedAt map[TaskRef]time.Time, sawOlder *bool) error {
	_, derr := b.drainConsumer(ctx, s, jetstream.ConsumerConfig{
		FilterSubject: taskSubjects,
		DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:   start,
	}, swarmTaskLimit+swarmDrainSlack, func(msg jetstream.Msg) {
		addressee, taskID, class, ok := lib.ParseTaskSubject(msg.Subject())
		if !ok {
			return
		}
		var at time.Time
		var seq uint64
		if md, err := msg.Metadata(); err == nil {
			at, seq = md.Timestamp, md.Sequence.Stream
		}
		ref := TaskRef{Addressee: addressee, TaskID: taskID}
		if !at.IsZero() && at.Before(since) {
			*sawOlder = true
			if _, have := submittedAt[ref]; !have && class == lib.TaskClassIn && agreedEnvelope(msg.Subject(), msg.Data()) != nil {
				submittedAt[ref] = at
			}
			return
		}
		env := agreedEnvelope(msg.Subject(), msg.Data())
		if env == nil {
			return
		}
		if at.IsZero() {
			at = env.TS
		}
		rd := reads[ref]
		if rd == nil {
			rd = &swarmTaskRead{ref: ref, task: SwarmTask{Addressee: addressee, TaskID: taskID}}
			reads[ref] = rd
			*order = append(*order, rd)
		}
		if rd.firstAt.IsZero() {
			rd.firstAt = at
		}
		if class != lib.TaskClassIn {
			noteQuestion(rd, env, seq)
			rd.events = append(rd.events, env)
			return
		}
		// The first message on .in in the read is the conversation's; later
		// ones are follow-ups and cancels.
		if rd.submitted {
			return
		}
		_, a, text, ok := submission(&nats.Msg{Subject: msg.Subject(), Data: msg.Data()})
		if !ok {
			return
		}
		// What a task's record keeps is bounded too: the conversation only
		// up to maxSwarmRefBytes (a delegated task's need not be a console
		// one; it is kept through its parent), the delegation parent only
		// as a valid task id, and the prompt capped at maxSwarmPromptRunes.
		conv := a.Audience.Conversation
		if len(conv) > maxSwarmRefBytes {
			conv = ""
		}
		via := ""
		if a.Via != nil && lib.ValidSubjectToken(a.Via.TaskID) {
			via = a.Via.TaskID
		}
		text = swarmCapRunes(text, maxSwarmPromptRunes)
		rd.submitted = true
		rd.task.Conversation = conv
		rd.task.ViaTaskID = via
		rd.task.Submitted = at
		// It is the submission only if nothing of the task came before it:
		// not an older .in, and no event earlier in the read. When events
		// came first, this .in is a steer, and the task began no later than
		// its first event in the read.
		switch older, ok := submittedAt[ref]; {
		case ok:
			rd.task.Submitted = older
		case len(rd.events) == 0:
			rd.task.Prompt = text
		case rd.firstAt.Before(at):
			rd.task.Submitted = rd.firstAt
		}
	})
	return derr
}

// noteQuestion records the task's latest input-required status message and
// its stream sequence, which the fold does not keep but the projection posts
// and keys on. Only its text parts are kept, capped: a question is shown as
// text, and the event it came in is charged against the byte cap already.
func noteQuestion(rd *swarmTaskRead, env *lib.Envelope, seq uint64) {
	if env.Kind != lib.KindStatusUpdate {
		return
	}
	var su lib.StatusUpdate
	if json.Unmarshal(env.Payload, &su) != nil || su.Status.State != lib.StateInputRequired || su.Status.Message == nil {
		return
	}
	q := &lib.Message{Role: su.Status.Message.Role, MessageID: su.Status.Message.MessageID}
	left := maxSwarmQuestionRunes
	for _, p := range su.Status.Message.Parts {
		if p.Text == "" || left <= 0 {
			continue
		}
		t := swarmCapRunes(p.Text, left)
		left -= utf8.RuneCountInString(t)
		q.Parts = append(q.Parts, lib.Part{Kind: p.Kind, Text: t})
	}
	rd.task.Question, rd.task.QuestionSeq = q, seq
}
