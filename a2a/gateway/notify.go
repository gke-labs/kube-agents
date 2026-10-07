package gateway

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The chat.notify route: proactive posts (alerts, cron findings, audit
// reports) from the platform agent to the install's home channel, with no
// inbound message to answer. Under spec.mode next the Hermes chat platform
// that used to carry them is off, and the gateway is the one process holding
// the chat credential.
//
// A notify is not a task. It mints no capability, starts no executor, opens no
// session and carries no authority block: it is text the agent could already
// post through `hermes send` on a today install, now posted by the process
// that owns the backend. What bounds it is where it may land - the configured
// home space only, a new thread there or a reply on one of that space's
// threads - and who may send it, which is the bus grant: the agent principal
// publishes chat.notify.<backend>, the gateway alone subscribes, and the answer
// travels on chat.notify.reply.agent.>, which only the gateway may publish and
// only the agent may read (platformagent_a2a_identities.go). The answer does
// not go to the requester's _INBOX for the reason the verifier's does not: the
// agent reads its JetStream replies there, and a gateway able to publish into
// it could forge them.
// notifyMaxBody bounds a request. A relayed audit report is the largest
// thing sent here; this is several of them.
const notifyMaxBody = 256 << 10

// notifyPoster is the backend half: post text into a space, new thread or
// reply, and say where it landed. GoogleChatAdapter.PostNotify is the one
// implementation.
type notifyPoster interface {
	PostNotify(space, thread, text string) (message, landed string, err error)
}

// Notifier answers chat.notify requests for one backend.
type Notifier struct {
	subject string
	home    string
	poster  notifyPoster
	log     *slog.Logger
}

// NewGchatNotifier builds the Google Chat notifier. home is the configured
// home space ("spaces/AAA"); anything else is refused here, at start, rather
// than on the first alert.
func NewGchatNotifier(poster notifyPoster, home string, log *slog.Logger) (*Notifier, error) {
	if !gchatIsSpaceName(home) {
		return nil, fmt.Errorf("notify: home channel %q is not a Chat space name (spaces/<id>)", home)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Notifier{subject: lib.NotifySubjectGchat, home: home, poster: poster, log: log}, nil
}

// Start subscribes the notifier on client. The subscription survives
// connection rebuilds (lib.Client.SubscribeCore).
func (n *Notifier) Start(client *lib.Client) (lib.Subscription, error) {
	sub, err := client.SubscribeCore(n.subject, n.handle)
	if err != nil {
		return nil, fmt.Errorf("notify: %w", err)
	}
	n.log.Info("chat.notify route armed", "subject", n.subject, "home", n.home)
	return sub, nil
}

func (n *Notifier) handle(m *nats.Msg) {
	if !strings.HasPrefix(m.Reply, lib.NotifyReplyPrefix) || len(m.Reply) == len(lib.NotifyReplyPrefix) {
		n.log.Warn("notify dropped: reply subject outside the notify reply namespace", "reply", m.Reply)
		return
	}
	reply := n.serve(m.Data)
	body, err := json.Marshal(reply)
	if err != nil {
		n.log.Error("notify: encoding the reply", "err", err)
		return
	}
	if err := m.Respond(body); err != nil {
		n.log.Error("notify: replying", "reply", m.Reply, "err", err)
	}
}

// serve validates and posts one request.
func (n *Notifier) serve(data []byte) lib.NotifyReply {
	if len(data) > notifyMaxBody {
		return n.refuse(fmt.Sprintf("request is %d bytes; the limit is %d", len(data), notifyMaxBody))
	}
	var req lib.NotifyRequest
	if err := json.Unmarshal(data, &req); err != nil {
		return n.refuse("request is not JSON: " + err.Error())
	}
	if strings.TrimSpace(req.Text) == "" {
		return n.refuse("text is empty")
	}
	if req.Thread != "" && !n.inHome(req.Thread) {
		return n.refuse(fmt.Sprintf("thread %q is not a thread of the home channel", req.Thread))
	}
	thread := req.Thread
	var first string
	for _, chunk := range chatChunks(req.Text, discordChunk) {
		message, landed, err := n.poster.PostNotify(n.home, thread, chunk)
		if err != nil {
			n.log.Error("notify post failed", "home", n.home, "thread", thread, "err", err)
			if first == "" {
				return lib.NotifyReply{Error: "post failed: " + err.Error()}
			}
			// Part of the text is already in the channel; say where.
			return lib.NotifyReply{MessageID: first, ThreadID: thread, Error: "post failed partway: " + err.Error()}
		}
		if first == "" {
			first = message
		}
		if landed != "" {
			thread = landed
		}
	}
	n.log.Info("notify posted", "home", n.home, "thread", thread, "message", first)
	return lib.NotifyReply{MessageID: first, ThreadID: thread}
}

// inHome reports whether thread is a thread resource of the home space
// ("spaces/AAA/threads/BBB", nothing nested further).
func (n *Notifier) inHome(thread string) bool {
	id, ok := strings.CutPrefix(thread, n.home+gchatThreadsToken)
	return ok && id != "" && !strings.Contains(id, "/")
}

func (n *Notifier) refuse(reason string) lib.NotifyReply {
	n.log.Warn("notify refused", "reason", reason)
	return lib.NotifyReply{Error: reason}
}
