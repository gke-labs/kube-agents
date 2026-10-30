package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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

const (
	agentProfileCRDFile = "kubeagents.x-k8s.io_agentprofiles.yaml"
	agentProfileCRDName = "agentprofiles.kubeagents.x-k8s.io"
)

// agentProfileCRDBeforeTiers is this release's AgentProfile CRD with the
// per-tier name bound swapped for the flat 55 that 0.19.0 shipped, so a test
// can create what an older install could, then upgrade the CRD under it.
func agentProfileCRDBeforeTiers(t *testing.T) (old []byte, full map[string]any) {
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
	swapped := 0
	for _, version := range doc["spec"].(map[string]any)["versions"].([]any) {
		root := version.(map[string]any)["schema"].(map[string]any)["openAPIV3Schema"].(map[string]any)
		rules := root["x-kubernetes-validations"].([]any)
		for i, r := range rules {
			rule := r.(map[string]any)
			if strings.Contains(rule["rule"].(string), "? 44 : 50") {
				rules[i] = map[string]any{"rule": "self.metadata.name.size() <= 55", "message": "at most 55 characters"}
				swapped++
			}
		}
	}
	if swapped == 0 {
		t.Fatal("the CRD has no per-tier name rule to swap; the fixture would not be an upgrade")
	}
	old, err = yaml.Marshal(doc)
	if err != nil {
		t.Fatalf("serialising the old CRD: %v", err)
	}
	return old, full
}

// A profile named for 0.19.0's 55-character bound must stay writable after the
// CRD moves to the per-tier bound: a rule on the root object runs on every
// update, so without a ratchet its status, its finalizer removal and the
// documented hand-clear would all be refused, and a delete would leave it
// Terminating. The bound still holds for anything new: a new profile that long
// is refused, and an existing one can't take on a tier its name is too long for.
func TestAProfileNamedUnderTheOldBoundSurvivesTheCRDUpgrade(t *testing.T) {
	old, full := agentProfileCRDBeforeTiers(t)
	crdDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(crdDir, agentProfileCRDFile), old, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, scheme := startEnvtestConfigWithCRDs(t, crdDir)
	c, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	const ns = "agentprofile-upgrade"
	if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	create := func(name string, tiers ...string) error {
		p := testAgentProfile(ns, name, func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = tiers })
		p.ResourceVersion, p.Generation, p.UID = "", 0, ""
		p.Finalizers = []string{agentProfileFinalizer}
		return c.Create(ctx, &p)
	}
	long, mid, unattended := strings.Repeat("l", 55), strings.Repeat("m", 48), strings.Repeat("u", 45)
	for _, name := range []string{long, mid} {
		if err := create(name); err != nil {
			t.Fatalf("creating %d-character profile under the old CRD: %v", len(name), err)
		}
	}
	if err := create(unattended, agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended); err != nil {
		t.Fatalf("creating the 45-character unattended profile under the old CRD: %v", err)
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

	// The new rules are in force once a fresh 55-character create is refused.
	// Until the server picks up the new CRD such a create is still admitted,
	// so each probe gets its own name and an admitted one is deleted.
	deadline := time.Now().Add(crdSettleTimeout)
	for i := 0; ; i++ {
		probe := fmt.Sprintf("n%03d%s", i, strings.Repeat("n", 51))
		err := create(probe)
		if err != nil && strings.Contains(err.Error(), "too long for its tiers") {
			break
		}
		if err == nil {
			p := get(probe)
			p.Finalizers = nil
			if err := c.Update(ctx, p); err != nil {
				t.Fatalf("clearing a probe admitted under the old CRD: %v", err)
			}
			if err := c.Delete(ctx, p); err != nil {
				t.Fatalf("deleting a probe admitted under the old CRD: %v", err)
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("the upgraded CRD never refused a new 55-character profile: %v", err)
		}
		time.Sleep(200 * time.Millisecond)
	}

	// The old profile: status, spec, finalizer removal and delete all go through.
	p := get(long)
	p.Status.AgentRef = "after-upgrade"
	if err := c.Status().Update(ctx, p); err != nil {
		t.Errorf("status write on the 55-character profile: %v", err)
	}
	p = get(long)
	p.Spec.Description = "edited after the upgrade"
	if err := c.Update(ctx, p); err != nil {
		t.Errorf("spec update on the 55-character profile: %v", err)
	}
	if err := c.Delete(ctx, get(long)); err != nil {
		t.Errorf("delete of the 55-character profile: %v", err)
	}
	p = get(long)
	p.Finalizers = nil
	if err := c.Update(ctx, p); err != nil {
		t.Errorf("finalizer removal on the 55-character profile: %v", err)
	}
	var gone agentv1alpha1.AgentProfile
	if err := c.Get(ctx, client.ObjectKey{Namespace: ns, Name: long}, &gone); err == nil {
		t.Errorf("the 55-character profile is still there (finalizers %v, deleting %v)", gone.Finalizers, gone.DeletionTimestamp)
	}

	// A 45-character profile that already allowed unattended broke the new
	// bound before the upgrade too, so it stays writable as well.
	p = get(unattended)
	p.Spec.Description = "edited after the upgrade"
	if err := c.Update(ctx, p); err != nil {
		t.Errorf("spec update on the 45-character unattended profile: %v", err)
	}

	// A 48-character profile fits read; taking on unattended would newly break
	// the bound, which the ratchet still refuses.
	p = get(mid)
	p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	if err := c.Update(ctx, p); err == nil || !strings.Contains(err.Error(), "too long for its tiers") {
		t.Errorf("a 48-character profile took on unattended: %v", err)
	}
}
