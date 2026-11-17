# Sized test fleet: results of four runs on sizes M and L

## High level summary

This page records the cost and the memory of one kube-agents install, measured with the sized test fleet. Every number here comes from four new runs, made on 2026-11-16 and 11-17 on `main`'s commit `544e2db2c`:

- A and D: size L, 16 clusters and 500 nodes.
- B and C: size M, 8 clusters and 250 nodes.

Each run builds the fleet, resets the install, and measures two windows. The burst window holds the first-run flow and all 10 audits. The soak window is 1 hour of normal work and includes one eval question. All four runs were green, and the eval question passed on each. Run A's answer was correct, but an earlier form of the check failed it.

The tokens are counted by capability from the install's own records and checked against Vertex AI's count for the project: 97.9 % to 98.8 %, apart from run A (85 %, explained below). The memory is split by capability with the shared-process estimate: each container's memory above its idle level, split every minute among the sessions active in it.

Results:

- **Memory:** about three quarters of the memory-time is the install's idle footprint. Of the rest, the first-run flow takes the most, then incident triage, the stall watch, `compliance-audit` and `upgrade-retrospective`.
- **Model cost:** the first-run flow dominates the burst window. In 1 of the 2 size-M runs, one Cluster Agent session looped to its 500-call limit and used 92 M tokens. Over the six size-M and size-L runs made since 2026-11-12, that happened in 2 of 3 size-M runs and in none of the size-L runs.
- **A month:** the scheduled audits cost about $120 to $150 in model tokens. The host install costs about $333 before the model.

## The four runs

| Run | Size | Verdict | Burst window | Run cost |  Fleet |  Model | Burst tokens | Records against Vertex AI | Token source     |
| --- | ---- | ------- | ------------ | -------: | -----: | -----: | -----------: | ------------------------: | ---------------- |
| A   | L    | green   | 60 min       |   $95.78 | $63.33 | $32.45 |       77.5 M |                    85.1 % | session records  |
| B   | M    | green   | 81 min       |   $86.65 | $35.40 | $51.25 |      216.0 M |                    98.8 % | session records  |
| C   | M    | green   | 60 min       |   $54.27 | $30.89 | $23.38 |       71.5 M |                    98.0 % | model-call spans |
| D   | L    | green   | 64 min       |  $102.83 | $66.16 | $36.67 |      122.9 M |                    97.9 % | session records  |

"Fleet" is the test fleet's own nodes, disks and fees, a cost of the test. "Model" is the install's model spend, by LiteLLM's counter. Run A's records hold 85 %: a stray size-M fleet stood beside it, and the Cluster Agents that triaged events on that fleet were pruned with it, together with their records.

## Memory by capability: share of memory-time, burst window

| Capability                        |  A (L) |  B (M) |  C (M) |  D (L) |       Mean | Mean, of the memory above idle |
| --------------------------------- | -----: | -----: | -----: | -----: | ---------: | -----------------------------: |
| Platform base (idle)              | 64.8 % | 69.2 % | 81.9 % | 89.0 % |     76.2 % |                              — |
| **1. First-run flow**             |  9.7 % | 18.3 % |  7.8 % |  4.9 % | **10.2 %** |                           43 % |
| **2. Incident triage**            |  6.8 % |  1.1 % |  1.0 % |  1.5 % |  **2.6 %** |                           11 % |
| **3. Stall-watch investigations** |  3.9 % |  1.9 % |  1.3 % |  1.5 % |  **2.1 %** |                            9 % |
| **4. `compliance-audit`**         |  3.0 % |  1.1 % |  2.2 % |  1.0 % |  **1.8 %** |                            8 % |
| **5. `upgrade-retrospective`**    |  2.2 % |  2.4 % |  0.9 % |  0.3 % |  **1.4 %** |                            6 % |
| `fleet-wide-cost-analysis`        |  2.7 % |  1.7 % |  0.5 % |  0.2 % |      1.3 % |                            5 % |
| `obtainability-audit`             |  1.5 % |  1.7 % |  1.1 % |  0.3 % |      1.1 % |                            5 % |
| `stockout-prevention`             |  1.8 % |  1.0 % |  0.9 % |  0.3 % |      1.0 % |                            4 % |
| The other 5 audits, together      |  2.7 % |  1.0 % |  1.5 % |  0.8 % |      1.5 % |                            6 % |
| Unattributed                      |  0.9 % |  0.8 % |  0.9 % |  0.3 % |      0.7 % |                            3 % |

In the soak window, the platform base is 94 % on average. The stall watch, at 0.7 %, and chat, at 0.3 %, are the only capabilities with a share. The rest, 5 % on average, is memory above the idle level with no session active: memory the burst left held.

## Model tokens by capability: cost, burst window

| Capability                   | A (L) |                          B (M) |  C (M) |  D (L) |
| ---------------------------- | ----: | -----------------------------: | -----: | -----: |
| First-run flow               | $8.90 | **$35.31** (a looping session) | $10.51 | $18.85 |
| Incident triage              | $6.61 |                          $4.12 |  $2.21 |  $4.88 |
| Stall-watch investigations   | $3.35 |                          $2.41 |  $2.48 |  $3.12 |
| `obtainability-audit`        | $1.01 |                          $1.68 |  $1.13 |  $1.03 |
| `compliance-audit`           | $0.67 |                          $0.96 |  $1.52 |  $1.04 |
| `upgrade-retrospective`      | $0.87 |                          $2.21 |  $0.72 |  $0.63 |
| `fleet-wide-cost-analysis`   | $1.26 |                          $1.34 |  $0.75 |  $0.65 |
| The other 7 audits, together | $2.85 |                          $2.47 |  $3.15 |  $3.00 |

