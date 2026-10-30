package gateway

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

const (
	// reapInterval paces the idle scan; reapPassTimeout bounds one pass so
	// a hung registry or API call cannot make passes pile up. Same clock
	// and reasoning as the orphan sweep's pair in spawn.go.
	reapInterval    = time.Minute
	reapPassTimeout = time.Minute
	// primerTaskResultCap bounds one task's result text in the rehydration
	// primer, so one giant artifact cannot crowd every other task out of a
	// fresh pod's first input.
	primerTaskResultCap = 2000
)

// noFirstEventNotice is what the reap scan posts, once per task, when a
// task has produced nothing on its event stream past FirstEventGrace and
// nobody has spoken since: the task id, the grace, and what the next
// message will do. Nothing is released here (noticeNoFirstEvent says why),
// so the line hedges on a start that is merely late.
const noFirstEventNotice = "⚠️ task `%s` has produced nothing on its event stream in %s; unless it starts first, your next message here starts a new task instead of going to it"

// reapLoop enforces the idle TTL — a session nobody has written to for the
// TTL loses its pod — the overdue bound on a reused pod's running task
// (taskOverdue), and the ask bound (boundAskCopy), which runs on every record the
// scan visits, pod or no pod. It also enforces SessionTTL, deleting session
// records that have been idle past the retention horizon, and posts the
// no-first-event notice (noticeNoFirstEvent) on the same visit.
func (g *Gateway) reapLoop(ctx context.Context) {
	ticker := time.NewTicker(reapInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			g.reapOnce(ctx)
		}
	}
}

func (g *Gateway) reapOnce(ctx context.Context) {
	ctx, cancel := context.WithTimeout(ctx, reapPassTimeout)
	defer cancel()

	g.mu.Lock()
	cursor := g.reapCursor
	g.mu.Unlock()

	nextCursor, done, err := g.reg.ScanSessions(ctx, cursor, func(rec *SessionRecord) (bool, error) {
		g.reapSession(ctx, rec)
		if g.reapScanHook != nil {
			return g.reapScanHook(rec), nil
		}
		return true, nil
	})
	if err != nil && !errors.Is(err, context.DeadlineExceeded) && !errors.Is(err, context.Canceled) {
		g.log.Error("reap: session scan failed", "err", err, "cursor", cursor)
	}

	g.mu.Lock()
	if done {
		g.reapCursor = ""
	} else if nextCursor != "" {
		g.reapCursor = nextCursor
	}
	g.mu.Unlock()
}

