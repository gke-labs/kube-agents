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

// The roll window this change opens (#5479): the operator applies the
// ConfigMap before the dispatcher Deployment, so a dispatcher from before
// #5479 reads a config it refuses (it requires listed durables). A refusing
// reader keeps serving its last good config rather than stopping, and the
// dispatcher reads nothing but that store. Here the store holds a config an
// operator of either shape could write, then is handed one it refuses (a
// listed durable set that isn't the derived one, the refusal this change
// adds): it keeps the last good config, stays ready, and says why.
func TestARefusedConfigKeepsTheLastGoodServing(t *testing.T) {
	s := NewConfigStore(quietLog())
	good := testDispatchConfig(6, testProfile("tse", 2)) // lists its durables, as an older operator does
	good.Version = "old-operator"
	if err := s.Update(rawConfig(t, good)); err != nil {
		t.Fatal(err)
	}
	refused := testDispatchConfig(6, testProfile("tse", 2, "read", "unattended"))
	refused.Version = "refused"
	refused.Profiles[0].Durables = refused.Profiles[0].Durables[:1]
	if err := s.Update(rawConfig(t, refused)); err == nil {
		t.Fatal("a listed durable set that isn't the derived one was accepted")
	}
	if !s.Ready() || s.Config().Version != "old-operator" || s.LastError() == "" {
		t.Fatalf("ready %v, serving %q, last error %q: want the last good config still serving", s.Ready(), s.Config().Version, s.LastError())
	}
	if got := s.Config().Profiles[0].DerivedDurables(); len(got) != 2 {
		t.Errorf("the config still serving derives %d durables; want 2", len(got))
	}
	// A config with no listed durables (this operator's shape) loads.
	current := testDispatchConfig(6, testProfile("tse", 2))
	current.Version = "new-operator"
	current.Profiles[0].Durables = nil
	if err := s.Update(rawConfig(t, current)); err != nil || s.Config().Version != "new-operator" {
		t.Fatalf("the new shape: err %v, serving %q", err, s.Config().Version)
	}
}
