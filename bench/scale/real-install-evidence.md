# Real-install evidence sheet: memory, tokens, tasks and cost

## High level summary

This sheet keeps the measurements behind the real-install test in one place. The test ran one
kube-agents install at the L container size on 40 to 80 small test clusters. Each test cluster had
about 6 nodes. The results and the verdicts are in [`real-install-results.md`](real-install-results.md).
This sheet gives the data that explains them.

The sheet answers six questions. What uses the memory? What does the install run, and what grows
with the fleet? What do the model tokens pay for? How many commands does the credential proxy run?
What does the install cost? What does the test not cover?

The memory of the install grows with the number of agent tasks that run at the same time. A fixed
cap of 6 tasks bounds it. Only the output of a command in the sandbox shell has no bound. The model
tokens grow with the fleet. Most of them pay for the conversation history that each model call
sends again. The tool output and the number of processes are small causes.

Each table names its source. "M" is a measurement in this test or in Phases 1 to 4. "C" is the
code. "#" is an open issue. The list of gaps for a fleet of 50 clusters with 2,000 nodes each is
in #4529.

The step results stay in [`real-install-results.md`](real-install-results.md): the first-run
timeline and task timings, each question's time and signals, the report time at 1, 10 and 42
clusters, the restart and failing-Pod windows, Pod churn, the 12-hour soak and its audit times, the
stress runs, the S-size red test, the predictions and their results, and the findings.

## The tested fleet

The cluster count is not the only measure of a fleet. Kube-agents' load follows the Pods,
namespaces and events in each cluster.

| Item                  | Value                                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------ |
| Clusters              | 40 (42 from Step 5 on, with two signal clusters; 80 in the token-scaling run)                                |
| Nodes                 | 248 (about 6 per cluster: 2 × e2-standard-2 and about 4 × e2-small)                                          |
| Pods                  | 17,385 (about 435 per cluster)                                                                               |
| Namespaces            | 1,683 (about 42 per cluster)                                                                                 |
| Deployments, Services | 7,643 and 7,520                                                                                              |
| Install               | `main` at `0108e59d`, `spec.mode: next`, model `vertex_ai/gemini-3.5-flash`                                  |
| Container size        | L: agent 8 CPU / 16 GB, credential proxy 8 CPU / 8 GB, event watcher 2 CPU / 4 GB; shell fixed 2 CPU / 2 GiB |
| Host                  | 2 × e2-standard-4                                                                                            |

## Memory

### Peak memory of each container, per window (GB, M)

Containers peak at different moments, so the total is an upper bound.

| Window              | Agent | Proxy | Shell | LiteLLM (2) | Event watcher | Others | Total |
| ------------------- | ----- | ----- | ----- | ----------- | ------------- | ------ | ----- |
| First run           | 2.29  | 1.02  | 0.49  | 1.42        | 0.15          | 0.18   | 5.6   |
| 12-hour soak        | 2.57  | 1.37  | 0.80  | 1.43        | 0.16          | 0.18   | 6.5   |
| 100 failing Pods    | 3.21  | 0.97  | 0.78  | 1.52        | 0.18          | 0.18   | 6.8   |
| 8 questions at once | 4.04  | 1.39  | 2.00  | 1.52        | 0.16          | 0.19   | 9.3   |
| Live, 80 clusters   | 2.65  | 0.60  | 0.33  | 1.43        | 0.18          | 0.16   | 5.3   |

"Others" are the A2A gateway, callout, verifier and console, NATS, fluent-bit, hermes-bridge and
the operator.

### Agent memory against fleet size (GB, M)

| Fleet              | Nodes | Pods   | Agent | Proxy | Shell | Event watcher | LiteLLM |
| ------------------ | ----- | ------ | ----- | ----- | ----- | ------------- | ------- |
| 2 small clusters   | 7     | 259    | 1.81  | 0.97  | 0.44  | 0.15          | 0.67    |
| 8                  | 25    | 677    | 2.25  | 0.75  | 0.48  | 0.14          | 0.67    |
| 16                 | 32    | 1,076  | 2.45  | 1.58  | 0.88  | 0.14          | 0.68    |
| 32                 | 64    | 2,048  | 1.86  | 0.70  | 0.82  | 0.14          | 0.67    |
| 40 small (Phase 4) | 80    | 2,541  | 3.30  | 0.84  | 0.90  | 0.36          | 0.67    |
| 40, real install   | 255   | 18,619 | 4.04  | 1.57  | 2.02  | 0.19          | 0.76    |

