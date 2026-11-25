package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
	"unicode"
)

// The Microsoft Teams backend rides the credential proxy's Teams relay, the
// way the Google Chat backend rides its chat relay: Bot Framework activities
// reach the broker's queue (a receiver in front of the broker posts them to
// /v1/chat/teams/events), the gateway long-polls that queue and settles each
// receipt, and every reply goes out through the broker's /v1/chat/teams/api
// passthrough, which alone holds the bot's app id and password and alone
// talks to Microsoft. The gateway opens no inbound port for Teams, so the
// question #3056 raised about an inbound endpoint on the gateway does not
// arise here; the receiver stays where it is on the Hermes path (#1034).
const (
	teamsBackend   = "teams"
	teamsKeyPrefix = "teams:"
	// teamsVerifiedBy names the mechanism that checked a Teams requester at
	// ingress (authority.requester.verifiedBy): the broker's Teams relay,
	// which takes an activity only from the install's tenant, and the
	// allowed-users list here. The receiver in front of the broker is not
	// part of this repository, so nothing here vouches for its JWT check.
	teamsVerifiedBy = "teams-relay+allowlist"

	teamsRelayEventsPath = "/v1/chat/teams/events"
	teamsRelayAckPath    = "/v1/chat/teams/events/ack"
	teamsRelayAPIPath    = "/v1/chat/teams/api"
	// teamsThreadToken joins a channel conversation id to the message id of
	// the thread a reply belongs to ("19:…@thread.tacv2;messageid=…").
	teamsThreadToken = ";messageid="
	// A channel conversation id ends in one of these; a channel is the one
	// kind Teams threads, so a top-level post there starts a thread. A group
	// chat ("19:…@thread.v2") and a personal chat ("a:…") are not threaded.
	teamsChannelSuffix       = "@thread.tacv2"
	teamsLegacyChannelSuffix = "@thread.skype"
	// The two shapes a Teams conversation id takes: "19:…" for a channel,
	// a group chat or a meeting chat, "a:…" for a personal chat; something
	// must follow the prefix, no whitespace of any script may sit inside,
	// and the characters the broker's activity path refuses are out. The
	// operator applies the same rule to teams.homeChannel
	// (a2aTeamsHomeConversation).
	teamsGroupConversationPrefix    = "19:"
	teamsPersonalConversationPrefix = "a:"
	teamsConversationRefused        = "/?#%\\"

	// teamsDefaultServiceURL is where a reply goes when the conversation's
	// own regional serviceUrl is not known; Microsoft routes it.
	teamsDefaultServiceURL = "https://smba.trafficmanager.net/teams/"

	// teamsMaxActivityBytes is the outbound activity budget, measured on the
	// serialized activity the broker puts on the wire: Microsoft's figure is
	// about 100 KB of UTF-16 with 80 KB advised, and 28 KB of UTF-8 is under
	// it in every script (the Hermes adapter's budget, agents/platform/
	// scripts/teams_relay_patch.py).
	teamsMaxActivityBytes = 28 * 1024
	// teamsPieceBytes is the most bytes one post or edit carries
	// (PieceSizer): the relay and the notifier split above it. Well under
	// teamsMaxActivityBytes once the card's per-line overhead is added, and
	// far above discordChunk, so a report's table lands in one card rather
	// than cut into two with its header repeated (the 11-17 live test: a
	// seven-row table with console links came as six rows and one).
	teamsPieceBytes = 10000
	// The Adaptive Card vocabulary the builder emits (teamsMarkdownToCard):
	// element types, the header weight, heading sizes, the code font, and
	// the Markdown marks it reads.
	teamsCardType        = "AdaptiveCard"
	teamsCardTextBlock   = "TextBlock"
	teamsCardTable       = "Table"
	teamsCardTableRow    = "TableRow"
	teamsCardTableCell   = "TableCell"
	teamsCardBold        = "Bolder"
	teamsCardSizeLarge   = "Large"
	teamsCardSizeMedium  = "Medium"
	teamsCardMonospace   = "Monospace"
	teamsCardColumnWidth = 1
	teamsCardBullet      = "• "
	teamsCodeFence       = "```"
	teamsCodeTick        = "`"
	teamsBulletDash      = "- "
	teamsBulletStar      = "* "
	teamsPipe            = "|"
	// teamsTruncationMarker ends a text cut to fit the budget.
	teamsTruncationMarker = "\n… (message truncated to fit Teams' size limit)"
	// teamsSummaryMax bounds the plain-text preview a card activity carries.
	teamsSummaryMax = 140

	// teamsSeenCap bounds the inbound dedupe map; teamsServiceURLCap bounds
	// the per-conversation serviceUrl memory.
	teamsSeenCap       = 2048
	teamsServiceURLCap = 1024

	teamsRelayTimeout       = 60 * time.Second
	teamsPullRetryDelay     = 2 * time.Second
	teamsPullIdleDelay      = 200 * time.Millisecond
	teamsEventCountInterval = 5 * time.Minute
	// teamsRelayErrorBodyMaxBytes bounds a refused call's body read.
	teamsRelayErrorBodyMaxBytes = 4096

	// Bot Framework's own vocabulary.
	teamsActivityMessage    = "message"
	teamsConversationPerson = "personal"
	teamsEntityMention      = "mention"
	// teamsConversationsPath prefixes the activities endpoint the passthrough
	// relays: v3/conversations/<id>/activities.
	teamsConversationsPath = "v3/conversations/"
	teamsActivitiesSuffix  = "/activities"
	// teamsCardContentType is an Adaptive Card attachment's content type.
	teamsCardContentType = "application/vnd.microsoft.card.adaptive"
	// teamsCardVersion and teamsCardSchema are the Adaptive Card version
	// Teams renders Tables in, and its schema.
	teamsCardVersion = "1.5"
	teamsCardSchema  = "http://adaptivecards.io/schemas/adaptive-card.json"
	// teamsCardWidth asks Teams for the full pane: without it a card is
	// drawn in a fixed narrow column that squeezes a wide table.
	teamsCardWidth = "Full"
	// teamsHeadingMark and teamsHeadingGap are a Markdown heading's shape:
	// one or more marks, a space, the text.
	teamsHeadingMark = "#"
	teamsHeadingGap  = " "
	// teamsRelayBodyMaxBytes bounds an answered call's body read.
	teamsRelayBodyMaxBytes = 1 << 20
	// teamsBotFrameworkDomain is the domain under which Microsoft's other
	// Bot Framework hosts live.
	teamsBotFrameworkDomain = ".botframework.com"
)