func (g *Gateway) reapSession(ctx context.Context, rec *SessionRecord) {
	g.boundAskCopy(ctx, rec)
	g.noticeNoFirstEvent(ctx, rec)

	// Prune records older than SessionTTL whose pod has been reaped (or never
	// incarnated); sessionPruneDue has the whole test.
	if g.sessionPruneDue(rec, time.Now()) {
		l := g.lockSession(rec.Key)
		l.Lock()
		fresh, err := g.reg.Get(ctx, rec.Key)
		if err == nil && fresh != nil && g.sessionPruneDue(fresh, time.Now()) {
			if err := g.reg.DeleteSession(ctx, fresh.Key); err != nil {
				g.log.Error("reap: session record delete failed", "session", fresh.Key, "err", err)
			} else {
				g.log.Info("reaped expired session record", "session", fresh.Key, "lastActivity", fresh.LastActivity)
				if fresh.ActiveTask != nil {
					_ = g.reg.DropTask(ctx, fresh.ActiveTask.TaskID)
					g.mu.Lock()
					delete(g.relays, fresh.ActiveTask.TaskID)
					delete(g.taskSessions, fresh.ActiveTask.TaskID)
					g.mu.Unlock()
				}
			}
		}
		l.Unlock()
		return
	}

	if rec.PodName == "" {
		return // nothing incarnated (the Hermes-first world, or already reaped)
	}
	// A reused pod's running task past its bound is the one exception to
	// the rule below: the task is closed as its supervisor and the pod goes
	// (retireOverdue), so a wedged adapter cannot hold a conversation for
	// the hours its pod deadline allows.
	if g.taskOverdue(rec, time.Now()) {
		g.retireOverdue(ctx, rec.Key)
		return
	}
	if rec.ActiveTask != nil && !rec.ActiveTask.Detached {
		return // never delete a pod out from under a running task
	}
	// The idle clock is the last user message, not the last activity: the
	// answer to a message does not keep the pod (idleSince).
	if time.Since(rec.idleSince()) < g.cfg.IdleTTL {
		return
	}
	l := g.lockSession(rec.Key)
	l.Lock()
	// Re-run every predicate on the fresh record under the lock: a
	// message that arrived between scan and lock may have started a task
	// or reset the idle clock, and reap must never delete a pod out from
	// under either.
	fresh, err := g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil || fresh.PodName == "" ||
		(fresh.ActiveTask != nil && !fresh.ActiveTask.Detached) ||
		time.Since(fresh.idleSince()) < g.cfg.IdleTTL {
		l.Unlock()
		return
	}
	// A detached task does not exempt the session, so reap may delete a
	// pod whose harness is still working — the supervisor rule is what
	// keeps that from being a silent stop: its terminal `canceled` goes
	// on the stream before the pod goes. A publish failure keeps the
	// pod (and the reap retries next cycle) rather than stranding the
	// task non-terminal for the retention window.
	if !g.closeDetachedBeforeDelete(ctx, fresh) {
		l.Unlock()
		return
	}
	if g.spawner != nil {
		if err := g.spawner.Delete(ctx, fresh.PodName); err != nil {
			g.log.Error("reap: pod delete failed", "pod", fresh.PodName, "err", err)
			l.Unlock()
			return
		}
	}
	g.log.Info("reaped idle session", "session", fresh.Key, "pod", fresh.PodName)
	// The pod was an incarnation, not the identity: contextId persists
	// until SessionTTL expires.
	fresh.PodName = ""
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("reap: record write failed", "session", fresh.Key, "err", err)
	}
	l.Unlock()
}

// boundAskCopy is the independent bound the content posture owes the `ask`
// copy in session-state. The copy's justification — same text on the
// W-bounded stream, deleted with the active-task record at the terminal
// event — holds only where a terminal is guaranteed, and the spec names the
// cases where it is not (a wedged adapter until every pod carries its
// deadline; fixed-route executors with no janitor until stage 3). So an ask
// older than AskTTL is cleared here, in the same scan that reaps — content
// only: the task record itself, its serialization, and its detach state are
// untouched, because this bound is about the copy's horizon, not the
// task's lifecycle. The same pass bounds the history entries' requester and
// attribution copies by their StartedAt. A copy exactly AskTTL old is past
// it, on both sides.
func (g *Gateway) boundAskCopy(ctx context.Context, rec *SessionRecord) {
	g.boundAskCopyAt(ctx, rec, time.Now())
}

