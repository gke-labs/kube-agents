package gateway

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
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

// neverStartedNotice is what the conversation sees when the heal releases a
// task that produced no first event inside FirstEventGrace: the task id,
// the grace, and what happens to the message that triggered it. It states
// the evidence (nothing on the stream in that long), not the inference.
const neverStartedNotice = "⚠️ task `%s` has produced nothing on its event stream in %s, so this conversation is released and this message is handled as a new turn"

// Hex-suffix widths for the ids the gateway mints. Context and correlation
// ids are wider than task and message ids: they outlive one task and join
// records across surfaces, so a collision costs more.
const (
	// droppedNoticesCap bounds the once-per-sender drop-notice memory; one
	// entry per unverified sender, evicted wholesale rather than leaked.
	droppedNoticesCap     = 4096
	taskIDHexWidth        = 8
	messageIDHexWidth     = 8
	contextIDHexWidth     = 12
	correlationIDHexWidth = 12
)

// gatewayParty is the gateway's own identity in from. Never the source of
// authority: what makes a supervisor terminal the supervisor's is the subject
// it is published on (`…supervisor`, which only the gateway's grant reaches),
// and from is checked for agreement with that subject by every consumer -
// a terminal on `…supervisor` whose from is not this party is a protocol
// error, and so is one on an executor's `…events` wearing it. That is how
// replay distinguishes "the worker said failed" from "the supervisor declared
// it dead" without trusting a field the publisher writes.
var gatewayParty = lib.Party{Session: "gateway", AgentType: "a2a-gateway"}

