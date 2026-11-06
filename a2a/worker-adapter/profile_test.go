package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
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

// readyListener binds the readiness port for a test worker and hands over
// the listener itself, so the port can't be taken between choosing it and
// listening on it.
func readyListener(t *testing.T) (net.Listener, string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	return ln, ln.Addr().String()
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
	ln, addr := readyListener(t)
	done := make(chan error, 1)
	go func() {
		done <- RunProfileWorker(ctx, cfg, ProfileWorkerConfig{Tier: pwTier, OneShot: oneShot, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second})
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

// Nothing of an abandoned task goes out while its harness drains, and a
// SIGTERM in that window is not a second terminal. The stub leaves a child in
// its own process group holding stdout, so the kill on abandon does not end
// the drain: the child writes an assistant line after the abandon, and the
// worker is told to stop while it is still holding the pipe.
func TestAnAbandonedTaskPublishesNothingWhileItDrains(t *testing.T) {
	url := startServer(t)
	mark := detachedChildMark(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-drain"}'
read first || exit 1
perl -e '$|=1; setpgrp(0,0); open(my $m, ">", $ENV{PGRP_MARK}) and close($m); sleep 1; print qq({"type":"assistant","message":{"content":[{"type":"text","text":"late-text"}]}}\n); sleep 4' &
sleep 30
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)

	const task = "task-abandon-drain"
	d.offer(task, profileSubmission(t, task, "long job", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	waitState(t, testClient(t, url), pwProfile, task, lib.StateWorking)
	before := len(replayEvents(t, url, pwProfile, task))
	waitDetached(t, mark)
	d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: task, Reason: "lease-expired - test"})

	// The late line lands about a second after the abandon; the SIGTERM
	// comes after it, while the child still holds stdout.
	time.Sleep(2 * time.Second)
	cancel()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("the worker did not stop after SIGTERM")
	}

	after := replayEvents(t, url, pwProfile, task)
	if len(after) != before {
		for _, e := range after[before:] {
			t.Errorf("published after the abandon: %s", e.Kind)
		}
	}
	if got := foldTask(t, testClient(t, url), pwProfile, task); got.Final {
		t.Errorf("the worker published a terminal (%s) for an abandoned task", got.State)
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
	ln, addr := readyListener(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{Tier: pwTier, ReadyAddr: addr, readyListener: ln, TaskTokenFile: tokenFile, deadlineMargin: time.Second})
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
	root := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
md=no; [ -f "$CLAUDE_CONFIG_DIR/CLAUDE.md" ] && md=yes
flags=no; case " $* " in *" --allowedTools "*) flags=yes ;; esac
echo "$CLAUDE_CONFIG_DIR" > %s/seen-dir
printf '{"type":"result","subtype":"success","result":"md=%%s flags=%%s"}\n' "$md" "$flags"
`, root))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	ln, addr := readyListener(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second,
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

// The harness can't rewrite what the worker rendered: its user settings,
// where hooks would run commands, and its instructions. It can still write
// its own state in the directory.
func TestTheHarnessCantRewriteItsRenderedConfig(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	root := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
c="$CLAUDE_CONFIG_DIR"
s=ok; ( printf '{"hooks":{}}' > "$c/settings.json" ) 2>/dev/null || s=refused
m=ok; ( printf 'x' >> "$c/CLAUDE.md" ) 2>/dev/null || m=refused
st=ok; mkdir -p "$c/projects" 2>/dev/null && touch "$c/projects/state" 2>/dev/null || st=refused
stat -f '%%Lp' "$c/settings.json" > %s/settings-mode 2>/dev/null || stat -c '%%a' "$c/settings.json" > %s/settings-mode
printf '{"type":"result","subtype":"success","result":"settings=%%s md=%%s state=%%s"}\n' "$s" "$m" "$st"
`, root, root))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	ln, addr := readyListener(t)
	configRoot := t.TempDir()
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second,
			PersonaDir: "../lib/persona/testdata/example", Harness: persona.ClaudeCode, ConfigRoot: configRoot,
		})
	}()
	waitReady(t, addr)
	const task = "task-locked"
	d.offer(task, profileSubmission(t, task, "try it", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal")
	}
	mode, err := os.ReadFile(filepath.Join(root, "settings-mode"))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(mode)); got != "444" {
		t.Errorf("settings.json mode %s, want 444", got)
	}
	got := artifactText(foldTask(t, testClient(t, url), pwProfile, task), lib.ArtifactResult)
	if !strings.HasSuffix(got, "state=ok") {
		t.Errorf("the harness couldn't write its own state: %q", got)
	}
	if os.Geteuid() == 0 {
		t.Skip("running as root, which ignores file modes; the modes are checked above")
	}
	if got != "settings=refused md=refused state=ok" {
		t.Errorf("the harness saw %q, want its settings and instructions refused and its state allowed", got)
	}
}

