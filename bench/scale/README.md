# bench/scale

## High level summary

The harness is the Platform Agent and its supporting components. This directory holds the plan and the results of a load measurement of the harness. The runner applies a fleet size to test clusters. It then runs agent operations and records what each container used. The admin console reads the results and gives container limits and a node size for a fleet.

## Details

Load and sizing measurements of the agent harness at fleet scale. The plan, the metrics and their
thresholds are in
[`docs/designs/agent-harness-bottlenecks.md`](../../docs/designs/agent-harness-bottlenecks.md).
The code is `kube_agents_bench/scale/`; this directory holds what it reads and writes, and the admin
console's Harness limits page reads `harness-limits.json`.

- `phase1.yaml`: the Phase 1 plan: target, factors and levels, operations and run counts. A later
  phase is another file of the same shape.
- `phase2.yaml`: the Phase 2 plan. Its base is Phase 1's, so an install that runs the same build
  reuses the Phase 1 base runs.
- `phase3.yaml`: the Phase 3 plan: every window 30 minutes, with the first-run audits that a new
  install starts late moved into a window of their own.
- `phase3-proxy-raised.yaml`: Phase 3's two windows at 2 and 16 clusters with the credential proxy's
  limits raised, so its CPU peak shows what it needs; set the limits before the runs and remove them
  after.
- `harness-limits.json`: every measured run. A run merges its records in and replaces only a record with the same key; a record leaves the file only by hand, as Phase 1's runs spoiled by an outside IAM change did before their re-runs.
- `harness-limits.sample.json`: a small fake data set the console page and its tests use when
  `harness-limits.json` does not exist.

## Running it

The plan names no project. Set the target in the environment; any key of the plan's `target` block
can be overridden as `SCALE_<KEY>`:

```bash
export SCALE_PROJECT_ID=<project>          # required
export SCALE_HOST_CLUSTER=scale-host       # the experiment install's cluster (default shown)
make scale-plan                            # print the matrix
make scale-run                             # run it, resuming from harness-limits.json
```

`SCALE_CONFIG` picks another plan file. The host context defaults to gcloud's name for the host
cluster, and the runner refuses a `SCALE_HOST_CONTEXT` that does not name it, because an onboarding
run resets the state volumes of the install that context reaches. The runner creates and deletes test
clusters named `<cluster_prefix>NN` in the target zone that carry the plan's `cluster_label`, and
creates or deletes no other cluster. On the host cluster, it restarts the agent's pods and, for each onboarding run, deletes and re-creates the install's state volumes; it refuses that reset unless the host cluster also carries the plan's `cluster_label`. The runner works on a private copy of your kubeconfig, deleted when it exits, so test-cluster contexts never reach your own kubeconfig and other tools' writes to it cannot drop a context mid-run.

`SCALE_PREFLIGHT` names a shell command the runner runs before each level and each run, for a
project where something outside the install can undo its preconditions: an organization's IAM
automation that strips service-account bindings, for one. A failing preflight is logged and the run
goes on.

The runner module takes more than the two targets. Install bench first (`make test-bench-deps`, or `uv sync` in `bench/`), so the harness the prompts go through can be imported:

```bash
cd bench
python3 -m kube_agents_bench.scale.runner run ../bench/scale/phase1.yaml --only-factor clusters --limit 3
python3 -m kube_agents_bench.scale.runner run <plan> --force          # re-run and replace existing keys
python3 -m kube_agents_bench.scale.runner apply <plan> --factor objects --level 4000
python3 -m kube_agents_bench.scale.runner teardown <plan>             # remove fixtures, shrink pools
python3 -m kube_agents_bench.scale.runner teardown <plan> --delete-fleet
```

An operation's `kind` says what a run does:

- `prompt` sends the operation's `prompt` through the agent API, and at a `concurrent` value of N
  sends N − 1 copies of its `follow_up` beside it;
- `soak` waits `window_seconds`; `restart_soak` restarts the gateway pod first; `reset_soak` resets
  the install's state volumes first;
