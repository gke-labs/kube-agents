package gateway

import (
	"context"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The rounds tests set DelegationRoundsMax past the clamp New applies (one,
// until raising it lands with its own eval): they exercise the record's round
// handling the cap at one never reaches.

// clusterSubmissions is every submission to the cluster profile so far.
func clusterSubmissions(t *testing.T, r *rig) []*lib.Envelope {
	t.Helper()
	var out []*lib.Envelope
	for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>") {
		out = append(out, e.env)
	}
	return out
}

func twoClusterBatch(t *testing.T, a, b string) lib.Artifact {
	t.Helper()
	return delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "look at " + a, Parameters: targetParams("p1", "us-east1", a)},
		lib.DelegateTask{Addressee: "cluster", Text: "look at " + b, Parameters: targetParams("p1", "us-east1", b)})
}

// TestAProfileParentsSecondRoundAfterABatch: with rounds left, a profile
// parent whose batch has every end may delegate again. The new batch is round
// two: its children record it, their ends carry Round 2 with entries from 1,
// and the first round's children move to EarlierChildren.
func TestAProfileParentsSecondRoundAfterABatch(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-rounds-second-batch"
	parent, origin, first := profileBatchParent(t, r, conv, "a", "b")
	r.g.cfg.DelegationRoundsMax = 2
	completeChild(t, r, first[0], "a is fine")
	completeChild(t, r, first[1], "b is fine")
	waitFor(t, "round one's two ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
		if c.Round != 0 {
			t.Fatalf("round one's end carries Round %d, want it omitted", c.Round)
		}
	}
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "c", "d")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "round two's children submitted", func() bool { return len(clusterSubmissions(t, r)) == 4 })
	rec, _ := r.g.reg.Get(ctx, conv)
	p, _ := rec.TaskRefFor(origin.TaskID)
	if p.DelegationRound != 2 || len(p.Children) != 2 || len(p.EarlierChildren) != 2 || p.BatchSize != 2 {
		t.Fatalf("parent after round two = round %d children %v earlier %v size %d", p.DelegationRound, p.Children, p.EarlierChildren, p.BatchSize)
	}
	var second []*lib.Envelope
	for _, e := range clusterSubmissions(t, r) {
		if e.TaskID != first[0].TaskID && e.TaskID != first[1].TaskID {
			second = append(second, e)
		}
	}
	for _, k := range second {
		ref, _ := rec.TaskRefFor(k.TaskID)
		if ref.BatchRound != 2 {
			t.Fatalf("round two child %s records round %d", k.TaskID, ref.BatchRound)
		}
		completeChild(t, r, k, "done")
	}
	waitFor(t, "round two's two ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 4 })
	entries := map[int]bool{}
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID)[2:] {
		if c.Round != 2 {
			t.Fatalf("round two's end = %+v, want Round 2", c)
		}
		entries[c.Entry] = true
	}
	if !entries[1] || !entries[2] {
		t.Fatalf("round two's entries = %v, want 1 and 2", entries)
	}
}

// TestARoundIsBusyWhileTheLastOneRuns: with rounds left, a request while the
// round's children still run is no new round: nothing is minted, and the
// stored round doesn't move.
func TestARoundIsBusyWhileTheLastOneRuns(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-rounds-busy"
	parent, origin, first := profileBatchParent(t, r, conv, "a", "b")
	r.g.cfg.DelegationRoundsMax = 2
	completeChild(t, r, first[0], "a is fine")
	waitFor(t, "one end", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 1 })
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "c", "d")); err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil { // a marker after it
		t.Fatal(err)
	}
	time.Sleep(500 * time.Millisecond)
	if n := len(clusterSubmissions(t, r)); n != 2 {
		t.Fatalf("submissions = %d, want the first round's 2 only", n)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if p, _ := rec.TaskRefFor(origin.TaskID); p.DelegationRound != 1 || len(p.EarlierChildren) != 0 {
		t.Fatalf("parent = round %d earlier %v, want still round 1", p.DelegationRound, p.EarlierChildren)
	}
}

