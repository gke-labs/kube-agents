package mcp

import (
	"cmp"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// The console door (a2a/gateway/console.go): a frame published on
// chat.console.<token>.in becomes one turn of the conversation
// console:<token>; the gateway's notices come back on chat.console.<token>.out
// and the answer streams on TASKS. Only the `console` user may publish there,
// and this server is one.
const (
	consoleInSubject      = "chat.console.%s.in"
	consoleOutSubject     = "chat.console.%s.out"
	consoleConversationOf = consoleConversationPrefix + "%s"
	// consoleConversationPrefix is the console door's conversation key prefix.
	consoleConversationPrefix = "console:"
	// submissionSubjects is every task's .in subject, which the console grant
	// lets the server watch (a2a.>): submissions, follow-ups and cancels.
	submissionSubjects = "a2a.tasks.*.*." + lib.TaskClassIn
	// submissionSubjectParts is a2a.tasks.<addressee>.<task>.in; the
	// addressee is field 2.
	submissionSubjectParts = 5
	submissionAddressee    = 2

	// consoleTextCap is the gateway's own cap on one frame's text; a longer
	// one would be dropped there with a notice, so it is refused here first.
	consoleTextCap = 16 * 1024

	conversationTokenPrefix = "mcp"
	conversationTokenBytes  = 12
	messageIDBytes          = 8
	// maxConversations bounds the alias table; past it the least recently
	// used alias is forgotten.
	maxConversations = 1024
	// maxAliasRunes bounds a conversation's name, so the alias table's
	// memory is bounded by its count too: a request body may be megabytes.
	maxAliasRunes = 128

	// submissionWait bounds the wait for the gateway to publish the turn.
	// noticeGrace is how long notices alone are waited on before they are
	// taken as the whole answer (a status question or a refused turn gets
	// notices and no task); the gateway can post one and then do a little
	// work before a submission, so the grace is generous, and a late
	// submission cannot be mistaken for a later turn's, whose text differs.
	submissionWait = 30 * time.Second
	noticeGrace    = 10 * time.Second
	// noticeSettle is how long, once the turn's own submission has matched,
	// notices are still read before the turn is returned. The gateway posts
	// its acknowledgement unflushed on the console connection and the
	// submission synchronously through JetStream on another, so the
	// acknowledgement can reach this server after the submission it
	// precedes; returned at the match, the turn would show without it. A
	// steered turn's acknowledgement is the second producer: the gateway
	// posts it after the follow-up is published and after a read of the
	// task's stream, so one that read takes longer than the window to post
	// is missed, as every acknowledgement was before the window.
	noticeSettle = 200 * time.Millisecond
	// maxFollowedTasks bounds the delegated tasks one wait follows.
	maxFollowedTasks = 8

	// DefaultConversation is the alias a call without one uses.
	DefaultConversation = "default"

	firstStepWaitWindow   = 6 * time.Second
	nextStepWaitWindow    = 5 * time.Second
	minStepBatchWait      = 600 * time.Millisecond
	stepDebounceWait      = 350 * time.Millisecond
	stepActivityWaitMax   = 1100 * time.Millisecond
	stepPollWait          = 200 * time.Millisecond
	stepPollIdleSleep     = 50 * time.Millisecond
	delegationSettleGrace = 400 * time.Millisecond
	// delegationFollowGrace is how long a streamed poll waits for the first
	// task delegated from a finished, delegating root before it takes the
	// root's own answer. The gateway's relay mints that task and can trail
	// the root's terminal by up to its relayLagGrace (10 minutes by
	// default), and the non-streamed wait (waitForChain) waits its whole
	// wait for it; each poll inside the grace reports the turn still
	// running.
	delegationFollowGrace = 10 * time.Minute
	reportGatherBudget    = 5 * time.Second
	stepWatchIdleRelease  = 2 * time.Second
)

// ConsoleInFrame is what the console door reads (gateway.ConsoleInFrame).
type ConsoleInFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
}

// ConsoleOutFrame is a frame the gateway posts to a console conversation
// (gateway.ConsoleOutFrame). Kind is unset on its notices about a turn; a
// frame with a kind reports something else (a task's terminal result, a
// proactive notification) and is not a notice about the turn being sent.
// A result names its task, addressee and state; a notification names, in
// ReplyTo, the task whose turn filed the work it reports on, and an edit
// re-sends a notification's message id with Edit set. A chain's completed
// result sets CardsPending when the turn handed work to kanban cards (a
// gateway that predates the flag never sets it).
type ConsoleOutFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
	Edit      bool   `json:"edit,omitempty"`
	Kind      string `json:"kind,omitempty"`
	State     string `json:"state,omitempty"`
	Addressee string `json:"addressee,omitempty"`
	TaskID    string `json:"taskId,omitempty"`
	ReplyTo   string `json:"replyTo,omitempty"`
	// CardsPending is set on a result whose chain handed work to cards.
	CardsPending bool `json:"cardsPending,omitempty"`
}

// submissionAuthority is the part of a submission's authority block that says
// which conversation opened it, and which task it was delegated from.
type submissionAuthority struct {
	Audience struct {
		Conversation string `json:"conversation"`
	} `json:"audience"`
	Via *struct {
		TaskID string `json:"taskId"`
	} `json:"via,omitempty"`
}

// Conversations maps the aliases callers name to console tokens, per call:
// there is no server-wide "active" conversation for one caller to change under
// another. It also holds one send per conversation at a time, which, with the
// text, is what tells a turn's own submission from the conversation's next.
// Callers share one bearer token and so one alias namespace: two callers that
// use the same name share a conversation.
//
// Neither table can be filled for good: an alias table at maxConversations
// forgets its least recently used alias (whose conversation stays reachable
// by its token), and a send lock exists only while a send holds or waits on
// it.
type Conversations struct {
	mu      sync.Mutex
	byAlias map[string]*aliasEntry
	sending map[string]*sendLock
	// running is each conversation's turn task last seen unfinished, which
	// its next turn waits on; at most maxConversations are kept.
	running map[string]TaskRef
	steps   map[string]*taskStepState
	clock   uint64
	// now is the wall clock an alias's last use is stamped with; tests set it.
	now func() time.Time
	// events, when set, listens to every conversation resolved here, and
	// is told when the table forgets one.
	events *Events
}

// aliasEntry is one alias: its token, the description manage_session's new
// gave it, and when it was last used, by counter (which orders uses exactly)
// and by wall clock (which a person can read).
type aliasEntry struct {
	token       string
	description string
	used        uint64
	lastUsed    time.Time
}

// ConversationInfo is one alias as manage_session lists it.
type ConversationInfo struct {
	Alias       string
	Token       string
	Description string
	LastUsed    time.Time
}

type sendLock struct {
	ch   chan struct{}
	refs int
}

// ErrConversationBusy is a send that gave up waiting for the conversation's
// running turn to end: busy, not broken. A turn sent while another runs would
// be steered into the running task, which may refuse it and still answer
// with the earlier turn's answer, so turns on one conversation are sent one
// at a time.
var ErrConversationBusy = errors.New("conversation busy: a turn is still running")

// NewConversations returns an empty alias table.
func NewConversations() *Conversations {
	return &Conversations{
		byAlias: map[string]*aliasEntry{},
		sending: map[string]*sendLock{},
		running: map[string]TaskRef{},
		steps:   map[string]*taskStepState{},
		now:     time.Now,
	}
}

// WithEvents has the table's conversations listened to by events.
func (c *Conversations) WithEvents(events *Events) *Conversations {
	c.events = events
	return c
}

// Resolve returns the console token for a call: token when the caller names
// one (to resume a conversation, after a restart of this server included),
// otherwise the alias's, minted on first use. The conversation is listened
// to from then on.
func (c *Conversations) Resolve(alias, token string) (string, error) {
	t, _, err := c.ResolveCall(alias, token)
	return t, err
}

// ResolveCall is Resolve, also reporting whether the call named its
// conversation by token, so its answer is labelled by the token rather than
// by a name it may not have.
func (c *Conversations) ResolveCall(alias, token string) (string, bool, error) {
	r, err := c.resolve(alias, token)
	if err != nil {
		return "", false, err
	}
	if err := c.listen(r.token, r.named, r.evicted); err != nil {
		// A name whose conversation is not listened to is not kept, as
		// Create does not keep one, so a retry is not refused as existing.
		if r.created != "" {
			c.mu.Lock()
			if e, ok := c.byAlias[r.created]; ok && e.token == r.token {
				delete(c.byAlias, r.created)
			}
			c.mu.Unlock()
		}
		return "", false, err
	}
	return r.token, r.byToken, nil
}

// listen tells events of a conversation resolved and of one the table
// evicted for it.
func (c *Conversations) listen(token string, named bool, evicted string) error {
	c.events.Unname(evicted)
	if _, err := c.events.Watch(token, named); err != nil {
		return fmt.Errorf("listen to the conversation's posts: %w", err)
	}
	return nil
}

