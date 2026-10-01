package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// ---- harness ----------------------------------------------------------

func startServer(t *testing.T) *natsserver.Server {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		JetStream: true,
		StoreDir:  t.TempDir(),
		NoLog:     true,
		NoSigs:    true,
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	return s
}

// provision creates the TASKS stream and session-state bucket the way the W6
// operator's provision Job does.
func provision(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	}); err != nil {
		t.Fatalf("create TASKS: %v", err)
	}
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.SessionStateBucket}); err != nil {
		t.Fatalf("create session-state: %v", err)
	}
}

// deleteTasksStream takes the task stream away, which is how a test makes the
// gateway's submission publish fail for real rather than through a fake. The
// session-state bucket stays, so everything up to the publish still works:
// the session is minted, the task is announced and the placeholder posted.
func deleteTasksStream(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := js.DeleteStream(ctx, lib.TasksStream); err != nil {
		t.Fatalf("delete TASKS: %v", err)
	}
}

type fakePost struct {
	Conversation string
	MessageID    string
	Text         string
}

// fakeAdapter is the backend stand-in: it records posts and edits and hands
// inbound messages to the gateway's handler.
type fakeAdapter struct {
	mu       sync.Mutex
	posts    []fakePost
	edits    []fakePost
	roster   []string
	complete bool
	nextID   int
	inbox    chan InboundMessage
	// failEdits makes the next N Edit calls fail (and go unrecorded), for
	// pinning what the relay does when a Chat edit does not land.
	failEdits int
	// stopped is closed when Run returns, so a test can assert that a
	// backend was actually told to stop rather than left running.
	stopped  chan struct{}
	stopOnce sync.Once
	// stopDelay is how long Run takes to return once told to stop, for
	// pinning that a caller waits for it rather than exiting on its own.
	stopDelay time.Duration
}

func newFakeAdapter() *fakeAdapter {
	return &fakeAdapter{
		inbox:    make(chan InboundMessage, 16),
		roster:   []string{"1001"},
		complete: true,
		stopped:  make(chan struct{}),
	}
}

func (a *fakeAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	defer a.stopOnce.Do(func() { close(a.stopped) })
	for {
		select {
		case <-ctx.Done():
			time.Sleep(a.stopDelay)
			return nil
		case msg := <-a.inbox:
			handler(msg)
		}
	}
}

func (a *fakeAdapter) Post(conversation, text string) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.nextID++
	id := fmt.Sprintf("m%d", a.nextID)
	a.posts = append(a.posts, fakePost{conversation, id, text})
	return id, nil
}

func (a *fakeAdapter) Edit(conversation, messageID, text string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.failEdits > 0 {
		a.failEdits--
		return errors.New("fake edit failure")
	}
	a.edits = append(a.edits, fakePost{conversation, messageID, text})
	return nil
}

func (a *fakeAdapter) Roster(string) ([]string, bool, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	return append([]string(nil), a.roster...), a.complete, nil
}

func (a *fakeAdapter) OpenDirect(userID string) (string, error) {
	return "discord:dm/du-" + userID, nil
}

func (a *fakeAdapter) postTexts() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]string, len(a.posts))
	for i, p := range a.posts {
		out[i] = p.Text
	}
	return out
}

func (a *fakeAdapter) editTexts() []string {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]string, len(a.edits))
	for i, p := range a.edits {
		out[i] = p.Text
	}
	return out
}

type rig struct {
	g       *Gateway
	adapter *fakeAdapter
	client  *lib.Client // the gateway's client
	bus     *lib.Client // a second client playing the executor
	url     string
}

// startRig assembles a gateway on an embedded server, with user 1001 mapped
// to a test principal, and runs it.
func startRig(t *testing.T) *rig {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n1002 test:adam\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	adapter := newFakeAdapter()
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: "platform",
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord"})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()

	return &rig{g: g, adapter: adapter, client: client, bus: bus, url: url}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// inSubjectEnvelopes replays everything on a task's in subject.
func inSubjectEnvelopes(t *testing.T, url, addressee string) []*lib.Envelope {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cons, err := js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{fmt.Sprintf("a2a.tasks.%s.*.in", addressee)},
	})
	if err != nil {
		t.Fatal(err)
	}
	var out []*lib.Envelope
	it, err := cons.Messages()
	if err != nil {
		t.Fatal(err)
	}
	defer it.Stop()
	for {
		it2, cancel2 := context.WithTimeout(ctx, 300*time.Millisecond)
		msg, err := fetchNext(it2, it)
		cancel2()
		if err != nil {
			break
		}
		env, err := lib.ParseEnvelope(msg.Data())
		if err == nil {
			out = append(out, env)
		}
	}
	return out
}

func fetchNext(ctx context.Context, it jetstream.MessagesContext) (jetstream.Msg, error) {
	done := make(chan struct{})
	var msg jetstream.Msg
	var err error
	go func() {
		msg, err = it.Next()
		close(done)
	}()
	select {
	case <-done:
		return msg, err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// executor drives the other side of one task through the lib, the way W7's
// bridge does.
func (r *rig) awaitTask(t *testing.T, addressee string) *lib.Envelope {
	t.Helper()
	var env *lib.Envelope
	waitFor(t, "task submission on "+addressee, func() bool {
		envs := inSubjectEnvelopes(t, r.url, addressee)
		for _, e := range envs {
			if e.Kind == lib.KindMessage && env == nil {
				env = e
				return true
			}
		}
		return false
	})
	return env
}

func (r *rig) execFor(t *testing.T, origin *lib.Envelope, addressee string) *lib.TaskExecution {
	t.Helper()
	exec, err := r.bus.NewTaskExecution(origin, lib.Party{Session: addressee, AgentType: "test-executor"}, addressee)
	if err != nil {
		t.Fatalf("NewTaskExecution: %v", err)
	}
	return exec
}

// ---- tests -------------------------------------------------------------

func TestNewTaskRoutesToPlatformWithMintedIdsAndAuthority(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread1", Kind: "group",
		AuthorID: "1001", MessageID: "d-42", Text: "how is the fleet?",
	}

	origin := r.awaitTask(t, "platform")
	if origin.To == nil || origin.To.Session != "platform" {
		t.Fatalf("to = %+v, want platform", origin.To)
	}
	if !strings.HasPrefix(origin.TaskID, "task-") || !strings.HasPrefix(origin.CorrelationID, "corr-") {
		t.Fatalf("minted ids look wrong: %s / %s", origin.TaskID, origin.CorrelationID)
	}
	if origin.ContextID == "" {
		t.Fatal("contextId missing")
	}

	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatalf("authority block: %v", err)
	}
	if !strings.HasPrefix(auth.Requester.Principal, "hmac:") {
		t.Fatalf("principal not pseudonymized: %q", auth.Requester.Principal)
	}
	if auth.Requester.Backend != "discord" || auth.Requester.VerifiedBy != "principal-map" {
		t.Fatalf("requester = %+v", auth.Requester)
	}
	if auth.Audience.Conversation != "discord:g1/thread1" || !auth.Audience.RosterComplete {
		t.Fatalf("audience = %+v", auth.Audience)
	}
	if string(auth.Grants) != "null" {
		t.Fatalf("grants must stay null, got %s", auth.Grants)
	}

	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if m.Role != "user" || joinTextParts(m.Parts) != "how is the fleet?" {
		t.Fatalf("payload message = %+v", m)
	}
}

