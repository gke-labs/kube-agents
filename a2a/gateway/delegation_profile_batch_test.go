package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

// profileBatchParent starts a top-level profile task on "chat" in a console
// conversation and has it delegate a batch of the given clusters to the
// cluster profile. It returns the parent's execution, its submission, and
// the children's submissions in batch order.
func profileBatchParent(t *testing.T, r *rig, conv string, clusters ...string) (*lib.TaskExecution, *lib.Envelope, []*lib.Envelope) {
	t.Helper()
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	ctx := context.Background()
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	var tasks []lib.DelegateTask
	for _, c := range clusters {
		tasks = append(tasks, lib.DelegateTask{Addressee: "cluster", Text: "look at " + c, Parameters: targetParams("p1", "us-east1", c)})
	}
	if err := parent.PublishArtifact(ctx, delegateBatchArtifact(t, tasks...)); err != nil {
		t.Fatal(err)
	}
	var kids []*lib.Envelope
	waitFor(t, "every child submitted", func() bool {
		kids = nil
		for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>") {
			kids = append(kids, e.env)
		}
		return len(kids) == len(clusters)
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	index := map[string]int{}
	for _, k := range childrenOf(rec, origin.TaskID) {
		index[k.ID] = k.BatchIndex
	}
	sort.Slice(kids, func(i, j int) bool { return index[kids[i].TaskID] < index[kids[j].TaskID] })
	return parent, origin, kids
}

// startRestartableProfileRig is startProfileRig on a rig restartRig can
// stop and replace.
func startRestartableProfileRig(t *testing.T) *rig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	r, _ := startRigWithSpawnerCap(t, "platform", 0, func(c *Config) { c.ScopeFile = scope; c.Namespace = "kubeagents-system" })
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams, YieldToPinned: true})
	return r
}

// childResultsOn reads every childResult on a parent's .in.
func childResultsOn(t *testing.T, r *rig, addressee, taskID string) []lib.ChildResult {
	t.Helper()
	var out []lib.ChildResult
	for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject(addressee, taskID)) {
		if c, ok := lib.ChildResultOf(messageParts(t, e.env)); ok {
			out = append(out, c)
		}
	}
	return out
}

func completeChild(t *testing.T, r *rig, env *lib.Envelope, text string) {
	t.Helper()
	ctx := context.Background()
	child := r.execFor(t, env, "cluster")
	if err := child.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-" + env.TaskID, Name: lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: text}}}); err != nil {
		t.Fatal(err)
	}
	if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
}

// TestAProfileParentsBatchSendsEachChildsEndAsItEnds: a profile task's batch
// mints a child per entry at the parent's tier, and each child's end reaches
// the parent's .in as its own childResult, naming its entry, as that child
// ends. The parent holds the conversation the whole time, since it waits
// open: no wake runs.
func TestAProfileParentsBatchSendsEachChildsEndAsItEnds(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch"
	parent, origin, kids := profileBatchParent(t, r, conv, "a", "b", "c")
	// A progress write mid-wait is no end either.
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}

	for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>") {
		if _, tier, kind, _, ok := lib.ParseSubmitSubject(e.subject); !ok || tier != "read" || kind != lib.SubmitKindChild {
			t.Fatalf("child subject = %q, want a read child submission", e.subject)
		}
	}
	waitForRecord(t, r, conv, "the parent holding the conversation", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == origin.TaskID
	})
	// Each child carries the parent on its chain, so its own children are
	// held to the cycle rule as a single child's are.
	rec0, _ := r.g.reg.Get(ctx, conv)
	for _, k := range childrenOf(rec0, origin.TaskID) {
		if strings.Join(k.ProfileChain, ",") != "chat" {
			t.Fatalf("child %s chain = %v, want [chat]", k.ID, k.ProfileChain)
		}
	}

	completeChild(t, r, kids[1], "b is fine")
	waitFor(t, "b's result on the parent's .in", func() bool {
		got := childResultsOn(t, r, "chat", origin.TaskID)
		return len(got) == 1 && got[0].ChildTaskID == kids[1].TaskID && got[0].Entry == 2 &&
			got[0].State == lib.StateCompleted && got[0].Text == "b is fine"
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask == nil || rec.ActiveTask.TaskID != origin.TaskID {
		t.Fatalf("active = %+v, want the parent while its other children run", rec.ActiveTask)
	}
	// Handing off and waiting is no end: only the parent's terminal status
	// cancels its batch, so the children still running get no cancel.
	for _, k := range kids {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", k.TaskID)) {
			if e.env.Kind == lib.KindCancel {
				t.Fatalf("child %s cancelled while its parent waits open", k.TaskID)
			}
		}
	}

	completeChild(t, r, kids[0], "a is fine")
	completeChild(t, r, kids[2], "c is fine")
	waitFor(t, "all three results", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 3 })
	entries := map[int]string{}
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
		entries[c.Entry] = c.ChildTaskID
	}
	for i, k := range kids {
		if entries[i+1] != k.TaskID {
			t.Fatalf("entry %d's result names %q, want %q", i+1, entries[i+1], k.TaskID)
		}
	}
	waitForRecord(t, r, conv, "the parent still holding the conversation", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == origin.TaskID && rec.Addressee == "chat"
	})
	if n := platformSubmissions(t, r); n != 0 {
		t.Fatalf("%d platform submission(s): a profile parent's batch woke something", n)
	}
	// The results are delivered once each, not again at the last end.
	time.Sleep(200 * time.Millisecond)
	if got := childResultsOn(t, r, "chat", origin.TaskID); len(got) != 3 {
		t.Fatalf("%d childResults, want 3", len(got))
	}
}

