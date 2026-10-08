package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"slices"
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
// home space (a new thread there or a reply on one of that space's threads),
// or a conversation the gateway holds a live session record for, named with
// that record's context id - and who may send it, which is the bus grant: the agent principal
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
	// notifyEmptyText answers an empty request: nothing to post.
	notifyEmptyText = "text is empty"
	// The backoff between attempts to bind the route when the first fails
	// (a flush that timed out on a connection that had just dialled). Run
	// keeps trying for the life of the process: a route left unbound would
	// answer every notify with no responders while the agent is told the
	// route exists.
	notifyStartRetryMin = time.Second
	notifyStartRetryMax = 30 * time.Second
	// notifyLostLineMax bounds how much of a lost request's text the error
	// log carries: enough to find the post it was, not the whole report.
	notifyLostLineMax = 120
	// notifyLookupTimeout bounds the session-record read a conversation
	// request is checked against.
	notifyLookupTimeout = 5 * time.Second
	// notifyNoHome refuses a home-channel post on an install that names no
	// home channel: the route then serves conversation requests only.
	notifyNoHome = "no home channel is configured; only a request naming a conversation is posted"
	// notifyNoConversations refuses a conversation request on a notifier
	// armed without the gateway's conversations.
	notifyNoConversations = "this route does not post into conversations"
	// notifyConversationRefused is the one refusal for a conversation the
	// request may not post into, whatever the reason: no session record, a
	// context id that does not match, or a record that cannot be read.
	notifyConversationRefused = "not a live conversation with that context"
)

// notifyMentionTokens and notifyMentionElements are what blocksMention
// refuses: the mrkdwn spellings that ping, and the rich_text element types
// that do.
var (
	notifyMentionTokens   = []string{"<!", "<@"}
	notifyMentionElements = []string{"broadcast", "user", "usergroup"}
)

// NotifyConversations is the gateway's side of a request aimed at a
// conversation rather than the home channel: the context id of the
// conversation's session record ("" when it has none), and a post into it
// with the gateway's own adapter.
type NotifyConversations interface {
	ConversationContext(ctx context.Context, key string) (string, error)
	Post(conversation, text string) (messageID string, err error)
}

// notifyPoster is the backend half: post text into a space, new thread or
// reply, and say where it landed. GoogleChatAdapter.PostNotify is the one
// implementation.
type notifyPoster interface {
	PostNotify(space, thread, text string) (message, landed string, err error)
}

// notifyBlocksPoster is the optional half a backend that renders Block Kit
// adds: one message, blocks with text as the fallback. SlackAdapter is the
// one implementation; a request with blocks to any other backend is refused.
type notifyBlocksPoster interface {
	PostNotifyBlocks(home, thread, text string, blocks json.RawMessage) (message, landed string, err error)
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
	// convPrefix is the conversation-key prefix of this backend, and conv
	// the gateway's conversations; nil leaves conversation requests refused.
	convPrefix string
	conv       NotifyConversations
	log        *slog.Logger
	jobs       chan notifyJob
	done       chan struct{}

	// mu orders handle's enqueue against Stop's close of jobs: a request that
	// arrives while Stop runs is refused under the lock rather than sent on a
	// closed channel, which would panic.
	mu       sync.Mutex
	stopping bool
}

// NewGchatNotifier builds the Google Chat notifier. home is the configured
// home space ("spaces/AAA"), or "" when there is none, which leaves the route
// serving conversation requests only; anything else is refused here, at
// start, rather than on the first alert.
func NewGchatNotifier(poster notifyPoster, home string, log *slog.Logger) (*Notifier, error) {
	if home != "" && !gchatIsSpaceName(home) {
		return nil, fmt.Errorf("notify: home channel %q is not a Chat space name (spaces/<id>)", home)
	}
	if log == nil {
		log = slog.Default()
	}
	n := &Notifier{subject: lib.NotifySubjectGchat, home: home, poster: poster, log: log,
		convPrefix: gchatKeyPrefix}
	n.threadOK = n.inHome
	return n, nil
}

