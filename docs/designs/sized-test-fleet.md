# A sized test fleet and its nightly run

**Status:** design, not built.

## High level summary

A test fleet is a set of GKE clusters that a kube-agents install watches during a test. This
design defines three fleet sizes, S, M and L, and a nightly run that builds one fleet, installs
kube-agents on it, and checks the result.

Size M has 250 nodes in 8 clusters. It has 8 node pools in total and 2,000 workloads. A workload is
one Deployment or one CronJob. Many workloads have a short lifecycle: their Pods start, run for one
to three minutes, and stop. This gives a high rate of Pod churn. Pod churn is the creation and
deletion of Pods. Size L doubles each count of size M. Size S halves each count of size M. M is the
default size.

The nodes are real GKE nodes, not simulated nodes. The run builds size M on five nights a week and
size L on one night a week. An audit is a scheduled agent job that checks the fleet and writes a
report. Each run has two measurement windows:

- the burst window: the first 4 hours after the install starts. The install does its first-run
  flow (OOBE, the out-of-box experience). Beside it, each audit that the first-run flow does not
  run also runs once, the weekly audits included.
- the soak window: the next 8 hours. The install does its normal daily work.

A run passes when no container crashes, every audit finishes, and the first-run flow finishes. The
run also records the existing bottleneck measurements. A second design, the capability usage
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

| Parameter                            | S          | **M (default)** | L          |
| ------------------------------------ | ---------- | --------------- | ---------- |
| Clusters                             | 4          | **8**           | 16         |
| Nodes, in total                      | 125        | **250**         | 500        |
| Node pools, in total (one a cluster) | 4          | **8**           | 16         |
| Workloads                            | 1,000      | **2,000**       | 4,000      |
| Long-running Deployments             | 750        | **1,500**       | 3,000      |
| Short-lifecycle CronJobs             | 250        | **500**         | 1,000      |
| Pods started a minute (churn)        | 50         | **100**         | 200        |
| Pods failing at any time             | 3          | **5**           | 10         |
| Pod lifetime of a CronJob run        | 60 – 180 s | **60 – 180 s**  | 60 – 180 s |

The rules behind the numbers:

- **Node pools.** Each cluster has one node pool, so 8 node pools exist in total at M. A cluster
  is created with that pool as its only pool, so no default pool adds nodes outside the count. The
  250 nodes divide as 32, 32 and six pools of 31.
- **Workloads.** 75 % are Deployments with two replicas, which run all the time. 25 % are CronJobs
  that run every 5 minutes. Each run is one Pod that lives 60 to 180 s, chosen at random, and then
  ends. The CronJobs' start minutes are spread evenly over the 5 minutes (`0-59/5`, `1-59/5`, and so
  on), so 500 CronJobs give 100 Pod starts in every minute, about 12 a minute in each cluster. Five
  identical `*/5` schedules would start all 500 Pods in the same minute.
- **Why this churn.** 100 Pod starts a minute, held for 12 hours, is the shape of a batch-heavy
  fleet: many short jobs on a fixed set of nodes. It is not a stress test. The real-install test
  ran 3 a minute across 40 clusters for its soak (`bench/scale/real-install.yaml`) and reached
  about 800 a minute in a short stress stage with no strain (`bench/scale/real-install-results.md`).
  This design holds a rate about 30 times its soak rate for a whole run.
- **Lifetime is not doubled.** Doubling a lifetime would lower the churn. L doubles the number of
  CronJobs, which doubles the churn rate, and keeps each Pod's lifetime.
- **Failing Pods.** A few Pods fail on purpose (an image that does not exist, a command that exits
  1), so the audits and the stall watch have real findings. M has 5, as the real-install test did.
  At 20 failing Pods that test measured $69.5 of triage tokens in one hour, so the count stays low.
- **Pod shape.** Each container is `busybox` that sleeps, with a request of 10m CPU and 16 MiB, so
  that Pods run as real containers, write logs and emit events. At M, about 3,200 Pods run at once,
  about 13 a node.
- **Machine type.** `e2-medium` (2 vCPU, 4 GB) for every node.

The sizes live in one file, `bench/scale/fleet-sizes.yaml`, as M's values and the two multipliers.
The runner reads the size from `SCALE_FLEET_SIZE` (`S`, `M` or `L`, default `M`).

## The install under test

One install on the scale host cluster, which is outside the fleet. It runs the build under test:
the nightly's images from `main`.

- **Container sizes.** The [fleet sizing page](../site/src/content/docs/deploy/fleet-sizing.md)
  picks a tier by the largest row any metric lands in. On the fleet-controllers row, S (1,000) and M
  (2,000) land in `medium` and L (4,000) in `fleet`, so S and M run the `medium` sizes and L the
  `fleet` sizes and host-cluster pools. The run tests the documented answer.
