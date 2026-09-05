package gateway

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/slack-go/slack/socketmode"
)

// slackDMPrefix marks a DM conversation key. The whole DM is the session,
// like Discord's — "a DM, or a thread in a group space" (gateway design).
const slackDMPrefix = "slack:dm/"

// slackSeenCap bounds the at-least-once dedupe ring: Socket Mode redelivers
// unacked envelopes, so delivered (channel, ts) pairs are remembered and
// re-deliveries dropped. Sized to roughly a busy hour of messages.
const slackSeenCap = 2048

// slackRootsCap bounds the thread-root cache the same way; one entry
// accrues per distinct thread replied to, and a busy workspace should not
// grow the gateway forever.
const slackRootsCap = 2048

// slackTurnSubtypes are the message subtypes that are genuine user turns.
// Plain messages have no subtype; thread_broadcast is a thread reply with
// "also send to channel" checked (dropping it would eat a steer silently),
// and file_share is an ask with an attachment. Everything else — edits,
// deletes, joins, bot_message — is not a turn.
var slackTurnSubtypes = map[string]bool{"": true, "thread_broadcast": true, "file_share": true}

// slackEscaper is Slack's documented escaping for the three characters that
// open control sequences. Relayed text is executor-authored — model output,
// by definition — so without this a prompt-injected result containing
// <!channel> would ping the room.
var slackEscaper = strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;")

// slackRosterPage is one conversations.members page; a channel past it is
// reported rosterComplete=false, not paged — rosterCap (32) truncates far
// below it anyway, and larger rooms are live-read territory for the LCD
// tool (gateway design, roster cap decision).
const slackRosterPage = 200

// Slack token prefixes, checked at construction so a swapped pair fails at
// boot with a message instead of as an opaque 401 from the first API call.
const (
	slackBotTokenPrefix = "xoxb-"
	slackAppTokenPrefix = "xapp-"
)

// slackAPI is the slice of the Slack Web API the adapter uses; *slack.Client
// satisfies it, tests fake it.
type slackAPI interface {
	AuthTest() (*slack.AuthTestResponse, error)
	PostMessage(channelID string, options ...slack.MsgOption) (string, string, error)
	UpdateMessage(channelID, timestamp string, options ...slack.MsgOption) (string, string, string, error)
	GetUsersInConversation(params *slack.GetUsersInConversationParameters) ([]string, string, error)
	OpenConversation(params *slack.OpenConversationParameters) (*slack.Channel, bool, bool, error)
	GetConversationReplies(params *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error)
}

// SlackAdapter is the first real mapped-identity backend. Transport is
// Socket Mode — an outbound websocket, so no inbound endpoint on the
// cluster and no ingress to secure, the property that made Discord cheap.
// The sender is whatever user_id Slack's authenticated connection asserted;
// joining it to a principal (or dropping it) is the session manager's job
// against the install's mapping table. Never profile.email: whether that
// field is IdP-asserted or user-editable is workspace configuration we do
// not control, and a user-editable field feeding a principal is an
// impersonation primitive (gateway design, identity section).
type SlackAdapter struct {
	api       slackAPI
	sm        *socketmode.Client // nil in unit tests
	log       *slog.Logger
	botUserID string

	mu sync.Mutex
	// sessionRoots caches whether a thread's root message mentions the bot
	// — the rule that lets a bot-rooted thread carry every message without
	// making every thread in a joined channel a session. rootsOrder gives
	// it the same eviction ring as seen.
	sessionRoots map[string]bool
	rootsOrder   []string
	// seen and seenOrder are the at-least-once dedupe ring over (channel, ts).
	seen      map[string]bool
	seenOrder []string
}

// slackLinkRE rewrites the markdown links the relay emits into mrkdwn's
// <url|text> form; anything fancier is presentation polish, not this card.
var slackLinkRE = regexp.MustCompile(`\[([^\]\n]+)\]\((https?://[^)\s]+)\)`)

