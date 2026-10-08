package hermesbridge

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The API executor: a task is one turn in the conversation's Hermes session.
//
// Instead of a cold `hermes chat -Q` per task, the bridge POSTs the task's
// text to the Hermes API server in the same pod, which runs it under the
// gateway's own profile (default on a stock install), the persona that answers the same message on
// the chat path, delegating through kanban. Two headers make the turn part
// of a conversation rather than a one-off: X-Hermes-Session-Key, the
// long-term-memory scope, and X-Hermes-Session-Id, the session whose history
// the server loads from its own store before the turn and appends to after.
// Both are derived from the task's contextId, which the gateway mints once
// per backend conversation, so every task in a thread lands in one session
// and the second task sees the first's turns. Nothing in Hermes changes.
//
// The subprocess executor stays as a fallback (Config.Executor); what this
// one does not do, by design of the stopgap it is: steer a running turn (the
// fixed route keeps refusing steers). A kanban card's completion never
// reaches the A2A task (the API server has no push channel); it goes back to
// the conversation through the gateway's chat.notify route instead, on the
// route this executor records before each turn (route.go). Both are named in
// a2a/docs/hermes-bridge.md.
const (
	// ExecutorAPI runs a task as a turn in the conversation's Hermes session
	// through the pod's API server. The daemon's default.
	ExecutorAPI = "api"
	// ExecutorCLI runs a task as its own `hermes chat -Q` subprocess, the
	// original executor and Config's zero value; a fresh session per task.
	ExecutorCLI = "cli"
	// DefaultAPIURL is the Hermes API server's chat-completions endpoint in
	// the pod: loopback, the port hermes's api_server binds by default.
	DefaultAPIURL = "http://127.0.0.1:8642/v1/chat/completions"
	// DefaultAPIModel is the model name a request carries. The pod's API
	// server serves one model under its API_SERVER_MODEL_NAME, the inference
	// gateway's; the name routes, it does not pick.
	DefaultAPIModel = "model-default"

	// apiSessionIDPrefix starts the session id derived from a contextId, so
	// the sessions the bridge opened are told apart in the profile's store
	// from the chat platforms' and the CLI's.
	apiSessionIDPrefix = "a2a-"
	// apiHashedSessionPrefix follows apiSessionIDPrefix when the contextId
	// is not usable verbatim (apiSessionID). It marks the id as hashed for a
	// reader; it does not partition the namespace, since a contextId of
	// "h-" plus 32 hex is itself verbatim-safe. Nothing rests on it: a
	// sender who can name a session's contextId can already send into it.
	apiHashedSessionPrefix = "h-"
	// apiContextIDMaxLen is the longest contextId used verbatim. The
	// gateway's are "ctx-" plus 32 hex; the server caps the header too.
	apiContextIDMaxLen = 128
	// apiHashedSessionHexLen is how much of the SHA-256 a hashed id keeps:
	// 128 bits, past any collision two conversations could stumble into.
	apiHashedSessionHexLen = 32
	// apiSessionKeyHeader and apiSessionIDHeader are the API server's two
	// session headers (gateway/platforms/api_server.py); both are echoed on
	// the response, and both require the server's API key to be configured.
	apiSessionKeyHeader = "X-Hermes-Session-Key"
	apiSessionIDHeader  = "X-Hermes-Session-Id"
	// apiIdempotencyHeader is the server's replay guard: a request retried
	// with the same key returns the first run's answer instead of running
	// the turn again. The task id is the key, so a bridge restart that
	// redelivers a task does not run it twice in the session.
	apiIdempotencyHeader = "Idempotency-Key"
	// apiRateLimitedStatus is the status the server answers when it is
	// already running its cap of concurrent turns
	// (gateway.api_server.max_concurrent_runs), before the turn starts. It
	// maps to the reason token of the CLI's EX_TEMPFAIL exit, so the eval
	// harness classes it as infrastructure. A provider's own rate limit is
	// not this: the turn runs and fails, and is read from
	// apiFailureReasonHeader.
	apiRateLimitedStatus = http.StatusTooManyRequests
	// apiFailureReasonHeader names the failure_reason Hermes classified a
	// failed turn under; the image's api_failure_reason_header patch adds it
	// to both the 200 and the 502 answer. apiRateLimitedReasons are the
	// reasons the CLI exits 75 on, so a turn that gave up on the provider is
	// hermes-rate-limited on either executor.
	apiFailureReasonHeader = "X-Hermes-Failure-Reason"
	// apiBodyTailBytes bounds the error body quoted in a failed terminal.
	apiBodyTailBytes = 2048
	// apiResponseCap bounds a successful response body read: an answer is
	// text, and anything past this is not one. The read takes one byte more,
	// so a body over the cap is refused as hermes-api-oversize rather than
	// cut at the cap and misread as hermes-api-unreadable.
	apiResponseCap = 8 << 20
	// apiURLSchemeHTTP and apiURLSchemeHTTPS are the schemes the API URL
	// may carry; the client cannot send to anything else.
	apiURLSchemeHTTP  = "http"
	apiURLSchemeHTTPS = "https"
	// DefaultAPIConnectRetry and apiConnectRetryInterval pace the retry of a
	// refused connection: the sidecar and the agent container start
	// together, and the bridge can be consuming before hermes's API server
	// listens. A refused connection never reached the server, so retrying it
	// cannot run a turn twice. Past the window the server is not starting,
	// it is absent, and the task ends hermes-api-unreachable.
	DefaultAPIConnectRetry  = 2 * time.Minute
	apiConnectRetryInterval = time.Second
)

