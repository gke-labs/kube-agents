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
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	authorizationv1client "k8s.io/client-go/kubernetes/typed/authorization/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// concurrentProbes is how many Probe calls the race test runs at once.
	concurrentProbes = 8
)

// probeLabels is probeRBAC with the denials flattened to their labels, which
// is what most probe tests assert on.
func probeLabels(ctx context.Context, reviews authorizationv1client.SelfSubjectAccessReviewInterface, namespace string, agentNamespaces []string) ([]string, error) {
	denied, err := probeRBAC(ctx, reviews, namespace, agentNamespaces)
	if err != nil {
		return nil, err
	}
	return labelsOf(denied), nil
}

// secretsOnlyIn is an authorizer that grants the Secret write verbs in the
// named namespaces and nowhere else, which is what a RoleBinding to the
// operator's namespaced Role looks like from a SelfSubjectAccessReview. The
// Secret reads are the ClusterRole's and are granted everywhere.
func secretsOnlyIn(namespaces ...string) *fakeAuthorizer {
	return &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		if !isSecretWrite(attrs) {
			return false
		}
		for _, ns := range namespaces {
			if attrs.Namespace == ns {
				return false
			}
		}
		return true
	}}
}

// The namespaced Role is bound in the install namespace and nowhere else, so
// a PlatformAgent in another namespace is the one install shape the
// operator-namespace probe cannot see. Once the checker has heard of that
// namespace it probes the Secret write verbs there too, labels the denial with the
// namespace, and reports it to that agent only.
func TestTheProbeCoversEveryAgentNamespaceItHasHeardOf(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	authorizer := secretsOnlyIn(operatorNamespace)

	denied, err := probeRBAC(context.Background(), authorizer.reviews(), operatorNamespace, nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) != 0 {
		t.Fatalf("with no agent namespace observed the operator-namespace grant must satisfy the probe; got %v", labelsOf(denied))
	}

	denied, err = probeRBAC(context.Background(), authorizer.reviews(), operatorNamespace, []string{"team-a"})
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) == 0 {
		t.Fatal("an agent namespace with no RoleBinding must be a denial once observed")
	}
	for _, d := range denied {
		if d.namespace != "team-a" || !strings.Contains(d.label, "secrets") || !strings.HasSuffix(d.label, "(in namespace team-a)") {
			t.Errorf("denied %+v; want only Secret write verbs, labelled for and attributed to team-a", d)
		}
	}
}

// DeniedFor hands an agent the denials that concern it: cluster-wide ones,
// the operator namespace's, and its own namespace's. Another agent's
// namespace is that agent's condition. Forget drops a namespace whose agent
// is gone, denials included.
func TestDeniedForFiltersByAgentNamespaceAndForgetDropsIt(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	checker := NewRBACChecker(secretsOnlyIn(operatorNamespace).reviews(), operatorNamespace)
	checker.Observe("team-a")
	checker.Observe(operatorNamespace) // the operator's own namespace is never added
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("probe: %v", err)
	}
	if got := checker.DeniedFor(operatorNamespace); len(got) != 0 {
		t.Errorf("an agent in the operator's namespace must not carry team-a's denials; got %v", got)
	}
	got := checker.DeniedFor("team-a")
	if len(got) == 0 || !strings.Contains(got[0], "(in namespace team-a)") {
		t.Fatalf("the team-a agent must see its own denials; got %v", got)
	}
	if all := checker.Denied(); len(all) != len(got) {
		t.Errorf("Denied() = %v, want the same %d team-a denials", all, len(got))
	}

	checker.Forget("team-a")
	if got := checker.DeniedFor("team-a"); len(got) != 0 {
		t.Errorf("Forget must drop the namespace's standing denials; got %v", got)
	}
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("re-probe: %v", err)
	}
	if got := checker.Denied(); len(got) != 0 {
		t.Errorf("after Forget the namespace must not be probed again; got %v", got)
	}
}

