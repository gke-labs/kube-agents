package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

type notifyPost struct{ space, thread, text string }

type fakeNotifyPoster struct {
	mu      sync.Mutex
	posts   []notifyPost
	failAt  int // 1-based post index to fail; 0 never
	landsIn string
	noName  bool // answer with an empty message name
}

func (f *fakeNotifyPoster) PostNotify(space, thread, text string) (string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, notifyPost{space, thread, text})
	n := len(f.posts)
	if n == f.failAt {
		return "", "", errors.New("relay answered 403")
	}
	landed := thread
	if landed == "" {
		landed = f.landsIn
	}
	if f.noName {
		return "", landed, nil
	}
	return space + "/messages/m" + string(rune('0'+n)), landed, nil
}

func (f *fakeNotifyPoster) all() []notifyPost {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]notifyPost(nil), f.posts...)
}

const testHome = "spaces/HOME"

func newTestNotifier(t *testing.T, p *fakeNotifyPoster) *Notifier {
	t.Helper()
	n, err := NewGchatNotifier(p, testHome, nil)
	if err != nil {
		t.Fatalf("NewGchatNotifier: %v", err)
	}
	return n
}

func serveJSON(t *testing.T, n *Notifier, req lib.NotifyRequest) lib.NotifyReply {
	t.Helper()
	body, err := json.Marshal(req)
	if err != nil {
		t.Fatal(err)
	}
	return serveBytes(t, n, body)
}

// serveBytes runs one request the way the worker does, synchronously, and
// returns the one answer it gives.
func serveBytes(t *testing.T, n *Notifier, body []byte) lib.NotifyReply {
	t.Helper()
	req, refusal := n.validate(body)
	if refusal != nil {
		return *refusal
	}
	var answers []lib.NotifyReply
	n.post(notifyJob{req: req, answer: func(r lib.NotifyReply) { answers = append(answers, r) }})
	if len(answers) != 1 {
		t.Fatalf("post answered %d times, want exactly once: %+v", len(answers), answers)
	}
	return answers[0]
}

func TestNotifyRefusesAHomeThatIsNotASpace(t *testing.T) {
	for _, home := range []string{"spaces/", "spaces/A/threads/B", "AAAA", "users/123"} {
		if _, err := NewGchatNotifier(&fakeNotifyPoster{}, home, nil); err == nil {
			t.Errorf("home %q accepted; want refused at start", home)
		}
	}
}

func TestNotifyStartsANewThreadInTheHomeSpace(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1"}
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: "drift found"})
	if got.Error != "" {
		t.Fatalf("refused: %s", got.Error)
	}
	posts := p.all()
	if len(posts) != 1 || posts[0] != (notifyPost{testHome, "", "drift found"}) {
		t.Fatalf("posts = %+v, want one new-thread post into the home space", posts)
	}
	if got.ThreadID != testHome+"/threads/T1" || got.MessageID == "" {
		t.Errorf("reply = %+v, want the landed thread and the message", got)
	}
}

func TestNotifyRepliesOnAHomeThread(t *testing.T) {
	p := &fakeNotifyPoster{}
	thread := testHome + "/threads/T9"
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: "follow-up", Thread: thread})
	if got.Error != "" || got.ThreadID != thread {
		t.Fatalf("reply = %+v, want it on %s", got, thread)
	}
	if posts := p.all(); len(posts) != 1 || posts[0].thread != thread || posts[0].space != testHome {
		t.Errorf("posts = %+v", posts)
	}
}

// The home channel is the whole of where a notify may land. A thread in any
// other space, or anything shaped otherwise, is refused before a post.
func TestNotifyRefusesAThreadOutsideTheHomeSpace(t *testing.T) {
	for _, thread := range []string{
		"spaces/OTHER/threads/T1",
		"spaces/HOMEX/threads/T1", // prefix of the home name, not the home
		testHome,
		testHome + "/threads/",
		testHome + "/threads/T1/extra",
		testHome + "/messages/M1",
		"users/123",
	} {
		p := &fakeNotifyPoster{}
		got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: "x", Thread: thread})
		if got.Error == "" {
			t.Errorf("thread %q accepted; want refused", thread)
		}
		if posts := p.all(); len(posts) != 0 {
			t.Errorf("thread %q: posted %+v before refusing", thread, posts)
		}
	}
}

