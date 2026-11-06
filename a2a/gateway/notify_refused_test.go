package gateway

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// memRefusedStore is refusedStore in memory, for the notifier's tests.
type memRefusedStore struct {
	mu        sync.Mutex
	posts     []refusedPost
	meta      refusedMeta
	fail      bool
	pruneFail bool // Keep stores, then errors as a failed prune would
	metaFail  int  // Meta errors this many times, then answers
	metaWait  time.Duration
	metaRead  int // Meta calls so far
	keepWait  time.Duration
}

func (m *memRefusedStore) Keep(_ context.Context, post refusedPost) (refusedMeta, error) {
	if m.keepWait > 0 {
		time.Sleep(m.keepWait)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.fail {
		return refusedMeta{}, context.DeadlineExceeded
	}
	m.posts = append(m.posts, post)
	m.meta = metaWith(m.meta, post)
	if m.pruneFail {
		return m.meta, context.DeadlineExceeded
	}
	return m.meta, nil
}

func (m *memRefusedStore) Meta(context.Context) (refusedMeta, error) {
	if m.metaWait > 0 {
		time.Sleep(m.metaWait)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.metaRead++
	if m.metaFail > 0 {
		m.metaFail--
		return refusedMeta{}, context.DeadlineExceeded
	}
	return m.meta, nil
}

func (m *memRefusedStore) Clear(_ context.Context, digested int) (refusedMeta, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.meta = metaLess(m.meta, digested)
	return m.meta, nil
}

func (m *memRefusedStore) reads() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.metaRead
}

func (m *memRefusedStore) Kept(_ context.Context, since time.Time) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	kept := 0
	for _, p := range m.posts {
		if !p.At.Before(since) {
			kept++
		}
	}
	return kept, nil
}

func (m *memRefusedStore) snapshot() ([]refusedPost, refusedMeta) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]refusedPost(nil), m.posts...), m.meta
}

// projectedHome is a home source the test flips, as the operator's
// projected file changes under a running gateway.
type projectedHome struct {
	mu   sync.Mutex
	home string
}

func (h *projectedHome) set(home string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.home = home
}

func (h *projectedHome) read() (string, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.home, true
}

// startWorker runs the notifier's worker without a bus, as Start would, and
// primes the kept count as Start does.
func startWorker(t *testing.T, n *Notifier) {
	t.Helper()
	n.jobs = make(chan notifyJob, notifyQueueDepth)
	n.done = make(chan struct{})
	n.startKeeper()
	go n.work()
	n.primeKept()
	n.keptWG.Wait()
	t.Cleanup(func() {
		n.mu.Lock()
		n.stopping = true
		close(n.jobs)
		if n.keeps != nil {
			close(n.keeps)
		}
		n.mu.Unlock()
		<-n.done
		if n.keepDone != nil {
			<-n.keepDone
		}
	})
}

// waitPosts waits until the poster holds n posts, or the deadline passes.
func waitPosts(p *fakeNotifyPoster, n int) []notifyPost {
	deadline := time.Now().Add(5 * time.Second)
	for len(p.all()) < n && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	return p.all()
}