// teamsBotFrameworkHosts are the Bot Framework hosts a reply may be sent to;
// the broker enforces the same list, this is the first gate.
var teamsBotFrameworkHosts = []string{"smba.trafficmanager.net", "api.botframework.com"}

// teamsSummaryMarks are the structural Markdown marks dropped from a card's
// preview; an underscore stays, since it is part of a name as often as a mark.
var teamsSummaryMarks = []string{"#", "*", "`", "|", ">"}

// errTeamsNoOpenDirect is OpenDirect's answer: creating a conversation is a
// POST to v3/conversations, outside the passthrough's allowed paths.
var errTeamsNoOpenDirect = errors.New("teams: opening a direct conversation is not served by the relay")

// teamsActivity is the part of a Bot Framework activity the adapter reads.
type teamsActivity struct {
	Type string `json:"type"`
	ID   string `json:"id"`
	// Timestamp is read for the ingress lag alone and kept raw, so a
	// timestamp of an unexpected JSON type cannot fail the decode and drop
	// the turn.
	Timestamp  json.RawMessage `json:"timestamp"`
	Text       string          `json:"text"`
	ServiceURL string          `json:"serviceUrl"`
	// ReplyToID and Value are a card press's: the message whose card was
	// pressed, and the pressed action's data (teams_press.go). Value is kept
	// raw, since a message may carry one of any shape.
	ReplyToID    string          `json:"replyToId"`
	Value        json.RawMessage `json:"value"`
	Conversation struct {
		ID               string `json:"id"`
		ConversationType string `json:"conversationType"`
		TenantID         string `json:"tenantId"`
	} `json:"conversation"`
	From struct {
		ID          string `json:"id"`
		AadObjectID string `json:"aadObjectId"`
		Name        string `json:"name"`
	} `json:"from"`
	Recipient struct {
		ID string `json:"id"`
	} `json:"recipient"`
	Entities []struct {
		Type      string `json:"type"`
		Text      string `json:"text"`
		Mentioned struct {
			ID string `json:"id"`
		} `json:"mentioned"`
	} `json:"entities"`
	ChannelData struct {
		Tenant struct {
			ID string `json:"id"`
		} `json:"tenant"`
	} `json:"channelData"`
}

// tenant is the activity's tenant: channelData.tenant.id, else
// conversation.tenantId, the two places Teams writes it.
func (a *teamsActivity) tenant() string {
	if t := strings.TrimSpace(a.ChannelData.Tenant.ID); t != "" {
		return t
	}
	return strings.TrimSpace(a.Conversation.TenantID)
}

// TeamsAdapter implements Adapter over the credential proxy's Teams relay.
// It holds no Microsoft credential: it authenticates to the relay with the
// pod's projected ServiceAccount token (the a2a-chat audience, the one the
// Google Chat backend presents), and the bot's app password stays in the
// broker.
type TeamsAdapter struct {
	relayURL  string
	tokenPath string
	// tenantID, when set, is the one Microsoft Entra tenant served; an
	// activity from another is acked away. The broker refuses them at
	// intake too; this is the second gate, so a broker configured without
	// the tenant still serves one.
	tenantID string
	// adaptiveCards renders a reply as an Adaptive Card when true; plain
	// text when false (spec.integration.teams.adaptiveCards).
	adaptiveCards bool
	// allowed is the allowed-users list, lowercased, and allowAll its
	// explicit off switch (SetAllowedUsers). A sender may be listed by the
	// Entra object id or the Bot Framework user id; classify hands the
	// gateway whichever one the list names, so the gateway's own check on
	// AuthorID (resolvePrincipal) sees the listed identity.
	allowed  map[string]bool
	allowAll bool
	client   *http.Client
	log      *slog.Logger
	// metrics counts each pull by outcome, times each passthrough call and
	// observes each turn's ingress lag (SetMetrics; nil counts nothing).
	metrics *Metrics

	mu sync.Mutex
	// seen dedupes inbound activities by id, with seenOrder its eviction
	// queue: Teams itself may deliver an activity twice to the receiver, and
	// a second copy must not become a second turn.
	seen      map[string]bool
	seenOrder []string
	// serviceURLs remembers each conversation's regional serviceUrl from its
	// inbound activities, so a reply goes where the tenant lives rather than
	// to the global host; serviceURLOrder is its eviction queue.
	serviceURLs     map[string]string
	serviceURLOrder []string

	// countInterval paces the received-events summary line (tests shorten
	// it). Read and written by Run's goroutine only.
	countInterval time.Duration
}

// NewTeamsAdapter builds the adapter against the credential proxy's relay
// base URL. tokenPath is the pod's projected ServiceAccount token; it is read
// per request because the kubelet rotates it. tenantID may be empty.
func NewTeamsAdapter(relayURL, tokenPath, tenantID string, adaptiveCards bool, log *slog.Logger) (*TeamsAdapter, error) {
	if relayURL == "" {
		return nil, fmt.Errorf("teams: relay URL is required")
	}
	if tokenPath == "" {
		return nil, fmt.Errorf("teams: relay token path is required")
	}
	return &TeamsAdapter{
		relayURL:      strings.TrimSuffix(relayURL, "/"),
		tokenPath:     tokenPath,
		tenantID:      strings.TrimSpace(tenantID),
		adaptiveCards: adaptiveCards,
		client:        &http.Client{Timeout: teamsRelayTimeout},
		log:           log,
		seen:          map[string]bool{},
		serviceURLs:   map[string]string{},
		allowed:       map[string]bool{},
		countInterval: teamsEventCountInterval,
	}, nil
}

