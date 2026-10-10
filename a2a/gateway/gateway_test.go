package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
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

	"github.com/gke-labs/kube-agents/a2a/capability"
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

// provision creates the TASKS stream and the session-state and cap buckets
// the way the W6 operator's provision Job does.
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
	// `nats kv add cap`, history 1: one live revision per key, which is what
	// the chain walk's revision pinning is written against.
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: capability.Bucket, History: 1}); err != nil {
		t.Fatalf("create cap: %v", err)
	}
}

// deleteTasksStream takes the task stream away, which is how a test makes the
// gateway's submission publish fail for real rather than through a fake. The
// session-state bucket stays, so everything up to the publish still works:
// the session is minted, the task is announced and the placeholder posted.
//
// Not before the gateway's relay has bound its durable on the stream, though.
// The rig starts Run on a goroutine, and Run binds the relay before it starts
// the adapter that reads the inbox; a stream deleted ahead of that bind leaves
// Run retrying "stream not found" for the whole bind window (lib's
// subscribeBindWindow, 45s), so no turn is ever read and the test times out
// waiting for a failure the gateway never got to have. Under a loaded
// `go test ./...` the goroutine can lose that race.
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
	waitFor(t, "the relay durable to bind", func() bool {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, err := js.Consumer(ctx, lib.TasksStream, relayDurable)
		return err == nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := js.DeleteStream(ctx, lib.TasksStream); err != nil {
		t.Fatalf("delete TASKS: %v", err)
	}
}

// The helper holds the delete until the relay's durable exists: a durable
// created after a delay still binds, because the stream is still there. With
// the wait gone the stream is deleted at once and the late create fails
// "stream not found", which is the race #2696 hit from the gateway's side.
func TestDeleteTasksStreamWaitsForTheRelayDurable(t *testing.T) {
	s := startServer(t)
	provision(t, s.ClientURL())
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	created := make(chan error, 1)
	go func() {
		time.Sleep(300 * time.Millisecond)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{Durable: relayDurable})
		if err != nil {
			// Reported here, at once: a create that fails leaves the helper
			// waiting out its deadline, and its timeout would otherwise be
			// the only message, naming the wait rather than the create.
			t.Errorf("the late durable create failed: %v", err)
		}
		created <- err
	}()
	deleteTasksStream(t, s.ClientURL())
	if err := <-created; err != nil {
		t.Fatalf("the late durable could not bind, so the stream was deleted before it: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.Stream(ctx, lib.TasksStream); !errors.Is(err, jetstream.ErrStreamNotFound) {
		t.Fatalf("TASKS should be gone after the helper returns, got %v", err)
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
	// logs is the gateway's log as text, on the rigs that capture it
	// (startRigWithSpawnerCap); nil elsewhere.
	logs *lockedBuffer
	// stop cancels the gateway's context, on the rigs restartRig can
	// replace (startRigWithSpawnerCap); nil elsewhere.
	stop context.CancelFunc
}

// startRig assembles a gateway on an embedded server, with user 1001 mapped
// to a test principal, and runs it.
func startRig(t *testing.T) *rig {
	t.Helper()
	return startRigWith(t, nil)
}

// startRigWith is startRig with one hook into the Config the gateway is built
// from, for the cases that have to arm a switch before Run starts rather than
// reach into a running gateway.
func startRigWith(t *testing.T, tweak func(*Config)) *rig {
	t.Helper()
	return startRigWithLogger(t, tweak, nil)
}

// startRigWithLogger is startRigWith with the gateway's logger supplied, for
// the cases that assert on what the gateway logs. nil is the default logger.
func startRigWithLogger(t *testing.T, tweak func(*Config), logger *slog.Logger) *rig {
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
	if tweak != nil {
		tweak(cfg)
	}
	g, err := New(Options{Client: client, Adapter: adapter, Config: cfg, Backend: "discord", Logger: logger})
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
	assertRootCapability(t, r, auth, origin.TaskID, "platform")

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
	// ...and to the same capability. One task is one capability, and the
	// reason is the reference rather than the ceiling: Ref pins a key AND a
	// revision, the executor resolved THAT pair when the task opened, and a
	// second mint per turn would hand it a root it never resolved. The
	// verifier walks what the envelope names, so the steer would be checked
	// against an entry whose arrival nothing ordered against the work already
	// in flight.
	//
	// Not a ceiling difference. A steerer has no ceiling of their own here --
	// mintCapability fills Tier and Scope from install-wide config and varies
	// only Delegate, so a re-mint on this turn would produce the same bound
	// with a different revision. An earlier version of this comment said the
	// re-mint would substitute "the steerer's ceiling for the submitter's",
	// which reads as a per-requester bound that this tree does not have.
	if string(auth.Grants) != string(originAuth.Grants) {
		t.Fatalf("the steer carries a different capability:\n  steer  %s\n  origin %s", auth.Grants, originAuth.Grants)
	}
	// The steer is acknowledged in-channel - silent absorption looked like
	// a dropped message live.
	waitFor(t, "steer acknowledgement", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, ackSteerQueued) {
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
	auth := BuildAuthority(ps, pm.Resolve, "test:bnaylor", "discord", "1001", "principal-map",
		"discord:g/x", "group", big, true)
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
		// These phrases exercise normalization through the exact set.
		if got := isStatusQuery(phrase, false); got != want {
			t.Errorf("isStatusQuery(%q) = %v, want %v", phrase, got, want)
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
	r, spawn := startRigWithSpawnerCap(t, "platform", 1, nil)
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

// A refusal reaches chat with the cause the executor named. gke-labs#1884
// put a capability check in front of every task, so an unreachable verifier
// now lands as `rejected` where only an empty submission used to -- and a
// bare "the executor rejected the task" sends the user to re-read their own
// prompt for a fault that is in the install. Asserted on the reason the
// verifier outage produces, not on any refusal, because that is the one the
// bare line was actively misleading about.
func TestARejectedTaskPostsTheReasonTheExecutorGave(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-rejected"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-1", Text: "start"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}

	const reason = "reason: capability-refused - the verifier could not be reached"
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateRejected, Message: &lib.Message{
			Role: "agent", MessageID: "msg-reject",
			Parts: []lib.Part{{Kind: "text", Text: reason}},
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

	waitFor(t, "the refusal posts with its reason", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, reason) {
				return true
			}
		}
		return false
	})
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
	return startRigWithSpawnerCap(t, defaultAddressee, 0, nil)
}

// startRigWithSpawnerCap additionally pins the session-pod cap; 0 keeps the
// default (New normalizes it), which is what every pre-cap test wants. tweak,
// when set, edits the Config before New sees it (an allowlist, a depth bound).
func startRigWithSpawnerCap(t *testing.T, defaultAddressee string, maxSessions int, tweak func(*Config)) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerAdapter(t, defaultAddressee, maxSessions, tweak, nil)
}

// startRigWithSpawnerAdapter is startRigWithSpawnerCap with the adapter the
// gateway drives chosen by wrap, which is handed the rig's fake (nil: the
// fake itself). The rig's posts and edits are still the fake's.
func startRigWithSpawnerAdapter(t *testing.T, defaultAddressee string, maxSessions int, tweak func(*Config), wrap func(*fakeAdapter) Adapter) (*rig, *fakeSpawner) {
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
	if tweak != nil {
		tweak(cfg)
	}
	// The log is captured as well as written, so a test can assert a path
	// that is observable only as its log line (an ignored delegation).
	logs := &lockedBuffer{}
	log := slog.New(slog.NewTextHandler(io.MultiWriter(os.Stderr, logs), nil))
	var driven Adapter = adapter
	if wrap != nil {
		driven = wrap(adapter)
	}
	g, err := New(Options{Client: client, Adapter: driven, Config: cfg, Backend: "discord", Spawner: spawn, Logger: log})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()

	return &rig{g: g, adapter: adapter, client: client, bus: bus, url: url, logs: logs, stop: cancel}, spawn
}

// restartRig stops r's gateway and starts a second one over the same server,
// KV and Config, with a fresh adapter, spawner and log: a gateway restart.
// The first is stopped (context canceled, Run returned, client closed)
// BEFORE the second starts, because the relay durable is shared and two live
// gateways would split its deliveries. The executor client is kept.
//
// whileDown, when given, runs between the two: the bus traffic a gateway that
// was down never saw.
func restartRig(t *testing.T, r *rig, whileDown ...func()) (*rig, *fakeSpawner) {
	t.Helper()
	return restartRigWrapped(t, r, nil, whileDown...)
}

// restartRigWrapped is restartRig with the second gateway's adapter chosen
// by wrap, as startRigWithSpawnerAdapter chooses the first's.
func restartRigWrapped(t *testing.T, r *rig, wrap func(*fakeAdapter) Adapter, whileDown ...func()) (*rig, *fakeSpawner) {
	t.Helper()
	r.stop()
	select {
	case <-r.adapter.stopped:
	case <-time.After(10 * time.Second):
		t.Fatal("the first gateway did not stop")
	}
	r.client.Close()
	for _, f := range whileDown {
		f()
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, r.url, lib.WithName("gateway-test-2"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	adapter := newFakeAdapter()
	spawn := &fakeSpawner{}
	cfg := *r.g.cfg
	logs := &lockedBuffer{}
	log := slog.New(slog.NewTextHandler(io.MultiWriter(os.Stderr, logs), nil))
	var driven Adapter = adapter
	if wrap != nil {
		driven = wrap(adapter)
	}
	g, err := New(Options{Client: client, Adapter: driven, Config: &cfg, Backend: "discord", Spawner: spawn, Logger: log})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = g.Run(ctx) }()
	return &rig{g: g, adapter: adapter, client: client, bus: r.bus, url: r.url, logs: logs, stop: cancel}, spawn
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

// TestDelegatedTaskStatusShapeSteers: the status matcher is the exact
// phrase set wherever the executor runs follow-ups, and a session worker
// does. During a delegated task a status-shaped but non-exact ask must
// reach the worker as a steer - only the exact phrases stay status
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
	r, spawn := startRigWithSpawnerCap(t, "platform", 1, nil)
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

// TestSessionOffWorksWithoutASpawner: a record left session-routed after the
// spawner was disarmed (the W4-rollback shape) must still have its way back,
// or every message in that conversation publishes to an addressee nothing
// serves until the operator re-arms.
func TestSessionOffWorksWithoutASpawner(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-s16"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-s16", Kind: "group", SessionRouted: true, Profile: "chat",
		BusSession: "chat-otter-dead", Addressee: "chat-otter-dead", LastActivity: time.Now().UTC()}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-250", "/session off")
	waitFor(t, "off ack", postedContaining(r, "session route off"))
	got, _ := r.g.reg.Get(context.Background(), conv)
	if got.SessionRouted || got.Addressee != "platform" || got.BusSession != "" {
		t.Fatalf("record after /session off with no spawner: %+v", got)
	}
	sessionRigTurn(r, conv, "s-251", "how is the fleet?")
	if env := r.awaitTask(t, "platform"); env.To == nil || env.To.Session != "platform" {
		t.Fatalf("not re-homed: %+v", env.To)
	}
	// The on-form stays refused: nothing can spawn.
	sessionRigTurn(r, conv, "s-252", "/session")
	waitFor(t, "not enabled", postedContaining(r, "not enabled"))
}

// TestHasSessionDoesNotCountATaskLessRoutedRecord: the Slack adapter's rule
// stands - a session thread is one the gateway has started a task in. A
// /session binding alone does not make one, because the adapter caches the
// registry's answer and nothing but a started task overwrites it; promising
// otherwise is a promise the adapter cannot keep.
func TestHasSessionDoesNotCountATaskLessRoutedRecord(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	ctx := context.Background()
	bound := &SessionRecord{Key: "slack:channel/C1/1.1", ContextID: "ctx-f", Kind: "group", SessionRouted: true, Profile: "chat",
		LastActivity: time.Now().UTC()}
	if err := r.g.reg.Put(ctx, bound); err != nil {
		t.Fatal(err)
	}
	if held, _, _ := r.g.hasSession(ctx, bound.Key); held {
		t.Fatal("a task-less session-routed record counted as a session thread")
	}
}

// TestSessionWithTextOnSessionDefaultRunsTheText: where every conversation
// is a session already, "/session <text>" is <text>, an ordinary turn - not a
// confirmation that silently drops the ask.
func TestSessionWithTextOnSessionDefaultRunsTheText(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-s17"
	sessionRigTurn(r, conv, "s-260", "/session list the pods in ns x")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if got := joinTextParts(m.Parts); got != "list the pods in ns x" {
		t.Fatalf("task text = %q, want the command stripped", got)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "already a session") {
			t.Fatalf("the text form was answered as a bare /session: %q", p)
		}
	}
}

// TestSessionOffDuringAPlatformTaskRehomesWithoutStopping: the off-refusal
// protects a running SESSION task's pod; a platform task has no pod to lose,
// so the way back goes through and leaves that task running.
func TestSessionOffDuringAPlatformTaskRehomesWithoutStopping(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s18"
	sessionRigTurn(r, conv, "s-270", "check the fleet")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-271", "/session")
	waitFor(t, "ack", postedContaining(r, "after it opens a session pod"))
	sessionRigTurn(r, conv, "s-272", "/session off")
	waitFor(t, "off ack", postedContaining(r, "session route off"))
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.SessionRouted || rec.Addressee != "platform" || rec.ActiveTask == nil || rec.ActiveTask.Detached {
		t.Fatalf("record after /session off during a platform task: %+v", rec)
	}
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.EnvelopeID != origin.EnvelopeID {
			t.Fatalf("/session off reached the platform task as %s", e.Kind)
		}
	}
	if len(spawn.calls()) != 0 {
		t.Fatal("spawned")
	}
}