// errWaitingForTurn is the error a task whose context ended while it waited
// for its session's previous turn carries into finalizeAPIError.
var errWaitingForTurn = errors.New("waiting for the session's previous turn")

// errNeverConnected is what sendAPI wraps around an error when no attempt
// got a connection: no request reached the server, so a deadline that ends
// it is not the turn's.
var errNeverConnected = errors.New("no connection to the hermes API server")

// apiRateLimitedReasons is the set apiFailureReasonHeader is checked against:
// the reasons apply_quiet_rate_limit_exit.py exits 75 on.
var apiRateLimitedReasons = map[string]bool{"rate_limit": true, "billing": true}

// apiSafeContextID is what a contextId may hold to be used verbatim in a
// session id: the server interpolates the id into a filename and refuses a
// separator or "..", and the bus does not vouch for a contextId's shape.
var apiSafeContextID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// apiSessionID is the Hermes session a conversation's tasks share: the
// contextId under apiSessionIDPrefix when it is path-safe, else a hash of
// it, so a hostile or odd contextId still maps to one stable session and
// never to a path. Every task has a contextId: the envelope refuses one
// without.
func apiSessionID(contextID string) string {
	if len(contextID) <= apiContextIDMaxLen && apiSafeContextID.MatchString(contextID) {
		return apiSessionIDPrefix + contextID
	}
	sum := sha256.Sum256([]byte(contextID))
	return apiSessionIDPrefix + apiHashedSessionPrefix + hex.EncodeToString(sum[:])[:apiHashedSessionHexLen]
}

// sessionTurns serializes turns per Hermes session. Two tasks in one
// conversation can be in flight at once (Concurrency > 1), and two
// concurrent turns in one session would each load the history without the
// other's turn and append in a race; the second waits for the first, as a
// second chat message waits in a chat platform's session.
type sessionTurns struct {
	mu    sync.Mutex
	slots map[string]*sessionSlot
}

type sessionSlot struct {
	ch   chan struct{} // capacity 1: held while a turn is in the session
	refs int           // tasks holding or waiting on ch
}