// resolve is ResolveCall's table work: the token, whether the table holds it
// by a name, whether the call named it by token, and the token of an alias
// forgotten to make room.
func (c *Conversations) resolve(alias, token string) (resolved, error) {
	alias, token, err := nameOrToken(alias, token)
	if err != nil {
		return resolved{}, err
	}
	if token != "" {
		// A call by token is a use of the name that holds it, if any.
		c.mu.Lock()
		if a, ok := c.aliasOfTokenLocked(token); ok {
			c.touchLocked(c.byAlias[a])
		}
		c.mu.Unlock()
		return resolved{token: token, byToken: true}, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if e, ok := c.byAlias[alias]; ok {
		c.touchLocked(e)
		return resolved{token: e.token, named: true}, nil
	}
	t, evicted, err := c.addLocked(alias, "")
	if err != nil {
		return resolved{}, err
	}
	return resolved{token: t, named: true, created: alias, evicted: evicted}, nil
}

// resolved is what resolve found: the token, whether a name in the table
// holds it, whether the call named it by token, the name it created (if it
// did), and the token of an alias forgotten to make room.
type resolved struct {
	token   string
	named   bool
	byToken bool
	created string
	evicted string
}

// nameOrToken sorts a call's conversation into a name or a token, by the
// rules every tool shares: a conversation's token given as its name, as list
// shows it (console:<token>) or bare, is the token, never a new name spelled
// like one, whether or not this table still knows it (after a delete, an
// eviction or a restart, the token is what a caller falls back to); and
// console:<x> in a name's place is a token only when x is one. A token comes
// back without its prefix and checked; a name comes back checked, default
// when none was given.
func nameOrToken(alias, token string) (string, string, error) {
	if token == "" && (strings.HasPrefix(alias, consoleConversationPrefix) || isMintedToken(alias)) {
		if rest := strings.TrimPrefix(alias, consoleConversationPrefix); !isMintedToken(rest) {
			return "", "", fmt.Errorf("%s is neither a conversation name nor a conversation's token (list shows a token as console:<token>)", cell(alias))
		}
		alias, token = "", alias
	}
	if token != "" {
		// manage_session lists a conversation as console:<token>; either form
		// names it.
		token = strings.TrimPrefix(token, consoleConversationPrefix)
		if !lib.ValidSubjectToken(token) {
			return "", "", fmt.Errorf("%q is not a conversation token", token)
		}
		return "", token, nil
	}
	if alias == "" {
		alias = DefaultConversation
	}
	if err := checkAlias(alias); err != nil {
		return "", "", err
	}
	return alias, "", nil
}

// isMintedToken reports whether s is spelled as this server mints tokens.
func isMintedToken(s string) bool {
	rest, ok := strings.CutPrefix(s, conversationTokenPrefix)
	if !ok || len(rest) != 2*conversationTokenBytes {
		return false
	}
	_, err := hex.DecodeString(rest)
	return err == nil && strings.ToLower(rest) == rest
}

// aliasOfTokenLocked finds the alias whose conversation has token t.
func (c *Conversations) aliasOfTokenLocked(t string) (string, bool) {
	for a, e := range c.byAlias {
		if e.token == t {
			return a, true
		}
	}
	return "", false
}

// ErrConversationExists is Create's refusal of an alias already in use.
var ErrConversationExists = errors.New("conversation already exists")

// Create adds alias with a fresh token and description, and refuses an alias
// already in use rather than handing back its conversation: a caller asking
// for a new conversation must not find itself continuing someone else's.
func (c *Conversations) Create(alias, description string) (string, error) {
	if err := checkAlias(alias); err != nil {
		return "", err
	}
	c.mu.Lock()
	if _, ok := c.byAlias[alias]; ok {
		c.mu.Unlock()
		return "", fmt.Errorf("%w: %s", ErrConversationExists, cell(alias))
	}
	t, evicted, err := c.addLocked(alias, description)
	c.mu.Unlock()
	if err != nil {
		return "", err
	}
	if err := c.listen(t, true, evicted); err != nil {
		// A name whose conversation is not listened to is not created, so
		// that a retry with the same name is not refused as existing.
		c.mu.Lock()
		if e, ok := c.byAlias[alias]; ok && e.token == t {
			delete(c.byAlias, alias)
		}
		c.mu.Unlock()
		return "", err
	}
	return t, nil
}

// lookup is resolve for a reader: the token of a conversation the caller
// names by token, or by a name the table already holds, and whether the
// table holds it by a name. An unknown name is refused, not created.
func (c *Conversations) lookup(alias, token string) (string, bool, error) {
	alias, token, err := nameOrToken(alias, token)
	if err != nil {
		return "", false, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if token != "" {
		// A call by token is a use of the name that holds it, if any.
		if a, ok := c.aliasOfTokenLocked(token); ok {
			c.touchLocked(c.byAlias[a])
		}
		return token, false, nil
	}
	if e, ok := c.byAlias[alias]; ok {
		c.touchLocked(e)
		return e.token, true, nil
	}
	return "", false, fmt.Errorf("no conversation named %s; send a turn first or pass conversation_token", cell(alias))
}

// Forget drops the name holding token, or else alias (a name, or a token in
// a name's place), and returns the name it dropped, if any. The conversation
// itself is untouched: it stays reachable by its token.
func (c *Conversations) Forget(alias, token string) (string, bool) {
	c.mu.Lock()
	// A token wins over a name, as it does in ResolveCall.
	name := ""
	if token == "" {
		if _, ok := c.byAlias[alias]; ok {
			name = alias
		} else {
			// Named by its token in a name's place, as list shows it, or bare.
			token = alias
		}
	}
	if name == "" {
		a, found := c.aliasOfTokenLocked(strings.TrimPrefix(token, consoleConversationPrefix))
		if !found {
			c.mu.Unlock()
			return "", false
		}
		name = a
	}
	forgotten := c.byAlias[name].token
	delete(c.byAlias, name)
	c.mu.Unlock()
	c.events.Unname(forgotten)
	return name, true
}

// List returns the aliases, most recently used first.
func (c *Conversations) List() []ConversationInfo {
	c.mu.Lock()
	type row struct {
		info ConversationInfo
		used uint64
	}
	rows := make([]row, 0, len(c.byAlias))
	for a, e := range c.byAlias {
		rows = append(rows, row{ConversationInfo{Alias: a, Token: e.token, Description: e.description, LastUsed: e.lastUsed}, e.used})
	}
	c.mu.Unlock()
	slices.SortFunc(rows, func(x, y row) int { return cmp.Compare(y.used, x.used) })
	out := make([]ConversationInfo, len(rows))
	for i, r := range rows {
		out[i] = r.info
	}
	return out
}

func (c *Conversations) touchLocked(e *aliasEntry) {
	c.clock++
	e.used, e.lastUsed = c.clock, c.now()
}

// addLocked mints a token for a new alias, forgetting the least recently
// used alias first if the table is full, and returns the forgotten alias's
// token too.
func (c *Conversations) addLocked(alias, description string) (string, string, error) {
	t, err := mintConversationToken()
	if err != nil {
		return "", "", err
	}
	evicted := ""
	if len(c.byAlias) >= maxConversations {
		evicted = c.forgetOldestLocked()
	}
	e := &aliasEntry{token: t, description: description}
	c.touchLocked(e)
	c.byAlias[alias] = e
	return t, evicted, nil
}

func mintConversationToken() (string, error) {
	b := make([]byte, conversationTokenBytes)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return conversationTokenPrefix + hex.EncodeToString(b), nil
}

// checkAlias refuses a conversation name longer than maxAliasRunes.
//
// It also refuses what list could not show back as typed, so a name copied
// from list is the same name: a token's console: prefix, backticks and pipes,
// control and invisible (format) characters, spaces at either end, doubled
// or other than the plain one, and a token's own spelling.
func checkAlias(alias string) error {
	if n := utf8.RuneCountInString(alias); n > maxAliasRunes {
		return fmt.Errorf("conversation name is %d characters; at most %d", n, maxAliasRunes)
	}
	// Only new reaches these two: ResolveCall takes such a name as the
	// token it is before checking it.
	if strings.HasPrefix(alias, consoleConversationPrefix) || isMintedToken(alias) {
		return errors.New("that is a conversation's token, not a name: the conversation already exists, " +
			"and a name cannot be given to it here; use it by its token, or pick another name for a new conversation")
	}
	// list collapses every run of Unicode spaces to one ASCII space.
	if strings.Join(strings.Fields(alias), " ") != alias {
		return errors.New("a conversation name may not start or end with spaces, hold two in a row, or use a space other than the plain one")
	}
	for _, r := range alias {
		if r == '`' || r == '|' || unicode.IsControl(r) || unicode.Is(unicode.Cf, r) {
			return fmt.Errorf("a conversation name may not hold %q", r)
		}
	}
	return nil
}

func (c *Conversations) forgetOldestLocked() string {
	oldest, at := "", uint64(0)
	for a, e := range c.byAlias {
		if oldest == "" || e.used < at {
			oldest, at = a, e.used
		}
	}
	token := c.byAlias[oldest].token
	delete(c.byAlias, oldest)
	return token
}

// lock takes the conversation's send lock, or gives up when ctx does.
func (c *Conversations) lock(ctx context.Context, token string) (func(), error) {
	c.mu.Lock()
	l, ok := c.sending[token]
	if !ok {
		l = &sendLock{ch: make(chan struct{}, 1)}
		c.sending[token] = l
	}
	l.refs++
	c.mu.Unlock()
	// A caller already out of time gets its own error, not one that reads
	// as contention: with the lock free, the select below could pick
	// either case.
	if err := ctx.Err(); err != nil {
		c.release(token, l)
		return nil, err
	}
	select {
	case l.ch <- struct{}{}:
		return func() {
			<-l.ch
			c.release(token, l)
		}, nil
	case <-ctx.Done():
		c.release(token, l)
		return nil, c.busy(token, ctx.Err())
	}
}

// busy is the error for a send that gave up waiting on the conversation's
// running turn, naming its task when it is known.
func (c *Conversations) busy(token string, cause error) error {
	if ref, ok := c.runningTask(token); ok {
		return fmt.Errorf("%w (task %s); send again once it ends, or stop it with task_control: %v", ErrConversationBusy, ref, cause)
	}
	return fmt.Errorf("%w; send again once it ends, or stop it with task_control: %v", ErrConversationBusy, cause)
}

// setRunning records ref as the conversation's unfinished turn task, or,
// with ref nil, that its turns are done.
func (c *Conversations) setRunning(token string, ref *TaskRef) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if ref == nil {
		delete(c.running, token)
		return
	}
	if _, ok := c.running[token]; !ok && len(c.running) >= maxConversations {
		for t := range c.running {
			delete(c.running, t)
			break
		}
	}
	c.running[token] = *ref
}

// clearRunning forgets the conversation's unfinished task if it is still ref.
func (c *Conversations) clearRunning(token string, ref TaskRef) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.running[token] == ref {
		delete(c.running, token)
	}
}

