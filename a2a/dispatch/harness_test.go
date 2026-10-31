package dispatch

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The test bus: an embedded JetStream server (the a2a/mcp testBus shape)
// with the TASKS and PROFILE_SUBMITS streams, the PROFILE_LEASES bucket, an admin user for the
// gateway, the dispatcher and the test, and one user per worker pod holding
// only the contract's worker grants. No worker can touch the lease bucket,
// so every test is also a test that the dispatcher is its only writer.

const (
	adminUser = "admin"
	adminPass = "admin-pw"
	workerPW  = "worker-pw"
)

// testConfig is the dispatcher's process settings at test speed.
func testConfig() Config {
	c := DefaultConfig()
	c.RetryDelay = 200 * time.Millisecond
	c.HealthInterval = 200 * time.Millisecond
	c.StallAfter = time.Second
	c.RefreshInterval = 200 * time.Millisecond
	c.SweepInterval = 200 * time.Millisecond
	c.SweepAfter = 10 * time.Second
	return c
}

// testInstall is the config source and the pool a test drives.
type testInstall struct {
	src  *StaticSource
	pool *StaticPool
}

// testProfile is a profile allowing tiers (read when none), with a durable
// per (tier, kind).
func testProfile(name string, concurrency int, tiers ...string) lib.DispatchProfile {
	if len(tiers) == 0 {
		tiers = []string{"read"}
	}
	p := lib.DispatchProfile{Name: name, Tiers: tiers, Concurrency: concurrency,
		QueueTimeoutSeconds: 3600, ActiveDeadlineSeconds: 60}
	for _, tier := range tiers {
		for _, kind := range submitKinds {
			p.Durables = append(p.Durables, lib.DispatchDurable{Tier: tier, Kind: kind, Name: lib.ProfileDurableName(name, tier, kind)})
		}
	}
	return p
}

// testDispatchConfig is the install at test speed. Lease and renewal ride
// the offer as whole seconds, so they can go no lower.
func testDispatchConfig(ceiling int, profiles ...lib.DispatchProfile) lib.DispatchConfig {
	// The operator's shape: disjoint reserves of about a third each, child
	// first, leaving at least one shared slot (ceiling 1: none, 2: child 1,
	// 3: 1 and 1, 6: 2 and 2).
	third := max(1, (ceiling+2)/3)
	child := min(third, ceiling-1)
	interactive := min(third, ceiling-1-child)
	return lib.DispatchConfig{Version: "test", Namespace: "kube-agents", ProfileNamespace: "kube-agents-profiles", Ceiling: ceiling, InteractiveReserve: interactive, ChildReserve: child,
		LeaseSeconds: 3, RenewSeconds: 1, RestartGraceSeconds: 2, AcceptTimeoutSeconds: 1, Profiles: profiles}
}

// oneProfile is an install with one profile whose read pool is pods.
func oneProfile(name string, concurrency int, pods ...string) *testInstall {
	ti := &testInstall{src: NewStaticSource(testDispatchConfig(6, testProfile(name, concurrency))), pool: NewStaticPool()}
	ti.pool.Set(name, "read", pods...)
	return ti
}

// update edits the install config in place.
func (ti *testInstall) update(edit func(*lib.DispatchConfig)) {
	cfg := ti.src.Config()
	cfg.Profiles = append([]lib.DispatchProfile(nil), cfg.Profiles...)
	edit(&cfg)
	if err := cfg.Validate(); err != nil {
		panic(err)
	}
	ti.src.Set(cfg)
}

type bus struct {
	t       *testing.T
	ctx     context.Context
	srv     *natsserver.Server
	url     string
	gateway *lib.Client
	admin   *nats.Conn
	js      jetstream.JetStream

	mu        sync.Mutex
	terminals map[string][]terminalSeen
}

type terminalSeen struct {
	state lib.TaskState
	text  string
	from  string
	// class is the subject's last token: an executor's terminal is on
	// `.events`, the dispatcher's on `.supervisor` (contract addendum 6).
	class string
}

func workerUser(pod string) string { return "w-" + pod }

// newBus starts the server with a user for each pod.
func newBus(t *testing.T, pods ...string) *bus {
	t.Helper()
	return newBusWith(t, nil, pods...)
}

