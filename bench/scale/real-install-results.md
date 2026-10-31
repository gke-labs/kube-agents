# Real-install test results: one install at the L container size

Status on 2026-10-31: Steps 1 to 4 are complete. Step 5 is in progress. Steps 6 to 8 are not
started. The plan is [`real-install-test-plan.md`](real-install-test-plan.md).

## High level summary

One install at the L container size holds the first-run flow of a 40-cluster fleet safely. The
first-run flow is the onboarding that a new install does by itself. No container restarted. No
container was killed for memory. The credential proxy refused no command. All 36 tasks of the
flow finished. The largest resource use was 63% of the sandbox shell's CPU limit. The other
containers used less than half of their limits.

The onboarding inventory report appeared 63 minutes after the install was ready. About 30 of
those minutes came from a test-environment fault: an external IAM process removed the install's
permissions, and one task had to be restarted. Without that fault, the report takes about 35
minutes.

Two limits are close at 40 clusters, and neither is a container size:

- Stall Watch takes 25 minutes to read 40 clusters, and it runs every 30 minutes. Stall Watch
  is the scheduled job that looks for stuck controllers. It is busy 84% of the time, so 40
  clusters is near its maximum.
- A heavy question, such as the upgrade readiness report, did not finish within the test
  harness's 30-minute wait for delegated work. The harness then stops the task; the product
  itself sets no such limit. The question ran at the same time as Stall Watch and 15 triage
  tasks. These are first results from Step 5, which is still in progress.

Model tokens are the largest running cost. The first five hours used 288 million input tokens, of
which 84% were cached. At the list price this is about $111, or about $22 per hour. One install
on GKE costs about $0.40 per hour. The 40-cluster test fleet costs about $15 per hour.

The onboarding report has two defects. It says "Scanned 6 clusters", but the agent examined all
40 clusters. The agent also could not register its findings, because the connection was refused.

## Expected conclusions

Each prediction comes from the first-run data above and the Phase 1 to 4 forecast. After the test,
the Result column says whether the data confirms the prediction.

| #   | Step | Prediction                                                                                                                                                                                                                                       | Basis                                                                                                                                                                                                                                  | Result  |
| --- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| 1   | 5    | No safe limit breaks when one question runs at a time.                                                                                                                                                                                           | First-run peaks are below half of every limit except the shell's CPU.                                                                                                                                                                  | Pending |
| 2   | 5    | An upgrade readiness report takes 35 to 60 minutes at 40 clusters, so it passes the harness's 30-minute wait. With the wait raised to 90 minutes, it finishes.                                                                                   | Run 1 took 24 minutes to list 40 clusters. Phase 4 measured 51 to 72 s per cluster for an audit.                                                                                                                                       | Pending |
| 2b  | 5    | The report's time is a fixed part plus a part per cluster. For 1 cluster it takes 5 to 12 minutes, for 10 clusters 15 to 25 minutes, for 42 clusters 40 to 60 minutes. The per-cluster part is 50 to 70 s.                                       | The fixed part is the skill read, the fleet listing and the answer. Phase 4 measured 51 to 72 s per cluster for an audit. The 1- and 10-cluster runs name their clusters in the prompt, so the install keeps all 42 clusters in scope. | Pending |
| 3   | 5    | The fleet health answer names the hot Deployment on `scale-07`, and the upgrade answer names `signal-old` as behind. The zone skew on `signal-skew` is found less reliably.                                                                      | Both are visible with one `kubectl top` or one cluster list. The skew needs Pod-to-zone joins.                                                                                                                                         | Pending |
| 4   | 5    | With 2 and 3 questions at once, containers stay inside their limits. The shell CPU reaches its 2-core limit, and each answer takes longer in proportion to the number of questions. At 3 at once, at least one answer passes the 30-minute wait. | All work goes through the one sandbox shell, which already peaked at 1.26 cores.                                                                                                                                                       | Pending |
| 5   | 6    | After an agent restart, the event watcher watches all clusters again within 10 to 15 minutes.                                                                                                                                                    | The runner's gateway restarts took about 11 minutes to settle.                                                                                                                                                                         | Pending |
| 6   | 6    | A window with 20 failing Pods does not start a session storm. The event watcher's quota holds new sessions, and the triage queue clears within 1 hour.                                                                                           | 15 triage tasks at 21:18 ran 2 to 3 at a time and cleared in 37 minutes.                                                                                                                                                               | Pending |
| 7   | 6    | Stall Watch reads every cluster once in each 30-minute cycle at 42 clusters, with less than 15% margin.                                                                                                                                          | 25 minutes per run at 40 clusters, about 37 s per cluster.                                                                                                                                                                             | Pending |
| 8   | 6    | The steady token cost after onboarding is $15 to $20 per hour, or $360 to $480 per day, and the agent's memory stays under 3 GB for 12 hours.                                                                                                    | $111 in the first 5 hours, which include the onboarding burst.                                                                                                                                                                         | Pending |
| 9   | 7    | High Pod churn does not harm the install. The test cluster cannot reach 3,000 Pods per minute before the install shows strain. The event watcher stays under 0.5 CPU and 1 GB.                                                                   | The event watcher reacts to warnings and failures, not to normal Pod creation.                                                                                                                                                         | Pending |
| 10  | 8    | At the S size the install fails. The first limit is CPU throttling of the credential proxy and the shell, seen as long command waits. Then the agent is killed for memory during onboarding.                                                     | At L, the proxy peaked at 3.45 cores (S gives 0.5), the shell at 1.26 cores (S gives 0.2), and the agent at 2.29 GB (S gives 2 GiB = 2.15 GB).                                                                                         | Pending |
| 11  | All  | One L install is enough for 40 clusters, and Stall Watch's cycle sets the maximum, not a container size.                                                                                                                                         | Every container peak at 40 clusters is below half of its L limit.                                                                                                                                                                      | Pending |