// TestAProfileParentsBatchChildStoppedMidBatchStillSendsOneEnd: a child that
// is stopped while its siblings run sends the parent one end, canceled, so a
// parent counting to the batch size isn't left waiting for it.
func TestAProfileParentsBatchChildStoppedMidBatchStillSendsOneEnd(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch-cancel"
	_, origin, kids := profileBatchParent(t, r, conv, "a", "b")

	// The dispatcher ends one child canceled (its queue timeout, say).
	child := r.execFor(t, kids[0], "cluster")
	if err := child.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	completeChild(t, r, kids[1], "b is fine")
	waitFor(t, "both ends", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
		switch c.ChildTaskID {
		case kids[0].TaskID:
			if c.State != lib.StateCanceled || c.Entry != 1 {
				t.Fatalf("a's end = %+v, want canceled, entry 1", c)
			}
		case kids[1].TaskID:
			if c.State != lib.StateCompleted || c.Entry != 2 {
				t.Fatalf("b's end = %+v, want completed, entry 2", c)
			}
		default:
			t.Fatalf("a result for %q", c.ChildTaskID)
		}
	}
}

// TestAStopDuringAProfileParentsBatchCancelsTheTree: a stop while a profile
// parent's batch runs cancels the parent and every live child, and writes a
// dispatch stop record for each.
func TestAStopDuringAProfileParentsBatchCancelsTheTree(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	conv := "console:tab-profile-batch-stop"
	_, origin, kids := profileBatchParent(t, r, conv, "a", "b", "c")
	completeChild(t, r, kids[0], "a is fine")
	waitFor(t, "a's result", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 1 })

	r.adapter.inbox <- InboundMessage{Conversation: conv, Backend: consoleBackend, Kind: "dm", AuthorID: consoleAuthor,
		MessageID: "stop-pb", Text: "stop"}
	cancelled := func(addressee, taskID string) bool {
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject(addressee, taskID)) {
			if e.env.Kind == lib.KindCancel {
				return true
			}
		}
		return false
	}
	waitFor(t, "the parent and the live children cancelled", func() bool {
		return cancelled("chat", origin.TaskID) && cancelled("cluster", kids[1].TaskID) && cancelled("cluster", kids[2].TaskID)
	})
	waitFor(t, "a stop record for each live profile task", func() bool {
		_, p := dispatchStopFor(t, r.url, "chat", origin.TaskID)
		_, b := dispatchStopFor(t, r.url, "cluster", kids[1].TaskID)
		_, c := dispatchStopFor(t, r.url, "cluster", kids[2].TaskID)
		return p && b && c
	})
}

