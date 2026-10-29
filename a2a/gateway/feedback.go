package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Answer feedback: a 👍 or 👎 on the final answer of an ask.
//
// The gateway marks the one message of a chain that is its answer
// (OutMessage.Feedback, set by the relay where the DeliverableObserver is
// handed the same result), and a backend that draws rating controls draws
// them there; today that is Slack, with its own feedback buttons. A rating
// from a sender the gateway would not take a turn from changes nothing.
//
// Each rating is counted (answer_feedback_total) and kept in the feedback
// bucket: the ask that started the rated chain, the answer and the ids, and
// for a 👎 an optional note from the rater, and nothing else of the thread.
// A rater has one rating per answer, so a 👍 after a 👎 replaces it (and its
// note, which said what went wrong), and the reverse. The
// bucket's max age is the retention, set where the deployment provisions it,
// and the gateway refuses to keep anything in a bucket without one. The
// gateway's user is the only one that reads or writes its records (the
// provision Job and the seed tooling can see its keys); an operator reads
// it through the gateway binary (`/gateway feedback export`, feedback_export.go),
// which writes each rating as a draft eval case: a 👎 as a case to fix, a
// 👍 as a good answer to keep. Logs and the metric carry ids and the
// rating, never the text.
const (
	// FeedbackUp and FeedbackDown are the two ratings, as the metric's
	// rating label and the record carry them.
	FeedbackUp   = "up"
	FeedbackDown = "down"

	// The caps on what a rating keeps, so one rating cannot grow the bucket
	// past what it is sized for: the ask, the answer and the note clipped
	// in bytes, at a rune boundary (truncateRunes). The ask arrives already
	// held to the session record's copy (wakeAskCap), so its cap here is a
	// backstop.
	feedbackAskMax    = 4000
	feedbackAnswerMax = 16000
	feedbackNoteMax   = 2000
	// feedbackNoteChars is how many characters a note may be, as a backend
	// asks for it; feedbackNoteMax holds that many in any script.
	feedbackNoteChars = 500

	// feedbackKeySep joins a record's task id and rater in its key.
	feedbackKeySep = "."

	// feedbackRecorderDeadline bounds one rating's store calls, and
	// feedbackMergeAttempts how many times a write that raced another is
	// tried again (mergeFeedback); two writers race at most.
	feedbackRecorderDeadline = 10 * time.Second
	feedbackMergeAttempts    = 5
	// feedbackLockStripes is how many locks the ratings are serialised on
	// (feedbackDesk.lockKey).
	feedbackLockStripes = 64
	// feedbackCardAnswersMax is how many asks the desk remembers having
	// rated a card answer for (claimCardAnswer), oldest forgotten first.
	feedbackCardAnswersMax = 1024

	// feedbackRetryInterval is how often the gateway looks again for a
	// feedback bucket it did not find at start: on an upgrade the provision
	// Job that creates it can finish after the new gateway starts.
	feedbackRetryInterval = time.Minute
	// feedbackRetries is how many times it looks before it stops: the
	// upgrade's Job finishes within minutes, and an install whose NATS
	// server never grants the gateway the bucket (an older operator's
	// nats.conf) would otherwise log a refused request every minute.
	feedbackRetries = 30

	// feedbackDay converts the bucket's max age to the days the rater is
	// told it is kept.
	feedbackDay = 24 * time.Hour
)

var (
	// errFeedbackUnverified is a rating from a sender the gateway would not
	// take a turn from.
	errFeedbackUnverified = errors.New("the rater is not a verified sender")
	// errFeedbackContended is a record that kept changing under every
	// attempt to merge into it.
	errFeedbackContended = errors.New("the record kept changing; giving up")
	// errFeedbackNotKept is a note for a 👎 the bucket does not hold: its
	// write failed, or a 👍 replaced it.
	errFeedbackNotKept = errors.New("no 👎 is kept for this answer and rater")
)

