# Agent Process Supervisor

> **STATUS — draft; not implemented.** Nothing here ships today. Section 1 describes the
> launch path as it currently exists; sections 3 onward are the proposal.
>
> Section 1 was re-verified against `main` on 2026-08-13. Every problem in section 2 still holds:
> `leader_elect.py` is unchanged, the gateway container still has no probe, and the lease
> inequality of 3.5 is still false. What moved underneath it since the first draft is the
> entrypoint, which roughly doubled in length and gained the `IS_BOOTSTRAP_PRIMARY` gate
> described in 1.2.

**Scope:** how long-lived processes inside the `platform-agent` container are started,
supervised, and stopped — at every replica count — and how their health reaches the kubelet.
**Owns:** the container's process model, `leader_elect.py`'s two modes, the per-child restart
policy, the supervisor health endpoint and the readiness probe that reads it, the lease timing
parameters, and what the Lease does and does not fence.
**Does not own:** what any individual child process does. The Session KV server is specified in
[`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md), which depends on this design for
its launch path and cites it rather than restating it.

**Why this is a separate design.** The Session KV decomposition needs a supervised, single-owner
KV server, and reached for `leader_elect.py` to get one. But the gap it has to cross —
`leader_elect.py` does not run at all at the default replica count — is not a `session_kv`
problem. It is the reason the gateway container has no probes, the reason there are three things
that can start a background process and nothing that owns one, and the reason a child crash
restarts the whole container. Fixing it under the KV server's name would leave the next component
that needs a supervised sibling to rediscover the same ground.

---

## 0. Source files

Every file this design cites, linked to `main`. Line numbers in the prose below were taken on
2026-08-13 and will drift; the links will not.

| File                                                                                                                                                                           | Its part in this design                                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------- |
| [`k8s-operator/internal/controller/leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py)                       | The script that becomes the supervisor. 159 lines; read it whole |
| [`k8s-operator/internal/controller/test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py)             | Its four existing tests, two of which S1 breaks                  |
| [`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh)                                                   | `exec "$@"`, the `IS_BOOTSTRAP_PRIMARY` gate, and step 5         |
| [`k8s-operator/internal/controller/platformagent_manifests.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/platformagent_manifests.go) | `Args`, the probes, the Service selector, the two stale comments |
| [`k8s-operator/internal/controller/manifest_helpers.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/manifest_helpers.go)               | Replica count and Deployment strategy                            |
| [`agents/platform/scripts/platform_mcp_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/platform_mcp_server.py)                           | The second, racing KV-server launcher                            |
| [`deploy/shared/entrypoint_gate_check.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/entrypoint_gate_check.sh)                                           | Asserts port 8699 is released; changes at S4                     |

## 1. What exists today

### 1.1 Two launch paths, chosen by replica count

The image `ENTRYPOINT` is
[`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh),
which builds the shared tree and ends in `exec "$@"` (`:1008`). What `"$@"` is depends on the
replica count:

| Replicas | Gateway container `Args`                              | What supervises `hermes gateway run` |
| -------- | ----------------------------------------------------- | ------------------------------------ |
| 1        | unset — the image `CMD`                               | nothing; it is PID 1's exec target   |
| > 1      | `/opt/hermes/.venv/bin/python3 $HOME/leader_elect.py` | `leader_elect.py`                    |

The operator sets `Args` only in the `replicas > 1` branch, and sets `ENABLE_LEADER_ELECTION` /
`LEADER_ELECTION_LEASE_NAME` / `LEADER_ELECTION_NAMESPACE` in the same branch (`:1574-1589`) —
[`platformagent_manifests.go:2207-2212`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/platformagent_manifests.go):

```go
var args []string

replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
if replicas > 1 {
	args = []string{"/opt/hermes/.venv/bin/python3", fmt.Sprintf("%s/leader_elect.py", homeDir)}
}
```

Deleting the `if` is the whole of change 3.1 on the operator side.

The branch tests the **effective** replica count, which `resolveDeploymentReplicasAndStrategy`
forces to `0` when `scaleToZero` is set (`manifest_helpers.go:281-282`). An agent configured
`availability.replicas: 3` with `scaleToZero: true` therefore renders no election wiring at all;
3.1's unconditional `Args` removes that inconsistency as a side effect.

[`leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py)
has a third path of its own — the first two statements of `main()`:

```python
def main():
    global process, is_shutting_down

    if not lease_name or not namespace:
        os.execvp("hermes", ["hermes", "gateway", "run"])
```

`execvp` replaces the process image, so there is no interpreter left to start a second child. Even
when the script is the entrypoint's exec target, it supervises nothing unless the election is
configured. This is P2, and 3.1's `solo` mode is what replaces these two lines.

### 1.2 Three launchers, none of them the supervisor

Nothing that is not the gateway is started by whatever is supervising the gateway:

| Process              | Started by                                            | Supervised by |
| -------------------- | ----------------------------------------------------- | ------------- |
| `hermes gateway run` | `leader_elect.py:138`, or the entrypoint's exec       | see above     |
| Session KV server    | `docker-entrypoint.sh:960-967`, with `&`              | nothing       |
| Session KV server    | `platform_mcp_server.py:613-654`, if the port is free | nothing       |

The last two race: the MCP launcher probes port 8699 and spawns if the connect fails, which is a
TOCTOU against the entrypoint's background start. The loser exits with `EADDRINUSE` into a log
file on the PVC.

The entrypoint's start is no longer unconditional. It is gated on `IS_BOOTSTRAP_PRIMARY`
(`:191-195`), which is `0` for `PLATFORM_AGENT_ROLE=sidecar`:

```bash
# 5. Start background microservices (FastAPI proxy)
#
# Primary only: this binds a fixed port in the pod's shared network namespace,
# and the sidecar's copy lost the race with `[Errno 98] address already in use`
# every boot while both wrote the same log file, interleaved. The port is what
# both containers reach it on, so one server serves the pod.
mkdir -p "$TARGET_DIR/logs"
if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$TARGET_DIR/scripts/session_kv_server.py" ]; then
    echo "Starting Session KV server on port 8699..."
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 127.0.0.1 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi
```

The `&` and the redirect to a file on the PVC are the two things 3.2 changes. So the dashboard
container no longer races the gateway for the port inside a single pod, which is the failure the
gate's own comment records. That narrows the race to two parties, and it removes the intra-pod
half of the problem. It does not remove the cross-replica half, and it does not make either
starter a supervisor: both still background the process with `&` and neither ever looks at it
again.

### 1.3 One supervision idiom

`leader_elect.py` handles exactly one child and has one response to its death — the whole of it,
at `:134-153`:

```python
if holder == pod_name:
    if process is None:
        print(f"[{pod_name}] Acquired leadership! Starting Hermes...", flush=True)
        update_pod_label(v1, True)
        process = subprocess.Popen(["hermes", "gateway", "run"])
    elif process.poll() is not None:
        print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
        sys.exit(process.returncode)
else:
    if process is not None:
        print(f"[{pod_name}] Lost leadership! Stopping Hermes...", flush=True)
        update_pod_label(v1, False)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[{pod_name}] Hermes did not exit in time, killing...", flush=True)
            process.kill()
            process.wait()
        process = None
```

A single `process` global, `sys.exit` on death, and a 10 s grace on loss. The `SIGTERM` path
(`:25-55`) repeats the same terminate-wait-kill and then releases the lease. Three things follow
that section 2 turns into problems: the crash response restarts the whole container without
releasing anything (P3), the state is one variable rather than a table (3.2), and the 10 s grace
is one of the two terms in the timing inequality (P5).

### 1.4 No health signal from the gateway container

