package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The A2A door: the gateway's ingress for an agent that speaks A2A over HTTP
// (Antigravity, an ADK client, the MCP bridge, curl). It is a sibling of the
// inject door and shares its shape - a side door beside the chat backends,
// authenticated by a bearer token, its callers resolved through a door-scoped
// principal map into eval identities and nothing else - and differs in what
// it speaks: JSON-RPC 2.0 in the A2A protocol's method set, and an agent card
// at the well-known path so a client can find it.
//
// Everything a caller does here becomes an ordinary gateway turn. The door
// holds no bus credential, mints no id the bus sees, writes no authority
// block; it hands an InboundMessage to handleInbound and reads what the
// gateway tells it back through TaskObserver and InboundObserver. That is the
// one-chokepoint rule (spec-chatops-gateway.md, "The A2A door"): the door
// must deliver into handleInbound and nowhere else.
//
// What a caller sees is the A2A Task object, assembled from what the relay
// posted on the door's conversation. The rolling progress line the relay
// edits is the task's status message; the deliverable the relay posts at a
// completed terminal is the task's `result` artifact; every post is in the
// history. A2A clients read exactly those three places.

const (
	// a2aBackend names the door in authority blocks and config, beside
	// injectBackend and consoleBackend.
	a2aBackend = "a2a"

	// a2aVerifiedBy is the mechanism the authority block records: this
	// door's bearer token, not a chat backend's channel and not the inject
	// door's token. Its own value for the same reason injectVerifiedBy is:
	// a reader downstream must be able to tell them apart.
	a2aVerifiedBy = "a2a-bearer"

	// a2aPrincipalPrefix qualifies every key in the door's own principal
	// map, and every value must carry injectEvalPrincipalPrefix. Same
	// construction as the inject door (Gateway.resolveA2APrincipal): the
	// lookup cannot reach an unprefixed entry, and a value outside the eval
	// namespace is refused rather than honoured. The developer identity
	// class (a2adoor_google.go) is a second resolver beside this one, not a
	// loosening of it.
	a2aPrincipalPrefix = "a2a:"

	// a2aKeyPrefix marks every conversation key this door handles. The mux
	// dispatches on the segment before the first colon, and the composite
	// beside it on the whole prefix.
	a2aKeyPrefix = "a2a:"

	// a2aConversationKind is what every door conversation reports as its
	// Kind: one caller, one context, so "dm" is the truth.
	a2aConversationKind = "dm"

	// a2aCallerHeader names the caller on a request, resolved through the
	// door's principal map the way the inject door's `author` field is. A
	// client that cannot set headers names itself in the message's
	// metadata under a2aCallerMetadataKey instead; the header wins when
	// both are present.
	a2aCallerHeader      = "X-A2A-Caller"
	a2aCallerMetadataKey = "caller"

	// a2aMessageIDPrefix marks the message ids the door mints for what the
	// gateway posts; a2aInboundIDPrefix the ids it mints for an inbound
	// message a caller did not name (the protocol requires one, but the
	// door would rather mint than refuse); a2aContextIDPrefix the context
	// ids it mints when a caller starts a conversation without one.
	a2aMessageIDPrefix = "a2a-"
	a2aInboundIDPrefix = "a2a-msg-"
	a2aContextIDPrefix = "ctx-"

	// Bounds on caller-supplied strings, in runes, with control characters
	// refused: each rides into the conversation key, the ingress log, or
	// both.
	a2aMaxCallerRunes    = 256
	a2aMaxContextRunes   = 256
	a2aMaxMessageIDRunes = injectMaxMessageIDRunes
	a2aMaxTextRunes      = injectMaxTextRunes
	a2aMaxBodyBytes      = injectMaxBodyBytes

	// Bounds on what the door remembers. Tasks evict oldest-first;
	// conversations evict the oldest idle one (see conversationLocked); a client that wants a task after eviction is told it is
	// not found, which is the protocol's own answer for a task the server
	// no longer holds.
	a2aMaxTasks         = 4096
	a2aMaxConversations = 1024
	// a2aMaxResultBytes bounds the artifact the door keeps. A longer
	// deliverable is cut at a rune boundary and the Task says so in
	// metadata.resultTruncatedFrom; history still carries the chunks whole.
	a2aMaxResultBytes = 4 << 20
	// a2aRetainedBudgetBytes bounds everything the door keeps per task,
	// summed across tasks: the caller's text, every post under the task and
	// the deliverable. Past it the oldest tasks are evicted whole (the
	// protocol's "not found", as under a2aMaxTasks), never the newest. The
	// counts above bound entries; this bounds the bytes behind them, sized
	// against the gateway's 512Mi pod. Turn logs and parked replies have
	// their own byte bounds below for the same reason.
	a2aRetainedBudgetBytes = 64 << 20
	// a2aMaxTurnPostBytes bounds one conversation's turn log; past it the
	// oldest posts go and the reply says so (metadata.postsEvicted).
	a2aMaxTurnPostBytes = 64 << 10
	// a2aMaxParkedReplyBytes bounds the reply kept for a retried message id.
	a2aMaxParkedReplyBytes = 8 << 10
	a2aMaxPostsPerTask     = 256
	a2aMaxTurnPosts        = 64
	a2aMaxSubmissions      = injectSeenCap

	// a2aSubmitWait bounds how long message/send waits for the gateway to
	// say what it did with the message - the same bound as the inject door,
	// for the same reason: a turn that will answer has answered by then.
	// a2aBlockingWait bounds a blocking message/send's wait for the task's
	// terminal on top of that; a client that wants longer polls tasks/get.
	a2aSubmitWait   = injectSubmitWait
	a2aBlockingWait = 5 * time.Minute

	// a2aPollInterval is the backstop for a missed wakeup on notify.
	a2aPollInterval = injectPollInterval

	a2aReadHeaderTimeout = injectReadHeaderTimeout
	a2aShutdownGrace     = injectShutdownGrace
)

// a2aPost is one message the gateway posted on a door conversation.
type a2aPost struct {
	id   string
	text string
}

// a2aTask is what the door knows about one task: the two ends the gateway
// announced, and what the relay posted between them.
type a2aTask struct {
	id        string
	contextID string
	key       string
	caller    string
	// user is the inbound message that started the task, for history[0].
	user lib.Message
	// state is submitted from TaskStarted, working from the first relay
	// edit, and the terminal state from TaskTerminal.
	state    lib.TaskState
	terminal bool
	reason   string
	source   TerminalSource
	accepted bool
	// lineID is the message id of the rolling progress line (the first post
	// after TaskStarted, which startTask makes the placeholder), and line
	// its current text. Everything else the relay posts under this task is
	// in posts, in order - the deliverable last, on a completed terminal.
	lineID string
	line   string
	posts  []a2aPost
	// result is the deliverable, handed over whole by the relay
	// (DeliverableObserver) before it posts it in chunks; delivered says
	// it arrived, so an empty result is distinguishable from none.
	result    string
	delivered bool
	// resultCutFrom is the deliverable's length when it exceeded
	// a2aMaxResultBytes; zero when it was kept whole.
	resultCutFrom int
	// bytes is what this task costs the retained budget: the caller's
	// text, the posts and the result.
	bytes int
	// cancelPublished: a cancel for this task reached the bus, so a retried
	// tasks/cancel is answered with the task, not refused.
	cancelPublished bool
	created         time.Time
	updated         time.Time
}

