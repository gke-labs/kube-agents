package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The inject backend: an HTTP door into handleInbound, for a harness that has
// to drive the gateway the way a chat user does. It is the next-stack
// analogue of the eval harness's POST to the agent's own /v1/responses -- the
// same idea as the Discord test backend in spec-chatops-gateway.md, with the
// eval as the consumer rather than a human.
//
// DEV AND EVAL ONLY, AND THE CODE CANNOT ENFORCE THAT. There is no
// authentication on either endpoint: a caller that reaches the listener may
// submit a task as any principal the map knows and read every reply the
// gateway has posted on any conversation. What confines it is the network
// edge and nothing else -- the operator renders this backend only under its
// eval flag, puts it behind a ClusterIP Service, and fences the gateway pod
// against every pod on the cluster network (the eval runner enters through
// kubectl port-forward, which is the node path and not pod-network traffic,
// so the fence does not govern it and the Kubernetes API session it already
// required is the authentication).
//
// The fence's exemption is the honest boundary: a NetworkPolicy does not
// govern host-local traffic, so a pod running with hostNetwork on the
// gateway's node reaches the listener the same way the port-forward does.
// Nothing this repository renders is hostNetwork, and a principal who can
// schedule one already has node-level reach -- but the claim is "no pod on
// the cluster network", not "nothing at all", and the difference should be
// written down rather than discovered.
//
// Arming this on an install a user can reach is handing that user the
// principal map. The design section in spec-chatops-gateway.md states the
// same thing in the place a reader looks for posture.

const (
	// injectBackend names the backend in authority blocks and config, the
	// way gchatBackend does for Google Chat.
	injectBackend = "inject"

	// injectVerifiedBy is what the authority block records as the mechanism
	// that checked the requester. It is deliberately not "principal-map",
	// which is what discord records: the map is consulted here too, and it
	// decides WHICH principal an author id stands for, but nothing
	// authenticates that the caller is that author. On Discord the
	// authenticated websocket does; on Google Chat the IAM-locked topic
	// does; here the network edge is the whole of it, and the audit record
	// should say so rather than borrow a stronger word.
	injectVerifiedBy = "inject-network-edge"

	// injectKeyPrefix marks every conversation key this backend handles, so
	// a synthetic conversation can never be mistaken for a real one in the
	// session registry, the ingress log or an authority block's audience.
	injectKeyPrefix = "inject:"

	// injectDMKeyPrefix is OpenDirect's answer: the DM-switch primitive
	// exists on every backend, and on this one a "DM" is just another
	// synthetic conversation.
	injectDMKeyPrefix = "inject:dm/"

	// injectMessageIDPrefix marks the synthetic backend message ids this
	// adapter mints, so a message id in a transcript is visibly not a
	// Discord snowflake or a Chat message resource name.
	// injectInboundIDPrefix is the same for the id minted for an inbound
	// message a caller did not name, which lands in the ingress log where a
	// Discord message id would.
	injectMessageIDPrefix = "inj-"
	injectInboundIDPrefix = "inj-msg-"

	// injectConversationKind is what every synthetic conversation reports as
	// its Kind, which rides into the authority block's audience. "dm" rather
	// than "group" because that is the truth: one participant, the caller.
	injectConversationKind = "dm"

	// The two endpoints. /inject takes a message; /conversations/<key>
	// returns what the relay has posted on that conversation.
	injectPath        = "/inject"
	conversationsPath = "/conversations/"

	// injectMaxBodyBytes bounds one request body. A prompt is a few
	// kilobytes; this is generous enough for any case's opening turn and
	// small enough that an unauthenticated door cannot be used to fill the
	// gateway's heap.
	injectMaxBodyBytes = 1 << 20

	// injectMaxTextRunes bounds the text of one injected message, and
	// injectMaxKeyRunes the caller-supplied conversation key. The key bound
	// matters twice: it rides into a JetStream KV key (registry.kvKey) and
	// into every log line for the conversation.
	injectMaxTextRunes = 64 * 1024
	injectMaxKeyRunes  = 128

	// injectMaxEntryBytes bounds one stored transcript entry, in BYTES: it is
	// handed to truncateRunes, which despite its name bounds a byte length at
	// a rune boundary. A separate name from the rune bound above because one
	// name for two units is how a bound comes to be misread -- the request
	// check counts characters a caller typed, this one counts memory the pod
	// holds. It applies to what the GATEWAY posts, never to the prompt: the
	// inbound text goes to the bus, not into this transcript.
	injectMaxEntryBytes = 64 * 1024

	// injectMaxEntries bounds one conversation's retained transcript and
	// injectMaxConversations how many conversations are retained at once.
	// Both evict oldest-first: this is an eval and debugging surface, not a
	// record -- the stream is the record -- so bounded memory beats complete
	// history on a long-lived pod.
	injectMaxEntries       = 1024
	injectMaxConversations = 256

	// injectReadHeaderTimeout bounds how long a client may take to send its
	// headers, and injectShutdownGrace how long Run waits for in-flight
	// requests once the context is done. A write timeout is deliberately
	// absent: a GET may block for the caller's requested wait, which
	// injectMaxWait bounds instead.
	injectReadHeaderTimeout = 10 * time.Second
	injectShutdownGrace     = 5 * time.Second

	// injectSubmitWait bounds how long POST /inject waits for the gateway to
	// say what it did with the message. It is the turn timeout plus a
	// margin: handleInbound runs under turnTimeout, so a turn that is going
	// to answer at all has answered by then, and a POST that waited longer
	// would be waiting on a gateway that has already given up.
	injectSubmitWait = turnTimeout + 10*time.Second

	// injectMaxWait bounds the wait a GET may ask for. A reader awaiting a
	// terminal re-issues the GET; an unbounded wait would let one caller
	// hold a connection for the life of the pod.
	injectMaxWait = 5 * time.Minute

	// injectPollInterval is how often a waiting request re-checks the
	// transcript. The notify channel below is what actually wakes it; this
	// is the backstop that bounds a missed wakeup to one interval.
	injectPollInterval = 250 * time.Millisecond
)

