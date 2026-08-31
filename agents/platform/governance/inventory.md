# First-Time Environment Discovery & Inventory Scan (`bootstrap-inventory-scan`)

**Purpose:** Executes the background GKE environment discovery, topology inspection, and SRE workload audit on initial agent boot, generating the unified `/opt/data/INVENTORY.raw.md` file.

That file is the **complete** findings set, and it is not what the user receives. A separate
prioritization stage (`inventory_prioritize_sop.md`) ranks it down to the short report delivered to
chat as `/opt/data/INVENTORY.md`. Your job is to be thorough; being brief is the next stage's job.

---

## Pre-Execution Check

1. **Verify Status:** Check directly via terminal command (`test -e /opt/data/INVENTORY.raw.md`) or directly inspect exact absolute file paths using `read_file` on `/opt/data/INVENTORY.raw.md`. **Do not run relative directory search patterns (`search_files`) since your active working directory (`cwd`) resides inside a subfolder where `/opt/data/` markers won't be listed.**
   - If `/opt/data/INVENTORY.md` is already built on disk, the whole flow has run: return strictly `[SILENT]` immediately and do nothing.
   - If `/opt/data/INVENTORY.raw.md` exists but `/opt/data/INVENTORY.md` does not, the sweep already finished and the handoff is what did not: **skip discovery entirely and go straight to Step 5** to file the prioritization card. Do not re-scan the fleet, and do not write the report yourself.
   - If both are confirmed absent, proceed through the systematic technical discovery process below.

---

## Step 1: Environment Landscape & Fleet Discovery

Use native Google Cloud CLI (`gcloud`) and Kubernetes (`kubectl`) read-only commands to systematically map the project landscape:

1. **Identify GCP Project & Fleet Bounds:**
   - Run `gcloud config get-value project` and `gcloud container clusters list --project=<project-id>` to enumerate every active and stopped GKE cluster in the project.
2. **Inspect Cluster Control Planes & Topologies:**
   - For every running GKE cluster discovered (`e.g., kage-mgmt, platform-agent-host`), inspect its configuration: Kubernetes version, control plane region/zone, node pools (`machine types, node counts, autoscaling boundaries`), network configuration (`VPC-native, Dataplane V2 / eBPF`), and enabled GKE features (`Workload Identity, Managed Prometheus, OpenTelemetry collection`).
3. **Verify Access & Tenancy Boundaries:**
   - Audit your own ServiceAccount permissions (`kubectl auth can-i --list`) across each cluster to verify your read-only fleet visibility vs specific elevated write access on agent-specific Custom Resources (CRDs).

---

## Step 2: Workload & Service SRE Audit

For each running cluster discovered in Step 1, perform an SRE production-readiness audit across all namespaces and active workloads:

1. **Multi-Tenancy & Governance Audit:** List all non-system namespaces (`kubectl get ns`). Verify if ResourceQuotas, LimitRanges, and NetworkPolicies are configured to enforce boundary defense.
2. **Workload Health & QoS Inspection:**
   - List all Deployments, StatefulSets, DaemonSets, and Jobs across all namespaces (`kubectl get deployments,statefulsets,daemonsets -A`).
   - **Probes Check:** Verify that every service has `livenessProbe`, `readinessProbe`, and `startupProbe` configured.
   - **Resource Management Check:** Verify that containers define explicit `requests` and `limits` (check Quality of Service class: `Guaranteed`, `Burstable`, or `BestEffort`).
   - **Scaling Check:** Audit Horizontal Pod Autoscaler (HPA) settings (`minReplicas, maxReplicas, metrics targets`).
   - **Security Context Check:** Verify if workloads run as non-root (`runAsNonRoot: true`) and use read-only root filesystems (`readOnlyRootFilesystem: true`).
3. **Core Infrastructure Addons:** Check for ingress controllers (`GKE Gateway API, NGINX`), cert-manager deployments, OpenTelemetry collectors (`gke-managed-otel`), and identity integration endpoints (`such as github-token-minter / minty`).

---

## Step 3: Proactive GKE Infrastructure Improvement Analysis

Based on your discovery and engineering best practices (`use the developer_knowledge tool to query for up-to-date Google Cloud and GKE best practices when appropriate`), proactively evaluate gaps against modern GKE patterns:

### 1. Observability & Telemetry (`OpenTelemetry & Managed Prometheus`)