// a2aConversation is one caller's context: the turn accounting message/send
// needs, and the posts the turn in flight produced, which are the Message a
// turn that started no task returns.
type a2aConversation struct {
	key       string
	caller    string
	contextID string
	// busy is the one-turn-at-a-time guard: the counters below are per
	// conversation, so a second submission waits for the first turn to end
	// before it can read them. Set when a message is handed over and
	// cleared by TurnFinished, not by the send that returns on the task's
	// accept: the turn runs on past the accept (a session spawn is seconds),
	// and a claim inside that gap would read counters one turn short and
	// be answered from the first turn's end.
	busy    bool
	turns   int
	drops   int
	cancels int
	// active is the task the relay is posting under, set at TaskStarted and
	// cleared at TaskTerminal. tasks is the task ids in order, bounded the
	// way the inject door bounds its logs: a waiter indexes by position and
	// is told when its position was evicted past.
	active *a2aTask
	tasks  injectIDLog
	// turnPosts is every post the gateway made while a turn was in flight,
	// whatever task it was filed under, as an append-only log read from an
	// offset (the next claim cannot wipe a reply its waiter has not read). It is
	// what a turn that started no task answers with: a steer's
	// acknowledgement, a status answer, a cancel's notice, a refusal. Posts
	// under a running task are also that task's, so a follow-up on it is
	// answered from here while the task keeps them in its history.
	turnPosts a2aPostLog
	// pending is the inbound message of the turn in flight, which
	// TaskStarted copies onto the task it mints.
	pending lib.Message
}

// a2aPrior is a conversation's counters read before a turn is handed over,
// so the wait for its answer cannot mistake an earlier turn's for it.
type a2aPrior struct {
	turns, drops, cancels, tasks, posts int
}

// a2aPostLog is the turn posts' append-only log, injectIDLog's shape.
type a2aPostLog struct {
	posts []a2aPost
	total int
	bytes int
}

func (l *a2aPostLog) add(p a2aPost) {
	l.posts = append(l.posts, p)
	l.total++
	l.bytes += len(p.text)
	for len(l.posts) > 1 && (len(l.posts) > a2aMaxTurnPosts || l.bytes > a2aMaxTurnPostBytes) {
		l.bytes -= len(l.posts[0].text)
		l.posts = l.posts[1:]
	}
}

// since returns the posts past offset and whether some were evicted.
func (l *a2aPostLog) since(offset int) ([]a2aPost, bool) {
	if l.total <= offset {
		return nil, false
	}
	idx := offset - (l.total - len(l.posts))
	if idx < 0 {
		return l.posts, true
	}
	return l.posts[idx:], false
}

// a2aOutcome is what a submission resolved to, kept by caller and message id
// so a retry of the same message/send is answered with the same task rather
// than routed again.
type a2aOutcome struct {
	// key is the conversation the message id was accepted on: a retry on
	// another context is a different message under a reused id, refused.
	key     string
	taskID  string
	message *a2aMessageObject
	err     *rpcError
	done    chan struct{}
}

// A2ADoor is the adapter. One instance serves one listener.
type A2ADoor struct {
	listen string
	token  string
	// google verifies the developer class's Google access tokens; nil when
	// the class is not configured, and then only the static token is
	// accepted.
	google *googleTokenVerifier
	// googleAllowed is the class's allowlist, keyed lower-cased; empty
	// admits nobody.
	googleAllowed    map[string]bool
	publicURL        string
	publicURLSet     bool
	agentName        string
	agentVersion     string
	defaultAddressee string
	// taskDeadline is the gateway's own; a conversation whose active task
	// is older than it no longer counts as live for the cap (see
	// conversationLocked).
	taskDeadline time.Duration
	log          *slog.Logger

	mu            sync.Mutex
	conversations map[string]*a2aConversation
	convOrder     []string
	tasks         map[string]*a2aTask
	taskOrder     []string
	// retainedBytes is the tasks' bytes summed, for the budget.
	retainedBytes int
	submissions   map[string]*a2aOutcome
	subOrder      []string
	directOf      map[string]string
	directOrder   []string
	nextID        int
	// notify is closed and replaced whenever anything changes; see the
	// inject door for why one channel serves the whole adapter.
	notify chan struct{}

	handlerMu sync.RWMutex
	handler   func(InboundMessage)
	// probe is the gateway's ConversationProbe (ProbeSink), set before Run
	// and not read yet: the read that consults it, for a task the door
	// stopped holding across a restart, is a later change.
	probe ConversationProbe

	// listener, when set, is an already-bound listener Run serves on. Test
	// injection only.
	listener net.Listener
}

// A2ADoorOptions is what NewA2ADoor needs beyond the listen address and the
// token.
type A2ADoorOptions struct {
	// PublicURL is the URL the agent card advertises for the JSON-RPC
	// endpoint: what a client reaches this door at, which behind a
	// port-forward or an ingress is not the listen address. Empty makes the
	// card advertise the address it was fetched from (rpcURLFor).
	PublicURL string
	// DefaultAddressee is the gateway's default destination, which is the
	// one skill the card lists until profiles supply a catalog.
	DefaultAddressee string
	// AgentName and AgentVersion are the card's; empty takes defaults.
	AgentName    string
	AgentVersion string
	// TaskDeadline is the gateway's task deadline (A2A_TASK_DEADLINE_SECONDS);
	// zero takes the config default. See A2ADoor.taskDeadline.
	TaskDeadline time.Duration
	// GoogleClientID arms the developer identity class: a bearer that is
	// not the static token is checked as a Google access token issued for
	// this client id. Empty leaves the class off.
	GoogleClientID string
	// GoogleAllowedUsers is the class's allowlist (A2A_DOOR_ALLOWED_USERS):
	// a verified email off it is refused before the door holds anything for
	// the caller. Empty admits nobody.
	GoogleAllowedUsers []string
	Logger             *slog.Logger
}

// NewA2ADoor builds the door. The token is required here as well as in
// FromEnv, for the reason NewInjectAdapter gives: a door that could be built
// without one would make "unauthenticated" a thing a caller can choose.
func NewA2ADoor(listen, token string, o A2ADoorOptions) (*A2ADoor, error) {
	if strings.TrimSpace(listen) == "" {
		return nil, fmt.Errorf("the A2A door needs a listen address")
	}
	if strings.TrimSpace(token) == "" {
		return nil, fmt.Errorf("the A2A door needs a bearer token: it authenticates every RPC request (the agent card is the one unauthenticated route), and the NetworkPolicy in front of it does not govern the port-forward path")
	}
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	publicURL := strings.TrimSpace(o.PublicURL)
	publicURLSet := publicURL != ""
	if !publicURLSet {
		publicURL = "http://" + listen + a2aRPCPath
	}
	name := o.AgentName
	if name == "" {
		name = "kube-agents"
	}
	version := o.AgentVersion
	if version == "" {
		version = "0.1.0"
	}
	deadline := o.TaskDeadline
	if deadline <= 0 {
		deadline = defaultTaskDeadline
	}
	return &A2ADoor{
		listen:           listen,
		token:            token,
		publicURL:        publicURL,
		publicURLSet:     publicURLSet,
		agentName:        name,
		agentVersion:     version,
		defaultAddressee: o.DefaultAddressee,
		taskDeadline:     deadline,
		log:              log,
		google:           googleVerifierFor(o.GoogleClientID),
		googleAllowed:    googleAllowlist(o.GoogleAllowedUsers),
		conversations:    map[string]*a2aConversation{},
		tasks:            map[string]*a2aTask{},
		submissions:      map[string]*a2aOutcome{},
		directOf:         map[string]string{},
		notify:           make(chan struct{}),
	}, nil
}