## Setup

| Item                             | Value                                                                                                                                     |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Build                            | `main` at `0108e59d`, `spec.mode: next`, model `vertex_ai/gemini-3.5-flash`                                                               |
| Install                          | New install with `install.sh`, no GitOps repository                                                                                       |
| Host cluster                     | `scale-host`, zonal Standard, 2 × e2-standard-4 (default and gVisor pools)                                                                |
| Agent                            | limit 8 CPU, 16 GB                                                                                                                        |
| Credential proxy                 | limit 8 CPU, 8 GB                                                                                                                         |
| Event watcher (`agent-api-auth`) | limit 2 CPU, 4 GB                                                                                                                         |
| Sandbox shell                    | 2 CPU, 2 GiB (fixed in the operator)                                                                                                      |
| Test fleet                       | 40 zonal clusters in one zone, 248 nodes, 17,385 Pods, 7,643 Deployments, 7,520 Services, 7,240 HPAs, 14,003 ConfigMaps, 1,683 namespaces |
| Load                             | 3 Pods created per minute per run (2.8 achieved), 5 failing Pods                                                                          |
| Planted signal                   | one hot Deployment (`payments/hot-api`, 6 busy Pods) on `scale-07`                                                                        |

The host nodes have 4 vCPU and 16 GB each. So the 8-CPU and 16-GB limits are ceilings that the
node cannot give in full. No container came near them.

The 40 test clusters cannot carry two planned signals. Zonal skew needs a cluster in more than one
zone, and an older GKE version needs a second release channel. Two signal clusters are added
during Step 5, outside the runner's set: `signal-old` on GKE 1.33 (extended channel, three minor
versions behind the fleet's 1.36) and `signal-skew`, a regional cluster with its load pinned to one
zone. The fleet is then 42 clusters. `upgrade_report` runs 1 and 2 ran before they existed.

## Steps 3 and 4: the first-run flow

The window ran from 16:06:45Z (install ready) to 21:08:15Z. The record is
`0108e59d|next|combined|1|first_run|1` in [`harness-limits.json`](harness-limits.json).

### Timeline

