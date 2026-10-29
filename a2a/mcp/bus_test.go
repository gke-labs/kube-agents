package mcp

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// consolePublish and consoleSubscribe are the console principal's grants as
// the operator renders them. TestTheGrantsAreTheRenderedOnes holds them to the
// rendered nats.conf, so a status read that needs more than the console has
// fails here rather than on a cluster.
var consolePublish = []string{
	"$JS.API.INFO",
	"$JS.API.STREAM.INFO.TASKS",
	"$JS.API.STREAM.INFO.DIRECTORY",
	"$JS.API.STREAM.INFO.TOPICS-STATE",
	"$JS.API.STREAM.INFO.TOPICS-JOURNAL",
	"$JS.API.CONSUMER.CREATE.TASKS.>",
	"$JS.API.CONSUMER.CREATE.DIRECTORY.>",
	"$JS.API.CONSUMER.CREATE.TOPICS-STATE.>",
	"$JS.API.CONSUMER.CREATE.TOPICS-JOURNAL.>",
	"$JS.API.CONSUMER.INFO.TASKS.*",
	"$JS.API.CONSUMER.INFO.DIRECTORY.*",
	"$JS.API.CONSUMER.INFO.TOPICS-STATE.*",
	"$JS.API.CONSUMER.INFO.TOPICS-JOURNAL.*",
	"$JS.API.CONSUMER.MSG.NEXT.TASKS.*",
	"$JS.API.CONSUMER.MSG.NEXT.DIRECTORY.*",
	"$JS.API.CONSUMER.MSG.NEXT.TOPICS-STATE.*",
	"$JS.API.CONSUMER.MSG.NEXT.TOPICS-JOURNAL.*",
	"chat.console.*.in",
	"_INBOX.console.>",
}

var consoleSubscribe = []string{"a2a.>", "chat.console.*.out", "_INBOX.console.>"}

// consolePublishDeny and consoleSubscribeDeny are the console's deny lists as
// rendered. A deny the operator adds must be mirrored here, or the test server
// would grant what the cluster refuses.
var (
	consolePublishDeny   []string
	consoleSubscribeDeny = []string{"a2a.cap.>"}
)

// renderedConf is the operator's render the callout suite starts from.
const renderedConf = "../authcallout/testdata/rendered-nats.conf"

func TestTheGrantsAreTheRenderedOnes(t *testing.T) {
	raw, err := os.ReadFile(renderedConf)
	if err != nil {
		t.Fatal(err)
	}
	conf := string(raw)
	start := strings.Index(conf, "user: console")
	if start < 0 {
		t.Fatal("the rendered conf has no console user")
	}
	block := conf[start:]
	// The console user's block ends where the next user's begins.
	if next := strings.Index(block[len("user:"):], "user:"); next >= 0 {
		block = block[:len("user:")+next]
	}
	// kindBlock returns the console's `<kind> { ... }` permissions block.
	kindBlock := func(kind string) string {
		t.Helper()
		i := strings.Index(block, kind+" {")
		if i < 0 {
			t.Fatalf("the console user has no %s block", kind)
		}
		rest := block[i+len(kind+" {"):]
		depth := 1
		for j, r := range rest {
			switch r {
			case '{':
				depth++
			case '}':
				if depth--; depth == 0 {
					return rest[:j]
				}
			}
		}
		t.Fatalf("the console user's %s block is not closed", kind)
		return ""
	}
	// list returns the quoted entries of the `<field> = [` list in a kind
	// block, in order, or nil when the block has no such list.
	list := func(kb, field string) []string {
		open := strings.Index(kb, field+" = [")
		if open < 0 {
			return nil
		}
		rest := kb[open+len(field+" = ["):]
		var out []string
		for _, f := range strings.Split(rest[:strings.Index(rest, "]")], ",") {
			if f = strings.Trim(strings.TrimSpace(f), `"`); f != "" {
				out = append(out, f)
			}
		}
		return out
	}
	for _, c := range []struct {
		kind, field string
		want        []string
	}{
		{"publish", "allow", consolePublish},
		{"subscribe", "allow", consoleSubscribe},
		{"publish", "deny", consolePublishDeny},
		{"subscribe", "deny", consoleSubscribeDeny},
	} {
		got := list(kindBlock(c.kind), c.field)
		if strings.Join(got, "\n") != strings.Join(c.want, "\n") {
			t.Errorf("the rendered console %s %s list is\n  %v\nthe test has\n  %v\nupdate the test's copy", c.kind, c.field, got, c.want)
		}
	}
}

