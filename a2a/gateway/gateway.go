package gateway

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

// sessionProfile is the AgentProfile a /session conversation runs as - the
// conversation front door of spec-subagent-profiles.md (a2a/profiles/chat.yaml).
const sessionProfile = "chat"

// sessionKindDM is the SessionRecord.Kind of a direct message, the one Slack
// surface that carries every message without a mention.
const sessionKindDM = "dm"

// What retireIncarnation posts when the previous task cannot be closed on
// the bus, by what the caller was about to do: three callers start a task,
// one leaves the route, and the user needs to hear which did not happen.
const (
	retireRefusalNotStarted = "⚠️ not started: could not close the previous task on the bus; try again in a moment"
	retireRefusalStillOn    = "⚠️ could not close the previous task on the bus; the session route is still on and its pod still there — try `/session off` again in a moment"
)

// turnTimeout bounds one handler turn — an inbound message or a relay
// batch — so a stuck bus or backend call frees the conversation's queue
// slot instead of holding it forever. Where the clock starts relative to
// the wait for the conversation's session lock depends on who is waiting
// for the answer; handleInbound says which and why.
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
	// droppedNoticeKeySep joins the backend and the author in its key.
	droppedNoticesCap     = 4096
	droppedNoticeKeySep   = "/"
	taskIDHexWidth        = 8
	messageIDHexWidth     = 8
	contextIDHexWidth     = 12
	correlationIDHexWidth = 12
)

