package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// One thread per schedule per UTC day of the run's finish (docs/designs/scheduling-on-profiles.md
// section 4, item 3): the first report of a job key on a UTC day opens a new
// home-channel message, and the day's later reports reply under it. Which
// thread is the day's lives in SCHEDULE_THREADS, keyed <jobKey>.<YYYY-MM-DD>,
// so it survives a gateway failover. The first post of a day claims the key
// with a create before it posts, so two replicas overlapping on the same
// first post don't open two threads: the one whose create loses waits for
// the winner's thread and replies under it.

const (
	// scheduleThreadDay is the UTC date layout of a thread key's second half.
	scheduleThreadDay = "2006-01-02"
	// scheduleThreadWait is how often a post that lost the day's claim reads
	// the key again for the winner's thread, while its own post's time lasts.
	scheduleThreadWait = 200 * time.Millisecond
	// scheduleThreadShare divides a post's time: the day's thread is read
	// in at most a third of it, and the post keeps the rest.
	scheduleThreadShare = 3
	// scheduleClaimSkew is how far this gateway's clock may run ahead of
	// the server's before a live claim reads as stale.
	scheduleClaimSkew = 30 * time.Second
)

// ScheduleThread is a day's thread in SCHEDULE_THREADS. A claim (Claim set,
// ThreadID empty) is the first post of the day in flight.
type ScheduleThread struct {
	Platform  string    `json:"platform,omitempty"`
	ChatID    string    `json:"chatId,omitempty"`
	ThreadID  string    `json:"threadId,omitempty"`
	CreatedAt time.Time `json:"createdAt"`
	// Claim is the task whose post is opening the thread.
	Claim string `json:"claim,omitempty"`
}

// reportIncidentWriter keeps a posted report against the thread it landed in
// (the incident store a reply in that thread is answered from, read by
// a2a/hermes-bridge/incident.go as <untrusted_report>). Called on every
// post with the stored, truncated report.
type reportIncidentWriter interface {
	keepReportIncident(ctx context.Context, platform, chatID, threadID, report string) error
}

// scheduleThreadKey is jobKey's thread key for the UTC day of at.
func scheduleThreadKey(jobKey string, at time.Time) string {
	return jobKey + scheduledKeySeparator + at.UTC().Format(scheduleThreadDay)
}

// threadStore is the SCHEDULE_THREADS store, every operation inside ctx.
func (g *Gateway) threadStore(ctx context.Context) scheduledStore {
	return scheduledStore{g: g, ctx: ctx, bucket: lib.ScheduleThreadsBucket}
}

// errThreadClaimBusy is a day's first post still in flight on another run:
// this post waits, as a failed post, for the redelivery that finds the
// thread, rather than open a second one.
var errThreadClaimBusy = errors.New("the day's first post is still opening its thread")

// postScheduledReport posts report through reporter into the day's thread of
// its job key, opening the thread when the day has none, then keeps the
// report against the thread it landed in. The day is the run's finish time
// in UTC, so a retry after midnight stays in its own day. A thread that
// can't be read or kept costs the thread, never the report: it posts as a
// new message. The thread's reads spend at most scheduleThreadShare of the
// post's time, and its writes after the post run in scheduledKVReserve.
func (g *Gateway) postScheduledReport(ctx context.Context, reporter homeReporter, home string, report ScheduledReport) (homePost, error) {
	key := scheduleThreadKey(report.JobKey, report.FinishedAt)
	logArgs := []any{"jobKey", report.JobKey, "taskId", report.TaskID, "threadKey", key}

	lookupCtx, cancel := context.WithTimeout(ctx, threadLookupBudget(ctx))
	thread, rev, owned, err := g.dayThread(lookupCtx, g.threadStore(lookupCtx), key, report.TaskID)
	cancel()
	if errors.Is(err, errThreadClaimBusy) {
		return homePost{}, fmt.Errorf("%s: %w", key, err)
	}
	if ctx.Err() != nil {
		// The thread's reads used the post's time and nothing was queued:
		// a failed post, tried again, and never one whose outcome is unknown.
		return homePost{}, fmt.Errorf("the day's thread on %s was not read in time: %w", key, ctx.Err())
	}
	// The writes after the post, bounded so the post's outcome is still
	// recorded inside scheduledPostStale.
	writeCtx, cancelWrite := context.WithTimeout(context.WithoutCancel(ctx), scheduledKVReserve/2)
	defer cancelWrite()
	threads := g.threadStore(writeCtx)

	var post homePost
	switch {
	case err != nil:
		g.log.Warn("schedule thread unavailable; posting as a new message", append(logArgs, "err", err)...)
		post, err = reporter.postHomeReport(ctx, home, "", report.Text, report.Chat)
	case thread != nil && thread.ThreadID != "" && thread.ChatID == home:
		post, err = reporter.postHomeReport(ctx, home, thread.ThreadID, report.Text, report.Chat)
		if err != nil && !errors.Is(err, errPostOutcomeUnknown) && ctx.Err() == nil {
			// The thread refused (no longer a thread of the channel, or
			// the platform turned the reply down): once as a new message,
			// which becomes the day's thread.
			g.log.Warn("schedule thread refused the reply; opening a new one", append(logArgs, "thread", thread.ThreadID, "err", err)...)
			if post, err = reporter.postHomeReport(ctx, home, "", report.Text, report.Chat); err == nil {
				g.keepDayThread(threads, key, post, rev, logArgs)
			}
		}
	case owned:
		post, err = reporter.postHomeReport(ctx, home, "", report.Text, report.Chat)
		switch {
		case errors.Is(err, errPostOutcomeUnknown):
			// It may yet land: the claim stays, and goes stale.
		case err != nil:
			// Unclaimed, so the day's next report opens the thread.
			if derr := threads.do(func(ctx context.Context, kv jetstream.KeyValue) error {
				return kv.Delete(ctx, key, jetstream.LastRevision(rev))
			}); derr != nil {
				g.log.Warn("schedule thread claim not released", append(logArgs, "err", derr)...)
			}
		default:
			g.keepDayThread(threads, key, post, rev, logArgs)
		}
	default:
		// A thread in another channel: the home channel changed today. A
		// new message, kept nowhere, so the day's thread stays as it was.
		g.log.Info("schedule thread is in another channel; posting as a new message", append(logArgs, "threadChat", thread.ChatID)...)
		post, err = reporter.postHomeReport(ctx, home, "", report.Text, report.Chat)
	}
	if err != nil {
		return post, err
	}
	g.keepIncident(ctx, post, report, logArgs)
	return post, nil
}

