package gateway

import (
	"context"
	"crypto/subtle"
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
// one-chokepoint rule the design track set (round 3, external agents): the
// door must deliver into handleInbound and nowhere else.
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
	// namespace is refused rather than honoured. When the developer
	// identity class lands (ID tokens), it arrives as a second resolver
	// beside this one, not as a loosening of it.
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

	// Bounds on what the door remembers. Tasks and conversations evict
	// oldest-first; a client that wants a task after eviction is told it is
	// not found, which is the protocol's own answer for a task the server
	// no longer holds.
	a2aMaxTasks         = 4096
	a2aMaxConversations = 1024
	a2aMaxPostsPerTask  = 256
	a2aMaxLoosePosts    = 64
	a2aMaxSubmissions   = injectSeenCap

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
	lineID  string
	line    string
	posts   []a2aPost
	created time.Time
	updated time.Time
}

// a2aConversation is one caller's context: the turn accounting message/send
// needs, and the posts that arrived with no task active (a reply, a refusal,
// a status answer), which are the Message a taskless turn returns.
type a2aConversation struct {
	key       string
	caller    string
	contextID string
	// busy is the one-turn-at-a-time guard: the counters below are per
	// conversation, so a second submission waits for the first turn to end
	// before it can read them.
	busy    bool
	turns   int
	drops   int
	cancels int
	// active is the task the relay is posting under, set at TaskStarted and
	// cleared at TaskTerminal. tasks is every task id in order.
	active *a2aTask
	tasks  []string
	loose  []a2aPost
	// pending is the inbound message of the turn in flight, which
	// TaskStarted copies onto the task it mints.
	pending lib.Message
}

// a2aPrior is a conversation's counters read before a turn is handed over,
// so the wait for its answer cannot mistake an earlier turn's for it.
type a2aPrior struct {
	turns, drops, cancels, tasks, loose int
}

// a2aOutcome is what a submission resolved to, kept by caller and message id
// so a retry of the same message/send is answered with the same task rather
// than routed again.
type a2aOutcome struct {
	taskID  string
	message *a2aMessageObject
	err     *rpcError
	done    chan struct{}
}

// A2ADoor is the adapter. One instance serves one listener.
type A2ADoor struct {
	listen           string
	token            string
	publicURL        string
	agentName        string
	agentVersion     string
	defaultAddressee string
	firstEventGrace  time.Duration
	log              *slog.Logger

	mu            sync.Mutex
	conversations map[string]*a2aConversation
	convOrder     []string
	tasks         map[string]*a2aTask
	taskOrder     []string
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
	probe     ConversationProbe

	// listener, when set, is an already-bound listener Run serves on. Test
	// injection only.
	listener net.Listener
}

// A2ADoorOptions is what NewA2ADoor needs beyond the listen address and the
// token.
type A2ADoorOptions struct {
	// PublicURL is the URL the agent card advertises for the JSON-RPC
	// endpoint: what a client reaches this door at, which behind a
	// port-forward or an ingress is not the listen address. Empty renders
	// http://<listen>/a2a.
	PublicURL string
	// DefaultAddressee is the gateway's default destination, which is the
	// one skill the card lists until profiles supply a catalog.
	DefaultAddressee string
	// AgentName and AgentVersion are the card's; empty takes defaults.
	AgentName    string
	AgentVersion string
	// FirstEventGrace is the gateway's, reported to callers.
	FirstEventGrace time.Duration
	Logger          *slog.Logger
}

// NewA2ADoor builds the door. The token is required here as well as in
// FromEnv, for the reason NewInjectAdapter gives: a door that could be built
// without one would make "unauthenticated" a thing a caller can choose.
func NewA2ADoor(listen, token string, o A2ADoorOptions) (*A2ADoor, error) {
	if strings.TrimSpace(listen) == "" {
		return nil, fmt.Errorf("the A2A door needs a listen address")
	}
	if strings.TrimSpace(token) == "" {
		return nil, fmt.Errorf("the A2A door needs a bearer token: it authenticates every request, and the NetworkPolicy in front of it does not govern the port-forward path")
	}
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	publicURL := strings.TrimSpace(o.PublicURL)
	if publicURL == "" {
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
	return &A2ADoor{
		listen:           listen,
		token:            token,
		publicURL:        publicURL,
		agentName:        name,
		agentVersion:     version,
		defaultAddressee: o.DefaultAddressee,
		firstEventGrace:  o.FirstEventGrace,
		log:              log,
		conversations:    map[string]*a2aConversation{},
		tasks:            map[string]*a2aTask{},
		submissions:      map[string]*a2aOutcome{},
		directOf:         map[string]string{},
		notify:           make(chan struct{}),
	}, nil
}

// SetProbe receives the gateway's ConversationProbe (ProbeSink). Held for
// the read side; tasks/get answers from the door's own record today and the
// probe is what a later change reads when that record is gone.
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
		"Dev and eval installs only until the identity classes land.",
		"address", ln.Addr().String(), "card", d.publicURL)

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

