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
	// routeMemoryTTL is how long a remembered record is trusted to still be
	// in the store. session-kv prunes a route row 14 days after its last
	// write (CLEANUP_TTL_DAYS in session_kv_server.py), and only a successful
	// PUT refreshes it, so a day leaves a wide margin.
	routeMemoryTTL = 24 * time.Hour
	// routeLostNote follows a completed answer whose route could not be
	// recorded, so the user is told rather than left waiting for a card's
	// answer that cannot arrive.
	routeLostNote = "\n\n(If this turn filed a card, its answer cannot be posted back to this conversation: the conversation's route could not be recorded.)"
)

// routePlatforms maps a gateway conversation key's prefix to the platform name
// the agent side spells it with (Hermes's, and lib's NotifyPlatform names).
// A conversation on any other door (inject, console, the A2A door, Discord) has
// no chat notify route; identityRouteFor records an identity-only route for
// doors that still carry speaker or audience context.
var routePlatforms = map[string]string{
	"gchat:": lib.NotifyPlatformGchat,
	"slack:": lib.NotifyPlatformSlack,
}

// errNoChatConversation is an authority that names no chat conversation the
// notify route serves; nothing is recorded and nothing is wrong.
var errNoChatConversation = errors.New("no chat conversation to route to")

// errRouteDisabled is a bridge configured with no route store (RouteURL).
var errRouteDisabled = errors.New("no route store configured")

// conversationRoute is the PUT's body. Ask is the kind of the task's ask,
// a mark.ask token (askKind in chat.go): the kanban notifier keeps it marked
// on the chat messages its cards send while they run. UserID and ChatType
// carry the gateway's requester principal (hmac:<32-hex>) and audience kind
// ("dm" or "group") so the memory provider can isolate personal memory per
// user and fail closed in shared threads.
type conversationRoute struct {
	Platform     string `json:"platform"`
	Conversation string `json:"conversation"`
	ContextID    string `json:"context_id"`
	Ask          string `json:"ask,omitempty"`
	UserID       string `json:"user_id,omitempty"`
	ChatType     string `json:"chat_type,omitempty"`
}

type routeAuthority struct {
	Audience struct {
		Conversation string `json:"conversation"`
		Kind         string `json:"kind"`
	} `json:"audience"`
	Requester struct {
		Principal  string `json:"principal"`
		Backend    string `json:"backend"`
		VerifiedBy string `json:"verifiedBy"`
	} `json:"requester"`
}

func parseRouteAuthority(env *lib.Envelope) (routeAuthority, error) {
	var auth routeAuthority
	if len(env.Authority) > 0 {
		if err := json.Unmarshal(env.Authority, &auth); err != nil {
			return routeAuthority{}, fmt.Errorf("authority unreadable: %w", err)
		}
	}
	return auth, nil
}

// routeFor reads the conversation the gateway's authority block names
// (audience.conversation, its session-record key), the platform it is on, and
// the speaker identity and audience kind for memory scoping.
func routeFor(env *lib.Envelope) (conversationRoute, error) {
	auth, err := parseRouteAuthority(env)
	if err != nil {
		return conversationRoute{}, err
	}
	key := strings.TrimSpace(auth.Audience.Conversation)
	userID := strings.TrimSpace(auth.Requester.Principal)
	chatType := strings.TrimSpace(auth.Audience.Kind)
	for prefix, platform := range routePlatforms {
		if strings.HasPrefix(key, prefix) && len(key) > len(prefix) && env.ContextID != "" {
			return conversationRoute{
				Platform:     platform,
				Conversation: key,
				ContextID:    env.ContextID,
				UserID:       userID,
				ChatType:     chatType,
			}, nil
		}
	}
	return conversationRoute{}, errNoChatConversation
}