func TestNotifyRefusesMalformedRequests(t *testing.T) {
	n := newTestNotifier(t, &fakeNotifyPoster{})
	for name, body := range map[string][]byte{
		"not json":   []byte("hello"),
		"empty text": []byte(`{"text":"   "}`),
		"too large":  []byte(`{"text":"` + strings.Repeat("a", notifyMaxBody) + `"}`),
	} {
		if got := serveBytes(t, n, body); got.Error == "" {
			t.Errorf("%s: accepted", name)
		}
	}
}

// A long report goes out in chunks, the first starting the thread and the
// rest replying on it, and the reply names the first message.
func TestNotifyChunksALongReportIntoOneThread(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1"}
	text := strings.Repeat("line of a long audit report\n", 200)
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: text})
	posts := p.all()
	if len(posts) < 2 {
		t.Fatalf("posted %d chunks; want the report split", len(posts))
	}
	if posts[0].thread != "" {
		t.Errorf("first chunk thread = %q, want a new thread", posts[0].thread)
	}
	for i, post := range posts[1:] {
		if post.thread != testHome+"/threads/T1" {
			t.Errorf("chunk %d thread = %q, want the first chunk's thread", i+2, post.thread)
		}
	}
	if got.MessageID != testHome+"/messages/m1" || got.ThreadID != testHome+"/threads/T1" {
		t.Errorf("reply = %+v", got)
	}
}

// A failure after the first chunk does not change the answer: the caller
// already has where the text started, and is answered once.
func TestNotifyAnswersOnceWhenALaterChunkFails(t *testing.T) {
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1", failAt: 2}
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: strings.Repeat("x\n", 2000)})
	if got.Error != "" || got.MessageID == "" || got.ThreadID != testHome+"/threads/T1" {
		t.Errorf("reply = %+v, want the first chunk's message and thread", got)
	}
	if n := len(p.all()); n != 2 {
		t.Errorf("posted %d chunks, want 2 (the second failed and the rest were not tried)", n)
	}
}

func TestNotifyReportsAFirstPostFailure(t *testing.T) {
	p := &fakeNotifyPoster{failAt: 1}
	got := serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: "x"})
	if got.Error == "" || got.MessageID != "" {
		t.Errorf("reply = %+v, want the failure and no message", got)
	}
}

// Over the bus: a request whose reply subject is in the notify reply
// namespace is answered there, and one whose reply subject is anywhere else
// is dropped unanswered - the gateway does not reply on a subject the
// requester chose freely.
func TestNotifyAnswersOnlyInTheReplyNamespace(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-gateway"))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()
	p := &fakeNotifyPoster{landsIn: testHome + "/threads/T1"}
	sub, err := newTestNotifier(t, p).Start(client)
	if err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer sub.Stop()

	agent, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	body, _ := json.Marshal(lib.NotifyRequest{Text: "alert"})

	ask := func(reply string) (*nats.Msg, error) {
		in, err := agent.SubscribeSync(reply)
		if err != nil {
			t.Fatal(err)
		}
		defer in.Unsubscribe()
		if err := agent.PublishRequest(lib.NotifySubjectGchat, reply, body); err != nil {
			t.Fatal(err)
		}
		return in.NextMsg(2 * time.Second)
	}

	msg, err := ask(lib.NotifyReplyPrefix + "r1")
	if err != nil {
		t.Fatalf("no answer in the reply namespace: %v", err)
	}
	var got lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &got); err != nil || got.ThreadID != testHome+"/threads/T1" {
		t.Fatalf("answer = %s (%v)", msg.Data, err)
	}

	for _, reply := range []string{"_INBOX.agent.r2", "chat.notify.reply.other.r3", lib.NotifyReplyPrefix[:len(lib.NotifyReplyPrefix)-1]} {
		if msg, err := ask(reply); err == nil {
			t.Errorf("answered on %q (%s); want dropped", reply, msg.Data)
		}
	}
	if n := len(p.all()); n != 1 {
		t.Errorf("posted %d times; the dropped requests must not post", n)
	}
}

type blockingPoster struct{ release chan struct{} }

func (b *blockingPoster) PostNotify(space, thread, text string) (string, string, error) {
	<-b.release
	return space + "/messages/1", space + "/threads/1", nil
}

