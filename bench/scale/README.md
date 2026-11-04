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
what the test clusters really had, summed over the fleet; fits use the totals for the factors the totals count (clusters, nodes, objects), the achieved rate for churn (`churn_achieved_per_minute`), and `values` for the rest. `limits` and
`replicas` are read from the live pods during the run, and `mode` from the install's `PlatformAgent` (`today` when its spec leaves the mode unset). `answers` holds the start of each prompt's answer, with the project ID replaced by `<project>`, so a response time can be checked against what the agent said. `runner` is the measurement runner's own commit, so a change in how a metric is read shows in the data. A metric without `container` belongs to the
run. `excluded_from` names the fits the record is left out of: `["response_seconds"]` when a prompt
run saw a provider 429 or an unfinished prompt, or, for a fleet audit or upgrade readiness record written
before answers were kept, when its completion cannot be checked. `python3 -m kube_agents_bench.scale.runner reclassify <plan>` re-applies these rules to the stored records. `errors` lists what made the run differ from its plan: a failed prompt, a
cluster the event watcher did not watch, a churn batch that failed, a monitoring read that failed.

Metric names:

- per container: `memory_peak_bytes`, `cpu_peak_cores`, `restarts`, `oomkills`;
- the data volume: `pvc_used_bytes`, `pvc_capacity_bytes`, `profile_bytes_max`, `profiles`;
- the credential proxy: `proxy_wait_p50_seconds`, `proxy_wait_max_seconds`, `proxy_refusals`;
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
