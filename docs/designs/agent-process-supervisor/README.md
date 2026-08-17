# Agent process supervisor — prototype

Executable backing for [`../agent-process-supervisor.md`](../agent-process-supervisor.md) §6.0.

**This is not shipping code and is not wired into CI.** It exists so the design's mechanisms could
be tested before anyone implements them in
[`k8s-operator/internal/controller/leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py).
It earned its place by being wrong: three claims the design asserted were falsified here and
corrected before reaching an implementation PR.

## Running it

```bash
cd docs/designs/agent-process-supervisor
python3 run_experiments.py          # all of them, ~25s
python3 run_experiments.py E1 E6    # a subset
```

Standard library only and no cluster. `E7` additionally needs a Go toolchain to render the
operator's manifests, and skips itself if `go` is absent. Every experiment **asserts**, so a
non-zero exit means a claim in the design has stopped holding — which is the point of keeping it,
and what re-ran the suite after merging `d44ea21` (42 commits) and confirmed nothing had rotted.

## What is here

| File                      | What it is                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `supervisor.py`           | The supervisor of §3.2–3.7: process table with criticality, backoff and cap, status file, one reaper. Section numbers are in the docstrings    |
| `probe.py`                | The readiness probe of §3.4 — reads the status file, fails on staleness as well as on content                                                  |
| `run_experiments.py`      | The experiments, with assertions                                                                                                               |
| `manifest_claims_test.go` | E7's checks. Copied into `k8s-operator/internal/controller/` by the runner, executed, and removed — it is **not** part of the operator's suite |

Leader election is stubbed to the presence of a file, so this runs without an API server.
Everything else is the real shape. The timing constants are scaled down (2 s grace rather than
10 s, 3 restarts rather than 5) so the suite finishes in seconds; `E5` rescales its measurements
back to the design's figures before asserting.

## What the experiments establish

| #     | Claim                                                            | Note                                                                                         |
| ----- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `E1`  | `waitpid(-1)` rewrites a crash into a clean exit                 | **Falsified the design.** See below                                                          |
| `E1b` | One reaper dispatching by pid preserves exit statuses            | The fix `E1` forced                                                                          |
| `E2`  | An `httpGet` probe cannot reach a server bound to `127.0.0.1`    | Why §3.4 uses an `exec` probe                                                                |
| `E4`  | The restart cap diverges on criticality; cleanup tells the truth | **Surfaced the stale-`ready:true`-on-exit gap**                                              |
| `E4c` | A wedged loop is detected via `updated_at`                       | A hung supervisor must not report healthy                                                    |
| `E5`  | Shutdown is the sum over the table                               | 3 processes overrun the 30 s default `terminationGracePeriodSeconds`                         |
| `E6`  | Every margin in §3.5 reproduces                                  | Including a third process failing the startup assertion                                      |
| `E7`  | Eleven manifest-level claims, against rendered output            | Uses the operator's own `buildDeployment` / `buildNetworkPolicy` / `buildPlatformLeaderRole` |
| `E8`  | An entrypoint background job reparents to the supervisor         | The Hindsight migration: one zombie per boot without §3.7's reaper                           |

### The three the design got wrong

1. **The reaper.** §3.7 proposed reaping `-1` and skipping PIDs found in the process table. The
   guard cannot work — `waitpid(-1)` has already consumed the status by the time it returns the
   PID. Worse, the consequence is not ambiguity: CPython catches the resulting `ECHILD` and sets
   `returncode = 0`, so a process that exited **3** is reported as a clean exit. Any policy reading
   exit 0 as "stopped on purpose" would stop restarting a crash-looping process.
2. **The rollout arithmetic.** `maxUnavailable` is 0 at **one, two and three** replicas, not just
   two as the design said. See `E3` below.
3. **Cleanup left the status file lying.** After the supervisor exited on a required process the
   probe still reported `Ready`, because the document was a second old and the staleness window had
   not elapsed. Cleanup now writes a final `ready: false`.

## E3 — the rounding check, in Go

Superseded by `E7`'s `TestClaimC6SurgeRounding`, which the runner executes automatically; kept
here as the minimal standalone recipe. It needs the operator's Go module and its vendored
`k8s.io/apimachinery`. It evaluates `defaultSurgePercent` through
`intstr.GetScaledValueFromIntOrPercent`, the function the Deployment controller itself calls, so
the answer is Kubernetes' rather than an approximation of it.

```go
// k8s-operator/internal/controller/zz_e3_test.go -- create, run, delete
package controller

import (
	"fmt"
	"testing"

	"k8s.io/apimachinery/pkg/util/intstr"
)

func TestE3SurgeRounding(t *testing.T) {
	pct := intstr.FromString(defaultSurgePercent)
	for _, n := range []int{1, 2, 3, 4, 8} {
		up, _ := intstr.GetScaledValueFromIntOrPercent(&pct, n, true)
		down, _ := intstr.GetScaledValueFromIntOrPercent(&pct, n, false)
		fmt.Printf("replicas=%d maxSurge=%d maxUnavailable=%d\n", n, up, down)
	}
}
```

```bash
cd k8s-operator && go test ./internal/controller/ -run TestE3SurgeRounding -v
```

Result: `maxUnavailable` is 0 for 1, 2 and 3 replicas and only reaches 1 at four.

## What this does not cover

No Docker or `kind` was available, so two things are argued rather than observed:

- **PID 1 signal semantics.** `E2` demonstrates the network fact behind the probe bug, not the
  kubelet performing a probe. Verifying that needs a cluster.
- **The `terminationGracePeriodSeconds` overrun.** `E5` measures shutdown and the overrun is
  arithmetic from it; no `SIGKILL`-mid-cleanup was observed in a real pod.

Both belong in the end-to-end checks of §6, not here.

## Where this goes

At **S1/S2** the cases in `run_experiments.py` become the additions to
[`test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py)
listed under **Unit** in §6 — `E1`/`E1b` in particular, since the reaper is the subtlest thing
here and the one no obvious test would otherwise catch. This directory should be **deleted** when
that happens; a prototype kept alongside the implementation it seeded is just a second thing to
keep in sync.
