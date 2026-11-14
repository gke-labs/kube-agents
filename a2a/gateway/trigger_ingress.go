package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The trigger ingress (#4685, Track 2; docs/designs/scheduling-on-profiles.md
// section 3): an unattended source asks the elected gateway for a task on
// lib.TriggerSubmitSubject(source), request/reply. The callout binds the
// subject's source to the caller's identity, so the source is trusted here
// and nothing in the request is. The gateway checks the request, caps each
// trigger's rate, mints the task's root grant with the trigger bound
// (capability.Entry.Trigger), writes the same trigger into the envelope's
// authority block (Authority.Trigger), records the run and its delivery on
// the trigger's own conversation, publishes the submission on the profile's
// unattended intake, and answers with the task's id or a refusal.
//
// It ships dark: until the callout grants a source publish on the subject
// (#5617's second pull request), nothing can reach it.

const (
	// triggerSubmitWildcard is every source's submission subject.
	triggerSubmitWildcard = lib.TriggerSubmitSubjectPrefix + "*" + lib.TriggerSubmitSubjectSuffix
	// triggerConversationPrefix opens a trigger's own conversation key:
	// trigger:<source>/<namespace>/<name>.
	triggerConversationPrefix = "trigger:"
	// triggerRequesterBackend and triggerRequesterVerifiedBy are the
	// requester stamp's backend and how the source was verified: the
	// callout bound the subject to an unattended source's ServiceAccount.
	triggerRequesterBackend    = "trigger"
	triggerRequesterVerifiedBy = "unattended-ksa"
	// triggerAudienceKind is the audience kind of a trigger's task: no
	// conversation reads it as it runs.
	triggerAudienceKind = "unattended"
	// DefaultTriggerRatePerHour is each trigger's cap when the config sets
	// none: twelve an hour, so a schedule every five minutes runs every
	// time (#5635).
	DefaultTriggerRatePerHour = 12
	// triggerLimiterMax bounds the limiter's map: past it, a full bucket
	// (one that would admit at once anyway) is dropped first.
	triggerLimiterMax = 4096
	// triggerIngressRetry is the wait before retrying a failed subscribe.
	triggerIngressRetry = 5 * time.Second
	// triggerTaskIDHex is the hex width of a task id derived from an
	// idempotency key, the width lib.NewTaskID mints.
	triggerTaskIDHex = 16
)

// startTriggerIngress subscribes the elected gateway to every source's
// submission subject, retrying a failed subscribe as the directory does, for
// as long as the term's ctx lives.
func (g *Gateway) startTriggerIngress(ctx context.Context) {
	go func() {
		for {
			sub, err := g.client.SubscribeCore(triggerSubmitWildcard, func(m *nats.Msg) {
				// Answered only on the source's own reply namespace
				// (lib.TriggerReplySubject): a submission naming any other
				// reply subject is dropped before anything is started, so
				// no answer, and no task, goes where the source can't read.
				source, err := lib.ParseTriggerSubmitSubject(m.Subject)
				if err != nil || !lib.ValidTriggerReplySubject(source, m.Reply) {
					g.log.Info("trigger submission dropped: no reply subject in the source's namespace", "subject", m.Subject)
					return
				}
				reply := g.handleTriggerSubmit(ctx, m.Subject, m.Data)
				if data, err := json.Marshal(reply); err == nil {
					if err := m.Respond(data); err != nil {
						g.log.Warn("trigger submission reply failed", "subject", m.Subject, "err", err)
					}
				}
			})
			if err == nil {
				<-ctx.Done()
				sub.Stop()
				return
			}
			g.log.Warn("trigger ingress subscription failed; retrying", "err", err, "in", triggerIngressRetry)
			select {
			case <-ctx.Done():
				return
			case <-time.After(triggerIngressRetry):
			}
		}
	}()
}

// triggerRefused is a refusal reply, logged with the trigger it was for.
func (g *Gateway) triggerRefused(trigger, reason, detail string) lib.TriggerSubmitReply {
	g.log.Info("trigger submission refused", "trigger", trigger, "reason", reason, "detail", detail)
	return lib.TriggerSubmitReply{Refused: &lib.TriggerRefusal{Reason: reason, Detail: detail}}
}

// handleTriggerSubmit answers one submission.
func (g *Gateway) handleTriggerSubmit(ctx context.Context, subject string, data []byte) lib.TriggerSubmitReply {
	source, err := lib.ParseTriggerSubmitSubject(subject)
	if err != nil {
		return g.triggerRefused("", lib.TriggerRefusedInvalid, err.Error())
	}
	var s lib.TriggerSubmit
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&s); err != nil {
		return g.triggerRefused("", lib.TriggerRefusedInvalid, "the submission is not well-formed")
	}
	if _, err := dec.Token(); err != io.EOF {
		return g.triggerRefused("", lib.TriggerRefusedInvalid, "the submission has data after its object")
	}
	if err := lib.ValidateTriggerSubmit(source, s); err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, err.Error())
	}
	// A trigger's identity is schedule/<name>, with no namespace in it: the
	// grant, the report stream it binds and the rate cap all key on it. A
	// built-in schedule lives in the install's own namespace (one
	// PlatformAgent per cluster, which owns them), so a schedule anywhere
	// else is refused, and two namespaces never share a trigger.
	if g.cfg.Namespace == "" || s.Schedule.Namespace != g.cfg.Namespace {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "schedule "+s.Schedule.Namespace+"/"+s.Schedule.Name+" is not in the install's namespace")
	}
	key := triggerConversationKey(source, s.Schedule)
	// One submission at a time per trigger, and the relay's batches for its
	// tasks wait on the same lock (relayBatch): the record read here is the
	// one written back, and a fast executor's terminal finds its TaskRef.
	lock := g.lockSession(key)
	lock.Lock()
	defer lock.Unlock()

	rec, err := g.reg.Get(ctx, key)
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
	}
	card, ok := g.profiles.lookup(s.Profile)
	if !ok {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedProfileUnknown, "no profile "+s.Profile+" in the directory")
	}
	if !cardAllowsTier(card, capability.TierUnattended) {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedTierNotAllowed, "profile "+s.Profile+" does not allow the unattended tier")
	}
	// The same route every profile task takes (profileRouteFor): the
	// install's namespace scope, or a target the scope file admits; the
	// parameters the persona declares; a pin's tiers on its cluster. A
	// target that routes the task to another profile isn't this schedule's.
	route, err := g.profileRouteFor(s.Profile, s.Parameters, capability.TierUnattended, lib.SubmitKindTop)
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, err.Error())
	}
	if route.Profile != s.Profile {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the schedule's target routes to profile "+route.Profile+", not "+s.Profile)
	}

	// A recent run under this key is that run. One already on the bus is
	// answered with its task; one recorded but not yet published (its
	// publish failed, or the gateway stopped) is published again with the
	// same grant and envelope id, so the stream dedupes a publish that had
	// in fact landed. Neither mints or spends the rate budget.
	if run := rec.triggerRun(s.IdempotencyKey); run != nil {
		if run.Published {
			return lib.TriggerSubmitReply{TaskID: run.Task}
		}
		return g.publishTriggerRun(ctx, rec, s, route, *run)
	}

	if !g.triggerLimiter().allow(key, time.Now()) {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedRateLimited, "the trigger is over its rate cap")
	}
	// The cap bucket's writes are create-only, so a key a failed attempt
	// minted can't be minted again. Such an attempt was never recorded, so
	// never published (the run is recorded before its publish): the next
	// attempt's task id is safe to use.
	trigger := &capability.Trigger{Name: s.Trigger.Name, Origin: s.Trigger.Origin, Stream: s.Trigger.Stream}
	var run TriggerRun
	for attempt := 0; ; attempt++ {
		taskID := triggerTaskID(s.IdempotencyKey, attempt)
		minted, err := capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, capability.Entry{
			Tier:     capability.TierUnattended,
			Scope:    route.Scope,
			Profile:  route.Profile,
			Delegate: profileGrantDelegate,
			Trigger:  trigger,
		})
		if err == nil {
			run = TriggerRun{Key: s.IdempotencyKey, Task: taskID, Capability: &minted, CorrelationID: "corr-" + randHex(contextIDHexWidth)}
			break
		}
		if isMintedAlready(err) && attempt+1 < triggerMintAttempts {
			g.log.Warn("trigger task grant already minted by an unrecorded attempt; minting the next", "trigger", s.Trigger.Name, "taskId", taskID)
			continue
		}
		g.log.Error("trigger task capability mint failed", "trigger", s.Trigger.Name, "taskId", taskID, "err", err)
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the task's grant could not be minted")
	}

	if rec == nil {
		rec = &SessionRecord{Key: key, ContextID: "ctx-" + randHex(contextIDHexWidth), Kind: triggerAudienceKind, LastActivity: time.Now().UTC()}
		if err := g.reg.Create(ctx, rec); err != nil {
			if !errors.Is(err, ErrSessionExists) {
				return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
			}
			if rec, err = g.reg.Get(ctx, key); err != nil || rec == nil {
				return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
			}
		}
	}
	// Recorded, pending, before its publish: a retry after anything below
	// fails publishes this run again rather than minting another.
	rec.addTriggerRun(run)
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("trigger record write failed", "conversation", rec.Key, "taskId", run.Task, "err", err)
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
	}
	return g.publishTriggerRun(ctx, rec, s, route, run)
}