// offerUntilAccepted offers task until the pod accepts it, as the dispatcher
// does. A pod stays busy after a task's final renewal until its post-task
// workspace reset ends, and declines an offer meanwhile; any other decline
// fails the test.
func offerUntilAccepted(t *testing.T, d *fakeDispatcher, task string, sub *lib.Envelope) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for {
		d.offer(task, sub, 5)
		a := d.nextAccept(5 * time.Second)
		if a.TaskID != task {
			t.Fatalf("an answer for %q, want %q", a.TaskID, task)
		}
		if a.Accepted {
			return
		}
		if a.Reason != declineBusy {
			t.Fatalf("%s declined: %s", task, a.Reason)
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s: the pod stayed busy", task)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// Each task's harness gets its own config directory, never one an earlier
// task's model could have touched, and starts fresh rather than resuming.
func TestEachTaskGetsItsOwnConfigAndAFreshHarness(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	root := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
echo "$CLAUDE_CONFIG_DIR $*" >> %s/runs
echo '{"type":"result","subtype":"success","result":"ok"}'
`, root))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	ln, addr := readyListener(t)
	var slowOnce sync.Once
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second,
			PersonaDir: "../lib/persona/testdata/example", Harness: persona.ClaudeCode, ConfigRoot: t.TempDir(),
			// A slow post-task reset after task-one, as on a loaded runner:
			// the pod stays busy after its final renewal until the reset
			// ends. Only the first, so task-two's reset is not still running
			// when the test's temp directories are removed.
			ResetPaths: []string{t.TempDir()},
			resetHook: func(phase string) {
				if phase == "after-task" {
					slowOnce.Do(func() { time.Sleep(500 * time.Millisecond) })
				}
			},
		})
	}()
	waitReady(t, addr)
	for _, task := range []string{"task-one", "task-two"} {
		offerUntilAccepted(t, d, task, profileSubmission(t, task, "go", nil))
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
		if _, final := drainLeases(d, task, 20*time.Second); !final {
			t.Fatalf("%s: no final renewal", task)
		}
	}
	raw, err := os.ReadFile(filepath.Join(root, "runs"))
	if err != nil {
		t.Fatal(err)
	}
	runs := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(runs) != 2 {
		t.Fatalf("%d harness runs, want 2: %q", len(runs), runs)
	}
	dirs := []string{strings.Fields(runs[0])[0], strings.Fields(runs[1])[0]}
	if dirs[0] == dirs[1] {
		t.Errorf("both tasks ran with config %s", dirs[0])
	}
	for _, r := range runs {
		if strings.Contains(r, "--resume") {
			t.Errorf("a harness resumed: %s", r)
		}
	}
}

// A confirm that arrives too late for the task to finish inside its deadline
// runs nothing: the harness would be killed at once, after spending a turn.
func TestALateConfirmRunsNothing(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, stub(t, `read first; echo '{"type":"result","subtype":"success","result":"ran"}'`)), false)

	const task = "task-late"
	raw, _ := json.Marshal(profileSubmission(t, task, "too late", nil))
	d.send(lib.DispatchMessage{
		Type: lib.DispatchOffer, TaskID: task, Kind: lib.SubmitKindTop, Envelope: raw,
		// In time at the offer (the test's margin is 1s), not by the confirm.
		Deadline: time.Now().Add(1500 * time.Millisecond), LeaseSeconds: 3, RenewSeconds: 1, AcceptSeconds: 5,
	})
	if a := d.nextAccept(5 * time.Second); !a.Accepted {
		t.Fatalf("the offer was declined: %+v", a)
	}
	time.Sleep(time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 2*time.Second); final {
		t.Error("a final renewal for a task that shouldn't have run")
	}
	if evs := replayEvents(t, url, pwProfile, task); len(evs) != 0 {
		t.Errorf("%d events for a task confirmed too late to finish", len(evs))
	}
}

// A dispatched task cut short by its worker stopping says why, and that it
// can be resubmitted: a rollout or a scale-down the requester can't see.
func TestAnInterruptedTaskSaysItCanBeResubmitted(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	ctx, cancel := context.WithCancel(context.Background())
	done := startProfileWorker(t, ctx, profileWorkerConfig(t, url, stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-evict"}'
read first || exit 1
sleep 30
`)), false)
	const task = "task-evicted"
	d.offer(task, profileSubmission(t, task, "long", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	waitState(t, testClient(t, url), pwProfile, task, lib.StateWorking)
	cancel() // SIGTERM
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatal("the worker didn't stop")
	}
	events := replayEvents(t, url, pwProfile, task)
	last := statusOf(t, events[len(events)-1])
	if !last.Final || last.Status.State != lib.StateFailed || last.Status.Message == nil ||
		!strings.Contains(last.Status.Message.Parts[0].Text, "can be resubmitted") {
		t.Errorf("the interrupted task's terminal %+v; want failed, saying it can be resubmitted", last)
	}
}

