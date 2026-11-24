package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// agentProfileCRDLooseTiers is this release's AgentProfile CRD with
// allowedTiers' item pattern, item length and item count taken off, so a test
// can store values the release's rules refuse, then upgrade the CRD under
// them. No shipped release could store such a value (they took the read and
// unattended enum), so this is the worst case for the upgrade, not a real one.
func agentProfileCRDLooseTiers(t *testing.T) (loose []byte, full map[string]any) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(envtestCRDDir, agentProfileCRDFile))
	if err != nil {
		t.Fatalf("reading the CRD: %v", err)
	}
	if err := yaml.Unmarshal(raw, &full); err != nil {
		t.Fatalf("parsing the CRD: %v", err)
	}
	var doc map[string]any
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parsing the CRD: %v", err)
	}
	loosened := 0
	for _, version := range doc["spec"].(map[string]any)["versions"].([]any) {
		root := version.(map[string]any)["schema"].(map[string]any)["openAPIV3Schema"].(map[string]any)
		spec := root["properties"].(map[string]any)["spec"].(map[string]any)
		tiers := spec["properties"].(map[string]any)["allowedTiers"].(map[string]any)
		delete(tiers, "maxItems")
		items := tiers["items"].(map[string]any)
		delete(items, "pattern")
		delete(items, "maxLength")
		loosened++
	}
	if loosened == 0 {
		t.Fatal("the CRD has no allowedTiers to loosen")
	}
	loose, err = yaml.Marshal(doc)
	if err != nil {
		t.Fatalf("serialising the loose CRD: %v", err)
	}
	return loose, full
}

// A profile whose stored allowedTiers breaks the release's tier rules (a
// hyphen, over 10 characters, more than 4) stays writable after the CRD
// upgrade: status, an unrelated spec edit, finalizer removal and delete all go
// through, because the rules ratchet on values a write doesn't change. A write
// that changes allowedTiers is held to the rules.
func TestAProfileStoredBeforeTheTierRulesSurvivesTheCRDUpgrade(t *testing.T) {
	loose, full := agentProfileCRDLooseTiers(t)
	crdDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(crdDir, agentProfileCRDFile), loose, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, scheme := startEnvtestConfigWithCRDs(t, crdDir)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	const ns = "agentprofile-tier-upgrade"
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	create := func(name string, tiers ...string) error {
		p := testAgentProfile(ns, name, func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = tiers })
		p.ResourceVersion, p.Generation, p.UID = "", 0, ""
		p.Finalizers = []string{agentProfileFinalizer}
		return c.Create(ctx, &p)
	}
	stored := map[string][]string{
		"hyphen": {"read", "on-call"},
		"long":   {"read", "elevenchars"},
		"many":   {"read", "a", "b", "c", "d"},
	}
	for name, tiers := range stored {
		if err := create(name, tiers...); err != nil {
			t.Fatalf("storing %s under the loose CRD: %v", name, err)
		}
	}

	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"})
	if err := c.Get(ctx, client.ObjectKey{Name: agentProfileCRDName}, live); err != nil {
		t.Fatalf("reading the served CRD: %v", err)
	}
	live.Object["spec"] = full["spec"]
	if err := c.Update(ctx, live); err != nil {
		t.Fatalf("upgrading the CRD: %v", err)
	}
	get := func(name string) *agentv1alpha1.AgentProfile {
		t.Helper()
		var p agentv1alpha1.AgentProfile
		if err := c.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &p); err != nil {
			t.Fatalf("get %s: %v", name, err)
		}
		return &p
	}
	// The release's rules are in force once a fresh hyphenated tier is
	// refused; an admitted probe is removed.
	deadline := time.Now().Add(crdSettleTimeout)
	for i := 0; ; i++ {
		probe := fmt.Sprintf("probe-%03d", i)
		err := create(probe, "read", "on-call")
		if err != nil {
			break
		}
		p := get(probe)
		p.Finalizers = nil
		if err := c.Update(ctx, p); err != nil {
			t.Fatalf("clearing a probe admitted under the loose CRD: %v", err)
		}
		if err := c.Delete(ctx, p); err != nil {
			t.Fatalf("deleting a probe admitted under the loose CRD: %v", err)
		}
		if time.Now().After(deadline) {
			t.Fatal("the upgraded CRD never refused a hyphenated tier")
		}
		time.Sleep(200 * time.Millisecond)
	}

	for name := range stored {
		p := get(name)
		p.Status.AgentRef = "after-upgrade"
		if err := c.Status().Update(ctx, p); err != nil {
			t.Errorf("%s: status write after the upgrade: %v", name, err)
		}
		p = get(name)
		p.Spec.Description = "edited after the upgrade"
		if err := c.Update(ctx, p); err != nil {
			t.Errorf("%s: unrelated spec edit after the upgrade: %v", name, err)
		}
	}
	// Changing allowedTiers is held to the rules: adding another bad name is
	// refused.
	p := get("hyphen")
	p.Spec.AllowedTiers = []string{"read", "on-call", "night-shift"}
	if err := c.Update(ctx, p); err == nil {
		t.Error("a write adding a hyphenated tier was admitted")
	}
	for name := range stored {
		if err := c.Delete(ctx, get(name)); err != nil {
			t.Errorf("%s: delete after the upgrade: %v", name, err)
		}
		p := get(name)
		p.Finalizers = nil
		if err := c.Update(ctx, p); err != nil {
			t.Errorf("%s: finalizer removal after the upgrade: %v", name, err)
		}
	}
}
