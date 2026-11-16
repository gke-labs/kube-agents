package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Unattended delivery (#5618, Track 3; docs/designs/scheduling-on-profiles.md
// section 4): when a schedule trigger's top-level task ends, the relay
// records the run in SCHEDULED_REPORTS and, per the delivery the trigger
// ingress recorded on the run's entry (TaskRef.Delivery), posts the
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
	// scheduledPostUnknown: a post was started and its outcome never came
	// back (it timed out, or the gateway stopped mid-post); it may be in
	// the channel, so it is not tried again.
	scheduledPostUnknown = "post_unknown"
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
	// scheduledPostTimeout bounds one post: the wait for the notify route's
	// answer, and the age past which a post started with no outcome
	// recorded is read as post_unknown. It is cut shorter when the relay
	// batch's own budget (turnTimeout) has less left, less
	// scheduledKVReserve for the outcome's write.
	scheduledPostTimeout = 20 * time.Second
	// scheduledPostStale is the age past which a post started with no
	// outcome recorded, or a day's thread claim never filled in, is read
	// as dead: the post's own time, plus the reserve its thread and
	// outcome writes run in.
	scheduledPostStale = scheduledPostTimeout + scheduledKVReserve
	// scheduledKVReserve is what a post leaves of the batch's budget for
	// the record writes after it.
	scheduledKVReserve = 10 * time.Second
	// scheduledKVTimeout bounds each record read or write. The writes run
	// off the batch's context (WithoutCancel), so a post that used up the
	// batch's time still records its outcome.
	scheduledKVTimeout = 5 * time.Second
	// scheduledTextMax bounds a record's report, in bytes: the report is
	// model output, and a record over the bucket's max value size would
	// fail every write and leave the run unrecorded. 24KiB holds a fleet
	// audit's report; past it the text is cut at a rune boundary and
	// scheduledTruncatedNote says so. JSON escaping can grow the stored
	// value (control characters six-fold), which the bucket's
	// --max-value-size allows for.
	scheduledTextMax = 24 << 10
	// scheduledTruncatedNote ends a cut report.
	scheduledTruncatedNote = "\n\n[report truncated: %d bytes not shown; the task's full result is on its stream]"
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
	// PostStartedAt is when the post in flight began, written before it
	// and cleared with its outcome: a record still posting with one older
	// than scheduledPostStale lost its outcome, and is post_unknown.
	PostStartedAt time.Time `json:"postStartedAt,omitzero"`
}

