package gateway

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// gchatRelayAPIPath is the credential proxy's Chat API passthrough — the
	// same route the legacy chat caller uses, so the destructive-method
	// denylist and error scrubbing hold for this adapter without new code.
	gchatRelayAPIPath = "/v1/chat/api"
	// gchatReplyOption makes a threaded create degrade to a new thread when
	// the referenced thread cannot take replies, instead of failing the post.
	gchatReplyOption = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
	// gchatMemberPageSize is one page of the members list; the roster is
	// complete only when Chat reports no further page, same one-page posture
	// as the Discord adapter.
	gchatMemberPageSize = 100
	// gchatRelayTimeout bounds one relay round trip. The relay's pull runs
	// a Pub/Sub pull with a 20s retry deadline whose worst case overruns
	// it, and the legacy client allows 35s for the same call; a client that
	// gives up before the relay answers strands a pulled message behind a
	// receipt nobody holds until the ack deadline redelivers it, so this
	// sits well above both.
	gchatRelayTimeout = 45 * time.Second
	// gchatRelayEventsPath is the A2A-dedicated event route — its own
	// GoogleChatRelay instance on its own subscription, so this consumer
	// never splits deliveries with the legacy chat path.
	gchatRelayEventsPath    = "/v1/chat/a2a/events"
	gchatRelayEventsAckPath = "/v1/chat/a2a/events/ack"
	// gchatPullRetryDelay paces re-polls after a pull error, so a relay
	// outage logs a warning a second rather than a thousand.
	gchatPullRetryDelay = 2 * time.Second
	// gchatPullIdleDelay paces re-polls after an EMPTY pull. The relay's
	// server-side long poll usually paces for free, but a synchronous
	// Pub/Sub pull may legitimately return nothing before its deadline, and
	// each early-empty return would otherwise be an instant extra round
	// trip through the proxy.
	gchatPullIdleDelay = 500 * time.Millisecond
	// gchatSeenCap bounds the redelivery-dedupe memory. Pub/Sub's
	// redelivery window is bounded (ack deadline and retention), so an
	// unbounded map would only ever be a leak, one entry per message for
	// the pod's lifetime.
	gchatSeenCap = 4096
)

const (
	// gchatBackend names the backend in authority blocks and config.
	gchatBackend = "gchat"
	// gchatVerifiedBy names what ingress verification actually checked: the
	// event arrived from a subscription on the topic whose only permitted
	// publishers are Google's Chat service accounts, and Google Chat
	// asserted the sender email after authenticating the user's session.
	// NOT a per-request signed token — none exists on the Pub/Sub shape
	// (spec-chatops-gateway.md, "The Google Chat adapter").
	gchatVerifiedBy = "chat-event-topic-iam"
)

const (
	gchatShapeLegacy = "legacy"
	gchatShapeAddon  = "addon"
	// gchatAddonMessageKey is the chat.* payload key that marks a message
	// interaction in the add-on event object.
	gchatAddonMessageKey = "messagePayload"
	// gchatAddonUserKey is the interacting user in the add-on event object,
	// alongside message.sender; both name the same person.
	gchatAddonUserKey      = "user"
	gchatAddonEventTimeKey = "eventTime"
)

// Conversation key prefixes for the Google Chat adapter. A space is not a
// session; a conversation in it is — and what counts as the conversation
// depends on the surface: a thread in a threaded space, the whole space in a
// DM or in a space whose threading state does not support replies
// (spec-chatops-gateway.md, "The Google Chat adapter").
const (
	gchatKeyPrefix      = "gchat:"
	gchatDMKeyPrefix    = "gchat:dm/"
	gchatSpaceKeyPrefix = "gchat:space/"
	gchatSpacesToken    = "spaces/"
	gchatThreadsToken   = "/threads/"
	// gchatUsersToken prefixes a user resource name ("users/{id}").
	gchatUsersToken = "users/"
)

// Google Chat's own vocabulary, as it appears in event payloads and API
// responses.
const (
	gchatEventTypeMessage    = "MESSAGE"
	gchatSenderHuman         = "HUMAN"
	gchatSenderBot           = "BOT"
	gchatSpaceTypeDM         = "DIRECT_MESSAGE"
	gchatLegacySpaceTypeDM   = "DM"
	gchatSpaceTypeGroupChat  = "GROUP_CHAT"
	gchatThreadingUnthreaded = "UNTHREADED_MESSAGES"
)

