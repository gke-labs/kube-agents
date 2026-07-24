# 🧭 kube-agents — The Kubernetes Agentic Harness

**Stop driving your clusters. Start delegating them.**

`kube-agents` replaces the traditional imperative DevOps presentation layer — `kubectl`, `gcloud`, the Google Cloud Console — with autonomous, proactive AI agents that manage your Kubernetes/GKE infrastructure, enforce multi-tenant governance, and continuously audit security posture. Instead of you reacting to pages and typing commands, a **Platform Agent** watches your fleet around the clock, opens pull requests with fixes, and reports to you in chat.

| Traditional Ops                              | With `kube-agents`                                                                                           |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Reactive, manual toil (`kubectl` + runbooks) | Proactive, intent-driven operations                                                                          |
| Drift discovered during incidents            | Scheduled compliance & blueprint audits ([10 autonomous watchdogs](agents/platform/cron/jobs.json))          |
| Hand-rolled RBAC and tenancy reviews         | Automated RBAC & boundary enforcement, [credential isolation by design](docs/credential-isolation-design.md) |
| Patch Tuesdays and CVE spreadsheets          | Continuous vulnerability scanning & staggered patch orchestration                                            |
| One human, one terminal                      | Seamless multi-agent collaboration over Google Chat & Slack                                                  |

### ⚡ Try it now

The fastest way in: clone this repository into the workspace of your multi-agent platform's default agent (CrewAI, LangGraph, Microsoft AutoGen, or any Hermes-compatible harness) and delegate the setup itself to an agent:

```text
"Using kube-agents/INSTALL.md provision k8s agentic harness and create platform agent"
```

Or register the Platform Agent yourself:

**Declarative (YAML/JSON)** — for platforms and gateways that load agents from configuration:

```yaml
agents:
  - id: platform
    workspace: ./agents/platform
```

**Imperative (CLI)** — for hosts supporting CLI-driven imports:

```bash
# Register the Platform Agent from the repository root
gateway-cli agents add platform --workspace ./agents/platform --non-interactive
```

