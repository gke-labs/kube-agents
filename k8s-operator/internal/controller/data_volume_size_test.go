/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	storagev1 "k8s.io/api/storage/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// sizedAgent is a PlatformAgent whose spec.storage.size is size, or which has
// no storage block when size is "".
func sizedAgent(size string) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name: "size-agent", Namespace: "test-ns", UID: types.UID("size-agent-uid"),
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster"},
		},
	}
	if size != "" {
		agent.Spec.Storage = &agentv1alpha1.PlatformAgentStorageSpec{Size: ptr.To(resource.MustParse(size))}
	}
	return agent
}

// sandboxDataClaimRequest is the storage request of the sandbox StatefulSet's
// data claim template.
func sandboxDataClaimRequest(t *testing.T, agent *agentv1alpha1.PlatformAgent) resource.Quantity {
	t.Helper()
	sts := buildShellSandboxStatefulSet(agent, shellSandboxAuthorizedKeysSecretName(agent), credentialProxySandboxURL(agent), "hash")
	for _, tmpl := range sts.Spec.VolumeClaimTemplates {
		if tmpl.Name == shellSandboxDataVolume {
			return tmpl.Spec.Resources.Requests[corev1.ResourceStorage]
		}
	}
	t.Fatalf("the sandbox StatefulSet has no %s claim template", shellSandboxDataVolume)
	return resource.Quantity{}
}

// TestBothDataClaimsFollowTheResolvedSize pins the mirror invariant: the
// agent's data claim and the sandbox's data claim template always request the
// same size, the default when spec.storage.size is absent and the field when
// it is set.
func TestBothDataClaimsFollowTheResolvedSize(t *testing.T) {
	cases := []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  string
	}{
		{"no storage block takes the default", sizedAgent(""), agentDataStorageSize},
		{"a storage block without a size takes the default", func() *agentv1alpha1.PlatformAgent {
			agent := sizedAgent("")
			agent.Spec.Storage = &agentv1alpha1.PlatformAgentStorageSpec{}
			return agent
		}(), agentDataStorageSize},
		{"an explicit size is used", sizedAgent("80Gi"), "80Gi"},
		{"a size below the default is used on a new install", sizedAgent("20Gi"), "20Gi"},
		{"a zero size, which only a webhook-off install admits, takes the default", sizedAgent("0"), agentDataStorageSize},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			want := resource.MustParse(tc.want)
			agentClaim := buildPVC(tc.agent).Spec.Resources.Requests[corev1.ResourceStorage]
			if agentClaim.Cmp(want) != 0 {
				t.Errorf("agent data claim requests %s, want %s", agentClaim.String(), want.String())
			}
			sandboxClaim := sandboxDataClaimRequest(t, tc.agent)
			if sandboxClaim.Cmp(want) != 0 {
				t.Errorf("sandbox data claim template requests %s, want %s", sandboxClaim.String(), want.String())
			}
			if agentClaim.Cmp(sandboxClaim) != 0 {
				t.Errorf("the two data claims differ (%s and %s) with no expected profile count", agentClaim.String(), sandboxClaim.String())
			}
		})
	}
}

// profileCountAgent is sizedAgent with spec.storage.expectedClusterProfiles = n.
func profileCountAgent(size string, n int32) *agentv1alpha1.PlatformAgent {
	agent := sizedAgent(size)
	if agent.Spec.Storage == nil {
		agent.Spec.Storage = &agentv1alpha1.PlatformAgentStorageSpec{}
	}
	agent.Spec.Storage.ExpectedClusterProfiles = ptr.To(n)
	return agent
}

