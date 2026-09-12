package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"time"

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

// slackThreadsCap bounds the session-thread cache the same way; one entry
// accrues per distinct thread replied to, and a busy workspace should not
// grow the gateway forever.
const slackThreadsCap = 2048

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

// slackUnescaper reverses that same escaping on the way in. Slack's Events
// API delivers message text with &, < and > already replaced by &amp;, &lt;
// and &gt; — the encoding side of the control sequences slackEscaper writes
// — so an ask of "get pods && describe node <name>" arrives entity-encoded,
// reaches the executor that way, and comes back double-escaped in the status
// card's echo of the user's own words.
//
// One Replacer, not three passes: it scans the input once and never rescans
// what it wrote, so a literally-typed "&lt;" (on the wire as "&amp;lt;")
// decodes back to "&lt;" rather than collapsing to "<". These three and no
// more — html.UnescapeString would also decode &copy;, &#123; and the rest
// of the HTML5 entity set, which Slack never produces and a user may type.
var slackUnescaper = strings.NewReplacer("&amp;", "&", "&lt;", "<", "&gt;", ">")

// slackRosterPage is one conversations.members page; a channel past it is
// reported rosterComplete=false, not paged — rosterCap (32) truncates far
// below it anyway, and larger rooms are live-read territory for the LCD
// tool (gateway design, roster cap decision).
const slackRosterPage = 200

// slackRepliesTimeout bounds the one synchronous Web API read the event
// pump makes on its own goroutine (the thread-root lookup). Envelopes are
// acked before it runs, but the pump reads the NEXT envelope only after it
// returns, so this value is the worst-case delay to that envelope's ack, and
// it has to sit under Slack's delivery deadline. The Events API docs require
// "an HTTP 2xx within three seconds" or the delivery is retried, and Socket
// Mode inherits that window for the envelope_id ack. Two seconds leaves
// headroom for the ack write and the rest of inbound; a lookup slower than
// that reports false, which is safe (the user can @mention) and is retried
// on the next reply.
const slackRepliesTimeout = 2 * time.Second

const (
	// slackBackend names the backend in authority blocks and config.
	slackBackend = "slack"
	// slackVerifiedBy names what ingress verification actually checked:
	// Slack authenticated the sender over the Socket Mode connection and
	// asserted the immutable user_id, and the install's mapping table
	// joined that id to a principal. Both halves, because either alone
	// would overstate it.
	slackVerifiedBy = "slack-socket-mode+principal-map"
)

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
	GetConversationRepliesContext(ctx context.Context, params *slack.GetConversationRepliesParameters) ([]slack.Message, bool, string, error)
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
	// sessionThreads caches whether a thread is a SESSION thread — one the
	// bot has been addressed in, whether by its root message or by a later
	// reply — which is the rule that lets such a thread carry every message
	// without making every thread in a joined channel a session. Not "the
	// root mentions the bot", which is only how the answer is DERIVED for a
	// thread the adapter has not already seen a mention in: a session can be
	// minted on a reply that mentions the bot inside a thread someone else
	// rooted, and that thread has to carry the follow-ups too. threadsOrder
	// gives it the same eviction ring as seen.
	sessionThreads map[string]bool
	threadsOrder   []string
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
		api:            api,
		sm:             socketmode.New(api),
		log:            log,
		sessionThreads: map[string]bool{},
		seen:           map[string]bool{},
	}, nil
}