func (c *Conversations) runningTask(token string) (TaskRef, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	ref, ok := c.running[token]
	return ref, ok
}

type taskStepState struct {
	mu                   sync.Mutex
	active               atomic.Bool
	ref                  TaskRef
	token                string
	alias                string
	byToken              bool
	notices              []string
	startedAt            time.Time
	prevReportedSteps    int
	lastReportedSteps    int
	prevReportedActs     int
	lastReportedActs     int
	lastWaitStepTitle    string
	followEmptySince     time.Time
	settledDelegationsAt time.Time
	settledFollowedCount int
	followedRefs         []TaskRef
	// seenStepKeys holds the steps already reported: true once a step was
	// reported whole, false while it was reported as a plan step before
	// any of its activities arrived (the api executor publishes the plan
	// step when a call starts and its activity only when it ends), so the
	// step is reported again once they do.
	seenStepKeys map[string]bool
	watch        *turnWatch
	watchTimer   *time.Timer
	taskWatches  map[TaskRef]*taskWatch
	finalTasks   map[TaskRef]*lib.Task
	failedTasks  map[TaskRef]error
	// idleTimer releases the task watches a streamed poll kept once no
	// poll has followed within stepWatchIdleRelease; idleGen is bumped by
	// every arm and disarm, and a firing that finds another generation is
	// stale (it was waiting on mu while a new poll ran) and does nothing,
	// so it cannot close the watches that poll just kept.
	idleTimer *time.Timer
	idleGen   uint64
	// superseded is set when a newer turn on the conversation found this
	// state's tasks finished (awaitRunningTurn). A client may still be
	// streaming it, so it stays for that client's own settle, but it no
	// longer counts as the conversation's running turn or records one.
	superseded atomic.Bool
	// cancelPoll cancels the in-flight poll (WaitForTurnStep holds mu for
	// all of it), so a stop need not wait out the poll window for mu.
	// cancelMu guards it, not mu.
	cancelMu   sync.Mutex
	cancelPoll context.CancelFunc
}

func (s *taskStepState) setCancelPoll(cancel context.CancelFunc) {
	s.cancelMu.Lock()
	s.cancelPoll = cancel
	s.cancelMu.Unlock()
}

// cancelInFlightPoll cuts short the poll holding mu, if there is one.
func (s *taskStepState) cancelInFlightPoll() {
	s.cancelMu.Lock()
	cancel := s.cancelPoll
	s.cancelMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

// armIdleReleaseLocked schedules the task watches' release after
// stepWatchIdleRelease, superseding any earlier schedule. Caller holds s.mu.
func (s *taskStepState) armIdleReleaseLocked() {
	s.disarmIdleReleaseLocked()
	gen := s.idleGen
	s.idleTimer = time.AfterFunc(stepWatchIdleRelease, func() { s.releaseIdleWatches(gen) })
}

// disarmIdleReleaseLocked cancels the scheduled release and retires its
// generation, so a firing already past Stop finds itself stale. Caller
// holds s.mu.
func (s *taskStepState) disarmIdleReleaseLocked() {
	if s.idleTimer != nil {
		s.idleTimer.Stop()
		s.idleTimer = nil
	}
	s.idleGen++
}

// releaseIdleWatches is the idle timer's body: it closes the task watches
// only when gen is still the armed generation.
func (s *taskStepState) releaseIdleWatches(gen uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.idleGen != gen {
		return
	}
	s.closeTaskWatchesLocked()
}

func (s *taskStepState) isUnsubmittedRootLocked(ref TaskRef) bool {
	return ref == s.ref && s.watch == nil && s.lastReportedSteps == 0 && s.lastReportedActs == 0 && s.finalTasks[ref] == nil && s.failedTasks[ref] == nil
}

func (s *taskStepState) syncFollowedTasksLocked() []TaskRef {
	if s.watch != nil {
		for _, ref := range s.watch.followedTasks() {
			if ref.TaskID != "" && !slices.Contains(s.followedRefs, ref) {
				s.followedRefs = append(s.followedRefs, ref)
			}
		}
	}
	return slices.Clone(s.followedRefs)
}

func (s *taskStepState) followedTasks() []TaskRef {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.syncFollowedTasksLocked()
}

func (s *taskStepState) hasFollowedTaskID(wantIDs map[string]bool) bool {
	for _, f := range s.followedTasks() {
		if wantIDs[f.TaskID] {
			return true
		}
	}
	return false
}

func (s *taskStepState) attachWatch(w *turnWatch) {
	if w == nil {
		return
	}
	s.mu.Lock()
	if s.watch == nil {
		if w.release != nil {
			w.release()
		}
		s.watch = w
		s.active.Store(true)
		s.watchTimer = time.AfterFunc(MaxTurnWait, s.closeAll)
	}
	s.mu.Unlock()
}

func (s *taskStepState) closeTaskWatchesLocked() {
	s.disarmIdleReleaseLocked()
	for k, tw := range s.taskWatches {
		tw.close()
		delete(s.taskWatches, k)
	}
}

func (s *taskStepState) closeTaskWatches() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeTaskWatchesLocked()
}

func (s *taskStepState) closeAllLocked() {
	s.active.Store(false)
	s.syncFollowedTasksLocked()
	s.closeTaskWatchesLocked()
	if s.watchTimer != nil {
		s.watchTimer.Stop()
		s.watchTimer = nil
	}
	if s.watch != nil {
		s.watch.close()
		s.watch = nil
	}
}

func (s *taskStepState) closeAll() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closeAllLocked()
}

func (c *Conversations) removeStepStateKeysLocked(target *taskStepState) {
	if target == nil {
		return
	}
	for k, cur := range c.steps {
		if cur == target {
			delete(c.steps, k)
		}
	}
}

func (c *Conversations) evictStepStateLocked(keep *taskStepState) *taskStepState {
	for k, old := range c.steps {
		if old == nil {
			delete(c.steps, k)
			return nil
		}
		if old != keep && !old.active.Load() {
			c.removeStepStateKeysLocked(old)
			return old
		}
	}
	for k, old := range c.steps {
		if old != keep {
			if old != nil {
				c.removeStepStateKeysLocked(old)
			} else {
				delete(c.steps, k)
			}
			return old
		}
	}
	return nil
}

func (c *Conversations) initTaskStepState(ref TaskRef, token, alias string, byToken bool, notices []string, w *turnWatch) *taskStepState {
	if w != nil && w.release != nil {
		// Release the submission-matching sendLock now that ownSubmission has
		// matched and c.setRunning(token, &ref) is recorded.
		w.release()
	}
	var toClose []*taskStepState
	defer func() {
		for _, s := range toClose {
			s.closeAll()
		}
	}()
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.steps == nil {
		c.steps = map[string]*taskStepState{}
	}
	if old, ok := c.steps[ref.TaskID]; ok && old != nil {
		c.removeStepStateKeysLocked(old)
		toClose = append(toClose, old)
	}
	for len(c.steps) >= maxConversations {
		evicted := c.evictStepStateLocked(nil)
		if evicted != nil {
			toClose = append(toClose, evicted)
		} else if len(c.steps) >= maxConversations {
			break
		}
	}
	now := time.Now()
	if c.now != nil {
		now = c.now()
	}
	st := &taskStepState{
		ref:         ref,
		token:       token,
		alias:       alias,
		byToken:     byToken,
		notices:     slices.Clone(notices),
		startedAt:   now,
		watch:       w,
		taskWatches: map[TaskRef]*taskWatch{},
		finalTasks:  map[TaskRef]*lib.Task{},
		failedTasks: map[TaskRef]error{},
	}
	if w != nil {
		st.active.Store(true)
		st.watchTimer = time.AfterFunc(MaxTurnWait, st.closeAll)
	}
	c.steps[ref.TaskID] = st
	return st
}

func (c *Conversations) getOrCreateTaskStepState(ref TaskRef, w *turnWatch) *taskStepState {
	c.mu.Lock()
	if c.steps == nil {
		c.steps = map[string]*taskStepState{}
	}
	if st, ok := c.steps[ref.TaskID]; ok && st != nil {
		c.mu.Unlock()
		st.attachWatch(w)
		return st
	}
	var toClose []*taskStepState
	for len(c.steps) >= maxConversations {
		evicted := c.evictStepStateLocked(nil)
		if evicted != nil {
			toClose = append(toClose, evicted)
		} else if len(c.steps) >= maxConversations {
			break
		}
	}
	now := time.Now()
	if c.now != nil {
		now = c.now()
	}
	if w != nil && w.release != nil {
		w.release()
	}
	st := &taskStepState{
		ref:         ref,
		startedAt:   now,
		watch:       w,
		taskWatches: map[TaskRef]*taskWatch{},
		finalTasks:  map[TaskRef]*lib.Task{},
		failedTasks: map[TaskRef]error{},
	}
	if w != nil {
		st.active.Store(true)
		st.watchTimer = time.AfterFunc(MaxTurnWait, st.closeAll)
	}
	c.steps[ref.TaskID] = st
	c.mu.Unlock()
	for _, s := range toClose {
		s.closeAll()
	}
	return st
}

func (c *Conversations) aliasTaskStepState(child TaskRef, st *taskStepState) {
	if c == nil || st == nil || child.TaskID == "" {
		return
	}
	var toClose []*taskStepState
	c.mu.Lock()
	if c.steps == nil {
		c.steps = map[string]*taskStepState{}
	}
	if c.steps[child.TaskID] != st {
		if old := c.steps[child.TaskID]; old != nil {
			c.removeStepStateKeysLocked(old)
			toClose = append(toClose, old)
		}
		for len(c.steps) >= maxConversations {
			evicted := c.evictStepStateLocked(st)
			if evicted != nil {
				toClose = append(toClose, evicted)
			} else {
				break
			}
		}
		c.steps[child.TaskID] = st
	}
	c.mu.Unlock()
	for _, s := range toClose {
		s.closeAll()
	}
}

