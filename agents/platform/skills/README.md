# Platform Agent Skills Guide

This directory contains the bundled skills for the **Platform Agent**. Skills extend the agent's intent-driven capabilities by providing Standard Operating Procedures (SOPs), diagnostic workflows, and automated scripts.

## How Skills Work

The Platform Agent automatically scans and registers all skill subdirectories under `agents/platform/skills/` during container startup (`[stage2] Setup complete`).

Each skill directory must contain:

- **`SKILL.md`** (Required): The primary instruction file with YAML frontmatter (`name`, `description`) and markdown workflows.
- **`scripts/`** (Optional): Executable Bash/Python scripts used by the skill.
- **`assets/`** (Optional): YAML manifests, templates, or reference configurations.

---

## Importing Skills from `google/skills`

You can easily import or update skills from Google's open-source skills repository ([`https://github.com/google/skills`](https://github.com/google/skills)) or custom team repositories:

### 1. Manual Import Procedure

To add a new skill from `google/skills`:

1. **Copy the Skill Directory**: Clone or download the skill folder into `agents/platform/skills/<skill-name>/`.
2. **Verify `SKILL.md` Frontmatter**: Ensure the top of `SKILL.md` contains valid YAML frontmatter:
   ```yaml
   ---
   name: gke-skill-name
   description: Brief 1-2 sentence description of when and why the agent should invoke this skill.
   ---
   ```
3. **Verify Execution Tools**: Ensure all commands referenced in `SKILL.md` use standard CLI tools installed in the Platform Agent container (`kubectl`, `gcloud`, `helm`, `python3`, `curl`, `jq`).

### 2. Updating Existing Skills

To update an existing skill to the latest upstream version from `google/skills`:

```bash
# Example: Syncing gke-cost-analysis from an upstream skills checkout
cp -r /path/to/upstream/skills/gke-cost-analysis/* agents/platform/skills/gke-cost-analysis/
```

Once committed and pushed, the Platform Agent container automatically loads the updated skill instructions on container restart.

---

## Skill Index

| Skill                              | Category      | Description                                                                                                |
| :--------------------------------- | :------------ | :--------------------------------------------------------------------------------------------------------- |
| **`gke-node-problem-detector`**    | Diagnostics   | Diagnoses node-level issues, kernel deadlocks, OOM kills, and hardware degradation.                        |
| **`gke-security-posture-audit`**   | Security      | Audits root execution, privileged containers, hostPath mounts, and Pod Security Admission standards.       |
| **`gke-quota-and-limits-checker`** | Quotas        | Audits namespace ResourceQuotas, LimitRanges, and unconstrained container CPU/memory limits.               |
| **`gke-gateway-api-diagnostics`**  | Ingress       | Diagnoses GKE Gateway API routes, HTTPRoute statuses, TLS cert expirations, and Cloud Armor policies.      |
| **`gke-workload-troubleshooting`** | Diagnostics   | Systematic SOP for diagnosing pod failures, CrashLoopBackOffs, resource OOMs, and PVC errors.              |
| **`gke-workload-security`**        | Security      | Audits Workload Identity, Shielded Nodes, Network Policies, gVisor, and Pod Security Standards.            |
| **`gke-cost-analysis`**            | Cost          | Answers cost queries using BigQuery export data, unattached disk detection, and Spot VM deltas.            |
| **`gke-compute-classes`**          | Compute       | Configures, optimizes, and debugs GKE ComputeClasses and accelerator targeting (GPUs/TPUs).                |
| **`gke-inference-quickstart`**     | AI/ML         | Deploys optimized AI/ML inference workloads (vLLM, Ollama, HuggingFace) on GKE.                            |
| **`gke-multi-tenancy`**            | Governance    | Implements namespace isolation, RBAC role bindings, ResourceQuotas, and LimitRanges.                       |
| **`gke-manifest-generation`**      | Manifests     | SOP for generating secure, compliant, cost-effective GKE manifests.                                        |
| **`gke-reliability`**              | Reliability   | Audits high availability, disruption budgets (PDBs), and multi-zone workload redundancy.                   |
| **`gke-observability`**            | Observability | Configures and audits Google Cloud Managed Service for Prometheus (GMP), Cloud Logging, and OpenTelemetry. |
