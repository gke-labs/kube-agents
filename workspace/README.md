# Multi-Agent Kubernetes Workspace

This directory contains agent personas, configuration profiles, and cooperative routing policies for deploying specialized Kubernetes (k8s) AI agents. These assets are designed using open, standardized agentic design patterns, allowing them to be easily imported and run in various modern multi-agent orchestrators, Model Context Protocol (MCP) hosts, or gateways (such as CrewAI, LangGraph, or Microsoft AutoGen).

---

## Core Agentic Design Patterns

By separating concerns into discrete role-based agents, this workspace implements three core industry patterns for robust, production-safe operations:

### 1. Role-Based Agent Crews (e.g., CrewAI, AutoGen)
- **Kubernetes Operator Agent (`operator`)**: An autonomous custodian of the infrastructure. It manages global cluster concerns (multi-cluster balancing, capacity scaling, version upgrades, security patching) and executes scheduled operational cron tasks (health patrols, CVE scans, log rotations, backup validation).
- **Development Team Agent (`devteam`)**: A production-safety coach and application workload custodian. It acts as the developers' first-responder, automating manifest validation, PR reviews (enforcing requests/limits and Pod Security Standards), canary rollouts, dependency management, and incident root-cause analysis.

### 2. State-Machine Task Delegation (e.g., LangGraph)
The "main" agent acts as the primary orchestrator and dispatcher. It uses a strict routing guide (`ROUTING.md`) to safely delegate incoming developer requests to the most appropriate specialized subagent:
- **`@devteam <task>`**: Routes development-related work (writing code, manifests, build pipelines, rollouts, application-level bug fixes and debugging).
- **`@operator <task>`**: Routes cluster/platform operations (cluster health, scaling, upgrades, platform policies, cert scans, global security patches).
- **`@main <task>`**: Routes coordination, tradeoffs verification, planning, and human-in-the-loop communication.

#### Strict Proof Gates
Before the coordinator reports success to the human operator, it enforces strict proof validation gates:
- **For Development Tasks**: Requires Git commit SHAs, changed files listing, build/compilation terminal outputs, container digests (`@sha256`), and live deployment status evidence (`kubectl get deploy/pods/svc`).
- **For Operational Tasks**: Requires active context checking (`kubectl config current-context`), resource inspection scope, before/after state comparisons, and event/log evidence.

### 3. Standardized Tool Binding (Model Context Protocol)
These agents consume kubernetes tools via the open **Model Context Protocol (MCP)**. The underlying GKE MCP server (`gke-mcp`) exposes standard APIs for reading cluster states, inspecting logs, and running safe mutations. This makes the tools compatible with any MCP-capable host (such as Claude Desktop, Cursor, or Goose).

---

## Harness Integration & Setup

These configurations can be loaded by pointing any compatible agent orchestrator or gateway to the absolute path of the agent directories in this repository.

### 1. Declarative Registration (YAML/JSON)
For hosts or gateways that load agents declaratively, add the workspace paths to your gateway profile or orchestration configuration:

```yaml
agents:
  - id: operator
    workspace: ./workspace/agents/operator
  - id: devteam
    workspace: ./workspace/agents/devteam
```

### 2. Imperative CLI Registration
For hosts supporting CLI-driven workspace imports, register the agent directories from the repository root. For example (using a generic gateway CLI or reference host):

```bash
# Register operator agent
gateway-cli agents add operator --workspace ./workspace/agents/operator --non-interactive

# Register devteam agent
gateway-cli agents add devteam --workspace ./workspace/agents/devteam --non-interactive
```

---

## Interacting with the Cooperative Layout

Once imported into your orchestrator or gateway client, you can interact with the cooperative agent layout in two primary ways:

### 1. Coordinating Session (Recommended)
Start a chat session with the coordinator agent to leverage the automatic delegation and routing policies in a shared thread. For example, in an interactive chat or TUI session with the coordinator:

```bash
# Example: starting a session with the coordinator/main agent using a CLI or reference TUI
gateway-cli chat --agent main
```

Once inside, you can use routing shortcuts to delegate work transparently:
- `@devteam Implement a new React checkout component in repo X...`
- `@operator Audit the current cluster egress policies...`
- Or simply describe your task and let `main` automatically interpret your intent and route it.