// boundAskCopyAt is boundAskCopy at a given instant, so the TTL boundary is
// testable without a sleep.
func (g *Gateway) boundAskCopyAt(ctx context.Context, rec *SessionRecord, now time.Time) {
	active := rec.ActiveTask
	askExpired := active != nil && active.Ask != "" && !active.SubmittedAt.IsZero() &&
		now.Sub(active.SubmittedAt) >= g.cfg.AskTTL
	if !askExpired && !g.requesterExpired(rec, now) && !g.sessionAuthorsExpired(rec, now) {
		return
	}
	l := g.lockSession(rec.Key)
	l.Lock()
	defer l.Unlock()
	// Same discipline as the reap: re-check on the fresh record under the
	// lock, and clear only the copies the scan saw expire.
	fresh, err := g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil {
		return
	}
	changed := false
	var askTaskID string      // the active task whose ask was cleared, if any
	var requesterIDs []string // history entries whose requester copy was cleared
	if askExpired && fresh.ActiveTask != nil && fresh.ActiveTask.TaskID == active.TaskID &&
		fresh.ActiveTask.Ask != "" && !fresh.ActiveTask.SubmittedAt.IsZero() &&
		now.Sub(fresh.ActiveTask.SubmittedAt) >= g.cfg.AskTTL {
		fresh.ActiveTask.Ask = ""
		askTaskID = fresh.ActiveTask.TaskID
		changed = true
	}
	// The requester copy on the task history is bounded the same way: the
	// pseudonymized requester a later child task would be checked against,
	// the attribution it would inherit, and the request text a wake would
	// open with, outlive nothing past the TTL. The entry
	// itself stays; a delegation from it is refused rather than guessed.
	for i := range fresh.Tasks {
		ref := &fresh.Tasks[i]
		if !ref.holdsRequesterCopy() {
			continue
		}
		if ref.StartedAt.IsZero() || now.Sub(ref.StartedAt) < g.cfg.AskTTL {
			continue
		}
		ref.Requester, ref.Attribution = nil, nil
		ref.SteerAuthors, ref.SteerAuthorsOverflow = nil, false
		ref.Request = "" // user content, the ActiveTask.Ask posture
		requesterIDs = append(requesterIDs, ref.ID)
		changed = true
	}
	// The incarnation's author set is the same kind of copy (hashed ids a
	// delegation is checked against) and is bounded the same way, from its
	// oldest entry. Cleared, it no longer lists everyone, so it is marked
	// and the incarnation's delegations fail closed, as a cleared
	// requester's do.
	sessionAuthorsCleared := false
	if g.sessionAuthorsExpired(fresh, now) {
		fresh.SessionAuthors, fresh.SessionAuthorsSince = nil, time.Time{}
		fresh.SessionAuthorsUnknown = true
		sessionAuthorsCleared, changed = true, true
	}
	if !changed {
		return
	}
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("ask bound: record write failed", "session", fresh.Key, "err", err)
		return
	}
	g.log.Info("ask bound: cleared copies past their TTL", "session", fresh.Key,
		"taskId", askTaskID, "requesterTaskIds", requesterIDs, "sessionAuthors", sessionAuthorsCleared)
}

// sessionAuthorsExpired reports whether the incarnation's author set is past
// AskTTL, counted from its oldest entry.
func (g *Gateway) sessionAuthorsExpired(rec *SessionRecord, now time.Time) bool {
	return len(rec.SessionAuthors) > 0 && !rec.SessionAuthorsSince.IsZero() &&
		now.Sub(rec.SessionAuthorsSince) >= g.cfg.AskTTL
}

// holdsRequesterCopy reports whether the entry holds any of the copies the
// ask bound ages out: the requester, its attribution, the steer authors,
// and the request text.
func (ref TaskRef) holdsRequesterCopy() bool {
	return ref.Requester != nil || ref.Attribution != nil || len(ref.SteerAuthors) > 0 || ref.SteerAuthorsOverflow ||
		ref.Request != ""
}

// requesterExpired reports whether any history entry's requester copy is
// past AskTTL, from the scan's own view of the record.
func (g *Gateway) requesterExpired(rec *SessionRecord, now time.Time) bool {
	for _, ref := range rec.Tasks {
		if ref.holdsRequesterCopy() && !ref.StartedAt.IsZero() &&
			now.Sub(ref.StartedAt) >= g.cfg.AskTTL {
			return true
		}
	}
	return false
}