// newBusWith adds extra users (a dispatcher's, with narrower grants).
func newBusWith(t *testing.T, extra []*natsserver.User, pods ...string) *bus {
	t.Helper()
	users := append([]*natsserver.User{{Username: adminUser, Password: adminPass}}, extra...)
	for _, pod := range pods {
		users = append(users, &natsserver.User{Username: workerUser(pod), Password: workerPW,
			Permissions: &natsserver.Permissions{
				Publish: &natsserver.SubjectPermission{Allow: []string{
					"a2a.dispatch.*.*." + pod + ".accept",
					"a2a.dispatch.*.*." + pod + ".lease",
					"a2a.tasks.*.*.events",
					"$JS.API.STREAM.INFO.*", // the lib's publish path asks nothing else
				}},
				Subscribe: &natsserver.SubjectPermission{Allow: []string{"a2a.dispatch.*.*." + pod, "_INBOX.>"}},
			}})
	}
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(),
		NoLog: true, NoSigs: true, Users: users}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	t.Cleanup(cancel)
	b := &bus{t: t, ctx: ctx, srv: srv, url: srv.ClientURL(), terminals: map[string][]terminalSeen{}}
	b.admin, err = nats.Connect(b.url, nats.UserInfo(adminUser, adminPass))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(b.admin.Close)
	b.js, err = jetstream.New(b.admin)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := b.js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{Name: "TASKS", Subjects: []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy, MaxAge: time.Hour}); err != nil {
		t.Fatal(err)
	}
	// Profile submissions' own stream (contract addendum 7), one message per
	// subject as provisioned.
	if _, err := b.js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{Name: lib.ProfileSubmitStream,
		Subjects: []string{lib.SubmitSubjectPrefix + ">"}, Retention: jetstream.LimitsPolicy, MaxAge: time.Hour,
		MaxMsgsPerSubject: 1, Duplicates: 2 * time.Minute}); err != nil {
		t.Fatal(err)
	}
	if _, err := b.js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.ProfileLeaseBucket, History: 1}); err != nil {
		t.Fatal(err)
	}
	b.gateway = b.connect("gateway")
	for _, class := range []string{lib.TaskClassEvents, lib.TaskClassSupervisor} {
		if _, err := b.admin.Subscribe("a2a.tasks.*.*."+class, b.onEvent); err != nil {
			t.Fatal(err)
		}
	}
	return b
}

func (b *bus) connect(name string) *lib.Client {
	b.t.Helper()
	c, err := lib.Connect(b.ctx, b.url, lib.WithName(name), lib.WithLogger(quietLog()),
		lib.WithNATSOptions(nats.UserInfo(adminUser, adminPass)))
	if err != nil {
		b.t.Fatal(err)
	}
	b.t.Cleanup(c.Close)
	return c
}

func quietLog() *slog.Logger {
	if os.Getenv("DISPATCH_TEST_LOG") != "" {
		return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug}))
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func (b *bus) onEvent(msg *nats.Msg) {
	env, err := lib.ParseEnvelope(msg.Data)
	if err != nil || env.Kind != lib.KindStatusUpdate {
		return
	}
	_, _, class, ok := lib.ParseTaskSubject(msg.Subject)
	if !ok {
		return
	}
	var su lib.StatusUpdate
	if json.Unmarshal(env.Payload, &su) != nil || !su.Final {
		return
	}
	var text string
	if su.Status.Message != nil && len(su.Status.Message.Parts) > 0 {
		text = su.Status.Message.Parts[0].Text
	}
	b.mu.Lock()
	b.terminals[env.TaskID] = append(b.terminals[env.TaskID], terminalSeen{su.Status.State, text, env.From.Session, class})
	b.mu.Unlock()
}

func (b *bus) terminalsOf(taskID string) []terminalSeen {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]terminalSeen(nil), b.terminals[taskID]...)
}

// waitTerminal waits for a task's first terminal.
func (b *bus) waitTerminal(taskID string, within time.Duration) terminalSeen {
	b.t.Helper()
	var got terminalSeen
	waitFor(b.t, within, "a terminal for "+taskID, func() bool {
		ts := b.terminalsOf(taskID)
		if len(ts) > 0 {
			got = ts[0]
			return true
		}
		return false
	})
	return got
}

// ensure creates the operator's durables for profile at every tier.
func (b *bus) ensure(profile string, maxAckPending int, ackWait time.Duration, tiers ...string) {
	b.t.Helper()
	for _, tier := range tiers {
		for _, kind := range submitKinds {
			if err := b.gateway.EnsureHeldConsumer(b.ctx, lib.HeldConsumerConfig{Stream: lib.ProfileSubmitStream,
				Durable: lib.ProfileDurableName(profile, tier, kind), FilterSubject: lib.ProfileDurableFilter(profile, tier, kind),
				MaxAckPending: maxAckPending, AckWait: ackWait}); err != nil {
				b.t.Fatalf("EnsureHeldConsumer: %v", err)
			}
		}
	}
}

