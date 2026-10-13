package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// ActiveTask is the task currently serializing a session's conversation.
type ActiveTask struct {
	TaskID        string `json:"taskId"`
	CorrelationID string `json:"correlationId"`
	// Ask is the task's instruction, truncated, for status rendering only -
	// "working on: <ask>" beats "it's working" and the fold cannot supply it
	// (the submission is a message part, not folded status). This is user
	// CONTENT at rest on the bus, deliberately: the same text already rides
	// the TASKS stream in the submission envelope for the whole retention
	// window, the gateway is the only user granted $KV.session-state.>, the
	// bucket keeps one revision, and the copy dies with ActiveTask at the
	// terminal event. The pseudonymization rule covers identifiers, not
	// content (spec-chatops-gateway.md states the distinction, ratified
	// 8/31) — and because the terminal event is not guaranteed on every
	// path, the copy also carries an independent age bound (AskTTL,
	// enforced by the reap scan), so it can never outlive the stream copy
	// its justification rests on.
	Ask string `json:"ask,omitempty"`
	// SubmittedAt feeds the elapsed clock in status answers.
	SubmittedAt time.Time `json:"submittedAt,omitempty"`
	// StatusMsgID is the backend message the relay edits — the rolling
	// progress line.
	StatusMsgID string `json:"statusMsgId,omitempty"`
	// Capability pins this task's root entry in the `cap` bucket: the key
	// and the revision the mint returned. Every envelope the gateway sends
	// toward this task carries it, and it is on the record rather than in
	// memory so a gateway restart does not orphan a running task from its
	// own authority.
	Capability *capability.Ref `json:"capability,omitempty"`
	// Detached means the user said stop but no terminal event has arrived
	// (the executor may be dead and platform tasks have no janitor yet, W3
	// retarget). A detached task no longer serializes the session; its events,
	// if they ever arrive, still relay.
	Detached bool `json:"detached,omitempty"`
	// NoFirstEventNoticeAt is when the reap scan told the conversation that
	// this task had produced nothing past FirstEventGrace. Zero until then.
	// On the record rather than in memory so the notice is posted once per
	// task across gateway restarts; it goes with ActiveTask when the task is
	// released or ends.
	NoFirstEventNoticeAt time.Time `json:"noFirstEventNoticeAt,omitempty"`
	// LineNote suffixes every render of the rolling line (taskStart.LineNote);
	// on the record so a gateway restart keeps rendering it.
	LineNote string `json:"lineNote,omitempty"`
}

// SessionRecord is one conversation's durable state in the session-state KV
// bucket: contextId, current pod, bus session name, last activity, roster.
// Runtime state is not git and not pod annotations; KV is the house answer.
type SessionRecord struct {
	Key          string    `json:"key"`
	ContextID    string    `json:"contextId"`
	BusSession   string    `json:"busSession,omitempty"`
	PodName      string    `json:"podName,omitempty"`
	Addressee    string    `json:"addressee"`
	Kind         string    `json:"kind"`
	LastActivity time.Time `json:"lastActivity"`
	// LastTaskActivity is when a task last started or, from an executor's
	// terminal, ended in this session. It is what the Slack adapter's
	// session-thread rule bounds on (Gateway.hasSession), separately from
	// LastActivity, which every verified turn moves: a "@bot stop" with
	// nothing running is activity for the reap but must not re-admit a
	// thread whose last task ended hours ago. Zero on records written before
	// the field existed, which reads as "no task activity": such a thread
	// needs a fresh mention after the upgrade, once.
	LastTaskActivity time.Time   `json:"lastTaskActivity,omitempty"`
	Roster           []string    `json:"roster,omitempty"`
	ActiveTask       *ActiveTask `json:"activeTask,omitempty"`
	// SessionRouted marks a conversation on the session-pod route: the
	// addressee is a bus session name minted fresh per incarnation, and
	// Profile names the AgentProfile the incarnations run as.
	SessionRouted bool   `json:"sessionRouted,omitempty"`
	Profile       string `json:"profile,omitempty"`
	// Tasks is the context's task history, newest last — what rehydration
	// replays. Each entry keeps the addressee its subjects carried, because
	// session-routed addressees rotate per incarnation. Bounded; the
	// stream's retention is the real horizon.
	Tasks []TaskRef `json:"tasks,omitempty"`
	// SessionAuthors is everyone whose text was published to the
	// incarnation SessionAuthorsFor names: each turn's requester and each
	// steer author, stored as TaskRequester (hashed, never the id),
	// deduplicated, at most sessionAuthorCap. An incarnation's pod keeps what
	// it was told, so a delegation from it is checked against all of them,
	// not only the delegating turn's people. The set belongs to one
	// BusSession: once BusSession moves on (any rotation or retirement), it
	// is stale and the next add starts the new incarnation's (addSessionAuthor),
	// which is the one place the reset happens. SessionAuthorsUnknown marks
	// a set that no longer lists everyone (past the cap, or cleared by the
	// ask bound); the incarnation's delegations are refused until a fresh
	// one. SessionAuthorsSince is when the set's oldest entry was added,
	// what the ask bound ages it by.
	SessionAuthors        []TaskRequester `json:"sessionAuthors,omitempty"`
	SessionAuthorsFor     string          `json:"sessionAuthorsFor,omitempty"`
	SessionAuthorsUnknown bool            `json:"sessionAuthorsUnknown,omitempty"`
	SessionAuthorsSince   time.Time       `json:"sessionAuthorsSince,omitzero"`
}