// SetProbe receives the gateway's ConversationProbe (ProbeSink); see the
// field.
func (d *A2ADoor) SetProbe(probe ConversationProbe) {
	d.probe = probe
}

// Run serves the card and the RPC endpoint until ctx is done.
func (d *A2ADoor) Run(ctx context.Context, handler func(InboundMessage)) error {
	d.handlerMu.Lock()
	d.handler = handler
	d.handlerMu.Unlock()
	defer func() {
		d.handlerMu.Lock()
		d.handler = nil
		d.handlerMu.Unlock()
	}()

	mux := http.NewServeMux()
	mux.HandleFunc(a2aCardPath, d.handleCard)
	mux.HandleFunc(a2aRPCPath, d.handleRPC)
	srv := &http.Server{Handler: mux, ReadHeaderTimeout: a2aReadHeaderTimeout}

	ln := d.listener
	if ln == nil {
		var err error
		ln, err = net.Listen("tcp", d.listen)
		if err != nil {
			return fmt.Errorf("A2A door listen on %s: %w", d.listen, err)
		}
	}
	d.log.Warn("the A2A door is armed: a bearer-token holder that reaches this listener can "+
		"submit tasks as a mapped eval principal and read the tasks it submitted. "+
		"Dev and eval installs only until the door has ingress a customer reaches.",
		"address", ln.Addr().String(), "rpc", d.publicURL)
	if d.google != nil {
		d.log.Warn("the A2A door's Google sign-in is armed: a Google account on the door's allowlist that reaches this listener "+
			"can submit tasks as itself, and any bearer that is not the door's token is sent to Google's tokeninfo endpoint to be checked",
			"allowedUsers", len(d.googleAllowed))
	}

	errs := make(chan error, 1)
	go func() {
		err := srv.Serve(ln)
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		errs <- err
	}()
	select {
	case err := <-errs:
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), a2aShutdownGrace)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
		return nil
	}
}

// authorized is the doors' shared bearer check (bearerAuthorized).
func (d *A2ADoor) authorized(w http.ResponseWriter, r *http.Request) bool {
	return bearerAuthorized(w, r, d.token, "the A2A door requires a bearer token")
}

// handleCard serves the agent card. Unauthenticated, deliberately: A2A
// discovery reads the card to learn which security scheme the endpoint
// wants, so a card behind the token would be unreachable by the client that
// needs it. What it discloses is the endpoint URL, the scheme, and the name
// of the default destination - nothing a request could not learn from a 401.
func (d *A2ADoor) handleCard(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		injectError(w, http.StatusMethodNotAllowed, "GET only")
		return
	}
	writeJSON(w, http.StatusOK, d.card(d.rpcURLFor(r)))
}

