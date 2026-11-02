# Real-install test results: one install at the L container size

Status on 2026-11-02: complete. Steps 1 to 8 and the stress stage have run.

## High level summary

One install at the L container size runs a fleet of 40 to 42 clusters without a crash, at fair use
and beyond it. Its containers have room to spare. Its Stall Watch does not: it checks each cluster
for stuck controllers only about every 7 to 12 hours on this fleet. An install is one kube-agents
deployment. The L size gives the agent 8 CPU and 16 GB, the credential proxy 8 CPU and 8 GB, and the
event watcher 2 CPU and 4 GB. The test ran for about 40 hours. In that time no container was killed
for memory, and at fair use no brokered command was refused and no prompt failed. A brokered command
is a `kubectl` or `gcloud` command that the credential proxy runs for the agent. Beyond fair use, at
5 questions at once, the credential proxy refused 2 commands. At 8 questions at once it restarted
once, for a reason that is not known. No container that the L size sets used more than half of its
limit.

The first-run flow finished in 63 minutes. The first-run flow is the onboarding that a new install
does by itself. About 30 of the 63 minutes came from a fault in the test environment: an external
IAM process removed the install's permissions. Without the fault, the flow takes about 35 minutes.

A heavy question takes 10 to 25 minutes when it runs alone. Five questions at once took 78 minutes
and eight took 71 minutes. The time of a question does not grow with the cluster count. A question
about 10 named clusters took longer than one about the whole fleet, because the upgrade script reads
the whole fleet.

The limits that matter at 40 clusters are not container sizes:

- Stall Watch does not cover this fleet in one run. Stall Watch is the scheduled job that looks for
  stuck controllers. It runs every 30 minutes and stops each run at a 25-minute budget. With no
  other load, one run read 70 namespaces and moved its cursor about 3 clusters on. The fleet has
  1,683 namespaces in 42 clusters. By namespaces a full pass takes about 24 runs, or 12 hours. By
  the cursor it takes about 14 runs, or 7 hours. These durations are derived from one run, not
  observed. Under 5 questions at once a run read 20 namespaces. The cost follows the namespace
  count, not the cluster count.
- The sandbox shell has a fixed size of 2 CPU and 2 GiB. At 8 questions at once its memory reached
  94%. It is the first limit beyond fair use.

The S container size does not crash on this fleet. The S size sets each container to its request.
The first-run flow still finished, but brokered commands waited up to 60 seconds and the audits took
up to 3.6 times as long.

The test found six defects in the product:

1. The event watcher does not watch clusters that are registered after it starts. On a new install
   it watches no cluster until the gateway restarts.
2. The upgrade report is not stable in scope. In 4 of the 5 finished fleet-wide reports it reported
   on clusters that the install is told to exclude, and once it left out the managed clusters.
3. The fleet health answers also count the excluded clusters.
4. The onboarding report says "Scanned 6 clusters", but the agent examined all 40.
5. The agent could not register its findings, because the connection was refused.
6. After an outage, the event watcher creates a large queue of triage tasks at once: about 120 in 10
   minutes.

Model tokens are the largest running cost. In steady operation the install used $16.6 of tokens per
hour, about $400 per day. One install on GKE costs about $0.40 per hour. The 40-cluster test fleet
costs about $15 per hour.

## Expected conclusions

Each prediction comes from the first-run data above and the Phase 1 to 4 forecast. After the test,
the Result column says whether the data confirms the prediction.