// TestPostFlipSessionStopIsAStop: on a session-default install the stripped
// "/session stop" must be the stop it unwraps to - a cancel on the running
// task, never a steer whose text reads "stop" and never a new task.
func TestPostFlipSessionStopIsAStop(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-s19"
	sessionRigTurn(r, conv, "s-280", "check the fleet")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	origin := r.awaitTask(t, call.Session)
	exec := r.execFor(t, origin, call.Session)
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-281", "/session stop")
	waitFor(t, "cancel detached the task", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		return rec != nil && rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	for _, e := range inSubjectEnvelopes(t, r.url, call.Session) {
		if e.EnvelopeID == origin.EnvelopeID {
			continue
		}
		if e.Kind == lib.KindMessage {
			t.Fatalf("/session stop reached the session task as a message (steer): %s", string(e.Payload))
		}
	}
	if got := len(spawn.calls()); got != 1 {
		t.Fatalf("/session stop spawned: %d spawns", got)
	}
}

// TestSessionWithTextOnARunningSessionSteers: already on the route with its
// own task running, "/session <text>" is what a plain message would be - a
// steer into that task - not an ack that drops the text.
func TestSessionWithTextOnARunningSessionSteers(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/thread-s20"
	sessionRigTurn(r, conv, "s-290", "/session long task")
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	call := spawn.calls()[0]
	origin := r.awaitTask(t, call.Session)
	exec := r.execFor(t, origin, call.Session)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	sessionRigTurn(r, conv, "s-291", "/session make it about NATS")
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
	if got := len(spawn.calls()); got != 1 {
		t.Fatalf("a steer spawned: %d", got)
	}
}

// TestSessionOnAckTellsSlackChannelsToMention: the Slack adapter forwards an
// unmentioned thread reply only once a task has started there, so the ack
// after a bare /session in a Slack channel must not promise that the next
// message opens the pod - it asks for the mention. DMs and every other
// backend keep the plain promise. A Slack DM is told to reply in the thread:
// each top-level DM is a conversation of its own, so the binding holds only
// there.
func TestSessionOnAckTellsSlackChannelsToMention(t *testing.T) {
	slackChannel := sessionOnAck("slack", "group", "platform")
	if !strings.Contains(slackChannel, "mention") || strings.Contains(slackChannel, "your next message opens") {
		t.Fatalf("slack channel ack = %q", slackChannel)
	}
	slackDM := sessionOnAck("slack", "dm", "platform")
	if !strings.Contains(slackDM, "reply in this thread") || strings.Contains(slackDM, "mention") || strings.Contains(slackDM, "your next message opens") {
		t.Fatalf("slack dm ack = %q", slackDM)
	}
	for _, c := range [][2]string{{"discord", "dm"}, {"discord", "group"}, {"gchat", "group"}, {"inject", "group"}} {
		ack := sessionOnAck(c[0], c[1], "platform")
		if !strings.Contains(ack, "your next message opens a session pod") || strings.Contains(ack, "mention") {
			t.Fatalf("%s/%s ack = %q", c[0], c[1], ack)
		}
	}
	if !strings.Contains(sessionOnAck("discord", "group", "platform"), "`platform`") {
		t.Fatal("the ack does not name the default addressee")
	}
}

// TestNewRefusesTheClusterViewWithoutABrokerURL: told the view is on but not
// where the broker is, the gateway refuses to start rather than spawn pods
// whose shim dials nothing.
func TestNewRefusesTheClusterViewWithoutABrokerURL(t *testing.T) {
	s := startServer(t)
	url := s.ClientURL()
	provision(t, url)
	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	cfg := &Config{NATSURL: url, PrincipalMapPath: mapFile, DefaultAddressee: "platform", IdleTTL: 30 * time.Minute,
		AttributionSalt: []byte("test-salt"), SessionClusterView: true}
	if _, err := New(Options{Client: client, Adapter: newFakeAdapter(), Config: cfg, Backend: "discord", Spawner: &fakeSpawner{}}); err == nil ||
		!strings.Contains(err.Error(), "A2A_CREDENTIAL_PROXY_URL") {
		t.Fatalf("New() = %v, want a refusal naming A2A_CREDENTIAL_PROXY_URL", err)
	}
}

// assertRootCapability is the DoD's first clause in unit form: grants is not
// null, it names the key the gateway minted for this task, and the entry is
// really there at the pinned revision with the addressee as its delegate.
//
// It resolves through the same Resolver the verifier runs, against the same
// real bucket, so what is under test is the write the gateway performed and
// not the struct it marshalled.
func assertRootCapability(t *testing.T, r *rig, auth Authority, taskID, delegate string) {
	t.Helper()
	if string(auth.Grants) == "null" || len(auth.Grants) == 0 {
		t.Fatalf("grants is null; the task carries no capability")
	}
	var grants AuthorityGrants
	if err := json.Unmarshal(auth.Grants, &grants); err != nil {
		t.Fatalf("grants: %v", err)
	}
	ref := grants.Capability
	if want := "root." + taskID; ref.Key != want {
		t.Fatalf("capability key = %q, want %q", ref.Key, want)
	}
	if ref.Revision == 0 {
		t.Fatalf("capability reference is not pinned to a revision: %+v", ref)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	store, err := capability.NewStore(ctx, r.client.JetStream())
	if err != nil {
		t.Fatalf("cap store: %v", err)
	}
	res := &capability.Resolver{Store: store}

	entry, err := res.Resolve(ctx, delegate, ref)
	if err != nil {
		t.Fatalf("the delegate could not resolve its own capability: %v", err)
	}
	if entry.Delegate != delegate {
		t.Fatalf("delegate = %q, want %q", entry.Delegate, delegate)
	}
	if entry.Tier != capability.TierDeveloperTeam {
		t.Fatalf("tier = %q, want the narrow default", entry.Tier)
	}
	if entry.Scope == "" {
		t.Fatal("scope is empty; the ceiling was never applied")
	}

	// Same reference, wrong holder. The block travels on a bus other
	// principals read, so possession of the reference must not be the test.
	if _, err := res.Resolve(ctx, "somebody-else", ref); err == nil {
		t.Fatal("a principal the root does not name resolved it anyway")
	} else if !errors.Is(err, capability.ErrRefused) {
		t.Fatalf("wrong holder refused for the wrong reason: %v", err)
	}
}

// deleteCapBucket takes the capability bucket away, the way deleteTasksStream
// takes the task stream away: it is how an install that never provisioned the
// bucket, or a gateway whose $KV.cap.root.* grant is missing, looks from here.
func deleteCapBucket(t *testing.T, url string) {
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
	if err := js.DeleteKeyValue(ctx, capability.Bucket); err != nil {
		t.Fatalf("delete cap bucket: %v", err)
	}
}

// TestAMintFailureRefusesTheTurn covers the armed half of the mint-failure
// branch, which had no test: every other gateway test provisions the cap
// bucket in provision(), so Mint never fails and neither arm was reachable.
//
// Enforcement on is the default, and the contract is that a gateway which
// cannot mint refuses rather than passes. The alternative -- send it anyway --
// is not a smaller failure: the executor refuses the capability-less
// submission regardless, one round trip and (on the session route) one pod
// later, with the reason surfacing in a different component's log than the
// one the operator is reading.
func TestAMintFailureRefusesTheTurn(t *testing.T) {
	r := startRig(t)
	deleteCapBucket(t, r.url)
	conv := "discord:g1/thread-mint-refuses"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "do a thing"}

	waitFor(t, "the refusal to be posted", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "could not mint") {
				return true
			}
		}
		return false
	})
	// And nothing went to an executor: the refusal is the whole outcome.
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("a submission was published despite the mint failing: %d envelope(s) on the in subject", len(envs))
	}
}