Full setup options — from a one-command GKE provisioning pipeline to local Kind development — are in [INSTALL.md](INSTALL.md) and covered in [Installation & Quickstart](#-installation--quickstart) below.

---

## 📖 Overview

At the heart of the harness is the **Platform Agent (`platform`)** — the master custodian and agent architect. It serves as the primary chat entrypoint into the entire harness, manages the GKE infrastructure lifecycle, establishes multi-tenancy boundaries, and enforces fleet-wide compliance.

The Platform Agent is driven by:

- 🧬 **An architectural persona** — [`agents/platform/SOUL.md`](agents/platform/SOUL.md) defines its identity, its _Automation First_ rule (no manual cluster mutations; all changes flow through declarative, PR-based workflows), and its _Least Privilege_ constraint (read-only fleet visibility, with narrowly scoped write access only to its own Custom Resources).
- 📚 **Operational playbooks** — nine governance SOPs in [`agents/platform/governance/`](agents/platform/governance/) covering blueprint sync, compliance audits, cost analysis, capacity orchestration, security patch orchestration, lifecycle/deprecation management, and more.
- 🛠️ **Specialized Skills** — 20 task-focused skills under [`agents/platform/skills/`](agents/platform/skills/), each a documented `SKILL.md` bundle: cluster creation from templates, app onboarding, workload troubleshooting, cost analysis via BigQuery, observability setup, autoscaling, backup & DR, and manifest generation, among others.
- 🔍 **Security review skills** — a dedicated suite under [`.agents/skills/`](.agents/skills/) for continuous security auditing: admission control (webhooks, VAP/MAP), NetworkPolicy isolation, Pod security contexts, Gateway API configurations, RBAC, service accounts, and agent-specific reviews including **prompt injection defense**, **credential isolation**, **execution sandbox hardening**, and **data exfiltration prevention**.

The agent runtime is built on the Hermes agent framework and wires in MCP servers for platform control and GKE's hosted MCP endpoint, so the agent speaks to your clusters through structured tools rather than raw shell access.

📗 **Full documentation** lives in the docs site under [`docs/site/src/content/docs/`](docs/site/src/content/docs/), including [architecture](docs/site/src/content/docs/overview/architecture.mdx), [concepts](docs/site/src/content/docs/concepts/), and a [complete skill catalog](docs/site/src/content/docs/skills/index.mdx).

---

## 🚀 Installation & Quickstart

Three paths, depending on where you want to run. [INSTALL.md](INSTALL.md) is the deep-dive reference for all of them.

### Method 1: Automated GCP & GKE Provisioning (Recommended)

A modular, idempotent pipeline that takes you from an empty GCP project to a production-grade deployment:

```bash
cd k8s-operator
make gcp-provision
```

The pipeline runs 11 staged scripts (each re-runnable and supporting `--dry-run`): GKE cluster creation, a gVisor-sandboxed node pool, operator + CRD installation, IAM & Workload Identity, Google Chat Pub/Sub wiring, Slack integration, secrets, the Platform Agent Custom Resource, the LiteLLM gateway, the GitHub token minter, and inference replay. A matching `make gcp-teardown` reverses everything.

### Method 2: Manual Kubernetes Deployment

For existing clusters, deploy the pieces yourself:

1. Install **cert-manager** (required for the operator's admission webhooks).
2. Create the `kubeagents-system` namespace and a `platform-agent-secrets` Secret (API keys for your model providers).
3. Build and deploy the Kubebuilder-powered Go operator from [`k8s-operator/`](k8s-operator/): `make install && make deploy IMG=<your-image>`.
4. Optionally deploy the LiteLLM gateway (`make deploy-litellm`) and GitHub integration (`make deploy-github`).
5. Create your agent: `kubectl apply -f examples/platformagent.yaml` — the operator reconciles the `PlatformAgent` Custom Resource into a fully wired workload.

### Method 3: Local Development

Fast offline iteration against a local [Kind](https://kind.sigs.k8s.io/) cluster (or a remote GKE cluster):

```bash
cd k8s-operator
make install                      # install CRDs
ENABLE_WEBHOOKS=false make run    # run the operator locally, outside the cluster
make dev-rebuild-agent ARGS="platform"   # fast agent-image rebuild loop
```

---

## 🛡️ Governance & Multi-Tenancy

`kube-agents` is designed for enterprise fleets where agents must be powerful _and_ provably contained.

### Isolation by construction

- **Least-privilege RBAC boundaries** — the operator provisions each agent with read-only (`view` + a scoped custom `explorer` ClusterRole) fleet visibility; write access is confined to the agent's own Custom Resources. NetworkPolicies are owned and reconciled by the operator.
- **Credential isolation** — the agent sandbox container _never_ receives API keys or tokens. An Envoy credential-proxy sidecar injects credentials at the network boundary, and the sandbox image ships only non-functional CLI wrappers — the real credential-aware CLIs live in a separate, inaccessible image. See the full design in [docs/credential-isolation-design.md](docs/credential-isolation-design.md).
- **Kernel-level sandboxing** — agent workloads run under a gVisor RuntimeClass (GKE Sandbox), validated by the operator at reconcile time.
- **GitOps-only mutations** — the agent proposes changes as pull requests (via the `submit-suggestion` skill and short-lived GitHub App tokens minted through KMS) for human SRE review; it does not apply mutations directly.

### Continuous security auditing

The [`.agents/skills/`](.agents/skills/) suite gives the harness automated Kubernetes security reviews across: admission control, network policies, Pod security contexts, Gateway API configs, RBAC, service accounts, namespaces, nodes, storage, audit logs — plus agent-threat-model reviews for prompt injection, execution sandbox escape, credential exposure, and data exfiltration.

### Scheduled governance watchdogs

Ten cron-driven jobs in [`agents/platform/cron/jobs.json`](agents/platform/cron/jobs.json) keep the fleet honest without human prompting:

| Watchdog                        | Cadence      | What it does                                             |
| ------------------------------- | ------------ | -------------------------------------------------------- |
| Blueprint Sync                  | Daily        | Checks cluster state against master blueprints           |
| Compliance Audit                | Weekly       | Fleet-wide scan for security/network policy deviations   |
| Policy Propagation              | Hourly       | Verifies latest policies are applied across clusters     |
| Global Capacity Orchestrator    | Hourly       | Cross-cluster capacity optimization                      |
| Fleet-wide Cost Analysis        | Daily        | Aggregates spend, flags savings                          |
| Security Patch Orchestrator     | Daily        | CVE scanning, staggered rolling updates                  |
| Lifecycle / Deprecation Manager | Monthly      | Tracks Kubernetes version deprecations                   |
| Standardization Validator       | Weekly       | Deep-diffs live state vs. global standards               |
| Obtainability Audit             | Daily        | Finds rigid allocations, proposes capacity patches       |
| GitHub Issue Resolver           | Every 30 min | Triages and resolves open issues within authorized scope |

---

## 🏗️ End State Architecture

What a fully provisioned harness looks like, across three layers:

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
        RBAC["RBAC isolation boundaries<br/>+ NetworkPolicies"]
        OP -->|reconciles| CRD
        CRD --> POD
        OP --> RBAC
    end

    subgraph integration["🔀 Integration & Routing Layer"]
        LLM["LiteLLM Gateway<br/>Gemini · OpenAI · Anthropic"]
        CHAT["Messaging bridges<br/>Google Chat (Pub/Sub) · Slack (Socket Mode)"]
        GH["Minty — GitHub App<br/>token minter (KMS)"]
    end

    PA -.runs inside.-> POD
    POD --> LLM
    CHAT <--> POD
    POD -->|PR-based changes| GH
```

**1. Control Plane (Agent Layer)** — the Platform Agent workspace at [`agents/platform/`](agents/platform/): its persona ([`SOUL.md`](agents/platform/SOUL.md)), operational playbooks ([`governance/`](agents/platform/governance/)), skills, and scheduled governance tasks ([`cron/jobs.json`](agents/platform/cron/jobs.json)).

**2. Cluster Plane (Kubernetes Layer)** — the Go-based operator at [`k8s-operator/`](k8s-operator/) reconciles `PlatformAgent` Custom Resources (`kubeagents.x-k8s.io/v1alpha1`) into complete workloads: the sandboxed agent container, an Envoy credential-proxy sidecar, log and cluster-event-watcher sidecars, per-agent ServiceAccounts with Workload Identity, RBAC bindings, PVCs, Services, and NetworkPolicies — with admission webhooks enforcing spec validity.

**3. Integration & Routing Layer** — a [LiteLLM gateway](k8s-operator/config/integrations/litellm/) routes inference between Gemini (default), OpenAI, and Anthropic (see [`examples/`](examples/) for provider configs and local vLLM serving); messaging bridges connect the agent to **Google Chat** (via GCP Pub/Sub) and **Slack** (via Socket Mode); and the **Minty** GitHub token minter brokers short-lived, KMS-backed GitHub App tokens for the agent's PR workflow. Observability flows through OpenTelemetry and Prometheus into Cloud Trace and GKE Managed Prometheus.

---

## 🤝 Contributing

Contributions are welcome! See [docs/contributing.md](docs/contributing.md) for CLA requirements, community guidelines, and the review process. Follow the [PR hygiene guidelines](AGENTS.md#pull-request-hygiene) — Conventional Commits, scoped changes, and the [PR template](.github/PULL_REQUEST_TEMPLATE.md).

---

## Disclaimer

This is not an officially supported Google product.

This project is not eligible for the Google Open Source Software Vulnerability Rewards Program.
