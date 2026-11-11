package gateway

import (
	"context"
	"fmt"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func targetNames(t lib.DelegateTargets) []string {
	out := []string{}
	for _, d := range t.Targets {
		out = append(out, d.Name)
	}
	return out
}

// A session turn names platform and every profile its requester may use, in
// name order, with each card's blurb folded to one line.
func TestDelegateTargetsNamePlatformThenEachProfile(t *testing.T) {
	g := &Gateway{}
	d := newProfileDirectory()
	d.put("e2e5", &lib.AgentCard{Name: "e2e5", Description: "reads one\ncluster's workloads"})
	d.put("cluster", &lib.AgentCard{Name: "cluster", Tiers: []string{"read"}})
	got := g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
	if want := []string{"platform", "cluster", "e2e5"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("targets = %v, want %v", targetNames(got), want)
	}
	if got.Targets[0].Description != platformTargetDescription {
		t.Errorf("platform's blurb = %q", got.Targets[0].Description)
	}
	if got.Targets[2].Description != "reads one cluster's workloads" {
		t.Errorf("e2e5's blurb = %q, want it on one line", got.Targets[2].Description)
	}
	if _, err := lib.DelegateTargetsPart(got); err != nil {
		t.Fatalf("the list doesn't build a part: %v", err)
	}
}

// The list applies the checks the delegation meets when it arrives, so it
// names nothing the gateway would refuse.
func TestDelegateTargetsLeaveOutWhatTheDelegationWouldRefuse(t *testing.T) {
	d := newProfileDirectory()
	d.put("open", &lib.AgentCard{Name: "open"})
	d.put("listed", &lib.AgentCard{Name: "listed"})
	d.put("writers", &lib.AgentCard{Name: "writers", Tiers: []string{"write"}})
	g := &Gateway{targetAllowed: map[string]map[string]map[string]bool{
		"listed":   {"discord": {"u2": true}},
		"platform": {"discord": {"u2": true}},
	}}
	got := g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
	if want := []string{"open"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("u1's targets = %v, want %v (listed and platform hold only u2; writers is write-tier)", targetNames(got), want)
	}
	got = g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u2"}, nil, false, d)
	if want := []string{"platform", "listed", "open"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("u2's targets = %v, want %v", targetNames(got), want)
	}
	// The A2A door delegates only under a list of its own.
	got = g.delegateTargets(TaskRequester{Backend: a2aBackend, Subject: "u2"}, nil, false, d)
	if len(got.Targets) != 0 {
		t.Fatalf("door caller's targets = %v, want none", targetNames(got))
	}
	// No attested identity reaches no profile.
	got = (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord"}, nil, false, d)
	if want := []string{"platform"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("no-identity targets = %v, want %v", targetNames(got), want)
	}
}

// A card named platform, or one whose name isn't an addressee, is never
// offered; and the list stops at the part's cap.
func TestDelegateTargetsSkipBadNamesAndStopAtTheCap(t *testing.T) {
	d := newProfileDirectory()
	d.put("platform", &lib.AgentCard{Name: "platform", Description: "an impostor"})
	d.put("a.b", &lib.AgentCard{Name: "a.b"})
	for i := 0; i < lib.DelegateTargetsMax+5; i++ {
		name := "p" + string(rune('a'+i%26)) + string(rune('a'+i/26))
		d.put(name, &lib.AgentCard{Name: name})
	}
	got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
	if len(got.Targets) != lib.DelegateTargetsMax || !got.Truncated {
		t.Fatalf("%d targets, truncated=%v; want the cap %d, truncated", len(got.Targets), got.Truncated, lib.DelegateTargetsMax)
	}
	for i, d := range got.Targets {
		if d.Name == "a.b" || (d.Name == "platform" && (i != 0 || d.Description != platformTargetDescription)) {
			t.Fatalf("offered %+v at %d", d, i)
		}
	}
}

// The submission to a conversation's own session pod carries the list, and a
// profile added after the pod's first turn is in the next turn's.
func TestASessionTurnCarriesItsDelegateTargets(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	r.g.profiles.put("e2e5", &lib.AgentCard{Name: "e2e5", Description: "reads one cluster"})
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/t-dt", Kind: "group", AuthorID: "1001", MessageID: "dt-1", Text: "use e2e5"}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	got, found, err := lib.DelegateTargetsOf(messageParts(t, origin))
	if err != nil || !found {
		t.Fatalf("delegateTargets part: found=%v err=%v", found, err)
	}
	if want := []string{"platform", "e2e5"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("targets = %v, want %v", targetNames(got), want)
	}
}

// A turn addressed to platform or to a profile carries no list: neither runs
// a session's delegate tool.
func TestAPlatformTurnCarriesNoDelegateTargets(t *testing.T) {
	r := startRig(t)
	r.g.profiles.put("e2e5", &lib.AgentCard{Name: "e2e5"})
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/t-pl", Kind: "group", AuthorID: "1001", MessageID: "pl-1", Text: "hello"}
	origin := r.awaitTask(t, "platform")
	if _, found, _ := lib.DelegateTargetsOf(messageParts(t, origin)); found {
		t.Fatal("a platform turn carried delegate targets")
	}
}

// A card's declared parameters ride with it; a card whose parameters break the
// part's rules is skipped without cutting the list short; and a list that would
// pass the byte bound stops early and says so.
func TestDelegateTargetsCarryParametersAndStayInsideTheBytes(t *testing.T) {
	d := newProfileDirectory()
	d.put("a-cluster", &lib.AgentCard{Name: "a-cluster", Parameters: clusterParams})
	d.put("b-bad", &lib.AgentCard{Name: "b-bad", Parameters: []lib.ProfileParameter{{Name: "Not-An-Identifier"}}})
	d.put("c-open", &lib.AgentCard{Name: "c-open"})
	got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
	if want := []string{"platform", "a-cluster", "c-open"}; !reflect.DeepEqual(targetNames(got), want) || got.Truncated {
		t.Fatalf("targets = %v truncated=%v, want %v untruncated", targetNames(got), got.Truncated, want)
	}
	if !reflect.DeepEqual(got.Targets[1].Parameters, clusterParams) {
		t.Fatalf("a-cluster's parameters = %+v, want %+v", got.Targets[1].Parameters, clusterParams)
	}

	big := newProfileDirectory()
	blurb := strings.Repeat("x", lib.DelegateTargetDescriptionMax)
	for i := 0; i < lib.DelegateTargetsMax-1; i++ {
		name := fmt.Sprintf("p%02d", i)
		var params []lib.ProfileParameter
		for j := 0; j < lib.TaskParametersMax; j++ {
			params = append(params, lib.ProfileParameter{Name: fmt.Sprintf("parameter_number_%02d", j), Required: true})
		}
		big.put(name, &lib.AgentCard{Name: name, Description: blurb, Parameters: params})
	}
	got = (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, big)
	if !got.Truncated || len(got.Targets) >= lib.DelegateTargetsMax {
		t.Fatalf("%d targets, truncated=%v; want fewer than the cap, truncated by bytes", len(got.Targets), got.Truncated)
	}
	if _, err := lib.DelegateTargetsPart(got); err != nil {
		t.Fatalf("the truncated list doesn't build a part: %v", err)
	}
}

// A profile pinned to a cluster takes its target from the pin, so the list
// offers none of project, location and cluster for it, only what else it takes.
func TestAPinnedProfileOffersNoTargetParameters(t *testing.T) {
	d := newProfileDirectory()
	extra := lib.ProfileParameter{Name: "namespace"}
	d.put("pinned", &lib.AgentCard{Name: "pinned", ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "web-1"},
		Parameters: append(append([]lib.ProfileParameter{}, clusterParams...), extra)})
	d.put("unpinned", &lib.AgentCard{Name: "unpinned", Parameters: clusterParams})
	got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
	params := map[string][]lib.ProfileParameter{}
	for _, target := range got.Targets {
		params[target.Name] = target.Parameters
	}
	if want := []lib.ProfileParameter{extra}; !reflect.DeepEqual(params["pinned"], want) {
		t.Fatalf("pinned offers %+v, want %+v", params["pinned"], want)
	}
	if !reflect.DeepEqual(params["unpinned"], clusterParams) {
		t.Fatalf("unpinned offers %+v, want %+v", params["unpinned"], clusterParams)
	}
}

// Everyone whose text reached the session is held to the target's lists, as
// the delegation holds them: an off-list author takes a listed profile out,
// and an incomplete set leaves nothing.
func TestDelegateTargetsHoldEveryoneInTheSession(t *testing.T) {
	d := newProfileDirectory()
	d.put("listed", &lib.AgentCard{Name: "listed"})
	d.put("open", &lib.AgentCard{Name: "open"})
	g := &Gateway{targetAllowed: map[string]map[string]map[string]bool{"listed": {"discord": {"u1": true}}}}
	u1 := TaskRequester{Backend: "discord", Subject: "u1"}
	u3 := TaskRequester{Backend: "discord", Subject: "u3"}
	if got := g.delegateTargets(u1, []TaskRequester{u1}, false, d); !reflect.DeepEqual(targetNames(got), []string{"platform", "listed", "open"}) {
		t.Fatalf("u1 alone: %v", targetNames(got))
	}
	if got := g.delegateTargets(u1, []TaskRequester{u1, u3}, false, d); !reflect.DeepEqual(targetNames(got), []string{"platform", "open"}) {
		t.Fatalf("u1 with off-list u3 in the session: %v, want listed left out", targetNames(got))
	}
	if got := g.delegateTargets(u1, nil, true, d); len(got.Targets) != 0 {
		t.Fatalf("an incomplete author set: %v, want none", targetNames(got))
	}
}

// A wake runs without the delegate tool, so its submission carries no list.
func TestAWakeCarriesNoDelegateTargets(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	rec := &SessionRecord{Key: "discord:g1/t-wk", ContextID: "ctx-wk", Kind: "group"}
	r.adapter.inbox <- InboundMessage{Conversation: rec.Key, Kind: "group", AuthorID: "1001", MessageID: "wk-1", Text: "hello"}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	r.awaitTask(t, session)
	got := waitForRecord(t, r, rec.Key, "the record", func(rec *SessionRecord) bool { return rec.BusSession == session })
	taskID, ok := r.g.startTaskWith(context.Background(), got, taskStart{Text: "the child came back", Role: taskRoleWake,
		Requester: TaskRequester{Backend: "discord", Subject: "u1"}})
	if !ok {
		t.Fatal("the wake wasn't submitted")
	}
	var wake *lib.Envelope
	waitFor(t, "the wake on the bus", func() bool {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject(session, taskID)) {
			wake = e.env
		}
		return wake != nil
	})
	if _, found, _ := lib.DelegateTargetsOf(messageParts(t, wake)); found {
		t.Fatal("a wake carried delegate targets")
	}
}