### Room left at the L size, at 42 clusters × about 6 nodes (M)

| Limit                               | Use                                             | Room left                           |
| ----------------------------------- | ----------------------------------------------- | ----------------------------------- |
| Agent memory                        | 2.6 GB of 16 GB (4.0 GB at 5 questions at once) | About 4×                            |
| Credential proxy                    | 1.4 GB of 8 GB; 3.5 of 8 CPU                    | About 2× on CPU                     |
| Event watcher                       | 0.16 GB of 4 GB                                 | About 20×                           |
| Sandbox shell (fixed 2 CPU / 2 GiB) | 0.8 GB steady; 2.0 GB at 5–8 questions at once  | About 2× idle; none under questions |
| Stall Watch coverage                | About 70 namespaces per 30-minute run           | Each cluster every 7–12 hours       |

### From events to memory (M)

| Step                                      | Measured                                                                                                         |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Nodes and Pods to events                  | 2.7 events per minute with the watcher blind; 565–815 when it watched 42 clusters; about 3,000 in the churn test |
| Events to triage cards                    | 190 Pod-triage cards on 2026-11-02 04:47–15:50; 143 waiting at 80 clusters                                       |
| Cards to running workers                  | At most 6 at once; 5 or 6 for 42% of the time                                                                    |
| Each running worker to memory             | About 254 MB worker + 2 × 167 MB `mcp-remote` bridges ≈ 590 MB                                                   |
| Each worker's commands to proxy and shell | 46–99 MB per `kubectl` or `gcloud` process, and the output in the shell                                          |

### Agent memory against event load, at the L size (M)

| Window                                      | Event watcher      | Events per minute | Agent peak |
| ------------------------------------------- | ------------------ | ----------------- | ---------- |
| First run                                   | Watched no cluster | 2.7               | 2.29 GB    |
| 20 failing Pods                             | Watching all       | 575               | 2.79 GB    |
| Churn at about 800 Pods per minute          | Watching all       | 2,993             | 3.18 GB    |
| 100 failing Pods                            | Watching all       | 725               | 3.21 GB    |
| 5–8 questions at once with the triage queue | Watching all       | About 570         | 4.04 GB    |

The event watcher's own container stayed at 0.15–0.19 GB in these windows.

### What holds the memory inside each container (M, C)

Read at 80 clusters with `kubectl top` and each process's proportional set size.

| Container        | What holds the memory                                                                                                                                                      | Grows with                     |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| Agent            | Hermes gateway about 0.6 GB; MCP supervisors and router about 0.2 GB; per running task about 590 MB (worker about 254 MB, two `mcp-remote` Node bridges about 167 MB each) | Tasks running at once, 0 to 6  |
| LiteLLM          | One Python process of about 735 MB per replica                                                                                                                             | Nothing measured               |
| Credential proxy | `credential_proxy.py` about 270 MB, Envoy about 75 MB, one process per command in flight (`kubectl` 46–91 MB, `gcloud` 68–99 MB)                                           | Commands in flight, 0 to 8     |
| Sandbox shell    | A `kubectl` wrapper per command (about 28 MB) and the output it holds                                                                                                      | Commands × output size; no cap |
| Event watcher    | Go informers, one per cluster                                                                                                                                              | Barely, on these clusters      |

The task cap is `kanban.max_in_progress = 6` (`defaultKanbanMaxInProgress`,
`k8s-operator/internal/controller/platformagent_manifests.go:1138`). The agent's peak is about
0.8 GB + 0.59 GB × 6 ≈ 4.3 GB, which matches the 4.04 GB measured.

### Processes in the agent container at 80 clusters (M)

`kubectl top` read a working set of 2,106 MiB. Per process (proportional set size), which adds up
to more than the working set because shared pages count differently:

| Processes                                                                 | Count | Memory   | Each         |
| ------------------------------------------------------------------------- | ----- | -------- | ------------ |
| `mcp-remote` bridges (Node `proxy.js`), children of Cluster Agent workers | 10    | 1,671 MB | About 167 MB |
| Cluster Agent workers (`hermes -p cluster-…`)                             | 5     | 1,270 MB | About 254 MB |
| Hermes gateway                                                            | 2     | 599 MB   |              |
| MCP supervisors and router                                                | 7     | 209 MB   |              |