// TestUnmappedSenderIsDropped: nothing an unmapped sender types reaches the
// bus, and the drop is visible — one notice per sender, so a real user's
// silent drop doesn't become a support ticket while a repeat-typer still
// can't make the gateway spam the room (chat-adapters card: "say so visibly
// somewhere"). The second message arrives in a DIFFERENT conversation on
// purpose: a channel mention mints a fresh conversation every time, so a
// conversation-scoped dedupe would be no bound at all.
func TestUnmappedSenderIsDropped(t *testing.T) {
	r := startRig(t)
	for i := 0; i < 2; i++ {
		r.adapter.inbox <- InboundMessage{
			Conversation: fmt.Sprintf("discord:g1/thread%d", i), Kind: "group",
			AuthorID: "9999", MessageID: fmt.Sprintf("d-%d", i), Text: "let me in",
		}
	}
	time.Sleep(500 * time.Millisecond)
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("unverified sender reached the bus: %d envelopes", len(envs))
	}
	posts := r.adapter.postTexts()
	if len(posts) != 1 {
		t.Fatalf("drop notice must be once per sender, not once per conversation: one unmapped sender across two conversations produced %d posts: %v", len(posts), posts)
	}
	if !strings.Contains(posts[0], "can't verify") {
		t.Fatalf("drop notice missing: %q", posts[0])
	}
}

// TestVerifiedByNamesTheMechanism: the authority block should say what was
// actually checked, per backend. Google Chat asserted the sender email over
// a topic only its own service accounts may publish to; Slack's sender is
// asserted by Slack over the Socket Mode connection and joined by our
// table; Discord (and anything unlisted) is the test mapping table alone.
func TestVerifiedByNamesTheMechanism(t *testing.T) {
	for backend, want := range map[string]string{
		"gchat":   "chat-event-topic-iam",
		"slack":   "slack-socket-mode+principal-map",
		"discord": "principal-map",
		"":        "principal-map",
	} {
		if got := verifiedByFor(backend); got != want {
			t.Errorf("verifiedByFor(%q) = %q, want %q", backend, got, want)
		}
	}
}

func TestReplyRelayAndRollingProgressLine(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread2"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "do the thing"}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "reading the fleet"}}}); err != nil {
		t.Fatal(err)
	}
	// The rolling line: the placeholder message is edited, not re-posted.
	waitFor(t, "progress edit", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "working") && strings.Contains(e, "reading the fleet") {
				return true
			}
		}
		return false
	})

	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "the fleet is fine"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "the fleet is fine" {
				return true
			}
		}
		return false
	})

	// Terminal releases serialization: the next message is a new task.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "again"}
	waitFor(t, "second task", func() bool {
		count := 0
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage {
				count++
			}
		}
		return count == 2
	})
	envs := inSubjectEnvelopes(t, r.url, "platform")
	if envs[0].ContextID != envs[len(envs)-1].ContextID {
		t.Fatalf("contextId changed across tasks in one conversation: %s vs %s", envs[0].ContextID, envs[len(envs)-1].ContextID)
	}
	if envs[0].TaskID == envs[len(envs)-1].TaskID {
		t.Fatal("second turn reused the first taskId")
	}
}

func TestMessageDuringWorkingIsSteeringOnSameTask(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread3"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	// A second author steers; the steer carries its own authority block.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1002", MessageID: "d-2", Text: "focus on us-east"}
	waitFor(t, "steer envelope", func() bool {
		return len(inSubjectEnvelopes(t, r.url, "platform")) >= 2
	})
	envs := inSubjectEnvelopes(t, r.url, "platform")
	steer := envs[len(envs)-1]
	if steer.TaskID != origin.TaskID {
		t.Fatalf("steer minted a new task: %s vs %s", steer.TaskID, origin.TaskID)
	}
	if steer.CorrelationID != origin.CorrelationID {
		t.Fatalf("steer re-minted correlationId: %s vs %s", steer.CorrelationID, origin.CorrelationID)
	}
	var auth Authority
	if err := json.Unmarshal(steer.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	var originAuth Authority
	_ = json.Unmarshal(origin.Authority, &originAuth)
	if auth.Requester.Principal == originAuth.Requester.Principal {
		t.Fatal("steer must be attributed to its own sender")
	}
	// The steer is acknowledged in-channel - silent absorption looked like
	// a dropped message live.
	waitFor(t, "steer acknowledgement", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "steering sent") {
				return true
			}
		}
		return false
	})
}

func TestStatusQueryAnsweredByReplayNotForwarded(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread4"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress, Parts: []lib.Part{{Kind: "text", Text: "step 2 of 5"}}}); err != nil {
		t.Fatal(err)
	}
	// Let the relay drain so the replay horizon includes the progress event.
	waitFor(t, "relay caught up", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "step 2 of 5") {
				return true
			}
		}
		return false
	})

	before := len(inSubjectEnvelopes(t, r.url, "platform"))
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "What is it doing?"}
	waitFor(t, "replayed status post", func() bool {
		for _, p := range r.adapter.postTexts() {
			// The card carries the state, the progress line, the replay
			// notice, the echoed ask, and the elapsed clock.
			if strings.Contains(p, "working") && strings.Contains(p, "step 2 of 5") &&
				strings.Contains(p, "replay") && strings.Contains(p, "🎯 on: “start”") &&
				strings.Contains(p, "so far") {
				return true
			}
		}
		return false
	})
	if after := len(inSubjectEnvelopes(t, r.url, "platform")); after != before {
		t.Fatalf("status query was forwarded to the executor: %d -> %d envelopes", before, after)
	}
}