// slackConversationID is the backend-qualified session key. A channel is
// not a session; a thread in it is — and Slack threads are implicit
// (replying with thread_ts creates one), so a channel mention binds the
// session to the mention message's own ts as thread root, with no
// thread-creation failure mode to handle.
func slackConversationID(channelType, channel, threadTS string) string {
	if channelType == "im" {
		return slackDMPrefix + channel
	}
	return "slack:" + channel + "/" + threadTS
}

// slackChannelThread inverts slackConversationID for the adapter's own use;
// threadTS is "" for DMs.
func slackChannelThread(conversation string) (channel, threadTS string, ok bool) {
	if dm, found := strings.CutPrefix(conversation, slackDMPrefix); found {
		return dm, "", dm != ""
	}
	rest, found := strings.CutPrefix(conversation, "slack:")
	if !found {
		return "", "", false
	}
	channel, threadTS, found = strings.Cut(rest, "/")
	if !found || channel == "" || threadTS == "" {
		return "", "", false
	}
	return channel, threadTS, true
}

// toMrkdwn escapes Slack's control characters, then translates the two
// markdown forms the relay emits (bold pairs, links) into mrkdwn. Escaping
// first, so the only < and > on the wire are the ones our own deterministic
// link translation writes. Narrow on purpose: full markdown fidelity is
// presentation polish, and the legacy Hermes path's converter is not this
// code path's to reuse.
func toMrkdwn(text string) string {
	text = slackEscaper.Replace(text)
	text = strings.ReplaceAll(text, "**", "*")
	return slackLinkRE.ReplaceAllString(text, "<$2|$1>")
}

var _ Adapter = (*SlackAdapter)(nil)

// NewSlackAdapter builds the Socket Mode client pair. The bot token drives
// the Web API and the app token the outbound websocket — the two refs the
// existing SlackSpec already carries, and everything Socket Mode needs.
func NewSlackAdapter(botToken, appToken string, log *slog.Logger) (*SlackAdapter, error) {
	if !strings.HasPrefix(botToken, slackBotTokenPrefix) || !strings.HasPrefix(appToken, slackAppTokenPrefix) {
		return nil, fmt.Errorf("slack tokens look wrong: bot tokens start %s, app tokens %s", slackBotTokenPrefix, slackAppTokenPrefix)
	}
	api := slack.New(botToken, slack.OptionAppLevelToken(appToken))
	return &SlackAdapter{
		api:          api,
		sm:           socketmode.New(api),
		log:          log,
		sessionRoots: map[string]bool{},
		seen:         map[string]bool{},
	}, nil
}

// Run resolves the bot's own identity, consumes Socket Mode events, and
// holds the websocket open until ctx is done.
func (s *SlackAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	auth, err := s.api.AuthTest()
	if err != nil {
		return fmt.Errorf("slack auth.test: %w", err)
	}
	s.botUserID = auth.UserID
	s.log.Info("slack connected", "user", auth.User, "botUserID", auth.UserID)
	go func() {
		// socketmode never closes Events; exiting on ctx keeps embedders and
		// the live test from leaking this goroutine past Run.
		for {
			var evt socketmode.Event
			select {
			case <-ctx.Done():
				return
			case evt = <-s.sm.Events:
			}
			if evt.Type != socketmode.EventTypeEventsAPI {
				continue
			}
			e, ok := evt.Data.(slackevents.EventsAPIEvent)
			if !ok {
				continue
			}
			// Ack immediately: unacked envelopes redeliver in seconds, and
			// per-conversation ordering is the gateway queue's job, not the
			// socket's.
			if evt.Request != nil {
				if err := s.sm.Ack(*evt.Request); err != nil {
					s.log.Warn("socket mode ack failed", "err", err)
				}
			}
			if e.Type != slackevents.CallbackEvent {
				continue
			}
			m, ok := e.InnerEvent.Data.(*slackevents.MessageEvent)
			if !ok {
				continue
			}
			if msg, ok := s.inbound(m); ok {
				handler(msg)
			}
		}
	}()
	return s.sm.RunContext(ctx)
}

