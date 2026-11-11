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
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	authorizationv1client "k8s.io/client-go/kubernetes/typed/authorization/v1"
	k8stesting "k8s.io/client-go/testing"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// generatedRolePath is controller-gen's output for the markers above
// Reconcile, relative to this package directory (where `go test` runs).
const generatedRolePath = "../../config/rbac/role.yaml"

// fakeAuthorizer answers SelfSubjectAccessReviews from a fake clientset. deny
// decides per review; err, when set, fails every review instead. Both are
// read under a lock because the ticker test changes them while the checker's
// goroutine is probing.
type fakeAuthorizer struct {
	mu   sync.Mutex
	deny func(*authorizationv1.ResourceAttributes) bool
	err  error
}

func (f *fakeAuthorizer) set(deny func(*authorizationv1.ResourceAttributes) bool, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deny, f.err = deny, err
}

func (f *fakeAuthorizer) reviews() authorizationv1client.SelfSubjectAccessReviewInterface {
	clientset := k8sfake.NewClientset()
	clientset.PrependReactor("create", "selfsubjectaccessreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		f.mu.Lock()
		deny, err := f.deny, f.err
		f.mu.Unlock()
		if err != nil {
			return true, nil, err
		}
		review := action.(k8stesting.CreateAction).GetObject().(*authorizationv1.SelfSubjectAccessReview)
		review.Status.Allowed = deny == nil || !deny(review.Spec.ResourceAttributes)
		return true, review, nil
	})
	return clientset.AuthorizationV1().SelfSubjectAccessReviews()
}

func denyPDBPatch(attrs *authorizationv1.ResourceAttributes) bool {
	return attrs.Group == "policy" && attrs.Resource == "poddisruptionbudgets" && attrs.Verb == "patch"
}

// generatedRoles is role.yaml parsed into its documents: the ClusterRole the
// markers without `namespace=` produce, and the Role the one with it does.
type generatedRoles struct {
	clusterRole rbacv1.ClusterRole
	roles       []rbacv1.Role
}

// strictJSON decodes one document into v and refuses unknown fields, the
// json counterpart of yaml.UnmarshalStrict for a document the YAML decoder has
// already turned into JSON.
func strictJSON(doc json.RawMessage, v any) error {
	dec := json.NewDecoder(bytes.NewReader(doc))
	dec.DisallowUnknownFields()
	return dec.Decode(v)
}

// loadGeneratedRoles reads role.yaml document by document. controller-gen
// writes the namespaced Role into the same file after the ClusterRole, and a
// single-object parse would read the first document and silently drop the
// rest -- the Secret grant among them. The apimachinery decoder splits the
// stream, so a separator written with trailing whitespace or CRLF line ends
// still parses as two documents rather than one.
func loadGeneratedRoles(t *testing.T) generatedRoles {
	t.Helper()
	raw, err := os.ReadFile(generatedRolePath)
	if err != nil {
		t.Fatalf("read %s: %v", generatedRolePath, err)
	}
	var out generatedRoles
	var clusterRoles int
	decoder := utilyaml.NewYAMLOrJSONDecoder(bytes.NewReader(raw), len(raw))
	for {
		var doc json.RawMessage
		if err := decoder.Decode(&doc); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			t.Fatalf("parse %s: %v", generatedRolePath, err)
		}
		if len(bytes.TrimSpace(doc)) == 0 || string(bytes.TrimSpace(doc)) == "null" {
			continue
		}
		var kind metav1.TypeMeta
		if err := json.Unmarshal(doc, &kind); err != nil {
			t.Fatalf("parse %s: %v", generatedRolePath, err)
		}
		switch kind.Kind {
		case "ClusterRole":
			clusterRoles++
			if err := strictJSON(doc, &out.clusterRole); err != nil {
				t.Fatalf("parse %s ClusterRole: %v", generatedRolePath, err)
			}
		case "Role":
			var role rbacv1.Role
			if err := strictJSON(doc, &role); err != nil {
				t.Fatalf("parse %s Role: %v", generatedRolePath, err)
			}
			out.roles = append(out.roles, role)
		default:
			t.Fatalf("%s carries a %q document; controller-gen writes only ClusterRole and Role", generatedRolePath, kind.Kind)
		}
	}
	if clusterRoles != 1 {
		t.Fatalf("%s carries %d ClusterRole documents, want 1", generatedRolePath, clusterRoles)
	}
	if len(out.clusterRole.Rules) == 0 {
		t.Fatalf("%s parsed to no ClusterRole rules; every comparison would pass vacuously", generatedRolePath)
	}
	return out
}

