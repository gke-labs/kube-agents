package gateway

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// threadPost is one post a threadedReporter took: the thread it was asked
// to reply on ("" for a new message) and the text.
type threadPost struct{ thread, text string }

// threadedReporter is a home channel that opens a thread per new message
// (t1, t2, ...) and replies on the thread it is given. hold, when set,
// holds every new-message post until it closes, after telling started.
type threadedReporter struct {
	mu      sync.Mutex
	posts   []threadPost
	opened  int
	fail    func(call int) bool
	hold    chan struct{}
	started chan struct{}
}

func (f *threadedReporter) homeChannel() string { return "C0HOME" }

func (f *threadedReporter) Backend() string { return lib.NotifyPlatformSlack }

func (f *threadedReporter) postHomeReport(_ context.Context, home, thread, text string) (homePost, error) {
	if thread == "" && f.hold != nil {
		f.started <- struct{}{}
		<-f.hold
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.posts = append(f.posts, threadPost{thread, text})
	if f.fail != nil && f.fail(len(f.posts)) {
		return homePost{}, errors.New("slack answered 503")
	}
	if thread == "" {
		f.opened++
		thread = fmt.Sprintf("t%d", f.opened)
	}
	return homePost{Platform: lib.NotifyPlatformSlack, ChatID: home, ThreadID: thread, MessageID: "m" + thread}, nil
}

func (f *threadedReporter) all() []threadPost {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]threadPost(nil), f.posts...)
}

// incidentRecorder is the incident store as the scheduled delivery writes it.
type incidentRecorder struct {
	mu      sync.Mutex
	kept    []string
	sources []string
}

func (r *incidentRecorder) keepReportIncident(_ context.Context, platform, chatID, threadID, report, source string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.kept = append(r.kept, platform+"|"+chatID+"|"+threadID+"|"+report)
	r.sources = append(r.sources, source)
	return nil
}

func (r *incidentRecorder) all() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.kept...)
}

// testClock is a clock a test sets.
type testClock struct {
	mu sync.Mutex
	at time.Time
}

func (c *testClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.at
}

func (c *testClock) set(at time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.at = at
}

// deliverRun posts one chat run's report on g, failing the test if it is
// handed back, and returns its record.
func deliverRun(t *testing.T, r *rig, g *Gateway, taskID, text string) *ScheduledReport {
	t.Helper()
	rec, ref, s := scheduledChatRun(taskID)
	if g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, text, terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatalf("%s was handed back", taskID)
	}
	return readScheduledReport(t, r, "compliance-audit", taskID)
}

// threadRig is a rig whose gateway posts to a threadedReporter on a test
// clock, keeping incidents in a recorder.
func threadRig(t *testing.T) (*rig, *threadedReporter, *testClock, *incidentRecorder) {
	t.Helper()
	r := startRig(t)
	home := &threadedReporter{}
	clock := &testClock{at: time.Date(2026, 11, 16, 9, 0, 0, 0, time.UTC)}
	incidents := &incidentRecorder{}
	r.g.setHomeReporters(home)
	r.g.scheduleClock = clock.now
	r.g.reportIncidents = incidents
	return r, home, clock, incidents
}

// TestAScheduleRepliesInTheDaysThread: the first report of a UTC day opens
// a thread, and the day's later reports reply under it.
func TestAScheduleRepliesInTheDaysThread(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	first := deliverRun(t, r, r.g, "task-00000000000000e1", "First run.")
	clock.set(time.Date(2026, 11, 16, 23, 59, 0, 0, time.UTC))
	second := deliverRun(t, r, r.g, "task-00000000000000e2", "Second run.")
	if first.Status != scheduledPosted || second.Status != scheduledPosted || first.ThreadID != "t1" || second.ThreadID != "t1" {
		t.Fatalf("records = %+v / %+v, want both posted in t1", first, second)
	}
	if posts := home.all(); len(posts) != 2 || posts[0].thread != "" || posts[1].thread != "t1" {
		t.Fatalf("posts = %+v, want a new message then a reply in t1", posts)
	}
}

