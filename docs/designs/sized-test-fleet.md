# A sized test fleet and its nightly run

**Status:** design, not built.

## High level summary

A test fleet is a set of GKE clusters that a kube-agents install watches during a test. This
design defines three fleet sizes, S, M and L, and a nightly run that builds one fleet, installs
kube-agents on it, and checks the result.

Size M has 250 nodes in 8 clusters. It has 8 node pools in total and 2,000 workloads. A workload is
one Deployment or one CronJob. The workloads use little CPU and memory; the test needs many of
them, not large ones. A quarter of them are CronJobs: each run starts a Pod that sends a few
requests to a service and stops. This gives a high rate of Pod churn. Pod churn is the creation and
deletion of Pods. Size L doubles each count of size M. Size S halves each count of size M. M is the
default size.

The nodes are real GKE nodes, not simulated nodes. The run builds size M on six nights a week and
size L on one night. An audit is a scheduled agent job that checks the fleet and writes a report.
Each run has two measurement windows:

- the burst window: about 1 hour from the install's start. The install does its first-run flow
  (OOBE, the out-of-box experience). Beside it, each audit that the first-run flow does not run
  also runs once.
- the soak window: the next 1 hour. No audit runs; the watchers run, and a few scripted chat
  questions are asked.

A run passes when no container crashes, every audit finishes, and the first-run flow finishes. Each
run writes logs of its cost and its resource use. A second design, the capability usage
attribution design, uses the same two windows to divide memory and model tokens among the agent's
capabilities.

## Why

The scale work in [`agent-harness-bottlenecks.md`](agent-harness-bottlenecks.md) measured limits
with one-off runs of the scale runner (`bench/kube_agents_bench/scale/`). Nothing measures them
again after a change merges. The two existing recurring checks do not cover the install:

- `scale-lane.yml` seeds one KWOK cluster nightly and measures two binaries, the event watcher
  and the k8s-api-proxy. It runs no agent.
- The eval nightlies grade agent behaviour on the small seeded fleet of the evaluation pool
  projects. That fleet has a few small clusters, and no fleet-scale load.

A crash, an audit that no longer finishes, or a first-run flow that stalls at fleet scale therefore
ships without a signal. [#2348](https://github.com/gke-labs/kube-agents/issues/2348) asks for a
nightly scale run over the pool projects; this design gives it a fixed, reproducible fleet instead.

## Fleet sizes

One table holds size M. S and L are derived from it: every count is multiplied by 0.5 or 2,
rounded up. The per-node and per-Pod shapes stay the same, so the density of each cluster does not
change between sizes; only the number of clusters, nodes and workloads does.

| Parameter                            | S     | **M (default)** | L     |
| ------------------------------------ | ----- | --------------- | ----- |
| Clusters                             | 4     | **8**           | 16    |
| Nodes, in total                      | 125   | **250**         | 500   |
| Node pools, in total (one a cluster) | 4     | **8**           | 16    |
| Workloads                            | 1,000 | **2,000**       | 4,000 |
| Long-running Deployments             | 750   | **1,500**       | 3,000 |
| Short-lifecycle CronJobs             | 250   | **500**         | 1,000 |
| Pods started a minute (churn)        | 50    | **100**         | 200   |
| Pods failing at any time             | 3     | **5**           | 10    |

The rules behind the numbers:

- **Node pools.** Each cluster has one node pool, so 8 node pools exist in total at M. A cluster
  is created with that pool as its only pool, so no default pool adds nodes outside the count. The
  250 nodes divide as 32, 32 and six pools of 31.
- **Many small workloads.** The test measures how the agent copes with the number of objects and
  their churn, not with heavy applications, so every container is small: a request of 10m CPU and
  16 MiB. At M, about 3,200 Pods run at once, about 13 a node.
- **Simple traffic.** Each Deployment has two replicas of a small HTTP server (`busybox httpd`)
  behind a Service. Each CronJob runs every 5 minutes; its Pod sends a few HTTP requests to one of
  its cluster's Services and exits, after 60 to 180 seconds. The traffic is light, but it is real:
  Pods start, serve, log and end, and Endpoints change.
- **Churn.** The CronJobs' start minutes are spread evenly over the 5 minutes (`0-59/5`, `1-59/5`,
  and so on), so 500 CronJobs give 100 Pod starts in every minute, about 12 a minute in each
  cluster. Five identical `*/5` schedules would start all 500 Pods in the same minute. The
  real-install test ran 3 a minute across 40 clusters for its soak
  (`bench/scale/real-install.yaml`) and about 800 a minute in a short stress stage
  (`bench/scale/real-install-results.md`); this design holds a rate about 30 times its soak rate
  for a whole run.