// verifiedByFor names the mechanism that checked the requester at ingress
// for one backend (authority.requester.verifiedBy).
func verifiedByFor(backend string) string {
	if backend == gchatBackend {
		return gchatVerifiedBy
	}
	return "principal-map"
}

// unverifiedRemedyFor names what an admin edits to admit a sender — the
// allowlist on gchat, the mapping table everywhere else.
func unverifiedRemedyFor(backend string) string {
	if backend == gchatBackend {
		return "the allowed users list"
	}
	return "the principal map"
}

// gchatLinkRe rewrites markdown links to Chat's <url|text> form.
var gchatLinkRe = regexp.MustCompile(`\[([^\]]+)\]\((https?://[^)\s]+)\)`)

// gchatSpace, gchatSender and gchatMessage are the Chat resources both event
// shapes carry; only the fields the adapter reads are declared.
type gchatSpace struct {
	Name string `json:"name"`
	// Type is the legacy field ("ROOM"/"DM"); SpaceType its successor
	// ("SPACE"/"GROUP_CHAT"/"DIRECT_MESSAGE"). Events have carried either
	// depending on API vintage, so both are read.
	Type                string `json:"type"`
	SpaceType           string `json:"spaceType"`
	SpaceThreadingState string `json:"spaceThreadingState"`
}

type gchatSender struct {
	Name        string `json:"name"`
	DisplayName string `json:"displayName"`
	Email       string `json:"email"`
	Type        string `json:"type"`
}

type gchatMessage struct {
	Name string `json:"name"`
	Text string `json:"text"`
	// ArgumentText is the message text with the app mention stripped —
	// Chat computes it, so there is no mention grammar to re-derive. A
	// pointer because present-but-empty is meaningful: it is Google's
	// documented shape for a mention-only message in a space, and
	// collapsing it into absent would resurrect the raw mention as the
	// ask. (In a DM, measured live, Chat sends argumentText equal to text
	// with nothing stripped — a typed "@app" there is plain text to Chat
	// too, with no mention annotation.)
	ArgumentText *string `json:"argumentText"`
	Thread       struct {
		Name string `json:"name"`
	} `json:"thread"`
	Sender gchatSender `json:"sender"`
}

// gchatEvent is one Chat event normalized to the legacy field layout: Type
// is "MESSAGE" for a turn, Space and Message are the resources. Two wire
// shapes decode into it (decodeGchatEvent):
//
//   - the legacy Chat-API event, {"type":"MESSAGE","space":…,"message":…} —
//     what tests/e2e/gchat_agent_test.py forges and what the docs called
//     DeprecatedEvent;
//   - the Google Workspace add-on event object, {"commonEventObject":…,
//     "chat":{"user":…,"eventTime":…,"messagePayload":{"space":…,
//     "message":…}}} — what a Chat app configured through the add-on
//     surface publishes. It has no top-level type at all; the interaction
//     kind is which payload key is present. Measured live 2026-09-09: every
//     event from the app in bnaylor-kagents-dev arrived in this shape, and
//     an adapter reading only the legacy one acked all of them away.
type gchatEvent struct {
	Type    string
	Space   gchatSpace
	Message gchatMessage
	// shape names what was decoded, for the drop log: "legacy", "addon",
	// or, for an add-on event carrying some other payload, that payload's
	// key(s).
	shape string
}

// gchatWireEvent is the union of both wire shapes as JSON sees them.
type gchatWireEvent struct {
	Type    string                     `json:"type"`
	Space   gchatSpace                 `json:"space"`
	Message gchatMessage               `json:"message"`
	Chat    map[string]json.RawMessage `json:"chat"`
}

// gchatAddonMessagePayload is chat.messagePayload of the add-on event object.
type gchatAddonMessagePayload struct {
	Space   gchatSpace   `json:"space"`
	Message gchatMessage `json:"message"`
}

