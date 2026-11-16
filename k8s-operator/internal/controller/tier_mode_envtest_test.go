package controller

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A tier's mode can't change once set (#3659): a grant already minted at the
// tier would gain or lose verbs with it. Other spec edits still go through.
func TestATiersModeCantChange(t *testing.T) {
	c, _ := startEnvtest(t)
	ctx := context.Background()
	tier := &agentv1alpha1.Tier{
		ObjectMeta: metav1.ObjectMeta{Name: "nightly", Namespace: "default"},
		Spec:       agentv1alpha1.TierSpec{Mode: agentv1alpha1.TierModeUnattended, WriteMode: agentv1alpha1.TierWriteModePropose},
	}
	if err := c.Create(ctx, tier); err != nil {
		t.Fatal(err)
	}
	tier.Spec.Mode = agentv1alpha1.TierModeInteractive
	if err := c.Update(ctx, tier); err == nil || !strings.Contains(err.Error(), "mode can't change") {
		t.Fatalf("a mode change = %v, want the immutability refusal", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: tier.Namespace, Name: tier.Name}, tier); err != nil {
		t.Fatal(err)
	}
	tier.Spec.WriteMode = agentv1alpha1.TierWriteModeNone
	if err := c.Update(ctx, tier); err != nil {
		t.Fatalf("a writeMode edit was refused: %v", err)
	}
}
