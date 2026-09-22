package lib

import (
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The consumer TasksGet creates on TASKS must be gone within
// EphemeralConsumerInactiveThreshold of the call returning, and TasksGet must
// not try to delete it by hand (#1739). Both are measured on a real server
// against the rendered bridge grant, because the second one only matters
// under that grant: the delete would be a $JS.API.CONSUMER.DELETE.TASKS.<name>
// publish, and this principal does not hold the subject.
const (
	// thresholdReapWithin is how long the server gets to reap a consumer by
	// its inactive threshold once the client is gone: the threshold itself
	// plus the server's scan interval and slack.
	thresholdReapWithin = 3 * EphemeralConsumerInactiveThreshold
	// replayReturnWithin is the budget for one tasks/get. It is well inside
	// the threshold, so a synchronous delete refused without a reply -- the
	// shape a re-added explicit delete would take under this grant -- shows
	// up here as a slow return rather than passing unnoticed.
	replayReturnWithin = 2 * time.Second
	// logWithin is how long an async error gets to travel from the server to
	// the connection's error handler.
	logWithin = 2 * time.Second
	// replayBurst is how many tasks/get calls the burst case makes.
	replayBurst = 20

	// The TASKS stream as the provision script creates it, for the flags
	// that matter here: allow_direct (the replay horizon is DIRECT.GET, the
	// subject the grant names, not STREAM.MSG.GET), limits retention with
	// discard old, and the 64-consumer floor the leak counts against.
	replayStreamSubjects     = "a2a.tasks.>"
	replayStreamMaxAge       = 72 * time.Hour
	replayStreamMaxConsumers = 64
	// permissionsViolation is how nats.go spells a refused publish on the
	// connection's async error handler. Matching the prefix rather than a
	// whole line keeps the count subject-agnostic, which is the point.
	permissionsViolation = "Permissions Violation"

	// replayAdminTimeout bounds each admin-side call (provision, list). It is
	// per call, not per test: the poll loops below run longer than one of them.
	replayAdminTimeout = 5 * time.Second

	replayAdminUser   = "admin"
	replayReaderUser  = "bridge-shaped"
	replayPassword    = "pw"
	replayControlSubj = "$JS.API.STREAM.DELETE.TASKS"
)

// replayGrant is the reader grant in the shape the operator renders for the
// `bridge` principal: the JetStream API subjects TasksGet emits on TASKS,
// enumerated per stream and verb, plus the ack, flow-control and inbox
// subjects beside them in the identity. CONSUMER.DELETE is deliberately not
// in the list -- that is the grant TasksGet has to work within, and the whole
// point of the tests below.
func replayGrant(user string) *natsserver.Permissions {
	return &natsserver.Permissions{
		Publish: &natsserver.SubjectPermission{Allow: []string{
			"$JS.API.STREAM.INFO.TASKS",
			"$JS.API.CONSUMER.CREATE.TASKS.>",
			"$JS.API.CONSUMER.MSG.NEXT.TASKS.*",
			"$JS.API.DIRECT.GET.TASKS.>",
			"$JS.ACK.TASKS.>",
			"$JS.FC.>",
			"_INBOX." + user + ".>",
		}},
		Subscribe: &natsserver.SubjectPermission{Allow: []string{"_INBOX." + user + ".>"}},
	}
}

// startPermissionedServer runs a JetStream server whose reader carries the
// grant above. admin is unrestricted: it provisions the stream, publishes the
// task and reads the consumer list back, the way seed and web do on an
// install.
func startPermissionedServer(t *testing.T) *natsserver.Server {
	t.Helper()
	s := runJetStreamServer(t, -1, t.TempDir(), func(o *natsserver.Options) {
		o.Users = []*natsserver.User{
			{Username: replayAdminUser, Password: replayPassword},
			{Username: replayReaderUser, Password: replayPassword, Permissions: replayGrant(replayReaderUser)},
		}
	})
	t.Cleanup(s.Shutdown)
	return s
}

// adminJetStream connects as the unrestricted admin, the way seed and web
// reach the bus on an install, for provisioning and observation. Open it once
// per test and pass the handle down: the consumer list below is read from a
// poll loop at 20ms, so a connection per call would open hundreds of them and
// hold every one open until cleanup.
func adminJetStream(t *testing.T, url string) jetstream.JetStream {
	t.Helper()
	nc, err := nats.Connect(url, nats.UserInfo(replayAdminUser, replayPassword))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	return js
}

// provisionTasksStreamAsProvisioned creates TASKS with the provision script's
// flags (the replayStream* constants above). It is not testutil's
// provisionTasksStream because that one leaves allow_direct off, which
// routes the horizon read through STREAM.MSG.GET -- a subject no rendered
// grant carries.
func provisionTasksStreamAsProvisioned(t *testing.T, js jetstream.JetStream) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), replayAdminTimeout)
	defer cancel()
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:         TasksStream,
		Subjects:     []string{replayStreamSubjects},
		Retention:    jetstream.LimitsPolicy,
		Discard:      jetstream.DiscardOld,
		MaxAge:       replayStreamMaxAge,
		MaxConsumers: replayStreamMaxConsumers,
		AllowDirect:  true,
	}); err != nil {
		t.Fatalf("create TASKS stream: %v", err)
	}
}