| Credential proxy processes | Memory        |
| -------------------------- | ------------- |
| `credential_proxy.py`      | About 270 MB  |
| Envoy                      | About 75 MB   |
| `gcloud` (bundled Python)  | 68–99 MB each |
| `kubectl` children         | 46–91 MB each |
| `gke-gcloud-auth-plugin`   | About 20 MB   |

### Memory over time against running work (GB, M)

From Cloud Monitoring (`kubernetes.io/container/memory/used_bytes`, non-evictable, 5-minute
maximum), against the tasks and scheduled jobs that ran at each moment.

| First run (UTC) | Agent     | Proxy     | Shell     | Running tasks        | Scheduled job                 |
| --------------- | --------- | --------- | --------- | -------------------- | ----------------------------- |
| 16:20           | 2.11      | 0.56      | 0.03      | 5 onboarding         |                               |
| 16:35           | 2.29      | 0.60      | 0.09      | 5 onboarding         | Stall Watch                   |
| 16:50           | 1.52      | 0.55      | 0.14      | 2 onboarding         | Stall Watch                   |
| 17:20           | 1.21      | 0.67      | 0.34      | none                 | Stall Watch, compliance audit |
| 17:50           | 1.20      | 1.02      | 0.33      | none                 | Stall Watch, stockout audit   |
| 18:05           | 2.00      | 0.85      | 0.49      | 3 Stall Watch triage | upgrade retrospective         |
| 18:20 onward    | 0.73–0.78 | 0.47–0.55 | 0.18–0.20 | none                 | Stall Watch only              |

| Soak: what ran                    | Agent     | Proxy     | Shell     |
| --------------------------------- | --------- | --------- | --------- |
| Stall Watch sweep only            | 1.1–1.6   | 0.52–0.77 | 0.22–0.54 |
| 1–3 Stall Watch triage tasks      | 1.8–2.35  | 0.6–0.94  | 0.22–0.55 |
| 1 event-triage task               | 1.85–2.57 | 0.56–0.63 | 0.25–0.54 |
| Stockout audit and 2 triage tasks | 2.31      | 1.37      | 0.36      |
| Upgrade retrospective             | 1.88      | 0.82      | 0.80      |

The event watcher watched no cluster during the first run (finding 9 in the results), and in the
soak at most 1 event-triage task ran at a time. Its own container used 0.15–0.36 GB.

## Agent tasks

Every worker task fell into one of five categories (M, from `kanban.db`).

| Category                         | Soak, 42 clusters: tasks | Soak: worker time | 2026-11-02 04:47–15:50: tasks | Worker time  | Minutes each |
| -------------------------------- | ------------------------ | ----------------- | ----------------------------- | ------------ | ------------ |
| Stall Watch triage               | 92% (69)                 | 91% (8.0 h)       | 19% (48)                      | 43% (14.2 h) | 18           |
| Event watcher triage             | 8% (6)                   | 9% (0.8 h)        | 75% (190)                     | 39% (13.0 h) | 4            |
| User question: fleet health      | 0                        | 0                 | 2% (4)                        | 11% (3.6 h)  | 54           |
| First-run onboarding             | 0                        | 0                 | 3% (8)                        | 6% (2.1 h)   | 16           |
| User question: upgrade readiness | 0                        | 0                 | 1% (2)                        | 1% (0.4 h)   | 13           |

In the soak an average of 0.8 tasks ran at a time. On 2026-11-02 the average was 3.0 and the
maximum 6, with 5 or 6 running for 42% of the time. At 80 clusters, 143 event-triage cards waited
while 5 ran. Duplicate Pod-triage cards for one workload were 6% (11 of 188).

## What the model tokens pay for

From each profile's session store (`session_model_usage`, `messages`), 2026-11-02 04:47 to
15:50 UTC, about $352 of tokens (M).

### Tool calls by kind (11,282 calls, 30.5 MB returned)

