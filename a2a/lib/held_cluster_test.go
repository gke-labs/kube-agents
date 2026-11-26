package lib

// A held stop on a three-node JetStream cluster (#4849): the holders connect
// to servers other than the consumer's leader, so the stop's unsubscribe and
// the handler's give-back both cross a route before they reach it. This one
// passes on the lib before #4849's fix too: the race shows on one server,
// the shape installs run today. It guards that the drain-first stop keeps
// working where production is headed.

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// clusterSize is the cluster an install runs (R3).
const clusterSize = 3

// clusterStartAttempts is how many clusters startCluster starts, each on
// fresh route ports, before a server that never binds its route port fails
// the test.
const clusterStartAttempts = 3

// serverReadyTimeout is how long tryStartCluster waits for each server to
// listen.
const serverReadyTimeout = 10 * time.Second

// clusterRouteTimeout is how long tryStartCluster waits for every server in
// the cluster to connect its route mesh to the other clusterSize-1 peers.
const clusterRouteTimeout = 10 * time.Second

// clusterTestTimeout bounds the full cross-route hand-back test across R3
// stream/consumer Raft setup and all delivery rounds.
const clusterTestTimeout = 90 * time.Second

// clusterMetaWaitTimeout is how long the cluster test waits for JetStream
// R3 stream and consumer leader election.
const clusterMetaWaitTimeout = 20 * time.Second

// clusterStreamAttemptTimeout bounds a single R3 stream or consumer API
// call while waiting for Raft leader election under load.
const clusterStreamAttemptTimeout = 5 * time.Second

// clusterHandBackRounds is how many cross-route SubscribeHeld/Stop hand-offs
// the test runs across the follower nodes.
const clusterHandBackRounds = 50

// clusterHandBackWait bounds how long a round waits for cross-route
// redelivery or subscription drain after Stop.
const clusterHandBackWait = 5 * time.Second

// clusterTasksStream, clusterTasksSubjects, and clusterTasksMaxAge match the
// TASKS stream configuration used by held-delivery tests.
const (
	clusterTasksStream   = "TASKS"
	clusterTasksSubjects = "a2a.tasks.>"
	clusterTasksMaxAge   = 72 * time.Hour
)

// clusterProfile, clusterTaskID, and clusterConsumerAckWait configure the
// durable consumer and test message for the cross-route hand-back test.
const (
	clusterProfile         = "cluster"
	clusterTaskID          = "task-cluster"
	clusterConsumerAckWait = time.Minute
)

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

// startCluster runs clusterSize JetStream servers routed to each other.
// freePort releases each route port before its server binds it, so another
// socket can take the port in between. That server then listens for clients
// but not for routes and never becomes ready (#5056). A cluster with such a
// server is shut down and started again on fresh ports.
func startCluster(t *testing.T) []*natsserver.Server {
	t.Helper()
	return startClusterOn(t, freePort)
}

// startClusterOn is startCluster with the route ports taken from pick, so a
// test can hand it a port that another socket holds.
func startClusterOn(t *testing.T, pick func(*testing.T) int) []*natsserver.Server {
	t.Helper()
	for attempt := 1; ; attempt++ {
		servers, unrouted := tryStartCluster(t, pick)
		if unrouted == nil {
			return servers
		}
		for _, s := range servers {
			s.Shutdown()
			s.WaitForShutdown()
		}
		if attempt == clusterStartAttempts {
			t.Fatalf("each of %d clusters had a server that never bound its route port (last: %s)", clusterStartAttempts, unrouted.Name())
		}
		t.Logf("nats-server %s never bound its route port; starting the cluster again on fresh ports", unrouted.Name())
	}
}

// tryStartCluster starts one cluster, and returns with it the server that
// listens for clients but not for routes, or nil when every server is ready.
func tryStartCluster(t *testing.T, pick func(*testing.T) int) ([]*natsserver.Server, *natsserver.Server) {
	t.Helper()
	ports := make([]int, clusterSize)
	for i := range ports {
		ports[i] = pick(t)
	}
	var routes []*url.URL
	for _, p := range ports {
		routes = append(routes, &url.URL{Scheme: "nats-route", Host: fmt.Sprintf("127.0.0.1:%d", p)})
	}
	servers := make([]*natsserver.Server, clusterSize)
	for i := range servers {
		i := i
		opts := &natsserver.Options{
			ServerName: fmt.Sprintf("n%d", i), Host: "127.0.0.1", Port: -1,
			JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
			Cluster: natsserver.ClusterOpts{Name: "held", Host: "127.0.0.1", Port: ports[i]},
			Routes:  routes,
		}
		opts.Cluster.PoolSize = -1
		s, err := natsserver.NewServer(opts)
		if err != nil {
			t.Fatalf("NewServer: %v", err)
		}
		go s.Start()
		t.Cleanup(func() {
			s.Shutdown()
			s.WaitForShutdown()
		})
		servers[i] = s
	}
	for _, s := range servers {
		if s.ReadyForConnections(serverReadyTimeout) {
			continue
		}
		// A server binds its route port right after its client port, so one
		// that listens only for clients lost its route port.
		client, route := s.Addr(), s.ClusterAddr()
		if client != nil && route == nil {
			return servers, s
		}
		t.Fatalf("nats-server %s not ready: client listener %v, route listener %v", s.Name(), client, route)
	}
	waitFor(t, clusterRouteTimeout, "cluster routes", func() bool {
		for _, s := range servers {
			if s.NumRoutes() != clusterSize-1 {
				return false
			}
		}
		return true
	})
	return servers, nil
}