// waitCleared waits until the store's count is zero, or the deadline passes.
func waitCleared(store *memRefusedStore) refusedMeta {
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, meta := store.snapshot()
		if meta.Count == 0 || !time.Now().Before(deadline) {
			return meta
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// digestPosts is the posts that are digests: those opening with the digest
// head's words before its first verb.
func digestPosts(posts []notifyPost) []notifyPost {
	head := refusedDigestHead[:strings.Index(refusedDigestHead, "%")]
	var out []notifyPost
	for _, p := range posts {
		if strings.HasPrefix(p.text, head) {
			out = append(out, p)
		}
	}
	return out
}

// A home post on a route with no home channel is refused as before, and kept:
// the post, the count and the last subject land in the store.
func TestNotify_ARefusedHomePostIsKeptAndCounted(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewGchatNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{}
	n.SetKept(store)
	startWorker(t, n)

	reply := serveJSON(t, n, lib.NotifyRequest{Text: "🔴 prod-eu: 3 pods CrashLoopBackOff\n\nthe rest of the alert"})
	if reply.Error != notifyNoHomeKept || !reply.Kept {
		t.Fatalf("reply = %+v, want the no-home refusal unchanged", reply)
	}
	n.keptWG.Wait()
	posts, meta := store.snapshot()
	if len(posts) != 1 || posts[0].Text != "🔴 prod-eu: 3 pods CrashLoopBackOff\n\nthe rest of the alert" ||
		posts[0].Subject != "🔴 prod-eu: 3 pods CrashLoopBackOff" || posts[0].Backend != lib.NotifyPlatformGchat {
		t.Fatalf("kept = %+v", posts)
	}
	if meta.Count != 1 || len(meta.Tail) != 1 || meta.Tail[0].Subject != posts[0].Subject || meta.Since.IsZero() {
		t.Fatalf("meta = %+v", meta)
	}
	if posts := poster.all(); len(posts) != 0 {
		t.Fatalf("posted %+v, want nothing", posts)
	}
}

// A store that fails leaves the refusal as it was: the plain no-home
// refusal, not kept, so no sender files the post as handled; nothing posted.
func TestNotify_AKeepThatFailsLeavesTheRefusalAlone(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	n.SetKept(&memRefusedStore{fail: true})
	startWorker(t, n)
	if reply := serveJSON(t, n, lib.NotifyRequest{Text: "an alert"}); reply.Error != notifyNoHome || reply.Kept {
		t.Fatalf("reply = %+v, want the plain refusal", reply)
	}
	n.keptMu.Lock()
	pending := n.keptPending
	n.keptMu.Unlock()
	if pending {
		t.Fatal("a failed keep marked a digest owed")
	}
}

// A prune that fails after the post and the count are written is not a
// failed keep: the requester is told kept, and the digest is owed.
func TestNotify_APruneThatFailsStillKeeps(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{pruneFail: true}
	n.SetKept(store)
	startWorker(t, n)
	if reply := serveJSON(t, n, lib.NotifyRequest{Text: "an alert"}); reply.Error != notifyNoHomeKept || !reply.Kept {
		t.Fatalf("reply = %+v, want kept", reply)
	}
	n.keptMu.Lock()
	pending := n.keptPending
	n.keptMu.Unlock()
	if _, meta := store.snapshot(); meta.Count != 1 || !pending {
		t.Fatalf("meta %+v pending %v, want the post counted and a digest owed", meta, pending)
	}
}

// Refusals arriving faster than a slow store keeps them do not pile up
// without bound: past the queue's depth a post is answered as a plain
// refusal, never as kept, and every requester gets exactly one answer.
func TestNotify_AFullKeepQueueRefusesPlainly(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{keepWait: 50 * time.Millisecond}
	n.SetKept(store)
	startWorker(t, n)
	const burst = notifyQueueDepth * 2
	answers := make(chan lib.NotifyReply, burst)
	for i := 0; i < burst; i++ {
		n.keepRefused(lib.NotifyRequest{Text: "alert"}, func(r lib.NotifyReply) { answers <- r })
	}
	kept, plain := 0, 0
	for i := 0; i < burst; i++ {
		select {
		case r := <-answers:
			switch {
			case r.Kept && r.Error == notifyNoHomeKept:
				kept++
			case !r.Kept && r.Error == notifyNoHome:
				plain++
			default:
				t.Fatalf("answer %+v", r)
			}
		case <-time.After(10 * time.Second):
			t.Fatalf("answered %d of %d", i, burst)
		}
	}
	if plain == 0 || kept == 0 || kept > notifyQueueDepth+1 {
		t.Fatalf("kept %d plain %d, want the queue's depth kept and the rest refused plainly", kept, plain)
	}
	if posts, _ := store.snapshot(); len(posts) != kept {
		t.Fatalf("store holds %d, %d were answered kept", len(posts), kept)
	}
}

// A conversation request on a route with no home channel is not a refused
// home post; nothing is kept. Nor is a reply into a thread of the home
// channel: it gets the plain refusal, so follow-ups and card notes do not
// count as proactive posts in the digest.
func TestNotify_AConversationRequestIsNotKept(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewGchatNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{}
	n.SetKept(store)
	startWorker(t, n)
	serveJSON(t, n, lib.NotifyRequest{Text: "report", Conversation: "gchat:spaces/X/threads/Y", ContextID: "ctx"})
	if reply := serveJSON(t, n, lib.NotifyRequest{Text: "follow-up", Thread: "spaces/H/threads/T"}); reply.Error != notifyNoHome || reply.Kept {
		t.Fatalf("thread reply with no home = %+v, want the plain refusal", reply)
	}
	n.keptWG.Wait()
	if posts, _ := store.snapshot(); len(posts) != 0 {
		t.Fatalf("kept %+v", posts)
	}
}

// When the projected home channel arrives under a running route, the first
// home request that finds it posts the digest beside it: the count, the
// span, the last subjects, the kept count; then the count is cleared and the
// posts stay. A later request posts no second digest.
func TestNotify_AHomeChannelArrivingPostsTheDigestAndClearsTheCount(t *testing.T) {
	poster := &fakeNotifyPoster{landsIn: "1712.0001"}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	projected := &projectedHome{}
	n.SetHomeSource(projected.read)
	store := &memRefusedStore{}
	n.SetKept(store)
	startWorker(t, n)
	for _, text := range []string{"first alert\nbody", "second alert", "third alert"} {
		if reply := serveJSON(t, n, lib.NotifyRequest{Text: text}); reply.Error != notifyNoHomeKept {
			t.Fatalf("reply = %+v", reply)
		}
	}
	n.keptWG.Wait()

	projected.set("C0HOME")
	if reply := serveJSON(t, n, lib.NotifyRequest{Text: "fourth alert, delivered"}); reply.Error != "" {
		t.Fatalf("reply = %+v, want the post to land", reply)
	}
	n.keptWG.Wait()
	posts := waitPosts(poster, 2)
	digests := digestPosts(posts)
	if len(posts) != 2 || len(digests) != 1 || digests[0].space != "C0HOME" || digests[0].thread != "" {
		t.Fatalf("posted %+v, want the request and one digest into the home channel", posts)
	}
	text := digests[0].text
	for _, want := range []string{"3 proactive posts", "The last posts:", "- ", "first alert", "second alert", "third alert", "kept the full text of 3 of them for 30 days"} {
		if !strings.Contains(text, want) {
			t.Errorf("digest lacks %q:\n%s", want, text)
		}
	}
	if strings.Contains(text, "body") || strings.Contains(text, "fourth") {
		t.Errorf("digest carries a body or the delivered post:\n%s", text)
	}
	if meta := waitCleared(store); meta.Count != 0 {
		t.Fatalf("after the digest: meta %+v, want the count cleared", meta)
	}
	if kept, _ := store.snapshot(); len(kept) != 3 {
		t.Fatalf("the clearing dropped kept posts: %d", len(kept))
	}
	serveJSON(t, n, lib.NotifyRequest{Text: "fifth alert, delivered"})
	n.keptWG.Wait()
	time.Sleep(50 * time.Millisecond)
	if digests := digestPosts(poster.all()); len(digests) != 1 {
		t.Fatalf("digests %+v, want no second one", digests)
	}
}

// A home channel set before the route starts (the CR's, on a restart) posts
// the digest once per process; a rebind is not a second arrival; and a count
// read that failed at start is tried again at the next home request, so a
// store unreachable at boot defers the digest rather than losing it.
func TestNotify_StartWithAHomeChannelPostsTheDigestOnce(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-start-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	poster := &fakeNotifyPoster{}
	n, err := NewGchatNotifier(poster, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	at := time.Now().Add(-time.Hour)
	store := &memRefusedStore{metaFail: 1}
	store.posts = []refusedPost{{At: at, Subject: "overnight alert", Text: "overnight alert\nbody"}}
	store.meta = refusedMeta{Count: 1, Since: at, Last: at, Tail: []refusedSubject{{At: at, Subject: "overnight alert"}}}
	n.SetKept(store)

	// The start's read fails: nothing posts, nothing is spent.
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Stop()
	n.keptWG.Wait()
	time.Sleep(50 * time.Millisecond)
	if posts := poster.all(); len(posts) != 0 {
		t.Fatalf("posted %+v after a failed count read", posts)
	}
	// The next home request reads again and the digest posts beside it.
	if reply := serveJSON(t, n, lib.NotifyRequest{Text: "morning report"}); reply.Error != "" {
		t.Fatalf("reply = %+v", reply)
	}
	n.keptWG.Wait()
	n.keptWG.Wait()
	posts := waitPosts(poster, 2)
	digests := digestPosts(posts)
	if len(digests) != 1 || digests[0].space != testHome || !strings.Contains(digests[0].text, "1 proactive post") || !strings.Contains(digests[0].text, "overnight alert") {
		t.Fatalf("posted %+v, want the digest into the home space", posts)
	}
	if meta := waitCleared(store); meta.Count != 0 {
		t.Fatalf("meta after the digest = %+v", meta)
	}
	// A rebind after a lost connection is not a new arrival: a count another
	// process left meanwhile is read by the next process, not by this one.
	if _, err := store.Keep(context.Background(), refusedPost{At: time.Now(), Subject: "left by another process", Text: "x"}); err != nil {
		t.Fatal(err)
	}
	sub.Stop()
	sub2, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	defer sub2.Stop()
	n.keptWG.Wait()
	time.Sleep(50 * time.Millisecond)
	if digests := digestPosts(poster.all()); len(digests) != 1 {
		t.Fatalf("digests %+v, want no second one", digests)
	}
}

// A home channel set before a start with a count left by an earlier process
// posts the digest at the start itself, before any request.
func TestNotify_StartWithAHomeChannelPostsTheDigestBeforeAnyRequest(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-start-test-2"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "C0HOME", nil)
	if err != nil {
		t.Fatal(err)
	}
	at := time.Now().Add(-time.Hour)
	store := &memRefusedStore{}
	store.posts = []refusedPost{{At: at, Subject: "overnight alert", Text: "overnight alert"}}
	store.meta = refusedMeta{Count: 1, Since: at, Last: at, Tail: []refusedSubject{{At: at, Subject: "overnight alert"}}}
	n.SetKept(store)
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Stop()
	posts := waitPosts(poster, 1)
	if len(digestPosts(posts)) != 1 || posts[0].space != "C0HOME" {
		t.Fatalf("posted %+v, want the digest at start", posts)
	}
	if meta := waitCleared(store); meta.Count != 0 {
		t.Fatalf("meta after the digest = %+v", meta)
	}
}

// Stop waits for a keep in flight, so a refused post is not lost to the
// exit.
func TestNotify_StopWaitsForAKeepInFlight(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-stop-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{keepWait: 300 * time.Millisecond}
	n.SetKept(store)
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	// Queued, not awaited, and in flight by the time Stop runs: Stop has to
	// wait it out rather than refuse it.
	n.keepRefused(lib.NotifyRequest{Text: "an alert"}, func(lib.NotifyReply) {})
	time.Sleep(50 * time.Millisecond)
	sub.Stop()
	if posts, _ := store.snapshot(); len(posts) != 1 {
		t.Fatalf("Stop returned before the keep landed: kept %d", len(posts))
	}
}

// Stop answers what is still queued for the keeper: the keep in flight
// finishes, the rest get the stopping refusal without a store call, and no
// requester is left in silence to record its post as possibly posted.
func TestNotify_StopAnswersQueuedKeeps(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("notify-stop-queue-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	poster := &fakeNotifyPoster{}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := &memRefusedStore{keepWait: 300 * time.Millisecond}
	n.SetKept(store)
	sub, err := n.Start(client)
	if err != nil {
		t.Fatal(err)
	}
	const queued = 4
	answers := make(chan lib.NotifyReply, queued)
	for i := 0; i < queued; i++ {
		n.keepRefused(lib.NotifyRequest{Text: "an alert"}, func(r lib.NotifyReply) { answers <- r })
	}
	time.Sleep(50 * time.Millisecond) // the first keep is in flight
	sub.Stop()
	kept, stopping := 0, 0
	for i := 0; i < queued; i++ {
		select {
		case r := <-answers:
			switch r.Error {
			case notifyNoHomeKept:
				kept++
			case notifyStoppingRefusal:
				if !r.Retry {
					t.Fatalf("a stopping refusal not marked retry: %+v", r)
				}
				stopping++
			default:
				t.Fatalf("answer %+v", r)
			}
		default:
			t.Fatalf("only %d of %d answered by the time Stop returned", i, queued)
		}
	}
	if kept != 1 || stopping != queued-1 {
		t.Fatalf("kept %d stopping %d, want the one in flight kept and the rest refused", kept, stopping)
	}
	if posts, _ := store.snapshot(); len(posts) != 1 {
		t.Fatalf("store holds %d posts after Stop, want the one in flight", len(posts))
	}
}

// A refusal kept while the digest's post is in flight is not cleared with
// it: the count keeps that one, and the next arrival digests it.
func TestNotify_ARefusalDuringTheDigestStaysCounted(t *testing.T) {
	poster := &fakeNotifyPoster{landsIn: "1712.0001"}
	n, err := NewSlackNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	projected := &projectedHome{}
	n.SetHomeSource(projected.read)
	store := &memRefusedStore{}
	n.SetKept(store)
	startWorker(t, n)
	for _, text := range []string{"first alert", "second alert", "third alert"} {
		serveJSON(t, n, lib.NotifyRequest{Text: text})
	}
	n.keptWG.Wait()
	// While the digest is on its way out, the home drops and a fourth
	// refusal is kept.
	var once sync.Once
	poster.before = func(text string) {
		if !strings.HasPrefix(text, refusedDigestHead[:strings.Index(refusedDigestHead, "%")]) {
			return
		}
		once.Do(func() {
			projected.set("")
			// Kept by the time it answers (the keeper answers after the
			// store); no keptWG.Wait here, on the worker: the digest's own
			// slot on it is released only once this post has landed.
			serveJSON(t, n, lib.NotifyRequest{Text: "fourth alert, during the digest"})
		})
	}
	projected.set("C0HOME")
	n.digestKept("C0HOME") // no keptWG.Wait here: the hook's keep adds to it from the worker
	posts := waitPosts(poster, 1)
	if digests := digestPosts(posts); len(digests) != 1 || !strings.Contains(digests[0].text, "3 proactive posts") {
		t.Fatalf("posted %+v, want one digest of three", posts)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, meta := store.snapshot(); meta.Count == 1 {
			break
		}
		if !time.Now().Before(deadline) {
			_, meta := store.snapshot()
			t.Fatalf("meta = %+v, want the fourth refusal still counted", meta)
		}
		time.Sleep(10 * time.Millisecond)
	}
	poster.before = nil
	n.digestKept("C0HOME")
	n.keptWG.Wait()
	if digests := digestPosts(waitPosts(poster, 2)); len(digests) != 2 || !strings.Contains(digests[1].text, "1 proactive post ") || !strings.Contains(digests[1].text, "fourth alert") {
		t.Fatalf("digests %+v, want a second one naming the fourth refusal", digests)
	}
	if meta := waitCleared(store); meta.Count != 0 {
		t.Fatalf("meta = %+v after the second digest", meta)
	}
}

// Home requests arriving while the count is being read for the first time
// start no second read: one prime is in flight at a time.
func TestNotify_HomeRequestsDuringThePrimeStartOneRead(t *testing.T) {
	poster := &fakeNotifyPoster{}
	n, err := NewGchatNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	projected := &projectedHome{home: testHome}
	n.SetHomeSource(projected.read)
	store := &memRefusedStore{metaFail: 1, metaWait: 200 * time.Millisecond}
	n.SetKept(store)
	startWorker(t, n) // the first read fails; the route stays unprimed
	for range 5 {
		go func() { // as the worker serves it; no t off the test goroutine
			if req, home, refusal := n.validate([]byte(`{"text":"an alert"}`)); refusal == nil {
				n.post(notifyJob{req: req, home: home, answer: func(lib.NotifyReply) {}})
			}
		}()
	}
	waitPosts(poster, 5)
	n.keptWG.Wait()
	if reads := store.reads(); reads != 2 {
		t.Fatalf("meta read %d times, want the failed one and one retry", reads)
	}
	n.keptMu.Lock()
	primed := n.keptPrimed
	n.keptMu.Unlock()
	if !primed {
		t.Fatal("the retry did not prime the route")
	}
}

// A digest whose post fails keeps the count and stays owed, so the next
// arrival posts it.
func TestNotify_ADigestThatFailsToPostKeepsTheCount(t *testing.T) {
	poster := &fakeNotifyPoster{failAt: 1}
	n, err := NewGchatNotifier(poster, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	projected := &projectedHome{}
	n.SetHomeSource(projected.read)
	store := &memRefusedStore{}
	n.SetKept(store)
	startWorker(t, n)
	serveJSON(t, n, lib.NotifyRequest{Text: "an alert"})
	n.keptWG.Wait()
	projected.set(testHome)
	n.digestKept(testHome)
	n.keptWG.Wait()
	waitPosts(poster, 1)
	time.Sleep(50 * time.Millisecond)
	if _, meta := store.snapshot(); meta.Count != 1 {
		t.Fatalf("meta = %+v, want the count kept after a failed post", meta)
	}
	// The next arrival posts it: the failed post re-armed the digest, and
	// the store still says one, so it is read anew.
	n.digestKept(testHome)
	n.keptWG.Wait()
	if posts := waitPosts(poster, 2); len(digestPosts(posts)) != 2 {
		t.Fatalf("posted %+v, want the digest retried", posts)
	}
	if meta := waitCleared(store); meta.Count != 0 {
		t.Fatalf("meta = %+v after the retried digest", meta)
	}
}

func TestRefusedDigest_Wording(t *testing.T) {
	at := time.Date(2026, 10, 30, 3, 12, 0, 0, time.UTC)
	one := refusedMeta{Count: 1, Since: at, Last: at, Tail: []refusedSubject{{At: at, Subject: "disk full on seeded-a"}}}
	got := refusedDigest(one, 1)
	want := "While no home channel was set, 1 proactive post (alerts and scheduled reports) could not be delivered here, between 2026-10-30 03:12 and 2026-10-30 03:12 UTC.\n" +
		"The last post:\n- 2026-10-30 03:12: disk full on seeded-a\n" +
		"The gateway kept the full text of 1 of them for 30 days."
	if got != want {
		t.Fatalf("got\n%s\nwant\n%s", got, want)
	}
	if refusedDigest(refusedMeta{}, 5) != "" {
		t.Fatal("a digest with nothing refused is not empty")
	}
	many := refusedMeta{Count: 7, Since: at, Last: at.Add(6 * time.Hour)}
	if got := refusedDigest(many, 0); !strings.HasPrefix(got, "While no home channel was set, 7 proactive posts") || strings.Contains(got, "kept the full text") || strings.Contains(got, "The last") {
		t.Fatalf("got\n%s", got)
	}
	// Texts kept from an earlier run of refusals are not counted against this one.
	if got := refusedDigest(refusedMeta{Count: 2, Since: at, Last: at}, 50); !strings.Contains(got, "kept the full text of 2 of them") {
		t.Fatalf("got\n%s", got)
	}
}

// The bucket-backed store keeps posts under its own prefix, counts them,
// prunes past the cap and the age bound, and clears the meta without the
// posts. The registry's listing filter ("sessions.>") never sees the prefix.
func TestKVRefusedStore_KeepsPrunesAndClears(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("refused-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	store := NewRefusedStore(client, lib.NotifyPlatformSlack)
	base := time.Date(2026, 10, 30, 0, 0, 0, 0, time.UTC)
	store.now = func() time.Time { return base.Add(time.Duration(refusedKeepMax+5) * time.Minute) }

	var meta refusedMeta
	for i := 0; i < refusedKeepMax+3; i++ {
		at := base.Add(time.Duration(i) * time.Minute)
		meta, err = store.Keep(ctx, refusedPost{At: at, Backend: lib.NotifyPlatformSlack, Subject: "alert", Text: "alert body"})
		if err != nil {
			t.Fatalf("keep %d: %v", i, err)
		}
	}
	if meta.Count != refusedKeepMax+3 || !meta.Since.Equal(base) || len(meta.Tail) != refusedLastMax {
		t.Fatalf("meta = %+v", meta)
	}
	kept, err := store.Kept(ctx, time.Time{})
	if err != nil || kept != refusedKeepMax {
		t.Fatalf("kept = %d (%v), want the cap %d", kept, err, refusedKeepMax)
	}
	if kept, _ = store.Kept(ctx, base.Add(time.Duration(refusedKeepMax)*time.Minute)); kept != 3 {
		t.Fatalf("kept since the last three = %d, want 3", kept)
	}
	// Age: a clock past the TTL prunes everything on the next keep.
	store.now = func() time.Time { return base.Add(refusedKeepTTL + hoursPerDay*time.Hour) }
	if _, err := store.Keep(ctx, refusedPost{At: store.now(), Subject: "late", Text: "late"}); err != nil {
		t.Fatal(err)
	}
	if kept, _ = store.Kept(ctx, time.Time{}); kept != 1 {
		t.Fatalf("kept = %d after the age prune, want 1", kept)
	}
	read, err := store.Meta(ctx)
	if err != nil || read.Count != refusedKeepMax+4 {
		t.Fatalf("meta = %+v (%v)", read, err)
	}
	// A clear takes off what was digested; a refusal kept meanwhile stands,
	// with the span starting at the oldest entry left in the tail.
	left, err := store.Clear(ctx, read.Count-2)
	if err != nil {
		t.Fatal(err)
	}
	if left.Count != 2 || len(left.Tail) != 2 || left.Tail[0].Subject != "late" || !left.Since.Equal(left.Tail[1].At) {
		t.Fatalf("meta after a partial clear = %+v", left)
	}
	if left, err = store.Clear(ctx, 2); err != nil {
		t.Fatal(err)
	}
	if read, _ = store.Meta(ctx); read.Count != 0 || !read.Since.IsZero() || len(read.Tail) != 0 || left.Count != 0 {
		t.Fatalf("meta after clear = %+v (returned %+v)", read, left)
	}
	if kept, _ = store.Kept(ctx, time.Time{}); kept != 1 {
		t.Fatalf("clear dropped the kept posts: %d", kept)
	}
	// A second backend's store is its own.
	other := NewRefusedStore(client, lib.NotifyPlatformGchat)
	if kept, _ = other.Kept(ctx, time.Time{}); kept != 0 {
		t.Fatalf("the other backend sees %d posts", kept)
	}
	// A listing cut off by its context is an error, not an empty store.
	cut, cancelCut := context.WithCancel(ctx)
	cancelCut()
	if _, err := store.Kept(cut, time.Time{}); err == nil {
		t.Fatal("a cancelled listing read as complete")
	}
	// The registry's sweep lists sessions only.
	reg := NewRegistry(client)
	kv, err := reg.kv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	lister, err := kv.ListKeysFiltered(ctx, "sessions.>")
	if err == nil {
		for key := range lister.Keys() {
			if strings.HasPrefix(key, refusedKeyPrefix) {
				t.Fatalf("a kept post is under the sessions filter: %s", key)
			}
		}
	}
}