// sessionAuthorCap bounds SessionRecord.SessionAuthors. It holds a turn's
// requester and a full steer list (1 + steerAuthorCap) with room for what a
// wake carries over; past it the set is marked and the incarnation refuses
// to delegate rather than drop an author.
const sessionAuthorCap = 2 * steerAuthorCap

// currentSessionAuthors makes the set the current incarnation's: a set
// recorded for an earlier BusSession is dropped. This is the reset, and the
// only one; every rotation or retirement moves BusSession, so none needs its
// own.
func (rec *SessionRecord) currentSessionAuthors() {
	if rec.SessionAuthorsFor == rec.BusSession {
		return
	}
	rec.SessionAuthors, rec.SessionAuthorsUnknown, rec.SessionAuthorsSince = nil, false, time.Time{}
	rec.SessionAuthorsFor = rec.BusSession
}

// addSessionAuthor adds an author to the current incarnation's set: not
// twice, and past the cap only as the mark.
func (rec *SessionRecord) addSessionAuthor(a TaskRequester) {
	if rec.BusSession == "" {
		return
	}
	rec.currentSessionAuthors()
	for _, have := range rec.SessionAuthors {
		if have == a {
			return
		}
	}
	if len(rec.SessionAuthors) >= sessionAuthorCap {
		rec.SessionAuthorsUnknown = true
		return
	}
	if rec.SessionAuthorsSince.IsZero() {
		rec.SessionAuthorsSince = time.Now().UTC()
	}
	rec.SessionAuthors = append(rec.SessionAuthors, a)
}

// addTurnToSession adds the people behind text published to the current
// incarnation: the turn's requester and the steer authors its entry carries
// (TaskRef.SteerAuthors), the mark if those overflowed.
func (rec *SessionRecord) addTurnToSession(requester TaskRequester, steer []TaskRequester, steerOverflow bool) {
	if rec.BusSession == "" {
		return
	}
	rec.addSessionAuthor(requester)
	for _, a := range steer {
		rec.addSessionAuthor(a)
	}
	if steerOverflow {
		rec.SessionAuthorsUnknown = true
	}
}

// sessionAuthorsOf returns the current incarnation's set, with its mark and
// age, for a caller about to rotate the incarnation (the wake).
func (rec *SessionRecord) sessionAuthorsOf() (authors []TaskRequester, unknown bool, since time.Time) {
	if rec.BusSession == "" || rec.SessionAuthorsFor != rec.BusSession {
		return nil, false, time.Time{}
	}
	return append([]TaskRequester(nil), rec.SessionAuthors...), rec.SessionAuthorsUnknown, rec.SessionAuthorsSince
}

// seedSessionAuthors starts the current incarnation's set from another's.
// The age carried is the older of the two, so nothing outlives the bound.
func (rec *SessionRecord) seedSessionAuthors(authors []TaskRequester, unknown bool, since time.Time) {
	if rec.BusSession == "" {
		return
	}
	rec.currentSessionAuthors()
	for _, a := range authors {
		rec.addSessionAuthor(a)
	}
	rec.SessionAuthorsUnknown = rec.SessionAuthorsUnknown || unknown
	if !since.IsZero() && (rec.SessionAuthorsSince.IsZero() || since.Before(rec.SessionAuthorsSince)) {
		rec.SessionAuthorsSince = since
	}
}