// TestAScheduleOpensANewThreadOnTheNextUTCDay: a report after UTC midnight
// opens the new day's thread.
func TestAScheduleOpensANewThreadOnTheNextUTCDay(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	deliverRun(t, r, r.g, "task-00000000000000f1", "Late run.")
	clock.set(time.Date(2026, 11, 17, 0, 1, 0, 0, time.UTC))
	next := deliverRun(t, r, r.g, "task-00000000000000f2", "Early run.")
	if next.ThreadID != "t2" {
		t.Fatalf("record = %+v, want the new day's thread t2", next)
	}
	if posts := home.all(); len(posts) != 2 || posts[1].thread != "" {
		t.Fatalf("posts = %+v, want two new messages", posts)
	}
}

// TestAScheduleThreadSurvivesAGatewayRestart: the day's thread is read back
// from the bucket by a gateway that never saw it opened.
func TestAScheduleThreadSurvivesAGatewayRestart(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	deliverRun(t, r, r.g, "task-0000000000000011", "Before the restart.")

	fresh, err := New(Options{Client: r.client, Adapter: newFakeAdapter(), Config: r.g.cfg, Backend: "discord"})
	if err != nil {
		t.Fatal(err)
	}
	fresh.setHomeReporters(home)
	fresh.scheduleClock = clock.now
	after := deliverRun(t, r, fresh, "task-0000000000000012", "After the restart.")
	if after.ThreadID != "t1" {
		t.Fatalf("record = %+v, want the day's thread t1", after)
	}
	if posts := home.all(); len(posts) != 2 || posts[1].thread != "t1" {
		t.Fatalf("posts = %+v", posts)
	}
}

// TestTwoFirstPostsOfADayOpenOneThread: two reports racing on a day with no
// thread yet end in one thread: the one that claims the day opens it, and
// the other waits for it and replies under it.
func TestTwoFirstPostsOfADayOpenOneThread(t *testing.T) {
	t.Parallel()
	r, home, _, _ := threadRig(t)
	home.hold, home.started = make(chan struct{}), make(chan struct{}, 2)

	var wg sync.WaitGroup
	records := make([]*ScheduledReport, 2)
	run := func(i int, taskID string) {
		defer wg.Done()
		rec, ref, s := scheduledChatRun(taskID)
		if r.g.deliverScheduledReport(context.Background(), rec, ref, taskID, s, "Race "+taskID, terminalTry{redeliverable: true, delivery: 1}) {
			t.Errorf("%s was handed back", taskID)
		}
		records[i] = readScheduledReport(t, r, "compliance-audit", taskID)
	}
	wg.Add(1)
	go run(0, "task-0000000000000021")
	<-home.started // the first holds the day's claim and is posting
	wg.Add(1)
	go run(1, "task-0000000000000022")
	time.Sleep(3 * scheduleThreadWait) // the second is waiting on the claim
	close(home.hold)
	wg.Wait()

	for i, rep := range records {
		if rep == nil || rep.Status != scheduledPosted || rep.ThreadID != "t1" {
			t.Fatalf("record %d = %+v, want posted in t1", i, rep)
		}
	}
	posts := home.all()
	opened := 0
	for _, p := range posts {
		if p.thread == "" {
			opened++
		}
	}
	if len(posts) != 2 || opened != 1 {
		t.Fatalf("posts = %+v, want one new message and one reply", posts)
	}
}

// TestEveryScheduledPostIsKeptForReplies: each post writes its stored report
// against the thread it landed in.
func TestEveryScheduledPostIsKeptForReplies(t *testing.T) {
	t.Parallel()
	r, _, _, incidents := threadRig(t)
	deliverRun(t, r, r.g, "task-0000000000000031", "One finding.")
	deliverRun(t, r, r.g, "task-0000000000000032", "Two findings.")
	want := []string{
		lib.NotifyPlatformSlack + "|C0HOME|t1|One finding.",
		lib.NotifyPlatformSlack + "|C0HOME|t1|Two findings.",
	}
	if got := incidents.all(); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("incidents = %q, want %q", got, want)
	}
}