func (c *Conversations) knownTaskRef(taskID string) (TaskRef, bool) {
	if c == nil || taskID == "" {
		return TaskRef{}, false
	}
	c.mu.Lock()
	st := c.steps[taskID]
	if st != nil && st.ref.TaskID == taskID && st.ref.Addressee != "" {
		c.mu.Unlock()
		return st.ref, true
	}
	var runningRef TaskRef
	hasRunning := false
	for _, ref := range c.running {
		if ref.TaskID == taskID && ref.Addressee != "" {
			runningRef = ref
			hasRunning = true
			break
		}
	}
	c.mu.Unlock()
	if st != nil {
		for _, f := range st.followedTasks() {
			if f.TaskID == taskID && f.Addressee != "" {
				return f, true
			}
		}
	}
	if hasRunning {
		return runningRef, true
	}
	return TaskRef{}, false
}

func (c *Conversations) stopTaskStepState(token string, refs ...TaskRef) {
	matched := c.matchTaskStepStates(token, refs...)
	if len(matched) == 0 {
		return
	}
	for st := range matched {
		st.cancelInFlightPoll()
	}
	c.mu.Lock()
	for k, st := range c.steps {
		if matched[st] {
			delete(c.steps, k)
		}
	}
	c.mu.Unlock()
	for st := range matched {
		st.closeAll()
	}
}

// supersedeTaskStepState marks the conversation's step states for refs
// superseded (see taskStepState.superseded) without closing them: a client
// still streaming one settles it, with its delegated tasks' answer, itself.
func (c *Conversations) supersedeTaskStepState(token string, refs ...TaskRef) {
	for st := range c.matchTaskStepStates(token, refs...) {
		st.superseded.Store(true)
	}
}

// matchTaskStepStates returns token's step states keyed under, rooted at or
// following one of refs, or all of token's with no refs.
func (c *Conversations) matchTaskStepStates(token string, refs ...TaskRef) map[*taskStepState]bool {
	if c == nil || (token == "" && len(refs) == 0) {
		return nil
	}
	wantIDs := make(map[string]bool, len(refs))
	for _, r := range refs {
		if r.TaskID != "" {
			wantIDs[r.TaskID] = true
		}
	}
	c.mu.Lock()
	matched := map[*taskStepState]bool{}
	seenUnmatched := map[*taskStepState]bool{}
	var unmatched []*taskStepState
	for k, st := range c.steps {
		if st == nil || (token != "" && st.token != token) {
			continue
		}
		if len(wantIDs) == 0 || wantIDs[k] || wantIDs[st.ref.TaskID] {
			matched[st] = true
		} else if !seenUnmatched[st] {
			seenUnmatched[st] = true
			unmatched = append(unmatched, st)
		}
	}
	c.mu.Unlock()
	for _, st := range unmatched {
		if !matched[st] && st.hasFollowedTaskID(wantIDs) {
			matched[st] = true
		}
	}
	return matched
}

func (c *Conversations) deleteTaskStepState(ref TaskRef, st *taskStepState) {
	if c == nil || ref.TaskID == "" {
		return
	}
	c.mu.Lock()
	if c.steps[ref.TaskID] == st {
		delete(c.steps, ref.TaskID)
	}
	c.mu.Unlock()
}

func (c *Conversations) removeTaskStepState(st *taskStepState) {
	if c == nil || st == nil {
		return
	}
	c.mu.Lock()
	c.removeStepStateKeysLocked(st)
	c.mu.Unlock()
}

func (c *Conversations) activeStepFollowedTasks(token string, ref TaskRef) []TaskRef {
	if c == nil || (token == "" && ref.TaskID == "") {
		return nil
	}
	var wantRef map[string]bool
	if ref.TaskID != "" {
		wantRef = map[string]bool{ref.TaskID: true}
	}
	c.mu.Lock()
	seenState := map[*taskStepState]bool{}
	directMatch := map[*taskStepState]bool{}
	var candidates []*taskStepState
	for k, st := range c.steps {
		if st == nil || (token != "" && st.token != token) {
			continue
		}
		if ref.TaskID == "" || k == ref.TaskID || st.ref.TaskID == ref.TaskID || c.steps[ref.TaskID] == st {
			directMatch[st] = true
		}
		if !seenState[st] {
			seenState[st] = true
			candidates = append(candidates, st)
		}
	}
	c.mu.Unlock()
	seenRef := map[string]bool{}
	if ref.TaskID != "" {
		seenRef[ref.TaskID] = true
	}
	var out []TaskRef
	for _, st := range candidates {
		st.mu.Lock()
		followed := st.syncFollowedTasksLocked()
		matched := directMatch[st]
		if !matched && len(wantRef) > 0 {
			for _, f := range followed {
				if wantRef[f.TaskID] {
					matched = true
					break
				}
			}
		}
		if matched {
			st.closeTaskWatchesLocked()
			for _, f := range followed {
				if f.TaskID == "" || seenRef[f.TaskID] {
					continue
				}
				if st.finalTasks[f] != nil || st.failedTasks[f] != nil {
					continue
				}
				seenRef[f.TaskID] = true
				out = append(out, f)
			}
		}
		st.mu.Unlock()
	}
	return out
}

func (c *Conversations) closeTaskStepState(ref TaskRef) {
	if c == nil || ref.TaskID == "" {
		return
	}
	c.mu.Lock()
	st := c.steps[ref.TaskID]
	if st != nil {
		for k, cur := range c.steps {
			if cur == st {
				delete(c.steps, k)
			}
		}
	}
	c.mu.Unlock()
	if st != nil {
		st.closeAll()
	}
}

func (c *Conversations) hasActiveStepState(token string) (TaskRef, bool) {
	if c == nil || token == "" {
		return TaskRef{}, false
	}
	c.mu.Lock()
	seen := map[*taskStepState]bool{}
	var candidates []*taskStepState
	for _, st := range c.steps {
		if st != nil && !seen[st] && st.token == token && !st.superseded.Load() {
			seen[st] = true
			candidates = append(candidates, st)
		}
	}
	c.mu.Unlock()
	for _, st := range candidates {
		st.mu.Lock()
		active := st.watch != nil
		ref := st.ref
		st.mu.Unlock()
		if active {
			return ref, true
		}
	}
	return TaskRef{}, false
}

// release drops one holder or waiter, and the lock with the last of them.
func (c *Conversations) release(token string, l *sendLock) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if l.refs--; l.refs == 0 && c.sending[token] == l {
		delete(c.sending, token)
	}
}

// TurnResult is what a sent turn produced: the task the gateway opened, or
// steered, for it, and the notices it posted.
type TurnResult struct {
	Token   string
	Task    *TaskRef
	Notices []string
}

// turnFate is what the gateway does with a turn's text, by the same rules it
// applies (a2a/gateway/gateway.go's command switch, which the console page's
// turnFate mirrors too): settled turns are answered with a notice and open no
// task; any other turn opens or steers one under one of texts, the text as
// sent or, for a delegate turn, its stripped rest.
func turnFate(text string) (settled bool, texts []string) {
	t := strings.TrimSpace(text)
	// A bare /feedback and /feedback summary are answered in place, as
	// the gateway and the web console's turnFate both treat them.
	if rest, ok := chattext.IsFeedbackCommand(t); ok && (strings.TrimSpace(rest) == "" || chattext.IsFeedbackSummaryCommand(t)) {
		return true, nil
	}
	if rest, ok := chattext.IsSessionCommand(t); ok {
		rest = strings.TrimSpace(rest)
		if rest == "" || chattext.IsSessionOff(rest) || chattext.IsStop(rest) {
			return true, nil
		}
		t = rest
	}
	if chattext.IsStop(t) {
		return true, nil
	}
	texts = []string{t}
	if rest, ok := chattext.IsDelegate(t); ok && strings.TrimSpace(rest) != "" {
		texts = append(texts, strings.TrimSpace(rest))
	}
	return false, texts
}

// turnWatch is a sent turn's view of the conversation's submissions: the
// turn's own task once it appears, then the tasks delegated from it.
type turnWatch struct {
	conversation string
	token        string
	texts        []string
	// since is the event cursor before the turn was sent: a card's report
	// on this turn comes after it.
	since       uint64
	subs        chan *nats.Msg
	notices     chan *nats.Msg
	unsubscribe func()
	// unpin releases the conversation's buffer, which the turn holds while
	// it gathers from it.
	unpin func()
	// resultSeen is signalled on each result frame recorded in results.
	resultSeen chan struct{}
	// release hands back the conversation's send lock. A turn holds it from
	// before it is sent until the watch closes, after its answer is read, so
	// a second turn on the conversation is sent only once this one is done:
	// sent while this one ran, the gateway would steer it into this turn's
	// task, and its answer would be this turn's (or, refused, nothing).
	release func()

	// mu guards followed and known, which the follower goroutine fills
	// from the conversation's submissions once the turn's task is known.
	mu       sync.Mutex
	followed []TaskRef
	known    map[string]bool
	stop     chan struct{}
	// results is the result frames heard on the conversation, by the task
	// each names (its taskId, or the r-<task> of its message id): whether
	// any said cards are pending. Bounded by maxTurnResults.
	results map[string]bool
}

// maxTurnResults bounds the result frames one turn records; past it a new
// task's is not recorded, and the turn falls back to its quiet period.
const maxTurnResults = 64

// resultMessagePrefix is the gateway's message id prefix for a chain's
// result frame: r-<root task id>-<state> (r-<root task id> before the
// gateway named the state in it). The frame's taskId field says the same
// and is read first; the id is the fallback.
const resultMessagePrefix = "r-"

// resultIDTask is the task a result frame's message id names, with the
// state suffix the gateway adds stripped.
func resultIDTask(messageID string) (string, bool) {
	id, ok := strings.CutPrefix(messageID, resultMessagePrefix)
	if !ok {
		return "", false
	}
	if i := strings.LastIndex(id, "-"); i > 0 && lib.TaskState(id[i+1:]).Terminal() {
		id = id[:i]
	} else if i := strings.LastIndex(id, "-input-required"); i > 0 && id[i:] == "-input-required" {
		id = id[:i]
	}
	return id, lib.ValidSubjectToken(id)
}