### 2. Direct Subagent Sessions
If you want to bypass the coordinator and open a direct session with a specialized agent, launch a session targeting their specific agent keys:

```bash
# Direct session with the Kubernetes Operator agent
gateway-cli chat --agent operator

# Direct session with the Development Team agent
gateway-cli chat --agent devteam
```

---

## Showcasing the Harness in Action (Demo Scenarios)

Once loaded into your gateway harness, you can test and showcase the advanced capabilities of this Kubernetes agentic layout using these structured chat scenarios in a shared session.

### 1. Infrastructure Scope Binding (Cluster Operator Agent)
*   **Scenario**: Tell the active `operator` agent to bind its operational scope to a target cluster and region.
*   **Chat Command**:
    ```bash
    @operator Use cluster 'payment-prod' in 'us-central1'.
    ```
*   **Expected Behavior**: The `operator` agent will parse the request, locate the `payment-prod` context in `us-central1`, configure its internal `kubectl` credentials, and respond with confirmation of its new active target scope.

### 2. Workload Onboarding & Deployment (Development Team Agent)
*   **Scenario**: Instruct the active `devteam` agent to deploy a specific application to a target namespace.
*   **Chat Command**:
    ```bash
    @devteam Deploy the payment-gateway application to the 'payment' namespace.
    ```
*   **Expected Behavior**: The `devteam` agent will generate the necessary deployment and service manifests, verify them against Pod Security Standards, execute the rollout in the `payment` namespace, and return proof (such as Git SHAs, container digests, and successful rollout status).

### 3. GitOps Manifest Resiliency & Autonomy (Development Team Agent)
*   **Scenario**: Ask the `devteam` agent to configure node-failure resiliency for a deployment. The agent must autonomously determine and apply the correct Kubernetes strategy (such as configuring `PodAntiAffinity` or pod topology spread constraints) without being given the solution.
*   **Chat Command**:
    ```bash
    @devteam Make my payment-gateway deployment tolerant to node failures.
    ```
*   **Expected Behavior**: The `devteam` agent will audit the deployment manifest, identify that all replicas could fail if a single node goes down, autonomously inject a `podAntiAffinity` block into the manifest, apply the change via GitOps, and explain the rationale for its fix.

### 4. Proactive Audit & Auto-Remediation (Cluster Operator Agent)
*   **Scenario**: Manually trigger a diagnostic patrol to show how the agent identifies and automatically remediates infrastructure failures (e.g., simulating a hung worker node `kubelet` that causes `NodeNotReady`).
*   **Chat Command**:
    ```bash
    @operator Perform a cluster health check.
    ```
*   **Expected Behavior**: The `operator` agent will scan all nodes, identify a node in `NotReady` status, execute its remediation playbook to automatically restart the hung `kubelet` process via SSH, verify the node returns to `Ready`, and post a clean incident timeline and trace log summary to the chat.

### 5. Cross-Agent Handoff & Resource Negotiation (Visible Collaboration)
*   **Scenario**: Showcase how the `operator` and `devteam` agents collaborate, negotiate resource optimizations, and relay requests transparently.
*   **Trigger Command**:
    ```bash
    @operator Analyze resource utilization and suggest optimizations.
    ```
*   **Expected Cooperative Flow**:
    1.  `operator` audits metrics, identifies potential savings by bin-packing, and sends a direct request: `@devteam Proposing a 30% reduction in on-demand nodes for payment-gateway to optimize resource spending. Do you approve?`
    2.  The `main` agent automatically intercepts and **relays** this proposal to the `devteam` session.
    3.  `devteam` audits its historical latency profiles and SLO metrics, determines that CPU throttling would cause unacceptable cold-start degradation, and replies: `@operator Rejecting proposal. Historical performance telemetry shows CPU throttling causes severe cold-start latency degradation, violating our payment-gateway SLO.`
    4.  `main` automatically **relays** the rejection back to `operator` and **mirrors the entire negotiation chat thread** to your active chat screen, allowing the human operator to transparently review the collaborative cost-vs-performance decision.

---

## References

- [Model Context Protocol (MCP) Specifications](https://modelcontextprotocol.io/)
- [CrewAI Multi-Agent Framework](https://www.crewai.com/)
- [Microsoft AutoGen multi-agent orchestration](https://microsoft.github.io/autogen/)
- [LangGraph Multi-Agent Workflows](https://www.langchain.com/langgraph)
