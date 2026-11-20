package gateway

import (
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// fakeIncidentBridge answers the incident route on the rig's bus as the
// bridge does, refusing the writes refuse says to, and records them.
type fakeIncidentBridge struct {
	mu     sync.Mutex
	keeps  []lib.IncidentKeep
	refuse func(n int) bool
}

func (f *fakeIncidentBridge) answer(m *nats.Msg) {
	var k lib.IncidentKeep
	_ = json.Unmarshal(m.Data, &k)
	f.mu.Lock()
	f.keeps = append(f.keeps, k)
	n := len(f.keeps)
	f.mu.Unlock()
	r := lib.IncidentKeepResult{OK: true}
	if f.refuse != nil && f.refuse(n) {
		r = lib.IncidentKeepResult{Error: "session-kv answered HTTP 503: busy"}
	}
	data, _ := json.Marshal(r)
	_ = m.Respond(data)
}

func (f *fakeIncidentBridge) all() []lib.IncidentKeep {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]lib.IncidentKeep(nil), f.keeps...)
}

// busIncidentRig is a thread rig whose incident writer is the gateway's own,
// over the bus.
func busIncidentRig(t *testing.T) (*rig, *threadedReporter) {
	t.Helper()
	r, home, _, _ := threadRig(t)
	r.g.reportIncidents = busIncidentWriter{client: r.client}
	return r, home
}

// TestEveryScheduledPostAsksTheBridgeToKeepIt: each post sends one incident
// write on the route, with its platform, channel, thread and stored report;
// a write the bridge could not store is logged and the post stands.
func TestEveryScheduledPostAsksTheBridgeToKeepIt(t *testing.T) {
	t.Parallel()
	r, _ := busIncidentRig(t)
	bridge := &fakeIncidentBridge{refuse: func(n int) bool { return n == 2 }}
	nc, err := connectTest(r.url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	if _, err := nc.Subscribe(lib.IncidentSubject, bridge.answer); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	first := deliverRun(t, r, r.g, "task-00000000000000c1", "One finding.")
	second := deliverRun(t, r, r.g, "task-00000000000000c2", "Two findings.")
	if first.Status != scheduledPosted || second.Status != scheduledPosted {
		t.Fatalf("records = %+v / %+v, want both posted though the second's keep failed", first, second)
	}
	want := []lib.IncidentKeep{
		{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "t1", Report: "One finding."},
		{Platform: lib.NotifyPlatformSlack, ChatID: "C0HOME", ThreadID: "t1", Report: "Two findings."},
	}
	if got := bridge.all(); len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("incident writes = %+v, want %+v", got, want)
	}
}

// TestAScheduledPostStandsWithNoBridgeToKeepIt: with nothing answering the
// route, the write fails at once, and the post is recorded posted.
func TestAScheduledPostStandsWithNoBridgeToKeepIt(t *testing.T) {
	t.Parallel()
	r, home := busIncidentRig(t)
	start := time.Now()
	rep := deliverRun(t, r, r.g, "task-00000000000000c3", "One finding.")
	if rep.Status != scheduledPosted || len(home.all()) != 1 {
		t.Fatalf("record = %+v, posts %+v", rep, home.all())
	}
	if waited := time.Since(start); waited > incidentKeepTimeout+2*time.Second {
		t.Fatalf("the post took %s; the keep's bound is %s", waited, incidentKeepTimeout)
	}
}