// A full queue is refused at once, over the bus, rather than left to time
// out: a sender in a loop gets an answer, and the requests already accepted
// still post.
func TestNotifyRefusesWhenTheQueueIsFull(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-gateway"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	poster := &blockingPoster{release: make(chan struct{})}
	n, err := NewGchatNotifier(poster, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Stop()
	defer close(poster.release)

	agent, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	in, err := agent.SubscribeSync(lib.NotifyReplyPrefix + ">")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(lib.NotifyRequest{Text: "x"})
	// One in the worker's hands, notifyQueueDepth queued, one more refused.
	for i := 0; i < notifyQueueDepth+2; i++ {
		if err := agent.PublishRequest(lib.NotifySubjectGchat, lib.NotifyReplyPrefix+"q", body); err != nil {
			t.Fatal(err)
		}
	}
	msg, err := in.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatalf("no refusal while the queue was full: %v", err)
	}
	var got lib.NotifyReply
	if err := json.Unmarshal(msg.Data, &got); err != nil || !strings.Contains(got.Error, "already waiting") {
		t.Errorf("first answer = %s, want the queue-full refusal", msg.Data)
	}
}

// Stop answers every accepted request before the connection closes: the post
// in flight finishes and succeeds, and what is still queued is refused, never
// left silent (silence reads as "may have posted" and is not retried). A
// request arriving after Stop is refused, not sent on the closed queue.
func TestStopAnswersEveryAcceptedRequest(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-gateway"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	poster := &blockingPoster{release: make(chan struct{})}
	n, err := NewGchatNotifier(poster, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}

	agent, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	in, err := agent.SubscribeSync(lib.NotifyReplyPrefix + ">")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(lib.NotifyRequest{Text: "x"})
	for i := 0; i < 3; i++ {
		if err := agent.PublishRequest(lib.NotifySubjectGchat, lib.NotifyReplyPrefix+"s", body); err != nil {
			t.Fatal(err)
		}
	}
	_ = agent.Flush()
	time.Sleep(300 * time.Millisecond) // the worker holds the first; two are queued

	stopped := make(chan struct{})
	go func() { sub.Stop(); close(stopped) }()

	// The two queued requests are refused while the post in flight is still
	// blocked: their answer does not wait on it.
	for i := 0; i < 2; i++ {
		msg, err := in.NextMsg(2 * time.Second)
		if err != nil {
			t.Fatalf("queued request %d not answered while the in-flight post was blocked: %v", i+1, err)
		}
		var got lib.NotifyReply
		_ = json.Unmarshal(msg.Data, &got)
		if got.Error != notifyStoppingRefusal {
			t.Errorf("queued answer %d = %+v, want the stopping refusal", i+1, got)
		}
	}
	close(poster.release)
	msg, err := in.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("the in-flight post was not answered: %v", err)
	}
	var got lib.NotifyReply
	_ = json.Unmarshal(msg.Data, &got)
	if got.MessageID == "" {
		t.Errorf("in-flight answer = %+v, want the post", got)
	}
	select {
	case <-stopped:
	case <-time.After(notifyStopGrace):
		t.Fatal("Stop did not return")
	}

	// After Stop: refused under the lock, no panic on the closed queue.
	n.handle(&nats.Msg{Subject: lib.NotifySubjectGchat, Reply: lib.NotifyReplyPrefix + "late", Data: body})
}

// Run binds and serves, and returns when its context ends.
func TestRunServesUntilItsContextEnds(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-gateway"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	n, err := NewGchatNotifier(&fakeNotifyPoster{landsIn: testHome + "/threads/T1"}, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	runCtx, stopRun := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { n.Run(runCtx, client); close(done) }()

	agent, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer agent.Close()
	body, _ := json.Marshal(lib.NotifyRequest{Text: "x"})
	var answered bool
	for i := 0; i < 20 && !answered; i++ {
		in, _ := agent.SubscribeSync(lib.NotifyReplyPrefix + "run")
		_ = agent.PublishRequest(lib.NotifySubjectGchat, lib.NotifyReplyPrefix+"run", body)
		if _, err := in.NextMsg(250 * time.Millisecond); err == nil {
			answered = true
		}
		_ = in.Unsubscribe()
	}
	if !answered {
		t.Fatal("Run never armed the route")
	}
	stopRun()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return after its context ended")
	}
}