// buildRehydrationPrimer folds the conversation's earlier turns into a
// transcript primer for a fresh pod, the next incarnation's first input
// (the worker reads it as lib.EnvPrimerFile and puts it ahead of the new
// message). A pod spawned for a conversation that already has turns (after a
// reap, an eviction, or when the live pod could not take the turn) starts
// with no harness session to resume, so this is all it knows of the
// conversation before it; a reused pod's later turns resume the session this
// fed instead. Each human turn contributes what the user asked
// (TaskRef.Request, the copy session-state keeps until AskTTL) and each task
// its result from JetStream, labelled by who answered. current is the task
// the pod is being started for, left out so the new message is not
// replayed as history.
//
// It also returns the people behind every turn it replays (each turn's
// requester and steer authors), the mark when one of them is no longer on
// record, and the oldest turn's start. The pod reads what they said, so a
// delegation from it is checked against them too (seedSessionAuthors, in
// ensureSessionPod), exactly as a wake carries its parent's set. Task-stream retention bounds how far back this
// reaches, deliberately: a three-day-silent thread restarting with fresh
// context beats a bot that suddenly remembers June. Session files are cache;
// the stream is the record.
func (g *Gateway) buildRehydrationPrimer(ctx context.Context, rec *SessionRecord, current string) (primer string, authors []TaskRequester, unknown bool, since time.Time) {
	var turns []string
	var people [][]TaskRequester // each kept turn's requester and steer authors
	var started []time.Time
	var overflow []bool
	for _, ref := range rec.Tasks {
		if ref.ID == current {
			continue
		}
		// A turn whose people are no longer on record (cleared by the ask
		// bound, with its Request, or written before the fields existed)
		// is left out whole. The pod must not read text whose authors the
		// delegation check can't count; counting them as unknown instead
		// would refuse every delegation in any conversation older than
		// A2A_ASK_TTL, for good.
		if ref.Requester == nil {
			continue
		}
		var said, answered, ended string
		if ref.Role == "" && strings.TrimSpace(ref.Request) != "" {
			said = ref.Request
		}
		task, err := g.client.TasksGet(ctx, ref.Addressee, ref.ID)
		if err != nil && !isTaskNotFound(err) {
			// The stream didn't answer (a transport error, a consumer
			// refusal, the turn's budget running out): leave the turn out
			// rather than replay it as asked and never answered.
			continue
		}
		if err == nil {
			if art := task.Artifact(lib.ArtifactResult); art != nil && len(ref.Children) == 0 {
				// truncateRunes, not a byte cut: the primer is annotated onto
				// the next pod and marshalled to JSON on the way, where invalid
				// UTF-8 becomes U+FFFD rather than an error. spawn.go's outer
				// truncateRunes only guards the primer's tail; a byte cut here
				// lands mid-transcript and survives it.
				answered = truncateRunes(joinTextParts(art.Parts), primerTaskResultCap)
			}
			ended = primerTurnEnd(task)
			// A turn that asked to delegate ended with the hand-off line
			// ("delegated to <addressee>"), which is never a deliverable.
			// With a child it is skipped above, and the child's labelled
			// answer follows; refused, its real end is on the record.
			if state, reason, handOff := rec.handOffEnd(ref.ID, task.State); handOff {
				answered = ""
				ended = string(state) + ": " + truncateRunes(reason, primerTurnEndCap)
			}
		}
		// A task aged out of retention, or one that never produced a
		// result, still leaves what the user asked.
		if said == "" && strings.TrimSpace(answered) == "" && ended == "" {
			continue
		}
		people = append(people, append([]TaskRequester{*ref.Requester}, ref.SteerAuthors...))
		started = append(started, ref.StartedAt)
		overflow = append(overflow, ref.SteerAuthorsOverflow)
		// Each turn's text is fenced the way the wake's is: a user's line
		// that reads "You: ..." stays inside its own block and cannot pass
		// for an earlier answer.
		var turn strings.Builder
		if said != "" {
			turn.WriteString("\n" + primerFenced("The user said", said))
		}
		if strings.TrimSpace(answered) != "" {
			who := "You answered"
			switch {
			case ref.Role == taskRoleChild:
				who = "The " + ref.Addressee + " agent, which you delegated to, answered"
			case ref.Addressee == targetPlatform:
				// A turn from before /session routed this conversation.
				who = "The " + targetPlatform + " agent answered"
			}
			turn.WriteString("\n" + primerFenced(who, answered))
		}
		if ended != "" {
			turn.WriteString("\n" + primerFenced("That turn ended without finishing", ended))
		}
		turns = append(turns, turn.String())
	}
	if len(turns) == 0 {
		return "", nil, false, time.Time{}
	}
	primer, first := primerFromTurns(turns)
	// Only the turns the primer carries count: a turn dropped to fit is
	// text the pod never reads, and counting its people would overflow the
	// incarnation's set in a busy conversation and refuse every delegation.
	for i := first; i < len(turns); i++ {
		authors = append(authors, people[i]...)
		unknown = unknown || overflow[i]
		if !started[i].IsZero() && (since.IsZero() || started[i].Before(since)) {
			since = started[i]
		}
	}
	return primer, authors, unknown, since
}