// TestAMintFailureUnderCapabilityOptionalSendsGrantsNull covers the relaxed
// twin -- the mixed-version window, and the only path in the tree that reaches
// an executor with grants null. It is asserted because it is the one way a
// capability-less submission is legitimate, so a regression that reached it by
// accident (the return dropped, the condition inverted) would otherwise look
// exactly like correct behaviour.
func TestAMintFailureUnderCapabilityOptionalSendsGrantsNull(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.CapabilityOptional = true })
	deleteCapBucket(t, r.url)
	conv := "discord:g1/thread-mint-relaxed"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "do a thing"}

	var env *lib.Envelope
	waitFor(t, "the submission to reach the executor anyway", func() bool {
		envs := inSubjectEnvelopes(t, r.url, "platform")
		if len(envs) == 0 {
			return false
		}
		env = envs[0]
		return true
	})
	var auth Authority
	if err := json.Unmarshal(env.Authority, &auth); err != nil {
		t.Fatalf("authority: %v", err)
	}
	if string(auth.Grants) != "null" {
		t.Fatalf("grants = %s, want null -- this is the one path that may carry no capability", auth.Grants)
	}
}

// TestStartTaskRecordsTheRequester: the history entry carries the backend and
// the backend-native author id (what the allowlist check compares when this
// turn asks the gateway to mint a child task) and the pseudonymized
// attribution, never the plaintext principal.
func TestStartTaskRecordsTheRequester(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-req"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "how is the fleet?"}
	r.awaitTask(t, "platform")
	var rec *SessionRecord
	waitFor(t, "record with a task", func() bool {
		rec, _ = r.g.reg.Get(context.Background(), conv)
		return rec != nil && len(rec.Tasks) == 1
	})
	ref := rec.Tasks[0]
	if want := requesterSubject(r.g.ps, "discord", "1001"); ref.Requester == nil ||
		ref.Requester.Backend != "discord" || ref.Requester.Subject != want || !strings.HasPrefix(want, "hmac:") {
		t.Fatalf("requester = %+v, want backend discord and subject %q", ref.Requester, want)
	}
	if raw := rawSessionRecord(t, r.g.reg, conv); strings.Contains(raw, `"1001"`) {
		t.Fatalf("the session KV holds the plaintext author id: %s", raw)
	}
	if ref.StartedAt.IsZero() {
		t.Fatal("startedAt not recorded")
	}
	if strings.Contains(string(ref.Attribution), "test:bnaylor") {
		t.Fatalf("attribution carries the plaintext principal: %s", ref.Attribution)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(ref.Attribution, &m); err != nil {
		t.Fatal(err)
	}
	if _, ok := m["requester"]; !ok {
		t.Fatalf("attribution lacks requester: %s", ref.Attribution)
	}
	if _, ok := m["grants"]; ok {
		t.Fatalf("attribution carries grants: %s", ref.Attribution)
	}
	// A human turn is the root of any chain: no role, no parent, no
	// children, depth zero — and none of the four keys written at all.
	if ref.Role != "" || ref.ParentTaskID != "" || ref.Children != nil || ref.Depth != 0 {
		t.Fatalf("human turn carries chain fields: %+v", ref)
	}
	var stored struct {
		Tasks []map[string]json.RawMessage `json:"tasks"`
	}
	if err := json.Unmarshal([]byte(rawSessionRecord(t, r.g.reg, conv)), &stored); err != nil || len(stored.Tasks) != 1 {
		t.Fatalf("stored record: %v (%d tasks)", err, len(stored.Tasks))
	}
	for _, k := range []string{"role", "parentTaskId", "children", "depth"} {
		if _, ok := stored.Tasks[0][k]; ok {
			t.Fatalf("human turn's history entry writes %q", k)
		}
	}
}

// TestStartTaskWithCarriesTheChain: the core a child or wake turn calls
// rides the correlationId it is handed rather than minting one, stores the
// requester it is handed verbatim (already hashed), and writes the chain
// fields onto the history entry.
func TestStartTaskWithCarriesTheChain(t *testing.T) {
	r := startRig(t)
	if r.g.cfg.DelegationDepthMax != 3 {
		t.Fatalf("New left DelegationDepthMax = %d on a hand-built Config, want 3", r.g.cfg.DelegationDepthMax)
	}
	conv := "discord:g1/thread-chain"
	rec := &SessionRecord{Key: conv, Kind: "group", Addressee: "platform", ContextID: "ctx-chain"}
	req := TaskRequester{Backend: "discord", Subject: requesterSubject(r.g.ps, "discord", "1001")}
	taskID, ok := r.g.startTaskWith(context.Background(), rec, taskStart{
		Text:          "child ask",
		MessageID:     "",
		Principal:     "test:bnaylor",
		Requester:     req,
		Authority:     Authority{Requester: AuthorityRequester{Principal: "test:bnaylor", Backend: "discord"}},
		CorrelationID: "corr-parent",
		Role:          taskRoleChild,
		ParentTaskID:  "task-parent",
		Depth:         1,
	})
	if !ok || taskID == "" {
		t.Fatalf("startTaskWith = (%q, %v), want a task id and true", taskID, ok)
	}
	env := r.awaitTask(t, "platform")
	if env.TaskID != taskID || env.CorrelationID != "corr-parent" {
		t.Fatalf("envelope task %q corr %q, want %q and the parent's corr-parent", env.TaskID, env.CorrelationID, taskID)
	}
	if rec.ActiveTask == nil || rec.ActiveTask.CorrelationID != "corr-parent" {
		t.Fatalf("active task = %+v, want correlationId corr-parent", rec.ActiveTask)
	}
	ref := rec.Tasks[len(rec.Tasks)-1]
	if ref.ID != taskID || ref.CorrelationID != "corr-parent" || ref.Role != taskRoleChild ||
		ref.ParentTaskID != "task-parent" || ref.Depth != 1 || ref.Children != nil {
		t.Fatalf("history entry = %+v", ref)
	}
	if ref.Requester == nil || *ref.Requester != req {
		t.Fatalf("requester = %+v, want %+v stored as handed", ref.Requester, req)
	}
}

// TestStartTaskHashesAGchatRequester: on Google Chat the author id is the
// sender's email, and the history entry lands in the session-state KV, which
// the content posture holds to pseudonyms. The stored requester is the
// backend plus the normalized id hashed under the install salt; the email
// appears nowhere in the record, in any case.
func TestStartTaskHashesAGchatRequester(t *testing.T) {
	r := startGchatRig(t, []string{"alice@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-req"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "Alice@Example.com", MessageID: "spaces/S1/messages/M1", Text: "how is the fleet?",
	}
	r.awaitTask(t, "platform")
	waitFor(t, "record with a task", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		return rec != nil && len(rec.Tasks) == 1
	})
	raw := rawSessionRecord(t, r.g.reg, conv)
	if strings.Contains(strings.ToLower(raw), "alice@example.com") {
		t.Fatalf("the session KV holds the requester's email: %s", raw)
	}
	var stored struct {
		Tasks []struct {
			Requester map[string]string `json:"requester"`
		} `json:"tasks"`
	}
	if err := json.Unmarshal([]byte(raw), &stored); err != nil {
		t.Fatal(err)
	}
	if len(stored.Tasks) != 1 {
		t.Fatalf("tasks = %+v", stored.Tasks)
	}
	want := NewPseudonymizer([]byte("test-salt")).Hash("alice@example.com") // trimmed, lowercased, hashed
	got := stored.Tasks[0].Requester
	if got["backend"] != gchatBackend || got["subject"] != want || len(got) != 2 {
		t.Fatalf("requester = %v, want backend %q and subject %q only", got, gchatBackend, want)
	}
}

// rawSessionRecord reads a record's bytes as the KV holds them, so a test can
// assert on what is at rest rather than on the decoded struct.
func rawSessionRecord(t *testing.T, reg *Registry, sessionKey string) string {
	t.Helper()
	kv, err := reg.kv(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	entry, err := kv.Get(context.Background(), kvKey(sessionKey))
	if err != nil {
		t.Fatal(err)
	}
	return string(entry.Value())
}

// TestAskTTLBoundaryClearsBothCopies: a copy exactly AskTTL old is past the
// TTL (>=), for the history entry's requester exactly as for the active
// task's ask, and a nanosecond younger is not, for either.
func TestAskTTLBoundaryClearsBothCopies(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.AskTTL = time.Minute })
	conv := "discord:g1/thread-ttl-edge"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "x"}
	r.awaitTask(t, "platform")
	ctx := context.Background()
	var rec *SessionRecord
	waitFor(t, "record", func() bool {
		rec, _ = r.g.reg.Get(ctx, conv)
		return rec != nil && len(rec.Tasks) == 1 && rec.ActiveTask != nil
	})
	start := time.Now().UTC().Truncate(time.Second) // survives the JSON round trip exactly
	rec.Tasks[0].StartedAt = start
	rec.ActiveTask.SubmittedAt = start
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}

	r.g.boundAskCopyAt(ctx, rec, start.Add(time.Minute-time.Nanosecond))
	fresh, _ := r.g.reg.Get(ctx, conv)
	if fresh.Tasks[0].Requester == nil || fresh.Tasks[0].Attribution == nil {
		t.Fatalf("requester cleared a nanosecond before the TTL: %+v", fresh.Tasks[0])
	}
	if fresh.ActiveTask == nil || fresh.ActiveTask.Ask == "" {
		t.Fatalf("ask cleared a nanosecond before the TTL: %+v", fresh.ActiveTask)
	}

	r.g.boundAskCopyAt(ctx, fresh, start.Add(time.Minute))
	fresh, _ = r.g.reg.Get(ctx, conv)
	if fresh.Tasks[0].Requester != nil || fresh.Tasks[0].Attribution != nil {
		t.Fatalf("requester survived at exactly the TTL: %+v", fresh.Tasks[0])
	}
	if fresh.ActiveTask == nil || fresh.ActiveTask.Ask != "" {
		t.Fatalf("ask survived at exactly the TTL: %+v", fresh.ActiveTask)
	}
}

