package gateway

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// refusalsOn is the delegationRefused parts on a task's .in.
func refusalsOn(t *testing.T, r *rig, profile, taskID string) []lib.DelegationRefusal {
	t.Helper()
	var out []lib.DelegationRefusal
	for _, e := range streamEnvelopes(t, r.url, lib.TaskInSubject(profile, taskID)) {
		if ref, ok := lib.DelegationRefusalOf(messageParts(t, e.env)); ok {
			out = append(out, ref)
		}
	}
	return out
}

// startProfileParent starts a prod-east task in conv and returns its
// executor and task id, the parent working.
func startProfileParent(t *testing.T, r *rig, conv string) (*lib.TaskExecution, string) {
	t.Helper()
	turn := profileTurn(conv, "prod-east", nil)
	turn.Backend, turn.AuthorID = consoleBackend, consoleAuthor
	r.adapter.inbox <- turn
	origin := submissionOn(t, r, "prod-east")
	parent := r.execFor(t, origin, "prod-east")
	if err := parent.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	return parent, origin.TaskID
}

// TestARefusedProfileParentIsToldOnItsIn: a profile parent waits open for
// its child, so a refusal, which mints no child, reaches it on its .in with
// the room's notice (#4860); without it the parent waits to its deadline.
func TestARefusedProfileParentIsToldOnItsIn(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-told"
	parent, taskID := startProfileParent(t, r, conv)
	putRecord(t, r, conv, func(rec *SessionRecord) {
		for j := range rec.Tasks {
			if rec.Tasks[j].ID == taskID {
				rec.Tasks[j].Scope = devScope
			}
		}
	})
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	want := "cluster p1/us-east1/prod is outside this task's cluster p1/us-east1/dev"
	var got []lib.DelegationRefusal
	waitFor(t, "the refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "prod-east", taskID)
		return len(got) > 0
	})
	// A first refusal is round one, untagged: it ends the parent's wait.
	if len(got) != 1 || got[0].Round != 0 || !strings.HasPrefix(got[0].Text, noticeDelegationRefusedFor) || !strings.Contains(got[0].Text, want) {
		t.Fatalf("refusals on the parent's .in = %+v, want one untagged naming %q", got, want)
	}
}

// TestABusyRefusedProfileParentIsToldOnItsIn: the conversation's one live
// child belongs to another task, so the profile parent's first request is
// refused busy (delegation.busy), and that refusal reaches its .in untagged,
// as every other first refusal does.
func TestABusyRefusedProfileParentIsToldOnItsIn(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-busy"
	parent, taskID := startProfileParent(t, r, conv)
	const live = "busy-child-1"
	putRecord(t, r, conv, func(rec *SessionRecord) {
		rec.Tasks = append(rec.Tasks, TaskRef{ID: live, Role: taskRoleChild, Addressee: "cluster"})
	})
	r.g.mu.Lock()
	r.g.taskSessions[live] = conv
	r.g.mu.Unlock()
	before := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"))
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	var got []lib.DelegationRefusal
	waitFor(t, "the busy refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "prod-east", taskID)
		return len(got) > 0
	})
	if len(got) != 1 || got[0].Round != 0 || !strings.Contains(got[0].Text, "a delegated task is still running (task "+live+")") {
		t.Fatalf("refusals on the parent's .in = %+v, want one untagged busy refusal naming %s", got, live)
	}
	if after := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">")); after != before {
		t.Fatalf("%d submission(s) for a busy request", after-before)
	}
}

// TestAProfileParentsRepeatWhileItsBatchRunsIsToldTagged: a profile parent
// waits open on the batch its first request minted, so a second request, past
// the one delegation round there is by default, is refused to it under
// delegation.rounds, tagged round 2, which ends nothing. Nothing more is
// minted, the first request's record is untouched, and its children's ends
// still reach the parent.
func TestAProfileParentsRepeatWhileItsBatchRunsIsToldTagged(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-repeat-live"
	parent, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	again := delegateArtifactWithParameters(t, "cluster", "look again", targetParams("p1", "us-east1", "c"))
	again.ArtifactID += "-again"
	if err := parent.PublishArtifact(ctx, again); err != nil {
		t.Fatal(err)
	}
	var got []lib.DelegationRefusal
	waitFor(t, "the repeat's refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "chat", origin.TaskID)
		return len(got) > 0
	})
	if len(got) != 1 || got[0].Round != 2 || got[0].Text != roundsRefusal(1, true) {
		t.Fatalf("refusals on the parent's .in = %+v, want one tagged round 2", got)
	}
	rec, _ := r.g.reg.Get(ctx, conv)
	if ref, _ := rec.TaskRefFor(origin.TaskID); ref.DelegationEnd != "" || len(ref.Children) != len(kids) {
		t.Fatalf("the parent's entry after the repeat = %+v, want its batch untouched", ref)
	}
	if n := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>")); n != len(kids) {
		t.Fatalf("%d child submissions after the repeat, want %d", n, len(kids))
	}
	for i, k := range kids {
		completeChild(t, r, k, fmt.Sprintf("result %d", i+1))
	}
	waitFor(t, "both children's ends on the parent's .in", func() bool {
		return len(childResultsOn(t, r, "chat", origin.TaskID)) == len(kids)
	})
	if got := refusalsOn(t, r, "chat", origin.TaskID); len(got) != 1 {
		t.Fatalf("%d refusals after the children ended, want the one repeat", len(got))
	}
}