// TaskRequester is the turn's requester as the target's allowlist is checked
// against it when this turn asks the gateway to mint a child task: the
// backend it came in on and its Subject, the author id normalized for that
// backend and HMAC'd under the install salt (requesterSubject). No plaintext
// id is stored; the allowlist entries are hashed the same way, so the check
// compares pseudonyms. On Google Chat it is the lowercased email's hash, so it
// can differ from Attribution's requester.subject (the raw id's hash, which
// the cross-surface audit join keys on); the two are not meant to join.
// It lives in the session-state KV and never reaches the
// bus; the bus carries Attribution. Cleared by the ask bound past AskTTL.
type TaskRequester struct {
	Backend string `json:"backend"`
	Subject string `json:"subject"`
}

// TaskRef names one historical task and the authority it ran under: the
// addressee, the correlation id that threads its envelopes, and the
// capability it was minted with. A cancel is rebuilt from these.
type TaskRef struct {
	ID        string `json:"id"`
	Addressee string `json:"addressee"`
	// CorrelationID is the task's, kept past ActiveTask so a cancel for a
	// task the record has released can still ride the task's own chain.
	// Empty on entries written before it was recorded.
	CorrelationID string `json:"correlationId,omitempty"`
	// Capability is the task's, kept past ActiveTask for the same reason
	// CorrelationID is: a cancel for a task the record has released still
	// carries the task's own authority rather than none. Nil on entries
	// written before the mint existed, which render `grants: null` — the
	// same block a pre-mint gateway sent, and no executor checks a cancel.
	Capability *capability.Ref `json:"capability,omitempty"`
	// Canceled records that the gateway published a cancel for this task —
	// set only after the publish succeeded, so a true here means the cancel
	// is on the stream. It is what lets a supervisor path reached long
	// after ActiveTask moved on (Sweep, routinely) tell "finishing a cancel
	// the requester asked for" (terminal `canceled`) from "the executor
	// died mid-work" (terminal `failed`) — assertion 13's distinction.
	Canceled bool `json:"canceled,omitempty"`
	// Requester, Attribution and StartedAt are what a child task minted on
	// this turn's behalf, or the wake-up turn after it, inherits: the
	// allowlist is checked against Requester, the child's authority block is
	// Attribution with fresh grants, and StartedAt is what the AskTTL pass
	// ages them by. Nil/zero on entries written before the fields existed.
	Requester   *TaskRequester  `json:"requester,omitempty"`
	Attribution json.RawMessage `json:"attribution,omitempty"`
	StartedAt   time.Time       `json:"startedAt,omitzero"`
	// SteerAuthors are the humans who steered this turn other than its
	// requester, stored as Requester is (backend and requesterSubject: hashed,
	// never the id), deduplicated, at most steerAuthorCap. Anyone in the room
	// can steer a running turn, so a delegation from it is checked against
	// each of them as well as the requester. A child carries its parent's, a
	// wake its parent's and its child's, so a later delegation in the chain
	// is checked against everyone who shaped it. SteerAuthorsOverflow marks a
	// turn steered by more people than the cap holds; its delegation is
	// refused rather than checked against a list that dropped someone.
	// Cleared with Requester by the ask bound.
	SteerAuthors         []TaskRequester `json:"steerAuthors,omitempty"`
	SteerAuthorsOverflow bool            `json:"steerAuthorsOverflow,omitempty"`
	// Role, ParentTaskID, Children and Depth are the delegation chain. A
	// child is minted on a session turn's request and names it as parent; a
	// wake turn is started on the child's terminal and names the child. Depth
	// counts delegations: a child's is its parent's plus one, a wake inherits
	// its child's, and a turn at the bound may not delegate again. Children
	// is a slice although one child runs at a time, so fan-out is one
	// condition later rather than a record migration. Role, ParentTaskID and
	// Depth are empty on a human turn, and Children is empty on one that did
	// not delegate; all four are empty on entries written before they
	// existed.
	// StatusMsgID is the rolling-line message of a turn that delegated:
	// the child takes ActiveTask, and with it the only other copy, so the
	// parent's line is kept here to close on the parent's terminal.
	StatusMsgID  string   `json:"statusMsgId,omitempty"`
	Role         string   `json:"role,omitempty"`
	ParentTaskID string   `json:"parentTaskId,omitempty"`
	Children     []string `json:"children,omitempty"`
	Depth        int      `json:"depth,omitempty"`
	// RootTaskID is the human (or door) turn a delegation chain started
	// from: a human turn's own id, copied to its child and the wake after
	// it, and down a longer chain unchanged. The adapter's observers know
	// the whole chain by it (observedAs). Empty on entries written before
	// it existed, which read as their own root.
	RootTaskID string `json:"rootTaskId,omitempty"`
	// Request is the human's text that started the chain, capped at
	// wakeAskCap: a human turn's own message, and a wake's copy of its
	// delegating turn's, so a wake of a wake still reads the human's
	// question rather than the gateway-authored text of the wake before
	// it. The wake opens with it (wakeText), because its pod starts with
	// no memory. This is user CONTENT at rest on the bus, deliberately,
	// under the same posture as ActiveTask.Ask: the same text already rides
	// the TASKS stream in the submission envelope for the whole retention
	// window, the gateway is the only user granted $KV.session-state.>, and
	// the bucket keeps one revision. It outlives the terminal event (the
	// wake needs it after the turn ends), so the independent age bound is
	// the whole bound: AskTTL clears it with the requester copy
	// (boundAskCopyAt). Empty on entries written before it existed, on a
	// child, and once cleared.
	Request string `json:"request,omitempty"`
	// ChainEnd is set on a child whose end started no wake: the root
	// terminal observeChildEnd announced, kept so the read route reports
	// the same end for a settled chain (probeConversation). Nil otherwise.
	ChainEnd *ChainEnd `json:"chainEnd,omitempty"`
	// DelegationEnd is the reason a session turn that asked to delegate
	// minted no child: `reason: delegation-refused - <the room's notice>`
	// for a refusal, `reason: delegation-not-started - <why>` otherwise
	// (the child could not reach the bus, the turn was stopped, the request
	// was malformed, lost or unreadable). Written when the outcome is known
	// (handleDelegateRequest, settleHandOff). The turn's own `completed` is
	// then only the hand-off line, which is never a deliverable: toward an
	// observer the turn ends failed with this reason and delivers nothing
	// (handOffEnd), and the read route reports the same. Empty otherwise.
	DelegationEnd string `json:"delegationEnd,omitempty"`
}

