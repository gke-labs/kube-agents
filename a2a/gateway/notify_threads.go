package gateway

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gateway's record of the home-channel threads it opened with a keyed
// chat.notify post (an alert's), in NOTIFY_THREADS, and the check a thread
// delivery (lib.ScheduleDeliveryThread) is held to at ingress: a trigger may
// name only a thread on that record, in the home channel, and only from a
// source the install trusts for the mode (Config.TriggerThreadSources). So
// a trigger can't steer a post into a thread the gateway never opened, even
// one in a channel it serves. The record is in the bucket rather than in
// memory (notifyUpdates is per process) so a restart or a second replica
// still knows the thread.

const (
	// notifyThreadRecordTimeout bounds one record write, off the notify
	// worker so a slow bucket never delays the next post.
	notifyThreadRecordTimeout = 5 * time.Second
	// notifyThreadKeyHashLen is the hex length of a record key's hash.
	notifyThreadKeyHashLen = 40
)

// NotifyThread is one thread on the record: where it is and when the
// gateway opened it.
type NotifyThread struct {
	Platform string    `json:"platform"`
	ChatID   string    `json:"chatId"`
	ThreadID string    `json:"threadId"`
	PostedAt time.Time `json:"postedAt"`
}

// notifyThreadKey is a thread's record key: the platform, then a hash of
// the chat and the thread as notifyThreadID puts them, since a Chat thread
// name holds characters no KV key may.
func notifyThreadKey(platform, chatID, threadID string) string {
	sum := sha256.Sum256([]byte(chatID + "\n" + threadID))
	return platform + scheduledKeySeparator + hex.EncodeToString(sum[:])[:notifyThreadKeyHashLen]
}

// notifyThreadID is thread in the one form the record keeps and compares,
// and whether it is a thread of chat on platform at all: on Slack the bare
// ts (a channel-qualified thread must name chat), on Google Chat the full
// thread name inside chat (a bare id is put inside it).
func notifyThreadID(platform, chatID, thread string) (string, bool) {
	switch platform {
	case lib.NotifyPlatformSlack:
		if !slackThreadOK(thread, chatID) {
			return "", false
		}
		return slackThreadTS(thread), true
	case lib.NotifyPlatformGchat:
		if !strings.Contains(thread, "/") {
			thread = chatID + gchatThreadsToken + thread
		}
		if !inHome(thread, chatID) {
			return "", false
		}
		return thread, true
	}
	return "", false
}

// notifyThreadStore is the NOTIFY_THREADS store, every operation inside ctx.
func (g *Gateway) notifyThreadStore(ctx context.Context) scheduledStore {
	return scheduledStore{g: g, ctx: ctx, bucket: lib.NotifyThreadsBucket}
}

// recordNotifyThread puts the thread a keyed home post opened or landed in
// on the record, in the background. A failed write is logged and costs only
// a later thread delivery into it, which is refused.
func (g *Gateway) recordNotifyThread(platform, chatID, landed string) {
	thread, ok := notifyThreadID(platform, chatID, landed)
	if !ok {
		g.log.Warn("notify thread not recorded: not a thread of the home channel", "platform", platform, "chatId", chatID, "thread", landed)
		return
	}
	rec := NotifyThread{Platform: platform, ChatID: chatID, ThreadID: thread, PostedAt: g.scheduleClock().UTC()}
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), notifyThreadRecordTimeout)
		defer cancel()
		if err := g.notifyThreadStore(ctx).putNotifyThread(rec); err != nil {
			g.log.Warn("notify thread not recorded", "platform", platform, "chatId", chatID, "thread", thread, "err", err)
			return
		}
		g.log.Debug("notify thread recorded", "platform", platform, "chatId", chatID, "thread", thread)
	}()
}

func (st scheduledStore) putNotifyThread(t NotifyThread) error {
	data, err := json.Marshal(t)
	if err != nil {
		return err
	}
	return st.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		_, err := kv.Put(ctx, notifyThreadKey(t.Platform, t.ChatID, t.ThreadID), data)
		return err
	})
}