// tasksConsumers lists the consumers on TASKS as admin. The InactiveThreshold
// it reports is the server's, read back off the consumer -- not the value the
// caller put in the config struct.
func tasksConsumers(t *testing.T, js jetstream.JetStream) []*jetstream.ConsumerInfo {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), replayAdminTimeout)
	defer cancel()
	st, err := js.Stream(ctx, TasksStream)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	var out []*jetstream.ConsumerInfo
	lister := st.ListConsumers(ctx)
	for info := range lister.Info() {
		out = append(out, info)
	}
	if err := lister.Err(); err != nil {
		t.Fatalf("list consumers: %v", err)
	}
	return out
}

func describeConsumers(infos []*jetstream.ConsumerInfo) string {
	var b strings.Builder
	for _, info := range infos {
		b.WriteString(info.Name)
		b.WriteString(" inactive_threshold=")
		b.WriteString(info.Config.InactiveThreshold.String())
		b.WriteString("; ")
	}
	return b.String()
}

// replayReader connects a reader under the bridge-shaped grant.
func replayReader(t *testing.T, url, name string, log *slog.Logger) *Client {
	t.Helper()
	opts := []ClientOption{WithName(name), WithUserPassword(replayReaderUser, replayPassword)}
	if log != nil {
		opts = append(opts, WithLogger(log))
	}
	c, err := Connect(testCtx(t), url, opts...)
	if err != nil {
		t.Fatalf("Connect as %s: %v", replayReaderUser, err)
	}
	t.Cleanup(c.Close)
	return c
}

// The ordered consumer TasksGet creates carries the five-second inactive
// threshold on the server, not nats.go's five-minute ordered default, and the
// server reaps it on that clock once the replay stops pulling. This is the
// whole of the cleanup: nothing deletes the consumer by name, so if the
// threshold does not reach the server the slot is held for five minutes
// (#1739). The threshold is read back off the consumer as the server holds
// it, not off the config struct TasksGet passed in.
func TestTasksGet_ReplayConsumerCarriesTheInactiveThreshold(t *testing.T) {
	s := startPermissionedServer(t)
	url := clientURL(s)
	admin := adminJetStream(t, url)
	provisionTasksStreamAsProvisioned(t, admin)
	const taskID = "task-replay-threshold"
	// The constant is the entire cleanup, so it has to be short on its own
	// terms; asserting only that the server agrees with it would pass with
	// the five minutes this change exists to get rid of.
	if EphemeralConsumerInactiveThreshold >= time.Minute {
		t.Fatalf("EphemeralConsumerInactiveThreshold = %s: nothing deletes the replay consumer, so the threshold is the slot's whole lifetime and must be seconds", EphemeralConsumerInactiveThreshold)
	}
	replayFixture(t, url, taskID, []TaskState{StateSubmitted, StateWorking, StateCompleted},
		WithUserPassword(replayAdminUser, replayPassword))
	if n := len(tasksConsumers(t, admin)); n != 0 {
		t.Fatalf("test bug: %d consumers on TASKS before the replay", n)
	}

	reader := replayReader(t, url, "threshold-reader", nil)
	start := time.Now()
	task, err := reader.TasksGet(testCtx(t), replayAddressee(taskID), taskID)
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	took := time.Since(start)
	if task.State != StateCompleted || !task.Final {
		t.Fatalf("fold = %s final=%v, want completed final", task.State, task.Final)
	}
	if took > replayReturnWithin {
		t.Fatalf("TasksGet took %s, want under %s: nothing on this path may wait out a refused request", took, replayReturnWithin)
	}

	after := tasksConsumers(t, admin)
	t.Logf("consumers on TASKS the instant TasksGet returned: %d %s", len(after), describeConsumers(after))
	if len(after) != 1 {
		t.Fatalf("consumers on TASKS after return = %d, want the one the replay created", len(after))
	}
	if got := after[0].Config.InactiveThreshold; got != EphemeralConsumerInactiveThreshold {
		t.Fatalf("replay consumer inactive_threshold on the server = %s, want %s (nats.go's ordered default is 5m)", got, EphemeralConsumerInactiveThreshold)
	}

	waitFor(t, thresholdReapWithin, "the inactive threshold to reap the replay consumer", func() bool {
		return len(tasksConsumers(t, admin)) == 0
	})
	t.Logf("consumers on TASKS %s after return: 0 (reaped by the %s threshold, nothing deleted it)",
		time.Since(start).Round(100*time.Millisecond), EphemeralConsumerInactiveThreshold)
}