// handOffEnd is the one rule for a session turn's own end when it asked to
// delegate: the hand-off line ("delegated to <addressee>") is never a
// deliverable. For a turn with a DelegationEnd and no child, a `completed`
// is replaced by failed with that reason, and nothing is delivered (the
// caller skips the deliverable when ok). Any other state stands, and a turn
// that minted a child ends quietly as the chain's parent (observedAs).
// relayTerminal, the heal and the read route all decide through it, after
// settleHandOff has written what the stream shows.
func (rec *SessionRecord) handOffEnd(taskID string, state lib.TaskState) (lib.TaskState, string, bool) {
	ref, ok := rec.TaskRefFor(taskID)
	if !ok || ref.DelegationEnd == "" || len(ref.Children) > 0 || state != lib.StateCompleted {
		return state, "", false
	}
	return lib.StateFailed, ref.DelegationEnd, true
}

// markDelegationEnd records why the task's delegate request minted no child,
// keeping the first reason recorded.
func (rec *SessionRecord) markDelegationEnd(taskID, reason string) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID && rec.Tasks[i].DelegationEnd == "" {
			rec.Tasks[i].DelegationEnd = reason
		}
	}
}

// ChainEnd is a delegation chain's root terminal as the gateway announced it
// when no wake followed the child.
type ChainEnd struct {
	State  lib.TaskState  `json:"state"`
	Source TerminalSource `json:"source"`
	Reason string         `json:"reason,omitempty"`
}

// rootID is the id the entry's chain is known by outside the gateway.
func (ref TaskRef) rootID() string {
	if ref.RootTaskID != "" {
		return ref.RootTaskID
	}
	return ref.ID
}

// Task roles on TaskRef.Role; the empty role is a human turn.
const (
	taskRoleChild = "child"
	taskRoleWake  = "wake"
)