The `platform-agent` container declares no probe of any kind (`platformagent_manifests.go:2248-2270`).
The only readiness probe in the pod belongs to the `envoy-credential-proxy` container, and it is
the shape 3.4's probe should match — an existing, working example in the same file (`:1911-1918`):

```go
ReadinessProbe: &corev1.Probe{
	ProbeHandler: corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{
		"curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8765/healthz",
	}}},
	InitialDelaySeconds: 5,
	PeriodSeconds:       15,
},
```

Nothing the gateway container runs — the gateway itself, the KV server's `/healthz` — is ever
asked whether it is alive.

At `replicas > 1` the Service selector gains `kubeagents.io/is-leader=true` (`:2730`), so
followers are already excluded from endpoints by label. Readiness today therefore changes
nothing about routing, and its absence costs only visibility.

### 1.5 The timing parameters

| Parameter                    | Value                                      | Where                                |
| ---------------------------- | ------------------------------------------ | ------------------------------------ |
| `lease_duration_seconds`     | 15 s                                       | `leader_elect.py:70`                 |
| poll interval                | 5 s + U(0,2)                               | `:71`, `:156`                        |
| child termination grace      | 10 s                                       | `:36`, `:148`                        |
| Deployment strategy, `n = 1` | **Recreate**                               | `manifest_helpers.go:270-272`        |
| Deployment strategy, `n > 1` | RollingUpdate, 25% surge / 25% unavailable | `manifest_helpers.go:61`, `:285-292` |

The single-replica row matters for 3.4 and is easy to miss: the default deployment does not roll,
it is torn down and replaced. A readiness probe that never passes there is not a stalled rollout
with the old pod still serving — it is an outage.

---

## 2. Problems

| ID  | Severity | Problem                                                               | Closed by                 |
| --- | -------- | --------------------------------------------------------------------- | ------------------------- |
| P1  | High     | The default replica count has no supervisor                           | 3.1                       |
| P2  | High     | The unconfigured path cannot supervise                                | 3.1                       |
| P3  | Medium   | Child death is container death, and the crash path leaks leader state | 3.3                       |
| P4  | Medium   | Child health is invisible, and a naive probe makes it worse           | 3.4                       |
| P5  | Medium   | The lease can be reacquired before the outgoing leader has let go     | 3.5                       |
| P6  | Low      | A Lease does not fence                                                | 3.6 (bounded, not closed) |

P1 and P2 are the two that block everything else: until there is a supervisor at every replica
count, there is nothing for a second child to be a child of. P3–P5 are defects in the supervision
that does exist. P6 is a property of leases rather than a bug, and 3.6 records it instead of
fixing it.

### P1 — The default replica count has no supervisor

`resolveDeploymentReplicasAndStrategy`
([`manifest_helpers.go:268-273`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/manifest_helpers.go))
starts from one replica and only ever raises it if the user asked:

```go
func resolveDeploymentReplicasAndStrategy(deployment *agentv1alpha1.DeploymentSpec) (int32, appsv1.DeploymentStrategy) {
	replicas := int32(1)
	strategy := appsv1.DeploymentStrategy{
		Type: appsv1.RecreateDeploymentStrategyType,
	}
```

Reaching anything else takes three optional fields in a row — `spec.deployment`, then
`.availability`, then `.replicas`, all pointer-typed and all `+optional`
([`common_types.go:326-331`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/common_types.go)):