// Entry kinds in a conversation's transcript. A reader reconstructs what the
// conversation looks like from these: posts are messages the gateway sent,
// edits are the rolling progress line being rewritten in place, and the two
// task entries are the lifecycle bracket that makes "the reply to task X" a
// well-defined range rather than a guess about which post belongs to what.
const (
	InjectEntryPost     = "post"
	InjectEntryEdit     = "edit"
	InjectEntryTask     = "task"
	InjectEntryTerminal = "terminal"
)

// InjectEntry is one line of a conversation's transcript.
//
// Seq is per conversation and starts at 1, so a reader polls with the last
// sequence it saw and can neither miss an entry nor re-read one. MessageID is
// the synthetic backend message id: minted on a post, and on an edit it names
// the post being rewritten. TaskID and State are set on the two task entries
// only.
type InjectEntry struct {
	Seq       int    `json:"seq"`
	Kind      string `json:"kind"`
	Text      string `json:"text,omitempty"`
	MessageID string `json:"messageId,omitempty"`
	TaskID    string `json:"taskId,omitempty"`
	State     string `json:"state,omitempty"`
	// Source is set on a terminal entry only: who declared it (TerminalSource).
	// A caller that treats every `failed` alike scores a bus outage as the
	// agent answering badly.
	Source string `json:"source,omitempty"`
	TS     string `json:"ts"`
}

// injectRequest is the POST /inject body.
type injectRequest struct {
	// Conversation is the caller's own key for the conversation. The
	// adapter prefixes it; two requests with the same key are two turns of
	// one conversation, which is how a follow-up reaches the same session
	// record.
	Conversation string `json:"conversation"`
	// Author is the sender id, resolved through the principal map exactly
	// as a Discord snowflake is. An author with no entry is dropped at
	// verification, like any other unverifiable sender.
	Author string `json:"author"`
	// Text is the message.
	Text string `json:"text"`
	// MessageID is the caller's own id for this message, recorded against
	// the correlationId in the ingress log the way a backend message id is.
	// Optional; the adapter mints one when it is absent.
	MessageID string `json:"messageId,omitempty"`
}

// injectResponse is the POST /inject reply.
//
// TaskID is the task the message started, and Accepted says whether it
// started one at all: a message that steers a running task, asks for its
// status or stops it is a turn the gateway answered without minting a task,
// and so is a message from an author the principal map does not know. In
// every one of those cases the reply the conversation received is in Entries,
// and the caller reads it there rather than inferring it from a status code.
type injectResponse struct {
	Conversation string        `json:"conversation"`
	TaskID       string        `json:"taskId,omitempty"`
	Accepted     bool          `json:"accepted"`
	MessageID    string        `json:"messageId"`
	Entries      []InjectEntry `json:"entries"`
	Note         string        `json:"note,omitempty"`
}