const (
	testUser = "console"
	testPass = "pw-console"
	// directoryMaxConsumers is the cap the operator provisions DIRECTORY
	// with.
	directoryMaxConsumers = 64
)

// testBus starts a JetStream server with the operator's message streams and
// a console user holding the rendered grants, and returns its URL and an
// admin connection for seeding.
func testBus(t *testing.T) (string, jetstream.JetStream) {
	t.Helper()
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true}
	opts.Users = []*natsserver.User{
		{Username: testUser, Password: testPass, Permissions: &natsserver.Permissions{
			Publish:   &natsserver.SubjectPermission{Allow: consolePublish, Deny: consolePublishDeny},
			Subscribe: &natsserver.SubjectPermission{Allow: consoleSubscribe, Deny: consoleSubscribeDeny},
		}},
		{Username: "admin", Password: "admin"},
	}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server did not start")
	}
	t.Cleanup(srv.Shutdown)

	nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo("admin", "admin"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	for name, subjects := range map[string][]string{
		lib.TasksStream:         {"a2a.tasks.>"},
		streamDirectory:         {directorySubjects},
		lib.StreamTopicsState:   {"a2a.topics.state.>"},
		lib.StreamTopicsJournal: {"a2a.topics.journal.>"},
	} {
		cfg := jetstream.StreamConfig{Name: name, Subjects: subjects, MaxBytes: 1 << 20}
		if name == streamDirectory {
			// As the operator provisions it (--max-consumers=64), so a read
			// that leaks consumers runs into the cap here as on a cluster.
			cfg.MaxMsgsPerSubject = 1
			cfg.MaxConsumers = directoryMaxConsumers
		}
		if _, err := js.CreateStream(ctx, cfg); err != nil {
			t.Fatalf("stream %s: %v", name, err)
		}
	}
	return srv.ClientURL(), js
}

func publishCard(t *testing.T, js jetstream.JetStream, profile string, card map[string]any) {
	t.Helper()
	payload, _ := json.Marshal(card)
	env, err := lib.NewAgentCardEnvelope(lib.Party{Session: profile, Profile: profile}, "corr-"+profile, payload)
	if err != nil {
		t.Fatal(err)
	}
	publishEnvelope(t, js, profile, env)
}

func publishEnvelope(t *testing.T, js jetstream.JetStream, profile string, env *lib.Envelope) {
	t.Helper()
	data, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.Publish(context.Background(), lib.AgentSubject(profile), data); err != nil {
		t.Fatal(err)
	}
}