func TestStopPublishesCancelAndDetaches(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread5"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "stop"}
	waitFor(t, "cancel envelope", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindCancel && e.TaskID == origin.TaskID {
				return true
			}
		}
		return false
	})

	// Detached: the conversation is released even though no terminal event
	// ever arrives (platform tasks have no janitor yet).
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-3", Text: "new question"}
	waitFor(t, "new task after stop", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage && e.TaskID != origin.TaskID {
				return true
			}
		}
		return false
	})
}

func TestRegistrySurvivesRestartShapedReload(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread6"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")

	// A fresh registry over the same bucket — the restart shape — must
	// rediscover the session and the task index.
	ctx := context.Background()
	reg := NewRegistry(r.client)
	rec, err := reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatalf("session not in KV: %v", err)
	}
	if rec.ContextID != origin.ContextID {
		t.Fatalf("KV contextId %s != envelope %s", rec.ContextID, origin.ContextID)
	}
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID {
		t.Fatalf("active task not recorded: %+v", rec.ActiveTask)
	}
	key, err := reg.SessionForTask(ctx, origin.TaskID)
	if err != nil || key != conv {
		t.Fatalf("task index = %q, %v", key, err)
	}
	sessions, err := reg.Sessions(ctx)
	if err != nil || len(sessions) != 1 {
		t.Fatalf("Sessions() = %d, %v", len(sessions), err)
	}
}

func TestRosterCapAndPseudonyms(t *testing.T) {
	ps := NewPseudonymizer([]byte("salt"))
	pm := &PrincipalMap{m: map[string]string{"1001": "test:bnaylor"}}
	big := make([]string, 40)
	for i := range big {
		big[i] = fmt.Sprintf("u%d", i)
	}
	raw := BuildAuthority(ps, pm, "test:bnaylor", "discord", "1001", "principal-map",
		"discord:g/x", "group", big, true)
	var auth Authority
	if err := json.Unmarshal(raw, &auth); err != nil {
		t.Fatal(err)
	}
	if len(auth.Audience.Roster) != rosterCap {
		t.Fatalf("roster len = %d, want %d", len(auth.Audience.Roster), rosterCap)
	}
	if auth.Audience.RosterComplete {
		t.Fatal("a capped roster must say rosterComplete=false")
	}
	for _, entry := range auth.Audience.Roster {
		if !strings.HasPrefix(entry, "hmac:") {
			t.Fatalf("roster entry not pseudonymized: %q", entry)
		}
	}
	if ps.Hash("x") == NewPseudonymizer([]byte("other")).Hash("x") {
		t.Fatal("pseudonyms must depend on the salt")
	}
}

func TestNormalizePhrases(t *testing.T) {
	for phrase, want := range map[string]bool{
		"What is it doing?": true,
		"what's it doing":   true,
		"STATUS":            true,
		"status update pls": false,
		"do the thing":      false,
	} {
		// Narrow mode: these phrases exercise normalization through the
		// exact set and classify the same way in both modes.
		if got := isStatusQuery(phrase, false); got != want {
			t.Errorf("isStatusQuery(%q, false) = %v, want %v", phrase, got, want)
		}
	}
	if !isStop("Stop!") || !isStop("cancel") || isStop("stop the deploy") {
		t.Error("isStop misclassifies")
	}
}

func TestStaleActiveTaskHealsInsteadOfSteering(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread7"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "done"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "terminal relayed", func() bool {
		for _, p := range r.adapter.postTexts() {
			if p == "done" {
				return true
			}
		}
		return false
	})

	// Fake the relay having missed the terminal: restore ActiveTask in KV,
	// the exact state a transient KV failure on the final event leaves.
	reg := NewRegistry(r.client)
	rec, err := reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID}
	if err := reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}

	// The next message must start a NEW task, not steer the finished one.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "next question"}
	waitFor(t, "healed into a new task", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindMessage && e.TaskID != origin.TaskID {
				return true
			}
		}
		return false
	})
}

// seedTasklessDelegate writes the record #1318 observed: a Delegate whose
// task is on the serialization record but produced NOTHING on its events
// subject — no pod (PodName empty), no terminal for the heal to find, and
// not even a `submitted` for Sweep or the relay to act on. Nothing is
// published to the stream here on purpose; a task with events would pass
// the old heal whether or not the no-events case is handled.
func seedTasklessDelegate(t *testing.T, r *rig, conv string, age time.Duration) *SessionRecord {
	t.Helper()
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-taskless-" + randHex(4), Kind: "group", LastActivity: time.Now().UTC(),
		BusSession: "chat-otter-dead", Addressee: "chat-otter-dead",
		ActiveTask: &ActiveTask{TaskID: "task-never", CorrelationID: "corr-never",
			Ask: "write a haiku", SubmittedAt: time.Now().Add(-age)},
		Tasks: []TaskRef{{ID: "task-never", Addressee: "chat-otter-dead"}},
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	return rec
}

// TestTasklessActiveTaskPastGraceHealsIntoNewDelegation (#1318): an active
// task older than FirstEventGrace with no events at all must release the
// conversation — the next "Delegate:" is a NEW delegation (fresh task, fresh
// session, a spawn), not a steer into the task that never started, and the
// conversation is told why.
func TestTasklessActiveTaskPastGraceHealsIntoNewDelegation(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-taskless-old"
	seedTasklessDelegate(t, r, conv, defaultFirstEventGrace+time.Minute)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "t-1", Text: "Delegate: write a haiku about otters"}
	waitFor(t, "a fresh delegation spawned", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	if call.TaskID == "task-never" || call.Session == "chat-otter-dead" {
		t.Fatalf("delegation reused the dead task or session: %+v", call)
	}
	if steers := inSubjectEnvelopes(t, r.url, "chat-otter-dead"); len(steers) != 0 {
		t.Fatalf("the message was steered into the task that never started: %+v", steers)
	}
	var told bool
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "task-never") && strings.Contains(p, "produced nothing") {
			told = true
		}
	}
	if !told {
		t.Fatalf("the conversation was not told the task produced nothing: %q", r.adapter.postTexts())
	}
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil || rec.ActiveTask == nil || rec.ActiveTask.TaskID != call.TaskID {
		t.Fatalf("record does not serialize on the new task: %+v (err=%v)", rec, err)
	}
}