| Kind                                  | Calls | Returned data | Median / p95 result |
| ------------------------------------- | ----- | ------------- | ------------------- |
| `kubectl get`                         | 45%   | 38%           | 383 B / 9.3 KB      |
| `kubectl describe`                    | 7%    | 7%            | 590 B / 21 KB       |
| `kubectl top`, `api-resources`, other | 1%    | 1%            |                     |
| `gcloud container`                    | 9%    | 5%            | 226 B / 7.0 KB      |
| `gcloud compute`, `logging`, other    | 1%    | 2%            |                     |
| GKE MCP through `tool_call`           | 6%    | 13%           | 1.1 KB / 15 KB      |
| Skill scripts (Python)                | 3%    | 3%            |                     |
| File read and write                   | 6%    | 9%            |                     |
| Reading skills and tool docs          | 5%    | 8%            | 0.8 KB / 11 KB      |
| Task board (kanban)                   | 5%    | 8%            |                     |
| Shell text tools and other terminal   | 10%   | 6%            |                     |
| Web search, `curl`                    | 1%    | 1%            |                     |

Logs were almost never read: 1 `kubectl logs` call and 20 `gcloud logging` calls.

### Token cost by capability

| Capability                                   | Cost share | Sessions | Model calls per session | Prompt per call | Cost per session |
| -------------------------------------------- | ---------- | -------- | ----------------------- | --------------- | ---------------- |
| Stall Watch triage                           | 36%        | 96       | 47                      | 69k tokens      | $1.32            |
| Event watcher triage                         | 32%        | 246      | 16                      | 64k tokens      | $0.45            |
| User questions: fleet health                 | 10%        | 10       | 71                      | 122k tokens     | $3.39            |
| Cluster Agent sessions not matched to a task | 8%         | 12       | 85                      | 74k tokens      | $2.41            |
| First-run onboarding (S-size run)            | 7.5%       | 8        | 122                     | 93k tokens      | $3.30            |
| Scheduled audits                             | 5.5%       | 12       | 42                      | 82k tokens      | $1.61            |
| User questions: upgrade readiness            | 1%         | 4        | 24                      |                 | $1.04            |

### Tool kinds in each capability (top five, M)

| Capability                   | Tool kinds                                                                                         |
| ---------------------------- | -------------------------------------------------------------------------------------------------- |
| Stall Watch triage           | `kubectl get` 53%, `gcloud container` 11%, `kubectl describe` 10%, GKE MCP 6%, skill scripts 5%    |
| Event watcher triage         | `kubectl get` 50%, GKE MCP 10%, skill and tool docs 9%, `gcloud container` 9%, task board 9%       |
| User questions: fleet health | Other terminal 21%, file read/write 20%, shell text tools 16%, `kubectl get` 11%, skill scripts 9% |
| First-run onboarding         | File read/write 35%, `kubectl get` 27%, other terminal 24%, shell text tools 4%, task board 3%     |
| Scheduled audits             | Other terminal 26%, file read/write 25%, shell text tools 13%, skill scripts 10%, GKE MCP 8%       |

### Why the tokens are high

| Possible cause      | Data                                                                                                                                                                                                                                                                               | Verdict     |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| Large tool output   | 30.5 MB returned is about 7.6 M tokens, under 1% of about 877 M prompt tokens; median result 403 B                                                                                                                                                                                 | Small cause |
| Too many processes  | At most 6 workers; duplicate cards 6%                                                                                                                                                                                                                                              | Small cause |
| History sent again  | Each call sends the system prompt (about 11k tokens, 43,891 characters) and all earlier turns: 64–69k tokens per call in triage. Each turn adds about 940 tokens: 61% tool result, 38% tool-call JSON with thought signatures. Each tool result is sent again about 100–130 times. | Main cause  |
| Many turns per task | A Stall Watch triage task takes 47 model calls, about 3.3 M prompt tokens                                                                                                                                                                                                          | Multiplier  |

The prompt cache served 83% of the input tokens, at $0.15 instead of $1.50 per million.

## Credential proxy requests

From the record's `proxy_commands` and the proxy's request log in Cloud Logging (M).

| Period               | Commands (`/v1/exec`) | Per hour | Per second | All requests per hour |
| -------------------- | --------------------- | -------- | ---------- | --------------------- |
| First 2 hours        | 10,452                | 5,226    | 1.45       | 5,944                 |
| First run, hours 2–5 | 18,493                | 6,113    | 1.70       | 6,581                 |
| 12-hour soak         | 72,830                | 6,069    | 1.69       |                       |

| Endpoint (first run, 31,796 requests) | Share |
| ------------------------------------- | ----- |
| `/v1/exec`                            | 91.0% |
| `/healthz`                            | 5.7%  |
| `/v1/mcp/gke`                         | 1.5%  |
| `/v1/mcp/developer_knowledge`         | 1.4%  |