// Run resolves the bot's own identity, consumes Socket Mode events, and
// holds the websocket open until ctx is done. It does not return until the
// event pump it starts has exited, so a caller that has seen Run return
// knows no handler call is still in flight.
//
// That guarantee covers the handler INVOCATION and nothing downstream of it.
// The gateway's handler is keyedQueue.enqueue, which appends under a mutex
// and returns, leaving a "go q.run(key)" worker to do the actual task
// dispatch and Web API writes; and Gateway.Run returns this Run's error
// directly (gateway.go) without waiting on those workers, on reapLoop or on
// sweepLoop. So "Run returned" means the pump is finished — not that the
// gateway is.
func (s *SlackAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	// Deferred calls run LIFO, so the order these two are REGISTERED in is
	// the reverse of the order they run in, and it matters: wg.Wait is
	// registered first so that it runs last, after cancel has told the pump
	// to stop. Registered the other way round — cancel first, Wait second —
	// Run would block in Wait on a pump whose context is still live and
	// deadlock. Everything the pump can block on inside this file is
	// ctx-bounded — the Events receive selects on ctx.Done, isSessionThread's
	// Web API read is capped at slackRepliesTimeout and takes this ctx, and
	// the ack below is AckCtx rather than Ack for exactly this reason — so
	// the wait is finite for any handler that is. The gateway's handler is a
	// non-blocking enqueue (keyedQueue.enqueue takes a mutex and returns);
	// an embedder passing a handler that can block forever gets a Run that
	// blocks with it, which is the honest reading of "the pump is finished".
	var wg sync.WaitGroup
	defer wg.Wait()

	// The pump below exits only on ctx, so derive our own: any return from
	// Run — a failed auth.test, or RunContext giving up on an invalid token
	// or an unrecoverable connection error while the parent ctx is still
	// live — must signal it rather than leak it for the process's lifetime.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	auth, err := s.api.AuthTest()
	if err != nil {
		return fmt.Errorf("slack auth.test: %w", err)
	}
	s.botUserID = auth.UserID
	s.log.Info("slack connected", "user", auth.User, "botUserID", auth.UserID)
	wg.Add(1)
	go func() {
		defer wg.Done()
		// socketmode never closes Events; exiting on ctx keeps embedders and
		// the live test from leaking this goroutine past Run.
		for {
			var evt socketmode.Event
			select {
			case <-ctx.Done():
				return
			case evt = <-s.sm.Events:
			}
			// Both cases above are ready at once on a shutdown — ctx.Done is
			// closed and Events still holds up to 50 buffered envelopes — and
			// a select picks uniformly at random among its ready cases, so
			// roughly half of all cancelled pumps take the envelope anyway.
			// Re-check here, where the answer is not a coin toss. A cancelled
			// pump acks nothing and starts nothing: every envelope left in
			// the buffer stays unacked, which is the safe half of
			// at-least-once, and Slack redelivers it to whoever is still up.
			if ctx.Err() != nil {
				return
			}
			if evt.Type != socketmode.EventTypeEventsAPI {
				// Everything that is not an EventsAPI envelope is dropped
				// here, unacked. socketmode's parseEvent attaches a Request
				// to all five request types it knows
				// (socketmode/socket_mode_managed_conn.go): hello and
				// disconnect carry one whose EnvelopeID is empty — acking
				// those would write a junk frame back up the socket — while
				// slash_commands and interactive carry a real, non-empty one
				// that this continue throws away.
				//
				// Which is fine only because the app subscribes to neither,
				// so neither ever arrives. Turning on slash commands or
				// interactivity means acking them here first: Slack wants the
				// envelope acked inside three seconds, and an unacked one
				// redelivers and shows the user a timeout.
				continue
			}
			// Ack before parsing, not after: unacked envelopes redeliver in
			// seconds, so a payload we fail to parse would redeliver forever.
			// Per-conversation ordering is the gateway queue's job, not the
			// socket's.
			//
			// AckCtx, not Ack: Ack passes context.TODO() and hands the
			// response to a 20-deep buffered channel drained by a sender
			// goroutine that itself exits on ctx. Once that sender is gone a
			// full buffer makes Ack block forever, and with Run now waiting
			// on this goroutine that is a hung shutdown rather than a leaked
			// one. Failing the ack on a cancelled ctx is the right answer
			// anyway — the socket is going away and Slack redelivers.
			if evt.Request != nil {
				if err := s.sm.AckCtx(ctx, evt.Request.EnvelopeID, nil); err != nil {
					// DROP the turn — do not fall through and handle it.
					// That reads like throwing away a user's message and is
					// the opposite. An envelope we did not ack is one Slack
					// redelivers, and redelivery is the safe half of
					// at-least-once: the copy that comes back is either
					// suppressed by alreadySeen or handled exactly once by
					// whichever instance receives it. Handling it HERE as
					// well is what makes it unsafe — the ack fails, we start
					// or steer a task anyway, the process exits with nothing
					// acked on the wire, and Slack redelivers to the next
					// instance, whose alreadySeen map is fresh and cannot
					// suppress it. One user message, two turns. The next
					// reader will be tempted to "recover" here by handling it
					// anyway; that recovery is the bug.
					if errors.Is(err, context.Canceled) {
						// The pod is terminating with envelopes still sitting
						// in the 50-deep Events buffer — routine, and a WARN
						// on every SIGTERM is a false positive for anything
						// alerting on logs. Return rather than continue: the
						// context is now definitively done, so every envelope
						// still buffered behind this one is going to end the
						// same way, and the loop has nothing left to do but
						// exit and let Slack redeliver the lot.
						s.log.Debug("socket mode ack abandoned; shutting down", "err", err)
						return
					}
					// Anything else is a real failure to write this one ack —
					// an oversized envelope ID, say — and keeps its WARN. It
					// says nothing about the next envelope, so keep pumping;
					// one bad ack should not take the adapter down.
					s.log.Warn("socket mode ack failed", "err", err)
					continue
				}
				// A nil from AckCtx does NOT mean Slack has the ack. It means
				// the response was QUEUED: SendCtx (socketmode's
				// socket_mode_managed_conn.go) selects ctx.Done against a send
				// into the 20-deep socketModeResponses channel, and in
				// production runResponseSender keeps that channel drained, so
				// there is always room and both cases are ready the moment ctx
				// is cancelled — uniformly at random again. Measured directly
				// against this version of the library: 537 of 1000 AckCtx
				// calls on an already-cancelled context with an empty buffer
				// returned nil. Meanwhile runResponseSender's own select has
				// the same shape and exits on ctx.Done WITHOUT flushing what
				// is queued. So a nil here on a dead context means the ack is
				// sitting in a buffer nobody will drain; handling the turn now
				// produces exactly the duplicate the error branch above exists
				// to prevent.
				//
				// The trade, stated here rather than left to be discovered:
				// if the sender goroutine won its own race and got the ack
				// onto the wire in the nanoseconds before this check, Slack
				// has it, will not redeliver, and we have just dropped that
				// turn on the floor. Landing in that window takes a completed
				// websocket write; the duplicate it replaces happens on
				// roughly half of all shutdowns. A dropped turn costs the user
				// a re-ask. A duplicate turn is two agent sessions doing real
				// work against the same cluster. Take the drop.
				//
				// And it is a smaller loss than it looks: Gateway.Run returns
				// this Run's error without waiting on the "go q.run(key)"
				// queue workers (gateway.go), so a turn started here on the
				// way out would likely be executed only partway anyway.
				// Declining to start new work during shutdown is the honest
				// answer regardless of the ack.
				if ctx.Err() != nil {
					s.log.Debug("socket mode ack queued but not flushed; dropping the turn",
						"envelopeID", evt.Request.EnvelopeID)
					return
				}
			}
			e, ok := evt.Data.(slackevents.EventsAPIEvent)
			if !ok {
				continue
			}
			if e.Type != slackevents.CallbackEvent {
				continue
			}
			m, ok := e.InnerEvent.Data.(*slackevents.MessageEvent)
			if !ok {
				continue
			}
			if msg, ok := s.inbound(ctx, m); ok {
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

// isSessionThread reports whether a thread carries every message: from the
// cache when a mention has already been seen in it (inbound records that)
// or when an earlier read answered, and otherwise from one
// conversations.replies read of the root message. That read happens on the
// event pump's goroutine, so it is bounded by slackRepliesTimeout as well
// as by ctx. An API failure — the timeout included — reports false without
// caching: dropping is safe (the user can @mention), and the next reply
// retries.
//
// A false cached here is not permanent, and must not be. A later mention in
// the same thread calls markSessionThread(key, true), which overwrites it,
// so a thread that becomes a session mid-conversation stops dropping its
// unmentioned messages from that mention on. Without the overwrite the
// negative entry would outlive — and silence — the very session it was
// cached before.
func (s *SlackAdapter) isSessionThread(ctx context.Context, channel, threadTS string) bool {
	key := channel + "/" + threadTS
	s.mu.Lock()
	if v, ok := s.sessionThreads[key]; ok {
		s.mu.Unlock()
		return v
	}
	s.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, slackRepliesTimeout)
	defer cancel()
	msgs, _, _, err := s.api.GetConversationRepliesContext(ctx, &slack.GetConversationRepliesParameters{
		ChannelID: channel, Timestamp: threadTS, Limit: 1, Inclusive: true,
	})
	if err != nil || len(msgs) == 0 {
		// Canceled, and only Canceled, is demoted: that is the pod going away
		// mid-read on shutdown, not an operational problem. Deliberately NOT
		// DeadlineExceeded — at this site that means slackRepliesTimeout
		// genuinely expired on a slow conversations.replies, which cost a
		// user their reply and is the signal worth alerting on. A nil err
		// with no messages back is not a context error either, and also
		// stays at WARN.
		if errors.Is(err, context.Canceled) {
			s.log.Debug("thread root lookup abandoned on shutdown; reply not delivered", "channel", channel, "thread", threadTS, "err", err)
		} else {
			s.log.Warn("thread root lookup failed; reply not delivered", "channel", channel, "thread", threadTS, "err", err)
		}
		return false
	}
	root := slackMentionsBot(msgs[0].Text, s.botUserID)
	s.markSessionThread(key, root)
	return root
}

// markSessionThread records the answer for a thread. Overwriting an
// existing entry deliberately does NOT re-append to threadsOrder: the
// eviction ring holds one position per key, and a flip from false to true
// must not move a key's place in it or let it hold two.
func (s *SlackAdapter) markSessionThread(key string, isSession bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.sessionThreads[key]; !exists {
		s.threadsOrder = append(s.threadsOrder, key)
		if len(s.threadsOrder) > slackThreadsCap {
			delete(s.sessionThreads, s.threadsOrder[0])
			s.threadsOrder = s.threadsOrder[1:]
		}
	}
	s.sessionThreads[key] = isSession
}

// inbound normalizes one message event, or reports it not-a-turn. The
// affordance rule, deterministic: DMs carry every message; a channel
// message must mention the bot, and the ask's own ts becomes the session
// thread's root (Slack threads are implicit); a thread reply is a turn when
// it mentions the bot or the thread is already a session thread — and a
// mention in a thread MAKES it one, whoever rooted it. Everything else —
// bots, our own posts, edits and other subtypes, redeliveries — is not a
// turn.
func (s *SlackAdapter) inbound(ctx context.Context, m *slackevents.MessageEvent) (InboundMessage, bool) {
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
			// Decoding also changes what the affordance matchers see. That is
			// intended, and it is not obvious: normalize (text.go) strips
			// every non-alphanumeric, so a typed "<stop>" — on the wire as
			// "&lt;stop&gt;" — used to normalize to "ltstopgt" and match
			// nothing, and decoded first it normalizes to "stop" and is a
			// hard task cancel. Matching what the user typed beats matching
			// Slack's entity mangling, so this is the right way round. The
			// same shift makes normalized text shorter, so an ask that
			// decodes can newly fall under isStatusQuery's wideMatchLenCap.
			Text: slackUnescaper.Replace(text),
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
		// Slack threads are implicit, so the ask's own ts is the root of the
		// thread the session will live in.
		threadTS = m.TimeStamp
	}
	if mentioned {
		// Addressing the bot in a thread makes that thread a session thread,
		// and this is the one place that is recorded — for the channel ask
		// above, whose own ts is the root, and equally for a mention inside
		// a thread someone else started. The second case is why this is not
		// inside the !isReply branch: the gateway mints a session on the
		// key below either way and starts a task there, and a session whose
		// thread does not carry every message is one the user cannot steer
		// or "stop" without re-@mentioning for each message. Discord, which
		// this is parity with, has no root condition at all.
		//
		// It also un-poisons the cache: an earlier unmentioned reply in this
		// thread will have read the root, found no mention and cached false,
		// and markSessionThread overwrites that rather than leaving the
		// thread dropped for the life of the entry.
		s.markSessionThread(m.Channel+"/"+threadTS, true)
	}
	if text == "" {
		// A bare mention has nothing to run; same shape as Discord's rule.
		//
		// Checked before the session-thread lookup below, not after it. An
		// attachment-only (file_share with no caption) or whitespace-only
		// reply in an uncached thread is discarded either way, and
		// isSessionThread can spend slackRepliesTimeout on a
		// conversations.replies read with the event pump — and so the next
		// envelope's ack — blocked behind it. Nothing is lost by skipping
		// that read: sessionThreads is a pure lookup cache with no reader
		// outside isSessionThread itself, and for a thread with no mention
		// in it the answer is derived from the root message's text, so the
		// next reply in the thread fills it with the same answer. The
		// markSessionThread above is deliberately still reached — a bare
		// "@bot" addresses the bot in that thread whether the user typed it
		// as a channel message or as a reply, later messages there are the
		// ask, and recording it costs no API call.
		return InboundMessage{}, false
	}
	if isReply && !mentioned && !s.isSessionThread(ctx, m.Channel, threadTS) {
		return InboundMessage{}, false
	}
	return InboundMessage{
		Conversation: slackConversationID(m.ChannelType, m.Channel, threadTS),
		Kind:         "group",
		AuthorID:     m.User,
		MessageID:    m.TimeStamp,
		// Decoded last, after the mention match and strip above: both key on
		// Slack's raw "<@U…>" form, which decoding would have turned into
		// plain text they no longer recognize. Carries the same deliberate
		// effect on normalize and the affordance matchers as the DM path.
		Text: slackUnescaper.Replace(text),
	}, true
}
