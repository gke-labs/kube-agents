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

- **Definition:** A cooperative multi-agent system designed to replace traditional Kubernetes/GKE interfaces (e.g., `kubectl`, `gcloud`, Google Cloud Console) with intelligent, intent-driven autonomous agents.

---

## Agents in `kube-agents`

### Platform Agent (`platform`)

- **Role:** Architectural custodian and agent orchestrator.
- **Scope:** Configured with an architectural persona (`SOUL.md`). It manages multi-tenancy boundaries, fleet-wide governance, and RBAC isolation. It is responsible for dynamically provisioning and configuring specialized subagents (`operator` and `devteam`) at runtime based on the required operational scopes.

### Kubernetes Operator Agent (`operator`)

- **Role:** Infrastructure custodian and operations manager.
- **Scope:** Configured with a calm, analytical persona (`SOUL.md`). It handles global concerns like multi-cluster balancing, node capacity, cluster upgrades, and platform security policy enforcement. It also executes scheduled background tasks (e.g., health patrols, CVE scans, and log management).

### Development Team Agent (`devteam`)

- **Role:** Developer coach and workload custodian.
- **Scope:** Configured with a performance-driven persona (`SOUL.md`). It represents the interests of application developers, enforcing manifest validation, resource allocation templates, and automatic NetworkPolicy generation. It manages development-specific loops like rollout monitoring and SLO checks.
