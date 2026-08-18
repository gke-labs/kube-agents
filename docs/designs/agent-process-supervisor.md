# Agent Process Supervisor

> **STATUS — draft; not implemented.** Nothing here ships today. Section 1 describes the
> launch path as it currently exists; sections 3 onward are the proposal.
>
> Section 1 was re-verified against `main` at `76a074b` on 2026-08-18. **Every problem in section
> 2 still holds**, and the eleven manifest-level claims are asserted against real rendered
> operator output rather than read off the source (E7 in 6.0): `leader_elect.py` and its tests are
> byte-identical, the gateway container still carries no probe at any replica count,
> `terminationGracePeriodSeconds` is still unset, and the lease inequality of 3.5 is still false.
>
> Three things changed underneath it across the last two passes. `platformagent_manifests.go`
> moved by several hundred lines, so every line number and anchor here was re-derived by locating
> the cited text in the new tree — 22 of the 71 anchors moved at the most recent rebase and were
> re-pinned mechanically. `k8s-operator/Makefile` stopped naming `test_leader_elect.py` and now
> discovers it (#722), which changes how 6 cites it but not that it runs. And the Hindsight memory
> provider added a **fourth** background launcher to the entrypoint (1.2), which strengthens 3.7
> rather than complicating it — it is the first concrete orphan the supervisor inherits, and E8
> measures it.
>
> **Q5 has since been answered, and this revision is written for the answer.**
> `session-kv-decomposition.md`
> §8 now settles the store as a file rather than moving it to the in-cluster Postgres, so the
> single-writer requirement this design exists to serve is load-bearing rather than provisional.
> 3.8A keeps the Postgres analysis as the recorded reason and as the trigger that would reopen it;
> 8 records Q5 as closed. Three further corrections came out of the same pass and are not
> cosmetic: 3.5's poll term was unbounded and is now a **renew deadline** (the mechanism client-go
> uses, which 3.0 already cited without adopting); 3.5 mispriced the failover blackhole by ignoring
> that a clean handover releases the lease rather than waiting out its expiry; and 3.2's claim that
> inherited stdout reaches fluent-bit was simply wrong about how this pod collects logs.

