package gateway

import (
	"context"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The install scope the profile rig runs with (startProfileRig).
const chainTestNamespace = "kubeagents-system"

var (
	installScope = string(capability.NamespaceScope(chainTestNamespace))
	prodScope    = "project/p1/location/us-east1/cluster/prod"
	devScope     = "project/p1/location/us-east1/cluster/dev"
)

func chainTestGateway() *Gateway {
	return &Gateway{cfg: &Config{Namespace: chainTestNamespace}}
}

func TestAProfileParentWithoutARecordedScopeOrAtUnattendedMayNotDelegate(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		parent TaskRef
		rule   string
	}{
		{"scope not on record", TaskRef{Addressee: "chat", Tier: "read"}, ruleDelegationParentScopeUnknown},
		{"unattended", TaskRef{Addressee: "chat", Tier: "unattended", Scope: installScope}, ruleDelegationUnattendedParent},
		{"read with a scope", TaskRef{Addressee: "chat", Tier: "read", Scope: installScope}, ""},
	}
	for _, tc := range cases {
		if rule, _ := profileParentRefusal(tc.parent); rule != tc.rule {
			t.Errorf("%s: rule %q, want %q", tc.name, rule, tc.rule)
		}
	}
}

func TestNoProfileTwiceOnAChainButTheHeadMayNestItselfOnce(t *testing.T) {
	t.Parallel()
	g := chainTestGateway()
	cases := []struct {
		name  string
		chain []string // the parent's carried chain
		par   string   // the parent's profile
		child string   // the child's resolved profile
		rule  string
	}{
		{"head nests itself once", nil, "a", "a", ""},
		{"a child doesn't nest its own profile", []string{"a"}, "a", "a", ruleDelegationProfileCycle},
		{"A → B → A", []string{"a"}, "b", "a", ruleDelegationProfileCycle},
		{"A → B → C", []string{"a"}, "b", "c", ""},
		{"A → A → B (head's nest, then another)", []string{"a"}, "a", "b", ""},
		{"A → B → C → B", []string{"a", "b"}, "c", "b", ruleDelegationProfileCycle},
	}
	for _, tc := range cases {
		parent := TaskRef{ID: "p", Addressee: tc.par, Tier: "read", Scope: installScope, ProfileChain: tc.chain}
		if len(tc.chain) > 0 {
			// A parent with a chain is a child task; its own parent's entry is
			// left out, as if evicted, so the chain rule alone is under test.
			parent.Role, parent.ParentTaskID, parent.Depth = taskRoleChild, "evicted", len(tc.chain)
		}
		rec := &SessionRecord{Tasks: []TaskRef{parent}}
		rule, notice := g.profileChildRefusal(rec, parent, &profileRoute{Profile: tc.child, Scope: capability.Scope(prodScope)})
		if rule != tc.rule {
			t.Errorf("%s: rule %q (%s), want %q", tc.name, rule, notice, tc.rule)
		}
		if rule == ruleDelegationProfileCycle && !strings.Contains(notice, tc.child+" is already on this task's chain") {
			t.Errorf("%s: notice %q doesn't name the child and the chain", tc.name, notice)
		}
	}
}

func TestAChildNeverReachesOutsideItsProfileParentsScope(t *testing.T) {
	t.Parallel()
	g := chainTestGateway()
	cases := []struct {
		name        string
		parent, cld string
		rule        string
	}{
		{"same cluster", prodScope, prodScope, ""},
		{"another cluster", prodScope, devScope, ruleDelegationParentScope},
		{"targetless child under a cluster-scoped parent", prodScope, installScope, ruleDelegationParentScope},
		{"install-scoped parent, any cluster the install admits", installScope, devScope, ""},
		{"install-scoped parent, targetless child", installScope, installScope, ""},
		{"another namespace is not the install's", "namespace/other", devScope, ruleDelegationParentScope},
	}
	for _, tc := range cases {
		parent := TaskRef{ID: "p", Addressee: "a", Tier: "read", Scope: tc.parent}
		rec := &SessionRecord{Tasks: []TaskRef{parent}}
		rule, _ := g.profileChildRefusal(rec, parent, &profileRoute{Profile: "b", Scope: capability.Scope(tc.cld)})
		if rule != tc.rule {
			t.Errorf("%s: rule %q, want %q", tc.name, rule, tc.rule)
		}
	}
}

func TestTheCarriedChainIsHeldToTheAncestorsTheRecordStillHas(t *testing.T) {
	t.Parallel()
	g := chainTestGateway()
	route := &profileRoute{Profile: "c", Scope: capability.Scope(prodScope)}
	top := TaskRef{ID: "t-a", Addressee: "a", Tier: "read", Scope: installScope}
	b := TaskRef{ID: "t-b", Addressee: "b", Tier: "read", Scope: installScope, Role: taskRoleChild, ParentTaskID: "t-a", ProfileChain: []string{"a"}, Depth: 1}
	cases := []struct {
		name   string
		tasks  []TaskRef
		parent TaskRef
		rule   string
	}{
		{"every ancestor present and agreeing", []TaskRef{top, b}, b, ""},
		{"the top evicted: the carried chain stands", []TaskRef{b}, b, ""},
		{"an ancestor contradicting the chain", []TaskRef{{ID: "t-a", Addressee: "x", Tier: "read", Scope: installScope}, b}, b, ruleDelegationParentScopeUnknown},
		{"a chain longer than the present top allows", []TaskRef{top, {ID: "t-b", Addressee: "b", Tier: "read", Scope: installScope, Role: taskRoleChild,
			ParentTaskID: "t-a", ProfileChain: []string{"z", "a"}, Depth: 1}},
			TaskRef{ID: "t-b", Addressee: "b", Tier: "read", Scope: installScope, Role: taskRoleChild, ParentTaskID: "t-a", ProfileChain: []string{"z", "a"}, Depth: 1},
			ruleDelegationParentScopeUnknown},
	}
	for _, tc := range cases {
		rec := &SessionRecord{Tasks: tc.tasks}
		if rule, notice := g.profileChildRefusal(rec, tc.parent, route); rule != tc.rule {
			t.Errorf("%s: rule %q (%s), want %q", tc.name, rule, notice, tc.rule)
		}
	}
}

