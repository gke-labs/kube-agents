package gateway

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// turnTimeout bounds one handler turn — an inbound message or a relay
// batch — so a stuck bus or backend call frees the conversation's queue
// slot instead of holding it forever.
const turnTimeout = 60 * time.Second

// relayDurable is the event relay's durable consumer name; see
// Options.RelayDurable for the one caller that may not share it.
const relayDurable = "gateway-relay"

// Hex-suffix widths for the ids the gateway mints. Context and correlation
// ids are wider than task and message ids: they outlive one task and join
// records across surfaces, so a collision costs more.
const (
	taskIDHexWidth        = 8
	messageIDHexWidth     = 8
	contextIDHexWidth     = 12
	correlationIDHexWidth = 12
)

// gatewayParty is the gateway's own identity in from — routing and display
// only, never an authorization input. Its supervisor events carry it so
// replay always distinguishes "the worker said failed" from "the supervisor
// declared it dead".
var gatewayParty = lib.Party{Session: "gateway", AgentType: "a2a-gateway"}

// Gateway wires the adapter, the session manager, and the bus client.
type Gateway struct {
	cfg     *Config
	client  *lib.Client
	reg     *Registry
	adapter Adapter
	pm      *PrincipalMap
	ps      *Pseudonymizer
	log     *slog.Logger
	spawner spawner // nil until SpawnSessions arms (W4)

	// runCtx is Run's context; queue workers derive their timeouts from it.
	runCtx context.Context

	// inbox orders inbound messages per conversation (the backend delivers
	// events on unordered goroutines) and events orders relay work per
	// session, so no conversation can block another.
	inbox  *keyedQueue[InboundMessage]
	events *keyedQueue[*lib.Envelope]

	mu sync.Mutex
	// sessionLocks serializes work per conversation; tasks serialize per
	// session by construction (a message during a running task is a steer,
	// never a second task).
	sessionLocks map[string]*sync.Mutex
	// taskSessions caches taskId -> session key; the KV task index is the
	// durable copy a restart falls back to. Entries retire with the task.
	taskSessions map[string]string
	// relays holds per-task render state for the rolling progress line.
	relays map[string]*relayState

	// backend names the chat backend for authority blocks.
	backend string
	// relayDurable is the event relay's durable name (Options.RelayDurable).
	relayDurable string
}

// Options are the injectable pieces; tests provide fakes.
type Options struct {
	Client  *lib.Client
	Adapter Adapter
	Config  *Config
	Logger  *slog.Logger
	Backend string
	// Spawner overrides the k8s-backed pod spawner - test injection only.
	Spawner spawner
	// RelayDurable overrides the event relay's durable consumer name (the
	// default relayDurable). Two gateways bound to one durable SPLIT the
	// event deliveries - and this relay acks what it cannot route - so an
	// in-process gateway pointed at an install with a running gateway pod
	// (the live tests) must bind its own durable or the two starve each
	// other probabilistically. The deployed binary never sets this.
	RelayDurable string
}

// New assembles a gateway.
func New(o Options) (*Gateway, error) {
	if o.Client == nil || o.Adapter == nil || o.Config == nil {
		return nil, fmt.Errorf("gateway needs a client, an adapter, and a config")
	}
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	pm, err := LoadPrincipalMap(o.Config.PrincipalMapPath)
	if err != nil {
		return nil, err
	}
	if pm.Len() == 0 {
		log.Warn("principal map is empty; every inbound message will be dropped at verification",
			"path", o.Config.PrincipalMapPath)
	}
	backend := o.Backend
	if backend == "" {
		backend = "discord"
	}
	if o.RelayDurable == "" {
		o.RelayDurable = relayDurable
	}
	// Tests and embedders build Config directly, bypassing FromEnv's
	// parse-and-validate; unset means the default there too.
	if o.Config.MaxSessions <= 0 {
		o.Config.MaxSessions = defaultMaxSessions
	}
	if o.Config.TaskDeadline <= 0 {
		o.Config.TaskDeadline = defaultTaskDeadline
	}
	if o.Config.AskTTL <= 0 {
		o.Config.AskTTL = defaultAskTTL
	}
	g := &Gateway{
		cfg:          o.Config,
		client:       o.Client,
		reg:          NewRegistry(o.Client),
		adapter:      o.Adapter,
		pm:           pm,
		ps:           NewPseudonymizer(o.Config.AttributionSalt),
		log:          log,
		runCtx:       context.Background(),
		sessionLocks: map[string]*sync.Mutex{},
		taskSessions: map[string]string{},
		relays:       map[string]*relayState{},
		backend:      backend,
		relayDurable: o.RelayDurable,
	}
	g.inbox = newKeyedQueue(func(_ string, batch []InboundMessage) {
		for _, msg := range batch {
			g.handleInbound(msg)
		}
	})
	g.events = newKeyedQueue(g.relayBatch)
	if o.Spawner != nil {
		g.spawner = o.Spawner
	} else if o.Config.SpawnSessions {
		s, err := newPodSpawner(o.Config, log)
		if err != nil {
			return nil, fmt.Errorf("session-pod spawning is enabled but the k8s client failed: %w", err)
		}
		g.spawner = s
	}
	if o.Config.DefaultAddressee == RouteSession && g.spawner == nil {
		return nil, fmt.Errorf("A2A_DEFAULT_ADDRESSEE=%s requires A2A_SPAWN_SESSIONS=true: without a spawner the sentinel would publish tasks to a literal %q addressee no executor owns", RouteSession, RouteSession)
	}
	return g, nil
}

