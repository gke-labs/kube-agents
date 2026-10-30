package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

const (
	pwProfile = "auditor"
	pwTier    = "read"
	pwPod     = "auditor-read-7f9c"
)

// fakeDispatcher is the dispatcher's side of the hand-off: it offers,
// confirms and abandons on the pod's subject, and reads the pod's accepts
// and renewals.
type fakeDispatcher struct {
	t       *testing.T
	nc      *nats.Conn
	accepts chan lib.DispatchAccept
	leases  chan lib.LeaseRenewal
}

func newFakeDispatcher(t *testing.T, url string) *fakeDispatcher {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	d := &fakeDispatcher{t: t, nc: nc, accepts: make(chan lib.DispatchAccept, 16), leases: make(chan lib.LeaseRenewal, 256)}
	if _, err := nc.Subscribe(lib.DispatchAcceptSubject(pwProfile, pwTier, pwPod), func(m *nats.Msg) {
		var a lib.DispatchAccept
		if json.Unmarshal(m.Data, &a) == nil {
			d.accepts <- a
		}
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := nc.Subscribe(lib.DispatchLeaseSubject(pwProfile, pwTier, pwPod), func(m *nats.Msg) {
		var r lib.LeaseRenewal
		if json.Unmarshal(m.Data, &r) == nil {
			d.leases <- r
		}
	}); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return d
}

func (d *fakeDispatcher) send(m lib.DispatchMessage) {
	d.t.Helper()
	data, err := json.Marshal(m)
	if err != nil {
		d.t.Fatal(err)
	}
	if err := d.nc.Publish(lib.DispatchSubject(pwProfile, pwTier, pwPod), data); err != nil {
		d.t.Fatal(err)
	}
}

func (d *fakeDispatcher) offer(task string, env *lib.Envelope, acceptSeconds int) {
	d.t.Helper()
	raw, err := json.Marshal(env)
	if err != nil {
		d.t.Fatal(err)
	}
	d.send(lib.DispatchMessage{
		Type: lib.DispatchOffer, TaskID: task, Kind: lib.SubmitKindTop, Envelope: raw,
		Deadline: time.Now().Add(time.Minute), LeaseSeconds: 3, RenewSeconds: 1, AcceptSeconds: acceptSeconds,
	})
}

func (d *fakeDispatcher) nextAccept(within time.Duration) lib.DispatchAccept {
	d.t.Helper()
	select {
	case a := <-d.accepts:
		return a
	case <-time.After(within):
		d.t.Fatal("no answer to the offer")
		return lib.DispatchAccept{}
	}
}

// profileSubmission is the gateway's submission for a profile task, as the
// dispatcher carries it in the offer. It isn't published anywhere: the
// worker reads it from the offer.
func profileSubmission(t *testing.T, task, text string, params map[string]string) *lib.Envelope {
	t.Helper()
	parts := []lib.Part{{Kind: "text", Text: text}}
	if params != nil {
		p, err := lib.TaskParametersPart(lib.TaskParameters{Parameters: params})
		if err != nil {
			t.Fatal(err)
		}
		parts = append(parts, p)
	}
	payload, err := json.Marshal(lib.Message{Role: "user", Parts: parts, MessageID: "msg-" + task, TaskID: task, ContextID: "ctx-" + task})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, task, "ctx-"+task, "corr-"+task, payload, lib.WithTo(lib.Party{Session: pwProfile}))
	if err != nil {
		t.Fatal(err)
	}
	return env
}

func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().String()
}

func profileWorkerConfig(t *testing.T, url string, harness []string) Config {
	t.Helper()
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("bus-token-for-tests"), 0o600); err != nil {
		t.Fatal(err)
	}
	return Config{
		NATSURL: url, Profile: pwProfile, PodName: pwPod, ProfileExecutor: true, BusTokenFile: token,
		HarnessCommand: harness, HarnessEnv: os.Environ(), KillGrace: 500 * time.Millisecond,
	}
}

