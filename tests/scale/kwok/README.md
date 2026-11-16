# KWOK scale lane

A pinned [KWOK](https://kwok.sigs.k8s.io/) cluster of the shape of a large multi-tenant GKE cluster
(2,000 nodes, 60,000 pods, 500 namespaces, 4,000 Services), seeded with the object shapes of the fake
API server generator in `k8s-operator/internal/testing/fakeapiserver`, and a baseline of the two stock
binaries that read a cluster at scale: `k8s-event-watcher` and `k8s-api-proxy`. It is the first step
of sizing the install for such clusters: measure before anything is sized. The lane measures and
records; it does not grade. The only gates are "it ran": a seeder request that fails or a Running
wait that times out, a binary that does not build, a proxy that never becomes healthy, a CLI read
that exits non-zero, or a proxy still running after the reads whose `requests_total` family is
empty, or a watcher synced on its gauge whose `cluster_up` is absent (those names are copied from
the Go sources, so an empty family on a live process means a rename, not a measurement; a proxy
that died during the reads is recorded and fails the run through its failed reads). A slow list or
a large resident set does not fail anything.

KWOK runs a real API server, etcd, controller-manager and its own node and pod simulator as plain
processes (no container engine), so a cluster of 2,000 nodes and 60,000 pods fits one machine.

## Pieces

- `kwok-cluster.sh`: `install-tools` downloads `kwokctl` and `kwok` (pinned version and sha256);
  `up NAME` creates a cluster (Kubernetes version pinned, scheduler off, Event TTL 24h); `down NAME`
  removes it and the files `baseline.sh` kept beside its kubeconfig; `kubeconfig`, `ca`, `server` and `dir`
  print its coordinates; `pins` prints the two pinned versions, which the workflow uses as its
  download-cache key. Every CLI call passes `--kubeconfig`; nothing reads or writes
  `~/.kube/config`. The context is `kwok-NAME`.
- `k8s-operator/cmd/kwok-seed`: seeds namespaces, nodes, pods, services and events from a profile,
  through client-go, and writes a summary JSON.
- `baseline.sh NAME PROFILE`: builds the two binaries, measures them against the seeded cluster and
  writes the record.
- `profiles/`: the shapes, one JSON file each.
- `.github/workflows/scale-lane.yml`: the `smoke` profile on pull requests that touch this lane; the
  `large-cluster` profile nightly; on dispatch, the profile chosen from the input's list, `smoke` by
  default.

## Prerequisites

Linux on x86_64 (the only platform with pinned binaries), the Go toolchain `k8s-operator/go.mod`
names, `kubectl`, `jq`, `curl`, `openssl` and `sha256sum`. No Docker. The smoke profile needs about
2 GB of free memory. The full profile needs about 16 GB: on an 8 vCPU machine at 60,000 pods and
150,000 Events the kwok API server peaked at 5.2 GB of resident memory, etcd at 0.9 GB, the
controller manager at 1.3 GB, and the CLI's unpaged list of every pod and the watcher's initial sync
each take their own share beside those.

## Run it locally

```bash
tests/scale/kwok/kwok-cluster.sh install-tools
tests/scale/kwok/kwok-cluster.sh up scale
(cd k8s-operator && go run ./cmd/kwok-seed \
  --profile ../tests/scale/kwok/profiles/smoke.json \
  --kubeconfig "$(../tests/scale/kwok/kwok-cluster.sh kubeconfig scale)" \
  --summary-out /tmp/seed.json)
BASELINE_SEED_SUMMARY=/tmp/seed.json tests/scale/kwok/baseline.sh scale smoke
tests/scale/kwok/kwok-cluster.sh down scale
```

`baseline.sh` prints a Markdown summary and writes `results/<profile>-<UTC timestamp>.json` next to the
proxy and watcher logs of the run (`results/` is gitignored). Both scripts print their environment
variables with `-h`; the ones you are likely to set are `KWOK_WORKDIR` (kwokctl's state and download
cache, default `~/.kwok`), `BASELINE_SKIP_BUILD` (reuse the binaries from an earlier run) and
`BASELINE_OUT_DIR`.

The lists run through the CLI, so its own JSON handling dominates the wall time. Every list is
therefore measured twice, straight at the kwok API server with the admin kubeconfig and through the
proxy with a caller token, and the proxy's overhead is the difference between the two columns.

## Profiles

| Profile         | What it is for                                                             | Runs                             |
| --------------- | -------------------------------------------------------------------------- | -------------------------------- |
| `smoke`         | proves the lane itself on a pull request in about ten minutes              | pull request, dispatch (default) |
| `large-cluster` | the shape of a large multi-tenant cluster, the measurement the lane is for | nightly, dispatch when chosen    |

The counts live in the profile files, not here: a profile is a JSON object with five fields,
`nodes`, `pods`, `namespaces`, `services` and `events`, each a positive integer, with `pods` at
least `nodes`. To add one, put `profiles/<name>.json` next to these and run `baseline.sh NAME
<name>`; to make it dispatchable, add the name to the `profile` input's `options` list in the
workflow as well, since that input is a fixed choice. The nightly profile is fixed in the workflow.

## What the JSON holds

`schema_version` is 1. Bytes are bytes, durations are seconds, memory figures are `VmHWM` (peak
resident set) and `VmRSS` (resident set at the moment of reading) from `/proc/<pid>/status`.

- `git_commit`: the checkout the binaries were built from.
- `profile`: `name` and the five counts requested.
- `versions`: `kwok`, `kube` (the API server), `cli`, `go`.
- `host`: `nproc`, `mem_total_bytes`.
- `cluster`: `name`, the counts found (`nodes`, `pods`, `services`, `events`, `namespaces`) and
  `apiserver` (`pid`, `vm_hwm_bytes`, `vm_rss_bytes`).
- `seed`: the seeder's summary when `BASELINE_SEED_SUMMARY` names it, else `null`.
- `proxy`: `exit_code`, `vm_hwm_bytes` and `vm_rss_bytes` after the lists; `lists`, one entry per
  read (`name`, `args`, and `direct` and `proxy` each with `seconds`, `exit_code`, `bytes`,
  `stderr`); `requests_total` and `request_duration_seconds` (the `_sum` and `_count` series), each a
  list of `{metric, labels, value}` from the proxy's metrics; `audit_outcomes`, the proxy's audit
  lines counted by `outcome`.
- `watcher`: `synced`, `sync_seconds` (start to the watcher's `cluster_up` gauge turning 1, or to
  its `informer synced` log line when the metrics port did not bind), `exited_early`,
  `timed_out`, `exit_code`; `at_sync` and `steady` (memory at sync and `seconds_after_sync` later);
  `events_seen_total` and `events_seen_by_reason` from its metrics; `cluster_up`;
  `apiserver_events_requests`, the API server's `LIST` and `WATCH` request counts for Events before
  and after the run and their `delta`, which says whether the informer's initial sync ran as a
  classic list or as a watch-list stream.
- `started_at`, `finished_at`.

## Known limits

- One cluster, not a fleet. The watcher's fan-in across many clusters is not exercised.
- No model in the loop; the agent itself is not run.
- Whether the watcher's initial sync is a classic list or a watch-list stream depends on the API server
  and client-go feature gates, which is why the record carries the request deltas rather than assuming
  one path.
- The Event TTL is raised from the default 1h to 24h so a seeded cluster keeps its Events for a whole
  run; a real cluster forgets them sooner.
- The seeder strips `ownerReferences` (the generator points at ReplicaSets that do not exist and the
  garbage collector would delete the pods), and the scheduler is disabled (pods carry `nodeName`).
  Controllers that act on owners or on scheduling are not exercised. An Event's `involvedObject.uid`
  is the generator's, not the live pod's; its name and namespace do match, except for the one Event
  shape in ten that concerns the pod's ReplicaSet, an object the seeder never creates.
- The watcher exits on its own when no cluster has synced within its two-minute grace
  (`initialSyncGrace` in `k8s-operator/cmd/k8s-event-watcher`). A profile whose Event backlog
  outlasts that shows up as `exited_early: true` with a non-zero `exit_code` in the record. That is
  the measurement, not a fault in the lane; `BASELINE_WATCHER_TIMEOUT` is reached only by a watcher
  that neither syncs nor exits.
- The seeder waits for kwok to mark every pod Running (`--wait-timeout`, 30 minutes by default) and
  fails the job when that runs out, so a run on a machine far slower than measured loses its record
  before the baseline starts, with the cluster fully seeded. The seeder's own summary still says how
  far it got.
- Nodes keep the generator's status (8 CPUs, 32 GiB, 110 pods, a GKE kubelet version, eight images),
  which the API server accepts on create; kwok adds the conditions and keeps them Ready. Pods turn
  Running at kwok-controller's pace: `up` raises its stage parallelism from 4 to 64 so that pace
  matches the seeder's (about 60 pods a second at the default, which would hold the full profile
  for 17 minutes after the last create), and the seeder waits for the last pod before it exits.
- KWOK nodes run no kubelet: the API server's resident set is the only resource signal on the cluster
  side, and the CLI's peak memory is not recorded.
- Numbers from a shared or busy machine are noisy; compare the direct and proxy columns of one run
  before comparing runs.