// noteResult records a result frame heard on the conversation.
func (w *turnWatch) noteResult(f ConsoleOutFrame) {
	var ids []string
	if lib.ValidSubjectToken(f.TaskID) {
		ids = append(ids, f.TaskID)
	}
	if id, ok := resultIDTask(f.MessageID); ok && id != f.TaskID {
		ids = append(ids, id)
	}
	w.mu.Lock()
	for _, id := range ids {
		if _, ok := w.results[id]; ok || len(w.results) < maxTurnResults {
			// Once any result said cards are pending, a later one without
			// the flag does not undo it.
			w.results[id] = w.results[id] || f.CardsPending
		}
	}
	w.mu.Unlock()
	select {
	case w.resultSeen <- struct{}{}:
	default:
	}
}

// rootResult reports whether a result frame for root was heard, and whether
// one said cards are pending.
func (w *turnWatch) rootResult(root string) (seen, pending bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	pending, seen = w.results[root]
	return seen, pending
}

// openTurn subscribes to the conversation's submissions and notices, live on
// the server before it returns so that nothing published after is missed.
func (b *NATSBus) openTurn(token string, texts []string, submissions bool) (*turnWatch, error) {
	nc := b.client.Conn()
	w := &turnWatch{
		conversation: fmt.Sprintf(consoleConversationOf, token),
		token:        token,
		texts:        texts,
		subs:         make(chan *nats.Msg, 256),
		notices:      make(chan *nats.Msg, 64),
		known:        map[string]bool{},
		stop:         make(chan struct{}),
		results:      map[string]bool{},
		resultSeen:   make(chan struct{}, 1),
	}
	// Submissions are every addressee's .in traffic, drained by the turn's
	// matcher and then its follower. A settled turn has neither, and an
	// undrained subscription on a busy install is a slow consumer on the
	// connection every tool shares, so it takes the notices alone.
	var sub *nats.Subscription
	if submissions {
		var err error
		if sub, err = nc.ChanSubscribe(submissionSubjects, w.subs); err != nil {
			return nil, err
		}
	}
	// Notices are read only until the turn's task appears, and a card's
	// posts can come in bursts while the turn is followed (Events keeps
	// those), so a notice that finds the channel full is dropped rather
	// than left to back up into a slow consumer. A result frame is
	// recorded, for whether the chain handed work to cards. Nothing but a
	// notice takes a slot: a burst of reports must not crowd out the
	// turn's notice.
	nsub, err := nc.Subscribe(fmt.Sprintf(consoleOutSubject, token), func(m *nats.Msg) {
		var f ConsoleOutFrame
		if json.Unmarshal(m.Data, &f) != nil {
			return
		}
		if f.Kind == frameKindResult {
			w.noteResult(f)
			return
		}
		if noticeText(m) == "" {
			return
		}
		select {
		case w.notices <- m:
		default:
		}
	})
	if err != nil {
		if sub != nil {
			_ = sub.Unsubscribe()
		}
		return nil, err
	}
	w.unsubscribe = func() {
		if sub != nil {
			_ = sub.Unsubscribe()
		}
		_ = nsub.Unsubscribe()
	}
	if err := nc.Flush(); err != nil {
		w.unsubscribe()
		return nil, err
	}
	return w, nil
}

// SendTurn publishes text as one turn of the conversation and waits for the
// gateway's answer to it: the task carrying the turn (a new one, or a running
// one the text was sent into), or, when the gateway answers with notices
// alone (a status question, a refusal), those. The returned watch keeps
// following the turn's delegations until it is closed. A turn still running
// on the conversation, in another call or left by an earlier one, is waited
// on for up to wait first; past it the send fails with ErrConversationBusy.
func (b *NATSBus) SendTurn(ctx context.Context, convs *Conversations, token, text string, wait time.Duration) (TurnResult, *turnWatch, error) {
	res := TurnResult{Token: token}
	if strings.TrimSpace(text) == "" {
		return res, nil, errors.New("empty message")
	}
	if len(text) > consoleTextCap {
		return res, nil, fmt.Errorf("message is %d bytes; the console door takes at most %d", len(text), consoleTextCap)
	}
	settled, texts := turnFate(text)
	if settled {
		res, err := b.sendSettled(ctx, convs, token, text)
		return res, nil, err
	}
	queued, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	unlock, err := convs.lock(queued, token)
	if err != nil {
		return res, nil, err
	}
	// A caller that gave up while waiting for the lock must not send.
	if err := ctx.Err(); err != nil {
		unlock()
		return res, nil, err
	}
	if err := b.awaitRunningTurn(queued, convs, token); err != nil {
		unlock()
		return res, nil, err
	}
	// The conversation's reports are listened to before the turn goes,
	// so none on it is missed, whether or not its name was resolved here,
	// and its buffer is held until the turn is done gathering, whatever
	// drops the name meanwhile.
	unpin, err := b.events.Pin(token)
	if err != nil {
		unlock()
		return res, nil, err
	}
	w, err := b.openTurn(token, texts, true)
	if err != nil {
		unpin()
		unlock()
		return res, nil, err
	}
	w.unpin = unpin
	w.since = b.events.Cursor()
	// From here the watch holds the lock, and closing it releases it.
	var once sync.Once
	w.release = func() { once.Do(unlock) }
	if err := b.publishFrame(token, text); err != nil {
		w.close()
		return res, nil, err
	}

	deadline := time.NewTimer(submissionWait)
	defer deadline.Stop()
	var grace <-chan time.Time
	for {
		select {
		case m := <-w.subs:
			if ref, ok := w.ownSubmission(m); ok {
				res.Task = &ref
				convs.setRunning(token, &ref)
				go w.follow()
				res.Notices = w.settleNotices(ctx, res.Notices)
				return res, w, nil
			}
		case m := <-w.notices:
			if n := noticeText(m); n != "" {
				res.Notices = append(res.Notices, n)
				if grace == nil {
					grace = time.After(noticeGrace)
				}
			}
		case <-grace:
			w.close()
			return res, nil, nil
		case <-deadline.C:
			w.close()
			if len(res.Notices) > 0 {
				return res, nil, nil
			}
			return res, nil, fmt.Errorf("the gateway did not answer the turn within %s", submissionWait)
		case <-ctx.Done():
			w.close()
			return res, nil, ctx.Err()
		}
	}
}

// settleNotices reads the gateway's notices about the turn for noticeSettle
// after its submission matched, and returns them appended to notices: the
// acknowledgement may still be in flight at the match (see noticeSettle), and
// with both channels ready the select above picks either at random. The
// follower drains the submissions meanwhile; ctx cuts the window short.
func (w *turnWatch) settleNotices(ctx context.Context, notices []string) []string {
	settle := time.NewTimer(noticeSettle)
	defer settle.Stop()
	for {
		select {
		case m := <-w.notices:
			if n := noticeText(m); n != "" {
				notices = append(notices, n)
			}
		case <-settle.C:
			return notices
		case <-ctx.Done():
			return notices
		}
	}
}

// awaitRunningTurn waits, within ctx, for the conversation's unfinished turn
// task, if it has one, to finish.
func (b *NATSBus) awaitRunningTurn(ctx context.Context, convs *Conversations, token string) error {
	for {
		ref, ok := convs.runningTask(token)
		if !ok {
			if stepRef, active := convs.hasActiveStepState(token); active {
				ref = stepRef
			} else {
				return nil
			}
		}
		t, err := b.waitForSubmittedTask(ctx, ref, time.Until(deadlineOf(ctx)))
		switch {
		case errors.Is(err, ErrTaskNotFound), err == nil && t != nil && t.Final:
			for _, fRef := range convs.activeStepFollowedTasks(token, ref) {
				if err := b.awaitSingleTask(ctx, convs, token, fRef); err != nil {
					return err
				}
			}
			convs.clearRunning(token, ref)
			convs.supersedeTaskStepState(token, ref)
			if _, active := convs.hasActiveStepState(token); active {
				continue
			}
			return nil
		case tooLargeToRead(err):
			// The task can't be read here, now or later, so it can't be waited
			// on either. Holding the conversation for it would refuse every
			// later message, so let this one go.
			convs.clearRunning(token, ref)
			convs.supersedeTaskStepState(token, ref)
			if _, active := convs.hasActiveStepState(token); active {
				continue
			}
			return nil
		case ctx.Err() != nil:
			return convs.busy(token, ctx.Err())
		case errors.Is(err, context.DeadlineExceeded):
			// The read's own deadline, ctx's, passed a moment before ctx's did.
			return convs.busy(token, err)
		case err != nil:
			return fmt.Errorf("reading the conversation's running task %s: %w", ref, err)
		}
		return convs.busy(token, context.DeadlineExceeded)
	}
}

func (b *NATSBus) awaitSingleTask(ctx context.Context, convs *Conversations, token string, ref TaskRef) error {
	t, err := b.waitForSubmittedTask(ctx, ref, time.Until(deadlineOf(ctx)))
	switch {
	case errors.Is(err, ErrTaskNotFound), err == nil && t != nil && t.Final, tooLargeToRead(err):
		return nil
	case ctx.Err() != nil:
		return convs.busy(token, ctx.Err())
	case errors.Is(err, context.DeadlineExceeded):
		return convs.busy(token, err)
	case err != nil:
		return fmt.Errorf("reading the conversation's running task %s: %w", ref, err)
	default:
		return convs.busy(token, context.DeadlineExceeded)
	}
}

// SendStop sends the console door's stop word.
func (b *NATSBus) SendStop(ctx context.Context, convs *Conversations, token string) (TurnResult, error) {
	return b.sendSettled(ctx, convs, token, stopText)
}