// Post writes into the conversation — threaded for sessions rooted in a
// channel, plain for DMs — and returns the message ts the rolling line edits.
func (s *SlackAdapter) Post(conversation, text string) (string, error) {
	channel, threadTS, ok := slackChannelThread(conversation)
	if !ok {
		return "", fmt.Errorf("malformed conversation id %q", conversation)
	}
	opts := []slack.MsgOption{slack.MsgOptionText(toMrkdwn(text), false)}
	if threadTS != "" {
		opts = append(opts, slack.MsgOptionTS(threadTS))
	}
	_, ts, err := s.api.PostMessage(channel, opts...)
	return ts, err
}

// Edit replaces a previously posted message — the rolling progress line.
func (s *SlackAdapter) Edit(conversation, messageID, text string) error {
	channel, _, ok := slackChannelThread(conversation)
	if !ok {
		return fmt.Errorf("malformed conversation id %q", conversation)
	}
	_, _, _, err := s.api.UpdateMessage(channel, messageID, slack.MsgOptionText(toMrkdwn(text), false))
	return err
}

// Roster is the channel's membership. Slack has no per-thread membership,
// and anyone in the channel can read the thread, so the channel roster IS
// the "who could have read this" the audience snapshot exists to answer.
// One page; a channel past it is incomplete rather than paged.
func (s *SlackAdapter) Roster(conversation string) ([]string, bool, error) {
	channel, _, ok := slackChannelThread(conversation)
	if !ok {
		return nil, false, fmt.Errorf("malformed conversation id %q", conversation)
	}
	members, next, err := s.api.GetUsersInConversation(&slack.GetUsersInConversationParameters{
		ChannelID: channel, Limit: slackRosterPage,
	})
	if err != nil {
		return nil, false, err
	}
	return members, next == "", nil
}

// OpenDirect returns the DM conversation for a user — the DM-switch
// primitive. Shipped, unused: everything posts to the room it came from
// until the classifier exists.
func (s *SlackAdapter) OpenDirect(userID string) (string, error) {
	ch, _, _, err := s.api.OpenConversation(&slack.OpenConversationParameters{
		Users: []string{userID}, ReturnIM: true,
	})
	if err != nil {
		return "", err
	}
	return slackDMPrefix + ch.ID, nil
}

// slackMentionsBot reports whether text mentions the bot user. Slack encodes
// mentions as <@U123> or <@U123|display>; requiring the closing form keeps a
// longer id sharing the prefix (<@U123X>) from matching.
func slackMentionsBot(text, botID string) bool {
	marker := "<@" + botID
	for {
		i := strings.Index(text, marker)
		if i < 0 {
			return false
		}
		rest := text[i+len(marker):]
		if strings.HasPrefix(rest, ">") || strings.HasPrefix(rest, "|") {
			return true
		}
		text = text[i+1:]
	}
}

// stripSlackMention removes every mention of the bot (both encoded forms)
// and trims the remainder — the task text is the ask, not the addressing.
func stripSlackMention(text, botID string) string {
	marker := "<@" + botID
	var b strings.Builder
	for {
		i := strings.Index(text, marker)
		if i < 0 {
			break
		}
		rest := text[i+len(marker):]
		switch {
		case strings.HasPrefix(rest, ">"):
			b.WriteString(text[:i])
			text = rest[1:]
		case strings.HasPrefix(rest, "|"):
			j := strings.Index(rest, ">")
			if j < 0 {
				b.WriteString(text[:i+len(marker)])
				text = rest
				continue
			}
			b.WriteString(text[:i])
			text = rest[j+1:]
		default:
			// A longer id sharing the prefix; keep it and move past.
			b.WriteString(text[:i+len(marker)])
			text = rest
		}
	}
	b.WriteString(text)
	return strings.TrimSpace(b.String())
}