// rpcURLFor is the endpoint the card advertises: the configured public URL,
// else the address this card was fetched from, so a card reached through a
// port-forward, a Service name or an ingress points back at itself rather
// than at the pod's loopback. Behind a proxy the forwarded headers win.
func (d *A2ADoor) rpcURLFor(r *http.Request) string {
	if d.publicURLSet {
		return d.publicURL
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	// Both headers are lists on the wire once two hops forward; the first
	// element is the client-facing one. The scheme is one of two values,
	// not whatever a hop wrote.
	proto, _, _ := strings.Cut(r.Header.Get("X-Forwarded-Proto"), ",")
	if p := strings.TrimSpace(proto); p == "http" || p == "https" {
		scheme = p
	}
	fhost, _, _ := strings.Cut(r.Header.Get("X-Forwarded-Host"), ",")
	host := strings.TrimSpace(fhost)
	if host == "" {
		host = r.Host
	}
	if host == "" {
		return d.publicURL
	}
	return scheme + "://" + host + a2aRPCPath
}

// card is the catalog. One skill per destination this door routes to, which
// today is the gateway's default addressee; when profiles land the list is
// rendered from DIRECTORY and the caller's entitlements, and the card
// becomes per caller.
func (d *A2ADoor) card(rpcURL string) a2aAgentCard {
	// The schemes are alternatives: the static bearer, and Google sign-in
	// when the developer class is armed.
	schemes := map[string]a2aScheme{"bearer": {Type: "http", Scheme: "bearer"}}
	security := []map[string][]string{{"bearer": {}}}
	if d.google != nil {
		schemes[a2aGoogleSchemeName] = a2aScheme{Type: "openIdConnect", OpenIDConnectURL: a2aGoogleOpenIDConfigURL}
		security = append(security, map[string][]string{a2aGoogleSchemeName: {}})
	}
	var skills []a2aSkill
	if d.defaultAddressee != "" {
		skills = append(skills, a2aSkill{
			ID:   d.defaultAddressee,
			Name: d.defaultAddressee,
			Description: fmt.Sprintf("The %s agent on this install. Ask in natural language; the message is routed "+
				"the way a chat message from a verified user is.", d.defaultAddressee),
			Tags: []string{"kubernetes"},
			Examples: []string{
				"what's the upgrade readiness of the fleet?",
				"which clusters have PodDisruptionBudgets missing?",
			},
		})
	}
	return a2aAgentCard{
		Name:            d.agentName,
		Description:     "kube-agents: a fleet of Kubernetes agents behind one chat gateway. This endpoint is the gateway's A2A door.",
		URL:             rpcURL,
		Version:         d.agentVersion,
		ProtocolVersion: a2aProtocolVersion,
		Capabilities: a2aCapabilities{
			// Streaming lands with message/stream; until then a client
			// that reads the card does not try it.
			Streaming:              false,
			PushNotifications:      false,
			StateTransitionHistory: false,
		},
		DefaultInputModes:  []string{a2aTextMediaType},
		DefaultOutputModes: []string{a2aTextMediaType},
		Skills:             skills,
		SecuritySchemes:    schemes,
		Security:           security,
	}
}

// handleRPC is the JSON-RPC endpoint. Auth first, before the method and the
// body, as on the inject door. Protocol-level failures are JSON-RPC errors
// on a 200, which is the binding's convention; only the transport-level
// refusals (no token, wrong method, oversize body) are HTTP statuses.
func (d *A2ADoor) handleRPC(w http.ResponseWriter, r *http.Request) {
	verified, ok := d.identify(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodPost {
		injectError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	var req rpcRequest
	body := http.MaxBytesReader(w, r.Body, a2aMaxBodyBytes)
	if err := json.NewDecoder(body).Decode(&req); err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			injectError(w, http.StatusRequestEntityTooLarge, "the request body is over the door's limit")
			return
		}
		writeJSON(w, http.StatusOK, rpcFail(nil, rpcParseError, "malformed JSON-RPC request: "+err.Error(), nil))
		return
	}
	if req.JSONRPC != "2.0" || strings.TrimSpace(req.Method) == "" {
		writeJSON(w, http.StatusOK, rpcFail(req.ID, rpcInvalidRequest, `jsonrpc must be "2.0" and method is required`, nil))
		return
	}
	d.handlerMu.RLock()
	handler := d.handler
	d.handlerMu.RUnlock()
	if handler == nil {
		writeJSON(w, http.StatusOK, rpcFail(req.ID, rpcInternalError, "the gateway is not running the A2A door yet", nil))
		return
	}

	var resp rpcResponse
	switch req.Method {
	case a2aMethodSend:
		resp = d.send(r, verified, handler, req)
	case a2aMethodGet:
		resp = d.get(r, verified, req)
	case a2aMethodCancel:
		resp = d.cancel(r, verified, handler, req)
	case a2aMethodStream:
		// The card says streaming is off; a client that tries anyway is
		// told so in the protocol's own terms rather than with a method
		// the door has never heard of.
		resp = rpcFail(req.ID, a2aErrUnsupportedOp,
			"message/stream is not served yet; use message/send with configuration.blocking and poll tasks/get", nil)
	default:
		resp = rpcFail(req.ID, rpcMethodNotFound, "unknown method "+req.Method, nil)
	}
	writeJSON(w, http.StatusOK, resp)
}

// callerOf names the caller: the header, else the message metadata. Empty
// is a refusal, never a default - the map is the whole of verification here
// and an unnamed caller has nothing to look up.
func callerOf(r *http.Request, metadata map[string]any) (string, *rpcError) {
	caller := strings.TrimSpace(r.Header.Get(a2aCallerHeader))
	if caller == "" && metadata != nil {
		if v, ok := metadata[a2aCallerMetadataKey].(string); ok {
			caller = strings.TrimSpace(v)
		}
	}
	if caller == "" {
		return "", &rpcError{Code: a2aErrAuthenticationFail,
			Message: fmt.Sprintf("the caller is not named: set the %s header or message.metadata.%s; it is resolved through the door's principal map",
				a2aCallerHeader, a2aCallerMetadataKey)}
	}
	if err := injectFieldWellFormed("caller", caller, a2aMaxCallerRunes); err != nil {
		return "", &rpcError{Code: rpcInvalidParams, Message: err.Error()}
	}
	// No colon: the caller is a segment of the conversation key, and a
	// colon in it would let two callers spell one key.
	if strings.Contains(caller, ":") {
		return "", &rpcError{Code: rpcInvalidParams, Message: "the caller must not contain a colon"}
	}
	return caller, nil
}

// a2aConversationKey is the door's key for one caller's context. Qualified
// by the door's prefix, so it cannot be mistaken for a chat backend's, and
// by the caller, so two callers naming the same context id do not share a
// conversation. The caller is the map key rather than the resolved
// principal because the door does not resolve - the gateway does, and the
// door's key must be computable before the turn.
func a2aConversationKey(caller, contextID string) string {
	return a2aKeyPrefix + caller + ":" + contextID
}

// send is message/send: one turn, answered with the Task it started or the
// Message the gateway replied with.
func (d *A2ADoor) send(r *http.Request, verified string, handler func(InboundMessage), req rpcRequest) rpcResponse {
	var params a2aSendParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := d.callerFor(r, verified, params.Message.Metadata)
	if rerr != nil {
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	msg := params.Message
	if msg.Role != "" && msg.Role != a2aRoleUser {
		return rpcFail(req.ID, rpcInvalidParams, `message.role must be "user"`, nil)
	}
	text, textOnly := textOf(msg.Parts)
	if !textOnly {
		return rpcFail(req.ID, a2aErrContentTypeNotSupp, "only text parts are accepted; a data or file part has no home on a chat turn", nil)
	}
	if strings.TrimSpace(text) == "" {
		return rpcFail(req.ID, rpcInvalidParams, "message.parts must carry text", nil)
	}
	if len([]rune(text)) > a2aMaxTextRunes {
		return rpcFail(req.ID, rpcInvalidParams, fmt.Sprintf("text is longer than %d runes", a2aMaxTextRunes), nil)
	}
	if err := injectFieldWellFormed("message.messageId", msg.MessageID, a2aMaxMessageIDRunes); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, err.Error(), nil)
	}
	if err := injectFieldWellFormed("message.contextId", msg.ContextID, a2aMaxContextRunes); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, err.Error(), nil)
	}
	contextID := strings.TrimSpace(msg.ContextID)
	if msg.TaskID != "" {
		// A follow-up onto a task: it lands on that task's conversation,
		// where the gateway treats it as a steer. The task must be the
		// caller's own and still running.
		task, ok := d.taskFor(msg.TaskID, caller)
		if !ok {
			return rpcFail(req.ID, a2aErrTaskNotFound, "no such task for this caller: "+msg.TaskID, nil)
		}
		if task.terminal {
			return rpcFail(req.ID, rpcInvalidParams, "task "+msg.TaskID+" is over; start a new one", nil)
		}
		if task.cancelPublished {
			// The gateway holds a cancelled task detached until the executor
			// confirms, and routes a follow-up on it as a new task or as a
			// steer of the next one; neither is what the caller named.
			return rpcFail(req.ID, rpcInvalidParams, "a cancel is pending on task "+msg.TaskID+"; start a new one", nil)
		}
		if contextID != "" && contextID != task.contextID {
			return rpcFail(req.ID, rpcInvalidParams, "message.contextId does not match the task's", nil)
		}
		if active := d.activeTaskOf(task.key); active != "" && active != task.id {
			// Same reason: the gateway steers whatever is active, so a
			// follow-up on an earlier task would be delivered to a later one.
			return rpcFail(req.ID, rpcInvalidParams, "task "+msg.TaskID+" is not the conversation's active task ("+active+"); follow up on that one or start a new task", nil)
		}
		contextID = task.contextID
	}
	if contextID == "" {
		contextID = a2aContextIDPrefix + randHex(messageIDHexWidth)
	}
	key := a2aConversationKey(caller, contextID)
	blocking := params.Configuration != nil && params.Configuration.Blocking != nil && *params.Configuration.Blocking

	messageID := strings.TrimSpace(msg.MessageID)
	// The wait for the turn's answer. A named message id is the dedupe key,
	// and a retry after a dropped connection must be answered with the
	// task the first attempt started, so the turn waits on a context the
	// disconnect does not cancel. A minted id cannot repeat, so it waits on
	// the request's own.
	turnCtx := r.Context()
	var outcome *a2aOutcome
	if messageID == "" {
		messageID = a2aInboundIDPrefix + randHex(messageIDHexWidth)
	} else {
		var seen bool
		outcome, seen = d.claimSubmission(caller, messageID, key)
		if seen {
			if outcome.key != key {
				// The id names one message on one context (the inject
				// door's 409): a reused id elsewhere is not a retry, and
				// answering it with the first context's task would drop
				// this message in silence.
				return rpcFail(req.ID, rpcInvalidParams, "message.messageId "+messageID+
					" was already accepted on another context; a message id names one message on one context", nil)
			}
			return d.answerOutcome(turnCtx, req.ID, outcome, blocking)
		}
		turnCtx = context.WithoutCancel(r.Context())
		defer d.completeOutcome(outcome, "", nil,
			&rpcError{Code: rpcInternalError, Message: "the first message/send with this messageId aborted before its turn answered"})
	}

	user := lib.Message{Role: a2aRoleUser, Parts: textParts(text), MessageID: messageID, ContextID: contextID, TaskID: msg.TaskID}
	deadline := time.Now().Add(a2aSubmitWait)
	prior, ok := d.claimTurn(turnCtx, key, caller, contextID, user, deadline)
	if !ok {
		// Nothing was handed over, so there is nothing for a retry under
		// this id to be answered with: settle any duplicate already parked
		// in answerOutcome with the refusal, then forget the id so the next
		// attempt is routed afresh rather than refused for good.
		rerr := &rpcError{Code: rpcInternalError, Message: earlierTurnNote()}
		d.completeOutcome(outcome, "", nil, rerr)
		d.forgetSubmission(caller, messageID, outcome)
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	handler(InboundMessage{
		Conversation: key,
		Kind:         a2aConversationKind,
		AuthorID:     caller,
		MessageID:    messageID,
		Text:         text,
		Backend:      a2aBackendForCaller(caller),
		TaskID:       msg.TaskID,
	})
	taskID, reply, rerr := d.awaitTurn(turnCtx, key, prior, deadline)
	d.completeOutcome(outcome, taskID, reply, rerr)
	if rerr != nil {
		if r.Context().Err() != nil {
			// Handed over, then the client left: the turn runs on and may
			// start a task nobody is watching, which is not "nothing".
			d.log.Info("a2a: the caller went away before the turn answered; the turn runs on",
				"conversation", key, "messageId", messageID)
		} else {
			d.log.Info("a2a: the door started nothing for a message/send",
				"conversation", key, "messageId", messageID, "code", rerr.Code, "note", rerr.Message)
		}
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	if taskID == "" {
		return rpcOK(req.ID, reply)
	}
	if blocking {
		// On the request's context, not the turn's: the submission is
		// settled above, a retry is answered from it, and a client that has
		// gone has nothing to deliver the terminal to.
		d.awaitTerminal(r.Context(), taskID, time.Now().Add(a2aBlockingWait))
	}
	return rpcOK(req.ID, d.taskObject(taskID))
}

// answerOutcome answers a retried message/send from the first attempt's
// outcome, waiting for it if the first is still in flight.
func (d *A2ADoor) answerOutcome(ctx context.Context, id json.RawMessage, o *a2aOutcome, blocking bool) rpcResponse {
	timer := time.NewTimer(a2aSubmitWait)
	defer timer.Stop()
	select {
	case <-o.done:
	case <-ctx.Done():
		return rpcFail(id, rpcInternalError, "the caller went away before the first attempt answered", nil)
	case <-timer.C:
		return rpcFail(id, rpcInternalError, "the first attempt with this messageId has not answered inside the bound", nil)
	}
	if o.err != nil {
		return rpcFail(id, o.err.Code, o.err.Message, nil)
	}
	if o.taskID == "" {
		return rpcOK(id, o.message)
	}
	if blocking {
		d.awaitTerminal(ctx, o.taskID, time.Now().Add(a2aBlockingWait))
	}
	return rpcOK(id, d.taskObject(o.taskID))
}

// get is tasks/get. Scoped to the caller: a task another caller started is
// not found, not forbidden, so the door does not confirm ids it will not
// serve.
func (d *A2ADoor) get(r *http.Request, verified string, req rpcRequest) rpcResponse {
	var params a2aIDParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := d.callerFor(r, verified, params.Metadata)
	if rerr != nil {
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	if strings.TrimSpace(params.ID) == "" {
		return rpcFail(req.ID, rpcInvalidParams, "id is required", nil)
	}
	if _, ok := d.taskFor(params.ID, caller); !ok {
		return rpcFail(req.ID, a2aErrTaskNotFound, "no such task for this caller: "+params.ID, nil)
	}
	obj := d.taskObject(params.ID)
	if params.HistoryLength != nil && *params.HistoryLength >= 0 && *params.HistoryLength < len(obj.History) {
		obj.History = obj.History[len(obj.History)-*params.HistoryLength:]
	}
	return rpcOK(req.ID, obj)
}

// cancel is tasks/cancel: a cancel turn on the task's conversation, answered
// once the gateway has put the cancel on the bus. The Task returned is the
// door's current snapshot - the executor decides when the task is canceled,
// and the client polls tasks/get for that terminal, as it would for any.
func (d *A2ADoor) cancel(r *http.Request, verified string, handler func(InboundMessage), req rpcRequest) rpcResponse {
	var params a2aIDParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := d.callerFor(r, verified, params.Metadata)
	if rerr != nil {
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	if strings.TrimSpace(params.ID) == "" {
		return rpcFail(req.ID, rpcInvalidParams, "id is required", nil)
	}
	task, ok := d.taskFor(params.ID, caller)
	if !ok {
		return rpcFail(req.ID, a2aErrTaskNotFound, "no such task for this caller: "+params.ID, nil)
	}
	if task.terminal {
		return rpcFail(req.ID, a2aErrTaskNotCancelable, "task "+params.ID+" is already "+string(task.state), nil)
	}
	if task.cancelPublished {
		// A retry (a lost response, a client timeout during the claim):
		// the cancel is on the bus already, which is what this call asks.
		return rpcOK(req.ID, d.taskObject(params.ID))
	}
	key, contextID := task.key, task.contextID
	messageID := a2aInboundIDPrefix + randHex(messageIDHexWidth)
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("stop"), MessageID: messageID, ContextID: contextID, TaskID: params.ID}
	deadline := time.Now().Add(a2aSubmitWait)
	// The claim is taken on a context the client cannot end, for the
	// inject door's reason: a caller that gives up while an earlier turn
	// still runs has still asked for the stop, and a cancel never handed
	// over leaves the stray running with nobody to stop it. The bound alone
	// ends the claim; the wait below stays on the request's context.
	prior, ok := d.claimTurn(context.WithoutCancel(r.Context()), key, caller, contextID, user, deadline)
	if !ok {
		return rpcFail(req.ID, rpcInternalError, earlierTurnNote(), nil)
	}
	handler(InboundMessage{
		Conversation: key,
		Kind:         a2aConversationKind,
		AuthorID:     caller,
		MessageID:    messageID,
		Text:         "stop",
		Backend:      a2aBackendForCaller(caller),
		Intent:       IntentCancel,
		TaskID:       params.ID,
	})
	published, note := d.awaitCancel(r.Context(), key, prior, deadline)
	if !published {
		return rpcFail(req.ID, a2aErrTaskNotCancelable, note, nil)
	}
	return rpcOK(req.ID, d.taskObject(params.ID))
}

// claimTurn takes the conversation's turn slot, waiting for an earlier turn
// to end, and reads the counters the wait for this turn's answer compares
// against. It also mints the conversation, so Roster answers inside the
// turn.
func (d *A2ADoor) claimTurn(ctx context.Context, key, caller, contextID string, user lib.Message, deadline time.Time) (a2aPrior, bool) {
	for {
		d.mu.Lock()
		conv := d.conversationLocked(key)
		conv.caller = caller
		conv.contextID = contextID
		d.noteDirectLocked(caller, key)
		if !conv.busy {
			if time.Until(deadline) < turnTimeout {
				// The slot is free, but too late: handed over now, this
				// turn would have less than the turnTimeout handleInbound
				// runs under, and the wait's deadline would fall while the
				// turn was still legitimately running -- a refusal pinned
				// under the message id for a message the gateway goes on
				// to act on. A refusal has to mean nothing started, so the
				// message is not handed over at all (the inject door's
				// claimTurn, which handleInbound's routing relies on).
				d.mu.Unlock()
				return a2aPrior{}, false
			}
			conv.busy = true
			conv.pending = user
			prior := a2aPrior{turns: conv.turns, drops: conv.drops, cancels: conv.cancels, tasks: conv.tasks.total, posts: conv.turnPosts.total}
			d.mu.Unlock()
			return prior, true
		}
		wait := d.notify
		d.mu.Unlock()
		if !d.sleep(ctx, wait, deadline) {
			return a2aPrior{}, false
		}
	}
}

// sleep waits for a wakeup, the poll interval, the deadline or the context,
// and reports whether the caller should keep waiting.
func (d *A2ADoor) sleep(ctx context.Context, wake <-chan struct{}, deadline time.Time) bool {
	remaining := time.Until(deadline)
	if remaining <= 0 {
		return false
	}
	if remaining > a2aPollInterval {
		remaining = a2aPollInterval
	}
	timer := time.NewTimer(remaining)
	defer timer.Stop()
	select {
	case <-wake:
	case <-timer.C:
	case <-ctx.Done():
		return false
	}
	return time.Now().Before(deadline)
}

// awaitTurn waits for the gateway to say what it did with the message: a
// task started (returned as soon as its submission reached the bus, or as
// soon as the gateway declared it failed to), a drop for an unmapped
// caller, or a turn that ended with a reply and no task.
func (d *A2ADoor) awaitTurn(ctx context.Context, key string, prior a2aPrior, deadline time.Time) (string, *a2aMessageObject, *rpcError) {
	for {
		d.mu.Lock()
		conv := d.conversationLocked(key)
		if taskID, evicted := conv.tasks.at(prior.tasks); evicted {
			d.mu.Unlock()
			return "", nil, &rpcError{Code: rpcInternalError,
				Message: "the door evicted this turn's task id while the turn ran; poll tasks/get by the ids you hold"}
		} else if taskID != "" {
			task := d.tasks[taskID]
			if task != nil && (task.accepted || task.terminal) || conv.turns > prior.turns {
				d.mu.Unlock()
				return taskID, nil, nil
			}
		} else if conv.turns > prior.turns {
			if conv.drops > prior.drops {
				d.mu.Unlock()
				return "", nil, &rpcError{Code: a2aErrAuthenticationFail,
					Message: "the door's principal map does not carry this caller; nothing was started"}
			}
			if posts, evicted := conv.turnPosts.since(prior.posts); len(posts) > 0 {
				reply := d.messageObjectLocked(conv, posts, evicted)
				d.mu.Unlock()
				return "", reply, nil
			}
			d.mu.Unlock()
			return "", nil, &rpcError{Code: rpcInternalError,
				Message: "the turn ended with the gateway having posted nothing this door could see"}
		}
		wait := d.notify
		d.mu.Unlock()
		if !d.sleep(ctx, wait, deadline) {
			if ctx.Err() != nil {
				return "", nil, &rpcError{Code: rpcInternalError,
					Message: "the caller went away before the turn answered; the turn runs on"}
			}
			return "", nil, &rpcError{Code: rpcInternalError,
				Message: fmt.Sprintf("the gateway did nothing this door could see inside %s", a2aSubmitWait)}
		}
	}
}

// awaitCancel waits for the cancel to reach the bus, or for the turn to end
// without it.
func (d *A2ADoor) awaitCancel(ctx context.Context, key string, prior a2aPrior, deadline time.Time) (bool, string) {
	for {
		d.mu.Lock()
		conv := d.conversationLocked(key)
		if conv.cancels > prior.cancels {
			d.mu.Unlock()
			return true, ""
		}
		if conv.turns > prior.turns {
			note := "the turn ended without a cancel reaching the bus"
			if posts, evicted := conv.turnPosts.since(prior.posts); len(posts) > 0 {
				note += ": " + posts[len(posts)-1].text
				if evicted {
					note += " (earlier posts of this turn were evicted)"
				}
			}
			d.mu.Unlock()
			return false, note
		}
		wait := d.notify
		d.mu.Unlock()
		if !d.sleep(ctx, wait, deadline) {
			return false, "the gateway did not answer the cancel inside the bound"
		}
	}
}

// awaitTerminal waits for a task's terminal, bounded. A timeout is not an
// error: the caller gets the task as it stands and polls.
func (d *A2ADoor) awaitTerminal(ctx context.Context, taskID string, deadline time.Time) {
	for {
		d.mu.Lock()
		task := d.tasks[taskID]
		if task == nil || task.terminal {
			d.mu.Unlock()
			return
		}
		wait := d.notify
		d.mu.Unlock()
		if !d.sleep(ctx, wait, deadline) {
			return
		}
	}
}

// claimSubmission records a caller's message id before its turn runs; the
// second return is true when it has been seen, and the outcome is the first
// attempt's to wait on. Oldest-first eviction at a2aMaxSubmissions.
func (d *A2ADoor) claimSubmission(caller, messageID, key string) (*a2aOutcome, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	subKey := caller + "\x00" + messageID
	if o, ok := d.submissions[subKey]; ok {
		return o, true
	}
	o := &a2aOutcome{key: key, done: make(chan struct{})}
	d.submissions[subKey] = o
	d.subOrder = append(d.subOrder, subKey)
	for len(d.subOrder) > a2aMaxSubmissions {
		delete(d.submissions, d.subOrder[0])
		d.subOrder = d.subOrder[1:]
	}
	return o, false
}

// forgetSubmission drops a settled outcome that handed nothing over, so the
// id can be sent again. Only the outcome that was claimed is dropped: a
// later claim under the same id is someone else's.
func (d *A2ADoor) forgetSubmission(caller, messageID string, o *a2aOutcome) {
	d.mu.Lock()
	defer d.mu.Unlock()
	subKey := caller + "\x00" + messageID
	if d.submissions[subKey] == o {
		delete(d.submissions, subKey)
	}
}

// completeOutcome settles a submission once; later calls are no-ops, which
// is what lets send defer the abort case.
func (d *A2ADoor) completeOutcome(o *a2aOutcome, taskID string, message *a2aMessageObject, err *rpcError) {
	if o == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	select {
	case <-o.done:
		return
	default:
	}
	if message != nil {
		// A retry's answer is kept per named submission; bounded so the
		// submissions cap is a byte bound too.
		text := joinTextParts(message.Parts)
		if len(text) > a2aMaxParkedReplyBytes {
			bounded := *message
			bounded.Parts = textParts(truncateRunes(text, a2aMaxParkedReplyBytes))
			message = &bounded
		}
	}
	o.taskID, o.message, o.err = taskID, message, err
	close(o.done)
}

// activeTaskOf is the id of the task the relay is posting under on a
// conversation, or "" (none, terminal, or no longer held).
func (d *A2ADoor) activeTaskOf(key string) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversations[key]
	if conv == nil || conv.active == nil || conv.active.terminal || d.tasks[conv.active.id] != conv.active {
		return ""
	}
	return conv.active.id
}

// taskFor is the caller-scoped lookup.
func (d *A2ADoor) taskFor(taskID, caller string) (*a2aTask, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	task, ok := d.tasks[taskID]
	if !ok || task.caller != caller {
		return nil, false
	}
	snapshot := *task
	return &snapshot, true
}

// conversationLocked finds or mints a conversation. Caller holds d.mu.
func (d *A2ADoor) conversationLocked(key string) *a2aConversation {
	if conv, ok := d.conversations[key]; ok {
		return conv
	}
	if len(d.convOrder) >= a2aMaxConversations {
		// Evict the oldest IDLE conversation (liveLocked says which are
		// not). A live one evicted here would be re-minted with zeroed
		// counters on the next touch: a waiter that read its counts before
		// the eviction would classify from the new incarnation as though
		// nothing had moved, the one-turn guard would admit a second
		// message, and the relay's posts would find no task to attach to.
		// Live is bounded: a turn ends inside turnTimeout, and an active
		// task counts only until the gateway's task deadline, so the live
		// set is at most the arrival rate over one deadline, after which
		// the cap holds again.
		for i, candidate := range d.convOrder {
			old := d.conversations[candidate]
			if old != nil && d.liveLocked(old) {
				continue
			}
			d.convOrder = append(d.convOrder[:i], d.convOrder[i+1:]...)
			if old != nil && old.caller != "" && d.directOf[old.caller] == candidate {
				delete(d.directOf, old.caller)
			}
			delete(d.conversations, candidate)
			break
		}
	}
	conv := &a2aConversation{key: key}
	d.conversations[key] = conv
	d.convOrder = append(d.convOrder, key)
	return conv
}

// liveLocked reports whether a conversation is exempt from eviction: a turn
// in flight, or an active task the door still holds that is younger than
// the gateway's task deadline. A task the relay never terminated (an
// addressee with no executor) ages out of that exemption rather than
// pinning its conversation for the life of the process; one that fell off
// the task cap is let go at once. Caller holds d.mu.
func (d *A2ADoor) liveLocked(conv *a2aConversation) bool {
	if conv.busy {
		return true
	}
	task := conv.active
	if task == nil {
		return false
	}
	if d.tasks[task.id] != task || time.Since(task.created) > d.taskDeadline {
		conv.active = nil
		return false
	}
	return true
}

func (d *A2ADoor) noteDirectLocked(caller, key string) {
	if _, ok := d.directOf[caller]; !ok {
		d.directOrder = append(d.directOrder, caller)
	}
	d.directOf[caller] = key
	for len(d.directOrder) > injectMaxDirectAuthors {
		delete(d.directOf, d.directOrder[0])
		d.directOrder = d.directOrder[1:]
	}
}

func (d *A2ADoor) wakeLocked() {
	close(d.notify)
	d.notify = make(chan struct{})
}

func (d *A2ADoor) mintMessageIDLocked() string {
	d.nextID++
	return a2aMessageIDPrefix + strconv.Itoa(d.nextID)
}

// Post records a message the gateway sent. Under an active task the first
// post is the rolling line (startTask's placeholder) and the rest are the
// task's messages. Every post also goes on the turn in flight, which is
// what a turn that started no task answers with.
func (d *A2ADoor) Post(conversation, text string) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	id := d.mintMessageIDLocked()
	text = truncateRunes(text, injectMaxEntryBytes)
	post := a2aPost{id: id, text: text}
	if task := conv.active; task != nil {
		if task.lineID == "" {
			task.lineID, task.line = id, text
		} else {
			task.posts = append(task.posts, post)
			d.chargeLocked(task, len(text))
			if len(task.posts) > a2aMaxPostsPerTask {
				d.chargeLocked(task, -len(task.posts[0].text))
				task.posts = task.posts[1:]
			}
		}
		task.updated = time.Now()
		d.enforceBudgetLocked(task)
	}
	if conv.busy {
		conv.turnPosts.add(post)
	}
	d.wakeLocked()
	return id, nil
}