// SetMetrics gives the adapter the gateway's counters, so each pull of the
// broker's Teams queue, each Bot Framework call through the passthrough and
// each turn's ingress lag is counted beside the other backends'.
func (a *TeamsAdapter) SetMetrics(m *Metrics) {
	a.metrics = m
}

// SetAllowedUsers hands the adapter the install's allowed-users list and the
// allow-all switch (Config.TeamsAllowedUsers, TeamsAllowAllUsers), the same
// values the gateway checks AuthorID against. Called once, before Run.
func (a *TeamsAdapter) SetAllowedUsers(users []string, allowAll bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.allowed = map[string]bool{}
	for _, u := range users {
		if u = strings.ToLower(strings.TrimSpace(u)); u != "" {
			a.allowed[u] = true
		}
	}
	a.allowAll = allowAll
}

// authorFor is the identity a turn is attributed to: the Entra object id
// (immutable; the one the allowlist should name) unless the list names the
// Bot Framework user id instead, in which case that one, so a list written
// with either kind of id admits its sender. An unlisted sender keeps the
// object id and the gateway refuses it with its notice. Caller holds a.mu.
func (a *TeamsAdapter) authorForLocked(aadObjectID, bfID string) string {
	aad := strings.TrimSpace(aadObjectID)
	bf := strings.TrimSpace(bfID)
	if aad != "" && (a.allowAll || a.allowed[strings.ToLower(aad)] || !a.allowed[strings.ToLower(bf)]) {
		return aad
	}
	return bf
}

// teamsConversationID is the session key for a Teams conversation id. The
// id is the whole binding: a channel reply's conversation id already names
// its thread ("19:…@thread.tacv2;messageid=…"), and a personal chat's names
// the chat, so the adapter adds the backend prefix and nothing else.
func teamsConversationID(conversation string) string {
	return teamsKeyPrefix + conversation
}

// teamsConversationFromID is the Teams conversation id behind a session key,
// or "" for a key of another backend.
func teamsConversationFromID(conversation string) string {
	if !strings.HasPrefix(conversation, teamsKeyPrefix) {
		return ""
	}
	return strings.TrimPrefix(conversation, teamsKeyPrefix)
}

// teamsServiceURLAllowed reports an https URL on an official Bot Framework
// host: the one place a reply, and the bot's token with it, may be sent.
func teamsServiceURLAllowed(raw string) bool {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme != "https" || u.Host == "" || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	host := strings.ToLower(u.Hostname())
	for _, known := range teamsBotFrameworkHosts {
		if host == known {
			return true
		}
	}
	return strings.HasSuffix(host, teamsBotFrameworkDomain)
}

// teamsStripBotMentions drops the bot's own <at>Name</at> mention from
// channel text: a channel message reaches the bot only when it is mentioned,
// and Teams leaves the markup in the text. Mentions of other people stay,
// and so does the rest of the text as typed, line breaks included: a pasted
// manifest or log must reach the agent in its own shape (the Chat adapter
// trims and nothing more).
func teamsStripBotMentions(a *teamsActivity) string {
	text := a.Text
	recipient := a.Recipient.ID
	for _, e := range a.Entities {
		if e.Type != teamsEntityMention || recipient == "" || e.Mentioned.ID != recipient || e.Text == "" {
			continue
		}
		text = strings.ReplaceAll(text, e.Text, "")
	}
	return strings.TrimSpace(text)
}

// classify turns one activity into a turn, or names why it is not one.
func (a *TeamsAdapter) classify(act *teamsActivity) (InboundMessage, string) {
	if act.Type != teamsActivityMessage {
		return InboundMessage{}, "not a message activity (" + act.Type + ")"
	}
	conversation := strings.TrimSpace(act.Conversation.ID)
	if conversation == "" {
		return InboundMessage{}, "activity names no conversation"
	}
	if act.ServiceURL != "" && !teamsServiceURLAllowed(act.ServiceURL) {
		return InboundMessage{}, "serviceUrl is not a Bot Framework host"
	}
	if a.tenantID != "" && !strings.EqualFold(act.tenant(), a.tenantID) {
		return InboundMessage{}, "tenant is not the install's"
	}
	if strings.TrimSpace(act.From.AadObjectID) == "" && strings.TrimSpace(act.From.ID) == "" {
		return InboundMessage{}, "sender has no id"
	}
	kind := sessionKindGroup
	if strings.EqualFold(act.Conversation.ConversationType, teamsConversationPerson) {
		kind = sessionKindDM
	}
	// A press of a button the adapter drew arrives as a message whose value
	// is the button's data and whose text is empty (teams_press.go).
	if press := teamsCardPress(act.Value); press.Action != "" {
		return a.classifyPress(act, press, conversation, kind)
	}
	text := teamsStripBotMentions(act)
	if text == "" {
		return InboundMessage{}, "empty ask (bare mention)"
	}

	author, dup := a.admit(act, conversation)
	if dup {
		return InboundMessage{}, "duplicate delivery of " + act.ID
	}
	return InboundMessage{
		Conversation: teamsConversationID(conversation),
		Kind:         kind,
		AuthorID:     author,
		MessageID:    act.ID,
		Text:         text,
	}, ""
}

