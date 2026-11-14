package gateway

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"

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
				reply := g.handleTriggerSubmit(ctx, m.Subject, m.Data)
				if m.Reply == "" {
					return
				}
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
	if err := lib.ValidateTriggerSubmit(source, s); err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, err.Error())
	}
	key := triggerConversationKey(source, s.Schedule)

	// A repeat of the last submission is the same run: answered with its
	// task, ahead of the rate cap, so a retry never spends the budget.
	rec, err := g.reg.Get(ctx, key)
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be read")
	}
	if rec != nil && rec.TriggerKey == s.IdempotencyKey && rec.TriggerTask != "" {
		return lib.TriggerSubmitReply{TaskID: rec.TriggerTask}
	}

	card, ok := g.profiles.lookup(s.Profile)
	if !ok {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedProfileUnknown, "no profile "+s.Profile+" in the directory")
	}
	if !cardAllowsTier(card, capability.TierUnattended) {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedTierNotAllowed, "profile "+s.Profile+" does not allow the unattended tier")
	}
	if !g.triggerLimiter().allow(s.Trigger.Name, time.Now()) {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedRateLimited, "the trigger is over its rate cap")
	}

	taskID := triggerTaskID(s.IdempotencyKey)
	trigger := &capability.Trigger{Name: s.Trigger.Name, Origin: s.Trigger.Origin, Stream: s.Trigger.Stream}
	minted, err := capability.NewMinter(g.client.JetStream()).Mint(ctx, taskID, capability.Entry{
		Tier:     capability.TierUnattended,
		Scope:    g.cfg.AuthorityScope,
		Profile:  s.Profile,
		Delegate: profileGrantDelegate,
		Trigger:  trigger,
	})
	if err != nil {
		g.log.Error("trigger task capability mint failed", "trigger", s.Trigger.Name, "taskId", taskID, "err", err)
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the task's grant could not be minted")
	}
	capRef := &minted

	if rec == nil {
		rec = &SessionRecord{Key: key, ContextID: "ctx-" + randHex(contextIDHexWidth), Kind: triggerAudienceKind}
		if err := g.reg.Create(ctx, rec); err != nil && !errors.Is(err, ErrSessionExists) {
			return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the trigger's record could not be written")
		}
	}
	authority := Authority{
		Requester: AuthorityRequester{
			Principal:  g.ps.Hash("trigger:" + s.Trigger.Name),
			Backend:    triggerRequesterBackend,
			Subject:    s.Trigger.Name,
			VerifiedBy: triggerRequesterVerifiedBy,
		},
		Audience: AuthorityAudience{Conversation: key, Kind: triggerAudienceKind},
		Trigger:  trigger,
	}
	route := &profileRoute{Profile: s.Profile, Tier: capability.TierUnattended, Kind: lib.SubmitKindTop, Params: s.Parameters}
	payload, err := submissionPayload(triggerTaskText(s), taskID, rec.ContextID, route)
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the task's message could not be built")
	}
	correlationID := "corr-" + randHex(contextIDHexWidth)
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID, correlationID, payload,
		lib.WithTo(lib.Party{Session: s.Profile}),
		lib.WithAuthority(authority.Render(capRef)),
		// The idempotency key's envelope id: a publish repeated after a
		// crash between the publish and the record's write is the stream's
		// duplicate, not a second task.
		lib.WithEnvelopeID("env-"+taskID))
	if err != nil {
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedInvalid, "the task's envelope could not be built")
	}

	// Registered where the relay looks before the publish, as startTask
	// does, so the executor's first event finds its conversation.
	requester := TaskRequester{Backend: triggerRequesterBackend, Subject: s.Trigger.Name}
	rec.Delivery = &s.Delivery
	rec.ActiveTask = &ActiveTask{TaskID: taskID, CorrelationID: correlationID, Ask: truncateRunes(s.Prompt, askCap),
		SubmittedAt: time.Now(), Capability: capRef}
	rec.Tasks = append(rec.Tasks, TaskRef{
		ID: taskID, Addressee: s.Profile, CorrelationID: correlationID, Capability: capRef,
		Requester: &requester, Attribution: authority.Attribution(), StartedAt: time.Now().UTC(),
		RootTaskID: taskID, Tier: string(capability.TierUnattended), Request: capAsk(s.Prompt),
	})
	if len(rec.Tasks) > taskHistoryCap {
		rec.Tasks = rec.Tasks[len(rec.Tasks)-taskHistoryCap:]
	}
	rec.LastTaskActivity = time.Now().UTC()
	g.mu.Lock()
	g.taskSessions[taskID] = rec.Key
	g.relays[taskID] = &relayState{local: true}
	g.mu.Unlock()
	if err := g.reg.IndexTask(ctx, taskID, rec.Key); err != nil {
		g.log.Error("trigger task index write failed", "taskId", taskID, "err", err)
	}
	if _, err := g.client.PublishSeq(ctx, lib.TaskSubmitSubject(s.Profile, string(capability.TierUnattended), lib.SubmitKindTop, taskID), env); err != nil {
		g.log.Error("trigger task publish failed", "trigger", s.Trigger.Name, "taskId", taskID, "err", err)
		g.mu.Lock()
		delete(g.taskSessions, taskID)
		delete(g.relays, taskID)
		g.mu.Unlock()
		return g.triggerRefused(s.Trigger.Name, lib.TriggerRefusedUnavailable, "the task could not be published")
	}
	// The run is on the bus: only now does the record name it, so a repeat
	// after a failed publish starts the run rather than answering with a
	// task that never went out.
	rec.TriggerKey, rec.TriggerTask = s.IdempotencyKey, taskID
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("trigger record write failed", "conversation", rec.Key, "taskId", taskID, "err", err)
	}
	g.log.Info("trigger task started", "trigger", s.Trigger.Name, "taskId", taskID, "profile", s.Profile,
		"idempotencyKey", s.IdempotencyKey)
	return lib.TriggerSubmitReply{TaskID: taskID}
}

// triggerConversationKey is a trigger's own conversation:
// trigger:<source>/<namespace>/<name>.
func triggerConversationKey(source string, s lib.ScheduleRef) string {
	return triggerConversationPrefix + source + "/" + s.Namespace + "/" + s.Name
}

// triggerTaskID derives the run's task id from its idempotency key, so the
// same run always names the same task (and the same grant key).
func triggerTaskID(idempotencyKey string) string {
	sum := sha256.Sum256([]byte("trigger-run\x00" + idempotencyKey))
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
