package dispatch

import (
	"context"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A hash-only reference (an operator from #5479 on) reads the profile's own
// param by its derived name.
func TestAHashOnlyJobTemplateRefReadsTheProfilesOwnParam(t *testing.T) {
	var asked string
	k := &KubeJobs{getTemplate: func(_ context.Context, _ string, name string) ([]byte, error) {
		asked = name
		return []byte(`{"metadata":{"annotations":{"` + lib.DispatchJobTemplateHashAnnotation + `":"aaaa"}},"spec":{"template":{"spec":{"containers":[{"name":"w"}]}}}}`), nil
	}}
	prof := &lib.DispatchProfile{Name: "tse", JobTemplateRef: &lib.DispatchJobTemplateRef{Hash: "aaaa"}}
	if tmpl, err := k.jobTemplate(context.Background(), "ns", prof); err != nil || len(tmpl) == 0 {
		t.Fatalf("template %s, err %v: want the param's", tmpl, err)
	}
	if asked != lib.DispatchJobTemplateName("tse") {
		t.Errorf("read %q; want the profile's own param %q", asked, lib.DispatchJobTemplateName("tse"))
	}
}

// A config whose profiles list no durables (an operator from #5479 on)
// binds the derived ones and dispatches as before.
func TestAProfileWithNoListedDurablesDispatches(t *testing.T) {
	b := newBus(t, "tse-1")
	b.ensure("tse", 2, 0, "read")
	b.worker("tse", "read", "tse-1", answerAccept, time.Second)
	prof := testProfile("tse", 2)
	prof.Durables = nil
	ti := &testInstall{src: NewStaticSource(testDispatchConfig(6, prof)), pool: NewStaticPool()}
	ti.pool.Set("tse", "read", "tse-1")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
}