// submission is the gateway's submission envelope, with a capability ref
// keyed capKey and the given task parameters.
func submissionEnv(t *testing.T, profile, taskID, capKey string, params map[string]string) *lib.Envelope {
	t.Helper()
	msg := lib.Message{Role: "user", MessageID: "m-" + taskID, Parts: []lib.Part{{Kind: "text", Text: "do the thing"}}}
	if params != nil {
		part, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: params})
		if err != nil {
			t.Fatal(err)
		}
		msg.Parts = append(msg.Parts, part)
	}
	payload, err := json.Marshal(msg)
	if err != nil {
		t.Fatal(err)
	}
	var opts []lib.EnvelopeOption
	opts = append(opts, lib.WithTo(lib.Party{Session: profile}))
	if capKey != "" {
		opts = append(opts, lib.WithAuthority(json.RawMessage(fmt.Sprintf(`{"grants":{"capability":{"key":%q,"revision":1}}}`, capKey))))
	}
	env, err := lib.NewMessageEnvelope(lib.Party{Session: "gateway", AgentType: "a2a-gateway"}, taskID, "ctx-"+taskID, "corr-"+taskID, payload, opts...)
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func (b *bus) submit(profile, tier, kind, taskID string) {
	b.submitWith(profile, tier, kind, taskID, "cap-ok", map[string]string{})
}

func (b *bus) submitWith(profile, tier, kind, taskID, capKey string, params map[string]string) {
	b.t.Helper()
	if err := b.gateway.Publish(b.ctx, lib.TaskSubmitSubject(profile, tier, kind, taskID),
		submissionEnv(b.t, profile, taskID, capKey, params)); err != nil {
		b.t.Fatalf("submit %s: %v", taskID, err)
	}
}

// leases lists the lease bucket's running records (no tombstones) by key.
func (b *bus) leases() map[string]lib.LeaseRecord {
	b.t.Helper()
	out := map[string]lib.LeaseRecord{}
	for k, rec := range b.records() {
		if !rec.Finished() {
			out[k] = rec
		}
	}
	return out
}

// tombstones lists the lease bucket's tombstones by key.
func (b *bus) tombstones() map[string]lib.LeaseRecord {
	b.t.Helper()
	out := map[string]lib.LeaseRecord{}
	for k, rec := range b.records() {
		if rec.Finished() {
			out[k] = rec
		}
	}
	return out
}

// records lists every record in the lease bucket by key.
func (b *bus) records() map[string]lib.LeaseRecord {
	b.t.Helper()
	kv, err := b.js.KeyValue(b.ctx, lib.ProfileLeaseBucket)
	if err != nil {
		b.t.Fatal(err)
	}
	out := map[string]lib.LeaseRecord{}
	lister, err := kv.ListKeys(b.ctx)
	if err != nil {
		b.t.Fatal(err)
	}
	for k := range lister.Keys() {
		e, err := kv.Get(b.ctx, k)
		if err != nil {
			continue
		}
		var rec lib.LeaseRecord
		_ = json.Unmarshal(e.Value(), &rec)
		out[k] = rec
	}
	return out
}

// pending is how many deliveries a durable holds unacknowledged, plus how
// many it still has to deliver.
func (b *bus) pending(durable string) (ackPending int, numPending uint64) {
	b.t.Helper()
	c, err := b.js.Consumer(b.ctx, lib.ProfileSubmitStream, durable)
	if err != nil {
		b.t.Fatal(err)
	}
	info, err := c.Info(b.ctx)
	if err != nil {
		b.t.Fatal(err)
	}
	return info.NumAckPending, info.NumPending
}

// fakeVerifier refuses any ref whose key is in refuse.
type fakeVerifier struct {
	mu     sync.Mutex
	refuse map[string]bool
	calls  []string
}

func (v *fakeVerifier) CheckProfileRun(_ context.Context, ref capability.Ref, profile string, tier capability.Tier, target capability.Scope) error {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.calls = append(v.calls, fmt.Sprintf("%s|%s|%s|%s", ref.Key, profile, tier, target))
	if v.refuse[ref.Key] {
		return &capability.Refusal{Rule: "the grant does not reach that target"}
	}
	return nil
}

func (v *fakeVerifier) callList() []string {
	v.mu.Lock()
	defer v.mu.Unlock()
	return append([]string(nil), v.calls...)
}