// alreadySeen records and reports (channel, ts) pairs — Socket Mode is
// at-least-once, and a redelivered ask must not become a steer.
func (s *SlackAdapter) alreadySeen(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.seen[key] {
		return true
	}
	s.seen[key] = true
	s.seenOrder = append(s.seenOrder, key)
	if len(s.seenOrder) > slackSeenCap {
		delete(s.seen, s.seenOrder[0])
		s.seenOrder = s.seenOrder[1:]
	}
	return false
}

// isSessionRoot reports whether a thread's root message mentions the bot,
// via cache or one conversations.replies read. An API failure reports
// false without caching: dropping is safe (the user can @mention), and the
// next reply retries.
func (s *SlackAdapter) isSessionRoot(channel, threadTS string) bool {
	key := channel + "/" + threadTS
	s.mu.Lock()
	if v, ok := s.sessionRoots[key]; ok {
		s.mu.Unlock()
		return v
	}
	s.mu.Unlock()
	msgs, _, _, err := s.api.GetConversationReplies(&slack.GetConversationRepliesParameters{
		ChannelID: channel, Timestamp: threadTS, Limit: 1, Inclusive: true,
	})
	if err != nil || len(msgs) == 0 {
		s.log.Warn("thread root lookup failed; reply not delivered", "channel", channel, "thread", threadTS, "err", err)
		return false
	}
	root := slackMentionsBot(msgs[0].Text, s.botUserID)
	s.markSessionRoot(key, root)
	return root
}

func (s *SlackAdapter) markSessionRoot(key string, isRoot bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.sessionRoots[key]; !exists {
		s.rootsOrder = append(s.rootsOrder, key)
		if len(s.rootsOrder) > slackRootsCap {
			delete(s.sessionRoots, s.rootsOrder[0])
			s.rootsOrder = s.rootsOrder[1:]
		}
	}
	s.sessionRoots[key] = isRoot
}

// inbound normalizes one message event, or reports it not-a-turn. The
// affordance rule, deterministic: DMs carry every message; a channel
// message must mention the bot, and the ask's own ts becomes the session
// thread's root (Slack threads are implicit); a thread reply is a turn when
// it mentions the bot or the thread root did. Everything else — bots, our
// own posts, edits and other subtypes, redeliveries — is not a turn.
func (s *SlackAdapter) inbound(m *slackevents.MessageEvent) (InboundMessage, bool) {
	if !slackTurnSubtypes[m.SubType] || m.BotID != "" || m.User == "" || m.User == s.botUserID ||
		m.Channel == "" || m.TimeStamp == "" {
		return InboundMessage{}, false
	}
	if s.alreadySeen(m.Channel + "/" + m.TimeStamp) {
		return InboundMessage{}, false
	}
	text := strings.TrimSpace(m.Text)
	if m.ChannelType == "im" {
		if text == "" {
			return InboundMessage{}, false
		}
		return InboundMessage{
			Conversation: slackConversationID(m.ChannelType, m.Channel, ""),
			Kind:         "dm",
			AuthorID:     m.User,
			MessageID:    m.TimeStamp,
			Text:         text,
		}, true
	}
	mentioned := slackMentionsBot(text, s.botUserID)
	if mentioned {
		text = stripSlackMention(text, s.botUserID)
	}
	isReply := m.ThreadTimeStamp != "" && m.ThreadTimeStamp != m.TimeStamp
	threadTS := m.ThreadTimeStamp
	if !isReply {
		if !mentioned {
			return InboundMessage{}, false
		}
		// The ask roots the session thread; remember that so the first
		// unmentioned reply needn't re-read it from the API.
		threadTS = m.TimeStamp
		s.markSessionRoot(m.Channel+"/"+threadTS, true)
	} else if !mentioned && !s.isSessionRoot(m.Channel, threadTS) {
		return InboundMessage{}, false
	}
	if text == "" {
		// A bare mention has nothing to run; same shape as Discord's rule.
		return InboundMessage{}, false
	}
	return InboundMessage{
		Conversation: slackConversationID(m.ChannelType, m.Channel, threadTS),
		Kind:         "group",
		AuthorID:     m.User,
		MessageID:    m.TimeStamp,
		Text:         text,
	}, true
}