| #   | Step | Prediction                                                                                                                                                                                                                                       | Basis                                                                                                                                                                                                                                  | Result                                                                                                                                                                                                                                                                                                                                                        |
| --- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | 5    | No safe limit breaks when one question runs at a time.                                                                                                                                                                                           | First-run peaks are below half of every limit except the shell's CPU.                                                                                                                                                                  | Confirmed for the 6 one-at-a-time runs: no restart, OOMKill, refusal or 429; longest command wait 15 s.                                                                                                                                                                                                                                                       |
| 2   | 5    | An upgrade readiness report takes 35 to 60 minutes at 40 clusters, so it passes the harness's 30-minute wait. With the wait raised to 90 minutes, it finishes.                                                                                   | Run 1 took 24 minutes to list 40 clusters. Phase 4 measured 51 to 72 s per cluster for an audit.                                                                                                                                       | Disproved: finished reports took 16 to 18 minutes at 40 to 42 clusters. Run 1 passed the wait only because 15 triage tasks shared the shell.                                                                                                                                                                                                                  |
| 2b  | 5    | The report's time is a fixed part plus a part per cluster. For 1 cluster it takes 5 to 12 minutes, for 10 clusters 15 to 25 minutes, for 42 clusters 40 to 60 minutes. The per-cluster part is 50 to 70 s.                                       | The fixed part is the skill read, the fleet listing and the answer. Phase 4 measured 51 to 72 s per cluster for an audit. The 1- and 10-cluster runs name their clusters in the prompt, so the install keeps all 42 clusters in scope. | Disproved: 8 minutes for 1 cluster, 26 to 37 for 10 named clusters, 16 for the whole fleet of 42. The fixed part is about 8 minutes; above it, the agent's path sets the time, not the cluster count.                                                                                                                                                         |
| 3   | 5    | The fleet health answer names the hot Deployment on `scale-07`, and the upgrade answer names `signal-old` as behind. The zone skew on `signal-skew` is found less reliably.                                                                      | Both are visible with one `kubectl top` or one cluster list. The skew needs Pod-to-zone joins.                                                                                                                                         | Confirmed: the zone pin was named in 3 of 3 fleet health answers, the hot Deployment in 2 of 3, and `signal-old` in the 42-cluster upgrade report.                                                                                                                                                                                                            |
| 4   | 5    | With 2 and 3 questions at once, containers stay inside their limits. The shell CPU reaches its 2-core limit, and each answer takes longer in proportion to the number of questions. At 3 at once, at least one answer passes the 30-minute wait. | All work goes through the one sandbox shell, which already peaked at 1.26 cores.                                                                                                                                                       | Partly confirmed: containers stayed inside their limits and 3 at once passed the 30-minute wait. Disproved: the shell's CPU stayed at 0.8 cores, not 2.                                                                                                                                                                                                       |
| 5   | 6    | After an agent restart, the event watcher watches all clusters again within 10 to 15 minutes.                                                                                                                                                    | The runner's gateway restarts took about 11 minutes to settle.                                                                                                                                                                         | Disproved, faster: all 43 clusters (the 42 test clusters and the host) watched again about 3 minutes after the restart.                                                                                                                                                                                                                                       |
| 6   | 6    | A window with 20 failing Pods does not start a session storm. The event watcher's quota holds new sessions, and the triage queue clears within 1 hour.                                                                                           | 15 triage tasks at 21:18 ran 2 to 3 at a time and cleared in 37 minutes.                                                                                                                                                               | Confirmed for sessions: the quota suppressed all 11,683 session starts in the hour, and no container restarted. The hour still cost $69.5 in tokens.                                                                                                                                                                                                          |
| 7   | 6    | Stall Watch reads every cluster once in each 30-minute cycle at 42 clusters, with less than 15% margin.                                                                                                                                          | Every first-run Stall Watch run took 25 minutes at 40 clusters.                                                                                                                                                                        | Disproved: each run stops at its 25-minute budget. A quiet run read 70 of 1,683 namespaces, so a full pass takes about 7 to 12 hours (derived).                                                                                                                                                                                                               |
| 8   | 6    | The steady token cost after onboarding is $15 to $20 per hour, or $360 to $480 per day, and the agent's memory stays under 3 GB for 12 hours.                                                                                                    | $111 in the first 5 hours, which include the onboarding burst.                                                                                                                                                                         | Confirmed: $16.6 per hour ($398 per day) over 12 hours; agent memory peak 2.57 GB, with no growth.                                                                                                                                                                                                                                                            |
| 9   | 7    | High Pod churn does not harm the install. The test cluster cannot reach 3,000 Pods per minute before the install shows strain. The event watcher stays under 0.5 CPU and 1 GB.                                                                   | The event watcher reacts to warnings and failures, not to normal Pod creation.                                                                                                                                                         | Confirmed: the test cluster reached at most about 800 Pods per minute. The event watcher stayed at 0.03 CPU and 0.18 GB.                                                                                                                                                                                                                                      |
| 10  | 8    | At the S size the install fails. The first limit is CPU throttling of the credential proxy and the shell, seen as long command waits. Then the agent is killed for memory during onboarding.                                                     | At L, the proxy peaked at 3.45 cores (S gives 0.5), the shell at 1.26 cores (S gives 0.2), and the agent at 2.29 GB (S gives 2 GiB = 2.15 GB).                                                                                         | Partly confirmed: the throttled credential proxy made commands wait up to 60 s and the audits ran up to 3.6 times as long. Disproved: no container was killed for memory (the agent's memory metric read 2.35 GB against its 2 GiB limit, so the metric likely counts memory the kernel can reclaim), and the first run finished in 56 minutes.               |
| 11  | All  | One L install is enough for 40 clusters, and Stall Watch's cycle sets the maximum, not a container size.                                                                                                                                         | Every container peak at 40 clusters is below half of its L limit.                                                                                                                                                                      | Partly confirmed: in the soak and the stress runs no L-sized container passed half of its limit. But Stall Watch, not a container, sets the limit, and it is tighter than predicted: a full pass takes about 7 to 12 hours at 1,683 namespaces. Beyond fair use, the sandbox shell's fixed memory is the next limit (93 to 94% at 5 and 8 questions at once). |

## Setup

| Item                             | Value                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Build                            | `main` at `0108e59d`, `spec.mode: next`, model `vertex_ai/gemini-3.5-flash`                                                                                                                                                                                                                                                                                            |
| Install                          | New install with `install.sh` on 2026-10-29 (uninstall first), no GitOps repository. The first-run window starts from a reset: the runner deleted and re-created the agent's data volumes, which clears all agent state and starts the first-run flow again. The operator, credential proxy and A2A Pods ran from the install on 2026-10-29; they hold no agent state. |
| Host cluster                     | `scale-host`, zonal Standard, 2 × e2-standard-4 (default and gVisor pools)                                                                                                                                                                                                                                                                                             |
| Agent                            | limit 8 CPU, 16 GB                                                                                                                                                                                                                                                                                                                                                     |
| Credential proxy                 | limit 8 CPU, 8 GB                                                                                                                                                                                                                                                                                                                                                      |
| Event watcher (`agent-api-auth`) | limit 2 CPU, 4 GB                                                                                                                                                                                                                                                                                                                                                      |
| Sandbox shell                    | 2 CPU, 2 GiB (fixed in the operator)                                                                                                                                                                                                                                                                                                                                   |
| Test fleet                       | 40 zonal clusters in one zone, 248 nodes, 17,385 Pods, 7,643 Deployments, 7,520 Services, 7,240 HPAs, 14,003 ConfigMaps, 1,683 namespaces                                                                                                                                                                                                                              |
| Load                             | 3 Pods created per minute per run (2.8 achieved), 5 failing Pods                                                                                                                                                                                                                                                                                                       |
| Planted signal                   | one hot Deployment (`payments/hot-api`, 6 busy Pods) on `scale-07`                                                                                                                                                                                                                                                                                                     |

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
| 16:30:25 to 16:55:30 |                | First Stall Watch run: stopped at its 25-minute budget                                                             |
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

Safe, with one gap. Every check passed, and the first-run flow finished without intervention by the
install. The one manual step, the unblock at 16:54, repaired a fault in the test environment. The
gap: the event watcher watched no test cluster in this window (Step 6, finding 9), so the window
holds none of the event watcher's load. Every window after 21:16 holds it, and all of them were safe.

### Findings

1. **The onboarding report under-counts.** `INVENTORY.md` says "Scanned 6 clusters and 183
   workloads". The discovery task audited the other 34 clusters, but the report covers only the 6
   clusters with Cluster Agents.
2. **Findings registration failed.** The ranking task got `Connection refused` (exit 13) for every
   cluster and wrote the scores to `INVENTORY.scores.json` instead.
3. **The event watcher started with an empty watch list.** The runner found that it watched no
   cluster and restarted the gateway before T0. After the restart it watched all 40.
4. **Stall Watch covers the fleet about once every 7 to 12 hours.** Every run stops at its 25-minute
   budget (`TICK_BUDGET_SECONDS` = 1,500 in `agents/platform/scripts/stall_watch.py`) and the next
   run resumes where it stopped. The script's ledger, read after a run at 09:00 UTC on 2026-11-02
   with no test load, reported 70 namespaces read, about 21 s per namespace, and its cursor moved
   from `scale-06` to `scale-09`. The fleet has 1,683 namespaces in 42 clusters, so a full pass
   takes about 24 runs by namespaces or 14 runs by the cursor. These are derived from one run. A
   run during 5 questions at once read 20 namespaces. Phase 4 measured 52 to 60 s per cluster on
   test clusters with few namespaces, so that rate does not hold for a fleet with many namespaces.

## Step 5: heavy questions, one at a time

Each run sends the question and then a follow-up. The plan is `real-install.yaml`, operations
`upgrade_report` and `fleet_health`, 3 runs each. The delegated work of each run is one task on
the Platform Agent. Every run overlapped a Stall Watch run. The token counts cover the whole
window, so they include that Stall Watch run.

| Run                 | Window (UTC)   | Minutes | Finished                 | Input tokens | Shell CPU peak | Proxy CPU peak | Agent memory peak | Longest command wait |
| ------------------- | -------------- | ------- | ------------------------ | ------------ | -------------- | -------------- | ----------------- | -------------------- |
| `upgrade_report` #1 | 21:31 to 22:02 | 30      | No: harness wait ran out | 63.5 M       | 0.77           | 3.11           | 2.62 GB           | 0.1 s                |
| `upgrade_report` #2 | 22:03 to 22:20 | 17      | Yes                      | 11.7 M       | 0.78           | 3.20           | 1.84 GB           | 0.1 s                |
| `upgrade_report` #3 | 22:22 to 22:40 | 18      | Yes                      | 30.4 M       | 0.89           | 3.24           | 2.11 GB           | 2.5 s                |
| `fleet_health` #1   | 22:42 to 23:07 | 25      | Yes                      | 26.8 M       | 1.17           | 3.50           | 2.17 GB           | 15 s                 |
| `fleet_health` #2   | 23:09 to 23:23 | 14      | Yes                      | 11.3 M       | 0.73           | 3.16           | 1.58 GB           | 15 s                 |
| `fleet_health` #3   | 23:25 to 23:35 | 10      | Yes                      | 30.4 M       | 0.76           | 2.84           | 2.14 GB           | 0.1 s                |

No run had a restart, an OOMKill, a refused command or a model 429.

`upgrade_report` #1 is slow for a reason outside the question. The runner restarted the gateway
at 21:16 to fix the event watcher's empty watch list. After the restart the event watcher created
15 triage tasks at once, for failures that already existed. The report's task shared the one
sandbox shell with them, and it took 24 minutes to list the clusters. The harness archived it at
its 1,800 s wait (`AGENT_DELEGATION_TIMEOUT`); the product sets no such limit.

### Planted signals

The two signal clusters came up at about 22:30, so the upgrade runs did not see them. The
upgrade check for `signal-old` moves to the runs with the longer wait.

| Signal                                                    | `fleet_health` #1 | #2                              | #3                              |
| --------------------------------------------------------- | ----------------- | ------------------------------- | ------------------------------- |
| Hot Deployment `payments/hot-api` on `scale-07`           | Named             | Not named                       | Named                           |
| Zone pin `storefront/checkout` on `signal-skew`           | Named             | Named, as a critical zonal lock | Named, as a critical zonal lock |
| Fleet-wide zone imbalance (41 of 42 clusters in one zone) | Named             | Named                           | Named                           |

### Findings

5. **The upgrade report's scope is not stable.** Run 2 reported on the 40 test clusters and said
   the other clusters are outside the fleet scope, which is correct. Run 3 did the reverse: it
   reported on the 8 clusters that `spec.scope.exclude` names, and left out the 40 managed test
   clusters.
6. **Fleet health reads excluded clusters.** All three answers counted 50 clusters and reported
   failures on clusters that `spec.scope.exclude` names. The `PlatformAgent` CRD says that the
   gke MCP server's `list_clusters` still covers excluded clusters, which is a likely path.
7. **The questions are faster than predicted.** A finished report took 17 to 18 minutes at 40
   clusters, with its follow-up, while Stall Watch ran.

## Step 5: questions at once

The plan is `real-install-concurrent.yaml`. Each level sends the upgrade question and `N - 1`
copies of the follow-up at the same time.

| Questions at once | Window (UTC)   | Minutes | Finished                           | Input tokens | Shell CPU peak | Agent memory peak | Longest command wait | Model 429s |
| ----------------- | -------------- | ------- | ---------------------------------- | ------------ | -------------- | ----------------- | -------------------- | ---------- |
| 2                 | 23:52 to 00:17 | 25      | 2 of 2                             | 47.2 M       | 0.79           | 2.80 GB           | 30 s                 | 0          |
| 3                 | 00:28 to 00:58 | 30      | 1 of 3; the harness wait cut off 2 | 134.7 M      | 0.80           | 3.26 GB           | 15 s                 | 4, retried |

No run had a restart, an OOMKill, a refused command or a run-slot refusal. The sandbox shell's CPU
stayed at 0.8 cores, so the shell is not what slows three questions down.

## Report duration by cluster count

These runs set `AGENT_DELEGATION_TIMEOUT=5400`, so the harness does not cut a report off. The 1- and
10-cluster runs name their clusters in the prompt; the install keeps all 42 clusters in scope.

| Run                      | Clusters | Window (UTC)   | Minutes | Input tokens | Tool calls | Brokered commands | Agent memory peak |
| ------------------------ | -------- | -------------- | ------- | ------------ | ---------- | ----------------- | ----------------- |
| `upgrade_report_1` #1    | 1        | 01:49 to 01:57 | 8       | 3.6 M        | 25         | 701               | 1.98 GB           |
| `upgrade_report_1` #2    | 1        | 01:59 to 02:08 | 8       | 13.2 M       |            | 530               | 2.22 GB           |
| `upgrade_report_10` #1   | 10       | 02:24 to 02:50 | 26      | 14.7 M       | 55         | 2,127             | 2.15 GB           |
| `upgrade_report_10` #2   | 10       | 02:52 to 03:29 | 37      | 43.4 M       | 78         | 438               | 2.26 GB           |
| `upgrade_report_long` #1 | 42       | 01:15 to 01:32 | 16      | 19.7 M       | 70         | 1,318             | 2.51 GB           |

Tool calls are the Platform Agent's calls in the window. Brokered commands include the Stall Watch
run that overlapped every window.

The time does not grow with the cluster count. It depends on the path the agent takes. The
fleet-wide question runs the skill's script (`fleet_upgrade_report.py`) once over the whole fleet.
The 10-cluster question asks for a subset, and the agent found no subset option in the script: in
run 2 it ran the script 11 times with different flags and made 14 more per-cluster `gcloud` calls.
The 1-cluster question used direct reads and took 8 minutes, which is the fixed part of a report.

The 42-cluster report named `signal-old` as three minor versions behind. It also reported on the
excluded clusters, as run 3 did (finding 5).

### Findings

8. **A report on a subset of the fleet is slower than one on the whole fleet.** 26 to 37 minutes for
   10 named clusters, 16 minutes for 42. The upgrade skill's script reads the whole fleet, and the
   agent spends turns trying to narrow it.

## Step 6: restart recovery and 20 failing Pods

| Run                | Window (UTC)   | Events seen per minute | Session starts asked | Suppressed by quota | Events injected | Agent memory peak | Agent CPU peak | Token cost           |
| ------------------ | -------------- | ---------------------- | -------------------- | ------------------- | --------------- | ----------------- | -------------- | -------------------- |
| `restart_recovery` | 03:46 to 04:18 | 664                    | 6,972                | 6,805               | 49              | 2.43 GB           | 2.27           | $40.6 ($78 per hour) |
| `failing_20`       | 04:34 to 05:34 | 575                    | 11,683               | 11,683              | 0               | 2.79 GB           | 1.65           | $69.5 ($70 per hour) |

No run had a restart other than the planned one, an OOMKill or a refused command.

**Restart recovery.** The gateway restarted at 03:46:42. The event watcher reported 7 clusters up at
03:48:00, 25 at 03:49:18 and all 43 (the 42 test clusters and the host) at 03:49:57: about 3 minutes.

**20 failing Pods.** The session quota held. The event watcher asked for 11,683 triage sessions in
the hour and the quota suppressed all of them. The token cost of the hour was $69.5, so work other
than triage sessions also ran in it; the soak gives the baseline to compare.

### The event watcher was not watching during the first run

The event watcher builds its watch list once, when it starts, from the Cluster Agent profiles that
exist then. A profile created later is not watched until the watcher restarts. The first-run window
started from a reset that deleted the profiles. The watcher started at about 16:06, and the
reconcile created the 40 profiles at 16:11. So the watcher watched no test cluster for the whole
first-run window, which is why it saw only 2.7 events per minute.

The runner restarted the gateway at 21:16 before Step 5. From then on the watcher watched all
clusters and saw 560 to 660 events per minute in every window without added failing Pods or churn. It asked for 1,700 to 11,700 session
starts per window and the quota suppressed almost all of them.

### Findings

9. **A new install does not watch the clusters it registers after start.** On a new install the
   event watcher starts before the first reconcile creates the Cluster Agent profiles, so it watches
   none of them until the gateway restarts. The first-run measurement therefore under-counts the
   event watcher's load.
10. **At 40 clusters the session quota carries the event load.** With the watcher watching every
    cluster, it asked for about 100 to 250 triage sessions per minute, mostly for the 785 Pods that
    the test fleet cannot schedule. The quota suppressed nearly all of them.

## Step 6: the 12-hour soak

The window ran from 07:29 to 19:29 UTC on 2026-11-01, with all 42 clusters watched. It covers the
daily audits from 07:45 to 18:00. The compliance (06:20) and obtainability (06:50) audits ran
before it; their durations are in the first-run timeline.

| Measure                                                           | Value                                                                                             |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Restarts, OOMKills, refused commands, failed prompts, lost triage | 0                                                                                                 |
| Model 429s                                                        | 20, all retried                                                                                   |
| Token cost                                                        | $198.7, or $16.6 per hour ($398 per day)                                                          |
| Agent memory peak                                                 | 2.57 GB of 16 GB                                                                                  |
| Credential proxy CPU peak                                         | 3.54 of 8 cores                                                                                   |
| Sandbox shell peak                                                | 0.89 cores, 0.80 GB                                                                               |
| Brokered commands                                                 | 72,830; median wait 0.1 s, longest 5 s                                                            |
| Event watcher                                                     | 565 events per minute; 139,633 session starts asked, 139,601 suppressed by the quota, 10 injected |
| Stall Watch                                                       | 23 runs, each stopped at its 25-minute budget; about 4 clusters read per run                      |

| Scheduled audit         | Started (UTC) | Minutes |
| ----------------------- | ------------- | ------- |
| GCE fleet audit         | 07:45         | 18      |
| Networking audit        | 08:00         | 2       |
| AI security audit       | 08:50         | 11      |
| Stockout prevention     | 09:20         | 19      |
| Upgrade readiness watch | 10:11         | 26      |
| Upgrade retrospective   | 18:00         | 13      |

Every scheduled job finished. The agent's memory did not grow over the 12 hours.

## Step 7: high Pod churn

The first try is invalid. Its churn Job targeted `scale-01`'s `churn-pool`, which the runner's
fixture Pods had already filled, so no churn happened. Its records were removed from the data file.
The re-run used a pool of its own, `churn-fast` (6 × e2-standard-4, 110 Pods per node), tainted so
that only churn Pods run on it.

| Target per minute | Pods at once | Reached per minute (median of the Job's per-minute completions) | Events seen per minute | Event watcher CPU / memory | Agent memory peak |
| ----------------- | ------------ | --------------------------------------------------------------- | ---------------------- | -------------------------- | ----------------- |
| 500               | 65           | 447                                                             | 2,357                  | 0.03 / 0.17 GB             | 3.33 GB           |
| 1,000             | 130          | 585                                                             | 2,493                  | 0.03 / 0.17 GB             | 3.39 GB           |
| 3,000             | 400          | 800                                                             | 2,993                  | 0.03 / 0.18 GB             | 3.18 GB           |

The test cluster could not create Pods faster than about 800 per minute. At that rate the event
watcher saw five times its usual event rate and its CPU and memory did not move. No run had a
restart, an OOMKill or a refused command.

## Stress beyond fair use

The plans are `real-install-stress.yaml` (5 and 8 questions at once) and
`real-install-stress-load.yaml` (the failing Pods and the command workers).

| Run                                                        | Window (UTC)              | Result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 5 questions at once                                        | 2026-11-02 07:32 to 08:50 | 5 answers in 78 minutes; 3 complete. The other 2 report denied model calls: an IAM strip removed the install's roles during the run (re-added at 08:47 to 08:48). Sandbox shell memory 2.00 GB of 2.15 GB (93%). The credential proxy refused 2 commands, the only refusals in the whole test; the record does not show whether the load or the IAM strip caused them. Longest command wait 60 s. Agent memory 4.04 GB. An earlier try on 2026-11-01 lost one task at its 150-step budget before a credential outage on the test machine ended it. |
| 8 questions at once                                        | 00:31 to 01:42            | All 8 answers finished in 71 minutes. Sandbox shell memory 2.02 GB of its fixed 2.15 GB (94%). The credential proxy restarted once (not an OOMKill). The longest command wait reached 60 s. Agent memory 3.77 GB. 37 model 429s, all retried.                                                                                                                                                                                                                                                                                                      |
| 100 failing Pods, 30 minutes                               | 01:59 to 02:31            | Safe. 725 events per minute; the quota suppressed 5,302 of 5,498 session starts. Agent memory 3.21 GB.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 32 command workers, 5 minutes, during the 100 failing Pods | 02:31 to 02:37            | 551 commands, 0 errors, 1.76 per second. Each command took 18 s at the median and 23 s at most. Credential proxy CPU 3.0 of 8 cores.                                                                                                                                                                                                                                                                                                                                                                                                               |

The 8 questions ran while about 130 triage tasks waited in the queue. The event watcher created
about 120 of them in 10 minutes at 00:21, right after model access came back from an IAM strip.

### Findings

11. **The first limit beyond fair use is the sandbox shell.** At 5 and 8 questions at once its
    memory reached 93% and 94% of its fixed 2 GiB, and brokered commands waited up to 60 s. At 5 at
    once the credential proxy refused 2 commands. The containers that
    the L size sets stayed below half of their limits.
12. **The command path is not CPU-bound.** 32 workers drove the credential proxy to 3 of its 8
    cores, and each command still took 18 s.
13. **The triage queue builds after an outage.** About 120 triage tasks appeared in 10 minutes
    when model access returned, and they drain a few at a time.

## Step 8: red test at the S size

The agent got 1 CPU and 2 GiB, the credential proxy 0.5 CPU and 1 GiB, and the event watcher
0.15 CPU and 384 MiB, from 04:28 UTC on 2026-11-02. The sandbox shell kept its fixed 2 CPU and
2 GiB. The runner reset the install's state at 04:45 and measured a 2-hour first-run window, then
one upgrade question.

| Measure                                 | S size                        | L size (first run)                              |
| --------------------------------------- | ----------------------------- | ----------------------------------------------- |
| Onboarding inventory report ready       | 56 minutes after the reset    | 63 minutes, of which about 30 were an IAM strip |
| Restarts and OOMKills                   | 0                             | 0                                               |
| Brokered command wait, median / longest | 2.5 s / 60 s                  | 0.1 s / 5 s                                     |
| Credential proxy CPU peak               | 0.58 of 0.5 cores (throttled) | 3.45 of 8                                       |
| Compliance audit                        | 54 minutes                    | 15 minutes                                      |
| Stall Watch run                         | 25 to 28 minutes of 30        | 25 minutes of 30                                |
| Fleet cost analysis                     | 21 minutes                    | 7 minutes                                       |

The upgrade question at the S size failed: its delegated task blocked after two failed turns, and
the answer blamed the service account. An IAM strip removed 10 of the install's roles during that
question (re-added at 07:09 to 07:10), so this run does not show an S-size effect.

The event watcher was blind again in this window (5.5 events per minute), for the same reason as
in the first run (finding 9). So the S size did not carry the event watcher's load.

### Findings

14. **The S size degrades by time, not by a crash.** The first run finished, but brokered commands
    queued behind a throttled credential proxy and reached the 60-second limit, the compliance audit
    took 3.6 times as long, and one Stall Watch run ran past its budget to 28 minutes. No container was
    killed for memory. The agent's memory metric read 2.35 GB against its 2 GiB limit with no OOMKill,
    so the metric likely counts memory the kernel can reclaim; the record cannot show the working set.

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

| Window                                       | Hours      | Token cost  | Per hour |
| -------------------------------------------- | ---------- | ----------- | -------- |
| 12-hour soak (steady operation, 42 clusters) | 12.0       | $198.7      | $16.6    |
| One question alone                           | 0.2 to 0.6 | $2.5 to $24 |          |
| 8 questions at once                          | 1.2        | $71.6       | $60.7    |
| S-size first run                             | 2.0        | $50.0       | $24.6    |

The steady cost of one install at 42 clusters is about $400 per day in tokens and about $10 per
day on GKE. The test fleet itself costs about $360 per day.

## Data

- The run record: [`harness-limits.json`](harness-limits.json), key
  `0108e59d|next|combined|1|first_run|1`.
- Task, cron and session data were read from the agent's volume (`kanban.db`,
  `cron/executions.db`, each profile's `state.db`).