// runDispatcher starts a dispatcher on its own connection and returns a
// stop that waits for Run to return.
func (b *bus) runDispatcher(cfg Config, ti *testInstall, v Verifier) (*Dispatcher, func()) {
	b.t.Helper()
	return b.runDispatcherOn(b.connect("dispatcher"), cfg, ti, v)
}

// runDispatcherOn is runDispatcher on a connection the caller holds, so a
// test can kill it.
func (b *bus) runDispatcherOn(c *lib.Client, cfg Config, ti *testInstall, v Verifier) (*Dispatcher, func()) {
	b.t.Helper()
	d, err := New(cfg, c, ti.src, ti.pool, v, quietLog())
	if err != nil {
		b.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(b.ctx)
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := d.Run(ctx); err != nil {
			b.t.Errorf("Run: %v", err)
		}
	}()
	var once sync.Once
	stop := func() {
		once.Do(func() {
			cancel()
			<-done
		})
	}
	b.t.Cleanup(stop)
	return d, stop
}

// workerMode is how a fake worker answers.
type workerMode int

const (
	answerAccept  workerMode = iota
	answerDecline            // declines every offer
	answerSilent             // never answers
)

// fakeWorker is a warm worker on its own restricted connection. It keeps
// the contract's waiting side: an accepted offer starts only on the
// dispatcher's confirm, an abandon drops it, and one that hears neither
// within pendingFor is dropped.
type fakeWorker struct {
	t             *testing.T
	b             *bus
	profile, tier string
	pod           string
	nc            *nats.Conn
	mode          workerMode
	// run is the task's work; nil means it runs for runFor. It returns
	// when the task's context ends (abandon or kill).
	run    func(ctx context.Context, offer lib.DispatchMessage)
	runFor time.Duration
	// skipFinal publishes the terminal but sends no final renewal (a crash
	// between the two).
	skipFinal bool
	// noRenew accepts and never renews.
	noRenew bool
	// acceptDelay holds each accept back that long (a late accept).
	acceptDelay time.Duration
	// dropAccept takes the offer and waits for a confirm, but its accept
	// never reaches the bus (a lost accept).
	dropAccept bool
	// dropConfirm never hears a confirm (a lost confirm).
	dropConfirm bool

	mu        sync.Mutex
	offers    []lib.DispatchMessage
	abandons  []lib.DispatchMessage
	confirms  []lib.DispatchMessage
	pending   map[string]lib.DispatchMessage
	started   []string
	cancels   map[string]context.CancelFunc
	killed    atomic.Bool
	completed atomic.Int32
}

func (b *bus) worker(profile, tier, pod string, mode workerMode, runFor time.Duration) *fakeWorker {
	b.t.Helper()
	nc, err := nats.Connect(b.url, nats.UserInfo(workerUser(pod), workerPW),
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		b.t.Fatalf("worker %s connect: %v", pod, err)
	}
	b.t.Cleanup(nc.Close)
	w := &fakeWorker{t: b.t, b: b, profile: profile, tier: tier, pod: pod, nc: nc, mode: mode, runFor: runFor,
		cancels: map[string]context.CancelFunc{}, pending: map[string]lib.DispatchMessage{}}
	if _, err := nc.Subscribe(lib.DispatchSubject(profile, tier, pod), w.onMessage); err != nil {
		b.t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		b.t.Fatal(err)
	}
	return w
}

func (w *fakeWorker) offerCount() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.offers)
}

func (w *fakeWorker) abandonCount() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.abandons)
}

func (w *fakeWorker) confirmCount() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.confirms)
}

// startedCount is how many tasks the worker started (on a confirm).
func (w *fakeWorker) startedCount() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.started)
}

// pendingFor is how long an accepted offer waits for a confirm or an
// abandon before the worker drops it.
const pendingFor = 5 * time.Second

// kill stops every renewal and every task, as a dead pod.
func (w *fakeWorker) kill() {
	w.killed.Store(true)
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, c := range w.cancels {
		c()
	}
}

func (w *fakeWorker) publish(subject string, v any) {
	data, _ := json.Marshal(v)
	_ = w.nc.Publish(subject, data)
}

