package gateway

import (
	"bytes"
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startDelegateTargetsRig is the profile rig (startProfileRig) with the
// profiles the list tests read: chat delegates and takes no target, dev-east
// is pinned to p1/us-east1/dev, needs takes a required service and no target,
// and writer runs only unattended, a tier no console task has.
func startDelegateTargetsRig(t *testing.T) *rig {
	t.Helper()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.profiles.put("dev-east", &lib.AgentCard{Name: "dev-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "dev"}})
	r.g.profiles.put("needs", &lib.AgentCard{Name: "needs", Parameters: []lib.ProfileParameter{{Name: "service", Required: true}}})
	r.g.profiles.put("writer", &lib.AgentCard{Name: "writer", Tiers: []string{string(capability.TierUnattended)}})
	return r
}

// rootOn starts a console task for profile in conv and returns its
// submission, found by the task id the record holds, so several
// conversations can share a rig.
func rootOn(t *testing.T, r *rig, conv, profile string) *lib.Envelope {
	t.Helper()
	turn := profileTurn(conv, profile, nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	rec := waitForRecord(t, r, conv, "the task on record", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	return submissionFor(t, r, profile, rec.ActiveTask.TaskID)
}

// submissionFor waits for taskID's submission on profile's queue.
func submissionFor(t *testing.T, r *rig, profile, taskID string) *lib.Envelope {
	t.Helper()
	var env *lib.Envelope
	waitFor(t, "the submission of "+taskID, func() bool {
		for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+profile+".>") {
			if e.env.TaskID == taskID {
				env = e.env
				return true
			}
		}
		return false
	})
	return env
}

// targetsOn is the delegateTargets part on a submission, failing the test
// when there is none.
func targetsOn(t *testing.T, env *lib.Envelope) lib.DelegateTargets {
	t.Helper()
	got, ok, err := lib.DelegateTargetsOf(messageParts(t, env))
	if !ok || err != nil {
		t.Fatalf("delegateTargets part: found=%v err=%v", ok, err)
	}
	return got
}

// delegateFrom starts a profile root in conv, delegates e from it, and
// reports whether the delegation minted a child, with the parent's id and the
// child's when it did.
func delegateFrom(t *testing.T, r *rig, conv, profile string, e lib.DelegateTask) (minted bool, parentID, childID string) {
	t.Helper()
	ctx := context.Background()
	origin := rootOn(t, r, conv, profile)
	parent := r.execFor(t, origin, profile)
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, e.Addressee, e.Text, e.Parameters)); err != nil {
		t.Fatal(err)
	}
	rec := waitForRecord(t, r, conv, "the delegation's outcome", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && (len(ref.Children) > 0 || ref.DelegationEnd != "")
	})
	ref, _ := rec.TaskRefFor(origin.TaskID)
	if len(ref.Children) > 0 {
		return true, origin.TaskID, ref.Children[0]
	}
	return false, origin.TaskID, ""
}

// TestADelegatingProfileRootListsWhatTheDelegationAccepts: a delegating
// profile's own task carries the profiles it may delegate to (#6789), and the
// list is the delegation's rules, not a guess: every target listed is
// accepted when the parent delegates to it, and one left off is refused. The
// width is the setting the delegation refuses a wider batch under.
func TestADelegatingProfileRootListsWhatTheDelegationAccepts(t *testing.T) {
	t.Parallel()
	r := startDelegateTargetsRig(t)
	type entry struct {
		name   string
		params map[string]string
	}
	cases := []struct {
		parent   string
		listed   []entry // in name order, each with the parameters a delegation passes
		unlisted []entry
	}{
		{
			// The install's scope: any profile it reaches, at the parent's
			// tier, its own profile once (the head's self-nest).
			parent: "chat",
			listed: []entry{
				{"chat", nil},
				{"cluster", targetParams("p1", "us-east1", "staging")},
				{"dev-east", nil},
				{"needs", map[string]string{"service": "web"}},
				{"prod-east", nil},
			},
			unlisted: []entry{{"writer", nil}, {targetPlatform, nil}},
		},
		{
			// Scoped to p1/us-east1/prod: only what runs on that cluster.
			parent: "prod-east",
			listed: []entry{
				{"cluster", targetParams("p1", "us-east1", "prod")},
				{"prod-east", nil},
			},
			unlisted: []entry{
				{"chat", nil},
				{"dev-east", nil},
				{"needs", map[string]string{"service": "web"}},
				{"writer", nil},
			},
		},
	}
	for _, tc := range cases {
		got := targetsOn(t, rootOn(t, r, "console:tab-list-"+tc.parent, tc.parent))
		var want []string
		for _, e := range tc.listed {
			want = append(want, e.name)
		}
		if names := targetNames(got); !slices.Equal(names, want) {
			t.Fatalf("%s's targets = %v, want %v", tc.parent, names, want)
		}
		if got.WidthMax != r.g.cfg.DelegationWidthMax || got.Truncated {
			t.Fatalf("%s's width = %d (truncated %v), want %d", tc.parent, got.WidthMax, got.Truncated, r.g.cfg.DelegationWidthMax)
		}
		// At the one round there is by default the part carries no
		// rounds, as a worker built before rounds reads it.
		if got.RoundsMax != 0 || got.Rounds() != 1 {
			t.Fatalf("%s's rounds = %d, want none advertised at the default of 1", tc.parent, got.RoundsMax)
		}
		for _, e := range tc.listed {
			conv := "console:tab-listed-" + tc.parent + "-" + e.name
			if minted, _, _ := delegateFrom(t, r, conv, tc.parent, lib.DelegateTask{Addressee: e.name, Text: "look", Parameters: e.params}); !minted {
				rec, _ := r.g.reg.Get(context.Background(), conv)
				t.Errorf("%s -> %s is listed but was refused: %+v", tc.parent, e.name, rec.Tasks)
			}
		}
		for _, e := range tc.unlisted {
			conv := "console:tab-unlisted-" + tc.parent + "-" + e.name
			if minted, _, _ := delegateFrom(t, r, conv, tc.parent, lib.DelegateTask{Addressee: e.name, Text: "look", Parameters: e.params}); minted {
				t.Errorf("%s -> %s is not listed but was accepted", tc.parent, e.name)
			}
		}
	}
}

// TestAProfileParentTheDelegationRefusesOutrightListsNobody: a parent with
// no scope on its record (profileParentRefusal) or at the depth cap is
// refused every child, so its list is empty, never a profile it can't reach.
func TestAProfileParentTheDelegationRefusesOutrightListsNobody(t *testing.T) {
	t.Parallel()
	r := startDelegateTargetsRig(t)
	rec := &SessionRecord{Key: "console:tab-unattended"}
	requester := TaskRequester{Backend: consoleBackend, Subject: "hmac:00"}
	for _, parent := range []TaskRef{
		{Addressee: "chat", Tier: string(capability.TierRead), Scope: "", Requester: &requester},
		{Addressee: "chat", Tier: string(capability.TierRead), Scope: installScope, Requester: &requester, Depth: r.g.cfg.DelegationDepthMax},
	} {
		if got := r.g.profileDelegateTargets(rec, parent); len(got.Targets) != 0 {
			t.Errorf("parent %+v lists %v, want nobody", parent, targetNames(got))
		}
	}
	// And the same parent at read is offered the profiles, so the empty
	// lists above are the refusal's, not the rig's.
	read := TaskRef{Addressee: "chat", Tier: string(capability.TierRead), Scope: installScope, Requester: &requester}
	if got := r.g.profileDelegateTargets(rec, read); len(got.Targets) == 0 {
		t.Fatal("a read parent lists nobody")
	}
}

// TestAProfileParentsListAdvertisesRoundsOverOne: the part carries the
// delegation rounds only over one, so at the default it is the shape a worker
// built before rounds reads, with no roundsMax on the wire. Over one is set
// on the config directly here: New clamps it to 1 until later rounds land.
func TestAProfileParentsListAdvertisesRoundsOverOne(t *testing.T) {
	t.Parallel()
	r := startDelegateTargetsRig(t)
	rec := &SessionRecord{Key: "console:tab-rounds"}
	requester := TaskRequester{Backend: consoleBackend, Subject: "hmac:00"}
	parent := TaskRef{Addressee: "chat", Tier: string(capability.TierRead), Scope: installScope, Requester: &requester}
	if r.g.cfg.DelegationRoundsMax != 1 {
		t.Fatalf("the rig's rounds = %d, want New's default of 1", r.g.cfg.DelegationRoundsMax)
	}
	part, err := lib.DelegateTargetsPart(r.g.profileDelegateTargets(rec, parent))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(part.Data), "roundsMax") {
		t.Fatalf("the part at one round carries roundsMax: %s", part.Data)
	}
	g := &Gateway{cfg: &Config{DelegationRoundsMax: 3}}
	if got := g.advertisedRounds(); got != 3 {
		t.Fatalf("advertised rounds at 3 = %d, want 3", got)
	}
	for _, n := range []int{0, 1} {
		g.cfg.DelegationRoundsMax = n
		if got := g.advertisedRounds(); got != 0 {
			t.Fatalf("advertised rounds at %d = %d, want none", n, got)
		}
	}
}