func dial(t *testing.T, url string) *NATSBus {
	t.Helper()
	b, err := DialBus(context.Background(), BusConfig{
		URL: url, User: testUser, Password: testPass,
		Deployment: Deployment{Agent: "platform-agent", Namespace: "kubeagents-system"},
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	return b
}

// The status reads everything it shows through the console grants alone.
func TestStatusReadsTheBusAsTheConsole(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, "platform", map[string]any{"name": "Platform agent", "description": "does | things\nwell"})
	publishCard(t, js, "cluster-a", map[string]any{"name": "Cluster A"})
	publishCard(t, js, "retired", map[string]any{"name": "Retired"})
	closed, err := lib.NewAgentClosedEnvelope(lib.Party{Session: "retired", Profile: "retired"}, "corr-retired")
	if err != nil {
		t.Fatal(err)
	}
	publishEnvelope(t, js, "retired", closed)
	// The gateway's durable exists; the bridge's does not, which the status
	// names rather than failing on.
	if _, err := js.CreateOrUpdateConsumer(context.Background(), lib.TasksStream, jetstream.ConsumerConfig{Durable: gatewayRelayDurable}); err != nil {
		t.Fatal(err)
	}

	st := dial(t, url).Status(context.Background())

	if !st.Connected {
		t.Fatal("status says disconnected")
	}
	if len(st.Streams) != len(statusStreams) {
		t.Errorf("streams = %+v, want all %d", st.Streams, len(statusStreams))
	}
	if len(st.Consumers) != 1 || st.Consumers[0].Name != gatewayRelayDurable {
		t.Errorf("consumers = %+v, want the gateway relay alone", st.Consumers)
	}
	if len(st.Problems) != 1 || !strings.Contains(st.Problems[0], consumerBridgePlatform) {
		t.Errorf("problems = %v, want the missing bridge consumer named", st.Problems)
	}
	if len(st.AgentCards) != 2 || st.AgentCards["platform"].Name != "Platform agent" || st.AgentCards["cluster-a"].Name != "Cluster A" {
		t.Errorf("cards = %v, want platform and cluster-a (retired closed its card)", st.AgentCards)
	}

	md := FormatBusStatusMarkdown(st)
	for _, want := range []string{"`kubeagents-system/platform-agent`", "| `platform` | `Platform agent` | `does ¦ things well` |", "consumer TASKS/" + consumerBridgePlatform} {
		if !strings.Contains(md, want) {
			t.Errorf("markdown does not contain %q:\n%s", want, md)
		}
	}
}

func TestStatusWithNoAgentsSaysSo(t *testing.T) {
	url, _ := testBus(t)
	st := dial(t, url).Status(context.Background())
	if len(st.AgentCards) != 0 {
		t.Errorf("cards = %v, want none", st.AgentCards)
	}
	if !strings.Contains(FormatBusStatusMarkdown(st), "No agent cards on DIRECTORY") {
		t.Error("an empty directory is not stated")
	}
}

func TestDialBusRefusesAMissingCredentialOrABus(t *testing.T) {
	url, _ := testBus(t)
	for name, cfg := range map[string]BusConfig{
		"no URL":           {User: testUser, Password: testPass},
		"no password":      {URL: url, User: testUser},
		"a wrong password": {URL: url, User: testUser, Password: "nope"},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		if b, err := DialBus(ctx, cfg); err == nil {
			b.Close()
			t.Errorf("DialBus accepted %s", name)
		}
		cancel()
	}
}

func TestTheStatusToolRendersTheBus(t *testing.T) {
	url, _ := testBus(t)
	s := mustServer(t, ConnectionStatusTool(dial(t, url)))
	res := s.CallTool(context.Background(), ToolConnectionStatus, nil)
	if res.IsError || !strings.Contains(res.Content[0].Text, "connected") {
		t.Errorf("result = %+v", res)
	}
}

func TestACardUnderAnInvalidProfileIsDropped(t *testing.T) {
	cards := map[string]AgentCard{}
	payload, _ := json.Marshal(map[string]any{"name": "x"})
	env, err := lib.NewAgentCardEnvelope(lib.Party{Session: "p", Profile: "p"}, "corr-p", payload)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(env)
	for _, subject := range []string{lib.AgentSubject(""), lib.AgentSubject("a.b"), lib.AgentSubject("a`b"), "a2a.tasks.x"} {
		addCard(cards, subject, data)
	}
	if len(cards) != 0 {
		t.Errorf("cards = %v, want none from invalid subjects", cards)
	}
	addCard(cards, lib.AgentSubject("good-profile"), data)
	if len(cards) != 1 {
		t.Errorf("a valid profile was not added: %v", cards)
	}
}

func directoryConsumers(t *testing.T, js jetstream.JetStream) int {
	t.Helper()
	s, err := js.Stream(context.Background(), streamDirectory)
	if err != nil {
		t.Fatal(err)
	}
	return s.CachedInfo().State.Consumers
}

// Each read of the directory costs one consumer, which the server reaps when
// idle, and a caller polling faster than the cache costs none: never the two
// per read an ordered consumer leaves behind (its replacement, and the
// original it cannot delete under the console grant), which filled
// DIRECTORY's cap after 32 calls.
func TestStatusReadsDoNotExhaustTheDirectoryCap(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, "platform", map[string]any{"name": "Platform agent"})
	b := dial(t, url)

	for i := 0; i < 2*directoryMaxConsumers; i++ {
		st := b.Status(context.Background())
		if len(st.AgentCards) != 1 || strings.Contains(strings.Join(st.Problems, ";"), "agent cards") {
			t.Fatalf("call %d: problems %v cards %v", i, st.Problems, st.AgentCards)
		}
	}
	if got := directoryConsumers(t, js); got != 1 {
		t.Errorf("%d rapid calls left %d consumers, want 1: the cache answers them", 2*directoryMaxConsumers, got)
	}

	clock := time.Now()
	b.now = func() time.Time { clock = clock.Add(statusCacheTTL + time.Second); return clock }
	const reads = directoryMaxConsumers / 2
	for i := 0; i < reads; i++ {
		st := b.Status(context.Background())
		for _, p := range st.Problems {
			if strings.Contains(p, "agent cards") {
				t.Fatalf("read %d: %s", i, p)
			}
		}
	}
	if got := directoryConsumers(t, js); got != reads+1 {
		t.Errorf("%d uncached reads left %d consumers, want %d: one per read", reads, got, reads+1)
	}
}

