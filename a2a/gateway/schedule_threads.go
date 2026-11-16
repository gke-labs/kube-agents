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

// One thread per schedule per UTC day (docs/designs/scheduling-on-profiles.md
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

func (g *Gateway) threadStore(ctx context.Context) scheduledStore {
	return scheduledStore{g: g, ctx: context.WithoutCancel(ctx), bucket: lib.ScheduleThreadsBucket}
}

// postScheduledReport posts report through reporter into the day's thread of
// its job key, opening the thread when the day has none, then keeps the
// report against the thread it landed in. A thread that can't be read or
// kept costs the thread, never the report: it posts as a new message.
func (g *Gateway) postScheduledReport(ctx context.Context, reporter homeReporter, home string, report ScheduledReport) (homePost, error) {
	key := scheduleThreadKey(report.JobKey, g.scheduleClock())
	logArgs := []any{"jobKey", report.JobKey, "taskId", report.TaskID, "threadKey", key}
	threads := g.threadStore(ctx)

	thread, rev, owned, err := g.dayThread(ctx, threads, key, report.TaskID)
	var post homePost
	switch {
	case err != nil:
		g.log.Warn("schedule thread unavailable; posting as a new message", append(logArgs, "err", err)...)
		post, err = reporter.postHomeReport(ctx, home, "", report.Text)
	case thread != nil && thread.ThreadID != "" && thread.ChatID == home:
		post, err = reporter.postHomeReport(ctx, home, thread.ThreadID, report.Text)
	case owned:
		post, err = reporter.postHomeReport(ctx, home, "", report.Text)
		if err != nil {
			// Unclaimed, so the day's next report opens the thread.
			if derr := threads.do(func(ctx context.Context, kv jetstream.KeyValue) error {
				return kv.Delete(ctx, key, jetstream.LastRevision(rev))
			}); derr != nil {
				g.log.Warn("schedule thread claim not released", append(logArgs, "err", derr)...)
			}
			break
		}
		landed := post.ThreadID
		if landed == "" {
			landed = post.MessageID
		}
		day := ScheduleThread{Platform: post.Platform, ChatID: post.ChatID, ThreadID: landed, CreatedAt: g.scheduleClock().UTC()}
		if _, perr := putScheduleThread(threads, key, day, rev); perr != nil {
			g.log.Warn("schedule thread not kept; the day's next report opens another", append(logArgs, "err", perr)...)
		}
	default:
		// A thread in another channel (the home channel changed today), or
		// a claim whose post never finished: a new message, kept nowhere.
		if ctx.Err() != nil {
			// The wait for another post's thread used this post's time;
			// nothing was queued, so this is a failed post, tried again.
			return homePost{}, fmt.Errorf("waited out the day's thread claim on %s: %w", key, ctx.Err())
		}
		g.log.Info("schedule thread not usable; posting as a new message", logArgs...)
		post, err = reporter.postHomeReport(ctx, home, "", report.Text)
	}
	if err != nil {
		return post, err
	}
	g.keepIncident(ctx, post, report, logArgs)
	return post, nil
}

// dayThread reads key's thread, or claims key for taskID when it has none
// (owned true, rev the claim's revision). A key another post has claimed is
// read again until that post records its thread, for as long as ctx lasts;
// a claim older than a post's timeout is taken over.
func (g *Gateway) dayThread(ctx context.Context, threads scheduledStore, key, taskID string) (thread *ScheduleThread, rev uint64, owned bool, err error) {
	for {
		thread, rev, err = getScheduleThread(threads, key)
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
		if g.scheduleClock().Sub(thread.CreatedAt) > scheduledPostTimeout {
			// A claim older than any post: its post died without
			// recording a thread or releasing the claim. Taken over, by
			// revision, so two posts can't both take it.
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
			return thread, rev, false, nil
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

// getScheduleThread reads key's thread and its revision, nil when none.
func getScheduleThread(threads scheduledStore, key string) (t *ScheduleThread, rev uint64, err error) {
	err = threads.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		e, err := kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			t, rev = nil, 0
			return nil
		}
		if err != nil {
			return err
		}
		var v ScheduleThread
		if err := json.Unmarshal(e.Value(), &v); err != nil {
			return fmt.Errorf("schedule thread %s does not decode: %w", key, err)
		}
		t, rev = &v, e.Revision()
		return nil
	})
	return t, rev, err
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
