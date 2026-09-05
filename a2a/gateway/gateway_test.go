package gateway

import (
	"context"
	"encoding/json"
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
}

func newFakeAdapter() *fakeAdapter {
	return &fakeAdapter{inbox: make(chan InboundMessage, 16), roster: []string{"1001"}, complete: true}
}

func (a *fakeAdapter) Run(ctx context.Context, handler func(InboundMessage)) error {
	for {
		select {
		case <-ctx.Done():
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

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"))
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

func TestUnmappedSenderIsDropped(t *testing.T) {
	r := startRig(t)
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread1", Kind: "group",
		AuthorID: "9999", MessageID: "d-1", Text: "let me in",
	}
	time.Sleep(500 * time.Millisecond)
	if envs := inSubjectEnvelopes(t, r.url, "platform"); len(envs) != 0 {
		t.Fatalf("unverified sender reached the bus: %d envelopes", len(envs))
	}
	if posts := r.adapter.postTexts(); len(posts) != 0 {
		t.Fatalf("unverified sender got a reply: %v", posts)
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
	Session string
	TaskID  string
}

func (s *fakeSpawner) Spawn(_ context.Context, rec *SessionRecord, taskID, _ string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.spawnErr != nil {
		return "", s.spawnErr
	}
	s.spawns = append(s.spawns, fakeSpawn{Session: rec.BusSession, TaskID: taskID})
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

	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"))
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