// notifyThreadKnown reports whether the thread is on the record, as the
// same platform, chat and thread.
func (st scheduledStore) notifyThreadKnown(platform, chatID, threadID string) (bool, error) {
	var known bool
	err := st.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		e, err := kv.Get(ctx, notifyThreadKey(platform, chatID, threadID))
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			known = false
			return nil
		}
		if err != nil {
			return err
		}
		var t NotifyThread
		if err := json.Unmarshal(e.Value(), &t); err != nil {
			return fmt.Errorf("notify thread record unreadable: %w", err)
		}
		known = t.Platform == platform && t.ChatID == chatID && t.ThreadID == threadID
		return nil
	})
	return known, err
}

// threadDeliveryRefusal is why a thread delivery from source is refused at
// ingress, with the refusal's reason, or "" when it is taken. d has passed
// lib.ValidateScheduleDelivery. The checks, each refused on its own:
//   - source is one the install trusts for the mode;
//   - a chat.notify route serves d's platform and has a home channel, and
//     d's chat is that home channel;
//   - d's thread is a thread of that channel, on the gateway's record.
//
// A record that can't be read refuses the submission as unavailable, worth
// a retry; never taken on trust.
func (g *Gateway) threadDeliveryRefusal(ctx context.Context, source string, d lib.ScheduleDelivery) (reason, detail string) {
	if !slices.Contains(g.cfg.TriggerThreadSources, source) {
		return lib.TriggerRefusedInvalid, fmt.Sprintf("trigger source %q may not name a thread delivery", source)
	}
	reporter, home := g.homeReporter()
	if reporter == nil {
		return lib.TriggerRefusedInvalid, "a thread delivery needs a home channel, and the install has none"
	}
	if reporter.Backend() != d.Platform {
		return lib.TriggerRefusedInvalid, fmt.Sprintf("thread delivery platform %q is not the home channel's %q", d.Platform, reporter.Backend())
	}
	if d.ChatID != home {
		return lib.TriggerRefusedInvalid, "thread delivery chat is not the home channel"
	}
	thread, ok := notifyThreadID(d.Platform, d.ChatID, d.ThreadID)
	if !ok {
		return lib.TriggerRefusedInvalid, "thread delivery thread is not a thread of the home channel"
	}
	known, err := g.notifyThreadStore(ctx).notifyThreadKnown(d.Platform, d.ChatID, thread)
	if err != nil {
		g.log.Warn("notify thread record unavailable; thread delivery refused", "err", err)
		return lib.TriggerRefusedUnavailable, "the gateway's thread record could not be read"
	}
	if !known {
		return lib.TriggerRefusedInvalid, "thread delivery thread is not one the gateway opened"
	}
	return "", ""
}

// postThreadDelivery posts a thread delivery's report into the thread d
// names, then keeps it there under d's incident source. The ingress check
// held d to the home channel and the record; the home channel is checked
// again here, since it can change between the submission and the post, and
// a thread outside the channel the route posts into now is never posted.
// SCHEDULE_THREADS is not read: the thread is d's, not the day's.
func (g *Gateway) postThreadDelivery(ctx context.Context, reporter homeReporter, home string, d lib.ScheduleDelivery, report ScheduledReport) (homePost, error) {
	logArgs := []any{"jobKey", report.JobKey, "taskId", report.TaskID, "platform", d.Platform, "chatId", d.ChatID}
	if reporter.Backend() != d.Platform || home != d.ChatID {
		return homePost{}, fmt.Errorf("the home channel is no longer the thread delivery's %s %s", d.Platform, d.ChatID)
	}
	thread, ok := notifyThreadID(d.Platform, d.ChatID, d.ThreadID)
	if !ok {
		return homePost{}, errors.New("the thread delivery's thread is not a thread of the home channel")
	}
	post, err := reporter.postHomeReport(ctx, home, thread, report.Text)
	if err != nil {
		return homePost{}, err
	}
	if post.ThreadID == "" {
		post.ThreadID = thread
	}
	g.keepIncidentAs(ctx, post, report, d.IncidentSourceOf(), append(logArgs, "thread", thread))
	return post, nil
}