// FeedbackRating is one rating as a backend hands it to the gateway.
type FeedbackRating struct {
	// Platform is the metric's platform label (platformSlack).
	Platform string
	// Backend is the ingress the rater is verified against, as a turn's
	// sender is (resolvePrincipal).
	Backend string
	// Rater is the backend's id for the person who rated.
	Rater        string
	Conversation string
	// MessageID is the rated message.
	MessageID string
	// TaskID is the task id the answer was marked with (OutMessage.Feedback).
	TaskID string
	Rating string
	// Answer is the rated message's text.
	Answer string
}

// FeedbackRecord is a kept rating, as the feedback bucket holds it.
type FeedbackRecord struct {
	Rating       string    `json:"rating"`
	Note         string    `json:"note,omitempty"`
	Platform     string    `json:"platform"`
	Rater        string    `json:"rater"`
	Conversation string    `json:"conversation"`
	MessageID    string    `json:"messageId"`
	TaskID       string    `json:"taskId"`
	ContextID    string    `json:"contextId,omitempty"`
	Ask          string    `json:"ask,omitempty"`
	Answer       string    `json:"answer,omitempty"`
	RatedAt      time.Time `json:"ratedAt"`
}

// FeedbackRecorder is what the gateway offers a backend that draws rating
// controls: whether it takes ratings from a sender, record a rating, add the
// rater's note to their kept 👎, how many days a rating is kept (for the
// rater to be told), and how many of each rating were given since a time.
// Rate reports whether the kept record holds the rated chain's ask, so the
// rater is told only what is kept.
// Every call is safe from any goroutine and bounded by its own deadline.
type FeedbackRecorder interface {
	Admits(backend, rater string) bool
	Rate(r FeedbackRating) (askKept bool, err error)
	Note(r FeedbackRating, note string) error
	KeptDays() int
	Tally(ctx context.Context, since time.Time) (up, down int, err error)
}

// FeedbackSink is the optional extension an Adapter implements to receive
// the gateway's FeedbackRecorder, once the feedback bucket is found. An
// adapter that is never handed one draws no rating controls.
type FeedbackSink interface {
	SetFeedbackRecorder(r FeedbackRecorder)
}

// feedbackDesk is the gateway's FeedbackRecorder.
type feedbackDesk struct {
	g   *Gateway
	ttl time.Duration
	// stripes serialise the ratings on one answer by one rater, so a 👍
	// tapped while that rater's 👎 is still being written waits for it and
	// then takes it back, instead of finding nothing to take back. Striped
	// by the key's hash rather than one lock per key, so the set stays
	// fixed however many answers are rated; two unrelated ratings sharing
	// a stripe only wait for each other.
	stripes [feedbackLockStripes]sync.Mutex

	// cardAnswers are the chain roots a card answer was rated for, in
	// this process, oldest first (claimCardAnswer).
	cardMu      sync.Mutex
	cardAnswers map[string]bool
	cardOrder   []string
}

// claimCardAnswer reports whether root's card answer may be rated, and
// records it so the next is not: one rated card answer per ask. The record
// is this process's, so a card answer after a gateway restart can be rated
// a second time.
func (d *feedbackDesk) claimCardAnswer(root string) bool {
	d.cardMu.Lock()
	defer d.cardMu.Unlock()
	if d.cardAnswers[root] {
		return false
	}
	if d.cardAnswers == nil {
		d.cardAnswers = map[string]bool{}
	}
	d.cardAnswers[root] = true
	d.cardOrder = append(d.cardOrder, root)
	if len(d.cardOrder) > feedbackCardAnswersMax {
		delete(d.cardAnswers, d.cardOrder[0])
		d.cardOrder = d.cardOrder[1:]
	}
	return true
}

// lockKey takes the rating lock for key and returns its release.
func (d *feedbackDesk) lockKey(key string) func() {
	h := fnv.New32a()
	_, _ = h.Write([]byte(key))
	mu := &d.stripes[h.Sum32()%feedbackLockStripes]
	mu.Lock()
	return mu.Unlock
}

