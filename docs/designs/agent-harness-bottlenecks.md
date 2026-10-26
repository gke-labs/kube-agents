# Agent harness bottlenecks at fleet scale

## High level summary

kube-agents runs the Platform Agent and its supporting components in one GKE cluster. This document calls them the harness. The harness watches and queries a fleet of clusters. The event watcher is the harness component that reads cluster events and starts a session, one agent conversation, for each incident. A large fleet puts load on the harness. Too much load makes responses slow and causes OOMKills. An OOMKill stops a container that uses more memory than its limit.

This research measures the harness on small fleets. Each experiment increases one stress point. A stress point is one property of the fleet or of its use, for example the number of clusters. The research fits a model to the data and predicts the breaking points for larger fleets. The result is a chart that answers two questions. First: are the current container limits enough for a given fleet? Second: which node size and container limits does a given fleet need?

The first measurement, Phase 1, found no container near its memory limit at 8 clusters. It found that Warning events make the event watcher open many sessions, one for each event. It also found that the credential proxy reaches its CPU limit at 8 clusters. The credential proxy is the component that runs the agent's kubectl and gcloud commands.

# Context

When kube-agents is installed to large projects, depending on the shape of the fleet and usage pattern, the agent harness is stressed in various ways. This results in slow response time, or even OOMKills.

## Estimating Limits

Driven by client use cases, some projects have thousands of clusters, some have a large volume of logs, some have very short pod lifecycles; they all stress the harness in different ways.

Many are infeasible to replicate in a testing environment. So we estimate system limits by:

1. Set up a base project, starting from a reasonable size
2. Progressively experiment on different sizes of key stress points, one at a time
3. Collect data points, 3 runs per point (Phase 1 runs the 2-hour onboarding window and the event watcher soak once, so those points have no spread)
4. Fit a model per metric: a line where the data is linear (memory against objects, disk against activity), and the measured knee where a queue saturates (run slots, credential proxy admission)
5. Visualize known points via plot
6. Predict larger values on a dynamic plot; a prediction past the largest measured point is labelled as predicted, not measured
7. Suggest kube-agents deployment size according to fleet size

