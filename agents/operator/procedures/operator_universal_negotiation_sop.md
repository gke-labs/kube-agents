# SOP: Operator Universal Negotiation Playbook

## Purpose

This Standard Operating Procedure (SOP) defines the mandatory steps for the Kubernetes Cluster Operator Agent when receiving any cross-boundary negotiation inquiry (cluster upgrades, maintenance windows, node draining, resource requests/limits, or quota tuning) via `delegate_workload`.

## Prerequisites

- Active connection to the **Target Workload Cluster** defined in `/opt/data/SETTINGS.md` (e.g., `<workload_cluster_name>`). **CRITICAL:** Do not confuse the host management cluster (`<management_cluster_name>`) where this agent pod is running with the target workload cluster being evaluated.
- Access to target cluster node headroom, quota boundaries, and version skew telemetry.

## Execution Steps

1. **Assess Target Cluster Infrastructure & Headroom:**
   - **CRITICAL CLUSTER SEPARATION MANDATE:** You are running inside a management cluster. All `kubectl` commands MUST be explicitly targeted at the external **Target Workload Cluster** specified in `/opt/data/SETTINGS.md`. Never run inspection commands against the default host management cluster.
   - For **Temporal Operations** (`CLUSTER_UPGRADE`, `NODE_MAINTENANCE`): Execute `kubectl get nodes` in the target cluster to check health (`Ready`), surge scaling headroom (~25% buffer per node pool), and active Pod Disruption Budgets (`allowedDisruptions > 0`).
   - For **Spatial Operations** (`RESOURCE_NEGOTIATION`, `QUOTA_TUNING`): Inspect physical node allocatable capacity and namespace `ResourceQuotas` in the target cluster to verify if proposed CPU/memory allocations fit within infrastructure limits.

2. **Coordinate with Target DevTeam Workloads:**
   - Whenever an operation intersects with developer namespaces, discover all active DevTeam agents (e.g., all active `devteam-<namespace>` subagents) and execute `delegate_workload` forwarding the universal YAML negotiation contract before finalizing your decision.

3. **Formulate Explicit Negotiation Assessment & Haggling:**
   - **If infrastructure capacity/headroom supports the proposal:**
     Conclude explicitly with:
     ```text
     STATUS: APPROVED
     RATIONALE: Infrastructure headroom and timing boundaries fully support the proposed operational parameters.
     ```
   - **If proposal exceeds physical capacity or breaches hard infrastructure constraints:**
     Conclude explicitly with:
     ```text
     STATUS: REJECTED
     RATIONALE: Proposed parameters exceed physical cluster allocatable limits or conflict with system blackout window.
     ```
   - **If terms can be adjusted (e.g. haggling CPU/memory sizing or proposing alternative timestamp):**
     Conclude explicitly with:
     ```text
     STATUS: COUNTER_PROPOSAL
     COUNTER_ALLOCATION: { cpu: "<counter_cpu>", memory: "<counter_mem>" }
     PROPOSED_WINDOW: "<alternative_timestamp>"
     RATIONALE: Adjusted parameters fit within physical cluster headroom while maintaining safety margins.
     ```