// What the render leaves out of a persona is in the pod's log: the render
// reports it in Warnings and logs nothing itself.
func TestPersonaRenderWarningsAreLogged(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, nil))
	logPersonaWarnings(log, &persona.Output{Warnings: []string{`mcp server "fleet" (via: broker) dropped: no relay target`}})
	if !strings.Contains(buf.String(), `fleet`) || !strings.Contains(buf.String(), "level=WARN") {
		t.Errorf("the render's warning is not in the log: %q", buf.String())
	}
	buf.Reset()
	logPersonaWarnings(log, nil)
	if buf.Len() != 0 {
		t.Errorf("logged %q with no persona", buf.String())
	}
}

// Two tasks on one warm worker share its consumer names while confirms carry
// no task credential (the stem is the pod's), and the second task's
// consumers start fresh: an input left on the first task after it ended
// never reaches the second. Until per-task credentials, this is the
// isolation between tasks on a warm pod.
func TestTheSecondTaskOnAWarmWorkerSeesNothingOfTheFirst(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-two"}'
read first || exit 1
all="$first"
while read -t 2 line; do all="$all $line"; done
saw=$(printf '%s' "$all" | grep -oE 'ALPHAWORD|BETAWORD|STALEWORD' | tr '\n' ' ')
printf '{"type":"result","subtype":"success","result":"saw: %s"}\n' "$saw"
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)
	c := testClient(t, url)

	origins := map[string]*lib.Envelope{}
	run := func(task, text string) *lib.Task {
		t.Helper()
		origins[task] = profileSubmission(t, task, text, nil)
		d.offer(task, origins[task], 5)
		if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
			t.Fatalf("offer of %s = %+v, want an accept", task, a)
		}
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
		waitState(t, c, pwProfile, task, lib.StateCompleted)
		drainLeases(d, task, time.Second)
		return foldTask(t, c, pwProfile, task)
	}

	first := run("task-first", "ALPHAWORD")
	if got := artifactText(first, lib.ArtifactResult); !strings.Contains(got, "ALPHAWORD") {
		t.Fatalf("the first task's result %q doesn't carry its own prompt", got)
	}
	// An input on the first task after it ended: undelivered, and on a
	// consumer name the second task reuses.
	stalePayload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: "STALEWORD"}},
		MessageID: "msg-stale-1", TaskID: "task-first", ContextID: origins["task-first"].ContextID,
	})
	if err != nil {
		t.Fatal(err)
	}
	stale, err := lib.NewFollowUpEnvelope(origins["task-first"], gatewayParty, stalePayload, lib.WithTo(lib.Party{Session: pwProfile}))
	if err != nil {
		t.Fatal(err)
	}
	pubCtx, pubCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer pubCancel()
	if err := c.Publish(pubCtx, lib.TaskInSubject(pwProfile, "task-first"), stale); err != nil {
		t.Fatalf("publish the stale input: %v", err)
	}

	second := run("task-second", "BETAWORD")
	got := artifactText(second, lib.ArtifactResult)
	if !strings.Contains(got, "BETAWORD") {
		t.Errorf("the second task's result %q doesn't carry its own prompt", got)
	}
	if strings.Contains(got, "STALEWORD") || strings.Contains(got, "ALPHAWORD") {
		t.Errorf("the second task saw the first task's input: %q", got)
	}
}

// After an abandon, the next task on the pod starts whatever the abandoned
// task left of its consumers: it gets its own input, under the same names,
// even with a consumer of the old task still standing under one of them.
func TestATaskAfterAnAbandonStartsCleanly(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-after-abandon"}'
read first || exit 1
case "$first" in
  *HANGWORD*) sleep 30 ;;
  *NEXTWORD*) sleep 3 ;;
