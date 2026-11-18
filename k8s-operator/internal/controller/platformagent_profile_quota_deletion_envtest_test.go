package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	admissionregistrationv1 "k8s.io/api/admissionregistration/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// refuseQuotaDeletes installs an admission policy refusing every ResourceQuota
// delete in ns, waits until it holds, and returns its removal.
func refuseQuotaDeletes(ctx context.Context, t *testing.T, c client.Client, ns string) func() {
	t.Helper()
	policy := &admissionregistrationv1.ValidatingAdmissionPolicy{
		ObjectMeta: metav1.ObjectMeta{Name: "refuse-quota-deletes"},
		Spec: admissionregistrationv1.ValidatingAdmissionPolicySpec{
			FailurePolicy: ptrTo(admissionregistrationv1.Fail),
			MatchConstraints: &admissionregistrationv1.MatchResources{ResourceRules: []admissionregistrationv1.NamedRuleWithOperations{{
				RuleWithOperations: admissionregistrationv1.RuleWithOperations{
					Operations: []admissionregistrationv1.OperationType{admissionregistrationv1.Delete},
					Rule:       admissionregistrationv1.Rule{APIGroups: []string{""}, APIVersions: []string{"v1"}, Resources: []string{"resourcequotas"}},
				},
			}}},
			Validations: []admissionregistrationv1.Validation{{Expression: "false", Message: "quota deletes are refused here"}},
		},
	}
	binding := &admissionregistrationv1.ValidatingAdmissionPolicyBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "refuse-quota-deletes"},
		Spec: admissionregistrationv1.ValidatingAdmissionPolicyBindingSpec{
			PolicyName:        policy.Name,
			ValidationActions: []admissionregistrationv1.ValidationAction{admissionregistrationv1.Deny},
			MatchResources: &admissionregistrationv1.MatchResources{NamespaceSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{corev1.LabelMetadataName: ns},
			}},
		},
	}
	for _, obj := range []client.Object{policy, binding} {
		if err := c.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	// A probe quota the policy also covers: refused on a dry-run delete once
	// the policy is enforced.
	probe := &corev1.ResourceQuota{ObjectMeta: metav1.ObjectMeta{Name: "probe", Namespace: ns}}
	if err := c.Create(ctx, probe); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(gatewayPodPolicyActivation)
	for {
		err := c.Delete(ctx, probe.DeepCopy(), client.DryRunAll)
		if apierrors.IsForbidden(err) || apierrors.IsInvalid(err) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the refusing policy was not enforced within %s (last delete: %v)", gatewayPodPolicyActivation, err)
		}
		time.Sleep(time.Second)
	}
	return func() {
		if err := c.Delete(ctx, binding); err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(gatewayPodPolicyActivation)
		for c.Delete(ctx, probe.DeepCopy(), client.DryRunAll) != nil {
			if time.Now().After(deadline) {
				t.Fatal("the refusing policy still held after its binding was removed")
			}
			time.Sleep(time.Second)
		}
	}
}

func ptrTo[T any](v T) *T { return &v }

func eventsOf(r *record.FakeRecorder) []string {
	var out []string
	for {
		select {
		case e := <-r.Events:
			out = append(out, e)
		default:
			return out
		}
	}
}