// truncateReport cuts text to scheduledTextMax bytes at a rune boundary and
// says how much was cut.
func truncateReport(text string) string {
	if len(text) <= scheduledTextMax {
		return text
	}
	cut := scheduledTextMax
	for cut > 0 && !utf8.RuneStart(text[cut]) {
		cut--
	}
	return text[:cut] + fmt.Sprintf(scheduledTruncatedNote, len(text)-cut)
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
// terminal: whether it can be handed back for a later try (only one off the
// relay durable, whose server counts its deliveries; not one requeued in
// memory, nor the heal's), and which delivery of the event this is.
type terminalTry struct {
	redeliverable bool
	delivery      uint64
}

// deliverScheduledReport records a schedule trigger's finished top-level run
// and delivers it per the run's Delivery. It reports true when the terminal
// should be handed back to the relay for another try: the record could not
// be written, or the post failed short of its last attempt. The caller then
// ends the terminal there, so its redelivery runs it again.
//
// The post and the record writes have their own clocks: the post at most
// scheduledPostTimeout and never past the batch's budget less
// scheduledKVReserve, the writes off the batch's context. A post that timed
// out may still land (its job can be in the route's worker), so it is
// recorded post_unknown and never handed back.
func (g *Gateway) deliverScheduledReport(ctx context.Context, rec *SessionRecord, ref TaskRef, taskID string, s lib.StatusUpdate, result string, try terminalTry) (redeliver bool) {
	reason := ""
	if s.Status.Message != nil {
		reason = joinTextParts(s.Status.Message.Parts)
	}
	text, silent := scheduledReportFrom(s.Status.State, result, reason)
	report := ScheduledReport{
		TaskID: taskID, Profile: ref.Addressee, FinishedAt: g.scheduleClock().UTC(),
		State: s.Status.State, Silent: silent, Text: truncateReport(text),
	}
	if ref.Requester != nil {
		report.TriggerName = ref.Requester.Subject
	}
	// The run's own delivery, recorded on its entry at submit; the
	// conversation's is the newest submission's, read only for an entry
	// from before the field.
	d := ref.Delivery
	if d == nil {
		d = rec.Delivery
	}
	switch {
	case d == nil:
		report.Status = scheduledNoDelivery
	case d.Mode == lib.ScheduleDeliveryLocal:
		report.Status = scheduledLocal
	case d.SilentWhenClean && silent:
		report.Status = scheduledSilent
	default:
		report.Status = scheduledPosting
	}
	// One resolution for the writer here and the status reader
	// (scheduledRunStatus), so the reader finds this record.
	report.JobKey = scheduledRunJobKey(rec, taskID)
	key := scheduledReportKey(report.JobKey, taskID)
	logArgs := []any{"jobKey", report.JobKey, "taskId", taskID, "conversation", rec.Key}
	store := g.scheduledStore(ctx)

	have, rev, err := store.get(key)
	if err != nil {
		return g.scheduledRecordFailed(store, key, try, err, logArgs, report.Status)
	}
	if have != nil && have.settled() {
		// A redelivered terminal whose run is already settled: posted,
		// kept, or given up on. Nothing is posted again.
		g.log.Debug("scheduled report already settled", append(logArgs, "status", have.Status)...)
		return false
	}
	if have != nil {
		report = *have
		if !report.PostStartedAt.IsZero() {
			// A post began and its outcome never came back.
			if g.scheduleClock().Sub(report.PostStartedAt) < scheduledPostStale && try.redeliverable {
				// It may still be in flight: look again later.
				return true
			}
			report.Status, report.PostStartedAt = scheduledPostUnknown, time.Time{}
			if _, err := store.put(key, report, rev); err != nil {
				g.log.Error("scheduled report outcome not recorded", append(logArgs, "status", report.Status, "err", err)...)
			}
			g.log.Warn("scheduled report post outcome unknown; not posting again", logArgs...)
			return false
		}
	}
	var reporter homeReporter
	var home string
	if report.Status == scheduledPosting {
		if reporter, home = g.homeReporter(); reporter == nil {
			report.Status = scheduledStoredNoHome
		}
	}
	if report.Status != scheduledPosting {
		if _, err := store.put(key, report, rev); err != nil {
			return g.scheduledRecordFailed(store, key, try, err, logArgs, report.Status)
		}
		g.log.Info("scheduled report recorded", append(logArgs, "status", report.Status)...)
		return false
	}

	postTimeout := scheduledPostTimeout
	if dl, ok := ctx.Deadline(); ok {
		postTimeout = min(postTimeout, time.Until(dl)-scheduledKVReserve)
	}
	if postTimeout <= 0 {
		// No time left in this batch to post and record the outcome: the
		// record goes first, then the terminal goes back untried.
		if _, err := store.put(key, report, rev); err != nil {
			return g.scheduledRecordFailed(store, key, try, err, logArgs, report.Status)
		}
		return try.redeliverable
	}
	// The record first, saying a post is starting.
	report.PostStartedAt = g.scheduleClock().UTC()
	if rev, err = store.put(key, report, rev); err != nil {
		return g.scheduledRecordFailed(store, key, try, err, logArgs, report.Status)
	}

	// Into the schedule's thread for the UTC day, or a new one
	// (schedule_threads.go).
	postCtx, cancel := context.WithTimeout(ctx, postTimeout)
	post, postErr := g.postScheduledReport(postCtx, reporter, home, report)
	cancel()
	report.Attempts++
	report.PostStartedAt = time.Time{}
	switch {
	case postErr == nil:
		report.Status, report.LastError = scheduledPosted, ""
		report.Platform, report.ChatID, report.ThreadID = post.Platform, post.ChatID, post.ThreadID
		if report.ThreadID == "" {
			report.ThreadID = post.MessageID
		}
	case errors.Is(postErr, errPostOutcomeUnknown):
		report.Status, report.LastError = scheduledPostUnknown, postErr.Error()
		g.log.Warn("scheduled report post outcome unknown; not posting again", append(logArgs, "err", postErr)...)
	case !try.redeliverable || max(uint64(report.Attempts), try.delivery) >= scheduledPostAttempts:
		report.Status, report.LastError = scheduledPostFailed, postErr.Error()
		g.log.Warn("scheduled report not posted; giving up", append(logArgs, "attempts", report.Attempts, "err", postErr)...)
	default:
		report.LastError = postErr.Error()
		redeliver = true
		g.log.Info("scheduled report post failed; the terminal goes back for another try",
			append(logArgs, "attempts", report.Attempts, "err", postErr)...)
	}
	if _, err := store.put(key, report, rev); err != nil {
		// The record still says a post started: a redelivery reads it as
		// post_unknown once scheduledPostStale has passed, and posts
		// nothing.
		g.log.Error("scheduled report outcome not recorded", append(logArgs, "status", report.Status, "err", err)...)
	}
	if report.Status == scheduledPosted {
		g.log.Info("scheduled report posted", append(logArgs, "platform", report.Platform, "chatId", report.ChatID, "threadId", report.ThreadID)...)
	}
	return redeliver
}

// scheduledRecordFailed decides a terminal whose record could not be read or
// written: nothing is posted without one, so the terminal goes back while it
// has deliveries left (only one off the relay durable can go back). Past
// them it is given up on, logged, with a best-effort post_failed on a record
// left posting. A run with no delivery has nothing to post and is not
// handed back.
func (g *Gateway) scheduledRecordFailed(store scheduledStore, key string, try terminalTry, err error, logArgs []any, status string) bool {
	if status != scheduledNoDelivery && try.redeliverable && try.delivery < scheduledRecordAttempts {
		g.log.Warn("scheduled report record unavailable; the terminal goes back for another try", append(logArgs, "delivery", try.delivery, "err", err)...)
		return true
	}
	g.log.Error("scheduled report not recorded; nothing posted", append(logArgs, "status", status, "err", err)...)
	if have, rev, gerr := store.get(key); gerr == nil && have != nil && have.Status == scheduledPosting {
		// A post that started and recorded no outcome may have landed.
		have.Status, have.LastError = scheduledPostFailed, err.Error()
		if !have.PostStartedAt.IsZero() {
			have.Status, have.PostStartedAt = scheduledPostUnknown, time.Time{}
		}
		_, _ = store.put(key, *have, rev)
	}
	return false
}

// scheduledStore reads and writes a bucket's records, each operation on its
// own scheduledKVTimeout inside ctx, retried as the relay's record writes
// are while ctx lasts. The records' store takes ctx's values without its
// cancellation, so an outcome is written after the post used the batch's
// time; a thread store's ctx is the slice of the post's budget it may spend.
type scheduledStore struct {
	g      *Gateway
	ctx    context.Context
	bucket string
}

func (g *Gateway) scheduledStore(ctx context.Context) scheduledStore {
	return scheduledStore{g: g, ctx: context.WithoutCancel(ctx), bucket: lib.ScheduledReportsBucket}
}

func (st scheduledStore) do(f func(ctx context.Context, kv jetstream.KeyValue) error) error {
	var err error
	for attempt := 0; attempt < kvRetryAttempts; attempt++ {
		if attempt > 0 {
			// withRetry's linear backoff, cut short by the store's own
			// deadline when it has one.
			select {
			case <-st.ctx.Done():
				return err
			case <-time.After(time.Duration(attempt) * kvRetryPause):
			}
		}
		if st.g.scheduledKVHook != nil {
			st.g.scheduledKVHook(st.bucket)
		}
		ctx, cancel := context.WithTimeout(st.ctx, scheduledKVTimeout)
		var kv jetstream.KeyValue
		if kv, err = st.g.client.KV(ctx, st.bucket); err == nil {
			err = f(ctx, kv)
		}
		cancel()
		// A create on a key that exists and an update of a revision that
		// moved are answers, not outages: trying again changes nothing. A
		// fenced write is withRetry's one stop, kept here.
		if err == nil || errors.Is(err, ErrFenced) || errors.Is(err, jetstream.ErrKeyExists) || isMintedAlready(err) {
			return err
		}
		if st.ctx.Err() != nil {
			return err
		}
	}
	return err
}

// get reads key's record and its revision, nil when there is none.
func (st scheduledStore) get(key string) (r *ScheduledReport, rev uint64, err error) {
	err = st.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		var e error
		r, rev, e = getScheduledReport(ctx, kv, key)
		return e
	})
	return r, rev, err
}