// TestAProfileParentsBatchWithAnOutOfScopeEntryMintsNothing: the parent's
// chain and scope rules apply to every entry, and one refused entry refuses
// the whole batch.
func TestAProfileParentsBatchWithAnOutOfScopeEntryMintsNothing(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	ctx := context.Background()
	conv := "console:tab-profile-batch-scope"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "x", Parameters: targetParams("p9", "us-east1", "x")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the batch recorded as refused", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.DelegationEnd != ""
	})
	time.Sleep(200 * time.Millisecond)
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>"); len(envs) != 0 {
		t.Fatalf("a refused batch left %d child submission(s)", len(envs))
	}
	if got := childResultsOn(t, r, "chat", origin.TaskID); len(got) != 0 {
		t.Fatalf("a refused batch sent %d childResult(s): %+v", len(got), got)
	}
}

// TestARedeliveredProfileBatchMintsNothingMore: the batch artifact arriving
// twice mints its children once.
func TestARedeliveredProfileBatchMintsNothingMore(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch-redeliver"
	parent, _, kids := profileBatchParent(t, r, conv, "a", "b")
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "look at a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "look at b", Parameters: targetParams("p1", "us-east1", "b")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>"); len(envs) != len(kids) {
		t.Fatalf("%d child submissions after a redelivery, want %d", len(envs), len(kids))
	}
}

// TestAWithdrawnProfileBatchEndsEveryEntry: a profile parent's batch cut
// short, whose request then fails its checks when it comes back, is
// withdrawn whole, and the parent gets one failed end per entry, naming the
// entry and no child, so it isn't left waiting for children that never ran.
func TestAWithdrawnProfileBatchEndsEveryEntry(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.beforeBatchChild = func(i int) bool { return i == 1 }
	ctx := context.Background()
	conv := "console:tab-profile-batch-withdrawn"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	first := submissionOn(t, r, "cluster")
	waitForRecord(t, r, conv, "the cut-short mint", func(rec *SessionRecord) bool {
		ref, _ := rec.TaskRefFor(origin.TaskID)
		return ref.BatchSize == 2 && len(ref.Children) == 1
	})
	// The profile goes away before the request comes back, so its checks
	// now refuse it and the batch is withdrawn.
	r.g.profiles.put("cluster", nil)
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "an end for each entry", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	seen := map[int]bool{}
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
		if c.ChildTaskID != "" || c.State != lib.StateFailed || c.Text != whyBatchCutShort {
			t.Fatalf("end = %+v, want failed, no child, the cut-short reason", c)
		}
		seen[c.Entry] = true
	}
	if !seen[1] || !seen[2] {
		t.Fatalf("entries ended = %v, want 1 and 2", seen)
	}
	// The withdrawn child's own end, when it comes, adds nothing.
	child := r.execFor(t, first, "cluster")
	if err := child.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	time.Sleep(300 * time.Millisecond)
	if got := childResultsOn(t, r, "chat", origin.TaskID); len(got) != 2 {
		t.Fatalf("%d ends after the withdrawn child's terminal, want 2: %+v", len(got), got)
	} // Each entry has its own end, so the parent gets no whole-request
	// refusal on top of them (#4860).
	if got := refusalsOn(t, r, "chat", origin.TaskID); len(got) != 0 {
		t.Fatalf("a withdrawn batch's parent got a refusal too: %+v", got)
	}
}

// TestAProfileParentThatEndsBeforeItsBatchGivesTheConversationUp: a profile
// parent's own terminal is its real end, not a hand-off line, so it releases
// the conversation even while its batch runs, and its children's later ends
// send nothing to a parent that no longer reads. Its live children are
// cancelled.
func TestAProfileParentThatEndsBeforeItsBatchGivesTheConversationUp(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch-parent-ends"
	parent, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	if err := parent.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitForRecord(t, r, conv, "the conversation released", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil
	})
	// Nothing reads the children's results now, so each is cancelled, with
	// a dispatch stop record, rather than left holding a dispatcher slot.
	waitFor(t, "each live child cancelled", func() bool {
		for _, k := range kids {
			cancelled := false
			for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", k.TaskID)) {
				cancelled = cancelled || e.env.Kind == lib.KindCancel
			}
			if _, stop := dispatchStopFor(t, r.url, "cluster", k.TaskID); !cancelled || !stop {
				return false
			}
		}
		return true
	})
	completeChild(t, r, kids[0], "a is fine")
	completeChild(t, r, kids[1], "b is fine")
	waitForRecord(t, r, conv, "both ends recorded", func(rec *SessionRecord) bool {
		n := 0
		for _, k := range childrenOf(rec, origin.TaskID) {
			if k.BatchEnd != nil {
				n++
			}
		}
		return n == 2
	})
	rec, _ := r.g.reg.Get(ctx, conv)
	if rec.ActiveTask != nil {
		t.Fatalf("active = %+v after the batch, want none", rec.ActiveTask)
	}
	if got := childResultsOn(t, r, "chat", origin.TaskID); len(got) != 0 {
		t.Fatalf("%d ends sent to an ended parent", len(got))
	}
}

