# SOP: Platform Universal Fleet Negotiation Playbook

## Purpose

This Standard Operating Procedure (SOP) defines the mandatory orchestration steps for the Platform Agent when negotiating any cross-boundary operation (cluster upgrades, maintenance windows, resource requests/limits, quota tuning, or network modifications) across specialized subagents.

## Prerequisites

- Registered GKE cluster and workload namespaces.
- Active `delegate_workload` custom toolset.

## Execution Steps

1. **Dispatch Exhaustive Structured Delegation Inquiries (Mandatory For All Agents):**
   - **CRITICAL RULE - NO SHORT-CIRCUITING:** You MUST execute `delegate_workload` against ALL discovered target agents (e.g., all active `operator-agent-<cluster>` and `devteam-<namespace>` subagents). Do NOT skip any agent even if an earlier agent rejects or counters the proposal.
   - Format all `delegate_workload` queries using the universal structured YAML negotiation schema supporting both temporal operations (`CLUSTER_UPGRADE`, `NODE_MAINTENANCE`) and spatial/resource operations (`RESOURCE_NEGOTIATION`, `QUOTA_TUNING`):
     ```yaml
     NEGOTIATION_HANDSHAKE:
       ACTION_TYPE: "<TEMPORAL_OPERATION | SPATIAL_OPERATION>"
       SPECIFIC_ACTION: "<CLUSTER_UPGRADE | RESOURCE_QUOTA_TUNING | HPA_SCALING | NODE_DRAIN>"
       TARGET_CLUSTER: "<workload_cluster_name>"
       TEMPORAL_CONTEXT: # Use for timing-based negotiations
         WINDOW_START: "<proposed_start_utc>"
         WINDOW_END: "<proposed_end_utc>"
         EXPECTED_DISRUPTION: "<ZERO_DOWNTIME | BRIEF_OUTAGE>"
       RESOURCE_CONTEXT: # Use for capacity/sizing negotiations
         CURRENT_ALLOCATION: { cpu: "<current>", memory: "<current>" }
         PROPOSED_ALLOCATION: { cpu: "<proposed>", memory: "<proposed>" }
         TELEMETRY_RATIONALE: "<justification>"
       INSTRUCTION: Evaluate this proposal against your domain constraints (surge headroom, blackout calendars, month-end freeze, PDB margins, or quota boundaries). Conclude explicitly with STATUS: APPROVED, STATUS: REJECTED, or STATUS: COUNTER_PROPOSAL with concrete justification.
     ```

2. **Synthesize Multi-Agent Assessments & Haggling:**
   - Collect returned decision blocks (`STATUS: APPROVED`, `STATUS: REJECTED`, or `STATUS: COUNTER_PROPOSAL`) from all target agents. Verify assessments from all operator and devteam tiers are present.
   - If an agent returns `COUNTER_PROPOSAL` (e.g., proposing an alternative timestamp or lower CPU/memory allocation), evaluate the counter-proposal against fleet policies.

3. **Formulate Final Fleet Consensus:**
   - **If all target agents returned APPROVED:**
     Confirm the negotiated terms and conclude explicitly with:
     ```text
     STATUS: APPROVED
     CONSENSUS: All specialized agent tiers (Operator and DevTeam tiers) have negotiated and approved the proposed operational terms.
     ```
   - **If any target agent returned REJECTED:**
     Synthesize the rejection rationale and conclude explicitly with:
     ```text
     STATUS: REJECTED
     CONSENSUS: Proposal rejected due to workload or infrastructure boundary constraints. Propose revised parameters based on agent feedback.
     ```
   - **If consensus requires adopting a COUNTER_PROPOSAL:**
     Synthesize the agreed compromise and conclude explicitly with:
     ```text
     STATUS: COUNTER_PROPOSAL
     CONSENSUS: All tiers have aligned on revised negotiated parameters.
     ```