// hasSharedPrincipal reports whether the gateway conversation or requester
// uses a shared credential across multiple callers (the console door's
// nats:console grant, or the inject and A2A static bearer-token doors where any
// token holder can self-assert an author/caller name). Personal memory must
// stay off for shared credentials so one caller's private memories never pool
// with or become readable by another's.
func hasSharedPrincipal(key string, auth routeAuthority) bool {
	if strings.HasPrefix(key, "console:") || strings.HasPrefix(key, "inject:") {
		return true
	}
	switch strings.TrimSpace(auth.Requester.Backend) {
	case "console", "inject", "a2a":
		return true
	}
	switch strings.TrimSpace(auth.Requester.VerifiedBy) {
	case "nats-grant", "inject-bearer", "a2a-bearer":
		return true
	}
	return false
}

// identityRouteFor extracts an identity-only record for doors without a chat
// notify route (such as the Google-token A2A door or Discord) when the gateway
// supplies a non-shared requester principal. Doors whose credential is shared
// across callers (console, inject, and static-token A2A) clear userID and do
// not record a user route, since any a2a-* session without a stored user_id
// already fails closed as an unattributable interactive conversation.
func identityRouteFor(env *lib.Envelope) (conversationRoute, bool) {
	auth, err := parseRouteAuthority(env)
	if err != nil {
		return conversationRoute{}, false
	}
	key := strings.TrimSpace(auth.Audience.Conversation)
	userID := strings.TrimSpace(auth.Requester.Principal)
	chatType := strings.TrimSpace(auth.Audience.Kind)
	if hasSharedPrincipal(key, auth) || key == "" || userID == "" || env.ContextID == "" {
		return conversationRoute{}, false
	}
	return conversationRoute{
		Conversation: key,
		ContextID:    env.ContextID,
		UserID:       userID,
		ChatType:     chatType,
	}, true
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
		if errors.Is(err, errNoChatConversation) {
			if idRoute, ok := identityRouteFor(env); ok && b.recordedRoute(sessionID) != idRoute {
				if putErr := b.putRoute(ctx, sessionID, idRoute); putErr != nil {
					b.cfg.Logger.Warn("conversation identity PUT failed", "session", sessionID, "err", putErr)
				} else {
					b.rememberRoute(sessionID, idRoute)
				}
			}
		}
		return err
	}
	prompt, _ := promptFromMessage(env.Payload)
	route.Ask = askKind(prompt)
	if err := b.putRoute(ctx, sessionID, route); err != nil {
		// The ask is a reaction's kind, not what a card needs to report
		// back: an earlier record of the same conversation still routes it.
		// In a group conversation, the requester principal varies by speaker
		// while personal memory is disabled regardless, so an earlier group
		// record of the same conversation still holds.
		earlier := b.recordedRoute(sessionID)
		earlier.Ask = route.Ask
		if route.ChatType == "group" {
			earlier.UserID = route.UserID
		}
		if earlier == route {
			b.cfg.Logger.Warn("conversation route PUT failed; the session's earlier record of the same route stands",
				"session", sessionID, "err", err)
			return nil
		}
		return err
	}
	b.rememberRoute(sessionID, route)
	return nil
}

// rememberedRoute is a route this process recorded and when it did.
type rememberedRoute struct {
	route conversationRoute
	at    time.Time
}

// routeNow is the clock route memory is aged by; tests replace it.
var routeNow = time.Now

// recordedRoute is the route sessionID last recorded in this process within
// routeMemoryTTL, or the zero route.
func (b *Bridge) recordedRoute(sessionID string) conversationRoute {
	b.routesMu.Lock()
	defer b.routesMu.Unlock()
	r, ok := b.routes[sessionID]
	if !ok || routeNow().Sub(r.at) >= routeMemoryTTL {
		return conversationRoute{}
	}
	return r.route
}

// rememberRoute notes route as sessionID's recorded route, as of now.
func (b *Bridge) rememberRoute(sessionID string, route conversationRoute) {
	b.routesMu.Lock()
	defer b.routesMu.Unlock()
	if b.routes == nil || len(b.routes) >= routeMemoryMax {
		b.routes = make(map[string]rememberedRoute)
	}
	b.routes[sessionID] = rememberedRoute{route: route, at: routeNow()}
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