// A bind that cannot succeed (the client is closed) is retried, not given
// up on, until the context ends.
func TestRunRetriesAFailedBind(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-gateway"))
	if err != nil {
		t.Fatal(err)
	}
	client.Close()
	n, err := NewGchatNotifier(&fakeNotifyPoster{}, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	runCtx, stopRun := context.WithTimeout(ctx, 2500*time.Millisecond)
	defer stopRun()
	started := time.Now()
	n.Run(runCtx, client)
	if elapsed := time.Since(started); elapsed < 2*time.Second {
		t.Errorf("Run returned after %s; a failed bind must be retried until the context ends", elapsed)
	}
}

// A poster that returns no message name still gets exactly one answer for a
// text that takes several chunks: the guard is whether it answered, not the
// name it got back.
func TestNotifyAnswersOnceWhenThePostHasNoName(t *testing.T) {
	p := &fakeNotifyPoster{noName: true, landsIn: testHome + "/threads/T1"}
	serveJSON(t, newTestNotifier(t, p), lib.NotifyRequest{Text: strings.Repeat("word ", discordChunk)})
	if got := len(p.all()); got < 2 {
		t.Fatalf("posted %d chunks; the test needs a text that takes several", got)
	}
}

// A request that fails after its requester stopped waiting is logged as lost:
// the requester exited "outcome unknown" and recorded it as possibly posted,
// so the refusal it never hears is the one record of the loss. Before the
// deadline the same failure is an ordinary refusal the requester hears.
func TestNotifyLogsARequestThatFailsAfterItsRequesterGaveUp(t *testing.T) {
	for _, tc := range []struct {
		name     string
		deadline time.Time
		wantLost bool
	}{
		{"past its deadline", time.Now().Add(-time.Second), true},
		{"inside its deadline", time.Now().Add(time.Minute), false},
		{"no deadline given", time.Time{}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var logs bytes.Buffer
			n, err := NewGchatNotifier(&fakeNotifyPoster{failAt: 1}, testHome, slog.New(slog.NewTextHandler(&logs, nil)))
			if err != nil {
				t.Fatal(err)
			}
			var got lib.NotifyReply
			n.post(notifyJob{req: lib.NotifyRequest{Text: "nightly audit: 3 findings\nmore"},
				answer: func(r lib.NotifyReply) { got = r }, deadline: tc.deadline})
			if got.Error == "" {
				t.Fatalf("reply = %+v, want the post failure", got)
			}
			lost := strings.Contains(logs.String(), "notify lost")
			if lost != tc.wantLost {
				t.Fatalf("logged lost = %v, want %v; logs:\n%s", lost, tc.wantLost, logs.String())
			}
			if lost && !strings.Contains(logs.String(), "nightly audit: 3 findings") {
				t.Errorf("the lost line does not name the text's first line:\n%s", logs.String())
			}
		})
	}
}

// The CLI tells the gateway how long it waits, so the gateway can tell a
// refusal the requester hears from one it does not.
func TestNotifyRequestCarriesTheWait(t *testing.T) {
	n := newTestNotifier(t, &fakeNotifyPoster{})
	n.jobs = make(chan notifyJob, 1)
	body, _ := json.Marshal(lib.NotifyRequest{Text: "x", WaitMillis: 60000})
	before := time.Now()
	n.handle(&nats.Msg{Subject: lib.NotifySubjectGchat, Reply: lib.NotifyReplyPrefix + "r", Data: body})
	job := <-n.jobs
	if d := job.deadline.Sub(before); d < 59*time.Second || d > 61*time.Second {
		t.Fatalf("deadline is %s after receipt, want the request's 60s", d)
	}
}

type fakeConversations struct {
	mu       sync.Mutex
	contexts map[string]string
	err      error
	posts    []notifyPost
}

func (f *fakeConversations) ConversationContext(_ context.Context, key string) (string, error) {
	return f.contexts[key], f.err
}

func (f *fakeConversations) Post(conversation, text string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, notifyPost{thread: conversation, text: text})
	return conversation + "/messages/c" + string(rune('0'+len(f.posts))), nil
}

const (
	testConversation = "gchat:spaces/OTHER/threads/T7"
	testContext      = "ctx-0011aabb"
)