- Check if an OpenTelemetry collector is deployed and actively receiving workload traces/metrics. `gke-managed-otel` is the default, but a self-hosted collector (commonly `otel-collector.otel-collector`) is equally valid — read `.status.telemetry` on the PlatformAgent to see which one the agents are actually exporting to, rather than inferring from the namespace list. Note a high-priority recommendation to enable OTel collection (`OTLP / Telemetry API`) only if no collector is reachable at all.
- Check if Google Cloud `Managed Service for Prometheus` (`gmp-system` / PodMonitoring CRDs) is enabled to eliminate manual Prometheus scraping overhead.

### 2. Alerting Hygiene & SLO Definition

- Evaluate whether alerting relies on Service Level Objectives (`SLOs`) and error budget burn rates rather than noisy, transient infrastructure thresholds (`such as CPU usage`).
- Identify missing standard SRE health alerts: `Pod CrashLoopBackOff / OOMKilled events`, `Control Plane API latency spikes`, `PersistentVolumeClaim exhaustion`, and `Workload probe failures`.

### 3. GKE Security Hardening & Workload Identity

- Verify whether pods accessing Google Cloud APIs (`e.g., Cloud KMS, Cloud Storage, BigQuery`) use **GKE Workload Identity** (`serviceAccountName` with `iam.gke.io/gcp-service-account` annotation) rather than static service accounts or JSON key files.
- For Standard mode clusters, evaluate adherence to baseline hardening: **Shielded GKE Nodes**, **Dataplane V2 (`eBPF`)**, **Node Auto-Upgrades**, and **Pod Security Admission (`PSA`)**.

---

## Step 4: Compile Raw Inventory (`/opt/data/INVENTORY.raw.md`)

Write the unified file `/opt/data/INVENTORY.raw.md`. **This is the complete findings set, and it is the only record of what the sweep saw — the prioritization stage reads this file and nothing else, so anything you omit here is invisible for the rest of onboarding.** Write in clean Markdown. Do not leave placeholders, "TODO", or truncated tables; fill in every value you discovered (use `n/a` only when a value genuinely does not apply).

Length is not a concern here and completeness is. This file is not delivered to chat directly; it is ranked down first, and it stays on disk so the user can ask for the full inventory later.

Structure the file in this order:

1. **Greeting Header:** A short, friendly heading and one or two sentences framing the report — e.g. a title like `# GKE Environment Discovery Report`, and a line noting this is the first-time environment scan for the project.

2. **GKE Fleet Discovery Table:** One row per discovered cluster.

   | Cluster Name | GCP Region / Zone | Status | K8s Version | Node Pools / Machine Types | Workload Identity | Observability Stack | Deployment Toolchain |
   | :----------- | :---------------- | :----- | :---------- | :------------------------- | :---------------- | :------------------ | :------------------- |

3. **Workloads Inventory Table:** One row per workload discovered across clusters.

   | Cluster | Namespace | Workload Name | Kind | Replicas (`Ready/Total`) | Probes (`Live/Ready`) | Resource QoS (`Req/Lim`) | OTel / Telemetry | Security Context (`NonRoot`) |
   | :------ | :-------- | :------------ | :--- | :----------------------- | :-------------------- | :----------------------- | :--------------- | :--------------------------- |

4. **Prioritized SRE Remediation Plan:** The full set of high-impact recommendations, grouped by priority — not just headings, but a concrete, actionable list under each:
   - **Priority 1 — Security & Identity Hardening** (Workload Identity, Shielded Nodes, Dataplane V2, Pod Security Admission, non-root/read-only filesystems).
   - **Priority 2 — Workload Reliability & Probes** (missing liveness/readiness/startup probes, resource requests/limits and QoS, HPA coverage).
   - **Priority 3 — Observability & Telemetry** (OpenTelemetry collection, Managed Service for Prometheus, SLO/error-budget alerting, missing standard SRE alerts).

   For each item, name the affected cluster/namespace/workload where applicable and state the recommended action concisely, so the reader can act on it directly.