// permissionKey identifies one grant for comparison. resourceNames is part of
// that identity rather than decoration on it: a rule scoped to one name
// authorizes strictly less than the same (verb, resource) unscoped, and a
// SelfSubjectAccessReview that omits the name asks for the unscoped form. Key
// without the names and the two sides match while the probe asks the API
// server a question role.yaml does not answer -- which is the live failure
// that put the Name field on requiredPermission in the first place. The
// namespaced flag is part of the identity for the same reason: a grant held
// through a Role answers only the review that names its namespace.
func permissionKey(verb, group, resource string, names []string, namespaced bool) string {
	key := describePermission(verb, group, resource)
	if len(names) > 0 {
		sorted := append([]string(nil), names...)
		sort.Strings(sorted)
		key += " [" + strings.Join(sorted, ",") + "]"
	}
	if namespaced {
		key += rbacNamespacedLabelSuffix
	}
	return key
}

func permissionSet(rules []rbacv1.PolicyRule, namespaced bool) map[string]struct{} {
	set := map[string]struct{}{}
	for _, rule := range rules {
		for _, group := range rule.APIGroups {
			for _, resource := range rule.Resources {
				for _, verb := range rule.Verbs {
					set[permissionKey(verb, group, resource, rule.ResourceNames, namespaced)] = struct{}{}
				}
			}
		}
	}
	return set
}

func sortedDifference(a, b map[string]struct{}) []string {
	var out []string
	for key := range a {
		if _, ok := b[key]; !ok {
			out = append(out, key)
		}
	}
	sort.Strings(out)
	return out
}

// TestRequiredPermissionsMatchTheGeneratedRole is what keeps the probe's copy
// of the RBAC markers honest: the markers are comments and role.yaml is not in
// the binary, so requiredPermissions is a second copy by necessity, and a
// marker change that forgets it would leave the self-check silently checking
// yesterday's role.
func TestRequiredPermissionsMatchTheGeneratedRole(t *testing.T) {
	roles := loadGeneratedRoles(t)
	generated := permissionSet(roles.clusterRole.Rules, false)
	for _, role := range roles.roles {
		for key := range permissionSet(role.Rules, true) {
			generated[key] = struct{}{}
		}
	}

	// Built from the flattened tuples rather than from requiredPermissions,
	// because the tuples are what probeRBAC actually puts on the wire. A
	// scoping that is declared but dropped in flattenRequiredPermissions
	// would satisfy a comparison against the declaration and still send the
	// unscoped review.
	probed := map[string]struct{}{}
	for _, tuple := range flattenRequiredPermissions() {
		resource := tuple.resource
		if tuple.subresource != "" {
			resource += rbacSubresourceSeparator + tuple.subresource
		}
		var names []string
		if tuple.name != "" {
			names = []string{tuple.name}
		}
		probed[permissionKey(tuple.verb, tuple.group, resource, names, tuple.namespaced)] = struct{}{}
	}

	if missing := sortedDifference(generated, probed); len(missing) > 0 {
		t.Errorf("role.yaml grants permissions the self-check does not probe; add them to requiredPermissions in rbac_selfcheck.go:\n  %s",
			strings.Join(missing, "\n  "))
	}
	if extra := sortedDifference(probed, generated); len(extra) > 0 {
		t.Errorf("the self-check probes permissions role.yaml does not grant; every install would report them denied. Remove them from requiredPermissions or add the marker and run make manifests:\n  %s",
			strings.Join(extra, "\n  "))
	}
}