// primerHeader and primerOmitted open the primer; the second only when
// earlier turns were dropped to fit.
const (
	primerHeader  = "Transcript primer, replayed from the task stream for this conversation:\n"
	primerOmitted = "\n(Earlier turns are omitted to fit.)\n"
)

// primerFromTurns joins the turns oldest first, keeping the newest that fit
// primerCap and dropping whole turns from the front, since a follow-up needs
// the most recent context most. spawn.go's truncateRunes stays as the
// backstop for a single turn larger than the cap. It returns the primer and
// the index of the first turn it kept, so the caller counts the people of
// the kept turns only.
func primerFromTurns(turns []string) (string, int) {
	budget := primerCap - len(primerHeader) - len(primerOmitted)
	start, size := len(turns), 0
	for start > 0 && size+len(turns[start-1]) <= budget {
		start--
		size += len(turns[start])
	}
	if start == len(turns) {
		start = len(turns) - 1 // one turn alone past the cap: the backstop cuts it
	}
	var b strings.Builder
	b.WriteString(primerHeader)
	if start > 0 {
		b.WriteString(primerOmitted)
	}
	for _, t := range turns[start:] {
		b.WriteString(t)
	}
	return b.String(), start
}

// primerTurnEnd says how a turn that didn't complete ended (failed,
// canceled or rejected), with the executor's reason when the terminal
// carried one, so a follow-up such as "did that work?" can be answered. A
// completed or still-open task says nothing.
func primerTurnEnd(task *lib.Task) string {
	switch task.State {
	case lib.StateFailed, lib.StateCanceled, lib.StateRejected:
	default:
		return ""
	}
	end := string(task.State)
	if task.FinalMessage != nil {
		if reason := strings.TrimSpace(joinTextParts(task.FinalMessage.Parts)); reason != "" {
			end += ": " + truncateRunes(reason, primerTurnEndCap)
		}
	}
	return end
}

// primerTurnEndCap bounds the reason quoted for a turn that didn't finish:
// enough for the executor's reason token and a line of detail.
const primerTurnEndCap = 300

// primerFenced is one turn of the primer: its label, then the text in a
// fence longer than any backtick run in it, so no line of the text can close
// the block (the wake's fencing, wakeFence and breakBacktickRuns).
func primerFenced(label, text string) string {
	text = breakBacktickRuns(strings.TrimSpace(text), wakeFenceMax-1)
	fence := wakeFence(text)
	return label + ":\n" + fence + "\n" + text + "\n" + fence + "\n"
}

// noFirstEventPastGrace is the one test for "this task has produced nothing
// past the first-event grace": nothing is on either of the task's replay
// subjects (streamEmpty; the events subject and the supervisor's, which the
// fold reads together) and the task is older than grace. A pure function of
// its arguments, so the heal, the reap scan's notice, and any later caller
// that has to tell a task nobody took from one in flight (a count of queued
// tasks, say) all draw the line in the same place. streamEmpty is true only
// on a read that found the subjects empty: the heal passes TasksGet's
// TaskNotFound, the notice taskStreamEmpty's answer, and a read that failed
// passes false, because a transport failure cannot rule out events. A task
// with no SubmittedAt has no age to judge and never qualifies. Detach is the
// caller's business: a detached task no longer holds the conversation, so
// neither the heal nor the notice looks at one.
func noFirstEventPastGrace(active *ActiveTask, streamEmpty bool, grace time.Duration, now time.Time) bool {
	return active != nil && streamEmpty &&
		!active.SubmittedAt.IsZero() && now.Sub(active.SubmittedAt) > grace
}