// TestARefusedFirstRoundLeavesTheSecondAndThePastCapAskStoresNothing: a
// refused request uses its round (round one, told with no Round); the next
// request mints round two; asks past the cap are refused, tagged past the
// round, and leave the stored round where it was however many come.
func TestARefusedFirstRoundLeavesTheSecondAndThePastCapAskStoresNothing(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationRoundsMax = 2
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	ctx := context.Background()
	conv := "console:tab-rounds-refused-first"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	r.g.cfg.DelegationWidthMax = 1 // two tasks are too wide: refused
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "a", "b")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "round one refused", func() bool { return len(refusalsOn(t, r, "chat", origin.TaskID)) == 1 })
	if got := refusalsOn(t, r, "chat", origin.TaskID)[0]; got.Round != 0 {
		t.Fatalf("round one's refusal carries Round %d, want it omitted", got.Round)
	}
	r.g.cfg.DelegationWidthMax = 10
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "c", "d")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "round two minted", func() bool { return len(clusterSubmissions(t, r)) == 2 })
	rec, _ := r.g.reg.Get(ctx, conv)
	p, _ := rec.TaskRefFor(origin.TaskID)
	if p.DelegationRound != 2 || p.DelegationEnd != "" || len(p.Children) != 2 {
		t.Fatalf("parent after round two = round %d end %q children %v", p.DelegationRound, p.DelegationEnd, p.Children)
	}
	for _, k := range clusterSubmissions(t, r) {
		completeChild(t, r, k, "done")
	}
	waitFor(t, "round two's ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	// Three asks past the cap: each refused under delegation.rounds, tagged
	// round 3, none stored.
	for i := 0; i < 3; i++ {
		if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "e", "f")); err != nil {
			t.Fatal(err)
		}
	}
	waitFor(t, "three past-cap refusals", func() bool { return len(refusalsOn(t, r, "chat", origin.TaskID)) == 4 })
	for _, got := range refusalsOn(t, r, "chat", origin.TaskID)[1:] {
		if got.Round != 3 {
			t.Fatalf("past-cap refusal carries Round %d, want 3", got.Round)
		}
	}
	rec, _ = r.g.reg.Get(ctx, conv)
	if p, _ := rec.TaskRefFor(origin.TaskID); p.DelegationRound != 2 {
		t.Fatalf("stored round after past-cap asks = %d, want 2", p.DelegationRound)
	}
	if n := len(clusterSubmissions(t, r)); n != 2 {
		t.Fatalf("submissions = %d, want round two's 2 only", n)
	}
}

// TestAStopAfterRoundTwoCancelsItsChild: the stop walk reaches a round-two
// child, past the first round's children in EarlierChildren.
func TestAStopAfterRoundTwoCancelsItsChild(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-rounds-stop"
	parent, origin, first := profileBatchParent(t, r, conv, "a", "b")
	r.g.cfg.DelegationRoundsMax = 2
	completeChild(t, r, first[0], "a is fine")
	completeChild(t, r, first[1], "b is fine")
	waitFor(t, "round one's ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "c", "d")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "round two submitted", func() bool { return len(clusterSubmissions(t, r)) == 4 })
	r.adapter.inbox <- InboundMessage{Conversation: conv, Backend: consoleBackend, Kind: "dm", AuthorID: consoleAuthor,
		MessageID: "stop-round-two", Text: "stop"}
	var second []string
	for _, e := range clusterSubmissions(t, r) {
		if e.TaskID != first[0].TaskID && e.TaskID != first[1].TaskID {
			second = append(second, e.TaskID)
		}
	}
	waitFor(t, "each round-two child cancelled", func() bool {
		for _, id := range second {
			cancelled := false
			for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", id)) {
				cancelled = cancelled || e.env.Kind == lib.KindCancel
			}
			if !cancelled {
				return false
			}
		}
		return true
	})
}

