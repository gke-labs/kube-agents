# Sized test fleet: results for sizes S, M and L

## High level summary

A run puts a fleet of GKE clusters around one kube-agents install, the host install. The run measures the host install in two windows:

- the burst window: the first-run flow and all 10 audits, 1 to 3 hours;
- the soak window: 1 hour of normal work, which includes one eval question.

The run then deletes the fleet. This page gives one run of each size: duration, cost, peak memory, and model tokens by capability. It then projects the cost of the install for one month, from the cron schedule of each job.

Every cost line has a source:

- the model tokens: Vertex AI's own token counter for the project, and LiteLLM's counters;
- the infrastructure: prices from the Cloud Billing Catalog API;
- logging and monitoring: the project's own billing metrics.

Results:

- Every run completed all 10 audits, and nothing crashed. The eval question passed on M and L.
- One run of each size cost $25.82 (S), $88.57 (M) and $104.13 (L), the test fleet included.
- The host install costs about $333 a month at list price, before the model.
- The scheduled audits cost about $115 a month in model tokens, at size M and at size L.
- Peak memory stayed under 27 % of every container's limit.
- The main cost risk is the Cluster Agents' first-run inventory. At size M, one looping session used 40 % of the burst window's tokens.

## The runs

|                                          | S                                                        | M                                                            | L                                                                                       |
| ---------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| Date                                     | 2026-11-11                                               | 2026-11-12                                                   | 2026-11-16                                                                              |
| Clusters, nodes                          | 4, 125                                                   | 8, 250                                                       | 16, 500                                                                                 |
| Fleet build                              | 15 min                                                   | 21 min                                                       | 16 min                                                                                  |
| Burst window                             | 64 min                                                   | 75 min                                                       | 60 min                                                                                  |
| Soak window                              | 65 min                                                   | 61 min                                                       | 61 min                                                                                  |
| Whole run                                | about 2.6 h                                              | about 3.0 h                                                  | about 2.8 h                                                                             |
| Audits completed                         | 10 of 10                                                 | 10 of 10                                                     | 10 of 10                                                                                |
| Crashes or restarts                      | none                                                     | none                                                         | none                                                                                    |
| Eval `chat-routing-fleet-question-sized` | not run                                                  | passed, 125 s, 636 k tokens                                  | passed, 209 s, 1.18 M tokens                                                            |
| Verdict                                  | red: churn read before the clients started (fixed later) | records rebuilt after a crash in the cost step (fixed later) | red: churn 179 Pods a minute against 200 designed, 0.5 point outside the 10 % tolerance |

All three runs used the developer project `haoxuw-gke-dev`. An org IAM automation there removes the install's role grants within hours, so each run granted them again just before it started.

## Cost of one run

| Line                                   |            S |          M |           L | Source                                                                             |
| -------------------------------------- | -----------: | ---------: | ----------: | ---------------------------------------------------------------------------------- |
| Test fleet: nodes, disks, cluster fees |       $16.31 |     $36.26 |      $62.99 | GKE and Compute lifetimes, priced from the Catalog API                             |
| Test fleet: Managed Prometheus         | not measured |      $8.10 |      $14.68 | `write_sample_count` for the fleet's clusters, $0.06 a million samples             |
| Test fleet: container logs             | not measured |      $0.35 |       $0.60 | `logging.googleapis.com/byte_count`, $0.50 a GiB                                   |
| Model: burst window                    |        $9.51 |     $43.35 |      $24.58 | S and L: LiteLLM's spend counter. M: Vertex AI's token counter at LiteLLM's prices |
| Model: soak window                     |           $0 |      $0.51 |       $1.28 | The same                                                                           |
| **Run, total**                         |   **$25.82** | **$88.57** | **$104.13** |                                                                                    |

Model prices for `vertex_ai/gemini-3.5-flash`, from LiteLLM's price table: $1.50 a million input tokens, $0.15 a million cached input tokens, $9 a million output tokens. Applied to the S run's tokens, they give LiteLLM's recorded $9.51 exactly.

