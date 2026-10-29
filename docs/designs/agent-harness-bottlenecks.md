# Agent harness bottlenecks at fleet scale

## High level summary

kube-agents runs the Platform Agent and its supporting components in one GKE cluster. This document calls them the harness. The harness watches and queries a fleet of clusters. The event watcher is the harness component that reads cluster events and starts a session, one agent conversation, for each incident. A large fleet puts load on the harness. Too much load makes responses slow and causes OOMKills. An OOMKill stops a container that uses more memory than its limit.

This research measures the harness on small fleets. Each experiment increases one stress point. A stress point is one property of the fleet or of its use, for example the number of clusters. The research fits a model to the data and predicts the breaking points for larger fleets. The result is a chart that answers two questions. First: are the current container limits enough for a given fleet? Second: which node size and container limits does a given fleet need?

The measurements cover fleets of 2 to 40 small clusters. A bottleneck is the part of the harness that stops a larger fleet first. The credential proxy is the first bottleneck. The credential proxy is the component that runs the agent's kubectl and gcloud commands. With the default limits, the proxy reached its CPU limit during onboarding at 6, 8 and 22 clusters. When the first-run audits run at once, it reaches the limit at every fleet size. Then commands wait. With the audits at once at 16 clusters, and during onboarding at 22, a command waited between 30 and 60 seconds. After 60 seconds the proxy refuses a command. The proxy's memory nears its limit at about 15 to 25 clusters. At 16 clusters the proxy needs about 2.5 CPU cores, and its default limit is 1. The agent's memory and the event watcher stay far below their limits. The agent's CPU reached 2 of its 3 cores.

More resources do not remove some bottlenecks. The agent runs at most 10 requests at once. The sandbox shell is the container where the agent's commands start. It passes at most about 1.5 commands per second. A question that checks the fleet takes a median of more than 2 minutes at every fleet size. An HPA (HorizontalPodAutoscaler) is an object that changes the number of Pods of a workload. A fleet with many Pods and HPAs makes the event watcher open thousands of sessions an hour, even with nothing failing.

An install is one kube-agents deployment with its own Platform Agent. One install cannot hold a fleet of 2,500 clusters, the largest example fleet. With raised agent and proxy limits, one install held 40 clusters of its daily work, and the sandbox shell used 79% of its fixed CPU. A fleet of 2,500 clusters needs about 63 to 105 installs of 24 to 40 clusters each. That is about 190 to 320 nodes of type e2-standard-4. At 40 clusters per install, a question takes about 2 to 6 minutes, and Stall Watch reads each cluster about once an hour. The model's daily audits use about 1.5 to 5 billion input tokens a day across the fleet. That cost is the largest open question.

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

Each metric has a threshold. The breaking point of a metric is the stress level where it reaches its threshold. The last column says whether the Phase 1 runner records the metric. Phases 2 and 3 add each container's requests and the CPU it used over each window (`cpu_seconds`), the commands the credential proxy ran and their run time, and the proxy load's commands per second and their times (`bench/scale/README.md` lists every metric).

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
| The first 30 minutes after a reset: the reconcile and the scan (Phase 3)                                                                                                             | Clusters                                           |
| OOBE's first-run audits, all started at the beginning of a 30-minute window (Phase 3)                                                                                                | Clusters                                           |
| Brokered commands from the sandbox shell, a set number at once, with no model call (proxy load)                                                                                      | Commands at once                                   |
| Run the scheduled jobs: hourly Cluster Agent reconcile, Controller Stall Watch, drift detection (not measured yet; drift detection is off in the experiment install)                 | Clusters                                           |
| Read every Config Connector resource on one cluster (needs Config Connector installed on a test cluster; not measured yet)                                                           | Objects per cluster                                |

# Delivery / Success Criteria

- For each stress point, a table and plot of every metric above against the stress level, with the fitted model
- The breaking point of each metric, against the threshold in the Metrics table
- Visualize the data as a chart in the UI of `scripts/admin_portal.sh`, answering two questions:
  1. **Are these container limits enough for this fleet?** Input: the fleet's stress points (clusters, nodes and objects per cluster, pod churn, Pods failing at once, concurrent requests, listing size) and the container limits (the defaults or the install's `PlatformAgent` values). Output: per container, the predicted peak memory and CPU against its limit, with the headroom; the same check for the limits that resources do not move and Phase 1 measures (credential proxy refusals, run-slot refusals, triage starts lost, failed prompts, response time, PVC use); and the first limit the fleet reaches.
  2. **What node size and container limits does this fleet need?** Input: the fleet's stress points. Output: a limit per container (predicted peak plus a safety margin) written as the `PlatformAgent` fields to set; the smallest machine type, from a list of typical ones, that fits the largest pod, and the node count the pool needs for all of them; the PVC size; and each limit that resources cannot fix, with the setting that moves it or the statement that none does.
- Show proof of the collected data in the UI, in a toggled section

## Decisions