- **No GitOps repository and no chat.** Each audit then runs without a forge and keeps its
  findings in the report store ([`oobe.md`](oobe.md) section 4.4). It opens no pull request and
  writes no ledger issue. `KUBEAGENTS_AUDIT_AUTO_PROMOTE` is set to `false` as well. A reset install
  has no home channel, so no report reaches a chat; the verdict reads the run records and the
  report store instead.
- **A reset before each run.** The install's state volumes are reset before the burst window, so
  the first-run flow starts as on a new install. The runner already does this reset (`reset_soak`).

## The nightly run

One run, in five steps:

| Step | What happens                                                                                                       | Time       |
| ---- | ------------------------------------------------------------------------------------------------------------------ | ---------- |
| 1    | Delete any test cluster an earlier run left (by its label). Create the fleet in parallel.                          | 30 – 45 m  |
| 2    | Apply the workloads and start the churn. Wait until the Deployments are ready.                                     | 15 m       |
| 3    | Reset the install's state and start it. **Burst window**: the first-run flow, and beside it the other audits once. | 4 h        |
| 4    | **Soak window**: normal daily work: the script watchers, one set of daily audits, the churn.                       | 8 h        |
| 5    | Wait up to 1 h for an audit still running. Write the verdict and the measurements. Delete the fleet.               | ≤ 1 h 15 m |

About 14 to 15 hours in total. The churn runs from step 2 to step 5.

**What the burst window holds.** The audits are the jobs in `agents/platform/cron/jobs.json`
that carry the `fleet-audit` skill, ten today. The first-run flow (`agents/chat/scripts/oobe.py`,
[`oobe.md`](oobe.md) section 4) runs the inventory scan, then five of them one after another:
`fleet-wide-cost-analysis`, `compliance-audit`, `obtainability-audit`, `stockout-prevention` and
`upgrade-retrospective`. The other five wait for their schedules: `security-patch-orchestrator`
and `fleet-consistency-drift` are weekly, on Monday mornings, and three are daily. Production staggers the audits 30 minutes apart (06:20, 06:50 and so on),
so the runner starts those five the same way: it marks one due every 30 minutes, from 30 minutes
after the install starts, through the same `cron.jobs.trigger_job` call the first-run flow uses.
They run beside the first-run chain, as their schedules would if a new install met a Monday
morning. The burst window therefore holds one run of all ten audits, the four weekly ones among
them.

**Time in the burst window.** The first-run chain is the long part. Single audit runs on the CI
install have reached 2,739 s (`oobe.py`, `RUN_LIMIT_SECONDS`), and the inventory scan may wait out
its hand-off deadline plus 30 minutes before the first audit starts, so the chain can take close to
4 hours. At 40 clusters the real-install test measured 63 minutes for the whole first-run flow. The
window ends at 4 hours by the clock whatever is running; an audit still running then finishes and
counts as a burst-window run. The hard limit is 6 hours from the install's start, and the verdict
goes red only past it. The measured length of the burst is a recorded measurement, not a check.

**When it starts.** The daily audits run between 06:20 and 09:20 UTC. A start at 20:30 UTC puts the
burst window at about 21:30 to 01:30 and the soak window at about 01:30 to 09:30, so the soak window
holds one full set of daily audits, as a normal weekday does. The last daily audit starts at 09:20
and may run past the window's end; step 5 waits for it.

The three Monday jobs (`fleet-wide-cost-analysis`, `security-patch-orchestrator` and
`fleet-consistency-drift`) run in every burst window, so the soak window must not hold them again:
no run starts on Sunday. L runs on Wednesday, the one night M does not, so the two never share the
install or the project:

| Night | Sun | Mon | Tue | Wed | Thu | Fri | Sat |
| ----- | --- | --- | --- | --- | --- | --- | --- |
| Run   | —   | M   | M   | L   | M   | M   | M   |

A Friday start's soak window is Saturday morning. It holds the Saturday upgrade readiness review
(06:00) and its weekly brief (07:00), as a real Saturday does. The brief is not a `fleet-audit` job,
so the burst window does not run it. S has no scheduled run; it is for a run by hand on a smaller
budget.

**Where it runs.** A Prow periodic, beside the eval nightlies, in its own GCP project. The eval pool
projects cannot hold the fleet: L needs 16 clusters and 1,000 vCPU, and the pool's projects are
leased by the eval jobs. The periodic's configuration is in `oss-test-infra`, outside this
repository; this repository holds the script it runs, `hack/ci-scale-nightly.sh`. Every test
cluster carries the plan's `cluster_label`, and the runner creates and deletes no cluster without
it.