// TestAWithdrawnProfileBatchLeavesAnEndedEntryAlone: when a cut-short
// batch's first child had already ended, and been sent, before the batch was
// withdrawn, that entry keeps its one end and only the entry that never ran
// gets a failed one.
func TestAWithdrawnProfileBatchLeavesAnEndedEntryAlone(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.beforeBatchChild = func(i int) bool { return i == 1 }
	ctx := context.Background()
	conv := "console:tab-profile-batch-withdrawn-ended"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	first := submissionOn(t, r, "cluster")
	completeChild(t, r, first, "a is fine")
	waitFor(t, "a's end", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 1 })
	r.g.profiles.put("cluster", nil)
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "entry 2's end", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) >= 2 })
	time.Sleep(200 * time.Millisecond)
	got := childResultsOn(t, r, "chat", origin.TaskID)
	if len(got) != 2 {
		t.Fatalf("%d ends, want one per entry: %+v", len(got), got)
	}
	for _, c := range got {
		switch c.Entry {
		case 1:
			if c.State != lib.StateCompleted || c.ChildTaskID != first.TaskID {
				t.Fatalf("entry 1's end = %+v, want a's completed end", c)
			}
		case 2:
			if c.State != lib.StateFailed || c.ChildTaskID != "" {
				t.Fatalf("entry 2's end = %+v, want failed with no child", c)
			}
		default:
			t.Fatalf("an end for entry %d", c.Entry)
		}
	}
}

// TestAHealedProfileParentEndCancelsItsBatch: when the relay missed a
// profile parent's terminal and the heal finds it instead, the heal releases
// the conversation and cancels the live children, as the relay would have.
func TestAHealedProfileParentEndCancelsItsBatch(t *testing.T) {
	t.Parallel()
	r := startRestartableProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch-healed-end"
	parent, _, kids := profileBatchParent(t, r, conv, "a", "b")
	r2, _ := restartRig(t, r, func() {
		if err := parent.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
			t.Fatal(err)
		}
		drainRelayDurable(t, r.url)
	})
	r2.adapter.inbox <- InboundMessage{Conversation: conv, Backend: consoleBackend, Kind: "dm", AuthorID: consoleAuthor,
		MessageID: "after-heal", Text: "anything new?"}
	waitFor(t, "each live child cancelled by the heal", func() bool {
		for _, k := range kids {
			cancelled := false
			for _, e := range streamEnvelopes(t, r2.url, lib.TaskInSubject("cluster", k.TaskID)) {
				cancelled = cancelled || e.env.Kind == lib.KindCancel
			}
			if !cancelled {
				return false
			}
		}
		return true
	})
}

// TestAProfileParentsBatchWithOneTargetsPinRefusingItsTierMintsNothing: the
// pinned-tier check runs for every entry, as for a single child. One target
// whose pinned profile doesn't take the child's tier refuses the whole
// batch, since a batch is whole or not run.
func TestAProfileParentsBatchWithOneTargetsPinRefusingItsTierMintsNothing(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.profiles.put("tse", &lib.AgentCard{Name: "tse", Parameters: clusterParams})
	// Cluster c's pinned profile takes only unattended, not the read tier
	// the parent's children are minted at.
	r.g.profiles.put("c-pin", &lib.AgentCard{Name: "c-pin", Parameters: clusterParams, Tiers: []string{"unattended"},
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "c"}})
	ctx := context.Background()
	conv := "console:tab-profile-batch-pinned-tier"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "tse", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "tse", Text: "c", Parameters: targetParams("p1", "us-east1", "c")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the batch recorded as refused for the pin", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && strings.Contains(ref.DelegationEnd, "c-pin")
	})
	time.Sleep(200 * time.Millisecond)
	if envs := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"tse.>"); len(envs) != 0 {
		t.Fatalf("a batch refused for one target's pin left %d child submission(s)", len(envs))
	}
}

