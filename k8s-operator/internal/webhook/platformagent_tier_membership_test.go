package webhook

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Step 7 is wired: a person mapped to an unattended Tier is refused on
// spec.tiers.membership, and one mapped to read is admitted (#3659).
func TestTheWebhookChecksTierMembership(t *testing.T) {
	scheme := runtime.NewScheme()
	_ = clientgoscheme.AddToScheme(scheme)
	_ = agentv1alpha1.AddToScheme(scheme)
	nightly := &agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "nightly", Namespace: "default"}, Spec: agentv1alpha1.TierSpec{Mode: agentv1alpha1.TierModeUnattended}}
	val := &PlatformAgentCustomValidator{Client: fake.NewClientBuilder().WithScheme(scheme).WithObjects(nightly).Build()}
	agent := func(tier string) *agentv1alpha1.PlatformAgent {
		a := storageAgent("")
		a.Spec.Tiers = &agentv1alpha1.PlatformAgentTiersSpec{Membership: []agentv1alpha1.TierMember{{Backend: "slack", User: "U1", Tier: tier}}}
		return a
	}
	_, err := val.ValidateCreate(context.Background(), agent("nightly"))
	assertFieldError(t, err, "spec.tiers.membership[0].tier")
	if _, err := val.ValidateCreate(context.Background(), agent("read")); err != nil {
		t.Fatalf("read refused: %v", err)
	}
}