**Scope:** how long-lived processes inside the `platform-agent` container are started,
supervised, and stopped — at every replica count — and how their health reaches the kubelet.
**Owns:** the container's process model, `leader_elect.py`'s two modes, the per-process restart
policy, the supervisor's health status and the probes that read it, the lease timing
parameters, and what the Lease does and does not fence.
**Must satisfy:** R1–R12 in 2.0, bounded by the hard limits L1–L11 and the three budgets beside
them. Those outlive this design — 3.8A works through a storage change that would collapse most of
section 3 while leaving the requirements standing.
**Does not own:** what any individual supervised process does. The Session KV server is specified in
`session-kv-decomposition.md`, which depends on this design for
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
[`76a074b`](https://github.com/gke-labs/kube-agents/commit/76a074b8cddc467c753e33801c3c69d814ec8469),
the commit these line numbers were read from on 2026-08-18. Pinning is what keeps an anchor
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
| [`k8s-operator/config/integrations/hindsight/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/hindsight)                                            | The memory service, deployed separately — 3.2 and 3.8A           |
| [`platformagent_manifests.go` — `buildFluentBitConfigMap`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/platformagent_manifests.go)                | How this pod actually collects logs. 3.2 was wrong about it      |

This table is files in **this** repository. The named patterns each mechanism comes from, and the
external implementations worth reading before writing any of it, are in 3.0.

**`session-kv-decomposition.md` is named throughout and deliberately not linked.** It is the
companion design this one hands S4 to, and it is not on `main` and has no pull request — so a
`blob/main` link to it is a 404 that nothing catches, because `make docs-check` verifies relative
links and cannot see absolute ones. An earlier revision of this document carried thirteen of them,
and the companion records making the mirror-image mistake in its own §0. When it lands, the name
becomes a link in one edit; until then, naming it is the honest form. Every other absolute link
here was checked against `main` and resolves.

## 1. What exists today

### 1.1 Two launch paths, chosen by replica count

The image `ENTRYPOINT` is
[`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh),
which builds the shared tree and ends in `exec "$@"` ([`docker-entrypoint.sh:1293`][docker-entrypoint-sh-1293]). What `"$@"` is
depends on the replica count:

| Replicas | Gateway container `Args`                              | What supervises `hermes gateway run` |
| -------- | ----------------------------------------------------- | ------------------------------------ |
| 1        | unset — the image `CMD`                               | nothing; it is PID 1's exec target   |
| > 1      | `/opt/hermes/.venv/bin/python3 $HOME/leader_elect.py` | `leader_elect.py`                    |

The operator sets `Args` only in the `replicas > 1` branch, and sets `ENABLE_LEADER_ELECTION` /
`LEADER_ELECTION_LEASE_NAME` / `LEADER_ELECTION_NAMESPACE` in the same branch
([`platformagent_manifests.go:1561-1576`][platformagent_manifests-go-1561-1576]) —
[`platformagent_manifests.go:2298-2303`][platformagent_manifests-go-2298-2303]:

```go
var args []string

replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
if replicas > 1 {
	args = []string{"/opt/hermes/.venv/bin/python3", fmt.Sprintf("%s/leader_elect.py", homeDir)}
}
```

Deleting the `if` is the whole of change 3.1 on the operator side.

The branch tests the **effective** replica count, which `resolveDeploymentReplicasAndStrategy`
forces to `0` when `scaleToZero` is set ([`manifest_helpers.go:286-287`][manifest_helpers-go-286-287]). An agent configured
`availability.replicas: 3` with `scaleToZero: true` therefore renders no election wiring at all.

**"No election wiring" is four separate gates, not one**, and they matter to 3.1 because mode
selection reads the second of them rather than the first. All four independently ask the same
question:

| Gate                                         | Where                                                                          | Keyed on  |
| -------------------------------------------- | ------------------------------------------------------------------------------ | --------- |
| `Args` — is there a supervisor at all?       | [`platformagent_manifests.go:2298-2303`][platformagent_manifests-go-2298-2303] | effective |
| `LEADER_ELECTION_*` — solo or elected? (3.1) | [`platformagent_manifests.go:1561-1576`][platformagent_manifests-go-1561-1576] | effective |
| Service selector — `kubeagents.io/is-leader` | [`platformagent_manifests.go:2846-2849`][platformagent_manifests-go-2846-2849] | effective |
| PVC access mode — `ReadWriteOnce` vs `Many`  | [`platformagent_manifests.go:100-117`][platformagent_manifests-go-100-117]     | effective |

Because all four agree, the `scaleToZero` rendering is _inert_ rather than wrong: no pods, no
election, and a `ReadWriteOnce` volume that matches. **What is not true is that 3.1's unconditional
`Args` tidies it up.** Removing only the first gate leaves the other three where they are, which
renders a supervisor with no lease environment — `solo` mode, a permanent leader on every pod — on
a Deployment the user configured for three replicas. 4 records why the remaining three
deliberately stay on the effective count, and 3.1 what the residual exposure is.

**A fifth replica-derived value exists and reads the other count.** The Deployment strategy is
selected on `intendedReplicas`, not on the effective count
([`manifest_helpers.go:290`][manifest_helpers-go-290]) — the one place in this file that does. So
`availability.replicas: 3` with `scaleToZero: true` renders **RollingUpdate** beside `solo` mode
and a `ReadWriteOnce` volume:

| Value                | `replicas: 3`, `scaleToZero: true` | Keyed on  |
| -------------------- | ---------------------------------- | --------- |
| the four gates above | single-replica behaviour           | effective |
| Deployment strategy  | **RollingUpdate**, 25% / 25%       | intended  |

It is not a bug today, because the strategy of a Deployment with zero pods never runs. It matters
to this design for two reasons: the "all four agree" argument above is narrower than it sounds, and
1.5's `Recreate`-at-one-replica figure — which 3.4 and P4 both lean on — holds for the effective
count only by coincidence of the two agreeing everywhere except here. E7's `C9` pins the
disagreement so that a later change to either side is visible rather than silent.

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
| Session KV server          | [`docker-entrypoint.sh:1193-1200`][docker-entrypoint-sh-1193-1200], with `&`            | nothing       |
| Session KV server          | [`platform_mcp_server.py:748-789`][platform_mcp_server-py-748-789], if the port is free | nothing       |
| Hindsight memory migration | [`docker-entrypoint.sh:1256-1264`][docker-entrypoint-sh-1256-1264], with `&`            | nothing       |

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
    # Bound to loopback, not 0.0.0.0. Every caller — this container's MCP
    # server and incident_context plugin, and the event watcher in the
    # credential-proxy container — reaches it over the shared pod network
    # namespace, so nothing needs it published on the pod IP. It carries chat
    # identifiers, so the narrower bind is the correct default.
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 127.0.0.1 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi
```

The `&` and the redirect to a file on the PVC are the two things 3.2 changes. So the dashboard
container no longer races the gateway for the port inside a single pod, which is the failure the
gate's own comment records. That narrows the race to two parties, and it removes the intra-pod
half of the problem. It does not remove the cross-replica half, and it does not make either
starter a supervisor: both still background the process with `&` and neither ever looks at it
again.

The loopback bind is quoted in full rather than elided because 3.8A turns on it. Loopback works
for every caller **because they share the pod's network namespace**, which a second container in
the same pod would too — so the bind is not an argument against 3.8A's sidecar option, and 3.8A
does not use it as one.

One line of this block is shared infrastructure and must survive S4: `mkdir -p "$TARGET_DIR/logs"`
is also what the Hindsight migration thirty lines below appends into. Inside the image the Hermes
skeleton already creates `logs/`, so deleting the `mkdir` breaks nothing in a real container — but
it does break `tests/test_docker_entrypoint.py`, which uses that directory as its host-side probe
for "did the setup run" and says so in its own docstring. S4 deletes the server, not the block.

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
([`platformagent_manifests.go:2339-2357`][platformagent_manifests-go-2339-2357]):

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
([`platformagent_manifests.go:2849`][platformagent_manifests-go-2849]), so followers are already excluded from endpoints by label.
Readiness today therefore changes nothing about routing, and its absence costs only visibility.

### 1.5 The timing parameters

| Parameter                    | Value                                      | Where                                                                                                            |
| ---------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `lease_duration_seconds`     | 15 s                                       | [`leader_elect.py:70`][leader_elect-py-70]                                                                       |
| sleep between polls          | 5 s + U(0,2)                               | [`leader_elect.py:71`][leader_elect-py-71], [`leader_elect.py:156`][leader_elect-py-156]                         |
| lease read/write timeout     | **none** — the client default              | [`leader_elect.py:78`][leader_elect-py-78], [`leader_elect.py:84`][leader_elect-py-84]                           |
| renew deadline               | **none** — there is no such concept        | —                                                                                                                |
| process termination grace    | 10 s                                       | [`leader_elect.py:35`][leader_elect-py-35], [`leader_elect.py:148`][leader_elect-py-148]                         |
| Deployment strategy, `n = 1` | **Recreate**                               | [`manifest_helpers.go:275-277`][manifest_helpers-go-275-277]                                                     |
| Deployment strategy, `n > 1` | RollingUpdate, 25% surge / 25% unavailable | [`manifest_helpers.go:61`][manifest_helpers-go-61], [`manifest_helpers.go:290-297`][manifest_helpers-go-290-297] |

Two rows deserve their names being "none" rather than being left out. **A poll iteration is not
bounded by the sleep**: it is the sleep plus a lease read and, on the leader, a lease write, none
of which carries a timeout. So "the leader notices within 7 s" is true of the sleep and false of
the iteration, which is the error 3.5 used to make. And there is **no renew deadline** — nothing
relates "how long since I last renewed successfully" to "when do I stop acting as leader." Today
the only thing that stops a leader is a poll that comes back and says someone else holds the
lease; a poll that never comes back stops nothing. 3.5 adds both.

The single-replica strategy row matters for 3.4 and is easy to miss: the default deployment does
not roll, it is torn down and replaced. A readiness probe that never passes there is not a stalled
rollout with the old pod still serving — it is an outage. Note it is keyed on the **intended**
replica count, unlike the four gates of 1.1.

---

## 2. Requirements and problems

### 2.0 What this has to satisfy, and what bounds it

The problems below are the gap between the two tables in this subsection and what the container
does today. Stating them separately matters because the requirements outlive this design — if the
Session KV decomposition moves to Postgres (3.8A) most of section 3 collapses, but R1–R4 and R7
still have to be met by whatever replaces it.

#### Requirements

The last column is what would **prove the requirement met**, not what demonstrates today's gap —
most of E7's claims assert the broken state and are meant to start failing when S1 ships (6.0).

| ID  | Requirement                                                                                                                                                                                                 | Met by   | Verified by                                                        |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------ |
| R1  | **Every long-lived process in the container has exactly one owner** that starts, watches, restarts and stops it — at the default replica count as much as above it                                          | 3.1, 3.2 | 6 Unit; 6 E2E                                                      |
| R2  | **One process exiting does not restart the container**, unless it is `required` and past its cap — and no probe may take that decision either                                                               | 3.3, 3.4 | E4; 6 E2E                                                          |
| R3  | **An `optional` process being down does not remove the pod from the endpoint list.** A fail-open dependency may not become an outage                                                                        | 3.2, 3.4 | E4; 6 E2E                                                          |
| R4  | **Every replica can be Ready**, including followers that run nothing — otherwise the rollout cannot complete (L1)                                                                                           | 3.4      | 6 E2E                                                              |
| R5  | **The health signal goes stale when the supervisor stops supervising.** A wedged loop must not report healthy                                                                                               | 3.4      | E4c                                                                |
| R6  | **Release before acquire**: the outgoing leader has stopped its processes before any other pod can acquire the lease, bounded by a **renew deadline** on its own clock rather than by an API call returning | 3.5      | E6, E11; 6 Timing; 6 E2E (the Lease object, not the constant — L7) |
| R7  | **There is exactly one exit path**, and it drops the label, releases the lease, stops the table and writes a final `ready: false`                                                                           | 3.3      | E4; 6 Unit                                                         |
| R8  | **A lease lost and regained restarts the table.** No state a demotion can enter may be terminal                                                                                                             | 3.3      | E9; 6 Unit                                                         |
| R9  | **The supervisor behaves as PID 1**: forwards signals, reaps orphans it never started, and leaves nothing of a supervised process behind when it stops one                                                  | 3.7, 3.3 | E1b, E8, E13; 6 Unit                                               |
| R10 | **Per-iteration writes are pod-local.** Nothing the supervisor writes every poll may be a fixed path on the shared volume (L5)                                                                              | 3.4      | 6 Operator; 6 E2E                                                  |
| R11 | **Timing safety is asserted mechanically, at startup of `elected` mode** — never in prose, never only in review, and never in `solo`, which has no lease to be unsafe about                                 | 3.5, 6   | E6; 6 Timing                                                       |
| R12 | **S1 is behaviour-preserving at both replica counts.** The single-replica path gains a parent process and nothing else                                                                                      | 5        | golden files; 6 E2E                                                |

#### Hard limits

Things this design must work around rather than change. Each one is measured rather than assumed —
the experiment or claim that pins it is in the last column.

| ID  | Limit                                                                                                                                      | Consequence                                                                                                                                                                   | Pinned by                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| L1  | `maxUnavailable` rounds to **0 at one, two and three replicas** — one constant drives both it and `maxSurge`, in opposite directions       | A leader-only readiness probe stalls every rollout below four replicas. R4 exists because of this                                                                             | E3, E7 `C5`                   |
| L2  | At one replica the strategy is **`Recreate`**, not RollingUpdate                                                                           | A probe that never passes is an outage, not a stalled rollout. There is no old pod serving                                                                                    | E7 `C4`                       |
| L3  | `terminationGracePeriodSeconds` defaults to **30 s**, and shutdown is the **sum** over the process table                                   | Two processes plus the lease release is ~22 s. A third overruns and loses the release entirely                                                                                | E5, E7 `C6`                   |
| L4  | A probe's `httpGet` connects to the **pod IP** — `HTTPGetAction.Host` defaults to it                                                       | The status signal cannot be an HTTP endpoint bound to loopback. This alone decides 3.4                                                                                        | E2                            |
| L5  | The data PVC is **ReadWriteMany above one replica** — one volume mounted by every replica                                                  | A fixed path under `$PLATFORM_AGENT_HOME` is a shared name. R10 exists because of this                                                                                        | E7 `C11`                      |
| L6  | **`waitpid` can have only one caller.** A second one consumes the status, and `Popen.poll()` then reports exit **0** rather than "unknown" | The reaper must dispatch by PID into the table; no `poll()` anywhere else (3.7)                                                                                               | E1                            |
| L7  | The **Lease object outlives the rollout** — no owner reference, and nothing outside the script writes it                                   | Constants baked into the image do not reach it. R6 needs a migration step, not just a constant                                                                                | E10                           |
| L8  | **A Lease does not fence**, and the reference implementation says so itself (3.0)                                                          | Bounded dual-leadership is unavoidable. P6 and 3.6 bound it rather than closing it                                                                                            | —                             |
| L9  | `docker-entrypoint.sh` **must run before anything is supervised**, because it builds the tree the processes read                           | The supervisor has to be the entrypoint's `exec` target — the same slot an init system would want (3.8B)                                                                      | —                             |
| L10 | `leader_elect.py` is **`//go:embed`ed into the operator** and mounted as a ConfigMap key                                                   | Its constants version with the operator and cannot be tuned per-install (4)                                                                                                   | —                             |
| L11 | The gateway has **no cheap health route** — the nearest is `POST /v1/responses`, a model call                                              | The probe can prove `running` and `listening`, never `serving`. Q2 owns this                                                                                                  | —                             |
| L12 | The container is **CPU-limited to 3 and runs under gVisor**, where the repository records fan-out degrading a task from 17–23 s to 57–63 s | A `time.sleep` loop is not guaranteed to wake on schedule, and a process spawn is expensive. 3.5's timing may not assume either, and 3.4's probe may not spawn an interpreter | `manifest_helpers.go:304-316` |

#### Budgets, and how much is left

The three numeric budgets, at the parameters 3.5 and 3.7 propose:

| Budget                       | Limit                           | Spent at two processes | Headroom | What a third process does          |
| ---------------------------- | ------------------------------- | ---------------------- | -------- | ---------------------------------- |
| Shutdown vs. pod grace (3.7) | 60 s (raised from 30)           | ~22 s                  | ~38 s    | ~32 s — still fits                 |
| Lease inequality (3.5)       | 35 s lease                      | 29 s                   | **6 s**  | 39 s — **−4 s, refuses to start**  |
| Restart cap (3.3)            | 5 restarts / 600 s, per process | —                      | —        | unaffected; the cap is per-process |

The lease row is the binding one and 3.5 derives it. Its spend is `renew_deadline (8) + Σ grace
(2 × 10)`, **not** a sleep interval plus a grace: an earlier revision used the sleep, which is not
a bound on anything, and 3.5 says at length why that mattered.

**The 7 s is not headroom for a third process, and must not be described as one.** An earlier
revision justified widening the margin on the grounds that "it has to absorb a third process
later" while simultaneously proving a third process cannot fit — the third column of that row is
the proof. The margin absorbs _variance_ in the two terms it is made of; the table's size is held
by 3.2's boundary, and the inequality failing at three is a consequence of that decision rather
than the mechanism enforcing it. Conflating the two is how the design came to boast about shipping
a budget nearly consumed, which is a fragile property to design for and not a virtue.

The shutdown row is comfortable **only because 3.7 raises the grace period**. Left at the
Kubernetes default of 30 s (L3) the same ~22 s leaves ~8 s, and overrunning it is not a slow
shutdown but a lost one — the kubelet `SIGKILL`s the supervisor mid-cleanup, which drops the lease
release and the label with it.

Two more windows, which are tolerances rather than budgets:

| Window                                | Value | Must exceed                                                                                       |
| ------------------------------------- | ----- | ------------------------------------------------------------------------------------------------- |
| Readiness `failureThreshold × period` | 60 s  | the longest legitimate start of a **required** process                                            |
| Status staleness (3.4)                | 30 s  | the slowest legitimate poll iteration, which 3.5 bounds at ~9 s once the API calls have a timeout |

#### Assumptions that would change the answer

Three of the limits above are conditional. They are listed together because they share a property:
**if any one of them changes, the right design changes, not just a parameter.**

| Assumption                                                  | If it stops holding                                                                        | Tracked as       |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ | ---------------- |
| The Session KV store stays a SQLite file on a shared volume | The single-writer requirement goes, and with it P1/P2's urgency, the table, and all of 3.5 | Q5 — **settled** |
| The Python client ships no Lease resource lock              | The hand-rolled election should be replaced rather than fixed                              | Q6               |
| The gateway exposes no cheap health route                   | `ready` could mean _serving_ instead of _listening_, and L11 lifts                         | Q2               |

The first row is settled rather than open.
`session-kv-decomposition.md`
§8 decided it — the store stays a file — and named the only thing that would reopen it: **an
in-cluster Postgres that ships unconditionally, replicated, and authenticated**, all three. It
stays in this table because it is still the assumption this design rests on, and because it is
worth being able to see what a change to it would cost; 3.8A's Postgres scenario is that costing,
kept for the same reason.

Measured against R1–R12, today's container fails as follows.

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
([`manifest_helpers.go:273-278`][manifest_helpers-go-273-278])
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
[`manifest_helpers.go:290-297`][manifest_helpers-go-290-297]:

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
  7 s   maximum SLEEP between polls (5 + U(0,2))   <- a floor, not a bound: see below
+ 10 s  process termination grace before SIGKILL
= 17 s  before the process is guaranteed gone

  15 s  after which any other replica may take the lease
```

**The 7 s is the sleep, not the iteration.** Between two sleeps the loop performs a lease read and,
on the leader, a lease write ([`leader_elect.py:78`][leader_elect-py-78],
[`leader_elect.py:84`][leader_elect-py-84]), neither carrying a request timeout, on a loop sharing a
CPU-limited gVisor sandbox (L12). So 17 s is the best case for this arithmetic and there is no
worst case at all — a hung API call stops the leader from noticing indefinitely, and nothing else
will. That is a second defect inside P5 rather than a footnote to it, and it is why 3.5's answer
is a renew deadline and a request timeout rather than only a larger constant.

Even at its floor, the lease expires two seconds _before_ the outgoing leader is required to have
stopped anything.
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

**Half of that is fixable and 3.5 fixes it.** A request timeout turns a hang into an exception, and
a renew deadline measured on the local clock stops the leader whether or not any call ever returns
— so "up to 7 s, if it raises" becomes "within the renew deadline, unconditionally." What is left
after that is the part no lease can close: the processes themselves may not stop within their
grace, and `SIGKILL` is asynchronous. The residue is small and real, and the rest of this section
is about it.

This is a limitation to design around, not a bug to fix. Closing it needs a fencing token — a
monotonically increasing number issued with the lease and checked on every write — which needs a
second store to hold the token, which is the dependency both this design and
`session-kv-decomposition.md`
§8 decline. 3.6 states the consequences instead: anything a process owns exclusively must tolerate
finding it still held, and any work that crosses the window must be idempotent.

---

## 3. Design

### 3.0 Patterns applied, and where they come from

Almost nothing below is invented here. Each mechanism is a named pattern with a canonical
statement and at least one well-tested implementation, and the sections that follow are mostly a
matter of choosing which ones apply and where this container has to diverge from them. Naming them
is not decoration: **a divergence you can name is a decision, and a divergence you cannot is a
bug.** Both defects the review of this design found (E9, E10 in 6.0) were places where a pattern
was applied incompletely, and in both cases the reference implementation prevents them
structurally.

Section 0's table lists the files in _this_ repository. The prior art cited here is external and
is named rather than linked, except where a version-pinned line is worth reading directly.

#### Process supervision

| Pattern                          | Applied at                                           | Canonical statement                                                                                                      |
| -------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **Supervision tree**             | 3.2's process table; the supervisor as PID 1         | Erlang/OTP `supervisor`. A process whose only job is starting, watching and restarting others, and doing nothing else    |
| **One-for-one restart**          | 3.3, per process rather than per container           | OTP's `one_for_one`: restart only the child that died. Today's behaviour (P3) is `one_for_all` escalated to the pod      |
| **Maximum restart intensity**    | 3.3's cap — 5 restarts in 10 minutes                 | OTP's `intensity`/`period`: past the cap the supervisor gives up and escalates to _its_ parent. Here that is the kubelet |
| **Bulkhead**                     | 3.3's **per-process** restart budget                 | Nygard, _Release It!_ — "the gateway flapping must not consume the KV server's budget" is precisely a bulkhead           |
| **Crash-only software**          | 3.3's required-process escalation; Q3                | Candea & Fox, HotOS 2003. Restart is the recovery path, so the supervisor exiting into `CrashLoopBackOff` composes       |
| **Ordered start, LIFO teardown** | 3.2's numbered start order, reversed on the way down | systemd `After=`/`Before=`; Kubernetes native sidecars. Dependencies start first and stop last                           |
| **init process (PID 1)**         | 3.7 — signal forwarding and zombie reaping           | `tini`, `dumb-init`. 3.8B rejects them as _supervisors_ and 8's Q1 leaves them open as _PID 1_                           |

#### Leadership and time

| Pattern                                            | Applied at                                   | Canonical statement                                                                                                                                            |
| -------------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Lease (a lock with an expiry)**                  | the whole elected mode; P5, 3.5              | Gray & Cheriton, 1989; Chubby, OSDI 2006. The holder may act only until the lease expires, and must stop before it does                                        |
| **Active/passive failover**                        | the Service's leader-label selector          | Already named in the script's own header comment ([`leader_elect.py:12-16`][leader_elect-py-12-16]) — a blackhole, not request-continuous HA                   |
| **Backoff with jitter**                            | the retry period, `2 + U(0,1)` (3.5)         | AWS, "Exponential Backoff And Jitter" (Brooker, 2015); client-go uses `JitterFactor = 1.2`. Jitter is what stops N replicas polling in lockstep                |
| **Renew deadline** (self-fencing on a local clock) | 3.5's `renew_deadline_seconds`               | client-go's `RenewDeadline`: the holder gives up leading when _it_ has not renewed in time, without waiting for a call to return. The one term 3.5 was missing |
| **Bounded remote call**                            | 3.5's `_request_timeout` on every lease call | Nygard again. An untimed call is an unbounded one, and the renew deadline is only enforceable if the loop reaches it                                           |
| **Validate the timing inequality up front**        | 3.5's inequality, asserted at startup (6)    | client-go's `NewLeaderElector` refuses to construct on `LeaseDuration <= RenewDeadline`. A misconfiguration should fail at boot, not at the failover           |
| **Fencing token** — **declined**                   | P6, 3.6, 7                                   | Kleppmann, _DDIA_ ch. 8. A lease alone cannot stop a partitioned holder from writing; only a token checked at the resource can                                 |
| **Idempotency instead of mutual exclusion**        | 3.6's two consequences                       | The standard answer once fencing is declined: at-least-once delivery plus dedup at the server, not locks at the caller                                         |

Two divergences worth stating rather than leaving to be discovered:

- **The restart backoff is not jittered**, only the retry period is (3.3 doubles 1 s → 30 s flat).
  With one supervisor per pod and two entries in the table there is no herd to disperse, so this
  is a deliberate omission rather than an oversight — but it stops being safe if a future entry's
  restart is itself a call to a shared dependency, which is exactly what a third process is likely
  to be.
- **The lease inequality here is client-go's, plus one term.** An earlier revision said it was a
  different inequality altogether, and that was the mistake: upstream relates `LeaseDuration`,
  `RenewDeadline` and `RetryPeriod`, and 3.5 used to relate the lease duration to a **sleep**
  interval — which is not any of the three and is not a bound on anything. 3.5 now adopts
  `RenewDeadline` verbatim and adds the term upstream genuinely does not have: **the sum of the
  process table's shutdown graces**, a property of what the leader is _running_ rather than of the
  election. Upstream cannot have it, because upstream does not know what the leader does. That
  remains the reason this design has arithmetic of its own, and the reason it tightens as the
  table grows.

#### Health and degradation

| Pattern                                     | Applied at                                                        | Canonical statement                                                                                                                           |
| ------------------------------------------- | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| **Shallow health check**                    | 3.4's probe; the `listening` TCP connect                          | Amazon Builders' Library, "Implementing health checks". A **deep** check that fails on a dependency correlates failure across the whole fleet |
| **Dependency health never fails readiness** | 3.4's `degraded`, for both optional processes and remote services | The same source's warning, applied twice: a remote outage must not become this pod's outage                                                   |
| **Fail-open / graceful degradation**        | 3.2's `optional` criticality                                      | The KV server's absence costs attribution, not availability, so it must not be able to empty the endpoint list                                |
| **Heartbeat with a staleness threshold**    | 3.4's `updated_at`, checked against a 30 s window                 | A watchdog: the liveness signal must be _produced by_ the loop being monitored, never served beside it                                        |
| **Atomic replace (write-temp-then-rename)** | 3.4's status write                                                | POSIX `rename(2)`. A reader sees the old document or the new one, never a half-written one                                                    |
| **Per-instance state, not shared state**    | 3.4's pod-local `emptyDir`                                        | The review's finding. On the shared PVC the two rows above both break at once — N writers defeat the atomicity _and_ the staleness check      |

#### Placement

| Pattern                    | Applied at                  | Canonical statement                                                                                                                                                                                                                                  |
| -------------------------- | --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Sidecar** — **rejected** | 3.8A weighs and declines it | Burns & Oppenheimer, "Design patterns for container-based distributed systems", HotCloud 2016. Rejected for one specific reason: a container boundary can express ordered _startup_, but not the lease-driven stop-then-start **handover** 3.2 needs |

#### The implementation worth reading before writing this

`k8s.io/client-go`'s `tools/leaderelection` solves the same problem, against the same API, and is
already a dependency of this repository — [`k8s-operator/go.mod:11`][go-mod-11] pins `v0.31.0`, and the
operator's own manager elects through it ([`cmd/main.go:197-198`][main-go-197-198]). Only the **agent pod** hand-rolls
the loop. Three things it does are worth copying, and two of them are things this design got wrong
first:

| client-go does                                                                                                                                                      | This design's bug that it would have prevented                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Constructs a **fresh** `LeaderElectionRecord` carrying `LeaseDurationSeconds` on every acquire and renew ([leaderelection.go:408-415][client-go-tryacquireorrenew]) | **E10.** `leader_elect.py` mutates the body it read back, so the duration is only ever written on create (3.5)                                         |
| Makes `OnStoppedLeading` a **mandatory** callback — `NewLeaderElector` refuses to construct without one ([leaderelection.go:95-96][client-go-onstoppedleading])     | **E9.** Losing the lease and reacquiring it needs a stop path _and_ a start path; the prototype had a stop that could not be undone                    |
| Validates the timing relationship in the constructor ([leaderelection.go:76-82][client-go-validate])                                                                | Nothing — this is the one 3.5 arrived at independently, and 6 asserts it the same way                                                                  |
| Stops leading on its own **`RenewDeadline`**, measured locally, rather than on a call coming back ([leaderelection.go:76-82][client-go-validate])                   | **The unbounded poll term.** 3.5's first term used to be a `time.sleep` interval, which bounds nothing; P5 and P6 both understated the gap as a result |

It states the fencing limitation in its own package documentation, which is worth quoting because
P6 is often read as a flaw in this design rather than a property of the mechanism
([leaderelection.go:19-20][client-go-fencing]):

> This implementation does not guarantee that only one client is acting as a leader (a.k.a.
> fencing).

**Why not simply use it?** The Python client ships `kubernetes.leaderelection` with the same
shape — its `Config.__init__` performs the identical constructor-time validation — and the agent
image already installs that package, unpinned
([`deploy/docker/Dockerfile:113`][Dockerfile-113]). The blocker is concrete rather than a matter of
taste: at the version that resolves today, `36.0.3`, the only resource lock it ships is
`resourcelock/configmaplock.py`. There is **no Lease lock**, and this design's lock is a
`coordination.k8s.io` Lease that the operator's RBAC, the pod label and the Service selector are
all built around. Adopting the library therefore means either changing the lock object or
contributing a Lease lock upstream. Check the current release before acting on this — an unpinned
dependency means the answer can change without anyone editing this document. 8's Q6 records the
choice rather than settling it here.

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

Three consequences worth naming. `solo` is chosen from the environment the operator renders, not
from an observed pod count, so a Deployment scaled directly with `kubectl scale` — outside the
CR — briefly runs several pods that all believe they are permanent leaders. The operator
reconciles the replica count back; it is a pre-existing hazard rather than one this design
introduces, but making the mode explicit is what makes it visible.

**`scaleToZero` reaches the same state by the other route**, and S1 widens it. A CR asking for
three replicas with `scaleToZero: true` renders `solo` after S1 where it renders no supervisor
today (1.1), so a manual scale-up there runs three permanent leaders rather than three
unsupervised gateways. The operator reconciles the count back to zero, because zero is the desired
state, so the window is short. **It is a bounded pre-existing hazard that S1 makes reachable by one
more path, not a new one**, and 4 says why the fix is not to move the gate.

**What does _not_ bound it is the access mode, and an earlier revision said twice that it did.**
The claim was that the volume is `ReadWriteOnce` at the effective count — true, and measured (E7's
`C10`) — and that RWO therefore stops N supervisors from becoming N writers of one file. It does
not. **`ReadWriteOnce` is a per-_node_ mode**: the volume may be mounted read-write by one node,
and Kubernetes explicitly allows several pods on that node to mount it read-write at once.
`ReadWriteOncePod` is the mode that means one pod, and nothing in this repository asks for it.
Nothing keeps the replicas apart either — `buildDeployment` takes `Affinity` only from
user-supplied `availability.affinity` ([`platformagent_manifests.go:1706`][platformagent_manifests-go-1706]), so there is no
default anti-affinity and the scheduler may co-schedule them.

So a `kubectl scale --replicas=3` that lands two pods on one node gives two `solo` supervisors
mounting the same `system-metadata` PVC, and after S4 two Session KV servers writing one SQLite
file. The hazard is still bounded — by the operator reconciling the count, not by the storage — and
the honest consequence is that **S4 needs a real single-writer guard rather than an assumed one**.
4 carries that as an explicit ask.

And a `solo` supervisor never labels the pod, so at one replica the Service selector must continue
not to require `kubeagents.io/is-leader` ([`platformagent_manifests.go:2849`][platformagent_manifests-go-2849] adds it only above
one replica) — S1 must not disturb that.

Making the script the exec target at every replica count is what collapses 1.1's table to one
row. It has two knock-ons, both of them comments in the operator that are written around the
single-replica case this removes:

- The entrypoint's shared-state auto-detection looks for a bare `gateway` argument
  ([`platformagent_manifests.go:65-70`][platformagent_manifests-go-65-70]), and the gateway container's argv only carries one at a
  single replica today. The operator already names the owner explicitly with
  `AGENT_SHARED_STATE_SETUP=owner`, so nothing changes in behaviour — but the comment gets simpler
  and should be updated rather than left describing a case that no longer exists.
- The `Args, never Command` comment ([`platformagent_manifests.go:2288-2297`][platformagent_manifests-go-2288-2297]) explains the
  exec-target choice partly in terms of the entrypoint "start[ing] the Session KV server on 8699
  that the event-watcher is pointed at". That clause survives S1 but not S4, where the entrypoint
  stops starting it.

### 3.2 The process table

**Terminology.** A **supervised process** is a long-lived process the supervisor starts, watches,
restarts and stops — as distinct from the supervisor itself, and from the short-lived commands
either of them may shell out to. The supervisor holds them in a table rather than in the single
`process` global of 1.3, and the rest of this design says "process" for a table entry wherever
that is unambiguous.

| Supervised process   | Start | Stop | Grace                 | Criticality                                               |
| -------------------- | ----- | ---- | --------------------- | --------------------------------------------------------- |
| Session KV server    | 1     | 2    | 10 s — **unmeasured** | **optional** — the gateway's plugins fail open without it |
| `hermes gateway run` | 2     | 1    | 10 s                  | **required** — the container exists to run it             |

**Grace is a property of the entry, not a global constant**, even though both rows carry the same
number today. 3.5's shutdown term is the sum of this column, so it is the column an implementer
tunes when the inequality gets tight — and 10 s for the KV server is inherited from today's single
`process.wait(timeout=10)` rather than measured. Measuring it at S4, when the process actually
moves here, is worth roughly 5 s of lease duration and therefore 5 s of failover blackhole; 3.5
prices that rather than assuming it.

**Criticality is a property of the table, not a footnote**, and 3.3 and 3.4 both branch on it. An
optional process being down costs a feature; a required one being down means the container is not
doing its job. Collapsing the two — treating any stopped process as equivalent — is how a
fail-open dependency turns into an outage, which is a mistake this design made in an earlier
draft and 3.4 now avoids explicitly.

#### What is deliberately not in the table

The table holds **long-lived processes inside this container**. Three things that arrived with the
Hindsight memory provider look adjacent and are each excluded for a different reason — worth
stating, because "why isn't memory in here?" is the obvious question a reader now has:

| Thing                       | Why it is not a table entry                                                                                                                                                                                                                                                                                                  |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Hindsight itself**        | Not in this container, or any container of this pod. It is a separate in-cluster Deployment reached over a Service — the operator hands the gateway `http://hindsight-api.<ns>.svc.cluster.local:8888` ([`platformagent_manifests.go:1627`][platformagent_manifests-go-1627]). A remote dependency, not a supervised process |
| **The memory migration**    | One-shot. It runs once per boot and exits, so there is nothing to keep running. It is in 1.2 rather than here, because what matters about it is that the supervisor _inherits_ it (3.7)                                                                                                                                      |
| **`memory_ttl_curator.py`** | Not scheduled by anything — its own header says so, and it is run by hand through `kubectl exec`. Periodic work belongs in Hermes cron or an operator action, not in a supervisor whose job is keeping services up                                                                                                           |

The last row is a boundary worth defending rather than an accident. A supervisor that also runs
periodic jobs becomes a cron daemon with a lease, and every job added to it tightens the shutdown
budget of 3.5 for no availability benefit. **The table should only ever grow for something that
must be running for the container to do its job.**

Both replica counts collapse to one tree, differing only in the supervisor's mode:

```text
  solo  (replicas: 1)                     elected  (replicas: > 1, this pod holds the lease)
  ─────────────────────────               ────────────────────────────────────────────────

  PID 1  docker-entrypoint.sh             PID 1  docker-entrypoint.sh
    │                                       │
    └─ exec ▸  leader_elect.py              └─ exec ▸  leader_elect.py
                 │  mode = solo                          │  mode = elected
                 │  status + ready (pod-local)           │  status + ready (pod-local)
                 │                                       │  holds <agent>-leader
                 ├─[1] session_kv_server                 ├─[1] session_kv_server
                 └─[2] hermes gateway run                └─[2] hermes gateway run

                                          A follower runs the same supervisor with
                                          nothing under it, and is still Ready.
```

The numbering is the start order; stop runs in reverse. This is the **supervision tree** of 3.0,
with **LIFO teardown** — start dependencies first, stop them last, the same relationship systemd
expresses with `After=` and Kubernetes with native sidecars.

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

Both write to **inherited stdout/stderr** rather than to a file on the PVC. An earlier revision
justified that with "so their output reaches fluent-bit like everything else", which is the
opposite of how this pod collects logs and is worth correcting in full, because the correction
changes what the move costs rather than whether to make it.

There are two log paths in this pod and they do not meet:

| Path                    | Who is on it                             | How it leaves the pod                                                            |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------- |
| container stdout/stderr | `hermes gateway run`, the supervisor     | the kubelet's log file for the container; rotated by the runtime                 |
| `/opt/data/logs/*.log`  | the Session KV server, the memory import | the `fluent-bit` sidecar's only `tail` INPUT, re-emitted as JSON on _its_ stdout |

So fluent-bit reads **files and nothing else** — the gateway's own output has never passed
through it. Moving the KV server to stdout therefore does not deliver it to fluent-bit; it takes
it off fluent-bit's input and puts it where the gateway's output already goes. Three consequences,
stated rather than discovered later:

- **What is gained is real.** Nothing truncates or rotates `/opt/data/logs/*.log` — fluent-bit
  tails them and never deletes — so today the KV server grows a file on a shared PVC without
  bound. Container stdout is rotated by the runtime.
- **What is lost is the enrichment, not the logs.** The tail input carries a `gchat_event` parser
  (`User=(?<gchat_user>…), Session=(?<gchat_session>…)`) and stamps `log_source: agent-file`, and
  the KV server's lines are what that parser was written for. No consumer in this repository reads
  either field; if one is wanted it has to be re-created on the stdout path, which is a decision
  for whoever owns log collection and not for this design to make silently.
- **fluent-bit's tail input is left with one producer**, the memory import. Whether that sidecar
  still earns its place is a question S4 raises and does not answer here.

The cost of sharing one stream is that lines from the two processes interleave with nothing to
tell them apart. Inheriting the descriptors directly, rather than piping through the supervisor to
add a prefix, is the deliberate choice: a pump thread per process is a new way to block or lose
output, and the processes already prefix their own lines (`[LeaderElect]`, uvicorn's own format).
If that proves insufficient in practice, the fix belongs in the processes' log formats, not in the
supervisor.

### 3.3 Restart policy

This is OTP's supervisor policy with two classes of child (3.0): **one-for-one** restart, a
**maximum restart intensity** past which the supervisor gives up and escalates to its own parent,
and a **bulkhead** so that one child's failures cannot spend another's budget. What OTP escalates
to a parent supervisor, this escalates to the kubelet.

Per process, not per pod:

- On exit, restart with exponential backoff (1 s doubling to 30 s).
- Count restarts in a sliding window, **per process**. The gateway flapping must not consume the
  KV server's budget.
- Past the cap — **5 restarts in 10 minutes** — what happens depends on the criticality column of
  3.2, and this is the part an earlier draft got wrong:

  | Past the cap | Action                                                                                                                                                                                                            |
  | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | **required** | Give up. The supervisor exits, and the kubelet restarts the container.                                                                                                                                            |
  | **optional** | Give up on **that process only**. Leave it stopped and keep everything else running. `degraded` is already true — 3.4 keys it on "not running", so the cap changes how long it stays true, not whether it is set. |

  A uniform cap that always exits is P3 wearing a different hat: it means a KV server
  crash-looping on a corrupt database eventually takes a perfectly healthy gateway down with it,
  and each cycle re-runs the 1283-line entrypoint. That is the exact complaint P3 makes about
  today's behaviour, arriving five restarts later. An optional process that cannot start is a
  degradation and must be reported as one, not escalated into a container restart.

  **A cap is a rate, and rates have a floor nobody states.** A sliding window trims entries older
  than `RESTART_WINDOW`, so reaching a cap of `C` restarts needs `C + 1` failures inside the
  window — which means a process whose failures are spaced more than `RESTART_WINDOW / C` apart
  can never reach it and will retry **forever**. `gave_up` becomes unreachable and the escalation
  this bullet is about silently does not exist. (`degraded` still fires — 3.4 keys it on "not
  running" rather than on `gave_up`, for exactly this reason.)

  That is not hypothetical for the entry S4 adds, and the earlier draft's "5 in 5 minutes" does not
  survive contact with it. The KV server's startup lock retry is bounded at 60 s
  (`session-kv-decomposition.md`
  §4.2), so with the 1→2→4→8→16 s backoff its failures land 61, 62, 64, 68 and 76 s apart — every
  one of them wider than the 60 s floor a 300 s window gives. Six failures span 331 s and the
  window trims the first before the sixth arrives.

  §4.2 computes the budget as spent "in about 255 s", which is five failures rather than six, and
  is what the `>=` off-by-one below actually implemented. Correcting that off-by-one is therefore
  the thing that would have made the cap unreachable — the two defects were cancelling. **The
  window goes to 600 s**, giving a 120 s floor with room over the worst spacing, and 6 asserts the
  floor against the KV server's figure rather than leaving the coupling to be rediscovered. A
  genuinely fast crash-looper is unaffected: at 1→2→4→8→16 s it retires in about 45 s either way.

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
  `session-kv-decomposition.md`
  and must fit inside the cap.
- On lease loss, stop all processes in reverse start order before returning to the watch loop.
  Termination keeps today's 10 s grace and `SIGKILL` fallback per process — see 3.2 on why the
  total, not the per-process figure, is what 3.5 and 3.8 are sized against.
- **Stopping signals the process group, not the process** — and sweeps it afterwards. Every entry
  is started with `start_new_session=True`, so it leads its own group, and stopped with `killpg`
  rather than `Popen.terminate()`. Today's script signals the direct child only
  ([`leader_elect.py:146`][leader_elect-py-146]), which is adequate when the child is the only
  thing that matters and is not adequate here: 3.5's guarantee is that _nothing the outgoing leader
  ran_ is still touching the resource, and the gateway shells out constantly. A grandchild
  outlives `terminate()`, reparents to the supervisor, and is then reaped by 3.7 — reaped, note,
  not stopped.

  **`SIGTERM` to the group is not sufficient on its own**, which a first pass at this got wrong and
  E13 caught. If the grandchild ignores `SIGTERM` and the parent honours it, the parent exits
  immediately, `wait()` returns well inside the grace, and the `SIGKILL`-after-grace branch never
  runs — leaving the grandchild alive on the fast path, the one that happens every time. So
  `stop()` ends with an unconditional `killpg(pgid, SIGKILL)` sweep once the child is gone. The
  child is reaped by then, so its pid is in principle reusable as another group's id; pids are
  handed out sequentially, the window is microseconds, and it is the trade every process-group
  supervisor makes. Worth writing down rather than leaving as a silent assumption.

- **A stopped process returns to `pending`, so re-acquisition restarts the table.** Leaving it in
  a terminal state is a deadlock, and a quiet one: `tick` starts a process only from `pending` or
  from an elapsed `backoff`, so a demoted leader that reacquires the lease — an ordinary
  API-server blip — would resume as leader with an empty table, hold the lease and the
  `kubeagents.io/is-leader` label, and publish `ready: false` forever. Nothing recovers that under
  S2, where a failed readiness probe never restarts the container, and the Service selects on the
  label, so the agent would have zero endpoints until someone intervened. A prototype had exactly
  this bug (E9 in 6.0).

  Two details make the rule safe rather than merely unstuck:

  | Field      | On a demotion                                                                                                                                                                                |
  | ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `state`    | back to `pending` — **except `gave_up`, which is left alone.** A lease flap must not revive a process the cap has retired, and leaving the state as it is also keeps `degraded` reporting it |
  | `backoff`  | reset to 1 s. A demotion is not a failure, and the next promotion should not inherit a 30 s delay                                                                                            |
  | `restarts` | **kept.** The sliding window is what stops a crash-looping process from laundering its budget through a flapping lease; it ages out on its own                                               |

  Cleanup (the terminating path, not a demotion) is the one case that leaves processes stopped for
  good, because the supervisor exits immediately behind it.

Sketched, to fix the shape rather than the implementation — this is the replacement for the
`elif process.poll() is not None: sys.exit(...)` of 1.3:

```python
RESTART_CAP    = 5     # restarts permitted ...
RESTART_WINDOW = 600   # ... within this many seconds. See the rate floor above:
                       # failures spaced further apart than WINDOW/CAP never reach it.
BACKOFF_MAX    = 30

class Supervised:
    def __init__(self, name, argv, required, grace):
        self.name, self.argv, self.required = name, argv, required
        self.grace = grace               # per entry, not global -- 3.2 and 3.5
        self.proc = None
        self.state = "pending"           # pending|running|backoff|exited|gave_up|stopped
        self.backoff = 1
        self.retry_at = 0
        self.failures = deque()          # monotonic timestamps, trimmed to RESTART_WINDOW

    def start(self, now):
        try:
            # start_new_session: the child leads its own process group, so stop()
            # can signal the group. Without it a grandchild survives the handover
            # 3.5 guarantees -- see the killpg note in stop().
            self.proc = subprocess.Popen(self.argv, start_new_session=True)
            self.state = "running"
        except OSError as exc:           # missing binary, unwritable path, ...
            log(f"{self.name}: start failed: {exc}")
            self.proc = None
            self.penalise(now)           # a failed start is a restart, or it spins

    def on_exit(self, code, now):
        """The ONLY entry point for a child exiting. reap() in 3.7 calls this and
        nothing else does. Deliberately no self.proc.poll() anywhere here -- see
        3.7 for why a second reader of a child's status rewrites a crash into a
        clean exit."""
        log(f"{self.name}: exited {code}")
        self.exit, self.proc, self.state = code, None, "exited"
        self.penalise(now)

    def tick(self, now):
        """Once per supervisor iteration. False => a REQUIRED process is past its cap."""
        if self.state == "pending" or (self.state == "backoff" and now >= self.retry_at):
            self.start(now)
        return self.state != "gave_up" or not self.required

    def penalise(self, now):
        """Public, because 3.7's reaper is a legitimate caller. One underscore
        here and none there is how the two sketches used to disagree."""
        self.failures.append(now)
        while self.failures and now - self.failures[0] > RESTART_WINDOW:
            self.failures.popleft()
        if len(self.failures) > RESTART_CAP:   # STRICTLY greater: the Nth failure is
            self.state = "gave_up"             # the (N-1)th restart, so `>=` retires
            return                             # after CAP-1 restarts, not CAP
        self.state = "backoff"
        self.retry_at = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    def stop(self, final):
        """Signal the process GROUP, wait out this entry's grace, SIGKILL it.

        Runs on the poll loop's thread, never inside reap() and never
        concurrently with it -- 3.7 states that as the invariant, because the
        wait() below is a second reader of a child status and is only safe
        because of it.

        `final` separates the terminating path from a demotion: only one of the
        two may leave the entry unstartable.
        """
        if self.proc is not None:
            pgid = self.proc.pid          # start_new_session made it its own leader
            killpg(pgid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                log(f"{self.name}: grace expired, SIGKILL to the group")
                killpg(pgid, signal.SIGKILL)
                self.proc.wait()
            # UNCONDITIONAL, not just on the timeout path. A grandchild that
            # ignores SIGTERM outlives a parent that honours it, so wait() returns
            # well inside the grace and the branch above never runs -- which is
            # the shape E13 falsified. See the prose above on pid reuse.
            killpg(pgid, signal.SIGKILL)
            self.proc = None
        # The transitions below run even when there was nothing to stop. An entry
        # in `backoff` has no process and still has to be reset; an early return
        # on `self.proc is None` skipped that, so a demotion mid-backoff carried a
        # 30 s delay into the next promotion. Same class of bug as E9.
        if self.state == "gave_up":
            return                       # sticky; the entry stays non-running, so `degraded` holds
        self.state = "stopped" if final else "pending"
        if not final:
            self.backoff, self.retry_at = 1, 0   # a demotion is not a failure
```

The shape is what matters, not the details. `tick` returning `False` — only ever for a
**required** process — routes through `release_lease_and_exit`, not `sys.exit`. A `gave_up`
**optional** process leaves the supervisor running and keeps 3.4's `degraded` set. And
`start` failing is charged to the same counter as an exit, because otherwise a missing binary is
an infinite loop at full speed rather than a bounded one.

Three details are load-bearing rather than incidental, and each was wrong in an earlier draft:

- **`> RESTART_CAP`, not `>=`.** The deque counts _failures_; the Nth failure is the (N−1)th
  restart. `>=` retires a process after four restarts while the document says five, which is the
  kind of drift that makes a cap untestable against its own prose.
- **`penalise` is public**, because 3.7's reaper calls it. The two sketches previously spelled it
  differently, and only one of them can be right.
- **`stop()` transitions state even with no process to stop**, for the reason in the comment.

**The backoff is exponential but not jittered**, unlike the retry period of 3.5. 3.0 records why
that is a decision rather than an omission — there is one supervisor per pod and no herd to
disperse — and the condition under which it stops being true.

### 3.4 Health status and readiness

#### A status file, not an HTTP server

At the end of every poll iteration the supervisor writes **two** files under
`/var/run/supervisor/`, each atomically by temp-file-and-rename:

| File          | Read by                                | Content                            |
| ------------- | -------------------------------------- | ---------------------------------- |
| `status.json` | humans, `kubectl exec`, 6's e2e checks | the full document below            |
| `ready`       | the probes, and only them              | one line: `<epoch-seconds> <0\|1>` |

**Why two, when one would do.** The probe runs every 10 s for readiness and every 20 s for
liveness, in a container that is CPU-limited and sandboxed by gVisor, where the repository's own
resource comment records process spawn being expensive enough to justify a CPU bump (L12). Parsing
JSON from `sh` needs an interpreter; starting CPython nine times a minute to read two fields is a
cost the design was about to pay by accident. A one-line file makes both probes four lines of
POSIX `sh` with one `date` fork, and leaves `status.json` free to stay a readable diagnostic rather
than becoming a parsing contract. `ready` is renamed into place **after** `status.json`, so a
reader that catches the pair mid-update sees a stale `ready` rather than a fresh one describing an
older document.

The diagnostic document:

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
`httpGet`. **That combination cannot work**, and one fact settles it: a probe's `httpGet` connects
to the _pod IP_ — `HTTPGetAction.Host` "defaults to the pod IP" — so a server bound to `127.0.0.1`
is unreachable from the kubelet and the probe fails every time (E2). Binding `0.0.0.0` instead
fixes the probe by publishing an unauthenticated status endpoint on the pod IP, which is the thing
not to do.

That earlier draft also argued the point via the NetworkPolicy — "the ingress allowlist does not
cover 8700" — and the argument runs backwards. An allowlist that omits the port is what would keep
_peers_ out; it is the mitigation, not the exposure, and leaning on it would also mean relying on a
NetworkPolicy the cluster may not enforce (the Hindsight integration's README notes these apply
cleanly and do nothing outside Dataplane V2). E2 decides this on its own. The allowlist is worth
knowing for a different reason and E7's `C8` still pins it: it is why nobody should reach for a
status port later and expect a peer to read it.

A file avoids the dilemma and is smaller in every direction. There is no port to allocate, no
HTTP server to run inside what is otherwise a `time.sleep` loop, no thread sharing mutable state
with the poll loop — and it matches the repository, where **every existing probe is an `exec`**
and none is an `httpGet`.

#### The file must be pod-local, and `$PLATFORM_AGENT_HOME` is not

The obvious home for it is under the agent's own home directory, and that is the one place it must
not go. `PLATFORM_AGENT_HOME` is `homeDir` ([`platformagent_manifests.go:1385-1389`][platformagent_manifests-go-1385-1389]), which is
`defaultAgentHome = "/opt/data"` ([`platformagent_manifests.go:55`][platformagent_manifests-go-55]) — the mount point of the
`platform-agent-data-vol` PVC ([`platformagent_manifests.go:1810-1815`][platformagent_manifests-go-1810-1815]). Above one replica that
PVC is `ReadWriteMany` on `standard-rwx` ([`platformagent_manifests.go:100-117`][platformagent_manifests-go-100-117]): **one volume,
mounted by every replica of the Deployment.**

So a fixed path under it would mean, in elected mode — the only mode in which more than one
supervisor exists — that all N supervisors write the same file every poll and all N probes read
it. That inverts every property this section is built on:

| Property                        | What a shared file does to it                                                                                                                                                                                        |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The freshness check             | Cannot fire. A healthy follower keeps rewriting `updated_at` for a wedged leader — and the wedge is only reachable in elected mode, exactly where the file would be shared                                           |
| The final `ready: false`        | 3.3's write on the way out publishes `ready: false` to _every_ replica's probe, so one pod terminating in a rolling update empties the endpoint list — and restarts every container once S2b's liveness probe exists |
| `role`, `degraded`, `processes` | Describe whichever pod wrote last, so the diagnostic names the wrong pod                                                                                                                                             |
| The atomic write                | Temp-file-and-rename is atomic per _writer_. Two supervisors sharing one temp name interleave, and one renames the other's half-written file into place                                                              |

The freshness row is the one that matters most, because it is the entire reason the file exists
rather than an HTTP thread — and a shared file would defeat it precisely in the mode it was added
for.

None of this is a new lesson here. The entrypoint already tags its staging paths with `$HOSTNAME`
for the same reason, and says why at length
([`docker-entrypoint.sh:872-896`][docker-entrypoint-sh-872-896]): "at `availability.replicas > 1` the operator hands every
replica the SAME PVC (ReadWriteMany) … fixed siblings … are shared names on a shared volume."

**So the status file goes on a pod-local volume**, not on the PVC and not under
`$PLATFORM_AGENT_HOME`: an `emptyDir` with `medium: Memory` and a 1Mi size limit, mounted at
`/var/run/supervisor` in the `platform-agent` container only. That is the shape the pod already
uses twice for exactly this kind of runtime scratch —
`credential-proxy-runtime` and `event-watcher-kubeconfig`
([`platformagent_manifests.go:2192-2193`][platformagent_manifests-go-2192-2193]) — so it needs no new pattern, and `fsGroup` on the pod
([`platformagent_manifests.go:1730-1736`][platformagent_manifests-go-1730-1736]) makes it writable by UID 10000 without any further
grant. Tagging a path on the PVC with `$HOSTNAME` would also work, and is rejected: it keeps a
per-poll write on a network volume, and leaves one file per pod name to be cleaned up by nobody.

One consequence of `emptyDir` is worth pinning down, because it is a way to be wrongly Ready. An
`emptyDir` is per **pod**, so it survives a container restart — which means a restarting
supervisor can find its own pre-crash `ready` line saying `1`, still inside the staleness window if
the restart was quick. **The supervisor therefore writes both files with `ready` false as its first
action, before starting anything**, which is the exact mirror of 3.3's final write on the way out.
3.2's stdout argument does not cover either write; this is the one thing in the design that
touches a volume per iteration, and it is why it gets its own.

#### Freshness is the point of `updated_at`

Serving health from a thread beside the poll loop has a failure mode that is easy to miss and
fatal to the purpose: if the loop wedges — blocked on an API call, which 3.6 concedes can
happen — the thread keeps answering `200` from stale state. The probe would report healthy
precisely when the supervisor has stopped supervising.

A file makes the check trivial, so the probe does it: if the timestamp is older than the staleness
window the supervisor is not running its loop, whatever the rest of the document says.

**The window is 30 s, and 3.5 is what makes that number derivable.** An earlier draft called it
"three poll intervals" and then, correcting itself, "roughly seven sleep intervals, chosen to
exceed the slowest _legitimate_ iteration" — while conceding in the same sentence that the
iteration is dominated by API calls that "vary". A window sized against an unbounded quantity is
not sized at all, and worse, 3.5's inequality was simultaneously assuming that same quantity was
4 s. One of the two had to give.

3.5 gives, by bounding the iteration: every lease call is clamped to at most a 2 s
`_request_timeout`, so the slowest legitimate iteration is
`sleep (≤3) + read (≤2) + write (≤2) + two small file writes`, call it **7 s**. The window is then
~4× a bound rather than a guess against a variable, and the
cost of the generosity is still bounded on the other side: readiness needs six consecutive
failures (60 s) before the pod leaves the endpoint list, and S2b's liveness needs 120 s. Being
tighter buys nothing and risks a restart loop during an API-server slowdown.

This is the **heartbeat with a staleness threshold** of 3.0, and its defining property is the one
the HTTP thread lacked: the signal must be _produced by_ the loop being monitored. A watchdog fed
by anything other than the work it watches will keep reporting healthy through exactly the failure
it exists to catch.

```sh
#!/bin/sh
# /opt/hermes/bin/supervisor-ready -- readiness. exit 0 = Ready.
# Reads one pod-local line; never blocks on anything but the file. No interpreter:
# see L12 on what a CPython start costs at probe frequency under gVisor.
read -r ts ready < /var/run/supervisor/ready 2>/dev/null || exit 1
[ $(( $(date +%s) - ts )) -le 30 ] || exit 1    # the loop has wedged
[ "$ready" = 1 ]
```

The path is `/var/run/supervisor/ready` — pod-local, **not** the shared PVC; see above for what a
shared one breaks.

#### What `ready` means, and what it deliberately does not

| Pod state                                            | `ready` | Effect                                    |
| ---------------------------------------------------- | ------- | ----------------------------------------- |
| follower (elected mode, not the holder)              | `true`  | stays Ready; runs nothing                 |
| leader or solo, everything running                   | `true`  | serving                                   |
| leader or solo, an **optional** process not running  | `true`  | Ready but `degraded: true`                |
| leader or solo, the **required** process not running | `false` | NotReady, leaves the endpoint list        |
| status file older than the 30 s staleness window     | `false` | NotReady — the supervisor's loop is stuck |

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

**`degraded` means "an optional process is not running", and it has exactly that one definition.**
Worth stating flatly, because an earlier revision carried three readings of it — this table's
"down", 3.3's cap row saying it is what a `gave_up` optional process surfaces as, and a prototype
that computed it as `any(state == "gave_up")`. They are not the same signal, and the narrowest of
them is nearly useless:

```
degraded = any(p.state != "running" for p in table if not p.required)
```

Keyed on `gave_up` alone it would miss every optional process that is merely restarting, and — by
3.3's own rate floor — it would never fire at all for a process failing less often than
`RESTART_WINDOW / RESTART_CAP`, which is the common case rather than an edge one. An alert on a
signal that cannot fire is worse than no alert. Keyed on "not running" it is true while the process
is in `backoff`, true once it has `gave_up`, and clears by itself when a restart succeeds — which
is what 6's end-to-end check asserts when it kills the KV server once and expects `degraded` to go
true and then clear. Being briefly true during an ordinary one-second restart is the correct cost:
this is a status field, not a probe, and it moves no traffic.

#### The residual gap: running is not serving

`state: "running"` means the process has a PID. It does not mean it is serving. To narrow that,
the supervisor also does a TCP connect to the gateway's `127.0.0.1:8642` each poll and records
`listening` — cheap, no new endpoint required, and strictly stronger than a PID check, because it
proves the listener is bound.

**With an explicit timeout, and the timeout is not optional.** `socket.create_connection(...,
timeout=1)`, and a timeout counts as `listening: false` rather than raising. A bare `connect()` to
a loopback port whose listener has a full accept queue blocks, and blocking here would wedge the
very loop whose freshness _is_ the health signal — the design would have introduced the failure
mode the staleness check exists to detect, inside the check itself. Every remote call the
supervisor makes is bounded: this one, and 3.5's two lease calls.

It is still not proof of service: a gateway that accepts connections and then wedges reports
`listening: true`. Closing that needs a cheap health route on the gateway itself — the closest
thing today is `POST /v1/responses` ([`hack/ci-deploy.sh:144`][ci-deploy-sh-144]), which is a model call and far too
expensive to run every 10 s. **This is a known and accepted limitation of S2**, not something the
probe silently covers; it is listed as an open question in 8.

**And the gap widens as the gateway grows dependencies it reaches over the network.** The memory
provider is the current example: recall and retention go to `hindsight-api` in another pod
([`platformagent_manifests.go:1627`][platformagent_manifests-go-1627]), so a gateway whose
Hindsight is unreachable is degraded in a way neither a PID nor a TCP accept can detect.

The tempting response — have the probe check the gateway's dependencies too — is to turn a
**shallow** health check into a **deep** one, and it is the same mistake 3.4 already rejected once,
one level out. A pod marked NotReady because a _remote_ service is down
removes the only endpoint there is and converts someone else's outage into this agent's outage,
while the agent could still answer everything that does not need memory. That is the correlated
failure the Builders' Library warns deep checks cause, arriving through a probe rather than
through a dependency: every replica fails the same check at the same moment. **Dependency health
belongs in `degraded`, never in `ready`** — the same rule the optional-process row follows, for
the same reason. What reports it is a matter for whoever owns the dependency; this design only
fixes where the answer may and may not be written.

```yaml
readinessProbe:
  exec: { command: ["/opt/hermes/bin/supervisor-ready"] }
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 6
```

The `10 s` initial delay is a courtesy, not a guard: a missing file fails closed, and the
supervisor writes `ready 0` before it starts anything (above). **But "immediately" is measured from
the supervisor's first instruction, not from container start**, and those are a long way apart —
which is what the startup probe below exists for.

#### The entrypoint runs before any of this, and it can take minutes

L9 is not a footnote here. `docker-entrypoint.sh` must finish before anything is supervised,
because it builds the tree the processes read, and the supervisor is its `exec "$@"` target
([`docker-entrypoint.sh:1293`][docker-entrypoint-sh-1293]). Until that `exec`, **no supervisor
exists and nothing has written `/var/run/supervisor/ready`** — so both probes fail, either on the
missing file or, worse, on a stale timestamp the previous container left in the pod-scoped
`emptyDir` that 3.4 relies on surviving a container restart.

How long is that? The bootstrap lock alone waits up to five minutes
([`docker-entrypoint.sh:304`][docker-entrypoint-sh-304], `flock -w 300`) when a peer container
takes it first, and the PVC seed, script sync and profile scaffold all follow it. Against that, a
liveness probe of `60 + 6 × 20` kills the container about 180 s after start. A container that loses
the lock race is killed before it ever reaches the supervisor, restarts, loses it again — a crash
loop **introduced by the probe**, on a container that has no probe today and therefore boots fine.

So readiness and liveness are both gated behind a **startup probe**, which is the primitive for
exactly this: while it is failing, neither of the other two runs, and only when it passes do their
clocks start.

```yaml
startupProbe:
  exec: { command: ["/opt/hermes/bin/supervisor-alive"] }
  periodSeconds: 10
  failureThreshold: 60 # 600 s — must exceed the entrypoint's own worst case
```

`failureThreshold × periodSeconds` = 600 s is sized against the 300 s lock wait plus the setup
behind it, with room over. Getting this wrong in the generous direction costs a slow crash-loop
detection on first boot; getting it wrong in the tight direction is an unbootable container, so it
is sized for the second. It runs `supervisor-alive` rather than `supervisor-ready` for the same
reason liveness does: what it is waiting for is the supervisor's loop to exist, not for a required
process to be up.

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
it into a _recovery_ needs a liveness probe — but **not the readiness script with a longer
threshold**, which is what an earlier draft proposed and which quietly undoes 3.3.

`supervisor-ready` fails for two unrelated reasons: the loop has wedged, and the required process
is not running. Only the first is a liveness condition. Run the same script as liveness and a
required process that is legitimately slow to come back — the case the 60 s readiness budget exists
for — becomes a container restart 120 s later, which is precisely the per-container escalation R2
forbids and 3.3 spent a section taking back from the kubelet. It also double-counts: 3.3 already
escalates a required process past its cap by exiting, so the liveness probe would be a second,
blunter path to the same outcome with none of the backoff or the cap in front of it.

So liveness reads the timestamp and ignores the flag — a second four-line script over the same
one-line file:

```sh
#!/bin/sh
# /opt/hermes/bin/supervisor-alive -- liveness. Staleness ONLY.
# A required process being down is readiness's business and 3.3's; restarting the
# container for it would undo the restart policy. The only thing this asks is
# whether the supervisor is still running its loop.
read -r ts ready < /var/run/supervisor/ready 2>/dev/null || exit 1
[ $(( $(date +%s) - ts )) -le 30 ]
```

```yaml
livenessProbe:
  exec: { command: ["/opt/hermes/bin/supervisor-alive"] }
  initialDelaySeconds: 60
  periodSeconds: 20
  failureThreshold: 6 # 120 s — must be comfortably longer than readiness
```

The `ready` variable is read and unused on purpose; `read` needs it to consume the second field,
and its absence from the test is the whole point of the script.

Sequencing matters more than the numbers. A liveness probe that fires wrongly is a restart loop,
and this container has never carried a probe of any kind, so **liveness ships after readiness has
soaked**, not with it. 5 splits them across S2 and S2b for that reason.

### 3.5 Lease timing

A lease is a lock with an expiry (3.0), and its safety condition is that the holder stops before
the expiry rather than after it. Every lease implementation states that condition as an inequality
over its own constants; **this one has a term none of them have**, because the outgoing holder is
not just releasing a lock, it is shutting down a process table.

State the inequality and pick parameters that satisfy it. **The shutdown term is the sum over the
process table, not one process's grace** — 3.2 stops them sequentially, so each one's grace is
paid in turn:

```
lease_duration_seconds  >  renew_deadline_seconds + Σ(per-process shutdown grace)
```

Getting this wrong is easy and this design has now got it wrong twice, in different places. The
first time was the second term: an earlier draft sized the proposal against a single 10 s grace and
claimed a 13 s margin, when the two-process table of 3.2 makes the term 20 s. **The term grows
with every process added to the table**, so it silently tightens as the design succeeds.

**The second was the first term, and it was worse, because it was not a term at all.** Until this
revision the inequality read `max_poll_interval + Σ(grace)`, with `max_poll_interval` meaning the
`time.sleep(3 + U(0,1))` at the bottom of the loop. A sleep is not a bound on an iteration. The
iteration is the sleep _plus_ a lease read and a lease write, neither of which carried a timeout,
on a loop sharing a CPU-limited gVisor sandbox (L12). 3.4 said so itself, in the course of sizing
the staleness window: "it is the API calls that vary, not the sleep." So one section of this
document sized a 30 s window on the premise that an iteration is long and unpredictable while
another asserted, in shipped code, that the same iteration is 4 s. Both could not be true, and the
one that mattered for safety was the optimistic one.

**The fix is the term client-go already has and 3.0 already cited without adopting: a renew
deadline.** The holder tracks its own last _successful_ renew on the monotonic clock, and stops
leading when that is `renew_deadline_seconds` old — whether or not any API call has come back. It
is self-fencing rather than server-told, so no remote latency can extend it.

**A deadline checked once per iteration is not a bound, and saying "renew deadline" is not enough
to get one.** This is the same mistake as the sleep, one level in: if the loop tests
`now - last_renew > renew_deadline` only at the top of each pass, the last test before the deadline
can land at `deadline − ε` and the next one a whole iteration later, so the true bound is
`renew_deadline + one iteration` — by 3.4's own numbers another 7–9 s. client-go does not have this
problem because it runs the renew under a **context whose deadline _is_ `RenewDeadline`**, which
aborts the in-flight call; adopting the name and not the mechanism buys nothing.

So the deadline is enforced **at every point the loop can block**, which means three rules rather
than one:

```
deadline = last_successful_renew + renew_deadline_seconds

every lease call times out at   min(lease_call_timeout, deadline - now)   # clamp the calls
the loop sleeps at most         min(retry_period,       deadline - now)   # clamp the WAIT too
the deadline is re-tested after every call and every sleep, not once per pass

renew_deadline_seconds  >  max_retry_period + 2 x lease_call_timeout      # one full retry fits
```

**Both clamps, or the term is still not the deadline.** Clamping only the calls leaves the sleep
free to run past it, so the first observation lands up to a full `retry_period` late and the honest
first term would be `renew_deadline + max_retry_period` — 12 s, not 9, and a 3 s margin instead of 6. Clamping the wait as well costs nothing (the loop simply wakes earlier when a deadline is near)
and is what makes the first term genuinely `renew_deadline`. This is the whole content of
client-go's "run the renew under a context whose deadline _is_ `RenewDeadline`": every blocking
operation inherits it. Adopting the name and leaving one blocking operation unbounded gets none of
the guarantee.

**A definitive loss clears the deadline; only a successful renew sets it.** The deadline exists to
answer "nobody has told me anything" — it must never answer "somebody told me no". If a read
returns and says another pod holds the lease, that is an answer, and `last_renew` has to be
invalidated on that path as well as on the stop. Leave it set and the next timed-out call
re-promotes a supervisor that was explicitly denied: read says not-held, the table stops, then one
call times out inside the deadline window and the supervisor restarts the whole table while the
real holder is running it. 3.5's own migration step reaches that state directly —
`kubectl delete lease` lets a peer create the object and become holder while this pod's last
successful renew is still fresh. Two supervisors running the table at once is exactly what R6
exists to prevent, so the invalidation is part of the mechanism rather than an implementation
detail.
The last line prices a renew correctly, which an earlier revision did not: **a renew is two calls,
not one** — `read_namespaced_lease` then `replace_namespaced_lease`
([`leader_elect.py:78`][leader_elect-py-78], [`leader_elect.py:84`][leader_elect-py-84]) — so
budgeting one round trip means budgeting two timeouts. Sized against one, a leader whose first
renew round trip timed out would demote without ever retrying, tearing down the table on a single
transient blip.

**What a timed-out call raises is not something to assume.** An earlier revision argued the
aggressive timeout was safe because "a timed-out read leaves `holder` at `None` and falls into the
loss branch" ([`leader_elect.py:111-153`][leader_elect-py-111-153]). The loop has exactly two
handlers and both are `except ApiException` ([`leader_elect.py:85`][leader_elect-py-85],
[`leader_elect.py:111`][leader_elect-py-111]), and `_request_timeout` is passed through to urllib3,
whose timeout errors the client does not convert — `kubernetes.client.rest` special-cases `SSLError`
and little else. An unconverted timeout therefore propagates out of `main()` and **kills the
process**, and since `release_lease_and_exit` is wired only to `SIGTERM`/`SIGINT`
([`leader_elect.py:63-64`][leader_elect-py-63-64]) nothing drops the label or clears
`holder_identity` — P3's leaked-leader state, reached by the very change that was supposed to be
the safe direction, and now holding a 35 s lease instead of a 15 s one.

The design does not depend on the answer: **the loop catches broadly around every lease call and
treats any failure as "no answer"**, which is both correct if the client does convert and correct
if it does not. 4 lists that as part of S3 rather than leaving it to the client's internals. With
that in place the direction genuinely is safe — an aggressive timeout can only cause an unnecessary
demotion, never an unsafe overlap — which is what makes 2 s defensible for a same-cluster call on
one Lease object.

Today, with one process, the shutdown term is 10 s. Laid out on a timeline from the moment the
outgoing leader stops being the holder:

```text
  TODAY — lease_duration = 15 s        15 > 7 + 10   is FALSE
                                       ...and 7 is a sleep, so even this is optimistic

   t     outgoing leader                    any other replica
  ────   ──────────────────────────────     ──────────────────────────────
   0     loses the lease
         │
         │  sleep: up to 5 + U(0,2) = 7 s
         │  plus an untimed lease read
   7+    notices; SIGTERM to its
         │  processes
         │
         │  10 s termination grace
         │
  15     │                                  lease expires — may acquire
         │                                  │
         │                                  └─ starts its own processes
         │  ◄════════ 2 s OVERLAP ════════► │
         │                                  │
  17+    SIGKILL; processes finally gone    already running


  PROPOSED — lease 35 s, renew deadline 9 s, two processes    35 > 9 + 20   holds

   t     outgoing leader                          any other replica
  ────   ────────────────────────────────────     ──────────────────────────────
   0     last SUCCESSFUL renew
         │  retries every 2 + U(0,1); each call
         │  times out at min(2s, 9 - elapsed),
         │  so nothing can run past t=9.
         │  9 > 3 + 2x2, so one full read+write
         │  retry fits inside the deadline
   9     deadline reached on the LOCAL clock —
         │  no call has to return, and no reply
         │  has to be believed
         │  stops the gateway
  19     gateway gone; stops session_kv
  29     session_kv gone — table empty
         ·
         ·  6 s margin
         ·
  35                                              lease expires — may acquire
```

P5 is the two-second overlap in the upper timeline — and the `+` signs are P5's second half, the
one the old arithmetic could not express.

#### What a longer lease actually costs, which is less than this design used to claim

Before pricing the options, correct the price. Every earlier revision costed a longer lease as
"+15 s of failover blackhole" flat, and that is wrong for the failover that happens most often.

**A clean handover does not wait for the lease to expire.** `release_lease_and_exit` clears
`holder_identity` ([`leader_elect.py:49-50`][leader_elect-py-49-50]), and a challenger's next poll
tests `if holder is None or is_expired` ([`leader_elect.py:100`][leader_elect-py-100]) — the
`None` arm fires immediately. The stored duration is never consulted. So:

| Failover                                                    | The challenger waits for                    | Effect of 15 → 35 s               |
| ----------------------------------------------------------- | ------------------------------------------- | --------------------------------- |
| **Clean** — rollout, `kubectl delete pod`, scale-down       | the explicit release, then one retry period | **none**, and ~4 s _faster_ via C |
| **Unclean** — node loss, OOM-kill, `SIGKILL` past the grace | the stored `leaseDurationSeconds` to expire | +20 s                             |

Rollouts are the common case and pay nothing. What pays is node loss and OOM-kill — and on node
loss the lease is not the dominant term anyway, since upstream's default node-monitor grace period
is 40 s before the node is even marked unready. This does not make a longer lease free; it makes it
affordable, and it is why the margin below is chosen for comfort rather than shaved to the
minimum.

It also means the sentence "S3 costs +15 s of blackhole", which appears in this document's own
migration section and in the KV decomposition's phase 3 row, was over-stated in both. The figure
to carry forward is **+20 s on unclean failover only**.

#### Four ways to satisfy it, and they are not equally priced

| Option                                           | Result with two processes     | Cost                                                         |
| ------------------------------------------------ | ----------------------------- | ------------------------------------------------------------ |
| D. Renew deadline (9 s), calls clamped to 2 s    | makes the first term _exist_  | **mandatory** — without it there is no inequality to satisfy |
| A. Raise the lease, 15 → 35 s                    | `35 > 13 + 20`, margin 2 s    | +20 s on unclean failover; none on a rollout                 |
| B. Shorten each grace, 10 → 4 s                  | `15 > 9 + 8` — **fails**      | and it truncates clean shutdown for an unmeasured saving     |
| C. Shorten the retry period, 5+U(0,2) → 2+U(0,1) | `15 > 9 + 20` — **fails**     | ~2.4× more lease reads; ~4 s faster clean handover           |
| **A + C + D** (proposed)                         | `35 > 9 + 20`, margin **6 s** | the two above, together                                      |

D is not an option in the sense the other three are: it is the precondition. A, B and C are all
adjustments to an inequality whose left-hand side means nothing until the first term is bounded,
which is why an earlier revision could weigh A against B against C and still ship something
unsafe. A's row is shown at a 13 s deadline because at today's 5+U(0,2) retry period the deadline has to be
that long to fit a full read-and-write retry; C is what buys it back down to 9.

B is the one to avoid _today_ and the one to revisit at S4. Four seconds is not obviously enough
for the gateway to finish in-flight work, and buying failover safety by truncating clean shutdown
trades one correctness problem for another. But 3.2's grace column is per-entry and the KV
server's 10 s is inherited rather than measured — measuring it is the cheapest remaining lever,
and it is a measurement rather than a guess.

| Parameter                     | Today               | Proposed                                   |
| ----------------------------- | ------------------- | ------------------------------------------ |
| `lease_duration_seconds`      | 15 s                | **35 s**                                   |
| retry period (the sleep)      | 5 s + U(0,2)        | **2 s + U(0,1)**                           |
| `renew_deadline_seconds`      | — (no such concept) | **9 s**                                    |
| lease call `_request_timeout` | — (no timeout)      | **2 s**, clamped to the remaining deadline |
| per-process termination grace | 10 s                | unchanged (3.2)                            |

**Why 35 and not 30, the smallest round number that fits.** At 30 the margin is 1 s, and the
margin's job is to absorb variance in its own two terms — clock granularity, a `SIGKILL` that the
kernel takes a moment to deliver, a grace that ends a hair late. It is explicitly _not_ headroom
for a third process, which 3.2 forbids and the arithmetic below independently refuses. Two seconds
of variance budget on a 28 s spend is not a margin, it is a rounding error, and the correction
above is what makes the extra 5 s cheap enough to take.

The guarantee this buys should be stated exactly: **once the Lease object itself carries the new
duration, and provided each process actually dies within its grace, the outgoing leader has
stopped its processes before any other pod can acquire the lease.** The first clause is a
migration step rather than a formality, and skipping it makes S3 actively harmful. The second is
the residue 3.6 owns; a renew deadline bounds when the supervisor _decides_ to stop, not when the
kernel finishes.

#### Raising the constant does not raise the Lease

`lease_duration_seconds` reaches the API object on exactly one code path: the 404-create branch
([`leader_elect.py:114-125`][leader_elect-py-114-125]). Neither of the other two writers touches the field —

| Path      | Where                                                | Writes `leaseDurationSeconds`?                            |
| --------- | ---------------------------------------------------- | --------------------------------------------------------- |
| create    | [`leader_elect.py:114-125`][leader_elect-py-114-125] | **Yes** — the only place the constant is ever stored      |
| renew     | [`leader_elect.py:82-84`][leader_elect-py-82-84]     | No — `replace_namespaced_lease` on the body read back     |
| take over | [`leader_elect.py:101-106`][leader_elect-py-101-106] | No — same; it sets holder, renew, acquire and transitions |

— and a challenger's expiry test reads the **stored** value, not its own constant:
`duration = lease.spec.lease_duration_seconds or lease_duration_seconds`
([`leader_elect.py:89`][leader_elect-py-89]). Nothing outside the script writes it either: the operator
reconciles the leader `Role` and `RoleBinding` and never the Lease
([`platformagent_controller.go:841-855`][platformagent_controller-go-841-855]), and the object is created with bare
name/namespace metadata and **no owner reference** ([`leader_elect.py:117`][leader_elect-py-117]), so it survives every
rollout, upgrade and CR deletion.

On any already-running multi-replica install, therefore, the Lease keeps `leaseDurationSeconds: 15`
after the upgrade, and S3's arithmetic is computed against a number nobody rewrote:

| State                                  | Challenger expires at | Outgoing leader needs          | Margin                   |
| -------------------------------------- | --------------------- | ------------------------------ | ------------------------ |
| Today (1 process)                      | 15 s (stored)         | 7 + 10 = 17 s _(7 is a sleep)_ | **−2 s**, optimistically |
| S3 only, stale Lease (1 process)       | 15 s (stored)         | 9 + 10 = 19 s                  | **−4 s**                 |
| **S3 + S4, stale Lease (2 processes)** | 15 s (stored)         | 9 + 20 = 29 s                  | **−14 s**                |
| S3 + S4, migrated Lease (2 processes)  | 35 s (stored)         | 9 + 20 = 29 s                  | **+6 s**                 |

Read the first two rows together before the third. They are not comparable as printed: today's
−2 s is computed against a sleep and so is a best case with no worst case, while S3's −4 s is a
real bound. **S3 against an unmigrated Lease is not an improvement on today even at one process** —
it converts an unknown into a known negative, which is progress of a kind and not the guarantee
the phase is for.

The third row is the one to act on: **S3 sequenced with S4, which is how 5 sequences it, is a 14 s
overlap against the 2 s that P5 exists to close.** Worse, it fails silently. The startup assertion
in 6 compares the supervisor's own constants to each other, so it passes and reports a safety
property that does not hold of the running system.

Two changes close it, and both are wanted:

- **Write the field on every renew and every takeover**, not only on create. The stored value then
  converges to the constant within one poll of the first upgraded leader renewing, and can never
  drift again — including for anyone who tunes it later, which is the failure mode that would
  otherwise recur.
- **Delete the Lease as part of S3's rollout**, or patch `leaseDurationSeconds` on it directly.
  This is what makes the guarantee true immediately rather than one renew later. Deleting is safe:
  the next poll hits the 404 branch and recreates it, at the cost of one election.

A rolling update still has a mixed-version window in which an old replica reads 15 s while a new
one needs 29 s. That window is bounded by the rollout and unavoidable from inside the script —
whichever value is authoritative, one side of a mixed pair disagrees with it. Deleting the Lease
_before_ the rollout does not help either, since the first pod to recreate it may be an old one.
It is a reason to sequence S3 as a deliberate step with the Lease patched and verified, not to
treat the constant change as self-applying.

#### A third process does not fit, and there is a candidate

The shutdown term is the sum of 3.2's grace column, so a third entry at the same 10 s takes it to
30 s: `35 > 9 + 30 = 39` is **false** by 4 s and the startup assertion refuses to boot — measured,
not predicted (E6 in 6.0).

**This is a consequence, not the guardrail.** 3.2's boundary — the table grows only for something
that must be running for the container to do its job — is what holds the line, and it holds it for
reasons that have nothing to do with arithmetic. An earlier revision inverted that, treating the
thin margin as the mechanism and then describing "shipping with the budget nearly consumed" as
deliberate. Designing so that a legitimate parameter change cannot boot is not a safety property;
it is a design that cannot be tuned. The assertion is a backstop that catches someone who ignores
3.2, and it should read as one.

It is worth naming the concrete candidate rather than leaving it abstract.
`agents/chat/scripts/memory_ttl_curator.py` exists, prunes the Hindsight bank, and is explicitly
unscheduled — "nothing schedules this yet. Running it is an operator action." The obvious way to
schedule it is a periodic worker, and the obvious place to put a periodic worker is next to the
other supervised processes. 3.2 says not to, and this is the arithmetic behind that: adding it
would fail the assertion at startup, and the fix would be to weaken one of the lease parameters
to buy room for a job that does not need to be running continuously at all.

If a third **service** is ever genuinely required, the inequality has to be re-solved rather than
nudged — most cheaply by shortening the grace of the process that provably does not need 10 s,
which is a measurement rather than a guess.

### 3.6 What the Lease does not do

It does not fence, and that is a property of leases rather than of this implementation — the
reference implementation says so in its own package documentation, quoted in 3.0. Closing it needs
a **fencing token**: a monotonically increasing number issued with the lease and checked at the
resource itself, which is Kleppmann's answer and the one this design declines below.

A leader partitioned from the API server keeps running until its own next poll
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
obligations an ordinary process does not. These two are the whole job of the **init process**
pattern (3.0), and the reason `tini` and `dumb-init` exist at all.

**Signals.** The kernel installs no default handlers for PID 1: an unhandled `SIGTERM` is
_ignored_ rather than fatal. [`leader_elect.py:63-64`][leader_elect-py-63-64] already registers one, so this works today;
it is listed because deleting that registration would not fail any test, and the symptom — pods
that take the full grace period and then die by `SIGKILL` on every rollout, losing the lease
release every time — is a slow, easily-misattributed regression.

**Reaping.** Orphaned processes reparent to PID 1, and PID 1 must `wait()` for them or they
accumulate as zombies until the PID table fills.

**Be precise about where the orphans come from**, because an earlier revision was not. "The agent
shells out constantly, and the KV server runs `hermes send` per alert" is true and is _not_ a
source of zombies here: those are children of the gateway and of the KV server, and each reaps its
own. They reparent to the supervisor only if their parent dies first — which is a real path, and a
narrow one, not a continuous drip. Two things actually put work on PID 1:

- **The entrypoint's backgrounded jobs**, which the supervisor inherits without ever having
  started them. The Hindsight memory migration (1.2's fourth row) is backgrounded roughly thirty
  lines before `exec "$@"`, so that subshell's parent is PID 1. **Measured on every boot: one
  zombie under today's supervisor, none under the reaper below** (E8 in 6.0).
- **Grandchildren orphaned by a supervised process exiting or being killed** — including by 3.3's
  own `killpg`, which is why the two mechanisms are complementary rather than redundant:
  `start_new_session` + `killpg` is what _stops_ them, and the reaper is what stops the corpses
  accumulating.

The measured figure is one per boot, which exhausts nothing. It matters because it proves the path
is live rather than theoretical, and because the next background job added to the entrypoint
inherits the same treatment — the argument for reaping is correctness of the process model, not
an imminent PID exhaustion, and it should not be sold as the latter.

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

So there is exactly one caller of `waitpid(-1)`, and it **dispatches** rather than discards:

```python
def reap(table, now):
    """The single point of truth for child exits. The ONLY caller of waitpid(-1).

    Nothing anywhere calls Popen.poll(). The one other place a child status is
    consumed is Supervised.stop(), whose Popen.wait() is a TARGETED waitpid on a
    pid this function has not been given a chance to steal -- see the invariant
    below, which is what makes that safe and is not optional.
    """
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
        entry.on_exit(code, now)                # 3.3 owns what happens next
```

**The invariant, stated exactly, because "exactly one `waitpid`" is not true and saying it that way
invites the bug it is meant to prevent.** `Supervised.stop()` has to wait for the process it just
signalled; there is no way to terminate-then-grace-then-kill without consuming a status. The rule
that actually holds is three parts:

1. **One caller of `waitpid(-1)`** — this function. A wildcard wait is what can steal another
   entry's status, and E1 measured the consequence: CPython catches the resulting `ECHILD` and
   `poll()` then reports **0**, so a process that exited 3 reads as a clean, intentional stop.
2. **`stop()`'s targeted wait is permitted**, because `waitpid(pid, ...)` cannot take a status that
   is not its own.
3. **`reap()` and `stop()` never run concurrently.** Both run on the poll loop's thread, in
   sequence. This is the part that is easy to lose: the obvious way to make reaping prompt is a
   `SIGCHLD` handler or a background thread, and either one puts a wildcard wait in a race with
   `stop()`'s targeted one — recreating E1 exactly, on the shutdown path, where it is hardest to
   observe. Reaping once per iteration is slow and correct; if promptness is ever wanted, the way
   to get it is a shorter iteration, not a second thread.

The consequence for 3.3 is a constraint rather than an option: nothing there may call
`self.proc.poll()`, which is why that sketch splits `tick` (start and cap arithmetic) from
`on_exit` (called only from here, and now actually called from here — the two sketches used to
disagree about whether `reap` went through `on_exit` or wrote the entry's fields itself).

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
server into a second container in the same pod and let the kubelet own it — the **sidecar**
pattern, in the sense Burns and Oppenheimer gave it (HotCloud 2016), and one this pod already uses
three times over.

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
`session-kv-decomposition.md`
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

**What is _not_ a reason: the loopback bind.** The server listens on `127.0.0.1:8699` and every
caller reaches it there, which looks at a glance like something a container boundary would break.
It would not — containers in a pod share the network namespace, which is exactly what the
entrypoint's own comment says the current arrangement relies on (quoted in full in 1.2). Reasons
1 and 2 carry this section; the bind does not, and listing it would be padding.

**A third placement now has a precedent, and it is worth naming.** Between this design's first
draft and now, the repository grew a stateful backing service — Hindsight — and put it neither in
the agent pod nor under a supervisor. It is its own Deployment with its own Postgres
(`k8s-operator/config/integrations/hindsight/`), reached over a Service. So "run it as a separate
in-cluster service" is demonstrably available here, and the Session KV server could in principle
follow it.

That option is **settled, and declined**, by
`session-kv-decomposition.md`
§8 rather than here — on operations rather than on feasibility, and with the reopening condition
named: an in-cluster Postgres that ships unconditionally, replicated, and authenticated, all
three. It changes nothing in this section as long as the store stays a SQLite file on the shared
volume: reasons 1 and 2 above still decide where the server runs.

**When to revisit.** Reasons 2 and 3 both descend from the single-writer requirement, which comes
from `session-kv-decomposition.md`
§4 and is not yet in force. **If that requirement goes away, this design should be re-scoped
rather than shipped as written**: without exclusive access there is no handover to sequence and
no reason to gate on the leader, and a plain sidecar container becomes the better answer to "the
KV server has no owner."

The scenario below is kept although the decision is made, for two reasons. It is what §8 cites
from this side when it argues the point, so deleting it would leave that argument half-recorded.
And it is the costing anyone re-opening the question needs — the value of writing down what a
decision would have cost is that the next person does not have to re-derive it.

##### The Postgres scenario

Suppose the Session KV store moved from SQLite-on-a-volume to the in-cluster Postgres that already
backs Hindsight. Most of this design would not simplify — it would **stop having a reason to
exist**, because nearly all of it hangs off one link in a chain:

```text
  SQLite on an RWX volume cannot do multi-writer            (session-kv R3)
    └─> the KV server must be the single writer
          └─> so it must be leader-gated
                └─> so the lease holder must start and stop it
                      └─> so it must be a supervised process
                            ├─> a supervisor at every replica count   P1, P2
                            ├─> a process table with ordering         3.2
                            ├─> a summed shutdown budget              3.2, 3.7
                            └─> the lease inequality                  3.5, P5
                                  └─> 35 s lease -> +20 s on UNCLEAN failover
```

Postgres cuts the first link, and everything under it goes. Nor is the file lock the only thing:
with a real database nothing else needs the leader either — an outbox drains with
`SELECT … FOR UPDATE SKIP LOCKED`, retention GC is an idempotent `DELETE`, and event dedup is a
unique index, which
`session-kv-decomposition.md`
§4.3 already names as the real authority.

| Would collapse                                  | Would survive                                                         |
| ----------------------------------------------- | --------------------------------------------------------------------- |
| P1, P2 and the two modes — nothing to supervise | **P3** — the crash path leaks the label and the lease                 |
| 3.2's process table, criticality, ordering      | **P4** — the gateway container still has no probe                     |
| 3.3's restart policy and required-vs-optional   | **3.7's reaping** — the entrypoint's migration job orphans regardless |
| 3.5's retiming, **and the longer lease**        |                                                                       |
| Reasons 2 and 3 of this section                 |                                                                       |

What would be left is perhaps a quarter of this document: route the crash path through
`release_lease_and_exit`, reap orphans as PID 1, keep a much smaller status file so the probe can
tell a follower from a broken leader, and add the exec probe. **And S3 would disappear rather than
ship**, so the lease would not have to grow at all.

**This design does not make that call**, and did not: it is a storage decision, and
`session-kv-decomposition.md`
§8 has now made it. An earlier revision of this section listed three considerations "worth handing
over with it, because they are not in that document today" — the conditional deployment, the
single-replica StatefulSet on the alert-and-triage path, and the `POSTGRES_HOST_AUTH_METHOD=trust`
question against #616's pseudonymisation work. All three are in §8 now, and are the three reasons
it gives, each stated there as sufficient on its own. They are not repeated here: §8 owns that
argument, and a second copy is a second thing to keep true.

The one thing worth carrying forward on this side is the shape of the dependency. **The chain
above is the reason a settled storage decision is not a detail of this design but a precondition
for it** — which is why 8 records Q5 as closed rather than dropping it, and why 5 nonetheless
keeps S1–S2 separable, so that P3 and P4 — defects in the leader election itself, independent of
where the KV server ends up — can ship either way.

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
  ([`platformagent_manifests.go:2298-2303`][platformagent_manifests-go-2298-2303]). **Change this gate only** — the other three in
  1.1's table stay on the effective replica count, deliberately:
  - Moving the `LEADER_ELECTION_*` gate ([`platformagent_manifests.go:1561-1576`][platformagent_manifests-go-1561-1576]) to the
    intended count would render `elected` mode on a Deployment with zero pods and a
    `ReadWriteOnce` volume, and would then need the Service selector moved with it or followers
    running nothing would sit in the endpoint list. That is three coupled changes to improve a
    state in which nothing runs.
  - Leaving them where they are means a `scaleToZero` agent renders `solo`, which 3.1 prices: the
    same bounded hazard as `kubectl scale`, held down by the operator reconciling the count back
    and by the RWO volume.
  - What must **not** happen is the middle ground: if anyone ever moves one of the four, they move
    together. E7's `C10` asserts they currently agree, and is the test that notices.
  - The **Deployment strategy is a fifth replica-derived value and reads the _intended_ count**
    ([`manifest_helpers.go:290`][manifest_helpers-go-290]). Leave it alone too, but know it is
    there: 1.1 works through why `replicas: 3` plus `scaleToZero` renders RollingUpdate beside
    `solo` mode, and E7's `C9` pins the disagreement so a later change to either side is visible.
- Add the **exec** readiness probe of 3.4 to the `platform-agent` container
  ([`platformagent_manifests.go:2339-2357`][platformagent_manifests-go-2339-2357]) — matching the `envoy-credential-proxy` probe's shape,
  not an `httpGet` — **together with the `startupProbe`, which is not optional and not deferrable**.
  Nothing writes the status file until the entrypoint `exec`s the supervisor, and the entrypoint's
  bootstrap lock alone can wait 300 s ([`docker-entrypoint.sh:304`][docker-entrypoint-sh-304]), so a
  probe with no startup gate turns a slow boot into a crash loop on a container that boots fine
  today (3.4). The liveness probe follows in a later phase, not with it, and runs a **different**
  script; 3.4 says why sharing one would undo 3.3.
- Add the status volume: an `emptyDir` with `medium: Memory` and `sizeLimit: 1Mi`, mounted
  at `/var/run/supervisor` in the `platform-agent` container **only** (3.4). It holds both files —
  `status.json` and the one-line `ready` the probes read. It must not be a path under
  `$PLATFORM_AGENT_HOME`, which is the ReadWriteMany PVC every replica shares; 3.4 works
  through what a shared status file breaks. The two `emptyDir`s at
  [`platformagent_manifests.go:2192-2193`][platformagent_manifests-go-2192-2193] are the shape to copy.
- Set `terminationGracePeriodSeconds: 60` on the pod spec (3.7). It is unset today, so it is 30 s,
  which the two-process shutdown budget does not fit inside with useful margin.
- **At S4, give the single-writer rule something that actually enforces it.** `ReadWriteOnce` does
  not: it is a per-node mode, and two co-scheduled pods may both mount the volume read-write (3.1).
  Either request `ReadWriteOncePod` for the volume the KV server owns, or add a default pod
  anti-affinity — today `Affinity` comes only from user-supplied `availability.affinity`
  ([`platformagent_manifests.go:1706`][platformagent_manifests-go-1706]). This is not needed for S1–S3, which add no exclusive
  writer, and it must not be skipped at S4, which does.
- Retime the election (3.5): `lease_duration_seconds` 15 → **35**, the retry period `5 + U(0,2)` →
  **`2 + U(0,1)`**, and two new constants — `renew_deadline_seconds = 9` and a **2 s
  `_request_timeout`, clamped to the remaining deadline, on every lease call**. The last two are
  the ones without which the rest is cosmetic. All of them live in
  [`k8s-operator/internal/controller/leader_elect.py:70-71`][leader_elect-py-70-71], a real file that
  [`platformagent_manifests.go:3452`][platformagent_manifests-go-3452] pulls in with `//go:embed` and
  [`platformagent_manifests.go:185`][platformagent_manifests-go-185] mounts as a ConfigMap key — they are not inline string literals
  in the Go source.
- **Stop leading on the renew deadline, not on a reply** (3.5). This is a change to the loop's
  shape rather than a constant, and it is the part of S3 that P5 and P6 both actually need. Four
  pieces, and it is not safe with three of them:
  - Track the last _successful_ renew on `time.monotonic()`, and take the loss branch once it is
    `renew_deadline_seconds` old, without waiting for a call to return.
  - **Clamp every lease call to `min(lease_call_timeout, deadline - now)`** and re-test the
    deadline after each call. A deadline tested once per iteration is bounded by the iteration, not
    by itself.
  - **Clear `last_renew` on a definitive loss**, not only on a timeout, or a denied leader
    re-promotes on its next timed-out call.
  - **Catch broadly around every lease call.** Today the loop has two handlers and both are
    `except ApiException` ([`leader_elect.py:85`][leader_elect-py-85],
    [`leader_elect.py:111`][leader_elect-py-111]); a `_request_timeout` firing need not produce
    one, and an unconverted exception kills the process while it still holds the lease and the
    label. Treat any failure of a lease call as "no answer" and let the deadline decide.
- **Signal the process group** (3.3): `start_new_session=True` on every `Popen`, and `killpg`
  rather than `Popen.terminate()`, so a grandchild of the gateway cannot outlive the handover 3.5
  guarantees.
- **Write `lease_duration_seconds` on the renew and takeover paths as well as on create**
  ([`leader_elect.py:82-84`][leader_elect-py-82-84], [`leader_elect.py:101-106`][leader_elect-py-101-106]). Without this the constant
  above changes nothing on an existing install, because a challenger reads the value stored on the
  Lease ([`leader_elect.py:89`][leader_elect-py-89]) and nothing ever rewrites it — 3.5 has the arithmetic, and the
  answer is a 9 s overlap where P5's is 2 s.
- **Migrate the Lease as an explicit step of S3**: `kubectl delete lease <agent>-leader`, or patch
  `leaseDurationSeconds` on it. The object has no owner reference, so it outlives the rollout that
  changed the constant. Verify with `kubectl get lease <agent>-leader -o jsonpath` rather than
  from the pod's logs, which only show what the supervisor believes.
- Ship the **two** probe scripts — `/opt/hermes/bin/supervisor-ready` and
  `/opt/hermes/bin/supervisor-alive`. They are new files in the image rather than an operator
  change, but they version with the operator's embedded `leader_elect.py` and have to move with
  it. Neither may invoke an interpreter (L12), and neither may hard-code `python3`, which is not
  the interpreter the operator's own `Args` names.
- Update the two comments named in 3.1: `AGENT_SHARED_STATE_SETUP` at
  [`platformagent_manifests.go:60-70`][platformagent_manifests-go-60-70] and `Args, never Command` at
  [`platformagent_manifests.go:2288-2297`][platformagent_manifests-go-2288-2297]. The second is also
  rewritten at S4 by the KV decomposition's phase 3, which quotes it — one edit, not two.
- Golden files in `k8s-operator/internal/testing/testdata/platform/expected/` gain the probe and,
  at a single replica, the `Args` they currently omit.

---

## 5. Migration

| Phase | Change                                                                                                                                                                                                                                                | Risk                                                                                                                                                                                    |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1    | Supervisor modes and the process table, with the gateway as the only process. PID-1 reaping (3.7). Operator sets `Args` unconditionally. Behaviour-preserving at both replica counts.                                                                 | Low — the single-replica path gains a parent process and nothing else                                                                                                                   |
| S2    | Per-process restart policy, the status files, and the **startup + readiness** probes (`supervisor-alive` gating `supervisor-ready`).                                                                                                                  | Medium — first probe on this container, and at one replica the strategy is `Recreate`, so a probe that never passes is an outage rather than a stalled rollout. Roll to one agent first |
| S2b   | **Liveness** probe — `supervisor-alive`, a _different_ script (3.4), longer threshold.                                                                                                                                                                | Medium — a wrongly-firing liveness probe is a restart loop. Ships only after S2's readiness has soaked                                                                                  |
| S3    | Renew deadline + lease-call timeout, lease 35 s, retry `2 + U(0,1)`, `terminationGracePeriodSeconds: 60`. Writes the duration on renew and takeover, **migrates the existing Lease object** (3.5), and adds the startup assertion of 6.               | Medium — the constant does not reach an existing Lease on its own, and S3+S4 against a stale one is a **13 s** overlap, worse than the 2 s P5 exists to close. Plus the longer lease    |
| S4    | Second process adopted (the Session KV server), and entrypoint step 5 plus the MCP launcher deleted — but not its `mkdir -p logs` (1.2). Adds the single-writer guard 3.1 shows RWO does not provide. Owned by `session-kv-decomposition.md` phase 3. | Medium — the entrypoint gate check asserts on step 5                                                                                                                                    |

**S1, S2 and S2b are worth shipping on their own merits.** They fix P1–P4, which are live defects
independent of anything the KV decomposition does: the KV server is already an unsupervised second
process, the crash path already leaks leader state, and the gateway already has no probe.

**S3 is different, and the reason it pairs with S4 is not the obvious one.** An earlier revision
said "sequence it with S4, which is what introduces the exclusive hold that makes it necessary,"
and contradicted itself one sentence earlier by observing that there is no
`locking_mode=EXCLUSIVE` anywhere. Both halves cannot be true. **S4 does not introduce the
exclusive hold**: it is the KV decomposition's phase 3, and `locking_mode=EXCLUSIVE` arrives at its
**phase 6**, three phases later.

The real reason is arithmetic. **S4 is the moment the shutdown term stops being one grace and
becomes a sum** — 10 s to 20 s — and with today's constants `15 > 7 + 20` is false by twelve
seconds. The overlap between an outgoing leader still stopping and an incoming one already
starting goes from about 2 s to about 13 s. Until phase 6 that is not a correctness break, because
nothing holds the file exclusively; it is two KV servers writing one SQLite database for thirteen
seconds instead of two, which default locking survives. It is still a measurable regression, and
shipping S4 without S3 means shipping it knowingly. The same conclusion is reached from the other
side, at more length, by
`session-kv-decomposition.md`
§6.

Getting the reason right changes what may be unbundled. The parts of S3 that only ever help —
the renew deadline, the lease-call timeout, the faster retry period and the raised grace period —
can go with S2, and arguably should: the deadline and the timeout close the unbounded half of P5
and P6 (see both), which is a live defect rather than a preparation. What must stay with S4 is the
**lease duration** and its migration, since that is the part whose only benefit is the longer
shutdown budget S4 creates and whose only cost is paid before then.

**And S3 is not done when the image ships.** Its guarantee lives in the Lease object, not in the
constant, and the object outlives the rollout that changed the constant (3.5). Until the stored
`leaseDurationSeconds` has been migrated and read back, S3 sequenced with S4 leaves a **wider**
overlap than the one P5 describes — so the migration belongs in the phase's runbook, and the
verification is `kubectl get lease`, not a log line.

S4 is where this design and the KV decomposition meet.

---

## 6. Verification

### 6.0 What was prototyped, and what it changed

The mechanisms in 3.3–3.7 were prototyped before this design was finalised — a supervisor with the
process table, criticality, backoff and cap, the status file, the probe, and sequential shutdown,
with the lease stubbed to a file so it runs without Kubernetes, plus a Go check that renders the
operator's real manifests. **Nine of the experiments falsified something this document previously
asserted** (E1, E3, E4, E9, E10, E11, E12, E13, E14), and those corrections are folded into the sections above.

The prototype lives in
[`agent-process-supervisor/`](agent-process-supervisor/)
next to this file and runs with the standard library alone:

```bash
cd docs/designs/agent-process-supervisor && python3 run_experiments.py
```

Every experiment asserts, so a non-zero exit means a claim below has stopped holding — which is
what re-ran them after each rebase onto `main` and confirmed nothing had rotted. It is **not wired
into CI**, for two reasons: the timing-based cases are prototype-grade, and E7 asserts what is
true _today_, so several of its checks are supposed to start failing the moment S1/S2 ship.
Keeping that in CI would be a tripwire on the implementation rather than a regression test. The
directory is meant to be **deleted at S1/S2**, when its cases become the `test_leader_elect.py`
additions listed under **Unit** below.

| #   | Claim under test                                                                                         | Result                                                                                                                                                                                                                                                                                                       |
| --- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| E1  | A generic `waitpid(-1)` breaks the table's view of its own process                                       | **Confirmed, and worse than stated.** See 3.7 — `poll()` reports `0`, not "unknown", and the proposed guard does not work                                                                                                                                                                                    |
| E2  | `httpGet` cannot reach a server bound to `127.0.0.1`                                                     | **Confirmed.** `ECONNREFUSED` from the routable address; `0.0.0.0` connects. `HTTPGetAction.Host` "defaults to the pod IP"                                                                                                                                                                                   |
| E3  | 25% yields `maxUnavailable: 0` at 2 replicas                                                             | **Confirmed and widened** — it is 0 at 1, 2 _and_ 3 replicas (P4)                                                                                                                                                                                                                                            |
| E4  | Optional-vs-required divergence at the cap                                                               | **Confirmed.** Optional → `ready:true, degraded:true`, supervisor lives. Required → cleanup, then exit. Also surfaced the stale-`ready:true`-on-exit gap now fixed in 3.3                                                                                                                                    |
| E5  | Shutdown is the sum over the table                                                                       | **Confirmed.** 10 s / 20 s / 30 s for 1 / 2 / 3 processes at a 10 s grace — 3 processes overruns the 30 s default and needs 3.7's 60 s                                                                                                                                                                       |
| E6  | The inequality catches table growth                                                                      | **Confirmed.** Reproduces every margin in 3.5: today −2, A alone +3, A+C+D +7, a third process −3 (refuses to start), and the unmigrated-Lease rows, where S3+S4 is −13                                                                                                                                      |
| E7  | The eleven manifest-level claims of sections 1 and 3 (fifteen assertions, two of them per-replica-count) | **Confirmed against rendered output.** Renders Deployments at 1/2/3 replicas through the operator's own `buildDeployment`, plus `buildNetworkPolicy`, `buildPlatformService` and `buildPlatformLeaderRole`                                                                                                   |
| E8  | An entrypoint background job reparents to the supervisor                                                 | **Confirmed.** One zombie per boot under today's supervisor, none under 3.7's reaper. The case is the Hindsight migration in 1.2                                                                                                                                                                             |
| E9  | A demoted leader restarts its table on reacquiring the lease                                             | **Falsified 3.3, now fixed.** A stopped entry was terminal, so a reacquired lease resumed with an empty table — label held, nothing served. Also the only case that runs in `elected` mode                                                                                                                   |
| E10 | Raising `lease_duration_seconds` reaches an existing Lease                                               | **Falsified 3.5, now fixed.** Parsed from the real `leader_elect.py`: the constant is stored on the create path only, and a challenger reads what is stored. 4 gains the migration step                                                                                                                      |
| E11 | A renew deadline bounds how long a leader takes to notice                                                | **Falsified 3.5 twice.** First the term was a `time.sleep`, which bounds nothing. Then the deadline was tested once per pass with the calls and the wait free to run past it, which bounds detection by the iteration — measured here as a 50% overshoot. Every blocking step is now clamped to the deadline |
| E12 | The restart cap fires at the rate the KV server will actually fail                                       | **Falsified 3.3, now fixed.** 5-in-300 s needs failures closer than 60 s apart; the KV server's are 61–76 s apart, so the cap was unreachable the moment the `>=` off-by-one was corrected. The window goes to 600 s                                                                                         |
| E13 | Stopping a supervised process stops everything it started                                                | **Falsified 3.3 twice.** `Popen.terminate()` leaves a grandchild running; so does `SIGTERM` to the process group, because the parent exits before the grace elapses and the `SIGKILL` branch never runs. `stop()` gains an unconditional group sweep                                                         |
| E14 | A definitive denial survives the next timed-out lease call                                               | **Falsified 3.5, now fixed.** The deadline answers "nobody has told me anything"; left set through an explicit not-the-holder read, the next timed-out call re-promoted a denied supervisor and restarted the table under the real holder. Verified to fail without the invalidation                         |

E2 and E3 are checks against the authoritative source rather than against reasoning: E2 reads
`HTTPGetAction.Host`'s own documentation in the vendored `k8s.io/api`, and E3 evaluates
`defaultSurgePercent` through `intstr.GetScaledValueFromIntOrPercent` — the function the Deployment
controller itself calls.

Its value was in being wrong three times before any of this reached an implementation PR, and the
cases it exercised are the ones listed under **Unit** below.

**Unit.** `leader_elect.py` has four tests today —
[`test_leader_elect.py`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/internal/controller/test_leader_elect.py),
discovered by [`k8s-operator/Makefile:71`][Makefile-71] rather than named there (#722 replaced the
explicit invocation with `unittest discover`, so a new `test_*.py` beside it is picked up for
free) — and S1 breaks two of them rather than leaving them alone:

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
same counter rather than spinning; lease loss stops processes in reverse order, **and a subsequent
re-acquisition starts them again**.

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
- **Demote, then promote the same supervisor**, and assert the table is running again with new
  PIDs — not merely that lease loss stopped it. Drive it through the lease rather than by calling
  `stop()`, since the bug this replaces was in the state a stopped entry lands in. Assert too that
  a `gave_up` entry stays retired across the cycle, and that `degraded` stays set while it is.
- **The renew path writes `lease_duration_seconds`.** Assert on the body passed to
  `replace_namespaced_lease`, not on the constant: reading the constant back is what made the
  original gap invisible, and a challenger only ever sees the object (3.5).
- **The renew deadline stops the table when no call ever returns.** Make the lease read hang rather
  than raise — a `Mock` whose `side_effect` sleeps past the deadline — and assert the processes are
  stopped anyway. Driving it with an `ApiException` tests the old path and passes without the new
  one, which is exactly how the gap survived review.
- **The cap fires on the sixth failure, not the fifth.** A cap of five means five restarts
  happened; `>=` gives four. Assert the restart count, not just the eventual `gave_up`.
- **A demotion resets the backoff of an entry that has no process.** Drive a process into
  `backoff`, demote, promote, and assert it starts promptly rather than waiting out a stale
  `retry_at`. `stop()`'s early return on `self.proc is None` is what this catches.
- **Stopping kills a grandchild.** Start a supervised process that itself forks a child which
  ignores `SIGTERM`; stop the entry; assert the grandchild is gone. `Popen.terminate()` passes the
  parent half of this and fails the whole of it.
- **`supervisor-alive` ignores `ready`.** Given a fresh `ready` line saying `0`, readiness must
  fail and liveness must pass. This is the regression test for the two probes sharing a script,
  which would restart the container for a down required process (3.4).
- **`assert_timing_safe` is a no-op in solo mode**, and raises in elected mode on a table whose
  graces no longer fit. Both directions, or the mode gate is untested.

The existing file mocks the `kubernetes` package wholesale before importing the module
([`test_leader_elect.py:5-13`][test_leader_elect-py-5-13]). Solo mode must not need that mock at all — a solo-mode test that
passes with `sys.modules['kubernetes']` unset is the real assertion that 3.1's "never contact the
API server" holds.

**Timing.** Assert the inequality in code rather than in prose, **at the start of `elected` mode
and nowhere else**. Both halves of that placement are load-bearing and neither was stated before:

```python
lease_duration_seconds = 35
retry_period           = 2      # + U(0, retry_jitter)
retry_jitter           = 1
renew_deadline_seconds = 9
lease_call_timeout     = 2      # per call, and clamped to the remaining deadline

# The KV server's startup lock retry, from session-kv-decomposition.md 4.2. Named
# here because 3.3's cap is unreachable if a process fails less often than this.
KV_MAX_FAILURE_SPACING = 76     # 60s lock window + the 16s backoff step

def assert_timing_safe(table, mode):
    """3.5 and 3.3, checked once, from main(), AFTER mode selection."""
    if mode != "elected":
        return                  # solo holds no Lease; see below

    # 3.5: the outgoing leader must be finished before anyone else may acquire.
    # The shutdown term is the SUM of the table's grace column -- 3.2 stops them
    # one at a time -- so this tightens automatically when a process is added.
    shutdown_budget = sum(p.grace for p in table)
    assert lease_duration_seconds > renew_deadline_seconds + shutdown_budget, (
        f"lease_duration_seconds={lease_duration_seconds} must exceed "
        f"{renew_deadline_seconds}s renew deadline + {shutdown_budget}s shutdown "
        f"({len(table)} processes, graces {[p.grace for p in table]})"
    )

    # 3.5: the deadline is only enforceable if one full retry fits inside it, and a
    # renew is TWO calls -- read then replace. Budgeting one is what made an earlier
    # revision demote on the first timed-out round trip without ever retrying.
    renew_round_trip = 2 * lease_call_timeout
    assert renew_deadline_seconds > retry_period + retry_jitter + renew_round_trip, (
        f"renew_deadline_seconds={renew_deadline_seconds} leaves no room for one retry of "
        f"{retry_period}+{retry_jitter}s plus a {renew_round_trip}s read+write round trip"
    )

    # 3.3: the cap is a RATE. Reaching C restarts needs C+1 failures in the window,
    # so anything failing less often than WINDOW/C never reaches it and retries
    # forever -- gave_up, degraded and the required-process escalation all vanish.
    assert RESTART_WINDOW / RESTART_CAP > KV_MAX_FAILURE_SPACING, (
        f"a process failing every {KV_MAX_FAILURE_SPACING}s cannot reach a cap of "
        f"{RESTART_CAP} in {RESTART_WINDOW}s"
    )
```

**Deriving the shutdown term from the table** rather than hard-coding it is the point. The
inequality is not a fact about four constants; it is a fact about the constants _and the table_,
and the table is the thing most likely to grow.

**Why it is a function called from `main()` and not a module-level `assert`.** Two ways the obvious
placement takes production down:

- **Mode.** `solo` holds no Lease and has no peer to hand over to, so there is no unsafe overlap
  for the inequality to be about. A top-level assertion fires there too, and would turn a lease
  parameter into a boot failure on the **default single-replica install** — the deployment P1
  exists to fix. A lease-timing check that can break an installation with no lease is worse than
  no check.
- **Phase.** With today's constants the inequality is `15 > 7 + 10`, which is **false** — 3.5's
  first timeline says so. Landing this block with S1 or S2 therefore refuses every boot at every
  replica count. It ships with **S3**, in the same change as the constants it is about, and 5's
  table now says so.

`terminationGracePeriodSeconds` is subject to the same arithmetic (3.7) but lives in the operator,
so it cannot be asserted from inside the pod. Assert it in `platformagent_manifests_test.go`
instead: the rendered grace period must exceed the same `shutdown_budget` plus a lease-release
allowance.

This is the only thing that keeps 3.5 true after someone tunes a constant, and it fails at
startup — loudly, in the pod's own logs — rather than at the failover it would otherwise
silently break.

**Be precise about what it does not check.** The assertion compares the supervisor's own constants
to each other. It cannot see `leaseDurationSeconds` on the Lease object, which is the number a
challenger actually expires on and which no code path rewrites (3.5, E10) — so on an unmigrated
install it passes while reporting a safety property that does not hold. The check that closes that
one has to read the API object, so it belongs with the runtime checks rather than here:

```python
# elected mode, once per acquisition. A WARNING, not an assertion: refusing to
# serve because a peer wrote an old duration would turn a stale field into an
# outage, which is the trade 3.4 refuses everywhere else.
stored = lease.spec.lease_duration_seconds
if stored != lease_duration_seconds:
    log(f"lease duration is {stored}s on the object, {lease_duration_seconds}s here; "
        f"release-before-acquire is NOT guaranteed until this converges")
```

With 4's renew-path write in place this can only be seen once, on the poll before the first
renew — so a message that persists means the write is not happening.

**Operator.** `platformagent_manifests_test.go` for `Args` at a single replica and the probe on
both; the golden files above. Plus two the review of this design added:

- The status volume is an `emptyDir`, is mounted only into `platform-agent`, and its mount path is
  **not** a prefix of `PLATFORM_AGENT_HOME`. Assert the negative explicitly: the failure mode of
  3.4 is a path that looks pod-local and is not, and it only misbehaves above one replica, where
  no golden file renders (see the coverage note below).
- The four gates of 1.1 still agree — `Args`, the election environment, the Service selector and
  the PVC access mode. After S1 the first of them is unconditional, so the assertion becomes "the
  remaining three agree with each other", and it is what notices if someone moves one alone.
  E7's `C10` is this check today.
- **The Deployment strategy still reads the _intended_ replica count** while those gates read the
  effective one (1.1). E7's `C9` pins the disagreement rather than the agreement: it is the one
  place the two counts diverge, and a test that only ever asserted agreement would have missed it.
- The rendered `terminationGracePeriodSeconds` exceeds the process table's summed grace plus a
  lease-release allowance — the operator half of 6's startup assertion, which cannot see it from
  inside the pod.

Note where the existing coverage sits, because S1 lands unevenly across it. The `replicas > 1`
branch is asserted by targeted unit tests — [`platformagent_manifests_test.go:2340`][platformagent_manifests_test-go-2340] pins the exact
`Args` slice, [`platformagent_manifests_test.go:2278-2282`][platformagent_manifests_test-go-2278-2282] the election environment — but **all three
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
  cat /var/run/supervisor/status.json                        # ready:true, degraded:true
# ...and the supervisor restarts it, so degraded clears on its own.

# The status file is PER POD, not one file on the shared PVC (3.4). Two pods must
# disagree about `role`, and neither may appear under $PLATFORM_AGENT_HOME.
for p in <leader> <follower>; do
  kubectl -n kubeagents-system exec $p -c platform-agent -- \
    sh -c 'cat /var/run/supervisor/status.json | tr -d "\n"; echo'      # leader vs follower
done
kubectl -n kubeagents-system exec <leader> -c platform-agent -- \
  ls /opt/data/run 2>&1                                      # expect: No such file or directory

# The Lease carries the raised duration, not just the image (3.5). Check the
# OBJECT: an upgraded install that was never migrated still reads 15 here, and
# S3's guarantee does not hold until this prints 35.
kubectl -n kubeagents-system get lease <agent>-leader \
  -o jsonpath='{.spec.leaseDurationSeconds}'                 # expect 35, not 15

# Readiness and liveness disagree, and must (3.4). With the REQUIRED process
# down the pod leaves the endpoint list but the container is NOT restarted --
# that decision belongs to 3.3's cap, not to a probe. Run both scripts by hand
# against the same file rather than waiting for the kubelet to disagree with you.
kubectl -n kubeagents-system exec <leader> -c platform-agent -- \
  sh -c '/opt/hermes/bin/supervisor-ready; echo "ready=$?"; \
         /opt/hermes/bin/supervisor-alive; echo "alive=$?"'   # expect ready=1 alive=0
kubectl -n kubeagents-system get pod <leader> \
  -o jsonpath='{.status.containerStatuses[?(@.name=="platform-agent")].restartCount}'

# A lease flap must not leave a leader serving nothing (3.3, E9). Deleting the
# Lease demotes every pod; one of them then re-acquires, and whichever pod that
# is must have STARTED ITS TABLE rather than resumed as an empty leader. Run it
# until the winner is a pod that had been leader before, which is the case the
# bug needed.
kubectl -n kubeagents-system delete lease <agent>-leader
sleep 30
kubectl -n kubeagents-system exec <new-leader> -c platform-agent -- \
  cat /var/run/supervisor/status.json                        # ready:true, both running
kubectl -n kubeagents-system get endpoints <agent>           # exactly one address

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

These are the things 2.0 deliberately did **not** make requirements, each with the limit that
would otherwise have forced one.

- **No fencing token.** 3.6 is a limitation, not an oversight. Fencing SQLite behind a monotonic
  token means a second store to hold the token, which is the dependency
  `session-kv-decomposition.md` §8 declines for the same reason.
- **No request-continuous HA.** Raising the lease duration lengthens the blackhole on _unclean_
  failover — node loss, OOM-kill — and leaves a rollout's untouched, since a clean handover
  releases the lease rather than waiting out its expiry (3.5). Either way it does not shorten
  anything. Closing the blackhole needs warm standbys, which is a different design.
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

| #   | Question                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | Bears on |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| Q1  | **`tini` as PID 1, or hand-rolled reaping?** 3.7 shows the reap loop is twelve lines and has one subtle failure (consuming a supervised process's exit status) that no test would obviously catch. `tini -- python3 leader_elect.py` is complementary, not competing. Cost is an image dependency.                                                                                                                                                                                       | S1       |
| Q2  | **How does the probe learn the gateway is _serving_, not merely listening?** The TCP connect in 3.4 is strictly better than a PID check and strictly weaker than a health check. Closing it needs a cheap route on the gateway; `POST /v1/responses` is a model call and far too expensive at probe frequency. Note the answer must distinguish the gateway's own health from its dependencies': 3.4 fixes that a remote service being down may set `degraded` but never `ready: false`. | S2, S2b  |
| Q3  | **Is 5-in-10-minutes the right cap, given the kubelet's own backoff sits underneath it?** For a required process the supervisor exiting hands over to `CrashLoopBackOff`, so the two compose. The cap may be redundant for required processes and only genuinely load-bearing for optional ones.                                                                                                                                                                                         | S2       |
| Q4  | **Should the probe scripts live in the image or the ConfigMap?** They version with the embedded `leader_elect.py`, so they must move together; the ConfigMap already carries one file for exactly that reason. There are two of them now (3.4), which tilts it slightly towards the image.                                                                                                                                                                                               | S2       |
| Q6  | **Should the election itself be hand-rolled at all?** 3.0 works this through: `kubernetes.leaderelection` is already installed in the image and would have prevented both E9 and E10 by construction, but at `36.0.3` it ships only a ConfigMap resource lock, and this design's lock is a Lease that the RBAC, the pod label and the Service selector are built around. The choices are a Lease lock contributed upstream, a change of lock object, or keeping the loop.                | S1, S3   |

Q1 and Q3 are cheap to settle during implementation. Q2 is the one that should not be quietly
dropped: it is the difference between a probe that detects a stopped process and one that detects
a broken one, and the design currently only claims the former.

Q1 and Q6 are the same question at two levels — reaping and election are both solved problems
with tested implementations, and both are currently hand-rolled here. Neither has to be answered
the same way, but they should be answered by the same reasoning rather than one by deliberation
and the other by inertia.

#### Q5 — closed

**Does the Session KV store stay a file, or move to the in-cluster Postgres?** It stays a file.
The decision belongs to
`session-kv-decomposition.md`
§8, which now makes it on operations rather than on feasibility, and gives three reasons each
sufficient alone: Hindsight's Postgres deploys only for installs that chose that memory provider,
it is a single-replica StatefulSet sitting on the alert-and-triage path, and it runs
`POSTGRES_HOST_AUTH_METHOD=trust` behind a NetworkPolicy that many clusters do not enforce.

It is recorded here rather than deleted because it was, correctly, the question that decided this
design's **scope** rather than one of its parameters — 3.8A's Postgres scenario is the costing of
what would have collapsed. The condition that would reopen it is narrow and named in §8: an
in-cluster Postgres that ships unconditionally, replicated, and authenticated. Short of all three,
the answer does not change, and the single-writer requirement this design serves is load-bearing.

<!-- Source links, line-anchored and pinned to the commit these line numbers
     were read from (76a074b). Re-pin here when the numbers are refreshed. -->

<!-- External prior art (3.0), pinned to the version this repository depends on. -->

[Dockerfile-113]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/docker/Dockerfile#L113
[Makefile-71]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/Makefile#L71
[ci-deploy-sh-144]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/hack/ci-deploy.sh#L144
[client-go-fencing]: https://github.com/kubernetes/client-go/blob/v0.31.0/tools/leaderelection/leaderelection.go#L19-L20
[client-go-onstoppedleading]: https://github.com/kubernetes/client-go/blob/v0.31.0/tools/leaderelection/leaderelection.go#L95-L96
[client-go-tryacquireorrenew]: https://github.com/kubernetes/client-go/blob/v0.31.0/tools/leaderelection/leaderelection.go#L408-L415
[client-go-validate]: https://github.com/kubernetes/client-go/blob/v0.31.0/tools/leaderelection/leaderelection.go#L76-L82
[common_types-go-385-390]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/api/v1alpha1/common_types.go#L385-L390
[docker-entrypoint-sh-1193-1200]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L1193-L1200
[docker-entrypoint-sh-1256-1264]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L1256-L1264
[docker-entrypoint-sh-1293]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L1293
[docker-entrypoint-sh-249-253]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L249-L253
[docker-entrypoint-sh-304]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L304
[docker-entrypoint-sh-872-896]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/docker-entrypoint.sh#L872-L896
[entrypoint_gate_check-sh-27-31]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/entrypoint_gate_check.sh#L27-L31
[entrypoint_gate_check-sh-313-324]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/entrypoint_gate_check.sh#L313-L324
[entrypoint_gate_check-sh-87]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/deploy/shared/entrypoint_gate_check.sh#L87
[go-mod-11]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/go.mod#L11
[leader_elect-py-100]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L100
[leader_elect-py-101-106]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L101-L106
[leader_elect-py-111-153]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L111-L153
[leader_elect-py-111]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L111
[leader_elect-py-114-125]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L114-L125
[leader_elect-py-117]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L117
[leader_elect-py-12-16]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L12-L16
[leader_elect-py-134-153]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L134-L153
[leader_elect-py-138]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L138
[leader_elect-py-139-141]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L139-L141
[leader_elect-py-146]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L146
[leader_elect-py-148]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L148
[leader_elect-py-156]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L156
[leader_elect-py-25-55]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L25-L55
[leader_elect-py-35]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L35
[leader_elect-py-41-55]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L41-L55
[leader_elect-py-49-50]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L49-L50
[leader_elect-py-60-61]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L60-L61
[leader_elect-py-63-64]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L63-L64
[leader_elect-py-70-71]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L70-L71
[leader_elect-py-70]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L70
[leader_elect-py-71]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L71
[leader_elect-py-78]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L78
[leader_elect-py-82-84]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L82-L84
[leader_elect-py-84]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L84
[leader_elect-py-85]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L85
[leader_elect-py-89]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/leader_elect.py#L89
[main-go-197-198]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/cmd/main.go#L197-L198
[manifest_helpers-go-273-278]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L273-L278
[manifest_helpers-go-275-277]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L275-L277
[manifest_helpers-go-286-287]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L286-L287
[manifest_helpers-go-290-297]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L290-L297
[manifest_helpers-go-290]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L290
[manifest_helpers-go-61]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/manifest_helpers.go#L61
[platform_mcp_server-py-748-789]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/agents/platform/scripts/platform_mcp_server.py#L748-L789
[platformagent_controller-go-841-855]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_controller.go#L841-L855
[platformagent_manifests-go-100-117]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L100-L117
[platformagent_manifests-go-1385-1389]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1385-L1389
[platformagent_manifests-go-1561-1576]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1561-L1576
[platformagent_manifests-go-1627]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1627
[platformagent_manifests-go-1706]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1706
[platformagent_manifests-go-1730-1736]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1730-L1736
[platformagent_manifests-go-1810-1815]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1810-L1815
[platformagent_manifests-go-1818-1819]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1818-L1819
[platformagent_manifests-go-185]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L185
[platformagent_manifests-go-1972-1979]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L1972-L1979
[platformagent_manifests-go-2192-2193]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2192-L2193
[platformagent_manifests-go-2288-2297]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2288-L2297
[platformagent_manifests-go-2298-2303]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2298-L2303
[platformagent_manifests-go-2339-2357]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2339-L2357
[platformagent_manifests-go-2846-2849]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2846-L2849
[platformagent_manifests-go-2849]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L2849
[platformagent_manifests-go-3452]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L3452
[platformagent_manifests-go-55]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L55
[platformagent_manifests-go-60-70]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L60-L70
[platformagent_manifests-go-65-70]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests.go#L65-L70
[platformagent_manifests_test-go-2278-2282]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests_test.go#L2278-L2282
[platformagent_manifests_test-go-2340]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/platformagent_manifests_test.go#L2340
[session_kv_server-py-382]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/agents/platform/scripts/session_kv_server.py#L382
[test_docker_entrypoint-py-19]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/tests/test_docker_entrypoint.py#L19
[test_leader_elect-py-5-13]: https://github.com/gke-labs/kube-agents/blob/76a074b8cddc467c753e33801c3c69d814ec8469/k8s-operator/internal/controller/test_leader_elect.py#L5-L13