```go
// AvailabilitySpec defines high availability and scheduling settings.
type AvailabilitySpec struct {
	// Replicas specifies the desired number of pod replicas. If omitted, defaults to 1.
	// +optional
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`
```

Omitting any one of the three yields one replica, and the chart's `values.yaml` sets none of
them. So the deployment this design has to work for is the one where `leader_elect.py` is mounted
into the container (`platformagent_manifests.go:1787-1788`) and never executed.

That is what makes this the blocking problem rather than a rough edge. The Session KV
decomposition's plan — make the store a supervised child of the election — is correct at
`replicas > 1` and vacuous at `replicas: 1`, because there is no parent process. Any sibling
moved under the supervisor disappears from the majority of installations, which is the opposite
of the intended effect.

### P2 — The unconfigured path cannot supervise

Even when the operator does make the script the exec target, it hands supervision back
immediately. The `execvp` shown in 1.1 is not an early return — it is a process-image
replacement: same PID, new program, and **no return on success**. There is no interpreter frame
left to run a second `Popen`, register a signal handler, or serve a health endpoint.

This matters beyond the unconfigured case, because it rules out the smallest possible fix. "Just
add the KV server as a second `Popen` in `leader_elect.py`" works only on the elected path; on
the `execvp` path there is no `leader_elect.py` process any more, and on the single-replica path
the script was never the exec target to begin with. The three paths of 1.1 have to collapse into
one before a child table means anything, which is why 3.1 replaces the `execvp` with a `solo`
mode rather than adding a branch beside it.

### P3 — Child death is container death, and the crash path leaks leader state

The response to a dead child is two lines (`:139-141`):

```python
elif process.poll() is not None:
    print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
    sys.exit(process.returncode)
```

Three things are wrong with this, in increasing order of severity.

**It is a container restart, not a pod restart.** The message says pod; the operator sets no
`restartPolicy`, so the pod default `Always` applies to _containers_. The kubelet restarts the
`platform-agent` container inside the existing pod — same pod object, same `$HOSTNAME`, same
labels, same PVC. The design's own child table depends on this being understood correctly,
because a container restart re-runs the entrypoint from the top, which rebuilds the shared tree
and starts another Session KV server.

**It bypasses the cleanup path.** `sys.exit` here is not `release_lease_and_exit`; that function
is only ever reached through the signal handlers registered at `:63-64`. So the crash path never
calls `update_pod_label(v1, False)` and never clears `holder_identity`:

| On child crash                 | State left behind                                     |
| ------------------------------ | ----------------------------------------------------- |
| `kubeagents.io/is-leader=true` | **still on the pod** — the Service keeps selecting it |
| Lease `holder_identity`        | **still this pod** — until it expires 15 s later      |
| Readiness                      | unchanged, because there is no probe (P4)             |

The pod therefore stays in the Service's endpoint list, advertised as the leader, while the
container that serves traffic is restarting. It self-heals — on restart `process is None`, the
holder is still this pod, so it starts the gateway and re-labels — but the window is a
blackhole that nothing reports. P3 and P4 compound here: the label says leader, the endpoint
list agrees, and no probe contradicts either.

**One idiom does not survive a second child.** The rule is "any child exiting kills everything in
this container." With one child that is defensible. With the child table of 3.2 it means a KV
server that crash-loops on a corrupt database takes the gateway down with it on every iteration,
and each iteration re-runs the entrypoint. That is the failure 3.3's per-child backoff and
restart cap exist to prevent.

### P4 — Child health is invisible, and a naive probe makes it worse

The invisibility half is 1.4: no probe, so `/healthz` on 8699 is dead code and a dead child is
indistinguishable from a healthy one.

The trap is in the obvious fix. A readiness probe that reports on a **leader-only** child marks
every follower NotReady, and the rollout arithmetic does not survive that. At `replicas: 2` with
`defaultSurgePercent = "25%"` on both knobs (`manifest_helpers.go:61`, `:285-292`), Kubernetes
rounds `maxSurge` **up** and `maxUnavailable` **down**:

| Setting          | 25% of 2 | Rounding | Effective |
| ---------------- | -------- | -------- | --------- |
| `maxSurge`       | 0.5      | up       | 1         |
| `maxUnavailable` | 0.5      | down     | **0**     |

`maxUnavailable: 0` means the rollout may never drop below 2 available pods — and with a
leader-only probe only one pod can ever be Ready, so it never reaches 2 and stalls until
`progressDeadlineSeconds`, which the operator does not set and therefore defaults to 600 s.
Today's no-probe state at least rolls.

At a single replica the failure mode is worse, not milder: the strategy there is `Recreate`
(1.5), so the old pod is torn down _before_ the new one is created. A probe that never passes is
then an outage with nothing to roll back to. This is why 3.4 has followers answer `200` — the
probe reports on the supervisor, which every replica runs, rather than on children only the
leader has.

### P5 — The lease can be reacquired before the outgoing leader has let go

Three constants set the timing, and they do not agree. From
[`leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py):

