# 🧭 kube-agents — The Kubernetes Agentic Harness

**Stop driving your clusters. Start delegating them.**

`kube-agents` replaces the traditional imperative DevOps presentation layer — `kubectl`, `gcloud`, the Google Cloud Console — with autonomous, proactive AI agents that manage your Kubernetes/GKE infrastructure, enforce multi-tenant governance, and continuously audit security posture. Instead of you reacting to pages and typing commands, a **Platform Agent** watches your fleet around the clock, opens pull requests with fixes, and reports to you in chat.

| Traditional Ops                              | With `kube-agents`                                                                                           |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Reactive, manual toil (`kubectl` + runbooks) | Proactive, intent-driven operations                                                                          |
| Drift discovered during incidents            | Scheduled compliance & blueprint audits ([autonomous watchdogs](agents/platform/cron/jobs.json))             |
| Hand-rolled RBAC and tenancy reviews         | Automated RBAC & boundary enforcement, [credential isolation by design](docs/credential-isolation-design.md) |
| Patch Tuesdays and CVE spreadsheets          | Daily vulnerability & patch scans with staggered rollout orchestration                                       |
| One human, one terminal                      | ChatOps with the agent over Google Chat & Slack                                                              |

📗 **Full documentation: [gke-labs.github.io/kube-agents](https://gke-labs.github.io/kube-agents/)**

[![An SRE types "@kube-agents run a full self-health check across the fleet" into a Google Chat room. The agent replies in a thread and streams its report back section by section: fleet and cluster state, agent and sidecar health, the active cron audit schedule, and a closing summary of warnings and failures.](.github/assets/chat-demo.webp)](https://gke-labs.github.io/kube-agents/)

_An SRE asks for a fleet self-health check; the agent answers in the thread. An illustrative replay — the names and figures are examples. It runs live at the top of the [documentation site](https://gke-labs.github.io/kube-agents/)._

## 💬 Five things to ask it

Each of these is a single chat message. The agent answers in the thread from read-only reads of the fleet, the GCP projects it monitors, and the linked GitOps repositories, and it recommends changes without applying them.

1. **"Which clusters are behind their release channel, and what would block upgrading them?"** — Every cluster's control-plane and node-pool versions against the channel default, with what would stop the upgrade named: drain-blocking PodDisruptionBudgets, fail-closed admission webhooks with an unreachable backend in the upgrade's path or able to refuse the control plane's bootstrap RBAC writes (ClusterRoles and ClusterRoleBindings; Roles and RoleBindings in kube-system and kube-public), maintenance exclusions, and node-pool version skew. Name a target version and it also scans the GitOps manifests for the `apiVersions` that version removes. ([`fleet-upgrade-verification`](agents/platform/skills/fleet-upgrade-verification/SKILL.md))
2. **"Which workloads request far more CPU and memory than they use?"** — Live `kubectl top` readings compared with each workload's requests, so the answer names the over-requested controllers in resource units; a stock install has no billing export to price them. ([`gke-cost-analysis`](agents/platform/skills/gke-cost-analysis/SKILL.md))
3. **"Design a Standard cluster for 32 A100s in us-central1. Check quota and live obtainability, and give me a ComputeClass fallback. Design only."** — Quota and capacity are checked separately, each against live `gcloud` evidence, and the design names the fallback tiers that change the workload's GPU class or interconnect characteristics. ([`capacity-obtainability`](agents/platform/skills/capacity-obtainability/SKILL.md), [`gke-compute-classes`](agents/platform/skills/gke-compute-classes/SKILL.md))
4. **"Something in the `payments` namespace on `prod-east` stopped making progress without erroring. Find it."** — The work is delegated to the Cluster Agent for that cluster, which looks for controllers whose `observedGeneration` lags, progress conditions that stopped advancing, repeating warning events, and references to objects that do not exist. ([`gke-stall-detection`](agents/cluster/skills/gke-stall-detection/SKILL.md))
5. **"`checkout` on `prod-east` has been crash-looping since this morning. What happened?"** — The Cluster Agent for that cluster fixes the time window, reads the container's exit codes and the events around them, tells an OOM kill from an application crash, and proposes the manifest correction without applying it. ([`gke-workload-troubleshooting`](agents/cluster/skills/gke-workload-troubleshooting/SKILL.md))

On an install running the unsupported `spec.mode: next`, the agent still answers these in the thread, with a few differences in how the answer is delivered; [how a chat question is answered under `next`](https://gke-labs.github.io/kube-agents/concepts/chatops/#how-a-chat-question-is-answered-under-next) walks through each step.

---

## ⚡ Try it now

The fastest, zero-friction way to install `kube-agents` in **Google Cloud Shell** or your terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash
```

_Substitute `<RELEASE_VERSION>` with the desired version tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases) (for example, `0.4.0`; a GitLab install needs `0.9.0` or later)._

This interactive installer (recommended for initial setup) guides you through GCP authentication, project selection, GKE cluster setup (Autopilot or Standard), chat integrations (Google Chat & Slack), and LLM model provider credentials. Sensible defaults are detected from your `gcloud` context, requiring minimal input.

### 🤖 AI Agent & Automation Usage

For automated environments, CI/CD pipelines, and AI Agents where no interactive TTY is available, invoke the release installer with `--non-interactive` and CLI flags:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="my-gcp-project" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

Or give this prompt to an AI coding assistant. It needs no checkout of this repository, and tells the assistant to confirm the target with you and show you the `--dry-run` summary before it creates any cloud resources:

```text
Install the latest official release of kube-agents (github.com/gke-labs/kube-agents) into my GCP project.
Follow INSTALL.md from that release tag — do not invent installer URLs, namespaces, or model names.
First inspect my gcloud project and existing GKE clusters and confirm the target, cluster, model provider,
and credential with me. Run install.sh with --dry-run and show me its printed summary before you change anything.
Only run the real install after I say yes.
```

The full procedure behind the prompt, including the credential and consent-flag checks, is in [INSTALL.md](INSTALL.md#ai-assisted-installation). The GitOps repository can be on GitHub or on GitLab (gitlab.com or a self-managed instance); [INSTALL.md](INSTALL.md#non-interactive--ai-agent-execution-mode) gives the flags for each. An assistant working in a checkout of this repository also picks up the [`install-kube-agents`](.agents/skills/install-kube-agents/SKILL.md) skill, which covers the same install.

Prefer to drive the engine by hand? Unpack `kube-agents-<RELEASE_VERSION>.tar.gz` from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases) (recommended), or clone the repository at an official release tag if a Git checkout is needed:

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
./install.sh                                              # the interview, then one terraform apply
# or, if a Git checkout is needed instead:
# git clone --branch <RELEASE_VERSION> https://github.com/gke-labs/kube-agents.git
# cd kube-agents && ./install.sh
# or, with your own terraform.tfvars:
cd terraform/examples/full-install && ./lifecycle.sh apply
```

Both paths run the same engine: `terraform/examples/full-install` provisions every GCP resource and installs the Helm chart that owns every Kubernetes one, end to end and idempotently. `./uninstall.sh` (or `lifecycle.sh destroy`) reverses it. See the [quick start](https://gke-labs.github.io/kube-agents/install/quickstart-gke/) for the walkthrough, or [INSTALL.md](INSTALL.md) for manual and local-development paths.

---

## 📖 What it is

The harness runs co-located agents in a single operator-deployed pod: the **Planning Agent** — the conversational front door that receives every chat message, works out what it needs, and delegates that work over a shared kanban board — the **Platform Agent** — the master custodian and agent architect that manages the GKE infrastructure lifecycle, establishes multi-tenancy boundaries, and enforces fleet-wide compliance — and a **Cluster Agent** per managed cluster, a single-cluster SRE persona the Platform Agent scaffolds from the [`agents/cluster/`](agents/cluster/) template for runtime operations and workload debugging, with read-only access to the cluster it watches. The Platform Agent is driven by:

- 🧬 **A persona** — [`agents/platform/SOUL.md`](agents/platform/SOUL.md) defines its identity, its _Automation First_ rule (no manual cluster mutations; changes flow through declarative, PR-based workflows), and its _Least Privilege_ constraint.
- 📚 **Governance playbooks** — SOPs in [`agents/platform/governance/`](agents/platform/governance/) covering compliance and security audits, fleet consistency drift, cost analysis, stockout prevention, and security patch orchestration.
- 🛠️ **Skills** — task-focused `SKILL.md` bundles under [`agents/platform/skills/`](agents/platform/skills/): cluster creation, app onboarding, cost analysis, backup & DR, and manifest generation. Single-cluster runtime skills — workload troubleshooting, observability, autoscaling, storage — belong to the Cluster Agent in [`agents/cluster/skills/`](agents/cluster/skills/). See the [skill catalog](https://gke-labs.github.io/kube-agents/skills/).
- ⏰ **Autonomous watchdogs** — cron-driven governance jobs in [`agents/platform/cron/jobs.json`](agents/platform/cron/jobs.json) that keep the fleet honest without human prompting. Ticking belongs to the Planning Agent's gateway, the only running one, so a job on its roster advances the Platform Agent's schedule once a minute. See [proactive autonomy](https://gke-labs.github.io/kube-agents/overview/proactive-autonomy/).

The runtime is built on the Hermes agent framework and wires in MCP servers for platform control and GKE's hosted MCP endpoint, so the agent speaks to your clusters through structured tools rather than raw shell access.

For three scenarios from trigger to pull request (a scheduled audit, a crash loop alert, and a chat question), see [A day with kube-agents](https://gke-labs.github.io/kube-agents/overview/day-in-the-life/).

---

## 🧰 Capabilities

The Platform Agent runs these audits on a schedule and posts each report to chat. Each skill is a procedure that the Platform Agent or a Cluster Agent loads when a request or an audit needs it. The [skill catalog](https://gke-labs.github.io/kube-agents/skills/) describes each skill, and the [cron job reference](https://gke-labs.github.io/kube-agents/reference/cron-jobs/) lists every scheduled job.

<!-- BEGIN GENERATED: capabilities -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

### Scheduled governance audits

| Audit | Schedule (UTC) | Procedure |
| ----- | -------------- | --------- |
| Security & RBAC Posture Audit | Daily 06:20 | [`compliance_audit_sop.md`](agents/platform/governance/compliance_audit_sop.md) |
| Workload Reliability Audit | Daily 06:50 | [`obtainability_audit_sop.md`](agents/platform/governance/obtainability_audit_sop.md) |
| GCE Compute Engine and MIG Fleet Audit | Daily 07:45 | [`gce_compute_fleet_sop.md`](agents/platform/governance/gce_compute_fleet_sop.md) |
| GCP Networking Fabric & VPC IPAM Audit | Daily 08:00 | [`gcp_networking_fabric_sop.md`](agents/platform/governance/gcp_networking_fabric_sop.md) |
| AI Workload Security Audit | Daily 08:50 | [`ai_security_audit_sop.md`](agents/platform/governance/ai_security_audit_sop.md) |
| Fleet Stockout Prevention & Capacity Audit | Daily 09:20 | [`stockout_prevention_sop.md`](agents/platform/governance/stockout_prevention_sop.md) |
| Upgrade & Patch Readiness Audit | Weekly, Monday 07:20 | [`security_patch_orchestrator_sop.md`](agents/platform/governance/security_patch_orchestrator_sop.md) |
| Fleet Waste Audit | Weekly, Monday 07:50 | [`fleet_wide_cost_analysis_sop.md`](agents/platform/governance/fleet_wide_cost_analysis_sop.md) |
| Fleet Consistency Drift Audit | Weekly, Monday 08:20 | [`fleet_consistency_drift_sop.md`](agents/platform/governance/fleet_consistency_drift_sop.md) |

### Skills by area

| Area | Skills |
| ---- | ------ |
| Cluster lifecycle | `cluster-agent-lifecycle`, `gke-cluster-creation`, `gke-multitenancy`, `manage-cluster` |
| Workloads | `gke-app-onboarding`, `gke-batch-hpc`, `gke-workload-scaling`, `gke-workload-security`, `gke-workload-troubleshooting`, `workload-rebalancing` |
| Cost and capacity | `capacity-obtainability`, `gce-compute-fleet-audit`, `gke-cluster-autoscaler`, `gke-compute-classes`, `gke-cost-analysis`, `gke-cost-optimization`, `gke-productionize` |
| Security and compliance | `gke-backup-dr`, `gke-platform-security`, `gke-workload-identity` |
| Networking and storage | `gcp-networking-fabric-audit`, `gke-networking`, `gke-service-networking`, `gke-storage` |
| AI and inference | `gke-ai-troubleshooting-handle-disruption-gpu-tpu`, `gke-ai-troubleshooting-jobset-interruption`, `gke-ai-troubleshooting-tpu-vbar-oom`, `gke-golden-path`, `gke-inference`, `gke-tpu-dynamic-slices-monitoring`, `gke-tpu-metrics-monitoring` |
| Observability | `gke-basics`, `gke-observability`, `kube-agents-observability` |
| Reliability | `fleet-upgrade-verification`, `gke-reliability`, `gke-upgrades` |
| Manifests and remediation | `gcp-config-connector`, `gke-manifest-generation`, `submit-suggestion` |
| Meta | `fleet-audit`, `fleet-audit-reports`, `github-issue-resolver`, `inspect-repository`, `pr-conversation`, `version-control` |
| Cluster Agent (per-cluster runtime) | `gke-observability`, `gke-reliability`, `gke-stall-detection`, `gke-storage`, `gke-workload-scaling`, `gke-workload-security`, `gke-workload-troubleshooting` |

<!-- prettier-ignore-end -->
<!-- END GENERATED: capabilities -->

---

## 🛡️ Governance & isolation

`kube-agents` is designed for enterprise fleets where agents must be powerful _and_ provably contained:

- **Least-privilege RBAC** — the agent's Kubernetes identity is read-only and cannot read Secrets.
- **Credential isolation** — model-authored code runs in a shell sandbox pod that holds no API keys or tokens; an Envoy credential broker in a pod of its own injects them at the network boundary.
- **At-rest database encryption & state security** — GKE etcd database encryption (CMEK) via Cloud KMS, strict state file permissions (`umask 077`), and mandatory encryption pre-flight gates.
- **Kernel-level sandboxing** — agent workloads run under a gVisor RuntimeClass (GKE Sandbox) by default; `--enable-gvisor=false` opts out.
- **GitOps-only mutations** — infrastructure changes are proposed as pull requests for human review.

Exactly what is _enforced_ on which plane — Kubernetes RBAC, GCP IAM, and the GitOps path each answer differently — is set out in [Security & IAM](https://gke-labs.github.io/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do). Read that before granting the agent access to a production project.

---

## 🏗️ Architecture

```mermaid
flowchart TB
    subgraph agent["🧠 Control Plane — Agent Layer"]
        SOUL["SOUL.md persona<br/>+ governance SOPs"]
        SKILLS["Skills<br/>(agents/platform/skills)"]
        CRON["Scheduled watchdogs<br/>(cron/jobs.json)"]
        PA["Platform Agent workspace<br/>(agents/platform)"]
        SOUL --> PA
        SKILLS --> PA
        CRON --> PA
    end

    subgraph cluster["☸️ Cluster Plane — Kubernetes Layer"]
        OP["k8s-operator<br/>(Go / Kubebuilder)"]
        CRD["PlatformAgent CRD<br/>kubeagents.x-k8s.io/v1alpha1"]
        POD["Agent pod: gVisor sandbox<br/>+ Envoy credential proxy<br/>+ Fluent Bit + event watcher"]
        RBAC["RBAC isolation boundaries<br/>(read-only view + explorer)"]
        OP -->|reconciles| CRD
        CRD --> POD
        OP --> RBAC
    end

    subgraph integration["🔀 Integration & Routing Layer"]
        LLM["LiteLLM Gateway<br/>Gemini · Vertex AI · OpenAI · Anthropic"]
        CHAT["Messaging bridges<br/>Google Chat (Pub/Sub) · Slack (Socket Mode)"]
        GH["Minty — GitHub App<br/>token minter (KMS)"]
    end

    PA -.runs inside.-> POD
    POD --> LLM
    CHAT <--> POD
    POD -->|PR-based changes| GH
```

Walkthrough: [Architecture](https://gke-labs.github.io/kube-agents/overview/architecture/). The [`k8s-operator/`](k8s-operator/) reconciles `PlatformAgent` custom resources into the sandboxed agent pod, its sidecars, per-agent ServiceAccounts with Workload Identity, read-only RBAC, and Services.

> **Looking for the end-state design?** [`docs/architecture/`](docs/architecture/) specifies a three-tier, fully read-only agent model that this repository is converging toward. It describes the target, not what ships today.

---

## 🤝 Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the CLA and where the contributor workflow is documented. Repository conventions for AI coding agents are in [AGENTS.md](AGENTS.md).

Bug reports and feature requests go in [issues](https://github.com/gke-labs/kube-agents/issues). If your GitHub account cannot open one here, use the [feedback form](https://gke-labs.github.io/kube-agents/feedback), which files it for you.

## Disclaimer

This is not an officially supported Google product.

This project is not eligible for the Google Open Source Software Vulnerability Rewards Program.
