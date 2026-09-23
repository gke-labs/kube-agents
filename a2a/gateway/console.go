package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/nats-io/nats.go"
)

// The console backend: the web console's chat door, over core NATS.
//
// A browser holding the `console` credential publishes ConsoleInFrame JSON
// to chat.console.<token>.in; this adapter turns each into one InboundMessage
// and the gateway handles it like any DM. The gateway's own notices (the
// placeholder while a pod comes up, the rolling progress line, drop notices)
// go back as ConsoleOutFrame JSON on chat.console.<token>.out. Answers never
// travel here: they stream through TASKS, which the console page already
// renders, so a lost .out frame costs a notice and never an answer.
//
// Identity is the grant. Only the `console` user may publish on
// chat.console.*.in (the operator's consoleIdentity), so the sender IS
// consoleAuthor and resolves to consolePrincipal with no mapping table in
// between - the same subject-derived identity every other writer on this bus
// has. One shared principal is the posture until the account split.
const (
	consoleBackend    = "console"
	consoleAuthor     = "console"
	consolePrincipal  = "nats:console"
	consoleVerifiedBy = "nats-grant"
	consoleKeyPrefix  = "console:"

	consoleInSubjectWildcard = "chat.console.*.in"

	// consoleTextCap bounds one frame's text. A browser can publish up to
	// the server's max_payload (1 MiB by default), which is far more ask
	// than the gateway should forward as a task; the ask echo in the
	// session record is truncated anyway. 16 KiB is roomy for a chat turn.
	consoleTextCap = 16 * 1024
)

// consoleTokenRe is one dot-free DNS-1123 label, so the `*` in the
// subscription covers exactly one conversation.
var consoleTokenRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// ConsoleInFrame is what the browser publishes. Kind defaults to "text";
// it is the seam for gateway-side commands later and is not interpreted yet.
type ConsoleInFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
	Kind      string `json:"kind,omitempty"`
}

// ConsoleOutFrame is what the gateway publishes back. Edit true means
// MessageID names a frame already shown and Text replaces it.
type ConsoleOutFrame struct {
	MessageID string `json:"messageId"`
	Text      string `json:"text"`
	Edit      bool   `json:"edit,omitempty"`
}

// consoleConversationToken splits "console:<token>" and validates the token.
func consoleConversationToken(conversation string) (string, bool) {
	token, ok := strings.CutPrefix(conversation, consoleKeyPrefix)
	if !ok || !consoleTokenRe.MatchString(token) {
		return "", false
	}
	return token, true
}

func consoleOutSubject(token string) string { return "chat.console." + token + ".out" }

// ConsoleAdapter is the five-operation Adapter over core NATS. It owns its
// own connection: lib.Client is a JetStream envelope client and this is
// plain pub/sub, and the two reconnect independently.
type ConsoleAdapter struct {
	nc  *nats.Conn
	log *slog.Logger

	subscribedFlag atomic.Bool
	mu             sync.Mutex
	nextID         uint64
}

// NewConsoleAdapter dials the bus with the gateway's own options. The async
// error handler is the whole point of owning the connection: a subscribe
// refused by the server (a NATS render that predates the console identity)
// arrives there and nowhere else, and silence would be the failure mode.
func NewConsoleAdapter(url string, natsOpts []nats.Option, log *slog.Logger) (*ConsoleAdapter, error) {
	if log == nil {
		log = slog.Default()
	}
	a := &ConsoleAdapter{log: log}
	opts := append([]nats.Option{
		nats.Name("a2a-gateway-console"),
		nats.MaxReconnects(-1),
		nats.ErrorHandler(func(_ *nats.Conn, sub *nats.Subscription, err error) {
			if errors.Is(err, nats.ErrPermissionViolation) || strings.Contains(err.Error(), "Permissions Violation") {
				subject := ""
				if sub != nil {
					subject = sub.Subject
				}
				log.Error("console subscription refused: the NATS config predates the console identity; upgrade the operator so the gateway grant carries chat.console.*.in",
					"subject", subject, "err", err)
				return
			}
			log.Warn("console connection error", "err", err)
		}),
	}, natsOpts...)
	nc, err := nats.Connect(url, opts...)
	if err != nil {
		return nil, fmt.Errorf("console adapter: connect: %w", err)
	}
	a.nc = nc
	return a, nil
}

// Close drains the connection. Run's ctx cancellation also closes it.
func (a *ConsoleAdapter) Close() {
	if a.nc != nil && !a.nc.IsClosed() {
		a.nc.Close()
	}
}

