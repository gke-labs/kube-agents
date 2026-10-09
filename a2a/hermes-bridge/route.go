package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The conversation route: before a task's first turn on the API executor, the bridge
// records which gateway conversation its Hermes session answers, in the
// pod's session-kv store (agents/platform/scripts/session_kv_server.py, PUT
// /v1/sessions/{id}/route). A kanban card the turn files subscribes to the
// API server and this session id; deploy/docker/patches/kanban_event_routing.py
// swaps in the recorded route, and the kanban notifier posts the card's report
// with `a2a notify --conversation`, which the gateway accepts only for the
// conversation whose session record carries this context id. Without the
// route the card's report reaches nobody once the turn has answered.
const (
	// DefaultRouteURL is the session-kv server, on loopback in the same pod.
	DefaultRouteURL = "http://127.0.0.1:8699"
	// RouteKeyEnv names the variable holding session-kv's bearer, which the
	// bridge sidecar inherits from the platform-agent container's env.
	RouteKeyEnv = "SESSION_KV_API_KEY"
	// routePathFormat is the route endpoint under the server.
	routePathFormat = "/v1/sessions/%s/route"
	// routeTimeout bounds the PUT: the turn waits on it, and the store is
	// local.
	routeTimeout = 5 * time.Second
	// routeErrorTailBytes bounds the server's refusal quoted in the log.
	routeErrorTailBytes = 200
	// routeMemoryMax bounds the sessions whose last recorded route the
	// bridge remembers; past it the memory is cleared, and a failed PUT is
	// then reported as lost, as it is after a restart.
	routeMemoryMax = 4096
	// routeLostNote follows a completed answer whose route could not be
	// recorded, so the user is told rather than left waiting for a card's
	// answer that cannot arrive.
	routeLostNote = "\n\n(If this turn filed a card, its answer cannot be posted back to this conversation: the conversation's route could not be recorded.)"
)

// routePlatforms maps a gateway conversation key's prefix to the platform name
// the agent side spells it with (Hermes's, and lib.NotifyPlatformGchat's).
// Slack's route is recorded the same way and is delivered once the gateway
// arms its notify route for Slack. A conversation on any other door (inject,
// the A2A door, Discord) has no notify route, and no route is recorded for it.
var routePlatforms = map[string]string{
	"gchat:": lib.NotifyPlatformGchat,
	"slack:": "slack",
}

// errNoChatConversation is an authority that names no chat conversation the
// notify route serves; nothing is recorded and nothing is wrong.
var errNoChatConversation = errors.New("no chat conversation to route to")

// errRouteDisabled is a bridge configured with no route store (RouteURL).
var errRouteDisabled = errors.New("no route store configured")

// conversationRoute is the PUT's body.
type conversationRoute struct {
	Platform     string `json:"platform"`
	Conversation string `json:"conversation"`
	ContextID    string `json:"context_id"`
}

// routeFor reads the conversation the gateway's authority block names
// (audience.conversation, its session-record key) and the platform it is on.
func routeFor(env *lib.Envelope) (conversationRoute, error) {
	var auth struct {
		Audience struct {
			Conversation string `json:"conversation"`
		} `json:"audience"`
	}
	if len(env.Authority) > 0 {
		if err := json.Unmarshal(env.Authority, &auth); err != nil {
			return conversationRoute{}, fmt.Errorf("authority unreadable: %w", err)
		}
	}
	key := strings.TrimSpace(auth.Audience.Conversation)
	for prefix, platform := range routePlatforms {
		if strings.HasPrefix(key, prefix) && len(key) > len(prefix) && env.ContextID != "" {
			return conversationRoute{Platform: platform, Conversation: key, ContextID: env.ContextID}, nil
		}
	}
	return conversationRoute{}, errNoChatConversation
}

// recordRoute records env's conversation route against sessionID. It returns
// errNoChatConversation when there is none to record, and an error when the
// store refused or could not be reached, unless an earlier task of the same
// session recorded the same route: the store still holds it, so nothing is
// lost.
func (b *Bridge) recordRoute(ctx context.Context, sessionID string, env *lib.Envelope) error {
	if b.cfg.RouteURL == "" {
		return errRouteDisabled
	}
	route, err := routeFor(env)
	if err != nil {
		return err
	}
	if err := b.putRoute(ctx, sessionID, route); err != nil {
		if b.recordedRoute(sessionID) == route {
			b.cfg.Logger.Warn("conversation route PUT failed; the session's earlier record of the same route stands",
				"session", sessionID, "err", err)
			return nil
		}
		return err
	}
	b.rememberRoute(sessionID, route)
	return nil
}

// recordedRoute is the route sessionID last recorded in this process, or the
// zero route.
func (b *Bridge) recordedRoute(sessionID string) conversationRoute {
	b.routesMu.Lock()
	defer b.routesMu.Unlock()
	return b.routes[sessionID]
}

// rememberRoute notes route as sessionID's recorded route.
func (b *Bridge) rememberRoute(sessionID string, route conversationRoute) {
	b.routesMu.Lock()
	defer b.routesMu.Unlock()
	if b.routes == nil || len(b.routes) >= routeMemoryMax {
		b.routes = make(map[string]conversationRoute)
	}
	b.routes[sessionID] = route
}

// putRoute writes route against sessionID in the store.
func (b *Bridge) putRoute(ctx context.Context, sessionID string, route conversationRoute) error {
	if strings.TrimSpace(b.cfg.RouteKey) == "" {
		return fmt.Errorf("%s is not set", RouteKeyEnv)
	}
	body, err := json.Marshal(route)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, routeTimeout)
	defer cancel()
	target := strings.TrimRight(b.cfg.RouteURL, "/") + fmt.Sprintf(routePathFormat, url.PathEscape(sessionID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, target, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.RouteKey)
	resp, err := b.routeClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, routeErrorTailBytes))
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return nil
}