Two lines are not counted. The eval judge, `gemini-3.1-pro-preview`, used 10.8 k input and 5.9 k output tokens on M, about $0.1. The judge is part of the test, not of kube-agents. The host install's own share during a run is in "Every cost of the host install" below.

## Peak memory against the limit, burst window

| Container                |         S |         M |         L |      Limit | L's share of the limit |
| ------------------------ | --------: | --------: | --------: | ---------: | ---------------------: |
| `platform-agent`         | 1,278 MiB | 2,517 MiB | 2,421 MiB | 15,259 MiB |                   16 % |
| `litellm`                |   649 MiB |   680 MiB |   821 MiB |  3,072 MiB |                   27 % |
| `envoy-credential-proxy` |   262 MiB |   423 MiB |   714 MiB |  4,096 MiB |                   17 % |
| `shell`                  |    55 MiB |   346 MiB |   511 MiB |  2,048 MiB |                   25 % |
| `agent-api-auth`         |   138 MiB |   233 MiB |   270 MiB |  4,096 MiB |                    7 % |
| `fluent-bit`             |           |    43 MiB |    45 MiB |    256 MiB |                   18 % |
| `manager` (operator)     |           |    40 MiB |    42 MiB |    512 MiB |                    8 % |

The credential proxy and the shell grow with the fleet: their peaks roughly doubled from M to L. The agent container did not.

## Model tokens by capability, burst window

| Capability                             |    S tokens |    M tokens |     M cost |   L tokens |     L cost |
| -------------------------------------- | ----------: | ----------: | ---------: | ---------: | ---------: |
| First-run flow                         |       6.4 M |     129.5 M |     $30.50 |     44.0 M |     $11.96 |
| — its Cluster Agent share              |             |     123.3 M |            |     35.7 M |            |
| Stall-watch investigations             | (in triage) |       6.7 M |      $3.34 |      8.0 M |      $3.60 |
| Incident triage                        |       4.6 M |       4.6 M |      $2.40 |      2.2 M |      $1.71 |
| `fleet-wide-cost-analysis` (weekly)    |             |       2.3 M |      $0.90 |      4.5 M |      $1.80 |
| `upgrade-retrospective` (weekly)       |             |       1.8 M |      $1.26 |      4.8 M |      $1.80 |
| `obtainability-audit` (daily)          |             |       3.8 M |      $1.07 |      2.3 M |      $0.71 |
| `compliance-audit` (daily)             |       2.6 M |       0.8 M |      $0.33 |      3.1 M |      $0.80 |
| `stockout-prevention` (daily)          |             |       1.1 M |      $0.69 |      1.3 M |      $0.40 |
| `ai-security-audit` (daily)            |       2.9 M |       0.9 M |      $0.45 |      1.1 M |      $0.59 |
| `security-patch-orchestrator` (weekly) |       3.2 M |       1.3 M |      $0.56 |      1.6 M |      $0.54 |
| `fleet-consistency-drift` (weekly)     |             |       1.2 M |      $0.45 |      1.0 M |      $0.44 |
| `gcp-networking-fabric-audit` (daily)  |             |       1.3 M |      $0.49 |      0.8 M |      $0.25 |
| `gce-compute-fleet-audit` (daily)      |             |       0.6 M |      $0.25 |      0.7 M |      $0.28 |
| Unattributed                           |       0.2 M |           0 |            |          0 |            |
| **Burst window, total**                |  **27.7 M** | **155.9 M** | **$43.35** | **75.5 M** | **$24.58** |

S's record predates the per-capability split for most audits: the empty cells were summed into an "Other" row of 7.6 M.

The first-run flow dominates and varies from run to run. At M, one Cluster Agent session on `sized-m-03` made 500 model calls in 31 minutes and used 62 M tokens. It wrote about 230 scripts that printed nothing, until the turn limit stopped it. Without that session, M's Cluster Agents used about 7.6 M a cluster; L's used 2.2 M a cluster. One run of each size does not show a trend.

## Proof: the session records against the model's own counters