// isStopText reports whether text is the console door's stop word, alone or
// as a /session stop.
func isStopText(text string) bool {
	t := strings.TrimSpace(text)
	if rest, ok := chattext.IsSessionCommand(t); ok {
		t = strings.TrimSpace(rest)
	}
	return chattext.IsStop(t)
}

// sendSettled sends a turn the gateway answers with a notice and no task (a
// stop, a bare /session, /session off, a bare /feedback) and returns the
// notice, or nothing if none arrives within noticeGrace.
func (b *NATSBus) sendSettled(ctx context.Context, convs *Conversations, token, text string) (TurnResult, error) {
	res := TurnResult{Token: token}
	// A stop does not wait for the conversation's turn to end: stopping
	// that turn is what it is for.
	if !isStopText(text) {
		unlock, err := convs.lock(ctx, token)
		if err != nil {
			return res, err
		}
		defer unlock()
	}
	if err := ctx.Err(); err != nil {
		return res, err
	}
	w, err := b.openTurn(token, nil, false)
	if err != nil {
		return res, err
	}
	defer w.close()
	if err := b.publishFrame(token, text); err != nil {
		return res, err
	}
	timeout := time.After(noticeGrace)
	for {
		select {
		case m := <-w.notices:
			if n := noticeText(m); n != "" {
				res.Notices = append(res.Notices, n)
				return res, nil
			}
		case <-timeout:
			return res, nil
		case <-ctx.Done():
			return res, ctx.Err()
		}
	}
}

func (b *NATSBus) publishFrame(token, text string) error {
	id := make([]byte, messageIDBytes)
	if _, err := rand.Read(id); err != nil {
		return err
	}
	frame, _ := json.Marshal(ConsoleInFrame{MessageID: conversationTokenPrefix + "-" + hex.EncodeToString(id), Text: text})
	return b.client.Conn().Publish(fmt.Sprintf(consoleInSubject, token), frame)
}

// noticeText is a frame's text when it is a gateway notice about a turn, and
// "" otherwise: an earlier task's result posted to the conversation must not
// read as an answer to the turn being sent, nor an edit of the gateway's
// rolling status line, since every notice answering a turn is a fresh post.
func noticeText(m *nats.Msg) string {
	var f ConsoleOutFrame
	if json.Unmarshal(m.Data, &f) != nil || f.Kind != "" || f.Edit {
		return ""
	}
	return strings.TrimSpace(f.Text)
}

func (w *turnWatch) close() {
	if w == nil {
		return
	}
	if w.unsubscribe != nil {
		w.unsubscribe()
	}
	if w.release != nil {
		w.release()
	}
	select {
	case <-w.stop:
	default:
		close(w.stop)
		if w.unpin != nil {
			w.unpin()
		}
	}
}

// submission parses an .in message into its task, conversation, the task it
// was delegated from (if any) and its text. Only a message counts: a cancel,
// or anything else on the subject, is not a turn.
func submission(m *nats.Msg) (ref TaskRef, a submissionAuthority, text string, ok bool) {
	parts := strings.Split(m.Subject, ".")
	if len(parts) != submissionSubjectParts {
		return
	}
	env, err := lib.ParseEnvelope(m.Data)
	if err != nil || env.Kind != lib.KindMessage || env.TaskID == "" || len(env.Authority) == 0 {
		return
	}
	if json.Unmarshal(env.Authority, &a) != nil {
		return
	}
	var msg lib.Message
	if json.Unmarshal(env.Payload, &msg) != nil {
		return
	}
	return TaskRef{Addressee: parts[submissionAddressee], TaskID: env.TaskID}, a, strings.TrimSpace(partsText(msg.Parts)), true
}

// ownSubmission reports whether m carries this turn: a message in this
// conversation, not delegated from another task, with this turn's text. A
// message for a task already running is the gateway sending the text into
// it, and that task is where the turn's answer is.
func (w *turnWatch) ownSubmission(m *nats.Msg) (TaskRef, bool) {
	ref, a, text, ok := submission(m)
	if !ok || a.Audience.Conversation != w.conversation || a.Via != nil || !slices.Contains(w.texts, text) {
		return TaskRef{}, false
	}
	w.known[ref.TaskID] = true
	return ref, true
}

// follow records the tasks delegated, directly or not, from the tasks this
// turn already knows, as their submissions arrive, until the watch closes. It
// keeps the subscription drained, so a busy install's submissions never back
// up into a slow-consumer drop.
func (w *turnWatch) follow() {
	for {
		select {
		case <-w.stop:
			return
		case m := <-w.subs:
			ref, a, _, ok := submission(m)
			if !ok || a.Audience.Conversation != w.conversation || a.Via == nil {
				continue
			}
			w.mu.Lock()
			if w.known[a.Via.TaskID] && !w.known[ref.TaskID] && len(w.followed) < maxFollowedTasks {
				w.known[ref.TaskID] = true
				w.followed = append(w.followed, ref)
			}
			w.mu.Unlock()
		}
	}
}

// inChain reports whether id is a task of this turn: its own, or one
// delegated from it, directly or not.
func (w *turnWatch) inChain(id string) bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return id != "" && w.known[id]
}

func (w *turnWatch) followedTasks() []TaskRef {
	w.mu.Lock()
	defer w.mu.Unlock()
	return slices.Clone(w.followed)
}

// TurnOutcome is a turn's task and the tasks delegated from it, each as last
// read, and which of them holds the answer.
type TurnOutcome struct {
	Root     TaskRef
	Task     *lib.Task
	Followed []FollowedTask
	// AnswerFrom is the task whose result is the turn's answer: the last
	// delegated task to finish with one (a delegation's answer lands on the
	// task that wakes the delegator), else the root.
	AnswerFrom *TaskRef
	Answer     *lib.Task
	// Reports are the cards' posts on the turn, in arrival order, an edit
	// in its post's place; ReportsEnd says why gathering them stopped, and
	// ReportCursor is the event cursor then, for poll_events.
	Reports      []EventFrame
	ReportsEnd   reportsEnd
	ReportCursor uint64
	// ReportsDropped: the conversation's buffer dropped a frame posted
	// after the turn was sent, which may have been one of its reports.
	ReportsDropped bool
	// Cards is what the gateway's result for the root said about cards,
	// as last read while gathering.
	Cards cardsSignal
}

// cardsSignal is what the gateway's result frame for a turn's root said
// about work handed to kanban cards.
type cardsSignal int

const (
	// cardsUnknown: no result frame for the root was heard (a gateway that
	// predates the flag, or one whose heal path missed it).
	cardsUnknown cardsSignal = iota
	// cardsNone: the root's result came without cardsPending.
	cardsNone
	// cardsPending: the root's result said work was handed to cards.
	cardsPending
)

// reportsEnd is why a turn's report gathering stopped.
type reportsEnd int

const (
	// reportsNotGathered: no listener (a bus without events).
	reportsNotGathered reportsEnd = iota
	// reportsRunning: the wait ended before the turn's tasks did.
	reportsRunning
	// reportsQuiet: reportQuiet passed with no new post on the turn.
	reportsQuiet
	// reportsWaitOver: the wait ended while gathering.
	reportsWaitOver
	// reportsNoCards: the root's result said no work went to cards.
	reportsNoCards
	// reportsNoCardSignal: a streamed turn ended with no handoff, card
	// step or result frame heard, so gathering stopped at once.
	reportsNoCardSignal
)

// reportQuiet is how long, once a turn's tasks have ended, gathering waits
// for a card's next post on it before it stops: counted from the end, then
// from each post.
var reportQuiet = 15 * time.Second

// Unfinished is a task of the turn last read as not yet final, the one the
// conversation's next turn waits on, or nil when the turn is done.
func (o TurnOutcome) Unfinished() *TaskRef {
	// A root read as nothing yet has not started, not ended.
	if o.Task == nil || !o.Task.Final {
		return &o.Root
	}
	for i := len(o.Followed) - 1; i >= 0; i-- {
		if f := o.Followed[i]; f.Task == nil || !f.Task.Final {
			return &f.Ref
		}
	}
	return nil
}

// FollowedTask is one delegated task as last read.
type FollowedTask struct {
	Ref  TaskRef
	Task *lib.Task
	Err  error
}

// delegationSettle is how long, once every followed task has finished,
// WaitForTurn waits for another delegation before taking the last answer.
const delegationSettle = 2 * time.Second

// WaitForTurn waits, within wait, for the turn's task to finish and, when it
// delegated (it carries a delegate artifact; a handoff to kanban cards is
// not followed, see delegated), for the tasks
// delegated from it: the answer of a delegation lands on the task that wakes
// the delegator, which may come minutes after the delegator itself finished.
// Followed tasks are read one at a time on one reader slot. Then, in what is
// left of wait, it gathers the cards' reports on the turn (gatherReports).
// It closes the watch.
func (b *NATSBus) WaitForTurn(ctx context.Context, w *turnWatch, root TaskRef, wait time.Duration) (TurnOutcome, error) {
	defer w.close()
	ctx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	out, ended, err := b.waitForChain(ctx, w, root)
	if err != nil {
		return out, err
	}
	b.gatherReports(ctx, w, &out, ended)
	return out, nil
}