// TestTheAgentClaimIsSizedByTheExpectedProfileCount pins the formula on the
// agent's claim, base + N x per-profile rounded up to whole GiB, never below
// spec.storage.size or the default, and the sandbox claim staying on
// ResolveDataStorageSize: it holds no session stores, which the mirror does not
// copy (sandbox_mirror.py's denylist).
func TestTheAgentClaimIsSizedByTheExpectedProfileCount(t *testing.T) {
	base := resource.MustParse(dataVolumeBaseSize)
	perProfile := resource.MustParse(perClusterProfileStorage)
	formula := func(n int64) resource.Quantity {
		const gib = int64(1) << 30
		need := base.Value() + n*perProfile.Value()
		return *resource.NewQuantity((need+gib-1)/gib*gib, resource.BinarySI)
	}
	cases := []struct {
		name        string
		agent       *agentv1alpha1.PlatformAgent
		wantAgent   resource.Quantity
		wantSandbox string
	}{
		{"a small count stays on the default", profileCountAgent("", 10), resource.MustParse(agentDataStorageSize), agentDataStorageSize},
		{"a large count raises the agent's claim only", profileCountAgent("", 200), formula(200), agentDataStorageSize},
		{"an explicit size larger than the formula wins", profileCountAgent("500Gi", 200), resource.MustParse("500Gi"), "500Gi"},
		{"an explicit size smaller than the formula is raised on the agent's claim", profileCountAgent("20Gi", 200), formula(200), "20Gi"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agentClaim := buildPVC(tc.agent).Spec.Resources.Requests[corev1.ResourceStorage]
			if agentClaim.Cmp(tc.wantAgent) != 0 {
				t.Errorf("agent data claim requests %s, want %s", agentClaim.String(), tc.wantAgent.String())
			}
			sandboxClaim := sandboxDataClaimRequest(t, tc.agent)
			if want := resource.MustParse(tc.wantSandbox); sandboxClaim.Cmp(want) != 0 {
				t.Errorf("sandbox data claim template requests %s, want %s", sandboxClaim.String(), want.String())
			}
		})
	}
	// The worked figures the docs quote: 200 profiles is 87Gi, and the 50Gi
	// default is reached at 108 (8Gi + 107 x 400Mi is 49.8Gi, rounded up to 50).
	if got := formula(200); got.Cmp(resource.MustParse("87Gi")) != 0 {
		t.Errorf("200 profiles size to %s, want 87Gi", got.String())
	}
	if got := ResolveAgentDataStorageSize(profileCountAgent("", 108)); got.Cmp(resource.MustParse("51Gi")) != 0 {
		t.Errorf("108 profiles resolve to %s, want 51Gi", got.String())
	}
	if got := ResolveAgentDataStorageSize(profileCountAgent("", 107)); got.Cmp(resource.MustParse(agentDataStorageSize)) != 0 {
		t.Errorf("107 profiles resolve to %s, want the default", got.String())
	}
}

// boundClaim is a bound claim at size in the given class, with the agent's
// controller reference when owned.
func boundClaim(agent *agentv1alpha1.PlatformAgent, name, size, class string, owned bool) *corev1.PersistentVolumeClaim {
	pvc := unownedClaim(name, agent.Namespace, size)
	if owned {
		pvc = existingClaim(agent, name, size)
	}
	pvc.Spec.StorageClassName = ptr.To(class)
	pvc.Status.Phase = corev1.ClaimBound
	pvc.Status.Capacity = corev1.ResourceList{corev1.ResourceStorage: resource.MustParse(size)}
	return pvc
}

// sizeFixture is a reconciler over a fake API server holding the agent, its
// sandbox keys, and objs.
func sizeFixture(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append([]client.Object{agent, shellSandboxKeysSecret(agent)}, objs...)...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &corev1.PersistentVolumeClaim{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}, cl
}

// reconcileOnce runs one Reconcile and returns the stored PlatformAgent.
func reconcileOnce(t *testing.T, r *PlatformAgentReconciler, cl client.Client, agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
	t.Helper()
	ctx := context.Background()
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	return got
}

// TestReconcileGrowsBothBoundDataClaimsToTheField raises spec.storage.size
// above two bound claims in a class that can expand: both are patched up to the
// field, and the condition stays absent.
func TestReconcileGrowsBothBoundDataClaimsToTheField(t *testing.T) {
	agent := sizedAgent("80Gi")
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	r, cl := sizeFixture(t, agent,
		storageClass(testStorageClassName, true),
		boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
		boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false))

	got := reconcileOnce(t, r, cl, agent)
	for _, name := range []string{agentClaim, sandboxClaim} {
		if size := claimSize(t, cl, name, agent.Namespace); size != "80Gi" {
			t.Errorf("claim %s is %s, want 80Gi from spec.storage.size", name, size)
		}
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, dataVolumeExpansionConditionType); cond != nil {
		t.Errorf("%s is set on claims that grew: %+v", dataVolumeExpansionConditionType, cond)
	}
}