// TestASupervisorEndForAProfileParentCancelsItsBatch: a parent whose worker
// died mid-wait never writes its own final status. The supervisor's
// terminal on the parent's task (its deadline, or the worker's loss) is the
// parent's end all the same, so the batch is cancelled then, not left to
// run to each child's own deadline.
func TestASupervisorEndForAProfileParentCancelsItsBatch(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	conv := "console:tab-profile-batch-supervisor-end"
	_, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	payload, err := json.Marshal(lib.StatusUpdate{TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: lib.StateFailed}, Final: true})
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewStatusUpdateEnvelope(gatewayParty, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(context.Background(), lib.TaskSupervisorSubject("chat", origin.TaskID), env); err != nil {
		t.Fatal(err)
	}
	waitForRecord(t, r, conv, "the conversation released", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil
	})
	waitFor(t, "each live child cancelled", func() bool {
		for _, k := range kids {
			cancelled := false
			for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", k.TaskID)) {
				cancelled = cancelled || e.env.Kind == lib.KindCancel
			}
			if _, stop := dispatchStopFor(t, r.url, "cluster", k.TaskID); !cancelled || !stop {
				return false
			}
		}
		return true
	})
}

// TestAProfileParentThatEndsCancelsItsSingleLiveChild: the cancel keys on a
// profile task's own terminal with any live descendant, not on a batch, so
// a single child, which holds the conversation while it runs, is cancelled
// too when its parent ends at its deadline.
func TestAProfileParentThatEndsCancelsItsSingleLiveChild(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	ctx := context.Background()
	conv := "console:tab-profile-single-parent-ends"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(lib.DelegateRequest{Addressee: "cluster", Text: "look at a", Parameters: targetParams("p1", "us-east1", "a")})
	if err != nil {
		t.Fatal(err)
	}
	if err := parent.PublishArtifact(ctx, lib.Artifact{ArtifactID: "artifact-single-" + lib.ArtifactDelegate, Name: lib.ArtifactDelegate,
		Parts: []lib.Part{{Kind: "data", Data: data}}}); err != nil {
		t.Fatal(err)
	}
	child := submissionOn(t, r, "cluster")
	waitForRecord(t, r, conv, "the child holding the conversation", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == child.TaskID
	})
	if err := parent.PublishStatus(ctx, lib.StateFailed, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the live child cancelled with its stop record", func() bool {
		cancelled := false
		for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject("cluster", child.TaskID)) {
			cancelled = cancelled || e.env.Kind == lib.KindCancel
		}
		_, stop := dispatchStopFor(t, r.url, "cluster", child.TaskID)
		return cancelled && stop
	})
	// The cancelled child no longer holds the conversation: it's detached,
	// as a stop detaches it, so the next turn is a new task, not a steer.
	waitForRecord(t, r, conv, "the cancelled child detached", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil || (rec.ActiveTask.TaskID == child.TaskID && rec.ActiveTask.Detached)
	})
	next := profileTurn(conv, "chat", nil)
	next.Backend, next.AuthorID, next.MessageID, next.Text = consoleBackend, consoleAuthor, "after-cancel", "look again"
	r.adapter.inbox <- next
	waitFor(t, "a new task for the next turn, not a steer into the cancelled child", func() bool {
		for _, e := range streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"chat.>") {
			if e.env.TaskID != origin.TaskID && e.env.TaskID != child.TaskID {
				return true
			}
		}
		return false
	})
}

// TestABatchChildUnderAConfirmedParentCarriesItsChangeRequest: each batch
// child takes the parent's changeRequested from the gateway's record, as a
// single child does, and can't raise it.
func TestABatchChildUnderAConfirmedParentCarriesItsChangeRequest(t *testing.T) {
	t.Parallel()
	for _, confirm := range []bool{true, false} {
		r := startProfileRig(t)
		r.g.cfg.DelegationWidthMax = 10
		r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
		ctx := context.Background()
		conv := fmt.Sprintf("discord:cr-batch/%v", confirm)
		turn := profileTurn(conv, "chat", nil)
		if confirm {
			turn.Intent = IntentConfirm
		}
		r.adapter.inbox <- turn
		origin := submissionOn(t, r, "chat")
		parent := r.execFor(t, origin, "chat")
		if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
			t.Fatal(err)
		}
		art := delegateBatchArtifact(t,
			lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
			lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
		if err := parent.PublishArtifact(ctx, art); err != nil {
			t.Fatal(err)
		}
		var kids []subjectEnvelope
		waitFor(t, "both children submitted", func() bool {
			kids = streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>")
			return len(kids) == 2
		})
		for _, k := range kids {
			if found, got := submittedChange(t, k.env); !found || got != confirm {
				t.Fatalf("confirm=%v: batch child changeRequested = %v (part found %v)", confirm, got, found)
			}
		}
	}
}