esac
saw=$(printf '%s' "$first" | grep -oE 'HANGWORD|NEXTWORD' | tr '\n' ' ')
printf '{"type":"result","subtype":"success","result":"saw: %s"}\n' "$saw"
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)
	c := testClient(t, url)

	d.offer("task-hang", profileSubmission(t, "task-hang", "HANGWORD", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-hang"})
	waitState(t, c, pwProfile, "task-hang", lib.StateWorking)
	d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: "task-hang", Reason: "stop - test"})
	drainLeases(d, "task-hang", 2*time.Second)

	// A consumer the abandoned task left under the pod's names, as a crash
	// or a failed delete would, on the hung task's subject. Its start
	// sequence matches the one the next task asks for, so JetStream would
	// accept that task's create as an update in place: only the filter
	// differs. The check below is what tells a fresh consumer from the
	// leftover updated, by creation time.
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	jsCtx, jsCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer jsCancel()
	inName := lib.SessionConsumerName(pwPod, lib.SessionConsumerIn)
	left, err := js.CreateOrUpdateConsumer(jsCtx, lib.TasksStream, jetstream.ConsumerConfig{
		Name: inName, FilterSubject: lib.TaskInSubject(pwProfile, "task-hang"),
		DeliverPolicy: jetstream.DeliverByStartSequencePolicy, OptStartSeq: 1,
		AckPolicy: jetstream.AckNonePolicy, MemoryStorage: true, Replicas: 1,
	})
	if err != nil {
		t.Fatalf("leave a consumer behind: %v", err)
	}
	leftCreated := left.CachedInfo().Created

	d.offer("task-next", profileSubmission(t, "task-next", "NEXTWORD", nil), 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-next" || !a.Accepted {
		t.Fatalf("an offer after the abandon = %+v, want an accept", a)
	}
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-next"})
	// While it runs, its input consumer is a new one, not the leftover
	// updated in place: an update keeps the old consumer's creation time
	// (and everything else about it the update can't change).
	var inInfo *jetstream.ConsumerInfo
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
		cons, err := js.Consumer(jsCtx, lib.TasksStream, inName)
		if err != nil {
			continue
		}
		if info := cons.CachedInfo(); info.Config.FilterSubject == lib.TaskInSubject(pwProfile, "task-next") {
			inInfo = info
			break
		}
	}
	if inInfo == nil {
		t.Fatalf("the task after the abandon never had an input consumer on its own subject under %s", inName)
	}
	if !inInfo.Created.After(leftCreated) {
		t.Errorf("the input consumer was the leftover updated in place (created %v, the leftover %v), not a fresh one", inInfo.Created, leftCreated)
	}
	waitState(t, c, pwProfile, "task-next", lib.StateCompleted)
	if got := artifactText(foldTask(t, c, pwProfile, "task-next"), lib.ArtifactResult); !strings.Contains(got, "NEXTWORD") {
		t.Errorf("the task after the abandon answered %q", got)
	}
}

