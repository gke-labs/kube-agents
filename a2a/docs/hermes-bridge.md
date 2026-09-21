# The Hermes bridge

- **Author:** [@bnaylor]
- **Date:** 2026-08-26
- **Status:** draft for review
- **Companions:** the A2A payload spec (task lifecycle, steering), the NATS deployment
  spec (accounts), the subagent profiles spec (supervision, CAS)

## Purpose

The retargeted first wave routes every gateway task to the addressee `platform`, and
nothing answers on that subject yet - the worker adapter (W4) fast-follows, and the
dispatcher is stage 3. The bridge is the stand-in executor: a small Go daemon on
`a2a/lib` that consumes tasks addressed to `platform`, runs
`hermes -p platform chat -Q -q <prompt>` per task, and publishes the lifecycle events with
the output as the `result` artifact. It is scaffolding with a planned demolition date:
when the dispatcher and worker adapter land, the bridge retires. Nothing here is
protocol - the wire contract is the payload spec's, unchanged.

## Where it runs

**Sidecar in the platform-agent pod, declared via the CR's `spec.deployment.sidecars`
field.** The bridge needs two things that only exist in that pod: the `hermes` CLI (it lives in the
platform-agent image, so the bridge image builds FROM it and adds one static binary) and
the persona state - `$HERMES_HOME` is the agent's data PVC, RWO, holding the platform
profile's config, memory, and skills. A separate Deployment would need that PVC mounted
cross-pod, which RWO only allows with same-node scheduling games. Not worth it for a
component we intend to delete.

The `sidecars` field takes ordinary `corev1.Container` entries, so the operator renders
the bridge without any operator code change and reconcile never fights us. The sidecar
mounts the same data volume, runs as the pod's KSA (model auth via Workload Identity for
free), and gets `NATS_URL` plus creds from the a2a creds Secret.

Concurrent hermes processes under one `$HERMES_HOME` is the kanban dispatcher's existing
posture (`deploy/docker/patches/kanban_result_required.py` documents `_default_spawn`
spawning the same kind of one-shot `hermes chat -q` process), so the bridge inherits a
known-working concurrency story. Cap is 2, matching the platform profile's `concurrency` in the profiles spec.

## What this deployment method costs

Two properties of riding `spec.deployment.sidecars`, stated here because the operator
cannot fix either one - gating a user-supplied container means overriding user intent.

**Flipping to `mode: today` with the sidecar still set takes the agent down.** The
operator copies `spec.deployment.sidecars` into the pod without consulting the mode, so
the flip removes the NATS Service and leaves the bridge dialling a host that no longer
resolves. Confirmed live 2026-09-05: the sidecar crash-loops, and because it shares the
agent's pod the pod never reaches Ready - the whole agent is down, not merely carrying
an A2A trace. Unset `spec.deployment.sidecars` _before_ flipping to `today`. In any
flip runbook that step is a blocker, not tidiness.

**The webhook does not screen sidecar env, on purpose.** The `SensitiveEnvVars`
refusal applies to `spec.deployment.env` only; a sidecar's own `env` is unscreened (the
webhook checks a sidecar's `securityContext`, and checks its `volumeMounts` against the
reserved volume names, and nothing else about it). The bridge depends on exactly that
gap - its `NATS_URL` and credentials arrive as sidecar env.
Closing it breaks this deployment method, so it stays open as a stated trade while the
bridge exists; the bridge's demolition removes the reason.

One provenance note: no build config for the bridge image ships in this repository.
The image is fork-built for the playground (`FROM` the platform-agent image plus the
one static binary above) and is not in `images.json` or the release pipeline; it joins
the release surface at stage-2 graduation or dies before it, whichever the dispatcher
decides.

## Bus user and grants

The `…in` subject has two reader roles by design - the dispatcher for new tasks, the
executor for everything after the submission. The bridge is both, collapsed into one
process: it holds the one durable consumer on `a2a.tasks.platform.*.in` (the dispatcher
role, `durable: bridge-platform`) and handles follow-ups and cancels for tasks it is
running (the executor role). That collapse is exactly what makes it a stand-in - when
the real dispatcher arrives, the roles separate again and the bridge has nothing left to
do.