// TestAnUnsentBatchChildEndIsSentAgainByTheHeal: a child's end that couldn't
// be published to its profile parent is recorded unsent, and the heal sends
// it once the bus takes it again, so the parent isn't left to its deadline.
func TestAnUnsentBatchChildEndIsSentAgainByTheHeal(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	conv := "console:tab-profile-batch-unsent"
	_, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	// The parent's .in stops being stored, so the publish fails.
	restore := withoutProfileParentIn(t, r.url)
	completeChild(t, r, kids[0], "a is fine")
	waitForRecord(t, r, conv, "a's end recorded unsent", func(rec *SessionRecord) bool {
		k, ok := rec.TaskRefFor(kids[0].TaskID)
		return ok && k.BatchEnd != nil && k.BatchEnd.Unsent
	})
	restore()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Backend: consoleBackend, Kind: "dm", AuthorID: consoleAuthor,
		MessageID: "heal-unsent", Text: "how's it going?"}
	waitFor(t, "a's end sent by the heal", func() bool {
		for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
			if c.ChildTaskID == kids[0].TaskID && c.Entry == 1 && c.Text == "a is fine" {
				return true
			}
		}
		return false
	})
	waitForRecord(t, r, conv, "a's end no longer unsent", func(rec *SessionRecord) bool {
		k, _ := rec.TaskRefFor(kids[0].TaskID)
		return k.BatchEnd != nil && !k.BatchEnd.Unsent
	})
}

// TestAMissedBatchChildTerminalIsHealedForAProfileParent: a child terminal
// the relay never delivered is found by the heal while the profile parent
// waits, and its end goes to the parent.
func TestAMissedBatchChildTerminalIsHealedForAProfileParent(t *testing.T) {
	t.Parallel()
	r := startRestartableProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-profile-batch-missed"
	_, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	child := r.execFor(t, kids[0], "cluster")
	r2, _ := restartRig(t, r, func() {
		if err := child.PublishArtifact(ctx, lib.Artifact{ArtifactID: "a-missed", Name: lib.ArtifactResult,
			Parts: []lib.Part{{Kind: "text", Text: "a is fine"}}}); err != nil {
			t.Fatal(err)
		}
		if err := child.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
			t.Fatal(err)
		}
		drainRelayDurable(t, r.url)
	})
	r2.adapter.inbox <- InboundMessage{Conversation: conv, Backend: consoleBackend, Kind: "dm", AuthorID: consoleAuthor,
		MessageID: "heal-missed", Text: "how's it going?"}
	waitFor(t, "a's end sent by the heal", func() bool {
		for _, c := range childResultsOn(t, r2, "chat", origin.TaskID) {
			if c.ChildTaskID == kids[0].TaskID && c.State == lib.StateCompleted {
				return true
			}
		}
		return false
	})
}