// A status read is bounded by its own timeout and the caller's, whatever the
// bus does: here the caller's context is already done.
func TestStatusHonoursTheCallersDeadline(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, "platform", map[string]any{"name": "Platform agent"})
	b := dial(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	st := b.Status(ctx)
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("a cancelled read took %s", elapsed)
	}
	if len(st.Problems) == 0 {
		t.Error("a cancelled read reported no problems")
	}
	// Nor is the cut-short read anyone else's answer: the next caller,
	// inside the cache's lifetime, reads the bus.
	next := b.Status(context.Background())
	if strings.Contains(strings.Join(next.Problems, "\n"), context.Canceled.Error()) || len(next.AgentCards) != 1 {
		t.Errorf("the next caller got the cancelled read: problems %v, cards %v", next.Problems, next.AgentCards)
	}
}

// A read whose caller gave up still completes and is cached: the next
// caller inside the cache's lifetime opens no DIRECTORY consumer of its own.
func TestAReadACallerGaveUpOnIsStillCached(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, "platform", map[string]any{"name": "Platform agent"})
	b := dial(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if st := b.Status(ctx); !strings.Contains(strings.Join(st.Problems, "\n"), context.Canceled.Error()) {
		t.Errorf("the caller who gave up got %v, want its cancellation named", st.Problems)
	}
	deadline := time.Now().Add(statusTimeout)
	for directoryConsumers(t, js) != 1 {
		if time.Now().After(deadline) {
			t.Fatal("the read the cancelled caller started never reached DIRECTORY")
		}
		time.Sleep(10 * time.Millisecond)
	}
	next := b.Status(context.Background())
	if len(next.AgentCards) != 1 || strings.Contains(strings.Join(next.Problems, "\n"), context.Canceled.Error()) {
		t.Errorf("the next caller got problems %v, cards %v", next.Problems, next.AgentCards)
	}
	if got := directoryConsumers(t, js); got != 1 {
		t.Errorf("the next caller left %d DIRECTORY consumers, want 1: the abandoned read answers it", got)
	}
}