The bridge connects as the static `bridge` user, whose grants are written for this
program and nothing else: subscribe `a2a.tasks.platform.*.in`, publish
`a2a.tasks.platform.*.events`, `$KV.runtime-state.>` both ways for the in-flight
registry below, and `_INBOX.bridge.>`. Nothing wider - a bridge that can publish
submissions is a bridge that can impersonate the gateway.

The JetStream tax is not `$JS.API.>`: it is the `$JS.API` subjects the bridge emits on
TASKS and `KV_runtime-state` — stream info, consumer create, pull, direct get, and the
KV watcher's consumer delete — named one by one in the operator's
`a2aBridgeJetStreamGrants`; plus `$JS.ACK.TASKS.>`, ack scoped to the one stream this
user consumes with explicit ack (unscoped `$JS.ACK.>` is a cross-principal +TERM), and
`$JS.FC.>`. Reads go through `DIRECT.GET` and not `STREAM.MSG.GET`; the provision script
sets `--allow-direct` on every stream so nats.go picks that route, and only that route
is granted.

**Static is the answer here, not a residue.** `bridge` replaced the shared `worker` user
rather than inheriting it, and it stays a password principal on purpose. The auth
callout keys its map on the username TokenReview returns, which names a ServiceAccount;
a sidecar shares its pod's ServiceAccount, so a projected token would resolve the bridge
to the same map entry as the `agent` principal in the container beside it and hand each
of them the union of the two grant sets — which is the `worker` user rebuilt under a new
name. The callout cannot see which container opened a connection, and `Narrowing` is
pod-scoped, so no map shape available today separates them. The bridge gets a token when
it stops sharing a pod with the agent, which is the same event that retires it.

What the split bought, measured from this side: the bridge holds no grant on
`TOPICS-STATE` or `TOPICS-JOURNAL` at all — not the reads and not the writes. The
blackboard belongs to the `a2a` CLI in the agent container, which is now its own callout
principal. `TestBridgeJetStreamGrantOnARealServer`'s refused table is where that is
measured.

### Migrating an existing sidecar

An install whose `spec.deployment.sidecars` entry still names the retired user fails
closed rather than quietly: `worker` is gone from the rendered `nats.conf`, so the
sidecar's connect is refused at authentication and the container crash-loops. Because it
shares the agent's pod, the pod does not reach Ready — the same failure shape the
`mode: today` flip produces above. Two edits, both in the sidecar's own `env`:
`NATS_USER` becomes `bridge`, and `NATS_PASSWORD`'s `secretKeyRef.key` becomes
`bridge-password`. The Secret is the same `<agent>-a2a-nats-creds`; the operator fills the
new key on the next reconcile. It does not remove the old one: `ensureA2ACredsSecret` only
fills keys that are missing or empty and never prunes, so `worker-password` stays in the
Secret of an upgraded install indefinitely. It is dead data rather than a live credential —
`worker` is no longer a user in the rendered `nats.conf`, so presenting that password
authenticates to nothing — but the key's presence is not evidence the sidecar has been
migrated, and a reader checking whether an install has taken the split should read
`nats.conf` or the sidecar's `env`, not the Secret's key set.

Both edits are in `env`, and that is the supported route on purpose. A `sidecarVolumes`
entry that mounts `<agent>-a2a-nats-creds` — or the `<agent>-a2a-nats-config` or
`<agent>-a2a-callout-keys` Secret, or a projection of the `a2a-bus` audience under any
name — is refused at admission, and stripped from the render on an install running the
A2A surface, because a volume hands a second container far more than the `bridge`
principal's one password. `<agent>-a2a-nats-creds` and the `nats.conf` in
`<agent>-a2a-nats-config` both carry `sys-password`, which is the `$SYS` account, and
`<agent>-a2a-callout-keys` holds the issuer seed the auth callout signs with. The
agent's own credential is in none of them: under the callout the `agent` principal has
no shared secret at all, and the `a2a-bus` audience projection is the only route to it
as a credential. The seed is a way to mint one, which is the other reason that Secret is
not something to hand a sidecar.