// TestTasklessActiveTaskInsideGraceStillSteers: the same record younger than
// the grace is a task that may simply not have started yet — a cold pod, a
// slow pull — and clearing it would start a second task under the first.
// The message steers, as before, and nothing is released. A record with no
// SubmittedAt at all has no age to judge and is left alone the same way.
func TestTasklessActiveTaskInsideGraceStillSteers(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	for name, age := range map[string]time.Duration{"discord:g1/thread-taskless-young": time.Minute, "discord:g1/thread-taskless-unaged": 0} {
		conv := name
		rec := seedTasklessDelegate(t, r, conv, age)
		if age == 0 {
			rec.ActiveTask.SubmittedAt = time.Time{}
			if err := r.g.reg.Put(context.Background(), rec); err != nil {
				t.Fatal(err)
			}
		}
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "t-" + conv, Text: "make it about otters"}
		waitFor(t, "steer published to the pending task for "+conv, func() bool {
			for _, e := range inSubjectEnvelopes(t, r.url, "chat-otter-dead") {
				if e.Kind == lib.KindMessage && e.TaskID == "task-never" && e.ContextID == rec.ContextID {
					return true
				}
			}
			return false
		})
		if got := len(spawn.calls()); got != 0 {
			t.Fatalf("%s: a task inside the grace was released: %d spawns", conv, got)
		}
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "produced nothing") {
				t.Fatalf("%s: released a task still inside the grace: %q", conv, p)
			}
		}
		got, err := r.g.reg.Get(context.Background(), conv)
		if err != nil || got == nil || got.ActiveTask == nil || got.ActiveTask.TaskID != "task-never" {
			t.Fatalf("%s: record no longer serializes on the pending task: %+v (err=%v)", conv, got, err)
		}
	}
}

// TestTasklessHealPersistsAcrossCapRefusal: the release is written when the
// heal fires, not at the end of the turn — a Delegate refused at the session
// cap returns before the end-of-turn write, and the conversation must not be
// told it is released while the record still says otherwise.
func TestTasklessHealPersistsAcrossCapRefusal(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 1)
	spawn.setLive(1)
	conv := "discord:g1/thread-taskless-cap"
	seedTasklessDelegate(t, r, conv, defaultFirstEventGrace+time.Minute)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "t-3", Text: "Delegate: write a haiku about otters"}
	waitFor(t, "cap refusal posted", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "cap 1") {
				return true
			}
		}
		return false
	})
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	if rec.ActiveTask != nil {
		t.Fatalf("release announced but not written: %+v", rec.ActiveTask)
	}
}

func TestLongFailureReasonIsChunkedUnderTheCap(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread8"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}

	longReason := strings.Repeat("stack frame käsemesser\n", 300) // ~7KB, multibyte
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed, Message: &lib.Message{
			Role: "agent", MessageID: "msg-fail",
			Parts: []lib.Part{{Kind: "text", Text: longReason}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: "platform"}, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskEventsSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}

	waitFor(t, "chunked failure posts", func() bool {
		joined := ""
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "failed") || strings.Contains(p, "stack frame") {
				joined += p
			}
		}
		return strings.Count(joined, "käsemesser") == 300
	})
	for _, p := range r.adapter.postTexts() {
		if len(p) > discordChunk {
			t.Fatalf("post over the cap: %d bytes", len(p))
		}
		if !utf8.ValidString(p) {
			t.Fatal("post is invalid UTF-8 (rune split at a chunk boundary)")
		}
	}
}

// fakeSpawner records spawn calls; the delegate flow's pod machinery without
// a cluster.
type fakeSpawner struct {
	mu       sync.Mutex
	spawns   []fakeSpawn
	deletes  []string
	orphans  []orphanPod
	live     int
	liveErr  error
	spawnErr error
	// onDelete, when set, observes the moment of deletion — the supervisor
	// tests use it to assert the terminal was already on the stream when
	// the pod went, which "terminal exists and pod deleted" alone cannot.
	onDelete func(podName string)
}

type fakeSpawn struct {
	Session   string
	TaskID    string
	OriginSeq uint64
}

func (s *fakeSpawner) Spawn(_ context.Context, rec *SessionRecord, taskID, _ string, originSeq uint64) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.spawnErr != nil {
		return "", s.spawnErr
	}
	s.spawns = append(s.spawns, fakeSpawn{Session: rec.BusSession, TaskID: taskID, OriginSeq: originSeq})
	return rec.BusSession, nil
}

// failSpawns makes every Spawn fail with err until reset with nil — the
// quota-refusal shape the spawn-failure supervisor test needs.
func (s *fakeSpawner) failSpawns(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.spawnErr = err
}

func (s *fakeSpawner) Delete(_ context.Context, podName string) error {
	s.mu.Lock()
	hook := s.onDelete
	s.deletes = append(s.deletes, podName)
	s.mu.Unlock()
	if hook != nil {
		hook(podName)
	}
	return nil
}

func (s *fakeSpawner) deleted() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.deletes...)
}

func (s *fakeSpawner) TerminalOrphans(context.Context) ([]orphanPod, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]orphanPod(nil), s.orphans...), nil
}

func (s *fakeSpawner) setOrphans(o []orphanPod) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.orphans = o
}

func (s *fakeSpawner) calls() []fakeSpawn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]fakeSpawn(nil), s.spawns...)
}

// startRigWithSpawner is startRig with the session-pod path armed through a
// fake spawner.
func startRigWithSpawner(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerRoute(t, "platform")
}

// startRigWithSpawnerRoute arms the spawner with a chosen default addressee
// - RouteSession is the post-flip W4 configuration.
func startRigWithSpawnerRoute(t *testing.T, defaultAddressee string) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerCap(t, defaultAddressee, 0)
}