// Run subscribes the event relay, starts the reap and sweep loops, and runs
// the adapter until ctx is done.
func (g *Gateway) Run(ctx context.Context) error {
	g.runCtx = ctx
	sub, err := g.client.SubscribeDurable(ctx, lib.SubscribeConfig{
		Stream:  lib.TasksStream,
		Subject: "a2a.tasks.*.*.events",
		Durable: g.relayDurable,
		Session: gatewayParty.Session,
	}, func(env *lib.Envelope) { g.relayEvent(ctx, env) })
	if err != nil {
		return fmt.Errorf("event relay subscription: %w", err)
	}
	defer sub.Stop()

	go g.reapLoop(ctx)
	if g.spawner != nil {
		go g.sweepLoop(ctx)
	}

	// The adapter's delivery goroutines only enqueue; per-conversation order
	// is the queue's job, not the backend's.
	return g.adapter.Run(ctx, func(msg InboundMessage) { g.inbox.enqueue(msg.Conversation, msg) })
}

// lockSession returns the per-conversation mutex, minting it on first use.
func (g *Gateway) lockSession(key string) *sync.Mutex {
	g.mu.Lock()
	defer g.mu.Unlock()
	l, ok := g.sessionLocks[key]
	if !ok {
		l = &sync.Mutex{}
		g.sessionLocks[key] = l
	}
	return l
}