- **Lifetime is not doubled.** Doubling a lifetime would lower the churn. L doubles the number of
  CronJobs, which doubles the churn rate, and keeps each Pod's lifetime.
- **Failing Pods.** A few Pods fail on purpose (an image that does not exist, a command that exits
  1), so the audits and the stall watch have real findings. M has 5, as the real-install test did.
  At 20 failing Pods that test measured $69.5 of triage tokens in one hour, so the count stays low.
- **Machine type.** `n1-standard-1` (1 vCPU, 3.75 GB) for every node, at every size, so only the
  counts change between sizes. The workloads need little CPU, and N1 machines draw on the general
  CPU quota: 500 vCPU at L fits it, where 500 nodes of a 2-vCPU E2 type would need 1,000 vCPU of
  the separate, smaller E2 quota.

The sizes live in one file, `bench/scale/fleet-sizes.yaml`, as M's values and the two multipliers.
The runner reads the size from `SCALE_FLEET_SIZE` (`S`, `M` or `L`, default `M`).

## The install under test

One install on the scale host cluster, which is outside the fleet. It runs the build under test:
the nightly's images from `main`.

- **Container sizes.** The [fleet sizing page](../site/src/content/docs/deploy/fleet-sizing.md)
  picks a tier by the largest row any metric lands in. On the fleet-controllers row, S (1,000) and M
  (2,000) land in `medium` and L (4,000) in `fleet`, so S and M run the `medium` sizes and L the
  `fleet` sizes and host-cluster pools. The run tests the documented answer.
- **No GitOps repository and no chat channel.** Each audit then runs without a forge and keeps its
  findings in the report store ([`oobe.md`](oobe.md) section 4.4). It opens no pull request and
  writes no ledger issue. `KUBEAGENTS_AUDIT_AUTO_PROMOTE` is set to `false` as well. A reset install
  has no home channel, so no report reaches a chat; the verdict reads the run records and the
  report store instead. The scripted chat questions reach the agent through the eval harness, as
  the eval nightlies' questions do.
- **A reset before each run.** The install's state volumes are reset before the burst window, so
  the first-run flow starts as on a new install. The runner already does this reset (`reset_soak`).

## The nightly run

One run, in five steps:

| Step | What happens                                                                                       | Time      |
| ---- | -------------------------------------------------------------------------------------------------- | --------- |
| 1    | Delete any test cluster an earlier run left (by its label). Create the fleet in parallel.          | 30 – 45 m |
| 2    | Apply the workloads and start the churn. Wait until the Deployments are ready.                     | 15 m      |
| 3    | Reset the install's state and start it. **Burst window**: the first-run flow and the other audits. | about 1 h |
| 4    | **Soak window**: the watchers, the churn, and the scripted chat questions.                         | 1 h       |
| 5    | Write the verdict, the measurements and the cost record. Delete the fleet.                         | 15 m      |

About 3 to 3.5 hours in total at M. The churn runs from step 2 to step 5.

**The burst window.** The audits are the jobs in `agents/platform/cron/jobs.json` that carry the
`fleet-audit` skill, ten today. The first-run flow (`agents/chat/scripts/oobe.py`,
[`oobe.md`](oobe.md) section 4) runs the inventory scan, then five of them one after another:
`fleet-wide-cost-analysis`, `compliance-audit`, `obtainability-audit`, `stockout-prevention` and
`upgrade-retrospective`. The other five (`security-patch-orchestrator` and
`fleet-consistency-drift`, which are weekly, and three daily audits) wait for their schedules, so
the runner starts them: it marks one due every 5 minutes from 5 minutes after the install starts,
through the same `cron.jobs.trigger_job` call the first-run flow uses. Production staggers audits
30 minutes apart; a 5-minute stagger packs the same ten runs into the hour, so the burst is denser
than a real morning. The burst window therefore holds one run of all ten audits.

**The window's length.** The burst window is 1 hour nominal. It ends when the last of the ten
audits ends, but not before 1 hour and not after 3 hours from the install's start, so the soak
window that follows is quiet. Single audit runs on the CI install have reached 2,739 s (`oobe.py`,
`RUN_LIMIT_SECONDS`), so a slow first-run chain can pass 1 hour; at 40 clusters the real-install test
measured 63 minutes for the whole first-run flow. The burst window's real length is recorded with
the run. An audit that has not ended at 3 hours turns the verdict red.