// Edit rewrites the rolling line. An A2A client reads a snapshot, so the
// edit lands in place; there is no delta reader to keep a sequence for.
func (d *A2ADoor) Edit(conversation, messageID, text string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	text = truncateRunes(text, injectMaxEntryBytes)
	if task := conv.active; task != nil && task.lineID == messageID {
		task.line = text
		if task.state == lib.StateSubmitted {
			task.state = lib.StateWorking
		}
		task.updated = time.Now()
		d.wakeLocked()
		return nil
	}
	// The terminal edit can land after TaskTerminal cleared active on a
	// path that orders them the other way; find the line by id.
	for i := len(conv.tasks.ids) - 1; i >= 0; i-- {
		if task := d.tasks[conv.tasks.ids[i]]; task != nil && task.lineID == messageID {
			task.line = text
			task.updated = time.Now()
			break
		}
	}
	d.wakeLocked()
	return nil
}

// Roster is the caller alone, complete: one participant per conversation,
// and no membership API behind the door to be incomplete about.
func (d *A2ADoor) Roster(conversation string) ([]string, bool, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv, ok := d.conversations[conversation]
	if !ok || conv.caller == "" {
		return nil, true, nil
	}
	return []string{conv.caller}, true, nil
}

// OpenDirect returns the conversation the caller last spoke on, for the
// reason the inject door's does: there is no second surface to move to.
func (d *A2ADoor) OpenDirect(userID string) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if key, ok := d.directOf[userID]; ok {
		return key, nil
	}
	return a2aConversationKey(userID, "direct"), nil
}