// acquire waits for the session's turn or ctx's end. The release it returns
// is non-nil exactly when the turn was taken.
func (t *sessionTurns) acquire(ctx context.Context, sessionID string) func() {
	t.mu.Lock()
	if t.slots == nil {
		t.slots = make(map[string]*sessionSlot)
	}
	s := t.slots[sessionID]
	if s == nil {
		s = &sessionSlot{ch: make(chan struct{}, 1)}
		t.slots[sessionID] = s
	}
	s.refs++
	t.mu.Unlock()
	drop := func() {
		t.mu.Lock()
		s.refs--
		if s.refs == 0 {
			delete(t.slots, sessionID)
		}
		t.mu.Unlock()
	}
	select {
	case s.ch <- struct{}{}:
		return func() {
			<-s.ch
			drop()
		}
	case <-ctx.Done():
		drop()
		return nil
	}
}

// apiChatRequest is the chat-completions request body, the subset the
// server reads for a plain turn.
type apiChatRequest struct {
	Model    string           `json:"model"`
	Messages []apiChatMessage `json:"messages"`
	Stream   bool             `json:"stream"`
}

type apiChatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// apiChatResponse is the subset of the chat-completions response the bridge
// reads: the first choice's text, and the server's own verdict on the turn.
// A turn that failed with no text comes back as an OpenAI error envelope
// with a non-2xx status, read as text; one that failed with text (a
// provider error, a rate limit the retries did not outlast, the failure
// summary Hermes writes for either) comes back 200 with Hermes.Failed set.
type apiChatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Hermes *struct {
		Failed bool   `json:"failed"`
		Error  string `json:"error"`
	} `json:"hermes"`
}

// apiTurnFailed reports whether the server marked the turn failed, the case
// in which `hermes chat -Q` exits non-zero; a partial or truncated turn that
// did not fail completes, as it does on the subprocess path.
func apiTurnFailed(out *apiChatResponse) bool {
	return out.Hermes != nil && out.Hermes.Failed
}