// withoutProfileParentIn stops TASKS storing any .in but the cluster
// profile's, so a publish to a chat parent's .in fails, and returns the
// function that puts the subjects back.
func withoutProfileParentIn(t *testing.T, url string) func() {
	t.Helper()
	nc, err := connectTest(url)
	if err != nil {
		t.Fatal(err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatal(err)
	}
	orig := stream.CachedInfo().Config
	cfg := orig
	cfg.Subjects = []string{"a2a.tasks.*.*.events", "a2a.tasks.*.*.supervisor", "a2a.tasks.cluster.*.in"}
	if _, err := js.UpdateStream(ctx, cfg); err != nil {
		t.Fatal(err)
	}
	return func() {
		defer nc.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := js.UpdateStream(ctx, orig); err != nil {
			t.Fatal(err)
		}
	}
}

// TestAWithdrawnProfileBatchSendsAnUnsentEndAsRecorded: an entry whose end
// was recorded but couldn't be sent before its batch was withdrawn gets that
// end, not a generic failure, since the withdrawal drops the child from the
// record and the heal would never find it again.
func TestAWithdrawnProfileBatchSendsAnUnsentEndAsRecorded(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name      string
		finish    func(t *testing.T, r *rig, child *lib.Envelope)
		wantState lib.TaskState
		wantText  string
	}{
		{"completed", func(t *testing.T, r *rig, child *lib.Envelope) { completeChild(t, r, child, "a is fine") },
			lib.StateCompleted, "a is fine"},
		// A child that asked ends input-required on the record; the parent
		// is sent it canceled with the question, as the heal's resend
		// sends it, since a non-terminal end is refused (profileBatchEndState).
		{"asked", func(t *testing.T, r *rig, child *lib.Envelope) {
			publishQuestion(t, r, child, "cluster", "which namespace?")
		}, lib.StateCanceled, "which namespace?"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := startProfileRig(t)
			r.g.cfg.DelegationWidthMax = 10
			r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
			r.g.beforeBatchChild = func(i int) bool { return i == 1 }
			ctx := context.Background()
			conv := "console:tab-profile-batch-withdrawn-unsent-" + tc.name
			turn := profileTurn(conv, "chat", nil)
			turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
			r.adapter.inbox <- turn
			origin := submissionOn(t, r, "chat")
			parent := r.execFor(t, origin, "chat")
			if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
				t.Fatal(err)
			}
			art := delegateBatchArtifact(t,
				lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
				lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
			if err := parent.PublishArtifact(ctx, art); err != nil {
				t.Fatal(err)
			}
			first := submissionOn(t, r, "cluster")
			restore := withoutProfileParentIn(t, r.url)
			tc.finish(t, r, first)
			waitForRecord(t, r, conv, "a's end recorded unsent", func(rec *SessionRecord) bool {
				k, ok := rec.TaskRefFor(first.TaskID)
				return ok && k.BatchEnd != nil && k.BatchEnd.Unsent
			})
			restore()
			r.g.profiles.put("cluster", nil)
			if err := parent.PublishArtifact(ctx, art); err != nil {
				t.Fatal(err)
			}
			waitFor(t, "an end for each entry", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) >= 2 })
			time.Sleep(200 * time.Millisecond)
			got := childResultsOn(t, r, "chat", origin.TaskID)
			if len(got) != 2 {
				t.Fatalf("%d ends, want one per entry: %+v", len(got), got)
			}
			for _, c := range got {
				switch c.Entry {
				case 1:
					if c.ChildTaskID != first.TaskID || c.State != tc.wantState || c.Text != tc.wantText {
						t.Fatalf("entry 1's end = %+v, want a's recorded end as %s %q", c, tc.wantState, tc.wantText)
					}
				case 2:
					if c.State != lib.StateFailed || c.ChildTaskID != "" {
						t.Fatalf("entry 2's end = %+v, want failed with no child", c)
					}
				default:
					t.Fatalf("an end for entry %d", c.Entry)
				}
			}
		})
	}
}

