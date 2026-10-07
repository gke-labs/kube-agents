package gateway

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

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
const (
	// notifyMaxBody bounds a request. A relayed audit report is the largest
	// thing sent here; this is several of them.
	notifyMaxBody = 256 << 10
	// notifyQueueDepth bounds the requests waiting for the worker. Proactive
	// posts arrive a few at a time; a full queue is a sender in a loop, and
	// is refused at once rather than left to time out.
	notifyQueueDepth = 32
	// notifyStopGrace bounds how long Stop waits for the worker to finish the
	// post in flight and refuse the rest, inside the pod's 30s termination
	// grace period.
	notifyStopGrace = 20 * time.Second
	// notifyStoppingRefusal is the answer to a request the gateway will not
	// post because it is stopping. A refusal (exit 1 at the CLI), never
	// silence: silence reads as "may have posted" and is not retried.
	notifyStoppingRefusal = "the gateway is stopping; not posted"
)

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
	// threadOK is the backend's test that a requested thread may be
	// replied on. The home channel is the bound either way: Chat's thread
	// names its space, so it is checked against the home space (inHome);
	// a Slack thread ts names no channel, so it is only checked for shape
	// and is always posted into the home channel.
	threadOK func(string) bool
	log      *slog.Logger
	jobs     chan notifyJob
	done     chan struct{}

	// mu orders handle's enqueue against Stop's close of jobs: a request that
	// arrives while Stop runs is refused under the lock rather than sent on a
	// closed channel, which would panic.
	mu       sync.Mutex
	stopping bool
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
	n := &Notifier{subject: lib.NotifySubjectGchat, home: home, poster: poster, log: log}
	n.threadOK = n.inHome
	return n, nil
}

// NewSlackNotifier builds the Slack notifier. home is the configured home
// channel's id ("C0123" or a private channel's "G0123"); anything else is
// refused here, at start. A reply thread is a thread root's ts and names no
// channel, so the notifier posts every request into home: the channel is
// the whole authority bound, and a ts from some other channel can only ever
// thread (or fail to thread) inside home.
func NewSlackNotifier(poster notifyPoster, home string, log *slog.Logger) (*Notifier, error) {
	if !slackIsHomeChannelID(home) {
		return nil, fmt.Errorf("notify: home channel %q is not a Slack channel id (C... or G...)", home)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Notifier{subject: lib.NotifySubjectSlack, home: home, poster: poster, log: log, threadOK: slackIsTS}, nil
}

// Start subscribes the notifier on client and starts the worker that posts.
// The subscription survives connection rebuilds (lib.Client.SubscribeCore);
// stopping it also stops the worker once the queue drains.
func (n *Notifier) Start(client *lib.Client) (lib.Subscription, error) {
	n.jobs = make(chan notifyJob, notifyQueueDepth)
	n.done = make(chan struct{})
	sub, err := client.SubscribeCore(n.subject, n.handle)
	if err != nil {
		return nil, fmt.Errorf("notify: %w", err)
	}
	go n.work()
	n.log.Info("chat.notify route armed", "subject", n.subject, "home", n.home)
	return &notifierSub{sub: sub, n: n}, nil
}

// notifyJob is one validated request waiting for the worker.
type notifyJob struct {
	req    lib.NotifyRequest
	answer func(lib.NotifyReply)
}

// notifierSub stops the subscription, closes the queue under the notifier's
// lock, and waits for the worker: it finishes the post in flight and refuses
// what is still queued, so every accepted request gets an answer while the
// connection is still open. In cmd/gateway this Stop is deferred after the
// client's Close, so it runs first.
type notifierSub struct {
	sub  lib.Subscription
	n    *Notifier
	once sync.Once
}

func (s *notifierSub) Stop() {
	s.once.Do(func() {
		s.sub.Stop()
		s.n.mu.Lock()
		s.n.stopping = true
		close(s.n.jobs)
		s.n.mu.Unlock()
		select {
		case <-s.n.done:
		case <-time.After(notifyStopGrace):
			s.n.log.Warn("chat.notify worker did not finish before shutdown", "grace", notifyStopGrace)
		}
	})
}

// handle runs on the subscription's goroutine. It answers a refusal at once
// and queues an accepted request for the worker, which answers it once the
// first post has landed. Posting here instead would hold every later request
// behind the relay calls of a long report.
func (n *Notifier) handle(m *nats.Msg) {
	if !strings.HasPrefix(m.Reply, lib.NotifyReplyPrefix) || len(m.Reply) == len(lib.NotifyReplyPrefix) {
		n.log.Warn("notify dropped: reply subject outside the notify reply namespace", "reply", m.Reply)
		return
	}
	answer := func(reply lib.NotifyReply) {
		body, err := json.Marshal(reply)
		if err != nil {
			n.log.Error("notify: encoding the reply", "err", err)
			return
		}
		if err := m.Respond(body); err != nil {
			n.log.Error("notify: replying", "reply", m.Reply, "err", err)
		}
	}
	req, refusal := n.validate(m.Data)
	if refusal != nil {
		answer(*refusal)
		return
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.stopping {
		answer(n.refuse(notifyStoppingRefusal))
		return
	}
	select {
	case n.jobs <- notifyJob{req: req, answer: answer}:
	default:
		answer(n.refuse(fmt.Sprintf("%d notifies are already waiting to post", notifyQueueDepth)))
	}
}

func (n *Notifier) work() {
	defer close(n.done)
	for job := range n.jobs {
		if n.isStopping() {
			job.answer(n.refuse(notifyStoppingRefusal))
			continue
		}
		n.post(job.req, job.answer)
	}
}

func (n *Notifier) isStopping() bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.stopping
}

// validate decodes one request and refuses what may not be posted.
func (n *Notifier) validate(data []byte) (lib.NotifyRequest, *lib.NotifyReply) {
	var req lib.NotifyRequest
	refuse := func(reason string) (lib.NotifyRequest, *lib.NotifyReply) {
		r := n.refuse(reason)
		return req, &r
	}
	if len(data) > notifyMaxBody {
		return refuse(fmt.Sprintf("request is %d bytes; the limit is %d", len(data), notifyMaxBody))
	}
	if err := json.Unmarshal(data, &req); err != nil {
		return refuse("request is not JSON: " + err.Error())
	}
	if strings.TrimSpace(req.Text) == "" {
		return refuse("text is empty")
	}
	if req.Thread != "" && !n.threadOK(req.Thread) {
		return refuse(fmt.Sprintf("thread %q is not a thread of the home channel", req.Thread))
	}
	return req, nil
}

// post writes one accepted request, chunked, and calls answer exactly once:
// after the first chunk lands, with where it landed, or with the reason it did
// not. The remaining chunks follow into the same thread; a failure among them
// is logged, since the caller already holds its answer and the start of the
// text is in the channel.
func (n *Notifier) post(req lib.NotifyRequest, answer func(lib.NotifyReply)) {
	thread := req.Thread
	var first string
	for i, chunk := range chatChunks(req.Text, discordChunk) {
		message, landed, err := n.poster.PostNotify(n.home, thread, chunk)
		if err != nil {
			n.log.Error("notify post failed", "home", n.home, "thread", thread, "chunk", i+1, "err", err)
			if first == "" {
				answer(lib.NotifyReply{Error: "post failed: " + err.Error()})
			}
			return
		}
		if landed != "" {
			thread = landed
		}
		if first == "" {
			first = message
			answer(lib.NotifyReply{MessageID: first, ThreadID: thread})
		}
	}
	n.log.Info("notify posted", "home", n.home, "thread", thread, "message", first)
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