// taskStreamEmpty reports whether nothing is on either of a task's replay
// subjects in the retention window. It is the test the replay makes before
// it opens a consumer (TasksGet answers TaskNotFound exactly when this
// answers true), made with direct gets and no consumer, so a caller that
// only needs to know whether the task has a first event does not spend one
// of the TASKS stream's consumer slots on a five-second ephemeral. An error
// is the read failing, not the subjects being empty.
func (g *Gateway) taskStreamEmpty(ctx context.Context, addressee, taskID string) (bool, error) {
	stream, err := g.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		return false, fmt.Errorf("stream %s: %w", lib.TasksStream, err)
	}
	for _, subject := range lib.TaskReplaySubjects(addressee, taskID) {
		_, err := stream.GetLastMsgForSubject(ctx, subject)
		if err == nil {
			return false, nil
		}
		if !errors.Is(err, jetstream.ErrMsgNotFound) {
			return false, fmt.Errorf("newest message on %s: %w", subject, err)
		}
	}
	return true, nil
}

// noticeCeilingGraces bounds the no-first-event notice from above, in
// multiples of FirstEventGrace: a task older than this many graces is past
// the notice. The notice is for the placeholder somebody may still be
// watching, and that task is minutes old. A never-started task otherwise
// stays on its record until the SessionTTL prune (7 days by default), so
// without a ceiling the first reap pass after a rollout, or after an outage
// longer than the grace, would post into every conversation that wedged in
// the last week, one line per record, into threads that may be days cold.
// 3 leaves the steady-state notice (due at grace, landing within one
// reapInterval of it) two graces of slack for a slow or missed pass, and is
// 30m at the 10m default. The heal is not bounded: it runs inside a turn,
// where the conversation has just spoken.
const noticeCeilingGraces = 3

// withinNoticeCeiling reports whether a task submitted at submittedAt is
// still young enough, at now, for the no-first-event notice: no older than
// noticeCeilingGraces × grace. Inclusive at the bound.
func withinNoticeCeiling(submittedAt time.Time, grace time.Duration, now time.Time) bool {
	return now.Sub(submittedAt) <= noticeCeilingGraces*grace
}

// sessionPruneDue reports whether the reap scan deletes rec on this visit
// for having outlived SessionTTL: no pod (reaped, or never incarnated),
// silent past SessionTTL, and, if it still holds an active task, that task
// past its TaskDeadline (a stale or abandoned executor). reapSession prunes
// on it, and the no-first-event notice skips a record it answers true for,
// so a conversation is never told about a task whose record the same pass
// deletes.
func (g *Gateway) sessionPruneDue(rec *SessionRecord, now time.Time) bool {
	if g.cfg.SessionTTL <= 0 || rec.PodName != "" || rec.LastActivity.IsZero() ||
		now.Sub(rec.LastActivity) < g.cfg.SessionTTL {
		return false
	}
	if a := rec.ActiveTask; a != nil && !a.SubmittedAt.IsZero() &&
		now.Sub(a.SubmittedAt) < g.cfg.TaskDeadline {
		return false
	}
	return true
}