// TestAProfileWhoseCardDoesNotDelegateIsRefused: the profile admin's opt-in
// (spec.delegates, on the card) is enforced here, not by the worker's tool
// being absent (#6789). The parent is told, and nothing is submitted.
func TestAProfileWhoseCardDoesNotDelegateIsRefused(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.profiles.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	ctx := context.Background()
	conv := "console:tab-nodelegate"
	parent, taskID := startProfileParent(t, r, conv)
	before := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"))
	if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))); err != nil {
		t.Fatal(err)
	}
	notice := fmt.Sprintf(noticeProfileDoesNotDelegate, "prod-east")
	waitFor(t, "the refusal on the parent's entry", func() bool {
		rec, _ := r.g.reg.Get(ctx, conv)
		ref, ok := rec.TaskRefFor(taskID)
		return ok && strings.Contains(ref.DelegationEnd, notice)
	})
	waitFor(t, "the refusal on the parent's .in", func() bool {
		got := refusalsOn(t, r, "prod-east", taskID)
		return len(got) == 1 && strings.Contains(got[0].Text, notice)
	})
	if after := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">")); after != before {
		t.Fatalf("%d submission(s) for a profile that doesn't delegate", after-before)
	}
}

// TestARefusedProfileParentsRequestAgainIsARepeat: the parent was told its
// refusal ends its wait, so the same task's request seen again (a
// redelivery after a restart reads the same way) is refused under
// delegation.rounds, the refused request having used the task's one round,
// tagged round 2 so it ends nothing, and is not minted late once what refused
// it has cleared.
func TestARefusedProfileParentsRequestAgainIsARepeat(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	ctx := context.Background()
	conv := "console:tab-again"
	parent, taskID := startProfileParent(t, r, conv)
	setScope := func(scope string) {
		putRecord(t, r, conv, func(rec *SessionRecord) {
			for j := range rec.Tasks {
				if rec.Tasks[j].ID == taskID {
					rec.Tasks[j].Scope = scope
				}
			}
		})
	}
	var prodScope string
	if rec, _ := r.g.reg.Get(ctx, conv); rec != nil {
		ref, _ := rec.TaskRefFor(taskID)
		prodScope = ref.Scope
	}
	setScope(devScope)
	ask := delegateArtifactWithParameters(t, "cluster", "look", targetParams("p1", "us-east1", "prod"))
	if err := parent.PublishArtifact(ctx, ask); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the refusal on the parent's .in", func() bool { return len(refusalsOn(t, r, "prod-east", taskID)) == 1 })
	// What refused it clears; the request comes again.
	setScope(prodScope)
	before := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">"))
	ask.ArtifactID += "-again"
	if err := parent.PublishArtifact(ctx, ask); err != nil {
		t.Fatal(err)
	}
	var got []lib.DelegationRefusal
	waitFor(t, "the repeat's refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "prod-east", taskID)
		return len(got) >= 2
	})
	time.Sleep(300 * time.Millisecond)
	got = refusalsOn(t, r, "prod-east", taskID)
	if len(got) != 2 || got[0].Round != 0 || got[1].Round != 2 || got[1].Text != roundsRefusal(1, false) {
		t.Fatalf("refusals after the request came again = %+v, want the first untagged and one tagged round 2", got)
	}
	if after := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+">")); after != before {
		t.Fatalf("%d submission(s) for a request already answered", after-before)
	}
}

// TestACutShortBatchWhoseProfileLostDelegatesIsWithdrawn: a batch a restart
// cut short is finished by its request seen again, every check applying. A
// profile that lost spec.delegates in between is refused, and the batch is
// withdrawn whole (each entry ended), never left with a child running and
// the rest unminted.
func TestACutShortBatchWhoseProfileLostDelegatesIsWithdrawn(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.beforeBatchChild = func(i int) bool { return i == 1 }
	ctx := context.Background()
	conv := "console:tab-batch-lost-delegates"
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
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "an end for each entry", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	rec, _ := r.g.reg.Get(ctx, conv)
	if ref, ok := rec.TaskRefFor(origin.TaskID); !ok || ref.DelegationEnd == "" {
		t.Fatalf("the parent's entry = %+v, want the batch withdrawn", ref)
	}
}