// TaskStarted mints the task record. Announced by startTask before the
// placeholder post, so the first post under it is the rolling line.
func (d *A2ADoor) TaskStarted(conversation, taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	now := time.Now()
	task := &a2aTask{
		id: taskID, contextID: conv.contextID, key: conversation, caller: conv.caller,
		user: conv.pending, state: lib.StateSubmitted, created: now, updated: now,
	}
	task.user.TaskID = taskID
	if prev, exists := d.tasks[taskID]; exists {
		d.retainedBytes -= prev.bytes
	} else {
		d.taskOrder = append(d.taskOrder, taskID)
		for len(d.taskOrder) > a2aMaxTasks {
			d.evictOldestTaskLocked(task)
		}
	}
	d.tasks[taskID] = task
	d.chargeLocked(task, len(joinTextParts(task.user.Parts)))
	conv.active = task
	conv.tasks.add(taskID)
	d.enforceBudgetLocked(task)
	d.wakeLocked()
}

// TaskAccepted records that the submission reached the bus, which is what
// message/send returns on.
func (d *A2ADoor) TaskAccepted(conversation, taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if task, ok := d.tasks[taskID]; ok {
		task.accepted = true
		task.updated = time.Now()
	}
	d.wakeLocked()
}

// TaskTerminal records the terminal. It lands after the relay has posted the
// deliverable and edited the line, so a snapshot taken on it is complete.
func (d *A2ADoor) TaskTerminal(conversation, taskID string, state lib.TaskState, source TerminalSource, reason string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	task, ok := d.tasks[taskID]
	if !ok {
		// A task the door did not see start: a restart between the two
		// ends. Recorded so the line edit and the conversation's log have
		// somewhere to land; no caller can read it (the conversation knows
		// no caller after a restart), which is the restart gap the read
		// through the gateway's probe will close.
		task = &a2aTask{id: taskID, key: conversation, contextID: conv.contextID, caller: conv.caller, created: time.Now()}
		d.tasks[taskID] = task
		d.taskOrder = append(d.taskOrder, taskID)
	}
	task.state = state
	task.terminal = true
	task.source = source
	task.reason = truncateRunes(reason, injectMaxEntryBytes)
	task.updated = time.Now()
	if conv.active == task {
		conv.active = nil
	}
	d.wakeLocked()
}