// The people the list is checked for are the people the delegation will be
// checked for: the incarnation's set with this turn's requester added, or,
// for an incarnation whose set isn't started yet (a pod the primer will
// seed), everyone still on the conversation's record.
func TestDelegateAuthorsAreWhomTheDelegationWillCheck(t *testing.T) {
	u1 := TaskRequester{Backend: "discord", Subject: "u1"}
	u2 := TaskRequester{Backend: "discord", Subject: "u2"}
	u3 := TaskRequester{Backend: "discord", Subject: "u3"}

	// A fresh incarnation: the record's earlier requester and steer author
	// count, though the set the primer seeds isn't written yet.
	fresh := &SessionRecord{BusSession: "chat-new", SessionAuthorsFor: "chat-old",
		Tasks: []TaskRef{{ID: "t1", Requester: &u2, SteerAuthors: []TaskRequester{u3}}, {ID: "t0"}}}
	got, unknown, approx := delegateAuthors(fresh, u1)
	if unknown || !approx || !slices.Contains(got, u2) || !slices.Contains(got, u3) || !slices.Contains(got, u1) {
		t.Fatalf("fresh incarnation: %v unknown=%v approximate=%v, want u1, u2 and u3, approximate", got, unknown, approx)
	}
	fresh.Tasks[0].SteerAuthorsOverflow = true
	if _, unknown, _ := delegateAuthors(fresh, u1); !unknown {
		t.Fatal("fresh incarnation with an overflowed steer set: known, want unknown")
	}

	// A running incarnation: its set, plus this turn's requester.
	live := &SessionRecord{BusSession: "chat-a", SessionAuthorsFor: "chat-a", SessionAuthors: []TaskRequester{u2},
		Tasks: []TaskRef{{ID: "t1", Requester: &u3}}}
	if got, unknown, approx := delegateAuthors(live, u1); unknown || approx || !reflect.DeepEqual(got, []TaskRequester{u2, u1}) {
		t.Fatalf("running incarnation: %v unknown=%v, want [u2 u1] (u3's turn was another incarnation's)", got, unknown)
	}

	// At the cap, a new requester makes the set unknown, as recording the
	// turn will; one already in it doesn't.
	full := &SessionRecord{BusSession: "chat-a", SessionAuthorsFor: "chat-a"}
	for i := 0; i < sessionAuthorCap; i++ {
		full.SessionAuthors = append(full.SessionAuthors, TaskRequester{Backend: "discord", Subject: fmt.Sprintf("p%d", i)})
	}
	if _, unknown, _ := delegateAuthors(full, u1); !unknown {
		t.Fatal("a new requester past the cap: known, want unknown")
	}
	if _, unknown, _ := delegateAuthors(full, full.SessionAuthors[0]); unknown {
		t.Fatal("a requester already in a full set: unknown, want known")
	}
}