// conversationResponse is the GET /conversations/<key> reply.
type conversationResponse struct {
	Conversation string        `json:"conversation"`
	Entries      []InjectEntry `json:"entries"`
	// LastSeq is the sequence of the last entry on the conversation, which
	// the caller passes back as the after parameter on its next GET.
	// Present even when Entries is empty, so a poll that timed out advances
	// nothing rather than rewinding.
	LastSeq int `json:"lastSeq"`
	// Terminal, when set, is the state of the task named by the request's
	// task parameter -- the whole of what "await terminal of task id X"
	// needs. Empty means that task has not terminated inside this GET's
	// wait.
	Terminal string `json:"terminal,omitempty"`
}

// injectConversation is one synthetic conversation's state.
type injectConversation struct {
	entries []InjectEntry
	// nextSeq is monotonic across evictions: a reader polling with a
	// sequence must never see one it has already consumed, even once the
	// oldest entries have been dropped.
	nextSeq int
	// terminals records the terminal state of every task that has ended on
	// this conversation, so a reader that arrives after the event still
	// learns the answer rather than waiting for one that has been and gone.
	terminals map[string]string
	// started records the tasks handleInbound minted on this conversation,
	// which is what POST /inject waits for.
	started []string
}

// InjectAdapter is the inject backend: an HTTP listener whose POST is an
// inbound chat message and whose GET is the conversation the relay has been
// posting into. It implements Adapter, so the gateway drives it exactly as it
// drives Discord, and TaskObserver, so it can answer about the task a POST
// started rather than making the caller parse chat text for it.
//
// Everything it holds is in memory and bounded. A restart loses the
// transcripts and keeps the sessions: the session record is in the KV bucket
// and the task's events are on the stream, which is where a conversation's
// history actually lives.
type InjectAdapter struct {
	listen string
	log    *slog.Logger

	mu            sync.Mutex
	conversations map[string]*injectConversation
	// order is the conversation keys in first-seen order, for the eviction
	// the map cannot do on its own.
	order  []string
	nextID int
	// notify is closed and replaced whenever anything changes, so a waiting
	// request wakes at once instead of at its poll interval. One channel for
	// the whole adapter rather than one per conversation: a wakeup is cheap
	// and a waiter re-checks its own conversation before returning.
	notify chan struct{}

	// handler is the gateway's inbound handler, set for the duration of Run.
	// A POST that arrives before Run (or after it returns) is refused rather
	// than silently dropped.
	handlerMu sync.RWMutex
	handler   func(InboundMessage)

	// listener, when set, is an already-bound listener Run serves on instead
	// of binding listen. Test injection only; the deployed adapter always
	// binds its configured address.
	listener net.Listener
}

// NewInjectAdapter builds the adapter for a listen address (host:port, or
// :port for every interface).
func NewInjectAdapter(listen string, log *slog.Logger) (*InjectAdapter, error) {
	if strings.TrimSpace(listen) == "" {
		return nil, fmt.Errorf("the inject backend needs a listen address")
	}
	if log == nil {
		log = slog.Default()
	}
	return &InjectAdapter{
		listen:        listen,
		log:           log,
		conversations: map[string]*injectConversation{},
		notify:        make(chan struct{}),
	}, nil
}

// Run serves the two endpoints until ctx is done.
func (a *InjectAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	a.handlerMu.Lock()
	a.handler = handler
	a.handlerMu.Unlock()
	defer func() {
		a.handlerMu.Lock()
		a.handler = nil
		a.handlerMu.Unlock()
	}()

	mux := http.NewServeMux()
	mux.HandleFunc(injectPath, a.handleInject)
	mux.HandleFunc(conversationsPath, a.handleConversation)
	srv := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: injectReadHeaderTimeout,
	}

	ln := a.listener
	if ln == nil {
		var err error
		ln, err = net.Listen("tcp", a.listen)
		if err != nil {
			return fmt.Errorf("inject backend listen on %s: %w", a.listen, err)
		}
	}
	a.log.Warn("the inject backend is armed: no authentication, no authorization -- "+
		"anything that reaches this listener can submit tasks as a mapped principal and "+
		"read every reply on every conversation. Dev and eval installs only.",
		"address", ln.Addr().String())

	errs := make(chan error, 1)
	go func() {
		err := srv.Serve(ln)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errs <- err
	}()

	select {
	case err := <-errs:
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), injectShutdownGrace)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
		return nil
	}
}

