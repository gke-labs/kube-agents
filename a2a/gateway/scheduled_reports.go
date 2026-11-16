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

// Unattended delivery (#5618, Track 3; docs/designs/scheduling-on-profiles.md
// section 4): when a schedule trigger's top-level task ends, the relay
// records the run in SCHEDULED_REPORTS and, per the delivery the trigger
// ingress recorded on the conversation (SessionRecord.Delivery), posts the
// report to the install's home channel or keeps it. The record is written
// before any post, so a run that never posts is still on record, and a
// redelivered terminal reads it back rather than posting twice.

// The statuses a scheduled report's record carries.
const (
	// scheduledNoDelivery: the conversation recorded no delivery (a record
	// from before the field), so nothing is posted, as before.
	scheduledNoDelivery = "no_delivery"
	// scheduledLocal: the schedule's delivery mode is local; kept, never posted.
	scheduledLocal = "local"
	// scheduledSilent: a clean run of a silent-when-clean schedule.
	scheduledSilent = "silent"
	// scheduledStoredNoHome: chat delivery on an install with no home
	// channel (the chat.notify route's notifyNoHome); kept for a reader to
	// find once one is set, and the task is not failed for it.
	scheduledStoredNoHome = "stored_no_home"
	// scheduledPosting: the post is being tried, or will be again when the
	// terminal is redelivered.
	scheduledPosting = "posting"
	// scheduledPosted: the report is in the home channel.
	scheduledPosted = "posted"
	// scheduledPostFailed: every attempt failed; nothing more is tried.
	scheduledPostFailed = "post_failed"
)

const (
	// scheduledSilentMarker is the whole report of a run that found nothing.
	scheduledSilentMarker = "[SILENT]"
	// scheduledPostAttempts bounds the posts one run makes: a failed post
	// hands the terminal back to the relay (redeliver), and the attempt that
	// reaches this count is the last, which marks the record post_failed.
	// The relay durable itself has no MaxDeliver (lib refuses to bind one
	// that stops redelivering), so the count is the record's own, which
	// survives a gateway failover.
	scheduledPostAttempts = 5
	// scheduledPostRetryDelay is how long a terminal whose post failed waits
	// before it is redelivered: a chat backend's outage outlasts the relay's
	// requeueDelay, and five tries a few seconds apart would all land in it.
	scheduledPostRetryDelay = 30 * time.Second
	// scheduledRecordAttempts bounds the deliveries of a terminal whose
	// record could not be written (no bucket, a KV outage): the record goes
	// before any post, so nothing is posted until it lands, and past this
	// many deliveries the run is given up on, logged.
	scheduledRecordAttempts = 5
	// scheduledKeySeparator joins a record's job key and task id. A KV key
	// is the tail of the subject $KV.SCHEDULED_REPORTS.<key>, so '.' splits
	// it into subject tokens: with each half one token, `<jobKey>.>` lists
	// one schedule's runs. A job key is a DNS-1123 label and a task id is
	// task-<hex>, so neither carries a '.'; one derived from a trigger name
	// has its '.' and '/' replaced (scheduledJobKeyFromTrigger).
	scheduledKeySeparator = "."
	// scheduledJobKeyFill replaces the characters of a trigger's name that
	// can't stand in one key token. '_' is in no DNS-1123 name, so a derived
	// key can't collide with a schedule's own job key.
	scheduledJobKeyFill = "_"
)

// ScheduledReport is one finished run's record in SCHEDULED_REPORTS.
type ScheduledReport struct {
	JobKey      string        `json:"jobKey"`
	TriggerName string        `json:"triggerName,omitempty"`
	Profile     string        `json:"profile,omitempty"`
	TaskID      string        `json:"taskId"`
	FinishedAt  time.Time     `json:"finishedAt"`
	State       lib.TaskState `json:"state"`
	Silent      bool          `json:"silent,omitempty"`
	Text        string        `json:"text"`
	Status      string        `json:"status"`
	// Where the post landed, once posted.
	Platform string `json:"platform,omitempty"`
	ChatID   string `json:"chatId,omitempty"`
	ThreadID string `json:"threadId,omitempty"`
	// LastError and Attempts are the post's failures so far.
	LastError string `json:"lastError,omitempty"`
	Attempts  int    `json:"attempts,omitempty"`
}