// NewSlackNotifier builds the Slack notifier. home is the configured home
// channel's id ("C0123" or a private channel's "G0123"), or "" when there is
// none, which leaves the route serving conversation requests only; anything
// else is refused here, at start. A reply thread is a thread root's ts and
// names no channel, so the notifier posts every home request into home: the
// channel is the whole authority bound, and a ts from some other channel can
// only ever thread (or fail to thread) inside home.
func NewSlackNotifier(poster notifyPoster, home string, log *slog.Logger) (*Notifier, error) {
	if home != "" && !slackIsHomeChannelID(home) {
		return nil, fmt.Errorf("notify: home channel %q is not a Slack channel id (C... or G...)", home)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Notifier{subject: lib.NotifySubjectSlack, home: home, poster: poster, log: log,
		threadOK: slackIsTS, convPrefix: slackKeyPrefix}, nil
}

// SetConversations arms requests aimed at a conversation the gateway holds.
// Call it before Start.
func (n *Notifier) SetConversations(c NotifyConversations) {
	n.conv = c
}

// ServesConversations reports whether conversation requests are armed.
func (n *Notifier) ServesConversations() bool {
	return n.conv != nil
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
	n.log.Info("chat.notify route armed", "subject", n.subject, "home", n.home, "conversations", n.ServesConversations())
	return &notifierSub{sub: sub, n: n}, nil
}

// notifyJob is one validated request waiting for the worker.
type notifyJob struct {
	req    lib.NotifyRequest
	answer func(lib.NotifyReply)
	// deadline is when the requester stops waiting for the answer; zero
	// when the request did not say.
	deadline time.Time
}

// reply answers the job. When the answer says nothing was posted and the
// requester has already stopped waiting, nobody hears it and the requester
// has recorded the text as possibly posted ("outcome unknown"), so the loss
// is logged as an error naming the text, the one place it shows.
func (n *Notifier) reply(job notifyJob, r lib.NotifyReply) {
	if r.MessageID == "" && r.Error != "" && !job.deadline.IsZero() && time.Now().After(job.deadline) {
		n.log.Error("notify lost: not posted after its requester stopped waiting, which records it as possibly posted",
			"reason", r.Error, "thread", job.req.Thread, "conversation", job.req.Conversation,
			"text", firstLine(job.req.Text, notifyLostLineMax))
	}
	job.answer(r)
}

// firstLine is text's first line, cut to max runes.
func firstLine(text string, max int) string {
	line, _, _ := strings.Cut(strings.TrimSpace(text), "\n")
	if r := []rune(line); len(r) > max {
		return string(r[:max]) + "…"
	}
	return line
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
		s.n.mu.Unlock()
		// Refuse what is queued now, not after the post in flight returns:
		// that post can outlast the grace (one relay call per chunk), and a
		// request still queued when the process exits gets no answer at all.
		s.n.drainRefusing()
		s.n.mu.Lock()
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
	job := notifyJob{req: req, answer: answer}
	if req.WaitMillis > 0 {
		job.deadline = time.Now().Add(time.Duration(req.WaitMillis) * time.Millisecond)
	}
	select {
	case n.jobs <- job:
	default:
		answer(n.refuse(fmt.Sprintf("%d notifies are already waiting to post", notifyQueueDepth)))
	}
}

func (n *Notifier) work() {
	defer close(n.done)
	for job := range n.jobs {
		if n.isStopping() {
			n.reply(job, n.refuse(notifyStoppingRefusal))
			continue
		}
		n.post(job)
	}
}

// drainRefusing answers every request still in the queue with the stopping
// refusal. The worker may take one concurrently; it refuses it too.
func (n *Notifier) drainRefusing() {
	for {
		select {
		case job := <-n.jobs:
			n.reply(job, n.refuse(notifyStoppingRefusal))
		default:
			return
		}
	}
}

// Run binds the route, retrying a failed bind with a capped backoff until it
// succeeds or ctx ends, then holds it until ctx ends and stops it. A bind
// that fails once is not a route that stays down for the life of the pod.
func (n *Notifier) Run(ctx context.Context, client *lib.Client) {
	wait := notifyStartRetryMin
	for {
		sub, err := n.Start(client)
		if err == nil {
			<-ctx.Done()
			sub.Stop()
			return
		}
		n.log.Error("chat.notify route not armed; retrying", "err", err, "in", wait)
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		wait = min(wait*2, notifyStartRetryMax)
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
		// An empty request is how the kanban notifier's stand-in probes
		// whether the route is armed (any answer means it is), every few
		// minutes while any card subscribes to a home-space thread: refused,
		// but not worth a warning.
		n.log.Debug("notify: empty request (route probe) answered")
		r := lib.NotifyReply{Error: notifyEmptyText}
		return req, &r
	}
	if req.Conversation != "" || req.ContextID != "" {
		return n.validateConversation(req)
	}
	if n.home == "" {
		return refuse(notifyNoHome)
	}
	if req.Thread != "" && !n.threadOK(req.Thread) {
		return refuse(fmt.Sprintf("thread %q is not a thread of the home channel", req.Thread))
	}
	if len(req.Blocks) > 0 {
		if _, ok := n.poster.(notifyBlocksPoster); !ok {
			return refuse("this backend posts text only; send the request without blocks")
		}
		var blocks []any
		if err := json.Unmarshal(req.Blocks, &blocks); err != nil || len(blocks) == 0 {
			return refuse("blocks must be a non-empty JSON array of Block Kit blocks")
		}
		if why := blocksMention(blocks); why != "" {
			return refuse("blocks carry a mention (" + why + "); the notify route posts no mentions, send them as text")
		}
	}
	return req, nil
}

// validateConversation admits a request aimed at a conversation only when
// the gateway holds a session record for it whose context id is the one the
// request carries: a conversation the agent is working in. The bound is the
// set of live records, not a secret: the agent principal, the one sender, can
// read every live conversation's context id (its Hermes sessions are named
// for them), so this refuses a conversation with no record, a made-up key
// and a stale route, not a post into another conversation the agent holds a
// session for. Anything else is refused before any post.
func (n *Notifier) validateConversation(req lib.NotifyRequest) (lib.NotifyRequest, *lib.NotifyReply) {
	refuse := func(reason string) (lib.NotifyRequest, *lib.NotifyReply) {
		r := n.refuse(reason)
		return req, &r
	}
	if n.conv == nil {
		return refuse(notifyNoConversations)
	}
	if req.Thread != "" {
		return refuse("a request names a thread or a conversation, not both")
	}
	if req.Conversation == "" || req.ContextID == "" {
		return refuse("a conversation request needs both the conversation and its context id")
	}
	if len(req.Blocks) > 0 {
		// Raw blocks are for home-channel posts; a conversation post carries
		// its layout as Chat, rendered by the gateway.
		return refuse("a conversation request carries no blocks")
	}
	if !strings.HasPrefix(req.Conversation, n.convPrefix) || len(req.Conversation) == len(n.convPrefix) {
		return refuse(fmt.Sprintf("conversation %q is not on this route's backend", req.Conversation))
	}
	ctx, cancel := context.WithTimeout(context.Background(), notifyLookupTimeout)
	defer cancel()
	have, err := n.conv.ConversationContext(ctx, req.Conversation)
	if err != nil {
		n.log.Warn("notify: session record unreadable", "conversation", req.Conversation, "err", err)
		return refuse(notifyConversationRefused)
	}
	if have == "" || have != req.ContextID {
		n.log.Warn("notify refused: no live conversation with that context", "conversation", req.Conversation)
		return refuse(notifyConversationRefused)
	}
	return req, nil
}

// post writes one accepted request, chunked, and answers it exactly once:
// after the first chunk lands, with where it landed, or with the reason it did
// not. The remaining chunks follow into the same thread; a failure among them
// is logged, since the caller already holds its answer and the start of the
// text is in the channel.
func (n *Notifier) post(job notifyJob) {
	if len(job.req.Blocks) > 0 {
		n.postBlocks(job)
		return
	}
	thread := job.req.Thread
	if job.req.Conversation != "" {
		thread = job.req.Conversation
	}
	var first string
	answered := false
	for i, chunk := range chatChunks(job.req.Text, discordChunk) {
		var message, landed string
		var err error
		if job.req.Conversation != "" {
			message, err = n.conv.Post(job.req.Conversation, chunk)
		} else {
			message, landed, err = n.poster.PostNotify(n.home, thread, chunk)
		}
		if err != nil {
			n.log.Error("notify post failed", "home", n.home, "thread", thread, "chunk", i+1, "err", err)
			if !answered {
				n.reply(job, lib.NotifyReply{Error: "post failed: " + err.Error()})
			}
			return
		}
		if landed != "" {
			thread = landed
		}
		if !answered {
			answered = true
			first = message
			n.reply(job, lib.NotifyReply{MessageID: first, ThreadID: thread})
		}
	}
	n.log.Info("notify posted", "home", n.home, "thread", thread, "message", first)
}

// blocksMention reports a mention anywhere in decoded Block Kit, or "": a
// mrkdwn token that pings (<!channel>, <!here>, <!everyone>, <!subteam^…>,
// <@U…>) in any string, or a rich_text element that does (broadcast, user,
// usergroup). The text path escapes < and > so none of these can render
// (toMrkdwn); blocks are posted as Block Kit, so the same bound is a refusal
// instead, and the caller falls back to the escaped text. A link (<https://…>)
// is not a mention and passes.
func blocksMention(v any) string {
	switch node := v.(type) {
	case string:
		for _, token := range notifyMentionTokens {
			if strings.Contains(node, token) {
				return token
			}
		}
	case []any:
		for _, item := range node {
			if why := blocksMention(item); why != "" {
				return why
			}
		}
	case map[string]any:
		if kind, _ := node["type"].(string); slices.Contains(notifyMentionElements, kind) {
			return "a " + kind + " element"
		}
		for _, item := range node {
			if why := blocksMention(item); why != "" {
				return why
			}
		}
	}
	return ""
}

// postBlocks writes a Block Kit request as one message: blocks are not
// chunked, so the text is only the notification and fallback, cut to one
// chunk. validate has already refused blocks to a backend without them.
func (n *Notifier) postBlocks(job notifyJob) {
	poster := n.poster.(notifyBlocksPoster)
	message, landed, err := poster.PostNotifyBlocks(n.home, job.req.Thread, truncateRunes(job.req.Text, discordChunk), job.req.Blocks)
	if err != nil {
		n.log.Error("notify blocks post failed", "home", n.home, "thread", job.req.Thread, "err", err)
		n.reply(job, lib.NotifyReply{Error: "post failed: " + err.Error()})
		return
	}
	n.reply(job, lib.NotifyReply{MessageID: message, ThreadID: landed})
	n.log.Info("notify posted", "home", n.home, "thread", landed, "message", message, "blocks", true)
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

// gatewayConversations is the gateway's NotifyConversations: session records
// from its registry, posts through the backend adapter.
type gatewayConversations struct {
	g      *Gateway
	poster Adapter
}

func (c gatewayConversations) ConversationContext(ctx context.Context, key string) (string, error) {
	rec, err := c.g.reg.Get(ctx, key)
	if err != nil || rec == nil {
		return "", err
	}
	return rec.ContextID, nil
}

func (c gatewayConversations) Post(conversation, text string) (string, error) {
	return c.poster.Post(conversation, text)
}

// Conversations is what Notifier.SetConversations takes: the gateway's
// session records, and poster (the backend adapter, not the composite) to post
// with.
func (g *Gateway) Conversations(poster Adapter) NotifyConversations {
	return gatewayConversations{g: g, poster: poster}
}