// A profile task delegated from a session turn, or from the wake after an
// earlier child ended, heads its chain: session → A1 → wake → A2 → B is
// legal, because A1 isn't waiting on anything when A2 runs.
func TestASessionOrWakeParentStartsTheChainAfresh(t *testing.T) {
	t.Parallel()
	g := chainTestGateway()
	session := TaskRef{ID: "t-s", Addressee: "session-pod"}
	a1 := TaskRef{ID: "t-a1", Addressee: "a", Tier: "read", Scope: installScope, Role: taskRoleChild, ParentTaskID: "t-s", Depth: 1}
	wake := TaskRef{ID: "t-w", Addressee: "session-pod", Role: taskRoleWake, ParentTaskID: "t-a1", Depth: 1}
	a2 := TaskRef{ID: "t-a2", Addressee: "a", Tier: "read", Scope: installScope, Role: taskRoleChild, ParentTaskID: "t-w", Depth: 2}
	rec := &SessionRecord{Tasks: []TaskRef{session, a1, wake, a2}}
	for _, parent := range []TaskRef{a1, a2} {
		if rule, notice := g.profileChildRefusal(rec, parent, &profileRoute{Profile: "b", Scope: capability.Scope(prodScope)}); rule != "" {
			t.Errorf("%s → b: refused %q (%s)", parent.ID, rule, notice)
		}
		// Each heads its chain, so it may nest its own profile once.
		if rule, _ := g.profileChildRefusal(rec, parent, &profileRoute{Profile: "a", Scope: capability.Scope(prodScope)}); rule != "" {
			t.Errorf("%s → a: refused %q, want the head's self-nest admitted", parent.ID, rule)
		}
	}
	if got := childProfileChain(session, false); got != nil {
		t.Errorf("a session parent's child carries %v, want no chain", got)
	}
	if got := childProfileChain(a2, true); len(got) != 1 || got[0] != "a" {
		t.Errorf("a2's child carries %v, want [a]", got)
	}
}

// The cycle check reads the profile the address resolved to: a generic
// profile that yields to the one pinned to the target comes back as the
// pinned one, and is checked as that.
func TestAGenericAddressThatResolvesToAProfileOnTheChainIsACycle(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	route, err := r.g.profileRouteFor("cluster", targetParams("p1", "us-east1", "prod"), capability.TierRead, lib.SubmitKindChild)
	if err != nil {
		t.Fatal(err)
	}
	if route.Profile != "prod-east" {
		t.Fatalf("cluster at prod resolved to %q, want prod-east", route.Profile)
	}
	parent := TaskRef{ID: "t-pe", Addressee: "prod-east", Tier: "read", Scope: prodScope, Role: taskRoleChild,
		ParentTaskID: "t-chat", ProfileChain: []string{"chat"}, Depth: 1}
	rec := &SessionRecord{Tasks: []TaskRef{parent}}
	if rule, _ := r.g.profileChildRefusal(rec, parent, route); rule != ruleDelegationProfileCycle {
		t.Fatalf("rule %q, want %q", rule, ruleDelegationProfileCycle)
	}
}

// End to end: a profile task records its scope at mint, its child records
// the chain and its own scope, and a parent whose scope isn't on record is
// refused with nothing minted.
func TestAProfileParentsChildCarriesTheChainAndBothRecordTheirScopes(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	ctx := context.Background()
	conv := "console:tab-chain"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	top, ok := rec.TaskRefFor(origin.TaskID)
	if !ok || top.Scope != installScope || len(top.ProfileChain) != 0 {
		t.Fatalf("top task scope %q chain %v, want %q and none", top.Scope, top.ProfileChain, installScope)
	}

	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "prod-east", "look at prod", nil)); err != nil {
		t.Fatal(err)
	}
	child := submissionOn(t, r, "prod-east")
	rec, _ = r.g.reg.Get(ctx, conv)
	ref, ok := rec.TaskRefFor(child.TaskID)
	if !ok || ref.Scope != prodScope || len(ref.ProfileChain) != 1 || ref.ProfileChain[0] != "chat" {
		t.Fatalf("child scope %q chain %v, want %q and [chat]", ref.Scope, ref.ProfileChain, prodScope)
	}
}

func TestAProfileParentWhoseScopeIsNotOnRecordMintsNothing(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	ctx := context.Background()
	conv := "console:tab-noscope"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	// As a record written before the scope was recorded.
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for i := range rec.Tasks {
			if rec.Tasks[i].ID == origin.TaskID {
				rec.Tasks[i].Scope = ""
			}
		}
	})
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "prod-east", "look at prod", nil)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the refusal on the parent's entry", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && strings.Contains(ref.DelegationEnd, noticeParentScopeUnknown)
	})
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"prod-east.>"); len(envs) != 0 {
		t.Fatalf("%d submission(s) for a parent whose scope isn't on record", len(envs))
	}
}
