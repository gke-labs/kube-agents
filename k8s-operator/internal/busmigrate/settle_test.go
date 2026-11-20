package busmigrate

import (
	"context"
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// threeServerBusAtR1 starts a three-server cluster and seeds TASKS at one
// replica with its worker consumer, as a restore into three servers leaves it
// before the raise.
func threeServerBusAtR1(t *testing.T) (*nats.Conn, func()) {
	nc, _, stop := threeServerBusAtR1With(t)
	return nc, stop
}

// threeServerBusAtR1With is threeServerBusAtR1 that also hands back a server
// the test may stop (the one the client is not connected to).
func threeServerBusAtR1With(t *testing.T) (*nats.Conn, func(), func()) {
	t.Helper()
	base := t.TempDir()
	s0 := startServer(t, "a2a-nats-0", filepath.Join(base, "0"), 16251)
	s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
	s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
	seedBus(t, s0.ClientURL(), 1)
	nc, err := nats.Connect(s1.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	stopped := false
	stopS2 := func() { shutdown(s2); stopped = true }
	return nc, stopS2, func() {
		nc.Close()
		if !stopped {
			shutdown(s2)
		}
		shutdown(s1, s0)
	}
}

// Raised to three replicas, the stream settles: replicas current and its
// durable up on its leader at three replicas. Before the raise, the restored
// consumers are waited for and the ephemeral among them left out.
func TestARaisedStreamSettlesWithItsDurable(t *testing.T) {
	nc, stop := threeServerBusAtR1(t)
	defer stop()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	js, _ := nc.JetStream()
	if _, err := js.AddConsumer("TASKS", &nats.ConsumerConfig{AckPolicy: nats.AckExplicitPolicy, InactiveThreshold: time.Hour}); err != nil {
		t.Fatal(err)
	}
	info, err := streamInfo(ctx, nc, "TASKS")
	if err != nil {
		t.Fatal(err)
	}
	durables, err := AssignedDurables(ctx, nc, "TASKS", 2, info.Config, 3)
	if err != nil || len(durables) != 1 || durables[0] != (DurableSpec{"worker", 3}) {
		t.Fatalf("AssignedDurables = %v, %v; want the worker at three, the ephemeral left out", durables, err)
	}
	if err := RaiseReplicas(ctx, nc, "TASKS", 3); err != nil {
		t.Fatal(err)
	}
	took, err := WaitRestored(ctx, nc, "TASKS", 3, durables)
	if err != nil {
		t.Fatalf("WaitRestored = %v, want settled", err)
	}
	t.Logf("restore-window: TASKS settled after %s", took.Round(time.Millisecond))
	if ci, err := js.ConsumerInfo("TASKS", "worker"); err != nil || ci.Cluster == nil || len(ci.Cluster.Replicas) != 2 {
		t.Errorf("worker after a settled wait: %+v, %v; want it at three replicas", ci, err)
	}
}

// The restore's final answer counts the consumers it brought back.
func TestTheRestoreCountsTheConsumersItBroughtBack(t *testing.T) {
	meta, data := snapshotTasks(t)
	dst := startServer(t, "a2a-nats-0", t.TempDir(), 0)
	defer shutdown(dst)
	nc, err := nats.Connect(dst.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	n, err := RestoreVerifiedCounting(context.Background(), nc, meta.Config, meta.State, meta.TakenAt, opener(data), Digest(data), policyOf(meta.Config))
	if err != nil || n != 1 {
		t.Fatalf("RestoreVerifiedCounting = %d, %v; want the worker counted", n, err)
	}
}

// After a raise, a consumer follows the stream unless it has its own count
// on a limits-retained stream: nats-server's remap rule.
func TestExpectedConsumerReplicas(t *testing.T) {
	limits, work := nats.StreamConfig{Retention: nats.LimitsPolicy}, nats.StreamConfig{Retention: nats.WorkQueuePolicy}
	for _, tc := range []struct {
		own    int
		stream nats.StreamConfig
		want   int
	}{{0, limits, 3}, {1, limits, 1}, {0, work, 3}, {1, work, 3}} {
		if got := ExpectedConsumerReplicas(nats.ConsumerConfig{Durable: "d", Replicas: tc.own}, tc.stream, 3); got != tc.want {
			t.Errorf("own %d on %v: %d, want %d", tc.own, tc.stream.Retention, got, tc.want)
		}
	}
}

// fakeJetStream answers the settle wait's three reads on a plain server, so
// a test can hold the shape #6174 saw: the stream settled at three replicas,
// the durable still listed by the cluster, and its info "not found".
func fakeJetStream(t *testing.T, listed []string, worker string) *nats.Conn {
	t.Helper()
	s, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, NoSigs: true, NoLog: true})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("server not ready")
	}
	t.Cleanup(func() { shutdown(s) })
	nc, err := nats.Connect(s.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	answer := func(subject string, v any) {
		b, _ := json.Marshal(v)
		if _, err := nc.Subscribe(subject, func(m *nats.Msg) { _ = m.Respond(b) }); err != nil {
			t.Fatal(err)
		}
	}
	current := []map[string]any{{"name": "a2a-nats-1", "current": true}, {"name": "a2a-nats-2", "current": true}}
	answer("$JS.API.STREAM.INFO.TASKS", map[string]any{
		"config":  map[string]any{"name": "TASKS", "num_replicas": 3},
		"cluster": map[string]any{"leader": "a2a-nats-0", "replicas": current},
		"state":   map[string]any{"consumer_count": 1},
	})
	answer("$JS.API.CONSUMER.NAMES.TASKS", map[string]any{"total": len(listed), "consumers": listed})
	// worker: "up", "no-leader" (placed, no leader elected yet), "r1" (up
	// at one replica, before the raise's remap), "lagging" (a replica not
	// current), or "not-found".
	if worker != "not-found" {
		leader, replicas := "a2a-nats-0", any(current)
		switch worker {
		case "no-leader":
			leader = ""
		case "r1":
			replicas = []map[string]any{}
		case "lagging":
			replicas = []map[string]any{{"name": "a2a-nats-1", "current": true}, {"name": "a2a-nats-2", "current": false}}
		}
		answer("$JS.API.CONSUMER.INFO.TASKS.worker", map[string]any{
			"name": "worker", "config": map[string]any{"durable_name": "worker"},
			"cluster": map[string]any{"leader": leader, "replicas": replicas},
		})
	} else {
		answer("$JS.API.CONSUMER.INFO.TASKS.worker", map[string]any{"error": map[string]any{"code": 404, "err_code": jsConsumerNotFound, "description": "consumer not found"}})
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return nc
}

// A durable the stream still lists, but whose leader answers "not found",
// keeps the wait going though the stream itself has settled and counts its
// consumer: the #6174 window.
// One answering with no leader yet, at one replica, or with a replica
// behind keeps it going too.
func TestADurableNotYetOnItsLeaderKeepsWaiting(t *testing.T) {
	for worker, want := range map[string]string{
		"not-found": "consumer worker not found on its leader",
		"no-leader": "consumer worker has no leader",
		"r1":        "consumer worker has 0 of 2 followers",
		"lagging":   "consumer worker has a replica not current",
	} {
		nc := fakeJetStream(t, []string{"worker"}, worker)
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		_, err := WaitRestored(ctx, nc, "TASKS", 3, []DurableSpec{{"worker", 3}})
		cancel()
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: WaitRestored = %v, want a timeout naming %q", worker, err, want)
		}
	}
}

// The same durable answering from its leader with its replicas current
// settles; one the stream no longer lists was deleted (only an ephemeral can
// be, under the freeze) and is not waited for.
func TestADurableUpOrDeletedSettles(t *testing.T) {
	for _, tc := range []struct {
		name   string
		listed []string
		worker string
	}{{"up", []string{"worker"}, "up"}, {"deleted", nil, "not-found"}} {
		nc := fakeJetStream(t, tc.listed, tc.worker)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		if _, err := WaitRestored(ctx, nc, "TASKS", 3, []DurableSpec{{"worker", 3}}); err != nil {
			t.Errorf("%s: WaitRestored = %v, want settled", tc.name, err)
		}
		cancel()
	}
}

// A stream left at one replica never settles as three.
func TestAStreamNotRaisedNeverSettles(t *testing.T) {
	nc, stop := threeServerBusAtR1(t)
	defer stop()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := WaitRestored(ctx, nc, "TASKS", 3, []DurableSpec{{"worker", 3}})
	if err == nil || !strings.Contains(err.Error(), "config reads 1 replicas") {
		t.Fatalf("WaitRestored on an R1 stream = %v, want a timeout naming the replicas", err)
	}
}

// A follower that is no longer current keeps the wait going, even with the
// config at three replicas and the durable up.
func TestAFollowerNotCurrentKeepsWaiting(t *testing.T) {
	nc, stopS2, stop := threeServerBusAtR1With(t)
	defer stop()
	ctx := context.Background()
	if err := RaiseReplicas(ctx, nc, "TASKS", 3); err != nil {
		t.Fatal(err)
	}
	wctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if _, err := WaitRestored(wctx, nc, "TASKS", 3, []DurableSpec{{"worker", 3}}); err != nil {
		t.Fatalf("precondition: settled before the stop: %v", err)
	}
	stopS2()
	// Wait until the leader reports a follower not current.
	for deadline := time.Now().Add(45 * time.Second); ; time.Sleep(500 * time.Millisecond) {
		info, err := streamInfo(ctx, nc, "TASKS")
		if err == nil && info.Cluster != nil && !allCurrent(info.Cluster.Replicas) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the leader never reported the stopped follower as not current in 45s")
		}
	}
	short, cancel2 := context.WithTimeout(ctx, 2*time.Second)
	defer cancel2()
	_, err := WaitRestored(short, nc, "TASKS", 3, []DurableSpec{{"worker", 3}})
	if err == nil || !strings.Contains(err.Error(), "not current") {
		t.Fatalf("WaitRestored with a follower down = %v, want a timeout naming a follower not current", err)
	}
}