// TestStartClusterStartsAgainWhenARoutePortIsTaken pins the #5056 retry and
// the nats-server behaviour it rests on: a server whose route port another
// socket holds still listens for clients, tryStartCluster names it, and
// startCluster starts the cluster again on fresh ports. If a nats-server
// upgrade stops binding the client listener first, this test fails rather
// than the flake coming back under another message.
func TestStartClusterStartsAgainWhenARoutePortIsTaken(t *testing.T) {
	held, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer held.Close()
	heldPort := held.Addr().(*net.TCPAddr).Port
	picks := 0
	pick := func(t *testing.T) int {
		picks++
		if picks == 1 {
			return heldPort
		}
		return freePort(t)
	}
	servers := startClusterOn(t, pick)
	if picks < 2*clusterSize {
		t.Fatalf("picked %d route ports, want at least %d: the cluster with the held port was not started again", picks, 2*clusterSize)
	}
	for _, s := range servers {
		if s.ClusterAddr() == nil {
			t.Fatalf("nats-server %s has no route listener", s.Name())
		}
	}
}

func TestHeldStopHandsBackAtOnceAcrossAClusterRoute(t *testing.T) {
	servers := startCluster(t)
	ctx, cancel := context.WithTimeout(context.Background(), clusterTestTimeout)
	t.Cleanup(cancel)
	admin, err := nats.Connect(clientURL(servers[0]))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer admin.Close()
	js, err := jetstream.New(admin)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	waitFor(t, clusterMetaWaitTimeout, "a JetStream meta leader", func() bool {
		sctx, cancel := context.WithTimeout(ctx, clusterStreamAttemptTimeout)
		defer cancel()
		_, err := js.CreateOrUpdateStream(sctx, jetstream.StreamConfig{
			Name: clusterTasksStream, Subjects: []string{clusterTasksSubjects}, Retention: jetstream.LimitsPolicy,
			MaxAge: clusterTasksMaxAge, Replicas: clusterSize,
		})
		return err == nil
	})
	req, err := Connect(ctx, clientURL(servers[0]), WithName("requester"))
	if err != nil {
		t.Fatalf("Connect requester: %v", err)
	}
	t.Cleanup(req.Close)
	h := &heldHarness{t: t, ctx: ctx, req: req, profile: clusterProfile, url: clientURL(servers[0])}
	var ci *jetstream.ConsumerInfo
	waitFor(t, clusterMetaWaitTimeout, "a consumer leader", func() bool {
		cctx, cancel := context.WithTimeout(ctx, clusterStreamAttemptTimeout)
		defer cancel()
		c := h.cfg()
		if err := req.EnsureHeldConsumer(cctx, HeldConsumerConfig{
			Stream: c.Stream, Durable: c.Durable, FilterSubject: c.Subject,
			MaxAckPending: 1, AckWait: clusterConsumerAckWait,
		}); err != nil {
			return false
		}
		cons, err := js.Consumer(cctx, clusterTasksStream, c.Durable)
		if err != nil {
			return false
		}
		info, err := cons.Info(cctx)
		if err != nil || info.Cluster == nil || info.Cluster.Leader == "" {
			return false
		}
		for _, s := range servers {
			if s.Name() == info.Cluster.Leader {
				ci = info
				return true
			}
		}
		return false
	})
	// Both holders on followers, so neither talks to the leader directly.
	var holders []*Client
	for _, s := range servers {
		if s.Name() == ci.Cluster.Leader {
			continue
		}
		c, err := Connect(ctx, clientURL(s), WithName("holder-"+s.Name()))
		if err != nil {
			t.Fatalf("Connect holder: %v", err)
		}
		t.Cleanup(c.Close)
		holders = append(holders, c)
	}
	if len(holders) != clusterSize-1 {
		t.Fatalf("leader %q not among the servers", ci.Cluster.Leader)
	}
	h.submit(clusterTaskID)
	for round := 0; round < clusterHandBackRounds; round++ {
		held := make(chan struct{}, 1)
		sub, err := holders[round%2].SubscribeHeld(ctx, h.cfg(), func(ctx context.Context, d Delivery) {
			select {
			case held <- struct{}{}:
			default:
			}
			<-ctx.Done()
		})
		if err != nil {
			t.Fatalf("round %d: SubscribeHeld: %v", round, err)
		}
		select {
		case <-held:
		case <-time.After(clusterHandBackWait):
			sub.Stop()
			t.Fatalf("round %d: the task wasn't handed back within 5s of the last holder's stop (AckWait is a minute)", round)
		}
		sub.Stop()
		wctx, cancel := context.WithTimeout(ctx, clusterHandBackWait)
		err = sub.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait after Stop: %v", round, err)
		}
	}
}
