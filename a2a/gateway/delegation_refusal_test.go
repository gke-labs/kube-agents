package gateway

import (
	"context"
	"fmt"
	"strings"
	"testing"

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
	if len(got) != 1 || !strings.HasPrefix(got[0].Text, noticeDelegationRefusedFor) || !strings.Contains(got[0].Text, want) {
		t.Fatalf("refusals on the parent's .in = %+v, want one naming %q", got, want)
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