// TestAThreadLookupFailureStillPostsTheReport: with the thread bucket gone,
// the report posts as a new message and is recorded posted.
func TestAThreadLookupFailureStillPostsTheReport(t *testing.T) {
	t.Parallel()
	r, home, _, incidents := threadRig(t)
	if err := r.client.JetStream().DeleteKeyValue(context.Background(), lib.ScheduleThreadsBucket); err != nil {
		t.Fatal(err)
	}
	rep := deliverRun(t, r, r.g, "task-0000000000000041", "One finding.")
	if rep.Status != scheduledPosted || rep.ThreadID != "t1" {
		t.Fatalf("record = %+v, want posted", rep)
	}
	if posts := home.all(); len(posts) != 1 || posts[0].thread != "" {
		t.Fatalf("posts = %+v, want one new message", posts)
	}
	if len(incidents.all()) != 1 {
		t.Fatal("the posted report was not kept for replies")
	}
}

// TestAFailedFirstPostReleasesTheDaysClaim: a first post that fails gives
// the day back, so the next report opens the thread rather than waiting on
// a claim nobody will fill.
func TestAFailedFirstPostReleasesTheDaysClaim(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	home.fail = func(call int) bool { return call == 1 }
	rec, ref, s := scheduledChatRun("task-0000000000000051")
	if !r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "First.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("a failed post was not handed back")
	}
	if got, _, _, err := getScheduleThread(r.g.threadStore(context.Background()), scheduleThreadKey("compliance-audit", clock.now())); err != nil || got != nil {
		t.Fatalf("the day's claim was kept after a failed post: %+v (%v)", got, err)
	}
	next := deliverRun(t, r, r.g, "task-0000000000000052", "Second.")
	if next.ThreadID != "t1" {
		t.Fatalf("record = %+v", next)
	}

}

// TestPostHomeReportRepliesOnlyOnAHomeThread: the route replies on a thread
// of its home channel, as a chat.notify request's thread is held, and
// refuses one elsewhere without posting.
func TestPostHomeReportRepliesOnlyOnAHomeThread(t *testing.T) {
	t.Parallel()
	p := &fakeNotifyPoster{}
	n := newTestNotifier(t, p)
	n.jobs, n.done = make(chan notifyJob, notifyQueueDepth), make(chan struct{})
	go n.work()
	t.Cleanup(func() { close(n.jobs) })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	post, err := n.postHomeReport(ctx, testHome, testHome+"/threads/t9", "In the thread.")
	if err != nil || post.ThreadID != testHome+"/threads/t9" {
		t.Fatalf("post = %+v (%v)", post, err)
	}
	if _, err := n.postHomeReport(ctx, testHome, "spaces/OTHER/threads/t1", "Elsewhere."); err == nil {
		t.Fatal("a thread outside the home space was replied on")
	}
	if got := p.all(); len(got) != 1 || got[0].thread != testHome+"/threads/t9" {
		t.Fatalf("posts = %+v", got)
	}
}

// shortPostCtx is a relay batch's context with postTime left for the post
// once the outcome's reserve is set aside.
func shortPostCtx(t *testing.T, postTime time.Duration) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), scheduledKVReserve+postTime)
	t.Cleanup(cancel)
	return ctx
}

// dayThreadOf reads the day's entry for the run's finish day on clock.
func dayThreadOf(t *testing.T, r *rig, at time.Time) *ScheduleThread {
	t.Helper()
	got, _, _, err := getScheduleThread(r.g.threadStore(context.Background()), scheduleThreadKey("compliance-audit", at))
	if err != nil {
		t.Fatal(err)
	}
	return got
}