// Whatever the cards, the list the gateway builds is a part it can send: a
// list cut short by the byte bound still fits once it says it was cut.
func TestEveryDelegateTargetsListBuildsAPart(t *testing.T) {
	var params []lib.ProfileParameter
	for j := 0; j < lib.TaskParametersMax; j++ {
		params = append(params, lib.ProfileParameter{Name: fmt.Sprintf("parameter_number_%02d", j), Required: true})
	}
	for blurb := 1; blurb <= lib.DelegateTargetDescriptionMax; blurb++ {
		d := newProfileDirectory()
		for i := 0; i < lib.DelegateTargetsMax; i++ {
			name := fmt.Sprintf("p%02d", i)
			d.put(name, &lib.AgentCard{Name: name, Description: strings.Repeat("x", blurb), Parameters: params})
		}
		got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, nil, false, d)
		if _, err := lib.DelegateTargetsPart(got); err != nil {
			t.Fatalf("blurb of %d bytes: %d targets, truncated=%v, and no part: %v", blurb, len(got.Targets), got.Truncated, err)
		}
	}
}

// A turn that starts a fresh incarnation is checked for the people on the
// conversation's record, whose turns the new pod's primer replays, though the
// set isn't seeded until the pod spawns.
func TestAFreshIncarnationsListCountsTheRecordsPeople(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	me := TaskRequester{Backend: "discord", Subject: requesterSubject(r.g.ps, "discord", "1001")}
	offList := TaskRequester{Backend: "discord", Subject: "someone-else"}
	r.g.profiles.put("listed", &lib.AgentCard{Name: "listed"})
	r.g.profiles.put("open", &lib.AgentCard{Name: "open"})
	r.g.targetAllowed = map[string]map[string]map[string]bool{"listed": {"discord": {me.Subject: true}}}
	conv := "discord:g1/t-fresh"
	// The earlier incarnation is gone (reaped): no bus session or pod, its
	// turn still on record.
	rec := &SessionRecord{Key: conv, ContextID: "ctx-fresh", Kind: "group", Addressee: "platform",
		Tasks: []TaskRef{{ID: "task-earlier", Addressee: "chat-old-1", Requester: &offList, Request: "an earlier ask"}}}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "fr-1", Text: "use listed"}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	got, found, err := lib.DelegateTargetsOf(messageParts(t, origin))
	if err != nil || !found {
		t.Fatalf("delegateTargets part: found=%v err=%v", found, err)
	}
	if slices.Contains(targetNames(got), "listed") {
		t.Fatalf("targets = %v: listed offered though someone off its list spoke in a turn the pod replays", targetNames(got))
	}
}

// A fresh incarnation whose record holds more people than the cap gets no
// list at all, so its tool falls back to platform: the primer may keep fewer
// turns than the record holds, and the pod's seeded set may be under the cap.
func TestAFreshIncarnationPastTheCapSendsNoList(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/t-busy"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-busy", Kind: "group", Addressee: "platform"}
	for i := 0; i <= sessionAuthorCap; i++ {
		p := TaskRequester{Backend: "discord", Subject: fmt.Sprintf("person-%d", i)}
		rec.Tasks = append(rec.Tasks, TaskRef{ID: fmt.Sprintf("task-busy-%d", i), Addressee: "chat-old-1", Requester: &p, Request: "an ask"})
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "busy-1", Text: "hello"}
	waitFor(t, "the session spawned", func() bool { return len(spawn.calls()) == 1 })
	origin := r.awaitTask(t, spawn.calls()[0].Session)
	if _, found, _ := lib.DelegateTargetsOf(messageParts(t, origin)); found {
		t.Fatal("a fresh incarnation past the cap carried a list; want none, so the tool names platform")
	}
}
