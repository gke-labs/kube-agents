package gateway

import (
	"fmt"
	"reflect"
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
	got := g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, d)
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
	got := g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, d)
	if want := []string{"open"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("u1's targets = %v, want %v (listed and platform hold only u2; writers is write-tier)", targetNames(got), want)
	}
	got = g.delegateTargets(TaskRequester{Backend: "discord", Subject: "u2"}, d)
	if want := []string{"platform", "listed", "open"}; !reflect.DeepEqual(targetNames(got), want) {
		t.Fatalf("u2's targets = %v, want %v", targetNames(got), want)
	}
	// The A2A door delegates only under a list of its own.
	got = g.delegateTargets(TaskRequester{Backend: a2aBackend, Subject: "u2"}, d)
	if len(got.Targets) != 0 {
		t.Fatalf("door caller's targets = %v, want none", targetNames(got))
	}
	// No attested identity reaches no profile.
	got = (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord"}, d)
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
	got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, d)
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
	got := (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, d)
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
	got = (&Gateway{}).delegateTargets(TaskRequester{Backend: "discord", Subject: "u1"}, big)
	if !got.Truncated || len(got.Targets) >= lib.DelegateTargetsMax {
		t.Fatalf("%d targets, truncated=%v; want fewer than the cap, truncated by bytes", len(got.Targets), got.Truncated)
	}
	if _, err := lib.DelegateTargetsPart(got); err != nil {
		t.Fatalf("the truncated list doesn't build a part: %v", err)
	}
}