// TestALiveDayClaimIsWaitedOnAndNotTakenOver: a day whose first post is
// still in flight on another run is waited on for the thread's share of
// this post's time, then handed back as a failed post with nothing posted;
// the claim is taken over only once the server has held it past a post's
// life, with this clock's skew allowed for.
func TestALiveDayClaimIsWaitedOnAndNotTakenOver(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	key := scheduleThreadKey("compliance-audit", clock.now())
	if _, err := putScheduleThread(r.g.threadStore(context.Background()), key,
		ScheduleThread{Claim: "task-other", CreatedAt: clock.now()}, 0); err != nil {
		t.Fatal(err)
	}
	rec, ref, s := scheduledChatRun("task-0000000000000061")
	try := terminalTry{redeliverable: true, delivery: 1}
	start := time.Now()
	if !r.g.deliverScheduledReport(shortPostCtx(t, 1500*time.Millisecond), rec, ref, ref.ID, s, "Waiting.", try) {
		t.Fatal("a post behind a live claim was not handed back")
	}
	if waited := time.Since(start); waited > time.Second {
		t.Fatalf("waited %s on the claim, past the thread's share of a 1.5s post", waited)
	}
	if rep := readScheduledReport(t, r, "compliance-audit", ref.ID); rep.Status != scheduledPosting || rep.LastError == "" {
		t.Fatalf("record = %+v, want posting with the wait as its error", rep)
	}
	if got := dayThreadOf(t, r, clock.now()); got == nil || got.Claim != "task-other" {
		t.Fatalf("the live claim was disturbed: %+v", got)
	}

	// A clock ahead of the server by less than the skew allowed: still live.
	r.g.claimClock = func() time.Time { return time.Now().Add(scheduledPostStale + scheduleClaimSkew - 5*time.Second) }
	try.delivery = 2
	if !r.g.deliverScheduledReport(shortPostCtx(t, 1500*time.Millisecond), rec, ref, ref.ID, s, "Waiting.", try) {
		t.Fatal("a claim inside the skew allowance was taken over")
	}
	if posts := home.all(); len(posts) != 0 {
		t.Fatalf("posted %+v behind a live claim", posts)
	}

	// Held past a post's life and the skew: taken over, and the day opened.
	r.g.claimClock = func() time.Time { return time.Now().Add(scheduledPostStale + scheduleClaimSkew + 5*time.Second) }
	try.delivery = 3
	if r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "Waiting.", try) {
		t.Fatal("a post after a stale claim's takeover was handed back")
	}
	if rep := readScheduledReport(t, r, "compliance-audit", ref.ID); rep.Status != scheduledPosted || rep.ThreadID != "t1" {
		t.Fatalf("record = %+v", rep)
	}
	if got := dayThreadOf(t, r, clock.now()); got == nil || got.ThreadID != "t1" {
		t.Fatalf("the day's thread = %+v", got)
	}
}

// TestASlowThreadStoreStillPostsTheReport: a thread store that is up but
// slower than the thread's share of the post's time costs the thread, not
// the report: the post goes out as a new message inside the post's time and
// is recorded posted, not post_unknown.
func TestASlowThreadStoreStillPostsTheReport(t *testing.T) {
	t.Parallel()
	r, home, _, _ := threadRig(t)
	r.g.scheduledKVHook = func(bucket string) {
		if bucket == lib.ScheduleThreadsBucket {
			time.Sleep(1200 * time.Millisecond)
		}
	}
	rec, ref, s := scheduledChatRun("task-0000000000000071")
	if r.g.deliverScheduledReport(shortPostCtx(t, 3*time.Second), rec, ref, ref.ID, s, "Slow day.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("handed back")
	}
	if rep := readScheduledReport(t, r, "compliance-audit", ref.ID); rep.Status != scheduledPosted {
		t.Fatalf("record = %+v, want posted", rep)
	}
	if posts := home.all(); len(posts) != 1 || posts[0].thread != "" {
		t.Fatalf("posts = %+v, want one new message", posts)
	}
}

// TestARefusedThreadReplyOpensANewThread: a reply the day's thread refuses
// posts once as a new message, which becomes the day's thread.
func TestARefusedThreadReplyOpensANewThread(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	home.fail = func(call int) bool { return call == 2 }
	deliverRun(t, r, r.g, "task-0000000000000081", "Opens t1.")
	second := deliverRun(t, r, r.g, "task-0000000000000082", "Refused in t1.")
	third := deliverRun(t, r, r.g, "task-0000000000000083", "Into t2.")
	if second.Status != scheduledPosted || second.ThreadID != "t2" || third.ThreadID != "t2" {
		t.Fatalf("records = %+v / %+v, want both in t2", second, third)
	}
	want := []threadPost{{"", "Opens t1."}, {"t1", "Refused in t1."}, {"", "Refused in t1."}, {"t2", "Into t2."}}
	if posts := home.all(); fmt.Sprint(posts) != fmt.Sprint(want) {
		t.Fatalf("posts = %+v, want %+v", posts, want)
	}
	if got := dayThreadOf(t, r, clock.now()); got == nil || got.ThreadID != "t2" {
		t.Fatalf("the day's thread = %+v", got)
	}
}

