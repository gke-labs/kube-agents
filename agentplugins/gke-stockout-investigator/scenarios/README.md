# Stockout investigation scenarios

Scripts that trigger a GKE Stockout Investigator run on demand, one per kind of
capacity failure. Each one wedges a real workload on the target cluster and then
publishes the matching scale-up failure alert, so an investigation starts immediately
instead of whenever the autoscaler next emits its periodic log.

They serve two callers. Run by hand they are demo and skill-exercise tools; they are
also driven by the E2E suite, where `tests/e2e/test_stockout_investigation.py` executes
`<slug>.sh --teardown` and fails on a non-zero exit. The RC promotion gate runs scenario
04 and the nightly matrix runs whatever `STOCKOUT_SCENARIOS` selects, so a scenario that
exits non-zero reds a release gate. `verify.sh` in the parent directory is the narrower
probe the same suite runs first: it checks only that an alert reaches the agent.

## Why each scenario deploys a workload

The alert payload carries almost nothing: the cluster, the namespace, and the name of
the controller whose pods could not be scheduled. Everything in the diagnosis comes
from the agent inspecting the live cluster afterwards — pod events, ComputeClass
definitions, quotas, capacity advice.

So publishing an alert on its own does not test the skill. It tests the plumbing, and
the agent correctly concludes there is nothing wrong. That verdict is worth testing
too, which is what `10-false-signal.sh` is for; every other scenario deploys something
genuinely unschedulable first.

## The scenarios

The first eight map one-to-one onto the diagnostic rules in the skill, so a wrong
diagnosis points at a specific rule rather than at "the agent got it wrong".

| Script                               | Rule | Failure                                                        |
| ------------------------------------ | ---- | -------------------------------------------------------------- |
| `01-gpu-regional-scarcity.sh`        | E    | L4 GPUs exhausted in the workload's only permitted zone        |
| `02-gpu-quota-exceeded.sh`           | F    | 32 GPUs requested against a smaller regional quota             |
| `03-large-vm-shape-scarcity.sh`      | B    | Pinned to `c3-standard-176`, the rarest shape in the family    |
| `04-missing-zone-fallback.sh`        | A    | Ordinary workload pinned to one family in one zone             |
| `05-missing-ondemand-floor.sh`       | D    | Every ComputeClass priority is Spot; no on-demand tier         |
| `06-stateful-disk-generation-mix.sh` | C    | Volume type attaches on some offered generations, not others   |
| `07-hyperdisk-incompatibility.sh`    | H    | Hyperdisk on a class offering only pre-Hyperdisk families      |
| `08-ccc-priority-starvation.sh`      | G    | Over-granular priority list; the autoscaler loops              |
| `09-duplicate-signal.sh`             | —    | The same alert three times: dedup and duplicate-PR suppression |
| `10-false-signal.sh`                 | —    | Alert for a workload that is not in trouble; agent stands down |

Each script explains in its own `scenario_notes` what a correct diagnosis looks like
and which wrong answer it is designed to catch. Read that before judging a run.

## Running one

```bash
export GCP_PROJECT_ID=your-project
./04-missing-zone-fallback.sh              # deploy, alert, watch
./04-missing-zone-fallback.sh --cleanup    # tear down when finished
```

Cleanup is not automatic. A wedged workload keeps producing real autoscaler alerts for
as long as it is left in place, so remove it when you are done — or use `--teardown`
to have the script do it once the watch finishes.

Useful flags, all documented under `--help`:

| Flag              | Effect                                                          |
| ----------------- | --------------------------------------------------------------- |
| `--dry-run`       | Print the manifest and the alert payload; change nothing        |
| `--no-wait`       | Publish and return instead of watching for the investigation    |
| `--no-workload`   | Alert only, no deployment — the false-signal path               |
| `--via-sink`      | Route through Cloud Logging instead of publishing to the topic  |
| `--keep-dedup`    | Leave the dedup registry alone, so a repeat alert is suppressed |
| `--watch-timeout` | Seconds to watch before printing where to look (default 600)    |

## What the harness does for you

`lib/common.sh` holds everything the scenarios share. Three parts of it exist because
of failures that are easy to mistake for something else:

**It clears the dedup registry first.** The adapter deduplicates on cluster, namespace
and controller name for 24 hours. Re-running a scenario is the normal case here, so
without clearing it the second run is dropped silently and looks like a broken agent.
`09-duplicate-signal.sh` relies on this being the default: it clears once, then
publishes twice more to show the suppression working.

**It uses its own namespace.** Workloads go to `stockout-scenarios`, created on demand,
not to `default` — that namespace carries a `tenant-resource-limits` ResourceQuota
capping it at 4 vCPU of requests. A quota rejection lands at pod _creation_: the
ReplicaSet reports `FailedCreate` and no Pod object is ever made, so there is no
unschedulable pod and no stockout. The scripts detect that case and stop rather than
publish an alert about a workload that does not exist.

**It applies ComputeClasses before the workloads that name them.** On Autopilot the
admission webhook rejects a pod referencing a ComputeClass that is not yet in its
allowed list, and that list lags creation by a few seconds. The harness applies
cluster-scoped objects first and retries the workload apply while admission catches up.

## Verifying a run

The script prints the commands at the end. In short: the gateway log shows whether the
alert was accepted, filtered, or deduplicated; the sessions API shows the
investigation; and the GitOps repository shows whether a remediation PR was opened.

Two Autopilot constraints shape what the manifests can ask for. A single pod cannot
exceed 30 vCPU or 110Gi, so scarcity has to come from the ComputeClass rather than from
an enormous pod — a larger request is rejected at admission and never becomes a
stockout. GPU pods are subject to the same ceiling on their CPU and memory.

## Configuration

Defaults describe the development fleet and are all overridable:

`GCP_PROJECT_ID` and `TARGET_CLUSTER_NAME` are required — the cluster name must match the
adapter's filter expression, so a wrong one drops every alert and the run looks like the
agent ignored it. `MGMT_CONTEXT` defaults to the active kubectl context. Also
`TARGET_CLUSTER_LOCATION`,
`MGMT_CONTEXT`, `PROD_CONTEXT`, `AGENT_NAMESPACE`, `WORKLOAD_NAMESPACE`,
`STOCKOUT_TOPIC`, `GITOPS_REPO`.

`SCENARIO_MACHINES` pins the machine shapes a scenario wedges on, skipping whatever
discovery it would otherwise do. Only `05-missing-ondemand-floor.sh` reads it today, and
that scenario is the one prerequisite worth calling out: it locates a shape the region
offers on demand but not on Spot using `gcloud beta compute advice capacity`, so it
refuses to start without the `beta` component (`gcloud components install beta`) — in
preflight, before anything is deployed. Setting `SCENARIO_MACHINES` skips the probe and
the requirement with it, and `--cleanup` is exempt either way, so a workload left behind
can still be torn down on a machine without the component. The E2E workflows install
`beta` already; this bites a hand-run.

## Adding a scenario

Copy the shortest one, `10-false-signal.sh`, and set `SCENARIO_TITLE`, `SCENARIO_RULE`
and `SCENARIO_CONTROLLER`. Define `scenario_manifest` to echo the YAML that wedges,
`scenario_reasons` to echo the `rejectedMigs` and `napFailureReasons` fragment that
steers the diagnosis, and `scenario_notes` to say what a correct answer looks like.
Then call `scenario_main "$@"`. Check it with `--dry-run` before running it for real.