1. **Where the data lives.** The measured data is committed to the repository as `bench/scale/harness-limits.json`: one record per run, with the commit and image the install ran, the runner's own commit, the mode, the date, the factor and its level, every factor's planned value, the clusters' real object totals, the containers' requests, limits and replica counts, the operation, each metric's value, the start of each prompt's answer, and any error that made the run differ from its plan. Its schema is in `bench/scale/README.md`. A page in the admin console (`scripts/admin_portal.sh`) loads that file, so the chart works without a connected install. A new run merges its records into the file, keyed by commit, mode, factor, level, operation and run number; it replaces only a record with the same key. Records from earlier commits stay; the page shows one commit at a time.
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
   - The plan is data: factors, levels, operations and run counts live in a plan file (`bench/scale/phase1.yaml`), so a later run changes the plan, not the code. Phases 2 and 3 are other plan files in the same directory.
   - The target is a parameter: the plan's `target` block names no project, and every key can be set as `SCALE_<KEY>` in the environment. The host context is derived from the host cluster, and a context that does not name it is refused.
   - Fixtures are idempotent: each level applies and removes its objects by label, so a level can be re-applied or torn down after a failed run.
   - The runner's commands are `plan`, `run`, `apply` (one level), `teardown`, `reclassify` (applies the current rules to stored records again) and `remonitor` (reads stored records' Cloud Monitoring values again); `run` resumes from the records already in the file, checks the plan before it starts, and stops when a level cannot be applied.
   - Each record carries the image the install ran and the commit its tag pins, so results from two builds compare directly. A build is keyed by its commit when the tag pins one, and by its whole tag otherwise, so a new build is a new set of keys.
   - Tests sit in `bench/tests/` and run with `make test-bench`; `make scale-plan` and `make scale-run` start the runner.

## Open questions

1. **Fleet size has more than one dimension.** Memory depends on total watched objects (clusters × objects per cluster), but single-factor experiments vary each factor alone. Plot memory against total watched objects so the two experiments fall on one axis, and confirm with one combined point (for example 8 clusters × 4,000 objects). Phase 2 measured that point: for memory the single-factor lines add up within about 15%, the agent's peak the furthest above the sum at 1.15 times; the shell's CPU was 2.1 times the sum (Phase 2 and Phase 3 results).
2. **Which mode.** `spec.mode: today` and `spec.mode: next` load the harness differently (session pods, the A2A bus, kanban dispatch). The results hold only for the mode measured; each record carries it.

# Phases

Phase 1 ran from 2026-10-22 to 2026-10-24; its results are under Phase 1 results below. Phase 2, from 2026-10-26 to 2026-10-27, measured the points the Phase 1 data pointed at. Phase 3, from 2026-10-27 to 2026-10-28, measured the cluster levels again in 30-minute windows.

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

Phase 2 (`bench/scale/phase2.yaml`) ran on the same image as Phase 1 and reused its base runs. It measured:

| What changed                                                  | Levels          | Operations                        | Why                                                                                                                               |
| ------------------------------------------------------------- | --------------- | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Commands sent to the credential proxy at once, from the shell | 1, 4, 8, 16, 32 | Proxy load                        | How many commands per second the command path carries, and what stops it: the shell's CPU, the proxy's CPU or the proxy's 8 slots |
| Questions sent at once                                        | 8, 10, 12       | Fan-out                           | The step at the agent's 10 run slots                                                                                              |
| Pods failing at once                                          | 10, 30          | Event watcher soak, boot burst    | Where the session storm starts                                                                                                    |
| 8 clusters × 4,000 objects                                    | one point       | Onboarding, watcher soak, fan-out | Whether the effects of the factors add up                                                                                         |
| Clusters                                                      | 22              | Onboarding (2-hour window)        | The largest fleet the project could hold                                                                                          |

The proxy load is a new operation. Workers in the sandbox shell run `kubectl get pods --all-namespaces` through `credential-proxy-exec`, the path every agent command takes, for 3 minutes, and the run records the commands per second and the time each took. No model is called. Every run now also records how many commands the proxy ran and for how long, and each container's requests beside its limits.

The plan was 16 and 32 clusters. Two things stopped it. The project's default network has room for the Pod ranges of about 30 clusters (a /14 each in 10.0.0.0/9), and the project's other clusters use 8 of them. The zone also ran out of E2 and then N2 machines for hours. So the largest fleet was 22 clusters. At 22 clusters the fixture Pods run on each cluster's 2 default nodes, without a separate fixture pool.

## Phase 3: 30-minute windows

Phases 1 and 2 measured onboarding over 2 hours and soaks over 1 hour, which made each point slow. Phase 3 measures every window in 30 minutes without missing the peak.

Cloud Monitoring's 1-minute samples show when each container peaked in the earlier 2-hour onboarding windows:

| Container                                           | Minute of the peak (2 clusters, 8 clusters, 8 × 4,000 objects) | Share of the peak in the first 30 minutes |
| --------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------- |
| Agent container (`platform-agent`), memory and CPU  | 4–11                                                           | 90–100%                                   |
| Event watcher (`agent-api-auth`)                    | 1–2                                                            | 100%                                      |
| Credential proxy (`envoy-credential-proxy`), memory | 40–81                                                          | 44–97%                                    |
| Credential proxy, CPU                               | 15–59                                                          | 78–100%                                   |
| Sandbox shell (`shell`), memory and CPU             | 15–121                                                         | 27–100%                                   |

The late peaks come from the onboarding job, OOBE ([`oobe.md`](oobe.md)). After its scan, it runs its first-run audits one after another, and each audit reads every cluster through the credential proxy. At 22 clusters the first audit ran from minute 33 to minute 53 of the window, and the proxy's and the shell's memory peaks came during it. The Stall Watch job also reads every cluster, one at a time, every 30 minutes. At 22 clusters it used up its 25-minute budget after 19 clusters, and the next run went on from there.

So Phase 3 (`bench/scale/phase3.yaml`) splits onboarding into two 30-minute windows:

1. Onboarding: the reset, the Cluster Agent reconcile and the scan.
2. Audits: the runner starts all of OOBE's first-run audits at the start of the window, the same way OOBE starts them. On the measured build four exist: fleet-wide cost analysis, compliance, obtainability and stockout prevention (`upgrade-retrospective` is not on that build's roster). Four audits at once are harder on the harness than one after another, so this window shows a worst case.

The runs go from the largest fleet down, so clusters are only deleted, never created: 16, 8, 6 and 4 clusters, then the base of 2. Fan-out questions run where no earlier run exists. Every Phase 3 point has 2 nodes per cluster.

Two more changes measure what the credential proxy needs, not only what it uses:

- In the earlier onboarding windows the proxy ran at its 1-core CPU limit from 8 clusters, so its CPU peak showed the limit, not the need. Every record now also stores the CPU time each container used over its window (`cpu_seconds`), which keeps growing after the peak stops at the limit. The plan was to forecast from 4 and 6 clusters, below the limit. In the event the audit window reached the limit at every size, 2 clusters included, so the proxy's CPU need comes from the raised-limit runs below.
- At 2 and 16 clusters, `bench/scale/phase3-proxy-raised.yaml` repeats both windows with the proxy's limits raised to 4 CPU and 4 GiB, so its CPU peak shows the need.

Phase 3 did not repeat the 22-cluster point. A plan error deleted those clusters first; the runner now checks a plan before it starts and stops when a level fails. The 22-cluster point is the Phase 2 window.

Not measured yet:

