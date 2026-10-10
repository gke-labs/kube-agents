package gateway

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/slack-go/slack/slackevents"
)

// slackEventAdapter is a real SlackAdapter, over the fake Web API, whose Run
// reads message events from a channel and hands them through the adapter's
// own inbound filter, so a test drives the gateway with what Slack would
// deliver rather than with a conversation key it wrote by hand. raw carries
// an InboundMessage straight to the gateway, for the record a previous
// version of the adapter would have keyed. It counts finished turns per
// conversation (InboundObserver), so a test can wait for the gateway to be
// done with a turn rather than sleep and hope.
type slackEventAdapter struct {
	*SlackAdapter
	events chan *slackevents.MessageEvent
	raw    chan InboundMessage

	finishedMu sync.Mutex
	finished   map[string]int
}

func (s *slackEventAdapter) MessageDropped(string, string) {}

func (s *slackEventAdapter) TurnFinished(conversation string) {
	s.finishedMu.Lock()
	defer s.finishedMu.Unlock()
	s.finished[conversation]++
}

func (s *slackEventAdapter) turnsFinished(conversation string) int {
	s.finishedMu.Lock()
	defer s.finishedMu.Unlock()
	return s.finished[conversation]
}

func (s *slackEventAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case m := <-s.events:
			if msg, ok := s.inbound(ctx, m); ok {
				handler(msg)
			}
		case msg := <-s.raw:
			handler(msg)
		}
	}
}

type slackDMRig struct {
	api     *fakeSlackAPI
	adapter *slackEventAdapter
	r       *rig
}

// startSlackDMRig runs a gateway on an embedded server with the Slack
// backend behind slackEventAdapter. U1 is listed and mapped; U2 is neither.
func startSlackDMRig(t *testing.T) *slackDMRig {
	t.Helper()
	return startSlackDMRigWith(t, nil, nil)
}

// startSlackDMRigWith is startSlackDMRig with a spawner (nil: none) and a
// Config tweak (nil: none), for the session route and its cap.
func startSlackDMRigWith(t *testing.T, spawn spawner, tweak func(*Config)) *slackDMRig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("U1 test:one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test-slack-dm"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	api := &fakeSlackAPI{members: []string{"U1"}}
	adapter := &slackEventAdapter{
		SlackAdapter: newTestSlackAdapter(api),
		events:       make(chan *slackevents.MessageEvent, 16),
		raw:          make(chan InboundMessage, 16),
		finished:     map[string]int{},
	}
	cfg := &Config{
		NATSURL:           url,
		PrincipalMapPath:  mapFile,
		DefaultAddressee:  "platform",
		IdleTTL:           30 * time.Minute,
		AttributionSalt:   []byte("test-salt"),
		SlackAllowedUsers: []string{"U1"},
	}
	if tweak != nil {
		tweak(cfg)
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: slackBackend, Spawner: spawn, Logger: slog.Default()})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()
	return &slackDMRig{api: api, adapter: adapter, r: &rig{g: g, client: client, bus: bus, url: url}}
}

type slackPost struct{ channel, thread, text string }

func (d *slackDMRig) posts() []slackPost {
	d.api.mu.Lock()
	defer d.api.mu.Unlock()
	out := make([]slackPost, 0, len(d.api.posted))
	for _, p := range d.api.posted {
		out = append(out, slackPost{p.channel, p.thread, p.text})
	}
	return out
}

// awaitPost waits for a post into channel/thread whose text contains want
// ("" matches any post there).
func (d *slackDMRig) awaitPost(t *testing.T, what, channel, thread, want string) {
	t.Helper()
	waitFor(t, what, func() bool {
		for _, p := range d.posts() {
			if p.channel == channel && p.thread == thread && strings.Contains(p.text, want) {
				return true
			}
		}
		return false
	})
}

// tasks returns the task submissions on the platform's in subject, one per
// task id in first-seen order: a steer reuses its task's id, so it adds no
// entry here.
func (d *slackDMRig) tasks(t *testing.T) []*lib.Envelope {
	t.Helper()
	seen := map[string]bool{}
	var out []*lib.Envelope
	for _, e := range inSubjectEnvelopes(t, d.r.url, "platform") {
		if e.Kind == lib.KindMessage && !seen[e.TaskID] {
			seen[e.TaskID] = true
			out = append(out, e)
		}
	}
	return out
}

func (d *slackDMRig) awaitTasks(t *testing.T, n int) []*lib.Envelope {
	t.Helper()
	var got []*lib.Envelope
	waitFor(t, "task submissions", func() bool {
		got = d.tasks(t)
		return len(got) >= n
	})
	return got
}