// TestAProfileSubmissionWithoutAListIsAsBefore: a profile whose card doesn't
// delegate, and a child of one that does, are submitted byte for byte as
// they were before the list (#6789): no delegateTargets part, nothing else
// moved. A worker from before the part reads the same submission either way.
func TestAProfileSubmissionWithoutAListIsAsBefore(t *testing.T) {
	t.Parallel()
	r := startDelegateTargetsRig(t)
	// Rounds over one, set past New's clamp, move nothing either: they ride
	// only the list.
	r.g.cfg.DelegationRoundsMax = 3
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	asBefore := func(what string, env *lib.Envelope, params map[string]string, tier capability.Tier, kind string) {
		t.Helper()
		parts := messageParts(t, env)
		if _, ok, _ := lib.DelegateTargetsOf(parts); ok {
			t.Fatalf("%s carries a delegateTargets part", what)
		}
		if _, ok, _ := lib.ScopeClustersOf(parts); ok {
			t.Fatalf("%s carries a scopeClusters part", what)
		}
		route, err := r.g.profileRouteFor("prod-east", params, tier, kind)
		if err != nil {
			t.Fatal(err)
		}
		want, err := submissionPayload(parts[0].Text, env.TaskID, env.ContextID, route, false)
		if err != nil {
			t.Fatal(err)
		}
		// The one field minted afresh per build is the message id.
		var gotMsg, wantMsg lib.Message
		if err := json.Unmarshal(env.Payload, &gotMsg); err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(want, &wantMsg); err != nil {
			t.Fatal(err)
		}
		want = bytes.Replace(want, []byte(`"messageId":"`+wantMsg.MessageID+`"`), []byte(`"messageId":"`+gotMsg.MessageID+`"`), 1)
		if !bytes.Equal(env.Payload, want) {
			t.Fatalf("%s's payload =\n%s\nwant\n%s", what, env.Payload, want)
		}
	}

	conv := "console:tab-asbefore-root"
	root := rootOn(t, r, conv, "prod-east")
	rec, _ := r.g.reg.Get(context.Background(), conv)
	ref, _ := rec.TaskRefFor(root.TaskID)
	asBefore("a root of a profile that doesn't delegate", root, nil, capability.Tier(ref.Tier), lib.SubmitKindTop)

	// A child of a delegating parent, to a profile that delegates too: a
	// child has a via, so it carries no list.
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams, Delegates: true,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	conv = "console:tab-asbefore-child"
	minted, parentID, childID := delegateFrom(t, r, conv, "chat", lib.DelegateTask{Addressee: "prod-east", Text: "look"})
	if !minted {
		t.Fatal("the child was refused")
	}
	rec, _ = r.g.reg.Get(context.Background(), conv)
	parentRef, _ := rec.TaskRefFor(parentID)
	asBefore("a child", submissionFor(t, r, "prod-east", childID), nil, capability.Tier(parentRef.Tier), lib.SubmitKindChild)
}

