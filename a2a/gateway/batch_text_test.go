package gateway

import (
	"context"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// At the ask bound, a profile batch child's end that was never sent is
// dropped with its text: by then the same bound has cleared the parent's
// attribution, which a send needs, so nothing could send it. A sent end
// keeps its state and loses only its text. After that one pass nothing is
// left to clear, so the next pass rewrites nothing.
func TestTheAskBoundDropsAnUnsentBatchEndOnce(t *testing.T) {
	t.Parallel()
	r := startRig(t)
	ctx := context.Background()
	r.g.cfg.AskTTL = time.Hour
	start := time.Now().UTC().Truncate(time.Second)
	conv := "trigger:schedule/kubeagents-system/unsent-bound"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-unsent-bound", Kind: "group", LastActivity: start,
		Tasks: []TaskRef{
			{ID: "p", Addressee: "fleet-audit", Tier: "unattended", StartedAt: start, BatchSize: 2,
				Children: []string{"c1", "c2"}, Attribution: []byte(`{}`)},
			{ID: "c1", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "p", BatchIndex: 1, StartedAt: start,
				BatchEnd: &BatchEnd{State: lib.StateCompleted, Text: "one done", Unsent: true}},
			{ID: "c2", Addressee: "cluster", Tier: "read", Role: taskRoleChild, ParentTaskID: "p", BatchIndex: 2, StartedAt: start,
				BatchEnd: &BatchEnd{State: lib.StateCompleted, Text: "two done"}},
		},
	}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	past := start.Add(time.Hour)
	r.g.boundAskCopyAt(ctx, rec, past)
	got, _ := r.g.reg.Get(ctx, conv)
	c1, _ := got.TaskRefFor("c1")
	if c1.BatchEnd == nil || c1.BatchEnd.Text != "" || c1.BatchEnd.Unsent || c1.BatchEnd.State != lib.StateCompleted {
		t.Fatalf("unsent end after the bound = %+v, want its text and Unsent cleared, its state kept", c1.BatchEnd)
	}
	c2, _ := got.TaskRefFor("c2")
	if c2.BatchEnd == nil || c2.BatchEnd.Text != "" || c2.BatchEnd.State != lib.StateCompleted {
		t.Fatalf("sent end after the bound = %+v, want its text cleared, its state kept", c2.BatchEnd)
	}
	if r.g.batchTextExpired(got, past.Add(time.Minute)) {
		t.Fatal("the next pass would clear again: the bound would rewrite the record every pass")
	}
	if parent, _ := got.TaskRefFor("p"); got.batchLeft(parent) != 0 {
		t.Fatalf("batchLeft = %d, want 0: a dropped end still ends its entry", got.batchLeft(parent))
	}
}