// Before the raise, the wait holds until the stream lists as many
// consumers as the restore brought back: the server assigns them after the
// restore answers.
func TestTheRaiseWaitsForTheRestoredConsumersAssignments(t *testing.T) {
	limits := nats.StreamConfig{Retention: nats.LimitsPolicy}
	nc := fakeJetStream(t, nil, "not-found")
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := AssignedDurables(ctx, nc, "TASKS", 1, limits, 3); err == nil || !strings.Contains(err.Error(), "0 of 1 restored consumers assigned") {
		t.Fatalf("AssignedDurables with nothing listed = %v, want a timeout naming 0 of 1", err)
	}
	nc = fakeJetStream(t, []string{"worker"}, "up")
	d, err := AssignedDurables(context.Background(), nc, "TASKS", 1, limits, 3)
	if err != nil || len(d) != 1 || d[0] != (DurableSpec{"worker", 3}) {
		t.Fatalf("AssignedDurables with the worker listed = %v, %v", d, err)
	}
}

// listInTurn makes AssignedDurables' consumer list answer each of lists in
// turn, then the last one forever.
func listInTurn(t *testing.T, lists ...[]string) {
	t.Helper()
	old := listAssigned
	t.Cleanup(func() { listAssigned = old })
	i := 0
	listAssigned = func(context.Context, *nats.Conn, string) ([]string, error) {
		l := lists[min(i, len(lists)-1)]
		i++
		return l, nil
	}
}