- 32 and 64 clusters, which need another network for the Pod ranges and a quota and cost check (cluster management costs about $0.10 per cluster-hour);
- a churn fixture that applies batches in parallel, so the 1,000 per minute level is reached;
- more than one run of each onboarding and audit window;
- the Config Connector read, on a test cluster with Config Connector installed;
- the scheduled jobs' sweep duration, CPU throttling, kanban queue depth, network throughput, time to first card and time to first token;
- a counter for triage starts lost ([#3583](https://github.com/gke-labs/kube-agents/issues/3583)) and a gauge for busy run slots ([#3724](https://github.com/gke-labs/kube-agents/issues/3724));
- a recurring run on each release candidate, with the page flagging a metric that moved past a set fraction of the previous release.

Each limit the data shows below a realistic fleet size gets an issue, linked from the Known Issues table.

# Answers for a default install

This section answers the two questions in Delivery / Success Criteria for a default install: the operator's default resources, on a Standard host cluster, in mode `next`, with image `eeb8a95c`. The test fleet's clusters are small: 2 to 4 nodes and about 300 objects each. The numbers come from the Phase 1, 2 and 3 records, through the same model as the Harness limits page and `python3 -m admin_console.estimate`, which answer for other fleets and other limits. Where a measured run disagrees with the model's line, this section says so and gives both.

The default resources, read from the install's pods:

| Container                        | Request           | Limit          | Set by                                      |
| -------------------------------- | ----------------- | -------------- | ------------------------------------------- |
| Agent (`platform-agent`)         | 1 CPU, 2 GiB      | 3 CPU, 8 GiB   | `spec.deployment.resources`                 |
| Credential proxy                 | 0.5 CPU, 1 GiB    | 1 CPU, 2 GiB   | `spec.deployment.credentialProxy.resources` |
| Event watcher (`agent-api-auth`) | 0.15 CPU, 384 MiB | 1 CPU, 2 GiB   | `spec.deployment.agentAPIAuth.resources`    |
| Sandbox shell                    | 0.2 CPU, 512 MiB  | 2 CPU, 2 GiB   | no field: fixed in the operator             |
| LiteLLM, 2 replicas              | 0.1 CPU, 2 GiB    | 0.5 CPU, 3 GiB | Helm value `litellm.resources`              |

## Question 1: are the default limits enough for this fleet?

For a fleet of small clusters like the test fleet, the credential proxy is the bottleneck. Commands wait for it from 4 clusters when the first-run audits run at once, and from 8 clusters when they run one after another, as a new install runs them. At about 16 to 22 clusters the waits are between 30 and 60 seconds, next to the 60-second refusal bound, and its memory nears its 2 GiB limit: the model's line says at about 15 clusters, the measured runs at about 25. No other container reaches its limit in the measured range, except the sandbox shell under 4 or more commands at once and in a session storm.

| Limit                               | When the fleet reaches it                                                                                                                                                                                                                                                                                                                                                                  | What happens                                                                                                                                |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Credential proxy CPU, 1 core        | During onboarding, at 6, 8 and 22 clusters, though not at 16 (one run each). With the first-run audits running at once, at every size from 2 clusters; with them one after another, at about 8 clusters.                                                                                                                                                                                   | Commands wait for the proxy: 15–30 s at 4 and 6 clusters and 5–10 s at 8 with the audits at once; 10–15 s at 8 with them one after another. |
| Credential proxy refusal, 60 s wait | The longest waits were between 30 and 60 s at 16 clusters with the audits at once, and at 22 clusters with them one after another.                                                                                                                                                                                                                                                         | A command that waits 60 s is refused. No command was refused yet at 16 or 22 clusters.                                                      |
| Credential proxy memory, 2 GiB      | 1.5 GiB at 16 clusters and 1.8 GiB at 22. The model's line reaches 2 GiB at about 15 clusters but over-predicts those two runs; continuing the runs from 8 to 22 clusters reaches it at about 25.                                                                                                                                                                                          | An OOMKill of the proxy stops every brokered command until it restarts.                                                                     |
| Sandbox shell CPU, 2 cores          | With 4 or more brokered commands at once, at any fleet size, and at 8 clusters × 4,000 objects during the session storm (2.3 cores). In the 2-hour onboarding windows it stayed at 0.8 to 0.9 cores at 2, 8 and 22 clusters, and in the 30-minute windows between 0.3 and 1.0. The model's line, from the Phase 1 upgrade readiness runs at 2 and 8 clusters, reaches 2 cores at about 15. | The command path carries at most about 1.5 commands per second.                                                                             |
| Agent CPU, 3 cores                  | The line reaches it at about 28 clusters.                                                                                                                                                                                                                                                                                                                                                  | Slower turns.                                                                                                                               |
| Agent memory, 8 GiB                 | 2.3 GiB at 16 clusters; the line reaches it at about 80 clusters.                                                                                                                                                                                                                                                                                                                          | OOMKill.                                                                                                                                    |
| Event watcher memory, 2 GiB         | 0.2 GiB at 22 clusters; far beyond the measured range.                                                                                                                                                                                                                                                                                                                                     | —                                                                                                                                           |

Limits that no container resource moves:

- **Response time.** A fan-out question took a median of 134 to 144 seconds at every size from 2 to 16 clusters (single runs 101 to 199 s), against the 60-second investigate gate ([#3140](https://github.com/gke-labs/kube-agents/issues/3140)).
- **Run slots.** The agent runs 10 conversations at once. With 10 questions sent together all were answered; with 12, two were refused in every run ([#3724](https://github.com/gke-labs/kube-agents/issues/3724)).
- **The command path.** A brokered command costs about 0.8 to 1 s of sandbox shell CPU when one runs at a time, most of it compiling the Python standard library ([#4027](https://github.com/gke-labs/kube-agents/issues/4027)). The shell's 2 cores are fixed, and the proxy runs at most 8 commands at once, also fixed.
- **Stall Watch.** It reads every cluster one at a time every 30 minutes, within a 25-minute budget. At 22 clusters, during onboarding, it read 19 clusters in one run.
- **Warning events.** The event watcher opens a session for each new incident, and Pods or HPAs that keep failing make many incidents. Sessions start between 10 and 30 Pods failing at once. With 4,800 HPAs and about 11,000 Pods in an 8-cluster fleet and nothing made to fail, it opened 7,483 sessions in an hour; the daily alert ceiling suppressed all of them, but that hour still used 3.2 million input tokens ([#3567](https://github.com/gke-labs/kube-agents/issues/3567)).
- **Model quota.** The onboarding scan used 7 to 13 million input tokens in its first 30 minutes at 2 to 16 clusters, and the 2-hour onboarding window 9 to 22 million.

On GKE Autopilot without room to burst, each container gets only its request. In the measured runs the agent passes its 2 GiB request at about 8 clusters (2.1 GiB), and the credential proxy its 1 GiB and the shell its 512 MiB between 8 and 16 clusters (at most 0.9 GiB and 0.5 GiB up to 8 clusters, 1.5 GiB and 0.8 GiB at 16). The model's lines say earlier: the proxy at about 3 clusters, the agent at about 6 and the shell at about 7; the proxy's line starts from one 2-cluster run at 926 MiB, above every cluster-count run at 4 to 8 clusters. Each container risks an OOMKill where it passes its request. Their CPU is above its request already at 2 clusters, so they run slower than on a Standard node from the start.

## Question 2: what node size and container limits does this fleet need?

Each limit below is the peak the data shows or predicts, plus the 30% margin of Decision 2. Fleets past 22 clusters are predicted, not measured.

| Fleet                  | Agent: memory, CPU | Credential proxy: memory, CPU | Node pool          |
| ---------------------- | ------------------ | ----------------------------- | ------------------ |
| 8 clusters             | 2.8 GiB, 2.2 CPU   | 1.8 GiB, 2.6 CPU              | 2 × e2-standard-4  |
| 16 clusters            | 3.7 GiB, 2.9 CPU   | 2.7 GiB, 3.4 CPU              | 3 × e2-standard-4  |
| 32 clusters, predicted | 5.4 GiB, 4.2 CPU   | 4.5 GiB, 5.0 CPU              | 2 × e2-standard-8  |
| 64 clusters, predicted | 8.9 GiB, 6.9 CPU   | 8.0 GiB, 8.1 CPU              | 2 × n2-standard-16 |

- **Credential proxy CPU** comes from the runs with the proxy's limits raised to 4 CPU and 4 GiB. Every audit run at the default limit sat at 1 core, so it shows the limit, not the need. With the four audits at once the proxy needed 1.6 cores at 2 clusters and 2.5 at 16. The rows above start from 1.6 cores at 2 clusters and add about 0.07 cores per cluster, the growth of the raised-limit onboarding window, plus small terms for the Pod churn and failing Pods that grow with the fleet. With the audits one after another, as a new install runs them, the proxy still sat at its 1-core limit from 8 clusters (Phase 1), so its need there is not measured, and these rows are the safe side.
- **The event watcher and LiteLLM** keep their defaults at every size here: the event watcher would use under 0.5 GiB at 64 clusters.
- **The sandbox shell's** 2 CPU and 2 GiB are fixed, and nothing in the `PlatformAgent` raises them. In the default-limit 30-minute windows it used 0.3 to 1.0 CPU and at most 0.8 GiB; at 16 clusters, once the proxy had the CPU it needed, it used 1.3 CPU. It reaches its 2 cores under 4 or more commands at once and in a session storm. The node pool counts it at the model's line for it, which is higher than these measurements, so the pool sizes are on the safe side.
- **The node pool** holds every long-running pod at the limits above, from the machine types in Decision 3. On Autopilot, set the requests to these values instead.
- **Where to set them.** On a Helm install the chart forwards `platformAgent.deployment.credentialProxy.resources` and `platformAgent.deployment.agentAPIAuth.resources` to the `PlatformAgent`; the agent container's `spec.deployment.resources` has no Helm value, so it is set on the `PlatformAgent` itself.

For 16 clusters, in the `PlatformAgent`. `resources` replaces the agent container's whole block, so it keeps the default requests; `credentialProxy.resources` replaces only the keys it sets:

```yaml
spec:
  deployment:
    resources:
      requests: { cpu: "1", memory: 2Gi }
      limits: { cpu: "3", memory: 4Gi }
    credentialProxy:
      resources:
        limits: { cpu: "3500m", memory: 2816Mi }
```

What no setting changes, and what it would take:

- The shell's 2 cores and the proxy's 8 command slots are fixed in the operator. Once the proxy has the CPU it needs, more commands reach the shell (1.3 cores at 16 clusters), and under 4 or more commands at once the shell is at its 2 cores. Past that the brokered command path needs code changes: a compiled standard library in the sandbox ([#4027](https://github.com/gke-labs/kube-agents/issues/4027)), a slot count that follows the proxy's limits, or more than one proxy replica ([#2637](https://github.com/gke-labs/kube-agents/issues/2637)).
- The 10 run slots, Stall Watch's per-cluster loop and the session per incident are code, not resources.
- The data volume: the agent's profiles used under 200 MiB at 22 clusters, so the default 50 GiB volume is not a limit in this range ([#3704](https://github.com/gke-labs/kube-agents/issues/3704) estimates about 3,200 clusters).

## The largest example fleet: about 2,500 clusters

The Harness limits page's example fleets are sizes built from customer fleets, rounded and without names: S (4 clusters), M (45), L (250) and XL (2,500 clusters, about 2,400 projects, 90% on Autopilot, about 260,000 workloads a day). Phase 4 measured what an onboarded install runs every day at 2 to 40 clusters, with the agent's limits raised to 6 CPU and 12 GiB and the credential proxy's to 4 CPU and 4 GiB, so the peaks show the need. Each level ran a 10-minute window that starts Stall Watch, one daily audit (`compliance-audit`) and two fan-out questions; the test clusters had 2 nodes and 100 objects each, and one point had 1,000 objects each. One run per point, so a point has no spread.

| Clusters           | Agent CPU, memory | Proxy CPU | Shell CPU | Longest command wait | Stall Watch pass         | One daily audit     | Fan-out question |
| ------------------ | ----------------- | --------- | --------- | -------------------- | ------------------------ | ------------------- | ---------------- |
| 2                  | 0.36, 934 MiB     | 0.37      | 0.79      | under 0.1 s          | 2.0 min                  | 2.6 min             | —                |
| 8                  | 0.55, 533 MiB     | 0.24      | 0.81      | under 0.1 s          | 7.0 min                  | 32 s (see below)    | —                |
| 16                 | 0.43, 910 MiB     | 0.56      | 0.83      | under 0.1 s          | 13.6 min                 | 36 s (see below)    | —                |
| 24                 | 1.07, 1,743 MiB   | 1.09      | 0.82      | 30–60 s (IAM outage) | 25 min, its whole budget | failed (IAM outage) | 239–291 s        |
| 32                 | 0.64, 1,778 MiB   | 1.13      | 0.81      | 15–30 s              | —                        | 39 min              | 101–127 s        |
| 40                 | 2.36, 3,146 MiB   | 2.45      | 1.58      | 2.5–5 s              | —                        | 34 min              | 129–375 s        |
| 40 × 1,000 objects | 0.90, 1,766 MiB   | 2.33      | 1.48      | 2.5–5 s              | —                        | 45 min              | 137–157 s        |

The data shows four things.

- **One install holds 40 clusters of daily work.** Every container stayed inside its limit, no command was refused, and the shell peaked at 1.58 of its fixed 2 cores. The model's line for the shell, from the Phase 1 upgrade readiness runs (a report a user starts), reaches 2 cores at about 15 clusters, so that report is what loads the shell; at 40 clusters it would run slower, not fail.
- **Stall Watch reads about 52 seconds per cluster,** one cluster at a time, within a 25-minute budget each half hour. It covers about 28 clusters per pass; at 40 clusters each cluster is read about once an hour.
- **A full daily audit takes about 51 to 67 seconds per cluster** at 32 and 40 clusters. At 8 and 16 clusters the audit completed in about 30 seconds with almost no model tokens; the cron log shows no error, and whether it read every cluster is unknown, so those two times are not used.
- **Model tokens grow fastest.** One daily audit used 3.1 to 13.9 million input tokens at 32 and 40 clusters, about 0.1 to 0.35 million per cluster.

For the XL fleet, with today's code:

| Clusters per install | Installs  | Nodes (3 × e2-standard-4 each)         | Stall Watch reads each cluster | Fits in one install's measured range |
| -------------------- | --------- | -------------------------------------- | ------------------------------ | ------------------------------------ |
| 40                   | 63        | about 190 (760 vCPU)                   | about once an hour             | yes, at the top of it                |
| 24 to 28             | 90 to 105 | about 270 to 320 (1,080 to 1,260 vCPU) | every 30 minutes               | yes                                  |

Each install sets the agent to about 3 CPU and 4 GiB and the credential proxy to about 3.5 CPU and 1 GiB, the 40-cluster peaks plus the 30% margin; the other containers keep their defaults. The delays, per install: a fan-out question takes about 2 to 6 minutes, a brokered command waits up to 5 to 30 seconds, and one daily audit takes 34 to 45 minutes, so the six daily audits take about 3.5 to 4.5 hours of each day. Across the fleet, the daily audits alone use about 1.5 to 5 billion input tokens a day (6 audits × 2,500 clusters × 0.1 to 0.35 million), before questions and triage; that needs a model quota and budget of its own.

The XL fleet is supportable this way, with three limits. Each install answers only for its own clusters: a question across all 2,500 clusters needs a layer above the installs that does not exist. The test clusters are small, and the XL fleet's are not, so per-cluster costs are a lower bound and the upper end of each range is the safer plan. The model's own lines, which take each input's largest increase across all operations, give about 15 clusters per install, or about 170 installs; the measured daily work says up to 40.

# Phase 1 results

The Phase 1 records are in `bench/scale/harness-limits.json` (46 runs, image `eeb8a95c`, mode `next`, 2 test clusters at the base); the admin console's Harness limits page charts them and answers the two sizing questions. This section states what they show; the page and the file hold every number.

## What the data shows

- **No container reached its memory limit, and none restarted or was OOM-killed.** The largest memory peaks were the agent container (`platform-agent`) at about 2.1 GiB of 8 GiB in the 8-cluster onboarding window, and the credential proxy at about 710 MiB of 2 GiB, in that window and when the agent read a namespace of 4,000 Pods. The event watcher's container (`agent-api-auth`) stayed between 125 and 181 MiB of 2 GiB at every point.
- **The credential proxy reaches its CPU limit and starts to queue at 8 clusters.** In the 8-cluster onboarding window it peaked at 1.05 of its 1-core limit, its admission wait reached the 10–15 s histogram bucket (at most 0.1 s at every other measured point), and its memory reached 712 MiB, against 0.61 cores and 536 MiB in the 2-cluster onboarding window. It reached 1.06 cores in one 8-cluster upgrade readiness run and 0.94 in another ([#2637](https://github.com/gke-labs/kube-agents/issues/2637)). The sandbox shell container reached 1.9–1.95 of its 2 cores in the 4,000-object onboarding window and the failing-Pods soak.
- **Warning events open sessions far faster than they become triages.** At the base the event watcher opened no session. With 100 nodes per cluster it opened 163 in an hour (GKE's own `netd` Pods failing on new nodes), with about 260 Pods created per minute 1,006, with 100 Pods failing at once 208, and while 4,000 listing Pods waited to be scheduled up to about 5,000 in half an hour. Across all 46 runs it opened 10,731 sessions for 8 triages: the daily alert ceiling suppressed 10,532, and each suppressed event still opened a session ([#3567](https://github.com/gke-labs/kube-agents/issues/3567)). The agent container's memory rose with the session count, from 556 MiB at the base soak to about 1.32–1.38 GiB at the churn and failing-Pods levels.
- **Every typical question is past the investigate gate already at 2 clusters.** A fan-out question took 140–146 s at 2 clusters and 101–173 s at 8; listing a 100-Pod namespace took 231–261 s, against the [#3140](https://github.com/gke-labs/kube-agents/issues/3140) gates (under 30 s OK, over 60 s investigate). With 4,000 Pods in one namespace, a fleet audit took 481–512 s and one run outlasted the 1,800 s delegation wait.
- **Fleet audit and upgrade readiness did not run as intended on this install.** Both can need a registered GitOps repository, which the experiment install deliberately has none of (Decision 7): of the runs whose answers were kept, 2 of 3 fleet audits at 8 clusters cancelled on a misread of the scope ([#3933](https://github.com/gke-labs/kube-agents/issues/3933)) and the third was declined for want of a repository, and one upgrade readiness run was declined. The records keep those runs, out of every fit; the base runs of both operations, and the first 4,000-object fleet audit, were written before answers were kept and are out of it too.
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

# Phase 2 and Phase 3 results

The Phase 2 and Phase 3 records are in `bench/scale/harness-limits.json` beside the Phase 1 records: the same image `eeb8a95c` and mode `next`. The Harness limits page charts them; this section states what they show.

## What the data shows

**The credential proxy is the first bottleneck, and the first-run audits are what reach it.** In the 30-minute windows (Phase 3, default limits):

| Clusters | Onboarding: proxy CPU peak | Audits at once: proxy CPU peak | Audits at once: longest wait | Audits at once: proxy memory peak | Onboarding: agent memory peak |
| -------- | -------------------------- | ------------------------------ | ---------------------------- | --------------------------------- | ----------------------------- |
| 2        | 0.65                       | 1.02 (at its limit)            | 0.1 s                        | 705 MiB                           | 1,484 MiB                     |
| 4        | 0.81                       | 0.97 (at its limit)            | 30 s                         | 689 MiB                           | 1,987 MiB                     |
| 6        | 1.00 (at its limit)        | 1.03 (at its limit)            | 30 s                         | 871 MiB                           | 2,005 MiB                     |
| 8        | 1.04 (at its limit)        | 1.12 (at its limit)            | 10 s                         | 496 MiB                           | 2,118 MiB                     |
| 16       | 0.75                       | 1.12 (at its limit)            | 60 s                         | 1,506 MiB                         | 2,316 MiB                     |

The 2-hour Phase 2 window at 22 clusters, with the audits one after another, showed the same bottleneck: the proxy at 1.11 cores, waits between 30 and 60 s and 1,779 MiB of memory, with 3,711 commands at 6.3 s each.

**With its limits raised, the proxy shows what it needs.** At 16 clusters, with 4 CPU and 4 GiB instead of 1 CPU and 2 GiB, the audit window peaked at 2.54 cores, the longest wait fell from 60 s to 10 s, the proxy ran 36 commands per minute at 3 s each instead of 22 at 17 s, and all four audits finished inside the window. The default-limit window lost two audits to an outside IAM failure at minute 20, so part of that difference in commands is fewer audits running. Its memory peak did not change (1,490 MiB against 1,506 MiB). In the onboarding window it peaked at 1.24 cores, and its memory stayed at 229 MiB against 874 MiB with the default limit: a proxy short of CPU keeps commands alive longer, and they hold memory.

**The work grows with the fleet even where the peak cannot.** The CPU the proxy used over a 30-minute window, divided by the window, went from 0.12 cores at 2 clusters to 0.35 at 8 and 0.47 at 16 (limits raised) in onboarding, and from 0.12 to 0.9 in the audit window. The peak stops at the limit; this average does not.

**The command path carries at most about 1.5 commands per second, and the sandbox shell sets that.** Workers in the shell ran `kubectl get pods --all-namespaces` through the proxy with no model call. One worker got 0.76 commands per second at 1.3 s each. At 4 workers it got 1.5 per second at 2.6 s each, with the shell's 1-minute CPU peak at 1.8 of its 2 cores; from 8 to 32 workers the shell was at its 2 cores, throughput stayed at 1.4 per second and each command took longer: 5.5 s at 8 workers, 11 s at 16 and 22 s at 32. The proxy stayed near 0.8 cores and its median wait at 0.1 s throughout. One command at a time costs about 0.8 to 1 s of shell CPU, most of it compiling the Python standard library; with a bytecode cache, measured in the shell, the client costs 0.39 s ([#4027](https://github.com/gke-labs/kube-agents/issues/4027)). Five of the nine runs at 4 to 16 workers shared the broker with other commands (more brokered commands than the workers sent) and are left out of these numbers.

**When the requests arrive together, run slots stop at exactly 10.** With 8 or 10 questions sent at once, every one was answered in all 3 runs (median 130–137 s). With 12, each run had exactly 2 refused with `HTTP 429` (`Too many concurrent runs`). In Phase 1, one of three runs with 15 at once had none refused.

**Fan-out questions take the same time from 2 to 16 clusters:** a median of 134 to 144 seconds.

**The session storm starts between 10 and 30 Pods failing at once.** In an hour the event watcher opened no session at 1 and 10 failing Pods, 3 at 30 and 208 at 100 (Phase 1). At 10, 30 and 100 failing Pods, a restart of the gateway opened 0, 4 to 7 and 63 to 68 sessions in the next 15 minutes. At 8 clusters × 4,000 objects (4,808 HPAs and about 11,000 Pods) it opened 7,483 sessions in an hour with nothing made to fail; the daily alert ceiling suppressed every one, and the hour still used 3.2 million input tokens and 1,848 brokered commands ([#3567](https://github.com/gke-labs/kube-agents/issues/3567)).

**The factors add up within about 15% for memory.** At 8 clusters × 4,000 objects the agent's memory peak was 1.15 times the sum of the single-factor lines (2,188 MiB against 1,898 MiB), the credential proxy's 1.02, the shell's 0.92 and the event watcher's 0.91, against the model's prediction for the same fleet from every run in the file; the numbers move as runs are added. The proxy's CPU sat at its limit, and the shell's reached 2.3 cores in that point's session storm, 2.1 times the sum.

**Stall Watch runs out of time at about 20 clusters during onboarding.** It reads every cluster one at a time every 30 minutes, within a 25-minute budget; at 22 clusters it read 19 in one run and continued from there in the next.

## Findings filed

- [#4027](https://github.com/gke-labs/kube-agents/issues/4027): every brokered command recompiles the Python standard library in the sandbox shell's client, which caps the command path at about 1.5 commands per second.
- [#4106](https://github.com/gke-labs/kube-agents/issues/4106): the credential proxy reaches its default 1-core CPU limit at every size when the first-run audits run at once, and in onboarding at 6, 8 and 22 clusters; at 16 clusters it needs about 2.5 cores.
- The measured session storms and run-slot refusals are added to [#3567](https://github.com/gke-labs/kube-agents/issues/3567) and [#3724](https://github.com/gke-labs/kube-agents/issues/3724).

## What limits the result

- **22 clusters at most.** The project's default network has room for the Pod ranges of about 30 clusters, and the zone ran out of E2 and N2 machines for hours. Larger fleets are predicted, not measured; a queue grows faster than a straight line once it fills.
- **Two methods.** The 22-cluster point is a 2-hour window with the audits one after another; the Phase 3 points are 30-minute windows with the audits at once, which is harder on the proxy. A plan error deleted the 22-cluster fleet before Phase 3 reached it.
- **The audit window is a worst case.** Four audits at once is more than a new install runs. On the measured build only four of OOBE's five first-run audits exist: `upgrade-retrospective` is not on that build's Platform Agent roster.
- **Small clusters, 2 nodes each.** Every Phase 3 point and the 22-cluster point have 2 nodes per cluster and no fixture pool. The Phase 1 points and the earlier Phase 2 points have a fixture pool beside the 2 default nodes: 3 or 4 nodes per cluster at the base, more at the node and object levels.
- **One run per onboarding and audit window,** so those points have no spread. The proxy's onboarding peaks move up and down between neighbouring points: its memory between 297 and 926 MiB from 2 to 16 clusters.
- **An outside IAM automation** removed the install's bindings again during the 16-cluster audit window. Two audits failed on a model call 20 minutes in, after the window's peaks, and the obtainability audit ran on into the next two fan-out runs, which are left out of the resource fits. At 6 clusters one audit was skipped, so that window ran three.
- **Runs that shared the broker are left out of the resource fits.** Stall Watch reads every cluster through the broker at :00 and :30, so the 13 short prompt runs and 8 boot-burst windows that started inside one of its reads or held a tick are left out, as are the proxy load runs that had commands from outside the load. The length of a Stall Watch read (2 minutes plus 30 seconds per cluster) is set from the short runs on either side of a tick, not measured directly.
- **A CPU line leaves out runs at the limit,** so where only low runs stay in, it can predict too little. The proxy's CPU answer uses the raised-limit runs for that reason.
- **The raised-limit runs met model quota limits:** 54 provider 429s in the 16-cluster onboarding window.

# Data to collect next

The answers above rest on one run per point, small test clusters and fleets of at most 40 clusters. The next measurements, in the order they would change the answers most:

- **Repeat runs at each point.** One run per point gives no spread: the two fan-out runs at 40 clusters took 129 and 375 seconds. Three runs per point at 8, 16, 24, 32 and 40 clusters would give the spread the 30% margin now stands in for.
- **Larger clusters.** Every Phase 4 cluster had 2 nodes and 100 objects, and one point had 1,000. A grid of clusters (8, 16, 32, 40) × nodes per cluster (2, 10, 50) × objects per cluster (100, 1,000, 4,000) would show how much a real fleet's clusters add, which now makes each per-cluster cost a lower bound.
- **Past 40 clusters.** The shell peaked at 79% of its fixed 2 CPU at 40 clusters, so the size where one install stops is between 40 and about 50, and it is not measured. Runs at 48, 56 and 64 clusters would find it; with an operator change that makes the shell settable, they would also find the next limit.
- **Stall Watch at every size.** A triggered pass finished only at 2 and 8 clusters; at 16, 32 and 40 it met a scheduled pass and was skipped. Starting the window away from the half hour would measure the pass time at those sizes.
- **The daily audit at 8 and 16 clusters.** It finished in about 30 seconds with almost no model tokens, against 34 to 45 minutes at 32 and 40; why is not known.
- **A day of model tokens.** The token figures here come from single windows. A 24-hour soak at 16 and 40 clusters would measure the daily total that the 1.5 to 5 billion tokens a day for the largest example fleet is extrapolated from.
- **An Autopilot host.** At the S container size the shell gets its 200m request on GKE Autopilot without bursting, against a 1.6-CPU peak. An install on Autopilot would show how slow the command path gets there.
- **Many projects.** The largest example fleet has about 2,400 projects; the test fleet had one. The Cluster Agent reconcile lists every project in scope, so its time at hundreds of projects is the remaining unmeasured input.

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

| Issue                                                        | Bottleneck                                                                                                                               | Scale                                   | Evidence                                                                                                                                    |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| [#2324](https://github.com/gke-labs/kube-agents/issues/2324) | 8 concurrent `gcloud` children exceed the broker's memory limit                                                                          | 143 clusters, 36 projects               | The Stall Watch cron fans out 8 at once; `memory.peak` 948MB against 1Gi                                                                    |
| [#2045](https://github.com/gke-labs/kube-agents/issues/2045) | `kubectl` children are capped by count, not by size                                                                                      | 4,000 pods                              | One `-o yaml` listing takes 1,281 MiB; the cgroup v2 group OOM killer stops the whole container                                             |
| [#2652](https://github.com/gke-labs/kube-agents/issues/2652) | An audit retries large-cluster reads into the proxy it crashed; `collect.py` runs 8-wide against 4 admission slots                       | 7-cluster audit on the 26-cluster fleet | Proxy at 827Mi, 6+ OOMKills; the audit never finishes                                                                                       |
| [#2637](https://github.com/gke-labs/kube-agents/issues/2637) | One replica handles all brokered execs; the admission budget turns the OOM into a queue                                                  | 143 clusters                            | ~6,000 execs/hour at the CPU limit; up to 1,225 queued requests, p50 wait 22.5s, max 56.7s; the broker refuses at 60s, the preflight at 15s |
| [#3142](https://github.com/gke-labs/kube-agents/issues/3142) | The agent reads CRDs one at a time through the broker                                                                                    | 233 Config Connector CRDs               | 769s at 3.3s per RPC; batched reads take 30s                                                                                                |
| [#4027](https://github.com/gke-labs/kube-agents/issues/4027) | Every brokered command recompiles the Python standard library in the sandbox shell's client                                              | any fleet                               | 0.85 s of shell CPU per command; the shell's 2-core limit caps the command path at about 1.5 commands per second                            |
| [#4106](https://github.com/gke-labs/kube-agents/issues/4106) | The default 1-core CPU limit is reached at every size with the first-run audits at once; they need 1.6 cores at 2 clusters and 2.5 at 16 | 2 to 22 test clusters                   | Waits of 30 to 60 s at 16 and 22 clusters; 1.8 GiB of memory at 22 against a 2 GiB limit                                                    |

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