// Post records a message the gateway sent to a conversation.
func (a *InjectAdapter) Post(conversation, text string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	a.nextID = a.nextID + 1
	messageID := injectMessageIDPrefix + strconv.Itoa(a.nextID)
	a.appendLocked(conv, InjectEntry{Kind: InjectEntryPost, Text: text, MessageID: messageID})
	return messageID, nil
}

// Edit rewrites a previously posted message -- the rolling progress line. The
// edit is appended as its own entry rather than mutating the post: a reader
// polling for what is new must see the change, and the sequence of edits is
// exactly the progress narration a chat user watches.
func (a *InjectAdapter) Edit(conversation, messageID, text string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	a.appendLocked(conv, InjectEntry{Kind: InjectEntryEdit, Text: text, MessageID: messageID})
	return nil
}

// Roster is empty and complete. A synthetic conversation has exactly one
// participant, the requester, and handleInbound adds them itself -- so
// returning nothing here is not a degradation, it is the whole roster minus
// the entry the caller is about to add. Reporting it complete says there is
// no room to be incomplete about.
func (a *InjectAdapter) Roster(conversation string) ([]string, bool, error) {
	return nil, true, nil
}

// OpenDirect is the DM-switch primitive. Every backend owes it; on this one a
// direct conversation is another synthetic key.
func (a *InjectAdapter) OpenDirect(userID string) (string, error) {
	return injectDMKeyPrefix + userID, nil
}

// TaskStarted records the task handleInbound minted for a conversation. See
// TaskObserver for why the gateway tells the adapter rather than the adapter
// reading it out of chat text.
func (a *InjectAdapter) TaskStarted(conversation, taskID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	conv.started = append(conv.started, taskID)
	if len(conv.started) > injectMaxEntries {
		conv.started = conv.started[len(conv.started)-injectMaxEntries:]
	}
	a.appendLocked(conv, InjectEntry{Kind: InjectEntryTask, TaskID: taskID})
}

// TaskTerminal records a task's terminal state, which is what a reader
// awaits. It lands AFTER the relay has posted the deliverable (relayTerminal
// posts, then edits the rolling line, then announces), so a caller that sees
// the terminal has already seen the answer.
func (a *InjectAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	if conv.terminals == nil {
		conv.terminals = map[string]string{}
	}
	if len(conv.terminals) >= injectMaxEntries {
		conv.terminals = map[string]string{}
	}
	conv.terminals[taskID] = string(state)
	a.appendLocked(conv, InjectEntry{
		Kind: InjectEntryTerminal, TaskID: taskID, State: string(state), Source: string(source),
	})
}

// conversationLocked returns the conversation's state, minting it on first
// use and evicting the oldest conversation at the cap. Caller holds a.mu.
func (a *InjectAdapter) conversationLocked(key string) *injectConversation {
	if conv, ok := a.conversations[key]; ok {
		return conv
	}
	if len(a.order) >= injectMaxConversations {
		oldest := a.order[0]
		a.order = a.order[1:]
		delete(a.conversations, oldest)
	}
	conv := &injectConversation{nextSeq: 1, terminals: map[string]string{}}
	a.conversations[key] = conv
	a.order = append(a.order, key)
	return conv
}

// appendLocked stamps and stores one entry, then wakes every waiting reader.
// Caller holds a.mu.
func (a *InjectAdapter) appendLocked(conv *injectConversation, entry InjectEntry) {
	entry.Seq = conv.nextSeq
	conv.nextSeq = conv.nextSeq + 1
	entry.TS = time.Now().UTC().Format(time.RFC3339Nano)
	entry.Text = truncateRunes(entry.Text, injectMaxEntryBytes)
	conv.entries = append(conv.entries, entry)
	if len(conv.entries) > injectMaxEntries {
		conv.entries = conv.entries[len(conv.entries)-injectMaxEntries:]
	}
	close(a.notify)
	a.notify = make(chan struct{})
}

// snapshot returns a conversation's entries after seq, its last sequence, and
// the terminal state of taskID if it has one.
func (a *InjectAdapter) snapshot(key string, after int, taskID string) ([]InjectEntry, int, string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return nil, 0, ""
	}
	var out []InjectEntry
	for _, entry := range conv.entries {
		if entry.Seq > after {
			out = append(out, entry)
		}
	}
	terminal := ""
	if taskID != "" {
		terminal = conv.terminals[taskID]
	}
	return out, conv.nextSeq - 1, terminal
}