// Deleting the PlatformAgent removes its profile quota. A refused delete
// holds the finalizer and is retried until the bound, then the finalizer
// releases with a Warning naming the quota it left, and a successor of
// another name removes that quota when it renders its own.
func TestADeletedAgentsRefusedProfileQuotaIsRetriedThenLeftForASuccessor(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	agent.Finalizers = []string{platformAgentFinalizer}
	for _, obj := range []client.Object{&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}, chartProfileNamespace(agent), agent} {
		if err := c.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	events := record.NewFakeRecorder(32)
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: events}
	if err := r.applyManaged(ctx, agent, buildProfileQuota(agent, nil)); err != nil {
		t.Fatal(err)
	}
	quotaKey := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileQuotaName(agent)}
	// The dispatcher's pod-read Role there, which deletion removes whether or
	// not the quota's delete is refused.
	podRead := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")}}
	if err := c.Create(ctx, podRead); err != nil {
		t.Fatal(err)
	}

	allow := refuseQuotaDeletes(ctx, t, c, profileNamespaceName(agent))
	defer func(was time.Duration) { profileQuotaReleaseAfter = was }(profileQuotaReleaseAfter)
	profileQuotaReleaseAfter = time.Hour

	if err := c.Delete(ctx, agent); err != nil {
		t.Fatal(err)
	}
	deleting := &agentv1alpha1.PlatformAgent{}
	if err := c.Get(ctx, client.ObjectKeyFromObject(agent), deleting); err != nil {
		t.Fatal(err)
	}
	res, err := r.handleDeletion(ctx, deleting)
	if err != nil || res.RequeueAfter != profileQuotaRetryEvery {
		t.Fatalf("inside the bound: %+v %v, want a retry in %s", res, err, profileQuotaRetryEvery)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(agent), deleting); err != nil || len(deleting.Finalizers) == 0 {
		t.Fatalf("inside the bound the finalizer was released: %v %v", deleting.Finalizers, err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(podRead), &rbacv1.Role{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the dispatcher's pod-read Role while the quota holds the finalizer: %v, want it already gone", err)
	}

	// Past the bound: shortened rather than slept through.
	profileQuotaReleaseAfter = 0
	if res, err := r.handleDeletion(ctx, deleting); err != nil || res.RequeueAfter != 0 {
		t.Fatalf("past the bound: %+v %v, want the release", res, err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(agent), &agentv1alpha1.PlatformAgent{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the PlatformAgent past the bound: %v, want it gone", err)
	}
	if err := c.Get(ctx, quotaKey, &corev1.ResourceQuota{}); err != nil {
		t.Fatalf("the refused quota: %v, want it left", err)
	}
	var left string
	for _, e := range eventsOf(events) {
		if strings.Contains(e, reasonProfileQuotaLeft) {
			left = e
		}
	}
	if !strings.Contains(left, corev1.EventTypeWarning) || !strings.Contains(left, profileQuotaName(agent)) {
		t.Fatalf("Warning naming the left quota: %q", left)
	}

	// A successor of another name in the same namespace removes it.
	allow()
	successor := a2aTestAgent()
	successor.Name = "successor"
	successor.Spec.Harness = agent.Spec.Harness
	if err := c.Create(ctx, successor); err != nil {
		t.Fatal(err)
	}
	if err := r.reconcileProfileQuota(ctx, successor, nil, true); err != nil {
		t.Fatal(err)
	}
	if err := c.Get(ctx, quotaKey, &corev1.ResourceQuota{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the leftover quota after the successor's render: %v, want it removed", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(successor), Name: profileQuotaName(successor)}, &corev1.ResourceQuota{}); err != nil {
		t.Fatalf("the successor's own quota: %v", err)
	}
	var adopted bool
	for _, e := range eventsOf(events) {
		adopted = adopted || strings.Contains(e, reasonProfileQuotaAdopted) && strings.Contains(e, profileQuotaName(agent))
	}
	if !adopted {
		t.Fatal("no Event saying the successor removed the leftover quota")
	}
}

// Adoption removes only an operator-rendered profile quota whose PlatformAgent
// is gone: not one whose agent still exists, and not a quota someone else
// wrote there.
func TestAdoptionTakesOnlyALeftoverOfADeletedAgent(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	live := a2aTestAgent()
	live.Name = "still-here"
	live.Spec.Harness = agent.Spec.Harness
	for _, obj := range []client.Object{&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}, chartProfileNamespace(agent), agent, live} {
		if err := c.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: record.NewFakeRecorder(32)}
	if err := r.applyManaged(ctx, live, buildProfileQuota(live, nil)); err != nil {
		t.Fatal(err)
	}
	foreign := &corev1.ResourceQuota{ObjectMeta: metav1.ObjectMeta{Name: "gone-profile-quota", Namespace: profileNamespaceName(agent),
		Labels: map[string]string{a2aComponentLabel: profileQuotaComponent}}}
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if err := r.reconcileProfileQuota(ctx, agent, nil, true); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{profileQuotaName(live), foreign.Name} {
		if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: name}, &corev1.ResourceQuota{}); err != nil {
			t.Errorf("%s: %v, want it left", name, err)
		}
	}
}

// With nothing refusing it, the quota goes and the finalizer releases on the
// same pass.
func TestADeletedAgentsProfileQuotaGoesWithIt(t *testing.T) {
	c, scheme := startEnvtest(t)
	ctx := context.Background()
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
	}
	agent.Finalizers = []string{platformAgentFinalizer}
	for _, obj := range []client.Object{&corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: agent.Namespace}}, chartProfileNamespace(agent), agent} {
		if err := c.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: record.NewFakeRecorder(32)}
	if err := r.applyManaged(ctx, agent, buildProfileQuota(agent, nil)); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(ctx, agent); err != nil {
		t.Fatal(err)
	}
	deleting := &agentv1alpha1.PlatformAgent{}
	if err := c.Get(ctx, client.ObjectKeyFromObject(agent), deleting); err != nil {
		t.Fatal(err)
	}
	if res, err := r.handleDeletion(ctx, deleting); err != nil || res.RequeueAfter != 0 {
		t.Fatalf("handleDeletion = %+v %v, want the release", res, err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: profileNamespaceName(agent), Name: profileQuotaName(agent)}, &corev1.ResourceQuota{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the quota: %v, want it gone", err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(agent), &agentv1alpha1.PlatformAgent{}); !apierrors.IsNotFound(err) {
		t.Fatalf("the PlatformAgent: %v, want it gone", err)
	}
}