// runTaskAPI is runTask for ExecutorAPI. The lifecycle is the subprocess
// path's: working when the turn starts, one terminal from this goroutine,
// cancel and the deadline end the request rather than a process group, and
// shutdown names itself.
func (b *Bridge) runTaskAPI(ctx context.Context, run *taskRun) {
	taskID := run.origin.TaskID
	prompt, ok := promptFromMessage(run.origin.Payload)
	if !ok {
		b.finalize(run, lib.StateRejected,
			"reason: no-text-parts - the submission message carries nothing hermes can be asked", nil)
		return
	}
	sessionID := apiSessionID(run.origin.ContextID)
	body, err := json.Marshal(apiChatRequest{
		Model:    b.cfg.APIModel,
		Messages: []apiChatMessage{{Role: "user", Content: prompt}},
	})
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: request-encode-failed - %v", err), nil)
		return
	}
	// The request's own context: the task deadline bounds it, cancel and
	// shutdown end it, and it is not the consumer's context, which a
	// shutdown cancels before shutdownTasks can name the cause.
	reqCtx, cancelReq := context.WithTimeout(context.Background(), b.cfg.TaskDeadline)
	defer cancelReq()
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, b.cfg.APIURL, bytes.NewReader(body))
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: request-build-failed - %v", err), nil)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.APIKey)
	req.Header.Set(apiSessionKeyHeader, sessionID)
	req.Header.Set(apiSessionIDHeader, sessionID)
	req.Header.Set(apiIdempotencyHeader, taskID)

	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return
	}
	run.cancelReq = cancelReq
	if run.canceled.Load() {
		cancelReq()
	}
	run.mu.Unlock()

	// A task behind its session's previous turn has not started: it stays
	// submitted, with no heartbeat, until the turn is its own, so nothing
	// reads a wait as a run.
	release := b.turns.acquire(reqCtx, sessionID)
	if release == nil {
		b.finalizeAPIError(run, reqCtx, errWaitingForTurn)
		return
	}
	defer release()
	if err := run.exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}

	// Recorded before the turn, so a card the turn files finds it. Best
	// effort: the turn runs either way, and a failure is said in the answer.
	routeLost := false
	if err := b.recordRoute(reqCtx, sessionID, run.origin); err != nil &&
		!errors.Is(err, errNoChatConversation) && !errors.Is(err, errRouteDisabled) {
		b.cfg.Logger.Warn("conversation route not recorded; a card this turn files cannot report back",
			"task", taskID, "session", sessionID, "err", err)
		routeLost = true
	}

	// The door's side of this task: attributed by the session id the hook
	// payload carries, signed with the pod's shared secret, and only while
	// this task holds the session's turn.
	act := newSessionActivityState(sessionID, b.apiTraced())
	act.inTurn.Store(true)
	defer act.inTurn.Store(false)
	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return
	}
	run.act.Store(act)
	go b.runActivity(run)
	run.mu.Unlock()

	resp, err := b.sendAPI(reqCtx, req, body)
	if err != nil {
		b.finalizeAPIError(run, reqCtx, err)
		return
	}
	defer resp.Body.Close()
	// cancelReq stays set through the body read: the server can send its
	// headers before the body, and a cancel or shutdown in between must
	// still end the request.
	raw, err := io.ReadAll(io.LimitReader(resp.Body, apiResponseCap+1))
	run.mu.Lock()
	run.cancelReq = nil
	run.mu.Unlock()
	if err != nil && reqCtx.Err() != nil {
		b.finalizeAPIError(run, reqCtx, err)
		return
	}
	if err != nil {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-read-failed - %v", err), nil)
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		reason := "hermes-api-failed"
		switch {
		case resp.StatusCode == apiRateLimitedStatus || apiRateLimitedReasons[resp.Header.Get(apiFailureReasonHeader)]:
			reason = "hermes-rate-limited"
		case resp.StatusCode >= http.StatusBadRequest && resp.StatusCode < http.StatusInternalServerError:
			// The server refuses a request in 4xx before an agent runs
			// (the key, the session headers, the profile, the body); a
			// turn that ran and failed answers 5xx.
			reason = "hermes-api-refused"
		}
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - HTTP %d; session: %s; body tail: %s",
			reason, resp.StatusCode, sessionID, tail(string(raw), apiBodyTailBytes)), nil)
		return
	}
	if len(raw) > apiResponseCap {
		// Refused, never truncated: a cut answer would fail to parse and
		// read as a protocol fault, and a shortened one is worse than a
		// loud failure.
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-oversize - HTTP %d; session: %s; "+
			"the response body is over the %d-byte limit (apiResponseCap in api.go); the answer was refused rather than truncated",
			resp.StatusCode, sessionID, apiResponseCap), nil)
		return
	}
	var out apiChatResponse
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreadable - HTTP %d; session: %s; body tail: %s",
			resp.StatusCode, sessionID, tail(string(raw), apiBodyTailBytes)), nil)
		return
	}
	if apiTurnFailed(&out) {
		reason := "hermes-api-failed"
		if apiRateLimitedReasons[resp.Header.Get(apiFailureReasonHeader)] {
			reason = "hermes-rate-limited"
		}
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - HTTP %d, turn failed; session: %s; error: %s",
			reason, resp.StatusCode, sessionID, tail(out.Hermes.Error, apiBodyTailBytes)), nil)
		return
	}
	// A canceled task that finished anyway won the race: completed wins,
	// per the payload spec's cancel mapping, as on the subprocess path.
	text := out.Choices[0].Message.Content
	if routeLost {
		text += routeLostNote
	}
	b.finalize(run, lib.StateCompleted, "", &text)
}

