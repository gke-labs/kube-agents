package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"slices"
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
// An event source (#6572) submits the same way, with a trigger named
// <source>/<kind> and no schedule, and only for a kind the install admits
// (TriggerEventKinds, empty by default). Each run has its own conversation,
// trigger:<key>, and each kind its own rate bucket.
//
// Only a source the callout grants publish on the subject can reach it: the
// operator, as the schedule source, while A2A_AGENT_SCHEDULES is on (#5617).
// No event source has that grant yet.

const (
	// triggerSubmitWildcard is every source's submission subject.
	triggerSubmitWildcard = lib.TriggerSubmitSubjectPrefix + "*" + lib.TriggerSubmitSubjectSuffix
	// triggerStatusWildcard is every source's status query subject.
	triggerStatusWildcard = lib.TriggerSubmitSubjectPrefix + "*" + lib.TriggerStatusSubjectSuffix
	// triggerStopWildcard is every source's stop subject.
	triggerStopWildcard = lib.TriggerSubmitSubjectPrefix + "*" + lib.TriggerStopSubjectSuffix
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
	// DefaultTriggerEventRatePerHour is each admitted event kind's cap when
	// the config sets none (#6572): above the largest daily alert quota the
	// session store applies to one kind before it asks for a triage, so one
	// day's alerts pass in a burst and a runaway source is held to the cap
	// on each kind the install admits.
	DefaultTriggerEventRatePerHour = 30
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
	g.startTriggerStop(ctx)
	denied := make(chan struct{}, 1)
	remove := g.client.OnAsyncError(func(err error) {
		if isTriggerSubscriptionRefusal(err) {
			select {
			case denied <- struct{}{}:
			default:
			}
		}
	})
	go func() {
		defer remove()
		for {
			// A refusal from a previous attempt's subscriptions is spent.
			select {
			case <-denied:
			default:
			}
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
				status, serr := g.client.SubscribeCore(triggerStatusWildcard, func(m *nats.Msg) {
					source, err := lib.ParseTriggerStatusSubject(m.Subject)
					if err != nil || !lib.ValidTriggerReplySubject(source, m.Reply) {
						g.log.Info("trigger status query dropped: no reply subject in the source's namespace", "subject", m.Subject)
						return
					}
					if data, err := json.Marshal(g.handleTriggerStatus(ctx, m.Subject, m.Data)); err == nil {
						if err := m.Respond(data); err != nil {
							g.log.Warn("trigger status reply failed", "subject", m.Subject, "err", err)
						}
					}
				})
				if serr != nil {
					sub.Stop()
					err = serr
				} else {
					// A refused SUB arrives after the flush, as an async
					// permissions violation (the bus hasn't loaded the
					// gateway's trigger grants yet, say, on an upgrade that
					// rolled the gateway first). A server reload doesn't
					// revive a refused subscription, so drop both and
					// subscribe again after the retry wait.
					select {
					case <-ctx.Done():
						sub.Stop()
						status.Stop()
						return
					case <-denied:
						sub.Stop()
						status.Stop()
						err = errTriggerSubscriptionRefused
					}
				}
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

// errTriggerSubscriptionRefused is the retry's reason when the server refused
// a trigger subscription after accepting the SUB.
var errTriggerSubscriptionRefused = errors.New("the server refused a trigger subscription (permissions violation)")

// isTriggerSubscriptionRefusal reports whether err is the server refusing a
// subscription on the trigger subjects. nats.go hands these with a nil
// subscription, so the subject comes from the error text.
func isTriggerSubscriptionRefusal(err error) bool {
	if err == nil {
		return false
	}
	text := err.Error()
	return (errors.Is(err, nats.ErrPermissionViolation) || strings.Contains(text, "Permissions Violation")) &&
		strings.Contains(text, "Subscription to \"a2a.trigger.") && !strings.Contains(text, "Subscription to \""+triggerStopWildcard+"\"")
}

// isTriggerStopSubscriptionRefusal reports whether err is the server refusing
// the stop subscription, which is retried on its own.
func isTriggerStopSubscriptionRefusal(err error) bool {
	if err == nil {
		return false
	}
	text := err.Error()
	return (errors.Is(err, nats.ErrPermissionViolation) || strings.Contains(text, "Permissions Violation")) &&
		strings.Contains(text, "Subscription to \""+triggerStopWildcard+"\"")
}

// startTriggerStop subscribes the elected gateway to every source's stop
// subject (#6052), on its own retry loop: the stop grant is newer than the
// submit and status ones, so a bus that hasn't loaded it yet refuses this
// subscription alone, and submissions and status queries carry on meanwhile.
func (g *Gateway) startTriggerStop(ctx context.Context) {
	denied := make(chan struct{}, 1)
	remove := g.client.OnAsyncError(func(err error) {
		if isTriggerStopSubscriptionRefusal(err) {
			select {
			case denied <- struct{}{}:
			default:
			}
		}
	})
	go func() {
		defer remove()
		for {
			select {
			case <-denied:
			default:
			}
			sub, err := g.client.SubscribeCore(triggerStopWildcard, func(m *nats.Msg) {
				source, err := lib.ParseTriggerStopSubject(m.Subject)
				if err != nil || !lib.ValidTriggerReplySubject(source, m.Reply) {
					g.log.Info("trigger stop dropped: no reply subject in the source's namespace", "subject", m.Subject)
					return
				}
				if data, err := json.Marshal(g.handleTriggerStop(ctx, m.Subject, m.Data)); err == nil {
					if err := m.Respond(data); err != nil {
						g.log.Warn("trigger stop reply failed", "subject", m.Subject, "err", err)
					}
				}
			})
			if err == nil {
				select {
				case <-ctx.Done():
					sub.Stop()
					return
				case <-denied:
					sub.Stop()
					err = errTriggerSubscriptionRefused
				}
			}
			g.log.Warn("trigger stop subscription failed; retrying", "err", err, "in", triggerIngressRetry)
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
	// A schedule's trigger identity is schedule/<name>, with no namespace in
	// it: the grant, the report stream it binds and the rate cap all key on
	// it. A built-in schedule lives in the install's own namespace (one
	// PlatformAgent per cluster, which owns them), so a schedule anywhere
	// else is refused, and two namespaces never share a trigger. An event
	// trigger names no schedule; it must be a kind the install admits.
	event := source != lib.TriggerSourceSchedule
	switch {
	case event && !slices.Contains(g.cfg.TriggerEventKinds, s.Trigger.Name):
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "event trigger "+s.Trigger.Name+" is not one this install admits")
	case !event && (g.cfg.Namespace == "" || s.Schedule.Namespace != g.cfg.Namespace):
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "schedule "+s.Schedule.Namespace+"/"+s.Schedule.Name+" is not in the install's namespace")
	}
	key := triggerConversationKey(source, s.Schedule)
	bucket := key
	if event {
		// An event run is its own conversation, so its delivery and
		// active task are its own and its key is answered for as long as
		// the record lives; its kind is its rate bucket.
		key = triggerConversationPrefix + s.IdempotencyKey
		bucket = triggerConversationPrefix + s.Trigger.Name
	}
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
	// A run already on the bus under this key is that run, answered with its
	// task before any directory check: it needs nothing from the directory,
	// and a new leader that hasn't read DIRECTORY yet, or a profile deleted
	// since, would otherwise answer its retry profile_unknown in place of
	// the task it started.
	if run := rec.triggerRun(s.IdempotencyKey); run != nil && run.Published {
		return lib.TriggerSubmitReply{TaskID: run.Task}
	}
	// So is one published within the key record's 24 hours whose run has
	// since left TriggerRuns. A run still in TriggerRuns, pending, is
	// published again below instead.
	if rec.triggerRun(s.IdempotencyKey) == nil {
		if task, ok := rec.triggerKeyTask(s.IdempotencyKey, time.Now()); ok {
			return lib.TriggerSubmitReply{TaskID: task}
		}
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
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the submission's target routes to profile "+route.Profile+", not "+s.Profile)
	}

	// A run recorded but not yet published (its publish failed, or the
	// gateway stopped) is published again with the same grant and envelope
	// id, so the stream dedupes a publish that had in fact landed. It
	// takes the route checks above, since it does reach the dispatcher.
	// Neither it nor a published run mints or spends the rate budget.
	if run := rec.triggerRun(s.IdempotencyKey); run != nil {
		return g.publishTriggerRun(ctx, rec, s, route, *run)
	}

	limiter := g.triggerLimiterFor(event)
	if !limiter.allow(bucket, time.Now()) {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedRateLimited, "the trigger is over its rate cap")
	}
	// Every failure from here until the run is recorded hands the token back:
	// the attempt count, not the rate budget, is what bounds the retries of
	// one key (#5923), and it is kept in the record.
	refuse := func(reason, detail string) lib.TriggerSubmitReply {
		limiter.refund(bucket, time.Now())
		return g.triggerRefused(s.Trigger.Name, reason, detail)
	}
	if rec == nil {
		rec = &SessionRecord{Key: key, ContextID: "ctx-" + randHex(contextIDHexWidth), Kind: triggerAudienceKind, LastActivity: time.Now().UTC()}
		if err := g.reg.Create(ctx, rec); err != nil {
			if !errors.Is(err, ErrSessionExists) {
				return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
			}
			if rec, err = g.reg.Get(ctx, key); err != nil || rec == nil {
				return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
			}
		}
	}
	// The cap bucket's writes are create-only, so a key an earlier attempt
	// minted can't be minted again. The attempt is recorded before each mint
	// (TriggerPending), so a retry after a record-write failure or a restart
	// resumes there, and each failure costs one attempt. An attempt that was
	// never recorded as a run was never published (the run is recorded before
	// its publish), so the next attempt's task id is safe to use.
	trigger := &capability.Trigger{Name: s.Trigger.Name, Origin: s.Trigger.Origin, Stream: s.Trigger.Stream, Script: s.Trigger.Script}
	attempt := rec.TriggerPending[s.IdempotencyKey].Attempt
	var run TriggerRun
	for {
		if attempt >= triggerMintAttempts {
			g.log.Error("trigger run out of mint attempts", "trigger", s.Trigger.Name, "idempotencyKey", s.IdempotencyKey, "attempts", attempt)
			return refuse(lib.TriggerRefusedUnavailable, "the run's task grants could not be minted")
		}
		taskID := triggerTaskID(s.IdempotencyKey, attempt)
		if p, ok := rec.TriggerPending[s.IdempotencyKey]; !ok || p.Attempt != attempt {
			rec.setTriggerPending(s.IdempotencyKey, TriggerPendingRun{Attempt: attempt, Task: taskID})
			if err := g.reg.Put(ctx, rec); err != nil {
				g.log.Error("trigger attempt write failed", "conversation", rec.Key, "taskId", taskID, "err", err)
				return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
			}
		}
		minted, err := g.mintTriggerGrant(ctx, taskID, capability.Entry{
			Tier:     capability.TierUnattended,
			Scope:    route.Scope,
			Profile:  route.Profile,
			Delegate: profileGrantDelegate,
			Trigger:  trigger,
		})
		if err == nil {
			run = TriggerRun{Key: s.IdempotencyKey, Task: taskID, Capability: &minted, CorrelationID: "corr-" + randHex(contextIDHexWidth), Trigger: trigger}
			break
		}
		if isMintedAlready(err) {
			// The next attempt is recorded before anything else, so a retry
			// never mints this one again, even once the cap is reached.
			g.log.Warn("trigger task grant already minted by an unrecorded attempt; minting the next", "trigger", s.Trigger.Name, "taskId", taskID)
			attempt++
			rec.setTriggerPending(s.IdempotencyKey, TriggerPendingRun{Attempt: attempt, Task: triggerTaskID(s.IdempotencyKey, attempt)})
			if err := g.reg.Put(ctx, rec); err != nil {
				g.log.Error("trigger attempt write failed", "conversation", rec.Key, "taskId", taskID, "err", err)
				return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
			}
			continue
		}
		g.log.Error("trigger task capability mint failed", "trigger", s.Trigger.Name, "taskId", taskID, "err", err)
		return refuse(lib.TriggerRefusedUnavailable, "the task's grant could not be minted")
	}

	// Recorded, pending, before its publish: a retry after anything below
	// fails publishes this run again rather than minting another.
	// The key record goes in the same write: a publish whose mark write
	// fails leaves the run unmarked, and once newer runs push it out of
	// TriggerRuns the key must still answer with this task. Answering an
	// unpublished run's key with its task fails closed (the reconciler reads
	// it as lost).
	delete(rec.TriggerPending, s.IdempotencyKey)
	rec.addTriggerRun(run)
	rec.recordTriggerKey(run.Key, run.Task, time.Now(), g.triggerKeysKept())
	if err := g.reg.Put(ctx, rec); err != nil {
		// A write that failed but landed refunds a token it used: the retry
		// republishes the recorded run without spending one. That lets one
		// run past the cap per such failure, never a second task for the key.
		g.log.Error("trigger record write failed", "conversation", rec.Key, "taskId", run.Task, "err", err)
		return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
	}
	return g.publishTriggerRun(ctx, rec, s, route, run)
}

// publishTriggerRun registers run where the relay looks, publishes its
// submission and marks it published. Called under the trigger's lock with
// run already recorded (pending) on rec.
func (g *Gateway) publishTriggerRun(ctx context.Context, rec *SessionRecord, s lib.TriggerSubmit, route *profileRoute, run TriggerRun) lib.TriggerSubmitReply {
	taskID := run.Task
	trigger := run.Trigger
	if trigger == nil {
		trigger = &capability.Trigger{Name: s.Trigger.Name, Origin: s.Trigger.Origin, Stream: s.Trigger.Stream, Script: s.Trigger.Script}
	}
	authority := g.triggerAuthority(rec, s.Trigger.Name, trigger)
	payload, err := submissionPayload(triggerTaskText(s), taskID, rec.ContextID, route, false)
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
	delivery := s.Delivery
	rec.Delivery = &delivery
	rec.ActiveTask = &ActiveTask{TaskID: taskID, CorrelationID: run.CorrelationID, Ask: truncateRunes(s.Prompt, askCap),
		SubmittedAt: now, Capability: run.Capability}
	if !rec.hasTask(taskID) {
		rec.Tasks = append(append([]TaskRef(nil), rec.Tasks...), TaskRef{
			ID: taskID, Addressee: route.Profile, CorrelationID: run.CorrelationID, Capability: run.Capability,
			Requester: &requester, Attribution: authority.Attribution(), StartedAt: now.UTC(),
			RootTaskID: taskID, Tier: string(capability.TierUnattended), Request: capAsk(s.Prompt),
			Delivery: &delivery, TriggerStream: s.Trigger.Stream, TriggerOrigin: s.Trigger.Origin,
			// Its scope, recorded as a profile task's is: the scope its
			// children are held to when it fans out (#6744).
			Scope: string(route.Scope),
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

// handleTriggerStatus answers what state one of a schedule's tasks is in,
// for a task this gateway recorded on that schedule's trigger record and no
// other, so a source reads only the tasks it started. A recorded run with no
// events yet is submitted; a task the record no longer holds is not found,
// which the reconciler reads as Lost.
func (g *Gateway) handleTriggerStatus(ctx context.Context, subject string, data []byte) lib.TriggerStatusReply {
	refuse := func(reason, detail string) lib.TriggerStatusReply {
		return lib.TriggerStatusReply{Refused: &lib.TriggerRefusal{Reason: reason, Detail: detail}}
	}
	source, err := lib.ParseTriggerStatusSubject(subject)
	if err != nil {
		return refuse(lib.TriggerRefusedInvalid, err.Error())
	}
	var q lib.TriggerStatus
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&q); err != nil {
		return refuse(lib.TriggerRefusedInvalid, "the query is not well-formed")
	}
	if _, err := dec.Token(); err != io.EOF {
		return refuse(lib.TriggerRefusedInvalid, "the query has data after its object")
	}
	if err := lib.ValidateTriggerStatus(source, q); err != nil {
		return refuse(lib.TriggerRefusedInvalid, err.Error())
	}
	if g.cfg.Namespace == "" || q.Schedule.Namespace != g.cfg.Namespace {
		return refuse(lib.TriggerRefusedInvalid, "schedule "+q.Schedule.Namespace+"/"+q.Schedule.Name+" is not in the install's namespace")
	}
	rec, err := g.reg.Get(ctx, triggerConversationKey(source, q.Schedule))
	if err != nil {
		return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
	}
	var run *TriggerRun
	if rec != nil {
		for i := range rec.TriggerRuns {
			if rec.TriggerRuns[i].Task == q.TaskID {
				run = &rec.TriggerRuns[i]
			}
		}
	}
	if run == nil {
		return lib.TriggerStatusReply{Found: false}
	}
	addressee := ""
	for _, t := range rec.Tasks {
		if t.ID == q.TaskID {
			addressee = t.Addressee
		}
	}
	if addressee == "" || !run.Published {
		return lib.TriggerStatusReply{Found: true, State: lib.StateSubmitted}
	}
	task, err := g.replayForTerminal(ctx, addressee, q.TaskID, g.taskIsProfile(rec, q.TaskID, addressee))
	if err != nil || task == nil || task.State == "" {
		return lib.TriggerStatusReply{Found: true, State: lib.StateSubmitted}
	}
	out := lib.TriggerStatusReply{Found: true, State: task.State, Terminal: task.Final}
	if task.Final {
		// How the run's report was delivered, from its SCHEDULED_REPORTS
		// record (scheduled_reports.go): omitted while there is none or it
		// can't be read, so the reconciler asks again.
		if delivery, err := g.scheduledRunStatus(ctx, rec, q.TaskID); err != nil {
			g.log.Warn("scheduled report status unreadable; the status reply omits it", "taskId", q.TaskID, "err", err)
		} else {
			out.Delivery = delivery
		}
	}
	return out
}

// triggerAuthority is the authority a trigger's task rides: the trigger as
// requester, its own conversation as audience, and the trigger the grant was
// minted with. The run's cancel rides the same.
func (g *Gateway) triggerAuthority(rec *SessionRecord, triggerName string, trigger *capability.Trigger) Authority {
	return Authority{
		Requester: AuthorityRequester{
			Principal:  g.ps.Hash("trigger:" + triggerName),
			Backend:    triggerRequesterBackend,
			Subject:    triggerName,
			VerifiedBy: triggerRequesterVerifiedBy,
		},
		Audience: AuthorityAudience{Conversation: rec.Key, Kind: triggerAudienceKind},
		Trigger:  trigger,
	}
}

// handleTriggerStop ends one of a schedule's tasks (#6052), for a task this
// gateway recorded on that schedule's trigger record and no other, so a
// source stops only the tasks it started. It writes the task's cancel on its
// .in subject and its dispatcher stop record: a run still starting has its
// Job removed, and a running one is canceled. A task already ended, or one
// already stopped, writes nothing again.
func (g *Gateway) handleTriggerStop(ctx context.Context, subject string, data []byte) lib.TriggerStopReply {
	refuse := func(reason, detail string) lib.TriggerStopReply {
		return lib.TriggerStopReply{Refused: &lib.TriggerRefusal{Reason: reason, Detail: detail}}
	}
	source, err := lib.ParseTriggerStopSubject(subject)
	if err != nil {
		return refuse(lib.TriggerRefusedInvalid, err.Error())
	}
	var q lib.TriggerStop
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&q); err != nil {
		return refuse(lib.TriggerRefusedInvalid, "the stop is not well-formed")
	}
	if _, err := dec.Token(); err != io.EOF {
		return refuse(lib.TriggerRefusedInvalid, "the stop has data after its object")
	}
	if err := lib.ValidateTriggerStop(source, q); err != nil {
		return refuse(lib.TriggerRefusedInvalid, err.Error())
	}
	if g.cfg.Namespace == "" || q.Schedule.Namespace != g.cfg.Namespace {
		return refuse(lib.TriggerRefusedInvalid, "schedule "+q.Schedule.Namespace+"/"+q.Schedule.Name+" is not in the install's namespace")
	}
	key := triggerConversationKey(source, q.Schedule)
	// Under the trigger's lock, as a submission and the relay's batches are:
	// the cancel mark written back is on the record they read.
	lock := g.lockSession(key)
	lock.Lock()
	defer lock.Unlock()
	rec, err := g.reg.Get(ctx, key)
	if err != nil {
		return refuse(lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
	}
	var run *TriggerRun
	if rec != nil {
		for i := range rec.TriggerRuns {
			if rec.TriggerRuns[i].Task == q.TaskID {
				run = &rec.TriggerRuns[i]
			}
		}
	}
	// Only a published run has a task to stop: a source learns a task id
	// from the answer to a submission that reached the bus.
	if run == nil || !run.Published {
		return lib.TriggerStopReply{Result: lib.TriggerStopUnknownRun}
	}
	ref, held := rec.TaskRefFor(q.TaskID)
	if !held {
		return lib.TriggerStopReply{Result: lib.TriggerStopUnknownRun}
	}
	if task, err := g.replayForTerminal(ctx, ref.Addressee, q.TaskID, ref.Tier != ""); err == nil && task != nil && task.Final {
		return lib.TriggerStopReply{Result: lib.TriggerStopAlreadyFinal}
	}
	if ref.Canceled {
		return lib.TriggerStopReply{Result: lib.TriggerStopStopped}
	}
	if run.Trigger == nil {
		// A run recorded before its trigger was kept: the cancel's authority
		// has to equal the grant's trigger, which this record can't say.
		return refuse(lib.TriggerRefusedInvalid, "the run predates the record of its trigger; it ends at its deadline")
	}
	correlation := ref.CorrelationID
	if correlation == "" {
		correlation = run.CorrelationID
	}
	authority := g.triggerAuthority(rec, run.Trigger.Name, run.Trigger)
	env, err := lib.NewCancelEnvelope(gatewayParty, q.TaskID, rec.ContextID, correlation,
		lib.WithTo(lib.Party{Session: ref.Addressee}),
		lib.WithAuthority(authority.Render(run.Capability)))
	if err != nil {
		g.log.Error("trigger stop envelope build failed", "taskId", q.TaskID, "err", err)
		return refuse(lib.TriggerRefusedInvalid, "the cancel could not be built")
	}
	// The stop record first: the dispatcher reads that, not the cancel, for
	// a submission it still holds queued or a Job still starting, so it is
	// the half the stop exists for. Either publish failing is refused
	// unavailable with nothing marked, so the operator asks again and both
	// are sent again (the record is one per subject, and a second cancel for
	// the same task is answered as the first).
	if ref.Tier != "" {
		if err := g.client.PublishDispatchStop(ctx, ref.Addressee, ref.ID); err != nil {
			g.log.Error("trigger stop record publish failed", "taskId", q.TaskID, "profile", ref.Addressee, "err", err)
			return refuse(lib.TriggerRefusedUnavailable, "the stop record could not be published")
		}
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(ref.Addressee, q.TaskID), env); err != nil {
		g.log.Error("trigger stop cancel publish failed", "taskId", q.TaskID, "err", err)
		return refuse(lib.TriggerRefusedUnavailable, "the cancel could not be published")
	}
	rec.MarkCanceled(q.TaskID)
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("trigger stop record write failed", "conversation", rec.Key, "taskId", q.TaskID, "err", err)
	}
	g.log.Info("trigger task stopped", "conversation", rec.Key, "taskId", q.TaskID, "profile", ref.Addressee)
	return lib.TriggerStopReply{Result: lib.TriggerStopStopped}
}

// triggerConversationKey is a trigger's own conversation:
// trigger:<source>/<namespace>/<name>.
func triggerConversationKey(source string, s lib.ScheduleRef) string {
	return triggerConversationPrefix + source + "/" + s.Namespace + "/" + s.Name
}

// unpostableTrigger reports whether conversation is a trigger's own, which
// no chat backend owns: the relay posts nothing there (a backend handed the
// key would refuse it, or worse, read it as one of its own). A schedule's
// result goes to the home channel, or nowhere, by the record's Delivery
// (deliverScheduledReport, from relayTerminal).
func (g *Gateway) unpostableTrigger(conversation string) bool {
	if !strings.HasPrefix(conversation, triggerConversationPrefix) {
		return false
	}
	g.log.Debug("nothing posted to a trigger's conversation", "conversation", conversation)
	return true
}

// triggerRunsKept bounds SessionRecord.TriggerRuns: a reconciler retries a
// slot only inside its starting deadline, so the recent runs are enough. It
// is taskHistoryCap, never more: a status query reads a run's addressee from
// the record's Tasks, which keeps that many, and a run kept here but trimmed
// there would answer "submitted" until the reconciler gave it up as lost.
const triggerRunsKept = taskHistoryCap

// triggerMintAttempts bounds the task ids one run may try, counting every
// attempt the record holds (TriggerPending), refunded or not: an earlier,
// unrecorded attempt leaves its grant key taken. Sixteen covers the
// reconciler's retries across one slot's starting deadline.
const triggerMintAttempts = 16

// triggerKeyTTL is how long a published run's key is answered with its task
// after the run has left TriggerRuns: longer than any reconciler's retries of
// one slot (#5617 asks for 24 hours).
const triggerKeyTTL = 24 * time.Hour

// triggerKeysKeptMin is the least TriggerKeys keeps whatever the clock does.
// The bound is a day of runs at the configured rate cap (triggerKeysKept), so
// no key a reconciler could still retry is evicted early.
const triggerKeysKeptMin = 512

// triggerPendingKept bounds TriggerPending: one entry per slot still being
// retried, which the reconciler keeps to one at a time per schedule (Forbid).
const triggerPendingKept = 64

// triggerKeysKept is how many published keys a trigger's record keeps: a
// day's worth at the rate cap, and at least triggerKeysKeptMin.
func (g *Gateway) triggerKeysKept() int {
	rate := g.cfg.TriggerRatePerHour
	if rate <= 0 {
		rate = DefaultTriggerRatePerHour
	}
	return max(triggerKeysKeptMin, rate*int(triggerKeyTTL/time.Hour)+1)
}

// mintTriggerGrant mints a trigger run's grant: triggerMint when a test sets
// it, the cap bucket's minter otherwise.
func (g *Gateway) mintTriggerGrant(ctx context.Context, taskID string, e capability.Entry) (capability.Ref, error) {
	if g.triggerMint != nil {
		return g.triggerMint(ctx, taskID, e)
	}
	return capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, e)
}

// setTriggerPending records key's mint attempt.
func (rec *SessionRecord) setTriggerPending(key string, p TriggerPendingRun) {
	if rec.TriggerPending == nil {
		rec.TriggerPending = map[string]TriggerPendingRun{}
	}
	if p.At.IsZero() {
		p.At = time.Now().UTC()
	}
	rec.TriggerPending[key] = p
	// A key a reconciler gave up on (its slot past the starting deadline, or
	// its attempts spent) is never recorded as a run, so its entry is dropped
	// here once it is a day old, then the oldest past triggerPendingKept.
	for k, v := range rec.TriggerPending {
		if k != key && p.At.Sub(v.At) > triggerKeyTTL {
			delete(rec.TriggerPending, k)
		}
	}
	for len(rec.TriggerPending) > triggerPendingKept {
		oldest := ""
		for k, v := range rec.TriggerPending {
			if k != key && (oldest == "" || v.At.Before(rec.TriggerPending[oldest].At)) {
				oldest = k
			}
		}
		delete(rec.TriggerPending, oldest)
	}
}

// triggerKeyTask is the task a run published under key within triggerKeyTTL
// started.
func (rec *SessionRecord) triggerKeyTask(key string, now time.Time) (string, bool) {
	if rec == nil {
		return "", false
	}
	k, ok := rec.TriggerKeys[key]
	if !ok || now.Sub(k.At) > triggerKeyTTL {
		return "", false
	}
	return k.Task, true
}

// recordTriggerKey records a published run's key, dropping keys older than
// triggerKeyTTL, then the oldest past triggerKeysKept.
func (rec *SessionRecord) recordTriggerKey(key, task string, now time.Time, kept int) {
	if rec.TriggerKeys == nil {
		rec.TriggerKeys = map[string]TriggerKeyRecord{}
	}
	rec.TriggerKeys[key] = TriggerKeyRecord{Task: task, At: now.UTC()}
	for k, v := range rec.TriggerKeys {
		if now.Sub(v.At) > triggerKeyTTL {
			delete(rec.TriggerKeys, k)
		}
	}
	for len(rec.TriggerKeys) > kept {
		oldest := ""
		for k, v := range rec.TriggerKeys {
			if oldest == "" || v.At.Before(rec.TriggerKeys[oldest].At) {
				oldest = k
			}
		}
		delete(rec.TriggerKeys, oldest)
	}
}

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

// triggerLimiterFor is the schedules' limiter, or for an event source the
// event kinds', made on first use.
func (g *Gateway) triggerLimiterFor(event bool) *triggerRateLimiter {
	if !event {
		return g.triggerLimiter()
	}
	g.triggerEventLimiterOnce.Do(func() {
		rate := g.cfg.TriggerEventRatePerHour
		if rate <= 0 {
			rate = DefaultTriggerEventRatePerHour
		}
		g.triggerEventLimits = newTriggerRateLimiter(rate)
	})
	return g.triggerEventLimits
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

// refund gives name back the token a submission spent and didn't use (it
// failed before its run was recorded), never beyond the bucket's size.
func (l *triggerRateLimiter) refund(name string, now time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	b, ok := l.buckets[name]
	if !ok {
		return
	}
	b.tokens = min(l.perHour, b.tokens+now.Sub(b.at).Hours()*l.perHour+1)
	b.at = now
}

// dropFullLocked forgets every bucket that would be full by now.
func (l *triggerRateLimiter) dropFullLocked(now time.Time) {
	for name, b := range l.buckets {
		if b.tokens+now.Sub(b.at).Hours()*l.perHour >= l.perHour {
			delete(l.buckets, name)
		}
	}
}