// startProfileWorker runs the worker and waits until /readyz says it is
// listening, which is the moment the dispatcher may offer.
func startProfileWorker(t *testing.T, ctx context.Context, cfg Config, oneShot bool) <-chan error {
	t.Helper()
	addr := freeAddr(t)
	done := make(chan error, 1)
	go func() {
		done <- RunProfileWorker(ctx, cfg, ProfileWorkerConfig{Tier: pwTier, OneShot: oneShot, ReadyAddr: addr, deadlineMargin: time.Second})
	}()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case err := <-done:
			t.Fatalf("the worker ended before it was ready: %v", err)
		default:
		}
		if resp, err := http.Get("http://" + addr + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return done
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("the worker never became ready")
	return done
}

// drainLeases collects renewals for task until a final one or the window
// ends.
func drainLeases(d *fakeDispatcher, task string, window time.Duration) (renewals int, final bool) {
	end := time.After(window)
	for {
		select {
		case r := <-d.leases:
			if r.TaskID != task {
				continue
			}
			if r.Final {
				return renewals, true
			}
			renewals++
		case <-end:
			return renewals, false
		}
	}
}

// The whole hand-off: accept, start only on confirm, renew, publish the
// task's events with submitted first and one final last, then a final
// renewal. The task's parameters reach the harness as PROFILE_TASK_<NAME>.
func TestAProfileWorkerRunsADispatchedTask(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-1"}'
read first || exit 1
sleep 1.5
printf '{"type":"result","subtype":"success","result":"cluster=%s"}\n' "$PROFILE_TASK_CLUSTER"
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)

	const task = "task-pw-1"
	d.offer(task, profileSubmission(t, task, "audit the cluster", map[string]string{"cluster": "prod-east"}), 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
		t.Fatalf("answer = %+v, want an accept", a)
	}
	// Nothing runs before the confirm.
	time.Sleep(300 * time.Millisecond)
	if evs := replayEvents(t, url, pwProfile, task); len(evs) != 0 {
		t.Fatalf("%d events before the confirm; the worker must start only on it", len(evs))
	}
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})

	renewals, final := drainLeases(d, task, 20*time.Second)
	if !final {
		t.Fatal("no final renewal after the task ended")
	}
	if renewals == 0 {
		t.Error("no renewals while the task ran")
	}
	task1 := foldTask(t, testClient(t, url), pwProfile, task)
	if task1.State != lib.StateCompleted {
		t.Fatalf("state %q, want completed", task1.State)
	}
	if got := artifactText(task1, lib.ArtifactResult); got != "cluster=prod-east" {
		t.Errorf("result %q, want the parameter passed through", got)
	}
	evs := replayEvents(t, url, pwProfile, task)
	if s := statusOf(t, evs[0]); s.Status.State != lib.StateSubmitted {
		t.Errorf("first event %+v, want submitted", s)
	}
}

// An offer while the pod holds a task is declined; one with no confirm in
// the accept window is dropped, renewals stop, and the pod takes the next.
func TestAProfileWorkerHoldsOneTaskAndDropsAnUnconfirmedOne(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, stub(t, "exit 0")), false)

	d.offer("task-a", profileSubmission(t, "task-a", "first", nil), 2)
	if a := d.nextAccept(5 * time.Second); !a.Accepted {
		t.Fatalf("first offer: %+v", a)
	}
	d.offer("task-b", profileSubmission(t, "task-b", "second", nil), 2)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-b" || a.Accepted || a.Reason != declineBusy {
		t.Fatalf("second offer while holding one = %+v, want declined busy", a)
	}
	// No confirm: the hold lapses after 2s and renewals stop.
	time.Sleep(3 * time.Second)
	for len(d.leases) > 0 {
		<-d.leases
	}
	if n, _ := drainLeases(d, "task-a", 2500*time.Millisecond); n != 0 {
		t.Errorf("%d renewals after the accept window lapsed", n)
	}
	if evs := replayEvents(t, url, pwProfile, "task-a"); len(evs) != 0 {
		t.Errorf("the unconfirmed task published %d events", len(evs))
	}
	d.offer("task-c", profileSubmission(t, "task-c", "third", nil), 2)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-c" || !a.Accepted {
		t.Fatalf("an offer after the lapse = %+v, want an accept", a)
	}
}