func TestTaskRefOmitsAZeroStartedAt(t *testing.T) {
	raw, err := json.Marshal(TaskRef{ID: "task-legacy", Addressee: "platform"}) // pre-field shape
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "startedAt") {
		t.Fatalf("a legacy-shaped entry marshals a zero startedAt: %s", raw)
	}
}

// TestAskTTLClearsTheRequesterToo: past AskTTL the history entry drops its
// requester and attribution the way ActiveTask drops its Ask; the entry itself
// stays, as do entries written before the fields existed.
func TestAskTTLClearsTheRequesterToo(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.AskTTL = time.Minute })
	conv := "discord:g1/thread-ttl"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "x"}
	r.awaitTask(t, "platform")
	ctx := context.Background()
	var rec *SessionRecord
	waitFor(t, "record", func() bool { rec, _ = r.g.reg.Get(ctx, conv); return rec != nil && len(rec.Tasks) == 1 })
	rec.Tasks[0].StartedAt = time.Now().Add(-2 * time.Minute)
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "task-legacy", Addressee: "platform"}) // pre-field entry
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.boundAskCopy(ctx, rec)
	fresh, _ := r.g.reg.Get(ctx, conv)
	if fresh.Tasks[0].Requester != nil || fresh.Tasks[0].Attribution != nil {
		t.Fatalf("requester survived the TTL: %+v", fresh.Tasks[0])
	}
	if fresh.Tasks[1].ID != "task-legacy" {
		t.Fatalf("legacy entry disturbed: %+v", fresh.Tasks[1])
	}
	if fresh.ActiveTask == nil || fresh.ActiveTask.Ask == "" {
		t.Fatalf("the active task's fresh ask was cleared: %+v", fresh.ActiveTask)
	}
}