// handleInbound is one user turn: verify the sender, resolve the session,
// and route the message — status query by replay, stop, steer, or a new
// task. Runs on the conversation's inbox worker, in arrival order.
func (g *Gateway) handleInbound(msg InboundMessage) {
	// Verify against the backend's identity mechanism — for Discord, the
	// test mapping table — and drop the message if we can't (gateway design,
	// turns-and-tasks step 1).
	principal := g.pm.Resolve(msg.AuthorID)
	if principal == "" {
		g.log.Warn("dropping message from unmapped sender",
			"backend", g.backend, "author", msg.AuthorID, "conversation", msg.Conversation)
		return
	}

	l := g.lockSession(msg.Conversation)
	l.Lock()
	defer l.Unlock()

	ctx, cancel := context.WithTimeout(g.runCtx, turnTimeout)
	defer cancel()

	rec, err := g.reg.Get(ctx, msg.Conversation)
	if err != nil {
		g.log.Error("session lookup failed", "conversation", msg.Conversation, "err", err)
		return
	}
	if rec == nil {
		rec, err = g.mintSession(ctx, msg)
		if err != nil {
			g.log.Error("session mint failed", "conversation", msg.Conversation, "err", err)
			return
		}
	}
	rec.LastActivity = time.Now().UTC()

	rosterIDs, rosterComplete, err := g.adapter.Roster(msg.Conversation)
	if err != nil {
		g.log.Warn("roster read failed; snapshotting requester only",
			"conversation", msg.Conversation, "err", err)
		rosterIDs, rosterComplete = nil, false
	}
	// The audience snapshot always contains at least the requester — an
	// empty roster would erase exactly the person the classifier's "who
	// could have read this" starts from.
	if !slices.Contains(rosterIDs, msg.AuthorID) {
		rosterIDs = append(rosterIDs, msg.AuthorID)
	}
	authority := BuildAuthority(g.ps, g.pm, principal, g.backend, msg.AuthorID,
		"principal-map", msg.Conversation, rec.Kind, rosterIDs, rosterComplete)
	rec.Roster = hashRoster(g.ps, g.pm, rosterIDs)

	// Heal a stale ActiveTask before routing: if the task is already
	// terminal on the stream (the relay's ack raced a transient failure, or
	// the gateway was down when the terminal fired and the redelivery
	// hasn't landed), release the serialization instead of steering the
	// user into a finished task. Only the serialization: the task index
	// stays until the relay retires it, so a queued terminal event still
	// posts its result. Healing at all means the render was probably lost
	// (an acked event is never redelivered), so post the replayed status
	// card rather than clearing silently — the same deterministic template
	// the status ask uses. In the relay-lag case this duplicates the
	// rolling-line edit that follows; redundant beats swallowed.
	if active := rec.ActiveTask; active != nil && !active.Detached {
		if task, err := g.client.TasksGet(ctx, rec.Addressee, active.TaskID); err == nil && task.Final {
			g.log.Info("healing stale active task", "taskId", active.TaskID, "state", task.State)
			g.post(rec.Key, formatTaskStatus(task, active.Ask, active.SubmittedAt))
			rec.ActiveTask = nil
		}
	}

	active := rec.ActiveTask
	// The status matcher's wide interrogative rule is only safe where a
	// stolen steer costs nothing: a fixed-route executor (Hermes) refuses
	// steers, a session worker absorbs them - so a session-addressed task
	// gets the exact phrases only (see isStatusQuery). A detached task
	// gets the exact phrases on either route: after a stop, the wide
	// reading of "any update on the rollout" would steal a NEW task to
	// replay a dead one, so the cost argument inverts there too.
	wideStatus := !rec.AddressedToOwnSession() && !(active != nil && active.Detached)
	switch {
	case active != nil && isStatusQuery(msg.Text, wideStatus):
		g.answerStatusByReplay(ctx, rec)
	case active != nil && !active.Detached && isStop(msg.Text):
		g.cancelTask(ctx, rec, authority)
	case isStop(msg.Text):
		// A stop with nothing to stop must not fall through and become a
		// task that literally reads "stop": the impatient second "stop"
		// (cancel sent, terminal pending) and a bare "stop" with nothing
		// running both land here, answered deterministically.
		if active != nil {
			g.post(rec.Key, "🛑 cancel already sent — the task ends when the executor confirms")
		} else {
			g.post(rec.Key, "🤷 nothing is running")
		}
	case active != nil && !active.Detached:
		g.steerTask(ctx, rec, msg, authority)
	default:
		if rest, ok := isDelegate(msg.Text); ok && g.spawner != nil {
			// The Delegate flow (W4 amendment): this ONE task goes to a
			// freshly spawned session worker - the addressee is the new
			// session name, the rest of the text is the task. On a
			// fixed-route conversation the next plain ask below re-homes to
			// the default addressee; on a session-routed one the delegate
			// incarnation becomes the next standing incarnation, which is
			// inside the session route's contract (incarnations rotate).
			//
			// The cap check precedes every route mutation, so a refused turn
			// leaves the record exactly as it was — on first contact that is
			// the bare identity record the mint just Created (contextId,
			// default route, no task): the early return skips the Put below,
			// so nothing the refused turn did persists.
			if g.refuseAtSessionCap(ctx, rec, rec.PodName != "") {
				return
			}
			// A lingering previous incarnation is not this task's executor,
			// and its task is already closed (healed or detached). Delete
			// the pod rather than just untrack it: once PodName clears,
			// reap can never find it again, and sweep only sees terminal
			// phases - a wedged Running pod would hold its bus credential
			// forever. If the task is detached, the gateway is its
			// supervisor and owes its terminal `canceled` BEFORE the delete
			// (the one deletion rule in Session lifecycle); a refusal here
			// precedes every route mutation, so the record is untouched.
			if rec.PodName != "" {
				if !g.closeDetachedBeforeDelete(ctx, rec) {
					g.post(rec.Key, "⚠️ not started: could not close the previous task on the bus; try again in a moment")
					return
				}
				if err := g.spawner.Delete(ctx, rec.PodName); err != nil {
					g.log.Warn("previous incarnation delete failed; pod may linger",
						"pod", rec.PodName, "err", err)
				}
				rec.PodName = ""
			}
			if rec.Profile == "" {
				rec.Profile = "chat"
			}
			rec.BusSession = mintSessionName(rec.Profile)
			rec.Addressee = rec.BusSession
			msg.Text = rest
		} else if rec.SessionRouted {
			// Every new task on the session route gets a fresh incarnation.
			// The worker adapter is one task per process, so a lingering
			// PodName names an executor that can never serve this task -
			// publishing toward it wedges the conversation with a task
			// nothing will run and nothing will terminate (S9 review
			// finding). Retire it the way Delegate does: supervisor
			// terminal for a detached task first, then the delete, then
			// the successor. The cap holds here too, or Delegate refusals
			// just push the flood one affordance over.
			if g.refuseAtSessionCap(ctx, rec, rec.PodName != "") {
				return
			}
			if rec.PodName != "" {
				if !g.closeDetachedBeforeDelete(ctx, rec) {
					g.post(rec.Key, "⚠️ not started: could not close the previous task on the bus; try again in a moment")
					return
				}
				// Spawner-nil is the W4-rollback shape: the record is
				// session-routed but nothing can manage pods. Clear the
				// binding and degrade the way this path always did.
				if g.spawner != nil {
					if err := g.spawner.Delete(ctx, rec.PodName); err != nil {
						g.log.Warn("previous incarnation delete failed; pod may linger",
							"pod", rec.PodName, "err", err)
					}
				}
				rec.PodName = ""
			}
			rec.BusSession = mintSessionName(rec.Profile)
			rec.Addressee = rec.BusSession
		} else {
			// Re-home after a delegated task: a plain ask on a fixed-route
			// conversation always goes to the configured addressee, never
			// to a dead delegate session.
			rec.Addressee = g.cfg.DefaultAddressee
			if g.spawner != nil && rec.Addressee == RouteSession {
				// A record minted before the W4 flip: upgrade it the way
				// first contact would. The sentinel is a route, never an
				// addressee - written literally it publishes the task to a
				// subject no executor owns (the exact failure New()'s
				// config guard describes). The upgrade spawns, so it pays
				// the same cap toll as first contact.
				if g.refuseAtSessionCap(ctx, rec, rec.PodName != "") {
					return
				}
				// A lingering pre-flip incarnation gets the Delegate
				// branch's delete-and-clear: left set, the stale PodName
				// turns ensureSessionPod into a no-op and the task
				// publishes to an addressee with no executor, while the
				// old pod holds a cap slot sweep can never reclaim. Same
				// supervisor rule as the Delegate branch: a detached
				// task's terminal is published before its pod goes.
				if rec.PodName != "" {
					if !g.closeDetachedBeforeDelete(ctx, rec) {
						g.post(rec.Key, "⚠️ not started: could not close the previous task on the bus; try again in a moment")
						return
					}
					if err := g.spawner.Delete(ctx, rec.PodName); err != nil {
						g.log.Warn("pre-flip incarnation delete failed; pod may linger",
							"pod", rec.PodName, "err", err)
					}
					rec.PodName = ""
				}
				rec.SessionRouted = true
				rec.Profile = "chat"
				rec.BusSession = mintSessionName(rec.Profile)
				rec.Addressee = rec.BusSession
			}
		}
		g.startTask(ctx, rec, msg, principal, authority)
	}

	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}
}

