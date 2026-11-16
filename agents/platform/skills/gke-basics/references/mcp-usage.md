# GKE MCP Server Usage

The GKE MCP server provides structured read tools for clusters, node pools,
Kubernetes resources, and diagnostics — without requiring shell access or
kubeconfig setup.

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## Connecting to the GKE MCP Server

The GKE remote MCP server is available for AI clients that support the Model
Context Protocol. For setup instructions, see
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/use-gke-mcp.md.txt

## Available Tools

All tools use hierarchical resource paths:

```
Project+Region:  projects/{PROJECT}/locations/{REGION}
Cluster:         projects/{PROJECT}/locations/{REGION}/clusters/{CLUSTER}
Node Pool:       projects/{PROJECT}/locations/{REGION}/clusters/{CLUSTER}/nodePools/{POOL}
Operation:       projects/{PROJECT}/locations/{REGION}/operations/{OP_ID}
```

Use `locations/-` to match all regions when listing.

### Cluster Management

| Tool            | Mode | Purpose                                                 |
| --------------- | ---- | ------------------------------------------------------- |
| `list_clusters` | READ | Discover clusters in a project/region                   |
| `get_cluster`   | READ | Inspect cluster config. Use `readMask` to select fields |

### Node Pool Management

| Tool              | Mode | Purpose                 |
| ----------------- | ---- | ----------------------- |
| `list_node_pools` | READ | List pools in a cluster |
| `get_node_pool`   | READ | Get pool details        |

### Kubernetes Resources

| Tool                     | Mode | Purpose                                                    |
| ------------------------ | ---- | ---------------------------------------------------------- |
| `get_k8s_resource`       | READ | List/get any K8s resource (supports label/field selectors) |
| `describe_k8s_resource`  | READ | Detailed info with events and conditions                   |
| `list_k8s_api_resources` | READ | Discover available resource types                          |

### Diagnostics & Observability

| Tool                     | Mode | Purpose                                               |
| ------------------------ | ---- | ----------------------------------------------------- |
| `list_k8s_events`        | READ | Scheduling failures, OOM kills, evictions             |
| `get_k8s_logs`           | READ | Container logs (supports `tail`, `since`, `previous`) |
| `get_k8s_cluster_info`   | READ | Control plane and service endpoints                   |
| `get_k8s_version`        | READ | Kubernetes server version                             |
| `get_k8s_rollout_status` | READ | Deployment/StatefulSet rollout progress               |
| `check_k8s_auth`         | READ | Verify RBAC permissions for a user/SA                 |

### Operations

| Tool              | Mode | Purpose                            |
| ----------------- | ---- | ---------------------------------- |
| `list_operations` | READ | Pending/running cluster operations |
| `get_operation`   | READ | Track create/upgrade progress      |

## Developer Knowledge MCP Server

The Developer Knowledge MCP server (`developer_knowledge`, proxied to `https://developerknowledge.googleapis.com/mcp`) provides authoritative, first-party documentation, API resource schemas, version lifecycle details, and recommended architecture patterns for Google Cloud and GKE.

### Purpose & Capabilities

- **GKE Facts & Architecture:** Authoritative feature behavior, Autopilot vs Standard constraints, configuration semantics, and security best practices.
- **API Schemas & Fields:** Accurate resource definitions and field specifications across GKE and Kubernetes API versions.
- **Version Lifecycle & Deprecations:** Release notes, deprecation schedules, and breaking change timelines.
- **Quotas & Limits:** Built-in resource limits and quota requirements.

### Available Tools

| Tool               | Mode | Arguments        | Purpose                                                                                                                                                                                       |
| ------------------ | ---- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `search_documents` | READ | `query` (string) | Every lookup starts here: documentation chunks and document names. Takes only `query` (do NOT pass `max_results`).                                                                            |
| `get_documents`    | READ | `names` (array)  | Full document content by resource name (e.g. `["documents/docs.cloud.../..."]`), when a chunk needs its surrounding page.                                                                     |
| `answer_query`     | READ | `query` (string) | Do not use. Its quota is 50 requests per day per project, shared by every agent in the install, and it reads the same corpus as `search_documents`. Never retry its `429 RESOURCE_EXHAUSTED`. |

## Tool Preference

Tool usage follows two distinct, domain-specific hierarchies:

### 1. Knowledge & Documentation Lookups (GKE Facts, Schemas, Best Practices)

```
1. Developer Knowledge MCP (`mcp-developer_knowledge`)  (preferred — authoritative, curated first-party docs)
   `search_documents(query=...)` first, `get_documents(names=[...])` for a full page; never `answer_query`
2. Web Search (`web_search`)                           (fallback — third-party tools, external CVEs, or when DK returns no match)
```

### 2. Live Cluster Operations & State Management

```
1. GKE MCP Tools (`mcp-gke`)  (preferred — structured, auditable, no shell required)
2. gcloud CLI                 (fallback — when MCP doesn't expose the operation)
3. kubectl                    (fallback — purely in-cluster ops not covered by MCP)
```

See [cli-reference.md](./cli-reference.md) for the full coverage comparison, CLI fallback commands, and user preference override options.