// startRigWithSpawnerCap additionally pins the session-pod cap; 0 keeps the
// default (New normalizes it), which is what every pre-cap test wants.
func startRigWithSpawnerCap(t *testing.T, defaultAddressee string, maxSessions int) (*rig, *fakeSpawner) {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n1002 test:adam\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	bus, err := lib.Connect(ctx, url, lib.WithName("executor-test"))
	if err != nil {
		t.Fatalf("executor client: %v", err)
	}
	t.Cleanup(bus.Close)

	adapter := newFakeAdapter()
	spawn := &fakeSpawner{}
	cfg := &Config{
		NATSURL:          url,
		PrincipalMapPath: mapFile,
		DefaultAddressee: defaultAddressee,
		MaxSessions:      maxSessions,
		IdleTTL:          30 * time.Minute,
		AttributionSalt:  []byte("test-salt"),
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", Spawner: spawn})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()

	return &rig{g: g, adapter: adapter, client: client, bus: bus, url: url}, spawn
}

// TestDelegatePrefixSpawnsSessionWorker: the W4 amendment's flow - a
// "delegate" turn mints a session, addresses that one task to it with the
// prefix stripped, and spawns the pod; steers follow the delegated task; the
// next plain ask re-homes to the default addressee.
func TestDelegatePrefixSpawnsSessionWorker(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-d1", Kind: "group",
		AuthorID: "1001", MessageID: "d-100", Text: "Delegate: write a haiku about message buses",
	}

	waitFor(t, "session pod spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	if !strings.HasPrefix(call.Session, "chat-") {
		t.Fatalf("session name %q not <profile>-<animal>", call.Session)
	}

	origin := r.awaitTask(t, call.Session)
	if origin.To == nil || origin.To.Session != call.Session {
		t.Fatalf("to = %+v, want %s", origin.To, call.Session)
	}
	if origin.TaskID != call.TaskID {
		t.Fatalf("spawned for %s, task is %s", call.TaskID, origin.TaskID)
	}
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "write a haiku about message buses" {
		t.Fatalf("prefix not stripped: %q", got)
	}

	// A message while the delegated task runs steers THAT task on the
	// session's in subject.
	exec := r.execFor(t, origin, call.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-d1", Kind: "group",
		AuthorID: "1001", MessageID: "d-101", Text: "make it about NATS",
	}
	waitFor(t, "steer on the session in subject", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, call.Session) {
			var sm lib.Message
			if e.Kind == lib.KindMessage && e.EnvelopeID != origin.EnvelopeID &&
				json.Unmarshal(e.Payload, &sm) == nil && joinTextParts(sm.Parts) == "make it about NATS" {
				return true
			}
		}
		return false
	})

	// Finish the delegated task; the next plain ask re-homes to platform.
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		ArtifactID: "artifact-" + origin.TaskID + "-result", Name: lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "buses hum softly"}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result relayed", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "buses hum softly") {
				return true
			}
		}
		return false
	})

	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-d1", Kind: "group",
		AuthorID: "1001", MessageID: "d-102", Text: "how is the fleet?",
	}
	rehomed := r.awaitTask(t, "platform")
	if rehomed.To == nil || rehomed.To.Session != "platform" {
		t.Fatalf("re-home failed: %+v", rehomed.To)
	}
	if got := len(spawn.calls()); got != 1 {
		t.Fatalf("plain ask spawned a pod: %d spawns", got)
	}
}

// TestDelegateWithoutSpawnerRoutesDefault: with the spawner dark, "delegate"
// is not an affordance - the turn routes to the default addressee with its
// text intact (stripping the prefix without an executor would lie).
func TestDelegateWithoutSpawnerRoutesDefault(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-d2", Kind: "group",
		AuthorID: "1001", MessageID: "d-110", Text: "Delegate: write a haiku",
	}
	origin := r.awaitTask(t, "platform")
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "Delegate: write a haiku" {
		t.Fatalf("text mangled without a spawner: %q", got)
	}
}

// TestDelegatedTaskStatusShapeSteers: the width bias inverts for executors
// that absorb steers. During a delegated task a wide interrogative shape
// must reach the worker as a steer - only the exact phrases stay status
// affordances, or a correction is stolen and answered by replay.
func TestDelegatedTaskStatusShapeSteers(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-d3"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-120", Text: "Delegate: tune the haiku",
	}
	waitFor(t, "session pod spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	origin := r.awaitTask(t, call.Session)
	exec := r.execFor(t, origin, call.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	// A wide status shape on a fixed route; a steer here.
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-121", Text: "what are you doing with the meter",
	}
	waitFor(t, "steer on the session in subject", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, call.Session) {
			var sm lib.Message
			if e.Kind == lib.KindMessage && e.EnvelopeID != origin.EnvelopeID &&
				json.Unmarshal(e.Payload, &sm) == nil &&
				joinTextParts(sm.Parts) == "what are you doing with the meter" {
				return true
			}
		}
		return false
	})

	// The exact phrase is still the status affordance, answered by replay.
	before := len(inSubjectEnvelopes(t, r.url, call.Session))
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-122", Text: "status",
	}
	waitFor(t, "replayed status post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "replay") {
				return true
			}
		}
		return false
	})
	if after := len(inSubjectEnvelopes(t, r.url, call.Session)); after != before {
		t.Fatalf("exact status phrase was forwarded: %d -> %d envelopes", before, after)
	}
}

// TestPreFlipRecordUpgradesToSessionRoute: a record minted before the W4
// flip keeps SessionRouted=false forever, so the re-home branch must upgrade
// it rather than write the literal RouteSession sentinel into Addressee -
// that would publish the task to an addressee no executor owns.
func TestPreFlipRecordUpgradesToSessionRoute(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-preflip"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-preflip", Addressee: "platform", Kind: "group"}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-130", Text: "check the fleet",
	}
	waitFor(t, "spawn for the upgraded record", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	if call.Session == RouteSession || !strings.HasPrefix(call.Session, "chat-") {
		t.Fatalf("upgraded record's session: %q", call.Session)
	}
	origin := r.awaitTask(t, call.Session)
	if origin.To == nil || origin.To.Session != call.Session {
		t.Fatalf("task addressed to %+v, want the minted session %s", origin.To, call.Session)
	}
}

// TestDelegateDeletesPreviousIncarnationPod: re-delegating must not orphan
// the previous incarnation's pod - once PodName is cleared, reap can never
// find it again, so the gateway deletes it instead.
func TestDelegateDeletesPreviousIncarnationPod(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-d4"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-140", Text: "Delegate: first task",
	}
	waitFor(t, "first spawn", func() bool { return len(spawn.calls()) == 1 })
	first := spawn.calls()[0]
	origin := r.awaitTask(t, first.Session)
	exec := r.execFor(t, origin, first.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "terminal relayed", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "non-text result") {
				return true
			}
		}
		return false
	})

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "d-141", Text: "Delegate: second task",
	}
	waitFor(t, "second spawn", func() bool { return len(spawn.calls()) == 2 })
	waitFor(t, "previous incarnation deleted", func() bool {
		deleted := spawn.deleted()
		return len(deleted) == 1 && deleted[0] == first.Session
	})
}

func TestAddresseeForStragglerTask(t *testing.T) {
	rec := &SessionRecord{
		Addressee: "platform",
		Tasks: []TaskRef{
			{ID: "task-old", Addressee: "chat-otter-abcd"},
			{ID: "task-new", Addressee: "platform"},
		},
	}
	if got := rec.AddresseeFor("task-old"); got != "chat-otter-abcd" {
		t.Fatalf("straggler addressee = %q, want the one its subjects carried", got)
	}
	if got := rec.AddresseeFor("task-unknown"); got != "platform" {
		t.Fatalf("unknown task falls back to the record's addressee, got %q", got)
	}
}

