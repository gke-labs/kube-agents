package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/nats-io/nats.go/jetstream"
)

// The door task index (#7454): doortasks.<id> holds the session key of the
// door conversation (a2a: or inject:) that started the task, for as long as
// that conversation's record lists the task. It is what lets a door that
// didn't see a task start, after a gateway handover, find the task's record
// and read its state, terminal and result off the record and the stream.
//
// It is kept apart from the route index (tasks.<id>), which the relay drops
// at the terminal so a post-terminal straggler finds no route and is never
// rendered: the relay never reads this prefix. The record bounds it. An
// entry goes when its task leaves the record, by the history trim or the
// session's deletion, an entry found orphaned is deleted when read, and the
// active gateway sweeps orphans once per term (a rollback to a gateway that
// doesn't know the prefix leaves them behind; it never reads them).
const doorTaskPrefix = "doortasks."

// doorTaskKey is one token by construction: task ids are dot-free DNS-1123
// labels, as for taskKey.
func doorTaskKey(taskID string) string { return doorTaskPrefix + taskID }

// isDoorSession reports whether a session key is a door conversation's: the
// A2A door's or the inject door's. Only those get door entries.
func isDoorSession(sessionKey string) bool {
	return strings.HasPrefix(sessionKey, a2aKeyPrefix) || strings.HasPrefix(sessionKey, injectKeyPrefix)
}

// listsTask reports whether the record still holds taskID: as its active
// task or in its history.
func (rec *SessionRecord) listsTask(taskID string) bool {
	if rec.ActiveTask != nil && rec.ActiveTask.TaskID == taskID {
		return true
	}
	_, ok := rec.TaskRefFor(taskID)
	return ok
}

// IndexDoorTask records a door task's entry. Fenced by the session record's
// epoch, as IndexTask is, and like it a bare value.
func (r *Registry) IndexDoorTask(ctx context.Context, taskID, sessionKey string) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	if mine := r.epoch.Load(); mine != 0 {
		if _, err := r.fencedRecord(ctx, kv, sessionKey, mine); err != nil {
			return err
		}
	}
	if _, err := kv.Put(ctx, doorTaskKey(taskID), []byte(sessionKey)); err != nil {
		return fmt.Errorf("door task index %s: %w", taskID, err)
	}
	return nil
}

// DropDoorTask deletes a door task's entry. Fenced and conditional as
// DropTask is; a missing entry is not an error.
func (r *Registry) DropDoorTask(ctx context.Context, taskID string) error {
	kv, err := r.kv(ctx)
	if err != nil {
		return err
	}
	if r.epoch.Load() == 0 {
		if err := kv.Delete(ctx, doorTaskKey(taskID)); err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
			return fmt.Errorf("door task index %s: %w", taskID, err)
		}
		return nil
	}
	entry, err := kv.Get(ctx, doorTaskKey(taskID))
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("door task index %s: %w", taskID, err)
	}
	if _, err := r.fencedRecord(ctx, kv, string(entry.Value()), r.epoch.Load()); err != nil {
		return err
	}
	if r.afterCheck != nil {
		r.afterCheck()
	}
	if err := kv.Delete(ctx, doorTaskKey(taskID), jetstream.LastRevision(entry.Revision())); err != nil && !isRevisionConflict(err) {
		return fmt.Errorf("door task index %s: %w", taskID, err)
	}
	return nil
}

// DropDoorTasks deletes the entries of every task a door record lists, for
// the reap that deletes the session. The first failure is returned; the
// rest are still tried, and the term-start sweep catches what's left.
func (r *Registry) DropDoorTasks(ctx context.Context, rec *SessionRecord) error {
	if !isDoorSession(rec.Key) {
		return nil
	}
	ids := make([]string, 0, len(rec.Tasks)+1)
	if rec.ActiveTask != nil {
		ids = append(ids, rec.ActiveTask.TaskID)
	}
	for _, ref := range rec.Tasks {
		ids = append(ids, ref.ID)
	}
	var first error
	for _, id := range ids {
		if err := r.DropDoorTask(ctx, id); err != nil && first == nil {
			first = err
		}
	}
	return first
}