// publishTriggerRun registers run where the relay looks, publishes its
// submission and marks it published. Called under the trigger's lock with
// run already recorded (pending) on rec.
func (g *Gateway) publishTriggerRun(ctx context.Context, rec *SessionRecord, s lib.TriggerSubmit, route *profileRoute, run TriggerRun) lib.TriggerSubmitReply {
	taskID := run.Task
	trigger := &capability.Trigger{Name: s.Trigger.Name, Origin: s.Trigger.Origin, Stream: s.Trigger.Stream}
	authority := Authority{
		Requester: AuthorityRequester{
			Principal:  g.ps.Hash("trigger:" + s.Trigger.Name),
			Backend:    triggerRequesterBackend,
			Subject:    s.Trigger.Name,
			VerifiedBy: triggerRequesterVerifiedBy,
		},
		Audience: AuthorityAudience{Conversation: rec.Key, Kind: triggerAudienceKind},
		Trigger:  trigger,
	}
	payload, err := submissionPayload(triggerTaskText(s), taskID, rec.ContextID, route)
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the task's message could not be built")
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID, run.CorrelationID, payload,
		lib.WithTo(lib.Party{Session: route.Profile}),
		lib.WithAuthority(authority.Render(run.Capability)),
		// The run's envelope id: a publish repeated inside the stream's
		// duplicate window is the stream's duplicate, not a second task.
		lib.WithEnvelopeID("env-"+taskID))
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the task's envelope could not be built")
	}

	// Registered where the relay looks before the publish, as startTask
	// does, so the executor's first event finds its conversation.
	requester := TaskRequester{Backend: triggerRequesterBackend, Subject: s.Trigger.Name}
	prevActive, prevTasks := rec.ActiveTask, rec.Tasks
	now := time.Now()
	rec.Delivery = &s.Delivery
	rec.ActiveTask = &ActiveTask{TaskID: taskID, CorrelationID: run.CorrelationID, Ask: truncateRunes(s.Prompt, askCap),
		SubmittedAt: now, Capability: run.Capability}
	if !rec.hasTask(taskID) {
		rec.Tasks = append(append([]TaskRef(nil), rec.Tasks...), TaskRef{
			ID: taskID, Addressee: route.Profile, CorrelationID: run.CorrelationID, Capability: run.Capability,
			Requester: &requester, Attribution: authority.Attribution(), StartedAt: now.UTC(),
			RootTaskID: taskID, Tier: string(capability.TierUnattended), Request: capAsk(s.Prompt),
		})
		if len(rec.Tasks) > taskHistoryCap {
			rec.Tasks = rec.Tasks[len(rec.Tasks)-taskHistoryCap:]
		}
	}
	// A submission is the trigger's turn: it keeps the record alive for the
	// reaper (sessionPruneDue) as an inbound message does a chat's.
	rec.LastActivity = now.UTC()
	rec.LastTaskActivity = now.UTC()
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("trigger record write failed", "conversation", rec.Key, "taskId", taskID, "err", err)
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
	}
	g.mu.Lock()
	g.taskSessions[taskID] = rec.Key
	g.relays[taskID] = &relayState{local: true}
	g.mu.Unlock()
	if err := g.reg.IndexTask(ctx, taskID, rec.Key); err != nil {
		g.log.Error("trigger task index write failed", "taskId", taskID, "err", err)
	}
	if _, err := g.client.PublishSeq(ctx, lib.TaskSubmitSubject(route.Profile, string(capability.TierUnattended), lib.SubmitKindTop, taskID), env); err != nil {
		g.log.Error("trigger task publish failed", "trigger", s.Trigger.Name, "taskId", taskID, "err", err)
		g.mu.Lock()
		delete(g.taskSessions, taskID)
		delete(g.relays, taskID)
		g.mu.Unlock()
		// The run stays recorded, pending, for the retry to publish.
		rec.ActiveTask, rec.Tasks = prevActive, prevTasks
		if err := g.reg.Put(ctx, rec); err != nil {
			g.log.Error("trigger record restore failed", "conversation", rec.Key, "taskId", taskID, "err", err)
		}
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the task could not be published")
	}
	// On the bus: a repeat of the key is now answered with the task. A
	// write that fails here leaves the run pending, and a repeat republishes
	// it under the same envelope id, which the stream dedupes inside its
	// window; the task id still goes back, so the reconciler holds it.
	rec.markTriggerRunPublished(run.Key)
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("trigger run write failed", "conversation", rec.Key, "taskId", taskID, "err", err)
	}
	g.log.Info("trigger task started", "trigger", s.Trigger.Name, "taskId", taskID, "profile", route.Profile,
		"idempotencyKey", s.IdempotencyKey)
	return lib.TriggerSubmitReply{TaskID: taskID}
}

