package gateway

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// scheduledOrigin submits s as a schedule's run and returns its submission.
func scheduledOrigin(t *testing.T, tr *triggerRig, s lib.TriggerSubmit) lib.Envelope {
	t.Helper()
	if reply := tr.submit(t, "schedule", s); reply.Refused != nil {
		t.Fatalf("refused: %+v", reply.Refused)
	}
	waitFor(t, "the submission", func() bool { return len(tr.submissions()) == 1 })
	var origin lib.Envelope
	if err := json.Unmarshal(tr.submissions()[0].Data, &origin); err != nil {
		t.Fatal(err)
	}
	return origin
}

// TestAScheduledRunIsSubmittedWithNoChangeRequest: a run that a schedule
// starts never asks for a change. The dispatcher's permit rule reads the
// submission's change request and its cluster target and does not look at
// the trigger, so the gateway's submission is what keeps the confirmed-change
// permit off a scheduled run. The run with a cluster target is the one the
// rule would act on; a delegating profile's submission carries one part more.
func TestAScheduledRunIsSubmittedWithNoChangeRequest(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		card   *lib.AgentCard // replaces the rig's card of the same name
		submit func(*lib.TriggerSubmit)
		// target says the submission carries a taskParameters part.
		target bool
	}{
		"a delegating profile":     {},
		"a non-delegating profile": {card: &lib.AgentCard{Name: "fleet-audit", Tiers: []string{"read", "unattended"}}},
		"a cluster target": {target: true, submit: func(s *lib.TriggerSubmit) {
			s.Profile, s.Parameters = "cluster", targetParams("p1", "us-east1", "prod")
		}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tr := startTriggerRig(t, nil)
			if tc.card != nil {
				tr.g.profiles.put(tc.card.Name, tc.card)
			}
			s := goodScheduleSubmit()
			if tc.submit != nil {
				tc.submit(&s)
			}
			origin := scheduledOrigin(t, tr, s)
			if found, change := submittedChange(t, &origin); found != tc.target || change {
				t.Fatalf("the scheduled run's submission: taskParameters part %v, change request %v; want %v, false", found, change, tc.target)
			}
			rec, err := tr.g.reg.Get(context.Background(), triggerConversationKey(lib.TriggerSourceSchedule, s.Schedule))
			if err != nil || rec == nil {
				t.Fatalf("the run's record: %v", err)
			}
			entry, ok := rec.TaskRefFor(origin.TaskID)
			if !ok {
				t.Fatal("the run has no entry on its record")
			}
			if entry.ChangeRequested {
				t.Fatal("the scheduled run's entry holds a change request; each child copies it")
			}
		})
	}
}

// TestAScheduledRunsChildrenAreSubmittedWithNoChangeRequest: a child copies
// its parent entry's change request. A child of a scheduled run that names a
// cluster and carried one would be a task the dispatcher gives the
// confirmed-change permit, or refuses for want of a Job runner. So a child's
// submission and its entry carry none, for a single child and for a batch.
func TestAScheduledRunsChildrenAreSubmittedWithNoChangeRequest(t *testing.T) {
	t.Parallel()
	for name, tasks := range map[string][]lib.DelegateTask{
		"a single child": {
			{Addressee: "cluster", Text: "look", Parameters: targetParams("p1", "us-east1", "prod")},
		},
		"a batch": {
			{Addressee: "cluster", Text: "a", Parameters: targetParams("p1", "us-east1", "a")},
			{Addressee: "cluster", Text: "b", Parameters: targetParams("p1", "us-east1", "b")},
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			tr := startTriggerRig(t, func(c *Config) { c.DelegationWidthMax = 10 })
			ctx := context.Background()
			_, parent := startScheduledParent(t, tr)
			art := delegateBatchArtifact(t, tasks...)
			if len(tasks) == 1 {
				art = delegateArtifactWithParameters(t, tasks[0].Addressee, tasks[0].Text, tasks[0].Parameters)
			}
			if err := parent.PublishArtifact(ctx, art); err != nil {
				t.Fatal(err)
			}
			// The parent is on fleet-audit, so cluster's queue holds the children alone.
			var children []subjectEnvelope
			waitFor(t, "the children's submissions", func() bool {
				children = streamEnvelopes(t, tr.url, lib.SubmitSubjectPrefix+"cluster.>")
				return len(children) == len(tasks)
			})
			rec, err := tr.g.reg.Get(ctx, triggerConversationKey(lib.TriggerSourceSchedule, goodScheduleSubmit().Schedule))
			if err != nil || rec == nil {
				t.Fatalf("the run's record: %v", err)
			}
			for _, child := range children {
				id := child.env.TaskID
				if found, change := submittedChange(t, child.env); !found || change {
					t.Fatalf("child %s: taskParameters part %v, change request %v; want its cluster target and no change request", id, found, change)
				}
				entry, ok := rec.TaskRefFor(id)
				if !ok {
					t.Fatalf("child %s has no entry on the run's record", id)
				}
				if entry.ChangeRequested {
					t.Fatalf("child %s of a scheduled run holds a change request on its entry", id)
				}
			}
		})
	}
}