// WaitForTurnStep advances a streaming turn by one or more live steps (or waits
// up to a short step window for the next step or task completion) and returns a
// TurnStepOutcome suitable for FormatTurnCustomToolEnvelope.
func (b *NATSBus) WaitForTurnStep(ctx context.Context, convs *Conversations, w *turnWatch, root TaskRef, wait time.Duration) (TurnStepOutcome, error) {
	if convs == nil {
		convs = NewConversations()
	}
	st := convs.getOrCreateTaskStepState(root, w)
	st.mu.Lock()
	defer st.mu.Unlock()

	st.disarmIdleReleaseLocked()
	if st.watch != nil && st.watchTimer != nil {
		st.watchTimer.Reset(MaxTurnWait)
	}

	window := nextStepWaitWindow
	if st.lastReportedSteps == 0 {
		window = firstStepWaitWindow
	}
	if wait > 0 && wait < window {
		window = wait
	}
	// A stop cancels pollCtx (cancelInFlightPoll) rather than wait for mu.
	pollCtx, pollCancel := context.WithCancel(ctx)
	defer pollCancel()
	st.setCancelPoll(pollCancel)
	defer st.setCancelPoll(nil)
	stepCtx, cancel := context.WithTimeout(pollCtx, window)
	defer cancel()

	prevSteps := st.lastReportedSteps
	prevActs := st.lastReportedActs
	callStart := time.Now()
	firstNewStepAt := time.Time{}

	var (
		out          TurnOutcome
		details      []StepDetail
		totalActs    int
		chainSettled bool
	)

	for {
		var (
			waitedOnBus bool
			err         error
		)
		unsubmittedRoot := st.isUnsubmittedRootLocked(root)
		out, chainSettled, waitedOnBus, err = b.pollTurnStepSnapshotLocked(stepCtx, convs, st)
		if err != nil && (unsubmittedRoot || (!errors.Is(err, ErrTaskNotFound) && stepCtx.Err() == nil)) {
			if unsubmittedRoot || tooLargeToRead(err) {
				st.closeAllLocked()
				if unsubmittedRoot {
					convs.deleteTaskStepState(root, st)
				}
			} else {
				st.armIdleReleaseLocked()
			}
			return TurnStepOutcome{}, err
		}
		details, totalActs = CollectTurnStepDetails(out)
		unseen := unseenStepDetails(details, st.seenStepKeys)

		if len(unseen) > 0 {
			if firstNewStepAt.IsZero() {
				firstNewStepAt = time.Now()
			}
			latestStep := unseen[len(unseen)-1]
			stepReady := stepReadyForDisplay(latestStep)
			if chainSettled {
				break
			}
			if stepReady && time.Since(callStart) >= minStepBatchWait && time.Since(firstNewStepAt) >= stepDebounceWait {
				break
			}
			if time.Since(firstNewStepAt) >= stepActivityWaitMax {
				break
			}
		} else if chainSettled {
			break
		}

		if stepCtx.Err() != nil {
			break
		}
		if !waitedOnBus {
			idleTimer := time.NewTimer(stepPollIdleSleep)
			select {
			case <-idleTimer.C:
			case <-stepCtx.Done():
				idleTimer.Stop()
			}
		}
	}

	newDetails := unseenStepDetails(details, st.seenStepKeys)
	if newDetails == nil {
		newDetails = []StepDetail{}
	}
	if st.seenStepKeys == nil {
		st.seenStepKeys = map[string]bool{}
	}
	nextSteps := prevSteps
	for _, d := range newDetails {
		if d.StepKey == "" {
			nextSteps++
			continue
		}
		if _, reported := st.seenStepKeys[d.StepKey]; !reported {
			nextSteps++
		}
		st.seenStepKeys[d.StepKey] = len(d.Activities) > 0 || !isPlanStep(d)
	}

	if chainSettled && len(details) > nextSteps {
		nextSteps = len(details)
	}
	nextActs := totalActs

	stillRunning := !chainSettled
	elapsed := time.Since(st.startedAt)
	if elapsed < 0 {
		elapsed = 0
	}

	waitTitle := ""
	if stillRunning && len(newDetails) == 0 {
		waitTitle = synthesizeWaitStepTitle(out, details, elapsed, st.lastWaitStepTitle)
		st.lastWaitStepTitle = waitTitle
	}

	// Only pass gateway notices on the first step so they are not repeated on every poll.
	var notices []string
	if prevSteps == 0 && len(st.notices) > 0 {
		notices = slices.Clone(st.notices)
	}

	st.prevReportedSteps = prevSteps
	st.lastReportedSteps = nextSteps
	st.prevReportedActs = prevActs
	st.lastReportedActs = nextActs

	if !stillRunning {
		if st.watch != nil {
			gatherBudget := reportGatherBudget
			if wait > 0 && wait < gatherBudget {
				gatherBudget = wait
			}
			reportCtx, reportCancel := context.WithTimeout(pollCtx, gatherBudget)
			b.gatherStepReports(reportCtx, st.watch, &out, details)
			reportCancel()
		}
		if st.token != "" && !st.superseded.Load() {
			convs.setRunning(st.token, nil)
		}
		st.closeAllLocked()
		convs.removeTaskStepState(st)
	} else {
		if len(st.taskWatches) > 0 {
			st.armIdleReleaseLocked()
		}
		if st.token != "" && !st.superseded.Load() {
			if unf := out.Unfinished(); unf != nil {
				convs.setRunning(st.token, unf)
			} else {
				convs.setRunning(st.token, &st.ref)
			}
		}
	}

	return TurnStepOutcome{
		Outcome:           out,
		Token:             st.token,
		Alias:             st.alias,
		ByToken:           st.byToken,
		Notices:           notices,
		StepDetails:       details,
		NewStepDetails:    newDetails,
		TotalActivities:   totalActs,
		PrevReportedSteps: prevSteps,
		LastReportedSteps: nextSteps,
		PrevReportedActs:  prevActs,
		LastReportedActs:  nextActs,
		Elapsed:           elapsed,
		StillRunning:      stillRunning,
		WaitStepTitle:     waitTitle,
	}, nil
}

// unseenStepDetails returns the steps not yet reported, and a plan step
// reported before its activities (seen false) once they have arrived.
func unseenStepDetails(details []StepDetail, seen map[string]bool) []StepDetail {
	var out []StepDetail
	for _, d := range details {
		if whole, reported := seen[d.StepKey]; d.StepKey != "" && reported && (whole || len(d.Activities) == 0) {
			continue
		}
		out = append(out, d)
	}
	return out
}

func stepReadyForDisplay(d StepDetail) bool {
	return len(d.Activities) > 0 || !isPlanStep(d)
}

func isPlanStep(d StepDetail) bool {
	return d.Step.Kind == "" || d.Step.Kind == stepmsg.KindPlan
}

func (b *NATSBus) pollTurnStepSnapshotLocked(ctx context.Context, convs *Conversations, st *taskStepState) (TurnOutcome, bool, bool, error) {
	root := st.ref
	out := TurnOutcome{Root: root}
	unsubmittedRoot := st.isUnsubmittedRootLocked(root)

	t, waitedRoot, err := b.pollSingleTaskLocked(ctx, st, root)
	if err != nil && (!errors.Is(err, ErrTaskNotFound) || unsubmittedRoot) {
		return out, false, waitedRoot, err
	}
	out.Task = t
	out.AnswerFrom, out.Answer = &out.Root, t

	if t == nil || !t.Final {
		return out, false, waitedRoot, nil
	}
	if !delegated(t) {
		return out, true, waitedRoot, nil
	}

	waitedAny := waitedRoot
	followedRefs := st.syncFollowedTasksLocked()
	if len(followedRefs) == 0 {
		if st.watch == nil {
			return out, true, waitedAny, nil
		}
		if st.followEmptySince.IsZero() {
			st.followEmptySince = time.Now()
		}
		if time.Since(st.followEmptySince) >= delegationFollowGrace {
			return out, true, waitedAny, nil
		}
		return out, false, waitedAny, nil
	}
	st.followEmptySince = time.Time{}

	allFollowedFinal := true
	for _, fRef := range followedRefs {
		convs.aliasTaskStepState(fRef, st)
		ft, waitedChild, ferr := b.pollSingleTaskLocked(ctx, st, fRef)
		if waitedChild {
			waitedAny = true
		}
		out.Followed = append(out.Followed, FollowedTask{Ref: fRef, Task: ft, Err: ferr})
		if tooLargeToRead(ferr) {
			st.failedTasks[fRef] = ferr
			continue
		}
		if ferr != nil {
			allFollowedFinal = false
			continue
		}
		if ft == nil || !ft.Final {
			allFollowedFinal = false
			continue
		}
		r := fRef
		if ft.Artifact(lib.ArtifactResult) != nil || out.AnswerFrom == nil || *out.AnswerFrom == root {
			out.AnswerFrom, out.Answer = &r, ft
		}
	}

	if allFollowedFinal {
		if st.settledDelegationsAt.IsZero() || len(followedRefs) != st.settledFollowedCount {
			st.settledFollowedCount = len(followedRefs)
			st.settledDelegationsAt = time.Now()
		}
		if time.Since(st.settledDelegationsAt) >= delegationSettleGrace {
			return out, true, waitedAny, nil
		}
	} else {
		st.settledFollowedCount = 0
		st.settledDelegationsAt = time.Time{}
	}
	return out, false, waitedAny, nil
}

func (b *NATSBus) pollSingleTaskLocked(ctx context.Context, st *taskStepState, ref TaskRef) (*lib.Task, bool, error) {
	if ft, ok := st.finalTasks[ref]; ok && ft != nil {
		return ft, false, nil
	}
	if fErr, ok := st.failedTasks[ref]; ok && fErr != nil {
		return nil, false, fErr
	}
	unsubmittedRoot := st.isUnsubmittedRootLocked(ref)
	tw, ok := st.taskWatches[ref]
	if !ok || tw == nil {
		var err error
		tw, err = b.watchTask(ctx, ref)
		if err != nil {
			return nil, false, err
		}
		st.taskWatches[ref] = tw
	}
	if tw.pending == 0 && len(tw.events) == 0 && unsubmittedRoot {
		tw.close()
		delete(st.taskWatches, ref)
		return nil, false, ErrTaskNotFound
	}

	waited := false
	for tw.pending > 0 {
		waited = true
		if err := tw.nextWait(ctx, stepPollWait); err != nil {
			if ctx.Err() != nil {
				break
			}
			tw.close()
			delete(st.taskWatches, ref)
			if tooLargeToRead(err) {
				st.failedTasks[ref] = err
			}
			return nil, waited, err
		}
	}
	if unsubmittedRoot && len(tw.events) == 0 {
		tw.close()
		delete(st.taskWatches, ref)
		return nil, waited, ErrTaskNotFound
	}

	var t *lib.Task
	if len(tw.events) > 0 {
		t, _ = tw.fold()
	}
	if t != nil && t.Final {
		st.finalTasks[ref] = t
		tw.close()
		delete(st.taskWatches, ref)
		return t, waited, nil
	}

	if ctx.Err() == nil {
		waited = true
		if err := tw.nextWait(ctx, stepPollWait); err != nil && ctx.Err() == nil {
			tw.close()
			delete(st.taskWatches, ref)
			if tooLargeToRead(err) {
				st.failedTasks[ref] = err
			}
			return nil, waited, err
		}
		if len(tw.events) > 0 {
			t, _ = tw.fold()
			if t != nil && t.Final {
				st.finalTasks[ref] = t
				tw.close()
				delete(st.taskWatches, ref)
			}
		}
	}
	if t == nil && len(tw.events) == 0 {
		return nil, waited, ErrTaskNotFound
	}
	return t, waited, nil
}