// firstEventOverdue is noFirstEventPastGrace for a caller holding only the
// record, bounded for the notice: when the active task is past the grace,
// inside the ceiling (withinNoticeCeiling), and the record is not about to
// be pruned (sessionPruneDue), it asks the stream whether the task has a
// first event (taskStreamEmpty, no consumer) and applies the test. A read
// and nothing else: no lock, no post, no write. A task outside that window
// is answered without touching the stream. A read that fails answers false.
func (g *Gateway) firstEventOverdue(ctx context.Context, rec *SessionRecord) bool {
	active := rec.ActiveTask
	now := time.Now()
	if active == nil || active.SubmittedAt.IsZero() ||
		now.Sub(active.SubmittedAt) <= g.cfg.FirstEventGrace ||
		!withinNoticeCeiling(active.SubmittedAt, g.cfg.FirstEventGrace, now) ||
		g.sessionPruneDue(rec, now) {
		return false
	}
	if g.noticeStreamReadHook != nil {
		g.noticeStreamReadHook(active.TaskID)
	}
	empty, err := g.taskStreamEmpty(ctx, rec.AddresseeFor(active.TaskID), active.TaskID)
	return err == nil && noFirstEventPastGrace(active, empty, g.cfg.FirstEventGrace, now)
}

// noticeNoFirstEvent tells a conversation, without waiting for it to speak,
// that its task has produced nothing past FirstEventGrace. The heal says the
// same thing, but only inside the next turn; a human who waits for the
// placeholder to move would otherwise hear nothing at all.
//
// It runs in the reap scan rather than on a timer per task: the scan
// already visits every record every reapInterval, survives a restart
// because the records are in KV, and is where the ask bound, the same
// kind of age bound on the same field, already lives. A per-task timer
// would be lost on a restart and need this scan to re-arm it anyway. The
// cost is latency: the line lands up to one reapInterval after the grace.
//
// It posts and does nothing else. No terminal and no release, for the
// heal's reasons (handleInbound): age alone is not evidence, and a first
// event that is merely late could still arrive and render. The release
// stays the next turn's, where the heal re-reads the stream first.
//
// Once per task, across restarts: the record carries the marker
// (ActiveTask.NoFirstEventNoticeAt), and it is written before the post, so
// a write that fails posts nothing and the next pass tries again, while a
// post that fails after the write is not repeated. At most once, because a
// line that repeats every minute is worse than one that is lost.
//
// Bounded above, too (firstEventOverdue): a task past noticeCeilingGraces ×
// the grace is not noticed, nor is one whose record this same pass prunes
// past SessionTTL. Both bounds apply to the read under the lock as well.
func (g *Gateway) noticeNoFirstEvent(ctx context.Context, rec *SessionRecord) {
	active := rec.ActiveTask
	if active == nil || active.Detached || !active.NoFirstEventNoticeAt.IsZero() {
		return
	}
	// The first read is outside the lock, as a filter that keeps the scan
	// from taking every overdue record's lock; it decides nothing on its
	// own. Under the lock the fresh record and the stream are both read
	// again, so a turn that moved the record on, or a first event that
	// landed (and was relayed) between the two, stops the post.
	if !g.firstEventOverdue(ctx, rec) {
		return
	}
	l := g.lockSession(rec.Key)
	l.Lock()
	defer l.Unlock()
	fresh, err := g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil || fresh.ActiveTask == nil ||
		fresh.ActiveTask.TaskID != active.TaskID || fresh.ActiveTask.Detached ||
		!fresh.ActiveTask.NoFirstEventNoticeAt.IsZero() || !g.firstEventOverdue(ctx, fresh) {
		return
	}
	fresh.ActiveTask.NoFirstEventNoticeAt = time.Now().UTC()
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("no-first-event notice: record write failed", "conversation", fresh.Key, "err", err)
		return
	}
	g.log.Info("no first event inside the grace; told the conversation",
		"taskId", active.TaskID, "conversation", fresh.Key, "addressee", fresh.AddresseeFor(active.TaskID),
		"age", time.Since(active.SubmittedAt).Round(time.Second), "grace", g.cfg.FirstEventGrace)
	g.post(fresh.Key, fmt.Sprintf(noFirstEventNotice, active.TaskID, g.cfg.FirstEventGrace))
}
