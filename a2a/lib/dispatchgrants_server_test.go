package lib

import (
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// On a real server, the dispatcher's grants for one profile (#5301): it may
// write that profile's terminal on either supervisor form, and is refused the
// six-token terminal of another profile and of a session, so the six-token
// grant names each profile and holds no wildcard over the profile token.
func TestOneProfilesDispatcherGrantCantWriteAnothersSupervisorSubject(t *testing.T) {
	g := DispatcherGrants([]DispatcherGrantProfile{{Name: "cluster", Tiers: []string{"read"}}})
	s, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1, Users: []*natsserver.User{{
		Username: "dispatcher", Password: "p",
		Permissions: &natsserver.Permissions{
			Publish:   &natsserver.SubjectPermission{Allow: g.Publish},
			Subscribe: &natsserver.SubjectPermission{Allow: append(g.Subscribe, "_INBOX.>")},
		},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	go s.Start()
	if !s.ReadyForConnections(5 * time.Second) {
		t.Fatal("server not ready")
	}
	t.Cleanup(s.Shutdown)
	var mu sync.Mutex
	var refused []string
	nc, err := nats.Connect(s.ClientURL(), nats.UserInfo("dispatcher", "p"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			mu.Lock()
			refused = append(refused, err.Error())
			mu.Unlock()
		}))
	if err != nil {
		t.Fatal(err)
	}
	defer nc.Close()
	allowed := []string{TaskSupervisorSubject("cluster", "t1"), ProfileSupervisorSubject("cluster", "t1")}
	denied := []string{ProfileSupervisorSubject("other", "t1"), ProfileSupervisorSubject("cto-otter-1a2b", "t1"), TaskSupervisorSubject("other", "t1")}
	for _, subject := range append(append([]string(nil), allowed...), denied...) {
		if err := nc.Publish(subject, []byte("{}")); err != nil {
			t.Fatal(err)
		}
	}
	_ = nc.FlushTimeout(2 * time.Second)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		n := len(refused)
		mu.Unlock()
		if n >= len(denied) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	joined := strings.Join(refused, "\n")
	for _, subject := range denied {
		if !strings.Contains(joined, `"`+subject+`"`) {
			t.Errorf("no refusal for %q; refusals:\n%s", subject, joined)
		}
	}
	for _, subject := range allowed {
		if strings.Contains(joined, `"`+subject+`"`) {
			t.Errorf("the profile's own terminal on %q was refused", subject)
		}
	}
}