// SupervisorAgreement is the envelope-subject agreement policy for the
// gateway's own consumers, and the one main hands the bus client so that
// tasks/get replay and the relay agree about who the supervisor is. The
// gateway is the supervisor for every session it spawned, so the
// `…supervisor` writer check is exact rather than the negative form.
//
// The strict flag is config, not code, on purpose. The `…events` writer-class
// check ships advisory because for one TASKS retention window after an
// install takes the supervisor split the stream still holds legitimate
// supervisor terminals on `…events`. Tightening it is then a Deployment env
// change (A2A_STRICT_EVENTS_WRITER=true) an operator can make - and revert -
// without an image, which is what makes "flip it 72h later" an instruction
// someone can actually carry out.
func SupervisorAgreement(cfg *Config) lib.AgreementPolicy {
	return lib.AgreementPolicy{
		Supervisor:         gatewayParty.Session,
		StrictEventsWriter: cfg != nil && cfg.StrictEventsWriter,
	}
}

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

	// backend names the gateway's configured chat backend, which is what a
	// message that names none is attributed to. A message from the inject
	// side door names its own (backendFor).
	backend string
	// injectAudience is injectPM's inject: section with the prefix removed
	// and only eval: values kept -- the shape BuildAuthority and hashRoster
	// resolve a roster's raw author ids through. Without it the requester's
	// roster entry hashes the raw author id while Requester.Principal
	// hashes the resolved principal, and "is the requester in the audience"
	// answers no on the one backend where the roster is exactly the
	// requester.
	injectAudience *PrincipalMap
	// injectPM is the side door's own principal map, loaded only when the
	// door is armed and never consulted for a message from a real backend.
	// Separate from pm on purpose: see Config.InjectPrincipalMapPath.
	injectPM *PrincipalMap
	// gchatAllowed and gchatAllowAll gate the gchat backend's identity
	// resolution (Config.GchatAllowedUsers, lowercased at build).
	gchatAllowed  map[string]bool
	gchatAllowAll bool
	// droppedNotices records which unverifiable senders have been told so —
	// the drop is visible once per sender, not once per message.
	droppedNotices map[string]bool
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
	backend := o.Backend
	if backend == "" {
		// Derived from the same config that selects the adapter, so a
		// caller that sets one and not the other cannot pair a gchat
		// relay with principal-map resolution.
		backend = o.Config.Backend()
	}
	if backend == "" && o.Config.InjectArmed() {
		// No real backend and the door armed: the door is the whole of the
		// ingress, so it is also the default attribution. Every message it
		// delivers stamps this anyway; what this decides is what an empty
		// Backend would mean, and "" in an authority block is worse than
		// the truth.
		backend = injectBackend
	}
	if backend == injectBackend {
		// Said out loud, and repeated on the door's read route
		// (ConversationState.InjectOnly): an eval install is meant to look
		// like this, but a `mode: next` install whose relay URL failed to
		// render looks exactly the same, and the difference between the
		// two must not be silence.
		log.Warn("the gateway is running on the inject door alone: no Discord token and no Chat relay are armed, " +
			"so nothing but the eval door can reach this install (inject-only)")
	}
	// gchat resolves identity from the Google-asserted email, not from the
	// map — an empty map is only a lockout on the backends that use one, and
	// a gateway whose only ingress is the side door uses the door's map
	// below instead of this one.
	if backend == discordBackend && pm.Len() == 0 {
		log.Warn("principal map is empty; every inbound message will be dropped at verification",
			"path", o.Config.PrincipalMapPath)
	}
	// The side door's map, which is a different file and not a section of
	// the one above: a colon is not a legal ConfigMap key, so the prefixed
	// entries cannot live in the directory-shaped map a chat backend mounts.
	var injectPM, injectAudience *PrincipalMap
	if o.Config.InjectArmed() {
		injectPM, err = LoadPrincipalMap(o.Config.InjectPrincipalMapPath)
		if err != nil {
			return nil, err
		}
		injectAudience = injectPM.Section(injectPrincipalPrefix, injectEvalPrincipalPrefix)
		if injectPM.Len() == 0 {
			log.Warn("the inject door's principal map is empty; every injected message will be dropped at verification",
				"path", o.Config.InjectPrincipalMapPath)
		}
	}
	gchatAllowed := map[string]bool{}
	for _, u := range o.Config.GchatAllowedUsers {
		if u = strings.TrimSpace(u); u != "" {
			gchatAllowed[strings.ToLower(u)] = true
		}
	}
	if backend == gchatBackend && len(gchatAllowed) == 0 && !o.Config.GchatAllowAllUsers {
		log.Warn("gchat allowlist is empty and allow-all is off; every inbound message will be dropped at verification")
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
	if o.Config.FirstEventGrace <= 0 {
		o.Config.FirstEventGrace = defaultFirstEventGrace
	}
	g := &Gateway{
		cfg:            o.Config,
		client:         o.Client,
		reg:            NewRegistry(o.Client),
		adapter:        o.Adapter,
		pm:             pm,
		ps:             NewPseudonymizer(o.Config.AttributionSalt),
		log:            log,
		runCtx:         context.Background(),
		sessionLocks:   map[string]*sync.Mutex{},
		taskSessions:   map[string]string{},
		relays:         map[string]*relayState{},
		backend:        backend,
		injectPM:       injectPM,
		injectAudience: injectAudience,
		gchatAllowed:   gchatAllowed,
		gchatAllowAll:  o.Config.GchatAllowAllUsers,
		droppedNotices: map[string]bool{},
		relayDurable:   o.RelayDurable,
	}
	g.inbox = newKeyedQueue(func(_ string, batch []InboundMessage) {
		for _, msg := range batch {
			g.handleInbound(msg)
		}
	})
	// The read route's probe, for an adapter whose caller is a program (the
	// inject door, and the side door composite in front of it). A chat
	// backend does not implement ProbeSink and is offered nothing.
	if sink, ok := o.Adapter.(ProbeSink); ok {
		sink.SetProbe(g.probeConversation)
	}
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
	// Both task-event subjects, one durable: the executors' events and the
	// terminals this gateway synthesizes as supervisor, which it relays to
	// the requester and retires the task on exactly like an executor's own.
	// Two filter subjects put the filter in the request body, which the
	// gateway's unscoped consumer-create grant permits and a session's
	// pinned grant would not; the durable already exists on every install
	// with the single filter, and rebinding it to the pair is an update the
	// server accepts (lib's rebind test).
	agreement := SupervisorAgreement(g.cfg)
	sub, err := g.client.SubscribeDurable(ctx, lib.SubscribeConfig{
		Stream:    lib.TasksStream,
		Subjects:  []string{"a2a.tasks.*.*." + lib.TaskClassEvents, "a2a.tasks.*.*." + lib.TaskClassSupervisor},
		Durable:   g.relayDurable,
		Session:   gatewayParty.Session,
		Agreement: &agreement,
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
	// First, and before the verification below, which returns early for a
	// sender it cannot place: whatever this turn does and however it
	// returns, the adapter is told it is over. See
	// InboundObserver.TurnFinished -- it is what lets a door whose caller is
	// a program say "the gateway answered without starting a task" as a fact
	// rather than as a guess about timing.
	defer g.observeTurnFinished(msg.Conversation)

	// Verify against the backend's identity mechanism — the mapping table
	// on Discord, the Google-asserted email gated by the allowlist on gchat
	// — and drop the message if we can't (gateway design, turns-and-tasks
	// step 1). The drop is visible once per sender: a silent drop of a real
	// user is a support burden. The notice names the sender's own
	// backend-asserted id — their own identity, in their own conversation,
	// which is what the admin needs to add and is not an oracle over
	// anything the sender does not already see.
	backend := g.backendFor(msg)
	principal := g.resolvePrincipal(backend, msg.AuthorID)
	if principal == "" {
		g.log.Warn("dropping message from unverified sender",
			"backend", backend, "author", msg.AuthorID, "conversation", msg.Conversation)
		// Keyed case-folded (an asserted address that varies in case is one
		// person) and bounded the way the adapters bound their own maps:
		// wholesale eviction at the cap, which at worst repeats a notice.
		key := strings.ToLower(msg.AuthorID)
		g.mu.Lock()
		if len(g.droppedNotices) >= droppedNoticesCap {
			g.droppedNotices = map[string]bool{}
		}
		notified := g.droppedNotices[key]
		g.droppedNotices[key] = true
		g.mu.Unlock()
		if !notified {
			g.post(msg.Conversation, "⛔ I can't verify who you are on "+backend+
				" (id "+msg.AuthorID+"), so I can't take asks from you yet — an admin has to add you to "+
				unverifiedRemedyFor(backend)+".")
		}
		// After the notice, and whether or not one was posted: an adapter
		// whose caller is a program has to learn about the drop it is not
		// being told about a second time (InboundObserver). Ordered after,
		// because this wakes a waiting request, and a waiter that answered
		// between the signal and the post would hand its caller a reply with
		// the notice missing from the transcript.
		g.observeMessageDropped(msg.Conversation, msg.AuthorID)
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
	authority := BuildAuthority(g.ps, g.principalMapFor(backend), principal, backend, msg.AuthorID,
		verifiedByFor(backend), msg.Conversation, rec.Kind, rosterIDs, rosterComplete)
	rec.Roster = hashRoster(g.ps, g.principalMapFor(backend), rosterIDs)

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
	//
	// The other stale shape has no terminal to find: a task with NO events
	// at all (TasksGet answers TaskNotFound) because its executor never
	// came up — nothing for the fold to see, nothing for Sweep to watch,
	// and reap never clears ActiveTask. Past FirstEventGrace that is a
	// task that never started, and the serialization is released the same
	// way, with a plain line instead of a status card (there is no status
	// to replay). Only TaskNotFound qualifies: a transport failure cannot
	// rule out events, so it heals nothing, as everywhere else the
	// supervisor paths consult the stream. No terminal is published here:
	// age alone is not evidence, a first event that is merely late could
	// still arrive, and no supervisor path ever sees a task with no pod —
	// so a task released here ages out with the stream's retention, the
	// residue Session lifecycle names. The task index stays, as in the
	// terminal case, so a late start still renders; its key is retired
	// only if the task ever terminates.
	//
	// This is the heal's only caller. The inject door's read route
	// (probeConversation) reports the same facts and heals nothing: the
	// heal writes the record under this conversation's lock, and a read
	// that also wrote would be a second writer racing the next turn.
	g.healActiveTask(ctx, rec)

	active := rec.ActiveTask
	// The status matcher's wide interrogative rule is only safe where a
	// stolen steer costs nothing: a fixed-route executor (Hermes) refuses
	// steers, a session worker absorbs them - so a session-addressed task
	// gets the exact phrases only (see isStatusQuery). A detached task
	// gets the exact phrases on either route: after a stop, the wide
	// reading of "any update on the rollout" would steal a NEW task to
	// replay a dead one, so the cost argument inverts there too.
	wideStatus := !rec.AddressedToOwnSession() && !(active != nil && active.Detached)
	// An explicit cancel from a backend that can express one is read before
	// the text is read at all: it is not an ask, and a program must never
	// have to spell "stop" to reach a control path.
	stopping := msg.Intent == IntentCancel || isStop(msg.Text)
	switch {
	case msg.Intent == "" && active != nil && isStatusQuery(msg.Text, wideStatus):
		g.answerStatusByReplay(ctx, rec)
	case stopping && msg.TaskID != "" && (active == nil || active.TaskID != msg.TaskID):
		// A cancel that names a task the conversation no longer holds as
		// active -- the heal a few lines up may just have released it.
		g.cancelNamedTask(ctx, rec, msg.TaskID, authority)
	case active != nil && !active.Detached && stopping:
		g.cancelTask(ctx, rec, authority)
	case stopping:
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
		// The one routing decision with no other log line: a new task logs
		// "ingress", a heal logs itself, but a steer used to be silent, and
		// a conversation wedged on a stale record was undiagnosable from
		// the gateway's logs (#1318).
		g.log.Info("routing as steer", "conversation", msg.Conversation, "taskId", active.TaskID,
			"addressee", rec.Addressee, "taskAge", time.Since(active.SubmittedAt).Round(time.Second))
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

// backendFor names the ingress one message arrived through. A message that
// names none is the configured backend's, which is every chat backend's
// case; the inject door stamps its own, because it can be armed beside a
// real backend and the authority block must say which door a task came in
// through rather than which backend the process was configured with.
func (g *Gateway) backendFor(msg InboundMessage) string {
	if msg.Backend != "" {
		return msg.Backend
	}
	return g.backend
}

// principalMapFor is the map that backend's identities live in. The side
// door's is its own and is never a fallback to the chat map, nor the chat
// map's a fallback to it: an id that resolves in the wrong map would be an
// identity asserted by a door that is not allowed to assert it.
func (g *Gateway) principalMapFor(backend string) *PrincipalMap {
	if backend == injectBackend && g.injectAudience != nil {
		return g.injectAudience
	}
	return g.pm
}

// healActiveTask is the stale-task heal handleInbound runs before routing,
// and it runs nowhere else: it writes the record, so it belongs under the
// per-conversation lock inside the keyed queue with the rest of a turn. It
// looks at the active task's stream once and, when the task is already
// terminal or has produced nothing past FirstEventGrace, releases the
// conversation and writes the record.
//
// A record with no active task, or a detached one, is left alone: there is
// nothing to heal and, for a detached task, the cancel already published is
// the answer the conversation is waiting on.
func (g *Gateway) healActiveTask(ctx context.Context, rec *SessionRecord) {
	active := rec.ActiveTask
	if active == nil || active.Detached {
		return
	}
	task, terminalSubject, err := g.client.TasksGetAttributed(ctx, rec.Addressee, active.TaskID)
	healed := false
	switch {
	case err == nil && task.Final:
		g.log.Info("healing stale active task", "taskId", active.TaskID, "state", task.State)
		g.post(rec.Key, formatTaskStatus(task, active.Ask, active.SubmittedAt))
		// The terminal the relay should have delivered, delivered to the
		// adapter now, with whose word it is: the fold reads both of the
		// task's subjects, and a terminal off the supervisor subject is the
		// supervisor's (an executor that died or never ran, the install's)
		// where one off the events subject is the executor's -- the same
		// attribution probeConversation makes, because a caller grades the
		// executor's terminal and not the supervisor's, whichever route the
		// terminal reached it by. A chat backend is told nothing
		// (TaskObserver); the inject door records it, and a program
		// awaiting the task ends its wait on it rather than on a status
		// card it would have to parse. With the reason the fold carries,
		// because the two deliveries of one terminal must not disagree: a
		// caller classifying a bridge's own failure by its reason token
		// would grade it as the persona's for the one that came this way.
		source := TerminalFromExecutor
		if terminalSubject == lib.TaskSupervisorSubject(rec.Addressee, active.TaskID) {
			source = TerminalFromSupervisor
		}
		g.observeTaskTerminal(rec.Key, active.TaskID, task.State, source, finalMessageText(task))
		healed = true
	case isTaskNotFound(err) && !active.SubmittedAt.IsZero() &&
		time.Since(active.SubmittedAt) > g.cfg.FirstEventGrace:
		g.log.Info("healing an active task with no first event inside the grace",
			"conversation", rec.Key, "taskId", active.TaskID, "addressee", rec.Addressee,
			"age", time.Since(active.SubmittedAt).Round(time.Second), "grace", g.cfg.FirstEventGrace)
		g.post(rec.Key, fmt.Sprintf(neverStartedNotice, active.TaskID, g.cfg.FirstEventGrace))
		// And tell the adapter, for the caller that is a program. The
		// notice above is the answer for a human; a program reading the
		// conversation would have to match its prose to learn that its
		// task was never taken, and the difference between that and a
		// failed answer decides whether a run is the agent's fault or
		// the install's. Nothing is published: as handleInbound's comment
		// says, age is not evidence.
		g.observeTaskTerminal(rec.Key, active.TaskID, lib.StateFailed, TerminalNeverStarted, "")
		healed = true
	}
	if healed {
		rec.ActiveTask = nil
		// Write the release now, not at the end of the turn: a turn that
		// returns early — a cap refusal, on exactly the Delegate that
		// follows a wedge — would otherwise announce a release it never
		// wrote and announce it again on the next turn.
		if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
			g.log.Error("healed record write failed", "conversation", rec.Key, "err", err)
		}
	}
}

// probeConversation is the ConversationProbe the gateway offers a ProbeSink:
// the session record for one conversation and the state of its active
// task's stream, as they stand. A pure read, as ConversationProbe requires:
// no lock, no heal, no post, no publish, no write. It does not take the
// session lock because it needs nothing the lock protects -- the KV read is
// atomic and the stream replay is its own snapshot -- and holding it would
// let a slow read delay the turn the caller is waiting on.
func (g *Gateway) probeConversation(ctx context.Context, key string) (ConversationState, error) {
	state := ConversationState{
		Backend:    g.backend,
		InjectOnly: g.backend == injectBackend,
		Grace:      g.cfg.FirstEventGrace,
	}
	rec, err := g.reg.Get(ctx, key)
	if err != nil {
		return state, fmt.Errorf("session lookup: %w", err)
	}
	if rec == nil || rec.ActiveTask == nil {
		return state, nil
	}
	active := rec.ActiveTask
	state.Active = true
	state.TaskID = active.TaskID
	state.SubmittedAt = active.SubmittedAt
	state.Detached = active.Detached
	if !active.SubmittedAt.IsZero() {
		state.Age = time.Since(active.SubmittedAt)
	}
	// Against the addressee the task's own subjects carried: after a
	// Delegate re-home rec.Addressee is not it (the relay's terminal replay
	// makes the same choice).
	addressee := rec.AddresseeFor(active.TaskID)
	task, terminalSubject, terr := g.client.TasksGetAttributed(ctx, addressee, active.TaskID)
	switch {
	case terr == nil:
		state.ExecutorState = task.State
		state.Final = task.Final
		if task.Final {
			// The fold's terminal, with whose word it is: the events
			// subject is the executor's, the supervisor subject the
			// supervisor's. A caller grading an answer off this read takes
			// only the executor's; a supervisor terminal says an executor
			// died or never ran, which is the install's.
			state.TerminalSource = TerminalFromExecutor
			if terminalSubject == lib.TaskSupervisorSubject(addressee, active.TaskID) {
				state.TerminalSource = TerminalFromSupervisor
			}
			if art := task.Artifact(lib.ArtifactResult); art != nil {
				state.Result = joinTextParts(art.Parts)
			}
			if task.FinalMessage != nil {
				state.Reason = joinTextParts(task.FinalMessage.Parts)
			}
		}
	case isTaskNotFound(terr):
		// No events at all: no executor has touched the task. Reported as
		// the empty state, which is the fact; whether the task's age makes
		// that "nobody took it" is the caller's to decide against Grace.
	default:
		// A transport failure cannot rule out events, so it is not "no
		// executor": the caller learns that the gateway could not look,
		// never that nothing is there.
		return state, fmt.Errorf("reading task %s on %s: %w", active.TaskID, addressee, terr)
	}
	return state, nil
}

// observeTaskStarted and observeTaskTerminal tell an adapter that implements
// TaskObserver about a task's two ends. Both are no-ops for every chat
// backend, which does not implement the interface: a human reads the chat, so
// the rendered text is the whole of what a chat backend needs.
func (g *Gateway) observeTaskStarted(conversation, taskID string) {
	if observer, ok := g.adapter.(TaskObserver); ok {
		observer.TaskStarted(conversation, taskID)
	}
}

func (g *Gateway) observeTaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	if observer, ok := g.adapter.(TaskObserver); ok {
		observer.TaskTerminal(conversation, taskID, state, source, reason)
	}
}

// finalMessageText is the text of a folded task's terminal status message,
// which is where an executor writes `reason: <token>[ - detail]`. Empty when
// the terminal carried no message.
func finalMessageText(task *lib.Task) string {
	if task == nil || task.FinalMessage == nil {
		return ""
	}
	return joinTextParts(task.FinalMessage.Parts)
}

// observeTaskAccepted tells a TaskObserver that a task's submission reached
// the bus. Separate from observeTaskStarted because the two answer different
// questions and only the second is evidence an executor can ever see the ask.
func (g *Gateway) observeTaskAccepted(conversation, taskID string) {
	if observer, ok := g.adapter.(TaskObserver); ok {
		observer.TaskAccepted(conversation, taskID)
	}
}

// observeCancelPublished tells a TaskObserver that a cancel reached the bus.
// Only the publish is announced; see TaskObserver.CancelPublished for why the
// refusals are not.
func (g *Gateway) observeCancelPublished(conversation, taskID string) {
	if observer, ok := g.adapter.(TaskObserver); ok {
		observer.CancelPublished(conversation, taskID)
	}
}

// observeMessageDropped tells an InboundObserver that a message was dropped
// for an unverifiable sender. Called on every drop, not only on the ones the
// gateway posts a notice for. See InboundObserver.
func (g *Gateway) observeMessageDropped(conversation, authorID string) {
	if observer, ok := g.adapter.(InboundObserver); ok {
		observer.MessageDropped(conversation, authorID)
	}
}

// observeTurnFinished tells an InboundObserver that a turn on a conversation
// has ended. Deferred in handleInbound, so an early return announces it too.
func (g *Gateway) observeTurnFinished(conversation string) {
	if observer, ok := g.adapter.(InboundObserver); ok {
		observer.TurnFinished(conversation)
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

	// Announced before the placeholder below, deliberately: an adapter that
	// has to correlate its caller's submission with a task (the inject
	// backend) then learns the id first and never has to decide whether a
	// post belongs to the task it just submitted. Announced after the two
	// build steps above and before the publish, so the only way to be told
	// about a task that never reaches the bus is the publish failure, which
	// announces its own terminal below. See TaskObserver.
	g.observeTaskStarted(rec.Key, taskID)

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
	rec.Tasks = append(rec.Tasks, TaskRef{ID: taskID, Addressee: rec.Addressee, CorrelationID: correlationID})
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

	// PublishSeq rather than Publish: the sequence the server assigns is the
	// only thing that lets the session pod name THIS message as its
	// submission. Its `…in` subject collects every steer after it under one
	// per-subject cap, and past that cap a worker scanning the subject
	// cannot tell the evicted submission's replacement from the real thing
	// (lib.EnvOriginSeq).
	originSeq, err := g.client.PublishSeq(ctx, lib.TaskInSubject(rec.Addressee, taskID), env)
	if err != nil {
		g.log.Error("task publish failed", "taskId", taskID, "err", err)
		if statusMsgID != "" {
			_ = g.adapter.Edit(rec.Key, statusMsgID, "❌ could not reach the bus; try again")
		}
		rec.ActiveTask = nil
		g.mu.Lock()
		delete(g.taskSessions, taskID)
		delete(g.relays, taskID)
		g.mu.Unlock()
		// The task was announced a moment ago and is now over before it
		// existed: nothing is on the bus, so no executor will ever publish
		// its terminal and no supervisor path will either. An observer told
		// about the start is owed the end, or it waits out its own deadline
		// for an answer that cannot come. Nothing is published here — this
		// is the gateway telling its own adapter, not a terminal on the
		// stream, which would be a claim about a task the stream has never
		// heard of.
		g.observeTaskTerminal(rec.Key, taskID, lib.StateFailed, TerminalFromGateway, "")
		return
	}

	// The submission is on the subject now, which is the first moment
	// anything can be said to have been handed to an executor. An observer
	// told only about the start above would have to treat a task that never
	// reached the bus as one that did. See TaskObserver.TaskAccepted.
	g.observeTaskAccepted(rec.Key, taskID)

	// Session-addressed routes get an incarnation; fixed addressees (the
	// Hermes-first "platform") have their own executor and spawn nothing.
	// A task addressed to the conversation's own bus session - the standing
	// session route or a one-shot Delegate - is what needs a pod.
	//
	// The publish above precedes this by more than convention: the pod is
	// told the submission's sequence, so the submission has to exist and
	// have one before the pod is created. The spec's ordering rule ("the pod
	// exists because the message is already durable") is now load-bearing
	// rather than an optimisation.
	if g.spawner != nil && rec.BusSession != "" && rec.Addressee == rec.BusSession {
		g.ensureSessionPod(ctx, rec, taskID, originSeq)
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
	g.observeCancelPublished(rec.Key, active.TaskID)
	g.post(rec.Key, "🛑 cancel sent — the task ends when the executor confirms")
}

// cancelNamedTask publishes kind:cancel for a task this conversation has
// held but no longer holds as active. Only a backend that was answered with
// a task id can ask for it (InboundMessage.TaskID), and the case it exists
// for is the inject door's: a submission no executor took is released by the
// never-started heal on the conversation's next turn, which is the cancel
// turn itself, so a cancel routed on the active task alone would find
// nothing to stop -- while the submission is still on the in subject, and the
// bridge's durable consumer delivers from the start of the stream, so a
// bridge that binds later within retention would run the stale prompt. The
// cancel bounds that run. It rides the task's own chain (the history entry
// keeps the correlation id and the addressee), is recorded on the history
// entry like any published cancel, and detaches nothing, because nothing is
// active. A task this conversation never held is refused: the gateway does
// not publish cancels for tasks it did not start.
func (g *Gateway) cancelNamedTask(ctx context.Context, rec *SessionRecord, taskID string, authority []byte) {
	ref, held := rec.TaskRefFor(taskID)
	if !held {
		g.post(rec.Key, fmt.Sprintf("🤷 this conversation never held task `%s`; nothing sent", taskID))
		return
	}
	if ref.Canceled {
		g.post(rec.Key, "🛑 cancel already sent — the task ends when the executor confirms")
		return
	}
	if ref.CorrelationID == "" {
		// A history entry written before the correlation id was recorded on
		// it. The envelope needs one, and inventing a fresh chain for a
		// cancel would break the audit join; say so rather than fail the
		// build silently and leave the caller waiting on an entry.
		g.post(rec.Key, fmt.Sprintf("🤷 task `%s` predates this record's correlation ids; nothing sent", taskID))
		return
	}
	env, err := lib.NewCancelEnvelope(gatewayParty, taskID, rec.ContextID, ref.CorrelationID,
		lib.WithTo(lib.Party{Session: ref.Addressee}),
		lib.WithAuthority(authority))
	if err != nil {
		g.log.Error("cancel envelope build failed", "taskId", taskID, "err", err)
		return
	}
	if err := g.client.Publish(ctx, lib.TaskInSubject(ref.Addressee, taskID), env); err != nil {
		g.log.Error("cancel publish failed", "taskId", taskID, "err", err)
		g.post(rec.Key, "⚠️ could not send the stop; try again")
		return
	}
	rec.MarkCanceled(taskID)
	g.observeCancelPublished(rec.Key, taskID)
	g.log.Info("cancel published for a task the conversation no longer holds",
		"conversation", rec.Key, "taskId", taskID, "addressee", ref.Addressee)
	g.post(rec.Key, fmt.Sprintf("🛑 cancel sent for task `%s`, which this conversation no longer holds — "+
		"it ends when an executor confirms, if one ever took it", taskID))
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