// TestTaskEndCountsAsActivity: the idle TTL that bounds a session (the reap,
// and the Slack adapter's session-thread rule through hasSession) counts
// from the task's end, not from the ask that started it. Otherwise a long
// task's thread went quiet the instant its answer posted, and the follow-up
// right after the result — the most ordinary message a session carries —
// was dropped. The relay stamps LastActivity when it clears the task.
func TestTaskEndCountsAsActivity(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-activity"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "a-1", Text: "take your time"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	before, err := r.g.reg.Get(ctx, conv)
	if err != nil || before == nil || before.ActiveTask == nil {
		t.Fatalf("no active task on the record after the ask: %+v err=%v", before, err)
	}
	asked := before.LastActivity
	time.Sleep(50 * time.Millisecond) // so the terminal's stamp is measurably later

	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "done, eventually"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal to release the task", func() bool {
		rec, err := r.g.reg.Get(ctx, conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	after, err := r.g.reg.Get(ctx, conv)
	if err != nil || after == nil {
		t.Fatal(err)
	}
	if !after.LastActivity.After(asked) {
		t.Fatalf("the task's end did not count as activity: LastActivity %v is not after the ask's %v", after.LastActivity, asked)
	}
	if !after.LastTaskActivity.Equal(after.LastActivity) {
		t.Fatalf("the executor's terminal must move the task's own clock with the session's: task=%v session=%v", after.LastTaskActivity, after.LastActivity)
	}
	held, until, err := r.g.hasSession(ctx, conv)
	if err != nil || !held || !until.Equal(after.LastTaskActivity.Add(r.g.cfg.IdleTTL)) {
		t.Fatalf("after the terminal the session must be held until LastTaskActivity+IdleTTL: held=%v until=%v err=%v", held, until, err)
	}
}

// TestSupervisorTerminalIsNotActivity: the reap closes an idle session's
// detached task by publishing a supervisor terminal, and that terminal must
// not re-open the window the reap just closed. Only an executor's end of a
// live task is activity.
func TestSupervisorTerminalIsNotActivity(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-sup-terminal"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "s-1", Text: "long one"}
	origin := r.awaitTask(t, "platform")
	ctx := context.Background()

	// The session went idle with the task stopped and unconfirmed: what the
	// reap sees right before it publishes the supervisor's canceled.
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("no active task after the ask: %+v err=%v", rec, err)
	}
	stale := time.Now().UTC().Add(-2 * r.g.cfg.IdleTTL)
	rec.ActiveTask.Detached = true
	rec.LastActivity, rec.LastTaskActivity = stale, stale
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || held {
		t.Fatalf("an idle session with a detached task must not be held before the terminal: held=%v err=%v", held, err)
	}

	payload, err := json.Marshal(lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateCanceled}, Final: true})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, lib.TaskSupervisorSubject("platform", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the supervisor's terminal to clear the task", func() bool {
		got, err := r.g.reg.Get(ctx, conv)
		return err == nil && got != nil && got.ActiveTask == nil
	})
	after, err := r.g.reg.Get(ctx, conv)
	if err != nil || after == nil {
		t.Fatal(err)
	}
	if !after.LastTaskActivity.Equal(stale) || !after.LastActivity.Equal(stale) {
		t.Fatalf("the supervisor's terminal counted as activity: task=%v session=%v, want both %v", after.LastTaskActivity, after.LastActivity, stale)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || held {
		t.Fatalf("the reap's own terminal re-admitted the thread: held=%v err=%v", held, err)
	}
}

// TestNoTaskTurnDoesNotReadmitAThread: a verified turn that starts nothing
// ("stop" with nothing running) moves the reap's clock, as any turn does,
// but must not move the clock the session-thread rule bounds on. Otherwise
// one "@bot stop" in a thread whose task ended hours ago re-admits it for a
// whole idle TTL.
func TestNoTaskTurnDoesNotReadmitAThread(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-no-task-turn"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "n-1", Text: "first ask"}
	r.awaitTask(t, "platform")
	ctx := context.Background()

	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	stale := time.Now().UTC().Add(-2 * r.g.cfg.IdleTTL)
	rec.ActiveTask = nil
	rec.LastActivity, rec.LastTaskActivity = stale, stale
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || held {
		t.Fatalf("an idle thread must not be held: held=%v err=%v", held, err)
	}

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "n-2", Text: "stop"}
	waitFor(t, "the no-task stop to be answered", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "nothing is running") {
				return true
			}
		}
		return false
	})
	after, err := r.g.reg.Get(ctx, conv)
	if err != nil || after == nil {
		t.Fatal(err)
	}
	if !after.LastActivity.After(stale) {
		t.Fatalf("a verified turn must still count for the reap: LastActivity %v not after %v", after.LastActivity, stale)
	}
	if !after.LastTaskActivity.Equal(stale) {
		t.Fatalf("a turn that started nothing moved the task clock: %v, want %v", after.LastTaskActivity, stale)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || held {
		t.Fatalf("a no-task turn re-admitted the thread: held=%v err=%v", held, err)
	}
}

// TestExecutorCanceledOnAStoppedTaskCountsAsActivity: the executor's
// confirmation of a stop is the answer the user was waiting on, so it moves
// the task's clock like any executor terminal; only the supervisor's (the
// reap's own) does not.
func TestExecutorCanceledOnAStoppedTaskCountsAsActivity(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-exec-canceled"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "x-1", Text: "long one"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil || rec.ActiveTask == nil {
		t.Fatalf("no active task after the ask: %+v err=%v", rec, err)
	}
	stale := time.Now().UTC().Add(-2 * r.g.cfg.IdleTTL)
	rec.ActiveTask.Detached = true
	rec.LastActivity, rec.LastTaskActivity = stale, stale
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the executor's canceled to clear the task", func() bool {
		got, err := r.g.reg.Get(ctx, conv)
		return err == nil && got != nil && got.ActiveTask == nil
	})
	after, err := r.g.reg.Get(ctx, conv)
	if err != nil || after == nil {
		t.Fatal(err)
	}
	if !after.LastTaskActivity.After(stale) {
		t.Fatalf("the executor's canceled on a stopped task did not count as activity: %v", after.LastTaskActivity)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || !held {
		t.Fatalf("after the executor confirmed the stop the thread must still be a session thread: held=%v err=%v", held, err)
	}
}

