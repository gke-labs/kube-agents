# ROUTING.md - Platform Delegation Playbook

Use this handoff playbook to coordinate work across dynamic agents.

## Routing Boundaries

- **Platform Agent (`platform`)**: Dynamic subagent provisioning, cluster creation, RBAC controls, security boundary policies.
- **Development Team Agent (`devteam-<cluster>-<location>-<namespace>`)**: Source code changes, app deployment manifests, canary setup, application debugging in a namespace.
- **Cluster Operator Agent (`operator-<cluster>-<location>`)**: Node capacity tuning, cluster version upgrades, live event troubleshooting, certificates, alerts at the cluster level.

## Required Subagent Proof Gating

Do not announce a task complete to the user without checking subagent outputs:

### 1. DevTeam Verification Checklist

- GitHub Pull Request URL and active repo path.
- Deployment status metrics showing all replicas are healthy (if applicable after merge).
- Staging application URL or terminal verification of curl outputs.

### 2. Operator Verification Checklist

- Cluster contexts inspected.
- Output comparison illustrating specific metrics or resource configurations before and after the change.
- Verification of dynamic node pool updates.

## Recommended Handoff Assignment Templates

### Dynamic App Handoff

> `@devteam-<cluster>-<location>-<namespace> Implement <feature>. Work in repo <repo>. Return proof: GitHub PR URL, repo path, git HEAD SHA, changed file names, and rollout verification plan.`

### Dynamic Operator Handoff

> `@operator-<cluster>-<location> Audit capacity in cluster <cluster>. Investigate <issues>. Return proof: context used, CLI before/after states, event log logs, and remediation outputs.`

## Subagent-to-Subagent Delegation Relay

If an active `devteam-<cluster>-<location>-<namespace>` subagent triggers an instruction scoped for the GKE cluster (e.g., requesting node resource class optimizations), the `platform` agent must:

1. Capture the generic `@operator` or `@platform` request in the subagent logs.
2. Resolve the target to the specific active `operator-<cluster>-<location>` agent ID for that cluster context.
3. Relay the query to the resolved `operator-<cluster>-<location>` agent session.
4. Mirror the operator's response back to the `devteam-<cluster>-<location>-<namespace>` workspace to facilitate optimization alignment.
