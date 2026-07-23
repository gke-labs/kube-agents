# Design 01: Vision & Scope

**Status:** ✅ Agreed

**Overview:** [README.md](README.md)

---

## TL;DR

`kube-agents` aims to make **intelligent, autonomous agents the primary presentation layer for
Kubernetes operations** — the long-term north star is that humans express _intent_ and agents
carry out fleet management, tenancy, and troubleshooting, so that direct use of `kubectl`,
`gcloud`, and the cloud console becomes the exception rather than the rule.

It serves **three layered audiences**, mapped onto the project → cluster → namespace containment
hierarchy: **platform teams** who own a project, **cluster administrators** who own a cluster, and
**developer teams** who operate within a namespace. **SRE is not a fourth agent** — it is a class
of critical user journeys (reliability, incident response, capacity, observability) that spans all
three personas, segmented by each persona's scope (see §3). The system is architected to be
**cloud-agnostic Kubernetes** in concept, with **GKE as the first fully supported target**.

---

## 1. The problem

The traditional Kubernetes presentation layer is static, imperative, and fragmented across
`kubectl`, `gcloud` and other cloud CLIs, and web consoles. This forces humans to:

- translate high-level intent ("make this tenant compliant", "this workload is unhealthy — fix it")
  into long sequences of low-level, tool-specific commands;
- react manually to drift, version skew, and policy violations that a system could detect and
  remediate proactively; and
- carry undocumented operational knowledge that doesn't scale across a fleet or a team.

The result is reactive, error-prone, expertise-gated operations.

## 2. The vision (north star)

Replace that presentation layer with **autonomous, intent-driven agents**. In the target state:

- Humans interact with the fleet primarily through natural-language **intent** via an agent (e.g. the
  Platform Agent chat entrypoint). Kubernetes is already largely declarative/GitOps; what agents remove
  is the **human in the middle of the cognitive loop** — noticing the alert, root-causing it, designing
  the fix, hand-writing the YAML, and opening the PR. Imperative `kubectl`/`gcloud` stay, but for
  **reading and debugging**; **all writes flow through the GitOps loop.**
- Agents **proactively** surface and remediate fleet-level issues (tenancy erosion, version skew,
  security-baseline drift, IaC drift) rather than waiting to be asked.
- Every mutation flows through a **declarative, reviewable workflow** — agents propose, humans (or
  policy) approve, the system reconciles (see [04-workflow-model.md](04-workflow-model.md)).
- There is **no direct-access escape hatch and no break-glass** — even exceptional changes go through
  the GitOps loop. Break-glass is **deliberately omitted for simplicity** and is **not planned work**
  (unlike the deferred-hardening items, which _are_ on the roadmap). It stays revisitable: if a hard
  operational need ever proves it necessary, it would be added only as a **designed, reviewed, audited**
  mechanism — never an ad-hoc escape hatch.
- Agents are **read-only and reachable only by trusted humans** — an agent's ceiling is its read-only,
  tier-scoped identity, so no one can use it to mutate or to read outside its tier (see
  [03-security-model.md](03-security-model.md) §4a). _(Per-user down-scoping — the delegate model — is
  deferred hardening.)_

This is a **full-replacement** ambition, reached by staging: agents augment humans first, and
assume more of the presentation layer as trust, safety, and coverage grow.

## 3. Who it's for (tri-layered audiences & agents)

The audience model is three layers, each served by a dedicated agent persona whose scope maps onto
a level of the Kubernetes containment hierarchy (project → cluster → namespace):

| Layer                | Agent persona            | Cardinality     | User                   | Scope of action                                                                                                                                            |
| -------------------- | ------------------------ | --------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Project / fleet**  | **Platform Agent**       | 1 per project   | Platform teams         | Fleet lifecycle, cluster provisioning, cross-cluster governance, global RBAC & policy, cost/capacity, compliance audits.                                   |
| **Cluster**          | **Cluster Admin Agent**  | 1 per cluster   | Cluster administrators | Cluster-level operations: node pools, cluster add-ons, namespace/tenant provisioning within the cluster, cluster-scoped policy and quotas.                 |
| **Namespace / team** | **Developer Team Agent** | 1 per namespace | Developer teams        | Self-service within a single namespace: workload onboarding, scaling, troubleshooting, observability — constrained by the boundaries the layers above set. |