// TestHealedExecutorTerminalCountsAsActivity: the heal is the executor's
// terminal reaching the record by the other route (the relay missed it), and
// it must move the task's clock the way relayTerminal does, or a healed
// thread goes quiet the moment its lost answer is posted. Driven with a
// "stop" after the heal, which starts nothing, so nothing else restamps.
func TestHealedExecutorTerminalCountsAsActivity(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-heal-activity"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ha-1", Text: "summarize the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "normal retire", func() bool {
		rec, err := r.g.reg.Get(ctx, conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	// The stale record a lost render leaves behind: the task still active
	// on it, both clocks past the TTL.
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatal(err)
	}
	stale := time.Now().UTC().Add(-2 * r.g.cfg.IdleTTL)
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID, Ask: "summarize the fleet", SubmittedAt: stale}
	rec.LastActivity, rec.LastTaskActivity = stale, stale
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ha-2", Text: "stop"}
	waitFor(t, "the heal to post the lost terminal", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, origin.TaskID) && strings.Contains(p, string(lib.StateCompleted)) {
				return true
			}
		}
		return false
	})
	waitFor(t, "the heal's release to be written", func() bool {
		got, err := r.g.reg.Get(ctx, conv)
		return err == nil && got != nil && got.ActiveTask == nil
	})
	after, err := r.g.reg.Get(ctx, conv)
	if err != nil || after == nil {
		t.Fatal(err)
	}
	if !after.LastTaskActivity.After(stale) {
		t.Fatalf("the healed executor terminal did not count as activity: %v", after.LastTaskActivity)
	}
	if held, _, err := r.g.hasSession(ctx, conv); err != nil || !held {
		t.Fatalf("after the heal posted the answer the thread must still be a session thread: held=%v err=%v", held, err)
	}
}

// sessionRigTurn is the one conversation these tests share per rig; a
// helper keeps the InboundMessage shape out of every assertion.
func sessionRigTurn(r *rig, conv, id, text string) {
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: id, Text: text,
	}
}

func postedContaining(r *rig, needle string) func() bool {
	return func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, needle) {
				return true
			}
		}
		return false
	}
}

// TestSessionCommandMarksTheRouteAndTheNextMessageSpawns: a bare /session
// spawns nothing itself (each new task is a fresh incarnation, so an eager
// pod would be retired by the first message); it marks the record and the
// next plain message opens the pod with its text as the task.
func TestSessionCommandMarksTheRouteAndTheNextMessageSpawns(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s1"
	sessionRigTurn(r, conv, "s-100", "/session")
	waitFor(t, "ack", postedContaining(r, "session route on"))
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("/session spawned %d pods; it must spawn none", got)
	}
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil {
		t.Fatalf("record: %v %v", rec, err)
	}
	if !rec.SessionRouted || rec.Profile != "chat" {
		t.Fatalf("record not session-routed after /session: %+v", rec)
	}
	ctxID := rec.ContextID

	sessionRigTurn(r, conv, "s-101", "what is running in kubeagents-system")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	if !strings.HasPrefix(call.Session, "chat-") {
		t.Fatalf("session %q not chat-<animal>-<hex>", call.Session)
	}
	origin := r.awaitTask(t, call.Session)
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "what is running in kubeagents-system" {
		t.Fatalf("task text = %q", got)
	}
	rec, _ = r.g.reg.Get(context.Background(), conv)
	if rec.ContextID != ctxID {
		t.Fatalf("contextId changed across /session: %s -> %s", ctxID, rec.ContextID)
	}
}

