package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
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
// the exchange it rated in the feedback bucket: the ask, the answer, the
// thread up to the answer, and later an optional note from the rater. The
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
	// (in bytes, at a rune boundary: truncateRunes), and the thread to its
	// newest turns, each clipped.
	feedbackAskMax        = 4000
	feedbackAnswerMax     = 16000
	feedbackNoteMax       = 500
	feedbackTurnMax       = 4000
	feedbackTranscriptMax = 40

	// feedbackKeySep joins a record's task id and rater in its key.
	feedbackKeySep = "."

	// feedbackRecorderDeadline bounds one rating's store calls.
	feedbackRecorderDeadline = 10 * time.Second

	// feedbackDay converts the bucket's max age to the days the rater is
	// told it is kept.
	feedbackDay = 24 * time.Hour
)

// The speakers a transcript turn names: a person in the conversation, or
// the agent (the bot's own posts).
const (
	FeedbackFromUser  = "user"
	FeedbackFromAgent = "agent"
)

var (
	// errFeedbackUnverified is a rating from a sender the gateway would not
	// take a turn from.
	errFeedbackUnverified = errors.New("the rater is not a verified sender")
	// errFeedbackGone is a note for a 👎 the bucket no longer holds: taken
	// back by a 👍, or expired.
	errFeedbackGone = errors.New("no 👎 is kept for this answer and rater")
)

// FeedbackTurn is one message of the exchange a 👎 keeps.
type FeedbackTurn struct {
	From string `json:"from"`
	Text string `json:"text"`
}

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
	// Ask, Answer and Transcript are the exchange, read by the backend for a
	// 👎 only.
	Ask        string
	Answer     string
	Transcript []FeedbackTurn
}

// FeedbackRecord is a kept 👎, as the feedback bucket holds it.
type FeedbackRecord struct {
	Rating       string         `json:"rating"`
	Note         string         `json:"note,omitempty"`
	Platform     string         `json:"platform"`
	Rater        string         `json:"rater"`
	Conversation string         `json:"conversation"`
	MessageID    string         `json:"messageId"`
	TaskID       string         `json:"taskId"`
	ContextID    string         `json:"contextId,omitempty"`
	Ask          string         `json:"ask,omitempty"`
	Answer       string         `json:"answer,omitempty"`
	Transcript   []FeedbackTurn `json:"transcript,omitempty"`
	RatedAt      time.Time      `json:"ratedAt"`
}

// FeedbackRecorder is what the gateway offers a backend that draws rating
// controls: whether it takes ratings from a sender, record a rating, add a
// note to a kept 👎, and how many days a 👎 is kept (for the rater to be
// told). Every call is safe from any goroutine and bounded by its own
// deadline.
type FeedbackRecorder interface {
	Admits(backend, rater string) bool
	Rate(r FeedbackRating) error
	Note(backend, rater, taskID, note string) error
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
}

// startFeedback finds the feedback bucket and, when it has a max age, turns
// answer feedback on: answers are marked from here on, and the adapter is
// handed the recorder. A missing bucket (a deployment that predates it, or
// a test) or one that would keep ratings forever leaves it off.
func (g *Gateway) startFeedback(ctx context.Context) {
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		g.log.Info("answer feedback is off: no feedback bucket", "bucket", lib.FeedbackBucket, "err", err)
		return
	}
	status, err := kv.Status(ctx)
	if err != nil {
		g.log.Warn("answer feedback is off: the feedback bucket's settings could not be read", "bucket", lib.FeedbackBucket, "err", err)
		return
	}
	if status.TTL() <= 0 {
		g.log.Warn("answer feedback is off: the feedback bucket has no max age, so a kept rating would never expire", "bucket", lib.FeedbackBucket)
		return
	}
	desk := &feedbackDesk{g: g, ttl: status.TTL()}
	g.feedback.Store(desk)
	if sink, ok := g.adapter.(FeedbackSink); ok {
		sink.SetFeedbackRecorder(desk)
	}
	g.log.Info("answer feedback is on", "bucket", lib.FeedbackBucket, "keptFor", status.TTL().String())
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
	if r.Rating != FeedbackDown {
		// A 👍 keeps nothing, and takes back the rater's 👎 if there is one.
		if err := kv.Delete(ctx, key); err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
			return fmt.Errorf("feedback %s: %w", key, err)
		}
		g.log.Info("answer rated", "platform", r.Platform, "rating", r.Rating, "taskId", r.TaskID,
			"conversation", r.Conversation, "kept", false)
		return nil
	}
	rec := FeedbackRecord{
		Rating:       FeedbackDown,
		Platform:     r.Platform,
		Rater:        r.Rater,
		Conversation: r.Conversation,
		MessageID:    r.MessageID,
		TaskID:       r.TaskID,
		Ask:          truncateRunes(r.Ask, feedbackAskMax),
		Answer:       truncateRunes(r.Answer, feedbackAnswerMax),
		Transcript:   clipTranscript(r.Transcript),
		RatedAt:      time.Now().UTC(),
	}
	// The context id joins the rating to the stream's copy of the
	// conversation; best effort, since a reaped session has no record.
	if session, err := g.reg.Get(ctx, r.Conversation); err == nil && session != nil {
		rec.ContextID = session.ContextID
	}
	// Re-rating keeps the note the rater already gave.
	if old, err := getFeedback(ctx, kv, key); err == nil {
		rec.Note = old.Note
	}
	if err := putFeedback(ctx, kv, key, rec); err != nil {
		return err
	}
	g.log.Info("answer rated", "platform", r.Platform, "rating", r.Rating, "taskId", r.TaskID,
		"conversation", r.Conversation, "kept", true)
	return nil
}

// Note adds the rater's note to their kept 👎 on taskID's answer.
func (d *feedbackDesk) Note(backend, rater, taskID, note string) error {
	g := d.g
	if !d.Admits(backend, rater) {
		return errFeedbackUnverified
	}
	ctx, cancel := context.WithTimeout(g.runCtx, feedbackRecorderDeadline)
	defer cancel()
	kv, err := g.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		return fmt.Errorf("feedback bucket: %w", err)
	}
	key := feedbackKey(taskID, rater)
	rec, err := getFeedback(ctx, kv, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return errFeedbackGone
	}
	if err != nil {
		return err
	}
	rec.Note = truncateRunes(strings.TrimSpace(note), feedbackNoteMax)
	if err := putFeedback(ctx, kv, key, rec); err != nil {
		return err
	}
	g.log.Info("answer feedback note kept", "taskId", taskID, "conversation", rec.Conversation)
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

// clipTranscript keeps the newest turns, each clipped.
func clipTranscript(turns []FeedbackTurn) []FeedbackTurn {
	if len(turns) > feedbackTranscriptMax {
		turns = turns[len(turns)-feedbackTranscriptMax:]
	}
	out := make([]FeedbackTurn, 0, len(turns))
	for _, t := range turns {
		out = append(out, FeedbackTurn{From: t.From, Text: truncateRunes(t.Text, feedbackTurnMax)})
	}
	return out
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

func putFeedback(ctx context.Context, kv jetstream.KeyValue, key string, rec FeedbackRecord) error {
	raw, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	if _, err := kv.Put(ctx, key, raw); err != nil {
		return fmt.Errorf("feedback %s: %w", key, err)
	}
	return nil
}