// TestSlackDMQuestionsAnswerInTheirOwnThreads is the decided behaviour for
// a Slack DM on next, matching the Hermes Slack platform's default: each
// top-level DM question is its own conversation, answered in its own
// thread, and a reply typed inside that thread is the same conversation --
// a steer while the task runs, a follow-up in the same context after it --
// and stays in that thread. A second top-level question is a second
// conversation with a second thread. Nothing in the DM is posted top-level.
func TestSlackDMQuestionsAnswerInTheirOwnThreads(t *testing.T) {
	d := startSlackDMRig(t)
	ctx := context.Background()

	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the fleet", "100.1", "")
	first := d.awaitTasks(t, 1)[0]
	d.awaitPost(t, "the placeholder in the question's thread", "D1", "100.1", "")

	exec := d.r.execFor(t, first, "platform")
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	// A reply in the thread while the task runs steers that task, and its
	// acknowledgement posts in the thread. The ack's wording is the route's
	// business, so only where it lands is asserted.
	inThread := func() int {
		n := 0
		for _, p := range d.posts() {
			if p.channel == "D1" && p.thread == "100.1" {
				n++
			}
		}
		return n
	}
	before := inThread()
	d.adapter.events <- slackMsg("im", "D1", "U1", "focus on us-east", "100.5", "100.1")
	waitFor(t, "the steer envelope", func() bool {
		return len(inSubjectEnvelopes(t, d.r.url, "platform")) >= 2
	})
	envs := inSubjectEnvelopes(t, d.r.url, "platform")
	if steer := envs[len(envs)-1]; steer.TaskID != first.TaskID {
		t.Fatalf("a reply in the DM thread minted task %s; it should steer %s", steer.TaskID, first.TaskID)
	}
	waitFor(t, "the steer acknowledgement in the thread", func() bool { return inThread() > before })

	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	d.awaitPost(t, "the answer in the question's thread", "D1", "100.1", "the fleet is fine")

	// A reply in the thread after the answer is a follow-up: a new task in
	// the same conversation's context.
	d.adapter.events <- slackMsg("im", "D1", "U1", "and prod?", "100.9", "100.1")
	tasks := d.awaitTasks(t, 2)
	followUp := tasks[1]
	if followUp.ContextID != first.ContextID {
		t.Fatalf("the in-thread follow-up ran in context %s, want the thread's %s", followUp.ContextID, first.ContextID)
	}

	// A second top-level question is a second conversation in its own thread.
	d.adapter.events <- slackMsg("im", "D1", "U1", "what about the nodes", "200.1", "")
	tasks = d.awaitTasks(t, 3)
	second := tasks[2]
	if second.ContextID == first.ContextID {
		t.Fatalf("a second top-level DM shares context %s with the first; it should be its own conversation", second.ContextID)
	}
	d.awaitPost(t, "the second question's placeholder in its own thread", "D1", "200.1", "")

	for _, p := range d.posts() {
		if p.channel == "D1" && p.thread == "" {
			t.Errorf("a post landed top-level in the DM: %+v", p)
		}
	}
}