// steerAuthorCap bounds TaskRef.SteerAuthors. Past it the entry is marked
// (SteerAuthorsOverflow) and its delegation refused: dropping an author
// silently would let the one it dropped through the check.
const steerAuthorCap = 8

// addSteerAuthor records a steer author on the entry: not the requester, not
// twice, and past the cap only as the overflow mark.
func (ref *TaskRef) addSteerAuthor(a TaskRequester) {
	if ref.Requester != nil && *ref.Requester == a {
		return
	}
	for _, have := range ref.SteerAuthors {
		if have == a {
			return
		}
	}
	if len(ref.SteerAuthors) >= steerAuthorCap {
		ref.SteerAuthorsOverflow = true
		return
	}
	ref.SteerAuthors = append(ref.SteerAuthors, a)
}

// carrySteerAuthors adds every steer author of from (and its overflow mark)
// to ref: how a child takes its parent's and a wake its parent's and child's.
func (ref *TaskRef) carrySteerAuthors(from TaskRef) {
	for _, a := range from.SteerAuthors {
		ref.addSteerAuthor(a)
	}
	ref.SteerAuthorsOverflow = ref.SteerAuthorsOverflow || from.SteerAuthorsOverflow
}

// recordSteerAuthor adds a steer author to the history entry of taskID.
func (rec *SessionRecord) recordSteerAuthor(taskID string, a TaskRequester) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].addSteerAuthor(a)
			return
		}
	}
}

// MarkCanceled records a published cancel against the task's history entry.
func (rec *SessionRecord) MarkCanceled(taskID string) {
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == taskID {
			rec.Tasks[i].Canceled = true
			return
		}
	}
}

// TaskRefFor returns the history entry for a task this conversation has
// held, active or not.
func (rec *SessionRecord) TaskRefFor(taskID string) (TaskRef, bool) {
	for _, ref := range rec.Tasks {
		if ref.ID == taskID {
			return ref, true
		}
	}
	return TaskRef{}, false
}

// observedAs is the task id the adapter's observers know taskID by, and
// whether taskID's own end is the end they are told. A delegation chain is
// one task to them, its root (TaskRef.RootTaskID): a child is never named,
// and neither is a turn that delegated, whose end is not the chain's. Every
// other turn's end is its root's - a human turn is its own root, and a
// wake that did not delegate ends the chain it continues. An id the record
// does not hold is passed through as it is.
func (rec *SessionRecord) observedAs(taskID string) (id string, ends bool) {
	ref, ok := rec.TaskRefFor(taskID)
	if !ok {
		return taskID, true
	}
	return ref.rootID(), ref.Role != taskRoleChild && len(ref.Children) == 0
}

// delegationHandled reports whether the task's history entry shows a
// delegate request of its was handled: a child linked, or a reason it
// minted none (DelegationEnd). Both are written to the record when they are
// made (startTaskWith's write for a mint, handleDelegateRequest's for the
// rest), so the record holds either from the moment the request is decided.
func (rec *SessionRecord) delegationHandled(taskID string) bool {
	ref, ok := rec.TaskRefFor(taskID)
	return ok && (len(ref.Children) > 0 || ref.DelegationEnd != "")
}

// mayHaveUnhandledDelegate reports whether the task is a session turn - not
// a child, addressed to the record's own bus session - whose entry shows no
// delegate request handled: the only kind of task whose fold can carry a
// delegate request still to run.
func (rec *SessionRecord) mayHaveUnhandledDelegate(taskID string) bool {
	ref, ok := rec.TaskRefFor(taskID)
	return ok && ref.Role != taskRoleChild && rec.BusSession != "" && ref.Addressee == rec.BusSession &&
		len(ref.Children) == 0 && ref.DelegationEnd == ""
}

// chainLast follows a delegation chain from the turn that started it to its
// last task: down each turn's last child to the wake that child started, until
// a turn that did not delegate (the wake that ends the chain) or a child no
// wake followed. false when root did not delegate, or a link has fallen off
// the history cap.
func (rec *SessionRecord) chainLast(root TaskRef) (TaskRef, bool) {
	cur := root
	for steps := 0; len(cur.Children) > 0 && steps <= taskHistoryCap; steps++ {
		child, ok := rec.TaskRefFor(cur.Children[len(cur.Children)-1])
		if !ok {
			return TaskRef{}, false
		}
		wake, ok := rec.wakeOf(child.ID)
		if !ok {
			return child, true
		}
		cur = wake
	}
	return cur, cur.ID != root.ID
}