// TestARaisedProfileCountGrowsTheAgentClaimOnly raises
// spec.storage.expectedClusterProfiles above two bound claims at the default in
// a class that can expand: the agent's claim is patched to the formula's size,
// the sandbox's stays at the default, and the condition stays absent.
func TestARaisedProfileCountGrowsTheAgentClaimOnly(t *testing.T) {
	agent := profileCountAgent("", 200)
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	r, cl := sizeFixture(t, agent,
		storageClass(testStorageClassName, true),
		boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
		boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false))

	got := reconcileOnce(t, r, cl, agent)
	if size := claimSize(t, cl, agentClaim, agent.Namespace); size != "87Gi" {
		t.Errorf("agent claim is %s, want 87Gi for 200 expected profiles", size)
	}
	if size := claimSize(t, cl, sandboxClaim, agent.Namespace); size != agentDataStorageSize {
		t.Errorf("sandbox claim is %s, want it left at %s", size, agentDataStorageSize)
	}
	if cond := meta.FindStatusCondition(got.Status.Conditions, dataVolumeExpansionConditionType); cond != nil {
		t.Errorf("%s is set on a claim that grew: %+v", dataVolumeExpansionConditionType, cond)
	}
}

// undersizedEvents drains the fake recorder and returns the DataVolumeUndersized
// Events it holds.
func undersizedEvents(r *PlatformAgentReconciler) []string {
	var out []string
	recorder := r.Recorder.(*record.FakeRecorder)
	for {
		select {
		case event := <-recorder.Events:
			if strings.Contains(event, dataVolumeUndersizedReason) {
				out = append(out, event)
			}
		default:
			return out
		}
	}
}

// TestAnUndersizedClaimThatWillNotGrowIsWarnedAboutOnce pins the
// DataVolumeUndersized Event: an agent claim below the formula's size that the
// operator does not control, or whose class cannot expand it, gets one Warning
// that says which, and a second pass in the same state writes none. A claim the
// operator can grow gets none.
func TestAnUndersizedClaimThatWillNotGrowIsWarnedAboutOnce(t *testing.T) {
	cases := []struct {
		name       string
		owned      bool
		expandable bool
		want       string
	}{
		{"a claim made by hand", false, true, dataVolumeUnownedSentence},
		{"a class that cannot expand", true, false, dataVolumeCannotGrowSentence},
		{"a claim the operator grows", true, true, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := profileCountAgent("", 200)
			agentClaim := buildPVC(agent).Name
			sandboxClaim := shellSandboxDataClaimName(agent)
			r, cl := sizeFixture(t, agent,
				storageClass(testStorageClassName, tc.expandable),
				boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, tc.owned),
				boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false))

			reconcileOnce(t, r, cl, agent)
			events := undersizedEvents(r)
			if tc.want == "" {
				if len(events) != 0 {
					t.Fatalf("a claim that grows was warned about: %v", events)
				}
				return
			}
			if len(events) != 1 || !strings.Contains(events[0], "Warning") || !strings.Contains(events[0], tc.want) ||
				!strings.Contains(events[0], "87Gi") {
				t.Fatalf("want one Warning naming 87Gi and %q, got %v", tc.want, events)
			}
			reconcileOnce(t, r, cl, agent)
			if again := undersizedEvents(r); len(again) != 0 {
				t.Errorf("a second pass in the same state warned again: %v", again)
			}
		})
	}
}