// subscribed reports whether Run has bound its subscription (tests).
func (a *ConsoleAdapter) subscribed() bool { return a.subscribedFlag.Load() }

// Run delivers frames as InboundMessages until ctx is done.
func (a *ConsoleAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	sub, err := a.nc.Subscribe(consoleInSubjectWildcard, func(m *nats.Msg) {
		msg, notice, ok := a.inbound(m)
		if notice != "" {
			// Best effort; the frame was already refused.
			_, _ = a.Post(msg.Conversation, notice)
		}
		if ok {
			handler(msg)
		}
	})
	if err != nil {
		return fmt.Errorf("console adapter: subscribe: %w", err)
	}
	if err := a.nc.Flush(); err != nil {
		return fmt.Errorf("console adapter: flush: %w", err)
	}
	a.subscribedFlag.Store(true)
	<-ctx.Done()
	_ = sub.Unsubscribe()
	return nil
}

// inbound parses one frame. It returns the message, an optional notice to
// post back on the conversation, and whether the message should be handled.
// Every drop is logged: a silent drop of a real user is the failure the
// gateway's own drop notice exists to avoid.
func (a *ConsoleAdapter) inbound(m *nats.Msg) (InboundMessage, string, bool) {
	// Subject is chat.console.<token>.in; the token is the third field.
	parts := strings.Split(m.Subject, ".")
	if len(parts) != 4 {
		a.log.Warn("console frame dropped", "reason", "subject shape", "subject", m.Subject)
		return InboundMessage{}, "", false
	}
	conversation := consoleKeyPrefix + parts[2]
	if _, ok := consoleConversationToken(conversation); !ok {
		a.log.Warn("console frame dropped", "reason", "conversation token", "subject", m.Subject)
		return InboundMessage{}, "", false
	}
	var f ConsoleInFrame
	if err := json.Unmarshal(m.Data, &f); err != nil {
		a.log.Warn("console frame dropped", "reason", "not a frame", "conversation", conversation, "err", err)
		return InboundMessage{}, "", false
	}
	text := strings.TrimSpace(f.Text)
	if text == "" {
		a.log.Warn("console frame dropped", "reason", "empty text", "conversation", conversation, "messageId", f.MessageID)
		return InboundMessage{}, "", false
	}
	msg := InboundMessage{Conversation: conversation, Kind: "dm", AuthorID: consoleAuthor, MessageID: f.MessageID}
	if len(text) > consoleTextCap {
		a.log.Warn("console frame dropped", "reason", "oversize", "conversation", conversation, "messageId", f.MessageID, "bytes", len(text))
		return msg, fmt.Sprintf("⚠️ that message is %d bytes and the console takes at most 16 KiB per turn; it was not sent", len(text)), false
	}
	msg.Text = text
	return msg, "", true
}

func (a *ConsoleAdapter) publish(conversation, messageID, text string, edit bool) error {
	token, ok := consoleConversationToken(conversation)
	if !ok {
		return fmt.Errorf("console adapter: malformed conversation id %q", conversation)
	}
	data, err := json.Marshal(ConsoleOutFrame{MessageID: messageID, Text: text, Edit: edit})
	if err != nil {
		return err
	}
	return a.nc.Publish(consoleOutSubject(token), data)
}

// Post publishes a new notice frame and returns its id.
func (a *ConsoleAdapter) Post(conversation, text string) (string, error) {
	a.mu.Lock()
	a.nextID++
	id := fmt.Sprintf("c-%d", a.nextID)
	a.mu.Unlock()
	if err := a.publish(conversation, id, text, false); err != nil {
		return "", err
	}
	return id, nil
}

// Edit republishes an existing notice id with new text.
func (a *ConsoleAdapter) Edit(conversation, messageID, text string) error {
	return a.publish(conversation, messageID, text, true)
}

// Roster is the one author: a console conversation is a DM by construction.
func (a *ConsoleAdapter) Roster(conversation string) ([]string, bool, error) {
	if _, ok := consoleConversationToken(conversation); !ok {
		return nil, false, fmt.Errorf("console adapter: malformed conversation id %q", conversation)
	}
	return []string{consoleAuthor}, true, nil
}

// OpenDirect has nothing to open: the conversation already is the direct
// channel. Refusing is the honest answer; the gateway does not call this yet.
func (a *ConsoleAdapter) OpenDirect(string) (string, error) {
	return "", errors.New("console adapter: no direct channel beyond the conversation")
}