- `proxy_load` runs the operation's `command` through `credential-proxy-exec` in the sandbox shell,
  from as many workers at once as the point's `proxy_concurrency` value, for `window_seconds`, with
  no model call;
- `trigger_soak` marks the cron `jobs` on the roster under `jobs_home` due at the window's start, as
  the OOBE job marks its first-run audits, and notes the ones not finished when the window ends. The
  next run waits up to `trigger_drain_seconds` (default 1800) for them; a later run that one still
  loads says so in `errors` and is left out of the resource fits.

A factor named `combined` has points rather than values as levels: level N applies the plan's Nth
`combined` point, which sets several factors at once. The levels run in the order the plan lists the
factors; with `base_last: true` the base point runs after them, so a plan whose levels shrink the
fleet ends at its smallest. The runner checks the plan before it starts, skips the settle time when
a level keeps the fleet of the one before, and stops with exit status 2 when a level cannot be
applied, so neither the next level nor a script's next step reshapes a half-applied fleet.

Settings with no default: `churn_pod_lifetime_seconds`, `fixture_machine_type`, `fixture_pool` and
`pods_per_node`. The others:

- `settle_seconds`: the wait after a level is applied;
- `trigger_reconcile`: run the Cluster Agent reconcile when the profiles differ from the test fleet,
  instead of waiting for its hourly run;
- `cluster_create_parallelism`: how many clusters or node pools change at once;
- `watcher_wait_seconds`: how long a restarted event watcher may take to report every test cluster;
- `default_pool_pods`: how many fixture Pods each cluster's default pool holds before a fixture pool
  is needed;
- `test_cluster_machine_type`: the machine type of new test clusters, for a zone short of the
  default one;
- `cluster_registration_timeout_seconds`: how long to wait for a Cluster Agent profile.
- `trigger_drain_seconds`: how long the run after a `trigger_soak` waits for its jobs to finish.