// TestAGchatSteerAuthorIsStoredHashed: the steer author lands in the
// session-state KV as the requester does, the normalized email hashed, the
// email nowhere in the record.
func TestAGchatSteerAuthorIsStoredHashed(t *testing.T) {
	r := startGchatRig(t, []string{"alice@example.com", "bob@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-steer"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "alice@example.com", MessageID: "spaces/S1/messages/M1", Text: "how is the fleet?"}
	origin := r.awaitTask(t, "platform")
	waitFor(t, "task on the record", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		return rec != nil && rec.ActiveTask != nil
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "Bob@Example.com", MessageID: "spaces/S1/messages/M2", Text: "and the costs"}
	var ref TaskRef
	waitFor(t, "steer author on the record", func() bool {
		rec, _ := r.g.reg.Get(context.Background(), conv)
		ref, _ = rec.TaskRefFor(origin.TaskID)
		return len(ref.SteerAuthors) == 1
	})
	want := NewPseudonymizer([]byte("test-salt")).Hash("bob@example.com")
	if ref.SteerAuthors[0] != (TaskRequester{Backend: gchatBackend, Subject: want}) {
		t.Fatalf("steer author = %+v, want gchat/%s", ref.SteerAuthors[0], want)
	}
	if raw := rawSessionRecord(t, r.g.reg, conv); strings.Contains(strings.ToLower(raw), "bob@example.com") {
		t.Fatalf("the session KV holds the steer author's email: %s", raw)
	}
}

// TestAskTTLClearsTheSteerAuthors: the steer authors are bounded with the
// requester, the overflow mark with them; an entry holding only steer
// authors (its requester already cleared) is found and cleared too.
func TestAskTTLClearsTheSteerAuthors(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.AskTTL = time.Minute })
	conv := "discord:g1/thread-ttl-steer"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "x"}
	r.awaitTask(t, "platform")
	ctx := context.Background()
	var rec *SessionRecord
	waitFor(t, "record", func() bool { rec, _ = r.g.reg.Get(ctx, conv); return rec != nil && len(rec.Tasks) == 1 })
	old := time.Now().Add(-2 * time.Minute)
	authors := []TaskRequester{{Backend: "discord", Subject: "hmac:x"}}
	rec.Tasks[0].StartedAt = old
	rec.Tasks[0].SteerAuthors, rec.Tasks[0].SteerAuthorsOverflow = authors, true
	rec.Tasks = append(rec.Tasks, TaskRef{ID: "task-steer-only", Addressee: "platform", StartedAt: old, SteerAuthors: authors})
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.boundAskCopy(ctx, rec)
	fresh, _ := r.g.reg.Get(ctx, conv)
	for _, ref := range fresh.Tasks {
		if ref.Requester != nil || len(ref.SteerAuthors) != 0 || ref.SteerAuthorsOverflow {
			t.Fatalf("steer authors survived the TTL: %+v", ref)
		}
	}
}

