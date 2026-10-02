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
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The API executor: a task is one turn in the conversation's Hermes session.
//
// Instead of a cold `hermes chat -Q` per task, the bridge POSTs the task's
// text to the Hermes API server in the same pod, which runs it under the
// gateway's default profile, the persona that answers the same message on
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
// fixed route keeps refusing steers), or bring a kanban card's completion
// back to the thread (the API server has no push channel, so the notifier
// wakes the session instead; the subprocess loses it the same way). Both are
// named in a2a/docs/hermes-bridge.md.
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
	// is not usable verbatim (apiSessionID), so a hashed id cannot collide
	// with a verbatim one.
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
	// apiRateLimitedStatus is the status the server answers when the
	// provider rate-limited or billed out the turn; it maps to the same
	// reason token as the CLI's EX_TEMPFAIL exit, so the eval harness
	// classes it as infrastructure either way.
	apiRateLimitedStatus = http.StatusTooManyRequests
	// apiBodyTailBytes bounds the error body quoted in a failed terminal.
	apiBodyTailBytes = 2048
	// apiResponseCap bounds a successful response body read: an answer is
	// text, and anything past this is not one.
	apiResponseCap = 8 << 20
	// DefaultAPIConnectRetry and apiConnectRetryInterval pace the retry of a
	// refused connection: the sidecar and the agent container start
	// together, and the bridge can be consuming before hermes's API server
	// listens. A refused connection never reached the server, so retrying it
	// cannot run a turn twice. Past the window the server is not starting,
	// it is absent, and the task ends hermes-api-unreachable.
	DefaultAPIConnectRetry  = 2 * time.Minute
	apiConnectRetryInterval = time.Second
)

// apiSafeContextID is what a contextId may hold to be used verbatim in a
// session id: the server interpolates the id into a filename and refuses a
// separator or "..", and the bus does not vouch for a contextId's shape.
var apiSafeContextID = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

// apiSessionID is the Hermes session a conversation's tasks share: the
// contextId under apiSessionIDPrefix when it is path-safe, else a hash of
// it, so a hostile or odd contextId still maps to one stable session and
// never to a path. A task with no contextId gets its own session, keyed by
// the task id, which is what the subprocess executor gave every task.
func apiSessionID(contextID, taskID string) string {
	if contextID == "" {
		contextID = "task-" + taskID
	}
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
// reads: the first choice's text. The server also answers an OpenAI error
// envelope on a hard failure, with a non-2xx status; that path reads the
// body as text.
type apiChatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
}

// runTaskAPI is runTask for ExecutorAPI. The lifecycle is the subprocess
// path's: working first, one terminal from this goroutine, cancel and the
// deadline end the request rather than a process group, and shutdown names
// itself.
func (b *Bridge) runTaskAPI(ctx context.Context, run *taskRun) {
	taskID := run.origin.TaskID
	prompt, ok := promptFromMessage(run.origin.Payload)
	if !ok {
		b.finalize(run, lib.StateRejected,
			"reason: no-text-parts - the submission message carries nothing hermes can be asked", nil)
		return
	}
	if err := run.exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}
	sessionID := apiSessionID(run.origin.ContextID, taskID)
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

	// The door's side of this task: attributed by the session id the hook
	// payload carries, signed with the pod's shared secret, and only while
	// this task holds the session's turn; the heartbeat runs either way,
	// waiting included.
	act := newSessionActivityState(sessionID)
	run.mu.Lock()
	if run.state != stateRunning {
		run.mu.Unlock()
		return
	}
	run.act.Store(act)
	run.cancelReq = cancelReq
	go b.runActivity(run)
	if run.canceled.Load() {
		cancelReq()
	}
	run.mu.Unlock()

	release := b.turns.acquire(reqCtx, sessionID)
	if release == nil {
		b.finalizeAPIError(run, reqCtx, fmt.Errorf("waiting for the session's previous turn: %w", reqCtx.Err()))
		return
	}
	defer release()
	act.inTurn.Store(true)
	defer act.inTurn.Store(false)

	resp, err := b.sendAPI(reqCtx, req, body)
	run.mu.Lock()
	run.cancelReq = nil
	run.mu.Unlock()
	if err != nil {
		b.finalizeAPIError(run, reqCtx, err)
		return
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, apiResponseCap))
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
		if resp.StatusCode == apiRateLimitedStatus {
			reason = "hermes-rate-limited"
		}
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - HTTP %d; session: %s; body tail: %s",
			reason, resp.StatusCode, sessionID, tail(string(raw), apiBodyTailBytes)), nil)
		return
	}
	var out apiChatResponse
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: hermes-api-unreadable - HTTP %d; session: %s; body tail: %s",
			resp.StatusCode, sessionID, tail(string(raw), apiBodyTailBytes)), nil)
		return
	}
	if got := resp.Header.Get(apiSessionIDHeader); got != "" && got != sessionID {
		b.cfg.Logger.Warn("hermes answered under another session id", "task", taskID, "sent", sessionID, "got", got)
	}
	// A canceled task that finished anyway won the race: completed wins,
	// per the payload spec's cancel mapping, as on the subprocess path.
	text := out.Choices[0].Message.Content
	b.finalize(run, lib.StateCompleted, "", &text)
}

// sendAPI sends req, retrying a refused connection every
// apiConnectRetryInterval for up to Config.APIConnectRetry while ctx lasts.
// Any other error, or a response of any status, is returned as it is.
func (b *Bridge) sendAPI(ctx context.Context, req *http.Request, body []byte) (*http.Response, error) {
	giveUp := time.Now().Add(b.cfg.APIConnectRetry)
	for {
		resp, err := b.apiClient.Do(req)
		if err == nil || !errors.Is(err, syscall.ECONNREFUSED) || time.Now().After(giveUp) {
			return resp, err
		}
		select {
		case <-ctx.Done():
			return nil, err
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
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		b.finalize(run, lib.StateFailed, shutdownReason, nil)
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
// headers. The daemon's environment carries API_SERVER_KEY into the sidecar
// (the agent container's env is copied), so an empty key is a misdeclared
// sidecar, refused at start rather than one 403 per task.
func apiExecutorValid(cfg *Config) error {
	if strings.TrimSpace(cfg.APIKey) == "" {
		return fmt.Errorf("executor %q needs APIKey (the pod's API_SERVER_KEY): the API server refuses the session headers without it", ExecutorAPI)
	}
	if _, err := http.NewRequest(http.MethodPost, cfg.APIURL, nil); err != nil {
		return fmt.Errorf("executor %q: APIURL %q: %w", ExecutorAPI, cfg.APIURL, err)
	}
	return nil
}