// End to end through Reconcile: an agent outside the install namespace, on a
// cluster whose RoleBinding exists only there, gets Degraded/RBACIncomplete
// naming its namespace, and the message says what to bind.
func TestReconcileReportsAMissingRoleBindingInTheAgentsNamespace(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "team-agent",
			Namespace:  "team-a",
			Finalizers: []string{platformAgentFinalizer},
		},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	checker := NewRBACChecker(secretsOnlyIn(operatorNamespace).reviews(), operatorNamespace)
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker, OperatorNamespace: operatorNamespace}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	// The first reconcile tells the checker about team-a, and the status
	// has to follow in that same pass: an agent whose reconcile makes no
	// Secret write (the upgrade case, where the kube-proxy TLS Secret already
	// exists) gets no error backoff, and the next scheduled pass is fifteen
	// minutes away. No probe between the two lines, by design of the test.
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatalf("re-read agent: %v", err)
	}
	degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType)
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != reasonRBACIncomplete {
		t.Fatalf("expected Degraded=True/%s on the CR, got %+v", reasonRBACIncomplete, degraded)
	}
	for _, want := range []string{"create secrets (in namespace team-a)", "RoleBinding"} {
		if !strings.Contains(degraded.Message, want) {
			t.Errorf("condition message %q does not name %q", degraded.Message, want)
		}
	}
	// Every denial is team-a's, so the cause is the missing Role and
	// RoleBinding alone: the skew sentence would send the operator to
	// re-apply manifests that install nothing in team-a.
	if strings.Contains(degraded.Message, RBACIncompleteCause) {
		t.Errorf("condition message %q blames image skew for a denial that is only the agent's namespace", degraded.Message)
	}

	// Deleting the agent forgets its namespace: the denial must not stand
	// against whatever reconciles next, and the namespace is not probed again.
	if _, err := r.handleDeletion(ctx, stored); err != nil {
		t.Fatalf("handleDeletion: %v", err)
	}
	if got := checker.DeniedFor("team-a"); len(got) != 0 {
		t.Errorf("after deletion the checker still holds team-a's denials: %v", got)
	}
	if _, err := checker.Probe(ctx); err != nil {
		t.Fatalf("probe after deletion: %v", err)
	}
	if got := checker.Denied(); len(got) != 0 {
		t.Errorf("after deletion team-a is still probed: %v", got)
	}
}

// Observe of a new namespace probes before it returns: the reconcile that
// called it reads DeniedFor on its next line, and a probe still in flight
// would hand that reconcile an empty answer and a fifteen-minute requeue.
// No Probe call and no waiting from the test, by design.
func TestObserveProbesANewNamespaceAtOnce(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	checker := NewRBACChecker(secretsOnlyIn(operatorNamespace).reviews(), operatorNamespace)
	checker.Observe("team-a")
	if got := checker.DeniedFor("team-a"); len(got) == 0 {
		t.Fatal("Observe returned before the new namespace was probed; the reconcile that called it reads an empty answer")
	}
}

// A probe that snapshotted a namespace before Forget removed it must not
// write that namespace's denials back when it finishes. The authorizer holds
// the probe open on its first team-a review, Forget runs in the gap, and the
// stored answer is read after the probe completes.
func TestAProbeInFlightAcrossForgetDoesNotRestoreTheNamespace(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	gate := make(chan struct{})
	started := make(chan struct{})
	var once sync.Once
	var armed atomic.Bool
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		if armed.Load() && attrs.Namespace == "team-a" {
			once.Do(func() { close(started) })
			<-gate
		}
		return isSecretWrite(attrs) && attrs.Namespace != operatorNamespace
	}}
	checker := NewRBACChecker(authorizer.reviews(), operatorNamespace)
	checker.Observe("team-a")
	if got := checker.DeniedFor("team-a"); len(got) == 0 {
		t.Fatal("setup: Observe's probe recorded no team-a denial")
	}

	armed.Store(true)
	done := make(chan error, 1)
	go func() {
		_, err := checker.Probe(context.Background())
		done <- err
	}()
	<-started // the probe holds a snapshot that names team-a and is mid-flight
	checker.Forget("team-a")
	close(gate)
	if err := <-done; err != nil {
		t.Fatalf("probe: %v", err)
	}
	if got := checker.DeniedFor("team-a"); len(got) != 0 {
		t.Errorf("a probe that finished after Forget restored team-a's denials: %v", got)
	}
	for _, label := range checker.Denied() {
		if strings.Contains(label, "(in namespace team-a)") {
			t.Errorf("Denied() still names the forgotten namespace: %q", label)
		}
	}
}

// Probes run one at a time under probeMu, so the ticker, Observe's goroutine
// and the boot probe can overlap without a staler snapshot overwriting a
// fresher answer. This test cannot see the ordering from outside; it runs
// concurrent probes against the shared state so the race detector reads
// every path, and checks the answer that comes out last is complete.
func TestConcurrentProbesAreSafeUnderTheRaceDetector(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	checker := NewRBACChecker(secretsOnlyIn(operatorNamespace).reviews(), operatorNamespace)
	checker.Observe("team-a")
	var wg sync.WaitGroup
	for i := 0; i < concurrentProbes; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := checker.Probe(context.Background()); err != nil {
				t.Errorf("probe: %v", err)
			}
		}()
	}
	wg.Wait()
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("final probe: %v", err)
	}
	if got := checker.DeniedFor("team-a"); len(got) == 0 {
		t.Fatal("the stored answer lost team-a's denial to an older probe")
	}
}