| Window                  | Vertex AI counted | LiteLLM counted | Session records | Session records against Vertex AI |
| ----------------------- | ----------------- | --------------- | --------------- | --------------------------------: |
| M burst, input          | 157.40 M          | not recorded    | 155.33 M        |                            98.7 % |
| M burst, output         | 552.5 k           | not recorded    | 533.1 k         |                            96.5 % |
| L burst, input          | 74.77 M           | 73.89 M         | 75.03 M         |                           100.3 % |
| L burst, output         | 504.7 k           | 499.3 k         | 498.1 k         |                            98.7 % |
| L burst and soak, input |                   | 76.88 M         | 76.18 M         |                 99.1 % of LiteLLM |

Vertex AI's counter is `aiplatform.googleapis.com/publisher/online_serving/token_count` for the whole project. The project's other install could make no model call in these windows, because its Vertex AI grant had been removed. So the counter holds the host install's calls and the eval judge's calls, on `gemini-3.1-pro-preview`.

A session counts in the window it started in, but LiteLLM counts a token when it happens. A Cluster Agent session that starts in the burst window and runs into the soak window therefore moves tokens from soak to burst. On L, 61 % of the soak window read as Unattributed for this reason. Over both windows together, the session records come to 99.1 % of LiteLLM's count.

## Every cost of the host install, a month

The host install runs on its own GKE Standard cluster, `scale-host`, zonal in `us-central1-c`.

| Line                               | Measured                                          | Price (Catalog API SKU)                                                                    |                          A month |
| ---------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------: |
| Nodes                              | 2 × `e2-standard-4`, one of them in a gVisor pool | E2 core $0.02181 an hour (`CF4E-A0C7-E3BF`); E2 RAM $0.00292 a GiB-hour (`F449-33EC-A5EF`) |                          $195.67 |
| Boot disks                         | 2 × 100 GB pd-balanced                            | $0.10 a GiB-month (`6AE1-525F-8B80`)                                                       |                           $20.00 |
| Volumes                            | 5 PVCs, 142 GiB, `standard-rwo` (pd-balanced)     | $0.10 a GiB-month                                                                          |                           $14.20 |
| GKE cluster fee                    | 1 zonal cluster                                   | $0.10 an hour (`6B92-A835-08AB`)                                                           |                           $73.00 |
| Managed Prometheus                 | 91.7 M samples in 7 days, `scale-host`            | $0.06 a million (`A4E4-DF03-CDB6`)                                                         |                           $23.58 |
| Cloud Logging                      | 2.9 GiB in 7 days, `scale-host`                   | $0.50 a GiB after 50 GiB a month free (`143F-A1B0-E0BE`)                                   | $6.17, or $0 under the free tier |
| Cloud Trace                        | 56 k spans in 7 days, the whole project           | free under 2.5 M spans a month                                                             |                               $0 |
| Load balancers, NAT, egress        | none on `scale-host`; images come from ghcr.io    |                                                                                            |                               $0 |
| **Host install, before the model** |                                                   |                                                                                            |                      **$332.62** |

GKE's free tier credits one zonal or Autopilot cluster's fee per billing account, which can remove the $73.

## A month of kube-agents, by cron schedule

Only 11 of the 29 cron jobs call the model: the 10 audits and `upgrade-readiness-weekly-brief`. The other 18 are scripts. The soak windows measured them at zero model tokens, the jobs that run every minute or every 30 minutes included.