func (b *NATSBus) gatherStepReports(ctx context.Context, w *turnWatch, out *TurnOutcome, details []StepDetail) {
	if b.events == nil || w == nil {
		return
	}
	seen, pending := w.rootResult(out.Root.TaskID)
	hasCards := pending || (out.Task != nil && out.Task.Artifact(lib.ArtifactHandoff) != nil) || len(extractKanbanCards(details)) > 0
	if !hasCards {
		for _, f := range out.Followed {
			if f.Task != nil && f.Task.Artifact(lib.ArtifactHandoff) != nil {
				hasCards = true
				break
			}
		}
	}
	if !hasCards {
		page, _ := b.events.snapshot(w.token)
		out.Reports, out.ReportCursor = nil, page.Cursor
		out.ReportsDropped = page.Dropped > 0 && page.DroppedNewest > w.since
		for _, f := range page.Frames {
			if f.Kind == frameKindNotify && f.Cursor > w.since && w.inChain(f.ReplyTo) {
				out.Reports = append(out.Reports, f)
			}
		}
		// The step poll often sees the root end before the gateway's
		// result frame arrives: name that frame only when it was heard.
		if seen {
			out.Cards, out.ReportsEnd = cardsNone, reportsNoCards
		} else {
			out.ReportsEnd = reportsNoCardSignal
		}
		return
	}
	b.gatherReports(ctx, w, out, true)
}

func synthesizeWaitStepTitle(out TurnOutcome, details []StepDetail, elapsed time.Duration, lastTitle string) string {
	activeRef, activeTask := selectActiveTurnTask(out, true)
	if activeTask != nil {
		if p := artifactText(activeTask, lib.ArtifactProgress); p != "" {
			line := truncateInlineText(collapseInlineWhitespace(lastLine(p)), maxWaitProgressLen)
			if line != "" && line != lastTitle {
				return line
			}
		}
	}
	if len(details) > 0 && (activeTask != nil || activeRef == out.Root) {
		last := details[len(details)-1]
		base := humanizeStepTitle(last)
		candidate := fmt.Sprintf("%s (elapsed %s)", base, elapsed.Round(time.Second))
		if candidate != lastTitle {
			return candidate
		}
	}
	agent := strings.TrimSpace(activeRef.Addressee)
	if agent == "" {
		agent = defaultStepAgent
	}
	return fmt.Sprintf("Waiting for @%s response (elapsed %s)", agent, elapsed.Round(time.Second))
}

// waitForChain is WaitForTurn's task wait. ended is a turn whose tasks all
// finished inside the wait.
func (b *NATSBus) waitForChain(ctx context.Context, w *turnWatch, root TaskRef) (TurnOutcome, bool, error) {
	out := TurnOutcome{Root: root}
	t, err := b.waitForSubmittedTask(ctx, root, time.Until(deadlineOf(ctx)))
	if err != nil && !errors.Is(err, ErrTaskNotFound) {
		return out, false, err
	}
	out.Task = t
	out.AnswerFrom, out.Answer = &out.Root, t
	if t == nil || !t.Final {
		return out, false, nil
	}
	if !delegated(t) {
		return out, true, nil
	}
	ended := false
	read := map[string]FollowedTask{}
	var order []TaskRef
	settledAt := time.Time{}
	for ctx.Err() == nil {
		progressed := false
		for _, ref := range w.followedTasks() {
			if f, ok := read[ref.TaskID]; ok && (f.Task != nil && f.Task.Final || tooLargeToRead(f.Err)) {
				// Done, or a read that would fail the same way again and
				// take a reader slot each time.
				continue
			}
			if _, ok := read[ref.TaskID]; !ok {
				order = append(order, ref)
			}
			ft, ferr := b.waitForSubmittedTask(ctx, ref, time.Until(deadlineOf(ctx)))
			read[ref.TaskID] = FollowedTask{Ref: ref, Task: ft, Err: ferr}
			// A read that failed fast (every reader slot busy, say) is
			// retried after the pause below, not at once.
			if ferr == nil {
				progressed = true
			}
			if ferr == nil && ft != nil && ft.Final && ft.Artifact(lib.ArtifactResult) != nil {
				r := ref
				out.AnswerFrom, out.Answer = &r, ft
			}
		}
		if progressed {
			settledAt = time.Time{}
			continue
		}
		if out.AnswerFrom != nil && *out.AnswerFrom != root {
			if settledAt.IsZero() {
				settledAt = time.Now()
			} else if time.Since(settledAt) >= delegationSettle {
				ended = true
				break
			}
		}
		select {
		case <-ctx.Done():
		case <-time.After(minFetchWait * 2):
		}
	}
	for _, ref := range order {
		out.Followed = append(out.Followed, read[ref.TaskID])
	}
	return out, ended, nil
}

// gatherReports collects the cards' posts on the turn: notify frames heard
// since the turn was sent whose ReplyTo is one of its tasks (a current
// gateway names the root; an older one, through a session pod, the platform
// task it delegated to). When the turn's tasks ended, the gateway's result
// frame for the root decides: without cardsPending, gathering stops at once;
// with it, or when no result was heard, gathering goes on until reportQuiet
// passes with no new post on the turn, or the wait ends. A card's post says
// nothing reliable about whether it is the last, so nothing in its text ends
// the gathering. When the tasks had not ended, it takes what is already
// here.
func (b *NATSBus) gatherReports(ctx context.Context, w *turnWatch, out *TurnOutcome, ended bool) {
	if b.events == nil {
		return
	}
	quiet := time.NewTimer(reportQuiet)
	defer quiet.Stop()
	var last uint64
	for {
		page, changed := b.events.snapshot(w.token)
		out.Reports, out.ReportCursor = nil, page.Cursor
		out.ReportsDropped = page.Dropped > 0 && page.DroppedNewest > w.since
		newest := last
		for _, f := range page.Frames {
			if f.Kind == frameKindNotify && f.Cursor > w.since && w.inChain(f.ReplyTo) {
				out.Reports = append(out.Reports, f)
				newest = max(newest, f.Cursor)
			}
		}
		// A new post on the turn, or an edit of one, starts the quiet
		// period again; a post on another turn, or a result, does not.
		if newest > last {
			last = newest
			quiet.Reset(reportQuiet)
		}
		if !ended {
			out.ReportsEnd = reportsRunning
			return
		}
		// The root's result says whether the chain handed work to cards:
		// without it nothing more is coming, with it a card's report may
		// take a while. With no result heard, the quiet period decides.
		seen, pending := w.rootResult(out.Root.TaskID)
		if pending {
			out.Cards = cardsPending
		}
		switch {
		case seen && !pending:
			out.Cards, out.ReportsEnd = cardsNone, reportsNoCards
			return
		case ctx.Err() != nil:
			out.ReportsEnd = reportsWaitOver
			return
		}
		select {
		case <-changed:
		case <-w.resultSeen:
		case <-quiet.C:
			out.ReportsEnd = reportsQuiet
			return
		case <-ctx.Done():
			out.ReportsEnd = reportsWaitOver
			return
		}
	}
}

// delegated reports whether a finished task delegated its work to another
// agent, whose answer comes back on TASKS through a wake task. A handoff to
// kanban cards (lib.ArtifactHandoff) is not one: the cards report into the
// conversation themselves, over the console door, and no task on TASKS
// will carry their answer, so following one would wait out the whole wait.
func delegated(t *lib.Task) bool {
	return t.Artifact(lib.ArtifactDelegate) != nil
}

func deadlineOf(ctx context.Context) time.Time {
	if dl, ok := ctx.Deadline(); ok {
		return dl
	}
	return time.Now()
}

// ResolveAddressee finds the addressee of a task named by id alone, from its
// submission: the .in message the gateway wrote for it. More than one
// addressee holding a submission under that id is refused, rather than one
// picked: the id alone does not say whose task it is.
func (b *NATSBus) ResolveAddressee(ctx context.Context, taskID string) (string, error) {
	if !lib.ValidSubjectToken(taskID) {
		return "", fmt.Errorf("%q is not a task id", taskID)
	}
	ctx, cancel := context.WithTimeout(ctx, taskReadTimeout)
	defer cancel()
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		return "", err
	}
	addressees := map[string]bool{}
	if _, err := b.drainConsumer(ctx, s, jetstream.ConsumerConfig{
		FilterSubject: fmt.Sprintf("a2a.tasks.*.%s.%s", taskID, lib.TaskClassIn),
		DeliverPolicy: jetstream.DeliverAllPolicy,
	}, maxTaskEvents, func(msg jetstream.Msg) {
		parts := strings.Split(msg.Subject(), ".")
		if len(parts) != submissionSubjectParts || agreedEnvelope(msg.Subject(), msg.Data()) == nil {
			return
		}
		addressees[parts[submissionAddressee]] = true
	}); err != nil {
		return "", err
	}
	switch len(addressees) {
	case 0:
		return "", ErrTaskNotFound
	case 1:
		for a := range addressees {
			return a, nil
		}
	}
	return "", fmt.Errorf("task id %s is submitted to more than one addressee; name it as <addressee>%s%s", taskID, taskRefSep, taskID)
}