// An abandon that's already waiting when the task starts ends it before it
// is marked working: nothing of the task goes out after the dispatcher's
// terminal. Run at the adapter, so the abandon is in place before setup
// rather than racing it.
func TestAnAbandonDuringSetupPublishesNothing(t *testing.T) {
	url := startServer(t)
	harness := stub(t, `
read first || exit 1
echo '{"type":"result","subtype":"success","result":"should never run"}'
`)
	cfg := profileWorkerConfig(t, url, harness)
	const task = "task-abandon-setup"
	origin := profileSubmission(t, task, "never started", nil)
	cfg.TaskID = task
	b, closeBus, err := dialBus(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer closeBus()
	abandon := make(chan string, 1)
	abandon <- "stop - test"
	a := newTaskAdapter(cfg, b)
	a.dispatched = true
	a.abandon = abandon

	out := a.execute(context.Background(), origin, 0, turnPlan{})
	if !out.res.Abandoned {
		t.Errorf("execute = %+v, err %v; want abandoned", out.res, out.err)
	}
	for _, e := range replayEvents(t, url, pwProfile, task) {
		if e.Kind == lib.KindStatusUpdate {
			var u lib.StatusUpdate
			if json.Unmarshal(e.Payload, &u) == nil {
				t.Errorf("published %s (final=%v) for a task abandoned before it started", u.Status.State, u.Final)
			}
		}
	}
}

// An abandon the worker has received and a SIGTERM right behind it: however
// the worker's selects order them, the task gets no terminal and no final
// renewal. The stub's child holds stdout past the kill, so the harness is
// still draining when the SIGTERM lands. (An abandon still on the wire when
// the SIGTERM lands can't be seen by any worker. The dispatcher sends the
// abandon before it writes its terminal, so either terminal can land first;
// for a running task's abandons today both are failed.)
func TestAnAbandonBesideASigtermPublishesNoTerminal(t *testing.T) {
	for i := 0; i < 3; i++ {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			url := startServer(t)
			mark := detachedChildMark(t)
			d := newFakeDispatcher(t, url)
			harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-both"}'
read first || exit 1
perl -e 'setpgrp(0,0); open(my $m, ">", $ENV{PGRP_MARK}) and close($m); sleep 4' &
sleep 30
`)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)
			task := fmt.Sprintf("task-both-%d", i)
			d.offer(task, profileSubmission(t, task, "long job", nil), 5)
			d.nextAccept(5 * time.Second)
			d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
			waitState(t, testClient(t, url), pwProfile, task, lib.StateWorking)
			// The abandon, then the SIGTERM as soon as it has arrived.
			waitDetached(t, mark)
			d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: task, Reason: "stop - test"})
			time.Sleep(50 * time.Millisecond)
			cancel()
			select {
			case <-done:
			case <-time.After(15 * time.Second):
				t.Fatal("the worker did not stop")
			}
			if _, final := drainLeases(d, task, time.Second); final {
				t.Error("a final renewal for an abandoned task")
			}
			if got := foldTask(t, testClient(t, url), pwProfile, task); got.Final {
				t.Errorf("the worker published a terminal (%s) for an abandoned task", got.State)
			}
		})
	}
}

// A task that ended with an error got no terminal (the publish of working,
// or of the terminal itself, failed), so it gets no final renewal either:
// the lease settles it.
func TestNoFinalRenewalWithoutATerminal(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	cfg := profileWorkerConfig(t, url, nil)
	if cfg.Logger == nil {
		cfg.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	b, closeBus, err := dialBus(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer closeBus()
	w := &profileWorker{cfg: cfg, pw: ProfileWorkerConfig{Tier: pwTier}, b: b}
	const task = "task-no-terminal"
	stop := func() {}
	renewDone := make(chan struct{})
	close(renewDone)
	h := &held{offer: lib.DispatchMessage{TaskID: task}, stopRenew: stop, renewDone: renewDone}

	w.finish(h, turnOutcome{res: Result{State: lib.StateFailed}, err: errors.New("publish working: connection closed")})
	if _, final := drainLeases(d, task, time.Second); final {
		t.Error("a final renewal for a task whose terminal didn't go out")
	}
	// And one that did end cleanly still gets it, so the check above isn't
	// passing on a worker that never sends one.
	w.finish(h, turnOutcome{res: Result{State: lib.StateCompleted}})
	if _, final := drainLeases(d, task, 2*time.Second); !final {
		t.Error("no final renewal for a task whose terminal went out")
	}
}

// finalize is the one door every terminal goes through, so it's where an
// abandon is honoured last: with one waiting, it publishes nothing and says
// so.
func TestFinalizeWritesNoTerminalForAnAbandonedTask(t *testing.T) {
	abandon := make(chan string, 1)
	abandon <- "stop - test"
	a := &adapter{abandon: abandon}
	// No execution is wired: a finalize that tried to publish would panic.
	if err := a.finalize(lib.StateFailed, "reason: worker-evicted", ""); !errors.Is(err, errAbandoned) {
		t.Errorf("finalize of an abandoned task = %v, want errAbandoned", err)
	}
	if !a.abandoned() || a.abandonReason != "stop - test" {
		t.Errorf("the abandon wasn't kept: seen=%v reason=%q", a.abandonSeen, a.abandonReason)
	}
}

// On a SIGTERM, the worker passes the held task an abandon that's already
// queued beside it, and only that task's: the loop's select may have taken
// the SIGTERM first.
func TestAQueuedAbandonReachesTheTaskOnEviction(t *testing.T) {
	w := &profileWorker{msgs: make(chan *nats.Msg, 4)}
	cur := &held{offer: lib.DispatchMessage{TaskID: "task-held"}, abandon: make(chan string, 1)}
	for _, m := range []lib.DispatchMessage{
		{Type: lib.DispatchAbandon, TaskID: "task-other", Reason: "not this one"},
		{Type: lib.DispatchConfirm, TaskID: "task-held"},
		{Type: lib.DispatchAbandon, TaskID: "task-held", Reason: "stop - test"},
	} {
		data, err := json.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		w.msgs <- &nats.Msg{Data: data}
	}
	w.forwardPendingAbandon(cur)
	select {
	case got := <-cur.abandon:
		if got != "stop - test" {
			t.Errorf("the held task got the abandon %q", got)
		}
	default:
		t.Error("the queued abandon didn't reach the held task")
	}
}

// runWorkerWithReset starts a profile worker whose ResetPaths are given, with
// its log captured, and waits for it to be Ready.
func runWorkerWithReset(t *testing.T, ctx context.Context, url string, harness []string, reset []string, logBuf *syncBuffer) string {
	t.Helper()
	addr, _ := startWorkerWithReset(t, ctx, url, harness, ProfileWorkerConfig{ResetPaths: reset}, logBuf, true)
	return addr
}

// startWorkerWithReset starts a profile worker with the given ResetPaths (and
// resetTimeout), its log captured. With waitReady it returns once /readyz
// answers 200; either way it returns the worker's result channel.
func startWorkerWithReset(t *testing.T, ctx context.Context, url string, harness []string, pw ProfileWorkerConfig, logBuf *syncBuffer, waitReady bool) (string, <-chan error) {
	t.Helper()
	cfg := profileWorkerConfig(t, url, harness)
	cfg.Logger = slog.New(slog.NewTextHandler(logBuf, nil))
	ln, addr := readyListener(t)
	pw.Tier, pw.ReadyAddr, pw.readyListener, pw.deadlineMargin = pwTier, addr, ln, time.Second
	done := make(chan error, 1)
	go func() { done <- RunProfileWorker(ctx, cfg, pw) }()
	if !waitReady {
		return addr, done
	}
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(50 * time.Millisecond) {
		if resp, err := http.Get("http://" + addr + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return addr, done
			}
		}
	}
	t.Fatal("the worker never became ready")
	return "", done
}

// syncBuffer is a bytes.Buffer safe for the logger's goroutines and the
// test's reads.
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// What one task leaves in the reset paths (its working directory, $HOME,
// /tmp and the rest) is gone before the next task's harness starts, on the
// same warm pod, and the log says the reset ran.
func TestATasksFilesAreGoneBeforeTheNextTask(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	work, home := t.TempDir(), t.TempDir()
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
case "$first" in
  *WRITEWORD*)
    mkdir -p %[1]s/sub && echo left > %[1]s/sub/notes.md
    echo left > %[2]s/.cache-file
    chmod 0500 %[1]s/sub
    echo '{"type":"result","subtype":"success","result":"wrote"}' ;;
  *)
    found=$(find %[1]s %[2]s -mindepth 1 | tr '\n' ' ')
    printf '{"type":"result","subtype":"success","result":"found: %%s"}\n' "$found" ;;
esac
`, work, home))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logBuf syncBuffer
	runWorkerWithReset(t, ctx, url, harness, []string{work, home}, &logBuf)
	c := testClient(t, url)
	run := func(task, text string) string {
		t.Helper()
		d.offer(task, profileSubmission(t, task, text, nil), 5)
		if a := d.nextAccept(5 * time.Second); a.TaskID != task || !a.Accepted {
			t.Fatalf("offer of %s = %+v, want an accept", task, a)
		}
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
		waitState(t, c, pwProfile, task, lib.StateCompleted)
		drainLeases(d, task, time.Second)
		return artifactText(foldTask(t, c, pwProfile, task), lib.ArtifactResult)
	}

	if got := run("task-writes", "WRITEWORD"); got != "wrote" {
		t.Fatalf("the first task answered %q", got)
	}
	got := run("task-reads", "look around")
	if got != "found: " {
		t.Errorf("the second task found the first task's files: %q", got)
	}
	for _, dir := range []string{work, home} {
		if _, err := os.Stat(dir); err != nil {
			t.Errorf("the reset removed the mount %s itself: %v", dir, err)
		}
	}
	if !strings.Contains(logBuf.String(), "workspace reset") || !strings.Contains(logBuf.String(), "paths=2") {
		t.Errorf("the log doesn't show the reset: %s", logBuf.String())
	}
}