// An abandon mid-task stops the harness, and the worker publishes no
// terminal and no final renewal: the dispatcher writes that task's terminal.
func TestAnAbandonedTaskPublishesNoTerminal(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-2"}'
read first || exit 1
sleep 30
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)

	const task = "task-abandon"
	d.offer(task, profileSubmission(t, task, "long job", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	waitState(t, testClient(t, url), pwProfile, task, lib.StateWorking)
	d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: task, Reason: "lease-expired - test"})

	if _, final := drainLeases(d, task, 3*time.Second); final {
		t.Error("a final renewal after an abandon")
	}
	if got := foldTask(t, testClient(t, url), pwProfile, task); got.Final {
		t.Errorf("the worker published a terminal (%s) for an abandoned task", got.State)
	}
	// And the pod is free again.
	d.offer("task-next", profileSubmission(t, "task-next", "next", nil), 2)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-next" || !a.Accepted {
		t.Fatalf("an offer after the abandon = %+v, want an accept", a)
	}
}

// A one-shot worker, a Job's pod, ends after its first task.
func TestAOneShotWorkerEndsAfterItsTask(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
read first || exit 1
echo '{"type":"result","subtype":"success","result":"done"}'
`)
	done := startProfileWorker(t, context.Background(), profileWorkerConfig(t, url, harness), true)

	const task = "task-oneshot"
	d.offer(task, profileSubmission(t, task, "once", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("one-shot worker: %v", err)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the one-shot worker didn't end after its task")
	}
	if _, final := drainLeases(d, task, time.Second); !final {
		t.Error("no final renewal before the one-shot worker ended")
	}
}

// An offer whose envelope is for another task or another profile is
// declined, and nothing runs.
func TestAProfileWorkerDeclinesAMismatchedOffer(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, stub(t, "exit 0")), false)

	d.offer("task-x", profileSubmission(t, "task-y", "wrong task", nil), 2)
	if a := d.nextAccept(5 * time.Second); a.Accepted || !strings.HasPrefix(a.Reason, "mismatch") {
		t.Errorf("an envelope for another task = %+v, want declined", a)
	}
	other := profileSubmission(t, "task-z", "wrong profile", nil)
	other.To = &lib.Party{Session: "someone-else"}
	d.offer("task-z", other, 2)
	if a := d.nextAccept(5 * time.Second); a.Accepted || !strings.HasPrefix(a.Reason, "mismatch") {
		t.Errorf("an envelope for another profile = %+v, want declined", a)
	}
}

// waitReady waits until the worker at addr answers /readyz.
func waitReady(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get("http://" + addr + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("the worker never became ready")
}

// The confirm's credentials (addendum 8): with a task credential, the task's
// consumers are the task connection's (<pod>_<task>-in), and the proxy token
// is in the shims' file only while the task runs.
func TestAConfirmsCredentialsReachTheTaskAndOnlyWhileItRuns(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	tokenFile := filepath.Join(t.TempDir(), "task-token")
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
sleep 1.5
printf '{"type":"result","subtype":"success","result":"token=%%s"}\n' "$(cat %s)"
`, tokenFile))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	addr := freeAddr(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{Tier: pwTier, ReadyAddr: addr, TaskTokenFile: tokenFile, deadlineMargin: time.Second})
	}()
	waitReady(t, addr)

	const task = "task-creds"
	d.offer(task, profileSubmission(t, task, "use the cluster", nil), 5)
	d.nextAccept(5 * time.Second)
	confirm, err := json.Marshal(map[string]string{
		"type": lib.DispatchConfirm, "taskId": task,
		"taskCredential": "cred-for-test", "proxyToken": "proxy-token-for-test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := d.nc.Publish(lib.DispatchSubject(pwProfile, pwTier, pwPod), confirm); err != nil {
		t.Fatal(err)
	}
	waitState(t, testClient(t, url), pwProfile, task, lib.StateWorking)

	// The task's input consumer is the task connection's, not the pod's.
	js, err := jetstream.New(d.nc)
	if err != nil {
		t.Fatal(err)
	}
	stream, err := js.Stream(context.Background(), lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	lister := stream.ConsumerNames(context.Background())
	for n := range lister.Name() {
		names = append(names, n)
	}
	want := lib.SessionConsumerName(lib.TaskConsumerStem(pwPod, task), lib.SessionConsumerIn)
	if !slices.Contains(names, want) {
		t.Errorf("consumers %v, want the task connection's %s", names, want)
	}
	if slices.Contains(names, lib.SessionConsumerName(pwPod, lib.SessionConsumerIn)) {
		t.Errorf("consumers %v include the pod's own input consumer", names)
	}

	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal")
	}
	if got := artifactText(foldTask(t, testClient(t, url), pwProfile, task), lib.ArtifactResult); got != "token=proxy-token-for-test" {
		t.Errorf("the harness read %q from the token file, want the confirm's proxy token", got)
	}
	if _, err := os.Stat(tokenFile); !os.IsNotExist(err) {
		t.Errorf("the proxy token file outlived its task: %v", err)
	}
}