A third edit is owed only by an install that overrode `BRIDGE_PROFILE`, and its failure
lands in an unhelpful place. The retired `worker` user's subscribe grant was
`a2a.tasks.*.*.in` — the addressee position was a wildcard, so pointing the bridge at
another addressee just worked. `bridge`'s grants name `platform` literally
(`a2aBridgeAddressee`, which is also `defaultProfile` in the bridge's own `main.go`: one
value living in two modules that cannot import each other). Override the env now and the
intake half still works — the consumer is created and pulled over `$JS.API`, where the
filter subject rides in the request body and no subject grant sees it — so the other
addressee's task is delivered. It stops there. `accept` publishes `submitted` on
`a2a.tasks.<other>.*.events` before it puts anything on the worker queue, and that subject
is not in the publish list, so the publish is refused, the submission is dropped, and
Hermes is never spawned. The refusal does not read as one: a rejected JetStream publish is
a reply that never arrives, so the bridge logs a timeout and the submitter waits on a task
that got no terminal event and was never run. Leave the env unset, or widen the grant in
the operator to match — the two have to move together.

The agent container is the other half of the same change and needs no edit: the operator
stops rendering `NATS_USER`/`NATS_PASSWORD` there and mounts a projected token instead.
One user-visible consequence — topic entries the `a2a` CLI writes now carry
`from.session` of `agent` rather than `worker`, so a query matching on the old value
returns nothing for entries written after the upgrade.

## Lifecycle, steering, cancel

Per task: `submitted` on accept (before the consumer ack, so a bridge death before the
ack just redelivers), `working` when the subprocess spawns, the stdout as a `result`
artifact (chunked if large), one terminal `status-update` with `final: true`. A nonzero
exit is terminal `failed` with the exit code and a stderr tail in the status message. A
submission with no text parts is terminal `rejected`. New-task detection is the
dispatcher's rule, and 9/9 widened it: BOTH event subjects empty means new, not `…events`
alone (profiles spec). The bridge satisfies that without a change of its own, because it
asks `lib.TasksGet` rather than reading a subject - and `tasks/get` folds `…events` and
`…supervisor` together, so a platform task carrying a supervisor terminal and nothing on
`…events` comes back `final` and is acked with a warning, not run again. That matters here
because the gateway's supervisor grant is an addressee wildcard, so such a task is
constructible. The component that does NOT get this for free is the worker adapter, whose
`priorEvents` deliberately replaced `lib.TasksGet` with a consumer on its own `…events`
(a session's grants reach neither `STREAM.INFO` nor a get-by-subject) and so cannot see a
terminal its own predecessor's supervisor declared. Anything on `…in` for a task with a
terminal event is acked with a warning and nothing else.

**Steering:** `hermes chat -Q -q` is one-shot - there is no stdin to inject into. A
follow-up message to a running task is acked and answered with a non-final status
echoing the task's current state (`working` once the subprocess spawned, `submitted`
while still queued) whose message says the input cannot be absorbed mid-run and cancel
is available.
Honest, never silent. This does not change task state (payload spec assertion 12).