```python
lease_duration_seconds = 15   # :70
base_poll_interval = 5        # :71
...
time.sleep(base_poll_interval + random.uniform(0, 2))   # :156 — so at most 7 s
...
process.wait(timeout=10)      # :148 — then SIGKILL
```

Worst case for an outgoing leader to notice it has lost the lease and finish stopping its child:

```
  7 s   maximum poll interval (5 + U(0,2))
+ 10 s  child termination grace before SIGKILL
= 17 s  before the child is guaranteed gone

  15 s  after which any other replica may take the lease
```

The lease expires two seconds _before_ the outgoing leader is required to have stopped anything.
An incoming leader can therefore be starting its children while the outgoing one's are still
running. For the gateway that is survivable — two gateways briefly bound to different pod IPs,
with the Service selecting on a label. For anything held **exclusively** it is not: a SQLite file
opened `locking_mode=EXCLUSIVE`, a lock file on the shared volume, a port on a shared mount. That
is exactly the resource the Session KV decomposition wants to put here, which is why 3.5 states
the inequality and 6 asserts it in code rather than in prose.

### P6 — A Lease does not fence

A Lease records who _should_ be leading. It says nothing about what is still executing, and
nothing prevents two processes from both believing they hold it for a bounded window.

The loop does self-terminate on a partition, though by accident rather than by design. A non-404
`ApiException` leaves `holder` at its initialised `None` and falls through to the loss branch
(`:111-153`):

```python
except ApiException as e:
    if e.status == 404:
        ...                       # create the lease; may set holder = pod_name
    else:
        print(f"[LeaderElect] Error reading lease: {e}", file=sys.stderr, flush=True)
        # holder stays None -> the `else` branch below stops the child
```

So a partitioned leader does stop its children — at its own next poll, up to 7 s later, and only
if the failure surfaces as an `ApiException` rather than a hang. "Eventually self-terminates" is
not "cannot still be writing," and the gap is unbounded if the API client blocks rather than
raising.