// A task connection that can't be opened fails closed: nothing runs on the
// pod connection's wider grants, no terminal and no final renewal go out,
// and the dispatcher's lease expiry fails the task.
func TestARefusedTaskConnectionRunsNothing(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, stub(t, `read first; echo '{"type":"result","subtype":"success","result":"ran"}'`))
	startProfileWorker(t, ctx, cfg, false)
	// The pod connection is up; the task connection, which reads the
	// token afresh, now can't.
	if err := os.Remove(cfg.BusTokenFile); err != nil {
		t.Fatal(err)
	}

	const task = "task-refused-conn"
	d.offer(task, profileSubmission(t, task, "anything", nil), 5)
	d.nextAccept(5 * time.Second)
	confirm, _ := json.Marshal(map[string]string{"type": lib.DispatchConfirm, "taskId": task, "taskCredential": "cred"})
	if err := d.nc.Publish(lib.DispatchSubject(pwProfile, pwTier, pwPod), confirm); err != nil {
		t.Fatal(err)
	}
	if _, final := drainLeases(d, task, 3*time.Second); final {
		t.Error("a final renewal for a task that never ran")
	}
	if evs := replayEvents(t, url, pwProfile, task); len(evs) != 0 {
		t.Errorf("%d events for a task whose task connection was refused", len(evs))
	}
}

// The persona is rendered into a fresh CLAUDE_CONFIG_DIR for the task, its
// flags follow the base argv, and the directory is gone when the task ends.
func TestTheWorkerRunsEachTaskWithItsPersonaRendered(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
read first || exit 1
md=no; [ -f "$CLAUDE_CONFIG_DIR/CLAUDE.md" ] && md=yes
flags=no; case " $* " in *" --allowedTools "*) flags=yes ;; esac
echo "$CLAUDE_CONFIG_DIR" > "$(dirname "$CLAUDE_CONFIG_DIR")/seen-dir"
printf '{"type":"result","subtype":"success","result":"md=%s flags=%s"}\n' "$md" "$flags"
`)
	root := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	addr := freeAddr(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, deadlineMargin: time.Second,
			PersonaDir: "../lib/persona/testdata/example", Harness: persona.ClaudeCode, ConfigRoot: root,
		})
	}()
	waitReady(t, addr)

	const task = "task-persona"
	d.offer(task, profileSubmission(t, task, "do the thing", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal")
	}
	if got := artifactText(foldTask(t, testClient(t, url), pwProfile, task), lib.ArtifactResult); got != "md=yes flags=yes" {
		t.Errorf("the harness saw %q, want the persona's CLAUDE.md and its tool flags", got)
	}
	seen, err := os.ReadFile(filepath.Join(root, "seen-dir"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(strings.TrimSpace(string(seen))); !os.IsNotExist(err) {
		t.Errorf("the task's config directory outlived the task: %v", err)
	}
}

// A persona that doesn't load stops the worker before it takes a task.
func TestABrokenPersonaStopsTheWorker(t *testing.T) {
	url := startServer(t)
	cfg := profileWorkerConfig(t, url, stub(t, "exit 0"))
	err := RunProfileWorker(context.Background(), cfg, ProfileWorkerConfig{
		Tier: pwTier, PersonaDir: t.TempDir(), Harness: persona.ClaudeCode, ConfigRoot: t.TempDir(),
	})
	if err == nil || !strings.Contains(err.Error(), "persona") {
		t.Errorf("a worker with an empty persona dir = %v, want refused at start", err)
	}
}
