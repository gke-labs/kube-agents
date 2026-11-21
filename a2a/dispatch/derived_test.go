package dispatch

import (
	"context"
	"encoding/json"
	"strings"
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
	if tmpl, err := k.jobTemplate(context.Background(), "ns", prof, "unattended"); err != nil || len(tmpl) == 0 {
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
	ti := &testInstall{src: NewStaticSource(testDispatchConfig(6, prof)), pool: NewStaticPool()}
	ti.pool.Set("tse", "read", "tse-1")
	b.runDispatcher(testConfig(), ti, &fakeVerifier{})

	b.submit("tse", "read", lib.SubmitKindTop, "task-1")
	if ts := b.waitTerminal("task-1", 10*time.Second); ts.state != lib.StateCompleted {
		t.Fatalf("terminal %+v", ts)
	}
}

// A refused config leaves the last good one serving: the store keeps it,
// stays ready, and says why.
func TestARefusedConfigKeepsTheLastGoodServing(t *testing.T) {
	s := NewConfigStore(quietLog())
	good := testDispatchConfig(6, testProfile("tse", 2))
	good.Version = "good"
	if err := s.Update(rawConfig(t, good)); err != nil {
		t.Fatal(err)
	}
	refused := testDispatchConfig(6, testProfile("tse", 2))
	refused.Version = "refused"
	refused.Profiles[0].Concurrency = 0
	if err := s.Update(rawConfig(t, refused)); err == nil {
		t.Fatal("a profile with no concurrency was accepted")
	}
	if !s.Ready() || s.Config().Version != "good" || s.LastError() == "" {
		t.Fatalf("ready %v, serving %q, last error %q: want the last good config still serving", s.Ready(), s.Config().Version, s.LastError())
	}
}

// The fields the dispatcher no longer reads (#5639), which no operator from
// #5649 on writes, are ignored if a config still carries them: a listed
// durable set, even one that isn't the derived one; an inline jobTemplate;
// and a jobTemplateRef name, even another profile's param. The durables come
// from the tiers, and the template from the profile's own param.
func TestAConfigsDroppedFieldsAreIgnored(t *testing.T) {
	s := NewConfigStore(quietLog())
	var doc map[string]any
	if err := json.Unmarshal(rawConfig(t, testDispatchConfig(6, testProfile("tse", 2, "read", "unattended"))), &doc); err != nil {
		t.Fatal(err)
	}
	p := doc["profiles"].([]any)[0].(map[string]any)
	p["durables"] = []any{map[string]any{"tier": "read", "kind": "top", "name": lib.ProfileDurableName("tse", "read", "top")}}
	p["jobTemplate"] = map[string]any{"spec": map[string]any{"containers": []any{map[string]any{"name": "inline"}}}}
	p["jobTemplateRef"] = map[string]any{"name": lib.DispatchJobTemplateName("other"), "hash": "aaaa"}
	raw, merr := json.Marshal(doc)
	if merr != nil {
		t.Fatal(merr)
	}
	if err := s.Update(raw); err != nil {
		t.Fatalf("a config carrying the dropped fields was refused: %v", err)
	}
	prof := s.Config().Profiles[0]
	if got := prof.DerivedDurables(); len(got) != 4 {
		t.Errorf("derived %d durables; want 4, one per tier and kind", len(got))
	}
	var asked string
	k := &KubeJobs{getTemplate: func(_ context.Context, _ string, name string) ([]byte, error) {
		asked = name
		return []byte(`{"metadata":{"annotations":{"` + lib.DispatchJobTemplateHashAnnotation + `":"aaaa"}},"spec":{"template":{"spec":{"containers":[{"name":"w"}]}}}}`), nil
	}}
	tmpl, err := k.jobTemplate(context.Background(), "ns", &prof, "unattended")
	if err != nil || strings.Contains(string(tmpl), "inline") {
		t.Fatalf("template %s, err %v: want the param's, not the inline copy", tmpl, err)
	}
	if asked != lib.DispatchJobTemplateName("tse") {
		t.Errorf("read %q; want the profile's own param %q", asked, lib.DispatchJobTemplateName("tse"))
	}
}