// TestAProfileParentsRepeatRequestAfterOneThatEndedMintsNothing: a profile
// parent stays open after its delegate request, so it can ask again after
// one that ended with no batch on the bus. That repeat mints nothing: its
// ends would all be held back by the first request's mark (DelegationEnd),
// leaving the parent waiting on children that never report. Both ways the
// first can end: refused before anything was minted, and withdrawn after a
// cut-short mint.
func TestAProfileParentsRepeatRequestAfterOneThatEndedMintsNothing(t *testing.T) {
	t.Parallel()
	two := func(t *testing.T) lib.Artifact {
		return delegateBatchArtifact(t,
			lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
			lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
	}
	for _, tc := range []struct {
		name string
		// first makes the parent's first request end with no batch on the
		// bus, and returns the record's child count after it.
		first func(t *testing.T, r *rig, parent *lib.TaskExecution, conv, originID string) int
	}{
		{"refused", func(t *testing.T, r *rig, parent *lib.TaskExecution, conv, originID string) int {
			r.g.cfg.DelegationWidthMax = 1 // two tasks are too wide
			if err := parent.PublishArtifact(context.Background(), two(t)); err != nil {
				t.Fatal(err)
			}
			waitForRecord(t, r, conv, "the refusal's mark", func(rec *SessionRecord) bool {
				ref, _ := rec.TaskRefFor(originID)
				return ref.DelegationEnd != ""
			})
			r.g.cfg.DelegationWidthMax = 10
			return 0
		}},
		{"withdrawn", func(t *testing.T, r *rig, parent *lib.TaskExecution, conv, originID string) int {
			r.g.cfg.DelegationWidthMax = 10
			r.g.beforeBatchChild = func(i int) bool { return i == 1 }
			art := two(t)
			if err := parent.PublishArtifact(context.Background(), art); err != nil {
				t.Fatal(err)
			}
			submissionOn(t, r, "cluster")
			waitForRecord(t, r, conv, "the cut-short mint", func(rec *SessionRecord) bool {
				ref, _ := rec.TaskRefFor(originID)
				return ref.BatchSize == 2 && len(ref.Children) == 1
			})
			card, _ := r.g.profiles.lookup("cluster")
			r.g.profiles.put("cluster", nil)
			if err := parent.PublishArtifact(context.Background(), art); err != nil {
				t.Fatal(err)
			}
			waitForRecord(t, r, conv, "the withdrawal's mark", func(rec *SessionRecord) bool {
				ref, _ := rec.TaskRefFor(originID)
				return ref.DelegationEnd != ""
			})
			r.g.profiles.put("cluster", &card)
			r.g.beforeBatchChild = nil
			rec, _ := r.g.reg.Get(context.Background(), conv)
			ref, _ := rec.TaskRefFor(originID)
			return len(ref.Children)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r := startProfileRig(t)
			r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
			ctx := context.Background()
			conv := "console:tab-profile-batch-repeat-" + tc.name
			turn := profileTurn(conv, "chat", nil)
			turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
			r.adapter.inbox <- turn
			origin := submissionOn(t, r, "chat")
			parent := r.execFor(t, origin, "chat")
			if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
				t.Fatal(err)
			}
			kids := tc.first(t, r, parent, conv, origin.TaskID)
			// The repeat, which every check would now pass.
			if err := parent.PublishArtifact(ctx, two(t)); err != nil {
				t.Fatal(err)
			}
			// A marker after it on the same stream: once the gateway has
			// handled the marker, it has handled the repeat.
			if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
				t.Fatal(err)
			}
			time.Sleep(500 * time.Millisecond)
			rec, _ := r.g.reg.Get(ctx, conv)
			ref, _ := rec.TaskRefFor(origin.TaskID)
			if len(ref.Children) != kids {
				t.Fatalf("children after the repeat = %v, want the %d from before it", ref.Children, kids)
			}
		})
	}
}

// TestAWithdrawnProfileBatchRetriesAnEndThatFailsToSend: nothing sends a
// withdrawn batch's ends again (the withdrawal leaves the heal no child to
// find), so a send that fails is tried again on the spot, and the parent
// still gets one end per entry.
func TestAWithdrawnProfileBatchRetriesAnEndThatFailsToSend(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	var armed, failed atomic.Int32
	r.g.childResultFails = func() bool {
		// Once armed, the first publish fails; the rest go through.
		return armed.Load() == 1 && failed.Add(1) == 1
	}
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	r.g.beforeBatchChild = func(i int) bool { return i == 1 }
	ctx := context.Background()
	conv := "console:tab-profile-batch-withdrawn-retry"
	turn := profileTurn(conv, "chat", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "chat")
	parent := r.execFor(t, origin, "chat")
	if err := parent.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	art := delegateBatchArtifact(t,
		lib.DelegateTask{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
		lib.DelegateTask{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	submissionOn(t, r, "cluster")
	waitForRecord(t, r, conv, "the cut-short mint", func(rec *SessionRecord) bool {
		ref, _ := rec.TaskRefFor(origin.TaskID)
		return ref.BatchSize == 2 && len(ref.Children) == 1
	})
	r.g.profiles.put("cluster", nil)
	armed.Store(1)
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "an end for each entry", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	if failed.Load() < 2 {
		t.Fatalf("publishes seen by the hook = %d, want the failed one and its retry", failed.Load())
	}
	seen := map[int]bool{}
	for _, c := range childResultsOn(t, r, "chat", origin.TaskID) {
		seen[c.Entry] = true
	}
	if !seen[1] || !seen[2] {
		t.Fatalf("entries ended = %v, want 1 and 2", seen)
	}
}