This is a limitation to design around, not a bug to fix. Closing it needs a fencing token — a
monotonically increasing number issued with the lease and checked on every write — which needs a
second store to hold the token, which is the dependency both this design and
[`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md)
§8 decline. 3.6 states the consequences instead: anything a child owns exclusively must tolerate
finding it still held, and any work that crosses the window must be idempotent.

---

## 3. Design

### 3.1 One supervisor, two modes, every replica count

`leader_elect.py` becomes a supervisor with an explicit mode, and the operator makes it the
gateway container's `Args` unconditionally.

```
mode = elected  if LEADER_ELECTION_LEASE_NAME and LEADER_ELECTION_NAMESPACE else solo
```

- **`solo`** — behave as a permanent leader. Start the children, supervise them, never contact
  the API server, never label the pod. This replaces the `os.execvp` at `:61`; the reason to
  supervise rather than exec is that there is more than one child to start, and that is true
  independent of how many replicas there are.
- **`elected`** — today's loop, unchanged in structure: acquire, label, start children, renew,
  and on loss drop the label and stop them.

Making the script the exec target at every replica count is what collapses 1.1's table to one
row. It has two knock-ons, both of them comments in the operator that are written around the
single-replica case this removes:

- The entrypoint's shared-state auto-detection looks for a bare `gateway` argument
  (`platformagent_manifests.go:64-69`), and the gateway container's argv only carries one at a
  single replica today. The operator already names the owner explicitly with
  `AGENT_SHARED_STATE_SETUP=owner`, so nothing changes in behaviour — but the comment gets simpler
  and should be updated rather than left describing a case that no longer exists.
- The `Args, never Command` comment (`:2197-2206`) explains the exec-target choice partly in terms
  of the entrypoint "start[ing] the Session KV server on 8699 that the event-watcher is pointed
  at". That clause survives S1 but not S4, where the entrypoint stops starting it.

### 3.2 The child table

| Child                | Start order | Stop order | Notes                                                              |
| -------------------- | ----------- | ---------- | ------------------------------------------------------------------ |
| Session KV server    | 1           | 2          | started first, stopped last: the gateway's plugins are its clients |
| `hermes gateway run` | 2           | 1          |                                                                    |

The ordering is deliberate rather than incidental. The plugins inside the gateway fail open when
the KV server is absent, so a slow start costs attribution rather than availability — but the
dependency runs in that direction and the start order should say so.

Children write to inherited stdout/stderr rather than to a file on the PVC, so their output
reaches fluent-bit like everything else and nothing grows unbounded on the volume.

### 3.3 Restart policy

Per child, not per pod:

- On exit, restart with exponential backoff (1 s doubling to 30 s).
- Count restarts in a sliding window. Past the cap — **5 restarts in 5 minutes** — the supervisor
  gives up on that child and exits, so the kubelet restarts the container. **That exit goes
  through the same cleanup as `SIGTERM`** — drop the label, stop the remaining children, release
  the lease — rather than through today's bare `sys.exit`. P3 is precisely the bug of having a
  second, cleanup-free way out; the supervisor must not reintroduce it.
- A child that has never started successfully is treated the same way, with the same cap. This
  matters for the KV server, which may legitimately need to wait out a departing leader's file
  lock; the length of that wait belongs to
  [`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md) and must fit inside the cap.
- On lease loss, stop all children (reverse start order) before returning to the watch loop.
  Termination keeps today's 10 s grace and `SIGKILL` fallback.

### 3.4 Health endpoint and readiness

The supervisor serves `GET /healthz` on `127.0.0.1:8700`, and it is the only thing the readiness
probe consults:

| Pod state                                   | Response                                            |
| ------------------------------------------- | --------------------------------------------------- |
| follower (elected mode, not the holder)     | `200 {"role": "follower", "children": []}`          |
| leader or solo, every child running         | `200 {"role": "leader"\|"solo", "children": [...]}` |
| leader or solo, a child down or backing off | `503 {"role": …, "children": [… "state": "down"]}`  |

A follower answering `200` is the point: it keeps every replica Ready, so the rollout arithmetic
in P4 works, while a leader with a dead child still goes NotReady and leaves the endpoint list.
Because the Service already selects on the leader label, NotReady on a leader is what actually
removes the only endpoint there is — which is the visibility that 1.4 lacks.

```yaml
readinessProbe:
  httpGet: { path: /healthz, port: 8700 }
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 6
```

`failureThreshold × periodSeconds` must exceed the longest legitimate child start, or a slow
start at failover becomes a container restart. That is the constraint the KV server's
lock-acquisition window has to be chosen against, in both directions.

The 60 s that arithmetic buys is also the margin protecting the single-replica case, where the
strategy is `Recreate` (1.5) and there is no old pod left to serve while a new one fails to
become Ready. `solo` mode has no election to lose and no follower branch, so the only way to be
NotReady there is a genuinely dead child — but the first probe this container has ever carried is
worth rolling to one agent before the fleet.

No liveness probe. A supervisor that has given up already exits (3.3), which is the same outcome
with fewer ways to be wrong.

### 3.5 Lease timing

State the inequality and pick parameters that satisfy it:

```
lease_duration_seconds  >  max_poll_interval + child_shutdown_grace
```

Today that reads `15 > 7 + 10`, which is false, and P5 is the consequence. The proposal:

| Parameter                | Today        | Proposed  |
| ------------------------ | ------------ | --------- |
| `lease_duration_seconds` | 15 s         | **30 s**  |
| poll interval            | 5 s + U(0,2) | unchanged |
| child termination grace  | 10 s         | unchanged |

`30 > 7 + 10` holds with margin. The cost is a longer failover blackhole — the window in which
the Service has zero ready endpoints grows by up to 15 s — which is a real regression in
availability bought for a real guarantee about exclusively-held resources. It is the right trade
only because the blackhole already exists and is already documented as inherited
(`leader_elect.py:12-16`); consumers must retry across it either way.

The guarantee this buys is narrow and should be stated as such: **in the absence of a partition,
the outgoing leader has stopped its children before any other pod can acquire the lease.**

### 3.6 What the Lease does not do

It does not fence. A leader partitioned from the API server keeps running until its own next poll
fails — and today's loop does self-terminate in that case, because a non-404 `ApiException`
leaves `holder` as `None` and falls into the loss branch (`:111-153`) — but "eventually
self-terminates" is not the same as "cannot still be writing". Nothing about the Lease prevents
two processes from both believing they are the leader for a bounded window.

Consequences for anything a child owns exclusively:

- It must be safe for the incoming instance to find the resource still held, and to wait.
- It must be safe for the same work to be attempted twice — idempotency keys, not locks, are what
  make the overlap survivable.

Callers that need continuity across the window retry; the server deduplicates. This design does
not attempt more, and 6 records why.

---

## 4. Operator changes

- Set the gateway container's `Args` to the supervisor at **every** replica count
  (`platformagent_manifests.go:2209-2212`). Note that the branch currently tests the effective
  replica count, so this also fixes the `scaleToZero` case in 1.1.
- Add the readiness probe of 3.4 to the `platform-agent` container (`:2248-2270`).
- Raise `lease_duration_seconds` to 30 s. That constant lives in
  `k8s-operator/internal/controller/leader_elect.py:70`, a real file that
  `platformagent_manifests.go:3305` pulls in with `//go:embed` and `:169` mounts as a ConfigMap
  key — it is not an inline string literal in the Go source.
- Update the two comments named in 3.1: `AGENT_SHARED_STATE_SETUP` at `:59-69` and
  `Args, never Command` at `:2197-2206`.
- Golden files in `k8s-operator/internal/testing/testdata/platform/expected/` gain the probe and,
  at a single replica, the `Args` they currently omit.

---

## 5. Migration

| Phase | Change                                                                                                                                                       | Risk                                                                                                                                                                                    |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1    | Supervisor modes and the child table, with the gateway as the only child. Operator sets `Args` unconditionally. Behaviour-preserving at both replica counts. | Low — the single-replica path gains a parent process and nothing else                                                                                                                   |
| S2    | Per-child restart policy and the health endpoint; readiness probe on the gateway container.                                                                  | Medium — first probe on this container, and at one replica the strategy is `Recreate`, so a probe that never passes is an outage rather than a stalled rollout. Roll to one agent first |
| S3    | Lease duration to 30 s.                                                                                                                                      | Low — longer blackhole, no new failure mode                                                                                                                                             |
| S4    | Second child adopted (the Session KV server), and entrypoint step 5 plus the MCP launcher deleted. Owned by `session-kv-decomposition.md` phase 3.           | Medium — the entrypoint gate check asserts on step 5                                                                                                                                    |

S1–S3 are independently shippable and are worth shipping before anything needs them. S4 is where
this design and the KV decomposition meet.

---

## 6. Verification