// unknownFirstPost is a home channel whose new-message post never answers in
// time.
type unknownFirstPost struct{ threadedReporter }

func (u *unknownFirstPost) postHomeReport(ctx context.Context, home, thread, text string) (homePost, error) {
	if thread == "" {
		return homePost{}, fmt.Errorf("%w: test", errPostOutcomeUnknown)
	}
	return u.threadedReporter.postHomeReport(ctx, home, thread, text)
}

// TestAFirstPostWithAnUnknownOutcomeKeepsTheClaim: a first post that may
// yet land leaves the day's claim in place, to go stale, rather than give
// the day to a second thread.
func TestAFirstPostWithAnUnknownOutcomeKeepsTheClaim(t *testing.T) {
	t.Parallel()
	r, _, clock, _ := threadRig(t)
	r.g.setHomeReporters(&unknownFirstPost{})
	rep := deliverRun(t, r, r.g, "task-0000000000000091", "Maybe posted.")
	if rep.Status != scheduledPostUnknown {
		t.Fatalf("record = %+v, want post_unknown", rep)
	}
	if got := dayThreadOf(t, r, clock.now()); got == nil || got.Claim != "task-0000000000000091" || got.ThreadID != "" {
		t.Fatalf("the day's entry = %+v, want the run's claim kept", got)
	}
}

// TestARetryAfterMidnightStaysInItsRunsDay: a run that finished before UTC
// midnight and posts after it goes into its own day's thread, not the new
// day's.
func TestARetryAfterMidnightStaysInItsRunsDay(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	day1 := time.Date(2026, 11, 16, 23, 59, 0, 0, time.UTC)
	clock.set(day1)
	home.fail = func(call int) bool { return call == 1 }
	rec, ref, s := scheduledChatRun("task-00000000000000a1")
	if !r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "Late.", terminalTry{redeliverable: true, delivery: 1}) {
		t.Fatal("a failed post was not handed back")
	}
	day2 := time.Date(2026, 11, 17, 0, 1, 0, 0, time.UTC)
	clock.set(day2)
	if r.g.deliverScheduledReport(context.Background(), rec, ref, ref.ID, s, "Late.", terminalTry{redeliverable: true, delivery: 2}) {
		t.Fatal("the retry was handed back")
	}
	next := deliverRun(t, r, r.g, "task-00000000000000a2", "Early.")
	if got := dayThreadOf(t, r, day1); got == nil || got.ThreadID != "t1" {
		t.Fatalf("day one's thread = %+v, want the retried run's t1", got)
	}
	if got := dayThreadOf(t, r, day2); got == nil || got.ThreadID != "t2" || next.ThreadID != "t2" {
		t.Fatalf("day two's thread = %+v, record %+v; want t2", got, next)
	}
}

// TestAHomeChannelChangedTodayPostsANewMessage: a day's thread in a channel
// that is no longer the home channel is left as it is, and the report posts
// as a new message in the new home channel.
func TestAHomeChannelChangedTodayPostsANewMessage(t *testing.T) {
	t.Parallel()
	r, home, clock, _ := threadRig(t)
	key := scheduleThreadKey("compliance-audit", clock.now())
	if _, err := putScheduleThread(r.g.threadStore(context.Background()), key,
		ScheduleThread{Platform: lib.NotifyPlatformSlack, ChatID: "C0OLD", ThreadID: "told", CreatedAt: clock.now()}, 0); err != nil {
		t.Fatal(err)
	}
	rep := deliverRun(t, r, r.g, "task-00000000000000b1", "New home.")
	if rep.Status != scheduledPosted || rep.ChatID != "C0HOME" || rep.ThreadID != "t1" {
		t.Fatalf("record = %+v", rep)
	}
	if posts := home.all(); len(posts) != 1 || posts[0].thread != "" {
		t.Fatalf("posts = %+v, want one new message", posts)
	}
	if got := dayThreadOf(t, r, clock.now()); got == nil || got.ChatID != "C0OLD" || got.ThreadID != "told" {
		t.Fatalf("the day's entry = %+v, want it left as it was", got)
	}
}