// TaskDelivered records the deliverable the relay hands over whole before
// posting it in chunks (DeliverableObserver). It is the task's one artifact;
// the posts stay what they are, the task's history.
func (d *A2ADoor) TaskDelivered(conversation, taskID, result string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	task, ok := d.tasks[taskID]
	if !ok {
		conv := d.conversationLocked(conversation)
		task = &a2aTask{id: taskID, key: conversation, contextID: conv.contextID, caller: conv.caller, created: time.Now()}
		d.tasks[taskID] = task
		d.taskOrder = append(d.taskOrder, taskID)
	}
	if len(result) > a2aMaxResultBytes {
		task.resultCutFrom = len(result)
		result = truncateRunes(result, a2aMaxResultBytes)
	}
	d.chargeLocked(task, len(result)-len(task.result))
	task.result = result
	task.delivered = true
	task.updated = time.Now()
	d.enforceBudgetLocked(task)
	d.wakeLocked()
}

// chargeLocked moves a task's retained bytes by delta. Caller holds d.mu.
func (d *A2ADoor) chargeLocked(task *a2aTask, delta int) {
	task.bytes += delta
	d.retainedBytes += delta
}

// enforceBudgetLocked evicts the oldest tasks whole while the retained
// bytes exceed the budget, never the one just touched. Caller holds d.mu.
func (d *A2ADoor) enforceBudgetLocked(keep *a2aTask) {
	for d.retainedBytes > a2aRetainedBudgetBytes && len(d.taskOrder) > 1 {
		if d.tasks[d.taskOrder[0]] == keep {
			return
		}
		d.evictOldestTaskLocked(keep)
	}
}