The agent API is reached through the repository's `kubectl exec` relay
(`scripts/hermes-dashboard-tunnel.py`), because the agent pod runs under gVisor
([`bench/README.md`](../README.md#sandboxed-installs)); the runner starts and stops it.

## `harness-limits.json`

```json
{
  "schema": 1,
  "records": [
    {
      "key": "eeb8a95c|next|clusters|8|fleet_audit|1",
      "commit": "eeb8a95ceb0a9efd66434161f87820b35b65360c",
      "mode": "next",
      "phase": "phase1",
      "factor": "clusters",
      "level": 8,
      "operation": "fleet_audit",
      "run": 1,
      "started": "2026-10-23T01:02:03Z",
      "ended": "2026-10-23T01:31:40Z",
      "image": "ghcr.io/gke-labs/kube-agents/platform-agent:eeb8a95ceb0a9efd66434161f87820b35b65360c",
      "runner": "537d72b571cc",
      "values": {
        "clusters": 8,
        "nodes": 2,
        "objects": 100,
        "churn_per_minute": 10,
        "failing_pods": 1,
        "concurrent": 1,
        "listing_pods": 100
      },
      "totals": {
        "clusters": 8,
        "nodes": 24,
        "pods": 820,
        "namespaces": 104,
        "deployments": 210,
        "services": 190,
        "hpas": 120,
        "configmaps": 610,
        "events": 9100,
        "pvcs": 0
      },
      "limits": {
        "agent-api-auth": { "memory_bytes": 2147483648, "cpu_cores": 1.0 }
      },
      "requests": {
        "agent-api-auth": { "memory_bytes": 402653184, "cpu_cores": 0.15 }
      },
      "replicas": { "agent-api-auth": 1, "litellm-container": 2 },
      "metrics": [
        {
          "name": "memory_peak_bytes",
          "container": "agent-api-auth",
          "value": 734003200
        },
        { "name": "response_seconds", "value": 812.4 }
      ],
      "provider_429s": 0,
      "excluded_from": [],
      "errors": []
    }
  ]
}
```

`key` is `commit[:8]|mode|factor|level|operation|run`; a record with the same key replaces the old
one. `commit` is the commit the agent image's tag pins (the tag itself when it is not a commit), read
from the install. The key holds the commit's first 8 characters, or the whole tag when the tag is not a commit, so a new build is a new set of keys. A base record has `factor` `base` and `level`
0; `values` holds every factor's planned value at the point, base records included. `totals` holds
what the test clusters really had, summed over the fleet; fits use the totals for the factors the totals count (clusters, nodes, objects), the achieved rate for churn (`churn_achieved_per_minute`), and `values` for the rest. `limits`, `requests` and
`replicas` are read from the live pods during the run (records written before the runner read requests have none), and `mode` from the install's `PlatformAgent` (`today` when its spec leaves the mode unset). `answers` holds the start of each prompt's answer, with the project ID replaced by `<project>`, so a response time can be checked against what the agent said. `runner` is the measurement runner's own commit, so a change in how a metric is read shows in the data. A metric without `container` belongs to the
run. `excluded_from` names the fits the record is left out of: `["response_seconds"]` when a prompt
run saw a provider 429 or an unfinished prompt, or, for a fleet audit or upgrade readiness record written
before answers were kept, when its completion cannot be checked. The CPU, memory and proxy metrics
are left out too when work other than the run's own shared the broker: jobs an earlier `trigger_soak`
started were still running; a prompt or boot-burst run started inside a Stall Watch read (2 minutes
plus 30 seconds per cluster after :00 or :30) or held a tick; a proxy load run's broker ran a command its
workers did not send (its throughput and tokens are left out as well). A prompt answered without its
operation is left out of those and of the token metrics. `python3 -m kube_agents_bench.scale.runner reclassify <plan>` re-applies these rules to the stored records whose operation the plan names, and warns how many it passed over; with a data file several plans wrote, run it once per plan. `remonitor` reads every stored record's Cloud
Monitoring metrics again with the current queries, within Monitoring's retention, and writes its own
revision in the record's `remonitored`; a record it could not read keeps the field as it was. `errors` lists what made the run differ from its plan: a failed prompt, a
cluster the event watcher did not watch, a churn batch that failed, a monitoring read that failed.

Metric names:

- per container: `memory_peak_bytes`, `cpu_peak_cores`, `cpu_seconds` (the CPU used over the window,
  summed over replicas: the work, which a container held at its CPU limit cannot show as a peak),
  `restarts`, `oomkills`;
- the data volume: `pvc_used_bytes`, `pvc_capacity_bytes`, `profile_bytes_max`, `profiles`;
- the credential proxy: `proxy_wait_p50_seconds`, `proxy_wait_max_seconds`, `proxy_refusals`,
  `proxy_commands` (brokered commands run) and `proxy_command_seconds` (the seconds they ran, summed);
- a proxy load run: `proxy_load_completed`, `proxy_load_errors`, `proxy_load_per_second` (commands
  completed per second), and `proxy_load_p50_seconds`, `proxy_load_p95_seconds`,
  `proxy_load_max_seconds` (each command's time as `credential-proxy-exec` saw it);
- the event watcher: `watcher_events_seen`, `watcher_events_seen_per_minute`,
  `watcher_session_creates`, `watcher_events_injected`, `watcher_quota_suppressed`;
- the agent: `triage_lost`, `run_slot_refusals`, `failed_prompts`, `unfinished_prompts`,
  `response_seconds` (the slowest prompt that completed its operation);
- tokens: `tokens_input`, `tokens_output`, `tokens_cached` (LiteLLM's counters over the run's window,
  every caller included; `tokens_input` counts the whole prompt, cached tokens included) and
  `run_tokens_input`, `run_tokens_output`, `run_tokens_cached` (the run's own prompts, as the harness
  reports them; `run_tokens_input` is the non-cached part of the prompt, so the run's share of the
  window is `run_tokens_input + run_tokens_cached` against `tokens_input`);
- the fixtures: `churn_achieved_per_minute`.
- the jobs a triggered window started, read after the drain: `job_seconds` (the slowest completed job's run
  time) and `jobs_completed`.