func TestProbeRBACNamesEachDeniedPermission(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return denyPDBPatch(attrs) ||
			(attrs.Resource == "pods" && attrs.Subresource == "log" && attrs.Verb == "get")
	}}
	denied, err := probeLabels(context.Background(), authorizer.reviews(), "", nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	want := []string{"get pods/log", "patch poddisruptionbudgets.policy"}
	got := append([]string(nil), denied...)
	sort.Strings(got)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("denied = %v, want %v", denied, want)
	}
}

// TestTheScopedBindProbeCarriesItsResourceName is the regression test for a
// live failure: the operator reported RBACIncomplete for `bind clusterroles`
// against a cluster whose ClusterRole granted exactly that, because the grant
// is scoped by resourceNames and the probe asked the unscoped question. RBAC
// does not widen a scoped grant to cover an unscoped request, so the API
// server answered "no" truthfully and the operator called it a shortfall.
//
// The authorizer here models that rule rather than asserting on the tuple:
// a review for clusterroles/bind is allowed only when it names the role the
// grant is scoped to. Drop Name from the review and this goes red the way the
// cluster did.
func TestTheScopedBindProbeCarriesItsResourceName(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		if attrs.Group != "rbac.authorization.k8s.io" || attrs.Resource != "clusterroles" || attrs.Verb != "bind" {
			return false
		}
		return attrs.Name != a2aAuthDelegatorRole
	}}
	denied, err := probeLabels(context.Background(), authorizer.reviews(), "", nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) != 0 {
		t.Fatalf("a grant of bind on %s, probed correctly, must report nothing denied; got %v",
			a2aAuthDelegatorRole, denied)
	}

	// And the scoping is a real bound, not an artifact of the fake always
	// saying yes: bind over any other role is still refused.
	var found bool
	for _, tuple := range flattenRequiredPermissions() {
		if tuple.resource == "clusterroles" && tuple.verb == "bind" {
			found = true
			if tuple.name != a2aAuthDelegatorRole {
				t.Errorf("the bind probe names %q, want %q", tuple.name, a2aAuthDelegatorRole)
			}
		}
	}
	if !found {
		t.Fatal("no clusterroles/bind tuple in the probe; the auth callout's grant is unprobed")
	}
}

// isSecretWrite reports whether attrs is one of the Secret write verbs, the
// half of the Secret grant that is a Role in the install namespace.
func isSecretWrite(attrs *authorizationv1.ResourceAttributes) bool {
	return attrs.Resource == "secrets" && slices.Contains(rbacSecretWriteVerbs, attrs.Verb)
}

// The Secret write verbs are a Role in the install namespace, so the probe
// has to ask about that namespace: an authorizer that grants them there and
// nowhere else -- which is what a RoleBinding is -- must satisfy the check,
// and the cluster-scoped tuples, the Secret reads among them, must still be
// reviewed cluster-wide, or a ClusterRole shortfall would hide behind a
// namespaced yes.
func TestTheNamespacedProbeCarriesTheOperatorsNamespace(t *testing.T) {
	const operatorNamespace = "kubeagents-system"
	// Counted atomically: the probe reviews rbacProbeConcurrency tuples at once.
	var namespacedReviews, clusterReviews atomic.Int64
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		if isSecretWrite(attrs) {
			namespacedReviews.Add(1)
			return attrs.Namespace != operatorNamespace
		}
		clusterReviews.Add(1)
		return attrs.Namespace != ""
	}}
	denied, err := probeLabels(context.Background(), authorizer.reviews(), operatorNamespace, nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) != 0 {
		t.Fatalf("Secret verbs granted in the operator's namespace, probed there, must report nothing denied; got %v", denied)
	}
	if namespacedReviews.Load() == 0 || clusterReviews.Load() == 0 {
		t.Fatalf("the probe sent %d namespaced and %d cluster-wide reviews; both halves of the role must be asked about", namespacedReviews.Load(), clusterReviews.Load())
	}

	// The bound is real: the same grant probed with no namespace is a
	// shortfall, and the label says which namespace was meant.
	denied, err = probeLabels(context.Background(), authorizer.reviews(), "", nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) == 0 {
		t.Fatal("a namespaced grant probed cluster-wide must be denied; the fake is not testing the namespace")
	}
	for _, label := range denied {
		if !strings.Contains(label, "secrets") || !strings.HasSuffix(label, rbacNamespacedLabelSuffix) {
			t.Errorf("denied %q; only the Secret write verbs were withheld and each should be labelled namespaced", label)
		}
	}
}

