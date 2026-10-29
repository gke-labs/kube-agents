package hermesbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// The turn stop (#3263): a cancel ends the bridge's request for a turn, but
// the Hermes API server interrupts an agent only when a streaming client
// disconnects, and this executor does not stream. So a turn the person
// stopped ran on in Hermes and could file a kanban card after the card stop
// had already looked for one. When a cancel ends a turn's request, the bridge
// asks the API server to stop the turn running in the session (the image's
// api_session_stop patch, deploy/docker/patches/api_session_stop.py): the
// route hard-interrupts it and answers once it has ended, or after its own
// wait (WAIT_SECONDS there, 10 s).
//
// The task's terminal goes out first, so the stop's reply is not held up, and
// the stop runs while the task still holds the session's turn (runTaskAPI
// releases it on return). A card stop for the conversation waits for that
// turn (cardStopTurnWait, 20 s), so it looks for cards only once the turn has
// ended, and archives a card the turn filed before the interrupt reached it.
// turnStopTimeout sits between the route's wait and cardStopTurnWait; raise
// them together.
const (
	// turnStopPathFormat replaces the chat-completions suffix of
	// Config.APIURL, so a profile-prefixed URL (/p/<profile>/v1/...) keeps
	// its prefix.
	turnStopPathFormat = "/kube-agents/sessions/%s/stop"
	// apiChatCompletionsSuffix is the end of the chat-completions URL the
	// stop path replaces.
	apiChatCompletionsSuffix = "/chat/completions"
	// turnStopResponseCap bounds the answer read: four fields.
	turnStopResponseCap = 4 << 10
)

// turnStopTimeout bounds the request: the route's 10 s wait plus room, under
// cardStopTurnWait. A var so a test can shorten it.
var turnStopTimeout = 15 * time.Second

// errTurnStopUnsupported is a server with no stop route: an image built
// before the api_session_stop patch.
var errTurnStopUnsupported = errors.New("the API server has no session stop route")

// turnStopResponse is the route's answer.
type turnStopResponse struct {
	Turns       int  `json:"turns"`
	Interrupted int  `json:"interrupted"`
	Ended       bool `json:"ended"`
}

// stopCanceledTurn stops the session's running turn in Hermes when a cancel
// ended this task's request for it. A request that never reached the server
// started nothing, and a shutdown takes the whole pod's turns with it.
func (b *Bridge) stopCanceledTurn(run *taskRun, sessionID string, err error) {
	if !run.canceled.Load() || b.closing.Load() || errors.Is(err, errNeverConnected) {
		return
	}
	taskID := run.origin.TaskID
	resp, err := b.postTurnStop(sessionID)
	switch {
	case err != nil && b.closing.Load():
		// The shutdown ended the request; the pod's turns go with it.
	case errors.Is(err, errTurnStopUnsupported):
		b.cfg.Logger.Warn("the canceled turn runs on in hermes: "+err.Error(), "task", taskID, "session", sessionID)
	case err != nil:
		b.cfg.Logger.Warn("the canceled turn could not be stopped in hermes", "task", taskID, "session", sessionID, "err", err)
	case resp.Turns == 0:
		b.cfg.Logger.Info("no turn found running in hermes for the canceled task; nothing to stop",
			"task", taskID, "session", sessionID)
	case !resp.Ended:
		b.cfg.Logger.Warn("the canceled turn was still running in hermes when the stop answered",
			"task", taskID, "session", sessionID, "turns", resp.Turns, "interrupted", resp.Interrupted)
	default:
		b.cfg.Logger.Info("canceled turn stopped in hermes", "task", taskID, "session", sessionID,
			"turns", resp.Turns, "interrupted", resp.Interrupted)
	}
}

// postTurnStop asks the API server to stop sessionID's running turns and
// returns its answer.
func (b *Bridge) postTurnStop(sessionID string) (*turnStopResponse, error) {
	base, ok := strings.CutSuffix(strings.TrimRight(b.cfg.APIURL, "/"), apiChatCompletionsSuffix)
	if !ok {
		return nil, fmt.Errorf("APIURL %q does not end in %s, so the stop route's address is unknown", b.cfg.APIURL, apiChatCompletionsSuffix)
	}
	parent := b.stopping
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, turnStopTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+fmt.Sprintf(turnStopPathFormat, url.PathEscape(sessionID)), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.APIKey)
	resp, err := b.apiClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, turnStopResponseCap))
	if err != nil {
		return nil, fmt.Errorf("reading the answer: %w", err)
	}
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return nil, errTurnStopUnsupported
	case resp.StatusCode != http.StatusOK:
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(tail(string(data), routeErrorTailBytes)))
	}
	var out turnStopResponse
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("unreadable answer: %w", err)
	}
	return &out, nil
}