// admit records what an activity that passed classify's checks teaches the
// adapter, and answers who sent it and whether it is a redelivery: the
// conversation's service URL is remembered, the sender is named as the
// allowlist names them (authorForLocked), and the activity's id is marked
// seen. A typed message and a press share it, so the two cannot drift.
func (a *TeamsAdapter) admit(act *teamsActivity, conversation string) (author string, dup bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if act.ServiceURL != "" {
		a.rememberServiceURLLocked(conversation, act.ServiceURL)
	}
	return a.authorForLocked(act.From.AadObjectID, act.From.ID), act.ID != "" && a.seenLocked(act.ID)
}

// seenLocked records an activity id and reports whether it was already
// seen; the oldest id is evicted at teamsSeenCap. Caller holds a.mu.
func (a *TeamsAdapter) seenLocked(id string) bool {
	if a.seen[id] {
		return true
	}
	a.seen[id] = true
	a.seenOrder = append(a.seenOrder, id)
	if len(a.seenOrder) > teamsSeenCap {
		delete(a.seen, a.seenOrder[0])
		a.seenOrder = a.seenOrder[1:]
	}
	return false
}

// rememberServiceURLLocked keeps a conversation's regional serviceUrl,
// bounded at teamsServiceURLCap. Caller holds a.mu.
func (a *TeamsAdapter) rememberServiceURLLocked(conversation, serviceURL string) {
	if _, known := a.serviceURLs[conversation]; !known {
		a.serviceURLOrder = append(a.serviceURLOrder, conversation)
		if len(a.serviceURLOrder) > teamsServiceURLCap {
			delete(a.serviceURLs, a.serviceURLOrder[0])
			a.serviceURLOrder = a.serviceURLOrder[1:]
		}
	}
	a.serviceURLs[conversation] = serviceURL
}

// serviceURLFor is the serviceUrl a reply to conversation goes to: the one
// its inbound activities named, else the global default.
func (a *TeamsAdapter) serviceURLFor(conversation string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	if u := a.serviceURLs[conversation]; u != "" {
		return u
	}
	return teamsDefaultServiceURL
}

// teamsEnvelope is one pulled event: the broker's receipt and the activity.
type teamsEnvelope struct {
	Receipt string          `json:"receipt"`
	Event   json.RawMessage `json:"event"`
}

// Run long-polls the relay's Teams event route and delivers turns to handler
// until ctx is done (Adapter.Run). Every pulled event is acked once its
// disposition is known, a payload that does not parse included: ingress is
// at-most-once, as on the Google Chat backend and for the same reason (a
// redelivery after a slow publish and a restart would be a duplicate task,
// worse than a lost ask a person retries by typing again).
func (a *TeamsAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	a.log.Info("teams event pull starting", "relay", a.relayURL+teamsRelayEventsPath, "summaryEvery", a.countInterval)
	windowStart := time.Now()
	var received, emptyPulls, failedPulls int
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if elapsed := time.Since(windowStart); elapsed >= a.countInterval {
			a.log.Info("teams events received", "events", received, "emptyPulls", emptyPulls,
				"failedPulls", failedPulls, "interval", elapsed.Round(time.Second))
			windowStart = time.Now()
			received, emptyPulls, failedPulls = 0, 0, 0
		}
		env, err := a.pullEvent(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			failedPulls++
			a.metrics.teamsPull(teamsPullFailed)
			a.log.Warn("teams event pull failed", "err", err)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(teamsPullRetryDelay):
			}
			continue
		}
		if env == nil {
			emptyPulls++
			a.metrics.teamsPull(teamsPullEmpty)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(teamsPullIdleDelay):
			}
			continue
		}
		// The receipt time, before the ack's round trip, so the ingress lag
		// holds only what happened before the gateway had the activity.
		arrived := time.Now()
		received++
		a.metrics.teamsPull(teamsPullEvents)
		var act teamsActivity
		decodeErr := json.Unmarshal(env.Event, &act)
		a.ack(env.Receipt)
		if decodeErr != nil {
			a.log.Warn("teams activity did not parse; acked away", "err", decodeErr)
			continue
		}
		msg, reason := a.classify(&act)
		if reason != "" {
			a.log.Info("teams activity is not a turn; acked", "activity", act.ID, "reason", reason)
			continue
		}
		a.metrics.teamsIngress(arrived, teamsTimestampText(act.Timestamp))
		handler(msg)
	}
}

