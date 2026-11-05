package busmigrate

import (
	"context"
	"fmt"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

const testRoutes = "nats://127.0.0.1:16251,nats://127.0.0.1:16252,nats://127.0.0.1:16253"

func startServer(t *testing.T, name, dir string, clusterPort int) *server.Server {
	t.Helper()
	o := &server.Options{ServerName: name, Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: dir, NoSigs: true, NoLog: true}
	if clusterPort != 0 {
		o.Cluster = server.ClusterOpts{Name: "bus", Host: "127.0.0.1", Port: clusterPort}
		o.Routes = server.RoutesFromStr(testRoutes)
	}
	s, err := server.NewServer(o)
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatalf("%s not ready", name)
	}
	return s
}

func shutdown(ss ...*server.Server) {
	for _, s := range ss {
		s.Shutdown()
		s.WaitForShutdown()
	}
}

// seedBus creates a work stream and a KV bucket at R3, the shapes the
// provision Job makes, with messages, a durable consumer partway through,
// and a key at revision 3.
func seedBus(t *testing.T, url string, replicas int) {
	t.Helper()
	var last error
	for i := 0; i < 30; i++ {
		last = func() error {
			nc, err := nats.Connect(url)
			if err != nil {
				return err
			}
			defer nc.Close()
			js, _ := nc.JetStream()
			if _, err := js.AddStream(&nats.StreamConfig{Name: "TASKS", Subjects: []string{"a2a.tasks.>"}, Replicas: replicas,
				AllowDirect: true, MaxBytes: 1 << 30, Discard: nats.DiscardOld, Duplicates: 2 * time.Minute}); err != nil {
				return err
			}
			for j := 0; j < 5; j++ {
				if _, err := js.Publish("a2a.tasks.x", []byte(fmt.Sprint(j))); err != nil {
					return err
				}
			}
			if _, err := js.AddConsumer("TASKS", &nats.ConsumerConfig{Durable: "worker", AckPolicy: nats.AckExplicitPolicy}); err != nil {
				return err
			}
			sub, err := js.PullSubscribe("a2a.tasks.>", "worker", nats.Bind("TASKS", "worker"))
			if err != nil {
				return err
			}
			msgs, err := sub.Fetch(2, nats.MaxWait(5*time.Second))
			if err != nil {
				return err
			}
			for _, m := range msgs {
				m.AckSync()
			}
			kv, err := js.CreateKeyValue(&nats.KeyValueConfig{Bucket: "cap", Replicas: replicas, History: 1, MaxBytes: 1 << 28})
			if err != nil {
				return err
			}
			for _, v := range []string{"1", "2", "3"} {
				if _, err := kv.Put("k", []byte(v)); err != nil {
					return err
				}
			}
			return nil
		}()
		if last == nil {
			return
		}
		time.Sleep(time.Second)
	}
	t.Fatalf("seeding the bus: %v", last)
}

func streamConfigs(t *testing.T, url string, names ...string) map[string]nats.StreamConfig {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	out := map[string]nats.StreamConfig{}
	for _, n := range names {
		info, err := streamInfo(context.Background(), nc, n)
		if err != nil {
			t.Fatal(err)
		}
		out[n] = info.Config
	}
	return out
}

// Three to one, end to end on embedded servers: after PinToServer every
// stream is R1 on server 0 with nothing else in its config changed, and
// server 0 restarted alone keeps every message at its sequence, the KV key
// at its revision and the consumer at its ack floor.
func TestThreeToOneKeepsTheBus(t *testing.T) {
	ctx := context.Background()
	base := t.TempDir()
	d0 := filepath.Join(base, "0")
	s0 := startServer(t, "a2a-nats-0", d0, 16251)
	s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
	s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
	seedBus(t, s0.ClientURL(), 3)
	streams := []string{"TASKS", "KV_cap"}
	before := streamConfigs(t, s0.ClientURL(), streams...)

	nc, err := nats.Connect(s1.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	if err := PinToServer(ctx, nc, "a2a-nats-0", streams, 500*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	// Idempotent: a run that failed part way runs again cleanly.
	if err := PinToServer(ctx, nc, "a2a-nats-0", streams, 500*time.Millisecond); err != nil {
		t.Fatalf("second run: %v", err)
	}
	nc.Close()
	after := streamConfigs(t, s0.ClientURL(), streams...)
	for _, n := range streams {
		b, a := before[n], after[n]
		if a.Replicas != 1 {
			t.Errorf("%s replicas %d after the pin, want 1", n, a.Replicas)
		}
		b.Replicas, a.Replicas = 1, 1
		if !reflect.DeepEqual(b, a) {
			t.Errorf("%s config changed beyond the replica count:\nbefore %+v\nafter  %+v", n, b, a)
		}
	}

	shutdown(s2, s1, s0)
	alone := startServer(t, "a2a-nats-0", d0, 0)
	defer shutdown(alone)
	nc, err = nats.Connect(alone.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	js, _ := nc.JetStream()
	si, err := js.StreamInfo("TASKS")
	if err != nil {
		t.Fatalf("TASKS on server 0 alone: %v", err)
	}
	if si.State.Msgs != 5 || si.State.FirstSeq != 1 || si.State.LastSeq != 5 {
		t.Errorf("TASKS on server 0 alone: %d msgs, sequences %d-%d; want 5, 1-5", si.State.Msgs, si.State.FirstSeq, si.State.LastSeq)
	}
	ci, err := js.ConsumerInfo("TASKS", "worker")
	if err != nil || ci.AckFloor.Stream != 2 || ci.NumPending != 3 {
		t.Errorf("worker on server 0 alone: %+v, err %v; want ack floor 2 and 3 pending", ci, err)
	}
	kv, err := js.KeyValue("cap")
	if err != nil {
		t.Fatal(err)
	}
	e, err := kv.Get("k")
	if err != nil || string(e.Value()) != "3" || e.Revision() != 3 {
		t.Errorf("cap/k on server 0 alone: %v, err %v; want 3 at revision 3", e, err)
	}
}

// A three-to-one run that fails before the bus shrinks is undone: streams the
// run pinned to R1 go back to R3 with three peers, the data intact.
func TestAFailedThreeToOneRunIsUndone(t *testing.T) {
	ctx := context.Background()
	base := t.TempDir()
	s0 := startServer(t, "a2a-nats-0", filepath.Join(base, "0"), 16251)
	s1 := startServer(t, "a2a-nats-1", filepath.Join(base, "1"), 16252)
	s2 := startServer(t, "a2a-nats-2", filepath.Join(base, "2"), 16253)
	defer shutdown(s2, s1, s0)
	seedBus(t, s0.ClientURL(), 3)
	nc, err := nats.Connect(s1.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	// The run got as far as pinning TASKS, then failed.
	if err := PinToServer(ctx, nc, "a2a-nats-0", []string{"TASKS"}, 500*time.Millisecond); err != nil {
		t.Fatal(err)
	}
	if err := UnpinFromServer(ctx, nc, []string{"TASKS", "KV_cap"}, 3); err != nil {
		t.Fatal(err)
	}
	for i := 0; ; i++ {
		info, err := streamInfo(ctx, nc, "TASKS")
		if err == nil && info.Config.Replicas == 3 && info.Cluster != nil && len(info.Cluster.Replicas) == 2 && info.State.Msgs == 5 {
			break
		}
		if i > 30 {
			t.Fatalf("TASKS after the undo: %+v, err %v; want R3 with two followers and 5 msgs", info, err)
		}
		time.Sleep(500 * time.Millisecond)
	}
}