// startFeedback turns answer feedback on (tryFeedback), or, when it cannot
// yet, tries again every feedbackRetryInterval until it can, ctx ends or
// feedbackRetries tries have failed.
// The first try is made before it returns, so the relay marks every answer
// alike from the start on an install that already has the bucket.
func (g *Gateway) startFeedback(ctx context.Context) {
	if g.cfg.AnswerFeedbackOff {
		g.log.Info("answer feedback is off: turned off on this install (slack.answerFeedback)")
		return
	}
	if g.tryFeedback(ctx, false) {
		return
	}
	go func() {
		ticker := time.NewTicker(feedbackRetryInterval)
		defer ticker.Stop()
		for range feedbackRetries {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if g.tryFeedback(ctx, true) {
					return
				}
			}
		}
		g.log.Info("answer feedback is off: still no feedback bucket the gateway can read; no longer looking", "bucket", lib.FeedbackBucket)
	}()
}

// tryFeedback finds the feedback bucket and, when it has a max age, turns
// answer feedback on: answers are marked from here on, and the adapter is
// handed the recorder. A missing bucket (a deployment that predates it, a
// provision Job not yet finished, or a test) or one that would keep ratings
// forever leaves it off. A retry (quiet) logs why only at debug.
func (g *Gateway) tryFeedback(ctx context.Context, quiet bool) bool {
	off := func(why string, args ...any) bool {
		args = append([]any{"bucket", lib.FeedbackBucket}, args...)
		if quiet {
			g.log.Debug(why, args...)
		} else {
			g.log.Info(why, args...)
		}
		return false
	}
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return off("answer feedback is off: no feedback bucket", "err", err)
	}
	status, err := kv.Status(ctx)
	if err != nil {
		return off("answer feedback is off: the feedback bucket's settings could not be read", "err", err)
	}
	if status.TTL() <= 0 {
		return off("answer feedback is off: the feedback bucket has no max age, so a kept rating would never expire")
	}
	desk := &feedbackDesk{g: g, ttl: status.TTL()}
	g.feedback.Store(desk)
	if sink, ok := g.adapter.(FeedbackSink); ok {
		sink.SetFeedbackRecorder(desk)
	}
	g.log.Info("answer feedback is on", "bucket", lib.FeedbackBucket, "keptFor", status.TTL().String())
	return true
}

// feedbackTarget is the task id an answer to taskID is marked with, or ""
// when it is not the chain's final answer or feedback is off: the root, for
// the one task whose end is the chain's (observedAs), and never a hand-off
// line (handOffEnd).
func (g *Gateway) feedbackTarget(rec *SessionRecord, taskID string) string {
	if g.feedback.Load() == nil {
		return ""
	}
	if _, _, handOff := rec.handOffEnd(taskID, lib.StateCompleted); handOff {
		return ""
	}
	id, ends := rec.observedAs(taskID)
	if !ends {
		return ""
	}
	if root, ok := rec.TaskRefFor(id); ok && root.CardsAnswer {
		// A card's report answers this ask (Notifier.postConversation).
		return ""
	}
	return id
}

// markCardsAnswer records on taskID's chain root that the chain handed the
// ask to cards (TaskRef.CardsAnswer).
func (rec *SessionRecord) markCardsAnswer(taskID string) {
	root := taskID
	if ref, ok := rec.TaskRefFor(taskID); ok {
		root = ref.rootID()
	}
	for i := range rec.Tasks {
		if rec.Tasks[i].ID == root {
			rec.Tasks[i].CardsAnswer = true
		}
	}
}