**Quota and network.** At L: 16 clusters, 500 nodes, 1,000 vCPU, 500 node IP addresses. Each
cluster gets its own subnet and small Pod and Service ranges, as Phase 4 did
(`bench/scale/phase4.yaml`), so the project's default network does not cap the cluster count.

## The verdict

The run writes `scale-verdict.json`. It is red when any check fails:

| Check                   | Passes when                                                                                                                                                                               |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| No crash                | No install container restarts or is OOMKilled; no `SandboxChanged` event on an install Pod; no `OOMKilling` line on the host cluster's nodes, in both windows.                            |
| Every audit finishes    | Each of the ten audits has a run that ended without error within 6 hours of the install's start, and each daily audit has one in the soak window, each with a report in the report store. |
| The first-run flow ends | The inventory scan settles and each audit in `FIRST_RUN_AUDITS` has an ended run within 6 hours of the install's start, as `oobe.py` records them.                                        |
| The run itself is sound | The fleet reached its size, the churn ran at its rate (within 10 %), and the teardown deleted every test cluster.                                                                         |

The last check keeps a broken fixture from reading as a pass or as a regression. Kubernetes keeps an
event for about an hour, so the runner reads `SandboxChanged` events and the nodes' `OOMKilling`
lines from Cloud Logging over the whole run, not from the API at the end. Restarts and OOMKilled
terminations come from the Pods' status, which the runner already collects.

**Where the results go.** The verdict and the measurements (container peaks, broker waits, token
counts) go to a results bucket, one folder a run, as the eval dashboards' data does. Each
measurement record's key gains the fleet size and the window, beside its commit, mode, factor,
level, operation and run. `bench/scale/harness-limits.json` stays the file a person merges chosen
runs into. The measurements do not decide the verdict; they are the trend the bottleneck work
reads.

Agent-behaviour checks stay eval cases. A case that needs the sized fleet names it as a fixture
role, `sized-fleet-m`, and runs in the soak window. The first candidate is a case parked today for
want of a fleet of large clusters.

## Cost

List prices, to check against the billing account before the first run: an on-demand `e2-medium`
node is about $0.034 an hour in us-central1, and a GKE cluster fee is $0.10 an hour. A run is about
14 hours.

| Size | Runs a month | Nodes | Node cost a run | Cluster fees a run | Infrastructure a run | Infrastructure a month |
| ---- | ------------ | ----- | --------------- | ------------------ | -------------------- | ---------------------- |
| M    | about 21.7   | 250   | ≈ $120          | ≈ $11              | ≈ $130               | ≈ $2,800               |
| L    | about 4.3    | 500   | ≈ $240          | ≈ $22              | ≈ $260               | ≈ $1,100               |

Model tokens are likely the larger cost. At 40 clusters the real-install test measured $111 of
tokens in its first 5 hours and $16.6 an hour after them, which is about $220 for one burst and one
soak window. M has 8 clusters and should use less; the capability usage attribution design measures
the real figure. Spot VMs would cut the node cost by about 60 % to 90 %. They are real nodes, but a
preemption deletes a node, which adds node churn that the size table does not ask for. The first
version uses on-demand nodes; Spot is an open question below.

## What is built, in order

Each item is its own pull request after this design merges.

1. **Sizes and the fleet builder.** `bench/scale/fleet-sizes.yaml`; the runner creates each
   cluster with one node pool of the right node count; `fixtures.py` gains the Deployment and
   CronJob mix with spread start minutes, and the failing Pods. One live M run is the evidence.
2. **The run plan and the verdict.** `bench/scale/nightly.yaml` (the five steps and the two
   windows) and `bench/kube_agents_bench/scale/verdict.py`, with unit tests for each check. The
   runner already has `reset_soak` and `trigger_soak`, and in `operations.py` `trigger_jobs`,
   `job_statuses`, `job_seconds` and `unfinished_jobs`; this item reuses them. `trigger_jobs` marks
   every job at once, so the 30-minute stagger is new. The Cloud Logging reads for
   events and node OOM kills are new.
3. **The results.** The upload to the results bucket, and the size and window in the record key.
4. **The nightly.** `hack/ci-scale-nightly.sh`, the GCP project's provisioning, and the periodic
   in `oss-test-infra`. The script always deletes the fleet, also when a step fails, and the sweep
   in step 1 deletes clusters an aborted run left.
5. **Eval cases on the fleet.** The `sized-fleet-m` fixture role and the first case.

## Open questions

- **Spot or on-demand nodes.** Spot saves most of the node cost and adds preemptions. The install
  runs on the host cluster, outside the fleet, so a preemption does not touch it.
- **The Prow job's time limit.** A run takes 14 to 15 hours, longer than the eval nightlies.
- **Who owns a red run.** `docs/designs/domains.yaml` has no row for fleet-scale harness behaviour;
  one is needed, with an owner and a backup.