// TestAskTTLBoundsTheIncarnationSet: the set is hashed ids like the
// requester copy and is bounded the same way, from its oldest entry; cleared,
// it is marked incomplete, so the incarnation's delegations fail closed as a
// cleared requester's do.
func TestAskTTLBoundsTheIncarnationSet(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.AskTTL = time.Minute })
	conv := "discord:g1/thread-ttl-incarnation"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "x"}
	r.awaitTask(t, "platform")
	ctx := context.Background()
	var rec *SessionRecord
	waitFor(t, "record", func() bool { rec, _ = r.g.reg.Get(ctx, conv); return rec != nil && len(rec.Tasks) == 1 })
	rec.BusSession = "chat-x"
	rec.addSessionAuthor(TaskRequester{Backend: "discord", Subject: "hmac:x"})
	rec.SessionAuthorsSince = time.Now().Add(-2 * time.Minute)
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.g.boundAskCopy(ctx, rec)
	fresh, _ := r.g.reg.Get(ctx, conv)
	if len(fresh.SessionAuthors) != 0 || !fresh.SessionAuthorsUnknown {
		t.Fatalf("incarnation set past the TTL: %+v unknown=%v", fresh.SessionAuthors, fresh.SessionAuthorsUnknown)
	}
}

// publishSteerNotice publishes the executor's non-final notice for a follow-up.
func publishSteerNotice(t *testing.T, r *rig, origin *lib.Envelope, addressee string, n lib.SteerNotice) {
	t.Helper()
	part, err := lib.SteerNoticePart(n)
	if err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateWorking, Message: &lib.Message{Role: "agent", MessageID: "msg-n-" + n.EnvelopeID + "-" + n.Steer,
			Parts: []lib.Part{{Kind: "text", Text: "follow-up " + n.Steer}, part}}}})
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: addressee, AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject(addressee, origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
}

// publishTurnAnswer publishes one finished turn's answer, one chunk, last.
func publishTurnAnswer(t *testing.T, r *rig, origin *lib.Envelope, addressee string, turn int, text string) {
	t.Helper()
	publishTurnChunk(t, r, origin, addressee, turn, text, false, true)
}

// publishTurnChunk publishes one chunk of a turn's answer.
func publishTurnChunk(t *testing.T, r *rig, origin *lib.Envelope, addressee string, turn int, text string, appendChunk, last bool) {
	t.Helper()
	payload, _ := json.Marshal(lib.ArtifactUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Artifact: lib.Artifact{ArtifactID: fmt.Sprintf("artifact-%s-turn-%d", origin.TaskID, turn), Name: lib.ArtifactTurn,
			Parts: []lib.Part{{Kind: "text", Text: text}}},
		Append: appendChunk, LastChunk: last})
	env, err := lib.NewArtifactUpdateEnvelope(lib.Party{Session: addressee, AgentType: "test-executor"},
		origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskEventsSubject(addressee, origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
}

// lastInSubject is the newest follow-up envelope on platform's in subject for origin.
func lastInSubject(t *testing.T, r *rig, origin *lib.Envelope) *lib.Envelope {
	t.Helper()
	var last *lib.Envelope
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		if e.TaskID == origin.TaskID && e.Kind == lib.KindMessage && e.EnvelopeID != origin.EnvelopeID {
			last = e
		}
	}
	if last == nil {
		t.Fatal("no follow-up on the in subject")
	}
	return last
}

func TestRelayPostsEachTurnAnswerThenTheDeliverable(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-turns"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tu-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tu-2", Text: "only prod"}
	waitFor(t, "ack", postedContaining(r, ackSteerQueued))
	steer := lastInSubject(t, r, origin)
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: steer.EnvelopeID})
	publishTurnAnswer(t, r, origin, "platform", 1, "fleet: 3 clusters green")
	waitFor(t, "turn 1 posted before the terminal", postedContaining(r, "fleet: 3 clusters green"))
	completeTask(t, exec, "prod: green")
	waitFor(t, "deliverable", postedContaining(r, "prod: green"))
	if a, b := postIndex(r, "fleet: 3 clusters green"), postIndex(r, "prod: green"); a < 0 || a > b {
		t.Fatalf("turn 1 at %d, deliverable at %d", a, b)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "ℹ️") || strings.Contains(p, "⚠️") {
			t.Fatalf("a queued notice or a shortfall line posted: %q", p)
		}
	}
}

func TestRelayRendersARefusedFollowUp(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-refused"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "rf-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	_ = r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "rf-2", Text: "seventeenth"}
	waitFor(t, "ack", postedContaining(r, ackSteerQueued))
	steer := lastInSubject(t, r, origin)
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: steer.EnvelopeID, Reason: lib.SteerReasonQueueFull})
	waitFor(t, "refusal", postedContaining(r, fmt.Sprintf(noticeSteerNotTaken, steerRefusalWhy[lib.SteerReasonQueueFull])))
}

// TestRelayWordsARefusalWithNoReason: a refused notice with no reason token
// says none was given; quoting the empty token would post "the executor said
// ." to the room.
func TestRelayWordsARefusalWithNoReason(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-refused-bare"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "rb-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	_ = r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "rb-2", Text: "only prod"}
	waitFor(t, "ack", postedContaining(r, ackSteerQueued))
	steer := lastInSubject(t, r, origin)
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: steer.EnvelopeID})
	waitFor(t, "refusal", postedContaining(r, fmt.Sprintf(noticeSteerNotTaken, "the executor gave no reason")))
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "the executor said") {
			t.Fatalf("an empty reason was quoted: %q", p)
		}
	}

	// An unknown token is quoted, bounded.
	long := strings.Repeat("x", 2*steerReasonQuoteMax)
	if got, want := steerNotTakenNotice(long), fmt.Sprintf(noticeSteerNotTaken, "the executor said "+truncateRunes(long, steerReasonQuoteMax)); got != want {
		t.Fatalf("unknown token = %q, want %q", got, want)
	}
	if got := steerNotTakenNotice(" "); got != fmt.Sprintf(noticeSteerNotTaken, "the executor gave no reason") {
		t.Fatalf("blank token = %q", got)
	}
}