// mintSession is first contact with a conversation: contextId is minted
// here and never changes — the durable name of the conversation on the bus,
// across every pod incarnation. The mint is create-only (KV Create,
// compare-and-swap; the spec's MUST), so two replicas or a rehydrate racing
// first contact cannot fork the conversation's identity: the loser reads
// and adopts the winner's record before the contextId reaches any envelope.
func (g *Gateway) mintSession(ctx context.Context, msg InboundMessage) (*SessionRecord, error) {
	rec := &SessionRecord{
		Key:          msg.Conversation,
		ContextID:    "ctx-" + randHex(contextIDHexWidth),
		Addressee:    g.cfg.DefaultAddressee,
		Kind:         msg.Kind,
		LastActivity: time.Now().UTC(),
	}
	// The W4 switch: with spawning armed and the route set to "session",
	// the conversation gets its own executor. The bus session name is
	// minted per incarnation (spawn time), not here — reaping and
	// respawning changes the pod and the bus session name; contextId
	// persists (gateway design).
	if g.spawner != nil && rec.Addressee == RouteSession {
		rec.SessionRouted = true
		rec.Profile = "chat"
	}
	err := g.reg.Create(ctx, rec)
	if err == nil {
		return rec, nil
	}
	if !errors.Is(err, ErrSessionExists) {
		return nil, err
	}
	winner, gerr := g.reg.Get(ctx, msg.Conversation)
	if gerr != nil || winner == nil {
		return nil, fmt.Errorf("lost the mint race but cannot read the winner: %v", gerr)
	}
	return winner, nil
}