// TestAScheduledRunListsWhatItsDelegationAccepts is the one source of truth
// for a scheduled run (#6744, #6789): a run of a delegating profile carries a
// delegateTargets part, and the list it carries and the delegation it makes
// run the same check (delegateEntryCheck), so the list holds exactly the
// profiles that run at read and list unattended, each of them is accepted,
// and each profile left off is refused.
func TestAScheduledRunListsWhatItsDelegationAccepts(t *testing.T) {
	t.Parallel()
	type entry struct {
		name   string
		params map[string]string
	}
	// The trigger rig's fleet-audit (read, unattended; delegates), cluster
	// (read, unattended; takes a target) and readonly (read), and these.
	cards := []*lib.AgentCard{
		{Name: "reader", Tiers: []string{"read", "unattended"}},
		{Name: "writer", Tiers: []string{"oncall", "unattended"}},
		{Name: "legacy"},
	}
	listed := []entry{
		{"cluster", targetParams("p1", "us-east1", "prod")},
		{"fleet-audit", nil},
		{"reader", nil},
	}
	unlisted := []entry{{"legacy", nil}, {"readonly", nil}, {"writer", nil}, {targetPlatform, nil}}
	key := "trigger:schedule/kubeagents-system/compliance-audit"
	const scheduledWidth = 4
	start := func(t *testing.T) (*triggerRig, lib.Envelope, *lib.TaskExecution) {
		t.Helper()
		tr := startTriggerRig(t, func(c *Config) { c.DelegationWidthMax = scheduledWidth })
		for _, c := range cards {
			tr.g.profiles.put(c.Name, c)
		}
		origin, parent := startScheduledParent(t, tr)
		return tr, origin, parent
	}

	t.Run("list", func(t *testing.T) {
		t.Parallel()
		tr, origin, _ := start(t)
		var want []string
		for _, e := range listed {
			want = append(want, e.name)
		}
		// The list rides the run's own submission (publishTriggerRun), with
		// the width the delegation refuses a wider batch under.
		got := targetsOn(t, &origin)
		if names := targetNames(got); !slices.Equal(names, want) {
			t.Fatalf("the scheduled run's submitted targets = %v, want %v", names, want)
		}
		if got.WidthMax != scheduledWidth || got.WidthMax != tr.g.advertisedWidth() || got.Truncated {
			t.Fatalf("the scheduled run's width = %d (truncated %v), want %d", got.WidthMax, got.Truncated, scheduledWidth)
		}
		// And it is the list the run's recorded entry is offered.
		rec, err := tr.g.reg.Get(context.Background(), key)
		if err != nil || rec == nil {
			t.Fatalf("the run's record: %v", err)
		}
		parent, ok := rec.TaskRefFor(origin.TaskID)
		if !ok || !parent.belowSchedule() {
			t.Fatalf("the run's entry = %+v, want one below a schedule", parent)
		}
		if names := targetNames(tr.g.profileDelegateTargets(rec, parent)); !slices.Equal(names, want) {
			t.Fatalf("the scheduled run's entry is offered %v, want %v", names, want)
		}
	})
	for _, tc := range []struct {
		entries []entry
		minted  bool
	}{{listed, true}, {unlisted, false}} {
		for _, e := range tc.entries {
			t.Run(e.name, func(t *testing.T) {
				t.Parallel()
				tr, origin, parent := start(t)
				ctx := context.Background()
				if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, e.name, "look", e.params)); err != nil {
					t.Fatal(err)
				}
				var end string
				var children int
				waitFor(t, "the delegation's outcome", func() bool {
					rec, _ := tr.g.reg.Get(ctx, key)
					if rec == nil {
						return false
					}
					ref, ok := rec.TaskRefFor(origin.TaskID)
					end, children = ref.DelegationEnd, len(ref.Children)
					return ok && (children > 0 || end != "")
				})
				if minted := children > 0; minted != tc.minted {
					t.Fatalf("delegating to %s: minted %v (end %q), want minted %v", e.name, minted, end, tc.minted)
				}
			})
		}
	}
}

