package gateway

import (
	"context"
	"fmt"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"
)

// A single-turn DM's pod is reaped after DMIdleTTL, a delegation's wake not
// counting as a turn; a DM whose pod has taken a second turn, a group conversation, and a single-turn DM inside the shorter
// TTL keep theirs (#2371). Tasks addressed to an earlier pod don't count: a
// DM keyed by its channel keeps one record across every question.
func TestASingleTurnDMIsReapedAfterTheShorterTTL(t *testing.T) {
	r, spawn := startReuseRig(t)
	r.g.cfg.IdleTTL, r.g.cfg.DMIdleTTL = 30*time.Minute, 5*time.Minute
	ctx := context.Background()
	now := time.Now().UTC()
	for _, c := range []struct {
		pod, kind string
		tasks     []string // addressees; "" is the record's pod
		idle      time.Duration
	}{
		{"chat-dm-one", "dm", []string{""}, 7 * time.Minute},
		{"chat-dm-two", "dm", []string{"", ""}, 7 * time.Minute},
		{"chat-group-one", "group", []string{""}, 7 * time.Minute},
		{"chat-dm-fresh", "dm", []string{""}, 3 * time.Minute},
		{"chat-dm-again", "dm", []string{"chat-old-1", "chat-old-1", "chat-old-2", ""}, 7 * time.Minute},
		// The turn delegated: the child went to platform, and its result came
		// back to the pod as a wake. Neither is a turn.
		{"chat-dm-woken", "dm", []string{"", "child:platform", "wake:"}, 7 * time.Minute},
	} {
		var tasks []TaskRef
		for i, a := range c.tasks {
			role := ""
			if r, rest, ok := strings.Cut(a, ":"); ok {
				role, a = r, rest
			}
			if a == "" {
				a = c.pod
			}
			tasks = append(tasks, TaskRef{ID: fmt.Sprintf("task-%s-%d", c.pod, i), Addressee: a, Role: role})
		}
		rec := &SessionRecord{
			Key: "inject:" + c.pod, ContextID: "ctx-" + c.pod, Kind: c.kind, Tasks: tasks,
			Addressee: c.pod, BusSession: c.pod, PodName: c.pod, PodReuse: true, SessionRouted: true,
			LastUserMessage: now.Add(-c.idle), LastActivity: now.Add(-c.idle), LastTaskActivity: now.Add(-c.idle),
		}
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
		r.g.reapSession(ctx, rec)
	}
	got := spawn.deleted()
	sort.Strings(got)
	if want := []string{"chat-dm-again", "chat-dm-one", "chat-dm-woken"}; !slices.Equal(got, want) {
		t.Fatalf("reap deleted %v, want %v", got, want)
	}
}

// Under the DM TTL the answer restarts the clock: a DM whose task ran longer
// than the TTL keeps its pod for the TTL after the answer, not zero. Under
// IdleTTL the answer doesn't count, as before.
func TestTheDMIdleClockCountsFromTheAnswer(t *testing.T) {
	cfg := &Config{IdleTTL: 30 * time.Minute, DMIdleTTL: 5 * time.Minute}
	now := time.Now()
	long := &SessionRecord{Kind: "dm", BusSession: "b", Tasks: []TaskRef{{ID: "t", Addressee: "b"}},
		LastUserMessage: now.Add(-8 * time.Minute), LastTaskActivity: now.Add(-2 * time.Minute)}
	if cfg.idleExpired(long, now) {
		t.Error("a DM answered 2m ago was expired")
	}
	if !cfg.idleExpired(long, now.Add(4*time.Minute)) {
		t.Error("a DM answered 6m ago was kept")
	}
	group := &SessionRecord{Kind: "group", LastUserMessage: now.Add(-31 * time.Minute), LastTaskActivity: now.Add(-time.Minute)}
	if !cfg.idleExpired(group, now) {
		t.Error("a group answered 1m ago but silent 31m was kept; the answer must not move IdleTTL's clock")
	}
}

// A DM turn through the gateway writes what the rule reads: Kind "dm", and
// one task addressed to the pod it spawned.
func TestADMTurnIsASingleTurnDM(t *testing.T) {
	r, spawn := startReuseRig(t)
	conv := "discord:dm/c-1"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "dm", AuthorID: "1001", MessageID: "m1", Text: "hello"}
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) == 1 })
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil || rec == nil {
		t.Fatalf("record: %v, %v", rec, err)
	}
	if rec.Kind != "dm" || !rec.singleTurnDM() {
		t.Fatalf("after one DM turn: Kind %q, tasks %+v, bus session %q; want a single-turn DM", rec.Kind, rec.Tasks, rec.BusSession)
	}
}

// DMIdleTTL never lengthens a conversation's TTL, and a Config built without
// it reads IdleTTL.
func TestTheDMIdleTTLIsCappedAtTheIdleTTL(t *testing.T) {
	dm := &SessionRecord{Kind: "dm", BusSession: "b", Tasks: []TaskRef{{ID: "t", Addressee: "b"}}}
	for _, c := range []struct {
		idle, dmIdle, want time.Duration
	}{
		{30 * time.Minute, 5 * time.Minute, 5 * time.Minute},
		{30 * time.Minute, time.Hour, 30 * time.Minute},
		{30 * time.Minute, 0, 30 * time.Minute},
	} {
		cfg := &Config{IdleTTL: c.idle, DMIdleTTL: c.dmIdle}
		if got := cfg.idleTTLFor(dm); got != c.want {
			t.Errorf("IdleTTL %v, DMIdleTTL %v: got %v, want %v", c.idle, c.dmIdle, got, c.want)
		}
	}
	if got := (&Config{IdleTTL: 30 * time.Minute, DMIdleTTL: 5 * time.Minute}).idleTTLFor(&SessionRecord{Kind: "dm"}); got != 5*time.Minute {
		t.Errorf("a DM with no task yet: got %v, want the DM TTL", got)
	}
}

// A2A_DM_IDLE_TTL defaults to 5m, takes a duration, and refuses an
// unparsable value or one under the 1m floor.
func TestTheGatewayReadsTheDMIdleTTL(t *testing.T) {
	setBaseEnv(t)
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.DMIdleTTL != 5*time.Minute {
		t.Errorf("default DMIdleTTL %v, want 5m", cfg.DMIdleTTL)
	}
	t.Setenv("A2A_DM_IDLE_TTL", "10m")
	if cfg, err = FromEnv(); err != nil || cfg.DMIdleTTL != 10*time.Minute {
		t.Errorf("A2A_DM_IDLE_TTL=10m: %v, %v", cfg, err)
	}
	for _, bad := range []string{"soon", "30s"} {
		t.Setenv("A2A_DM_IDLE_TTL", bad)
		if _, err := FromEnv(); err == nil {
			t.Errorf("A2A_DM_IDLE_TTL=%s was accepted", bad)
		}
	}
}