| Job                              | Schedule       | Runs a month |                   M a run |   M a month |                   L a run |   L a month |
| -------------------------------- | -------------- | -----------: | ------------------------: | ----------: | ------------------------: | ----------: |
| `obtainability-audit`            | 06:50 daily    |         30.4 |                     $1.07 |      $32.53 |                     $0.71 |      $21.58 |
| `stockout-prevention`            | 09:20 daily    |         30.4 |                     $0.69 |      $20.98 |                     $0.40 |      $12.16 |
| `gcp-networking-fabric-audit`    | 08:00 daily    |         30.4 |                     $0.49 |      $14.90 |                     $0.25 |       $7.60 |
| `ai-security-audit`              | 08:50 daily    |         30.4 |                     $0.45 |      $13.68 |                     $0.59 |      $17.94 |
| `compliance-audit`               | 06:20 daily    |         30.4 |                     $0.33 |      $10.03 |                     $0.80 |      $24.32 |
| `gce-compute-fleet-audit`        | 07:45 daily    |         30.4 |                     $0.25 |       $7.60 |                     $0.28 |       $8.51 |
| `upgrade-retrospective`          | Sunday 18:00   |         4.35 |                     $1.26 |       $5.48 |                     $1.80 |       $7.83 |
| `fleet-wide-cost-analysis`       | Monday 07:50   |         4.35 |                     $0.90 |       $3.92 |                     $1.80 |       $7.83 |
| `security-patch-orchestrator`    | Monday 07:20   |         4.35 |                     $0.56 |       $2.44 |                     $0.54 |       $2.35 |
| `fleet-consistency-drift`        | Monday 08:20   |         4.35 |                     $0.45 |       $1.96 |                     $0.44 |       $1.91 |
| `upgrade-readiness-weekly-brief` | Saturday 07:00 |         4.35 | not measured, about $0.50 |    about $2 | not measured, about $0.50 |    about $2 |
| **Scheduled model work**         |                |              |                           | **$115.52** |                           | **$114.03** |

The audits in these runs were first runs, because each run resets the install. A later run reuses state, so these are upper figures.

| Line                                           |      M a month |      L a month | Basis                                                                         |
| ---------------------------------------------- | -------------: | -------------: | ----------------------------------------------------------------------------- |
| Host install, before the model                 |        $332.62 |        $332.62 | The table above                                                               |
| Scheduled model work                           |        $115.52 |        $114.03 | The table above                                                               |
| Stall-watch and drift investigations           |      about $20 |       about $9 | One a day: $0.67 each at M, $0.30 at L                                        |
| Incident triage                                |      about $70 |      about $50 | One burst-sized day of events, every day; a real fleet's rate is not measured |
| Chat                                           |     about $146 |     about $225 | 10 questions a day: $0.48 each at M, $0.74 at L                               |
| **Total, a month**                             | **about $684** | **about $731** |                                                                               |
| Total, light use: 2 questions a day, no triage |     about $497 |     about $500 |                                                                               |

A new install also pays its first-run flow once: $30.50 at M (of it, $13 for the looping session) and $11.96 at L.

For the test bed itself: a nightly M and a weekly L cost about 30 × $89 + 4.35 × $104 = about $3,100 a month, plus the host install's $333.

## Defects the runs found

All are fixed, with tests, in #5584, #5679 and #5680:

1. kubectl's "connection to the server was refused", from a new zonal cluster's control-plane resize, was not retried, and the run stopped.
2. The run's kubeconfig held only the fleet, so the install reset could not reach the host.
3. In `catalog_prices`, a local named `skus` hid the function `skus()`, and the cost step crashed. A narrow `safe()` then lost every record.
4. `kubectl exec` into the sandboxed pod cuts stdout at 64 KiB, and the token read of a size-M run passed it.
5. Under `spec.mode: next`, a bus-delegated worker had no card to join through, so the eval's specialist read as Unattributed.

Still open:

- The Cluster Agent's first-run inventory loop at M.
- The soak window's boundary effect (above).
- The test fleets' Managed Prometheus, which turning off on the test clusters would save.

## Method and sources

- Runs: `hack/sized_fleet.py up` and `run --down --eval-cases`, from the branches of #5584, #5679 and #5680. The records went to `scale-verdict.json`, `scale-measurements.json`, `scale-cost.json`, `capability-usage.json` and `evals/`. M's records were rebuilt after the run: memory from Cloud Monitoring, tokens from the session records, and the fleet's lifetimes from GKE's operations.
- Memory: Cloud Monitoring's container memory over each window, against the containers' limits on the host.
- Tokens: the session records in each profile's `state.db`, attributed by `bench/kube_agents_bench/scale/attribution.py`. The check sources are Vertex AI's `token_count` and LiteLLM's Prometheus counters.
- Prices: the Cloud Billing Catalog API, read on 2026-11-16, and LiteLLM's model price table.
- Logging, monitoring and trace: `logging.googleapis.com/byte_count`, `monitoring.googleapis.com/collection/attribution/write_sample_count` and `cloudtrace.googleapis.com/billing/spans_ingested`.