Model responses vary from run to run, and a provider 429 can look like a harness limit. Each run records the provider 429s it saw. A run that hit one is excluded from the response-time fit, but its memory, CPU and disk values stay: a model 429 slows the agent down and does not lower its memory. The onboarding window is the exception: a fresh install can use up its own TPM quota ([#3388](https://github.com/gke-labs/kube-agents/issues/3388)), so there the 429 count is a result, not a reason to exclude the run.

## Metrics

Each metric has a threshold. The breaking point of a metric is the stress level where it reaches its threshold. The last column says whether the Phase 1 runner records the metric; the rest are Phase 2 work.

| Metric                                                                                                                                                                                                                                                                        | Source                                                                                                                                                                                                                             | Threshold                                                                                     | Phase 1                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Memory per container: working set over the run's time window                                                                                                                                                                                                                  | Cloud Monitoring `kubernetes.io/container/memory/used_bytes` (non-evictable)                                                                                                                                                       | The container's memory limit                                                                  | Recorded                                                              |
| CPU per container                                                                                                                                                                                                                                                             | Cloud Monitoring `kubernetes.io/container/cpu/core_usage_time`                                                                                                                                                                     | The container's CPU limit                                                                     | Recorded; throttling is not                                           |
| OOMKills and restarts per container                                                                                                                                                                                                                                           | Pod `restartCount` and `lastState`, per pod, so a replaced pod counts from zero                                                                                                                                                    | Any                                                                                           | Recorded                                                              |
| Disk usage of the agent data PVC, in total and the largest Cluster Agent profile                                                                                                                                                                                              | `df` and `du` on the `platform-agent-data` volume                                                                                                                                                                                  | PVC capacity                                                                                  | Recorded                                                              |
| Network throughput                                                                                                                                                                                                                                                            | Cloud Monitoring                                                                                                                                                                                                                   | None; recorded only                                                                           | Not recorded                                                          |
| Credential proxy queue                                                                                                                                                                                                                                                        | `kubeagents_credential_proxy_admission_wait_seconds`, `kubeagents_credential_proxy_admission_refusals_total`                                                                                                                       | Refusal at 60s, the broker's admission timeout                                                | Recorded (wait p50 and max, refusals)                                 |
| Event pipeline                                                                                                                                                                                                                                                                | `k8s_event_watcher_events_seen_total`, `..._session_creates_total`, `..._events_injected_total`, `..._events_quota_suppressed_total`                                                                                               | Session creates per injected event; recorded for the fit                                      | Recorded, with events seen per minute                                 |
| Triage starts lost                                                                                                                                                                                                                                                            | No metric today: the log lines that mark a triage whose session or turn did not start (`Aborting troubleshooting trigger`, `Gateway API chat execution failed`, `Failed to call gateway API chat execution`, `TRIAGE_NOT_STARTED`) | Any                                                                                           | Recorded (log count)                                                  |
| `429 Too many concurrent runs` refusals                                                                                                                                                                                                                                       | No metric today: the agent's log and the client's errors                                                                                                                                                                           | Any                                                                                           | Recorded; slot occupancy is not                                       |
| Failed prompts: the transport could not complete it, the answer is empty, or no model call stands behind it; a rate-limited one counts as a provider 429; unfinished prompts: answered without performing the operation (declined, cancelled, cut off by the delegation wait) | The runner                                                                                                                                                                                                                         | Any                                                                                           | Recorded                                                              |
| Model tokens (input, output, cached)                                                                                                                                                                                                                                          | LiteLLM's counters over the run's window, every caller included and the whole prompt counted; the harness's counts for the run's own prompts, whose input is the non-cached part alone                                             | The project's TPM quota                                                                       | Recorded; the quota check and time to first token are not             |
| Kanban `ready` queue depth and wait                                                                                                                                                                                                                                           | `kanban_board_health.py`                                                                                                                                                                                                           | Wait longer than the response time gate                                                       | Not recorded                                                          |
| Scheduled sweep duration (hourly Cluster Agent reconcile, Controller Stall Watch)                                                                                                                                                                                             | Job logs                                                                                                                                                                                                                           | The job's schedule interval                                                                   | Not recorded                                                          |
| Response time of typical queries                                                                                                                                                                                                                                              | The runner's timing of the slowest prompt in a run, through the agent API                                                                                                                                                          | [#3140](https://github.com/gke-labs/kube-agents/issues/3140) gates: <30s OK, >60s investigate | Recorded for the final answer; first card and acknowledgement are not |
| Pod churn achieved                                                                                                                                                                                                                                                            | The runner's churner                                                                                                                                                                                                               | None; it shows whether the planned rate held                                                  | Recorded                                                              |

## Testing Stress Points

### Base numbers

2 clusters, 2 nodes each, about 100 fixture objects per cluster, 10 pods created per minute, 1 Pod failing its image pull, 1 concurrent request, and a 100-pod listing. Each experiment starts from the base and changes one factor, so the first level of each factor is the base. Each test cluster has a 2-node `e2-standard-2` default pool, and a fixture pool of `e2-small` nodes sized for the level's Pods, so the base's real node count is 3 or 4 per cluster; every record holds the real totals.

### Progressive experiments

| Factor                                                                     | Levels             | Notes                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Number of clusters                                                         | 2, 4, 8, 64        | Real small clusters                                                                                                                                                                                                                                                                                                                                        |
| Nodes per cluster                                                          | 2, 10, 100, 200    | Real `e2-small` nodes in a fixture pool, resized per level beside the default pool; 64 × 200 is a large quota and cost request                                                                                                                                                                                                                             |
| Objects per cluster (see below)                                            | 100, 4,000, 10,000 | Real objects; Pods run the pause image with a 1m CPU and 8Mi memory request, so the HPAs can compute utilization and many Pods fit on small nodes (at most 110 Pods per node)                                                                                                                                                                              |
| Pods created/deleted per minute                                            | 10, 1,000, 100,000 | Across the fleet; 100,000 per minute on one cluster exceeds what its control plane accepts. The watcher sees churn through the Events it produces                                                                                                                                                                                                          |
| Pods failing at once, each writing Warning events                          | 1, 10, 100, 1,000  | One workload whose Pods fail their image pull: the fan-out shape the per-Pod dedup key turns into many incidents ([#660](https://github.com/gke-labs/kube-agents/issues/660), [#3567](https://github.com/gke-labs/kube-agents/issues/3567), [#3583](https://github.com/gke-labs/kube-agents/issues/3583)). The event rate it produces is measured, not set |
| Concurrent requests: user questions while scheduled audits and triages run | 1, 15, 50, 100     | Above 10, this measures refusal and queueing at the run slots                                                                                                                                                                                                                                                                                              |
| Size of one listing the agent reads (Pods in one namespace)                | 100, 1,000, 4,000  | Credential proxy child memory ([#2045](https://github.com/gke-labs/kube-agents/issues/2045)); a separate fixture of that many Pods in one namespace on the first test cluster, because the object levels hold fewer Pods                                                                                                                                   |

### What "objects per cluster" means

An object is one Kubernetes API resource in a fleet cluster that the harness reads, lists or caches. Two kinds of read load the harness differently:

- **Watched as a stream, or cached in the host cluster.** The event watcher lists and watches every `core/v1` Event of every watched cluster and keeps no copy once it has dispatched one: its informer store was removed after [#2648](https://github.com/gke-labs/kube-agents/issues/2648), so its memory follows the event rate and its dedup and incident caches, not the event backlog. Event count follows from the other objects: each Pod start, failure, scheduling attempt and node change writes Events, which the API server keeps for its event TTL (1 hour by default). The operator caches ConfigMaps, Deployments, Services, Roles and RoleBindings, but not Pods, and only in the cluster where kube-agents runs ([#3493](https://github.com/gke-labs/kube-agents/issues/3493)).
- **Listed on demand.** Agent operations list objects through the credential proxy: Pods, Nodes, Namespaces, Services, Deployments, HPAs, PersistentVolumeClaims and custom resources such as Config Connector resources (233 CRDs on one Config Connector cluster, [#3142](https://github.com/gke-labs/kube-agents/issues/3142)). One listing holds all its objects in memory in the `kubectl` child at once ([#2045](https://github.com/gke-labs/kube-agents/issues/2045)).

Examples, for one application called `checkout` in one fleet cluster:

| Object                    | Example                                                                   | What it is                                                                                                                               |
| ------------------------- | ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                 | `payments`                                                                | A named group of objects. A team's applications usually share one namespace.                                                             |
| Deployment                | `checkout-api`                                                            | Declares that 2 copies of the `checkout-api` container must run, and replaces a copy that stops.                                         |
| Pod                       | `checkout-api-7d9f8c-x2k4p`                                               | One running copy of the container. A Deployment with 2 replicas has 2 Pods; each restart or rollout creates new Pods with new names.     |
| Service                   | `checkout-api`                                                            | A fixed network address that sends traffic to the Deployment's Pods.                                                                     |
| HPA                       | `checkout-api`                                                            | Changes the Deployment's replica count between 2 and 10 from CPU usage.                                                                  |
| ConfigMap                 | `checkout-config`                                                         | Settings that the Pods read, for example a database host name.                                                                           |
| PersistentVolumeClaim     | `orders-data`                                                             | A request for a disk that a Pod mounts.                                                                                                  |
| Node                      | `gke-prod-pool-1-a1b2`                                                    | One VM that runs Pods.                                                                                                                   |
| Event                     | `Back-off restarting failed container` on Pod `checkout-api-7d9f8c-x2k4p` | A record that the cluster writes when something happens to an object. A Pod that crash-loops writes a new Event at each restart attempt. |
| Config Connector resource | `SQLInstance orders-db`                                                   | Declares a Google Cloud resource, here a Cloud SQL instance, as a Kubernetes object.                                                     |

One application in this example is 1 Deployment, 1 Service, 1 HPA, 2 Pods and 1 or 2 ConfigMaps. A cluster with 600 such applications in 100 namespaces is the 4,000 level below.

The experiment builds each level from a fixed mix modelled on the high-cardinality fleet in [#1986](https://github.com/gke-labs/kube-agents/issues/1986), where every cluster replicates its namespaces, Services and HPAs:

| Level  | Namespaces | Deployments (1 Service and 1 HPA each) | Pods (2 per Deployment) | ConfigMaps |
| ------ | ---------- | -------------------------------------- | ----------------------- | ---------- |
| 100    | 5          | 15                                     | 30                      | 20         |
| 4,000  | 100        | 600                                    | 1,200                   | 900        |
| 10,000 | 250        | 1,500                                  | 3,000                   | 2,250      |

The level is the sum of these fixture objects (namespaces + Deployments + Services + HPAs + Pods + ConfigMaps, ignoring ReplicaSets and Endpoints). A GKE cluster holds objects of its own before any fixture: a new Standard test cluster had about 13 namespaces, 50 Pods, 11 Deployments, 8 Services and 56 ConfigMaps, so the base's real total is about 250 objects of these kinds per cluster, not 100. Each run therefore records the clusters' real totals by kind, summed over the test fleet, and the fit uses the totals, not the level. Events are measured, not set: the pod-churn and Warning-event factors drive them.

## Agent Operations

Each operation runs against the factors that load it, not against every factor. `make scale-plan` prints the resulting matrix.

| Operation                                                                                                                                                                            | Factors                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------- |
| First 2 hours after installation, with the onboarding jobs running in the background: the runner resets the install's state volumes, as a new install starts without them, and soaks | Clusters, objects per cluster                      |
| The event watcher at steady state, a 1-hour soak                                                                                                                                     | Nodes per cluster, pod churn, Pods failing at once |
| Run a fleet audit (`fleet-audit`) across every cluster                                                                                                                               | Clusters, objects per cluster, listing size        |
| Run the Upgrade Readiness Report                                                                                                                                                     | Clusters                                           |
| Restart the agent pod while the fleet holds standing Warning events (boot burst)                                                                                                     | Pods failing at once                               |
| Ask "which clusters, if any, are stressed?" (a fan-out); at concurrency above 1, the other requests are "which clusters do you monitor?", sent at the same moment                    | Clusters, concurrent requests                      |
| List every Pod of the listing namespace on the first test cluster                                                                                                                    | Listing size                                       |
| Run the scheduled jobs: hourly Cluster Agent reconcile, Controller Stall Watch, drift detection (Phase 2; drift detection is off in the experiment install)                          | Clusters                                           |
| Read every Config Connector resource on one cluster (needs Config Connector installed on a test cluster; Phase 2)                                                                    | Objects per cluster                                |

# Delivery / Success Criteria

- For each stress point, a table and plot of every metric above against the stress level, with the fitted model
- The breaking point of each metric, against the threshold in the Metrics table
- Visualize the data as a chart in the UI of `scripts/admin_portal.sh`, answering two questions:
  1. **Are these container limits enough for this fleet?** Input: the fleet's stress points (clusters, nodes and objects per cluster, pod churn, Pods failing at once, concurrent requests, listing size) and the container limits (the defaults or the install's `PlatformAgent` values). Output: per container, the predicted peak memory and CPU against its limit, with the headroom; the same check for the limits that resources do not move and Phase 1 measures (credential proxy refusals, run-slot refusals, triage starts lost, failed prompts, response time, PVC use); and the first limit the fleet reaches.
  2. **What node size and container limits does this fleet need?** Input: the fleet's stress points. Output: a limit per container (predicted peak plus a safety margin) written as the `PlatformAgent` fields to set; the smallest machine type, from a list of typical ones, that fits the largest pod, and the node count the pool needs for all of them; the PVC size; and each limit that resources cannot fix, with the setting that moves it or the statement that none does.
- Show proof of the collected data in the UI, in a toggled section

## Decisions

1. **Where the data lives.** The measured data is committed to the repository as `bench/scale/harness-limits.json`: one record per run, with the commit and image the install ran, the runner's own commit, the mode, the date, the factor and its level, every factor's planned value, the clusters' real object totals, the containers' limits and replica counts, the operation, each metric's value, the start of each prompt's answer, and any error that made the run differ from its plan. Its schema is in `bench/scale/README.md`. A page in the admin console (`scripts/admin_portal.sh`) loads that file, so the chart works without a connected install. A new run merges its records into the file, keyed by commit, mode, factor, level, operation and run number; it replaces only a record with the same key. Records from earlier commits stay; the page shows one commit at a time.
2. **Safety margin.** Question 2 sets each container limit 30% above its predicted peak. The margin covers three things the fit does not: the spread between the 3 runs at a point, bursts shorter than the sampling interval (the boot burst, a fan-out), and garbage-collector headroom: the event watcher sets `GOMEMLIMIT` to half its container limit, so its heap is collected harder as it approaches that point. The 30% is the whole margin. Phase 1 draws no error band: its points have 3 runs at most, and the onboarding and soak points one, too few for a band to mean much; a prediction past the largest measured point is labelled predicted, not measured.
3. **Node placement.** All kube-agents pods run on one node pool. The node size is the smallest machine type whose allocatable memory and CPU fit the largest pod's recommended requests; the pool's node count is the sum of every pod's requests, replicas included, divided by that machine's allocatable. The node-size answer picks from five machine types. Allocatable memory is what remains after GKE's system reservation and the 100Mi eviction threshold; the values are approximate, from GKE's reservation formula:

   | Machine type     | vCPU | Memory | Allocatable memory (GiB, about) | Why it is on the list                      |
   | ---------------- | ---- | ------ | ------------------------------- | ------------------------------------------ |
   | `e2-standard-2`  | 2    | 8 GB   | 6.1 GB                          | Smallest size on the list; the lower bound |
   | `e2-standard-4`  | 4    | 16 GB  | 13.3 GB                         | The installer's Standard-mode default      |
   | `e2-standard-8`  | 8    | 32 GB  | 28.3 GB                         | Next step up, same family                  |
   | `n2-standard-16` | 16   | 64 GB  | 58.4 GB                         | Large fleet, CPU-bound fan-outs            |
   | `n2-highmem-16`  | 16   | 128 GB | 118.6 GB                        | Large fleet, memory-bound caches           |

   The experiment's host cluster is Standard, so the node-size answer can be checked. The installer's default is Autopilot, which has no node choice; there, question 2 answers with pod requests only.

4. **Container limits and node size.** A larger node does not raise the container limits by itself. The operator renders the agent's defaults (`agent-api-auth` 2Gi) and the `PlatformAgent` CR moves them: `spec.deployment.resources`, `spec.deployment.credentialProxy.resources` and `spec.deployment.agentAPIAuth.resources`. The controller-manager's limit (512Mi, request 128Mi) is the chart value `operator.resources`. Question 2 therefore derives the node from the limits, not the limits from the node.
5. **Where the experiments run.** A second kube-agents install in the author's dev project, not the dev install itself: the onboarding runs reset the install's state volumes (agent data, session metadata, shell data and the bus's data), which deletes its memory and profiles. The second install has its own Standard host cluster (`e2-standard-4`), its own service-account names (`PLATFORM_AGENT_GSA_NAME`, `LITELLM_GSA_NAME`, `TF_VAR_agent_ksa_name`), mode `next`, and one pinned, validated release-candidate image for the whole phase. Its drift detector is off in Phase 1, because the project's drift sink and subscription belong to the dev install. It shares the project's model quota with the dev install, so runs record provider 429s (see Estimating Limits).
6. **The test fleet.** New Standard zonal clusters, labelled `purpose=harness-scale`, created for the experiment and deleted after it (`teardown --delete-fleet`). The runner creates and deletes only clusters named `<prefix>NN` that also carry that label, and resets only an install whose host cluster carries it. The seeded fleet is not used: its planted failures already produce Warning events, so it cannot hold a controlled base, and eval cases depend on it. Scope is per project, so both installs see every cluster in the project: the experiment install excludes every cluster but the test fleet (`spec.scope.exclude.clusters`), and the dev install excludes the test fleet and the experiment's host cluster.
7. **Safety and the API.** The experiment install has no chat platform and no GitOps repository or GitHub token, so a planted fault cannot post to a channel or open a pull request ([#660](https://github.com/gke-labs/kube-agents/issues/660)). The runner sends every request through the agent API, over the repository's `kubectl exec` relay because the agent pod runs under gVisor, and counts triage outcomes from the agent's log lines and the API's answers. The alert ceilings stay at their defaults.
8. **Real nodes and Pods.** Every level uses real nodes and Pods. Fixture Pods run the pause image (`registry.k8s.io/pause`) with a 1m CPU and 8Mi memory request, on a fixture pool of `e2-small` nodes that the runner resizes per level beside each cluster's 2-node default pool, so the cost is mostly nodes: 100 `e2-small` nodes cost about $1.70 per hour. A node holds at most 110 Pods, so a level with many Pods also needs more nodes: 1,200 Pods need at least 11 nodes, and 4,000 need at least 37. The factors therefore do not change fully alone; each run records the clusters' real totals by kind, including nodes, and the fit uses those totals.
9. **Reusable tooling.** The measurement repeats as the codebase changes, so the tooling is a tool, not a one-off script:
   - The plan is data: factors, levels, operations and run counts live in a plan file (`bench/scale/phase1.yaml`), so a later run changes the plan, not the code. Phase 2 is another plan file.
   - The target is a parameter: the plan's `target` block names no project, and every key can be set as `SCALE_<KEY>` in the environment. The host context is derived from the host cluster, and a context that does not name it is refused.
   - Fixtures are idempotent: each level applies and removes its objects by label, so a level can be re-applied or torn down after a failed run.
   - The runner's commands are `plan`, `run`, `apply` (one level), `teardown` and `reclassify` (re-applies the current rules to stored records); `run` resumes from the records already in the file.
   - Each record carries the image the install ran and the commit its tag pins, so results from two builds compare directly. A build is keyed by its commit when the tag pins one, and by its whole tag otherwise, so a new build is a new set of keys.
   - Tests sit in `bench/tests/` and run with `make test-bench`; `make scale-plan` and `make scale-run` start the runner.

## Open questions

1. **Fleet size has more than one dimension.** Memory depends on total watched objects (clusters × objects per cluster), but single-factor experiments vary each factor alone. Plot memory against total watched objects so the two experiments fall on one axis, and confirm with one combined point (for example 8 clusters × 4,000 objects).
2. **Which mode.** `spec.mode: today` and `spec.mode: next` load the harness differently (session pods, the A2A bus, kanban dispatch). The results hold only for the mode measured; each record carries it.

# Phases

The work runs in two phases. Phase 1 ran from 2026-10-22 to 2026-10-24; its results are under Phase 1 results below. Phase 2 started on 2026-10-26 with the points the Phase 1 data pointed at.

## Phase 1: two data points per factor

Each factor is measured at the base and at one higher level, the one the known issues point at:

| Factor                          | Base     | Second point | Why this point                                                                                         |
| ------------------------------- | -------- | ------------ | ------------------------------------------------------------------------------------------------------ |
| Number of clusters              | 2        | 8            | New zonal test clusters; 64 needs a quota check                                                        |
| Nodes per cluster               | 2        | 100          | Event and Node volume of a mid-size cluster                                                            |
| Objects per cluster             | 100      | 4,000        | The high-cardinality fleet in [#1986](https://github.com/gke-labs/kube-agents/issues/1986)             |
| Pods created/deleted per minute | 10       | 1,000        | Within what one control plane accepts                                                                  |
| Pods failing at once            | 1        | 100          | One fault on 100 Pods, the fan-out shape in [#660](https://github.com/gke-labs/kube-agents/issues/660) |
| Concurrent requests             | 1        | 15           | Past the 10 Hermes run slots ([#3724](https://github.com/gke-labs/kube-agents/issues/3724))            |
| Size of one listing             | 100 pods | 4,000 pods   | The credential proxy child size in [#2045](https://github.com/gke-labs/kube-agents/issues/2045)        |

That is 8 points and 46 runs (`make scale-plan`): the base and 7 second points. Two points fit a line but cannot show a knee or a curve, so every Phase 1 prediction is labelled as a two-point estimate.

Phase 1 runs the 2-hour onboarding window and the event watcher soak once per point, and the other operations 3 times. It changes neither the agent nor the operator: triage starts lost and run-slot refusals are counted from log lines and the client's errors.

| Operation                                                | Points × runs | Time per run | Total     |
| -------------------------------------------------------- | ------------- | ------------ | --------- |
| First 2 hours after installation, after a state reset    | 3 × 1         | ~2.2 h       | ~7 h      |
| Event watcher at steady state                            | 4 × 1         | 1 h          | 4 h       |
| Fleet audit                                              | 4 × 3         | ~30 min      | 6 h       |
| Fan-out question                                         | 3 × 3         | ~20 min      | 3 h       |
| Upgrade Readiness Report                                 | 2 × 3         | ~20 min      | 2 h       |
| Boot burst                                               | 2 × 3         | ~15 min      | 1.5 h     |
| Listing                                                  | 2 × 3         | ~10 min      | 1 h       |
| Level changes, cluster creation and profile registration | 8             | ~20 min      | 3 h       |
| **Total**                                                |               |              | **~28 h** |

Building came first; the runs then took about 2 days of elapsed time, longer than the 28 h of machine time because of a network outage, re-runs and an IAM automation (see Phase 1 results). Every run records its model token counts. The scheduled jobs are not timed in Phase 1.

Steps, in order:

1. The experiment install, its Standard host cluster, the test clusters, and the scope exclusions on both installs (Decisions 5–7).
2. The fixtures and runner (`bench/kube_agents_bench/scale/`, with the Phase 1 plan in `bench/scale/phase1.yaml`), built to Decision 9, and their tests: the object mix for the 4,000 level, the node-pool scaling, the churn and burst generators, the 4,000-pod listing, and a loop that runs the operations and merges records into `bench/scale/harness-limits.json`.
3. The base run.
4. The other runs.
5. An admin console page that loads `bench/scale/harness-limits.json` and shows, per metric, the two points against the metric's threshold, with the raw data in a toggled section. It answers the two sizing questions from the two-point estimates.

## Phase 2: the points Phase 1 pointed at

Phase 2 (`bench/scale/phase2.yaml`) measures where the Phase 1 lines come close to a threshold, the combined point, and the credential proxy's own throughput. It runs on the same image as Phase 1, so it reuses the Phase 1 base runs.

| Factor                        | Levels          | Operations                        | Why                                                                                                                                           |
| ----------------------------- | --------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Credential proxy load workers | 1, 4, 8, 16, 32 | Proxy load                        | The brokered command path's throughput and latency with no model call, and what binds first: the shell's CPU, the broker's CPU or its 8 slots |
| Concurrent requests           | 8, 10, 12       | Fan-out                           | The step at the 10 Hermes run slots                                                                                                           |
| Pods failing at once          | 10, 30          | Event watcher soak, boot burst    | How the session storm grows between 1 and 100 Pods                                                                                            |
| 8 clusters × 4,000 objects    | one point       | Onboarding, watcher soak, fan-out | The first open question: whether the factors add up                                                                                           |
| Number of clusters            | 16, 32          | Onboarding, watcher soak, fan-out | Where the credential proxy queues, and how memory and the event watcher grow with the fleet                                                   |

The proxy load is a new operation. Workers in the sandbox shell repeat one brokered `kubectl get pods --all-namespaces` against the first test cluster through `credential-proxy-exec`, the path every agent command takes, for 3 minutes; the run records the commands completed per second and their latency. The client runs in the shell and the command in the broker, so the shell's and the broker's CPU peaks show which of them sets the throughput. Every run now also records the commands the broker ran and the seconds they ran for (`kubeagents_tool_invocations_total`, `kubeagents_tool_execution_duration_seconds`), so each operation's demand on the broker can be set against that throughput, and each container's requests beside its limits. The runner runs the Cluster Agent reconcile when the profiles differ from the test fleet, instead of waiting up to an hour for its scheduled run.

That is 55 runs, 47 of them new, and about 18 hours. The remaining candidates are not measured yet:

- the remaining planned levels, and the 64-cluster level after a quota and cost check (cluster management costs about $0.10 per cluster-hour);
- a churn fixture that applies batches in parallel, so the 1,000 per minute level is reached;
- 3 runs for the onboarding window and the watcher soak;
- the Config Connector read, on a test cluster with Config Connector installed;
- the scheduled jobs' sweep duration, kanban queue depth, CPU throttling, network throughput, time to first card and time to first token;
- the instrumentation: a counter for triage starts lost ([#3583](https://github.com/gke-labs/kube-agents/issues/3583)) and a gauge for occupied Hermes run slots ([#3724](https://github.com/gke-labs/kube-agents/issues/3724));
- a recurring run: the Phase 1 plan on each release candidate, with the page flagging a metric that moved past a set fraction of the previous release.

Each limit the data shows below a realistic fleet size gets an issue, linked from the Known Issues table.

# Phase 1 results

The Phase 1 records are in `bench/scale/harness-limits.json` (46 runs, image `eeb8a95c`, mode `next`, 2 test clusters at the base); the admin console's Harness limits page charts them and answers the two sizing questions. This section states what they show; the page and the file hold every number.

## What the data shows

- **No container reached its memory limit, and none restarted or was OOM-killed.** The largest memory peaks were the agent container (`platform-agent`) at about 2.1 GiB of 8 GiB in the 8-cluster onboarding window, and the credential proxy at about 710 MiB of 2 GiB, in that window and when the agent read a namespace of 4,000 Pods. The event watcher's container (`agent-api-auth`) stayed between 125 and 181 MiB of 2 GiB at every point.
- **The credential proxy reaches its CPU limit and starts to queue at 8 clusters.** In the 8-cluster onboarding window it peaked at 1.05 of its 1-core limit, its admission wait reached the 10–15 s histogram bucket (at most 0.1 s at every other measured point), and its memory reached 712 MiB, against 0.61 cores and 536 MiB in the 2-cluster onboarding window. It reached 1.06 cores in one 8-cluster upgrade readiness run and 0.94 in another ([#2637](https://github.com/gke-labs/kube-agents/issues/2637)). The sandbox shell container reached 1.9–1.95 of its 2 cores in the 4,000-object onboarding window and the failing-Pods soak.
- **Warning events open sessions far faster than they become triages.** At the base the event watcher opened no session. With 100 nodes per cluster it opened 163 in an hour (GKE's own `netd` Pods failing on new nodes), with about 260 Pods created per minute 1,006, with 100 Pods failing at once 208, and while 4,000 listing Pods waited to be scheduled up to about 5,000 in half an hour. Across all 46 runs it opened 10,731 sessions for 8 triages: the daily alert ceiling suppressed 10,532, and each suppressed event still opened a session ([#3567](https://github.com/gke-labs/kube-agents/issues/3567)). The agent container's memory rose with the session count, from 556 MiB at the base soak to about 1.32–1.38 GiB at the churn and failing-Pods levels.
- **Every typical question is past the investigate gate already at 2 clusters.** A fan-out question took 140–146 s at 2 clusters and 101–173 s at 8; listing a 100-Pod namespace took 231–261 s, against the [#3140](https://github.com/gke-labs/kube-agents/issues/3140) gates (under 30 s OK, over 60 s investigate). With 4,000 Pods in one namespace, a fleet audit took 481–512 s and one run outlasted the 1,800 s delegation wait.
- **Fleet audit and upgrade readiness did not run as intended on this install.** Both can need a registered GitOps repository, which the experiment install deliberately has none of (Decision 7): of the runs whose answers were kept, 2 of 3 fleet audits at 8 clusters cancelled on a misread of the scope ([#3933](https://github.com/gke-labs/kube-agents/issues/3933)) and the third was declined for want of a repository, and one upgrade readiness run was declined. The records keep those runs, out of the response-time fit; the base runs of both operations, and the first 4,000-object fleet audit, were written before answers were kept and are out of it too.
- **Concurrent requests meet the 10 run slots.** With 15 questions sent at once, all 15 were answered in one run; in each of the other two, 5 were refused with `Too many concurrent runs` ([#3724](https://github.com/gke-labs/kube-agents/issues/3724)).
- **The onboarding window costs millions of tokens.** LiteLLM counted 9.2 M input tokens over the 2-hour window at 2 clusters, 10.4 M at 8 clusters and 15.6 M at 4,000 objects per cluster; output tokens stayed between 0.07 and 0.13 M.

## Findings filed

- [#3932](https://github.com/gke-labs/kube-agents/issues/3932): the event watcher reads its watch set once, at start. Clusters registered later, every cluster after a reset of the agent's data volume, and clusters removed from scope stay as they were until the gateway pod restarts. The runner restarts the gateway to measure with the fleet watched; the onboarding runs measure the gap as shipped.
- [#3933](https://github.com/gke-labs/kube-agents/issues/3933): a fleet audit cancels, claiming every managed cluster is excluded, when as many clusters are excluded as are managed.
- The measured session storm and run-slot refusals are on [#3567](https://github.com/gke-labs/kube-agents/issues/3567) and [#3724](https://github.com/gke-labs/kube-agents/issues/3724).

## What limits the result

- **Two points per factor.** Each line is a two-point estimate, and the page labels it so; nothing here shows a knee or a curve.
- **No GitOps repository.** The safety decision that kept planted faults from opening pull requests also kept fleet audit and upgrade readiness from running fully; their response times stand only where the answer shows the operation ran.
- **The churn fixture fell short.** It achieved about 260 Pods per minute of the planned 1,000: one `kubectl apply` per batch takes longer than the batch interval. Records carry the achieved rate, and the page plots churn at it.
- **The project's IAM was rewritten from outside.** An organization IAM automation removed the install's service-account bindings about every 2 hours. Runs that overlapped a removal before a guard existed were deleted and re-run; after it, the bindings came back within 2 minutes, and the three records that overlapped a short gap say how many seconds (from the project's audit log, added after the runs).
- **Soaks and onboarding ran once per point,** so their points have no spread, and the prompt operations' response times varied by up to 4.8 times between runs of one point.
- **The runner restarts the gateway** when the watcher does not watch exactly the test fleet ([#3932](https://github.com/gke-labs/kube-agents/issues/3932)), so each such run starts on a fresh agent process.
- **The admission wait is a histogram bucket bound,** not an exact time: 15 s means between 10 and 15 s.

# Out of scope

For now we won't cover these topics, which are limitations of this research.

## Breaking points

Some issues only happen after a timing threshold is crossed, for example a retry storm after a timeout. We do not search for them, but a run that shows one is reported: retry loops caused known OOMKills ([#2652](https://github.com/gke-labs/kube-agents/issues/2652), [#1333](https://github.com/gke-labs/kube-agents/issues/1333)).

We do aim to find hard breaking points such as OOMKills, by comparing the predicted memory with the container limit.

## Correlated factors

Some stress points may amplify each other, for example cluster count and concurrent audit requests. Each experiment changes one factor, so these interactions are not measured, apart from the one combined point that checks the total-objects axis.

# Appendix

## Known Issues

Each row is a GitHub issue that reports a scale bottleneck with evidence. "Scale" is the fleet size where the reporter saw the bottleneck. A row describes the mechanism as the issue reported it; some have since changed (the event watcher no longer keeps an informer store, the operator no longer caches Pods and its limit is 512Mi, the data volume defaults to 50Gi), which is what a re-run of the measurement is for.

### Memory: unbounded caches against fixed limits

| Issue                                                        | Bottleneck                                                                                | Scale                                                               | Evidence                                                                                                     |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| [#2648](https://github.com/gke-labs/kube-agents/issues/2648) | Event watcher informers cache every Event of every cluster, with no selector or transform | 26 clusters, 4 at 1,600–1,950 nodes                                 | `agent-api-auth` goes from 440Mi to over 2Gi within minutes; 8+ restarts in 30 min                           |
| [#1333](https://github.com/gke-labs/kube-agents/issues/1333) | Forbidden Event informers retry forever on every cluster                                  | 26 clusters                                                         | OOM at the 2Gi limit; each restart resets profile directory permissions and blocks multi-cluster tasks       |
| [#1986](https://github.com/gke-labs/kube-agents/issues/1986) | Platform Agent OOM on high-cardinality clusters                                           | 39 clusters, ~2M cores, ~4,000 services/namespaces/HPAs per cluster | OOM "every few seconds" after 2–3 clusters; object count per cluster matters more than cluster count         |
| [#2018](https://github.com/gke-labs/kube-agents/issues/2018) | The watcher embedded in the Python credential proxy grows ~100Mi/min                      | same fleet as #1986                                                 | 6 OOMKills in 50 min against a 1Gi limit (2Gi before); the workaround was to disable the event watcher       |
| [#3493](https://github.com/gke-labs/kube-agents/issues/3493) | The operator cache is unscoped (every Pod, ConfigMap, Role) under a 128Mi limit           | 15- and 89-node clusters                                            | Repeated controller-manager restarts while 1–3 node installs stay clean; mechanism confirmed, OOM not proven |
| [#1934](https://github.com/gke-labs/kube-agents/issues/1934) | The drift detector has no `GOMEMLIMIT` and shares a container with the watcher            | —                                                                   | Missing guard; no observed failure                                                                           |

### Credential proxy: one broker for every command

| Issue                                                        | Bottleneck                                                                                                         | Scale                                   | Evidence                                                                                                                                    |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| [#2324](https://github.com/gke-labs/kube-agents/issues/2324) | 8 concurrent `gcloud` children exceed the broker's memory limit                                                    | 143 clusters, 36 projects               | The Stall Watch cron fans out 8 at once; `memory.peak` 948MB against 1Gi                                                                    |
| [#2045](https://github.com/gke-labs/kube-agents/issues/2045) | `kubectl` children are capped by count, not by size                                                                | 4,000 pods                              | One `-o yaml` listing takes 1,281 MiB; the cgroup v2 group OOM killer stops the whole container                                             |
| [#2652](https://github.com/gke-labs/kube-agents/issues/2652) | An audit retries large-cluster reads into the proxy it crashed; `collect.py` runs 8-wide against 4 admission slots | 7-cluster audit on the 26-cluster fleet | Proxy at 827Mi, 6+ OOMKills; the audit never finishes                                                                                       |
| [#2637](https://github.com/gke-labs/kube-agents/issues/2637) | One replica handles all brokered execs; the admission budget turns the OOM into a queue                            | 143 clusters                            | ~6,000 execs/hour at the CPU limit; up to 1,225 queued requests, p50 wait 22.5s, max 56.7s; the broker refuses at 60s, the preflight at 15s |
| [#3142](https://github.com/gke-labs/kube-agents/issues/3142) | The agent reads CRDs one at a time through the broker                                                              | 233 Config Connector CRDs               | 769s at 3.3s per RPC; batched reads take 30s                                                                                                |

### Event volume: fan-out per pod, not per fault

| Issue                                                                                                                    | Bottleneck                                                                | Scale                 | Evidence                                                                              |
| ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------- |
| [#660](https://github.com/gke-labs/kube-agents/issues/660), [#2512](https://github.com/gke-labs/kube-agents/issues/2512) | The dedup key is `(pod UID, reason)`, so a fault on N pods is N incidents | one registry-wide 429 | 60 sessions, 39 cards, 36 PRs (35 auto-merged), although dedup suppressed 482 repeats |
| [#3567](https://github.com/gke-labs/kube-agents/issues/3567)                                                             | The watcher opens a session before the inject decision                    | seeded fleet          | 101,492 session creates against 15 injected events, ~7 creates/min                    |
| [#3272](https://github.com/gke-labs/kube-agents/issues/3272)                                                             | No per-cluster or per-namespace alert controls                            | seeded fleet, 1 hour  | 27 triage cards and 20 alerts; triage holds 4 of 5 slots with 14 queued               |
| [#3192](https://github.com/gke-labs/kube-agents/issues/3192)                                                             | A Pub/Sub pull over the 8 MiB relay cap is redelivered forever            | audit-log bursts      | The batch is never acknowledged (not confirmed)                                       |

### Concurrency: slots, queues and bursts

| Issue                                                                                                                      | Bottleneck                                                                                    | Scale                  | Evidence                                                                                              |
| -------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------------------- |
| [#3724](https://github.com/gke-labs/kube-agents/issues/3724), [#1097](https://github.com/gke-labs/kube-agents/issues/1097) | The watcher's boot burst fills Hermes's 10 run slots; the triage route does not check the cap | 5 clusters             | User turns get `429 Too many concurrent runs` seconds after a pod restart                             |
| [#3583](https://github.com/gke-labs/kube-agents/issues/3583)                                                               | A triage start makes 1 attempt with a 10s timeout and is lost silently on failure             | bursts of 16–21 alerts | 6 of 16 and 10 of 21 triages lost; the ledger still records them as delivered                         |
| [#3111](https://github.com/gke-labs/kube-agents/issues/3111), [#2032](https://github.com/gke-labs/kube-agents/issues/2032) | Kanban runs 1 task per profile at a time; the global `max_in_progress` defaulted to 2         | 8 clusters             | 4 alerts on one cluster take >35 min in series; an 8-cluster node count takes 8–10 min                |
| [#3299](https://github.com/gke-labs/kube-agents/issues/3299), [#3310](https://github.com/gke-labs/kube-agents/issues/3310) | Background audits starve interactive queries; fan-out cards stall in `ready`                  | 5 clusters             | "List clusters" queues 5+ min and becomes a 15–20 min fan-out; 5 cards sit in `ready` for 40 min      |
| [#3286](https://github.com/gke-labs/kube-agents/issues/3286)                                                               | A multi-cluster fan-out adds a parent LLM turn before the cluster cards                       | 3-cluster prompts      | Time to first card 33–41s, against 13–28s for a single cluster                                        |
| [#3220](https://github.com/gke-labs/kube-agents/issues/3220)                                                               | A fixed 600s session wall clock                                                               | production traces      | 8–9% of gateway and triage runs abort; deep skills need 19–22 turns at ~15s each                      |
| [#3388](https://github.com/gke-labs/kube-agents/issues/3388), [#1214](https://github.com/gke-labs/kube-agents/issues/1214) | Fan-out uses up the model quota (TPM)                                                         | fresh install, eval    | A new install uses up its own Vertex TPM quota; 429 storms push runs to the 45-min delegation timeout |

### Linear per-cluster cost: disk and sweeps

| Issue                                                        | Bottleneck                                                                 | Scale                     | Evidence                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#3325](https://github.com/gke-labs/kube-agents/issues/3325) | Per-profile files fill the fixed 10 GiB data PVC, and nothing detects it   | 209 clusters, 42 projects | PVC full 36 min after a fleet health check; kanban writes fail with `disk I/O error` while the CR stays `Ready`; per-profile tirith copies took 6.5 GiB and npm caches ~100 MiB each ([#3344](https://github.com/gke-labs/kube-agents/pull/3344), [#3610](https://github.com/gke-labs/kube-agents/pull/3610)) |
| [#3704](https://github.com/gke-labs/kube-agents/issues/3704) | Nothing prunes a profile's `state.db` or logs                              | 202 profiles              | An active profile reaches 16.5 MB in 2 weeks, so a 50 GiB volume holds ~3,200 clusters, and fewer over time                                                                                                                                                                                                   |
| [#3703](https://github.com/gke-labs/kube-agents/issues/3703) | The hourly reconcile runs one `describe` per cluster on shared sandbox CPU | 202 profiles              | 484s at 2.4s per profile; the sweep overruns the hour at ~1,500 clusters                                                                                                                                                                                                                                      |
| [#1768](https://github.com/gke-labs/kube-agents/issues/1768) | One slow cluster uses up the drift detector's shared batch join budget     | multi-cluster             | 6 slow records exhaust a 60s budget and leave healthy clusters unenriched (not reproduced)                                                                                                                                                                                                                    |

### Measurement and customer reports

- [#2348](https://github.com/gke-labs/kube-agents/issues/2348): scheduled tests cover 4 small clusters. The proposal is a nightly run over ~140 pool clusters plus one high-cardinality cluster.
- [#1913](https://github.com/gke-labs/kube-agents/issues/1913): one Platform Agent with 200 cluster profiles. It measures cluster count, not object count per cluster.
- Customer reports: [#2596](https://github.com/gke-labs/kube-agents/issues/2596) (39 clusters, ~2M cores), [#2599](https://github.com/gke-labs/kube-agents/issues/2599) (20 clusters, 2,800 namespaces), [#2453](https://github.com/gke-labs/kube-agents/issues/2453) (credential proxy memory).
- [#3140](https://github.com/gke-labs/kube-agents/issues/3140): agent response latency gates (<10s great, <30s OK, >60s investigate).