// Tally counts the ratings kept that were given at or after since, by
// rating, reading every current record over one watcher rather than a
// request per key; a record that does not parse is skipped.
func (d *feedbackDesk) Tally(ctx context.Context, since time.Time) (up, down int, err error) {
	kv, err := d.g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return 0, 0, fmt.Errorf("feedback bucket: %w", err)
	}
	watcher, err := kv.WatchAll(ctx, jetstream.IgnoreDeletes())
	if err != nil {
		return 0, 0, fmt.Errorf("feedback bucket: %w", err)
	}
	defer func() { _ = watcher.Stop() }()
	for {
		select {
		case <-ctx.Done():
			return 0, 0, ctx.Err()
		case entry, ok := <-watcher.Updates():
			if !ok || entry == nil {
				// nil marks the end of the records already there.
				return up, down, nil
			}
			var rec FeedbackRecord
			if json.Unmarshal(entry.Value(), &rec) != nil || rec.RatedAt.Before(since) {
				continue
			}
			switch rec.Rating {
			case FeedbackUp:
				up++
			case FeedbackDown:
				down++
			}
		}
	}
}

// KeptDays is the bucket's max age in whole days, at least one.
func (d *feedbackDesk) KeptDays() int {
	return max(int(d.ttl/feedbackDay), 1)
}

// Admits reports a sender the gateway would take a turn from: a rating from
// anyone else changes nothing.
func (d *feedbackDesk) Admits(backend, rater string) bool {
	return d.g.resolvePrincipal(backend, rater) != ""
}

// Rate records one rating from a verified sender, and reports whether the
// kept record holds the chain's ask.
func (d *feedbackDesk) Rate(r FeedbackRating) (bool, error) {
	g := d.g
	if !d.Admits(r.Backend, r.Rater) {
		g.log.Info("dropping an answer rating from an unverified sender",
			"backend", r.Backend, "author", r.Rater, "conversation", r.Conversation)
		return false, errFeedbackUnverified
	}
	g.metrics.answerRated(r.Platform, r.Rating)
	key := feedbackKey(r.TaskID, r.Rater)
	// Locked before the deadline starts, so waiting on the rater's previous
	// tap does not spend this one's budget.
	defer d.lockKey(key)()
	ctx, cancel := context.WithTimeout(g.runCtx, feedbackRecorderDeadline)
	defer cancel()
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return false, fmt.Errorf("feedback bucket: %w", err)
	}
	// The ask is the human message that started the rated chain, as the
	// session record holds it (TaskRef.Request, until AskTTL clears it),
	// and the context id joins the rating to the stream's copy of the
	// conversation; both best effort, since a reaped session has neither.
	contextID, ask := "", ""
	if session, err := g.reg.Get(ctx, r.Conversation); err != nil {
		g.log.Warn("answer feedback: the session record could not be read; the rating is kept without its ask", "conversation", r.Conversation, "err", err)
	} else if session != nil {
		contextID = session.ContextID
		if root, ok := session.TaskRefFor(r.TaskID); ok {
			// As written, line breaks kept: a pasted manifest stays one.
			ask = root.Request
		}
	}
	rating := FeedbackDown
	if r.Rating != FeedbackDown {
		rating = FeedbackUp
	}
	// Merged, not replaced: a 👎 tapped again keeps the note already there.
	// A 👍 drops it, since the note said what went wrong.
	askKept := false
	if _, err := mergeFeedback(ctx, kv, key, true, func(rec *FeedbackRecord) bool {
		note := rec.Note
		if rating != FeedbackDown {
			note = ""
		}
		// A re-rating after the session dropped the ask (AskTTL, a reap)
		// keeps what the first rating kept.
		ask, contextID := ask, contextID
		if ask == "" {
			ask = rec.Ask
		}
		if contextID == "" {
			contextID = rec.ContextID
		}
		askKept = strings.TrimSpace(ask) != ""
		*rec = FeedbackRecord{
			Rating:       rating,
			Note:         note,
			Platform:     r.Platform,
			Rater:        r.Rater,
			Conversation: r.Conversation,
			MessageID:    r.MessageID,
			TaskID:       r.TaskID,
			ContextID:    contextID,
			Ask:          truncateRunes(ask, feedbackAskMax),
			Answer:       truncateRunes(r.Answer, feedbackAnswerMax),
			RatedAt:      time.Now().UTC(),
		}
		return true
	}); err != nil {
		return false, err
	}
	g.log.Info("answer rated", "platform", r.Platform, "rating", r.Rating, "taskId", r.TaskID,
		"conversation", r.Conversation, "kept", true)
	return askKept, nil
}

