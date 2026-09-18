package gateway

import (
	"context"
	"crypto/subtle"
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
// DEV AND EVAL ONLY. Four things confine it, and the first is the only one
// inside this file.
//
// Every request carries a bearer token (A2A_INJECT_TOKEN, which the operator
// renders into a Secret under its eval flag and the gateway refuses to arm
// the door without). The other three are the operator's: the door renders
// only under that flag, its Service is a ClusterIP, and while it is armed a
// NetworkPolicy fences the gateway pod against every pod on the cluster
// network.
//
// The token is not belt-and-braces over that fence, it is the control, and
// the fence is the secondary. The eval runner reaches the Service through
// `kubectl port-forward`, which enters from the node and is not pod-network
// traffic -- so the fence never governs the door's own caller. Without a
// token, the population that can drive the platform persona with the
// install's cluster and GitHub credentials would be everyone holding
// pods/portforward in the namespace, rather than the holders of the agent's
// API key the door stands in for. The fence's honest edge is the same
// exemption seen from the other side: a hostNetwork pod on the gateway's
// node reaches the listener the way the port-forward does, and the token is
// what that pod would still lack.
//
// What the token is not is an identity. It says the caller may use the door;
// the author in the request body says who they claim to be, and the door's
// own principal map (Gateway.resolveInjectPrincipal) is what decides whether
// that claim resolves to anything -- to an eval identity, never a cloud one.
// The design section in spec-chatops-gateway.md states the same in the place
// a reader looks for posture.

const (
	// injectBackend names the backend in authority blocks and config, the
	// way gchatBackend does for Google Chat.
	injectBackend = "inject"

	// injectVerifiedBy is what the authority block records as the mechanism
	// that checked the requester. Deliberately not "principal-map", which is
	// what discord records, and deliberately not anything a real backend
	// stamps: on Discord an authenticated websocket vouches for the sender,
	// on Google Chat an IAM-locked topic does, and here it is this door's
	// bearer token. A consumer that treats the three alike is the thing this
	// value exists to stop.
	injectVerifiedBy = "inject-bearer"

	// injectPrincipalPrefix qualifies every key in the door's own principal
	// map, and injectEvalPrincipalPrefix every value. Together they are what
	// makes the door structurally incapable of asserting a principal a real
	// backend's sender could hold: the lookup cannot reach an unprefixed
	// entry, and a value outside the eval namespace is refused rather than
	// honoured (Gateway.resolveInjectPrincipal).
	injectPrincipalPrefix     = "inject:"
	injectEvalPrincipalPrefix = "eval:"

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

	// The three endpoints. /inject takes a message; /conversations/<key>
	// returns what the relay has posted on that conversation and, asked
	// with ?probe=1, what the gateway's session record holds; and
	// /conversations/<key>/cancel stops the task running on it.
	injectPath        = "/inject"
	conversationsPath = "/conversations/"
	cancelSuffix      = "/cancel"

	// probeParam asks the read route to run the gateway's ConversationProbe
	// before answering, and probeTimeout bounds that call: one KV read and
	// one replay of the task's stream, which is what a turn's stream read
	// costs, so it gets a turn's bound.
	probeParam   = "probe"
	probeTimeout = turnTimeout

	// injectSeenCap bounds the dedupe memory for accepted POSTs, keyed by
	// the caller's backend message id -- the same shape and size as the
	// gchat adapter's redelivery set. A harness retries an opening POST
	// whose connection dropped with the same body, and startTask has by
	// then written the active task, so the retry would otherwise be routed
	// as a steer; the id is what says it is the same message.
	injectSeenCap = 4096

	// The bearer credential, spelled once. The scheme is compared
	// case-insensitively (RFC 7235 makes it a case-insensitive token) and
	// the credential in constant time.
	authorizationHeader = "Authorization"
	bearerScheme        = "bearer "

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

// Why a POST was answered with no task id. The note beside it is prose for a
// log; these are what a caller branches on, because a program classifying a
// run must not have to match on a sentence this file is free to rewrite. A
// refusal is the whole answer to that POST: nothing was started, nothing is
// on any subject, and a repeat of the same backend message id is answered
// with the same code.
const (
	// injectRefusalUnverifiedAuthor: the door's principal map does not carry
	// the author, so the gateway dropped the message. The notice naming the
	// author is in the entries on the first drop and nowhere on a later one
	// (it is posted once per sender), which is why this code rather than the
	// transcript is the answer.
	injectRefusalUnverifiedAuthor = "unverified-author"
	// injectRefusalPublishFailed: the gateway minted the task and could not
	// put it on the bus. It has already edited the placeholder to say so and
	// released the conversation; no executor can ever see the ask.
	injectRefusalPublishFailed = "publish-failed"
	// injectRefusalNoTask: the turn was answered without minting a task -- a
	// steer onto a running task, a status answer, a stop. The reply the
	// conversation received is in the entries.
	injectRefusalNoTask = "no-task"
	// injectRefusalNoAnswer: the bound expired with the gateway having done
	// nothing this door could see. Distinct from the three above because it
	// is the absence of an answer rather than one.
	injectRefusalNoAnswer = "no-answer"
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
	// Reason is set on a terminal entry only: the executor's terminal status
	// message, verbatim (`reason: <token>[ - detail]` from the bridge and
	// the worker adapter). Empty when the terminal carried none. A caller
	// reads the token to tell the executor's own failure (bridge-shutdown,
	// spawn-failed) from the persona's (hermes-exited-nonzero).
	Reason string `json:"reason,omitempty"`
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
	// Optional; the adapter mints one when it is absent. When present it is
	// also the dedupe key: a second POST carrying an id this door has
	// already accepted is answered with the first one's task id and starts
	// nothing (injectResponse.Deduplicated), which is what makes a retry of
	// a dropped POST safe.
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
	// FirstEventGraceSeconds is the gateway's A2A_FIRST_EVENT_GRACE: how
	// long a task with nothing on its events subject may hold this
	// conversation before the never-started heal releases it.
	//
	// Reported so that the caller does not run a second clock. "Nobody took
	// this task" is a window the gateway already owns, and a harness that
	// invents its own would disagree with the gateway about when a task is
	// abandoned -- cancelling inside the grace, which reaches no executor
	// and is answered by nothing. A caller reads this and sets its own
	// deadline above it, and reads the window off the read route
	// (probeReport) rather than off a clock of its own.
	FirstEventGraceSeconds int `json:"firstEventGraceSeconds"`
	// Refusal is why no task id is in this reply, as one of the codes above
	// (injectRefusalUnverifiedAuthor and the rest). Empty when TaskID is
	// set. A caller branches on it rather than on Note, which is prose.
	Refusal string `json:"refusal,omitempty"`
	// Deduplicated is true when this POST's message id had already been
	// accepted: TaskID, Note and Refusal are the first POST's, and nothing
	// was started or routed for this one.
	Deduplicated bool `json:"deduplicated,omitempty"`
}

// cancelRequest is the POST /conversations/<key>/cancel body. The
// conversation is in the path; the author is here, because a cancel is an
// authority-bearing action on the conversation and goes through the same
// verification an ordinary message does.
type cancelRequest struct {
	Author    string `json:"author"`
	MessageID string `json:"messageId,omitempty"`
	// TaskID names the task the cancel is for: the id the POST answered
	// with. Given, the cancel reaches the bus whether or not the record
	// still holds the task as active (see Gateway.cancelNamedTask); empty
	// stops whatever the conversation is running, as the stop text would.
	TaskID string `json:"taskId,omitempty"`
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
	// Probe is present when the request asked for one (?probe=1): the
	// gateway's session record and the task's stream state, read with
	// nothing changed (ConversationProbe). The caller classifies from it.
	Probe *probeReport `json:"probe,omitempty"`
}

// probeReport is ConversationState on the wire, plus the door's own two
// additions: the conversation's last post, which the gateway's record does
// not hold and this transcript does, and an error for "could not look".
//
// Nothing here is a verdict. A caller decides "nobody took this task" from
// active, executorState "" and ageSeconds past graceSeconds; "queued and
// never run" from executorState submitted at its own deadline; "running
// past my budget" from working. The read reports and the caller classifies,
// which is what keeps the read pure.
type probeReport struct {
	Backend      string `json:"backend"`
	InjectOnly   bool   `json:"injectOnly"`
	GraceSeconds int    `json:"graceSeconds"`
	Active       bool   `json:"active"`
	TaskID       string `json:"taskId,omitempty"`
	SubmittedAt  string `json:"submittedAt,omitempty"`
	AgeSeconds   int    `json:"ageSeconds,omitempty"`
	Detached     bool   `json:"detached,omitempty"`
	// ExecutorState is "" when the stream holds no event for the task.
	ExecutorState string `json:"executorState,omitempty"`
	Final         bool   `json:"final,omitempty"`
	// The fold's terminal, when Final: whose word it is ("executor" for a
	// terminal on the task's events subject, "gateway" for the
	// supervisor's), the result artifact's text, and the terminal's status
	// message. A caller that finds the record still holding a finished
	// task -- the relay acked the terminal and lost the record write --
	// grades from these, and only when the source is the executor's.
	TerminalSource string `json:"terminalSource,omitempty"`
	Result         string `json:"result,omitempty"`
	Reason         string `json:"reason,omitempty"`
	// LastPost is the newest post entry on this conversation, if any: the
	// last thing the relay said, for a caller deciding what a stalled task
	// was doing.
	LastPost *InjectEntry `json:"lastPost,omitempty"`
	// Error is set when the gateway could not look -- no probe was offered
	// (a door run outside a gateway) or the stream read failed. The other
	// fields are then whatever was learned before the failure and must not
	// be read as "nothing there".
	Error string `json:"error,omitempty"`
}

// injectSubmission is one accepted POST, kept by its backend message id so
// a retry carrying the same id is answered the same way and starts nothing.
// done is closed once the original turn has answered; a duplicate that
// arrives while the original is still in flight waits on it.
type injectSubmission struct {
	conversation string
	done         chan struct{}
	completed    bool
	taskID       string
	note         string
	refusal      string
}

// injectIDLog is an append-only log of task ids that a waiting request
// indexes by absolute position: it records the counts before its message is
// routed and then asks what appeared after them. Bounded like everything
// else on this door, and the total is kept because the slice is not the
// history -- without it, an eviction would shift every index and a waiter
// would be handed the wrong task's id.
type injectIDLog struct {
	ids []string
	// total is how many ids have ever been added, evictions included.
	total int
}

func (l *injectIDLog) add(id string) {
	l.ids = append(l.ids, id)
	l.total++
	if len(l.ids) > injectMaxEntries {
		l.ids = l.ids[len(l.ids)-injectMaxEntries:]
	}
}

// at answers what a waiter sitting at position prior should be told: the id
// that landed there, whether the log has not reached it yet, and whether it
// has been evicted past -- which is not "nothing happened" and must not be
// answered as though it were.
func (l *injectIDLog) at(prior int) (id string, evicted bool) {
	if l.total <= prior {
		return "", false
	}
	idx := prior - (l.total - len(l.ids))
	if idx < 0 {
		return "", true
	}
	return l.ids[idx], false
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
	// and accepted the ones whose submission then reached the bus, which is
	// what POST /inject waits for. Two logs rather than one: a task is
	// announced before the publish, and a publish that fails leaves an id
	// that never reached an executor (TaskObserver.TaskAccepted).
	started  injectIDLog
	accepted injectIDLog
	// publishFailed records the tasks the gateway declared a terminal for
	// without ever reaching the bus -- the other end of the wait above.
	publishFailed injectIDLog
	// drops counts the messages the gateway dropped here for an
	// unverifiable sender. A counter rather than a transcript entry: the
	// transcript is what the conversation received, and a drop the gateway
	// chose not to post a second notice for is not something it received.
	drops int
	// turns counts the inbound turns that have finished here. The inbox
	// worker runs a conversation's turns one at a time, so a waiter that
	// read this before handing its message over knows its own turn is over
	// when the count moves.
	turns int
	// requester is the author of the last message injected here, which is
	// the whole membership of a synthetic conversation. Roster returns it.
	requester string
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
	token  string
	// firstEventGrace is the gateway's, reported to callers and never
	// enforced here; see injectResponse.FirstEventGraceSeconds.
	firstEventGrace time.Duration
	log             *slog.Logger

	mu            sync.Mutex
	conversations map[string]*injectConversation
	// order is the conversation keys in first-seen order, for the eviction
	// the map cannot do on its own.
	order []string
	// directOf maps an author to the conversation they last spoke on, which
	// is what OpenDirect answers with. Bounded by the conversation cap: an
	// entry is dropped when its conversation is evicted.
	directOf map[string]string
	// submissions dedupes accepted POSTs by backend message id, and
	// submissionOrder is its eviction queue, bounded at injectSeenCap.
	submissions     map[string]*injectSubmission
	submissionOrder []string
	nextID          int
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
	// probe is the gateway's ConversationProbe, handed over in New through
	// ProbeSink. Nil on a door run outside a gateway, where the read route
	// reports that it could not look.
	probe ConversationProbe

	// listener, when set, is an already-bound listener Run serves on instead
	// of binding listen. Test injection only; the deployed adapter always
	// binds its configured address.
	listener net.Listener
}

// NewInjectAdapter builds the door for a listen address (host:port, or :port
// for every interface), the bearer token every request must carry, and the
// gateway's first-event grace to report to callers.
//
// The token is required here as well as in FromEnv, because this constructor
// is also what a test and an embedder reach: a door that could be built
// without one would make "unauthenticated" a thing a caller can choose.
func NewInjectAdapter(listen, token string, firstEventGrace time.Duration, log *slog.Logger) (*InjectAdapter, error) {
	if strings.TrimSpace(listen) == "" {
		return nil, fmt.Errorf("the inject door needs a listen address")
	}
	if strings.TrimSpace(token) == "" {
		return nil, fmt.Errorf("the inject door needs a bearer token: it authenticates every request, and the NetworkPolicy in front of it does not govern the port-forward path its caller uses")
	}
	if log == nil {
		log = slog.Default()
	}
	return &InjectAdapter{
		listen:          listen,
		token:           token,
		firstEventGrace: firstEventGrace,
		log:             log,
		conversations:   map[string]*injectConversation{},
		directOf:        map[string]string{},
		submissions:     map[string]*injectSubmission{},
		notify:          make(chan struct{}),
	}, nil
}

// authorized reports whether one request carries the door's bearer token,
// answering 401 itself when it does not.
//
// Constant-time on the credential: the door is reachable by anything that
// reaches the listener, and a byte-at-a-time comparison there is a token
// oracle for a caller who can time it. The scheme is matched
// case-insensitively, as RFC 7235 requires.
func (a *InjectAdapter) authorized(w http.ResponseWriter, r *http.Request) bool {
	header := r.Header.Get(authorizationHeader)
	if len(header) > len(bearerScheme) && strings.EqualFold(header[:len(bearerScheme)], bearerScheme) {
		presented := strings.TrimSpace(header[len(bearerScheme):])
		if subtle.ConstantTimeCompare([]byte(presented), []byte(a.token)) == 1 {
			return true
		}
	}
	// No detail: the refusal says a token is required, never whether one was
	// presented or how it was wrong.
	w.Header().Set("WWW-Authenticate", "Bearer")
	injectError(w, http.StatusUnauthorized, "the inject door requires a bearer token")
	return false
}

// SetProbe receives the gateway's ConversationProbe (ProbeSink). Called once
// from New, before Run; the read route reads it without a lock for the same
// reason handler is read under one -- it is set before any request can
// arrive and never changes after.
func (a *InjectAdapter) SetProbe(probe ConversationProbe) {
	a.probe = probe
}

// Run serves the endpoints until ctx is done.
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
	a.log.Warn("the inject door is armed: a bearer-token holder that reaches this listener can "+
		"submit tasks as a mapped eval principal and read every reply on every conversation. "+
		"Dev and eval installs only.",
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

// Roster is the requester alone, complete. A synthetic conversation has
// exactly one participant -- the author who injected into it -- and
// reporting it complete is the truth rather than a degradation: there is no
// membership API behind this door to be incomplete about, and no second
// member for the audience snapshot to be missing.
func (a *InjectAdapter) Roster(conversation string) ([]string, bool, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[conversation]
	if !ok || conv.requester == "" {
		// A conversation nothing has been injected into yet. Empty and
		// complete: handleInbound adds the requester itself, so the audience
		// snapshot is the same either way.
		return nil, true, nil
	}
	return []string{conv.requester}, true, nil
}

// OpenDirect is the DM-switch primitive, and on this door it returns the
// conversation the user is already in. Every synthetic conversation has one
// participant, so the direct conversation with that participant is the
// conversation -- there is no second surface to switch to, and minting one
// would strand a reply on a key its caller never polls.
//
// A user the door has not seen gets the key their first message would land
// on, so the primitive still answers rather than failing.
func (a *InjectAdapter) OpenDirect(userID string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if conversation, ok := a.directOf[userID]; ok {
		return conversation, nil
	}
	return injectDMKeyPrefix + userID, nil
}

// TaskStarted records the task handleInbound minted for a conversation. See
// TaskObserver for why the gateway tells the adapter rather than the adapter
// reading it out of chat text.
func (a *InjectAdapter) TaskStarted(conversation, taskID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	conv.started.add(taskID)
	a.appendLocked(conv, InjectEntry{Kind: InjectEntryTask, TaskID: taskID})
}

// TaskTerminal records a task's terminal state, which is what a reader
// awaits. It lands AFTER the relay has posted the deliverable (relayTerminal
// posts, then edits the rolling line, then announces), so a caller that sees
// the terminal has already seen the answer.
func (a *InjectAdapter) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
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
	if source == TerminalFromGateway {
		// The gateway's own word about a task that never reached the bus,
		// which is startTask's publish failure and nothing else on this
		// path: the relay reports executors, and the heal reports
		// TerminalNeverStarted. A POST still waiting on this task is
		// answered with the publish-failed refusal.
		conv.publishFailed.add(taskID)
	}
	a.appendLocked(conv, InjectEntry{
		Kind: InjectEntryTerminal, TaskID: taskID, State: string(state), Source: string(source),
		Reason: truncateRunes(reason, injectMaxEntryBytes),
	})
}

// TaskAccepted records that a task's submission reached the bus, which is
// what POST /inject answers with a task id. See TaskObserver.TaskAccepted for
// why the start alone is not that answer.
func (a *InjectAdapter) TaskAccepted(conversation, taskID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	conv.accepted.add(taskID)
	// No transcript entry: the placeholder the relay posted is what the
	// conversation received, and this is the door's own bookkeeping. The
	// wake-up is what a waiting POST needs.
	close(a.notify)
	a.notify = make(chan struct{})
}

// MessageDropped records that the gateway dropped a message here for a sender
// it could not verify. See InboundObserver: the notice is posted once per
// sender, so on a second drop this counter is the only thing that moves.
func (a *InjectAdapter) MessageDropped(conversation, authorID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	conv.drops++
	a.log.Warn("inject: the gateway dropped a message from an author its principal map does not carry",
		"conversation", conversation, "author", authorID)
	close(a.notify)
	a.notify = make(chan struct{})
}

// TurnFinished records that a turn on this conversation has ended, which is
// what tells a waiting POST that what it can see is all there is going to be.
// See InboundObserver.
func (a *InjectAdapter) TurnFinished(conversation string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(conversation)
	conv.turns++
	close(a.notify)
	a.notify = make(chan struct{})
}

// lastPost is the newest post entry on a conversation, or nil.
func (a *InjectAdapter) lastPost(key string) *InjectEntry {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return nil
	}
	for i := len(conv.entries) - 1; i >= 0; i-- {
		if conv.entries[i].Kind == InjectEntryPost {
			entry := conv.entries[i]
			return &entry
		}
	}
	return nil
}

// claimSubmission records a POST's message id before its turn runs. It
// returns the existing submission and true when the id has been seen, so
// the caller answers from it instead of routing; otherwise a fresh one the
// caller completes with completeSubmission once the turn has answered.
// Oldest-first eviction at injectSeenCap: a retry arrives within seconds of
// its original, never thousands of accepted messages later.
func (a *InjectAdapter) claimSubmission(messageID, conversation string) (*injectSubmission, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if sub, ok := a.submissions[messageID]; ok {
		return sub, true
	}
	sub := &injectSubmission{conversation: conversation, done: make(chan struct{})}
	a.submissions[messageID] = sub
	a.submissionOrder = append(a.submissionOrder, messageID)
	if len(a.submissionOrder) > injectSeenCap {
		delete(a.submissions, a.submissionOrder[0])
		a.submissionOrder = a.submissionOrder[1:]
	}
	return sub, false
}

// completeSubmission publishes what a claimed POST's turn produced to any
// duplicate waiting on it.
func (a *InjectAdapter) completeSubmission(sub *injectSubmission, taskID, note, refusal string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if sub.completed {
		return
	}
	sub.completed = true
	sub.taskID = taskID
	sub.note = note
	sub.refusal = refusal
	close(sub.done)
}

// answerDuplicate is the reply to a POST whose message id was already
// accepted: the original's task id and note once its turn has answered,
// bounded by the same wait the original had. Nothing is routed.
func (a *InjectAdapter) answerDuplicate(ctx context.Context, w http.ResponseWriter, key, messageID string, sub *injectSubmission) {
	if sub.conversation != key {
		injectError(w, http.StatusConflict, fmt.Sprintf(
			"message id %q was already accepted on conversation %q; a backend message id names one message on one conversation",
			messageID, sub.conversation))
		return
	}
	timer := time.NewTimer(injectSubmitWait)
	defer timer.Stop()
	select {
	case <-sub.done:
	case <-timer.C:
	case <-ctx.Done():
		return
	}
	// Read the first submission's answer under the lock and prefer it
	// whichever arm fired: the timer and the completion can be ready
	// together, and answering "the first POST has not been answered" beside
	// the task id it just answered with would contradict the Refusal
	// contract and tell the caller nothing was started when something was.
	a.mu.Lock()
	completed, taskID, note, refusal := sub.completed, sub.taskID, sub.note, sub.refusal
	a.mu.Unlock()
	if !completed {
		taskID, refusal = "", injectRefusalNoAnswer
		note = fmt.Sprintf("the first POST with this message id has not been answered within %s; nothing was started for this one", injectSubmitWait)
	}
	entries, _, _ := a.snapshot(key, 0, "")
	a.log.Info("inject: duplicate message id answered from the first submission",
		"conversation", key, "messageId", messageID, "taskId", taskID)
	writeJSON(w, http.StatusOK, injectResponse{
		Conversation:           key,
		TaskID:                 taskID,
		Accepted:               taskID != "",
		MessageID:              messageID,
		Entries:                entries,
		Note:                   note,
		Refusal:                refusal,
		FirstEventGraceSeconds: int(a.firstEventGrace / time.Second),
		Deduplicated:           true,
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
		if evicted := a.conversations[oldest]; evicted != nil && evicted.requester != "" {
			// Only if it still points here: the author may have spoken on a
			// newer conversation since, and that mapping is the live one.
			if a.directOf[evicted.requester] == oldest {
				delete(a.directOf, evicted.requester)
			}
		}
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
// given count of prior starts, acceptedSince the first whose submission
// reached the bus, and publishFailedSince the first that could not. Each also
// reports whether the position has been evicted, which is a different answer
// from "nothing yet" -- see injectIDLog.at.
func (a *InjectAdapter) startedSince(key string, prior int) (string, bool) {
	return a.sinceLocked(key, prior, func(conv *injectConversation) *injectIDLog { return &conv.started })
}

func (a *InjectAdapter) acceptedSince(key string, prior int) (string, bool) {
	return a.sinceLocked(key, prior, func(conv *injectConversation) *injectIDLog { return &conv.accepted })
}

func (a *InjectAdapter) publishFailedSince(key string, prior int) (string, bool) {
	return a.sinceLocked(key, prior, func(conv *injectConversation) *injectIDLog { return &conv.publishFailed })
}

func (a *InjectAdapter) sinceLocked(key string, prior int, pick func(*injectConversation) *injectIDLog) (string, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return "", false
	}
	return pick(conv).at(prior)
}

// injectCounts is what a waiting request records before handing a message
// over, so it can tell this turn's work from the last one's.
type injectCounts struct {
	starts        int
	accepted      int
	publishFailed int
	entries       int
	drops         int
	turns         int
}

// counts is the conversation's state as a waiting request sees it.
func (a *InjectAdapter) counts(key string) injectCounts {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv, ok := a.conversations[key]
	if !ok {
		return injectCounts{}
	}
	return injectCounts{
		starts:        conv.started.total,
		accepted:      conv.accepted.total,
		publishFailed: conv.publishFailed.total,
		entries:       conv.nextSeq - 1,
		drops:         conv.drops,
		turns:         conv.turns,
	}
}

// waitCh returns the channel closed on the next change to any conversation.
func (a *InjectAdapter) waitCh() <-chan struct{} {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.notify
}

// handleInject is POST /inject: one inbound chat message.
func (a *InjectAdapter) handleInject(w http.ResponseWriter, r *http.Request) {
	// First, before the method and before the body: an unauthenticated
	// caller learns nothing from this door, not which methods it takes and
	// not which bodies it parses.
	if !a.authorized(w, r) {
		return
	}
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
	// A caller-supplied id is the dedupe key; a minted one cannot repeat, so
	// there is nothing to dedupe on and nothing is recorded.
	var sub *injectSubmission
	// The wait for the turn's answer. A claimed POST waits on a context the
	// caller's disconnect does not cancel: the turn runs regardless once
	// handed over, and the whole point of the claim is that a retry arriving
	// after a dropped connection is answered with the task that turn
	// started -- which it cannot be if the original gave up on the turn the
	// moment its client went away. injectSubmitWait still bounds it.
	turnCtx := r.Context()
	if messageID == "" {
		messageID = injectInboundIDPrefix + randHex(messageIDHexWidth)
	} else {
		var seen bool
		sub, seen = a.claimSubmission(messageID, key)
		if seen {
			a.answerDuplicate(r.Context(), w, key, messageID, sub)
			return
		}
		turnCtx = context.WithoutCancel(r.Context())
		// Completed on every exit, whatever happens below (completeSubmission
		// is idempotent, so the normal completion wins and this is a no-op):
		// a submission whose done channel never closes would hold every
		// duplicate for the full wait and then answer it with nothing.
		defer a.completeSubmission(sub, "",
			"the first POST with this message id aborted before its turn answered", injectRefusalNoAnswer)
	}
	// Read the counters BEFORE handing the message over, so the wait below
	// cannot mistake a previous turn's task or reply for this one's.
	prior := a.counts(key)

	// Recorded before the turn, so Roster answers for this conversation even
	// if the gateway reads it inside handleInbound.
	a.noteRequester(key, req.Author)

	handler(InboundMessage{
		Conversation: key,
		Kind:         injectConversationKind,
		AuthorID:     req.Author,
		MessageID:    messageID,
		Text:         req.Text,
		// Stamped rather than inferred: this door can be armed beside a real
		// backend, and the authority block has to name the door the message
		// came through rather than the backend the process was configured
		// with.
		Backend: injectBackend,
	})

	taskID, note, refusal := a.awaitTurn(turnCtx, key, prior)
	if sub != nil {
		a.completeSubmission(sub, taskID, note, refusal)
	}
	if refusal != "" {
		a.log.Info("inject: the door started nothing for a POST",
			"conversation", key, "messageId", messageID, "refusal", refusal, "note", note)
	}
	entries, _, _ := a.snapshot(key, prior.entries, "")
	writeJSON(w, http.StatusOK, injectResponse{
		Conversation:           key,
		TaskID:                 taskID,
		Accepted:               taskID != "",
		MessageID:              messageID,
		Entries:                entries,
		Note:                   note,
		Refusal:                refusal,
		FirstEventGraceSeconds: int(a.firstEventGrace / time.Second),
	})
}

// noteRequester records who is talking on a conversation, for Roster and
// OpenDirect.
func (a *InjectAdapter) noteRequester(key, author string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	conv := a.conversationLocked(key)
	conv.requester = author
	a.directOf[author] = key
}

// handleCancel is POST /conversations/<key>/cancel: stop whatever is running
// on the conversation.
//
// A route rather than the text path. The gateway reaches cancel from a
// message whose whole text is "stop" (text.go), which is the right
// affordance for a human and the wrong one for a program: it makes a control
// action depend on a phrase list, it cannot be told apart from a user asking
// the agent to stop something in the world, and a caller that means "cancel"
// would be sending an ask that the gateway has to classify back into an
// intent. The message this delivers carries IntentCancel, and handleInbound
// takes the same branch it takes for the text -- so what lands on the bus is
// the same kind: cancel envelope, with nothing forked between the two paths.
func (a *InjectAdapter) handleCancel(w http.ResponseWriter, r *http.Request, key string) {
	if r.Method != http.MethodPost {
		injectError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	var req cancelRequest
	body := http.MaxBytesReader(w, r.Body, injectMaxBodyBytes)
	if err := json.NewDecoder(body).Decode(&req); err != nil {
		injectError(w, http.StatusBadRequest, "malformed body: "+err.Error())
		return
	}
	if strings.TrimSpace(req.Author) == "" {
		injectError(w, http.StatusBadRequest,
			"author is required: a cancel is verified like any other message on the conversation")
		return
	}

	a.handlerMu.RLock()
	handler := a.handler
	a.handlerMu.RUnlock()
	if handler == nil {
		injectError(w, http.StatusServiceUnavailable, "the gateway is not running the inject door yet")
		return
	}

	messageID := req.MessageID
	if messageID == "" {
		messageID = injectInboundIDPrefix + randHex(messageIDHexWidth)
	}
	prior := a.counts(key)
	a.noteRequester(key, req.Author)
	handler(InboundMessage{
		Conversation: key,
		Kind:         injectConversationKind,
		AuthorID:     req.Author,
		MessageID:    messageID,
		Backend:      injectBackend,
		Intent:       IntentCancel,
		TaskID:       strings.TrimSpace(req.TaskID),
	})

	// A cancel always answers the conversation with something -- the cancel
	// acknowledgement, the nothing-is-running reply, or the never-started
	// notice when the heal fires on the way past -- so waiting for one entry
	// is waiting for the turn, not for the executor.
	note := a.awaitEntry(r.Context(), key, prior)
	entries, _, _ := a.snapshot(key, prior.entries, "")
	writeJSON(w, http.StatusOK, injectResponse{
		Conversation:           key,
		MessageID:              messageID,
		Entries:                entries,
		Note:                   note,
		FirstEventGraceSeconds: int(a.firstEventGrace / time.Second),
	})
}

// awaitEntry waits for one new entry on a conversation, returning a note when
// the turn produced none inside the submit bound.
//
// A drop ends it too, for the reason awaitTurn gives: the notice for an
// unverifiable sender is posted once per sender, so the second cancel from an
// author already told they are unknown produces no entry at all and would
// otherwise hold the connection for the whole bound.
func (a *InjectAdapter) awaitEntry(ctx context.Context, key string, prior injectCounts) string {
	deadline := time.Now().Add(injectSubmitWait)
	for {
		now := a.counts(key)
		if now.entries > prior.entries {
			return ""
		}
		if now.drops > prior.drops {
			return "the gateway dropped this cancel: its principal map does not carry the author"
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return fmt.Sprintf("the gateway posted no reply to this turn within %s", injectSubmitWait)
		}
		woken := a.waitCh()
		timer := time.NewTimer(min(remaining, injectPollInterval))
		select {
		case <-ctx.Done():
			timer.Stop()
			return "the caller went away before the gateway answered"
		case <-woken:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// awaitTurn waits for the gateway to say what it did with a message, and
// returns the task id it started, or a note and one of the refusal codes.
//
// The answer is a task whose submission reached the bus, and not merely one
// the gateway minted. startTask announces the id, posts the placeholder,
// writes the record and then publishes; if the publish fails it edits the
// placeholder to say so, clears the active task and returns, and nothing is
// on any subject. Answering at the announcement would hand a caller an id
// for a task no executor can ever see, and it would wait out its whole
// budget for a terminal that cannot come. So the wait ends on the accept
// (TaskObserver.TaskAccepted) or on the gateway's own terminal for the same
// task, which is the publish-failed refusal.
//
// The other endings are read off the end of the turn rather than guessed
// from the transcript (InboundObserver.TurnFinished). A post is not proof the
// turn started nothing: handleInbound posts BEFORE startTask on the heal
// paths (a stale task's status card, the never-started notice), so a door
// concluding "answered without a task" from entries alone can answer a caller
// that nothing started while the turn goes on to mint a task -- which then
// runs with nobody watching it and nobody to cancel it. Once the turn is
// over, what the door can see is all there is, and the refusal is a fact: a
// drop, a reply with no task, or nothing at all.
//
// The drop is a signal rather than a post for a related reason: the gateway's
// notice is posted once per sender, so the second drop from the same author
// posts nothing at all.
func (a *InjectAdapter) awaitTurn(ctx context.Context, key string, prior injectCounts) (string, string, string) {
	deadline := time.Now().Add(injectSubmitWait)
	for {
		accepted, acceptedGone := a.acceptedSince(key, prior.accepted)
		if accepted != "" {
			return accepted, "", ""
		}
		failed, failedGone := a.publishFailedSince(key, prior.publishFailed)
		if failed != "" {
			return "", fmt.Sprintf("the gateway minted task %s and could not put it on the bus; "+
				"it has released the conversation and nothing is running", failed), injectRefusalPublishFailed
		}
		if acceptedGone || failedGone {
			// More than this door retains has happened on the conversation
			// since the message was handed over, so the answer to this POST
			// has scrolled off. Said rather than guessed: the id at the
			// nearest position it still holds would be another task's.
			return "", fmt.Sprintf("more than %d tasks have started on this conversation since "+
				"this message was routed, so what became of it is no longer retained",
				injectMaxEntries), injectRefusalNoAnswer
		}
		now := a.counts(key)
		if now.turns > prior.turns {
			// The turn is over and no task of it reached the bus. Which
			// refusal it is follows from what the turn did, in the order the
			// gateway could have done them.
			switch {
			case now.drops > prior.drops:
				return "", "the gateway dropped this message: its principal map does not carry the " +
					"author, so nothing was started (the notice naming the author is posted once per " +
					"sender, so it may not be in the entries)", injectRefusalUnverifiedAuthor
			case now.starts > prior.starts:
				// Announced, and the turn ended without the publish either
				// succeeding or failing: the gateway went away mid-turn.
				started, _ := a.startedSince(key, prior.starts)
				return "", fmt.Sprintf("the gateway minted task %s and the turn ended without putting "+
					"it on the bus or saying it could not", started), injectRefusalNoAnswer
			case now.entries > prior.entries:
				return "", "the gateway answered this turn without starting a task (a steer, a status " +
					"answer, or a stop); the reply is in entries", injectRefusalNoTask
			default:
				return "", "the turn ended with the gateway neither starting a task nor posting a reply",
					injectRefusalNoAnswer
			}
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			// The turn never ended, which is the gateway itself being stuck:
			// handleInbound runs under its own turn timeout, below this
			// bound, so reaching here means it has not returned at all.
			if started, _ := a.startedSince(key, prior.starts); started != "" {
				return "", fmt.Sprintf("the gateway minted task %s and within %s neither put it on the "+
					"bus nor said it could not", started, injectSubmitWait), injectRefusalNoAnswer
			}
			return "", fmt.Sprintf("the gateway did not finish this turn within %s",
				injectSubmitWait), injectRefusalNoAnswer
		}
		woken := a.waitCh()
		timer := time.NewTimer(min(remaining, injectPollInterval))
		select {
		case <-ctx.Done():
			timer.Stop()
			return "", "the caller went away before the gateway answered", injectRefusalNoAnswer
		case <-woken:
		case <-timer.C:
		}
		timer.Stop()
	}
}

// handleConversation is GET /conversations/<key>: what the relay has posted,
// and on request what the gateway's session record is doing.
//
// Query parameters: after returns only entries past that sequence, task names
// the task whose terminal state the reply should carry, and wait is how many
// seconds to block for something new (or for that task's terminal) before
// answering. The three together are the whole of "await terminal of task id
// X": poll with the last sequence and the task id, and each reply carries
// both the new chat lines and the answer to whether it is over.
//
// probe=1 adds the thing a program needs and the transcript cannot give it:
// the gateway's session record for the conversation and the state of its
// task's stream, read with nothing changed (ConversationProbe). It is how a
// caller learns, without sending a message that would itself become a turn,
// whether any executor has touched its task, whether the task has sat queued
// (submitted) or run (working), and how old it is against the gateway's
// grace -- and classifies for itself. The probe runs before the wait, so a
// caller that reads a terminal on the stream can stop waiting on the relay.
func (a *InjectAdapter) handleConversation(w http.ResponseWriter, r *http.Request) {
	if !a.authorized(w, r) {
		return
	}
	raw := strings.TrimPrefix(r.URL.Path, conversationsPath)
	// The cancel route lives under the conversation it acts on, so one
	// pattern serves both and the key is parsed once.
	cancel := strings.HasSuffix(raw, cancelSuffix)
	if cancel {
		raw = strings.TrimSuffix(raw, cancelSuffix)
	}
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
	if cancel {
		a.handleCancel(w, r, key)
		return
	}
	if r.Method != http.MethodGet {
		injectError(w, http.StatusMethodNotAllowed, "GET only")
		return
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
	probeAsked, err := intParam(r, probeParam)
	if err != nil {
		injectError(w, http.StatusBadRequest, err.Error())
		return
	}
	var probed *probeReport
	if probeAsked != 0 {
		probed = a.runProbe(r.Context(), key)
		probed.LastPost = a.lastPost(key)
	}

	deadline := time.Now().Add(wait)
	for {
		entries, lastSeq, terminal := a.snapshot(key, after, taskID)
		if len(entries) > 0 || terminal != "" || !time.Now().Before(deadline) {
			writeJSON(w, http.StatusOK, conversationResponse{
				Conversation: key,
				Entries:      entries,
				LastSeq:      lastSeq,
				Terminal:     terminal,
				Probe:        probed,
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

// runProbe asks the gateway about a conversation and puts the answer on the
// wire. Never a failure status: the transcript half of the reply is good
// whatever the probe found, and a caller reads "could not look" out of the
// report's error rather than out of a 5xx it would retry the whole poll for.
func (a *InjectAdapter) runProbe(ctx context.Context, key string) *probeReport {
	if a.probe == nil {
		return &probeReport{Error: "the gateway offered this door no probe"}
	}
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	state, err := a.probe(ctx, key)
	report := &probeReport{
		Backend:       state.Backend,
		InjectOnly:    state.InjectOnly,
		GraceSeconds:  int(state.Grace / time.Second),
		Active:        state.Active,
		TaskID:        state.TaskID,
		AgeSeconds:    int(state.Age / time.Second),
		Detached:      state.Detached,
		ExecutorState: string(state.ExecutorState),
		Final:         state.Final,
		// Bounded like an entry: a result is an agent's report and rides
		// the same in-memory transcript budget.
		TerminalSource: string(state.TerminalSource),
		Result:         truncateRunes(state.Result, injectMaxEntryBytes),
		Reason:         truncateRunes(state.Reason, injectMaxEntryBytes),
	}
	if !state.SubmittedAt.IsZero() {
		report.SubmittedAt = state.SubmittedAt.UTC().Format(time.RFC3339Nano)
	}
	if err != nil {
		a.log.Warn("the read route could not probe a conversation", "conversation", key, "err", err)
		report.Error = err.Error()
	}
	return report
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