// A request aimed at a conversation posts there, through the gateway's own
// adapter, when the conversation's session record carries the request's
// context id: the card's answer goes back to the thread that asked, which
// need not be in the home space.
func TestNotifyPostsIntoALiveConversationWithItsContext(t *testing.T) {
	conv := &fakeConversations{contexts: map[string]string{testConversation: testContext}}
	home := &fakeNotifyPoster{}
	n := newTestNotifier(t, home)
	n.SetConversations(conv)
	got := serveJSON(t, n, lib.NotifyRequest{Text: "3 nodes", Conversation: testConversation, ContextID: testContext})
	if got.Error != "" || got.MessageID == "" || got.ThreadID != testConversation {
		t.Fatalf("reply = %+v, want a post into the conversation", got)
	}
	if len(conv.posts) != 1 || conv.posts[0] != (notifyPost{thread: testConversation, text: "3 nodes"}) {
		t.Fatalf("conversation posts = %+v", conv.posts)
	}
	if len(home.all()) != 0 {
		t.Fatalf("home posts = %+v, want none", home.all())
	}
}

// Anything but a live conversation with that context is refused before any
// post, with one refusal whatever the reason.
func TestNotifyRefusesAConversationItCannotMatch(t *testing.T) {
	for _, tc := range []struct {
		name string
		conv *fakeConversations
		req  lib.NotifyRequest
		want string
	}{
		{"another conversation's context", &fakeConversations{contexts: map[string]string{testConversation: "ctx-other"}},
			lib.NotifyRequest{Conversation: testConversation, ContextID: testContext}, notifyConversationRefused},
		{"no session record", &fakeConversations{contexts: map[string]string{}},
			lib.NotifyRequest{Conversation: testConversation, ContextID: testContext}, notifyConversationRefused},
		{"an unreadable record", &fakeConversations{err: errors.New("kv down")},
			lib.NotifyRequest{Conversation: testConversation, ContextID: testContext}, notifyConversationRefused},
		{"another backend's conversation", &fakeConversations{contexts: map[string]string{"slack:C1/1.2": testContext}},
			lib.NotifyRequest{Conversation: "slack:C1/1.2", ContextID: testContext}, "not on this route's backend"},
		{"no context id", &fakeConversations{contexts: map[string]string{testConversation: ""}},
			lib.NotifyRequest{Conversation: testConversation}, "needs both"},
		{"a thread as well", &fakeConversations{contexts: map[string]string{testConversation: testContext}},
			lib.NotifyRequest{Conversation: testConversation, ContextID: testContext, Thread: testHome + "/threads/T"}, "not both"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			n := newTestNotifier(t, &fakeNotifyPoster{})
			n.SetConversations(tc.conv)
			tc.req.Text = "report"
			got := serveJSON(t, n, tc.req)
			if !strings.Contains(got.Error, tc.want) || got.MessageID != "" {
				t.Fatalf("reply = %+v, want a refusal containing %q", got, tc.want)
			}
			if len(tc.conv.posts) != 0 {
				t.Fatalf("posted %+v", tc.conv.posts)
			}
		})
	}
	n := newTestNotifier(t, &fakeNotifyPoster{})
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "x", Conversation: testConversation, ContextID: testContext}); got.Error != notifyNoConversations {
		t.Fatalf("reply = %+v, want the not-armed refusal from a notifier without conversations", got)
	}
}

// With no home channel the route serves conversation requests and refuses
// a home post.
func TestNotifyWithNoHomeServesConversationsOnly(t *testing.T) {
	n, err := NewGchatNotifier(&fakeNotifyPoster{}, "", nil)
	if err != nil {
		t.Fatalf("NewGchatNotifier with no home: %v", err)
	}
	conv := &fakeConversations{contexts: map[string]string{testConversation: testContext}}
	n.SetConversations(conv)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "alert"}); got.Error != notifyNoHome {
		t.Fatalf("home post reply = %+v, want refused", got)
	}
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "3 nodes", Conversation: testConversation, ContextID: testContext}); got.Error != "" {
		t.Fatalf("conversation post reply = %+v, want posted", got)
	}
}

// The gateway's side reads the session record's context id from its
// registry, and "" for a conversation it holds no record for.
func TestTheGatewaysConversationsReadTheSessionRecord(t *testing.T) {
	r := startRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := r.g.reg.Create(ctx, &SessionRecord{Key: testConversation, ContextID: testContext}); err != nil {
		t.Fatal(err)
	}
	conv := r.g.Conversations(r.adapter)
	if got, err := conv.ConversationContext(ctx, testConversation); err != nil || got != testContext {
		t.Fatalf("ConversationContext = %q, %v; want %q", got, err, testContext)
	}
	if got, err := conv.ConversationContext(ctx, "gchat:spaces/NONE/threads/X"); err != nil || got != "" {
		t.Fatalf("ConversationContext(unknown) = %q, %v; want empty", got, err)
	}
}
