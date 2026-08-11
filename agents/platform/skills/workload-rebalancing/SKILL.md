---
name: workload-rebalancing
description: Orchestrate cross-cluster workload rebalancing using the kanban board with the validation-then-declare pattern. Use when fleet utilization shows one cluster overutilized and another with headroom and a workload should move.
---

# Workload Rebalancing Skill (validation-then-declare)

When live cluster utilization — checked via the read-only `gke` MCP / `kubectl top` on each cluster — shows a cluster **overutilized / under pressure** and another with **headroom**, you may relocate a workload. You act as an **orchestrator**: cluster agents _validate_ feasibility (read-only); **you** _declare_ the change as a single GitOps PR; **KCC** reconciles the actual move. Never issue imperative start/stop.

## When to use vs. do-it-yourself

Delegating is optional. Use this fan-out/fan-in when you want per-cluster local validation and a single aggregated decision. For a trivial single-cluster change, act directly.

## The card graph (fan-out validation → fan-in declare)

Resolve each cluster's profile name first (`cluster_agent_profile.py name --project … --cluster … --location …`), then:

1. **Card A — can clusterA host it?** `kanban_create(assignee="<clusterA-profile>", title="Validate can-host <workload>", body="Can you host <ns/workload> (needs ~<cpu>/<mem>)? Check capacity, affinity/taints, quotas. Do NOT mutate.")`
2. **Card B — is clusterB safe to evacuate?** `kanban_create(assignee="<clusterB-profile>", title="Validate safe-to-evacuate <workload>", body="Is it safe to evacuate <ns/workload>? Check PDBs, statefulness/local PVs, in-flight work. Do NOT mutate.")`
3. **Card C — decide & declare (you):** `kanban_create(assignee="<your-own-platform-profile>", title="Rebalance <workload> B→A: decide & declare", parents=[<A id>, <B id>])`

Cards A and B run in **parallel** (independent read-only checks). Card C is gated until both finish; the dispatcher then spawns you on it with both parents' `metadata` in your worker context. (The actual make-before-break ordering of the move is handled by KCC when it reconciles the PR, not by the agents.)

## Expected validation `metadata`

Card A (clusterA):

```json
{
  "can_host": true,
  "workload": { "namespace": "...", "name": "..." },
  "headroom_after": { "cpu_vcpu": 54, "memory_gib": 194 },
  "constraints": [],
  "confidence": "high"
}
```

Card B (clusterB):

```json
{
  "safe_to_evacuate": true,
  "workload": { "namespace": "...", "name": "..." },
  "blockers": [],
  "pdb_ok": true,
  "stateful": false,
  "in_flight_work": false,
  "notes": "..."
}
```

## Decide & declare (Card C)

- **Both green →** generate the relocation change (move the workload's manifest from clusterB's overlay to clusterA's, or flip its target-cluster field) and open **one** PR via `submit-suggestion`. KCC/Config Sync performs the move. Record the decision in `kanban_complete(metadata={"decision":"proceed","from":"clusterB","to":"clusterA","pr_url":"...","rationale":"..."})`.
- **Either red →** do not declare. Report blockers to the user, or `kanban_block(kind="needs_input")` if a human must decide.

## Safety

The change is a PR, so rollback is a revert. A failed validation aborts the declaration (no partial move). Escalate ambiguity via `needs_input`. You never mutate clusters directly — cluster agents are read-only and you emit declarative artifacts only.