// TestTheMinimalRoleSecretsReadLabelsAreTheProbesOwn pins the contract between
// the self-check and resolveMinimalRoleSecretsRead, which reads the denial of
// `list secrets` and `watch secrets` out of Denied() to keep an upgraded
// install's minimal role as it is. That match holds only while the Secret
// read tuples are probed cluster-wide: a namespaced tuple is labelled with
// rbacNamespacedLabelSuffix, the strings would no longer be equal, and the
// operator would widen the agent's role on a cluster that denies it the verbs.
// Two halves: the probe sends the tuples under exactly those labels, and a
// cluster that denies the two verbs cluster-wide is reported under them.
func TestTheMinimalRoleSecretsReadLabelsAreTheProbesOwn(t *testing.T) {
	want := map[string]bool{rbacSecretsListLabel: false, rbacSecretsWatchLabel: false}
	for _, tuple := range flattenRequiredPermissions() {
		if _, ok := want[tuple.label]; !ok {
			continue
		}
		if tuple.namespaced {
			t.Errorf("tuple %q is probed namespaced; resolveMinimalRoleSecretsRead matches the cluster-wide label", tuple.label)
		}
		want[tuple.label] = true
	}
	for label, found := range want {
		if !found {
			t.Errorf("no tuple is labelled %q; resolveMinimalRoleSecretsRead would never see it denied", label)
		}
	}

	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return attrs.Resource == "secrets" && attrs.Namespace == "" && (attrs.Verb == "list" || attrs.Verb == "watch")
	}}
	checker := NewRBACChecker(authorizer.reviews(), "kubeagents-system")
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	denied := checker.Denied()
	sort.Strings(denied)
	if got := strings.Join(denied, ","); got != rbacSecretsListLabel+","+rbacSecretsWatchLabel {
		t.Fatalf("Denied() = %v, want exactly [%s %s]", denied, rbacSecretsListLabel, rbacSecretsWatchLabel)
	}
}

func TestProbeRBACWithEverythingGrantedReportsNothing(t *testing.T) {
	denied, err := probeLabels(context.Background(), (&fakeAuthorizer{}).reviews(), "", nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) != 0 {
		t.Fatalf("expected no denials, got %v", denied)
	}
}

func TestProbeRBACErrorIsNotADenial(t *testing.T) {
	authorizer := &fakeAuthorizer{err: errors.New("the API server is away")}
	denied, err := probeLabels(context.Background(), authorizer.reviews(), "", nil)
	if err == nil {
		t.Fatal("expected the transport error to surface")
	}
	if denied != nil {
		t.Fatalf("a probe that could not run must not report denials, got %v", denied)
	}
}

