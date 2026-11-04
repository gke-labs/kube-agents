# OOMKill reproduction on a KWOK fleet

## High level summary

This directory describes a manual test that reproduces OOMKills of the kube-agents harness. An
OOMKill is the kernel stopping a container that uses more memory than its limit. The test builds a
large fleet from KWOK (Kubernetes WithOut Kubelet) nodes. A KWOK node is a real Node object in a
real GKE cluster, but no virtual machine runs it. The fleet therefore costs little, and the agent
reads it as it reads a large fleet.

The test reproduces three failures at the operator's default memory limits:

- The sandbox shell runs the agents' terminals, scripts and `kubectl`. It is OOMKilled when an
  audit lists every Pod of two large clusters at the same time.
- On builds before the Kubernetes API proxy, `kubectl` ran inside the credential proxy instead,
  and the same audit OOMKilled the credential proxy.
- The event watcher sends cluster Events to the agent. It is OOMKilled at start-up when it reads
  the Events of several large clusters at the same time.

At larger limits on a larger fleet the failures still happen: all three at the harness's L size,
and the event watcher's at the fleet tier of the sizing page.

No CI job runs this test, and the nightly evaluation does not include it. You run it by hand
against an install and a GCP project of your own.

## What it reproduces

| Container (default limit)                                      | Build                           | Trigger                                                                                       | Share of memory at the kill                                                                         | Tickets                    |
| -------------------------------------------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------- |
| `shell` (2 CPU, 2 GiB)                                         | with the Kubernetes API proxy   | `compliance-audit` and the agents' own reads list every Pod of two large clusters             | `kubectl get … -A` lists 94 to 98 %, other 2 to 6 %                                                 | #2045                      |
| `envoy-credential-proxy` (2 CPU, 2 GiB)                        | before the Kubernetes API proxy | `compliance-audit` lists `deploy,sts,ds,cronjob,pod -A -o json` on two large clusters at once | `kubectl` children 91 %, the credential broker (`credential_proxy.py`) 4 %, Envoy 4 %, `hermes` 1 % | #2045, #2324, #2652, #4315 |
| `agent-api-auth`, the event watcher's container (1 CPU, 2 GiB) | both                            | start-up reads four clusters of 400,000 Events each                                           | `k8s-event-watcher` 97 %, the API authenticator (`credential_proxy.py` in that role) 3 %            | #2648, #4512               |

The shares are of the summed RSS (resident memory) of the container's processes in the killed
pod's last sample. The harness's reports:

- [`evidence/proxy-shell-main-b3d78bc8.md`](evidence/proxy-shell-main-b3d78bc8.md): the shell, on
  `main` commit `b3d78bc8` (2026-11-04). OOMKilled 3 times in about 2.5 minutes: twice with `kubectl get pods -A` holding 94 and 98 % of
  the memory, once with a `kubectl get deploy,sts,ds,cronjob,pod -A` dump at 96 %, the
  compliance-audit collector's (its `collect.py` was in the same sample).
  In a run outside the harness on the same build the shell went into `CrashLoopBackOff`. Every agent's terminal runs in the shell, so each kill ends all of them.
- [`evidence/proxy-main-fb56483b.md`](evidence/proxy-main-fb56483b.md): the credential proxy, on
  `main` commit `fb56483b` (2026-11-03), before the Kubernetes API proxy. OOMKilled once, and no second kill
  followed in the 20 minutes of the run; the proxy's log showed its resource watermark gate answering
  further calls with `429` and `503`. This report and the next come from an earlier revision of the
  harness, which did not yet write the `Samples:` line.
- [`evidence/watcher-main-fb56483b.md`](evidence/watcher-main-fb56483b.md): the event watcher, on
  `fb56483b`. OOMKilled 3 times in about 2 minutes, during its initial Event lists.

The evidence fleet had two full clusters of about 2,900 KWOK nodes and 80,000 Pods each and two
Events-only clusters, first built by hand for this test and then grown to
`--nodes 3000 --namespaces 300` with the cluster prefix `kwok-`; they carry the `purpose=oom-repro` label, so the harness finds
them. This harness builds a fleet of that shape with
`--prefix kwok- --nodes 3000 --namespaces 300 fleet create --clusters 2`, then
`--prefix kwok- fleet create --clusters 2 --first 3 --events-only`. The Pods-ready wait ended at about 80,000 of 90,000 per cluster, and more Pods turned ready afterwards (86,000 to
89,000 at the shell run). The global options are not saved between commands: give
`--prefix` to `fleet create`, `fleet load`, `events` and `fleet delete`, and the fleet's sizes (`--nodes`,
`--namespaces`, `--deployments`) to `fleet create`, `fleet load` and `events`. `proxy` and `watcher` use none of
them. The commands under "Run it" build the default
fleet of 1,500 nodes and 45,000 Pods per cluster.