**Unit.** `leader_elect.py` has four tests today —
[`test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py),
run by `k8s-operator/Makefile:68` — and S1 breaks two of them rather than leaving them alone:

```python
@patch("leader_elect.subprocess.Popen")
@patch("leader_elect.time.sleep")
def test_acquire_lease_when_no_lease_exists(self, mock_sleep, mock_popen):
    ...
    # Verify it started the process
    mock_popen.assert_called_once()
```

`test_acquire_lease_when_no_lease_exists` and `test_take_over_expired_lease` both end on that
assertion, which the child table of 3.2 makes false the moment there are two children;
`test_renew_lease_when_leader` asserts `assert_not_called`, which survives. Rewrite the two
against the child table rather than against a single `Popen`.

Then add: mode selection from the environment; solo mode starts children and never touches the
API client; a child exiting is restarted with backoff; the restart cap exits the supervisor
**and drops the label and releases the lease on the way out**, which is the regression test for
P3;
lease loss stops children in reverse order; the health endpoint's three responses.

The existing file mocks the `kubernetes` package wholesale before importing the module
(`test_leader_elect.py:5-13`). Solo mode must not need that mock at all — a solo-mode test that
passes with `sys.modules['kubernetes']` unset is the real assertion that 3.1's "never contact the
API server" holds.

**Timing.** Assert the inequality in code rather than in prose — a startup check that
`lease_duration_seconds > max_poll_interval + grace` and refuses to start otherwise. It is one
line, and it is the only thing that keeps 3.5 true after someone tunes a constant.

**Operator.** `platformagent_manifests_test.go` for `Args` at a single replica and the probe on
both; the golden files above.

Note where the existing coverage sits, because S1 lands unevenly across it. The `replicas > 1`
branch is asserted by targeted unit tests — `:2351` pins the exact `Args` slice, `:2289-2293` the
election environment — but **all three golden files render `replicas: 1`**, so no golden exercises
the elected path at all; `leader_elect.py` appears in them only as a ConfigMap key and a
volumeMount. S1 therefore changes every golden (each gains `Args` where it has none today) while
the elected-path assertions stay where they are. A green golden diff is not evidence that the
elected path still works, and vice versa.

**Entrypoint.** `deploy/shared/entrypoint_gate_check.sh:313-324` asserts that port 8699 is
released after each case, and its header comment (`:27-31`) plus the reaper at `:87` are written
around step 5 owning that port. `tests/test_docker_entrypoint.py:19` uses the `logs/` directory
step 5 creates as its probe. All of them change at S4, not before. The site's
`deploy/docker-images.md:57,78` describes the entrypoint as starting the Session KV server and
goes stale then too.

**End-to-end**, at `replicas: 2`:

```bash
# A follower is Ready even though it runs no children.
kubectl -n kubeagents-system get pod <follower> \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'   # expect True

# A rollout completes. This is the check that P4 stayed fixed.
kubectl -n kubeagents-system rollout restart deploy/<agent>-gateway
kubectl -n kubeagents-system rollout status  deploy/<agent>-gateway --timeout=5m

# Killing a child takes the leader out of endpoints without restarting the pod.
kubectl -n kubeagents-system exec <leader> -c platform-agent -- pkill -f 'session_kv'
kubectl -n kubeagents-system get endpoints <agent>

# And the supervisor restarts it, so the pod comes back on its own.
```

At a single replica the check is simply that the container comes up with a supervisor as its main
process and both children running — which is the state the default deployment does not have
today.

---

## 7. Deliberate non-goals

- **No fencing token.** 3.6 is a limitation, not an oversight. Fencing SQLite behind a monotonic
  token means a second store to hold the token, which is the dependency
  `session-kv-decomposition.md` §8 declines for the same reason.
- **No request-continuous HA.** Raising the lease duration makes the blackhole longer, not
  shorter. Closing it needs warm standbys, which is a different design.
- **No supervision of sidecars.** This owns processes inside the `platform-agent` container. The
  event watcher, the credential proxy, and fluent-bit are containers, and the kubelet already
  supervises those.
