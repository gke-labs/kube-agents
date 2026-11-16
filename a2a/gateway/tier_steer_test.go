package gateway

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startTierRig is a profile rig whose "cluster" profile runs at read and
// oncall, with the listed discord users mapped to oncall and the rest at read.
// The rig verifies two senders, 1001 and 1002.
func startTierRig(t *testing.T, oncall ...string) *rig {
	t.Helper()
	t.Cleanup(func() { capability.LoadTiers(nil) })
	capability.LoadTiers([]capability.TierDefinition{{Name: "oncall", Mode: capability.TierModeInteractive}})
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(`{"present":true,"projects":["p1"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &lockedBuffer{}
	r := startRigWithLogger(t, func(c *Config) {
		c.ScopeFile = scope
		c.Namespace = "kubeagents-system"
		for _, u := range oncall {
			c.TierMembership = append(c.TierMembership, TierMember{Backend: discordBackend, User: u, Tier: "oncall"})
		}
	}, slog.New(slog.NewTextHandler(io.MultiWriter(os.Stderr, logs), nil)))
	r.logs = logs
	r.g.profiles.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams, Tiers: []string{"read", "oncall"}})
	return r
}

func submittedTier(t *testing.T, r *rig) string {
	t.Helper()
	var got []subjectEnvelope
	waitFor(t, "a submission on cluster's .submit", func() bool {
		got = streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>")
		return len(got) == 1
	})
	_, tier, _, _, ok := lib.ParseSubmitSubject(got[0].subject)
	if !ok {
		t.Fatalf("subject %q", got[0].subject)
	}
	return tier
}

// Tier 2c (#3659): a profile task is submitted at its requester's tier, from
// the membership; an unlisted requester's runs at read.
func TestAProfileTaskRunsAtItsRequestersTier(t *testing.T) {
	for _, tc := range []struct{ author, want string }{{"1001", "oncall"}, {"1002", "read"}} {
		t.Run(tc.author, func(t *testing.T) {
			r := startTierRig(t, "1001")
			turn := profileTurn("discord:1/"+tc.author, "cluster", targetParams("p1", "us-east1", "prod"))
			turn.AuthorID = tc.author
			r.adapter.inbox <- turn
			if got := submittedTier(t, r); got != tc.want {
				t.Fatalf("submitted at %q, want %q", got, tc.want)
			}
		})
	}
}

// startOncallTask starts a profile task requested by 1001 at oncall and
// returns its ID.
func startOncallTask(t *testing.T, r *rig, conv string) string {
	t.Helper()
	turn := profileTurn(conv, "cluster", targetParams("p1", "us-east1", "prod"))
	turn.AuthorID = "1001"
	r.adapter.inbox <- turn
	if got := submittedTier(t, r); got != "oncall" {
		t.Fatalf("submitted at %q, want oncall", got)
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec == nil || rec.ActiveTask == nil {
		t.Fatal("no active profile task")
	}
	return rec.ActiveTask.TaskID
}

func steerAuthors(t *testing.T, r *rig, conv, taskID string) []TaskRequester {
	t.Helper()
	rec, _ := r.g.reg.Get(context.Background(), conv)
	ref, _ := rec.TaskRefFor(taskID)
	return ref.SteerAuthors
}

// A steer into an oncall task from an author at read is refused with a
// notice and an audit line, and its author isn't recorded.
func TestALowerTierSteerIsRefused(t *testing.T) {
	r := startTierRig(t, "1001")
	conv := "discord:1/steer"
	taskID := startOncallTask(t, r, conv)

	steerAs(r, conv, "st-1", "1002", "and the other cluster too")
	waitFor(t, "the notice", postedContaining(r, fmt.Sprintf(noticeSteerTierDiffers, "oncall", "read")))
	waitFor(t, "the audit line", loggedContaining(r, "steer refused", "rule="+ruleSteerTier, taskID))
	if got := steerAuthors(t, r, conv, taskID); len(got) != 0 {
		t.Fatalf("a refused steer recorded its author: %+v", got)
	}
}

// A steer from another author at the task's tier goes through.
func TestASameTierSteerGoes(t *testing.T) {
	r := startTierRig(t, "1001", "1002")
	conv := "discord:1/steer"
	taskID := startOncallTask(t, r, conv)

	steerAs(r, conv, "st-1", "1002", "include costs")
	waitFor(t, "the steer recorded", func() bool { return len(steerAuthors(t, r, conv, taskID)) == 1 })
	if loggedContaining(r, "steer refused")() {
		t.Fatal("a same-tier steer was refused")
	}
}

// A thread whose authors are at different tiers can't start a profile task:
// 1001 (oncall) asked for a task here before, now 1002 (read) asks for one,
// and nothing is submitted.
func TestAMixedTierThreadIsRefused(t *testing.T) {
	r := startTierRig(t, "1001")
	conv := "discord:1/mixed"
	earlier := TaskRequester{Backend: discordBackend, Subject: requesterSubject(r.g.ps, discordBackend, "1001")}
	if err := r.g.reg.Put(context.Background(), &SessionRecord{
		Key: conv, ContextID: "ctx-mixed", Kind: "group",
		Tasks: []TaskRef{{ID: "task-earlier", Requester: &earlier}},
	}); err != nil {
		t.Fatal(err)
	}
	turn := profileTurn(conv, "cluster", targetParams("p1", "us-east1", "prod"))
	turn.AuthorID = "1002"
	r.adapter.inbox <- turn
	waitFor(t, "the notice", postedContaining(r, fmt.Sprintf(noticeProfileTiersDiffer, "oncall, read")))
	if got := streamEnvelopes(t, r.url, lib.SubmitSubjectPrefix+"cluster.>"); len(got) != 0 {
		t.Fatalf("a mixed-tier thread submitted %d tasks", len(got))
	}
}