// TestDataVolumeNotExpandedFollowsTheStorageClass pins the condition: a claim
// below the field whose class cannot expand it keeps its size and sets
// DataVolumeNotExpanded with the reason; a class name that is empty or names
// no class counts as cannot expand. Making the class expandable then grows the
// claim and removes the condition.
func TestDataVolumeNotExpandedFollowsTheStorageClass(t *testing.T) {
	cases := []struct {
		name        string
		class       string
		seeded      *storagev1.StorageClass
		wantMention string
	}{
		{"a class with allowVolumeExpansion false", testStorageClassName, storageClass(testStorageClassName, false), "does not allow volume expansion"},
		{"a class with allowVolumeExpansion unset", testStorageClassName, &storagev1.StorageClass{
			ObjectMeta: metav1.ObjectMeta{Name: testStorageClassName}, Provisioner: "test.csi.example.com",
		}, "does not allow volume expansion"},
		{"an empty class name", "", nil, "names no StorageClass"},
		{"a class that does not exist", "gone-class", nil, "does not exist"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := sizedAgent("80Gi")
			agentClaim := buildPVC(agent).Name
			sandboxClaim := shellSandboxDataClaimName(agent)
			objs := []client.Object{
				boundClaim(agent, agentClaim, agentDataStorageSize, tc.class, true),
				boundClaim(agent, sandboxClaim, agentDataStorageSize, tc.class, false),
			}
			if tc.seeded != nil {
				objs = append(objs, tc.seeded)
			}
			r, cl := sizeFixture(t, agent, objs...)
			ctx := context.Background()

			got := reconcileOnce(t, r, cl, agent)
			for _, name := range []string{agentClaim, sandboxClaim} {
				if size := claimSize(t, cl, name, agent.Namespace); size != agentDataStorageSize {
					t.Errorf("claim %s moved to %s; a class that cannot expand must leave it at %s", name, size, agentDataStorageSize)
				}
			}
			cond := meta.FindStatusCondition(got.Status.Conditions, dataVolumeExpansionConditionType)
			if cond == nil {
				t.Fatalf("no %s condition; conditions: %+v", dataVolumeExpansionConditionType, got.Status.Conditions)
			}
			if cond.Status != metav1.ConditionTrue || cond.Reason != dataVolumeExpansionBlockedReason {
				t.Errorf("%s = %s/%s, want True/%s", dataVolumeExpansionConditionType, cond.Status, cond.Reason, dataVolumeExpansionBlockedReason)
			}
			for _, want := range []string{agentClaim, sandboxClaim, agentDataStorageSize, "80Gi", tc.wantMention} {
				if !strings.Contains(cond.Message, want) {
					t.Errorf("%s message does not mention %q: %s", dataVolumeExpansionConditionType, want, cond.Message)
				}
			}

			// The condition is in the unchanged comparison as well as the write.
			settled := got.ResourceVersion
			if _, _, err := r.updateStatusReady(ctx, got, "", otlpSourceNone, r.resolveNetpolProfile(ctx, got), a2aProvisionState{}); err != nil {
				t.Fatalf("updateStatusReady (settled): %v", err)
			}
			again := &agentv1alpha1.PlatformAgent{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), again); err != nil {
				t.Fatalf("get agent: %v", err)
			}
			if again.ResourceVersion != settled {
				t.Errorf("a status pass over unchanged state wrote the CR (resourceVersion %s -> %s)", settled, again.ResourceVersion)
			}
		})
	}

	t.Run("an expandable class grows the claims and clears the condition", func(t *testing.T) {
		agent := sizedAgent("80Gi")
		agentClaim := buildPVC(agent).Name
		sandboxClaim := shellSandboxDataClaimName(agent)
		r, cl := sizeFixture(t, agent,
			storageClass(testStorageClassName, false),
			boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
			boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false))
		ctx := context.Background()

		if got := reconcileOnce(t, r, cl, agent); meta.FindStatusCondition(got.Status.Conditions, dataVolumeExpansionConditionType) == nil {
			t.Fatalf("no %s condition before the class changed; this proves nothing", dataVolumeExpansionConditionType)
		}
		class := &storagev1.StorageClass{}
		if err := cl.Get(ctx, client.ObjectKey{Name: testStorageClassName}, class); err != nil {
			t.Fatalf("get class: %v", err)
		}
		class.AllowVolumeExpansion = ptr.To(true)
		if err := cl.Update(ctx, class); err != nil {
			t.Fatalf("update class: %v", err)
		}

		got := reconcileOnce(t, r, cl, agent)
		for _, name := range []string{agentClaim, sandboxClaim} {
			if size := claimSize(t, cl, name, agent.Namespace); size != "80Gi" {
				t.Errorf("claim %s is %s after the class allowed expansion, want 80Gi", name, size)
			}
		}
		if cond := meta.FindStatusCondition(got.Status.Conditions, dataVolumeExpansionConditionType); cond != nil {
			t.Errorf("%s still present after the class allowed expansion: %+v", dataVolumeExpansionConditionType, cond)
		}
	})
}