// wakeOf is the wake a child's end started, if any.
func (rec *SessionRecord) wakeOf(childID string) (TaskRef, bool) {
	for _, ref := range rec.Tasks {
		if ref.Role == taskRoleWake && ref.ParentTaskID == childID {
			return ref, true
		}
	}
	return TaskRef{}, false
}

// TaskCanceled reports whether a cancel for the task is on the stream (see
// TaskRef.Canceled).
func (rec *SessionRecord) TaskCanceled(taskID string) bool {
	for _, ref := range rec.Tasks {
		if ref.ID == taskID {
			return ref.Canceled
		}
	}
	return false
}

// AddressedToOwnSession reports whether tasks currently go to the
// conversation's own bus session (an incarnation the gateway spawns)
// rather than a fixed executor — true on the standing session route AND
// during a one-shot Delegate from a fixed-route conversation, which is why
// it is not the SessionRouted field: the two executors differ on steers
// (the fixed executor queues and answers them as further turns, a session
// worker absorbs them at its next turn boundary), so the steer
// acknowledgement conditions on where the task actually runs, not on the
// standing route.
func (rec *SessionRecord) AddressedToOwnSession() bool {
	return rec.BusSession != "" && rec.Addressee == rec.BusSession
}

// AddresseeFor returns the addressee a task was published to. Session
// addressees rotate per incarnation and Delegate re-homes the record, so
// rec.Addressee only says where the LATEST task went; a straggler's replay
// must use the addressee its own subjects carried. Unknown tasks fall back
// to the record's current addressee.
func (rec *SessionRecord) AddresseeFor(taskID string) string {
	for _, ref := range rec.Tasks {
		if ref.ID == taskID {
			return ref.Addressee
		}
	}
	return rec.Addressee
}

const taskHistoryCap = 50

// Registry is the KV-backed session registry. A gateway restart rediscovers
// its sessions from here, so a gateway crash strands nothing.
type Registry struct {
	c *lib.Client
}

// NewRegistry binds a registry to the client's session-state bucket.
func NewRegistry(c *lib.Client) *Registry {
	return &Registry{c: c}
}

// kvKey maps a session key onto one KV token: KV keys tokenize on '.' like
// subjects, so the whole session key must collapse to a single dot-free
// token for the "sessions.>" listing filter to see every session. Characters
// outside the token charset become '_', with a short content hash appended
// so distinct keys cannot collide through the substitution. The record
// stores the original key, so the mapping never needs inverting.
func kvKey(sessionKey string) string {
	sanitized := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			return r
		}
		return '_'
	}, sessionKey)
	sum := sha256.Sum256([]byte(sessionKey))
	return "sessions." + sanitized + "-" + hex.EncodeToString(sum[:4])
}

// taskKey is one token by construction: taskIds are dot-free DNS-1123 labels
// (payload spec assertion 2).
func taskKey(taskID string) string {
	return "tasks." + taskID
}

func (r *Registry) kv(ctx context.Context) (jetstream.KeyValue, error) {
	return r.c.KV(ctx, lib.SessionStateBucket)
}

// Get returns the record for a session key, or nil if none exists.
func (r *Registry) Get(ctx context.Context, sessionKey string) (*SessionRecord, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return nil, err
	}
	entry, err := kv.Get(ctx, kvKey(sessionKey))
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("session %s: %w", sessionKey, err)
	}
	var rec SessionRecord
	if err := json.Unmarshal(entry.Value(), &rec); err != nil {
		return nil, fmt.Errorf("session %s: %w", sessionKey, err)
	}
	return &rec, nil
}

// ErrSessionExists reports a Create that lost the first-contact race: a
// record for the key already exists, and the caller adopts it.
var ErrSessionExists = errors.New("session record already exists")

// Create writes a record only if none exists for its key — KV Create,
// compare-and-swap semantics. contextId minting MUST ride this rather than
// Put (gateway design): two replicas or a rehydrate racing first contact
// would otherwise each mint a contextId and the last Put would fork the
// conversation's identity; with Create, the loser gets ErrSessionExists and
// reads the winner's value.
func (r *Registry) Create(ctx context.Context, rec *SessionRecord) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	data, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	if _, err := kv.Create(ctx, kvKey(rec.Key), data); err != nil {
		if errors.Is(err, jetstream.ErrKeyExists) {
			return ErrSessionExists
		}
		return fmt.Errorf("session %s: %w", rec.Key, err)
	}
	return nil
}