// TestSlackLegacyDMRecordStillRelaysItsTerminal is the upgrade case. A task
// started under the thread-less key every DM had before DMs threaded,
// slack:dm/{channel}, is still relayed: its placeholder and its answer post
// top-level, where its ask was. A top-level DM sent while it runs is a new
// question in its own thread, not a steer into the old task.
func TestSlackLegacyDMRecordStillRelaysItsTerminal(t *testing.T) {
	d := startSlackDMRig(t)
	ctx := context.Background()

	d.adapter.raw <- InboundMessage{Conversation: "slack:dm/D1", Kind: "dm", AuthorID: "U1", MessageID: "50.1", Text: "an ask from before the upgrade"}
	legacy := d.awaitTasks(t, 1)[0]
	d.awaitPost(t, "the legacy placeholder, top-level", "D1", "", "")
	exec := d.r.execFor(t, legacy, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	d.adapter.events <- slackMsg("im", "D1", "U1", "a new question", "60.1", "")
	tasks := d.awaitTasks(t, 2)
	if tasks[1].TaskID == legacy.TaskID || tasks[1].ContextID == legacy.ContextID {
		t.Fatalf("a top-level DM joined the legacy conversation: task %s context %s", tasks[1].TaskID, tasks[1].ContextID)
	}
	d.awaitPost(t, "the new question's placeholder in its thread", "D1", "60.1", "")

	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "legacy answer"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	d.awaitPost(t, "the legacy task's answer, top-level", "D1", "", "legacy answer")
}

// TestSlackDMRefusalIsOncePerSenderAcrossThreads: the unverified-sender
// notice stays keyed by sender, not by conversation, so a refused sender who
// asks two top-level questions (two conversations now) hears it once, in
// the first question's thread, and nothing is published.
func TestSlackDMRefusalIsOncePerSenderAcrossThreads(t *testing.T) {
	d := startSlackDMRig(t)
	// One at a time: two top-level DMs are two conversations, whose queue
	// workers run concurrently, so sent together either could post first.
	d.adapter.events <- slackMsg("im", "D2", "U2", "drain node 4", "10.1", "")
	d.awaitPost(t, "the unverified-sender notice in the first thread", "D2", "10.1", "can't verify")
	d.adapter.events <- slackMsg("im", "D2", "U2", "drain node 5", "11.1", "")
	waitFor(t, "the gateway to finish the second refused turn", func() bool {
		return d.adapter.turnsFinished("slack:dm/D2/11.1") == 1
	})
	var notices []slackPost
	for _, p := range d.posts() {
		if p.channel == "D2" {
			notices = append(notices, p)
		}
	}
	if len(notices) != 1 {
		t.Fatalf("posts to the refused sender's DM = %+v, want exactly one notice", notices)
	}
	if got := len(inSubjectEnvelopes(t, d.r.url, "platform")); got != 0 {
		t.Errorf("%d task envelopes published for an unlisted sender", got)
	}
}

// TestSlackChannelMentionStillThreadsOnTheMention: the channel path is
// unchanged by DM threading -- a mention roots its session on its own ts and
// the placeholder posts in that thread.
func TestSlackChannelMentionStillThreadsOnTheMention(t *testing.T) {
	d := startSlackDMRig(t)
	d.adapter.events <- slackMsg("channel", "C1", "U1", "<@UBOT> how is the fleet", "300.1", "")
	d.awaitTasks(t, 1)
	d.awaitPost(t, "the placeholder in the mention's thread", "C1", "300.1", "")
}

// TestSlackDMTopLevelStopPointsAtTheThread: a "stop" typed at the top of the
// DM is a conversation of its own, so it cannot reach a question's task; it
// must not claim the DM is idle, and it must say where the stop goes. The
// question's task is not cancelled.
func TestSlackDMTopLevelStopPointsAtTheThread(t *testing.T) {
	d := startSlackDMRig(t)
	ctx := context.Background()
	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the fleet", "100.1", "")
	first := d.awaitTasks(t, 1)[0]
	exec := d.r.execFor(t, first, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	d.adapter.events <- slackMsg("im", "D1", "U1", "stop", "101.1", "")
	d.awaitPost(t, "the notice in the stop's own thread", "D1", "101.1", topLevelNothingRunningNotice)
	for _, e := range inSubjectEnvelopes(t, d.r.url, "platform") {
		if e.TaskID == first.TaskID && e.Kind != lib.KindMessage {
			t.Errorf("a top-level stop sent %s to the question's task", e.Kind)
		}
	}

	// Inside the question's thread, once its task has ended, a stop is
	// already in the right place: the plain notice, not the redirect.
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	d.awaitPost(t, "the answer in the question's thread", "D1", "100.1", "the fleet is fine")
	d.adapter.events <- slackMsg("im", "D1", "U1", "stop", "102.1", "100.1")
	d.awaitPost(t, "the plain notice in the question's thread", "D1", "100.1", "nothing is running")
	for _, p := range d.posts() {
		if p.thread == "100.1" && p.text == toMrkdwn(topLevelNothingRunningNotice) {
			t.Errorf("a stop in the question's own thread was redirected to it: %+v", p)
		}
	}
}

// TestSlackDMTopLevelStatusPointsAtTheThread: a status question typed at the
// top of the DM is a conversation of its own with nothing running in it. It
// must not become a task that reads "any update?"; it gets the same
// redirect a top-level stop gets. The redirect takes the exact status
// phrases only: a top-level question shaped like the wide match ("how is
// the deploy doing") is a real ask with nothing to report on, so it starts
// a task.
func TestSlackDMTopLevelStatusPointsAtTheThread(t *testing.T) {
	d := startSlackDMRig(t)
	ctx := context.Background()
	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the fleet", "100.1", "")
	first := d.awaitTasks(t, 1)[0]
	exec := d.r.execFor(t, first, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	d.adapter.events <- slackMsg("im", "D1", "U1", "any update?", "101.1", "")
	d.awaitPost(t, "the redirect in the status question's own thread", "D1", "101.1", topLevelNothingRunningNotice)
	waitFor(t, "the gateway to finish the status turn", func() bool {
		return d.adapter.turnsFinished("slack:dm/D1/101.1") == 1
	})
	if got := d.tasks(t); len(got) != 1 {
		t.Fatalf("a top-level status question started a task: %d task submissions, want 1", len(got))
	}

	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the deploy doing", "103.1", "")
	d.awaitTasks(t, 2)
	for _, p := range d.posts() {
		if p.thread == "103.1" && p.text == toMrkdwn(topLevelNothingRunningNotice) {
			t.Errorf("a top-level question was redirected as a status poke: %+v", p)
		}
	}
}

// TestSlackDMStopInARefusedThreadGetsThePlainNotice: a question refused at
// the session cap leaves a thread conversation with no task in it. A stop
// typed in that thread is in the right place, so it gets the plain notice,
// not the redirect to the thread it is already in.
func TestSlackDMStopInARefusedThreadGetsThePlainNotice(t *testing.T) {
	spawn := &fakeSpawner{}
	spawn.setLive(1)
	d := startSlackDMRigWith(t, spawn, func(c *Config) {
		c.DefaultAddressee = RouteSession
		c.MaxSessions = 1
	})
	d.adapter.events <- slackMsg("im", "D1", "U1", "how is the fleet", "100.1", "")
	d.awaitPost(t, "the cap refusal in the question's thread", "D1", "100.1", "not started")
	d.adapter.events <- slackMsg("im", "D1", "U1", "stop", "102.1", "100.1")
	d.awaitPost(t, "the plain notice in the refused question's thread", "D1", "100.1", "nothing is running")
	for _, p := range d.posts() {
		if p.text == toMrkdwn(topLevelNothingRunningNotice) {
			t.Errorf("a stop in the question's own thread was redirected: %+v", p)
		}
	}
}

// TestSlackChannelTopLevelControlPhrasesUnchanged: a channel mention roots
// its own thread too, but the redirect is a DM affordance; a channel
// mention reading "stop" gets the plain notice and one reading "any
// update?" is the ordinary turn it always was.
func TestSlackChannelTopLevelControlPhrasesUnchanged(t *testing.T) {
	d := startSlackDMRig(t)
	d.adapter.events <- slackMsg("channel", "C1", "U1", "<@UBOT> stop", "400.1", "")
	d.awaitPost(t, "the plain notice in the mention's thread", "C1", "400.1", "nothing is running")
	d.adapter.events <- slackMsg("channel", "C1", "U1", "<@UBOT> any update?", "401.1", "")
	d.awaitTasks(t, 1)
	for _, p := range d.posts() {
		if p.text == toMrkdwn(topLevelNothingRunningNotice) {
			t.Errorf("a channel mention got the DM redirect: %+v", p)
		}
	}
}

// TestSlackInboundMarksTopLevelDMs: the adapter is the one party that knows
// where a message was typed. A top-level DM is marked; a DM thread reply, a
// channel mention and a channel thread reply are not.
func TestSlackInboundMarksTopLevelDMs(t *testing.T) {
	a := newTestSlackAdapter(&fakeSlackAPI{})
	a.TaskStarted("slack:C1/100.1", "task-0")
	for _, c := range []struct {
		name string
		m    *slackevents.MessageEvent
		want bool
	}{
		{"top-level dm", slackMsg("im", "D1", "U1", "status", "1.0", ""), true},
		{"dm thread reply", slackMsg("im", "D1", "U1", "status", "1.5", "1.0"), false},
		{"channel mention", slackMsg("channel", "C1", "U1", "<@UBOT> status", "2.0", ""), false},
		{"channel thread reply", slackMsg("channel", "C1", "U1", "status", "2.5", "100.1"), false},
	} {
		got, ok := a.inbound(context.Background(), c.m)
		if !ok {
			t.Fatalf("%s: not delivered", c.name)
		}
		if got.TopLevel != c.want {
			t.Errorf("%s: TopLevel = %v, want %v", c.name, got.TopLevel, c.want)
		}
	}
}

// TestSlackDMTopLevelSessionOffPointsAtTheThread: a /session off typed at the
// top of a DM roots a fresh conversation, so it cannot reach the thread the
// user routed. It says where the command goes instead of "not on the session
// route", and a /session off inside a thread keeps the normal reply.
func TestSlackDMTopLevelSessionOffPointsAtTheThread(t *testing.T) {
	d := startSlackDMRigWith(t, &fakeSpawner{}, nil)
	d.adapter.events <- slackMsg("im", "D1", "U1", "/session off", "200.1", "")
	d.awaitPost(t, "the redirect in the command's own thread", "D1", "200.1", "acts on the thread it")

	d.adapter.events <- slackMsg("im", "D1", "U1", "/session off", "201.1", "200.1")
	d.awaitPost(t, "the plain reply inside a thread", "D1", "200.1", "not on the session route")
}