**The soak window.** 1 hour, starting when the burst window ends. No audit is scheduled in it (see
the start time below), so it holds the install's quiet work: the script watchers on their schedules
(the stall watch every 30 minutes, the hourly ones), the event watcher, the churn, and the scripted
chat questions below.

**The scripted chat questions.** Three eval cases that need no planted fixture also run against
the sized fleet's install in the soak window, once each, through the eval harness:

| Case                                  | Question, in short                                                    |
| ------------------------------------- | --------------------------------------------------------------------- |
| `chat-routing-fleet-question`         | How many clusters are in the fleet, and which versions do they run.   |
| `agent-kanban-smoke`                  | Which GKE clusters run in this project now.                           |
| `upgrade-chat-request-runs-collector` | Whether the clusters are patched and set to stay current on upgrades. |

`upgrade-chat-request-runs-collector` checks the commands the agent runs, so it runs as it is. The
other two check that the answer names the pool's clusters (`seeded-a`, `seeded-b`, `seeded-c`), so
the lane runs a variant of each, `chat-routing-fleet-question-sized` and `agent-kanban-smoke-sized`,
whose checks name the sized fleet's clusters instead. The runner names a size's clusters the same
way every night (`scale-m-01` to `scale-m-08` at M), so a variant's names are fixed in its case file.
The originals stay on the eval nightlies; this lane is one more run of each question, at fleet scale. A file,
`hack/eval/sized-fleet-cases.txt`, lists them, so a case joins or leaves in one line. Their results
are eval records, in the eval dashboard under their own lane; they do not change the scale verdict.
They also give the capability usage attribution design a measured chat load.

**When it starts.** At 00:00 UTC. The daily audits run between 06:20 and 09:20 UTC, the weekly ones on Monday
mornings and on Sunday at 18:00 (`agents/platform/cron/jobs.json`), so a run that ends by about
04:00 UTC never meets a scheduled audit, and the soak window holds none. L runs on Wednesday, the
one night M does not, so the two never share the install or the project:

| Night | Sun | Mon | Tue | Wed | Thu | Fri | Sat |
| ----- | --- | --- | --- | --- | --- | --- | --- |
| Run   | M   | M   | M   | L   | M   | M   | M   |

S has no scheduled run; it is for a run by hand on a smaller budget.

**Where it runs.** A Prow periodic, beside the eval nightlies, in its own GCP project. The eval pool
projects cannot hold the fleet: L needs 16 clusters and 500 vCPU, and the pool's projects are
leased by the eval jobs. The periodic's configuration is in `oss-test-infra`, outside this
repository; this repository holds the script it runs, `hack/ci-scale-nightly.sh`. Every test
cluster carries the plan's `cluster_label`, and the runner creates and deletes no cluster without
it.

**Quota and network.** At L: 16 clusters, 500 nodes, 500 vCPU of the general CPU quota, 500 node IP
addresses. Each
cluster gets its own subnet and small Pod and Service ranges, as Phase 4 did
(`bench/scale/phase4.yaml`), so the project's default network does not cap the cluster count.

## The verdict

The run writes `scale-verdict.json`. It is red when any check fails:

| Check                   | Passes when                                                                                                                                                    |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No crash                | No install container restarts or is OOMKilled; no `SandboxChanged` event on an install Pod; no `OOMKilling` line on the host cluster's nodes, in both windows. |
| Every audit finishes    | Each of the ten audits has a run that ended without error within 3 hours of the install's start, with a report in the report store.                            |
| The first-run flow ends | The inventory scan settles and each audit in `FIRST_RUN_AUDITS` has an ended run within 3 hours of the install's start, as `oobe.py` records them.             |
| The run itself is sound | The fleet reached its size, the churn ran at its rate (within 10 %), and the teardown deleted every test cluster.                                              |

A red run goes to haoxuw, the owner of the `harness-scale` row in `docs/designs/domains.yaml`.
The last check keeps a broken fixture from reading as a pass or as a regression. Kubernetes keeps an
event for about an hour, so the runner reads `SandboxChanged` events and the nodes' `OOMKilling`
lines from Cloud Logging over the whole run, not from the API at the end. Restarts and OOMKilled
terminations come from the Pods' status, which the runner already collects.

## Logs and records

Each run writes three records to a results bucket, one folder a run, as the eval dashboards' data
does, and prints a one-line summary of each to the job's log:

- **The verdict:** `scale-verdict.json`, above.
- **The measurements:** container peaks and memory-time, broker waits, token counts, the burst
  window's real length. Each record's key gains the fleet size and the window, beside its commit,
  mode, factor, level, operation and run. `bench/scale/harness-limits.json` stays the file a person
  merges chosen runs into.
- **The cost record:** `scale-cost.json`, computed by the run's own code. The run reads no billing
  data: the project has no billing export, so the code measures the inputs and prices them.

  | Input                      | Measured from                                                                                | Priced from                                      |
  | -------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------ |
  | Node-hours                 | Each node's create and delete time, from the Compute Engine instances the run's clusters own | The machine type's on-demand price in the region |
  | Cluster-hours              | Each cluster's create and delete time, from its GKE operations                               | The GKE cluster fee                              |
  | Disk-hours                 | Each node's boot disk size and lifetime                                                      | The disk type's price                            |
  | Model tokens               | LiteLLM's token counters for each window, by model and kind (input, cached input, output)    | The model's price per token                      |
  | Model spend, a cross-check | LiteLLM's own spend counter for each window (`litellm_spend_metric`)                         | LiteLLM's price map                              |

  The infrastructure prices come from the Cloud Billing Catalog API, which publishes list prices
  by SKU and needs no billing account access; the run stores the prices it used and their date in
  the record. The model prices are a table in the repository, with its source and date. The record
  gives each input, its price and its cost, and their sum for the run, so a reader can check the
  arithmetic. When the run's token cost and LiteLLM's spend counter differ by more than 10 %, the
  record says so.

Every test cluster also carries two GCP resource labels, `kube-agents-scale-run=<run id>` and
`kube-agents-scale-size=<S, M or L>`. The run finds its own instances, disks and clusters by them,
and they keep any later billing data attributable to a run.

## Cost

List prices, which the cost record replaces with the Catalog API's figures from the first run: an
on-demand `n1-standard-1` node is about $0.048 an hour in us-central1, and a GKE cluster fee is $0.10
an hour.

| Size | Runs a month | Nodes | Hours a run | Node cost a run | Cluster fees a run | Infrastructure a month |
| ---- | ------------ | ----- | ----------- | --------------- | ------------------ | ---------------------- |
| M    | about 26     | 250   | about 3.5   | ≈ $42           | ≈ $3               | ≈ $1,150               |
| L    | about 4.3    | 500   | about 4     | ≈ $95           | ≈ $6               | ≈ $440                 |

Model tokens come on top. At 40 clusters the real-install test measured $111 of tokens in its first
5 hours; M has 8 clusters and a 2-hour run, so its tokens should cost much less. The cost record
gives the real figure for every run.

## What is built, in order

Each item is its own pull request after this design merges.

1. **Sizes and the fleet builder.** `bench/scale/fleet-sizes.yaml`; the runner creates each
   cluster with one node pool of `n1-standard-1` nodes and the two resource labels; `fixtures.py`
   gains the HTTP Deployments with their Services, the CronJob clients with spread start minutes,
   and the failing Pods. One live M run is the evidence.
2. **The run plan and the verdict.** `bench/scale/nightly.yaml` (the five steps and the two
   windows) and `bench/kube_agents_bench/scale/verdict.py`, with unit tests for each check. The
   runner already has `reset_soak` and `trigger_soak`, and in `operations.py` `trigger_jobs`,
   `job_statuses`, `job_seconds` and `unfinished_jobs`; this item reuses them. `trigger_jobs` marks
   every job at once, so the 5-minute stagger is new. The Cloud Logging reads for events and node
   OOM kills are new.
3. **The records.** The upload to the results bucket, the size and window in the record key, the
   cost record with its price table, and the billing-export query.
4. **The chat cases.** The two `-sized` variants, `hack/eval/sized-fleet-cases.txt`, and the soak
   window's call to the eval harness against the sized install.
5. **The nightly.** `hack/ci-scale-nightly.sh`, the GCP project's provisioning, and the periodic
   in `oss-test-infra`. The script always deletes the fleet, also when a step fails, and the sweep
   in step 1 deletes clusters an aborted run left.

## Open questions

- **The Prow job's time limit.** A run takes 3 to 4 hours, longer than most eval nightlies.
- **Typical runs.** The install is reset each night, so every audit run is a first run, which reads
  every cluster as new. A weekly run without the reset would measure typical runs.
