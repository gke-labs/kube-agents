package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

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
	// Detached means the user said stop but no terminal event has arrived
	// (the executor may be dead and platform tasks have no janitor yet, W3
	// retarget). A detached task no longer serializes the session; its events,
	// if they ever arrive, still relay.
	Detached bool `json:"detached,omitempty"`
}

// SessionRecord is one conversation's durable state in the session-state KV
// bucket: contextId, current pod, bus session name, last activity, roster.
// Runtime state is not git and not pod annotations; KV is the house answer.
type SessionRecord struct {
	Key          string      `json:"key"`
	ContextID    string      `json:"contextId"`
	BusSession   string      `json:"busSession,omitempty"`
	PodName      string      `json:"podName,omitempty"`
	Addressee    string      `json:"addressee"`
	Kind         string      `json:"kind"`
	LastActivity time.Time   `json:"lastActivity"`
	Roster       []string    `json:"roster,omitempty"`
	ActiveTask   *ActiveTask `json:"activeTask,omitempty"`
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
}

// TaskRef names one historical task and the addressee it ran under.
type TaskRef struct {
	ID        string `json:"id"`
	Addressee string `json:"addressee"`
	// Canceled records that the gateway published a cancel for this task —
	// set only after the publish succeeded, so a true here means the cancel
	// is on the stream. It is what lets a supervisor path reached long
	// after ActiveTask moved on (Sweep, routinely) tell "finishing a cancel
	// the requester asked for" (terminal `canceled`) from "the executor
	// died mid-work" (terminal `failed`) — assertion 13's distinction.
	Canceled bool `json:"canceled,omitempty"`
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
// (refused by the fixed executor, absorbed by a session worker), so the
// status matcher's width bias and the steer acknowledgement condition on
// where the task actually runs, not on the standing route.
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

// Sessions lists every session record — the reap loop's scan.
func (r *Registry) Sessions(ctx context.Context) ([]*SessionRecord, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return nil, err
	}
	lister, err := kv.ListKeysFiltered(ctx, "sessions.>")
	if err != nil {
		return nil, err
	}
	var recs []*SessionRecord
	for key := range lister.Keys() {
		entry, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return nil, err
		}
		var rec SessionRecord
		if err := json.Unmarshal(entry.Value(), &rec); err != nil {
			continue // a malformed record must not kill the reaper
		}
		recs = append(recs, &rec)
	}
	return recs, nil
}