**Cancel:** SIGTERM to the subprocess's process group, SIGKILL after a grace period,
then terminal `canceled`. A task racing to completion may land `completed` first - both
orders are legal and the terminal event wins. A per-task deadline (default 7200s,
matching the profile's `activeDeadlineSeconds`) takes the same kill path and lands
`failed`.

## Supervision

The bridge finalizes its own orphans, which is not the same as being their supervisor.
The ratified split says every task's supervisor is the component that spawned its
execution; the bridge spawned its own execution, so there is no separate supervisor to
be - and no supervisor WRITE either.

**It is not a supervisor principal, and 9/9's subject split does not move it.** The
gateway spawns session pods and finalizes tasks it did not execute, so it publishes on
`…supervisor`, a subject no executor reaches. The bridge is the executor. When it
finalizes an orphan it is finishing its OWN task across a restart, as itself, so its
terminal stays on `…events` where every other event it writes goes, and its `from`
agrees with that subject like any executor's. Profile-addressed tasks have no
supervisor ROLE until the dispatcher lands - though the gateway's rendered grant is an
addressee wildcard and reaches their `…supervisor` regardless, which the profiles spec
spells out; that is a gap the profiles
spec names, not one this sweep fills. Two failure classes:

- **The subprocess dies under a live bridge.** The runner sees the exit and publishes
  terminal `failed` with the evidence. Ordinary executor path, nothing special.
- **The bridge dies mid-task.** The submission was already acked, so nobody redelivers
  it, and no terminal event exists. The bridge keeps an in-flight registry in the
  `runtime-state` KV bucket (key per accepted taskId, written before `submitted`,
  deleted after the terminal publish). On startup, before consuming, it sweeps: fold
  each registered task's events, and for any non-final one publish terminal `failed`
  (`reason: bridge-died-without-terminal-event`).

The sweep's publish is a compare-and-swap per the profiles spec, not a read-then-write:
expected-last-subject-sequence pinned to the last event the fold saw. A dying
subprocess's flush racing the sweep wins cleanly, the CAS is rejected, and the sweep
re-reads instead of double-finalizing. Whichever writer loses lands in the
warn-and-drop path like any other post-final event.

That still holds here after 9/9, and it is worth saying why, because the profiles spec
records that the same CAS stopped protecting the dispatcher's janitor on that date.
Expected-last-subject-sequence is per subject. The janitor's terminal moved to
`…supervisor` while the executor kept writing `…events`, so the two writers stopped
sharing the subject the CAS is evaluated on. The bridge's sweep reads and writes the
same `…events` subject as the runner it is racing, so the racing write still
invalidates the expectation and the server still refuses the loser.

The sweep assumes incarnations are serial. That assumption is real on this install -
the kubelet restarts the sidecar container in place, and the operator renders the agent
Deployment with strategy `Recreate`, so two bridges never run at once. It is an
assumption, not a mechanism, and it has a named boundary: `Recreate` is the render's
default only while replicas is 1 - `spec.deployment.availability.replicas` above 1
switches the strategy to RollingUpdate (`resolveDeploymentReplicasAndStrategy`,
`manifest_helpers.go`), and an overlapping incarnation could then sweep-fail a task
its predecessor is still running. Real executor fencing belongs to the stage-3
dispatcher, not to scaffolding with a demolition date.

Honest gaps, accepted for the playground: no queue-staleness guard (the lib's subscribe
path doesn't expose server ingest timestamps, and `queueTimeoutSeconds` is the
dispatcher's job when it exists), no heartbeats on `agents.hb.>`, a submission whose
events lookup fails transiently is dropped with a log line rather than redelivered (the
lib acks unconditionally after the handler; a nak path is a lib delta if it ever bites),
and a terminal publish that fails outright - a bus outage outlasting the finalize
budget at exactly that moment - leaves the task in the registry for the NEXT
incarnation's sweep, which may be far away on a healthy sidecar; until then the bridge
holds the task as done while the stream shows no terminal event, and a later cancel
cannot unstick it.
All retire with the bridge. One more: zombie reaping. The operator deliberately leaves
`ShareProcessNamespace` unset (`platformagent_manifests.go`, the pod-template comment)
so no container hands its `/proc/<pid>/environ` to its neighbours. The bridge binary is
therefore PID 1 of its own container, orphans escaping the group kill reparent to it,
and Go's `cmd.Wait` reaps only direct children - zombies can accumulate until the
container restarts. Accepted for the playground with the rest; the stage-3 dispatcher
runs executions as Jobs, where the problem does not exist.

## Definition of done

A task published to `a2a.tasks.platform.<id>.in` on the W6 install returns the platform
agent's real answer as a `result` artifact. Cancel works. The event sequence passes
the lifecycle conformance assertions (9, 10, 12, 13, 14, 15, 18), table-driven like the
`a2a/lib` conformance suite. Two of those changed meaning on 9/9 without changing number:
9 gained the supervisor-only-terminal exception and 10 now spans both event subjects, and
the bridge's own tests still assert the single-subject form. They pass because a
platform task has no supervisor writing to it in practice, not because they cover the
new wording - so do not read a green bridge suite as coverage of the split.
