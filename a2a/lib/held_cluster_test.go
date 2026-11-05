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
func startCluster(t *testing.T) []*natsserver.Server {
	t.Helper()
	ports := make([]int, clusterSize)
	for i := range ports {
		ports[i] = freePort(t)
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
		s, err := natsserver.NewServer(opts)
		if err != nil {
			t.Fatalf("NewServer: %v", err)
		}
		go s.Start()
		t.Cleanup(s.Shutdown)
		servers[i] = s
	}
	for _, s := range servers {
		if !s.ReadyForConnections(10 * time.Second) {
			t.Fatal("nats-server not ready")
		}
	}
	return servers
}

func TestHeldStopHandsBackAtOnceAcrossAClusterRoute(t *testing.T) {
	servers := startCluster(t)
	ctx := testCtx(t)
	admin, err := nats.Connect(clientURL(servers[0]))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer admin.Close()
	js, err := jetstream.New(admin)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	waitFor(t, 20e9, "a JetStream meta leader", func() bool {
		sctx, cancel := context.WithTimeout(ctx, time.Second)
		defer cancel()
		_, err := js.CreateOrUpdateStream(sctx, jetstream.StreamConfig{
			Name: "TASKS", Subjects: []string{"a2a.tasks.>"}, Retention: jetstream.LimitsPolicy,
			MaxAge: 72 * time.Hour, Replicas: clusterSize,
		})
		return err == nil
	})
	req, err := Connect(ctx, clientURL(servers[0]), WithName("requester"))
	if err != nil {
		t.Fatalf("Connect requester: %v", err)
	}
	t.Cleanup(req.Close)
	h := &heldHarness{t: t, ctx: ctx, req: req, profile: "cluster", url: clientURL(servers[0])}
	h.ensure(1, time.Minute)
	cons, err := js.Consumer(ctx, "TASKS", h.cfg().Durable)
	if err != nil {
		t.Fatalf("Consumer: %v", err)
	}
	ci, err := cons.Info(ctx)
	if err != nil || ci.Cluster == nil {
		t.Fatalf("consumer info: %v %+v", err, ci)
	}
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
	h.submit("task-cluster")
	for round := 0; round < 50; round++ {
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
		case <-time.After(5 * time.Second):
			sub.Stop()
			t.Fatalf("round %d: the task wasn't handed back within 5s of the last holder's stop (AckWait is a minute)", round)
		}
		sub.Stop()
		wctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		err = sub.Wait(wctx)
		cancel()
		if err != nil {
			t.Fatalf("round %d: Wait after Stop: %v", round, err)
		}
	}
}