// GoogleChatAdapter implements Adapter over the credential proxy's chat
// relay: events arrive by long-polling the relay's A2A event routes, and
// posts, edits, roster reads and openDirect ride its Chat API passthrough.
// The adapter holds no cloud credential — it authenticates to the relay with
// the pod's projected ServiceAccount token, and the one Chat credential in
// the deployment stays in the credential proxy.
type GoogleChatAdapter struct {
	relayURL  string
	tokenPath string
	client    *http.Client
	log       *slog.Logger

	mu sync.Mutex
	// seen dedupes inbound messages by resource name: Pub/Sub is
	// at-least-once, and a redelivered event must not become a second turn.
	// seenOrder is its eviction queue, bounded at gchatSeenCap.
	seen      map[string]bool
	seenOrder []string
	// userIDs maps a sender's immutable users/{id} resource name to the
	// email Chat asserted for them, learned from inbound events. Under app
	// credentials Chat withholds member emails from spaces.members.list
	// and refuses the email alias in user resource names (measured live,
	// 2026-09-09), so this is the only bridge between the roster's ids and
	// the principal the requester was verified as. userEmails is the
	// inverse, for OpenDirect.
	userIDs    map[string]string
	userEmails map[string]string
	// dmThreads remembers, per DM space, the thread of the latest inbound
	// message. A DM is one session for the whole space (the key never
	// carries a thread), but a DM space is threaded and a reply belongs
	// where the ask was made — measured live: without this the answer to
	// a question asked inside a thread lands top-level in the DM. This is
	// presentation, not identity: the conversation key and the session
	// are unchanged by it.
	dmThreads map[string]string
}

// NewGoogleChatAdapter builds the adapter against the credential proxy's
// relay base URL. tokenPath is the pod's projected ServiceAccount token
// (a2a-chat audience); it is read per request because the kubelet rotates it.
func NewGoogleChatAdapter(relayURL, tokenPath string, log *slog.Logger) (*GoogleChatAdapter, error) {
	if relayURL == "" {
		return nil, fmt.Errorf("gchat: relay URL is required")
	}
	if tokenPath == "" {
		return nil, fmt.Errorf("gchat: relay token path is required")
	}
	return &GoogleChatAdapter{
		relayURL:   strings.TrimSuffix(relayURL, "/"),
		tokenPath:  tokenPath,
		client:     &http.Client{Timeout: gchatRelayTimeout},
		log:        log,
		seen:       map[string]bool{},
		userIDs:    map[string]string{},
		userEmails: map[string]string{},
		dmThreads:  map[string]string{},
	}, nil
}

// apiCall runs one Chat API method through the relay passthrough and decodes
// the response body into out when out is non-nil.
func (a *GoogleChatAdapter) apiCall(resource []string, method string, arguments map[string]any, out any) error {
	payload, err := json.Marshal(map[string]any{
		"resource": resource, "method": method, "arguments": arguments,
	})
	if err != nil {
		return fmt.Errorf("gchat: encoding %s.%s: %w", strings.Join(resource, "."), method, err)
	}
	resp, err := a.relayPost(gchatRelayAPIPath, payload)
	if err != nil {
		return err
	}
	var body struct {
		Response json.RawMessage `json:"response"`
	}
	if err := json.Unmarshal(resp, &body); err != nil {
		return fmt.Errorf("gchat: decoding relay response: %w", err)
	}
	if out != nil && len(body.Response) > 0 {
		if err := json.Unmarshal(body.Response, out); err != nil {
			return fmt.Errorf("gchat: decoding %s.%s response: %w", strings.Join(resource, "."), method, err)
		}
	}
	return nil
}