// Put writes a record.
func (r *Registry) Put(ctx context.Context, rec *SessionRecord) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	data, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	if _, err := kv.Put(ctx, kvKey(rec.Key), data); err != nil {
		return fmt.Errorf("session %s: %w", rec.Key, err)
	}
	return nil
}

// IndexTask records taskId -> session key so the relay can route an event to
// its conversation after a gateway restart.
func (r *Registry) IndexTask(ctx context.Context, taskID, sessionKey string) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	if _, err := kv.Put(ctx, taskKey(taskID), []byte(sessionKey)); err != nil {
		return fmt.Errorf("task index %s: %w", taskID, err)
	}
	return nil
}

// DropTask retires a task's index entry once its terminal event has been
// rendered — the stream is the durable record; the index only routes live
// events, and one key per task forever is unbounded growth.
func (r *Registry) DropTask(ctx context.Context, taskID string) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	return kv.Delete(ctx, taskKey(taskID))
}

// DeleteSession retires a session record once its retention horizon has passed.
// In JetStream KV under --history=1, kv.Delete publishes a KV-Operation: DEL
// marker that displaces the record value while retaining a ~100-byte tombstone
// on the key's subject. This bounds bucket growth to a marker per conversation
// rather than accumulating full records, rosters, and task histories.
func (r *Registry) DeleteSession(ctx context.Context, sessionKey string) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	if err := kv.Delete(ctx, kvKey(sessionKey)); err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
		return fmt.Errorf("session %s: %w", sessionKey, err)
	}
	return nil
}

// SessionForTask resolves the task index, or "" if the task is unknown.
func (r *Registry) SessionForTask(ctx context.Context, taskID string) (string, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return "", err
	}
	entry, err := kv.Get(ctx, taskKey(taskID))
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return string(entry.Value()), nil
}

// SessionCallback is invoked for each session record in a scan.
// Returning false halts the scan early without error.
type SessionCallback func(rec *SessionRecord) (bool, error)

// ScanSessions streams session records via a callback, starting from the given cursor.
// It supports cursor resumption across passes: keys are sorted lexicographically,
// and when cursor is non-empty, records with keys <= cursor are skipped.
// If the scan reaches the end of the bucket, it returns nextCursor="", done=true, err=nil.
// If the scan is interrupted (by timeout, context cancellation, or callback returning false),
// it returns the last successfully visited key as nextCursor, done=false, and any error.
func (r *Registry) ScanSessions(ctx context.Context, cursor string, cb SessionCallback) (nextCursor string, done bool, err error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return "", false, err
	}
	lister, err := kv.ListKeysFiltered(ctx, "sessions.>")
	if err != nil {
		return "", false, err
	}
	defer func() {
		_ = lister.Stop()
	}()

	var keys []string
	for key := range lister.Keys() {
		if ctx.Err() != nil {
			return cursor, false, ctx.Err()
		}
		keys = append(keys, key)
	}
	sort.Strings(keys)

	lastVisited := cursor
	for _, key := range keys {
		if cursor != "" && key <= cursor {
			continue
		}
		if ctx.Err() != nil {
			return lastVisited, false, ctx.Err()
		}
		entry, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return lastVisited, false, err
		}
		var rec SessionRecord
		if err := json.Unmarshal(entry.Value(), &rec); err != nil {
			continue // a malformed record must not kill the reaper
		}
		cont, err := cb(&rec)
		if err != nil {
			return lastVisited, false, err
		}
		if ctx.Err() != nil {
			return lastVisited, false, ctx.Err()
		}
		lastVisited = key
		if !cont {
			return lastVisited, false, nil
		}
	}
	return "", true, nil
}

// Sessions lists every session record.
func (r *Registry) Sessions(ctx context.Context) ([]*SessionRecord, error) {
	var recs []*SessionRecord
	_, _, err := r.ScanSessions(ctx, "", func(rec *SessionRecord) (bool, error) {
		recs = append(recs, rec)
		return true, nil
	})
	if err != nil {
		return nil, err
	}
	return recs, nil
}