// A reset that fails ends the worker, at startup before it ever goes Ready,
// and after a task once that task's terminal is out, so the kubelet restarts
// it rather than leaving a pod that's alive and useless. A startup reset that
// runs past its bound ends it the same way.
func TestAFailedResetEndsTheWorker(t *testing.T) {
	harness := stub(t, `
read first || exit 1
case "$first" in *RMWORD*) rm -rf "$RESETDIR" ;; esac
echo '{"type":"result","subtype":"success","result":"done"}'
`)
	waitEnd := func(t *testing.T, done <-chan error, want string) {
		t.Helper()
		select {
		case err := <-done:
			if err == nil || !strings.Contains(err.Error(), want) {
				t.Errorf("the worker ended with %v, want an error naming %q", err, want)
			}
		case <-time.After(15 * time.Second):
			t.Fatal("the worker didn't end")
		}
	}
	t.Run("at startup", func(t *testing.T) {
		url := startServer(t)
		var logBuf syncBuffer
		_, done := startWorkerWithReset(t, context.Background(), url, harness, ProfileWorkerConfig{ResetPaths: []string{filepath.Join(t.TempDir(), "missing")}}, &logBuf, false)
		waitEnd(t, done, "workspace reset (startup)")
	})
	t.Run("past its bound", func(t *testing.T) {
		url := startServer(t)
		dir := t.TempDir()
		for i := 0; i < 200; i++ {
			if err := os.WriteFile(filepath.Join(dir, fmt.Sprint(i)), []byte("x"), 0o600); err != nil {
				t.Fatal(err)
			}
		}
		var logBuf syncBuffer
		_, done := startWorkerWithReset(t, context.Background(), url, harness, ProfileWorkerConfig{ResetPaths: []string{dir}, resetTimeout: time.Nanosecond}, &logBuf, false)
		waitEnd(t, done, "took longer")
	})
	t.Run("after a task", func(t *testing.T) {
		url := startServer(t)
		d := newFakeDispatcher(t, url)
		dir := filepath.Join(t.TempDir(), "work")
		if err := os.Mkdir(dir, 0o700); err != nil {
			t.Fatal(err)
		}
		t.Setenv("RESETDIR", dir)
		var logBuf syncBuffer
		_, done := startWorkerWithReset(t, context.Background(), url, harness, ProfileWorkerConfig{ResetPaths: []string{dir}}, &logBuf, true)
		d.offer("task-rm", profileSubmission(t, "task-rm", "RMWORD", nil), 5)
		d.nextAccept(5 * time.Second)
		d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-rm"})
		waitEnd(t, done, "workspace reset (after-task)")
		if got := foldTask(t, testClient(t, url), pwProfile, "task-rm"); !got.Final || got.State != lib.StateCompleted {
			t.Errorf("the task before the failed reset = %s (final %v), want its own completed terminal", got.State, got.Final)
		}
	})
}

