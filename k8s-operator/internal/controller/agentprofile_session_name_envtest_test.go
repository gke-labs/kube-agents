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
	// sessionNameRuleMessage is the start of the CRD rule's message, which
	// a refused create carries.
	sessionNameRuleMessage = "AgentProfile name 'session' is reserved"
	// sessionNameRuleMarker finds the rule in the generated CRD.
	sessionNameRuleMarker = "'session'"
)

// agentProfileCRDBeforeSessionRule is this release's AgentProfile CRD with the
// `session` name rule taken out, the CRD an install had before it, so a test
// can create a profile named `session` and then upgrade the CRD under it.
func agentProfileCRDBeforeSessionRule(t *testing.T) (old []byte, full map[string]any) {
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
	removed := 0
	for _, version := range doc["spec"].(map[string]any)["versions"].([]any) {
		root := version.(map[string]any)["schema"].(map[string]any)["openAPIV3Schema"].(map[string]any)
		var kept []any
		for _, r := range root["x-kubernetes-validations"].([]any) {
			if strings.Contains(r.(map[string]any)["rule"].(string), sessionNameRuleMarker) {
				removed++
				continue
			}
			kept = append(kept, r)
		}
		root["x-kubernetes-validations"] = kept
	}
	if removed == 0 {
		t.Fatal("the CRD has no `session` name rule to remove; the fixture would not be an upgrade, and the rule this test holds is gone")
	}
	old, err = yaml.Marshal(doc)
	if err != nil {
		t.Fatalf("serialising the old CRD: %v", err)
	}
	return old, full
}

// A profile named `session` before the CRD reserved the name must keep
// working after the upgrade: a rule on the root object runs on every update,
// so without the oldSelf ratchet its spec edits, status writes and finalizer
// removal would all be refused and a delete would leave it Terminating. A new
// profile of that name is refused with the rule's message.
func TestAProfileNamedSessionSurvivesTheCRDUpgrade(t *testing.T) {
	old, full := agentProfileCRDBeforeSessionRule(t)
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
	const ns = "agentprofile-session-upgrade"
	createIn := func(namespace string) error {
		if err := c.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
			return fmt.Errorf("namespace %s: %w", namespace, err)
		}
		p := testAgentProfile(namespace, agentv1alpha1.DefaultRouteSession)
		p.ResourceVersion, p.Generation, p.UID = "", 0, ""
		p.Finalizers = []string{agentProfileFinalizer}
		return c.Create(ctx, &p)
	}
	if err := createIn(ns); err != nil {
		t.Fatalf("creating a profile named %q under the old CRD: %v", agentv1alpha1.DefaultRouteSession, err)
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
	getIn := func(namespace string) *agentv1alpha1.AgentProfile {
		t.Helper()
		var p agentv1alpha1.AgentProfile
		if err := c.Get(ctx, client.ObjectKey{Namespace: namespace, Name: agentv1alpha1.DefaultRouteSession}, &p); err != nil {
			t.Fatalf("get %s/%s: %v", namespace, agentv1alpha1.DefaultRouteSession, err)
		}
		return &p
	}

	// The new rule is in force once a fresh `session` create is refused.
	// Until the server picks up the new CRD such a create is still admitted,
	// so each probe gets its own namespace and an admitted one is deleted.
	deadline := time.Now().Add(crdSettleTimeout)
	for i := 0; ; i++ {
		probeNS := fmt.Sprintf("%s-probe-%03d", ns, i)
		err := createIn(probeNS)
		if err != nil && strings.Contains(err.Error(), sessionNameRuleMessage) {
			break
		}
		if err == nil {
			p := getIn(probeNS)
			p.Finalizers = nil
			if err := c.Update(ctx, p); err != nil {
				t.Fatalf("clearing a probe admitted under the old CRD: %v", err)
			}
			if err := c.Delete(ctx, p); err != nil {
				t.Fatalf("deleting a probe admitted under the old CRD: %v", err)
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("the upgraded CRD never refused a new profile named %q: %v", agentv1alpha1.DefaultRouteSession, err)
		}
		time.Sleep(200 * time.Millisecond)
	}

	// The existing profile: spec, status, delete and finalizer removal all go
	// through.
	p := getIn(ns)
	p.Spec.Description = "edited after the upgrade"
	if err := c.Update(ctx, p); err != nil {
		t.Errorf("spec update on the existing %q profile: %v", agentv1alpha1.DefaultRouteSession, err)
	}
	p = getIn(ns)
	p.Status.AgentRef = "after-upgrade"
	if err := c.Status().Update(ctx, p); err != nil {
		t.Errorf("status write on the existing %q profile: %v", agentv1alpha1.DefaultRouteSession, err)
	}
	if err := c.Delete(ctx, getIn(ns)); err != nil {
		t.Errorf("delete of the existing %q profile: %v", agentv1alpha1.DefaultRouteSession, err)
	}
	p = getIn(ns)
	p.Finalizers = nil
	if err := c.Update(ctx, p); err != nil {
		t.Errorf("finalizer removal on the existing %q profile: %v", agentv1alpha1.DefaultRouteSession, err)
	}
	var gone agentv1alpha1.AgentProfile
	if err := c.Get(ctx, client.ObjectKey{Namespace: ns, Name: agentv1alpha1.DefaultRouteSession}, &gone); err == nil {
		t.Errorf("the %q profile is still there (finalizers %v, deleting %v)", agentv1alpha1.DefaultRouteSession, gone.Finalizers, gone.DeletionTimestamp)
	}
}