**SRE is a cross-cutting concern, not a persona.** Reliability work — incident response, capacity
planning, observability, rollout safety — appears as critical user journeys at every layer, scoped
to that layer's authority: the Platform Agent handles fleet-wide reliability and cross-cluster
capacity; the Cluster Admin Agent handles cluster health, node pools, and cluster-scoped rollouts;
the Developer Team Agent handles workload-level troubleshooting and scaling within its namespace.
The same SRE CUJ is served by whichever persona owns the scope it applies to.

The three layers are related by **strict containment**, mirroring their resource scope:

- The **Platform Agent** operates at the project level and defines/oversees the clusters within it.
- The **Cluster Admin Agent** operates within one cluster and defines/oversees the namespaces
  within it — bounded by project-level policy from the Platform Agent.
- The **Developer Team Agent** operates within one namespace and cannot cross it — bounded by the
  cluster- and project-level guardrails above it.

Each layer _defines and constrains_ the layer beneath it and _operates within_ the constraints of
the layer above it. No agent can act outside its scope. The concrete roles, boundaries, and
relationships of these three personas are specified in
[02-agent-personas.md](02-agent-personas.md); how their boundaries are enforced is in
[03-security-model.md](03-security-model.md).

## 4. Platform reach: cloud-agnostic, GKE-first

**Intent:** the core concepts — agent personas, declarative-only mutation, tenancy isolation,
skill-based capability, the agent-orchestration/runtime model — are **Kubernetes-generic** and must not assume a
specific cloud.

**Reality:** **GKE/GCP is the first and only fully supported target today**, and much of the
implementation is deliberately GKE-optimized (Managed Prometheus/OTel, Workload Identity,
GKE-specific skills and console links). Agents run as **Hermes**-harness pods reconciled by the
**kube-agents controller** (the extended `k8s-operator/`), built on **Scion**'s verified per-pod runtime
model ([08](08-agent-runtime-and-identity.md)). Actuation is deliberately **unopinionated** — the agent
emits KCC YAML or Terraform HCL and the customer's CI/CD applies it (§6, [04](04-workflow-model.md)
§1.1).
Portability is a design constraint, not a current feature. See the delta and its implications in §6.

## 5. Goals & non-goals

### Goals

- Establish intent-driven, agent-mediated operations as the primary interface to a K8s fleet.
- Serve platform, cluster-admin, and developer-team users as three distinct, layered personas
  (1 per project / 1 per cluster / 1 per namespace) with enforced containment boundaries.
- Keep all infrastructure mutation declarative, reviewable, and auditable.
- Make proactive detection and remediation of fleet drift a first-class behavior.
- Keep core concepts cloud-agnostic even while GKE is the first supported target.

### Non-goals

- Removing human control. Humans retain approval authority over every mutation; "full replacement"
  is about the _default_ interface, not about eliminating oversight.
- Being a general-purpose chatbot. The scope is Kubernetes/fleet operations.
- Immediate multi-cloud support. Cloud-agnosticism is an architectural constraint now and a
  supported feature later — not a claim about today's runtime.
- Bypassing established GitOps/IaC workflows with ad-hoc mutations (explicitly forbidden by
  `SOUL.md`).

## 6. Known delta: cloud-agnostic intent vs. GKE-coupled implementation

Per the "docs lead, code follows" principle, we record this gap rather than hide it.