// evictOldestTaskLocked drops the oldest task from the door's memory and
// its bytes from the budget. Caller holds d.mu.
func (d *A2ADoor) evictOldestTaskLocked(keep *a2aTask) {
	if len(d.taskOrder) == 0 {
		return
	}
	oldest := d.taskOrder[0]
	if old := d.tasks[oldest]; old != nil && old != keep {
		d.retainedBytes -= old.bytes
		delete(d.tasks, oldest)
	}
	d.taskOrder = d.taskOrder[1:]
}

// CancelPublished records that a cancel reached the bus.
func (d *A2ADoor) CancelPublished(conversation, taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.conversationLocked(conversation).cancels++
	if task, ok := d.tasks[taskID]; ok {
		task.cancelPublished = true
	}
	d.wakeLocked()
}

// MessageDropped records a drop for a caller the map does not carry.
func (d *A2ADoor) MessageDropped(conversation, authorID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.conversationLocked(conversation).drops++
	d.log.Warn("a2a: the gateway dropped a message from a caller the door's principal map does not carry",
		"conversation", conversation, "caller", authorID)
	d.wakeLocked()
}

// TurnFinished records the end of a turn and frees the slot the claim took:
// the gateway's deferred observeTurnFinished is the one place the turn is
// known to be over, whichever way it went.
func (d *A2ADoor) TurnFinished(conversation string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	conv.turns++
	conv.busy = false
	conv.pending = lib.Message{}
	d.wakeLocked()
}

// taskObject renders a task as the protocol's Task.
func (d *A2ADoor) taskObject(taskID string) a2aTaskObject {
	d.mu.Lock()
	defer d.mu.Unlock()
	task, ok := d.tasks[taskID]
	if !ok {
		return a2aTaskObject{ID: taskID, Kind: a2aKindTask, Status: a2aTaskStatus{State: "unknown"}}
	}
	return d.taskObjectLocked(task)
}

func (d *A2ADoor) taskObjectLocked(task *a2aTask) a2aTaskObject {
	state := task.state
	if state == "" {
		state = lib.StateSubmitted
	}
	status := a2aTaskStatus{State: state, TS: task.updated.UTC().Format(time.RFC3339Nano)}
	line := task.line
	if task.terminal && task.reason != "" {
		line = task.reason
	}
	if line != "" {
		status.Message = &a2aMessage{Message: lib.Message{Role: a2aRoleAgent, Parts: textParts(line), MessageID: task.lineID, TaskID: task.id, ContextID: task.contextID}, Kind: a2aKindMessage}
	}
	history := make([]a2aMessage, 0, 1+len(task.posts))
	if task.user.MessageID != "" {
		history = append(history, a2aMessage{Message: task.user, Kind: a2aKindMessage})
	}
	for _, p := range task.posts {
		history = append(history, a2aMessage{Message: lib.Message{Role: a2aRoleAgent, Parts: textParts(p.text), MessageID: p.id, TaskID: task.id, ContextID: task.contextID}, Kind: a2aKindMessage})
	}
	var artifacts []lib.Artifact
	if state == lib.StateCompleted && task.delivered {
		// The deliverable the relay handed over whole (TaskDelivered); the
		// posts are the same text in chat-sized chunks and stay history.
		// One artifact, named as the bus names it. A completed task the
		// relay handed nothing for (a non-text result, a heal with no
		// artifact on the stream) renders no artifact rather than a guess.
		artifacts = []lib.Artifact{{ArtifactID: task.id + "-result", Name: lib.ArtifactResult, Parts: textParts(task.result)}}
	}
	metadata := map[string]any{"backend": a2aBackendForCaller(task.caller)}
	if task.resultCutFrom > 0 {
		metadata["resultTruncatedFrom"] = task.resultCutFrom
	}
	if task.terminal {
		metadata["terminalSource"] = string(task.source)
	}
	return a2aTaskObject{
		ID: task.id, ContextID: task.contextID, Status: status,
		Artifacts: artifacts, History: history, Metadata: metadata, Kind: a2aKindTask,
	}
}

// messageObjectLocked renders a turn's posts as one agent Message. Caller
// holds d.mu.
func (d *A2ADoor) messageObjectLocked(conv *a2aConversation, posts []a2aPost, evicted bool) *a2aMessageObject {
	texts := make([]string, 0, len(posts))
	for _, p := range posts {
		texts = append(texts, p.text)
	}
	id := ""
	if len(posts) > 0 {
		id = posts[len(posts)-1].id
	}
	reply := &a2aMessageObject{
		Message: lib.Message{Role: a2aRoleAgent, Parts: textParts(strings.Join(texts, "\n")), MessageID: id, ContextID: conv.contextID},
		Kind:    a2aKindMessage,
	}
	if evicted {
		// The head of this turn's posts fell off the log; say so rather
		// than return a tail as the whole, as the artifact cut is marked.
		reply.Metadata = map[string]any{"postsEvicted": true}
	}
	return reply
}
