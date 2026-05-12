# Scion Role Templates

Reusable Scion agent templates that compose into a **GKE Platform Team**. Each template defines a single role: a system prompt (persona), an `agents.md` (status boilerplate + behavior), a curated subset of GKE skills (symlinks into the top-level `../skills/` tree), and the MCP server wiring appropriate to that role's blast radius.

These templates are intentionally **pure roles** — no cluster IDs, no namespace names, no demo-specific narrative. A demo project under `../demos/<name>/` selects the templates it needs and supplies the cluster/workload context via env vars (in `.scion/settings.json`) and a workspace-seed `MEMORY.md`.

## Roles

| Template | MCP wiring | Skills | Blast radius |
|---|---|---|---|
| `platform-coordinator` | none — pure router | none | tiny: never touches GKE |
| `upgrade-coordinator` | local gke-mcp HTTP (host:9080) | gke-cluster-lifecycle, gke-reliability | medium: cluster + node-pool upgrades, HITL gate before writes |
| `dev-workload-guardian` | local gke-mcp HTTP (host:9080) + remote read-only (host:8082) | gke-observability, gke-reliability, gke-workload-security | tiny: read-only by skill discipline |
| `node-pool-provisioner` | remote MCP full (host:8081) | gke-cluster-creator, gke-workload-scaling | high: HITL strict, never autonomous |
| `cost-optimizer` | local gke-mcp HTTP (host:9080) + remote read-only (host:8082) | gke-cost-analysis, gke-cost-optimization, gke-compute-class-creator | tiny: read-only |
| `workload-deployer` | local gke-mcp HTTP (host:9080) + remote MCP full (host:8081) | gke-app-onboarding, gke-productionize, gke-workload-scaling | medium: HITL gate before apply |

## How a demo composes these

Each demo's `bootstrap.sh` initializes a Scion project (`scion init`), symlinks the chosen templates into `.scion/templates/`, writes `.scion/settings.json` with cluster identity env vars, and seeds a `MEMORY.md` in the workspace. The demo then starts the `platform-coordinator` with an opening prompt; the coordinator spawns the specialists it needs.

## Parameterization seams

Templates declare `env:` blocks with these keys (values are filled in by the demo composition's `.scion/settings.json`):

- `GKE_PROJECT` — GCP project ID
- `GKE_LOCATION` — region or zone
- `GKE_CLUSTER` — cluster name
- `GKE_NAMESPACES_IN_SCOPE` — comma-separated allow-list

Anything else specific to a scenario lives in the demo's `workspace-seed/MEMORY.md` and the coordinator's opening prompt.

## Convention: workers do not message each other directly

Following Scion's `team-creation` convention: only `platform-coordinator` spawns specialists and only the coordinator routes messages between them. Workers report back to the coordinator; the coordinator relays. This keeps the topology readable as the platform team grows.