// startedSince reports the first task started on a conversation past the
// given count of prior starts.
func (a *InjectAdapter) startedSince(key string, priorStarts int) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return ""
	}
	if len(conv.started) > priorStarts {
		return conv.started[priorStarts]
	}
	return ""
}

// counts returns how many tasks have started and how many entries exist on a
// conversation -- the two things POST /inject watches for a change in.
func (a *InjectAdapter) counts(key string) (starts, entries int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return 0, 0
	}
	return len(conv.started), conv.nextSeq - 1
}

// waitCh returns the channel closed on the next change to any conversation.
func (a *InjectAdapter) waitCh() <-chan struct{} {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.notify
}

// handleInject is POST /inject: one inbound chat message.
func (a *InjectAdapter) handleInject(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		injectError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	var req injectRequest
	body := http.MaxBytesReader(w, r.Body, injectMaxBodyBytes)
	if err := json.NewDecoder(body).Decode(&req); err != nil {
		injectError(w, http.StatusBadRequest, "malformed body: "+err.Error())
		return
	}
	key, err := injectConversationKey(req.Conversation)
	if err != nil {
		injectError(w, http.StatusBadRequest, err.Error())
		return
	}
	if strings.TrimSpace(req.Author) == "" {
		injectError(w, http.StatusBadRequest, "author is required; it is resolved through the principal map")
		return
	}
	if req.Text == "" {
		injectError(w, http.StatusBadRequest, "text is required")
		return
	}
	if len([]rune(req.Text)) > injectMaxTextRunes {
		injectError(w, http.StatusRequestEntityTooLarge,
			fmt.Sprintf("text is longer than %d runes", injectMaxTextRunes))
		return
	}

	a.handlerMu.RLock()
	handler := a.handler
	a.handlerMu.RUnlock()
	if handler == nil {
		injectError(w, http.StatusServiceUnavailable, "the gateway is not running the inject backend yet")
		return
	}

	messageID := req.MessageID
	if messageID == "" {
		messageID = injectInboundIDPrefix + randHex(messageIDHexWidth)
	}
	// Read the counters BEFORE handing the message over, so the wait below
	// cannot mistake a previous turn's task or reply for this one's.
	priorStarts, priorEntries := a.counts(key)

	handler(InboundMessage{
		Conversation: key,
		Kind:         injectConversationKind,
		AuthorID:     req.Author,
		MessageID:    messageID,
		Text:         req.Text,
	})

	taskID, note := a.awaitTurn(r.Context(), key, priorStarts, priorEntries)
	entries, _, _ := a.snapshot(key, priorEntries, "")
	writeJSON(w, http.StatusOK, injectResponse{
		Conversation: key,
		TaskID:       taskID,
		Accepted:     taskID != "",
		MessageID:    messageID,
		Entries:      entries,
		Note:         note,
	})
}