// startTask mints the identifiers, publishes the submission, and posts the
// placeholder the relay will edit.
func (g *Gateway) startTask(ctx context.Context, rec *SessionRecord, msg InboundMessage, principal string, authority []byte) {
	taskID := "task-" + randHex(taskIDHexWidth)
	// correlationId is minted here and nowhere else — the originating user
	// interaction (payload spec field rule).
	correlationID := "corr-" + randHex(correlationIDHexWidth)

	// The ingress log is the plaintext join: backend message id against
	// correlationId, so the audit chain runs chat message -> correlationId ->
	// every hop -> change. Plaintext stays local; the bus gets pseudonyms.
	g.log.Info("ingress",
		"correlationId", correlationID,
		"taskId", taskID,
		"backendMessageId", msg.MessageID,
		"principal", principal,
		"conversation", msg.Conversation,
		"addressee", rec.Addressee)

	payload, err := messagePayload(msg.Text, taskID, rec.ContextID)
	if err != nil {
		g.log.Error("message payload build failed", "err", err)
		return
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID, correlationID, payload,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(authority))
	if err != nil {
		g.log.Error("envelope build failed", "err", err)
		return
	}

	// Placeholder first, so the rolling line exists before the first event
	// can arrive (the demo posts one while the pod cold-starts; same idea).
	statusMsgID, err := g.adapter.Post(rec.Key, "⏳ submitted…")
	if err != nil {
		g.log.Error("placeholder post failed", "conversation", rec.Key, "err", err)
	}

	// Register the task everywhere the relay looks BEFORE publishing: a fast
	// executor's submitted event must never race the mapping, because the
	// relay acks what it cannot route and the durable won't redeliver it.
	rec.ActiveTask = &ActiveTask{TaskID: taskID, CorrelationID: correlationID, StatusMsgID: statusMsgID,
		Ask: truncateRunes(msg.Text, askCap), SubmittedAt: time.Now()}
	rec.Tasks = append(rec.Tasks, TaskRef{ID: taskID, Addressee: rec.Addressee})
	if len(rec.Tasks) > taskHistoryCap {
		rec.Tasks = rec.Tasks[len(rec.Tasks)-taskHistoryCap:]
	}
	g.mu.Lock()
	g.taskSessions[taskID] = rec.Key
	g.relays[taskID] = &relayState{}
	g.mu.Unlock()
	if err := g.reg.IndexTask(ctx, taskID, rec.Key); err != nil {
		g.log.Error("task index write failed", "taskId", taskID, "err", err)
	}
	if err := g.reg.Put(ctx, rec); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}

	if err := g.client.Publish(ctx, lib.TaskInSubject(rec.Addressee, taskID), env); err != nil {
		g.log.Error("task publish failed", "taskId", taskID, "err", err)
		if statusMsgID != "" {
			_ = g.adapter.Edit(rec.Key, statusMsgID, "❌ could not reach the bus; try again")
		}
		rec.ActiveTask = nil
		g.mu.Lock()
		delete(g.taskSessions, taskID)
		delete(g.relays, taskID)
		g.mu.Unlock()
		return
	}

	// Session-addressed routes get an incarnation; fixed addressees (the
	// Hermes-first "platform") have their own executor and spawn nothing.
	// A task addressed to the conversation's own bus session - the standing
	// session route or a one-shot Delegate - is what needs a pod.
	if g.spawner != nil && rec.BusSession != "" && rec.Addressee == rec.BusSession {
		g.ensureSessionPod(ctx, rec, taskID)
	}
}