// TestTheDegradedPathCarriesDataVolumeNotExpanded pins the second status
// writer: a CR parked Degraded writes the condition from the recorded growth,
// rendered or not, and a second parked pass writes nothing. A reconciler that
// has read no claim yet leaves a condition already on the CR as it stands.
func TestTheDegradedPathCarriesDataVolumeNotExpanded(t *testing.T) {
	for _, rendered := range []workloadRenderState{workloadRendered, workloadNotRendered, brokerNotReconciled} {
		agent := sizedAgent("80Gi")
		r, cl := sizeFixture(t, agent,
			storageClass(testStorageClassName, false),
			boundClaim(agent, buildPVC(agent).Name, agentDataStorageSize, testStorageClassName, true))
		ctx := context.Background()

		if err := r.reconcilePVC(ctx, agent); err != nil {
			t.Fatalf("reconcilePVC: %v", err)
		}
		if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", rendered); err != nil {
			t.Fatalf("updateStatusDegraded: %v", err)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, dataVolumeExpansionConditionType)
		if cond == nil || cond.Reason != dataVolumeExpansionBlockedReason {
			t.Fatalf("rendered=%v: %s = %+v, want it set with reason %s", rendered, dataVolumeExpansionConditionType, cond, dataVolumeExpansionBlockedReason)
		}

		stored := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		settled := stored.ResourceVersion
		if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", rendered); err != nil {
			t.Fatalf("updateStatusDegraded (settled): %v", err)
		}
		if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), stored); err != nil {
			t.Fatalf("get agent: %v", err)
		}
		if stored.ResourceVersion != settled {
			t.Errorf("rendered=%v: a second parked pass wrote the CR (resourceVersion %s -> %s)", rendered, settled, stored.ResourceVersion)
		}

		// A new process has no record yet: the park leaves the condition alone.
		fresh := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: r.Scheme}
		if err := fresh.updateStatusDegraded(ctx, stored, reasonRuntimeClassNotFound, "a different refusal", rendered); err != nil {
			t.Fatalf("updateStatusDegraded (fresh process): %v", err)
		}
		if meta.FindStatusCondition(stored.Status.Conditions, dataVolumeExpansionConditionType) == nil {
			t.Errorf("rendered=%v: a reconciler that read no claim removed %s", rendered, dataVolumeExpansionConditionType)
		}
	}
}

// TestReconcilePVCGrowsTheSandboxClaimFirst pins the order of the two grows:
// reconcilePVC patches the sandbox's claim before the agent's own. A refusal
// of the agent's claim, or a park after reconcilePVC, then leaves the mirror's
// destination the larger claim, never the smaller.
func TestReconcilePVCGrowsTheSandboxClaimFirst(t *testing.T) {
	agent := sizedAgent("80Gi")
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	var patched []string
	refuseAgentClaim := interceptor.Funcs{
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, isPVC := obj.(*corev1.PersistentVolumeClaim); isPVC {
				patched = append(patched, obj.GetName())
				if obj.GetName() == agentClaim {
					return apierrors.NewForbidden(schema.GroupResource{Resource: "persistentvolumeclaims"}, obj.GetName(),
						fmt.Errorf("exceeded quota: storage-quota, requested: requests.storage=30Gi"))
				}
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, true),
			boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
			boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false)).
		WithInterceptorFuncs(refuseAgentClaim).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if want := []string{sandboxClaim, agentClaim}; strings.Join(patched, ",") != strings.Join(want, ",") {
		t.Errorf("claims patched in the order %v, want %v (the sandbox's first)", patched, want)
	}
	if size := claimSize(t, cl, sandboxClaim, agent.Namespace); size != "80Gi" {
		t.Errorf("sandbox claim is %s, want 80Gi: reconcilePVC must grow it", size)
	}
	if size := claimSize(t, cl, agentClaim, agent.Namespace); size != agentDataStorageSize {
		t.Errorf("agent claim is %s; the quota refused it, so it must stay at %s", size, agentDataStorageSize)
	}
}

// TestAStorageClassReadErrorLetsThePatchDecide pins the fallback for a class
// read that fails for a reason other than NotFound, as on an image that is
// ahead of its ClusterRole: the claim is still patched, the reconcile succeeds,
// and nothing sets DataVolumeNotExpanded.
func TestAStorageClassReadErrorLetsThePatchDecide(t *testing.T) {
	agent := sizedAgent("80Gi")
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	forbidClassRead := interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isClass := obj.(*storagev1.StorageClass); isClass {
				return apierrors.NewForbidden(schema.GroupResource{Group: storagev1.GroupName, Resource: "storageclasses"}, key.Name,
					fmt.Errorf("the operator's ClusterRole predates the grant"))
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, false),
			boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
			boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false)).
		WithInterceptorFuncs(forbidClassRead).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}

	if err := r.reconcilePVC(context.Background(), agent); err != nil {
		t.Fatalf("a class read that fails must not fail the reconcile: %v", err)
	}
	for _, name := range []string{agentClaim, sandboxClaim} {
		if size := claimSize(t, cl, name, agent.Namespace); size != "80Gi" {
			t.Errorf("claim %s is %s, want 80Gi: a class that could not be read leaves the patch to decide", name, size)
		}
	}
	msg, known := r.dataVolumeExpansionMessage(agent)
	if !known || msg != "" {
		t.Errorf("dataVolumeExpansionMessage = %q, known=%v; want \"\" and known: an unread class is not a class that cannot expand", msg, known)
	}
}