// TasksGet emits nothing the bridge grant refuses. The grant withholds
// $JS.API.CONSUMER.DELETE.TASKS.*, and a refused publish gets no reply: it
// costs one Error-level "Permissions Violation" line per call from the
// connection's async error handler, which is the line an operator is taught
// to read as a missing grant. A replay must not make that line routine, so
// this asserts its absence -- and proves the assertion is not vacuous by
// driving a known refusal down the same connection afterwards and requiring
// that one to show up. Refusals arrive in the order the publishes left, so
// the control line landing is the barrier: anything the replay itself was
// refused is already in the buffer by then.
//
// The assertion is over every violation in the buffer, not over
// CONSUMER.DELETE alone. DELETE is the subject this change is about, but it
// is not the only one the grant withholds, and TasksGet's request path is
// wider than the ordered consumer: the horizon read ahead of it emits
// STREAM.INFO and a direct get, and a nats.go bump could add a CONSUMER.INFO
// or CONSUMER.NAMES to either half. Counting the violations catches all of
// that, and costs nothing over checking one subject, because the control
// refusal already tells us exactly how many there should be.
func TestTasksGet_EmitsNothingTheBridgeGrantRefuses(t *testing.T) {
	s := startPermissionedServer(t)
	url := clientURL(s)
	admin := adminJetStream(t, url)
	provisionTasksStreamAsProvisioned(t, admin)
	const taskID = "task-replay-no-refusal"
	replayFixture(t, url, taskID, []TaskState{StateSubmitted, StateCompleted},
		WithUserPassword(replayAdminUser, replayPassword))

	// logCapture is the package's slog sink (resilience_test.go); it is
	// already mutex-guarded, which this needs because the violation arrives
	// on the connection's async error handler, from its own goroutine.
	logs := &logCapture{}
	log := slog.New(logs)
	reader := replayReader(t, url, "no-refusal-reader", log)
	task, err := reader.TasksGet(testCtx(t), replayAddressee(taskID), taskID)
	if err != nil {
		t.Fatalf("TasksGet: %v", err)
	}
	if task.State != StateCompleted {
		t.Fatalf("fold = %s, want completed", task.State)
	}

	nc, _ := reader.conn()
	if err := nc.Publish(replayControlSubj, nil); err != nil {
		t.Fatalf("control publish: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("control flush: %v", err)
	}
	waitFor(t, logWithin, "the control refusal to reach the async error handler", func() bool {
		out := logs.String()
		return strings.Contains(out, permissionsViolation) && strings.Contains(out, replayControlSubj)
	})

	out := logs.String()
	if n := strings.Count(out, permissionsViolation); n != 1 {
		t.Fatalf("%d %q lines on the reader's connection, want exactly 1 (the control publish on %s); every extra one is a subject TasksGet emitted that the bridge grant withholds, and an Error line per tasks/get on a real install:\n%s",
			n, permissionsViolation, replayControlSubj, out)
	}
	if !strings.Contains(out, replayControlSubj) {
		t.Fatalf("the one %q line does not name %s, so it is not the control refusal and TasksGet emitted it:\n%s", permissionsViolation, replayControlSubj, out)
	}
	t.Logf("client log after the replay, with a known refusal appended as the barrier:\n%s", out)
}

// Twenty replays in a row leave twenty consumers on upstream/main, one per
// call, each for five MINUTES -- a count that tracks the call rate over that
// window against a max_consumers sized for the replays in flight (#1739).
// Here every one of them carries the five-second threshold and the stream is
// clear within it.
func TestTasksGet_BurstBoundsReplayConsumersByTheThreshold(t *testing.T) {
	s := startPermissionedServer(t)
	url := clientURL(s)
	admin := adminJetStream(t, url)
	provisionTasksStreamAsProvisioned(t, admin)
	const taskID = "task-replay-burst"
	replayFixture(t, url, taskID, []TaskState{StateSubmitted, StateWorking, StateCompleted},
		WithUserPassword(replayAdminUser, replayPassword))

	ctx := testCtx(t)
	reader := replayReader(t, url, "burst-reader", nil)
	start := time.Now()
	for i := 0; i < replayBurst; i++ {
		if _, err := reader.TasksGet(ctx, replayAddressee(taskID), taskID); err != nil {
			t.Fatalf("TasksGet #%d: %v", i+1, err)
		}
	}
	after := tasksConsumers(t, admin)
	t.Logf("consumers on TASKS the instant the %d-call burst returned: %d %s", replayBurst, len(after), describeConsumers(after))
	if len(after) > replayBurst {
		t.Fatalf("%d consumers after %d calls: more than the calls that could be in flight", len(after), replayBurst)
	}
	if len(after) == 0 {
		t.Fatalf("test bug: the burst left no consumers to measure; the reap raced the list")
	}
	for _, info := range after {
		if got := info.Config.InactiveThreshold; got != EphemeralConsumerInactiveThreshold {
			t.Fatalf("burst consumer %s inactive_threshold on the server = %s, want %s", info.Name, got, EphemeralConsumerInactiveThreshold)
		}
	}
	waitFor(t, thresholdReapWithin, "every replay consumer to be reaped", func() bool {
		return len(tasksConsumers(t, admin)) == 0
	})
	t.Logf("consumers on TASKS %s after the burst: 0", time.Since(start).Round(100*time.Millisecond))
}