// settled reports whether the record's status is final: a redelivered
// terminal leaves it as it is and posts nothing.
func (r *ScheduledReport) settled() bool {
	return r.Status != scheduledPosting
}

// scheduledReportKey is the record's KV key: <jobKey>.<taskID>.
func scheduledReportKey(jobKey, taskID string) string {
	return jobKey + scheduledKeySeparator + taskID
}

// scheduledJobKeyFromTrigger is the job key of a run whose conversation
// recorded no delivery: the trigger's name without its source, its '.' and
// '/' replaced so it stays one key token (scheduledKeySeparator).
func scheduledJobKeyFromTrigger(name string) string {
	_, rest, ok := strings.Cut(name, "/")
	if !ok {
		rest = name
	}
	return strings.NewReplacer(".", scheduledJobKeyFill, "/", scheduledJobKeyFill).Replace(rest)
}

// isScheduleConversation reports whether key is a schedule trigger's own
// conversation: trigger:schedule/<ns>/<name>.
func isScheduleConversation(key string) bool {
	return strings.HasPrefix(key, triggerConversationPrefix+lib.TriggerSourceSchedule+"/")
}

// scheduledReportFrom is the seam a run's report comes from: today the
// task's result text (or, for a run that did not complete, the notice the
// conversation would have read), clean when it is exactly [SILENT]. The
// structured kube-agents.report/v1 artifact from the report store plugs in
// here, ahead of the result text, in a later change.
func scheduledReportFrom(state lib.TaskState, result, reason string) (text string, silent bool) {
	if state != lib.StateCompleted {
		return terminalNotice(state, reason), false
	}
	if strings.TrimSpace(result) == scheduledSilentMarker {
		return result, true
	}
	if result == "" {
		return completedNonTextResult, false
	}
	return result, false
}

// homePost is where a home-channel post landed.
type homePost struct {
	Platform  string
	ChatID    string
	ThreadID  string
	MessageID string
}

// homeReporter posts a report to the install's home channel through the
// chat.notify route (Notifier): homeChannel is the channel a home post goes
// to now, "" for none (the route's notifyNoHome), and postHomeReport posts
// text there. thread is "" for a new message; one thread per schedule per UTC
// day (the design's SCHEDULE_THREADS) passes the day's thread here in a
// later change.
type homeReporter interface {
	homeChannel() string
	postHomeReport(ctx context.Context, home, thread, text string) (homePost, error)
}

// SetHomeReporters points scheduled reports at this term's chat.notify
// routes, the first with a home channel taking the post. Call it before
// RunWith, and again each term.
func (g *Gateway) SetHomeReporters(routes ...*Notifier) {
	reporters := make([]homeReporter, 0, len(routes))
	for _, n := range routes {
		if n != nil && n.poster != nil {
			reporters = append(reporters, n)
		}
	}
	g.setHomeReporters(reporters...)
}

func (g *Gateway) setHomeReporters(reporters ...homeReporter) {
	g.homeReportersMu.Lock()
	defer g.homeReportersMu.Unlock()
	g.homeReporters = reporters
}

// homeReporter is the route a scheduled report posts through and its home
// channel, or nil when no route has one: the install has no home channel.
func (g *Gateway) homeReporter() (homeReporter, string) {
	g.homeReportersMu.Lock()
	reporters := append([]homeReporter(nil), g.homeReporters...)
	g.homeReportersMu.Unlock()
	for _, r := range reporters {
		if home := r.homeChannel(); home != "" {
			return r, home
		}
	}
	return nil, ""
}

// terminalTry is what relayTerminal knows of the delivery that brought the
// terminal: whether it can be handed back for a later try (one off the relay
// durable or the in-memory queue; not the heal's), and which delivery of the
// event this is (0 when unknown).
type terminalTry struct {
	redeliverable bool
	delivery      uint64
}