An average of 1.2 commands were in flight in the first run and 0.9 in the soak. From 18:20 to
21:00 UTC no agent task ran, and the proxy still ran about 6,200 commands per hour, so most
commands come from the Stall Watch sweep (inferred; the log does not name the caller).

## Cost

### Prices (Cloud Billing Catalog API)

| SKU                                               | Price                     |
| ------------------------------------------------- | ------------------------- |
| Gemini 3.5 Flash Global Text Input - Predictions  | $1.50 per million tokens  |
| Gemini 3.5 Flash Global Text Input Caching        | $0.15 per million tokens  |
| Gemini 3.5 Flash Global Text Output - Predictions | $9.00 per million tokens  |
| E2 Instance Core, Americas (us-central1)          | $0.02181159 per vCPU-hour |
| E2 Instance Ram, Americas (us-central1)           | $0.00292353 per GiB-hour  |
| Balanced PD Capacity (us-central1)                | $0.10 per GiB-month       |
| Zonal Kubernetes Clusters                         | $0.10 per hour            |

The install's LiteLLM runs with `VERTEXAI_LOCATION=global`, so the global SKUs apply. Regional
SKUs cost 10% more.

### Token counts from two sources

| Window       | Runner's LiteLLM counters (input / cached / output) | Vertex AI `token_count` in Cloud Monitoring (input / output) |
| ------------ | --------------------------------------------------- | ------------------------------------------------------------ |
| First run    | 287,578,240 / 242,440,461 / 793,483                 | 296.9 M / 0.80 M                                             |
| 12-hour soak | 482,263,605 / 398,552,265 / 1,488,761               | 455.4 M / 1.43 M                                             |

The hour buckets do not line up with the window edges, which explains the 3–6% difference.

```text
first run: (287,578,240 − 242,440,461) × $1.50/M + 242,440,461 × $0.15/M + 793,483 × $9.00/M
         = $67.71 + $36.37 + $7.14 = $111.21 (5.0 h)
soak:      (482,263,605 − 398,552,265) × $1.50/M + 398,552,265 × $0.15/M + 1,488,761 × $9.00/M
         = $125.57 + $59.78 + $13.40 = $198.75 (12.0 h) = $16.56 per hour
```

### Token cost per hour, first run against later hours

| Period               | Input tokens | Cost   | Per hour               |
| -------------------- | ------------ | ------ | ---------------------- |
| Hour 1 of onboarding | 130.9 M      | $50.2  | $50.2                  |
| Hour 2               | 58.3 M       | $22.6  | $22.6                  |
| First 2 hours        | 189.2 M      | $72.8  | $36.4                  |
| Hours 3–5            | 107.7 M      | $41.9  | $14.0                  |
| 12-hour soak         | 482 M        | $198.7 | $16.6 (hourly $9.5–21) |

Hour 1 lost about 30 minutes to an IAM strip, and the event watcher watched no cluster in the
first run, so the first 2 hours are a lower bound (about $75–150).

The hourly series reproduces in Metrics Explorer with this PromQL:

```promql
sum by (type) (increase(aiplatform_googleapis_com:publisher_online_serving_token_count{
  monitored_resource="aiplatform.googleapis.com/PublisherModel",
  model_user_id="gemini-3.5-flash"}[1h]))
```

### Cost of one install at 42 clusters, steady operation

| Item                                 | Per hour | Per day | Per month |
| ------------------------------------ | -------- | ------- | --------- |
| Host nodes (2 × e2-standard-4)       | $0.27    |         |           |
| GKE cluster fee                      | $0.10    |         |           |
| Disks (200 GB boot, 142 GiB volumes) | $0.05    |         |           |
| GKE and GCP total                    | $0.42    | $10     | $300      |
| Model tokens                         | $16.6    | $398    | $12,000   |
| Total                                | $17      | $410    | $12,300   |

First month of one install: $73 for the first 2 hours and $11,919 for the other 718 hours, about
$12,000 in tokens. Questions add $2.50–24 each. For the largest example fleet (2,500 clusters,
63 to 105 installs) the same rates give about $25,000–42,000 per day in tokens and about
$630–1,050 per day on GKE.