// The follow-up reached the stream after the executor's terminal, so
// nothing answered it; the room is told at the terminal. The executor has
// shown it answers follow-ups: on an earlier follow-up of the same task, or
// on an earlier task to the same addressee since the gateway started.
func TestRelayReportsAFollowUpNoExecutorAnswered(t *testing.T) {
	t.Run("a notice earlier in the task", func(t *testing.T) {
		r := startRig(t)
		conv := "discord:g1/thread-missed"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ms-1", Text: "check the fleet"}
		origin := r.awaitTask(t, "platform")
		exec := r.execFor(t, origin, "platform")
		_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ms-2", Text: "only prod"}
		waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
		publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: lastInSubject(t, r, origin).EnvelopeID})
		publishTurnAnswer(t, r, origin, "platform", 1, "fleet: 3 clusters")
		waitFor(t, "turn 1 posted", postedContaining(r, "fleet: 3 clusters"))
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ms-3", Text: "and staging"}
		waitFor(t, "second steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 3 })
		waitFor(t, "second ack", func() bool { return strings.Count(strings.Join(r.adapter.postTexts(), "\n"), ackSteerQueued) == 2 })
		completeTask(t, exec, "prod: green")
		missed := fmt.Sprintf(noticeSteerMissed, 1)
		waitFor(t, "missed line", postedContaining(r, missed))
		if a, b := postIndex(r, "prod: green"), postIndex(r, missed); a < 0 || a > b {
			t.Fatalf("deliverable at %d, missed line at %d: the missed line must follow the deliverable", a, b)
		}
		if strings.Contains(strings.Join(r.adapter.postTexts(), "\n"), "did not run") {
			t.Fatal("the follow-up that ran was counted as not run")
		}
	})
	t.Run("a notice on an earlier task", func(t *testing.T) {
		r := startRig(t)
		first := "discord:g1/thread-spoke"
		r.adapter.inbox <- InboundMessage{Conversation: first, Kind: "group", AuthorID: "1001", MessageID: "sp-1", Text: "check the fleet"}
		origin := r.awaitTask(t, "platform")
		exec := r.execFor(t, origin, "platform")
		_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
		r.adapter.inbox <- InboundMessage{Conversation: first, Kind: "group", AuthorID: "1001", MessageID: "sp-2", Text: "too many"}
		waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
		publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused,
			EnvelopeID: lastInSubject(t, r, origin).EnvelopeID, Reason: lib.SteerReasonQueueFull})
		completeTask(t, exec, "fleet: green")
		waitFor(t, "first task done", postedContaining(r, "fleet: green"))

		conv := "discord:g1/thread-missed-2"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ms-1", Text: "check the nodes"}
		waitFor(t, "second task", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 3 })
		second := inSubjectEnvelopes(t, r.url, "platform")[2]
		exec2 := r.execFor(t, second, "platform")
		_ = exec2.PublishStatus(context.Background(), lib.StateWorking, false)
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ms-2", Text: "only prod"}
		waitFor(t, "second ack", func() bool { return strings.Count(strings.Join(r.adapter.postTexts(), "\n"), ackSteerQueued) == 2 })
		completeTask(t, exec2, "nodes: green")
		waitFor(t, "missed line", postedContaining(r, fmt.Sprintf(noticeSteerMissed, 1)))
	})
	// Three follow-ups nothing answered are one line that counts them, so
	// the room is not told to resend "it" when there were three.
	t.Run("three unanswered, one counted line", func(t *testing.T) {
		r := startRig(t)
		conv := "discord:g1/thread-missed-3"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m3-1", Text: "check the fleet"}
		origin := r.awaitTask(t, "platform")
		exec := r.execFor(t, origin, "platform")
		_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m3-2", Text: "only prod"}
		waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
		publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: lastInSubject(t, r, origin).EnvelopeID})
		publishTurnAnswer(t, r, origin, "platform", 1, "fleet: 3 clusters")
		waitFor(t, "turn 1 posted", postedContaining(r, "fleet: 3 clusters"))
		for i, text := range []string{"and staging", "and dev", "and qa"} {
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("m3-s%d", i), Text: text}
			waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == i+3 })
		}
		waitFor(t, "four acks", func() bool { return strings.Count(strings.Join(r.adapter.postTexts(), "\n"), ackSteerQueued) == 4 })
		completeTask(t, exec, "prod: green")
		want := fmt.Sprintf(noticeSteerMissed, 3)
		waitFor(t, "missed line", postedContaining(r, want))
		waitFor(t, "terminal line", terminalEdited(r, lib.StateCompleted))
		if n := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), "as the task finished"); n != 1 {
			t.Fatalf("%d missed lines, want the one counted line", n)
		}
	})
}

// An executor that predates steer notices (an older bridge sidecar in front
// of this gateway) answers none, so the gateway has no evidence any
// follow-up was missed and posts no missed line: the ack and the old
// "steering received" text are all the room sees.
func TestRelayPostsNoMissedLineForAnExecutorThatSendsNoNotices(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-oldbridge"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ob-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ob-2", Text: "only prod"}
	waitFor(t, "ack", postedContaining(r, ackSteerQueued))
	completeTask(t, exec, "fleet: green")
	waitFor(t, "deliverable", postedContaining(r, "fleet: green"))
	waitFor(t, "terminal line", terminalEdited(r, lib.StateCompleted))
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "⚠️") {
			t.Fatalf("a shortfall line posted for an executor that never spoke steer notices: %q", p)
		}
	}
}

// terminalEdited is true once the task's rolling line has been edited to
// its terminal line, which the relay does after the shortfall lines.
func terminalEdited(r *rig, state lib.TaskState) func() bool {
	return func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.HasPrefix(e, terminalLine(state, "")) {
				return true
			}
		}
		return false
	}
}

// A follow-up the executor counted as started (its turn answer posted) but
// stopped before its turn ran is refused task-ended, and still gets its
// "did not run" line: the cancel landed between the turn answer and the
// spawn.
func TestRelayCountsAFollowUpStoppedBeforeItsTurnRan(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-gap"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "gp-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	_ = r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "gp-2", Text: "only prod"}
	waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
	id := lastInSubject(t, r, origin).EnvelopeID
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: id})
	publishTurnAnswer(t, r, origin, "platform", 1, "fleet: 3 clusters")
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: id, Reason: lib.SteerReasonTaskEnded})
	publishFinal(t, r, origin, "platform", lib.StateCanceled, "reason: canceled-by-request")
	waitFor(t, "unrun line", postedContaining(r, fmt.Sprintf(noticeSteersUnrun, 1)))
	if a, b := postIndex(r, "fleet: 3 clusters"), postIndex(r, fmt.Sprintf(noticeSteersUnrun, 1)); a < 0 || a > b {
		t.Fatalf("turn 1 at %d, unrun line at %d", a, b)
	}
}

// Two queued follow-ups that both ran: two turn answers post in order, then
// the deliverable, and no shortfall line.
func TestRelayPostsTwoTurnAnswersInOrder(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-two-turns"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tt-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	for i, text := range []string{"only prod", "and staging"} {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("tt-s%d", i), Text: text}
		waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == i+2 })
		publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: lastInSubject(t, r, origin).EnvelopeID})
	}
	publishTurnAnswer(t, r, origin, "platform", 1, "fleet: 3 clusters")
	publishTurnAnswer(t, r, origin, "platform", 2, "prod: green")
	completeTask(t, exec, "staging: green")
	waitFor(t, "deliverable", postedContaining(r, "staging: green"))
	waitFor(t, "terminal line", terminalEdited(r, lib.StateCompleted))
	a, b, c := postIndex(r, "fleet: 3 clusters"), postIndex(r, "prod: green"), postIndex(r, "staging: green")
	if a < 0 || a > b || b > c {
		t.Fatalf("turn 1 at %d, turn 2 at %d, deliverable at %d: want that order", a, b, c)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "⚠️") {
			t.Fatalf("a shortfall line posted when both follow-ups ran: %q", p)
		}
	}
}