// deliverScheduledReport records a schedule trigger's finished top-level run
// and delivers it per the conversation's Delivery. It reports true when the
// terminal should be handed back to the relay for another try: the record
// could not be written, or the post failed short of its last attempt. The
// caller then ends the terminal there, so its redelivery runs it again.
func (g *Gateway) deliverScheduledReport(ctx context.Context, rec *SessionRecord, ref TaskRef, taskID string, s lib.StatusUpdate, result string, try terminalTry) (redeliver bool) {
	reason := ""
	if s.Status.Message != nil {
		reason = joinTextParts(s.Status.Message.Parts)
	}
	text, silent := scheduledReportFrom(s.Status.State, result, reason)
	report := ScheduledReport{
		TaskID: taskID, Profile: ref.Addressee, FinishedAt: time.Now().UTC(),
		State: s.Status.State, Silent: silent, Text: text,
	}
	if ref.Requester != nil {
		report.TriggerName = ref.Requester.Subject
	}
	d := rec.Delivery
	switch {
	case d == nil:
		report.JobKey, report.Status = scheduledJobKeyFromTrigger(report.TriggerName), scheduledNoDelivery
	case d.Mode == lib.ScheduleDeliveryLocal:
		report.JobKey, report.Status = d.JobKey, scheduledLocal
	case d.SilentWhenClean && silent:
		report.JobKey, report.Status = d.JobKey, scheduledSilent
	default:
		report.JobKey, report.Status = d.JobKey, scheduledPosting
	}
	if report.JobKey == "" {
		report.JobKey = scheduledJobKeyFromTrigger(strings.TrimPrefix(rec.Key, triggerConversationPrefix))
	}
	key := scheduledReportKey(report.JobKey, taskID)
	logArgs := []any{"jobKey", report.JobKey, "taskId", taskID, "conversation", rec.Key}

	kv, err := g.client.KV(ctx, lib.ScheduledReportsBucket)
	var have *ScheduledReport
	var rev uint64
	if err == nil {
		have, rev, err = getScheduledReport(ctx, kv, key)
	}
	if err != nil {
		return g.scheduledRecordFailed(report, try, err, logArgs)
	}
	if have != nil && have.settled() {
		// A redelivered terminal whose run is already settled: posted,
		// kept, or given up on. Nothing is posted again.
		g.log.Debug("scheduled report already settled", append(logArgs, "status", have.Status)...)
		return false
	}
	if have != nil {
		// A post that failed before, tried again.
		report = *have
	}
	var reporter homeReporter
	var home string
	if report.Status == scheduledPosting {
		if reporter, home = g.homeReporter(); reporter == nil {
			report.Status = scheduledStoredNoHome
		}
	}
	// The record first, whatever comes next.
	if rev, err = putScheduledReport(ctx, kv, key, report, rev); err != nil {
		return g.scheduledRecordFailed(report, try, err, logArgs)
	}
	if report.Status != scheduledPosting {
		g.log.Info("scheduled report recorded", append(logArgs, "status", report.Status)...)
		return false
	}

	// One message per run; the thread is the day's in a later change.
	post, postErr := reporter.postHomeReport(ctx, home, "", report.Text)
	report.Attempts++
	switch {
	case postErr == nil:
		report.Status, report.LastError = scheduledPosted, ""
		report.Platform, report.ChatID, report.ThreadID = post.Platform, post.ChatID, post.ThreadID
		if report.ThreadID == "" {
			report.ThreadID = post.MessageID
		}
	case !try.redeliverable || report.Attempts >= scheduledPostAttempts:
		report.Status, report.LastError = scheduledPostFailed, postErr.Error()
		g.log.Warn("scheduled report not posted; giving up", append(logArgs, "attempts", report.Attempts, "err", postErr)...)
	default:
		report.LastError = postErr.Error()
		redeliver = true
		g.log.Info("scheduled report post failed; the terminal goes back for another try",
			append(logArgs, "attempts", report.Attempts, "err", postErr)...)
	}
	if _, err := putScheduledReport(ctx, kv, key, report, rev); err != nil {
		// Posted with the record still saying posting: a redelivery would
		// post it again, so the terminal is not handed back for that; a
		// failed post is handed back as it would have been.
		g.log.Error("scheduled report outcome not recorded", append(logArgs, "status", report.Status, "err", err)...)
	}
	if report.Status == scheduledPosted {
		g.log.Info("scheduled report posted", append(logArgs, "platform", report.Platform, "chatId", report.ChatID, "threadId", report.ThreadID)...)
	}
	return redeliver
}