The test fleet itself is not a cost of kube-agents. With the two signal clusters and the churn
pools in place, it cost:

| Item                                               | Per hour     |
| -------------------------------------------------- | ------------ |
| 162 × e2-small (fixture pools)                     | $2.71        |
| 85 × e2-standard-2 (80 test, 5 signal)             | $5.70        |
| 12 × e2-standard-4 (two churn pools on `scale-01`) | $1.61        |
| 43 GKE cluster fees                                | $4.30        |
| About 13,100 GB pd-balanced boot disks             | $1.80        |
| Total                                              | About $16.10 |

## Token cost against fleet size

`real-install-scaling.yaml` measures each fleet size the same way. 42 clusters is measured by the
12-hour soak ($16.6 per hour, $398 per day).

| Window                                   | What it gives                                                                |
| ---------------------------------------- | ---------------------------------------------------------------------------- |
| 30-minute settle after the fleet changes | Lets the triage burst of a gateway restart pass                              |
| 3-hour steady window                     | Background token cost per hour (triage, Stall Watch follow-ups, hourly jobs) |
| The 7 daily audits started together      | Token cost of one day's audits                                               |

A day's cost is 24 steady hours plus one round of audits. The levels are 80 clusters, then 20; 10
and 120 follow if the curve is unclear. 120 clusters needs about 760 external IP addresses against
the test project's quota of 575.

Prediction, made before the run: the cost grows less than linearly. Triage grows with incidents,
but Stall Watch reaches only a few clusters per run and the alert ceiling suppresses most event
alerts, and neither of those grows with the fleet.

### Results so far (M)

| Window                         | Clusters | Nodes | Pods   | Input tokens (cached) | Token cost | Per hour | Agent peak | Waiting cards at the end |
| ------------------------------ | -------- | ----- | ------ | --------------------- | ---------- | -------- | ---------- | ------------------------ |
| 12-hour soak (no backlog)      | 40       | 254   | 17,439 | 482.3 M (83%)         | $198.7     | $16.6    | 2.57 GB    | none                     |
| 80-cluster steady, 3 h         | 80       | 482   | 34,529 | 526.2 M (84%)         | $204.0     | $68.0    | 3.43 GB    | 96                       |
| 80-cluster daily audits, 1.5 h | 80       | 482   | 34,529 | 189.2 M (84%)         | $87.2      | $58.0    | 4.67 GB    | 303                      |

The 80-cluster daily audits did not all finish in their window: 5 of 7 completed, and the
compliance, stockout and GCE audits were still running at its end.

The first 20-cluster windows are not valid. At their end, 345 of the 405 waiting cards belonged to
clusters the runner had already deleted (`scale-21` to `scale-80`), left from the 80-cluster level,
so the windows measured that backlog: $69.5 per hour steady, and an agent peak of 5.29 GB. The
20-cluster level is being measured again after the deleted clusters' cards were archived.

What the data shows so far: the steady token cost follows how busy the 6 worker slots are. With a
backlog that keeps the slots full, it was about $68–70 per hour at both fleet sizes; without one, at
42 clusters, it was $16.6 per hour. The backlog formed at 80 clusters (96 cards waiting after the
steady window) and not in the 42-cluster soak. The agent's peak passed the 4.3 GB the six-worker
estimate gives, which agrees with #3937 (memory kept after worker bursts).

## What the install runs and what grows

"Capped" means growth stops at a fixed limit. Flags are Y or N.

### Always-on services