// A caller is answered by its own context, whatever the read does: one that
// gives up during a stalled read returns at once, the read runs on, and its
// answer is every later caller's, from one read.
func TestACallerDoesNotWaitOutAStalledRead(t *testing.T) {
	release := make(chan struct{})
	var mu sync.Mutex
	reads := 0
	b := &NATSBus{now: time.Now, readBus: func(context.Context) BusStatus {
		mu.Lock()
		reads++
		mu.Unlock()
		<-release
		return BusStatus{Connected: true, URL: "the-read"}
	}}
	// Two callers give up in turn while the read stalls: each returns at
	// once, and the second shares the first's read rather than starting one.
	for i := range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		start := time.Now()
		st := b.Status(ctx)
		cancel()
		if elapsed := time.Since(start); elapsed > time.Second {
			t.Errorf("caller %d: a caller whose context ended waited %s on a stalled read", i, elapsed)
		}
		if !strings.Contains(strings.Join(st.Problems, "\n"), context.DeadlineExceeded.Error()) {
			t.Errorf("caller %d: the caller who gave up got %v, want its deadline named", i, st.Problems)
		}
	}
	waiting := make(chan BusStatus)
	go func() { waiting <- b.Status(context.Background()) }()
	close(release)
	if got := <-waiting; got.URL != "the-read" {
		t.Errorf("a caller waiting on the read got %+v", got)
	}
	if got := b.Status(context.Background()); got.URL != "the-read" {
		t.Errorf("the next caller got %+v, want the cached read", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if reads != 1 {
		t.Errorf("%d reads, want 1: the abandoned read answers every later caller", reads)
	}
}

// A card read that fails is not an empty directory.
func TestAFailedCardReadIsNotNoCards(t *testing.T) {
	md := FormatBusStatusMarkdown(BusStatus{Connected: true, Problems: []string{"agent cards: context canceled"}})
	if strings.Contains(md, "No agent cards on DIRECTORY") || !strings.Contains(md, "could not be read") {
		t.Errorf("a failed card read renders as:\n%s", md)
	}
}

// A password rotated in the mounted file reaches the connection: the bus
// switches the console user to the new password and drops the old session,
// and the client's reconnect presents what the file now holds.
func TestARotatedPasswordReachesTheConnection(t *testing.T) {
	user := func(pw string) []*natsserver.User { return []*natsserver.User{{Username: testUser, Password: pw}} }
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true, Users: user("pw-one")}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server did not start")
	}
	t.Cleanup(srv.Shutdown)
	pwFile := filepath.Join(t.TempDir(), "console-password")
	if err := os.WriteFile(pwFile, []byte("pw-one\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := DialBus(context.Background(), BusConfig{URL: srv.ClientURL(), User: testUser, Password: "pw-one", PasswordFile: pwFile})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)

	if err := os.WriteFile(pwFile, []byte("pw-two\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	rotated := *opts
	rotated.Users = user("pw-two")
	if err := srv.ReloadOptions(&rotated); err != nil {
		t.Fatal(err)
	}
	// The reload closes the session authenticated with the old password.
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if b.client.Conn().IsConnected() && srv.NumClients() == 1 {
			if b.client.Conn().Reconnects > 0 {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("not reconnected on the rotated password: connected=%v reconnects=%d clients=%d",
		b.client.Conn().IsConnected(), b.client.Conn().Reconnects, srv.NumClients())
}

// The callback reads the file each time, and keeps the last good value over a
// file that is briefly empty or missing.
func TestPasswordFromFileRereadsAndKeepsTheLastGood(t *testing.T) {
	path := filepath.Join(t.TempDir(), "pw")
	cb := passwordFromFile("u", path, "initial")
	if _, pw := cb(); pw != "initial" {
		t.Errorf("missing file: %q, want the initial value", pw)
	}
	_ = os.WriteFile(path, []byte("rotated\r\n"), 0o600)
	if _, pw := cb(); pw != "rotated" {
		t.Errorf("after a rotation: %q, want rotated", pw)
	}
	_ = os.WriteFile(path, []byte("\n"), 0o600)
	if _, pw := cb(); pw != "rotated" {
		t.Errorf("an empty file mid-swap: %q, want the last good value", pw)
	}
}

// The password-file path, which the binary always takes, works under the
// rendered console grants: its inbox prefix is the console's, so JetStream
// replies reach it and a status read succeeds.
func TestThePasswordFilePathHoldsTheConsoleGrants(t *testing.T) {
	url, js := testBus(t)
	publishCard(t, js, "platform", map[string]any{"name": "Platform agent"})
	pwFile := filepath.Join(t.TempDir(), "console-password")
	if err := os.WriteFile(pwFile, []byte(testPass+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	b, err := DialBus(context.Background(), BusConfig{URL: url, User: testUser, Password: testPass, PasswordFile: pwFile})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.Close)
	st := b.Status(context.Background())
	if len(st.AgentCards) != 1 {
		t.Errorf("status over the password-file path: cards %v, problems %v", st.AgentCards, st.Problems)
	}
}

// A connection_status call whose request ends before the read does (a stop,
// or the caller's deadline) is an error, never a status reading as a
// disconnected bus.
func TestAStatusCallThatEndsEarlyIsAnErrorNotDisconnected(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	b := &NATSBus{now: time.Now, readBus: func(context.Context) BusStatus {
		<-release
		return BusStatus{URL: "the-read", Connected: true}
	}}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	text, err := ConnectionStatusTool(b).Call(ctx, nil)
	if err == nil || strings.Contains(text, "disconnected") {
		t.Errorf("an early-ended call returned %q, err %v; want an error and no status", text, err)
	}
}

// A read that panics is turned into a problem, stored, and its waiters are
// released: a later call gets an answer instead of waiting forever.
func TestAPanickingStatusReadIsReportedAndReleased(t *testing.T) {
	b := &NATSBus{now: time.Now, readBus: func(context.Context) BusStatus { panic("boom") }}
	done := make(chan BusStatus, 1)
	go func() { done <- b.Status(context.Background()) }()
	select {
	case st := <-done:
		if len(st.Problems) == 0 || !strings.Contains(st.Problems[0], "boom") {
			t.Errorf("problems = %v, want the panic reported", st.Problems)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a panicking read left its caller waiting")
	}
}