// TestAScheduledRunOfANonDelegatingProfileIsAsBefore: a run of a profile
// whose card doesn't delegate carries no delegateTargets part, and its
// submission is byte for byte what publishTriggerRun built before the list
// (#6789).
func TestAScheduledRunOfANonDelegatingProfileIsAsBefore(t *testing.T) {
	t.Parallel()
	tr := startTriggerRig(t, func(c *Config) { c.DelegationWidthMax = 4 })
	tr.g.cfg.DelegationRoundsMax = 4 // past New's clamp; it rides only the list
	tr.g.profiles.put("fleet-audit", &lib.AgentCard{Name: "fleet-audit", Tiers: []string{"read", "unattended"}})
	origin, _ := startScheduledParent(t, tr)
	parts := messageParts(t, &origin)
	if _, ok, _ := lib.DelegateTargetsOf(parts); ok {
		t.Fatal("a non-delegating profile's scheduled run carries a delegateTargets part")
	}
	if _, ok, _ := lib.ScopeClustersOf(parts); ok {
		t.Fatal("a non-delegating profile's scheduled run carries a scopeClusters part")
	}
	s := goodScheduleSubmit()
	route, err := tr.g.profileRouteFor(s.Profile, s.Parameters, capability.TierUnattended, lib.SubmitKindTop)
	if err != nil {
		t.Fatal(err)
	}
	want, err := submissionPayload(triggerTaskText(s), origin.TaskID, origin.ContextID, route, false)
	if err != nil {
		t.Fatal(err)
	}
	// The one field minted afresh per build is the message id.
	var gotMsg, wantMsg lib.Message
	if err := json.Unmarshal(origin.Payload, &gotMsg); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(want, &wantMsg); err != nil {
		t.Fatal(err)
	}
	want = bytes.Replace(want, []byte(`"messageId":"`+wantMsg.MessageID+`"`), []byte(`"messageId":"`+gotMsg.MessageID+`"`), 1)
	if !bytes.Equal(origin.Payload, want) {
		t.Fatalf("the run's payload =\n%s\nwant\n%s", origin.Payload, want)
	}
}
