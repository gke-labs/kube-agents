package controller

import (
	"context"
	"sort"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/types"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// ruleReachesTiers is the tier resources an RBAC rule reaches, wildcards
// included: the same predicate as the conformance suite's reaches_tiers
// (tests/conformance/test_A_authority.py, A4).
func ruleReachesTiers(rule rbacv1.PolicyRule) []string {
	group := false
	for _, g := range rule.APIGroups {
		if g == agentv1alpha1.GroupVersion.Group || g == "*" {
			group = true
		}
	}
	if !group {
		return nil
	}
	var out []string
	for _, r := range rule.Resources {
		switch {
		case r == "*":
			out = append(out, "tiers", "tiers/status")
		case r == "tiers" || strings.HasPrefix(r, "tiers/"):
			out = append(out, r)
		case strings.HasPrefix(r, "*/"):
			// RBAC matches `*/<sub>` against that subresource of every
			// resource in the group.
			out = append(out, "tiers/"+strings.TrimPrefix(r, "*/"))
		}
	}
	return out
}

// builtInRolesNextMayBind are the ClusterRoles the operator binds without
// rendering them. None reaches tiers; a binding to any other ClusterRole the
// operator doesn't render (cluster-admin, admin, edit) would hand out what it
// holds, which this test can't read, so it fails instead.
var builtInRolesNextMayBind = map[string]bool{
	"system:auth-delegator": true,
}

// Next mode reads tiers through an operator-rendered ConfigMap, so no role it
// mints needs them. With every next-mode flag on (the MCP door, the scope
// publisher, the dispatcher in Job mode, and a profile so the profile
// namespace is populated), no Role or ClusterRole the operator applies, in any
// namespace, reaches tiers at any verb, and every binding names a role it
// renders or one of the built-in roles above (gke-labs/kube-agents#5603).
func TestNoNextModeRoleReachesTiers(t *testing.T) {
	withOperatorBusPrincipal(t)
	t.Setenv(a2aMCPDoorEnvVar, "true")
	t.Setenv(a2aGatewayPodPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	f := newJobModeFixture(t, &profile)
	ctx := context.Background()
	live := f.agent(t)
	live.Spec.Scope = &agentv1alpha1.ScopeSpec{Folders: []string{"111111111111"}}
	if err := f.c.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	// A second pass picks up what the first one's objects unlock (the
	// scope publisher's identity once its ServiceAccount is owned).
	f.mustReconcile(t)
	f.mustReconcile(t)

	var roles rbacv1.RoleList
	var clusterRoles rbacv1.ClusterRoleList
	var bindings rbacv1.RoleBindingList
	var clusterBindings rbacv1.ClusterRoleBindingList
	if err := f.c.List(ctx, &roles); err != nil {
		t.Fatal(err)
	}
	if err := f.c.List(ctx, &clusterRoles); err != nil {
		t.Fatal(err)
	}
	if err := f.c.List(ctx, &bindings); err != nil {
		t.Fatal(err)
	}
	if err := f.c.List(ctx, &clusterBindings); err != nil {
		t.Fatal(err)
	}

	// The roles this test exists for have to be there, or it checks nothing
	// the conformance suite's goldens don't already.
	rendered := map[string]bool{}
	clusterRendered := map[string]bool{}
	var names []string
	for _, r := range roles.Items {
		rendered[r.Namespace+"/"+r.Name] = true
		names = append(names, r.Namespace+"/"+r.Name)
	}
	for _, r := range clusterRoles.Items {
		clusterRendered[r.Name] = true
		names = append(names, r.Name)
	}
	sort.Strings(names)
	for _, want := range []string{
		agent.Namespace + "/" + a2aGatewayName(agent),
		agent.Namespace + "/" + a2aCalloutName(agent),
		agent.Namespace + "/" + scopePublisherName(agent),
		agent.Namespace + "/" + a2aDispatcherName(agent),
		profileNamespaceName(agent) + "/" + a2aDispatcherName(agent),
		profileNamespaceName(agent) + "/" + a2aDispatcherJobsRoleName(agent),
	} {
		if !rendered[want] {
			t.Errorf("no Role %s was applied; the operator applied %v", want, names)
		}
	}
	// The MCP server holds no Role (its access is the broker's mcp caller), so
	// what proves the door was on is its Deployment.
	var mcp appsv1.Deployment
	if err := f.c.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aMCPName(agent)}, &mcp); err != nil {
		t.Errorf("the MCP server wasn't rendered, so the door wasn't on: %v", err)
	}

	for _, r := range roles.Items {
		for _, rule := range r.Rules {
			if got := ruleReachesTiers(rule); len(got) > 0 {
				t.Errorf("Role %s/%s reaches %v: %+v", r.Namespace, r.Name, got, rule)
			}
		}
	}
	for _, r := range clusterRoles.Items {
		for _, rule := range r.Rules {
			if got := ruleReachesTiers(rule); len(got) > 0 {
				t.Errorf("ClusterRole %s reaches %v: %+v", r.Name, got, rule)
			}
		}
	}

	checkRef := func(where string, ns string, ref rbacv1.RoleRef) {
		switch ref.Kind {
		case "Role":
			if !rendered[ns+"/"+ref.Name] {
				t.Errorf("%s binds Role %s/%s, which the operator didn't render", where, ns, ref.Name)
			}
		case "ClusterRole":
			if !clusterRendered[ref.Name] && !builtInRolesNextMayBind[ref.Name] {
				t.Errorf("%s binds ClusterRole %s, which the operator doesn't render and isn't a known built-in", where, ref.Name)
			}
		}
	}
	for _, b := range bindings.Items {
		checkRef("RoleBinding "+b.Namespace+"/"+b.Name, b.Namespace, b.RoleRef)
	}
	for _, b := range clusterBindings.Items {
		checkRef("ClusterRoleBinding "+b.Name, "", b.RoleRef)
	}
}