// A restored ephemeral listed and then deleted (idle) while the durable's
// assignment still lags still counts: every name ever listed does, since
// under the freeze only the server deletes consumers.
func TestAConsumerListedThenDeletedStillCountsAsRestored(t *testing.T) {
	nc := fakeJetStream(t, []string{"worker"}, "up")
	listInTurn(t, []string{"eph"}, nil, []string{"worker"})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	d, err := AssignedDurables(ctx, nc, "TASKS", 2, nats.StreamConfig{Retention: nats.LimitsPolicy}, 3)
	if err != nil || len(d) != 1 || d[0] != (DurableSpec{"worker", 3}) {
		t.Fatalf("AssignedDurables = %v, %v; want the worker, the deleted ephemeral counted", d, err)
	}
}

// Every listed consumer must answer before the raise: one listed but not
// answering yet holds it, so its expected replicas are known.
func TestAListedConsumerThatDoesNotAnswerHoldsTheRaise(t *testing.T) {
	nc := fakeJetStream(t, []string{"worker"}, "not-found")
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if d, err := AssignedDurables(ctx, nc, "TASKS", 1, nats.StreamConfig{Retention: nats.LimitsPolicy}, 3); err == nil || !strings.Contains(err.Error(), "consumer worker:") {
		t.Fatalf("AssignedDurables with the worker not answering = %v, %v; want a timeout naming it", d, err)
	}
}
