# Glossary of Agentic Terms

This glossary defines key terms and concepts related to the Kubernetes Agentic Harness (`kube-agents`) and the broader agentic ecosystem.

---

## Agent Platforms for Kubernetes

### Agent Substrate

- **Source:** [agent-substrate/substrate](https://github.com/agent-substrate/substrate)
- **Definition:** An open-source, Kubernetes-native platform specifically engineered to orchestrate, scale, and manage AI agent workloads. It introduces abstractions like Workers (managed compute pools in Kubernetes Pods) and Actors (individual agent instances running inside Pods) to facilitate high-efficiency multiplexing and stateful execution sandboxes.

### Agent Sandbox

- **Source:** [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
- **Definition:** An open-source Kubernetes SIG Apps project designed to manage isolated, stateful, singleton workloads. It provides low-latency warm pod pools, stable identity, persistence, and secure sandboxed execution environments (e.g., via gVisor or Kata Containers) suitable for running untrusted LLM-generated code.

---

## Agent Runtimes & Frameworks

### Agent Executor (AX)

- **Source:** [google/ax](https://github.com/google/ax)
- **Definition:** An open-source distributed agent runtime designed to manage the execution lifecycle of AI agents. It provides durable execution capabilities (including pausing, resuming, snapshotting, and replaying agent states) to ensure agent workloads remain operational and recover automatically from transient infrastructure failures.

### Kubernetes Agentic Harness (`kube-agents`)

- **Definition:** An agentic system designed to replace traditional Kubernetes/GKE interfaces (e.g., `kubectl`, `gcloud`, Google Cloud Console) with intelligent, intent-driven autonomous platform agents.

---

## Agents in `kube-agents`

### Planning Agent (`agents/chat/`, the `default` profile)

- **Role:** The single conversational front door to the harness, and the delegator/router.
- **Scope:** The `default` [Hermes Profile](#hermes-profile) — the only profile that receives chat ingress. It analyzes each message, reads which specialist agents exist and what each is responsible for off the roster injected into every turn (the `router` MCP tool `list_agents` re-reads the same list on demand), and delegates the request to the right specialist over the asynchronous kanban board (`kanban_create`). It holds **no** infrastructure tools of its own (no GKE, provisioning, or GitOps write path) — the front door can route, not mutate. Unlike the specialists, it is **exempt** from the pointer-only [Work Item](#work-item-shared-state) rule in the **outbound** direction: it passes full context to specialists in the kanban task `body`. It does **not** carry answers back — the gateway posts a completed card's `result` into the thread verbatim without waking the Planning Agent, which must not relay, re-post, or summarize one (the Planning Agent's `SOUL.md` §2 step 5). It is woken only when a card blocks or fails.

### Platform Agent (`platform`)

- **Role:** Architectural custodian and fleet orchestrator; the privileged doer behind the Planning Agent.
- **Scope:** A named [Hermes Profile](#hermes-profile) (`platform`) scaffolded at pod startup from the `agents/platform/` template. Configured with an architectural persona (`SOUL.md`), it manages multi-tenancy boundaries, fleet-wide governance, and RBAC isolation, and owns the GitOps write path. It no longer receives chat directly — the Planning Agent routes work to it — and it delegates single-cluster runtime debugging to Cluster Agents (pointer-only). It runs in the operator-deployed gateway pod and shares that pod's identity.

### Cluster Agent (`agents/cluster/`)

- **Role:** Single-cluster SRE operator for read-only runtime operations and workload root-cause analysis.
- **Scope:** A per-cluster [Hermes Profile](#hermes-profile) that the Platform Agent creates dynamically inside its own pod (one per managed GKE cluster, persistent until the cluster is deleted). It is scoped to one cluster by persona, toolset, and a pinned `KUBECONFIG`, and shares the Platform Agent pod's identity. It is strictly read-only: it returns an RCA and any proposed manifest patch to the Platform Agent rather than mutating the cluster or opening Pull Requests. It is not represented by the operator or a CRD.

---

## Hermes Runtime Concepts

### Hermes Profile

- **Definition:** A native Hermes feature (`hermes profile` / `hermes -p <name>`) that provides multiple isolated Hermes instances, each with its own config, sessions, skills, and home directory. Multiple profiles run concurrently within a single gateway process/pod. In `kube-agents`, the `default` profile is the [Planning Agent](#planning-agent-agentschat-the-default-profile) (front door), the `platform` profile is the [Platform Agent](#platform-agent-platform) (scaffolded at startup from `agents/platform/`), and each [Cluster Agent](#cluster-agent-agentscluster) is a profile scaffolded at runtime from `agents/cluster/`. Executable scripts are shared across profiles at `$HERMES_HOME/scripts`; persona, config, and skills are per-profile.

---

## Coordination

### Kanban Task (Delegation)

- **Definition:** The unit of task coordination between personas. Personas never pass task context or results directly to one another; they exchange a **kanban task (card)** on the shared board (`<HERMES_HOME-root>/kanban.db`). The Platform Agent (orchestrator) creates a card with `kanban_create(assignee="<cluster-profile>", body=...)`; the gateway's kanban **dispatcher** auto-spawns the assigned Cluster Agent as a worker (`hermes -p <cluster> chat -q "work kanban task <id>"`), which reads the card with `kanban_show`, does read-only work, and reports via `kanban_complete(result=..., summary=..., metadata={...})` — `result` carries the answer and is delivered to the requester verbatim. For fan-out, the creating worker keeps its own card open, polls the children with `kanban_show`, and completes with the synthesized answer — completing a card is the delivery, so the image refuses a completion over unfinished fan-out children (#1010); parent/child links (`parents`) queue a card to run only after the listed cards settle. Completions are pushed back to the originating chat (auto-subscribe). This keeps invocation to a pointer, makes coordination auditable, and gives claim/lease safety.
- **Exception — the Planning Agent:** The [Planning Agent](#planning-agent-agentschat-the-default-profile) is deliberately exempt from the pointer-only rule, but only on the way **out**: it passes full context to a specialist in the `kanban_create` task `body`. The return path is not its to carry — completions are delivered straight into the thread by the gateway and it is not woken for them (the Planning Agent's `AGENTS.md`: "Completions are not yours to repeat"). The pointer-only rule still governs all specialist-to-specialist coordination.
- **Note:** This replaced the earlier bespoke `worklog.py` shared-file store.