func TestTheCheckerReprobesOnItsOwnTickerAndKeepsTheLastAnswerOnError(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	checker := NewRBACChecker(authorizer.reviews(), "")
	checker.interval = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if denied, err := checker.Probe(ctx); err != nil || len(denied) != 1 {
		t.Fatalf("boot probe = %v, %v; want the one denial", denied, err)
	}
	if got := checker.Denied(); len(got) != 1 {
		t.Fatalf("Denied should read the boot answer, got %v", got)
	}

	done := make(chan struct{})
	go func() { _ = checker.Start(ctx); close(done) }()

	// The role is fixed; the ticker notices without anyone calling Denied.
	authorizer.set(nil, nil)
	waitFor(t, func() bool { return len(checker.Denied()) == 0 }, "the ticker should clear the denial")

	// A probe that cannot run keeps the previous (clean) answer rather than
	// raising a false alarm; a later denial is picked up once probes work.
	authorizer.set(nil, errors.New("timeout"))
	time.Sleep(5 * checker.interval)
	if got := checker.Denied(); len(got) != 0 {
		t.Fatalf("a failed probe must keep the previous answer; got %v", got)
	}
	authorizer.set(denyPDBPatch, nil)
	waitFor(t, func() bool { return len(checker.Denied()) == 1 }, "a new denial should be picked up")

	cancel()
	<-done
}

func waitFor(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal(what)
}

func TestProbeRBACReportsDenialsInDeclarationOrder(t *testing.T) {
	// The reviews run concurrently; the report must not depend on which
	// finished first, because the condition message compares by text.
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return attrs.Verb == "delete"
	}}
	var want []string
	for _, tuple := range flattenRequiredPermissions() {
		if tuple.verb == "delete" {
			want = append(want, tuple.label)
		}
	}
	for i := 0; i < 5; i++ {
		denied, err := probeLabels(context.Background(), authorizer.reviews(), "", nil)
		if err != nil {
			t.Fatalf("probe failed: %v", err)
		}
		if strings.Join(denied, ",") != strings.Join(want, ",") {
			t.Fatalf("run %d: denied = %v, want declaration order %v", i, denied, want)
		}
	}
}

func TestProbeRBACHonoursItsTimeout(t *testing.T) {
	clientset := k8sfake.NewClientset()
	clientset.PrependReactor("create", "selfsubjectaccessreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		time.Sleep(50 * time.Millisecond)
		review := action.(k8stesting.CreateAction).GetObject().(*authorizationv1.SelfSubjectAccessReview)
		review.Status.Allowed = true
		return true, review, nil
	})
	checker := NewRBACChecker(clientset.AuthorizationV1().SelfSubjectAccessReviews(), "")
	checker.timeout = 20 * time.Millisecond
	started := time.Now()
	denied, err := checker.Probe(context.Background())
	if err == nil {
		t.Fatal("a probe past its deadline should report an error, not an answer")
	}
	if denied != nil || len(checker.Denied()) != 0 {
		t.Fatalf("a timed-out probe must not record denials, got %v / %v", denied, checker.Denied())
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("the deadline did not stop the probe: %v", elapsed)
	}
}

func TestANilCheckerNeverProbes(t *testing.T) {
	var checker *RBACChecker
	if got := checker.Denied(); got != nil {
		t.Fatalf("nil checker reported %v", got)
	}
	if err := checker.Start(context.Background()); err != nil {
		t.Fatalf("nil checker Start = %v", err)
	}
	if denied, err := checker.Probe(context.Background()); denied != nil || err != nil {
		t.Fatalf("nil checker Probe = %v, %v", denied, err)
	}
}

