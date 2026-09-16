package controller

import (
	"os"
	"slices"
	"strings"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Preconditions the per-session credential design (round_2/a2-decision.md)
// stands on. Each one is a property of the whole render rather than of any one
// object, so each is checked by walking every Role and ClusterRole this
// operator produces instead of by reading the one that seemed relevant.
//
// These are not tests of new code. They are tests that the ground the new code
// is built on has not moved — the failure they exist to catch is someone
// adding a plausible-looking grant months from now and nothing objecting.

// renderedRole is one rendered RBAC object and where it came from, so a
// failure names the builder rather than an index.
type renderedRole struct {
	from  string
	rules []rbacv1.PolicyRule
}

// everyRenderedRole is every Role and ClusterRole the operator renders for a
// PlatformAgent. The count is asserted by TestEveryRenderedRoleIsOnThisList so
// a new builder cannot quietly escape the sweeps below.
func everyRenderedRole(agent *agentv1alpha1.PlatformAgent) []renderedRole {
	return []renderedRole{
		{"buildA2ACalloutRole", buildA2ACalloutRole(agent).Rules},
		{"buildA2AGatewayRole", buildA2AGatewayRole(agent).Rules},
		{"buildCredentialBrokerTokenReviewRole", buildCredentialBrokerTokenReviewRole(agent).Rules},
		{"buildMinimalPlatformRole", buildMinimalPlatformRole(agent).Rules},
		{"buildPlatformLocalRole", buildPlatformLocalRole(agent).Rules},
		{"buildPlatformLeaderRole", buildPlatformLeaderRole(agent).Rules},
	}
}

// ruleReaches answers whether one PolicyRule permits verb on group/resource,
// wildcards included.
//
// The wildcards are the point. A sweep that compared resource strings for
// equality would report a clean bill of health against a rule granting `*` on
// `*`, which is precisely the rule most worth finding. `resources: ["*"]` also
// covers subresources, so a rule holding it reaches serviceaccounts/token
// whether or not anyone typed those words.
func ruleReaches(rule rbacv1.PolicyRule, group, resource, verb string) bool {
	matches := func(list []string, want string) bool {
		return slices.Contains(list, "*") || slices.Contains(list, want)
	}
	if !matches(rule.APIGroups, group) || !matches(rule.Verbs, verb) {
		return false
	}
	if matches(rule.Resources, resource) {
		return true
	}
	// `pods` does not imply `pods/exec`, but `*` implies both, and that is
	// already handled above. Nothing else in RBAC widens a resource name.
	return false
}

// operatorClusterRole is the operator's own ClusterRole as it ships, parsed
// rather than grepped so a wildcard rule is seen for what it is.
func operatorClusterRole(t *testing.T) renderedRole {
	t.Helper()
	const path = "../../config/rbac/role.yaml"
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading %s: %v", path, err)
	}
	var cr rbacv1.ClusterRole
	if err := yaml.UnmarshalStrict(raw, &cr); err != nil {
		t.Fatalf("parsing %s: %v", path, err)
	}
	if len(cr.Rules) == 0 {
		t.Fatalf("%s parsed to no rules; the sweep would pass vacuously", path)
	}
	return renderedRole{path, cr.Rules}
}

// Nothing this deployment renders may mint a ServiceAccount token.
//
// A session pod's credential is a projected token the kubelet mints, bound to
// that pod, and the callout builds its grants from the pod the API server
// attests. `create` on `serviceaccounts/token` is the API that breaks that
// chain: the holder asks the API server for a token for any ServiceAccount it
// can name, with any audience and any bound object it likes — or none, in
// which case nothing ties the token to a pod at all. One such grant anywhere
// in the namespace turns per-session credentials back into a shared one, and
// it does it without touching a line of the callout.
//
// The operator's own ClusterRole is swept too. It is the most privileged thing
// here and the easiest place for the grant to arrive by way of a kubebuilder
// marker someone added for an unrelated reason.
func TestNothingRenderedCanMintServiceAccountTokens(t *testing.T) {
	roles := append(everyRenderedRole(a2aTestAgent()), operatorClusterRole(t))
	for _, r := range roles {
		for i, rule := range r.rules {
			if ruleReaches(rule, "", "serviceaccounts/token", "create") {
				t.Errorf("%s rule %d can mint ServiceAccount tokens (%v on %v in %v); "+
					"a token minted this way need not be bound to any pod, which is the whole basis of per-session grants",
					r.from, i, rule.Verbs, rule.Resources, rule.APIGroups)
			}
		}
	}
}

// Creating a pod is creating a bus principal, so the list of things that can
// do it is short and deliberate.
//
// Under claim narrowing a session pod's grants are derived from its own name,
// which the gateway chooses. Anything that can create a pod in this namespace
// with the session ServiceAccount and a name of its choosing can therefore
// mint itself a session identity — and, because the name is the whole
// derivation, a name colliding with a live session's. The gateway is the one
// component whose job that is. The operator holds it because it manages the
// namespace's workloads. Nothing else does.
func TestPodsCreateIsTheGatewayAndTheOperatorOnly(t *testing.T) {
	const allowed = "buildA2AGatewayRole"
	for _, r := range everyRenderedRole(a2aTestAgent()) {
		for i, rule := range r.rules {
			if !ruleReaches(rule, "", "pods", "create") {
				continue
			}
			if r.from != allowed {
				t.Errorf("%s rule %d can create pods (%v on %v); only %s and the operator may, "+
					"because a pod running as the session ServiceAccount IS a bus principal named by whoever created it",
					r.from, i, rule.Verbs, rule.Resources, allowed)
			}
		}
	}
	// The positive half, so this test fails if the gateway loses the grant
	// rather than passing on an empty world.
	gw := buildA2AGatewayRole(a2aTestAgent())
	found := false
	for _, rule := range gw.Rules {
		if ruleReaches(rule, "", "pods", "create") {
			found = true
		}
	}
	if !found {
		t.Error("the gateway cannot create pods; either the grant moved or this sweep is now checking nothing")
	}
	op := operatorClusterRole(t)
	found = false
	for _, rule := range op.rules {
		if ruleReaches(rule, "", "pods", "create") {
			found = true
		}
	}
	if !found {
		t.Errorf("%s cannot create pods; the exemption this test grants it is now unused and should go", op.from)
	}
}

// The sweeps above are only as good as their list. This is the tripwire: it
// reads the package's own source for role builders and fails when one exists
// that everyRenderedRole does not name.
//
// A test that enumerates by hand and never notices an addition is worse than
// no test, because it reports a property of six objects as a property of the
// render.
func TestEveryRenderedRoleIsOnThisList(t *testing.T) {
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	listed := make(map[string]bool)
	for _, r := range everyRenderedRole(a2aTestAgent()) {
		listed[r.from] = true
	}
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		src, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, line := range strings.Split(string(src), "\n") {
			if !strings.HasPrefix(line, "func build") {
				continue
			}
			// A builder of an RBAC subject holder, not of a binding: the
			// return type is what decides, because "Role" appears in
			// RoleBinding too.
			if !strings.Contains(line, "*rbacv1.Role {") && !strings.Contains(line, "*rbacv1.ClusterRole {") {
				continue
			}
			fn := strings.TrimSuffix(strings.SplitN(strings.TrimPrefix(line, "func "), "(", 2)[0], " ")
			if !listed[fn] {
				t.Errorf("%s renders RBAC (%s) and everyRenderedRole does not list it, so the precondition sweeps in this file never saw it", fn, name)
			}
		}
	}
}