| Time (UTC)           | After T0       | Event                                                                                                              |
| -------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------ |
| 16:08:30 to 16:11:15 | +2 min         | Triage of the gateway Pod's startup `FailedScheduling` (volumes provisioning); no action needed                    |
| 16:09:15 to 16:15:46 | +2.5 min       | The `oobe` cron job runs once and creates the onboarding tasks                                                     |
| 16:11:16             | +4.5 min       | Cluster Agent reconcile: 40 cluster profiles                                                                       |
| 16:16:59             | +10 min        | Discovery task created on the Platform Agent                                                                       |
| 16:17:00             | +10 min        | 6 cluster inventory tasks created (`scale-01` to `scale-06`, the first-run cap)                                    |
| 16:32:16 to 16:52:48 | +25 to +46 min | Cluster inventory tasks finish (15 to 36 min each)                                                                 |
| 16:23 to 16:54       |                | IAM strip: model calls fail with 403; the discovery task blocks after 2 failures and is unblocked by hand at 16:54 |
| 16:30:25 to 16:55:30 |                | First Stall Watch run: 25 min for 40 clusters                                                                      |
| 16:55 to 17:09       |                | Stall Watch triage: 3 tasks, all finished                                                                          |
| 17:04:29             | +58 min        | Discovery task finishes: 40 clusters found, 34 audited directly, 13,331 findings                                   |
| 17:04:35 to 17:09:50 | +63 min        | Ranking task writes the onboarding inventory report (`INVENTORY.md`)                                               |
| 17:10:38 to 17:17:57 |                | Fleet cost analysis (7 min)                                                                                        |
| 17:19:41 to 17:34:20 |                | Compliance audit (15 min)                                                                                          |
| 17:35:47 to 17:43:21 |                | Obtainability audit (8 min)                                                                                        |
| 17:44:50 to 17:54:04 |                | Stockout prevention audit (9 min)                                                                                  |
| 17:55:54 to 18:12:34 |                | Upgrade retrospective (17 min)                                                                                     |
| 16:30 to 21:00       |                | Stall Watch, 10 runs, each 25 min                                                                                  |

All 36 tasks of the window finished.

### Safe limits

| Check                       | Result                            |
| --------------------------- | --------------------------------- |
| Restarts and OOMKills       | 0 in every container              |
| Brokered commands refused   | 0 of 28,950                       |
| Command wait                | median 0.1 s, max 5 s             |
| Run-slot refusals           | 0                                 |
| Failed prompts, lost triage | 0, 0                              |
| Model 429s                  | 12, all retried without lost work |
| Every stage finished        | yes                               |

### Peak resource use

| Container           | Memory peak / limit     | CPU peak / limit | CPU-seconds |
| ------------------- | ----------------------- | ---------------- | ----------- |
| Agent               | 2.29 GB / 16 GB (14%)   | 1.39 / 8 (17%)   | 5,265       |
| Credential proxy    | 1.02 GB / 8 GB (13%)    | 3.45 / 8 (43%)   | 17,728      |
| Sandbox shell       | 0.49 GB / 2.15 GB (23%) | 1.26 / 2 (63%)   | 9,094       |
| Event watcher       | 0.15 GB / 4 GB (4%)     | 0.02 / 2 (1%)    | 17          |
| LiteLLM (each of 2) | 0.71 GB / 3.2 GB        | 0.07 / 0.5       | 442         |

The data volume used 0.28 GB of 52.5 GB. The largest profile was 21.5 MB.

### Model and tool traffic

| Measure                          | Value                                               |
| -------------------------------- | --------------------------------------------------- |
| Input tokens                     | 287.6 M (242.4 M cached, 84%)                       |
| Output tokens                    | 0.79 M                                              |
| Model turns (assistant messages) | 3,593                                               |
| Sessions                         | 75                                                  |
| Tool calls                       | 3,530, 11.1 MB returned                             |
| Event watcher                    | 816 events seen (2.7 per minute), 1 session created |

| Tool                      | Calls             | Median result | 95th percentile | Max     |
| ------------------------- | ----------------- | ------------- | --------------- | ------- |
| `terminal`                | 2,935             | 408 B         | 12.7 KB         | 58 KB   |
| `write_file`              | 110               | 261 B         | 337 B           | 351 B   |
| `search_files`            | 97                | 87 B          | 4.1 KB          | 4.8 KB  |
| `read_file`               | 95                | 5.3 KB        | 101 KB          | 106 KB  |
| `skill_view`              | 54                | 11.3 KB       | 11.3 KB         | 14.2 KB |
| `kanban_show`             | 52                | 13.6 KB       | 26.4 KB         | 28.8 KB |
| `mcp__gke__get_cluster`   | 10 (first 74 min) | 16 KB         | 16 KB           | 16 KB   |
| `mcp__gke__list_clusters` | 1 (first 74 min)  | 38.8 KB       |                 |         |