// TestAProfileParentsRequestAfterItsBatchWasWithdrawnIsARepeat: a batch
// withdrawn whole has had each entry ended (endWithdrawnProfileBatch), and
// no refusal beside those ends. The task's request after that is refused
// under delegation.rounds, tagged round 2, with nothing minted.
func TestAProfileParentsRequestAfterItsBatchWasWithdrawnIsARepeat(t *testing.T) {
	t.Parallel()
	r := startProfileRig(t)
	r.g.cfg.DelegationWidthMax = 10
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.beforeBatchChild = func(i int) bool { return i == 1 }
	ctx := context.Background()
	conv := "console:tab-repeat-withdrawn"
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
	// What the resume meets refuses it, so the batch is withdrawn.
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat"})
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "an end for each entry", func() bool { return len(childResultsOn(t, r, "chat", origin.TaskID)) == 2 })
	if got := refusalsOn(t, r, "chat", origin.TaskID); len(got) != 0 {
		t.Fatalf("refusals beside the withdrawn batch's ends = %+v, want none", got)
	}
	r.g.profiles.put("chat", &lib.AgentCard{Name: "chat", Delegates: true})
	r.g.beforeBatchChild = nil
	before := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>"))
	art.ArtifactID += "-again"
	if err := parent.PublishArtifact(ctx, art); err != nil {
		t.Fatal(err)
	}
	var got []lib.DelegationRefusal
	waitFor(t, "the repeat's refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "chat", origin.TaskID)
		return len(got) > 0
	})
	if len(got) != 1 || got[0].Round != 2 || got[0].Text != roundsRefusal(1, false) {
		t.Fatalf("refusals after the withdrawn batch's request again = %+v, want one tagged round 2", got)
	}
	if after := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>")); after != before {
		t.Fatalf("%d submission(s) for a repeat", after-before)
	}
}

// roundsRefusal is the parent's text for a request refused under
// delegation.rounds after used rounds, children saying they minted any.
func roundsRefusal(used int, children bool) string {
	text := noticeDelegationRefusedFor + fmt.Sprintf(noticeDelegationRounds, used, plural(used))
	if children {
		text += noticeDelegationRoundsChildren
	}
	return text
}

// TestASecondRoundIsRefusedUnderTheRoundsRuleEvenWhenSetHigher: a gateway set
// to more rounds than are supported (A2A_DELEGATION_ROUNDS_MAX=3) runs at one,
// with a warning naming the variable, and a profile parent's second request
// is refused under delegation.rounds, its refusal tagged round 2, with
// nothing minted. The part the parent was sent advertises no rounds.
func TestASecondRoundIsRefusedUnderTheRoundsRuleEvenWhenSetHigher(t *testing.T) {
	t.Parallel()
	scope := t.TempDir() + "/scope.json"
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, func(c *Config) {
		c.ScopeFile, c.Namespace = scope, "kubeagents-system"
		c.DelegationRoundsMax = 3
	}, slog.New(slog.NewTextHandler(io.MultiWriter(os.Stderr, logs), nil)))
	r.logs = logs
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams, YieldToPinned: true})
	if r.g.cfg.DelegationRoundsMax != 1 {
		t.Fatalf("DelegationRoundsMax = %d, want it clamped to 1", r.g.cfg.DelegationRoundsMax)
	}
	if !loggedContaining(r, "level=WARN", "A2A_DELEGATION_ROUNDS_MAX", "rounds above 1 are not supported yet", "set=3")() {
		t.Fatalf("no clamp warning naming the variable:\n%s", logs.String())
	}
	ctx := context.Background()
	conv := "console:tab-rounds-rule"
	parent, origin, kids := profileBatchParent(t, r, conv, "a", "b")
	if targets, ok, err := lib.DelegateTargetsOf(messageParts(t, origin)); err != nil || !ok || targets.RoundsMax != 0 {
		t.Fatalf("the parent's targets = %+v (%v, %v), want a list with no rounds", targets, ok, err)
	}
	again := delegateArtifactWithParameters(t, "cluster", "look again", targetParams("p1", "us-east1", "c"))
	again.ArtifactID += "-again"
	if err := parent.PublishArtifact(ctx, again); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the rounds refusal line", loggedContaining(r, "delegation refused", "rule="+ruleDelegationRounds, "roundsUsed=1", "roundsMax=1"))
	var got []lib.DelegationRefusal
	waitFor(t, "the refusal on the parent's .in", func() bool {
		got = refusalsOn(t, r, "chat", origin.TaskID)
		return len(got) > 0
	})
	if len(got) != 1 || got[0].Round != 2 || got[0].Text != roundsRefusal(1, true) {
		t.Fatalf("refusals = %+v, want one round-2 refusal %q", got, roundsRefusal(1, true))
	}
	if loggedContaining(r, "delegation refused", "rule="+ruleDelegationBusy)() {
		t.Fatal("the second round was refused under delegation.busy too")
	}
	if n := len(streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>")); n != len(kids) {
		t.Fatalf("%d child submissions after the second round, want %d", n, len(kids))
	}
}