// Note adds the rater's note (empty: none) to their kept 👎 on r's answer.
// It keeps nothing when no 👎 is kept (its write failed, or a 👍 replaced
// it) and reports errFeedbackNotKept, so the rater is not thanked for a
// record that does not exist.
func (d *feedbackDesk) Note(r FeedbackRating, note string) error {
	g := d.g
	if !d.Admits(r.Backend, r.Rater) {
		return errFeedbackUnverified
	}
	key := feedbackKey(r.TaskID, r.Rater)
	// Under the rater's lock too, ordering the note with that rater's taps; the
	// merge's revision check already keeps a 👍 between read and write from losing it.
	defer d.lockKey(key)()
	ctx, cancel := context.WithTimeout(g.runCtx, feedbackRecorderDeadline)
	defer cancel()
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return fmt.Errorf("feedback bucket: %w", err)
	}
	kept, err := mergeFeedback(ctx, kv, key, false, func(rec *FeedbackRecord) bool {
		if rec.Rating != FeedbackDown {
			return false
		}
		rec.Note = truncateRunes(strings.TrimSpace(note), feedbackNoteMax)
		return true
	})
	if err != nil {
		return err
	}
	if !kept {
		return errFeedbackNotKept
	}
	g.log.Info("answer feedback note kept", "taskId", r.TaskID, "conversation", r.Conversation)
	return nil
}

// feedbackKey is a kept rating's key: the task id (a dot-free DNS-1123 label,
// payload spec assertion 2) and the rater, reduced to a key token's
// characters, so a rater has one rating per answer and re-rating replaces it.
func feedbackKey(taskID, rater string) string {
	return taskID + feedbackKeySep + strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			return r
		}
		return '_'
	}, rater)
}

func getFeedback(ctx context.Context, kv jetstream.KeyValue, key string) (FeedbackRecord, error) {
	entry, err := kv.Get(ctx, key)
	if err != nil {
		return FeedbackRecord{}, err
	}
	var rec FeedbackRecord
	if err := json.Unmarshal(entry.Value(), &rec); err != nil {
		return FeedbackRecord{}, fmt.Errorf("feedback %s: %w", key, err)
	}
	return rec, nil
}

// mergeFeedback applies change to the record at key (a zero record when
// there is none and create is set; without create a missing record is left
// missing and reported unwritten, as is one change declines by returning
// false) and writes it back only if nobody wrote in
// between, trying again from a fresh read when someone did: the rating, its
// ask and its note are written by separate goroutines, and none may lose
// another's part.
func mergeFeedback(ctx context.Context, kv jetstream.KeyValue, key string, create bool, change func(*FeedbackRecord) bool) (bool, error) {
	for range feedbackMergeAttempts {
		var rec FeedbackRecord
		var revision uint64
		entry, err := kv.Get(ctx, key)
		switch {
		case errors.Is(err, jetstream.ErrKeyNotFound):
			if !create {
				return false, nil
			}
		case err != nil:
			return false, fmt.Errorf("feedback %s: %w", key, err)
		default:
			if err := json.Unmarshal(entry.Value(), &rec); err != nil {
				return false, fmt.Errorf("feedback %s: %w", key, err)
			}
			revision = entry.Revision()
		}
		if !change(&rec) {
			return false, nil
		}
		raw, err := json.Marshal(rec)
		if err != nil {
			return false, err
		}
		if revision == 0 {
			_, err = kv.Create(ctx, key, raw)
		} else {
			_, err = kv.Update(ctx, key, raw, revision)
		}
		if err == nil {
			return true, nil
		}
		if !errors.Is(err, jetstream.ErrKeyExists) && !errors.Is(err, jetstream.ErrKeyRevisionMismatch) {
			return false, fmt.Errorf("feedback %s: %w", key, err)
		}
	}
	return false, fmt.Errorf("feedback %s: %w", key, errFeedbackContended)
}