The session store truncates a tool result at its own limit, so a size is the stored size.

### Verdict

Safe. Every check passed, and the first-run flow finished without intervention by the install.
The one manual step, the unblock at 16:54, repaired a fault in the test environment.

### Findings

1. **The onboarding report under-counts.** `INVENTORY.md` says "Scanned 6 clusters and 183
   workloads". The discovery task audited the other 34 clusters, but the report covers only the 6
   clusters with Cluster Agents.
2. **Findings registration failed.** The ranking task got `Connection refused` (exit 13) for every
   cluster and wrote the scores to `INVENTORY.scores.json` instead.
3. **The event watcher started with an empty watch list.** The runner found that it watched no
   cluster and restarted the gateway before T0. After the restart it watched all 40.
4. **Stall Watch is near its cycle.** 25 minutes per run in a 30-minute cycle, about 37 s per
   cluster. This agrees with the Phase 4 forecast of about 25 clusters for a half-hourly Stall
   Watch with margin.

## Step 5: heavy questions (in progress)

Each run sends the question and then a follow-up. The plan is `real-install.yaml`, operations
`upgrade_report` and `fleet_health`, 3 runs each.

| Run                                          | Started   | Result                                                                                                                                                                              |
| -------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `upgrade_report` #1                          | 21:31:32Z | Not finished. The delegated task ran past the harness's 1,800 s delegation wait (`AGENT_DELEGATION_TIMEOUT`), the harness archived it, and the answer was the acknowledgement only. |
| `upgrade_report` #2                          | 22:03:37Z | Running                                                                                                                                                                             |
| `upgrade_report` #3, `fleet_health` #1 to #3 |           | Pending                                                                                                                                                                             |

In run 1 the delegated task took 24 minutes to list the 40 clusters before it started the
readiness script. In the same minutes the agent ran 15 event-watcher triage tasks (created at
21:18 after the runner restarted the gateway) and one Stall Watch run. These all share the one
sandbox shell. The cause of the slow listing is not confirmed yet.

## Steps 6 to 8 (not started)

Restart recovery, the 20-failing-Pod window, Stall Watch coverage, the overnight soak, the high
Pod churn steps and the S-size red test are not run yet.

## Cost

Prices are on-demand list prices for `us-central1`. Token prices are from LiteLLM's model cost
map for `vertex_ai/gemini-3.5-flash`: $1.50 per million input tokens, $0.15 per million cached
input tokens, $9.00 per million output tokens.

| Item                                                                           | Per hour      |
| ------------------------------------------------------------------------------ | ------------- |
| The install's host cluster (2 × e2-standard-4, cluster fee, disks)             | about $0.40   |
| Test fleet: 162 × e2-small, 80 × e2-standard-2, 6 × e2-standard-4 (churn pool) | $8.87         |
| Test fleet: 40 cluster fees                                                    | $4.00         |
| Test fleet: 12 TB pd-balanced boot disks                                       | $1.65         |
| **Test fleet total**                                                           | **about $15** |

| First-run window (5 h) | Tokens  | Cost                         |
| ---------------------- | ------- | ---------------------------- |
| Uncached input         | 45.1 M  | $67.7                        |
| Cached input           | 242.4 M | $36.4                        |
| Output                 | 0.79 M  | $7.1                         |
| **Total**              |         | **$111, about $22 per hour** |

The first hour holds the onboarding burst. The later hours are the scheduled audits and Stall
Watch every 30 minutes. The overnight soak (Step 6) gives the steady daily token cost.

## Data

- The run record: [`harness-limits.json`](harness-limits.json), key
  `0108e59d|next|combined|1|first_run|1`.
- Task, cron and session data were read from the agent's volume (`kanban.db`,
  `cron/executions.db`, each profile's `state.db`).