// put writes r at key over revision rev (0: a create).
func (st scheduledStore) put(key string, r ScheduledReport, rev uint64) (next uint64, err error) {
	err = st.do(func(ctx context.Context, kv jetstream.KeyValue) error {
		var e error
		next, e = putScheduledReport(ctx, kv, key, r, rev)
		return e
	})
	return next, err
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
// HTML escaping is off: it would grow a report's '<', '>' and '&' six-fold.
func putScheduledReport(ctx context.Context, kv jetstream.KeyValue, key string, r ScheduledReport, rev uint64) (uint64, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(r); err != nil {
		return 0, err
	}
	data := bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
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

// scheduledRunJobKey is the job key a run of rec's schedule is recorded
// under: the run's own delivery (TaskRef.Delivery, set at submit), else the
// conversation's for an entry from before that field, else the trigger name.
// The same resolution deliverScheduledReport uses, so a status reader finds
// the record the relay wrote.
func scheduledRunJobKey(rec *SessionRecord, taskID string) string {
	ref, _ := rec.TaskRefFor(taskID)
	d := ref.Delivery
	if d == nil {
		d = rec.Delivery
	}
	if d != nil && d.JobKey != "" {
		return d.JobKey
	}
	if ref.Requester != nil && ref.Requester.Subject != "" {
		return scheduledJobKeyFromTrigger(ref.Requester.Subject)
	}
	return scheduledJobKeyFromTrigger(strings.TrimPrefix(rec.Key, triggerConversationPrefix))
}

// scheduledRunStatus is the delivery status of rec's run taskID, "" when no
// record exists yet: the value a trigger status reply carries (the
// operator records it on the AgentSchedule's recent runs).
func (g *Gateway) scheduledRunStatus(ctx context.Context, rec *SessionRecord, taskID string) (string, error) {
	return g.scheduledReportStatus(ctx, scheduledRunJobKey(rec, taskID), taskID)
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
	// A thread is held to the home channel and put in the form the poster
	// takes, as validate does for a chat.notify request's.
	if thread != "" && n.threadOK != nil && !n.threadOK(thread, home) {
		return homePost{}, fmt.Errorf("thread %q is not a thread of the home channel", thread)
	}
	if thread != "" && n.threadTS != nil {
		thread = n.threadTS(thread)
	}
	job := notifyJob{req: lib.NotifyRequest{Text: text, Thread: thread}, home: home, dropLate: true,
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
		return homePost{}, fmt.Errorf("%w: %w", errPostOutcomeUnknown, ctx.Err())
	}
}

// errPostOutcomeUnknown is postHomeReport's answer when it stopped waiting:
// the job may yet be posted by the route's worker (one already posting runs
// to its end), so whether the report is in the channel is not known.
var errPostOutcomeUnknown = errors.New("no answer from the notify route in time; the post may still land")

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
