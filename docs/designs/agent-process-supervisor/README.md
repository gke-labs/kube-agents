# Agent process supervisor — prototype

Executable backing for [`../agent-process-supervisor.md`](../agent-process-supervisor.md) §6.0.

**This is not shipping code and is not wired into CI.** It exists so the design's mechanisms could
be tested before anyone implements them in
[`k8s-operator/internal/controller/leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py).
It earned its place by being wrong: nine claims the design asserted were falsified here and
corrected before reaching an implementation PR.

## Running it

```bash
cd docs/designs/agent-process-supervisor
python3 run_experiments.py          # all of them, ~90s
python3 run_experiments.py E1 E6    # a subset
```

Standard library only and no cluster. `E7` additionally needs a Go toolchain to render the
operator's manifests, and skips itself if `go` is absent. Every experiment **asserts**, so a
non-zero exit means a claim in the design has stopped holding — which is the point of keeping it.

Several experiments spawn deliberately awkward children (a grandchild that ignores `SIGTERM`, a
process group that has to be swept). They clean up after themselves on the happy path and on an
assertion failure; a `SIGKILL` of the runner mid-case can leave a `sleep 300` behind.

## What is here

| File                      | What it is                                                                                                                                     |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `supervisor.py`           | The supervisor of §3.2–3.7: process table with per-entry grace, criticality, backoff and cap, renew deadline, status files, one reaper         |
| `probe.py`                | The two probes of §3.4 — readiness and liveness over one line, differing only in whether they read the `ready` flag                            |
| `run_experiments.py`      | The experiments, with assertions                                                                                                               |
| `manifest_claims_test.go` | E7's checks. Copied into `k8s-operator/internal/controller/` by the runner, executed, and removed — it is **not** part of the operator's suite |

Leader election is stubbed to the presence of a file, and the "lease call" is a bounded sleep, so
this runs without an API server. Everything else is the real shape. The timing constants are
scaled down (2 s grace rather than 10 s, 2 restarts rather than 5, a 2 s renew deadline rather
than 8) so the suite finishes in minutes; `E5` rescales its measurements back to the design's
figures and `E6` and `E12` assert against the design's constants directly rather than the
prototype's.

## What the experiments establish

| #     | Claim                                                             | Note                                                                                         |
| ----- | ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `E1`  | `waitpid(-1)` rewrites a crash into a clean exit                  | **Falsified the design.** See below                                                          |
| `E1b` | One reaper dispatching by pid preserves exit statuses             | The fix `E1` forced                                                                          |
| `E2`  | An `httpGet` probe cannot reach a server bound to `127.0.0.1`     | Why §3.4 uses an `exec` probe                                                                |
| `E4`  | The restart cap diverges on criticality; the two probes disagree  | **Surfaced the stale-`ready:true`-on-exit gap**, and now pins readiness vs liveness          |
| `E4c` | A wedged loop is detected via the timestamp, by both probes       | A hung supervisor must not report healthy                                                    |
| `E5`  | Shutdown is the sum over the table                                | 3 processes overrun the 30 s default `terminationGracePeriodSeconds`                         |
| `E6`  | Every margin in §3.5 reproduces                                   | Including a third process failing the startup assertion, and the stale-Lease rows from `E10` |
| `E7`  | Eleven manifest-level claims, against rendered output             | Uses the operator's own `buildDeployment` / `buildNetworkPolicy` / `buildPlatformLeaderRole` |
| `E8`  | An entrypoint background job reparents to the supervisor          | The Hindsight migration: one zombie per boot without §3.7's reaper                           |
| `E9`  | A demoted leader restarts its table on reacquiring the lease      | **Falsified the design.** The only case that runs in `elected` mode                          |
| `E10` | The raised lease duration cannot reach an existing Lease          | **Falsified the design.** Parsed from the real `leader_elect.py`                             |
| `E11` | A renew deadline bounds detection only when every step is clamped | **Falsified the design twice.** Naming a deadline is not having one                          |
| `E12` | The restart cap is a rate, and its window was below the floor     | **Falsified the design.** Two defects were cancelling                                        |
| `E13` | Stopping a process stops everything it started                    | **Falsified the design twice.** Including the first attempt at the fix                       |
| `E14` | A definitive denial is not forgotten on the next timed-out call   | **Falsified the design.** A denied leader re-promoted itself                                 |

### The nine the design got wrong

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
4. **A demotion was terminal.** §3.3 said lease loss stops the processes and never said what state
   they land in; the prototype resolved that into `stopped`, with no transition out. A pod that
   lost the lease and reacquired it — an API-server blip — therefore resumed as leader with an
   empty table: label held, endpoint listed, nothing serving, and no probe that restarts anything.
   `stop()` now leaves entries `pending`, and only cleanup leaves them `stopped`.
5. **Raising `lease_duration_seconds` did nothing to an existing install.** §3.5 stated
   release-before-acquire as a property of the constant. It is a property of the Lease _object_,
   and the constant reaches it only on the 404-create path — renew and takeover replace the body
   read back from the server, and the operator never touches the Lease at all. §4 gained a
   renew-path write and an explicit Lease migration.
6. **The inequality's first term was a `time.sleep`.** §3.5 bounded "how long until the outgoing
   leader notices" with the loop's sleep interval, but an iteration is the sleep _plus_ an untimed
   lease read and write. The term measured nothing, and §3.4 said so in passing — it sized a 30 s
   staleness window on the premise that the same iteration is long and unpredictable. `E11` makes
   every lease call time out and shows the pre-fix loop leading forever. §3.5 now uses a **renew
   deadline** on the local clock. The first fix for that was itself too weak — a deadline _tested
   once per pass_, with the calls and the sleep free to run past it, is bounded by the iteration
   and not by itself. `E11` gained a third arm that measures the overshoot, and every blocking
   step is now clamped to the remaining deadline.
7. **The restart cap was unreachable, and only an off-by-one hid it.** The cap tested
   `len(failures) >= CAP`, retiring after `CAP-1` restarts rather than `CAP`. Correcting that to
   `>` is what exposed the real defect: reaching a cap of `C` needs `C+1` failures inside the
   window, so anything failing less often than `WINDOW/C` never reaches it. The KV server's
   failures land 61–76 s apart against a 60 s floor. The window goes from 300 s to 600 s.
8. **A denied leader re-promoted itself.** The renew deadline answers "nobody has told me
   anything", but §3.5 never said a _definitive_ not-the-holder read must invalidate it. Left set,
   the next timed-out call reads as "still inside the deadline" and restarts the whole table while
   the real holder is running it — reachable directly by §3.5's own `kubectl delete lease`
   migration step. `E14` is verified to falsify without the invalidation, which the first draft of
   that experiment was not: it passed either way because the deadline had already elapsed by the
   time the timeout landed, so it now asserts the window is still open before relying on it.
9. **Stopping a process did not stop what it started.** `Popen.terminate()` signals the direct
   child, so a grandchild survives the handover §3.5 guarantees. The first fix — `SIGTERM` to the
   process group — was also wrong, and `E13` is the case that shows why: the parent honours it and
   exits inside the grace, so the `SIGKILL`-after-grace branch never runs and the grandchild lives.
   `stop()` now sweeps the group unconditionally once the child is gone.

Six of the nine came out of review rather than out of running the suite (4, 5, 6, 7, 8 and half of
9), which is worth recording. Four of those five were about a **transition** — leader → follower →
leader, old constant → new object, a call that never returns, a parent that exits before its child
— and none had a case that crossed one. Every experiment that existed started from a clean state
and stayed there.

## E3 — the rounding check, in Go

Superseded by `E7`'s `TestClaimC5SurgeRounding`, which the runner executes automatically; kept
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

No Docker or `kind` was available, so three things are argued rather than observed:

- **PID 1 signal semantics.** `E2` demonstrates the network fact behind the probe bug, not the
  kubelet performing a probe. Verifying that needs a cluster.
- **The `terminationGracePeriodSeconds` overrun.** `E5` measures shutdown and the overrun is
  arithmetic from it; no `SIGKILL`-mid-cleanup was observed in a real pod.
- **The shipped probes are shell, and these are Python.** `probe.py` mirrors the logic so the
  experiments can drive it, but §3.4's reason for the shell versions — an interpreter start is
  expensive under gVisor at probe frequency — is precisely the thing this cannot measure here.

Two experiments are host-dependent and say so in their own comments. `E8` reads `ps` and counts
only processes whose parent is the supervisor it spawns; an earlier version counted zombies
machine-wide, which made it fail on any developer box that already had one — a false FALSIFIED,
and the more dangerous direction for a suite whose whole value is that a red result means
something. `E13` uses `os.fork()` and process groups, so it is POSIX-only.

The first two belong in the end-to-end checks of §6.

## Where this goes

At **S1/S2** the cases in `run_experiments.py` become the additions to
[`test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py)
listed under **Unit** in §6 — `E1`/`E1b`, `E11` and `E13` in particular, since the reaper, the
renew deadline and the group sweep are the three subtlest things here and none of them fails a
test that was not written for it. This directory should be **deleted** when that happens; a
prototype kept alongside the implementation it seeded is just a second thing to keep in sync.