| Area                   | End-state intent                                                      | Current reality                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Agent write access     | **Read-only agents**; all mutation actuated by the customer's CI/CD   | Agent **K8s RBAC is already read-only** — the operator runtime-mints a `view` binding + a `get/list` "explorer" ClusterRole (`buildPlatformExplorerRole`, `k8s-operator/internal/controller/platformagent_manifests.go`). The live write path is the remote `gke` MCP's `create_cluster` — a **cloud** write via the cloud SA's IAM, **not** K8s RBAC — plus dead `kubectl` helpers. Deltas: pre-create the RBAC (stop runtime-minting), drop `create_cluster`, scope the cloud SA to viewer-only |
| Actuation              | **Customer CI/CD** (GitHub Actions / CircleCI / …) — unopinionated    | Configured **externally** to this project (the customer's existing CI/CD applies merged artifacts); lives outside the kube-agents repo, so agents integrate with it rather than bundle it                                                                                                                                                                                                                                                                                                         |
| Provisioning artifact  | **KCC YAML or Terraform HCL** (per customer), applied by the pipeline | The agent writes `containerclusters` **directly** to the API; Terraform only in `k8s-operator/testing/`                                                                                                                                                                                                                                                                                                                                                                                           |
| Observability          | Pluggable OTel/metrics backend                                        | GKE Managed Prometheus + Cloud Trace/Logging, templated console URLs (interpolating `{project_id}`) in `SOUL.md §6`                                                                                                                                                                                                                                                                                                                                                                               |
| Identity               | Generic (read-only) workload identity                                 | GKE Workload Identity + GCP IAM                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Skills                 | Portable capability model                                             | Many `gke-*` skills are GCP-specific                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Console/CLI references | Abstracted                                                            | `gcloud`/GCP console links throughout                                                                                                                                                                                                                                                                                                                                                                                                                                                             |

**Implication:** achieving the stated vision means, over time, factoring GKE specifics behind
provider-neutral seams (observability backend, identity, IaC artifact format, provider skills). This
is direction, not a committed milestone; it should inform how new work is structured so we don't
deepen the coupling unnecessarily.

The largest single delta is the **read-only agent** move. The K8s RBAC is _already_ read-only; what
still gives agents a live write path is a **direct-mutation tool** — the remote `gke` MCP's
`create_cluster`, a **cloud** write via the cloud SA's IAM (`SOUL.md §4`'s declarative playbook already
forbids direct cluster mutation, so this is a tool/IAM gap, not a persona-doc grant). The end state
removes direct mutation entirely — agents become read-only, emit **KCC YAML or Terraform HCL**, and all
changes are actuated by the customer's CI/CD pipeline (see [04-workflow-model.md](04-workflow-model.md)
§1.1). Target artifacts to
update when this lands: `SOUL.md`; the operator's **`renderConfigYAML()`**
(`k8s-operator/internal/controller/platformagent_manifests.go`) — the **runtime-authoritative** config,
rendered into a ConfigMap mounted read-only over `/opt/data/config.yaml` — to drop or read-only-limit the
write-capable remote `gke` MCP that serves `create_cluster` and its `platform_toolsets` entry (the baked
configs `agents/platform/config.yaml` and `deploy/shared/defaults/config.yaml` are **shadowed at
runtime**, so editing only them leaves the deployed agent write-capable); the `agents/platform/skills/gke-cluster-creator` skill (retire its `create_cluster`
call); and `agents/platform/scripts/platform_mcp_server.py` (remove the unused `apply_manifest` /
`delete_cluster_manifest` `kubectl` helpers). _Note:_ `create_cluster` is a tool of the **remote `gke`
MCP** (`container.googleapis.com`), **not** a `platform_mcp_server.py` function.

## 7. Success criteria (how we'll know it's working)

- A platform operator can perform a representative fleet task (e.g. provision a cluster or onboard
  a tenant with correct isolation) end-to-end through the Platform Agent, with no manual
  `kubectl`/console steps, and a reviewable change trail.
- A cluster administrator can provision and configure a namespace within their cluster through the
  Cluster Admin Agent, within the guardrails set by the Platform Agent.
- A developer team can self-serve a scoped task within their namespace via their Developer Team
  Agent and is provably unable to affect another namespace or escalate beyond it.
- The Platform Agent detects and proposes a fix for an injected drift (RBAC/NetworkPolicy/version)
  without being prompted.
- Every agent-driven mutation is attributable and auditable (see
  `docs/designs/audit-logging-user-attribution.md`).

_Two v1 SLIs, measured continuously from the audit log ([05](05-system-architecture.md) §5,
`docs/designs/audit-logging-user-attribution.md`): **zero direct (non-GitOps) mutations** — alert on any
cluster/cloud write whose actor is an agent identity — and **zero cross-scope isolation escapes** — alert
on any agent read or `SubjectAccessReview`-allow outside its tier scope. The rest are qualitative
per-phase acceptance ([07](07-implementation-roadmap.md) §2)._

## 8. Verification

The §7 success criteria are the top-level acceptance. Each is made concrete and machine-checkable in
the relevant spec's **Verification** section (02 §10, 03 §11, 04 §9, 05 §8, 06 §10, 08 §7) and the
per-phase acceptance + **verification loop** in [07-implementation-roadmap.md](07-implementation-roadmap.md)
§2/§5. A build is "working" only when all of those checks pass.
