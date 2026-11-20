package lib

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

func permissionedServerOpts(allowSub []string) *natsserver.Options {
	return &natsserver.Options{Host: "127.0.0.1", Port: -1, Users: []*natsserver.User{
		{Username: "u", Password: "p", Permissions: &natsserver.Permissions{
			Publish:   &natsserver.SubjectPermission{Allow: []string{">"}},
			Subscribe: &natsserver.SubjectPermission{Allow: allowSub},
		}},
		{Username: "pub", Password: "p"},
	}}
}

// A subscription the server refuses is refused after the flush: SubscribeCore
// returns nil, and only OnAsyncError hears the permissions violation. A
// server reload that grants the subject doesn't revive the refused
// subscription, and a fresh one receives, which is why a caller that hears
// the refusal subscribes again (the gateway's trigger ingress, #5617).
func TestARefusedSubscriptionIsHeardOnlyByOnAsyncError(t *testing.T) {
	opts := permissionedServerOpts([]string{"_INBOX.>"})
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("server not ready")
	}
	t.Cleanup(s.Shutdown)
	c, err := Connect(context.Background(), s.ClientURL(), WithNATSOptions(nats.UserInfo("u", "p")))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)

	var mu sync.Mutex
	var heard []string
	remove := c.OnAsyncError(func(err error) {
		mu.Lock()
		heard = append(heard, err.Error())
		mu.Unlock()
	})
	got := make(chan struct{}, 4)
	old, err := c.SubscribeCore("a2a.trigger.*.submit", func(*nats.Msg) { got <- struct{}{} })
	if err != nil {
		t.Fatalf("SubscribeCore returned %v; the refusal is expected to arrive async", err)
	}
	defer old.Stop()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(heard)
		mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	if len(heard) == 0 || !strings.Contains(heard[0], `Subscription to "a2a.trigger.*.submit"`) {
		t.Fatalf("OnAsyncError heard %v, want the subscription's permissions violation", heard)
	}
	mu.Unlock()

	if err := s.ReloadOptions(permissionedServerOpts([]string{"_INBOX.>", "a2a.trigger.>"})); err != nil {
		t.Fatal(err)
	}
	pub, err := nats.Connect(s.ClientURL(), nats.UserInfo("pub", "p"))
	if err != nil {
		t.Fatal(err)
	}
	defer pub.Close()
	_ = pub.Publish("a2a.trigger.schedule.submit", []byte("{}"))
	_ = pub.Flush()
	select {
	case <-got:
		t.Fatal("the refused subscription received after the reload")
	case <-time.After(300 * time.Millisecond):
	}
	fresh, err := c.SubscribeCore("a2a.trigger.*.submit", func(*nats.Msg) { got <- struct{}{} })
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Stop()
	_ = pub.Publish("a2a.trigger.schedule.submit", []byte("{}"))
	_ = pub.Flush()
	select {
	case <-got:
	case <-time.After(3 * time.Second):
		t.Fatal("a fresh subscription after the reload received nothing")
	}

	remove()
	mu.Lock()
	before := len(heard)
	mu.Unlock()
	if _, err := c.SubscribeCore("chat.denied", func(*nats.Msg) {}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if len(heard) != before {
		t.Errorf("a removed hook still heard %v", heard[before:])
	}
}