// authorize sets the relay bearer token on one request. The token is re-read
// on every call: it is a projected ServiceAccount token the kubelet rotates.
func (a *GoogleChatAdapter) authorize(req *http.Request) error {
	token, err := os.ReadFile(a.tokenPath)
	if err != nil {
		return fmt.Errorf("gchat: reading relay token: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	return nil
}

// relayPost is one authenticated POST to the relay.
func (a *GoogleChatAdapter) relayPost(path string, payload []byte) ([]byte, error) {
	req, err := http.NewRequest(http.MethodPost, a.relayURL+path, bytes.NewReader(payload))
	if err != nil {
		return nil, fmt.Errorf("gchat: building relay request: %w", err)
	}
	if err := a.authorize(req); err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("gchat: relay request: %w", err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	if _, err := buf.ReadFrom(resp.Body); err != nil {
		return nil, fmt.Errorf("gchat: reading relay response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		// The relay scrubs Chat error bodies before they get here; carrying
		// the status forward is enough to tell a refusal from an outage.
		return nil, fmt.Errorf("gchat: relay %s answered %d", path, resp.StatusCode)
	}
	return buf.Bytes(), nil
}

// Post writes text to a conversation and returns the created message's
// resource name (Adapter.Post).
func (a *GoogleChatAdapter) Post(conversation, text string) (string, error) {
	space, thread, ok := gchatSpaceThread(conversation)
	if !ok {
		return "", fmt.Errorf("gchat: not a gchat conversation: %q", conversation)
	}
	body := map[string]any{"text": toGchatText(text)}
	arguments := map[string]any{"parent": space, "body": body}
	if thread == "" && strings.HasPrefix(conversation, gchatDMKeyPrefix) {
		a.mu.Lock()
		thread = a.dmThreads[space]
		a.mu.Unlock()
	}
	if thread != "" {
		body["thread"] = map[string]any{"name": thread}
		arguments["messageReplyOption"] = gchatReplyOption
	}
	var created struct {
		Name string `json:"name"`
	}
	if err := a.apiCall([]string{"spaces", "messages"}, "create", arguments, &created); err != nil {
		return "", err
	}
	return created.Name, nil
}

// Edit replaces the text of a previously posted message (Adapter.Edit) — the
// rolling progress line edits one message as artifacts arrive.
func (a *GoogleChatAdapter) Edit(conversation, messageID, text string) error {
	if _, _, ok := gchatSpaceThread(conversation); !ok {
		return fmt.Errorf("gchat: not a gchat conversation: %q", conversation)
	}
	arguments := map[string]any{
		"name":       messageID,
		"updateMask": "text",
		"body":       map[string]any{"text": toGchatText(text)},
	}
	return a.apiCall([]string{"spaces", "messages"}, "patch", arguments, nil)
}

// Roster returns one page of the space's membership (Adapter.Roster):
// emails where the backend surfaced one — they resolve straight to
// principals — the immutable users/ id where it did not, never the app
// itself. Complete only when Chat reports no further page.
func (a *GoogleChatAdapter) Roster(conversation string) ([]string, bool, error) {
	space, _, ok := gchatSpaceThread(conversation)
	if !ok {
		return nil, false, fmt.Errorf("gchat: not a gchat conversation: %q", conversation)
	}
	var out struct {
		Memberships []struct {
			Member struct {
				Name  string `json:"name"`
				Email string `json:"email"`
				Type  string `json:"type"`
			} `json:"member"`
		} `json:"memberships"`
		NextPageToken string `json:"nextPageToken"`
	}
	arguments := map[string]any{"parent": space, "pageSize": gchatMemberPageSize}
	if err := a.apiCall([]string{"spaces", "members"}, "list", arguments, &out); err != nil {
		return nil, false, err
	}
	var ids []string
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, m := range out.Memberships {
		if m.Member.Type == gchatSenderBot {
			continue
		}
		id := m.Member.Email
		if id == "" {
			// App credentials get no email here (measured live), so a
			// member who has spoken in any conversation resolves through
			// the id learned from their event — the same string the
			// requester was verified as, so the audience hash joins the
			// requester hash. A member who has never spoken stays an id.
			id = a.userIDs[m.Member.Name]
		}
		if id == "" {
			id = m.Member.Name
		}
		if id != "" {
			ids = append(ids, id)
		}
	}
	return ids, out.NextPageToken == "", nil
}

// OpenDirect returns the DM conversation for a user (Adapter.OpenDirect).
// userID is the Google-asserted email; the Chat API accepts the email alias
// in user resource names.
func (a *GoogleChatAdapter) OpenDirect(userID string) (string, error) {
	name := userID
	if !strings.HasPrefix(name, gchatUsersToken) {
		// The email alias in a user resource name is accepted only under
		// user credentials; app credentials, which is what the relay
		// holds, answer it 403 (measured live). A sender whose event has
		// been seen resolves to the immutable id Chat does accept; anyone
		// else is tried by alias, and the error says which was refused.
		a.mu.Lock()
		if id, ok := a.userEmails[strings.ToLower(userID)]; ok {
			name = id
		} else {
			name = gchatUsersToken + userID
		}
		a.mu.Unlock()
	}
	user := map[string]any{"name": name}
	var space struct {
		Name string `json:"name"`
	}
	err := a.apiCall([]string{"spaces"}, "findDirectMessage", user, &space)
	if err != nil {
		// No DM space yet (or the lookup was refused): ask Chat to set one
		// up between the app and the user.
		setupErr := a.apiCall([]string{"spaces"}, "setup", map[string]any{
			"body": map[string]any{
				"space": map[string]any{"spaceType": gchatSpaceTypeDM, "singleUserBotDm": true},
				"memberships": []any{
					map[string]any{"member": map[string]any{"name": name, "type": gchatSenderHuman}},
				},
			},
		}, &space)
		if setupErr != nil {
			return "", fmt.Errorf("gchat: openDirect: find failed (%v) and setup failed: %w", err, setupErr)
		}
	}
	if space.Name == "" {
		return "", fmt.Errorf("gchat: openDirect returned no space")
	}
	return gchatDMKeyPrefix + space.Name, nil
}

// gchatEnvelope is one pulled event as the relay wraps it: an opaque receipt
// for settling, and the Chat event JSON base64-encoded in data, the Pub/Sub
// message shape.
type gchatEnvelope struct {
	Receipt   string `json:"receipt"`
	Data      string `json:"data"`
	MessageID string `json:"messageId"`
}

// Run long-polls the relay's A2A event route and delivers turns to handler
// until ctx is done (Adapter.Run). Every pulled event is acked once its
// disposition is known — including a payload that does not parse: the legacy
// seam's recorded hole is a poison message that is never settled and
// redelivers forever (tests/integration/test_seam_chat_ingress.py), and an
// acked-away poison event beats an inbox wedged on one.
func (a *GoogleChatAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		env, err := a.pullEvent(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			a.log.Warn("gchat event pull failed", "err", err)
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(gchatPullRetryDelay):
			}
			continue
		}
		if env == nil {
			// Usually the server-side long poll has already paced this;
			// the delay only bites on an early-empty synchronous pull.
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(gchatPullIdleDelay):
			}
			continue
		}
		ev, decodeErr := decodeGchatEvent(env.Data)
		// Acked before the handler runs, which makes ingress at-most-once —
		// a deliberate, recorded decision, not an oversight. Acking after a
		// durable publish would be at-least-once, but the dedupe map is
		// in-memory, so a redelivery after a slow publish and a restart
		// becomes a DUPLICATE task — a worse failure than a lost ask,
		// which a user retries by typing again. It also matches every
		// other backend's ingress semantics: Discord and Slack websockets
		// redeliver nothing at all.
		a.settle(env.Receipt)
		if decodeErr != nil {
			a.log.Warn("gchat event payload did not parse; acked away",
				"pubsubMessageId", env.MessageID, "err", decodeErr)
			continue
		}
		msg, reason := a.classify(ev)
		if reason != "" {
			a.log.Info("gchat event is not a turn; acked",
				"pubsubMessageId", env.MessageID, "reason", reason)
			continue
		}
		handler(msg)
	}
}

// pullEvent asks the relay for one event; nil with no error means the poll
// came back empty.
func (a *GoogleChatAdapter) pullEvent(ctx context.Context) (*gchatEnvelope, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.relayURL+gchatRelayEventsPath, nil)
	if err != nil {
		return nil, fmt.Errorf("gchat: building pull request: %w", err)
	}
	if err := a.authorize(req); err != nil {
		return nil, err
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("gchat: event pull: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("gchat: event pull answered %d", resp.StatusCode)
	}
	var body struct {
		Event *gchatEnvelope `json:"event"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, fmt.Errorf("gchat: decoding pulled event: %w", err)
	}
	return body.Event, nil
}

// settle acks one pulled event. Failing to ack only means a redelivery the
// dedupe map absorbs, so the error is logged rather than returned.
func (a *GoogleChatAdapter) settle(receipt string) {
	payload, err := json.Marshal(map[string]string{"receipt": receipt})
	if err != nil {
		a.log.Warn("gchat ack encode failed", "err", err)
		return
	}
	if _, err := a.relayPost(gchatRelayEventsAckPath, payload); err != nil {
		a.log.Warn("gchat ack failed", "err", err)
	}
}

// decodeGchatEvent unwraps the base64 Pub/Sub payload into the Chat event,
// accepting either wire shape (gchatEvent).
func decodeGchatEvent(data string) (*gchatEvent, error) {
	raw, err := base64.StdEncoding.DecodeString(data)
	if err != nil {
		return nil, fmt.Errorf("base64: %w", err)
	}
	var wire gchatWireEvent
	if err := json.Unmarshal(raw, &wire); err != nil {
		return nil, fmt.Errorf("event json: %w", err)
	}
	if wire.Chat == nil {
		return &gchatEvent{Type: wire.Type, Space: wire.Space, Message: wire.Message, shape: gchatShapeLegacy}, nil
	}
	if payload, ok := wire.Chat[gchatAddonMessageKey]; ok {
		var mp gchatAddonMessagePayload
		if err := json.Unmarshal(payload, &mp); err != nil {
			return nil, fmt.Errorf("chat.messagePayload: %w", err)
		}
		ev := &gchatEvent{Type: gchatEventTypeMessage, Space: mp.Space, Message: mp.Message, shape: gchatShapeAddon}
		if ev.Message.Sender.Email == "" {
			// chat.user and message.sender name the same person; the
			// sender is what the legacy layout reads, so fill it from the
			// user only when the payload omitted it.
			if user, ok := wire.Chat[gchatAddonUserKey]; ok {
				_ = json.Unmarshal(user, &ev.Message.Sender)
			}
		}
		return ev, nil
	}
	// An add-on event carrying some other interaction: name its payload
	// keys so the drop log says what arrived rather than "not a MESSAGE".
	keys := make([]string, 0, len(wire.Chat))
	for k := range wire.Chat {
		if k != gchatAddonUserKey && k != gchatAddonEventTimeKey {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return &gchatEvent{shape: gchatShapeAddon + ":" + strings.Join(keys, ",")}, nil
}

// toGchatText translates executor markdown to Google Chat's text format and
// defangs the one control sequence Chat parses out of message text: a
// <users/…> mention, which a prompt-injected result could use to ping the
// room. The defusing is a visible space, not an invisible character.
func toGchatText(s string) string {
	s = strings.ReplaceAll(s, "<users/", "< users/")
	s = gchatLinkRe.ReplaceAllString(s, "<$2|$1>")
	return strings.ReplaceAll(s, "**", "*")
}

// classify normalizes one Chat event to an InboundMessage, or names why it
// is not a turn. Google Chat itself gates delivery (an app receives a space
// message only when mentioned, and every DM), so what is owned here is the
// surface binding and the drops. A non-turn is
// ordinary (a membership event, a bot's own message, a redelivery) but it
// must never be silent: an adapter that acks every event and says nothing
// is indistinguishable from one that receives none — which is exactly how
// the add-on wire shape went unnoticed until live traffic.
func (a *GoogleChatAdapter) classify(ev *gchatEvent) (InboundMessage, string) {
	if ev.Type != gchatEventTypeMessage {
		return InboundMessage{}, "not a message event (shape " + ev.shape + ", type " + strconv.Quote(ev.Type) + ")"
	}
	sender := ev.Message.Sender
	if sender.Type != gchatSenderHuman {
		return InboundMessage{}, "sender is not HUMAN (" + sender.Type + ")"
	}
	if sender.Email == "" {
		return InboundMessage{}, "sender has no email"
	}
	if ev.Space.Name == "" || ev.Message.Name == "" {
		return InboundMessage{}, "event names no space or no message"
	}
	// ArgumentText is authoritative whenever Chat sent the field: it is the
	// text with the app mention stripped, and a bare mention leaves it
	// EMPTY — Google's documented shape — so falling back to Text on empty
	// would resurrect the mention as the ask. Only a message with no
	// argumentText at all (a DM with no mention) reads Text.
	text := strings.TrimSpace(ev.Message.Text)
	if ev.Message.ArgumentText != nil {
		text = strings.TrimSpace(*ev.Message.ArgumentText)
	}
	if text == "" {
		return InboundMessage{}, "empty ask (bare mention)"
	}

	// A space misread as a DM would bind every thread in it to one session,
	// so DM requires a positive signal; anything unclassifiable is a group.
	kind := "group"
	if ev.Space.SpaceType == gchatSpaceTypeDM || ev.Space.Type == gchatLegacySpaceTypeDM {
		kind = "dm"
	}
	// A GROUP_CHAT surface never supports reply threading, whatever thread
	// name the event carries — in an unthreaded space every message has its
	// own thread resource, and binding those would fragment the group chat
	// into one session per ask.
	thread := ev.Message.Thread.Name
	if ev.Space.SpaceThreadingState == gchatThreadingUnthreaded || ev.Space.SpaceType == gchatSpaceTypeGroupChat {
		thread = ""
	}

	a.mu.Lock()
	if kind == "dm" && ev.Message.Thread.Name != "" {
		if len(a.dmThreads) >= gchatSeenCap {
			a.dmThreads = map[string]string{}
		}
		a.dmThreads[ev.Space.Name] = ev.Message.Thread.Name
	}
	if strings.HasPrefix(sender.Name, gchatUsersToken) {
		// Bounded by the same cap as the dedupe memory: one entry per
		// human who has spoken, evicted wholesale rather than leaked.
		if len(a.userIDs) >= gchatSeenCap {
			a.userIDs = map[string]string{}
			a.userEmails = map[string]string{}
		}
		a.userIDs[sender.Name] = sender.Email
		a.userEmails[strings.ToLower(sender.Email)] = sender.Name
	}
	dup := a.seen[ev.Message.Name]
	if !dup {
		a.seen[ev.Message.Name] = true
		a.seenOrder = append(a.seenOrder, ev.Message.Name)
		// Pub/Sub's redelivery window is bounded, so the dedupe memory is
		// too: evict oldest-first at the cap rather than leaking one entry
		// per message for the pod's lifetime.
		if len(a.seenOrder) > gchatSeenCap {
			delete(a.seen, a.seenOrder[0])
			a.seenOrder = a.seenOrder[1:]
		}
	}
	a.mu.Unlock()
	if dup {
		return InboundMessage{}, "duplicate delivery of " + ev.Message.Name
	}

	return InboundMessage{
		Conversation: gchatConversationID(ev.Space.Name, thread, kind),
		Kind:         kind,
		AuthorID:     sender.Email,
		MessageID:    ev.Message.Name,
		Text:         text,
	}, ""
}

// resolvePrincipal establishes the requester's principal from the backend's
// identity mechanism. On gchat the Google-asserted email IS the principal —
// resolution is the identity function gated by the allowlist (the mapping
// table other backends need is exactly what this backend exists to not
// have). Everything else goes through the principal map. Empty means drop.
func (g *Gateway) resolvePrincipal(authorID string) string {
	if g.backend != gchatBackend {
		return g.pm.Resolve(authorID)
	}
	if g.gchatAllowAll || g.gchatAllowed[strings.ToLower(authorID)] {
		// Returned case-preserved, deliberately: the audit join requires
		// hashing the SAME string the shipped attribution path hashes (the
		// delivered sender email, un-normalized). If Google ever varies the
		// asserted email's case across events, both surfaces fork the same
		// way — lowercasing here would fix nothing and break the join.
		return authorID
	}
	return ""
}

// gchatConversationID mints the session key for one inbound message. space is
// the Chat space resource name ("spaces/AAA"), thread the thread resource name
// ("spaces/AAA/threads/BBB", empty when the surface has none), kind "dm" or
// "group".
func gchatConversationID(space, thread, kind string) string {
	if kind == "dm" {
		return gchatDMKeyPrefix + space
	}
	if thread == "" {
		return gchatSpaceKeyPrefix + space
	}
	return gchatKeyPrefix + thread
}

// gchatSpaceThread inverts gchatConversationID: the space to post into and
// the thread to reply on (empty when the conversation is the whole space).
func gchatSpaceThread(conversation string) (space, thread string, ok bool) {
	rest, found := strings.CutPrefix(conversation, gchatDMKeyPrefix)
	if !found {
		rest, found = strings.CutPrefix(conversation, gchatSpaceKeyPrefix)
	}
	if found {
		if !gchatIsSpaceName(rest) {
			return "", "", false
		}
		return rest, "", true
	}
	rest, found = strings.CutPrefix(conversation, gchatKeyPrefix)
	if !found {
		return "", "", false
	}
	space, threadID, hasThread := strings.Cut(rest, gchatThreadsToken)
	if !hasThread || !gchatIsSpaceName(space) || threadID == "" || strings.Contains(threadID, "/") {
		return "", "", false
	}
	return space, rest, true
}

// gchatIsSpaceName reports whether s is a bare space resource name
// ("spaces/AAA", nothing nested under it).
func gchatIsSpaceName(s string) bool {
	id, found := strings.CutPrefix(s, gchatSpacesToken)
	return found && id != "" && !strings.Contains(id, "/")
}
