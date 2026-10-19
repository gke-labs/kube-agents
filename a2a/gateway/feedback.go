package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
// A 👍 is counted (answer_feedback_total) and nothing else is kept; it also
// takes back an earlier 👎 from the same rater. A 👎 is counted and keeps
// what the rater is told is kept, in the feedback bucket: the ask that
// started the rated chain, the answer, the ids, and an optional note from
// the rater, and nothing else of the thread. The
// bucket's max age is the retention, set where the deployment provisions it,
// and the gateway refuses to keep anything in a bucket without one. The
// gateway's user is the bucket's only reader and writer; an operator reads
// it through the gateway binary (`/gateway feedback export`, feedback_export.go),
// which writes each 👎 as a draft eval case. Logs and the metric carry ids
// and the rating, never the text.
const (
	// FeedbackUp and FeedbackDown are the two ratings, as the metric's
	// rating label and the record carry them.
	FeedbackUp   = "up"
	FeedbackDown = "down"

	// The caps on what a 👎 keeps, so one rating cannot grow the bucket
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

	// feedbackRetryInterval is how often the gateway looks again for a
	// feedback bucket it did not find at start: on an upgrade the provision
	// Job that creates it can finish after the new gateway starts.
	feedbackRetryInterval = time.Minute

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
	// write failed, or a 👍 took it back.
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
	// Answer is the rated message's text, handed over for a 👎 only.
	Answer string
}

// FeedbackRecord is a kept 👎, as the feedback bucket holds it.
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
// rater's note to their kept 👎, and how many days a 👎 is kept (for the
// rater to be told). Every call is safe from any goroutine and bounded by
// its own deadline.
type FeedbackRecorder interface {
	Admits(backend, rater string) bool
	Rate(r FeedbackRating) error
	Note(r FeedbackRating, note string) error
	KeptDays() int
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
	// keys serialises the ratings on one answer by one rater, so a 👍
	// tapped while that rater's 👎 is still being written waits for it and
	// then takes it back, instead of finding nothing to take back.
	keys sync.Map // feedbackKey -> *sync.Mutex
}

// lockKey takes the rating lock for key and returns its release.
func (d *feedbackDesk) lockKey(key string) func() {
	m, _ := d.keys.LoadOrStore(key, &sync.Mutex{})
	mu := m.(*sync.Mutex)
	mu.Lock()
	return mu.Unlock
}

// startFeedback turns answer feedback on (tryFeedback), or, when it cannot
// yet, keeps trying every feedbackRetryInterval until it can or ctx ends.
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
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if g.tryFeedback(ctx, true) {
					return
				}
			}
		}
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
	return id
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

// Rate records one rating from a verified sender.
func (d *feedbackDesk) Rate(r FeedbackRating) error {
	g := d.g
	if !d.Admits(r.Backend, r.Rater) {
		g.log.Info("dropping an answer rating from an unverified sender",
			"backend", r.Backend, "author", r.Rater, "conversation", r.Conversation)
		return errFeedbackUnverified
	}
	g.metrics.answerRated(r.Platform, r.Rating)
	ctx, cancel := context.WithTimeout(g.runCtx, feedbackRecorderDeadline)
	defer cancel()
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return fmt.Errorf("feedback bucket: %w", err)
	}
	key := feedbackKey(r.TaskID, r.Rater)
	defer d.lockKey(key)()
	if r.Rating != FeedbackDown {
		// A 👍 keeps nothing, and takes back the rater's 👎 if there is one.
		// Looked up first: a KV delete writes a marker whether or not the
		// key exists, and a marker would record the 👍.
		_, err := kv.Get(ctx, key)
		switch {
		case errors.Is(err, jetstream.ErrKeyNotFound):
		case err != nil:
			return fmt.Errorf("feedback %s: %w", key, err)
		default:
			if err := kv.Delete(ctx, key); err != nil {
				return fmt.Errorf("feedback %s: %w", key, err)
			}
		}
		g.log.Info("answer rated", "platform", r.Platform, "rating", r.Rating, "taskId", r.TaskID,
			"conversation", r.Conversation, "kept", false)
		return nil
	}
	// The ask is the human message that started the rated chain, as the
	// session record holds it (TaskRef.Request, until AskTTL clears it),
	// and the context id joins the rating to the stream's copy of the
	// conversation; both best effort, since a reaped session has neither.
	contextID, ask := "", ""
	if session, err := g.reg.Get(ctx, r.Conversation); err != nil {
		g.log.Warn("answer feedback: the session record could not be read; the 👎 is kept without its ask", "conversation", r.Conversation, "err", err)
	} else if session != nil {
		contextID = session.ContextID
		if root, ok := session.TaskRefFor(r.TaskID); ok {
			// As written, line breaks kept: a pasted manifest stays one.
			ask = root.Request
		}
	}
	// Merged, not replaced: re-rating keeps the note already there.
	if _, err := mergeFeedback(ctx, kv, key, true, func(rec *FeedbackRecord) {
		*rec = FeedbackRecord{
			Rating:       FeedbackDown,
			Note:         rec.Note,
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
	}); err != nil {
		return err
	}
	g.log.Info("answer rated", "platform", r.Platform, "rating", r.Rating, "taskId", r.TaskID,
		"conversation", r.Conversation, "kept", true)
	return nil
}

// Note adds the rater's note (empty: none) to their kept 👎 on r's answer.
// It keeps nothing when no 👎 is kept (its write failed, or a 👍 took it
// back) and reports errFeedbackNotKept, so the rater is not thanked for a
// record that does not exist.
func (d *feedbackDesk) Note(r FeedbackRating, note string) error {
	g := d.g
	if !d.Admits(r.Backend, r.Rater) {
		return errFeedbackUnverified
	}
	ctx, cancel := context.WithTimeout(g.runCtx, feedbackRecorderDeadline)
	defer cancel()
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return fmt.Errorf("feedback bucket: %w", err)
	}
	key := feedbackKey(r.TaskID, r.Rater)
	kept, err := mergeFeedback(ctx, kv, key, false, func(rec *FeedbackRecord) {
		rec.Note = truncateRunes(strings.TrimSpace(note), feedbackNoteMax)
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

// feedbackKey is a kept 👎's key: the task id (a dot-free DNS-1123 label,
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
// missing and reported unwritten) and writes it back only if nobody wrote in
// between, trying again from a fresh read when someone did: the rating, its
// ask and its note are written by separate goroutines, and none may lose
// another's part.
func mergeFeedback(ctx context.Context, kv jetstream.KeyValue, key string, create bool, change func(*FeedbackRecord)) (bool, error) {
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
		change(&rec)
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