// TestReconcileReportsAnOutOfDateClusterRoleAndKeepsGoing is the incident in
// #1009 as a test: the ClusterRole lacks `patch poddisruptionbudgets`, and
// the operator has to say so on the CR rather than only in an error loop —
// while still reconciling everything else, because a missing verb on one
// step is not a reason to withhold the workload.
func TestReconcileReportsAnOutOfDateClusterRoleAndKeepsGoing(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	checker := NewRBACChecker(authorizer.reviews(), "")
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	// Re-applying the manifests triggers no reconcile, so while the condition
	// stands the controller has to come back on its own to notice the fix.
	if result.RequeueAfter != rbacReprobeInterval {
		t.Errorf("RequeueAfter = %v while RBAC is incomplete, want %v", result.RequeueAfter, rbacReprobeInterval)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatalf("re-read agent: %v", err)
	}
	degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType)
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != reasonRBACIncomplete {
		t.Fatalf("expected Degraded=True/%s on the CR, got %+v", reasonRBACIncomplete, degraded)
	}
	for _, want := range []string{"patch poddisruptionbudgets.policy", RBACIncompleteCause} {
		if !strings.Contains(degraded.Message, want) {
			t.Errorf("condition message %q does not name %q", degraded.Message, want)
		}
	}
	// Ready is still whatever the reconcile decided; the skew is reported
	// beside it, not instead of it.
	if ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready"); ready == nil {
		t.Error("the Ready condition went missing; the self-check must not replace updateStatusReady")
	}

	// The rest of the reconcile ran: the workload exists.
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the self-check withheld the workload: %v", err)
	}

	// Re-apply the role; the ticker's next probe sees it and the requeued
	// reconcile clears the condition.
	authorizer.set(nil, nil)
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("re-probe: %v", err)
	}
	result, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatalf("re-read agent: %v", err)
	}
	if degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType); degraded != nil {
		t.Fatalf("the condition should clear once the role is current, still have %+v", degraded)
	}
	if result.RequeueAfter == rbacReprobeInterval {
		t.Error("the RBAC poll should stop once nothing is denied")
	}
}

// TestAStandingRBACDenialDoesNotWriteStatusEveryPass guards against the loop
// the report is meant to replace: the PlatformAgent watch has no generation
// filter, so a status write on every reconcile re-enqueues the agent at once.
// While a denial stands, a pass that changes nothing must write nothing.
func TestAStandingRBACDenialDoesNotWriteStatusEveryPass(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
	}
	statusWrites := 0
	funcs := fakeServerSideApplyInterceptors()
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		statusWrites++
		return cl.SubResource(subResourceName).Update(ctx, obj, opts...)
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).
		Build()
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews(), "")
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("first Reconcile failed: %v", err)
	}
	if statusWrites == 0 {
		t.Fatal("the first pass should have written the condition")
	}
	for pass := 2; pass <= 4; pass++ {
		before := statusWrites
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", pass, err)
		}
		if statusWrites != before {
			t.Fatalf("pass %d wrote status %d time(s) with nothing changed; that re-enqueues the agent forever", pass, statusWrites-before)
		}
	}
}

// TestTheRBACSkewYieldsToAnotherDegradedReason pins the ownership rule: while
// updateStatusReady's InvalidGitRepoURL holds the Degraded condition, the RBAC
// report neither overwrites it nor flaps it, and still asks for the poll.
func TestTheRBACSkewYieldsToAnotherDegradedReason(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	foreign := metav1.Condition{
		Type: degradedConditionType, Status: metav1.ConditionTrue,
		Reason: "InvalidGitRepoURL", Message: "not a URL", LastTransitionTime: metav1.Now(),
	}
	meta.SetStatusCondition(&agent.Status.Conditions, foreign)
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		Build()
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews(), "")
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}

	degraded, err := r.reportRBACSkew(context.Background(), agent)
	if err != nil {
		t.Fatalf("reportRBACSkew: %v", err)
	}
	if !degraded {
		t.Error("a denial must still be reported as degraded so the caller keeps polling")
	}
	got := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	if got == nil || got.Reason != foreign.Reason || got.Message != foreign.Message {
		t.Fatalf("the foreign Degraded reason was overwritten: %+v", got)
	}
}

// TestTheEventWriteIsProbed: the Recorder's create on events is a permission
// an older ClusterRole does not hold (it granted events read-only), and the
// self-check has to name it rather than let the recorder fail quietly in the
// background.
func TestTheEventWriteIsProbed(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return attrs.Group == "" && attrs.Resource == "events" && (attrs.Verb == "create" || attrs.Verb == "patch")
	}}
	denied, err := probeLabels(context.Background(), authorizer.reviews(), "", nil)
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	got := append([]string(nil), denied...)
	sort.Strings(got)
	if want := []string{"create events", "patch events"}; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("denied = %v, want %v", denied, want)
	}
}