// TestOneUnreadClaimLeavesTheConditionAlone pins the "known" rule: a first
// pass that reads the agent's claim but not the sandbox's must not clear a
// DataVolumeNotExpanded condition that names the sandbox's claim.
func TestOneUnreadClaimLeavesTheConditionAlone(t *testing.T) {
	agent := sizedAgent("80Gi")
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type: dataVolumeExpansionConditionType, Status: metav1.ConditionTrue,
		Reason: dataVolumeExpansionBlockedReason, Message: "Claim " + sandboxClaim + " requests 50Gi",
	})
	failSandboxRead := interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isClaim := obj.(*corev1.PersistentVolumeClaim); isClaim && key.Name == sandboxClaim {
				return fmt.Errorf("the API server timed out")
			}
			return cl.Get(ctx, key, obj, opts...)
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, true),
			boundClaim(agent, agentClaim, "80Gi", testStorageClassName, true),
			boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(failSandboxRead).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()

	if err := r.reconcilePVC(ctx, agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if msg, known := r.dataVolumeExpansionMessage(agent); known {
		t.Fatalf("dataVolumeExpansionMessage = %q, known; want unknown while the sandbox claim is unread", msg)
	}
	if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", workloadRendered); err != nil {
		t.Fatalf("updateStatusDegraded: %v", err)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, dataVolumeExpansionConditionType) == nil {
		t.Errorf("a pass that could not read %s removed %s", sandboxClaim, dataVolumeExpansionConditionType)
	}
}

// TestAnUnreadClassAndARefusedPatchKeepTheRecord pins the class-read side of
// the "known" rule: a pass whose StorageClass read fails and whose patch is
// refused learned nothing, so the record from the pass before stands and the
// DataVolumeNotExpanded condition stays.
func TestAnUnreadClassAndARefusedPatchKeepTheRecord(t *testing.T) {
	agent := sizedAgent("80Gi")
	agentClaim := buildPVC(agent).Name
	sandboxClaim := shellSandboxDataClaimName(agent)
	failClassRead := false
	flaky := interceptor.Funcs{
		Get: func(ctx context.Context, cl client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, isClass := obj.(*storagev1.StorageClass); isClass && failClassRead {
				return fmt.Errorf("the API server timed out")
			}
			return cl.Get(ctx, key, obj, opts...)
		},
		Patch: func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
			if _, isClaim := obj.(*corev1.PersistentVolumeClaim); isClaim {
				return apierrors.NewForbidden(schema.GroupResource{Resource: "persistentvolumeclaims"}, obj.GetName(),
					fmt.Errorf("only dynamically provisioned pvc can be resized and the storageclass that provisions the pvc must support resize"))
			}
			return cl.Patch(ctx, obj, patch, opts...)
		},
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, storageClass(testStorageClassName, false),
			boundClaim(agent, agentClaim, agentDataStorageSize, testStorageClassName, true),
			boundClaim(agent, sandboxClaim, agentDataStorageSize, testStorageClassName, false)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(flaky).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	ctx := context.Background()

	if err := r.reconcilePVC(ctx, agent); err != nil {
		t.Fatalf("reconcilePVC: %v", err)
	}
	if msg, known := r.dataVolumeExpansionMessage(agent); !known || msg == "" {
		t.Fatalf("dataVolumeExpansionMessage = %q, known=%v after a pass that read the class; this proves nothing", msg, known)
	}
	if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", workloadRendered); err != nil {
		t.Fatalf("updateStatusDegraded: %v", err)
	}

	failClassRead = true
	if err := r.reconcilePVC(ctx, agent); err != nil {
		t.Fatalf("reconcilePVC (class unread): %v", err)
	}
	if err := r.updateStatusDegraded(ctx, agent, reasonShellSandboxKeysMissing, "the sandbox keypair Secret is missing", workloadRendered); err != nil {
		t.Fatalf("updateStatusDegraded (class unread): %v", err)
	}
	if meta.FindStatusCondition(agent.Status.Conditions, dataVolumeExpansionConditionType) == nil {
		t.Errorf("a pass that could not read the class and had its patch refused removed %s", dataVolumeExpansionConditionType)
	}
}
