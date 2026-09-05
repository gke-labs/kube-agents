package gateway

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
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

// reapLoop enforces the idle TTL — a session silent past the TTL loses its
// pod — and the ask bound (boundAskCopy), which runs on every record the
// scan visits, pod or no pod. Nothing is saved first, because the stream
// already has everything — that's the whole point of the transcript of
// record. The KV entry stays, holding the contextId.
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
	recs, err := g.reg.Sessions(ctx)
	if err != nil {
		g.log.Error("reap: session scan failed", "err", err)
		return
	}
	for _, rec := range recs {
		g.boundAskCopy(ctx, rec)
		if rec.PodName == "" {
			continue // nothing incarnated (the Hermes-first world, or already reaped)
		}
		if rec.ActiveTask != nil && !rec.ActiveTask.Detached {
			continue // never delete a pod out from under a running task
		}
		if time.Since(rec.LastActivity) < g.cfg.IdleTTL {
			continue
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
			time.Since(fresh.LastActivity) < g.cfg.IdleTTL {
			l.Unlock()
			continue
		}
		// A detached task does not exempt the session, so reap may delete a
		// pod whose harness is still working — the supervisor rule is what
		// keeps that from being a silent stop: its terminal `canceled` goes
		// on the stream before the pod goes. A publish failure keeps the
		// pod (and the reap retries next cycle) rather than stranding the
		// task non-terminal for the retention window.
		if !g.closeDetachedBeforeDelete(ctx, fresh) {
			l.Unlock()
			continue
		}
		if g.spawner != nil {
			if err := g.spawner.Delete(ctx, fresh.PodName); err != nil {
				g.log.Error("reap: pod delete failed", "pod", fresh.PodName, "err", err)
				l.Unlock()
				continue
			}
		}
		g.log.Info("reaped idle session", "session", fresh.Key, "pod", fresh.PodName)
		// The pod was an incarnation, not the identity: contextId persists.
		fresh.PodName = ""
		if err := g.reg.Put(ctx, fresh); err != nil {
			g.log.Error("reap: record write failed", "session", fresh.Key, "err", err)
		}
		l.Unlock()
	}
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
// task's lifecycle.
func (g *Gateway) boundAskCopy(ctx context.Context, rec *SessionRecord) {
	active := rec.ActiveTask
	if active == nil || active.Ask == "" || active.SubmittedAt.IsZero() ||
		time.Since(active.SubmittedAt) < g.cfg.AskTTL {
		return
	}
	l := g.lockSession(rec.Key)
	l.Lock()
	defer l.Unlock()
	// Same discipline as the reap: re-check on the fresh record under the
	// lock, and clear only the copy the scan saw expire.
	fresh, err := g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil || fresh.ActiveTask == nil ||
		fresh.ActiveTask.TaskID != active.TaskID || fresh.ActiveTask.Ask == "" ||
		fresh.ActiveTask.SubmittedAt.IsZero() ||
		time.Since(fresh.ActiveTask.SubmittedAt) < g.cfg.AskTTL {
		return
	}
	fresh.ActiveTask.Ask = ""
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("ask bound: record write failed", "session", fresh.Key, "err", err)
		return
	}
	g.log.Info("ask bound: cleared an ask copy past its TTL", "session", fresh.Key, "taskId", fresh.ActiveTask.TaskID)
}

// buildRehydrationPrimer folds the context's tasks from JetStream into a
// transcript primer for a fresh pod — the next incarnation's first input.
// Task-stream retention bounds how far back this reaches, deliberately: a
// three-day-silent thread restarting with fresh context beats a bot that
// suddenly remembers June. Session files are cache; the stream is the
// record.
func (g *Gateway) buildRehydrationPrimer(ctx context.Context, rec *SessionRecord) string {
	var b strings.Builder
	b.WriteString("Transcript primer, replayed from the task stream for this conversation:\n")
	found := 0
	for _, ref := range rec.Tasks {
		task, err := g.client.TasksGet(ctx, ref.Addressee, ref.ID)
		if err != nil {
			continue // aged out of retention, or never produced events
		}
		found++
		fmt.Fprintf(&b, "\n--- task %s (%s)\n", task.ID, task.State)
		if art := task.Artifact(lib.ArtifactResult); art != nil {
			text := joinTextParts(art.Parts)
			if len(text) > primerTaskResultCap {
				text = text[:primerTaskResultCap] + "…"
			}
			b.WriteString(text)
			b.WriteString("\n")
		}
	}
	if found == 0 {
		return ""
	}
	return b.String()
}