// pullEvent asks the relay for one event; nil with no error means the long
// poll came back empty.
func (a *TeamsAdapter) pullEvent(ctx context.Context) (*teamsEnvelope, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.relayURL+teamsRelayEventsPath, nil)
	if err != nil {
		return nil, fmt.Errorf("teams: building pull request: %w", err)
	}
	if err := a.authorize(req); err != nil {
		return nil, err
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("teams: event pull: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, teamsRelayErrorBodyMaxBytes))
		return nil, teamsRelayError(fmt.Sprintf("teams: event pull answered %d", resp.StatusCode), body)
	}
	var body struct {
		Event *teamsEnvelope `json:"event"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("teams: decoding pulled event: %w", err)
	}
	if body.Event == nil || len(body.Event.Event) == 0 {
		return nil, nil
	}
	return body.Event, nil
}

// ack settles one receipt; every pulled event is acked (Run), so the relay's
// nack route has no caller here. A failed ack is logged and the event is
// gone either way (the relay requeues only on a nack and keeps an unsettled
// receipt in its table), which is the at-most-once rule the pull follows.
func (a *TeamsAdapter) ack(receipt string) {
	if receipt == "" {
		return
	}
	payload, _ := json.Marshal(map[string]string{"receipt": receipt})
	if _, err := a.relayPost(context.Background(), teamsRelayAckPath, payload); err != nil {
		a.log.Warn("teams receipt ack failed", "err", err)
	}
}

// authorize sets the relay bearer token on one request, re-read per call.
func (a *TeamsAdapter) authorize(req *http.Request) error {
	token, err := os.ReadFile(a.tokenPath)
	if err != nil {
		return fmt.Errorf("teams: reading relay token: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	return nil
}

// relayPost is one authenticated POST to the relay under ctx.
func (a *TeamsAdapter) relayPost(ctx context.Context, path string, payload []byte) ([]byte, error) {
	body, _, err := a.relayPostStatus(ctx, path, payload)
	return body, err
}

// relayPostStatus is relayPost with the relay's own HTTP status beside the
// answer, 0 for a call that got no response, for the passthrough's timing.
func (a *TeamsAdapter) relayPostStatus(ctx context.Context, path string, payload []byte) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, a.relayURL+path, bytes.NewReader(payload))
	if err != nil {
		return nil, 0, fmt.Errorf("teams: building relay request: %w", err)
	}
	if err := a.authorize(req); err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("teams: relay request: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, teamsRelayBodyMaxBytes))
	if err != nil {
		return nil, 0, fmt.Errorf("teams: reading relay response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, resp.StatusCode, teamsRelayError(fmt.Sprintf("teams: relay %s answered %d", path, resp.StatusCode), body)
	}
	return body, resp.StatusCode, nil
}

// apiPost is one Bot Framework call through the broker's passthrough, timed
// and counted under the operation httpMethod and endpoint spell
// (teamsAPIMethodLabel) and the outcome the broker's answer carried.
func (a *TeamsAdapter) apiPost(ctx context.Context, httpMethod, endpoint string, payload []byte) ([]byte, error) {
	start := time.Now()
	body, relayStatus, err := a.relayPostStatus(ctx, teamsRelayAPIPath, payload)
	a.metrics.teamsAPICall(teamsAPIMethodLabel(httpMethod, endpoint), relayStatus, teamsCarriedStatus(err), time.Since(start))
	return body, err
}

// teamsRelayError carries the broker's answer forward: its `error` and
// `detail` (the detail quotes Microsoft's refusal when there was one), so a
// 502 names invalid_client or a roster refusal rather than nothing. When the
// body carries Microsoft's own status (teams.status, the broker's record of
// an upstream refusal), the error is a teamsUpstreamError around that text,
// so the passthrough's timing can tell a 429 from the broker being down.
func teamsRelayError(prefix string, body []byte) error {
	var parsed struct {
		Error  string `json:"error"`
		Detail string `json:"detail"`
		Teams  struct {
			Status int `json:"status"`
		} `json:"teams"`
	}
	if len(body) == 0 || json.Unmarshal(body, &parsed) != nil {
		return errors.New(prefix)
	}
	parts := []string{}
	for _, v := range []string{parsed.Error, parsed.Detail} {
		if v != "" {
			parts = append(parts, v)
		}
	}
	err := errors.New(prefix)
	if len(parts) > 0 {
		err = fmt.Errorf("%s: %s", prefix, strings.Join(parts, " "))
	}
	if parsed.Teams.Status != 0 {
		return &teamsUpstreamError{Status: parsed.Teams.Status, Err: err}
	}
	return err
}

// Post writes text to a conversation as one activity and returns its id
// (Adapter.Post). The text arrives already split: the gateway cuts a
// backend's replies at its piece size before it posts (chatChunks at
// PieceSize, teamsPieceBytes here), so one call is one piece, and the
// adapter only fits it under the activity budget (teamsFitActivity).
func (a *TeamsAdapter) Post(conversation, text string) (string, error) {
	return a.post(conversation, text, nil)
}

// post is Post with the card's buttons: actions, when there are any, are
// drawn under the text on the card, and dropped with it when the activity
// goes out as plain text (teamsFitActivityWith).
func (a *TeamsAdapter) post(conversation, text string, actions []map[string]any) (string, error) {
	convID := teamsConversationFromID(conversation)
	if convID == "" {
		return "", fmt.Errorf("teams: malformed conversation id %q", conversation)
	}
	serviceURL := a.serviceURLFor(convID)
	endpoint := teamsConversationsPath + convID + teamsActivitiesSuffix
	activity := teamsFitActivityWith(text, a.adaptiveCards, teamsMaxActivityBytes, actions)
	payload, err := json.Marshal(map[string]any{
		"serviceUrl": serviceURL, "endpoint": endpoint, "method": http.MethodPost, "payload": activity,
	})
	if err != nil {
		return "", fmt.Errorf("teams: encoding activity: %w", err)
	}
	raw, err := a.apiPost(context.Background(), http.MethodPost, endpoint, payload)
	if err != nil {
		return "", err
	}
	var body struct {
		Response struct {
			ID string `json:"id"`
		} `json:"response"`
	}
	if json.Unmarshal(raw, &body) == nil {
		return body.Response.ID, nil
	}
	return "", nil
}

// PostNotify is the chat.notify route's half (notifyPoster): text into the
// conversation thread names, else into space, both Teams conversation ids;
// landed is that conversation, which is also the key of the session a
// reply to it joins.
func (a *TeamsAdapter) PostNotify(space, thread, text string) (message, landed string, err error) {
	conversation := strings.TrimSpace(thread)
	if conversation == "" {
		conversation = strings.TrimSpace(space)
	}
	if !teamsIsConversationID(conversation) {
		return "", "", fmt.Errorf("teams: not a conversation id: %q", conversation)
	}
	message, err = a.Post(teamsConversationID(conversation), text)
	if err != nil {
		return "", "", err
	}
	// A top-level post in a channel starts a thread Teams addresses as the
	// channel id with the post's activity id; later chunks and a person's
	// reply under it key that thread, so that is where it landed. A thread
	// post, a post into a home that is itself a thread, and a post in a group
	// or personal chat (which Teams does not thread) landed where they were
	// sent.
	if strings.TrimSpace(thread) == "" && message != "" && teamsIsChannelID(conversation) {
		return message, conversation + teamsThreadToken + message, nil
	}
	return message, conversation, nil
}

// teamsIsConversationID reports a Teams conversation id: one token with no
// whitespace, in one of the two shapes Teams issues, so a Chat space name or
// a Slack channel id pasted into teams.homeChannel is refused here rather
// than at every post.
func teamsIsConversationID(id string) bool {
	if id == "" || strings.ContainsAny(id, teamsConversationRefused) || strings.IndexFunc(id, unicode.IsSpace) >= 0 {
		return false
	}
	for _, prefix := range []string{teamsGroupConversationPrefix, teamsPersonalConversationPrefix} {
		if body, ok := strings.CutPrefix(id, prefix); ok && body != "" {
			return true
		}
	}
	return false
}

// teamsIsHomeConversationID is teamsIsConversationID for teams.homeChannel:
// a channel, group or personal chat, never a thread. Every message in a
// thread shares one key, so a thread home would have the relay store every
// report under it and the bridge splice the last one into every turn there;
// the operator applies the same rule (a2aTeamsHomeConversation).
func teamsIsHomeConversationID(id string) bool {
	return teamsIsConversationID(id) && !teamsHasThreadToken(id)
}

// teamsHasThreadToken reports a thread suffix in any spelling: Bot Framework
// writes ";messageid=", Microsoft's deep links and Graph spell it
// "messageId", and an operator may paste either into teams.homeChannel.
func teamsHasThreadToken(id string) bool {
	return strings.Contains(strings.ToLower(id), teamsThreadToken)
}

// teamsIsChannelID reports a channel conversation id with no thread suffix:
// the one place a top-level post starts a thread.
func teamsIsChannelID(id string) bool {
	return (strings.HasSuffix(id, teamsChannelSuffix) || strings.HasSuffix(id, teamsLegacyChannelSuffix)) &&
		!teamsHasThreadToken(id)
}

// teamsThreadInHome reports a thread the home conversation holds: the home
// itself, or a channel thread of it (home + ";messageid=…").
//
// The shape is checked here, where Notifier.validate reads it, not only on
// the worker: a padded or malformed thread accepted here is queued, and a
// padded one is keyed untrimmed, so every later update misses the stored
// message id and posts anew instead of editing. Only a channel threads, so
// a thread suffix on a personal or group chat is refused too.
func teamsThreadInHome(thread, home string) bool {
	if thread == home {
		return true
	}
	if !teamsIsChannelID(home) || !teamsIsConversationID(thread) {
		return false
	}
	rest, ok := strings.CutPrefix(thread, home+teamsThreadToken)
	return ok && rest != "" && !teamsHasThreadToken(rest)
}

// Edit replaces a posted activity with text (Adapter.Edit): Bot Framework's
// update, a PUT of the activity at its own id through the broker's
// passthrough, which admits that one PUT path. The rolling status line
// edits its message with it as artifacts arrive.
func (a *TeamsAdapter) Edit(conversation, messageID, text string) error {
	return a.edit(conversation, messageID, text, nil)
}

// edit is Edit with the card's buttons (post): an update replaces the whole
// card, so an edit that names no actions removes the ones the message had.
func (a *TeamsAdapter) edit(conversation, messageID, text string, actions []map[string]any) error {
	convID := teamsConversationFromID(conversation)
	if convID == "" {
		return fmt.Errorf("teams: malformed conversation id %q", conversation)
	}
	if strings.TrimSpace(messageID) == "" {
		return fmt.Errorf("teams: edit names no activity")
	}
	activity := teamsFitActivityWith(text, a.adaptiveCards, teamsMaxActivityBytes, actions)
	activity["id"] = messageID
	// No summary on an update: Teams renders an updated activity that carries
	// one as that text, in a plain bubble, and drops the card (seen on every
	// edited line in the 11-17 live test; the same card PUT without the field
	// kept its frame, with it lost it). The preview the summary feeds is the
	// chat list's, which the original post already set.
	delete(activity, "summary")
	endpoint := teamsConversationsPath + convID + teamsActivitiesSuffix + "/" + messageID
	payload, err := json.Marshal(map[string]any{
		"serviceUrl": a.serviceURLFor(convID),
		"endpoint":   endpoint,
		"method":     http.MethodPut,
		"payload":    activity,
	})
	if err != nil {
		return fmt.Errorf("teams: encoding activity update: %w", err)
	}
	_, err = a.apiPost(context.Background(), http.MethodPut, endpoint, payload)
	return err
}

// PostMessage and EditMessage post text only: a chat message falls back to
// its text (ErrTextOnly), rendered as an Adaptive Card by Post. A running
// task's line (m.Stop) carries the Stop button on that card
// (teams_press.go); an edit that names no task removes it.
func (a *TeamsAdapter) PostMessage(conversation string, m OutMessage) (string, error) {
	if m.Chat == nil && m.Stop != "" {
		return a.post(conversation, m.Text, teamsStopActions(m.Stop))
	}
	return postTextOnly(a.Post, conversation, m)
}

func (a *TeamsAdapter) EditMessage(conversation, messageID string, m OutMessage) error {
	if m.Chat == nil && m.Stop != "" {
		return a.edit(conversation, messageID, m.Text, teamsStopActions(m.Stop))
	}
	return editTextOnly(a.Edit, conversation, messageID, m)
}

// EditNotifyMessage is EditMessage for the chat.notify route's home posts
// (notifyChatEditor): the activity messageID in the thread when one is
// named, else in home, edited in place, so a keyed home post (an alert its
// triage updates) stays one message rather than one per update.
func (a *TeamsAdapter) EditNotifyMessage(home, thread, messageID string, m OutMessage) error {
	conversation := strings.TrimSpace(thread)
	if conversation == "" {
		conversation = strings.TrimSpace(home)
	}
	if !teamsIsConversationID(conversation) {
		return fmt.Errorf("teams: not a conversation id: %q", conversation)
	}
	return a.EditMessage(teamsConversationID(conversation), messageID, m)
}

// Roster reports an incomplete, empty membership: the passthrough serves no
// conversation member read, and guessing would be worse than saying so.
func (a *TeamsAdapter) Roster(string) ([]string, bool, error) { return nil, false, nil }

// OpenDirect is not served (errTeamsNoOpenDirect); everything posts to the
// conversation it came from.
func (a *TeamsAdapter) OpenDirect(string) (string, error) { return "", errTeamsNoOpenDirect }

// AnswersOnLine is true for every Teams conversation (LineAnswerer): the
// adapter draws no reaction and no Working… status, so the task's line is
// the ask's one receipt, and it ends as the hand-off acknowledgement or a
// bare mark rather than "✅ completed" under the question, as on Chat.
func (a *TeamsAdapter) AnswersOnLine(string) bool { return true }

// AnswersWholeOnLine is true for every Teams conversation
// (WholeLineAnswerer): a direct answer that fits one piece is edited onto
// the task's line, so the ask gets one card rather than a ✅ card above a
// second one. The live test of 11-17 showed the pair on every direct answer.
func (a *TeamsAdapter) AnswersWholeOnLine(string) bool { return true }

// PieceSize is teamsPieceBytes for every conversation (PieceSizer).
func (a *TeamsAdapter) PieceSize(string) int { return teamsPieceBytes }

// React and RemoveReaction draw no mark on Teams: a nil no-op, as the
// Adapter contract allows a backend that does not react.
func (a *TeamsAdapter) React(string, string, string) error          { return nil }
func (a *TeamsAdapter) RemoveReaction(string, string, string) error { return nil }

// teamsFitActivity builds the outbound activity for text and keeps it under
// cap bytes: the card as asked, then plain text (a card adds a TextBlock per
// line, so short lines inflate it), then the longest prefix that fits with
// teamsTruncationMarker.
func teamsFitActivity(text string, cards bool, cap int) map[string]any {
	return teamsFitActivityWith(text, cards, cap, nil)
}

// teamsFitActivityWith is teamsFitActivity for a card that carries buttons:
// actions ride the card alone, so the plain-text forms drop them, and a
// reader of such a message stops a task by typing the word.
func teamsFitActivityWith(text string, cards bool, cap int, actions []map[string]any) map[string]any {
	size := func(m map[string]any) int {
		b, _ := json.Marshal(m)
		return len(b)
	}
	if cards {
		if card := teamsCardActivityWith(text, actions); size(card) <= cap {
			return card
		}
	}
	plain := teamsTextActivity(text)
	if size(plain) <= cap {
		return plain
	}
	runes := []rune(text)
	low, high := 0, len(runes)
	for low < high {
		mid := (low + high + 1) / 2
		if size(teamsTextActivity(string(runes[:mid])+teamsTruncationMarker)) <= cap {
			low = mid
		} else {
			high = mid - 1
		}
	}
	return teamsTextActivity(string(runes[:low]) + teamsTruncationMarker)
}

// teamsTextActivity is a plain-text message activity.
func teamsTextActivity(text string) map[string]any {
	return map[string]any{"type": teamsActivityMessage, "text": text}
}

// teamsCardActivity is a message activity carrying one Adaptive Card, with
// the plain summary chat lists and notifications preview (without one Teams
// shows "Card - access it on cards.unsupported").
func teamsCardActivity(text string) map[string]any {
	return teamsCardActivityWith(text, nil)
}

// teamsCardActivityWith is teamsCardActivity with the card's buttons, under
// the card's own actions key; none leaves the card as it was.
func teamsCardActivityWith(text string, actions []map[string]any) map[string]any {
	card := teamsMarkdownToCard(text)
	if len(actions) > 0 {
		card[teamsCardActionsKey] = actions
	}
	return map[string]any{
		"type":    teamsActivityMessage,
		"summary": teamsActivitySummary(text),
		"attachments": []map[string]any{{
			"contentType": teamsCardContentType,
			"content":     card,
		}},
	}
}

// teamsActivitySummary is one line of plain text for a card's preview.
func teamsActivitySummary(text string) string {
	flat := strings.Join(strings.Fields(text), " ")
	for _, mark := range teamsSummaryMarks {
		flat = strings.ReplaceAll(flat, mark, "")
	}
	words := []string{}
	for _, w := range strings.Fields(flat) {
		if strings.Trim(w, "-:") != "" {
			words = append(words, w)
		}
	}
	flat = strings.Join(words, " ")
	if flat == "" {
		// Nothing but marks (a lone delimiter row): the preview is the
		// text's first line as written, never a sentence of the renderer's.
		for _, line := range strings.Split(text, "\n") {
			if line = strings.TrimSpace(line); line != "" {
				flat = line
				break
			}
		}
	}
	if runes := []rune(flat); len(runes) > teamsSummaryMax {
		flat = strings.TrimRight(string(runes[:teamsSummaryMax]), " ")
	}
	return flat
}

// teamsTableStart reports whether header and the line after it open a
// pipe table by GitHub's rule: the next line is a delimiter row (one or
// more dashes per cell, optional alignment colons; slackTableSepRE) that
// carries a pipe, as Slack's scanner requires, and its cell count is the
// header's. A line followed by a bare thematic break is text, whatever
// pipes it carries.
func teamsTableStart(header, next string) bool {
	next = strings.TrimSpace(next)
	if !strings.Contains(next, teamsPipe) || !slackTableSepRE.MatchString(next) {
		return false
	}
	return len(teamsTableCells(next)) == len(teamsTableCells(header))
}

// teamsOpensFence reports the length of an opening code fence (CommonMark):
// a run of three or more backticks followed by an info string with no
// backtick in it, so a line that opens and closes an inline triple-backtick
// span is a paragraph, not a block boundary. Zero when the line is no fence.
func teamsOpensFence(stripped string) int {
	n := teamsBacktickRun(stripped)
	if n < len(teamsCodeFence) || strings.Contains(stripped[n:], teamsCodeTick) {
		return 0
	}
	return n
}

// teamsClosesFence reports a closing fence for a block opened by a run of
// open backticks: at least as many backticks and nothing after them
// (CommonMark), so an info-string fence inside the block is content, and a
// three-backtick line does not close a four-backtick block.
func teamsClosesFence(stripped string, open int) bool {
	n := teamsBacktickRun(stripped)
	return n >= open && n == len(stripped)
}

// teamsBacktickRun is the length of the backtick run a line starts with.
func teamsBacktickRun(stripped string) int {
	return len(stripped) - len(strings.TrimLeft(stripped, teamsCodeTick))
}

// teamsTableCells is Slack's tableCells (one border pipe off each end, an
// escaped pipe kept in its cell) with one more case: a row that ends in an
// escaped pipe and no border keeps that pipe as content.
func teamsTableCells(line string) []string {
	line = strings.TrimSpace(line)
	if strings.HasSuffix(line, slackEscapedPipe) {
		line += slackTablePipe
	}
	return tableCells(line)
}

// teamsMarkdownToCard converts Markdown text into an Adaptive Card 1.5:
// headings, bullets and numbered lines as TextBlocks, fenced code as a
// monospace block, a pipe table as a Table with its first row the header.
func teamsMarkdownToCard(text string) map[string]any {
	var body []map[string]any
	var code []string
	var table [][]string
	inCode := false
	fenceLen := 0

	textBlock := func(t string, extra map[string]any) map[string]any {
		block := map[string]any{"type": teamsCardTextBlock, "text": t, "wrap": true}
		for k, v := range extra {
			block[k] = v
		}
		return block
	}
	flushTable := func() {
		if len(table) == 0 {
			return
		}
		width := 0
		for _, row := range table {
			if len(row) > width {
				width = len(row)
			}
		}
		columns := make([]map[string]any, width)
		for i := range columns {
			columns[i] = map[string]any{"width": teamsCardColumnWidth}
		}
		rows := make([]map[string]any, 0, len(table))
		for r, row := range table {
			cells := make([]map[string]any, width)
			for i := range cells {
				cell := ""
				if i < len(row) {
					cell = row[i]
				}
				extra := map[string]any{}
				if r == 0 {
					extra["weight"] = teamsCardBold
				}
				cells[i] = map[string]any{"type": teamsCardTableCell, "items": []map[string]any{textBlock(cell, extra)}}
			}
			rows = append(rows, map[string]any{"type": teamsCardTableRow, "cells": cells})
		}
		body = append(body, map[string]any{"type": teamsCardTable, "firstRowAsHeader": true, "columns": columns, "rows": rows})
		table = nil
	}

	lines := strings.Split(text, "\n")
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		stripped := strings.TrimSpace(line)
		if inCode {
			// Only a closing fence of the block's own length ends it; an
			// info-string fence or a shorter run inside it is content.
			if teamsClosesFence(stripped, fenceLen) {
				inCode, fenceLen = false, 0
				body = append(body, textBlock(strings.Join(code, "\n"), map[string]any{"fontType": teamsCardMonospace}))
				code = nil
				continue
			}
			code = append(code, line)
			continue
		}
		if n := teamsOpensFence(stripped); n > 0 {
			// A fence opens a block before any table rule runs, so a fence
			// line with a pipe in its info string is never a row.
			flushTable()
			inCode, fenceLen = true, n
			code = nil
			continue
		}
		if strings.Contains(stripped, teamsPipe) {
			switch {
			case len(table) > 0:
				// A row of the running table. Every row after the delimiter
				// is data (GitHub's rule, Slack's tableEnd), so a body row of
				// dashes, an empty entry, is kept.
				table = append(table, teamsTableCells(stripped))
				continue
			case i+1 < len(lines) && teamsTableStart(stripped, lines[i+1]):
				// A table starts only where a header row is followed by a
				// delimiter row (GitHub's rule); a lone "|x| + |y|" line is text.
				table = append(table, teamsTableCells(stripped))
				i++
				continue
			}
		}
		flushTable()
		if stripped == "" {
			continue
		}
		if strings.HasPrefix(stripped, teamsHeadingMark) {
			rest := strings.TrimLeft(stripped, teamsHeadingMark)
			if strings.HasPrefix(rest, teamsHeadingGap) {
				size := teamsCardSizeMedium
				if len(stripped)-len(rest) == 1 {
					size = teamsCardSizeLarge
				}
				body = append(body, textBlock(strings.TrimSpace(rest), map[string]any{"weight": teamsCardBold, "size": size}))
				continue
			}
		}
		if strings.HasPrefix(stripped, teamsBulletDash) || strings.HasPrefix(stripped, teamsBulletStar) {
			body = append(body, textBlock(teamsCardBullet+strings.TrimSpace(stripped[len(teamsBulletDash):]), nil))
			continue
		}
		body = append(body, textBlock(stripped, nil))
	}
	flushTable()
	if inCode && len(code) > 0 {
		body = append(body, textBlock(strings.Join(code, "\n"), map[string]any{"fontType": teamsCardMonospace}))
	}
	if len(body) == 0 {
		body = []map[string]any{textBlock(text, nil)}
	}
	return map[string]any{
		"type":    teamsCardType,
		"$schema": teamsCardSchema,
		"version": teamsCardVersion,
		"msteams": map[string]any{"width": teamsCardWidth},
		"body":    body,
	}
}