// TestSessionCommandWithTextRunsItAsTheFirstTurn: "/session <text>" marks the
// route and spawns once, with <text> as the task.
func TestSessionCommandWithTextRunsItAsTheFirstTurn(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s2"
	sessionRigTurn(r, conv, "s-110", "/session list the pods in ns x")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "list the pods in ns x" {
		t.Fatalf("task text = %q, want the command stripped", got)
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec == nil || !rec.SessionRouted {
		t.Fatalf("record not session-routed: %+v", rec)
	}
}

// TestSessionOffReleasesThePodAndRehomes: once the session task is done,
// /session off deletes the incarnation, clears the route, and the next plain
// message is a platform task.
func TestSessionOffReleasesThePodAndRehomes(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s3"
	sessionRigTurn(r, conv, "s-120", "/session first task")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	origin := r.awaitTask(t, call.Session)
	exec := r.execFor(t, origin, call.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "terminal relayed", postedContaining(r, "non-text result"))

	sessionRigTurn(r, conv, "s-121", "/session off")
	waitFor(t, "off ack", postedContaining(r, "session route off"))
	waitFor(t, "incarnation deleted", func() bool {
		d := spawn.deleted()
		return len(d) == 1 && d[0] == call.Session
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.SessionRouted || rec.PodName != "" || rec.BusSession != "" || rec.Addressee != "platform" {
		t.Fatalf("record after /session off: %+v", rec)
	}

	sessionRigTurn(r, conv, "s-122", "how is the fleet?")
	rehomed := r.awaitTask(t, "platform")
	if rehomed.To == nil || rehomed.To.Session != "platform" {
		t.Fatalf("re-home failed: %+v", rehomed.To)
	}
	if got := len(spawn.calls()); got != 1 {
		t.Fatalf("plain ask after /session off spawned: %d spawns", got)
	}
}

// TestSessionOffRefusesWhileASessionTaskRuns: the way back never kills a
// running task silently; the user stops it first.
func TestSessionOffRefusesWhileASessionTaskRuns(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s4"
	sessionRigTurn(r, conv, "s-130", "/session long task")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	origin := r.awaitTask(t, call.Session)
	exec := r.execFor(t, origin, call.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	sessionRigTurn(r, conv, "s-131", "/session off")
	waitFor(t, "refusal", postedContaining(r, "still running"))
	if d := spawn.deleted(); len(d) != 0 {
		t.Fatalf("/session off deleted %v while the task ran", d)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if !rec.SessionRouted || rec.ActiveTask == nil || rec.ActiveTask.Detached {
		t.Fatalf("record changed by a refused /session off: %+v", rec)
	}
}

// TestSessionOffOffTheRouteIsNotAStop: on a fixed-route conversation with a
// platform task running, "/session off" is answered, not forwarded as a
// stop and not steered into the task.
func TestSessionOffOffTheRouteIsNotAStop(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s5"
	sessionRigTurn(r, conv, "s-140", "check the fleet")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-141", "/session off")
	waitFor(t, "answer", postedContaining(r, "not on the session route"))
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.EnvelopeID == origin.EnvelopeID {
			continue
		}
		t.Fatalf("/session off reached the platform task's in subject as %s", e.Kind)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.Detached {
		t.Fatalf("the platform task was cancelled by /session off: %+v", rec.ActiveTask)
	}
	if len(spawn.calls()) != 0 {
		t.Fatal("/session off spawned")
	}
}

// TestSessionCommandWithoutSpawnerIsNotAnAffordance: with the spawner dark,
// /session answers that sessions are off and routes nothing.
func TestSessionCommandWithoutSpawnerIsNotAnAffordance(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-s6"
	sessionRigTurn(r, conv, "s-150", "/session")
	waitFor(t, "answer", postedContaining(r, "not enabled"))
	if got := len(inSubjectEnvelopes(t, r.url, "platform")); got != 0 {
		t.Fatalf("/session published %d envelope(s) to platform", got)
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec != nil && rec.SessionRouted {
		t.Fatalf("record marked session-routed with no spawner: %+v", rec)
	}
}

// TestSessionCommandOnSessionDefaultChangesNothing: where the default is
// already the session route, both forms are answered and nothing is minted.
func TestSessionCommandOnSessionDefaultChangesNothing(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-s7"
	sessionRigTurn(r, conv, "s-160", "/session")
	waitFor(t, "answer", postedContaining(r, "already a session"))
	sessionRigTurn(r, conv, "s-161", "/session off")
	waitFor(t, "answer", postedContaining(r, "the default on this install"))
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("spawned %d", got)
	}
}

// TestSessionCommandWithTextHonoursTheCap: a refused first turn leaves no
// half-written record - the cap post is the only reply.
func TestSessionCommandWithTextHonoursTheCap(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 1)
	spawn.mu.Lock()
	spawn.live = 1
	spawn.mu.Unlock()
	conv := "discord:g1/thread-s8"
	sessionRigTurn(r, conv, "s-170", "/session first task")
	waitFor(t, "cap refusal", postedContaining(r, "not started: 1 session worker is already running (cap 1)"))
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("spawned %d past the cap", got)
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec != nil && rec.SessionRouted {
		t.Fatalf("refused /session <text> persisted the route: %+v", rec)
	}
}

// TestSessionCommandIgnoredOnAnExplicitCancel: a programmatic cancel is a
// cancel whatever its text says.
func TestSessionCommandIgnoredOnAnExplicitCancel(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s9"
	sessionRigTurn(r, conv, "s-180", "check the fleet")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "s-181",
		Text: "/session", Intent: IntentCancel,
	}
	waitFor(t, "cancel detached the task", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		return rec != nil && rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec.SessionRouted || len(spawn.calls()) != 0 {
		t.Fatalf("a cancel was read as /session: %+v", rec)
	}
}

// TestSessionCommandWhileAPlatformTaskRunsSaysSo: a bare /session during a
// platform task marks the route but must not promise that the next message
// opens a pod - that message steers the running task. The reply says the
// task finishes first, and the message after it is the one that spawns.
func TestSessionCommandWhileAPlatformTaskRunsSaysSo(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s10"
	sessionRigTurn(r, conv, "s-190", "check the fleet")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-191", "/session")
	waitFor(t, "ack", postedContaining(r, "after it opens a session pod"))
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "your next message opens a session pod") {
			t.Fatalf("promised the next message opens a pod while a task runs: %q", p)
		}
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if !rec.SessionRouted || rec.ActiveTask == nil || rec.ActiveTask.Detached {
		t.Fatalf("record after /session during a task: %+v", rec)
	}
	if len(spawn.calls()) != 0 {
		t.Fatal("spawned during a running platform task")
	}
}

// TestSessionCommandWithTextWhileATaskRunsHoldsTheText: the route turns on,
// the text is not sent, and the reply says so.
func TestSessionCommandWithTextWhileATaskRunsHoldsTheText(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s11"
	sessionRigTurn(r, conv, "s-200", "check the fleet")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-201", "/session list the pods")
	waitFor(t, "held", postedContaining(r, "that message was not sent"))
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.EnvelopeID == origin.EnvelopeID {
			continue
		}
		t.Fatalf("the held text reached the platform task as %s", e.Kind)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if !rec.SessionRouted || len(spawn.calls()) != 0 {
		t.Fatalf("record %+v, spawns %d", rec, len(spawn.calls()))
	}
}

// TestSessionCommandTwiceIsAnsweredOnce: a second bare /session on a routed
// conversation is answered and changes nothing.
func TestSessionCommandTwiceIsAnsweredOnce(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s12"
	sessionRigTurn(r, conv, "s-210", "/session")
	waitFor(t, "ack", postedContaining(r, "session route on"))
	sessionRigTurn(r, conv, "s-211", "/session")
	waitFor(t, "already", postedContaining(r, "already on the session route"))
	if len(spawn.calls()) != 0 {
		t.Fatal("a repeated /session spawned")
	}
}

// TestSessionStopIsAHintNotATask: "/session stop" must never mint a task
// whose text reads "stop"; it points at /session off.
func TestSessionStopIsAHintNotATask(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s13"
	sessionRigTurn(r, conv, "s-220", "/session stop")
	waitFor(t, "hint", postedContaining(r, "`/session off`"))
	if len(spawn.calls()) != 0 {
		t.Fatal("/session stop spawned a task")
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec != nil && rec.SessionRouted {
		t.Fatalf("/session stop marked the route: %+v", rec)
	}
}

// TestSessionInfoRepliesStillCountAsActivity: an informational /session
// reply is a turn like "nothing is running" - the idle clock moves.
func TestSessionInfoRepliesStillCountAsActivity(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	conv := "discord:g1/thread-s14"
	sessionRigTurn(r, conv, "s-230", "/session off")
	waitFor(t, "answer", postedContaining(r, "not on the session route"))
	sessionRigTurn(r, conv, "s-231", "/session")
	waitFor(t, "ack", postedContaining(r, "session route on"))
	// The ack is persisted; take its clock, then let only an informational
	// reply move it.
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec == nil || !rec.SessionRouted {
		t.Fatalf("record after the ack: %+v", rec)
	}
	first := rec.LastActivity
	time.Sleep(30 * time.Millisecond)
	sessionRigTurn(r, conv, "s-232", "/session")
	waitFor(t, "already", postedContaining(r, "already on the session route"))
	time.Sleep(50 * time.Millisecond)
	rec, _ = r.g.reg.Get(context.Background(), conv)
	if !rec.LastActivity.After(first) {
		t.Fatalf("informational reply did not move LastActivity: %s then %s", first, rec.LastActivity)
	}
}

// TestSessionStatusIsAFirstTurn: the spec's sentence - "/session status" is
// /session with the text "status", a first turn, not a status ask.
func TestSessionStatusIsAFirstTurn(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s15"
	sessionRigTurn(r, conv, "s-240", "/session status")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "status" {
		t.Fatalf("task text = %q", got)
	}
}