// doorVisible reports whether a door may answer for taskID: a task the
// record lists that is its own chain's root. A delegating turn is one task
// to the door (observedAs), so a chain's inner tasks, its children and the
// wakes that continue it, are listed on the same record but never answered
// for: the door never learns their ids, and a read must not either.
func (rec *SessionRecord) doorVisible(taskID string) bool {
	ref, ok := rec.TaskRefFor(taskID)
	return ok && ref.rootID() == taskID
}

// LocateDoorTask is the door conversation whose record lists taskID as a
// task the door knows (doorVisible), or "" when there is none. It follows
// the door entry, else the route index (a task an older gateway started has
// no door entry, but its route lasts while it runs), and answers only with
// a door session. A door entry whose record no longer lists the task is
// deleted. The caller checks the key is its own conversation's: this says
// where the task is, not whose.
func (r *Registry) LocateDoorTask(ctx context.Context, taskID string) (string, error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return "", err
	}
	var key string
	fromDoor := false
	entry, err := kv.Get(ctx, doorTaskKey(taskID))
	switch {
	case err == nil:
		key, fromDoor = string(entry.Value()), true
	case !errors.Is(err, jetstream.ErrKeyNotFound):
		return "", fmt.Errorf("door task index %s: %w", taskID, err)
	}
	if key == "" {
		if key, err = r.SessionForTask(ctx, taskID); err != nil {
			return "", err
		}
	}
	if key == "" || !isDoorSession(key) {
		return "", nil
	}
	rec, err := r.Get(ctx, key)
	if err != nil {
		return "", err
	}
	if rec != nil && rec.listsTask(taskID) {
		if rec.doorVisible(taskID) {
			return key, nil
		}
		return "", nil
	}
	if fromDoor {
		if err := r.DropDoorTask(ctx, taskID); err != nil {
			return "", err
		}
	}
	return "", nil
}

// SweepDoorTasks deletes every door entry whose record doesn't list its
// task, and counts what it kept and dropped. The active gateway runs it once
// per term. A listing cut short by a disconnect only sweeps less.
func (r *Registry) SweepDoorTasks(ctx context.Context) (kept, dropped int, err error) {
	kv, err := r.kv(ctx)
	if err != nil {
		return 0, 0, err
	}
	lister, err := kv.ListKeysFiltered(ctx, doorTaskPrefix+">")
	if err != nil {
		if errors.Is(err, jetstream.ErrNoKeysFound) {
			return 0, 0, nil
		}
		return 0, 0, err
	}
	var keys []string
	for key := range lister.Keys() {
		keys = append(keys, key)
	}
	_ = lister.Stop()
	if ctx.Err() != nil {
		return 0, 0, ctx.Err()
	}
	records := map[string]*SessionRecord{}
	for _, key := range keys {
		taskID := strings.TrimPrefix(key, doorTaskPrefix)
		entry, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return kept, dropped, err
		}
		sessionKey := string(entry.Value())
		rec, seen := records[sessionKey]
		if !seen {
			if rec, err = r.Get(ctx, sessionKey); err != nil {
				return kept, dropped, err
			}
			records[sessionKey] = rec
		}
		if rec != nil && rec.listsTask(taskID) {
			kept++
			continue
		}
		if err := r.DropDoorTask(ctx, taskID); err != nil {
			return kept, dropped, err
		}
		dropped++
	}
	return kept, dropped, nil
}

// sweepDoorTasks runs SweepDoorTasks once, at the start of the term: what a
// rollback to a gateway that never drops door entries left behind goes now,
// and the count says how many entries the index holds.
func (g *Gateway) sweepDoorTasks(ctx context.Context) {
	kept, dropped, err := g.reg.SweepDoorTasks(ctx)
	if err != nil {
		if ctx.Err() == nil {
			g.log.Warn("door task index sweep failed", "kept", kept, "dropped", dropped, "err", err)
		}
		return
	}
	g.log.Info("door task index swept", "kept", kept, "dropped", dropped)
}