In the soak window, the model spend was the stall watch's investigations ($0 to $2.19) and the eval question ($0.15 to $0.92).

## A month of scheduled model work, by cron job

Each audit's mean cost in a run, times its runs a month: 30.4 for a daily audit, 4.35 for a weekly one. The runs reset the install, so every audit ran as a first run, and these are upper figures.

| Job                           | Schedule     | M a run |   M a month | L a run |   L a month |
| ----------------------------- | ------------ | ------: | ----------: | ------: | ----------: |
| `obtainability-audit`         | daily 06:50  |   $1.40 |      $42.67 |   $1.02 |      $31.01 |
| `compliance-audit`            | daily 06:20  |   $1.24 |      $37.77 |   $0.86 |      $26.13 |
| `stockout-prevention`         | daily 09:20  |   $0.66 |      $20.06 |   $0.68 |      $20.52 |
| `ai-security-audit`           | daily 08:50  |   $0.51 |      $15.40 |   $0.37 |      $11.16 |
| `gcp-networking-fabric-audit` | daily 08:00  |   $0.30 |       $9.04 |   $0.43 |      $13.00 |
| `gce-compute-fleet-audit`     | daily 07:45  |   $0.28 |       $8.48 |   $0.24 |       $7.19 |
| `upgrade-retrospective`       | Sunday 18:00 |   $1.47 |       $6.37 |   $0.75 |       $3.26 |
| `fleet-wide-cost-analysis`    | Monday 07:50 |   $1.04 |       $4.54 |   $0.95 |       $4.14 |
| `security-patch-orchestrator` | Monday 07:20 |   $0.58 |       $2.54 |   $0.77 |       $3.35 |
| `fleet-consistency-drift`     | Monday 08:20 |   $0.49 |       $2.12 |   $0.45 |       $1.95 |
| **Scheduled model work**      |              |         | **$148.98** |         | **$121.71** |

The other 18 cron jobs are scripts and called no model in the soak windows. The host install costs about $333 a month before the model:

| Line                      |  A month |
| ------------------------- | -------: |
| 2 × `e2-standard-4` nodes |  $195.67 |
| GKE zonal cluster fee     |   $73.00 |
| Managed Prometheus        |   $23.58 |
| Boot disks, 2 × 100 GB    |   $20.00 |
| Volumes, 142 GiB          |   $14.20 |
| Logging                   | $0 to $6 |

Events and chat come on top of both. Each stall investigation costs about $0.30 to $0.70, and each chat question about $0.15 to $0.90.

## What the runs found

- **The Cluster Agent's first-run inventory loops at size M.** Run B's session on `sized-m-04` made 998 messages (500 model calls) in 29 minutes and used 92 M tokens, 43 % of the burst window. It is the same pattern as an earlier size-M run, where `sized-m-03` used 62 M. Run C had no loop, and neither size-L run had one. The size-M and size-L clusters have the same shape, about 31 nodes each.
- **The L tier does not fit on the default host.** At the L tier, the credential proxy requests 4 CPUs and 8 GiB, and cannot be scheduled on two `e2-standard-4` nodes. Runs A and D therefore ran at the medium tier. The failed upgrade then held every later `helm upgrade` and `helm rollback` in their wait. Setting `spec.deployment.sizingProfile` back on the `PlatformAgent` recovered the install.
- **The session records miss work in two cases:**
  - **A pruned profile.** When a cluster leaves the fleet, its Cluster Agent profile is pruned, and its records go with it.
  - **A session that never reaches `state.db`.** In run C, all of one Cluster Agent's sessions failed to reach its `state.db`.
- **The model-call spans fill the second case, but must be read during the run.** Each profile's `hermes_otel_live.db` holds every model call with its exact usage, and gave 98 % on run C. It is a rolling buffer of about 2,000 events, so it must be read every few minutes. A per-call tag at the gateway (the design's step 4) would cover both cases.
- **This developer project removes the install's role grants within hours.** Each run granted them again. The shared test-bed project avoids it.

## Method

- **Runs:** `hack/sized_fleet.py up` and `run --down --eval-cases`, and the run's own records. Run A was measured afterwards with `hack/sized_fleet.py usage`.
- **Tokens:** the install's records by capability (`bench/kube_agents_bench/scale/attribution.py`):
  - from the model-call spans where they were read in time (run C);
  - otherwise from the session records.

  They are checked against Vertex AI's `token_count` and LiteLLM's counters (`model_counters.py`).

- **Memory:** Cloud Monitoring's container memory per minute, split with the shared-process estimate (`memory_by_capability`).
- **Prices:** LiteLLM's price table for `vertex_ai/gemini-3.5-flash`: $1.50 a million input tokens, $0.15 cached, $9 output. The Cloud Billing Catalog API priced the fleet and the host.
