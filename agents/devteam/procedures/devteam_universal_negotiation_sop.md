# SOP: DevTeam Universal Workload Negotiation Playbook

## Purpose

This Standard Operating Procedure (SOP) defines the mandatory steps for the Development Team Agent (e.g., active `@devteam-<namespace>` subagents) when receiving any cross-boundary negotiation inquiry (cluster upgrades, maintenance windows, resource requests/limits, or quota tuning) via `delegate_workload`.

## Prerequisites

- Assigned GKE Namespace and Target Cluster scope (`/opt/data/SETTINGS.md`). **CRITICAL CLUSTER SEPARATION:** You run inside the central management cluster (`kube-agent-management`). All `kubectl` commands MUST target your assigned external workload cluster (`<workload_cluster_name>`). Never inspect workloads against the local management cluster.
- Awareness of workload deployment strategies, PDB margins, peak business traffic windows, and resource requirements.

## Execution Steps

1. **Review Workload State & Disruption Margins in Target Cluster:**
   - **CRITICAL:** Target all inspection commands against your assigned workload cluster context/KUBECONFIG.
   - For **Temporal Operations** (`CLUSTER_UPGRADE`, `NODE_MAINTENANCE`): Verify deployment strategy (`Recreate` vs `RollingUpdate`), Pod Disruption Budgets (PDBs), and available replicas in your target cluster namespace. Workloads must maintain at least 1 available replica during node drains.
   - For **Spatial Operations** (`RESOURCE_NEGOTIATION`, `QUOTA_TUNING`): Inspect current container request/limit consumption (`kubectl top pods`) and verify whether proposed resource changes support peak traffic load without triggering CPU throttling or OOMKilled events.

2. **Check Business Blackout Calendars & Peak Traffic Windows:**
   - For temporal inquiries, parse proposed start/end timestamps from `TEMPORAL_CONTEXT` (e.g. `WINDOW_START: "2026-06-28T02:00:00Z"`).
   - Review `/opt/data/MEMORY.md` and daily memory logs to check blackout constraints against proposed timestamps.
   - For sensitive workloads (e.g. transactional processing or payroll batch windows), check if proposed timing conflicts with peak traffic surges or month-end utilization spikes where zero cluster upgrades or disruptions are permitted.

3. **Respond with Explicit Business Decision & Haggling:**
   - **If operational terms do not conflict with peak hours and maintain workload safety margins:**
     Conclude explicitly with:
     ```text
     STATUS: APPROVED
     RATIONALE: Proposed parameters satisfy application safety margins and blackout calendars.
     ```
   - **If proposed timing overlaps with critical blackout window / utilization spike or resource reduction would trigger outages:**
     Conclude explicitly with:
     ```text
     STATUS: REJECTED
     RATIONALE: Proposed parameters conflict with month-end reporting utilization spike or would cause application OOM failures.
     ```
   - **If parameters can be modified via negotiation (e.g. countering CPU/memory request or proposing alternative date):**
     Conclude explicitly with:
     ```text
     STATUS: COUNTER_PROPOSAL
     COUNTER_ALLOCATION: { cpu: "<required_cpu>", memory: "<required_mem>" }
     PROPOSED_WINDOW: "<alternative_offpeak_timestamp>"
     RATIONALE: Workload requires stated minimum allocation to survive peak traffic spikes.
     ```
