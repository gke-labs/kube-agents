package controller

import (
	"sort"
	"strings"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
)

// Every Role and ClusterRole the operator renders is applied under the
// operator's own identity, and the API server refuses a role whose grantor
// does not hold every verb in it (RBAC escalation prevention; the operator
// holds no `escalate`). So every (group, resource, verb) a rendered role
// grants has to be one the operator's markers declare: cluster-wide, or, for
// a namespaced Role, in the Role document bound where the operator renders
// it. A verb dropped from the markers because "nothing calls it" while a
// rendered role still grants it is the regression this pins: the local Role
// grants get/list/watch on platformagents/status, which no client call sends.
//
// resourceNames are ignored on both sides: an unscoped grant covers a named
// one, and the operator's own named grants (bind on system:auth-delegator,
// get on the dispatcher admission policy) are not verbs any rendered role
// grants.
func TestEveryGrantTheOperatorRendersIsHeldByItsOwnRole(t *testing.T) {
	agent := a2aTestAgent()
	const authMapShards = 1
	rendered := []struct {
		name  string
		rules []rbacv1.PolicyRule
		// clusterScoped roles can only be covered by the operator's
		// cluster-wide tuples; a namespaced Role is covered by either.
		clusterScoped bool
	}{
		{"minimal (ClusterRole)", buildMinimalPlatformRoleWithSecrets(agent, true).Rules, true},
		{"credential broker tokenreview (ClusterRole)", buildCredentialBrokerTokenReviewRole(agent).Rules, true},
		{"local", buildPlatformLocalRole(agent).Rules, false},
		{"leader", buildPlatformLeaderRole(agent).Rules, false},
		{"gateway", buildA2AGatewayRole(agent).Rules, false},
		{"callout", buildA2ACalloutRole(agent, authMapShards).Rules, false},
		{"scope publisher", buildScopePublisherRole(agent).Rules, false},
		{"dispatcher config", buildA2ADispatcherConfigRole(agent).Rules, false},
		{"dispatcher pods", buildA2ADispatcherPodsRole(agent).Rules, false},
		{"dispatcher jobs", buildA2ADispatcherJobsRole(agent).Rules, false},
	}

	clusterWide := map[string]struct{}{}
	namespaced := map[string]struct{}{}
	for _, tuple := range flattenRequiredPermissions() {
		resource := tuple.resource
		if tuple.subresource != "" {
			resource += rbacSubresourceSeparator + tuple.subresource
		}
		key := permissionKey(tuple.verb, tuple.group, resource, nil, false)
		if tuple.namespaced {
			namespaced[key] = struct{}{}
		} else {
			clusterWide[key] = struct{}{}
		}
	}

	for _, role := range rendered {
		var missing []string
		for _, rule := range role.rules {
			for _, group := range rule.APIGroups {
				for _, resource := range rule.Resources {
					for _, verb := range rule.Verbs {
						key := permissionKey(verb, group, resource, nil, false)
						if _, ok := clusterWide[key]; ok {
							continue
						}
						if _, ok := namespaced[key]; ok && !role.clusterScoped {
							continue
						}
						missing = append(missing, key)
					}
				}
			}
		}
		if len(missing) > 0 {
			sort.Strings(missing)
			t.Errorf("the %s role grants permissions the operator's markers do not declare, so the API server refuses to apply it:\n  %s",
				role.name, strings.Join(missing, "\n  "))
		}
	}
}