// triggerConversationKey is a trigger's own conversation:
// trigger:<source>/<namespace>/<name>.
func triggerConversationKey(source string, s lib.ScheduleRef) string {
	return triggerConversationPrefix + source + "/" + s.Namespace + "/" + s.Name
}

// unpostableTrigger reports whether conversation is a trigger's own, which
// no chat backend owns: the relay posts nothing there (a backend handed the
// key would refuse it, or worse, read it as one of its own). Where an
// unattended result goes is Track 3's (#5618), from the record's Delivery.
func (g *Gateway) unpostableTrigger(conversation string) bool {
	if !strings.HasPrefix(conversation, triggerConversationPrefix) {
		return false
	}
	g.log.Debug("nothing posted to a trigger's conversation", "conversation", conversation)
	return true
}

// triggerRunsKept bounds SessionRecord.TriggerRuns: a reconciler retries a
// slot only inside its starting deadline, so the recent runs are enough.
const triggerRunsKept = 64

// triggerMintAttempts bounds the task ids one run may try when an earlier,
// unrecorded attempt left its grant key taken.
const triggerMintAttempts = 4

// isMintedAlready reports whether a mint failed because the grant's key
// already holds an entry (the bucket's writes are create-only).
func isMintedAlready(err error) bool {
	var apiErr *jetstream.APIError
	return errors.As(err, &apiErr) &&
		(apiErr.ErrorCode == jetstream.JSErrCodeStreamWrongLastSequence ||
			apiErr.ErrorCode == jetstream.JSErrCodeStreamWrongLastSequenceConstant)
}