// awaitTurn waits for the gateway to say what it did with a message: a task
// it started, or a reply it posted without starting one.
//
// A turn produces one of three things and the wait ends on the first of them.
// A new task announces itself before the placeholder post (startTask mints,
// announces, then posts), so a task id always wins the race against its own
// placeholder rather than the answer depending on timing. A steer, a status
// answer, a stop, or the once-per-sender notice for an unverifiable author
// posts without starting anything, and the caller reads what happened in the
// entries. Anything else -- a second message from an author already told they
// are unknown, a turn whose bus publish failed before the placeholder --
// produces neither, and the wait ends at its deadline with a note saying so,
// because the honest answer there is that the gateway did not visibly do
// anything with this message.
func (a *InjectAdapter) awaitTurn(ctx context.Context, key string, priorStarts, priorEntries int) (string, string) {
	deadline := time.Now().Add(injectSubmitWait)
	// A post is not proof the turn started nothing: handleInbound posts
	// BEFORE startTask on the heal paths (a stale task's status card, the
	// never-started notice), and then goes on to mint a task. So a turn that
	// has posted without starting anything is only concluded after a second
	// look one poll apart, by which time a task the same turn is about to
	// mint has been announced. A turn that really answered without starting
	// anything pays one extra poll interval for that.
	postedWithoutTask := false
	for {
		if taskID := a.startedSince(key, priorStarts); taskID != "" {
			return taskID, ""
		}
		if _, entries := a.counts(key); entries > priorEntries {
			if postedWithoutTask {
				return "", "the gateway answered this turn without starting a task (a steer, a status " +
					"answer, a stop, or an author the principal map does not know); the reply is in entries"
			}
			postedWithoutTask = true
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return "", fmt.Sprintf("the gateway neither started a task nor posted a reply within %s", injectSubmitWait)
		}
		woken := a.waitCh()
		timer := time.NewTimer(min(remaining, injectPollInterval))
		select {
		case <-ctx.Done():
			timer.Stop()
			return "", "the caller went away before the gateway answered"
		case <-woken:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// handleConversation is GET /conversations/<key>: what the relay has posted.
//
// Query parameters: after returns only entries past that sequence, task names
// the task whose terminal state the reply should carry, and wait is how many
// seconds to block for something new (or for that task's terminal) before
// answering. The three together are the whole of "await terminal of task id
// X": poll with the last sequence and the task id, and each reply carries
// both the new chat lines and the answer to whether it is over.
func (a *InjectAdapter) handleConversation(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		injectError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	raw := strings.TrimPrefix(r.URL.Path, conversationsPath)
	if raw == "" {
		injectError(w, http.StatusBadRequest, "no conversation in the path")
		return
	}
	// The path may carry the whole key, prefix included: it is the key the
	// POST reply handed back, and asking a caller to strip and re-add the
	// prefix is how the two ends come to disagree about what a key is.
	key := raw
	if !strings.HasPrefix(key, injectKeyPrefix) {
		key = injectKeyPrefix + key
	}
	after, err := intParam(r, "after")
	if err != nil {
		injectError(w, http.StatusBadRequest, err.Error())
		return
	}
	waitSeconds, err := intParam(r, "wait")
	if err != nil {
		injectError(w, http.StatusBadRequest, err.Error())
		return
	}
	wait := time.Duration(waitSeconds) * time.Second
	if wait > injectMaxWait {
		wait = injectMaxWait
	}
	taskID := r.URL.Query().Get("task")

	deadline := time.Now().Add(wait)
	for {
		entries, lastSeq, terminal := a.snapshot(key, after, taskID)
		if len(entries) > 0 || terminal != "" || !time.Now().Before(deadline) {
			writeJSON(w, http.StatusOK, conversationResponse{
				Conversation: key,
				Entries:      entries,
				LastSeq:      lastSeq,
				Terminal:     terminal,
			})
			return
		}
		woken := a.waitCh()
		timer := time.NewTimer(min(time.Until(deadline), injectPollInterval))
		select {
		case <-r.Context().Done():
			timer.Stop()
			return
		case <-woken:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// injectConversationKey validates a caller's conversation id and prefixes it.
//
// The prefix is not decoration: the key becomes the session registry's key
// and the audience's conversation in every authority block, and a synthetic
// conversation that could spell itself as a Google Chat thread resource name
// would be indistinguishable from a real one in the audit record. A caller
// that supplies the prefix itself is accepted unchanged, so the key from a
// POST reply can be handed straight back.
func injectConversationKey(raw string) (string, error) {
	key := strings.TrimSpace(raw)
	if key == "" {
		return "", fmt.Errorf("conversation is required")
	}
	if len([]rune(key)) > injectMaxKeyRunes {
		return "", fmt.Errorf("conversation is longer than %d runes", injectMaxKeyRunes)
	}
	for _, r := range key {
		// A control character would ride into the ingress log and the KV
		// key; neither has any business carrying a newline.
		if unicode.IsControl(r) {
			return "", fmt.Errorf("conversation contains a control character")
		}
	}
	if strings.HasPrefix(key, injectKeyPrefix) {
		return key, nil
	}
	if strings.Contains(key, ":") {
		return "", fmt.Errorf("conversation must not contain a colon unless it starts with %q: "+
			"the prefix is what keeps a synthetic conversation distinguishable from a real backend's",
			injectKeyPrefix)
	}
	return injectKeyPrefix + key, nil
}

// intParam reads a non-negative integer query parameter, absent meaning zero.
func intParam(r *http.Request, name string) (int, error) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return 0, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value < 0 {
		return 0, fmt.Errorf("%s must be a non-negative integer, got %q", name, raw)
	}
	return value, nil
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func injectError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