// authorized is the inject door's check, verbatim in behaviour: constant
// time on the credential, case-insensitive on the scheme, and a refusal that
// says a token is required and nothing about what was presented.
func (d *A2ADoor) authorized(w http.ResponseWriter, r *http.Request) bool {
	header := r.Header.Get(authorizationHeader)
	if len(header) > len(bearerScheme) && strings.EqualFold(header[:len(bearerScheme)], bearerScheme) {
		presented := strings.TrimSpace(header[len(bearerScheme):])
		if subtle.ConstantTimeCompare([]byte(presented), []byte(d.token)) == 1 {
			return true
		}
	}
	w.Header().Set("WWW-Authenticate", "Bearer")
	injectError(w, http.StatusUnauthorized, "the A2A door requires a bearer token")
	return false
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
	writeJSON(w, http.StatusOK, d.card())
}

// card is the catalog. One skill per destination this door routes to, which
// today is the gateway's default addressee; when profiles land the list is
// rendered from DIRECTORY and the caller's entitlements, and the card
// becomes per caller.
func (d *A2ADoor) card() a2aAgentCard {
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
		URL:             d.publicURL,
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
		SecuritySchemes:    map[string]a2aScheme{"bearer": {Type: "http", Scheme: "bearer"}},
		Security:           []map[string][]string{{"bearer": {}}},
	}
}