// scheduledRecordFailed decides a terminal whose record could not be read or
// written: nothing is posted without one, so the terminal goes back while it
// has deliveries left, and is given up on (logged) past them. A run with no
// delivery has nothing to post and is not handed back.
func (g *Gateway) scheduledRecordFailed(report ScheduledReport, try terminalTry, err error, logArgs []any) bool {
	if report.Status != scheduledNoDelivery && try.redeliverable && try.delivery < scheduledRecordAttempts {
		g.log.Warn("scheduled report record unavailable; the terminal goes back for another try", append(logArgs, "delivery", try.delivery, "err", err)...)
		return true
	}
	g.log.Error("scheduled report not recorded; nothing posted", append(logArgs, "status", report.Status, "err", err)...)
	return false
}

// getScheduledReport reads key's record and its revision, nil when there is
// none.
func getScheduledReport(ctx context.Context, kv jetstream.KeyValue, key string) (*ScheduledReport, uint64, error) {
	e, err := kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return nil, 0, nil
	}
	if err != nil {
		return nil, 0, err
	}
	var r ScheduledReport
	if err := json.Unmarshal(e.Value(), &r); err != nil {
		return nil, 0, fmt.Errorf("scheduled report %s does not decode: %w", key, err)
	}
	return &r, e.Revision(), nil
}

// putScheduledReport writes r at key: a create when rev is 0, else an update
// of that revision, so a second writer's record is never overwritten blind.
func putScheduledReport(ctx context.Context, kv jetstream.KeyValue, key string, r ScheduledReport, rev uint64) (uint64, error) {
	data, err := json.Marshal(r)
	if err != nil {
		return 0, err
	}
	if rev == 0 {
		return kv.Create(ctx, key, data)
	}
	return kv.Update(ctx, key, data, rev)
}

// scheduledReportStatus is the status of jobKey's run taskID in
// SCHEDULED_REPORTS, "" when there is no record yet: what a trigger status
// reply carries as the run's delivery.
func (g *Gateway) scheduledReportStatus(ctx context.Context, jobKey, taskID string) (string, error) {
	kv, err := g.client.KV(ctx, lib.ScheduledReportsBucket)
	if err != nil {
		return "", err
	}
	r, _, err := getScheduledReport(ctx, kv, scheduledReportKey(jobKey, taskID))
	if err != nil || r == nil {
		return "", err
	}
	return r.Status, nil
}

// homeChannel is the channel a home post goes to now, "" for none: the
// check validate refuses a home request with notifyNoHome on.
func (n *Notifier) homeChannel() string {
	home, _ := n.homeState()
	return home
}

// postHomeReport posts text to home through the route's worker, as a
// chat.notify home request would be posted (post: the home check, chunking,
// the rating controls), and waits for the answer: after the first chunk
// lands, with where it landed, or with why it did not. Queued behind the
// route's other requests rather than posted here, since the worker alone
// owns the route's state.
func (n *Notifier) postHomeReport(ctx context.Context, home, thread, text string) (homePost, error) {
	answers := make(chan lib.NotifyReply, 1)
	job := notifyJob{req: lib.NotifyRequest{Text: text, Thread: thread}, home: home,
		answer: func(r lib.NotifyReply) { answers <- r }}
	if dl, ok := ctx.Deadline(); ok {
		job.deadline = dl
	}
	if err := n.enqueue(job); err != nil {
		return homePost{}, err
	}
	select {
	case r := <-answers:
		if r.Error != "" {
			return homePost{}, errors.New(r.Error)
		}
		return homePost{Platform: n.backend, ChatID: home, ThreadID: r.ThreadID, MessageID: r.MessageID}, nil
	case <-ctx.Done():
		return homePost{}, fmt.Errorf("no answer from the notify route: %w", ctx.Err())
	}
}

// enqueue hands job to the worker without waiting, or says why it can't:
// the route is stopping, not started, or full.
func (n *Notifier) enqueue(job notifyJob) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	switch {
	case n.stopping:
		return errors.New(notifyStoppingRefusal)
	case n.jobs == nil:
		return errors.New("the notify route is not armed")
	}
	select {
	case n.jobs <- job:
		return nil
	default:
		return fmt.Errorf("%d notifies are already waiting to post", notifyQueueDepth)
	}
}