// triggerRun is the recent run under key, or nil.
func (rec *SessionRecord) triggerRun(key string) *TriggerRun {
	if rec == nil {
		return nil
	}
	for i := range rec.TriggerRuns {
		if rec.TriggerRuns[i].Key == key {
			return &rec.TriggerRuns[i]
		}
	}
	return nil
}

// addTriggerRun records a run, keeping the newest triggerRunsKept.
func (rec *SessionRecord) addTriggerRun(r TriggerRun) {
	rec.TriggerRuns = append(rec.TriggerRuns, r)
	if n := len(rec.TriggerRuns) - triggerRunsKept; n > 0 {
		rec.TriggerRuns = append([]TriggerRun(nil), rec.TriggerRuns[n:]...)
	}
}

// markTriggerRunPublished marks the run under key as on the bus.
func (rec *SessionRecord) markTriggerRunPublished(key string) {
	if r := rec.triggerRun(key); r != nil {
		r.Published = true
	}
}

// hasTask reports whether the record already lists taskID.
func (rec *SessionRecord) hasTask(taskID string) bool {
	for _, t := range rec.Tasks {
		if t.ID == taskID {
			return true
		}
	}
	return false
}

// triggerTaskID derives the run's task id from its idempotency key and the
// mint attempt, so the same run always names the same task (and the same
// grant key) unless an earlier attempt's grant took it.
func triggerTaskID(idempotencyKey string, attempt int) string {
	sum := sha256.Sum256([]byte("trigger-run\x00" + idempotencyKey + "\x00" + strconv.Itoa(attempt)))
	return "task-" + hex.EncodeToString(sum[:])[:triggerTaskIDHex*2]
}