5. **Machine-Readable Findings Block:** the same findings again, one JSON object per line, inside a
   fence whose info string is exactly `findings`:

   ````
   ```findings
   {"check": "probes-readiness", "cluster": "prod-eu", "namespace": "payments", "object": "checkout", "title": "checkout Deployment has no readinessProbe", "detail": "3 replicas, no readinessProbe on any container", "severity_hint": "high"}
   {"check": "workload-identity-off", "cluster": "prod-eu", "object": "prod-eu", "title": "Workload Identity is not enabled on the cluster", "severity_hint": "high"}
   ```
   ````

   This block is what the prioritization stage registers, so **every problem in the prose plan above
   needs a line here, and every line here needs to be a real finding.** The two are the same set said
   twice: the prose for a person, the block for the next stage.

   - `check`, `cluster`, `object` and `title` are required. `namespace` is omitted for a
     cluster-scoped finding; `object` is then the cluster's own name.
   - **One line per affected object, not per condition.** A missing `readinessProbe` on three
     Deployments is three lines. Each has its own manifest to change and gets fixed on its own
     schedule, and `check` + `cluster` + `namespace` + `object` is the finding's identity in the
     queue — collapsing them here loses two of the three permanently. The report gathers them back
     into one line.
   - `check` is a lowercase hyphenated slug naming the condition, stable across sweeps. Use the
     vocabulary below where one fits.
   - Optional: `detail` (what was observed, including how you know — a command's output, an absent
     field), `severity_hint` (`high`/`medium`/`low`, your judgement with the whole fleet in view),
     `provider_managed` (`true` for an object in `kube-system`, `kube-public`, `kube-node-lease`,
     `gke-*` or `gmp-*`). `provider_managed` is a JSON boolean, not the string `"true"`.
   - Do not score anything. The rubric lives in the next stage and needs the whole fleet's findings
     side by side.
   - A clean fleet writes the fence with nothing between the lines. An absent block is not the same
     thing, and the next stage treats it as a broken sweep.

   Nothing else goes in the block: no rank, no severity word, no recommendation prose. A line the
   next stage cannot parse stops registration for the whole file, so keep each one to a single line
   of valid JSON.

   **The check vocabulary.** These are the audit streams' own slugs. Using them means the same
   problem carries one identity whichever source found it, and a finding promoted out of the queue
   routes to the stream that owns the check.

   | what you found                   | check slug                            |
   | -------------------------------- | ------------------------------------- |
   | liveness / readiness probes      | `probes-liveness`, `probes-readiness` |
   | missing `startupProbe`           | `probes-startup`                      |
   | requests, limits, QoS class      | `no-requests`, `no-memory-limit`      |
   | HPA coverage                     | `no-hpa`, `hpa-cannot-scale`          |
   | NetworkPolicy                    | `netpol-missing`                      |
   | ResourceQuota and LimitRange     | `no-resourcequota`                    |
   | Workload Identity                | `workload-identity-off`               |
   | `runAsNonRoot` security context  | `podsecurity-gaps`                    |
   | missing `readOnlyRootFilesystem` | `readonly-root-fs`                    |
   | Shielded Nodes                   | `shielded-nodes`                      |
   | Dataplane V2                     | `datapath-provider`                   |
   | Managed Service for Prometheus   | `managed-prometheus`                  |
   | node auto-upgrade                | `no-autoupgrade`                      |

   For anything else, write a lowercase hyphenated slug naming the condition, and keep it stable: it
   is the row's identity across every later sweep.

---

## Step 5: Hand Off to Prioritization

Once `/opt/data/INVENTORY.raw.md` is fully written and confirmed on disk, file exactly one card to
rank it into the delivered report:

```
kanban_create(
  assignee='platform',
  idempotency_key='bootstrap-inventory-prioritize',
  title='Prioritize the onboarding inventory report',
  body=<the instructions below>,
)
```

The body must tell that worker to follow the prioritization SOP, reading whichever of these exists:

- `/opt/data/profiles/platform/governance/inventory_prioritize_sop.md`
- `/opt/platform-template/governance/inventory_prioritize_sop.md`

and to read `/opt/data/INVENTORY.raw.md` as its only input, writing the ranked report to
`/opt/data/INVENTORY.md`.

**Use that exact idempotency key.** Onboarding must happen once; the key is what makes a retry or a
duplicate of this card re-attach to the prioritization already in flight instead of writing the
report twice. One caveat: the board answers a repeated key with the id of the existing card, even a
completed one. If the create returns a card that has already completed and `/opt/data/INVENTORY.md`
is still absent, that earlier card failed without producing a report — file one more card with a
suffixed key (`bootstrap-inventory-prioritize-retry-1`) instead of reusing a key the board has
already answered.

**Do not prioritize the findings yourself.** Ranking runs as its own card on purpose: it must see the
raw findings and nothing else. Doing it here would rank them against the whole transcript of your
sweep instead, which produces a different report depending on how the sweep happened to go.

---

## Step 6: Silent Exit

Once the prioritization card is filed, return strictly `[SILENT]` immediately without running any
further terminal commands. Delivery to chat is handled separately by the
`bootstrap-inventory-delivery` job, after prioritization writes `/opt/data/INVENTORY.md` — do not
attempt to send the report yourself.