// sendAPI sends req, retrying a refused connection every
// apiConnectRetryInterval for up to Config.APIConnectRetry while ctx lasts.
// A response of any status is returned as it is; an error is wrapped in
// errNeverConnected when no attempt got a connection, so no request was
// sent.
func (b *Bridge) sendAPI(ctx context.Context, req *http.Request, body []byte) (*http.Response, error) {
	var connected atomic.Bool
	ctx = httptrace.WithClientTrace(ctx, &httptrace.ClientTrace{
		GotConn: func(httptrace.GotConnInfo) { connected.Store(true) },
	})
	req = req.WithContext(ctx)
	neverConnected := func(err error) error {
		if connected.Load() {
			return err
		}
		return fmt.Errorf("%w: %w", errNeverConnected, err)
	}
	giveUp := time.Now().Add(b.cfg.APIConnectRetry)
	for {
		resp, err := b.apiClient.Do(req)
		if err == nil {
			return resp, nil
		}
		if !errors.Is(err, syscall.ECONNREFUSED) || time.Now().After(giveUp) {
			return nil, neverConnected(err)
		}
		select {
		case <-ctx.Done():
			return nil, neverConnected(err)
		case <-time.After(apiConnectRetryInterval):
		}
		req = req.Clone(ctx)
		req.Body = io.NopCloser(bytes.NewReader(body))
	}
}

// finalizeAPIError ends a task whose request (or wait for its session's
// turn) ended without a response, naming the cause: the cancel, the
// shutdown and the deadline each end reqCtx, so they are read first.
func (b *Bridge) finalizeAPIError(run *taskRun, reqCtx context.Context, err error) {
	switch {
	case run.canceled.Load() && errors.Is(err, errWaitingForTurn):
		// Nothing was sent: the task was still waiting for its turn, the
		// API executor's queue.
		b.finalize(run, lib.StateCanceled, canceledBeforeStartReason, nil)
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		b.finalize(run, lib.StateFailed, shutdownReason, nil)
	case reqCtx.Err() == context.DeadlineExceeded && errors.Is(err, errWaitingForTurn):
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: session-busy - waited %s for the session's previous turn; no request was sent", b.cfg.TaskDeadline), nil)
	case reqCtx.Err() == context.DeadlineExceeded && errors.Is(err, errNeverConnected):
		// Infrastructure, as the refused connection past the retry window
		// is: the deadline ended a wait for a server that never listened.
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreachable - task deadline %s ended before the server accepted a connection; no request was sent: %v", b.cfg.TaskDeadline, err), nil)
	case reqCtx.Err() == context.DeadlineExceeded:
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - request ended after %s", b.cfg.TaskDeadline), nil)
	default:
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreachable - %v", err), nil)
	}
}

// newAPIClient is the HTTP client the API executor uses: no client timeout
// (the request context carries the task deadline), and no proxy, since the
// server is loopback.
func newAPIClient() *http.Client {
	return &http.Client{Transport: &http.Transport{Proxy: nil, DisableKeepAlives: false}}
}

// apiExecutorValid reports whether cfg names a usable API executor: the
// name, a URL and the key the server needs before it honours the session
// headers. The daemon picks this executor with no key only when told to
// (BRIDGE_EXECUTOR=api), so an empty key here is a misdeclared sidecar,
// refused at start rather than one 401 per task.
func apiExecutorValid(cfg *Config) error {
	if strings.TrimSpace(cfg.APIKey) == "" {
		return fmt.Errorf("executor %q needs APIKey (the pod's API_SERVER_KEY): the API server refuses the session headers without it", ExecutorAPI)
	}
	u, err := url.ParseRequestURI(cfg.APIURL)
	if err != nil {
		return fmt.Errorf("executor %q: APIURL %q: %w", ExecutorAPI, cfg.APIURL, err)
	}
	// ParseRequestURI reads "localhost:8642/v1" as scheme "localhost", so
	// the scheme and host are what refuse a URL with no scheme.
	if (u.Scheme != apiURLSchemeHTTP && u.Scheme != apiURLSchemeHTTPS) || u.Host == "" {
		return fmt.Errorf("executor %q: APIURL %q: want an %s:// or %s:// URL with a host", ExecutorAPI, cfg.APIURL, apiURLSchemeHTTP, apiURLSchemeHTTPS)
	}
	return nil
}
