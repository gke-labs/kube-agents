package gateway

import (
	"context"
	"encoding/json"
	"errors"
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
	n.post(req, func(r lib.NotifyReply) { answers = append(answers, r) })
	if len(answers) != 1 {
		t.Fatalf("post answered %d times, want exactly once: %+v", len(answers), answers)
	}
	return answers[0]
}

func TestNotifyRefusesAHomeThatIsNotASpace(t *testing.T) {
	for _, home := range []string{"", "spaces/", "spaces/A/threads/B", "AAAA", "users/123"} {
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