// handleRPC is the JSON-RPC endpoint. Auth first, before the method and the
// body, as on the inject door. Protocol-level failures are JSON-RPC errors
// on a 200, which is the binding's convention; only the transport-level
// refusals (no token, wrong method, oversize body) are HTTP statuses.
func (d *A2ADoor) handleRPC(w http.ResponseWriter, r *http.Request) {
	if !d.authorized(w, r) {
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
		resp = d.send(r, handler, req)
	case a2aMethodGet:
		resp = d.get(r, req)
	case a2aMethodCancel:
		resp = d.cancel(r, handler, req)
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
func (d *A2ADoor) send(r *http.Request, handler func(InboundMessage), req rpcRequest) rpcResponse {
	var params a2aSendParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := callerOf(r, params.Message.Metadata)
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
		if contextID != "" && contextID != task.contextID {
			return rpcFail(req.ID, rpcInvalidParams, "message.contextId does not match the task's", nil)
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
		outcome, seen = d.claimSubmission(caller, messageID)
		if seen {
			return d.answerOutcome(turnCtx, req.ID, outcome, blocking)
		}
		turnCtx = context.WithoutCancel(r.Context())
		defer d.completeOutcome(outcome, "", nil,
			&rpcError{Code: rpcInternalError, Message: "the first message/send with this messageId aborted before its turn answered"})
	}

	user := lib.Message{Role: a2aRoleUser, Parts: msg.Parts, MessageID: messageID, ContextID: contextID, TaskID: msg.TaskID}
	deadline := time.Now().Add(a2aSubmitWait)
	prior, ok := d.claimTurn(turnCtx, key, caller, contextID, user, deadline)
	if !ok {
		rerr := &rpcError{Code: rpcInternalError, Message: earlierTurnNote()}
		d.completeOutcome(outcome, "", nil, rerr)
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	handler(InboundMessage{
		Conversation: key,
		Kind:         a2aConversationKind,
		AuthorID:     caller,
		MessageID:    messageID,
		Text:         text,
		Backend:      a2aBackend,
		TaskID:       msg.TaskID,
	})
	taskID, reply, rerr := d.awaitTurn(turnCtx, key, prior, deadline)
	d.releaseTurn(key)
	d.completeOutcome(outcome, taskID, reply, rerr)
	if rerr != nil {
		d.log.Info("a2a: the door started nothing for a message/send",
			"conversation", key, "messageId", messageID, "code", rerr.Code, "note", rerr.Message)
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	if taskID == "" {
		return rpcOK(req.ID, reply)
	}
	if blocking {
		d.awaitTerminal(turnCtx, taskID, time.Now().Add(a2aBlockingWait))
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
func (d *A2ADoor) get(r *http.Request, req rpcRequest) rpcResponse {
	var params a2aIDParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := callerOf(r, nil)
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
func (d *A2ADoor) cancel(r *http.Request, handler func(InboundMessage), req rpcRequest) rpcResponse {
	var params a2aIDParams
	if err := json.Unmarshal(req.Params, &params); err != nil {
		return rpcFail(req.ID, rpcInvalidParams, "params: "+err.Error(), nil)
	}
	caller, rerr := callerOf(r, nil)
	if rerr != nil {
		return rpcFail(req.ID, rerr.Code, rerr.Message, nil)
	}
	task, ok := d.taskFor(params.ID, caller)
	if !ok {
		return rpcFail(req.ID, a2aErrTaskNotFound, "no such task for this caller: "+params.ID, nil)
	}
	if task.terminal {
		return rpcFail(req.ID, a2aErrTaskNotCancelable, "task "+params.ID+" is already "+string(task.state), nil)
	}
	key, contextID := task.key, task.contextID
	messageID := a2aInboundIDPrefix + randHex(messageIDHexWidth)
	user := lib.Message{Role: a2aRoleUser, Parts: textParts("stop"), MessageID: messageID, ContextID: contextID, TaskID: params.ID}
	deadline := time.Now().Add(a2aSubmitWait)
	prior, ok := d.claimTurn(r.Context(), key, caller, contextID, user, deadline)
	if !ok {
		return rpcFail(req.ID, rpcInternalError, earlierTurnNote(), nil)
	}
	handler(InboundMessage{
		Conversation: key,
		Kind:         a2aConversationKind,
		AuthorID:     caller,
		MessageID:    messageID,
		Text:         "stop",
		Backend:      a2aBackend,
		Intent:       IntentCancel,
		TaskID:       params.ID,
	})
	published, note := d.awaitCancel(r.Context(), key, prior, deadline)
	d.releaseTurn(key)
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
			conv.busy = true
			conv.pending = user
			prior := a2aPrior{turns: conv.turns, drops: conv.drops, cancels: conv.cancels, tasks: len(conv.tasks), loose: len(conv.loose)}
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

func (d *A2ADoor) releaseTurn(key string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if conv, ok := d.conversations[key]; ok {
		conv.busy = false
		conv.pending = lib.Message{}
	}
	d.wakeLocked()
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
		if len(conv.tasks) > prior.tasks {
			taskID := conv.tasks[prior.tasks]
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
			if len(conv.loose) > prior.loose {
				reply := d.messageObjectLocked(conv, conv.loose[prior.loose:])
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
			if len(conv.loose) > prior.loose {
				note += ": " + conv.loose[len(conv.loose)-1].text
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
func (d *A2ADoor) claimSubmission(caller, messageID string) (*a2aOutcome, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	subKey := caller + "\x00" + messageID
	if o, ok := d.submissions[subKey]; ok {
		return o, true
	}
	o := &a2aOutcome{done: make(chan struct{})}
	d.submissions[subKey] = o
	d.subOrder = append(d.subOrder, subKey)
	for len(d.subOrder) > a2aMaxSubmissions {
		delete(d.submissions, d.subOrder[0])
		d.subOrder = d.subOrder[1:]
	}
	return o, false
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
	o.taskID, o.message, o.err = taskID, message, err
	close(o.done)
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
		oldest := d.convOrder[0]
		d.convOrder = d.convOrder[1:]
		if old, ok := d.conversations[oldest]; ok && old.caller != "" && d.directOf[old.caller] == oldest {
			delete(d.directOf, old.caller)
		}
		delete(d.conversations, oldest)
	}
	conv := &a2aConversation{key: key}
	d.conversations[key] = conv
	d.convOrder = append(d.convOrder, key)
	return conv
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
// task's messages; with no task active it is a loose reply on the
// conversation.
func (d *A2ADoor) Post(conversation, text string) (string, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	conv := d.conversationLocked(conversation)
	id := d.mintMessageIDLocked()
	text = truncateRunes(text, injectMaxEntryBytes)
	if task := conv.active; task != nil {
		if task.lineID == "" {
			task.lineID, task.line = id, text
		} else {
			task.posts = append(task.posts, a2aPost{id: id, text: text})
			if len(task.posts) > a2aMaxPostsPerTask {
				task.posts = task.posts[1:]
			}
		}
		task.updated = time.Now()
	} else {
		conv.loose = append(conv.loose, a2aPost{id: id, text: text})
		if len(conv.loose) > a2aMaxLoosePosts {
			conv.loose = conv.loose[1:]
		}
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
	for i := len(conv.tasks) - 1; i >= 0; i-- {
		if task := d.tasks[conv.tasks[i]]; task != nil && task.lineID == messageID {
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
	if _, exists := d.tasks[taskID]; !exists {
		d.taskOrder = append(d.taskOrder, taskID)
		for len(d.taskOrder) > a2aMaxTasks {
			delete(d.tasks, d.taskOrder[0])
			d.taskOrder = d.taskOrder[1:]
		}
	}
	d.tasks[taskID] = task
	conv.active = task
	conv.tasks = append(conv.tasks, taskID)
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
		// ends. Record what is known so tasks/get on it says something.
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

// CancelPublished records that a cancel reached the bus.
func (d *A2ADoor) CancelPublished(conversation, taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.conversationLocked(conversation).cancels++
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

// TurnFinished records the end of a turn.
func (d *A2ADoor) TurnFinished(conversation string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.conversationLocked(conversation).turns++
	d.wakeLocked()
}

// taskObject renders a task as the protocol's Task.
func (d *A2ADoor) taskObject(taskID string) a2aTaskObject {
	d.mu.Lock()
	defer d.mu.Unlock()
	task, ok := d.tasks[taskID]
	if !ok {
		return a2aTaskObject{ID: taskID, Kind: a2aKindTask, Status: lib.TaskStatus{State: "unknown"}}
	}
	return d.taskObjectLocked(task)
}

func (d *A2ADoor) taskObjectLocked(task *a2aTask) a2aTaskObject {
	state := task.state
	if state == "" {
		state = lib.StateSubmitted
	}
	status := lib.TaskStatus{State: state, TS: task.updated.UTC().Format(time.RFC3339Nano)}
	line := task.line
	if task.terminal && task.reason != "" {
		line = task.reason
	}
	if line != "" {
		status.Message = &lib.Message{Role: a2aRoleAgent, Parts: textParts(line), MessageID: task.lineID, TaskID: task.id, ContextID: task.contextID}
	}
	history := make([]lib.Message, 0, 1+len(task.posts))
	if task.user.MessageID != "" {
		history = append(history, task.user)
	}
	for _, p := range task.posts {
		history = append(history, lib.Message{Role: a2aRoleAgent, Parts: textParts(p.text), MessageID: p.id, TaskID: task.id, ContextID: task.contextID})
	}
	var artifacts []lib.Artifact
	if state == lib.StateCompleted && len(task.posts) > 0 {
		// The deliverable is what the relay posted last before the
		// terminal edit; earlier posts under the task are notices. One
		// artifact, named as the bus names it.
		last := task.posts[len(task.posts)-1]
		artifacts = []lib.Artifact{{ArtifactID: task.id + "-result", Name: lib.ArtifactResult, Parts: textParts(last.text)}}
	}
	metadata := map[string]any{"backend": a2aBackend}
	if task.terminal {
		metadata["terminalSource"] = string(task.source)
	}
	return a2aTaskObject{
		ID: task.id, ContextID: task.contextID, Status: status,
		Artifacts: artifacts, History: history, Metadata: metadata, Kind: a2aKindTask,
	}
}

// messageObjectLocked renders loose posts as one agent Message. Caller holds
// d.mu.
func (d *A2ADoor) messageObjectLocked(conv *a2aConversation, posts []a2aPost) *a2aMessageObject {
	texts := make([]string, 0, len(posts))
	for _, p := range posts {
		texts = append(texts, p.text)
	}
	id := ""
	if len(posts) > 0 {
		id = posts[len(posts)-1].id
	}
	return &a2aMessageObject{
		Role: a2aRoleAgent, Parts: textParts(strings.Join(texts, "\n")), MessageID: id,
		ContextID: conv.contextID, Kind: a2aKindMessage,
	}
}