// TestARoundTwoRequestRefusedByItsChecksIsToldWithItsRound: a request in a
// later round that its checks refuse carries its own round, and uses it.
func TestARoundTwoRequestRefusedByItsChecksIsToldWithItsRound(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-rounds-refused-second"
	parent, origin, first := profileBatchParent(t, r, conv, "a", "b")
	r.g.cfg.DelegationRoundsMax = 3
	completeChild(t, r, first[0], "a is fine")
	completeChild(t, r, first[1], "b is fine")
	waitFor(t, "round one's ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	r.g.cfg.DelegationWidthMax = 1 // round two's two tasks are too wide
	if err := parent.PublishArtifact(ctx, twoClusterBatch(t, "c", "d")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "round two refused", func() bool { return len(refusalsOn(t, r, "chat", origin.TaskID)) == 1 })
	if got := refusalsOn(t, r, "chat", origin.TaskID)[0]; got.Round != 2 {
		t.Fatalf("round two's refusal carries Round %d, want 2", got.Round)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if p, _ := rec.TaskRefFor(origin.TaskID); p.DelegationRound != 2 || p.DelegationEnd == "" || len(p.EarlierChildren) != 2 {
		t.Fatalf("parent after round two's refusal = round %d end %q earlier %v", p.DelegationRound, p.DelegationEnd, p.EarlierChildren)
	}
}

// A failed mint in a later round drops that round's entries alone: the
// earlier round's children stay on the record.
func TestAFailedMintInALaterRoundKeepsTheEarlierRoundsChildren(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	rec := &SessionRecord{Key: "console:rounds-drop", Tasks: []TaskRef{
		{ID: "p", Tier: "unattended", DelegationRound: 2, EarlierChildren: []string{"r1"}, Children: []string{"r2"}},
		{ID: "r1", Role: taskRoleChild, ParentTaskID: "p", Tier: "read"},
		{ID: "r2", Role: taskRoleChild, ParentTaskID: "p", Tier: "read", BatchRound: 2},
	}}
	r.g.dropFailedChildren(context.Background(), rec, "p")
	if _, ok := rec.TaskRefFor("r1"); !ok {
		t.Fatal("round one's child was dropped with round two's")
	}
	if _, ok := rec.TaskRefFor("r2"); ok {
		t.Fatal("round two's failed child is still on the record")
	}
}

// A round isn't settled while an end is still to send (Unsent) or a child of
// the round is still routed, even with every end recorded.
func TestARoundIsNotSettledWithAnUnsentEndOrARoutedChild(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	parent := TaskRef{ID: "p", Tier: "unattended", BatchSize: 2, Children: []string{"k1", "k2"}}
	base := func(unsent bool) *SessionRecord {
		return &SessionRecord{Key: "console:rounds-settled", Tasks: []TaskRef{parent,
			{ID: "k1", Role: taskRoleChild, ParentTaskID: "p", BatchIndex: 1, BatchEnd: &BatchEnd{State: lib.StateCompleted}},
			{ID: "k2", Role: taskRoleChild, ParentTaskID: "p", BatchIndex: 2, BatchEnd: &BatchEnd{State: lib.StateCompleted, Unsent: unsent}},
		}}
	}
	if !r.g.roundSettled(ctx, base(false), parent, "k2") {
		t.Fatal("a round with every end sent and no child routed reads as running")
	}
	if r.g.roundSettled(ctx, base(true), parent, "k2") {
		t.Fatal("a round with an unsent end reads as settled")
	}
	r.g.mu.Lock()
	r.g.taskSessions["k1"] = "console:rounds-settled"
	r.g.mu.Unlock()
	if r.g.roundSettled(ctx, base(false), parent, "k2") {
		t.Fatal("a round with a child still routed reads as settled")
	}
}

// From round two on, a request whose batch plan was written and then ended
// gets its per-entry ends, not a refusal on top of them, as in round one; a
// refusal for having used every round still goes.
func TestARoundTwoRefusalIsHeldBackWhenItsPlanWasWritten(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:rounds-refusal-plan"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-rounds-plan", Tasks: []TaskRef{
		{ID: "task-rp", Addressee: "chat", Tier: "unattended", DelegationRound: 2, BatchSize: 2, Attribution: []byte(`{}`), CorrelationID: "corr-rp",
			DelegationEnd: "reason: delegation-not-started - a child couldn't reach the bus"},
	}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.IndexTask(ctx, "task-rp", conv); err != nil { // still routed: it listens
		t.Fatal(err)
	}
	r.g.tellProfileParentNoChild(ctx, rec, "chat", "task-rp", "reason: delegation-not-started - x", 2, false)
	time.Sleep(300 * time.Millisecond)
	if got := refusalsOn(t, r, "chat", "task-rp"); len(got) != 0 {
		t.Fatalf("a refusal went on top of the written plan's ends: %+v", got)
	}
	r.g.tellProfileParentNoChild(ctx, rec, "chat", "task-rp", "reason: delegation-refused - rounds", 3, true)
	waitFor(t, "the past-cap refusal", func() bool { return len(refusalsOn(t, r, "chat", "task-rp")) == 1 })
}

// An ended profile parent's orphan cancel reaches every round's live
// children, an earlier round's (EarlierChildren) as well as the current
// one's: dropping the earlier rounds from the walk would leave a straggler
// of round one running after its parent ended.
func TestAnEndedParentsOrphanCancelReachesEveryRoundsChildren(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	conv := "console:rounds-orphans"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-rounds-orphans", Tasks: []TaskRef{
		{ID: "p-orph", Addressee: "chat", Tier: "unattended", DelegationRound: 2, EarlierChildren: []string{"r1-orph"}, Children: []string{"r2-orph"}, Attribution: []byte(`{}`)},
		{ID: "r1-orph", Addressee: "cluster", CorrelationID: "corr-r1-orph", Role: taskRoleChild, ParentTaskID: "p-orph", Tier: "read"},
		{ID: "r2-orph", Addressee: "cluster", CorrelationID: "corr-r2-orph", Role: taskRoleChild, ParentTaskID: "p-orph", Tier: "read", BatchRound: 2},
	}}
	r.g.mu.Lock()
	r.g.taskSessions["r1-orph"] = conv
	r.g.taskSessions["r2-orph"] = conv
	r.g.mu.Unlock()
	r.g.cancelOrphanedChildren(ctx, rec, "p-orph")
	for _, id := range []string{"r1-orph", "r2-orph"} {
		waitFor(t, id+" cancelled", func() bool {
			for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", id)) {
				if e.env.Kind == lib.KindCancel {
					return true
				}
			}
			return false
		})
	}
}