// A turn answer whose last chunk never came is posted, whole, when the next
// turn's first (non-append) chunk arrives, ahead of that turn.
func TestRelayFlushesAChunkedTurnAtTheNextNonAppendChunk(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-chunked"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ch-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	publishTurnChunk(t, r, origin, "platform", 1, "fleet: ", false, false)
	publishTurnChunk(t, r, origin, "platform", 1, "3 clusters", true, false)
	publishTurnAnswer(t, r, origin, "platform", 2, "prod: green")
	waitFor(t, "turn 2 posted", postedContaining(r, "prod: green"))
	if a, b := postIndex(r, "fleet: 3 clusters"), postIndex(r, "prod: green"); a < 0 || a > b {
		t.Fatalf("turn 1 at %d (want it whole), turn 2 at %d", a, b)
	}
	if postIndex(r, "fleet: ") != postIndex(r, "fleet: 3 clusters") {
		t.Fatal("turn 1's first chunk posted on its own")
	}
	completeTask(t, exec, "done")
	waitFor(t, "deliverable", postedContaining(r, "done"))
}

// An executor that cannot continue a session (the bridge's cli executor)
// refuses each follow-up no-resume as it arrives, and the room is told at
// once, one line per follow-up, while the task still runs: not the ack and
// then silence until the terminal. Nothing more is said at the terminal.
func TestRelayPostsANoResumeRefusalAtOnce(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-noresume"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "nr-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	want := steerNotTakenNotice(lib.SteerReasonNoResume)
	if !strings.Contains(want, "can't continue a session") || !strings.Contains(want, "after the answer") {
		t.Fatalf("no-resume wording %q does not say the executor can't continue a session", want)
	}
	for i, text := range []string{"one", "two", "three"} {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("nr-s%d", i), Text: text}
		waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == i+2 })
		publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused,
			EnvelopeID: lastInSubject(t, r, origin).EnvelopeID, Reason: lib.SteerReasonNoResume})
		waitFor(t, fmt.Sprintf("refusal %d posted while the task runs", i+1), func() bool {
			return strings.Count(strings.Join(r.adapter.postTexts(), "\n"), want) == i+1
		})
	}
	completeTask(t, exec, "fleet: green")
	waitFor(t, "deliverable", postedContaining(r, "fleet: green"))
	waitFor(t, "terminal line", terminalEdited(r, lib.StateCompleted))
	all := strings.Join(r.adapter.postTexts(), "\n")
	if n := strings.Count(all, "can't continue a session"); n != 3 {
		t.Fatalf("%d no-resume lines, want one per follow-up and none at the terminal:\n%s", n, all)
	}
	if strings.Contains(all, "did not run") || strings.Contains(all, "as the task finished") {
		t.Fatalf("a refused follow-up was also counted as unrun or missed:\n%s", all)
	}
	if a, b := postIndex(r, want), postIndex(r, "fleet: green"); a < 0 || a > b {
		t.Fatalf("first refusal at %d, deliverable at %d: the refusal must post before the terminal", a, b)
	}
}

// gh#2723 items 5 and 16: an addressee whose executor refuses follow-ups
// (the bridge's cli executor, learned from its first no-resume refusal)
// gets the wide status matcher back: a status-shaped ask is answered by
// replay, not acked and then refused. A queued notice from the addressee
// later (its executor now continues sessions) puts it back on the exact
// phrases, so the same ask steers again.
func TestNoResumeAddresseeGetsTheWideStatusMatcher(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-noresume-status"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ns-1", Text: "check the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ns-2", Text: "one"}
	waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 2 })
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused,
		EnvelopeID: lastInSubject(t, r, origin).EnvelopeID, Reason: lib.SteerReasonNoResume})
	waitFor(t, "refusal posted", postedContaining(r, steerNotTakenNotice(lib.SteerReasonNoResume)))
	acks := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), ackSteerQueued)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ns-3", Text: "what is the agent doing"}
	waitFor(t, "status reply", postedContaining(r, "🔎"))
	if n := len(inSubjectEnvelopes(t, r.url, "platform")); n != 2 {
		t.Fatalf("%d envelopes on the in subject, want 2: the status ask was sent as a follow-up", n)
	}
	if n := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), ackSteerQueued); n != acks {
		t.Fatalf("the status ask was acknowledged as a follow-up (%d acks, want %d)", n, acks)
	}

	// The executor queues one now: the exact phrases only, again.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ns-4", Text: "also check the memory limits"}
	waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 3 })
	publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued,
		EnvelopeID: lastInSubject(t, r, origin).EnvelopeID})
	waitFor(t, "queued notice applied", func() bool {
		r.g.mu.Lock()
		defer r.g.mu.Unlock()
		return !r.g.steersRefusedBy["platform"]
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ns-5", Text: "what is the agent doing"}
	waitFor(t, "status-shaped ask steers", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == 4 })
}

// Queued follow-ups that never started (refused task-ended at a cancel, or
// lost to a crash with only a failed terminal) are one line at the
// terminal, not one per follow-up.
func TestRelayReportsQueuedFollowUpsThatNeverRan(t *testing.T) {
	for _, tc := range []struct {
		name        string
		refuseAtEnd bool
		state       lib.TaskState
	}{
		{"refused task-ended at a cancel", true, lib.StateCanceled},
		{"a crash: no refusals, a failed terminal", false, lib.StateFailed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := startRig(t)
			conv := "discord:g1/thread-unrun-" + string(tc.state)
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ur-1", Text: "check the fleet"}
			origin := r.awaitTask(t, "platform")
			_ = r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
			var ids []string
			for i, text := range []string{"one", "two"} {
				r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("ur-s%d", i), Text: text}
				waitFor(t, "steer published", func() bool { return len(inSubjectEnvelopes(t, r.url, "platform")) == i+2 })
				ids = append(ids, lastInSubject(t, r, origin).EnvelopeID)
			}
			for _, id := range ids {
				publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: id})
			}
			if tc.refuseAtEnd {
				for _, id := range ids {
					publishSteerNotice(t, r, origin, "platform", lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: id, Reason: lib.SteerReasonTaskEnded})
				}
			}
			publishFinal(t, r, origin, "platform", tc.state, "reason: test")
			want := fmt.Sprintf(noticeSteersUnrun, 2)
			waitFor(t, "unrun line", postedContaining(r, want))
			if n := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), "did not run"); n != 1 {
				t.Fatalf("%d did-not-run lines, want 1", n)
			}
			if strings.Contains(strings.Join(r.adapter.postTexts(), "\n"), "not taken") {
				t.Fatal("a task-ended refusal posted its own line")
			}
		})
	}
}