// triggerTaskText is the task's text: the prompt, then the skills the run
// is asked to load, named so the executor's skill loader can find them.
func triggerTaskText(s lib.TriggerSubmit) string {
	if len(s.Skills) == 0 {
		return s.Prompt
	}
	return s.Prompt + "\n\nUse these skills: " + strings.Join(s.Skills, ", ") + "."
}

// cardAllowsTier reports whether a profile's card lists tier.
func cardAllowsTier(card lib.AgentCard, tier capability.Tier) bool {
	for _, t := range card.Tiers {
		if t == string(tier) {
			return true
		}
	}
	return false
}

// triggerLimiter is the gateway's per-trigger rate cap, made on first use.
func (g *Gateway) triggerLimiter() *triggerRateLimiter {
	g.triggerLimiterOnce.Do(func() {
		rate := g.cfg.TriggerRatePerHour
		if rate <= 0 {
			rate = DefaultTriggerRatePerHour
		}
		g.triggerLimits = newTriggerRateLimiter(rate)
	})
	return g.triggerLimits
}

// triggerRateLimiter is a token bucket per trigger name: perHour tokens,
// refilled continuously. It lives in the elected gateway's memory, so a
// failover starts every bucket full, which can admit one extra burst; the
// reconciler's own one-run-per-slot rule (Forbid) is the steady bound.
type triggerRateLimiter struct {
	mu      sync.Mutex
	perHour float64
	buckets map[string]*triggerBucket
}

type triggerBucket struct {
	tokens float64
	at     time.Time
}

func newTriggerRateLimiter(perHour int) *triggerRateLimiter {
	return &triggerRateLimiter{perHour: float64(perHour), buckets: map[string]*triggerBucket{}}
}

// allow spends one of name's tokens at now, or reports false.
func (l *triggerRateLimiter) allow(name string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[name]
	if !ok {
		if len(l.buckets) >= triggerLimiterMax {
			l.dropFullLocked(now)
		}
		if len(l.buckets) >= triggerLimiterMax {
			return false // every bucket is in use; fail closed
		}
		b = &triggerBucket{tokens: l.perHour, at: now}
		l.buckets[name] = b
	}
	b.tokens = min(l.perHour, b.tokens+now.Sub(b.at).Hours()*l.perHour)
	b.at = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// dropFullLocked forgets every bucket that would be full by now.
func (l *triggerRateLimiter) dropFullLocked(now time.Time) {
	for name, b := range l.buckets {
		if b.tokens+now.Sub(b.at).Hours()*l.perHour >= l.perHour {
			delete(l.buckets, name)
		}
	}
}