// threadLookupBudget is the share of ctx's time the day's thread may be read
// in: scheduleThreadShare of what is left, so the post keeps the rest.
func threadLookupBudget(ctx context.Context) time.Duration {
	left := scheduledPostTimeout
	if dl, ok := ctx.Deadline(); ok {
		left = time.Until(dl)
	}
	return left / scheduleThreadShare
}

// keepDayThread writes post's thread as the day's, over revision rev.
func (g *Gateway) keepDayThread(threads scheduledStore, key string, post homePost, rev uint64, logArgs []any) {
	landed := post.ThreadID
	if landed == "" {
		landed = post.MessageID
	}
	day := ScheduleThread{Platform: post.Platform, ChatID: post.ChatID, ThreadID: landed, CreatedAt: g.scheduleClock().UTC()}
	if _, err := putScheduleThread(threads, key, day, rev); err != nil {
		g.log.Warn("schedule thread not kept; the day's next report opens another", append(logArgs, "err", err)...)
	}
}

// dayThread reads key's thread, or claims key for taskID when it has none
// (owned true, rev the claim's revision). A key another post has claimed is
// read again until that post records its thread, for as long as ctx lasts,
// and is errThreadClaimBusy after. A claim the server stored longer ago than
// scheduledPostStale plus scheduleClaimSkew is taken over.
func (g *Gateway) dayThread(ctx context.Context, threads scheduledStore, key, taskID string) (thread *ScheduleThread, rev uint64, owned bool, err error) {
	for {
		var stored time.Time
		thread, rev, stored, err = getScheduleThread(threads, key)
		if err != nil {
			return nil, 0, false, err
		}
		if thread == nil {
			claim := ScheduleThread{Claim: taskID, CreatedAt: g.scheduleClock().UTC()}
			rev, err = putScheduleThread(threads, key, claim, 0)
			if err == nil {
				return &claim, rev, true, nil
			}
			if !errors.Is(err, jetstream.ErrKeyExists) {
				return nil, 0, false, err
			}
			continue // another post claimed it first
		}
		if thread.ThreadID != "" {
			return thread, rev, false, nil
		}
		if thread.Claim == taskID {
			// This run's own claim: a create retried after it landed, or
			// a redelivery of a post that failed before releasing it.
			return thread, rev, true, nil
		}
		if g.claimClock().Sub(stored) > scheduledPostStale+scheduleClaimSkew {
			// A claim older than any post, by the time the server stored
			// it: its post died without recording a thread or releasing
			// the claim. Taken over, by revision, so two posts can't both.
			claim := ScheduleThread{Claim: taskID, CreatedAt: g.scheduleClock().UTC()}
			if next, err := putScheduleThread(threads, key, claim, rev); err == nil {
				return &claim, next, true, nil
			} else if !isMintedAlready(err) {
				return nil, 0, false, err
			}
			continue // another post took it over first
		}
		select {
		case <-ctx.Done():
			return thread, rev, false, errThreadClaimBusy
		case <-time.After(scheduleThreadWait):
		}
	}
}

// keepIncident writes the posted report against where it landed, logged
// when it fails: the post stands either way.
func (g *Gateway) keepIncident(ctx context.Context, post homePost, report ScheduledReport, logArgs []any) {
	if g.reportIncidents == nil {
		g.log.Debug("no incident writer; the posted report is not kept for replies", logArgs...)
		return
	}
	thread := post.ThreadID
	if thread == "" {
		thread = post.MessageID
	}
	if err := g.reportIncidents.keepReportIncident(context.WithoutCancel(ctx), post.Platform, post.ChatID, thread, report.Text); err != nil {
		g.log.Warn("posted report not kept for replies in its thread", append(logArgs, "err", err)...)
	}
}

// getScheduleThread reads key's thread, its revision and when the server
// stored it, nil when none.
func getScheduleThread(threads scheduledStore, key string) (t *ScheduleThread, rev uint64, stored time.Time, err error) {
	err = threads.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		e, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			t, rev, stored = nil, 0, time.Time{}
			return nil
		}
		if err != nil {
			return err
		}
		var v ScheduleThread
		if err := json.Unmarshal(e.Value(), &v); err != nil {
			return fmt.Errorf("schedule thread %s does not decode: %w", key, err)
		}
		t, rev, stored = &v, e.Revision(), e.Created()
		return nil
	})
	return t, rev, stored, err
}

// putScheduleThread writes t at key: a create when rev is 0, else an update
// of that revision.
func putScheduleThread(threads scheduledStore, key string, t ScheduleThread, rev uint64) (next uint64, err error) {
	data, err := json.Marshal(t)
	if err != nil {
		return 0, err
	}
	err = threads.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		var e error
		if rev == 0 {
			next, e = kv.Create(ctx, key, data)
		} else {
			next, e = kv.Update(ctx, key, data, rev)
		}
		return e
	})
	return next, err
}
