# Agent Process Supervisor

> **STATUS — draft; not implemented.** Nothing here ships today. Section 1 describes the
> launch path as it currently exists; sections 3 onward are the proposal.
>
> Section 1 was re-verified against `main` at `d44ea21` on 2026-08-17, 42 commits after the
> previous pass. **Every problem in section 2 still holds**, and the eleven manifest-level claims
> are now asserted against real rendered operator output rather than read off the source (E7 in
> 6.0): `leader_elect.py` and its tests are byte-identical, the gateway container still carries no
> probe at any replica count, `terminationGracePeriodSeconds` is still unset, and the lease
> inequality of 3.5 is still false.
>
> Two things changed underneath it. `platformagent_manifests.go` moved by several hundred lines,
> so every line number and anchor here was re-derived by locating the cited text in the new tree.
> And the Hindsight memory provider added a **fourth** background launcher to the entrypoint
> (1.2), which strengthens 3.7 rather than complicating it — it is the first concrete orphan the
> supervisor inherits, and E8 measures it.

**Scope:** how long-lived processes inside the `platform-agent` container are started,
supervised, and stopped — at every replica count — and how their health reaches the kubelet.
**Owns:** the container's process model, `leader_elect.py`'s two modes, the per-process restart
policy, the supervisor's health status and the probes that read it, the lease timing
parameters, and what the Lease does and does not fence.
**Does not own:** what any individual supervised process does. The Session KV server is specified in
[`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md), which depends on this design for
its launch path and cites it rather than restating it.

**Why this is a separate design.** The Session KV decomposition needs a supervised, single-owner
KV server, and reached for `leader_elect.py` to get one. But the gap it has to cross —
`leader_elect.py` does not run at all at the default replica count — is not a `session_kv`
problem. It is the reason the gateway container has no probes, the reason there are four things
that can start a background process and nothing that owns one, and the reason a process crash
restarts the whole container. Fixing it under the KV server's name would leave the next component
that needs a supervised sibling to rediscover the same ground.

---

## 0. Source files

Every file this design cites, linked to `main` — follow these to read a file as it stands today.

Links **inside** the sections below work the other way. Each one names a line range and is
anchored to it, pinned to
[`d44ea21`](https://github.com/gke-labs/kube-agents/commit/d44ea2187557eafb592f4ddb32f84582f0ec71d8),
the commit these line numbers were read from on 2026-08-17. Pinning is what keeps an anchor
honest: `#L139-L141` on a moving branch silently comes to point at whatever code later occupies
those lines, which is worse than no anchor at all. GitHub offers a jump to the current file from
any pinned view. All the URLs live in one block at the end of this document, so re-pinning after a
refresh is a single edit.

| File                                                                                                                                                                                     | Its part in this design                                          |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| [`k8s-operator/internal/controller/leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/leader_elect.py)                                 | The script that becomes the supervisor. 159 lines; read it whole |
| [`k8s-operator/internal/controller/test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py)                       | Its four existing tests, two of which S1 breaks                  |
| [`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh)                                                             | `exec "$@"`, the `IS_BOOTSTRAP_PRIMARY` gate, and step 5         |
| [`k8s-operator/internal/controller/platformagent_manifests.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/platformagent_manifests.go)           | `Args`, the probes, the Service selector, the two stale comments |
| [`k8s-operator/internal/controller/platformagent_manifests_test.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/platformagent_manifests_test.go) | Where the `replicas > 1` path is asserted today                  |
| [`k8s-operator/internal/controller/manifest_helpers.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/manifest_helpers.go)                         | Replica count and Deployment strategy                            |
| [`k8s-operator/api/v1alpha1/common_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/common_types.go)                                               | The CRD's optional `availability.replicas` (P1)                  |
| [`agents/platform/scripts/platform_mcp_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/platform_mcp_server.py)                                     | The second, racing KV-server launcher                            |
| [`agents/platform/scripts/session_kv_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/session_kv_server.py)                                         | The process S4 adopts; cited in 3.8A only                        |
| [`deploy/shared/entrypoint_gate_check.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/entrypoint_gate_check.sh)                                                     | Asserts port 8699 is released; changes at S4                     |

## 1. What exists today

### 1.1 Two launch paths, chosen by replica count

The image `ENTRYPOINT` is
[`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh),
which builds the shared tree and ends in `exec "$@"` ([`docker-entrypoint.sh:1283`][docker-entrypoint-sh-1283]). What `"$@"` is
depends on the replica count:

| Replicas | Gateway container `Args`                              | What supervises `hermes gateway run` |
| -------- | ----------------------------------------------------- | ------------------------------------ |
| 1        | unset — the image `CMD`                               | nothing; it is PID 1's exec target   |
| > 1      | `/opt/hermes/.venv/bin/python3 $HOME/leader_elect.py` | `leader_elect.py`                    |

The operator sets `Args` only in the `replicas > 1` branch, and sets `ENABLE_LEADER_ELECTION` /
`LEADER_ELECTION_LEASE_NAME` / `LEADER_ELECTION_NAMESPACE` in the same branch
([`platformagent_manifests.go:1560-1575`][platformagent_manifests-go-1560-1575]) —
[`platformagent_manifests.go:2277-2282`][platformagent_manifests-go-2277-2282]:

```go
var args []string

replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
if replicas > 1 {
	args = []string{"/opt/hermes/.venv/bin/python3", fmt.Sprintf("%s/leader_elect.py", homeDir)}
}
```

Deleting the `if` is the whole of change 3.1 on the operator side.

The branch tests the **effective** replica count, which `resolveDeploymentReplicasAndStrategy`
forces to `0` when `scaleToZero` is set ([`manifest_helpers.go:281-282`][manifest_helpers-go-281-282]). An agent configured
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

`execvp` replaces the process image, so there is no interpreter left to start anything else. Even
when the script is the entrypoint's exec target, it supervises nothing unless the election is
configured. This is P2, and 3.1's `solo` mode is what replaces these two lines.

### 1.2 Four launchers, none of them the supervisor

Nothing that is not the gateway is started by whatever is supervising the gateway:

| Process                    | Started by                                                                              | Supervised by |
| -------------------------- | --------------------------------------------------------------------------------------- | ------------- |
| `hermes gateway run`       | [`leader_elect.py:138`][leader_elect-py-138], or the entrypoint's exec                  | see above     |
| Session KV server          | [`docker-entrypoint.sh:1183-1190`][docker-entrypoint-sh-1183-1190], with `&`            | nothing       |
| Session KV server          | [`platform_mcp_server.py:744-785`][platform_mcp_server-py-744-785], if the port is free | nothing       |
| Hindsight memory migration | [`docker-entrypoint.sh:1246-1254`][docker-entrypoint-sh-1246-1254], with `&`            | nothing       |

The fourth row arrived with the Hindsight memory provider, after this design's first draft, and it
is a different kind of thing from the others: a one-shot migration that runs once and exits, not a
service. That is why it is **not** in the process table of 3.2 — there is nothing to keep running.

It belongs here because of _where_ it is started: backgrounded some thirty lines before
`exec "$@"`, which makes it a process the supervisor inherits and never launched. 3.7 is what has
to deal with it, and it turns that section's argument from a hypothetical into a measurement —
E8 in 6.0 shows this job becoming a zombie on every boot under today's supervisor, and being
reaped correctly under the one proposed here.

Drawn as process trees, the two replica counts do not look like variants of one design — they
look like two designs:

```text
  replicas: 1  (the default)              replicas: > 1
  ───────────────────────────             ───────────────────────────────────

  PID 1  docker-entrypoint.sh             PID 1  docker-entrypoint.sh
    │                                       │
    ├─ fork &  session_kv_server            ├─ fork &  session_kv_server
    │            (no owner)                 │            (no owner)
    │                                       │
    └─ exec ▸  hermes gateway run           └─ exec ▸  leader_elect.py
                 ▲                                       │
                 └─ IS PID 1; the kubelet                 └─ Popen  hermes gateway run
                    restarts the container                            ▲
                    if it exits                                       └─ one owner,
                                                                         one process
```

The left-hand tree has a detail that is easy to miss and that no one chose. `exec` replaces the
shell's process image but keeps its PID, so the Session KV server — forked from the shell a
moment earlier — ends up parented to `hermes gateway run`. Nothing intends that relationship and
nothing acts on it: the gateway never reaps it, never restarts it, and never reports on it. It is
a parent in the kernel's bookkeeping only.

The two KV rows also race each other: the MCP launcher probes port 8699 and spawns if the connect
fails, which is a TOCTOU against the entrypoint's background start. The loser exits with
`EADDRINUSE` into a log file on the PVC.

The entrypoint's start is no longer unconditional. It is gated on `IS_BOOTSTRAP_PRIMARY`
([`docker-entrypoint.sh:249-253`][docker-entrypoint-sh-249-253]), which is `0` for `PLATFORM_AGENT_ROLE=sidecar`:

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

`leader_elect.py` handles exactly one process and has one response to it exiting — the whole of it,
at [`leader_elect.py:134-153`][leader_elect-py-134-153]:

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

A single `process` global, `sys.exit` when it exits, and a 10 s grace on loss. The `SIGTERM` path
([`leader_elect.py:25-55`][leader_elect-py-25-55]) repeats the same terminate-wait-kill and then releases the lease. Three
things follow that section 2 turns into problems: the crash response restarts the whole container
without releasing anything (P3), the state is one variable rather than a table (3.2), and the 10 s
grace is one of the two terms in the timing inequality (P5).

### 1.4 No health signal from the gateway container

The `platform-agent` container declares no probe of any kind. The whole container spec is short
enough to show, and the point is what is absent from it
([`platformagent_manifests.go:2318-2336`][platformagent_manifests-go-2318-2336]):

```go
Name:            "platform-agent",
Image:           image,
ImagePullPolicy: pullPolicy,
Args:            args,
Ports: []corev1.ContainerPort{
	{
		Name:          "api",
		ContainerPort: 8642,
	},
},
Env:          gatewayEnvVars,
Resources:    resources,
VolumeMounts: volumeMounts,
SecurityContext: &corev1.SecurityContext{
	AllowPrivilegeEscalation: ptr.To(false),
	Capabilities: &corev1.Capabilities{
		Drop: []corev1.Capability{"ALL"},
	},
},
```

No `ReadinessProbe`, no `LivenessProbe`, no `StartupProbe` — the container that serves port 8642 is
never asked a question. The only readiness probe in the pod belongs to the `envoy-credential-proxy`
container, and it is the shape 3.4's probe should match — an existing, working example in the same
file ([`platformagent_manifests.go:1972-1979`][platformagent_manifests-go-1972-1979]):

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

At `replicas > 1` the Service selector gains `kubeagents.io/is-leader=true`
([`platformagent_manifests.go:2828`][platformagent_manifests-go-2828]), so followers are already excluded from endpoints by label.
Readiness today therefore changes nothing about routing, and its absence costs only visibility.

### 1.5 The timing parameters

| Parameter                    | Value                                      | Where                                                                                                            |
| ---------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `lease_duration_seconds`     | 15 s                                       | [`leader_elect.py:70`][leader_elect-py-70]                                                                       |
| poll interval                | 5 s + U(0,2)                               | [`leader_elect.py:71`][leader_elect-py-71], [`leader_elect.py:156`][leader_elect-py-156]                         |
| process termination grace    | 10 s                                       | [`leader_elect.py:36`][leader_elect-py-36], [`leader_elect.py:148`][leader_elect-py-148]                         |
| Deployment strategy, `n = 1` | **Recreate**                               | [`manifest_helpers.go:270-272`][manifest_helpers-go-270-272]                                                     |
| Deployment strategy, `n > 1` | RollingUpdate, 25% surge / 25% unavailable | [`manifest_helpers.go:61`][manifest_helpers-go-61], [`manifest_helpers.go:285-292`][manifest_helpers-go-285-292] |

The single-replica row matters for 3.4 and is easy to miss: the default deployment does not roll,
it is torn down and replaced. A readiness probe that never passes there is not a stalled rollout
with the old pod still serving — it is an outage.

---

## 2. Problems

| ID  | Severity | Problem                                                                                 | Closed by                 |
| --- | -------- | --------------------------------------------------------------------------------------- | ------------------------- |
| P1  | High     | The default replica count has no supervisor                                             | 3.1                       |
| P2  | High     | The unconfigured path cannot supervise                                                  | 3.1                       |
| P3  | Medium   | One process exiting restarts the whole container, and the crash path leaks leader state | 3.3                       |
| P4  | Medium   | Process health is invisible, and a naive probe makes it worse                           | 3.4                       |
| P5  | Medium   | The lease can be reacquired before the outgoing leader has let go                       | 3.5                       |
| P6  | Low      | A Lease does not fence                                                                  | 3.6 (bounded, not closed) |

P1 and P2 are the two that block everything else: until there is a supervisor at every replica
count, a second process has nothing to be supervised by. P3–P5 are defects in the supervision
that does exist. P6 is a property of leases rather than a bug, and 3.6 records it instead of
fixing it.

### P1 — The default replica count has no supervisor

`resolveDeploymentReplicasAndStrategy`
([`manifest_helpers.go:268-273`][manifest_helpers-go-268-273])
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
([`common_types.go:385-390`][common_types-go-385-390]):

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
into the container ([`platformagent_manifests.go:1818-1819`][platformagent_manifests-go-1818-1819]) and never executed.

That is what makes this the blocking problem rather than a rough edge. The Session KV
decomposition's plan — put the store under the election's supervision — is correct at
`replicas > 1` and vacuous at `replicas: 1`, because there is no supervisor to put it under. Any
process moved there disappears from the majority of installations, which is the opposite of the
intended effect.

### P2 — The unconfigured path cannot supervise

Even when the operator does make the script the exec target, it hands supervision back
immediately. The `execvp` shown in 1.1 is not an early return — it is a process-image
replacement: same PID, new program, and **no return on success**. There is no interpreter frame
left to run a second `Popen`, register a signal handler, or publish a health status.

This matters beyond the unconfigured case, because it rules out the smallest possible fix. "Just
add the KV server as a second `Popen` in `leader_elect.py`" works only on the elected path; on
the `execvp` path there is no `leader_elect.py` process any more, and on the single-replica path
the script was never the exec target to begin with. The three paths of 1.1 have to collapse into
one before a process table means anything, which is why 3.1 replaces the `execvp` with a `solo`
mode rather than adding a branch beside it.

### P3 — One process exiting restarts the whole container, and the crash path leaks leader state

The response to a process that has exited is two lines ([`leader_elect.py:139-141`][leader_elect-py-139-141]):

```python
elif process.poll() is not None:
    print(f"[{pod_name}] Hermes process crashed with code {process.returncode}. Exiting to trigger pod restart...", flush=True)
    sys.exit(process.returncode)
```

Three things are wrong with this, in increasing order of severity.

**It is a container restart, not a pod restart.** The message says pod; the operator sets no
`restartPolicy`, so the pod default `Always` applies to _containers_. The kubelet restarts the
`platform-agent` container inside the existing pod — same pod object, same `$HOSTNAME`, same
labels, same PVC. The design's own process table depends on this being understood correctly,
because a container restart re-runs the entrypoint from the top, which rebuilds the shared tree
and starts another Session KV server.

**It bypasses the cleanup path.** `sys.exit` here is not `release_lease_and_exit`; that function
is only ever reached through the signal handlers registered at [`leader_elect.py:63-64`][leader_elect-py-63-64]:

```python
signal.signal(signal.SIGTERM, release_lease_and_exit)
signal.signal(signal.SIGINT,  release_lease_and_exit)
```

Everything that function does on the way out is therefore skipped when a supervised process
exits ([`leader_elect.py:41-55`][leader_elect-py-41-55]):

```python
    try:
        config.load_incluster_config()
        coordination_v1 = client.CoordinationV1Api()
        v1 = client.CoreV1Api()

        lease = coordination_v1.read_namespaced_lease(name=lease_name, namespace=namespace)
        if lease.spec.holder_identity == pod_name:
            print(f"[{pod_name}] Releasing lease...", flush=True)
            lease.spec.holder_identity = None
            coordination_v1.replace_namespaced_lease(name=lease_name, namespace=namespace, body=lease)
            update_pod_label(v1, False)
    except Exception as e:
        print(f"[LeaderElect] Error releasing lease: {e}", file=sys.stderr, flush=True)

    sys.exit(0)
```

So the crash path never calls `update_pod_label(v1, False)` and never clears `holder_identity`:

| On process crash               | State left behind                                     |
| ------------------------------ | ----------------------------------------------------- |
| `kubeagents.io/is-leader=true` | **still on the pod** — the Service keeps selecting it |
| Lease `holder_identity`        | **still this pod** — until it expires 15 s later      |
| Readiness                      | unchanged, because there is no probe (P4)             |

The pod therefore stays in the Service's endpoint list, advertised as the leader, while the
container that serves traffic is restarting. It self-heals — on restart `process is None`, the
holder is still this pod, so it starts the gateway and re-labels — but the window is a
blackhole that nothing reports. P3 and P4 compound here: the label says leader, the endpoint
list agrees, and no probe contradicts either.

**One idiom does not survive a second process.** The rule is "any process exiting kills everything in
this container." With one process that is defensible. With the process table of 3.2 it means a KV
server that crash-loops on a corrupt database takes the gateway down with it on every iteration,
and each iteration re-runs the entrypoint. That is the failure 3.3's per-process backoff and
restart cap exist to prevent.

### P4 — Process health is invisible, and a naive probe makes it worse

The invisibility half is 1.4: no probe, so `/healthz` on 8699 is never called and a stopped
process is indistinguishable from a running one.

The trap is in the obvious fix. A readiness probe that reports on a **leader-only** process marks
every follower NotReady, and the rollout arithmetic does not survive that. Both knobs are set
from the same constant — `defaultSurgePercent = "25%"` ([`manifest_helpers.go:61`][manifest_helpers-go-61]), applied at
[`manifest_helpers.go:285-292`][manifest_helpers-go-285-292]:

```go
if intendedReplicas > 1 {
	strategy = appsv1.DeploymentStrategy{
		Type: appsv1.RollingUpdateDeploymentStrategyType,
		RollingUpdate: &appsv1.RollingUpdateDeployment{
			MaxSurge:       &intstr.IntOrString{Type: intstr.String, StrVal: defaultSurgePercent},
			MaxUnavailable: &intstr.IntOrString{Type: intstr.String, StrVal: defaultSurgePercent},
		},
	}
}
```

One constant, two knobs — but Kubernetes rounds them in opposite directions: `maxSurge` **up** and
`maxUnavailable` **down**. Evaluating `defaultSurgePercent` through
`intstr.GetScaledValueFromIntOrPercent`, the same helper the Deployment controller uses, gives the
effective values per replica count:

| Replicas | `maxSurge` (rounds up) | `maxUnavailable` (rounds down) | Rollout needs |
| -------- | ---------------------- | ------------------------------ | ------------- |
| 1        | 1                      | **0**                          | all 1 Ready   |
| 2        | 1                      | **0**                          | all 2 Ready   |
| 3        | 1                      | **0**                          | all 3 Ready   |
| 4        | 1                      | 1                              | 3 of 4 Ready  |
| 8        | 2                      | 2                              | 6 of 8 Ready  |

`maxUnavailable: 0` holds not just at two replicas but at **every count below four** — so a
leader-only probe, under which exactly one pod can ever be Ready, stalls the rollout at 2 and 3
replicas alike, until `progressDeadlineSeconds`, which the operator does not set and therefore
defaults to 600 s. Today's no-probe state at least rolls.

At a single replica the failure mode is worse, not milder: the strategy there is `Recreate`
(1.5), so the old pod is torn down _before_ the new one is created. A probe that never passes is
then an outage with nothing to roll back to. This is why 3.4 keeps followers Ready — the
probe reports on the supervisor, which every replica runs, rather than on processes only the
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

Worst case for an outgoing leader to notice it has lost the lease and finish shutting down:

```
  7 s   maximum poll interval (5 + U(0,2))
+ 10 s  process termination grace before SIGKILL
= 17 s  before the process is guaranteed gone

  15 s  after which any other replica may take the lease
```

The lease expires two seconds _before_ the outgoing leader is required to have stopped anything.
An incoming leader can therefore be starting its processes while the outgoing one's are still
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
([`leader_elect.py:111-153`][leader_elect-py-111-153]):

```python
except ApiException as e:
    if e.status == 404:
        ...                       # create the lease; may set holder = pod_name
    else:
        print(f"[LeaderElect] Error reading lease: {e}", file=sys.stderr, flush=True)
        # holder stays None -> the `else` branch below stops the process
```

So a partitioned leader does stop its processes — at its own next poll, up to 7 s later, and only
if the failure surfaces as an `ApiException` rather than a hang. "Eventually self-terminates" is
not "cannot still be writing," and the gap is unbounded if the API client blocks rather than
raising.

This is a limitation to design around, not a bug to fix. Closing it needs a fencing token — a
monotonically increasing number issued with the lease and checked on every write — which needs a
second store to hold the token, which is the dependency both this design and
[`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md)
§8 decline. 3.6 states the consequences instead: anything a process owns exclusively must tolerate
finding it still held, and any work that crosses the window must be idempotent.

---

## 3. Design

### 3.1 One supervisor, two modes, every replica count

`leader_elect.py` becomes a supervisor with an explicit mode, and the operator makes it the
gateway container's `Args` unconditionally.

```
mode = elected  if LEADER_ELECTION_LEASE_NAME and LEADER_ELECTION_NAMESPACE else solo
```

- **`solo`** — behave as a permanent leader. Start the processes, supervise them, never contact
  the API server, never label the pod. This replaces the `os.execvp` at [`leader_elect.py:60-61`][leader_elect-py-60-61];
  the reason to supervise rather than exec is that there is more than one process to start, and
  that is true independent of how many replicas there are.
- **`elected`** — today's loop, unchanged in structure: acquire, label, start processes, renew,
  and on loss drop the label and stop them.

**Why two modes rather than "always elect"?** Running the election at every replica count would
be one code path instead of two, and it is not blocked by permissions: the leader `Role` and
`RoleBinding` granting lease and pod access are reconciled **unconditionally**
([`platformagent_controller.go:841-855`][platformagent_controller-go-841-855]), not gated on replica count. So the argument for `solo` is
not that it avoids RBAC.

The argument is availability. An elected supervisor cannot start the gateway until it has talked
to the API server and won a lease. At one replica — the default, and the case with no second pod
to fall back on — that would make an API-server outage into an agent outage, for an election with
exactly one candidate whose result is a foregone conclusion. `solo` keeps the default deployment
independent of the control plane, and confines the election to the configuration that actually
has something to elect.

Two consequences worth naming. `solo` is chosen from the environment the operator renders, not
from an observed pod count, so a Deployment scaled directly with `kubectl scale` — outside the
CR — briefly runs several pods that all believe they are permanent leaders. The operator
reconciles the replica count back, and at one replica the volume is `ReadWriteOnce`, which limits
the blast radius; it is a pre-existing hazard rather than one this design introduces, but making
the mode explicit is what makes it visible. And a `solo` supervisor never labels the pod, so at
one replica the Service selector must continue not to require `kubeagents.io/is-leader`
([`platformagent_manifests.go:2828`][platformagent_manifests-go-2828] adds it only above one replica) — S1 must not disturb that.

Making the script the exec target at every replica count is what collapses 1.1's table to one
row. It has two knock-ons, both of them comments in the operator that are written around the
single-replica case this removes:

- The entrypoint's shared-state auto-detection looks for a bare `gateway` argument
  ([`platformagent_manifests.go:65-70`][platformagent_manifests-go-65-70]), and the gateway container's argv only carries one at a
  single replica today. The operator already names the owner explicitly with
  `AGENT_SHARED_STATE_SETUP=owner`, so nothing changes in behaviour — but the comment gets simpler
  and should be updated rather than left describing a case that no longer exists.
- The `Args, never Command` comment ([`platformagent_manifests.go:2267-2276`][platformagent_manifests-go-2267-2276]) explains the
  exec-target choice partly in terms of the entrypoint "start[ing] the Session KV server on 8699
  that the event-watcher is pointed at". That clause survives S1 but not S4, where the entrypoint
  stops starting it.

### 3.2 The process table

**Terminology.** A **supervised process** is a long-lived process the supervisor starts, watches,
restarts and stops — as distinct from the supervisor itself, and from the short-lived commands
either of them may shell out to. The supervisor holds them in a table rather than in the single
`process` global of 1.3, and the rest of this design says "process" for a table entry wherever
that is unambiguous.

| Supervised process   | Start | Stop | Criticality                                               |
| -------------------- | ----- | ---- | --------------------------------------------------------- |
| Session KV server    | 1     | 2    | **optional** — the gateway's plugins fail open without it |
| `hermes gateway run` | 2     | 1    | **required** — the container exists to run it             |

**Criticality is a property of the table, not a footnote**, and 3.3 and 3.4 both branch on it. An
optional process being down costs a feature; a required one being down means the container is not
doing its job. Collapsing the two — treating any stopped process as equivalent — is how a
fail-open dependency turns into an outage, which is a mistake this design made in an earlier
draft and 3.4 now avoids explicitly.

Both replica counts collapse to one tree, differing only in the supervisor's mode:

```text
  solo  (replicas: 1)                     elected  (replicas: > 1, this pod holds the lease)
  ─────────────────────────               ────────────────────────────────────────────────

  PID 1  docker-entrypoint.sh             PID 1  docker-entrypoint.sh
    │                                       │
    └─ exec ▸  leader_elect.py              └─ exec ▸  leader_elect.py
                 │  mode = solo                          │  mode = elected
                 │  run/supervisor.json                  │  run/supervisor.json
                 │                                       │  holds <agent>-leader
                 ├─[1] session_kv_server                 ├─[1] session_kv_server
                 └─[2] hermes gateway run                └─[2] hermes gateway run

                                          A follower runs the same supervisor with
                                          nothing under it, and is still Ready.
```

The numbering is the start order; stop runs in reverse.

The ordering is deliberate rather than incidental. The plugins inside the gateway fail open when
the KV server is absent, so a slow start costs attribution rather than availability — but the
dependency runs in that direction and the start order should say so. On the way down the same
dependency dictates the reverse: stop the gateway first so its plugins stop calling the KV
server, then stop the KV server.

**Stopping is sequential, and that has a cost 3.5 and 3.8 both have to account for.** Each
process gets its own grace before `SIGKILL`, so the shutdown budget is the _sum_ over the table,
not one grace. Two processes at 10 s each is 20 s, and adding a third makes it 30 s. Two things
are sized against that total rather than against a single grace: the lease inequality (3.5) and
the pod's `terminationGracePeriodSeconds` (3.8).

Both write to inherited stdout/stderr rather than to a file on the PVC, so their output reaches
fluent-bit like everything else and nothing grows unbounded on the volume. The cost of sharing
one stream is that lines from the two interleave with nothing to tell them apart — today the KV
server at least has its own file. Inheriting the descriptors directly, rather than piping through
the supervisor to add a prefix, is the deliberate choice: a pump thread per process is a new way
to block or lose output, and the processes already prefix their own lines
(`[LeaderElect]`, uvicorn's own format). If that proves insufficient in practice, the fix belongs
in the processes' log formats, not in the supervisor.

### 3.3 Restart policy

Per process, not per pod:

- On exit, restart with exponential backoff (1 s doubling to 30 s).
- Count restarts in a sliding window, **per process**. The gateway flapping must not consume the
  KV server's budget.
- Past the cap — **5 restarts in 5 minutes** — what happens depends on the criticality column of
  3.2, and this is the part an earlier draft got wrong:

  | Past the cap | Action                                                                                                            |
  | ------------ | ----------------------------------------------------------------------------------------------------------------- |
  | **required** | Give up. The supervisor exits, and the kubelet restarts the container.                                            |
  | **optional** | Give up on **that process only**. Leave it stopped, mark the supervisor `degraded`, keep everything else running. |

  A uniform cap that always exits is P3 wearing a different hat: it means a KV server
  crash-looping on a corrupt database eventually takes a perfectly healthy gateway down with it,
  and each cycle re-runs the 1008-line entrypoint. That is the exact complaint P3 makes about
  today's behaviour, arriving five restarts later. An optional process that cannot start is a
  degradation and must be reported as one, not escalated into a container restart.

- **The exit path is `release_lease_and_exit`, never a bare `sys.exit`.** Dropping the label,
  stopping the remaining processes and releasing the lease all have to happen. P3 is precisely
  the bug of having a second, cleanup-free way out; the supervisor must not reintroduce it.
- **Cleanup writes a final `ready: false` status before exiting.** Without it the last document on
  disk still says `ready: true`, and the probe keeps returning Ready for up to the staleness
  window (3.4) while the container is on its way down. A PoC hit exactly this: after the
  supervisor exited on a required process, the probe still reported `ready=True healthy` because
  the file was only a second old. One write on the way out closes it; the staleness check is the
  backstop, not the mechanism.
- **Failure to start counts as a restart.** `Popen` can raise before there is anything to poll —
  a missing binary, an unwritable log path — and that must feed the same counter rather than
  spinning at full speed. This is also the path the KV server takes while it waits out a
  departing leader's file lock; the length of that wait belongs to
  [`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md)
  and must fit inside the cap.
- On lease loss, stop all processes in reverse start order before returning to the watch loop.
  Termination keeps today's 10 s grace and `SIGKILL` fallback per process — see 3.2 on why the
  total, not the per-process figure, is what 3.5 and 3.8 are sized against.

Sketched, to fix the shape rather than the implementation — this is the replacement for the
`elif process.poll() is not None: sys.exit(...)` of 1.3:

```python
RESTART_CAP    = 5     # restarts ...
RESTART_WINDOW = 300   # ... within this many seconds
BACKOFF_MAX    = 30

class Supervised:
    def __init__(self, name, argv, required):
        self.name, self.argv, self.required = name, argv, required
        self.proc = None
        self.state = "pending"           # pending | running | backoff | gave_up
        self.backoff = 1
        self.retry_at = 0
        self.restarts = deque()          # monotonic timestamps, trimmed to RESTART_WINDOW

    def start(self, now):
        try:
            self.proc = subprocess.Popen(self.argv)
            self.state = "running"
        except OSError as exc:           # missing binary, unwritable path, ...
            log(f"{self.name}: start failed: {exc}")
            self.proc = None
            self._penalise(now)          # a failed start is a restart, or it spins

    def on_exit(self, code, now):
        """Called ONLY by reap() in 3.7, which owns every waitpid in this process.
        Deliberately no self.proc.poll() anywhere here -- see 3.7 for why a second
        caller of waitpid silently rewrites a crash into a clean exit."""
        log(f"{self.name}: exited {code}")
        self.exit, self.proc, self.state = code, None, "exited"
        self._penalise(now)

    def tick(self, now):
        """Once per supervisor iteration. False => a REQUIRED process is past its cap."""
        if self.state == "pending" or (self.state == "backoff" and now >= self.retry_at):
            self.start(now)
        return self.state != "gave_up" or not self.required

    def _penalise(self, now):
        self.restarts.append(now)
        while self.restarts and now - self.restarts[0] > RESTART_WINDOW:
            self.restarts.popleft()
        if len(self.restarts) >= RESTART_CAP:
            self.state = "gave_up"       # required -> supervisor exits; optional -> degraded
            return
        self.state = "backoff"
        self.retry_at = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)
```

The shape is what matters, not the details. `tick` returning `False` — only ever for a
**required** process — routes through `release_lease_and_exit`, not `sys.exit`. A `gave_up`
**optional** process leaves the supervisor running and surfaces as `degraded` in 3.4's status. And
`start` failing is charged to the same counter as an exit, because otherwise a missing binary is
an infinite loop at full speed rather than a bounded one.

### 3.4 Health status and readiness

#### A status file, not an HTTP server

The supervisor writes a status document to `$PLATFORM_AGENT_HOME/run/supervisor.json` at the end
of every poll iteration — atomically, temp-file-and-rename — and the probe is an `exec` that
reads it:

```json
{
  "role": "leader",
  "ready": true,
  "degraded": true,
  "updated_at": 1755000000,
  "processes": [
    { "name": "session_kv", "required": false, "state": "gave_up" },
    {
      "name": "gateway",
      "required": true,
      "state": "running",
      "listening": true
    }
  ]
}
```

An earlier draft served this over `GET /healthz` on `127.0.0.1:8700` and probed it with
`httpGet`. **That combination cannot work**: a probe's `httpGet` connects to the _pod IP_, not to
loopback, so a server bound to `127.0.0.1` is unreachable from the kubelet and the probe fails
every time. Binding `0.0.0.0` instead would fix the probe and put an unauthenticated status
endpoint on the pod IP, which the NetworkPolicy's ingress allowlist (8642/8643, plus 9119 for the
dashboard) does not cover.

A file avoids the dilemma and is smaller in every direction. There is no port to allocate, no
HTTP server to run inside what is otherwise a `time.sleep` loop, no thread sharing mutable state
with the poll loop — and it matches the repository, where **every existing probe is an `exec`**
and none is an `httpGet`.

#### Freshness is the point of `updated_at`

Serving health from a thread beside the poll loop has a failure mode that is easy to miss and
fatal to the purpose: if the loop wedges — blocked on an API call, which 3.6 concedes can
happen — the thread keeps answering `200` from stale state. The probe would report healthy
precisely when the supervisor has stopped supervising.

A file makes the check trivial, so the probe does it: if `updated_at` is older than three poll
intervals the supervisor is not running its loop, whatever the rest of the document says.

```bash
#!/bin/sh
# readiness: exit 0 = Ready. Reads only; never blocks on anything but the file.
S="$PLATFORM_AGENT_HOME/run/supervisor.json"
[ -f "$S" ] || exit 1
python3 - "$S" <<'EOF' || exit 1
import json, sys, time
s = json.load(open(sys.argv[1]))
if time.time() - s["updated_at"] > 30:   # 3 x the poll interval: the loop has wedged
    sys.exit(1)
sys.exit(0 if s["ready"] else 1)
EOF
```

#### What `ready` means, and what it deliberately does not

| Pod state                                            | `ready` | Effect                                    |
| ---------------------------------------------------- | ------- | ----------------------------------------- |
| follower (elected mode, not the holder)              | `true`  | stays Ready; runs nothing                 |
| leader or solo, everything running                   | `true`  | serving                                   |
| leader or solo, an **optional** process down         | `true`  | Ready but `degraded: true`                |
| leader or solo, the **required** process not running | `false` | NotReady, leaves the endpoint list        |
| status file older than three poll intervals          | `false` | NotReady — the supervisor's loop is stuck |

Two of these rows are load-bearing.

**A follower answering `true`** keeps every replica Ready, so the rollout arithmetic in P4 works.
Because the Service already selects on the leader label, a follower's readiness changes no
routing; it only has to not stall the rollout.

**An optional process down must not make the pod NotReady.** 3.2 says the KV server's absence
costs attribution rather than availability. A probe that answered `false` for it would take the
leader out of the endpoint list — turning a fail-open degradation into a total outage, and making
the probe strictly worse than having none. `degraded` is what carries that signal instead: it is
visible in the status file and in the logs, and it is what an alert should fire on, but it does
not touch routing.

#### The residual gap: running is not serving

`state: "running"` means the process has a PID. It does not mean it is serving. To narrow that,
the supervisor also does a TCP connect to the gateway's `127.0.0.1:8642` each poll and records
`listening` — cheap, no new endpoint required, and strictly stronger than a PID check, because it
proves the listener is bound.

It is still not proof of service: a gateway that accepts connections and then wedges reports
`listening: true`. Closing that needs a cheap health route on the gateway itself — the closest
thing today is `POST /v1/responses` ([`hack/ci-deploy.sh:140`][ci-deploy-sh-140]), which is a model call and far too
expensive to run every 10 s. **This is a known and accepted limitation of S2**, not something the
probe silently covers; it is listed as an open question in 8.

```yaml
readinessProbe:
  exec: { command: ["/opt/hermes/bin/supervisor-ready"] }
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 6
```

`failureThreshold × periodSeconds` = 60 s must exceed the longest legitimate start of a
**required** process. Note what failing does and does not do: a failed **readiness** probe removes
the pod from the endpoint list; it never restarts the container. Only a liveness probe restarts.
So the 60 s buys tolerance of a slow start without flapping the endpoint list, and the KV server's
lock-acquisition window is bounded by the restart cap of 3.3 rather than by this figure.

At a single replica the strategy is `Recreate` (1.5), so there is no old pod serving while a new
one fails to become Ready. `solo` mode has no election to lose and no follower branch, so the only
way to be NotReady there is a required process that really is down — but this is the first probe
this container has ever carried, and it is worth rolling to one agent before the fleet.

#### On the liveness probe

An earlier draft declined one on the grounds that "a supervisor that has given up already exits."
That covers giving up; it does not cover **hanging**, and the two are different failures. A
supervisor blocked forever inside an API call never exits, and under a readiness-only regime the
pod goes NotReady and simply stays there — no restart, no recovery, manual intervention. At one
replica, with `Recreate`, that is an indefinite outage.

The freshness check above turns that state into a NotReady signal, which is the diagnosis. Turning
it into a _recovery_ needs a liveness probe running the same script with a longer threshold:

```yaml
livenessProbe:
  exec: { command: ["/opt/hermes/bin/supervisor-ready"] }
  initialDelaySeconds: 60
  periodSeconds: 20
  failureThreshold: 6 # 120 s — must be comfortably longer than readiness
```

Sequencing matters more than the numbers. A liveness probe that fires wrongly is a restart loop,
and this container has never carried a probe of any kind, so **liveness ships after readiness has
soaked**, not with it. 5 splits them across S2 and S2b for that reason.

### 3.5 Lease timing

State the inequality and pick parameters that satisfy it. **The shutdown term is the sum over the
process table, not one process's grace** — 3.2 stops them sequentially, so each one's grace is
paid in turn:

```
lease_duration_seconds  >  max_poll_interval + Σ(per-process shutdown grace)
```

Getting this wrong is easy and was wrong in an earlier draft, which sized the proposal against a
single 10 s grace and claimed a 13 s margin. With the two-process table of 3.2 the shutdown term
is 20 s, and the same proposal has a margin of 3 s. **The term grows with every process added to
the table**, so this is a constraint that silently tightens as the design succeeds.

Today, with one process, the term is 10 s. Laid out on a timeline from the moment the outgoing
leader stops being the holder:

```text
  TODAY — lease_duration = 15 s        15 > 7 + 10   is FALSE

   t     outgoing leader                    any other replica
  ────   ──────────────────────────────     ──────────────────────────────
   0     loses the lease
         │
         │  poll interval: up to
         │  5 + U(0,2) = 7 s
   7     notices; SIGTERM to its
         │  processes
         │
         │  10 s termination grace
         │
  15     │                                  lease expires — may acquire
         │                                  │
         │                                  └─ starts its own processes
         │  ◄════════ 2 s OVERLAP ════════► │
         │                                  │
  17     SIGKILL; processes finally gone    already running


  PROPOSED — lease 30 s, poll 3+U(0,1), two processes    30 > 4 + 20   holds

   t     outgoing leader                    any other replica
  ────   ──────────────────────────────     ──────────────────────────────
   0     loses the lease
   4     notices; stops the gateway
  14     gateway gone; stops session_kv
  24     session_kv gone — table empty
         ·
         ·  6 s margin
         ·
  30                                        lease expires — may acquire
```

P5 is the two-second overlap in the upper timeline.

#### Three ways to satisfy it, and they are not equally priced

The inequality can be repaired from either side, and an earlier draft reached for the most
expensive one without weighing the others:

| Option                                   | Result with two processes | Cost                                                    |
| ---------------------------------------- | ------------------------- | ------------------------------------------------------- |
| A. Raise the lease, 15 → 30 s            | `30 > 7 + 20`, margin 3 s | **+15 s failover blackhole** — a real availability loss |
| B. Shorten each grace, 10 → 4 s          | `15 > 7 + 8`, margin 0 s  | processes get under half the time to shut down cleanly  |
| C. Shorten the poll, 5+U(0,2) → 3+U(0,1) | `15 > 4 + 20` — **fails** | ~1.7× more lease reads; nothing else                    |
| **A + C** (proposed)                     | `30 > 4 + 20`, margin 6 s | +15 s blackhole, ~1.7× lease reads                      |

C alone cannot carry it — the shutdown term dominates once there are two processes — but it is
nearly free and doubles the margin that A alone provides, which matters because that margin has
to absorb a third process later. B is the one to avoid: 4 s is not obviously enough for the
gateway to finish in-flight work, and buying failover safety by truncating clean shutdown trades
one correctness problem for another.

| Parameter                 | Today        | Proposed         |
| ------------------------- | ------------ | ---------------- |
| `lease_duration_seconds`  | 15 s         | **30 s**         |
| poll interval             | 5 s + U(0,2) | **3 s + U(0,1)** |
| process termination grace | 10 s         | unchanged        |

The cost is a longer failover blackhole — the window in which the Service has zero ready
endpoints grows by up to 15 s — which is a real regression in availability bought for a real
guarantee about exclusively-held resources. It is the right trade only because the blackhole
already exists and is already documented as inherited ([`leader_elect.py:12-16`][leader_elect-py-12-16]); consumers must
retry across it either way. The faster poll partly repays it: the outgoing leader now notices 3 s
sooner, and a _new_ leader also detects an expired lease sooner.

The guarantee this buys is narrow and should be stated as such: **in the absence of a partition,
the outgoing leader has stopped its processes before any other pod can acquire the lease.**

### 3.6 What the Lease does not do

It does not fence. A leader partitioned from the API server keeps running until its own next poll
fails — and today's loop does self-terminate in that case, because a non-404 `ApiException` leaves
`holder` as `None` and falls into the loss branch ([`leader_elect.py:111-153`][leader_elect-py-111-153]) — but "eventually
self-terminates" is not the same as "cannot still be writing". Nothing about the Lease prevents two
processes from both believing they are the leader for a bounded window.

Consequences for anything a process owns exclusively:

- It must be safe for the incoming instance to find the resource still held, and to wait.
- It must be safe for the same work to be attempted twice — idempotency keys, not locks, are what
  make the overlap survivable.

Callers that need continuity across the window retry; the server deduplicates. This design does
not attempt more, and 6 records why.

### 3.7 The supervisor is PID 1, and PID 1 has a job

3.1 makes the supervisor the entrypoint's `exec` target at every replica count, which makes it
PID 1 in the container. That is already true above one replica, so none of this is new — but
generalising it to the default deployment makes it worth stating, because PID 1 carries two
obligations an ordinary process does not.

**Signals.** The kernel installs no default handlers for PID 1: an unhandled `SIGTERM` is
_ignored_ rather than fatal. [`leader_elect.py:63-64`][leader_elect-py-63-64] already registers one, so this works today;
it is listed because deleting that registration would not fail any test, and the symptom — pods
that take the full grace period and then die by `SIGKILL` on every rollout, losing the lease
release every time — is a slow, easily-misattributed regression.

**Reaping.** Orphaned processes reparent to PID 1, and PID 1 must `wait()` for them or they
accumulate as zombies until the PID table fills. This matters here more than it would elsewhere:
the agent shells out constantly, and the KV server runs `hermes send` per alert.

It is also no longer hypothetical. The entrypoint backgrounds the Hindsight memory migration
(1.2's fourth row) roughly thirty lines before `exec "$@"`, so that subshell's parent is PID 1 —
the supervisor, which never started it and does not know it exists. **Measured on every boot:
one zombie under today's supervisor, none under the reaper below** (E8 in 6.0). Today that
zombie is harmless, because one leaked entry per pod lifetime is not going to exhaust anything;
it matters because it proves the path is live rather than theoretical, and the next background
job added to the entrypoint inherits the same treatment.

So the loop reaps once per iteration:

**Reaping and `Popen.poll()` cannot coexist**, and the failure is quieter than it looks. An
earlier draft of this section proposed reaping `-1` and skipping PIDs found in the process table.
That does not work, for two reasons a PoC established rather than argued:

- **The guard is too late.** By the time `waitpid(-1)` has returned a PID, it has already consumed
  that process's status. Checking membership afterwards detects the theft; it does not prevent it.
- **`poll()` then reports success, not "unknown".** CPython's `Popen._internal_poll` catches the
  resulting `ECHILD` and sets `returncode = 0`. Measured: a process that exited **3**, reaped
  externally first, is subsequently reported by `poll()` as having exited **0**.

That second point is the dangerous one. The supervisor does not see a confusing state it might
log and recover from — it sees a clean, successful exit. Any policy that treats exit 0 as
"stopped on purpose" would stop restarting a process that is in fact crash-looping, and the
restart cap of 3.3 would never fire.

So there is exactly one `waitpid` in the design, and it **dispatches** rather than discards:

```python
def reap(table):
    """The single point of truth for child exits. Nothing else may call waitpid/poll."""
    by_pid = {p.proc.pid: p for p in table if p.proc is not None}
    while True:
        try:
            pid, sts = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return                              # no children at all
        if pid == 0:
            return                              # children exist, none have exited
        code = os.waitstatus_to_exitcode(sts)
        entry = by_pid.get(pid)
        if entry is None:
            continue                            # an orphan grandchild; reaped, discarded
        entry.exit = code                       # <- the status the table would otherwise lose
        entry.proc, entry.state = None, "exited"
        entry.penalise(time.monotonic())
```

The consequence for 3.3 is a constraint, not an option: `Supervised.poll()` as sketched there must
**not** call `self.proc.poll()` — which is why the sketch there splits `tick` (start and cap
arithmetic) from `on_exit` (called only from here). Two callers of `waitpid` is the bug; one caller
that knows the table is the design.

This raises the value of Q1 in 8 considerably. `tini` does the reaping half correctly and by
construction — but note that it does **not** remove the constraint above, because `tini` only
reaps PIDs the supervisor has not claimed. The supervisor still needs one owner of its own
children's statuses.

**This is also the strongest argument for `tini`**, which does exactly the above and is far better
tested than the twelve lines here. 3.8B rejects `tini` as a _supervisor_; it does not reject it as
PID 1. Running `tini -- python3 leader_elect.py` gets correct reaping and signal forwarding for
free while leaving every lease decision in the supervisor. The reason not to is the image
dependency and one more moving part in the boot path; the reason to is that reaping is fiddly and
already solved. **Either is defensible, and the choice should be made deliberately rather than by
omission** — 8 records it as open.

**`terminationGracePeriodSeconds` has to be raised.** The operator does not set it, so it is the
Kubernetes default of **30 s**, and the shutdown path now has to fit inside that:

```
  10 s   stop the gateway   (grace, then SIGKILL)
+ 10 s   stop session_kv    (grace, then SIGKILL)      -- sequential, see 3.2
+  ~2 s  read + rewrite the Lease, patch off the label
= ~22 s  against a 30 s budget
```

Roughly 8 s of headroom, and a third process in the table erases it. When the budget is exceeded
the kubelet `SIGKILL`s the supervisor mid-cleanup — which loses the lease release and the label
drop, arriving at exactly the leaked-leader state P3 describes, by a different route. Set
`terminationGracePeriodSeconds: 60` explicitly on the pod, and treat it as a value derived from
the process table rather than a constant.

### 3.8 Alternatives considered

#### A. Make the Session KV server its own container

The obvious cheaper answer, and the one to beat: don't write a supervisor at all. Move the KV
server into a second container in the same pod and let the kubelet own it.

```text
  A — sidecar container                    B — supervised process (3.2)
  ─────────────────────────────────        ──────────────────────────────────

  pod                                      pod
  ├── platform-agent                       ├── platform-agent
  │   └── hermes gateway run               │   └── leader_elect.py  (supervisor)
  │        ▲ still no owner,               │        ├─[1] session_kv_server
  │          still no probe                │        └─[2] hermes gateway run
  │                                        │        ▲ one owner, one probe,
  ├── session-kv          ◄── kubelet      │          ordered start/stop
  │   └── session_kv_server   owns only    │
  │                           this one     ├── envoy-credential-proxy
  └── envoy-credential-proxy               └── dashboard
```

It is genuinely attractive. The kubelet gives restart-with-backoff, `CrashLoopBackOff` reporting,
and per-container probes for nothing, and 7 already concedes the principle — the event watcher,
the credential proxy and fluent-bit are supervised exactly this way. Four things rule it out
here, and only the first two are about this design rather than about cost.

**1. It fixes one of the six problems.** P1 and P2 are about the KV server having no owner, and a
container closes them. P3 and P4 are about the **gateway** container — the crash path that leaks
leader state, and the absence of any probe on the process that actually serves traffic. Moving a
different process into a different container leaves both exactly where they are. The left-hand
diagram is deliberately drawn to show that: `hermes gateway run` is as unowned after the change
as before.

**2. Ordering across containers is not expressible.** 3.2 requires the KV server to start first
and stop last, and 3.5 requires the outgoing leader to have released before the incoming one
acquires. Kubernetes can order container **startup** — `initContainers`, and native sidecars —
but has no primitive for a lease-driven stop-and-start cycle in the middle of a pod's life. Two
containers each reacting to the same lease on their own timers have no ordering relationship
between one's stop and the other's start, and no third party to impose one.

That is the real distinction from the watcher, and it is worth being precise about it because the
obvious objection — "a container cannot be lease-gated" — is **false**.
[`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md)
§4.3 proposes exactly that for the event watcher, which lives inside `envoy-credential-proxy`. A
container can watch the Lease and idle. The difference is what each needs from it:

|                      | Event watcher                              | Session KV server                               |
| -------------------- | ------------------------------------------ | ----------------------------------------------- |
| Needs from the lease | a boolean: am I the leader?                | a **handover**: has the previous holder let go? |
| If it acts early     | duplicate alerts, deduplicated server-side | opens a file the outgoing leader still holds    |
| If it acts late      | an event-coverage gap                      | nothing serves 8699                             |
| Failure mode         | soft, self-correcting                      | hard, and 4.2's lock retry exists because of it |

Self-gating answers the boolean. It does not produce a handover, because a handover needs one
actor sequencing both sides — which is what a supervisor is.

**3. Followers pay for it.** A container is scheduled on every replica whether or not it does
anything. Under the single-writer rule only the leader's KV server may run, so at `replicas: 3`
two of the three reserve CPU and memory to idle in a lease-watch loop. A supervised process is
simply not started.

**4. It needs the tree the entrypoint builds.** The KV server shells out to the Hermes CLI —
`["hermes", "send", "--json", "--to", active_platform, alert_msg]`
([`session_kv_server.py:382`][session_kv_server-py-382]) — so the container would need the platform image and the data PVC
mounted at `$HERMES_HOME`, and would re-run the entrypoint's tree build. That is not fatal;
`envoy-credential-proxy` already mounts the data volume at `homeDir`. It is a duplicated cost
rather than an impossibility.

**When to revisit.** Reasons 2 and 3 both descend from the single-writer requirement, which comes
from [`session-kv-decomposition.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/session-kv-decomposition.md)
§4 and is not yet in force. **If that design is abandoned, this one should be re-scoped rather
than shipped as written**: without exclusive access there is no handover to sequence and no
reason to gate on the leader, and a plain sidecar container becomes the better answer to "the KV
server has no owner." What would remain worth doing is P3 and P4 — the crash-path cleanup and a
readiness probe on the gateway container — which are defects in the leader election itself and
are independent of where the KV server runs. 5 keeps S1–S2 separable for that reason.

#### B. Adopt a general init system as PID 1

`tini`, `s6-overlay`, `supervisord` and friends solve process supervision properly and are better
tested than anything written here. **Two separate jobs are easy to conflate here, and only one of
them is being rejected.**

**As the supervisor — rejected.** Start and stop in this container are **lease-driven**, not
static. A generic supervisor's model is "keep this set of programs running"; what is needed is
"run this set only while this pod holds a Lease, and stop them in reverse order when it does
not." Expressing that means teaching the supervisor about the Lease, at which point the election
logic lives in a config file and a set of hook scripts instead of in `leader_elect.py`, and the
pod gains an image dependency for the privilege. There is also an ordering constraint:
`docker-entrypoint.sh` must run before anything is supervised, because it builds the tree the
processes read, so any init system would have to be its `exec` target — the slot the supervisor
occupies.

**As PID 1 — open, and arguably yes.** `tini`'s actual job is zombie reaping and signal
forwarding, which 3.7 shows this design has to do either way and can get wrong in a way no test
would catch. `tini -- python3 leader_elect.py` composes cleanly with everything above: `tini`
reaps and forwards, the supervisor owns every lease decision. This is complementary to the design
rather than an alternative to it, and 8 records the choice as open rather than settling it here.

---

## 4. Operator changes

- Set the gateway container's `Args` to the supervisor at **every** replica count
  ([`platformagent_manifests.go:2279-2282`][platformagent_manifests-go-2279-2282]). Note that the branch currently tests the effective
  replica count, so this also fixes the `scaleToZero` case in 1.1.
- Add the **exec** readiness probe of 3.4 to the `platform-agent` container
  ([`platformagent_manifests.go:2318-2340`][platformagent_manifests-go-2318-2340]) — matching the `envoy-credential-proxy` probe's shape,
  not an `httpGet`. The liveness probe follows in a later phase, not with it.
- Set `terminationGracePeriodSeconds: 60` on the pod spec (3.7). It is unset today, so it is 30 s,
  which the two-process shutdown budget does not fit inside with useful margin.
- Raise `lease_duration_seconds` to 30 s and lower the poll interval to `3 + U(0,1)` (3.5). Both
  constants live in [`k8s-operator/internal/controller/leader_elect.py:70-71`][leader_elect-py-70-71], a real file that
  [`platformagent_manifests.go:3416`][platformagent_manifests-go-3416] pulls in with `//go:embed` and
  [`platformagent_manifests.go:185`][platformagent_manifests-go-185] mounts as a ConfigMap key — they are not inline string literals
  in the Go source.
- Ship the readiness script the probe execs. It is a new file in the image rather than an operator
  change, but it versions with the operator's embedded `leader_elect.py` and has to move with it.
- Update the two comments named in 3.1: `AGENT_SHARED_STATE_SETUP` at
  [`platformagent_manifests.go:60-70`][platformagent_manifests-go-60-70] and `Args, never Command` at
  [`platformagent_manifests.go:2267-2276`][platformagent_manifests-go-2267-2276].
- Golden files in `k8s-operator/internal/testing/testdata/platform/expected/` gain the probe and,
  at a single replica, the `Args` they currently omit.

---

## 5. Migration

| Phase | Change                                                                                                                                                                                | Risk                                                                                                                                                                                    |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1    | Supervisor modes and the process table, with the gateway as the only process. PID-1 reaping (3.7). Operator sets `Args` unconditionally. Behaviour-preserving at both replica counts. | Low — the single-replica path gains a parent process and nothing else                                                                                                                   |
| S2    | Per-process restart policy, the status file, and the **readiness** probe.                                                                                                             | Medium — first probe on this container, and at one replica the strategy is `Recreate`, so a probe that never passes is an outage rather than a stalled rollout. Roll to one agent first |
| S2b   | **Liveness** probe over the same script, longer threshold.                                                                                                                            | Medium — a wrongly-firing liveness probe is a restart loop. Ships only after S2's readiness has soaked                                                                                  |
| S3    | Lease 30 s, poll `3 + U(0,1)`, `terminationGracePeriodSeconds: 60`.                                                                                                                   | Low — longer blackhole, no new failure mode                                                                                                                                             |
| S4    | Second process adopted (the Session KV server), and entrypoint step 5 plus the MCP launcher deleted. Owned by `session-kv-decomposition.md` phase 3.                                  | Medium — the entrypoint gate check asserts on step 5                                                                                                                                    |

**S1, S2 and S2b are worth shipping on their own merits.** They fix P1–P4, which are live defects
independent of anything the KV decomposition does: the KV server is already an unsupervised second
process, the crash path already leaks leader state, and the gateway already has no probe.

**S3 is different, and should not ship on its own.** Its guarantee — release-before-acquire — has
no consumer today. Nothing currently holds a resource exclusively across a failover: there is no
`locking_mode=EXCLUSIVE` anywhere, the entrypoint's `flock` is released before `exec`, and port
8699 is per-pod rather than shared. So S3 pays up to 15 s of extra failover blackhole for a
property nothing yet relies on. **Sequence it with S4**, which is what introduces the exclusive
hold that makes it necessary. The two parts of S3 that are useful immediately — the faster poll
and the raised grace period — can go with S2 instead, since both only ever help.

S4 is where this design and the KV decomposition meet.

---

## 6. Verification

### 6.0 What was prototyped, and what it changed

The mechanisms in 3.3–3.7 were prototyped before this design was finalised — a supervisor with the
process table, criticality, backoff and cap, the status file, the probe, and sequential shutdown,
with the lease stubbed to a file so it runs without Kubernetes, plus a Go check that renders the
operator's real manifests. **Three of the experiments falsified something this document previously
asserted**, and those corrections are folded into the sections above.

The prototype lives in
[`agent-process-supervisor/`](https://github.com/gke-labs/kube-agents/tree/main/docs/designs/agent-process-supervisor)
next to this file and runs with the standard library alone:

```bash
cd docs/designs/agent-process-supervisor && python3 run_experiments.py
```

Every experiment asserts, so a non-zero exit means a claim below has stopped holding — which is
what re-ran them after the `d44ea21` merge and confirmed nothing had rotted. It is **not wired
into CI**, for two reasons: the timing-based cases are prototype-grade, and E7 asserts what is
true _today_, so several of its checks are supposed to start failing the moment S1/S2 ship.
Keeping that in CI would be a tripwire on the implementation rather than a regression test. The
directory is meant to be **deleted at S1/S2**, when its cases become the `test_leader_elect.py`
additions listed under **Unit** below.

| #   | Claim under test                                                   | Result                                                                                                                                                                                                     |
| --- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| E1  | A generic `waitpid(-1)` breaks the table's view of its own process | **Confirmed, and worse than stated.** See 3.7 — `poll()` reports `0`, not "unknown", and the proposed guard does not work                                                                                  |
| E2  | `httpGet` cannot reach a server bound to `127.0.0.1`               | **Confirmed.** `ECONNREFUSED` from the routable address; `0.0.0.0` connects. `HTTPGetAction.Host` "defaults to the pod IP"                                                                                 |
| E3  | 25% yields `maxUnavailable: 0` at 2 replicas                       | **Confirmed and widened** — it is 0 at 1, 2 _and_ 3 replicas (P4)                                                                                                                                          |
| E4  | Optional-vs-required divergence at the cap                         | **Confirmed.** Optional → `ready:true, degraded:true`, supervisor lives. Required → cleanup, then exit. Also surfaced the stale-`ready:true`-on-exit gap now fixed in 3.3                                  |
| E5  | Shutdown is the sum over the table                                 | **Confirmed.** 10 s / 20 s / 30 s for 1 / 2 / 3 processes at a 10 s grace — 3 processes overruns the 30 s default and needs 3.7's 60 s                                                                     |
| E6  | The inequality catches table growth                                | **Confirmed.** Reproduces every margin in 3.5: today −2, option A +3, A+C +6, and A+C with a third process −4 (refuses to start)                                                                           |
| E7  | The eleven manifest-level claims of sections 1 and 3               | **Confirmed against rendered output.** Renders Deployments at 1/2/3 replicas through the operator's own `buildDeployment`, plus `buildNetworkPolicy` and `buildPlatformLeaderRole`, and asserts each claim |
| E8  | An entrypoint background job reparents to the supervisor           | **Confirmed.** One zombie per boot under today's supervisor, none under 3.7's reaper. The case is the Hindsight migration in 1.2                                                                           |

E2 and E3 are checks against the authoritative source rather than against reasoning: E2 reads
`HTTPGetAction.Host`'s own documentation in the vendored `k8s.io/api`, and E3 evaluates
`defaultSurgePercent` through `intstr.GetScaledValueFromIntOrPercent` — the function the Deployment
controller itself calls.

Its value was in being wrong three times before any of this reached an implementation PR, and the
cases it exercised are the ones listed under **Unit** below.

**Unit.** `leader_elect.py` has four tests today —
[`test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py),
run by [`k8s-operator/Makefile:68`][Makefile-68] — and S1 breaks two of them rather than leaving them alone:

```python
@patch("leader_elect.subprocess.Popen")
@patch("leader_elect.time.sleep")
def test_acquire_lease_when_no_lease_exists(self, mock_sleep, mock_popen):
    ...
    # Verify it started the process
    mock_popen.assert_called_once()
```

`test_acquire_lease_when_no_lease_exists` and `test_take_over_expired_lease` both end on that
assertion, which the process table of 3.2 makes false the moment there are two processes;
`test_renew_lease_when_leader` asserts `assert_not_called`, which survives. Rewrite the two
against the process table rather than against a single `Popen`.

Then add: mode selection from the environment; solo mode starts processes and never touches the
API client; a process exiting is restarted with backoff; a `Popen` that _raises_ is charged to the
same counter rather than spinning; lease loss stops processes in reverse order.

The cases that carry the corrections in 3.3, 3.4 and 3.7 deserve naming individually, because each
is a bug this design had in an earlier draft:

- A **required** process past its cap exits the supervisor **through the cleanup path** — the
  label is dropped and the lease released. This is the regression test for P3.
- An **optional** process past its cap leaves the supervisor running, the required process
  untouched, and the status file `ready: true, degraded: true`. This is the test that a fail-open
  dependency cannot cause an outage.
- The status file's `updated_at` goes stale when the loop is blocked, and the probe script exits
  non-zero on it. Simulate by holding the loop, not by editing the file.
- The reaper does **not** consume a supervised process's exit status: spawn an orphan, reap it,
  and assert the process table still observes its own process exiting normally.

The existing file mocks the `kubernetes` package wholesale before importing the module
([`test_leader_elect.py:5-13`][test_leader_elect-py-5-13]). Solo mode must not need that mock at all — a solo-mode test that
passes with `sys.modules['kubernetes']` unset is the real assertion that 3.1's "never contact the
API server" holds.

**Timing.** Assert the inequality in code rather than in prose. It is genuinely one statement,
placed where the constants are defined so that tuning one without the other cannot start:

```python
lease_duration_seconds = 30
base_poll_interval     = 3
poll_jitter            = 1
process_shutdown_grace = 10

# 3.5: the outgoing leader must be finished before anyone else may acquire.
# The shutdown term is the SUM over the process table -- 3.2 stops them one at a
# time -- so this tightens automatically when a process is added.
shutdown_budget = process_shutdown_grace * len(PROCESS_TABLE)
assert lease_duration_seconds > base_poll_interval + poll_jitter + shutdown_budget, (
    f"lease_duration_seconds={lease_duration_seconds} must exceed "
    f"{base_poll_interval}+{poll_jitter} poll + {shutdown_budget} shutdown "
    f"({len(PROCESS_TABLE)} processes x {process_shutdown_grace}s)"
)
```

Deriving the shutdown term from `len(PROCESS_TABLE)` rather than hard-coding it is the point. The
inequality is not a fact about three constants; it is a fact about the constants _and the size of
the table_, and the table is the thing most likely to grow. Written this way, adding a third
process fails at startup instead of quietly eating the margin.

`terminationGracePeriodSeconds` is subject to the same arithmetic (3.7) but lives in the operator,
so it cannot be asserted from inside the pod. Assert it in `platformagent_manifests_test.go`
instead: the rendered grace period must exceed the same `shutdown_budget` plus a lease-release
allowance.

This is the only thing that keeps 3.5 true after someone tunes a constant, and it fails at
startup — loudly, in the pod's own logs — rather than at the failover it would otherwise
silently break.

**Operator.** `platformagent_manifests_test.go` for `Args` at a single replica and the probe on
both; the golden files above.

Note where the existing coverage sits, because S1 lands unevenly across it. The `replicas > 1`
branch is asserted by targeted unit tests — [`platformagent_manifests_test.go:2260`][platformagent_manifests_test-go-2260] pins the exact
`Args` slice, [`platformagent_manifests_test.go:2198-2202`][platformagent_manifests_test-go-2198-2202] the election environment — but **all three
golden files render `replicas: 1`**, so no golden exercises the elected path at all;
`leader_elect.py` appears in them only as a ConfigMap key and a volumeMount. S1 therefore changes
every golden (each gains `Args` where it has none today) while the elected-path assertions stay
where they are. A green golden diff is not evidence that the elected path still works, and vice
versa.

**Entrypoint.** [`deploy/shared/entrypoint_gate_check.sh:313-324`][entrypoint_gate_check-sh-313-324] asserts that port 8699 is released
after each case, and its header comment ([`entrypoint_gate_check.sh:27-31`][entrypoint_gate_check-sh-27-31]) plus the reaper at
[`entrypoint_gate_check.sh:87`][entrypoint_gate_check-sh-87] are written around step 5 owning that port.
[`tests/test_docker_entrypoint.py:19`][test_docker_entrypoint-py-19] uses the `logs/` directory step 5 creates as its probe. All of
them change at S4, not before. The site's `deploy/docker-images.md:57,78` describes the entrypoint
as starting the Session KV server and goes stale then too.

**End-to-end**, at `replicas: 2`:

```bash
# A follower is Ready even though it runs no processes.
kubectl -n kubeagents-system get pod <follower> \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'   # expect True

# A rollout completes. This is the check that P4 stayed fixed.
kubectl -n kubeagents-system rollout restart deploy/<agent>-gateway
kubectl -n kubeagents-system rollout status  deploy/<agent>-gateway --timeout=5m

# Killing the OPTIONAL process must NOT remove the leader from endpoints -- it is
# degraded, not unready. This is the check that 3.4 did not turn a fail-open
# dependency into an outage.
kubectl -n kubeagents-system exec <leader> -c platform-agent -- pkill -f 'session_kv'
kubectl -n kubeagents-system get endpoints <agent>          # leader still listed
kubectl -n kubeagents-system exec <leader> -c platform-agent -- \
  cat /opt/data/run/supervisor.json                          # ready:true, degraded:true
# ...and the supervisor restarts it, so degraded clears on its own.

# Killing the REQUIRED process does remove it, and it comes back.
kubectl -n kubeagents-system exec <leader> -c platform-agent -- pkill -f 'hermes gateway'
kubectl -n kubeagents-system get endpoints <agent>          # leader gone, then returns

# Graceful shutdown fits the grace period: no SIGKILL in the events, and the
# lease is released rather than left to expire. This is the 3.7 budget check.
kubectl -n kubeagents-system delete pod <leader> --wait=true
kubectl -n kubeagents-system get lease <agent>-leader -o jsonpath='{.spec.holderIdentity}'
```

At a single replica the check is simply that the container comes up with a supervisor as its main
process and both processes running — which is the state the default deployment does not have
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
  supervises those. The converse — moving a supervised process **out** into a container of its
  own, so that the kubelet supervises it too — is the alternative weighed and rejected in 3.8A,
  along with the conditions under which it would become the better answer.
- **No proof that a process is serving, only that it is listening.** 3.4 records why: the gateway
  has no cheap health route, and inventing one is a change to the gateway rather than to its
  supervisor. Listed in 8 rather than hidden.

---

## 8. Open questions

Decisions this design deliberately leaves open, recorded so they are made rather than defaulted
into.

| #   | Question                                                                                                                                                                                                                                                                                                       | Bears on |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| Q1  | **`tini` as PID 1, or hand-rolled reaping?** 3.7 shows the reap loop is twelve lines and has one subtle failure (consuming a supervised process's exit status) that no test would obviously catch. `tini -- python3 leader_elect.py` is complementary, not competing. Cost is an image dependency.             | S1       |
| Q2  | **How does the probe learn the gateway is _serving_, not merely listening?** The TCP connect in 3.4 is strictly better than a PID check and strictly weaker than a health check. Closing it needs a cheap route on the gateway; `POST /v1/responses` is a model call and far too expensive at probe frequency. | S2, S2b  |
| Q3  | **Is 5-in-5-minutes the right cap, given the kubelet's own backoff sits underneath it?** For a required process the supervisor exiting hands over to `CrashLoopBackOff`, so the two compose. The cap may be redundant for required processes and only genuinely load-bearing for optional ones.                | S2       |
| Q4  | **Should the readiness script live in the image or the ConfigMap?** It versions with the embedded `leader_elect.py`, so they must move together; the ConfigMap already carries one file for exactly that reason.                                                                                               | S2       |

Q1 and Q3 are cheap to settle during implementation. Q2 is the one that should not be quietly
dropped: it is the difference between a probe that detects a stopped process and one that detects
a broken one, and the design currently only claims the former.

<!-- Source links, line-anchored and pinned to the commit these line numbers
     were read from (d44ea21). Re-pin here when the numbers are refreshed. -->

[Makefile-68]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/Makefile#L68
[ci-deploy-sh-140]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/hack/ci-deploy.sh#L140
[common_types-go-385-390]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/api/v1alpha1/common_types.go#L385-L390
[docker-entrypoint-sh-1183-1190]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/docker-entrypoint.sh#L1183-L1190
[docker-entrypoint-sh-1246-1254]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/docker-entrypoint.sh#L1246-L1254
[docker-entrypoint-sh-1283]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/docker-entrypoint.sh#L1283
[docker-entrypoint-sh-249-253]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/docker-entrypoint.sh#L249-L253
[entrypoint_gate_check-sh-27-31]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/entrypoint_gate_check.sh#L27-L31
[entrypoint_gate_check-sh-313-324]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/entrypoint_gate_check.sh#L313-L324
[entrypoint_gate_check-sh-87]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/deploy/shared/entrypoint_gate_check.sh#L87
[leader_elect-py-111-153]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L111-L153
[leader_elect-py-12-16]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L12-L16
[leader_elect-py-134-153]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L134-L153
[leader_elect-py-138]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L138
[leader_elect-py-139-141]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L139-L141
[leader_elect-py-148]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L148
[leader_elect-py-156]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L156
[leader_elect-py-25-55]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L25-L55
[leader_elect-py-36]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L36
[leader_elect-py-41-55]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L41-L55
[leader_elect-py-60-61]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L60-L61
[leader_elect-py-63-64]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L63-L64
[leader_elect-py-70-71]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L70-L71
[leader_elect-py-70]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L70
[leader_elect-py-71]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/leader_elect.py#L71
[manifest_helpers-go-268-273]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/manifest_helpers.go#L268-L273
[manifest_helpers-go-270-272]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/manifest_helpers.go#L270-L272
[manifest_helpers-go-281-282]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/manifest_helpers.go#L281-L282
[manifest_helpers-go-285-292]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/manifest_helpers.go#L285-L292
[manifest_helpers-go-61]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/manifest_helpers.go#L61
[platform_mcp_server-py-744-785]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/agents/platform/scripts/platform_mcp_server.py#L744-L785
[platformagent_controller-go-841-855]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_controller.go#L841-L855
[platformagent_manifests-go-1560-1575]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L1560-L1575
[platformagent_manifests-go-1818-1819]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L1818-L1819
[platformagent_manifests-go-185]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L185
[platformagent_manifests-go-1972-1979]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L1972-L1979
[platformagent_manifests-go-2267-2276]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2267-L2276
[platformagent_manifests-go-2277-2282]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2277-L2282
[platformagent_manifests-go-2279-2282]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2279-L2282
[platformagent_manifests-go-2318-2336]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2318-L2336
[platformagent_manifests-go-2318-2340]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2318-L2340
[platformagent_manifests-go-2828]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L2828
[platformagent_manifests-go-3416]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L3416
[platformagent_manifests-go-60-70]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L60-L70
[platformagent_manifests-go-65-70]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests.go#L65-L70
[platformagent_manifests_test-go-2198-2202]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests_test.go#L2198-L2202
[platformagent_manifests_test-go-2260]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/platformagent_manifests_test.go#L2260
[session_kv_server-py-382]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/agents/platform/scripts/session_kv_server.py#L382
[test_docker_entrypoint-py-19]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/tests/test_docker_entrypoint.py#L19
[test_leader_elect-py-5-13]: https://github.com/gke-labs/kube-agents/blob/d44ea2187557eafb592f4ddb32f84582f0ec71d8/k8s-operator/internal/controller/test_leader_elect.py#L5-L13