func (w *fakeWorker) onMessage(msg *nats.Msg) {
	var m lib.DispatchMessage
	if err := json.Unmarshal(msg.Data, &m); err != nil {
		w.t.Errorf("worker %s: bad dispatch message: %v", w.pod, err)
		return
	}
	if err := m.Validate(); err != nil {
		w.t.Errorf("worker %s: dispatch message fails Validate: %v", w.pod, err)
		return
	}
	w.mu.Lock()
	switch m.Type {
	case lib.DispatchAbandon:
		w.abandons = append(w.abandons, m)
		delete(w.pending, m.TaskID)
		if c := w.cancels[m.TaskID]; c != nil {
			c()
		}
		w.mu.Unlock()
		return
	case lib.DispatchConfirm:
		w.confirms = append(w.confirms, m)
		offer, ok := w.pending[m.TaskID]
		delete(w.pending, m.TaskID)
		w.mu.Unlock()
		if ok && !w.dropConfirm && !w.killed.Load() {
			w.start(offer)
		}
		return
	case lib.DispatchOffer:
		w.offers = append(w.offers, m)
	}
	w.mu.Unlock()
	if w.killed.Load() {
		return
	}
	accept := lib.DispatchAcceptSubject(w.profile, w.tier, w.pod)
	switch w.mode {
	case answerSilent:
		return
	case answerDecline:
		w.publish(accept, lib.DispatchAccept{TaskID: m.TaskID, Accepted: false, Reason: "busy"})
		return
	}
	w.mu.Lock()
	w.pending[m.TaskID] = m
	w.mu.Unlock()
	time.AfterFunc(pendingFor, func() {
		w.mu.Lock()
		delete(w.pending, m.TaskID)
		w.mu.Unlock()
	})
	if w.dropAccept {
		return
	}
	if w.acceptDelay > 0 {
		time.AfterFunc(w.acceptDelay, func() { w.publish(accept, lib.DispatchAccept{TaskID: m.TaskID, Accepted: true}) })
		return
	}
	w.publish(accept, lib.DispatchAccept{TaskID: m.TaskID, Accepted: true})
}

// start runs a confirmed task.
func (w *fakeWorker) start(m lib.DispatchMessage) {
	ctx, cancel := context.WithCancel(context.Background())
	w.mu.Lock()
	w.cancels[m.TaskID] = cancel
	w.started = append(w.started, m.TaskID)
	w.mu.Unlock()
	go w.renew(ctx, m)
	go w.work(ctx, cancel, m)
}

func (w *fakeWorker) renew(ctx context.Context, m lib.DispatchMessage) {
	if w.noRenew {
		return
	}
	t := time.NewTicker(time.Duration(m.RenewSeconds) * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if w.killed.Load() {
				return
			}
			w.publish(lib.DispatchLeaseSubject(w.profile, w.tier, w.pod), lib.LeaseRenewal{TaskID: m.TaskID})
		}
	}
}

func (w *fakeWorker) work(ctx context.Context, cancel context.CancelFunc, m lib.DispatchMessage) {
	defer cancel()
	if w.run != nil {
		w.run(ctx, m)
	} else {
		select {
		case <-time.After(w.runFor):
		case <-ctx.Done():
		}
	}
	if ctx.Err() != nil || w.killed.Load() {
		return
	}
	w.finish(m.TaskID, lib.StateCompleted)
	w.completed.Add(1)
}

// finish publishes the task's terminal as its executor, then the final
// renewal unless skipFinal.
func (w *fakeWorker) finish(taskID string, state lib.TaskState) {
	payload, _ := json.Marshal(lib.StatusUpdate{TaskID: taskID, ContextID: "ctx-" + taskID,
		Status: lib.TaskStatus{State: state, Message: &lib.Message{Role: "agent", MessageID: "done-" + taskID,
			Parts: []lib.Part{{Kind: "text", Text: "done"}}}}, Final: true})
	env, err := lib.NewStatusUpdateEnvelope(lib.Party{Session: w.pod, Profile: w.profile}, taskID, "ctx-"+taskID, "corr-"+taskID, payload)
	if err != nil {
		w.t.Errorf("terminal: %v", err)
		return
	}
	data, _ := json.Marshal(env)
	// The worker's grant is core publish on .events; the stream stores it.
	if err := w.nc.Publish(lib.TaskEventsSubject(w.profile, taskID), data); err != nil {
		w.t.Errorf("publish terminal: %v", err)
	}
	if !w.skipFinal {
		w.publish(lib.DispatchLeaseSubject(w.profile, w.tier, w.pod), lib.LeaseRenewal{TaskID: taskID, Final: true})
	}
}

// waitFor polls until cond holds or the deadline passes.
func waitFor(t *testing.T, d time.Duration, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out after %v waiting for %s", d, what)
}

func hasReason(ts terminalSeen, reason string) bool {
	return strings.HasPrefix(ts.text, "reason: "+reason)
}