// steerTask forwards a message that arrived while the task runs as a
// follow-up on the same taskId — injected, absorbed at the executor's next
// turn boundary (decided 8/24). It reuses the task's correlationId; the
// steer is attributed by its own envelope and authority block.
func (g *Gateway) steerTask(ctx context.Context, rec *SessionRecord, msg InboundMessage, authority []byte) {
	active := rec.ActiveTask
	payload, err := messagePayload(msg.Text, active.TaskID, rec.ContextID)
	if err != nil {
		g.log.Error("steer payload build failed", "err", err)
		return
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, active.TaskID, rec.ContextID, active.CorrelationID, payload,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(authority))
	if err != nil {
		g.log.Error("steer envelope build failed", "err", err)
		return
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(rec.Addressee, active.TaskID), env); err != nil {
		g.log.Error("steer publish failed", "taskId", active.TaskID, "err", err)
		g.post(rec.Key, "⚠️ could not send that to the running task; it is still working on the original instruction")
		return
	}
	// Say what we know and no more: the steer is on the stream, and what
	// happens next is the route's contract (spec: gateway-authored posts,
	// amended 8/31) - a session worker absorbs at its next turn boundary if
	// the task is still running; the fixed-route executor refuses mid-task
	// input and publishes its refusal itself. Neither line claims the steer
	// was absorbed, which the gateway cannot know.
	if rec.AddressedToOwnSession() {
		g.post(rec.Key, "✏️ steering sent — the worker picks it up at its next turn boundary if the task is still running")
	} else {
		g.post(rec.Key, "✏️ steering sent — the standing executor does not take mid-task input; its reply will say so")
	}
}

// cancelTask publishes kind:cancel — the hard interrupt — and detaches the
// session. Detaching matters in the Hermes-first world: platform tasks have
// no janitor yet (the dispatcher arrives at stage 3), so a dead executor
// would otherwise wedge the conversation forever. The gateway never forges a
// terminal event for a task it doesn't supervise; it just stops letting that
// task serialize new ones.
func (g *Gateway) cancelTask(ctx context.Context, rec *SessionRecord, authority []byte) {
	active := rec.ActiveTask
	env, err := lib.NewCancelEnvelope(gatewayParty, active.TaskID, rec.ContextID, active.CorrelationID,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(authority))
	if err != nil {
		g.log.Error("cancel envelope build failed", "err", err)
		return
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(rec.Addressee, active.TaskID), env); err != nil {
		g.log.Error("cancel publish failed", "taskId", active.TaskID, "err", err)
		g.post(rec.Key, "⚠️ could not send the stop; the task is still running — try again")
		return
	}
	active.Detached = true
	// Detached outlives ActiveTask (a new turn overwrites it), so the
	// cancel is also recorded on the task's history entry — the evidence
	// Sweep reads when it must choose `canceled` over `failed`. It rides
	// the end-of-turn record write, so a sustained KV failure at exactly
	// this moment can lose the mark while the cancel stands, and a later
	// sweep would then say `failed` for a task the user stopped. Known
	// residue; replaying the in subject for the cancel envelope is the
	// close if it ever bites.
	rec.MarkCanceled(active.TaskID)
	g.post(rec.Key, "🛑 cancel sent — the task ends when the executor confirms")
}

// answerStatusByReplay answers "what is it doing" from the stream, not from
// a live connection — tasks/get materialized by replay is the durability
// payoff the payload spec's replay rule promises.
func (g *Gateway) answerStatusByReplay(ctx context.Context, rec *SessionRecord) {
	active := rec.ActiveTask
	task, err := g.client.TasksGet(ctx, rec.Addressee, active.TaskID)
	if err != nil {
		if _, ok := err.(*lib.A2AError); ok {
			g.post(rec.Key, "📭 no events on the stream for this task yet")
			return
		}
		g.log.Error("status replay failed", "taskId", active.TaskID, "err", err)
		g.post(rec.Key, "⚠️ replay failed; see gateway logs")
		return
	}
	g.post(rec.Key, formatTaskStatus(task, active.Ask, active.SubmittedAt))
}

// messagePayload builds the A2A Message for one chat turn.
func messagePayload(text, taskID, contextID string) ([]byte, error) {
	return marshalMessage(lib.Message{
		Role:      "user",
		Parts:     []lib.Part{{Kind: "text", Text: text}},
		MessageID: "msg-" + randHex(messageIDHexWidth),
		TaskID:    taskID,
		ContextID: contextID,
	})
}

func hashRoster(ps *Pseudonymizer, pm *PrincipalMap, ids []string) []string {
	out := make([]string, 0, min(len(ids), rosterCap))
	for _, id := range ids {
		if len(out) >= rosterCap {
			break
		}
		entry := id
		if p := pm.Resolve(id); p != "" {
			entry = p
		}
		out = append(out, ps.Hash(entry))
	}
	return out
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(fmt.Sprintf("crypto/rand: %v", err)) // process entropy failure; nothing sane to do
	}
	return hex.EncodeToString(b)
}