| What                                       | Clusters | Nodes per cluster | Workloads | Capped | Memory                                                                                   | Source |
| ------------------------------------------ | -------- | ----------------- | --------- | ------ | ---------------------------------------------------------------------------------------- | ------ |
| Hermes gateway                             | N        | N                 | N         | Y      | About 0.6 GB                                                                             | M      |
| Agent worker tasks                         | Y        | Y                 | Y         | Y      | About 0.59 GB each, 6 at most                                                            | M, C   |
| LiteLLM (2 replicas)                       | N        | N                 | N         | Y      | About 0.7 GB each                                                                        | M      |
| Event watcher                              | Y        | Y                 | Y         | N      | 0.15–0.36 GB on small clusters; 2.0 GB at 2 GiB on a 29-cluster production fleet (#4512) | M, #   |
| Credential proxy                           | Y        | Y                 | Y         | Y      | About 0.35 GB + 46–99 MB per command, 8 at most                                          | M, C   |
| Sandbox shell                              | N        | Y                 | Y         | N      | 0.1–2.0 GB, fixed limit 2 GiB                                                            | M, C   |
| A2A components, NATS, fluent-bit, operator | N        | N                 | N         | Y      | Under 0.2 GB together                                                                    | M      |

### Scheduled jobs

| Job                                                                                                                                                  | When            | Model session | Clusters | Nodes per cluster | Workloads | Notes                                             |
| ---------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- | ------------- | -------- | ----------------- | --------- | ------------------------------------------------- |
| Stall Watch sweep                                                                                                                                    | Every 30 min    | N             | Y        | N                 | Y         | 25-minute budget; about 21 s per namespace        |
| Cluster Agent reconcile                                                                                                                              | Hourly          | N             | Y        | N                 | N         | One profile per cluster, up to 21 MB each         |
| Daily audits (compliance, obtainability, AI security, stockout, networking, GCE)                                                                     | 06:20–09:20     | Y             | Y        | Y                 | Y         | 0.1–0.35 M tokens per cluster per audit (Phase 4) |
| Weekly audits (security patch, cost, consistency drift, upgrade retrospective)                                                                       | Weekly          | Y             | Y        | Y                 | Y         | Same collectors                                   |
| Upgrade-readiness watch, review, brief                                                                                                               | Daily, Saturday | Brief only    | Y        | N                 | N         |                                                   |
| Housekeeping (findings nudge, chat-delivery watch, board health, workspace cleanup, feedback, end-of-day report, sa-role-watch, github-repo-watcher) | Hourly to daily | N             | N        | N                 | N         | Fixed per install                                 |
| Profile cron tick, `oobe`                                                                                                                            | Every minute    | N             | N        | N                 | N         | `oobe` works once                                 |

### Work the jobs and services create

| Work                 | Created by                                     | Clusters | Nodes per cluster | Workloads | Cost each                   |
| -------------------- | ---------------------------------------------- | -------- | ----------------- | --------- | --------------------------- |
| Stall Watch triage   | Stall Watch alert                              | Y        | N                 | Y         | About $1.32, 47 model calls |
| Event triage         | Event watcher, through the daily alert ceiling | Y        | Y                 | Y         | About $0.45, 16 model calls |
| First-run onboarding | `oobe`, once                                   | Y        | Y                 | Y         | $73 in the first 2 hours    |
| User question        | A person                                       | N        | Y                 | Y         | $2.50–24                    |

## Settings that bound the work, with code locations

| Setting                                                                                   | Where                                                                             | Effect                                                                      |
| ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `spec.harness.tuning.maxInProgress` (default 6)                                           | `common_types.go:767`; `platformagent_manifests.go:1138`                          | Bounds the worker tasks: agent memory ≈ 0.8 GB + 0.59 GB × value            |
| `spec.harness.eventWatcher.enabled` (default true)                                        | `common_types.go:516`                                                             | Starts or stops the event watcher; documented as the emergency stop         |
| `ALERT_DAILY_LIMIT_CRITICAL`, `_WARNING`, `_INFO`, `_DRIFT` through `spec.deployment.env` | `session_kv_server.py:780-806`; allowed at `platformagent_manifests.go:4932-4943` | Daily ceiling on alerts that start a triage session (defaults 10, 5, 5, 20) |
| `spec.harness.stallWatch.excludeNamespaces`, `.minStallAgeMinutes`, `.kinds`              | `common_types.go:645-690`                                                         | Narrows the Stall Watch sweep and its alerts; no `enabled` field            |
| `spec.scope.exclude.clusters`                                                             | CRD scope                                                                         | No Cluster Agent and no watching for the excluded clusters                  |
| `spec.harness.tuning.maxSessions` (default 10, `mode: next`)                              | `common_types.go:769-838`                                                         | Bounds the A2A session Pods                                                 |
| Scheduled job `enabled`                                                                   | Image-owned (`agents/chat/scripts/cron_jobs_sync.py:36-41`)                       | Turns one job off only through the image, not the CR                        |
| Event watcher reasons                                                                     | Fixed in `deploy/shared/start-services.sh` (`--reason=`)                          | No CR field                                                                 |
| Credential proxy slots (8) and output cap (8 MiB)                                         | `credential_proxy.py:261`; `platformagent_manifests.go:81-82`                     | No CR field                                                                 |
| Sandbox shell size (2 CPU / 2 GiB)                                                        | `shell_sandbox_manifests.go`                                                      | No CR field                                                                 |

## What the test does not cover

| Measured point                               | Nodes per cluster | Pods per cluster | Result                                                                           |
| -------------------------------------------- | ----------------- | ---------------- | -------------------------------------------------------------------------------- |
| This test                                    | About 6           | About 435        | The tables above                                                                 |
| Phase 2, `listing_pods` factor               | 50                | About 4,500      | Listing 87–124 s; fleet audit 8–30 min; proxy up to 0.74 GB; shell up to 0.32 GB |
| Phase 1, `nodes` factor (event watcher only) | 100               | About 1,000      | Event watcher 0.18 GB                                                            |

Open issues report what larger clusters reach: event watcher memory (#4512) and logging (#4513),
silent truncation at the 8 MiB output cap above about 250–450 nodes per cluster (#4335), `kubectl`
children with no size cap (#2045), and proxy restarts on a fleet of tens of thousands of nodes
(#4315). #4529 lists all 28 gaps for 50 clusters × 2,000 nodes.

### Projection for clusters of hundreds of nodes (not measured)

At the growth the `listing_pods` runs show, a cluster of 1,000 nodes (20,000 to 40,000 Pods):

1. Needs about 1.5–2.3 GB in the sandbox shell to list its Pods in full, at the shell's fixed
   2 GiB.
2. Makes questions and audits slower in proportion to the Pods listed, and Stall Watch reaches
   fewer clusters per run.
3. Needs about 3–5.5 GB in the credential proxy for the same listing, inside the L size.
4. Makes tool outputs larger, so each question uses more tokens or sees truncated output.
5. Leaves the event watcher's memory flat on these clusters, though #4512 shows it at 2.0 GB on a
   production fleet.

### Breakpoints at 5,000 nodes per cluster (20 clusters, 100,000 nodes)

| Limit                               | Evidence                                                                  | At 5,000 nodes per cluster                                              |
| ----------------------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Full listings cut off silently      | #4335: collectors fail above about 250–450 nodes per cluster              | Every fleet audit, the first-run inventory and the upgrade scripts fail |
| `kubectl` children with no size cap | #2045: 4,000 Pods took 432 MiB (JSON) and 1,281 MiB (YAML)                | One full listing of about 150,000 Pods needs tens of GB                 |
| Credential proxy restarts           | #4315: 18 restarts in 11.5 hours on a fleet of tens of thousands of nodes | Already seen at this scale                                              |
| Event-watcher floods                | #4513, #660, #4331                                                        | Pending-Pod and autoscaling churn multiply all three                    |
| Stall Watch                         | M: about 21 s per namespace, 70 namespaces per run                        | One pass takes days                                                     |
| Collector sweeps                    | #4081: 4 clusters at a time, no fleet store                               | Hours to days per audit                                                 |

### What can be turned off, and what it costs

| Feature                                                                   | Why it matters at large cluster sizes                         | How                                                                      | What is lost                      |
| ------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------- |
| Daily and weekly fleet audits                                             | Full `-A -o json` collectors fail above 250–450 nodes (#4335) | `enabled: false` in `agents/platform/cron/jobs.json` (image)             | Posture, cost and capacity audits |
| First-run onboarding (`oobe`)                                             | Same collectors                                               | `enabled: false` in `agents/chat/defaults/cron/jobs.json` (image)        | The onboarding inventory          |
| Stall Watch                                                               | Days per pass; 36% of tokens                                  | Narrow with `spec.harness.stallWatch`; turn off in the image             | Stuck-controller detection        |
| Event reasons `FailedScheduling`, `TriggeredScaleUp`, `NotTriggerScaleUp` | Continuous on large autoscaling clusters (#4513)              | Fixed in `start-services.sh` (image); lower the alert ceilings meanwhile | Scale-up event alerts             |
| Event watcher entirely                                                    | Floods affect logging and triage                              | `spec.harness.eventWatcher.enabled: false`                               | All incident detection            |
| Worker slots                                                              | Bounds memory                                                 | `spec.harness.tuning.maxInProgress: 3`                                   | Longer triage queue               |

The token-scaling run at 80 and 20 clusters (`real-install-scaling.yaml`) is in progress; its
results come in a later change.