To raise a limit the tests reach, see the fleet sizing page
([`fleet-sizing.md`](../../docs/site/src/content/docs/deploy/fleet-sizing.md)): the shell's limits
come from `spec.harness.experimental.shellSandbox.resources`.

## At the L container size

`--size L` sets this harness's L preset, a raised size for a large fleet: the
shell 8 CPU and 8 GB, the credential proxy 8 CPU and 8 GB, and the event watcher's container 2 CPU
and 4 GB, in decimal gigabytes. It is not the fleet tier of the sizing page
([`fleet-sizing.md`](../../docs/site/src/content/docs/deploy/fleet-sizing.md)), which uses binary
GiB (8 GiB and 4 GiB, about 7 % more), leaves the event watcher's CPU limit at 1, and leaves the shell
at 2 GiB. `--size fleet` sets that tier's limits; requests stay as they are. All three failures reproduce at L on a larger fleet;
shares under 1 % are left out of the table.

| Container (L limit)                                                           | Build      | Fleet                                                                          | Result                               | Share of memory at the kill                                                                                                      |
| ----------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------ | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `shell` (8 CPU, 8 GB)                                                         | `b3d78bc8` | two clusters of about 2,900 KWOK nodes and 86,000 to 89,000 Pods               | OOMKilled once                       | two `kubectl get deploy,sts,ds,cronjob,pod -A -o json` processes (3,857 and 3,500 MiB) 99 %                                      |
| `envoy-credential-proxy` (8 CPU, 8 GB)                                        | `fb56483b` | two clusters of about 2,900 KWOK nodes and 80,000 Pods                         | OOMKilled once                       | two `kubectl get deploy,sts,ds,cronjob,pod -A -o json` children (4,272 and 3,238 MiB) 97 %, Envoy 2 %, the credential broker 1 % |
| `agent-api-auth` (2 CPU, 4 GB)                                                | `b3d78bc8` | four clusters of 900,000 to 990,000 Events, 500,000 of them with 1 KB messages | OOMKilled 3 times in about 2 minutes | `k8s-event-watcher` 98.5 %, the API authenticator 1.5 %                                                                          |
| `agent-api-auth` (1 CPU, 4 GiB: `--size fleet`, the sizing page's fleet tier) | `b3d78bc8` | the same Event batches, about 900,000 per cluster                              | OOMKilled 3 times in about 4 minutes | `k8s-event-watcher` 98.6 %, the API authenticator 1.4 %                                                                          |

The reports are in [`evidence/proxy-shell-L-main-b3d78bc8.md`](evidence/proxy-shell-L-main-b3d78bc8.md),
[`evidence/proxy-L-main-fb56483b.md`](evidence/proxy-L-main-fb56483b.md) and
[`evidence/watcher-L-main-b3d78bc8.md`](evidence/watcher-L-main-b3d78bc8.md); the fleet-tier run is in
[`evidence/watcher-fleet-main-b3d78bc8.md`](evidence/watcher-fleet-main-b3d78bc8.md). At the fleet
tier the shell keeps its 2 GiB default, which the shell row of "What it reproduces" covers; the
sizing page's raises do not stop the event watcher's OOMKill.

To reach the L limits, grow the two full clusters to 3,000 KWOK nodes and 90,000 Pods, then, within
one hour, add both batches of Events and run `watcher`:

```bash
uv run python -m kube_agents_bench.oom_repro --nodes 3000 --namespaces 300 fleet load --names oom-01,oom-02
uv run python -m kube_agents_bench.oom_repro --host-context <context> proxy --size L   # --watch proxy on older builds
uv run python -m kube_agents_bench.oom_repro events --count 400000
uv run python -m kube_agents_bench.oom_repro events --count 500000 --pad-to 1024
uv run python -m kube_agents_bench.oom_repro --host-context <context> watcher --size L --runc
```

`events` keeps the Events-only clusters' namespace count (150 by default), because `fleet load` does
not grow them: leave `--namespaces` off the two `events` commands. `fleet load` takes the full
clusters by name, only grows their node, namespace and Deployment counts, and grows each real node
pool to one `e2-standard-8` per 250 KWOK nodes, at least six: twelve per full cluster at 3,000
nodes. It leaves a real node pool of that size or larger as it is. The evidence fleet used
`--prefix kwok-`. The credential proxy's L kill came at about 80,000 Pods per cluster and the
shell's at 86,000 to 89,000.

## What you need

- An install of kube-agents ([INSTALL.md](../../INSTALL.md)) whose scope includes the GCP project
  of the test fleet, and a kubectl context for its cluster.
- Quota in that project for the fleet's real nodes: one `e2-standard-8` per 250 KWOK nodes, at
  least 6, per full cluster (12 at 3,000 KWOK nodes), and 2 `e2-standard-4` per Events-only
  cluster. The KWOK nodes use no quota.
- `gcloud`, `kubectl`, `gke-gcloud-auth-plugin` and `uv`, and Application Default Credentials
  (`gcloud auth application-default login`): the Event injector authenticates with them.

## Run it

From `bench/`, after `uv sync`, with `OOM_REPRO_PROJECT` set to the fleet's project:

```bash
uv run python -m kube_agents_bench.oom_repro fleet create --clusters 2      # 2 clusters, each 1,500 nodes and 45,000 Pods
uv run python -m kube_agents_bench.oom_repro fleet create --clusters 2 --first 3 --events-only
uv run python -m kube_agents_bench.oom_repro --host-context <context> proxy   # watches the shell; --watch proxy on older builds
uv run python -m kube_agents_bench.oom_repro events --count 400000          # into every fleet cluster
uv run python -m kube_agents_bench.oom_repro --host-context <context> watcher --runc
uv run python -m kube_agents_bench.oom_repro fleet delete
```

Each fleet cluster needs a Cluster Agent profile before `proxy` and `watcher`. Wait for the hourly
reconcile, or start it in the agent pod:
`kubectl --context <context> -n kubeagents-system exec <gateway pod> -c platform-agent --
/opt/hermes/.venv/bin/python3 /opt/data/scripts/cluster_agent_reconcile.py`.

Each scenario writes to its own directory under `--out`. It takes the install's live-test lease and
saves the `PlatformAgent` spec to `platformagent-before.json`. It sets the watched container to the
limits of `--size` (the operator's defaults unless `--size L` or `--size fleet`), lowering any request
above a limit, and waits until the running pod
carries them. Then it starts the trigger and waits for `--kills` OOMKills of that container, or
`--seconds`. At the end it puts the container's resources (and the runtime class) back as they were
and writes `report.md`: the samples it took, the kills, and for each kill the share of each process
class in the killed pod's last sample. The sampler's own memory is reported beside the shares, not
in them. A run with no samples writes its report and then ends with an error, so a run that watched
nothing is not read as zero kills. Two failed renewals of the
lease in a row stop the scenario, which then restores. Ctrl-C, `SIGTERM`, `SIGHUP` and a failed trigger also
restore, and still write the report. If a run is killed some other way, restore the spec from `platformagent-before.json`. The
killed container's previous log is kept beside the report.

`proxy` marks `compliance-audit` due, as the first-run flow does, and refuses a job that is
disabled, paused or missing from the roster. The harness does not stop the audit: it runs to its end
after the scenario, delivers its report to chat, and on an install with a GitOps repository it
rewrites (or opens) the audit's ledger issue, comments on it, and opens or closes remediation pull
requests unless `KUBEAGENTS_AUDIT_AUTO_PROMOTE=false`.

Events expire after the API server's event TTL (1 hour by default), so run `watcher` within the
hour after `events`.

### `--runc`

The agent pod runs under gVisor on an install made with `install.sh` or the chart's defaults. gVisor applies one memory limit to the whole
sandbox, not one limit per container, and in a run outside the harness the event watcher passed its
2 GiB limit under gVisor without being killed. `--runc` removes the runtime class for the scenario,
so the container's own limit applies, as it does on runc.

## Fleet notes

- GKE deletes a Node that is `NotReady` and has no VM behind it. KWOK keeps the nodes `Ready` with
  lease heartbeats. The harness raises KWOK's node lease and Pod stage parallelism to 32 and its
  node stage parallelism to 16; at KWOK's defaults about one node in twenty was lost while 1,500
  nodes registered.
- KWOK's default Pod CIDR is a `/24`, 254 addresses. The harness sets `100.64.0.1/10`.
- GKE refuses the FlowSchema in KWOK's release manifest (it names the cluster-critical `exempt`
  priority level); the harness drops it.
- GKE scales `kube-dns` and `konnectivity-agent` with the node count, KWOK nodes included. On
  three real nodes these system Pods requested over 90 % of the CPU at 1,500 KWOK nodes. The
  harness gives a full cluster at least six `e2-standard-8`, one more per 250 KWOK nodes above
  1,500, which leaves room for the KWOK controller.
- The control plane resizes when the node count jumps (`RESIZE_CLUSTER`) and refuses connections
  for a few minutes. The harness tries a failed apply stream up to six times in all, a minute
  apart, and applies the nodes once more if fewer than 99 % are `Ready` after an hour.