// JetStream storage capacity error codes and patterns.
const (
	// jsErrCodeStorageResourcesExceeded is nats-server's JSStorageResourcesExceededErr (10047),
	// returned when account or server storage resources are exhausted.
	jsErrCodeStorageResourcesExceeded jetstream.ErrorCode = 10047
	maxBytesErrPattern                                    = "max bytes"
	maximumBytesErrPattern                                = "maximum bytes"
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
	// turnBudget is the clock handleInbound mints for a turn: turnTimeout,
	// unless a test shortens it to hold a session lock past a whole turn
	// without waiting a real one. The door's own bounds (injectSubmitWait,
	// claimTurn) read the constant, which is what production runs.
	turnBudget time.Duration

	// inbox orders inbound messages per conversation (the backend delivers
	// events on unordered goroutines) and events orders relay work per
	// session, so no conversation can block another.
	inbox  *keyedQueue[InboundMessage]
	events *keyedQueue[relayItem]

	mu sync.Mutex
	// sessionLocks serializes work per conversation; tasks serialize per
	// session by construction (a message during a running task is a steer,
	// never a second task). Entries are refcounted and pruned when idle.
	sessionLocks map[string]*sessionLockEntry
	// reapCursor tracks the scan position in session-state across reap passes,
	// so a scan that hits reapPassTimeout resumes from where it left off
	// rather than restarting from the beginning.
	reapCursor string
	// reapScanHook is an optional test hook invoked during reap passes on each visited record.
	// Returning false halts the reap scan early.
	reapScanHook func(rec *SessionRecord) bool
	// taskSessions caches taskId -> session key; the KV task index is the
	// durable copy a restart falls back to. Entries retire with the task.
	taskSessions map[string]string
	// relays holds per-task render state for the rolling progress line.
	relays map[string]*relayState

	// backend names the gateway's configured chat backend, which is what a
	// message that names none is attributed to. Since the mux and the side
	// door there are ingresses that name their own - the console adapter
	// stamps console on every frame it delivers, the inject door stamps
	// inject - so this is the answer for the configured backend's own
	// messages rather than for all of them (backendFor).
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
	// targetAllowed is Config.TargetAllowedUsers compiled for lookup: the
	// platform agent's own lists, checked when a session asks to delegate.
	targetAllowed targetAllowed
	// droppedNotices records which unverifiable senders have been told so —
	// the drop is visible once per sender, not once per message. Per
	// sender, NOT per conversation: a channel mention mints a fresh
	// conversation every time, so a conversation-scoped key would be no
	// bound at all. Bounded by droppedNoticesCap, so an unverified sender
	// cannot grow it without bound either.
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
		log.Warn("the gateway is running on the inject door alone: no Discord token, Slack pair or Chat relay is armed, " +
			"so nothing but the eval door can reach this install (inject-only)")
	}
	// gchat resolves identity from the Google-asserted email, not from the
	// map — an empty map is only a lockout on the backends that use one
	// (Discord's test table and Slack's user_id join alike). The console
	// does not use one either: its grant is the mechanism, since only the
	// console credential may publish on the console subject. And a gateway
	// whose only ingress is the side door uses the door's map below instead
	// of this one. Slack is the case that matters operationally: nothing
	// renders its map yet (#2099), so a Slack gateway whose map path is
	// missing would otherwise pass boot silently and drop every sender.
	// Naming the backend matters, because the other ingresses beside it
	// keep working.
	if (backend == discordBackend || backend == slackBackend) && pm.Len() == 0 {
		log.Warn(fmt.Sprintf("principal map is empty; every %s message will be dropped at verification", backend),
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
	ps := NewPseudonymizer(o.Config.AttributionSalt)
	targetAllowed := buildTargetAllowed(o.Config, ps)
	for target, byBackend := range targetAllowed {
		for b, set := range byBackend {
			log.Info("target allowlist loaded", "target", target, "backend", b, "entries", len(set))
		}
	}
	if o.RelayDurable == "" {
		o.RelayDurable = relayDurable
	}
	// Tests and embedders build Config directly, bypassing FromEnv's
	// parse-and-validate; unset means the default there too.
	if o.Config.MaxSessions <= 0 {
		o.Config.MaxSessions = defaultMaxSessions
	}
	if o.Config.DelegationDepthMax <= 0 {
		o.Config.DelegationDepthMax = defaultDelegationDepthMax
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
	if o.Config.SessionTTL <= 0 {
		o.Config.SessionTTL = defaultSessionTTL
	}
	o.Config.defaultCapabilityCeiling()
	if err := o.Config.validateCapabilityCeiling(); err != nil {
		return nil, err
	}
	g := &Gateway{
		turnBudget:     turnTimeout,
		cfg:            o.Config,
		client:         o.Client,
		reg:            NewRegistry(o.Client),
		adapter:        o.Adapter,
		pm:             pm,
		ps:             ps,
		log:            log,
		runCtx:         context.Background(),
		sessionLocks:   map[string]*sessionLockEntry{},
		taskSessions:   map[string]string{},
		relays:         map[string]*relayState{},
		backend:        backend,
		injectPM:       injectPM,
		injectAudience: injectAudience,
		gchatAllowed:   gchatAllowed,
		gchatAllowAll:  o.Config.GchatAllowAllUsers,
		targetAllowed:  targetAllowed,
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
	// The session registry as a read, for an adapter that has to decide on
	// its own goroutine whether a conversation is one the gateway is in (the
	// Slack adapter's session-thread rule, and the side door composite in
	// front of it), with the idle TTL the read is bounded by so the adapter
	// can expire its own positive cache on the same bound. See
	// SessionLookup.
	if sink, ok := o.Adapter.(SessionLookupSink); ok {
		sink.SetSessionLookup(g.hasSession, o.Config.IdleTTL)
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
	if o.Config.SessionClusterView && o.Config.CredentialProxyURL == "" {
		return nil, fmt.Errorf("A2A_SESSION_CLUSTER_VIEW=true requires A2A_CREDENTIAL_PROXY_URL: a session pod with the view and no broker address would have wrappers that dial nothing")
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
	// Attributed: the subject each envelope arrived on rides with it, because
	// it is the only thing that tells a supervisor terminal from an
	// executor's, and the relay owes the adapter that distinction
	// (TerminalSource) the same way the heal and the read route give it.
	sub, err := g.client.SubscribeDurableAttributed(ctx, lib.SubscribeConfig{
		Stream:    lib.TasksStream,
		Subjects:  []string{"a2a.tasks.*.*." + lib.TaskClassEvents, "a2a.tasks.*.*." + lib.TaskClassSupervisor},
		Durable:   g.relayDurable,
		Session:   gatewayParty.Session,
		Agreement: &agreement,
	}, func(subject string, env *lib.Envelope) { g.relayEvent(ctx, subject, env) })
	if err != nil {
		return fmt.Errorf("event relay subscription: %w", err)
	}
	defer sub.Stop()

	go g.reapLoop(ctx)
	if g.spawner != nil {
		go g.sweepLoop(ctx)
	}

	// The adapter's delivery goroutines only enqueue (or refuse a console
	// turn whose queue is full); per-conversation order is the queue's job,
	// not the backend's.
	return g.adapter.Run(ctx, g.enqueueInbound)
}

// enqueueInbound queues a turn for its conversation. Console turns are
// bounded per conversation (consoleQueueCap), because the console is the
// one ingress whose rate the sender sets.
func (g *Gateway) enqueueInbound(msg InboundMessage) {
	if msg.Backend != consoleBackend {
		g.inbox.enqueue(msg.Conversation, msg)
		return
	}
	accepted, first := g.inbox.enqueueBounded(msg.Conversation, msg, consoleQueueCap)
	if accepted || !first {
		return
	}
	// Once per fill: a tab publishing in a loop would otherwise get a log
	// line and a post for every frame it loses.
	g.log.Warn("console frame dropped", "reason", "queue full", "conversation", msg.Conversation, "messageId", msg.MessageID, "queued", consoleQueueCap)
	g.post(msg.Conversation, fmt.Sprintf(consoleQueueFullNotice, consoleQueueCap))
}

type sessionLockEntry struct {
	mu       sync.Mutex
	refcount int
}

// sessionLockHandle pairs a session lock with its release hook, refcounting
// the entry so idle locks are pruned from memory when no longer referenced.
type sessionLockHandle struct {
	g        *Gateway
	key      string
	entry    *sessionLockEntry
	unlocked bool
}

func (h *sessionLockHandle) Lock() {
	h.entry.mu.Lock()
}

func (h *sessionLockHandle) Unlock() {
	if h.unlocked {
		return
	}
	h.unlocked = true
	h.entry.mu.Unlock()
	h.g.mu.Lock()
	h.entry.refcount--
	if h.entry.refcount <= 0 {
		delete(h.g.sessionLocks, h.key)
	}
	h.g.mu.Unlock()
}

// lockSession returns the per-conversation mutex handle, refcounting the entry
// so idle locks are pruned from memory when no longer referenced.
func (g *Gateway) lockSession(key string) *sessionLockHandle {
	g.mu.Lock()
	defer g.mu.Unlock()
	entry, ok := g.sessionLocks[key]
	if !ok {
		entry = &sessionLockEntry{}
		g.sessionLocks[key] = entry
	}
	entry.refcount++
	return &sessionLockHandle{g: g, key: key, entry: entry}
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

	backend, principal, ok := g.verifySender(msg)
	if !ok {
		return
	}

	// Where the turn's clock starts depends on who is waiting for the
	// answer. The relay, the reap and the sweep each hold the conversation's
	// session lock, the relay for as long as its chat posts take (a Post or
	// Edit to a slow chat API is not bound by the relay's own context), so
	// a turn can queue behind the lock for a minute or more.
	//
	// A door turn's caller is a program waiting on a bound: the door hands
	// a message over only with a whole turnTimeout of its submit bound left
	// and reads a bound that expires with the turn unfinished as "nothing
	// started", which has to be true (InjectAdapter.claimTurn, awaitTurn).
	// So its clock starts before the lock wait, and a turn whose clock ran
	// out waiting does nothing. The door's own conversations are relayed
	// into memory, so on them the lock is held for the bus calls alone.
	//
	// A chat turn's caller is a person, for whom a late answer beats none:
	// the lock first, then a whole turn, as before the door existed.
	if backend == injectBackend {
		ctx, cancel := context.WithTimeout(g.runCtx, g.turnBudget)
		defer cancel()
		g.runTurn(ctx, msg, backend, principal)
		return
	}
	l := g.lockSession(msg.Conversation)
	l.Lock()
	defer l.Unlock()
	ctx, cancel := context.WithTimeout(g.runCtx, g.turnBudget)
	defer cancel()
	g.routeTurn(ctx, msg, backend, principal)
}

// runTurn is a door turn under a clock already running: take the session
// lock, and route only if the clock has not run out in the wait. The
// ordering it exists for -- the clock before the lock -- is handleInbound's,
// and the rig test that pins it drives handleInbound with turnBudget
// shortened rather than this function with a clock of its own.
func (g *Gateway) runTurn(ctx context.Context, msg InboundMessage, backend, principal string) {
	l := g.lockSession(msg.Conversation)
	l.Lock()
	defer l.Unlock()
	if err := ctx.Err(); err != nil {
		// The whole turn went on waiting for the lock. Every call below
		// would fail on the expired context anyway; said once, and plainly,
		// rather than as a session lookup failure.
		g.log.Warn("turn skipped: the conversation's session lock was held for the whole turn",
			"conversation", msg.Conversation, "messageId", msg.MessageID, "err", err)
		return
	}
	g.routeTurn(ctx, msg, backend, principal)
}

// droppedNoticeKey is the once-per-sender memory's key: the backend the
// message came through and the author's id, case-folded.
func droppedNoticeKey(backend, authorID string) string {
	return backend + droppedNoticeKeySep + strings.ToLower(authorID)
}

// verifySender resolves the sender to a principal, or drops the message and
// says so. Returns the backend the message came through, the principal, and
// whether the turn goes on.
func (g *Gateway) verifySender(msg InboundMessage) (backend, principal string, ok bool) {
	// Verify against the backend's identity mechanism — the mapping table
	// on Discord, the Google-asserted email gated by the allowlist on gchat
	// — and drop the message if we can't (gateway design, turns-and-tasks
	// step 1). The drop is visible once per sender: a silent drop of a real
	// user is a support burden. The notice names the sender's own
	// backend-asserted id — their own identity, in their own conversation,
	// which is what the admin needs to add and is not an oracle over
	// anything the sender does not already see.
	// The backend is the message's, not the process's: one gateway now has
	// several ingresses at once - a chat backend, the console, and the
	// inject door - and each of the latter two stamps its own on what it
	// delivers, so which mechanism has to check this sender is a property
	// of the message.
	backend = g.backendFor(msg)
	principal = g.resolvePrincipal(backend, msg.AuthorID)
	if principal == "" {
		g.log.Warn("dropping message from unverified sender",
			"backend", backend, "author", msg.AuthorID, "conversation", msg.Conversation)
		// Keyed by backend and case-folded author: one gateway has several
		// ingresses, and the same id on two of them is not the same sender,
		// so a notice on one must not silence the other. Case-folded because
		// an asserted address that varies in case is one person. Bounded the
		// way the adapters bound their own maps: wholesale eviction at the
		// cap, which at worst repeats a notice.
		key := droppedNoticeKey(backend, msg.AuthorID)
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
		return "", "", false
	}
	return backend, principal, true
}

// routeTurn is the turn proper: resolve the session, heal a stale task, and
// route the message. The caller holds the conversation's session lock and
// owns the context's clock.
func (g *Gateway) routeTurn(ctx context.Context, msg InboundMessage, backend, principal string) {
	rec, err := g.reg.Get(ctx, msg.Conversation)
	if err != nil {
		g.log.Error("session lookup failed", "conversation", msg.Conversation, "err", err)
		return
	}
	if rec == nil {
		rec, err = g.mintSession(ctx, msg)
		if err != nil {
			if isMaxBytes(err) {
				g.log.Error("session mint failed: session-state bucket is full (max bytes reached)",
					"conversation", msg.Conversation, "err", err)
			} else {
				g.log.Error("session mint failed", "conversation", msg.Conversation, "err", err)
			}
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
	resolveRoster := g.rosterResolver(backend)
	authority := BuildAuthority(g.ps, resolveRoster, principal, backend, msg.AuthorID,
		verifiedByFor(backend), msg.Conversation, rec.Kind, rosterIDs, rosterComplete)
	rec.Roster = hashRoster(g.ps, resolveRoster, rosterIDs)

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
	// only if the task ever terminates. A delegated child is the exception
	// in both shapes: its index is the one-live-child rule's liveness, so
	// the heal retires it (healActiveTask says why).
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
	// A slash command resolves before everything else (architecture 02,
	// "Chat entrypoints"): it is not a status ask, not a stop, and never a
	// steer. Text only - a programmatic cancel keeps its intent whatever
	// its text says.
	sessionRest, sessionCmd := isSessionCommand(msg.Text)
	if sessionCmd && g.spawner != nil && g.cfg.DefaultAddressee == RouteSession &&
		sessionRest != "" && !isSessionOff(sessionRest) {
		// Post-flip, every conversation is a session already, so
		// "/session <text>" is <text>: the ordinary turn it would have been
		// without the prefix, not a confirmation that drops the ask.
		msg.Text, sessionCmd = sessionRest, false
	}
	// An explicit cancel from a backend that can express one is read before
	// the text is read at all: it is not an ask, and a program must never
	// have to spell "stop" to reach a control path. Read after the strip
	// above, so a post-flip "/session stop" is the stop it unwraps to.
	stopping := msg.Intent == IntentCancel || isStop(msg.Text)
	switch {
	case msg.Intent == "" && sessionCmd:
		if !g.sessionCommand(ctx, rec, msg, backend, sessionRest, principal, authority) {
			return
		}
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
		g.steerTask(ctx, rec, msg, backend, authority)
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
			if !g.retireIncarnation(ctx, rec, "delegate", retireRefusalNotStarted) {
				return
			}
			if rec.Profile == "" {
				rec.Profile = sessionProfile
			}
			rec.BusSession = mintSessionName(rec.Profile)
			rec.Addressee = rec.BusSession
			msg.Text = rest
		} else if rec.SessionRouted {
			// Every new task on the session route gets a fresh incarnation;
			// freshIncarnation says why and holds the cap.
			if !g.freshIncarnation(ctx, rec) {
				return
			}
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
				if !g.retireIncarnation(ctx, rec, "pre-flip upgrade", retireRefusalNotStarted) {
					return
				}
				rec.SessionRouted = true
				rec.Profile = sessionProfile
				rec.BusSession = mintSessionName(rec.Profile)
				rec.Addressee = rec.BusSession
			}
		}
		g.startTask(ctx, rec, msg, backend, principal, authority)
	}

	if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
		g.log.Error("session record write failed", "conversation", rec.Key, "err", err)
	}
}

// backendFor names the ingress one message arrived through. A message that
// names none is the configured backend's, which is the Discord and Google
// Chat adapters' case; the inject door and the console adapter stamp their
// own, because either can be armed beside a real backend and the authority
// block must say which door a task came in through rather than which backend
// the process was configured with.
func (g *Gateway) backendFor(msg InboundMessage) string {
	if msg.Backend != "" {
		return msg.Backend
	}
	return g.backend
}

// principalMapFor is the map that backend's identities live in. The side
// door's is its own and is never a fallback to the chat map, nor the chat
// map's a fallback to it: an id that resolves in the wrong map would be an
// identity asserted by a door that is not allowed to assert it. The console
// is not an arm here because it has no map to name - its grant is the
// mechanism (resolvePrincipal), and rosterResolver sends it down that path
// before it ever reaches this one.
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
	// The addressee the task's own subjects carry, as probeConversation
	// reads it. For a non-detached active task it equals rec.Addressee
	// today, because every write to rec.Addressee sits in the routing
	// switch's default branch, which only a nil or detached active task
	// reaches; reading it off the ref keeps the heal from depending on that.
	addressee := rec.AddresseeFor(active.TaskID)
	task, terminalSubject, err := g.client.TasksGetAttributed(ctx, addressee, active.TaskID)
	healed := false
	var healedSource TerminalSource
	var healedTask *lib.Task // the terminal the heal found, shape (a) only
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
		if terminalSubject == lib.TaskSupervisorSubject(addressee, active.TaskID) {
			source = TerminalFromSupervisor
		}
		g.observeTaskTerminal(rec.Key, active.TaskID, task.State, source, finalMessageText(task))
		healed, healedSource, healedTask = true, source, task
	case isTaskNotFound(err) && !active.SubmittedAt.IsZero() &&
		time.Since(active.SubmittedAt) > g.cfg.FirstEventGrace:
		g.log.Info("healing an active task with no first event inside the grace",
			"conversation", rec.Key, "taskId", active.TaskID, "addressee", addressee,
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
		healed, healedSource = true, TerminalNeverStarted
	}
	if healed {
		// Notices held for the task's terminal (a delegation refusal)
		// follow the answer the heal just posted, as relayTerminal posts
		// them after the deliverable.
		g.mu.Lock()
		rs := g.relays[active.TaskID]
		g.mu.Unlock()
		if rs != nil {
			g.flushNotices(rec.Key, rs)
		}
		// A delegated child's route is retired here, as relayTerminal
		// retires it: its index is the liveness the one-live-child rule
		// reads (liveChild), so a healed child left indexed would refuse
		// every later delegation in the conversation until the reap. It is
		// also what keeps the wake below to one: a duplicate of the
		// terminal the heal found finds no route and is dropped, so it
		// neither posts nor wakes. A never-started child's late events are
		// dropped the same way. A human turn keeps its index, so its late
		// result still posts.
		ref, known := rec.TaskRefFor(active.TaskID)
		child := known && ref.Role == taskRoleChild
		if child {
			g.retireTaskRoute(ctx, active.TaskID)
		}
		rec.ActiveTask = nil
		// The same rule as relayTerminal's, for the same terminal reaching
		// the record by the other route: an executor's end of the task is
		// activity, and the idle window opens at the answer. Without this a
		// healed thread went quiet the moment its lost answer was posted.
		if healedSource == TerminalFromExecutor {
			now := time.Now().UTC()
			rec.LastActivity = now
			rec.LastTaskActivity = now
		}
		// A child's terminal found on the stream wakes the session as the
		// relay would have (spec §4), with the same guards and text, after
		// the card, the end's announcement and the route's retirement. A
		// child that never started does not: the room has the never-started
		// notice, and there is no outcome to hand the session. The turn
		// that ran the heal then routes against the wake as its active task.
		if child && healedTask != nil {
			result, reason := healedChildOutcome(healedTask)
			g.wakeSession(ctx, rec, ref, healedTask.State, result, reason)
		}
		// Write the release now, not at the end of the turn: a turn that
		// returns early — a cap refusal, on exactly the Delegate that
		// follows a wedge — would otherwise announce a release it never
		// wrote and announce it again on the next turn.
		if err := withRetry(kvRetryAttempts, func() error { return g.reg.Put(ctx, rec) }); err != nil {
			g.log.Error("healed record write failed", "conversation", rec.Key, "err", err)
		}
	}
}

// healedChildOutcome is a healed child's result and reason as relayTerminal
// hands them to wakeSession: the result artifact's text (the stand-in line
// for a completed task with none) and the terminal message.
func healedChildOutcome(task *lib.Task) (result, reason string) {
	if art := task.Artifact(lib.ArtifactResult); art != nil {
		result = joinTextParts(art.Parts)
	}
	if result == "" && task.State == lib.StateCompleted {
		result = completedNonTextResult
	}
	return result, finalMessageText(task)
}

// probeConversation is the ConversationProbe the gateway offers a ProbeSink:
// the session record for one conversation and the state of one task's
// stream, as they stand. A pure read, as ConversationProbe requires:
// no lock, no heal, no post, no publish, no write. It does not take the
// session lock because it needs nothing the lock protects -- the KV read is
// atomic and the stream replay is its own snapshot -- and holding it would
// let a slow read delay the turn the caller is waiting on.
//
// Which task: the one taskID names when the caller gives one, else the
// record's active task. A caller names the task it is grading, and the
// record stops holding that task the moment the relay posts its terminal
// (relayTerminal clears ActiveTask) -- which is exactly when an eval
// harness assembles its record -- so a read of the active task alone
// answers a finished run with no stream at all. The record's task history
// still knows the addressee the task's subjects carried (AddresseeFor, the
// relay's own replay choice) and the stream is durable, so the named read
// is the same read the active one is. Active and the fields beside it
// describe the record's active task only when it is the task being read; a
// named task the record no longer holds reports Active false with zero
// SubmittedAt and Age, and its stream. A task the record never held, active
// or in its history, is not read at all: the door's token admits its holder
// to conversations, not to every task on a shared addressee, and an id that
// is not the record's is never formatted into a subject. A conversation with no record has
// had no turn, so no task of it was ever published: the empty state, not a
// read against the configured default addressee, which may be the
// RouteSession sentinel -- a route, never a subject.
func (g *Gateway) probeConversation(ctx context.Context, key, taskID string) (ConversationState, error) {
	state := ConversationState{
		Backend:    g.backend,
		InjectOnly: g.backend == injectBackend,
		Grace:      g.cfg.FirstEventGrace,
	}
	rec, err := g.reg.Get(ctx, key)
	if err != nil {
		return state, fmt.Errorf("session lookup: %w", err)
	}
	if rec == nil {
		return state, nil
	}
	active := rec.ActiveTask
	if taskID == "" {
		if active == nil {
			return state, nil
		}
		taskID = active.TaskID
	}
	state.TaskID = taskID
	// A named task is read only when this conversation owns it: the active
	// task, or one in the record's own history. The door's token admits a
	// caller to conversations under its prefix, not to every task on the
	// addressee, and AddresseeFor's fallback for an unknown id is the
	// record's addressee - on a fixed-addressee install the same `platform`
	// every conversation shares - so an unowned id must never reach the
	// bus. It also keeps the query string off the subject builder: an id
	// that is not in the record is never formatted into a NATS subject, so
	// a wildcard is a miss, not a replay of the whole addressee. A task
	// older than the history cap reads as unknown, which is the price.
	var addressee string
	switch {
	case active != nil && active.TaskID == taskID:
		state.Active = true
		state.SubmittedAt = active.SubmittedAt
		state.Detached = active.Detached
		if !active.SubmittedAt.IsZero() {
			state.Age = time.Since(active.SubmittedAt)
		}
		// Against the addressee the task's own subjects carried: after a
		// Delegate re-home rec.Addressee is not it (the relay's terminal
		// replay makes the same choice).
		addressee = rec.AddresseeFor(taskID)
	default:
		ref, owned := rec.TaskRefFor(taskID)
		if !owned {
			return state, nil
		}
		addressee = ref.Addressee
	}
	task, terminalSubject, terr := g.client.TasksGetAttributed(ctx, addressee, taskID)
	switch {
	case terr == nil:
		state.ExecutorState = task.State
		state.Final = task.Final
		state.ReachedWorking = slices.Contains(task.StatusHistory, lib.StateWorking)
		// The trace and the progress line as they stand, final or not: a
		// caller watching a running task reads what it has called so far.
		// Non-nil from here on even when empty, because "read and found
		// nothing" is a fact about the executor and nil is not.
		state.Activity = make([]json.RawMessage, 0)
		for _, p := range artifactParts(task, lib.ArtifactActivity) {
			if p.Kind == "data" && len(p.Data) != 0 {
				state.Activity = append(state.Activity, p.Data)
			}
		}
		state.Progress = lastTextPart(artifactParts(task, lib.ArtifactProgress))
		if task.Final {
			// The fold's terminal, with whose word it is: the events
			// subject is the executor's, the supervisor subject the
			// supervisor's. A caller grading an answer off this read takes
			// only the executor's; a supervisor terminal says an executor
			// died or never ran, which is the install's.
			state.TerminalSource = TerminalFromExecutor
			if terminalSubject == lib.TaskSupervisorSubject(addressee, taskID) {
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
		return state, fmt.Errorf("reading task %s on %s: %w", taskID, addressee, terr)
	}
	return state, nil
}

// retireIncarnation deletes the record's lingering session pod before a
// successor is minted or the route is left. Delete rather than just untrack:
// once PodName clears, reap can never find the pod again, and sweep only
// sees terminal phases - a wedged Running pod would hold its bus credential
// forever. A detached task's supervisor terminal is owed BEFORE the delete
// (the one deletion rule in Session lifecycle); false means that terminal
// could not be published, a post has said so, and nothing was touched.
// Spawner-nil is the W4-rollback shape: the record names a pod nothing can
// manage, so the binding is cleared and the path degrades as it always did.
func (g *Gateway) retireIncarnation(ctx context.Context, rec *SessionRecord, why, refusal string) bool {
	if rec.PodName == "" {
		return true
	}
	if !g.closeDetachedBeforeDelete(ctx, rec) {
		g.post(rec.Key, refusal)
		return false
	}
	if g.spawner != nil {
		if err := g.spawner.Delete(ctx, rec.PodName); err != nil {
			g.log.Warn(why+": incarnation delete failed; pod may linger", "pod", rec.PodName, "err", err)
		}
	}
	rec.PodName = ""
	return true
}

// freshIncarnation retires the previous session pod, if any, and mints the
// next incarnation's name as the record's addressee. The worker adapter is
// one task per process, so a lingering PodName names an executor that can
// never serve the next task; publishing toward it wedges the conversation
// (S9 review finding). Retire it the way Delegate does: supervisor terminal
// for a detached task first, then the delete, then the successor. The cap
// holds here too, or Delegate refusals just push the flood one affordance
// over.
//
// False means the turn was refused - at the cap, or because the previous
// task could not be closed on the bus - and a post has already said so.
// Nothing below the last refusal has run, so the caller returns without a
// Put and whatever it set in memory beforehand never persists.
func (g *Gateway) freshIncarnation(ctx context.Context, rec *SessionRecord) bool {
	if g.refuseAtSessionCap(ctx, rec, rec.PodName != "") {
		return false
	}
	if !g.retireIncarnation(ctx, rec, "session route", retireRefusalNotStarted) {
		return false
	}
	rec.BusSession = mintSessionName(rec.Profile)
	rec.Addressee = rec.BusSession
	return true
}

// sessionCommand resolves the one slash command the gateway owns:
// "/session" marks the conversation session-routed, "/session <text>" marks
// it and runs <text> as the first turn, "/session off" releases the
// incarnation and re-homes to the default addressee. Deterministic, like
// every other affordance here; it names a route, not a handle, and it is
// resolved before status, stop and steer (spec-chatops-gateway, "Sessions by
// default"). It reports whether the record is to be written back: true for
// every answered turn, including the informational replies (a turn is a turn,
// and the idle clock moves as it does for "nothing is running"); false only
// when the turn was refused part-way (the cap, or a previous task that could
// not be closed) and the in-memory route change must not persist.
func (g *Gateway) sessionCommand(ctx context.Context, rec *SessionRecord, msg InboundMessage, backend, rest, principal string, authority Authority) bool {
	off := isSessionOff(rest)
	// The way back is answered even with no spawner: a record left
	// session-routed after A2A_SPAWN_SESSIONS was disarmed (the W4-rollback
	// shape) would otherwise publish every ask to an addressee nothing
	// serves, with no user-reachable way out. retireIncarnation tolerates a
	// nil spawner; only the on-forms need one.
	if g.spawner == nil && !(off && rec.SessionRouted) {
		g.post(rec.Key, "🤷 sessions are not enabled on this install")
		return true
	}
	if g.cfg.DefaultAddressee == RouteSession {
		// Post-flip: every conversation is a session already.
		if off {
			g.post(rec.Key, "ℹ️ sessions are the default on this install; there is nothing to turn off")
		} else {
			g.post(rec.Key, "ℹ️ this conversation is already a session")
		}
		return true
	}
	running := rec.ActiveTask != nil && !rec.ActiveTask.Detached
	if off {
		if !rec.SessionRouted {
			g.post(rec.Key, "ℹ️ not on the session route; nothing to turn off")
			return true
		}
		if running && rec.AddressedToOwnSession() {
			// The refusal protects a running SESSION task's pod: retiring it
			// would delete the pod out from under the task. A platform task
			// running on a record that was marked mid-task has no pod to
			// lose and keeps its addressee, so the way back goes through.
			g.post(rec.Key, "⚠️ a session task is still running — `stop` it first, then `/session off`")
			return true
		}
		if !g.retireIncarnation(ctx, rec, "session off", retireRefusalStillOn) {
			return false
		}
		rec.SessionRouted = false
		rec.Profile = ""
		rec.BusSession = ""
		rec.Addressee = g.cfg.DefaultAddressee
		g.log.Info("session route off", "conversation", rec.Key, "addressee", rec.Addressee)
		g.post(rec.Key, "↩️ session route off — back to `"+g.cfg.DefaultAddressee+"`")
		return true
	}
	if rec.SessionRouted && rest == "" {
		g.post(rec.Key, "ℹ️ already on the session route")
		return true
	}
	if isStop(rest) {
		// "/session stop" is almost certainly the way back misspelled. It
		// must never become a task whose text reads "stop" - the same rule
		// the stopping case below enforces for a bare "stop".
		g.post(rec.Key, "ℹ️ to leave the session route say `/session off`; to stop a running task say `stop`")
		return true
	}
	rec.SessionRouted = true
	if rec.Profile == "" {
		rec.Profile = sessionProfile
	}
	g.log.Info("session route on", "conversation", rec.Key, "firstTurn", rest != "", "taskRunning", running)
	switch {
	case rest == "" && running:
		// Truthful about the order: the next message steers the running
		// task (the steer case below), so it is the one after the task
		// ends that opens the pod.
		g.post(rec.Key, "🧵 session route on — the running task finishes first; the message after it opens a session pod; `/session off` returns to `"+g.cfg.DefaultAddressee+"`")
		return true
	case rest == "":
		g.post(rec.Key, sessionOnAck(backend, rec.Kind, g.cfg.DefaultAddressee))
		return true
	case running && rec.AddressedToOwnSession():
		// Already on the route with its own task running: the text is what
		// a plain message would have been, a steer into that task.
		msg.Text = rest
		g.steerTask(ctx, rec, msg, backend, authority)
		return true
	case running:
		g.post(rec.Key, "🧵 session route on — a task is still running, so that message was not sent; send it again when the task finishes")
		return true
	}
	msg.Text = rest
	if !g.freshIncarnation(ctx, rec) {
		return false
	}
	g.startTask(ctx, rec, msg, backend, principal, authority)
	return true
}

// sessionOnAck is the reply to a bare /session. It promises what the backend
// will deliver: the Slack adapter forwards an unmentioned channel-thread
// reply only once the gateway has started a task in that thread, and a
// /session binding starts none, so in a Slack channel the next message needs
// the mention. DMs carry every message; the other backends route the next
// message on their own rules.
func sessionOnAck(backend, kind, defaultAddressee string) string {
	if backend == slackBackend && kind != sessionKindDM {
		return "🧵 session route on — mention me in your next message to open a session pod (a Slack thread carries unmentioned replies only once a task has started); `/session off` returns to `" + defaultAddressee + "`"
	}
	return "🧵 session route on — your next message opens a session pod; `/session off` returns to `" + defaultAddressee + "`"
}

// hasSession is the SessionLookup the gateway offers a SessionLookupSink:
// whether the gateway has started a task in the conversation and the
// session is not idle past the idle TTL, read from the registry and
// reported with nothing changed, plus the moment that answer stops being
// trustworthy. A pure read, as SessionLookup requires -- no lock, no heal,
// no post, no publish, no write.
//
// A record alone is not the answer. mintSession creates one for ANY verified
// turn before the text is dispatched, so a mapped user's "@bot stop" with
// nothing running, or an ask refused at the session cap, leaves a record
// behind and starts nothing; answering true on that would adopt the thread
// the way TaskStarted never did, and the two sources would disagree. A
// started task is what startTask writes -- ActiveTask while it runs, and a
// TaskRef in Tasks for the record's life -- so that is what this reads.
//
// Nor is a record with a past task the answer forever. A running task keeps
// the conversation a session however long it runs (reapOnce never touches a
// pod under one, and a thread whose task is still working must carry the
// "stop"). Running is the reap's own predicate -- an ActiveTask that is not
// Detached -- so this read and the reap share one definition of it: a task
// the user has stopped, whose terminal never arrived (cancelTask sets
// Detached and leaves ActiveTask in place), is not running here any more
// than it exempts the pod there. Otherwise the conversation is a session
// only while it has had activity within the idle TTL: LastTaskActivity is
// written by routeTurn on every turn and persisted, and the TTL is the same
// g.cfg.IdleTTL reapOnce reads. The record itself is not the bound --
// reapOnce deletes the pod and keeps the record, Tasks and all, and nothing
// else deletes it -- so a read keyed on the record's existence would answer
// true for any thread a task ever started in, for good. Activity is the
// bound; the record merely carries it.
//
// The until returned with a true is the registry's own bound, not the
// adapter's: for the idle case it is LastTaskActivity + IdleTTL, the instant the
// registry itself would start answering false, so a cache that expires on
// it cannot outlive the answer it was given (a message the gateway refuses,
// an unmapped sender's, moves nothing, and a cache stamped from its own
// clock would hold a true the registry had already withdrawn). For a
// running task there is no bound to hand over, so it is now + IdleTTL: the
// adapter asks again then, and a task still running is answered again.
func (g *Gateway) hasSession(ctx context.Context, conversation string) (bool, time.Time, error) {
	rec, err := g.reg.Get(ctx, conversation)
	if err != nil || rec == nil {
		return false, time.Time{}, err
	}
	now := time.Now()
	if rec.ActiveTask != nil && !rec.ActiveTask.Detached {
		return true, now.Add(g.cfg.IdleTTL), nil
	}
	if len(rec.Tasks) > 0 {
		// The last TASK's activity, not the record's: LastActivity moves on
		// every verified turn, a "@bot stop" with nothing running included,
		// and a turn that starts nothing must not re-admit a thread whose
		// last task ended hours ago. LastTaskActivity moves only when a task
		// starts (startTask) or an executor ends a live one (relayTerminal).
		if until := rec.LastTaskActivity.Add(g.cfg.IdleTTL); now.Before(until) {
			return true, until, nil
		}
	}
	return false, time.Time{}, nil
}

// artifactParts is every part under one reserved artifact name, artifact by
// artifact in first-appearance order and each artifact's parts in arrival
// order (which is stream order for an executor that appends to one
// artifact id, as both executors do), across every artifact the fold holds under it -- not the first
// alone (Task.Artifact). The fold keys on artifactId when an update carries
// one, so an executor that gives each activity update its own id leaves the
// fold holding several artifacts named activity, and the trace is all of
// them; an executor that appends onto one id leaves one, and this reads the
// same.
func artifactParts(task *lib.Task, name string) []lib.Part {
	var parts []lib.Part
	for i := range task.Artifacts {
		if task.Artifacts[i].Name == name {
			parts = append(parts, task.Artifacts[i].Parts...)
		}
	}
	return parts
}

// observeTaskStarted and observeTaskTerminal tell an adapter that implements
// TaskObserver about a task's two ends. Both are no-ops for an adapter that
// does not implement the interface -- Discord and gchat, for which a human
// reads the chat and the rendered text is the whole interface. The Slack
// adapter implements it for TaskStarted and TaskTerminal: a task starting in a thread is
// what makes that thread a session thread, and the adapter learns it here
// rather than inferring it from a mention it has not yet seen verified.
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
	if gerr != nil {
		return nil, fmt.Errorf("lost the mint race but cannot read the winner: %w", gerr)
	}
	if winner == nil {
		return nil, errors.New("lost the mint race but winner record not found")
	}
	return winner, nil
}

// isMaxBytes reports whether err represents a NATS JetStream max_bytes limit
// or storage capacity refusal (e.g. JSStorageResourcesExceededErr, ErrMaxBytesExceeded,
// or maximum bytes exceeded). It checks typed jetstream.APIError fields and the
// innermost unwrapped root error to ensure conversation keys (which may embed arbitrary
// digit sequences like Discord snowflakes) cannot trigger false positives.
func isMaxBytes(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, jetstream.ErrMaxBytesExceeded) {
		return true
	}
	var apiErr *jetstream.APIError
	if errors.As(err, &apiErr) {
		if apiErr.ErrorCode == jsErrCodeStorageResourcesExceeded {
			return true
		}
		desc := strings.ToLower(apiErr.Description)
		if strings.Contains(desc, maxBytesErrPattern) || strings.Contains(desc, maximumBytesErrPattern) {
			return true
		}
	}
	root := err
	for {
		if unwrapped := errors.Unwrap(root); unwrapped != nil {
			root = unwrapped
		} else {
			break
		}
	}
	msg := strings.ToLower(root.Error())
	return strings.Contains(msg, maxBytesErrPattern) || strings.Contains(msg, maximumBytesErrPattern)
}

// taskStart is what startTaskWith needs to open a turn, whoever asked for
// it: a human message (startTask), a child minted on a session turn's
// request, or the wake turn started on that child's terminal.
type taskStart struct {
	// Text is the turn's ask.
	Text string
	// MessageID and Principal feed only the ingress log: the backend message
	// id the audit chain joins on, and the plaintext principal. A turn the
	// gateway starts on its own (a child, a wake) has no backend message.
	MessageID string
	Principal string
	// Requester is stored on the history entry as given — backend and the
	// already-pseudonymized subject. startTask hashes a human's author id;
	// a child copies its parent turn's entry.
	Requester TaskRequester
	// Authority renders the envelope's authority block; its Attribution is
	// what the history entry keeps.
	Authority Authority
	// CorrelationID empty mints a fresh one, which is a human turn: the
	// originating user interaction. A child rides its parent's (spec §3).
	CorrelationID string
	// Role, ParentTaskID and Depth go onto the history entry as the
	// delegation chain; all zero for a human turn.
	Role         string
	ParentTaskID string
	Depth        int
	// LineNote suffixes the turn's rolling line for as long as it renders;
	// a child says it was delegated. Empty for a human turn.
	LineNote string
	// SteerAuthors and SteerAuthorsOverflow seed the entry's
	// (TaskRef.SteerAuthors): a child's are its parent's, a wake's its
	// parent's and its child's. Empty for a human turn.
	SteerAuthors         []TaskRequester
	SteerAuthorsOverflow bool
}

// startTask opens a turn for a human message.
func (g *Gateway) startTask(ctx context.Context, rec *SessionRecord, msg InboundMessage, backend, principal string, authority Authority) {
	g.startTaskWith(ctx, rec, taskStart{
		Text:      msg.Text,
		MessageID: msg.MessageID,
		Principal: principal,
		Requester: TaskRequester{Backend: backend, Subject: requesterSubject(g.ps, backend, msg.AuthorID)},
		Authority: authority,
	})
}

// startTaskWith mints the identifiers, publishes the submission, and posts
// the placeholder the relay will edit. It reports the task id and true once
// the submission is on the bus, and "" and false on every path that refused
// or failed before that.
func (g *Gateway) startTaskWith(ctx context.Context, rec *SessionRecord, ts taskStart) (string, bool) {
	taskID := "task-" + randHex(taskIDHexWidth)
	// correlationId is minted here and nowhere else — the originating user
	// interaction (payload spec field rule). A turn opened on another
	// turn's behalf carries that turn's instead.
	correlationID := ts.CorrelationID
	if correlationID == "" {
		correlationID = "corr-" + randHex(correlationIDHexWidth)
	}

	// The ingress log is the plaintext join: backend message id against
	// correlationId, so the audit chain runs chat message -> correlationId ->
	// every hop -> change. Plaintext stays local; the bus gets pseudonyms.
	g.log.Info("ingress",
		"correlationId", correlationID,
		"taskId", taskID,
		"backendMessageId", ts.MessageID,
		"principal", ts.Principal,
		"conversation", rec.Key,
		"addressee", rec.Addressee)

	// The root capability, minted before anything is published. The
	// gateway allocates the taskId and the addressee in the same breath, so
	// the entry can name the principal that will serve the request before
	// that principal's credential exists — which is the property 09 §5 says
	// both delegate rules need and nothing in the design set provided.
	//
	// A mint that fails refuses the turn. The alternative is a submission
	// with no capability, which the executor refuses anyway, one round trip
	// and a pod later, with the reason in a different component's log.
	capRef, err := g.mintCapability(ctx, taskID, rec.Addressee)
	if err != nil {
		g.log.Error("capability mint failed", "taskId", taskID, "addressee", rec.Addressee, "err", err)
		if !g.cfg.CapabilityOptional {
			g.post(rec.Key, "⚠️ not started: could not mint this task's capability")
			return "", false
		}
		// The mixed-version window, and the only path that reaches an
		// executor with grants null. Loud: an install left here has the
		// control switched off on both sides at once.
		g.log.Warn("A2A_CAPABILITY_REQUIRED=false; sending this task with no capability",
			"taskId", taskID, "addressee", rec.Addressee)
		capRef = nil
	}

	payload, err := messagePayload(ts.Text, taskID, rec.ContextID)
	if err != nil {
		g.log.Error("message payload build failed", "err", err)
		return "", false
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, rec.ContextID, correlationID, payload,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(ts.Authority.Render(capRef)))
	if err != nil {
		g.log.Error("envelope build failed", "err", err)
		return "", false
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
	statusMsgID, err := g.adapter.Post(rec.Key, withLineNote("⏳ submitted…", ts.LineNote))
	if err != nil {
		g.log.Error("placeholder post failed", "conversation", rec.Key, "err", err)
	}

	// Register the task everywhere the relay looks BEFORE publishing: a fast
	// executor's submitted event must never race the mapping, because the
	// relay acks what it cannot route and the durable won't redeliver it.
	rec.ActiveTask = &ActiveTask{TaskID: taskID, CorrelationID: correlationID, StatusMsgID: statusMsgID,
		Ask: truncateRunes(ts.Text, askCap), SubmittedAt: time.Now(), Capability: capRef, LineNote: ts.LineNote}
	requester := ts.Requester
	ref := TaskRef{
		ID: taskID, Addressee: rec.Addressee, CorrelationID: correlationID, Capability: capRef,
		Requester:    &requester,
		Attribution:  ts.Authority.Attribution(),
		StartedAt:    time.Now().UTC(),
		Role:         ts.Role,
		ParentTaskID: ts.ParentTaskID,
		Depth:        ts.Depth,
	}
	ref.carrySteerAuthors(TaskRef{SteerAuthors: ts.SteerAuthors, SteerAuthorsOverflow: ts.SteerAuthorsOverflow})
	rec.Tasks = append(rec.Tasks, ref)
	// Text published to the conversation's own incarnation joins its author
	// set; a child to platform does not reach the incarnation.
	if rec.AddressedToOwnSession() {
		rec.addTurnToSession(requester, ref.SteerAuthors, ref.SteerAuthorsOverflow)
	}
	rec.LastTaskActivity = time.Now().UTC()
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
		return "", false
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
	return taskID, true
}

// steerTask forwards a message that arrived while the task runs as a
// follow-up on the same taskId — injected, absorbed at the executor's next
// turn boundary (decided 8/24). It reuses the task's correlationId; the
// steer is attributed by its own envelope and authority block.
func (g *Gateway) steerTask(ctx context.Context, rec *SessionRecord, msg InboundMessage, backend string, authority Authority) {
	active := rec.ActiveTask
	// The steer's author is on the turn's entry before the publish, and
	// stays there if the publish fails: a lost ack does not prove the steer
	// never arrived, and an author recorded for nothing costs at most a
	// refused delegation. The caller writes the record back.
	author := TaskRequester{Backend: backend, Subject: requesterSubject(g.ps, backend, msg.AuthorID)}
	rec.recordSteerAuthor(active.TaskID, author)
	if rec.AddressedToOwnSession() {
		rec.addSessionAuthor(author)
	}
	payload, err := messagePayload(msg.Text, active.TaskID, rec.ContextID)
	if err != nil {
		g.log.Error("steer payload build failed", "err", err)
		return
	}
	// One task, one capability: the steer rides the reference minted at
	// submission rather than a fresh one. A second root for the same request
	// would be a second thing to revoke.
	env, err := lib.NewMessageEnvelope(gatewayParty, active.TaskID, rec.ContextID, active.CorrelationID, payload,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(authority.Render(active.Capability)))
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
func (g *Gateway) cancelTask(ctx context.Context, rec *SessionRecord, authority Authority) {
	active := rec.ActiveTask
	env, err := lib.NewCancelEnvelope(gatewayParty, active.TaskID, rec.ContextID, active.CorrelationID,
		lib.WithTo(lib.Party{Session: rec.Addressee}),
		lib.WithAuthority(authority.Render(active.Capability)))
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
	// The post before the signal: a waiter woken by the signal snapshots the
	// conversation's entries, and the line saying the cancel went belongs in
	// what it reads.
	g.post(rec.Key, "🛑 cancel sent — the task ends when the executor confirms")
	g.observeCancelPublished(rec.Key, active.TaskID)
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
// cancel bounds that run. It rides the task's own chain and its own authority
// (the history entry keeps the correlation id, the addressee and the
// capability), is recorded on the history
// entry like any published cancel, and detaches nothing, because nothing is
// active. A task this conversation never held is refused: the gateway does
// not publish cancels for tasks it did not start.
func (g *Gateway) cancelNamedTask(ctx context.Context, rec *SessionRecord, taskID string, authority Authority) {
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
		lib.WithAuthority(authority.Render(ref.Capability)))
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
	g.log.Info("cancel published for a task the conversation no longer holds",
		"conversation", rec.Key, "taskId", taskID, "addressee", ref.Addressee)
	// The post before the signal, as in cancelTask.
	g.post(rec.Key, fmt.Sprintf("🛑 cancel sent for task `%s`, which this conversation no longer holds — "+
		"it ends when an executor confirms, if one ever took it", taskID))
	g.observeCancelPublished(rec.Key, taskID)
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

// rosterResolver picks the principal resolution to apply to one backend's
// roster ids, and two things decide it.
//
// Roster ids arrive in AuthorID vocabulary, so the console's fixed author
// has to resolve the way its requester does: resolvePrincipal maps "console"
// to "nats:console", while a principal map knows nothing about it and would
// leave H("console") in a snapshot whose requester.principal is
// H("nats:console") - the requester missing from its own audience.
//
// Every other backend resolves in its OWN map rather than in whichever one
// the gateway happens to hold, which is principalMapFor: the roster has to
// be read under the same map the requester's principal was read under, and
// one backend's map is never a fallback for another's.
func (g *Gateway) rosterResolver(backend string) func(string) string {
	if backend == consoleBackend {
		return func(id string) string { return g.resolvePrincipal(consoleBackend, id) }
	}
	return g.principalMapFor(backend).Resolve
}

func hashRoster(ps *Pseudonymizer, resolve func(string) string, ids []string) []string {
	out := make([]string, 0, min(len(ids), rosterCap))
	for _, id := range ids {
		if len(out) >= rosterCap {
			break
		}
		entry := id
		if p := resolve(id); p != "" {
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