// A restarted worker starts on what the last process left in its emptyDirs,
// so the startup reset empties them before the first offer.
func TestTheStartupResetClearsWhatALastProcessLeft(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "left-by-the-last-process"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
found=$(find %s -mindepth 1 | tr '\n' ' ')
printf '{"type":"result","subtype":"success","result":"found: %%s"}\n' "$found"
`, dir))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logBuf syncBuffer
	startWorkerWithReset(t, ctx, url, harness, ProfileWorkerConfig{ResetPaths: []string{dir}}, &logBuf, true)
	c := testClient(t, url)
	d.offer("task-first", profileSubmission(t, "task-first", "look", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-first"})
	waitState(t, c, pwProfile, "task-first", lib.StateCompleted)
	if got := artifactText(foldTask(t, c, pwProfile, "task-first"), lib.ArtifactResult); got != "found: " {
		t.Errorf("the first task after a restart found %q", got)
	}
	if !strings.Contains(logBuf.String(), "phase=startup") {
		t.Errorf("the log doesn't show the startup reset: %s", logBuf.String())
	}
}

// A pod that can't run a harness ends after the task it couldn't start,
// rather than staying Ready and failing every offer the same way.
func TestAPodThatCantRunAHarnessEnds(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	var logBuf syncBuffer
	_, done := startWorkerWithReset(t, context.Background(), url, []string{filepath.Join(t.TempDir(), "no-such-harness")}, ProfileWorkerConfig{}, &logBuf, true)
	d.offer("task-cant", profileSubmission(t, "task-cant", "anything", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-cant"})
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "can't run a harness") {
			t.Errorf("the worker ended with %v, want the broken-pod error", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("a pod that can't run a harness kept running")
	}
	// It ends after the task's terminal and final renewal are out, so the
	// dispatcher doesn't wait for the lease to lapse.
	if got := foldTask(t, testClient(t, url), pwProfile, "task-cant"); !got.Final || got.State != lib.StateFailed {
		t.Errorf("the task the broken pod couldn't start = %s (final %v), want a failed terminal", got.State, got.Final)
	}
	if _, final := drainLeases(d, "task-cant", 2*time.Second); !final {
		t.Error("no final renewal for the task the broken pod couldn't start")
	}
}

// A config root the worker can't write ends the pod after the task it
// couldn't set up, like a harness it can't start: every task would fail
// the same way.
func TestABrokenConfigRootEndsTheWorker(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
read first || exit 1
echo '{"type":"result","subtype":"success","result":"done"}'
`)
	notADir := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(notADir, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	var logBuf syncBuffer
	_, done := startWorkerWithReset(t, context.Background(), url, harness, ProfileWorkerConfig{
		PersonaDir: "../lib/persona/testdata/example", Harness: persona.ClaudeCode, ConfigRoot: notADir,
	}, &logBuf, true)
	d.offer("task-noconf", profileSubmission(t, "task-noconf", "anything", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-noconf"})
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "can't run a harness") {
			t.Errorf("the worker ended with %v, want the broken-pod error", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("a pod with a broken config root kept running")
	}
	// Its task gets a failed terminal and a final renewal before the pod
	// ends, so the dispatcher doesn't wait for the lease to lapse.
	got := foldTask(t, testClient(t, url), pwProfile, "task-noconf")
	if !got.Final || got.State != lib.StateFailed {
		t.Errorf("the task the pod couldn't set up = %s (final %v), want a failed terminal", got.State, got.Final)
	}
	if got.FinalMessage == nil || len(got.FinalMessage.Parts) == 0 || !strings.HasPrefix(got.FinalMessage.Parts[0].Text, "reason: harness-config-failed ") {
		t.Errorf("the terminal's reason = %+v, want it to lead with harness-config-failed", got.FinalMessage)
	}
	if _, final := drainLeases(d, "task-noconf", 2*time.Second); !final {
		t.Error("no final renewal for the task the pod couldn't set up")
	}
}

// The after-task reset runs beside the worker loop: an offer that arrives
// while it runs is declined as busy at once, and once it's done the next
// offer is accepted.
func TestAnOfferDuringTheAfterTaskResetIsAnsweredAtOnce(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
read first || exit 1
echo '{"type":"result","subtype":"success","result":"done"}'
`)
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	var logBuf syncBuffer
	startWorkerWithReset(t, context.Background(), url, harness, ProfileWorkerConfig{
		ResetPaths: []string{t.TempDir()},
		resetHook: func(phase string) {
			if phase == "after-task" {
				once.Do(func() { close(started) })
				<-release
			}
		},
	}, &logBuf, true)
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})

	d.offer("task-first", profileSubmission(t, "task-first", "first", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-first"})
	select {
	case <-started:
	case <-time.After(10 * time.Second):
		t.Fatal("the after-task reset never started")
	}
	d.offer("task-during", profileSubmission(t, "task-during", "during", nil), 5)
	if a := d.nextAccept(time.Second); a.TaskID != "task-during" || a.Accepted || a.Reason != declineBusy {
		t.Errorf("an offer during the reset = %+v, want a prompt busy decline", a)
	}
	close(release)
	time.Sleep(200 * time.Millisecond)
	d.offer("task-after", profileSubmission(t, "task-after", "after", nil), 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-after" || !a.Accepted {
		t.Errorf("an offer after the reset = %+v, want an accept", a)
	}
}

// Liveness fails once the bus connection is closed for good, so the kubelet
// restarts the worker instead of keeping a pod that can't take a task.
func TestHealthzFailsOnceTheBusConnectionIsClosed(t *testing.T) {
	url := startServer(t)
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	w := &profileWorker{cfg: Config{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}, b: &busConns{nc: nc}}
	ln, addr := readyListener(t)
	stop, err := w.serveReadiness(addr, ln)
	if err != nil {
		t.Fatal(err)
	}
	defer stop()
	get := func() int {
		resp, err := http.Get("http://" + addr + "/healthz")
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := get(); code != http.StatusOK {
		t.Fatalf("/healthz with the connection open = %d", code)
	}
	nc.Close()
	if code := get(); code != http.StatusServiceUnavailable {
		t.Errorf("/healthz with the connection closed = %d, want 503", code)
	}
}

// While an abandoned task drains, the worker still answers: an offer that
// arrives meanwhile is declined as busy at once, not left waiting out the
// drain, and once the task is released the next offer is accepted.
func TestAnOfferDuringAnAbandonedDrainIsAnsweredAtOnce(t *testing.T) {
	url := startServer(t)
	mark := detachedChildMark(t)
	d := newFakeDispatcher(t, url)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"pw-drain-offer"}'
read first || exit 1
case "$first" in
  *SLOWDRAIN*) perl -e 'setpgrp(0,0); open(my $m, ">", $ENV{PGRP_MARK}) and close($m); sleep 4' & sleep 30 ;;
esac
echo '{"type":"result","subtype":"success","result":"done"}'
`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	startProfileWorker(t, ctx, profileWorkerConfig(t, url, harness), false)

	d.offer("task-slow", profileSubmission(t, "task-slow", "SLOWDRAIN", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: "task-slow"})
	waitState(t, testClient(t, url), pwProfile, "task-slow", lib.StateWorking)
	waitDetached(t, mark)
	d.send(lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: "task-slow", Reason: "lease-expired - test"})
	time.Sleep(100 * time.Millisecond)

	d.offer("task-during", profileSubmission(t, "task-during", "during", nil), 5)
	if a := d.nextAccept(time.Second); a.TaskID != "task-during" || a.Accepted || a.Reason != declineBusy {
		t.Errorf("an offer during the drain = %+v, want a prompt busy decline", a)
	}
	// After the drain (the child holds stdout for about 4s), the pod is free.
	time.Sleep(5 * time.Second)
	d.offer("task-after", profileSubmission(t, "task-after", "after", nil), 5)
	if a := d.nextAccept(5 * time.Second); a.TaskID != "task-after" || !a.Accepted {
		t.Errorf("an offer after the drain = %+v, want an accept", a)
	}
}

// detachedChildMark is the file the drain tests' perl child writes once it
// has its own process group, and waitDetached waits for it, so an abandon
// is never sent before the child is out of the harness's group.
func detachedChildMark(t *testing.T) string {
	t.Helper()
	mark := filepath.Join(t.TempDir(), "pgrp")
	t.Setenv("PGRP_MARK", mark)
	return mark
}

func waitDetached(t *testing.T, mark string) {
	t.Helper()
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if _, err := os.Stat(mark); err == nil {
			return
		}
	}
	t.Fatal("the harness's child never left its process group")
}
