package gateway

import (
	"context"
	"crypto/rand"
	"encoding/hex"
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

	// consoleConnName is the adapter's NATS connection name, and the
	// "console" field on its connection-state log lines.
	consoleConnName = "a2a-gateway-console"

	// consoleBootIDBytes is the random prefix on notice ids, per adapter:
	// 4 bytes (8 hex characters) keeps ids from repeating across restarts.
	consoleBootIDBytes = 4

	// bytesPerKiB names the unit consoleTextCap is expressed in, so the
	// oversize notice's "N KiB" can be derived from the cap rather than
	// hardcoded and left free to drift from it.
	bytesPerKiB = 1024

	// consoleTextCap bounds one frame's text. A browser can publish up to
	// the server's max_payload (1 MiB by default), which is far more ask
	// than the gateway should forward as a task; the ask echo in the
	// session record is truncated anyway. 16 KiB is roomy for a chat turn.
	consoleTextCap = 16 * bytesPerKiB
)

// consoleTokenRe is one dot-free DNS-1123 label, so the `*` in the
// subscription covers exactly one conversation.
var consoleTokenRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// ConsoleInFrame is what the browser publishes. Kind is empty or "text";
// it is the seam for gateway-side commands later, and a frame of any other
// kind is dropped (logged, no notice) rather than forwarded as an ask.
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
	// closingFlag distinguishes our own Close from a real drop: nats.go runs
	// DisconnectErrHandler for a user-initiated Close too, with a nil err, so
	// without this every orderly shutdown logs a reconnect that never comes.
	// Same guard as the bus client's (lib/client.go).
	closingFlag atomic.Bool
	mu          sync.Mutex
	nextID      uint64
	// bootID is minted per adapter so notice ids (c-<boot>-<n>) do not
	// repeat across gateway restarts, where a page still holding c-1 from
	// the last boot would otherwise take a new c-1's edits as its own.
	bootID string
}

// consolePublishViolationRe pulls the subject out of nats-server's
// "Permissions Violation for Publish to \"<subject>\"" line.
var consolePublishViolationRe = regexp.MustCompile(`for Publish to "(\S+)"`)

// NewConsoleAdapter dials the bus with the gateway's own options. The async
// error handler is the whole point of owning the connection: a subscribe
// refused by the server (a NATS render that predates the console identity)
// arrives there and nowhere else, and silence would be the failure mode. The
// adapter's own handlers are appended after natsOpts, so a caller's options
// cannot replace them.
func NewConsoleAdapter(url string, natsOpts []nats.Option, log *slog.Logger) (*ConsoleAdapter, error) {
	if log == nil {
		log = slog.Default()
	}
	boot := make([]byte, consoleBootIDBytes)
	if _, err := rand.Read(boot); err != nil {
		return nil, fmt.Errorf("console adapter: boot id: %w", err)
	}
	a := &ConsoleAdapter{log: log, bootID: hex.EncodeToString(boot)}
	opts := append([]nats.Option{nats.Name(consoleConnName), nats.MaxReconnects(-1)}, natsOpts...)
	opts = append(opts,
		nats.ErrorHandler(func(_ *nats.Conn, sub *nats.Subscription, err error) {
			if !errors.Is(err, nats.ErrPermissionViolation) && !strings.Contains(err.Error(), "Permissions Violation") {
				log.Warn("console connection error", "console", consoleConnName, "err", err)
				return
			}
			// nats.go's transient-error path hands a nil sub for every
			// permissions violation, so the subject comes from the error
			// text, or from the one subscription this adapter makes.
			if m := consolePublishViolationRe.FindStringSubmatch(err.Error()); m != nil {
				log.Error("console publish refused: the NATS config predates the console identity, or the NATS pod has not rolled onto the new config yet; notices are lost until the config carries chat.console.*.out for the gateway",
					"subject", m[1], "err", err)
				return
			}
			subject := consoleInSubjectWildcard
			if sub != nil {
				subject = sub.Subject
			}
			log.Error("console subscription refused: the NATS config predates the console identity, or the NATS pod has not rolled onto the new config yet; the subscription is resent on reconnect and recovers once the config carries chat.console.*.in for the gateway",
				"subject", subject, "err", err)
		}),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			if a.closingFlag.Load() {
				return
			}
			log.Warn("console connection lost; reconnecting", "console", consoleConnName, "err", err)
		}),
		nats.ReconnectHandler(func(nc *nats.Conn) {
			log.Info("console connection restored", "console", consoleConnName, "url", nc.ConnectedUrl())
		}),
	)
	nc, err := nats.Connect(url, opts...)
	if err != nil {
		return nil, fmt.Errorf("console adapter: connect: %w", err)
	}
	a.nc = nc
	return a, nil
}

// Close is for an adapter that was never run, or needs closing early: Run
// closes the connection itself once ctx is cancelled, so a caller that also
// defers Close (or registers it with t.Cleanup) after cancelling is safe —
// IsClosed makes this idempotent.
func (a *ConsoleAdapter) Close() {
	if a.nc != nil && !a.nc.IsClosed() {
		a.closingFlag.Store(true)
		a.nc.Close()
	}
}

// subscribed reports whether Run has bound its subscription (tests).
func (a *ConsoleAdapter) subscribed() bool { return a.subscribedFlag.Load() }

// closed reports whether the connection has been closed (tests).
func (a *ConsoleAdapter) closed() bool { return a.nc != nil && a.nc.IsClosed() }

// Run delivers frames as InboundMessages until ctx is done. It owns the
// connection's lifecycle from here: on ctx cancellation it unsubscribes and
// closes the connection, so a caller does not leak it by trusting Run alone.
//
// The subscription is plain core NATS with no queue group, so every gateway
// process holding the console identity answers every frame rather than one
// of them taking it. The deployment is single-replica with a Recreate
// strategy (platformagent_a2a_manifests.go, a2aGatewayRecreateStrategyPatch),
// which is what keeps that from double-handling today - so for the console
// that replica count is an invariant, not a capacity setting. A second
// replica, or a developer's gateway on a port-forward, becomes a second
// subscriber on the same door.
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
	a.Close()
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
	if f.Kind != "" && f.Kind != "text" {
		a.log.Warn("console frame dropped", "reason", fmt.Sprintf("unknown kind %q", f.Kind), "conversation", conversation, "messageId", f.MessageID)
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
		return msg, fmt.Sprintf("⚠️ that message is %d bytes and the console takes at most %d KiB per turn; it was not sent", len(text), consoleTextCap/bytesPerKiB), false
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
	id := fmt.Sprintf("c-%s-%d", a.bootID, a.nextID)
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
