# Self-Improvement — kube-agents as its own subject

> **STATUS — implemented, off by default.** The runner lives in
> [`agents/selfimprove/`](../../agents/selfimprove/), the manifests in
> [`charts/kube-agents/templates/self-improvement.yaml`](../../charts/kube-agents/templates/self-improvement.yaml),
> and the Google identity in
> [`terraform/modules/kube-agents-selfimprove/`](../../terraform/modules/kube-agents-selfimprove/).
> `selfImprovement.enabled` defaults to false and `mode` defaults to `report-only`, so an install
> that does nothing gets nothing. This document describes what is built; where a part of the design
> is deliberately not built, the section that owns it says so.

**Scope:** A disabled-by-default hourly investigation of kube-agents itself — its source, its
harness, and the installation it is running in — which grades what it finds and, above a
configurable bar, opens a pull request against this repository.

**Owns:** the isolation boundary between that investigation and the agent it observes; the evidence
sources it may read; the severity and frequency gate; and the GitHub identity it files under.

---

## 1. The distinction this turns on

kube-agents already runs continuous audits. The roster in
[`agents/platform/cron/jobs.json`](../../agents/platform/cron/jobs.json) schedules a compliance
audit, an obtainability audit, a fleet-wide cost analysis and their siblings; each drives the
`fleet-audit` skill, and each produces findings about **the clusters under management**. The agent
is the observer and the customer's infrastructure is the observed.

This feature inverts that. The observer is the same harness; the observed is kube-agents — the
Python under `agents/`, the operator under `k8s-operator/`, the chart under `charts/`, the Hermes
harness the image is built on, and the behaviour all of it exhibits in the pod it is running in
right now. Nothing about a customer's cluster is in scope, and nothing the loop concludes can reach
one.

|                       | Fleet audits (shipping)                                | Self-improvement (this document)                                                             |
| --------------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Subject               | the managed clusters and the customer's GitOps repo    | kube-agents' own source, harness, and running installation                                   |
| Evidence              | the live GKE and Kubernetes APIs, billing, GitOps tree | the agent's own logs, traces, stores, CRs, and this repository's history                     |
| Output goes to        | the customer's GitOps repository                       | the ledger in every mode, plus a pull request against `github.upstreamRepo` under `upstream` |
| Who reviews it        | the cluster owner                                      | this repository's maintainers                                                                |
| Worst case from a bug | a wrong change proposed against a customer's cluster   | a pull request nobody merges                                                                 |
| Cadence               | the roster in `agents/platform/cron/jobs.json`         | hourly, off unless switched on                                                               |
| Identity              | `platform-agent-scope`, the GitOps repo                | a robot account's personal access token, held by the loop alone (§6)                         |

Three consequences follow immediately, and the rest of the design is mostly working them out.

**The loop is inside its own scope.** "kube-agents' own source" includes `agents/selfimprove/` — the
runner, the ledger, the evidence CLI and the two skills this document specifies. The loop
investigates the pod it is running in, and the code it finds there is its own as much as the
operator's, so a defect in the investigator is a finding like any other and reaches a maintainer the
same way. Nothing marks the loop's files as the loop's: a finding against `agents/selfimprove/` is
graded and filed on the same rules as one against the operator.

One boundary bounds this, and §8 states it as a refusal rather than a preference: the filing agent
will not open a pull request that changes the loop's own gate, ledger, or grants. A loop that files
patches to its own investigator improves; a loop that files patches to the thing deciding what it is
allowed to file has no ceiling.

The boundary is on the fix, not on the finding, and the difference is easy to lose because the
ledger sits on both sides of it. `selfimprove_ledger.py` is the gate as well as the ledger, so it is
a module the investigator is asked to audit and forbidden to patch. Both instructions are meant: it
writes the finding, grades it honestly, and the filing turn then declines it permanently, leaving it
in the ledger for a human. What makes that survivable rather than an hourly tax is that the refusal
is recorded — `record_refusal` marks the finding and the gate stops offering it, charging nothing
against the day's budget, because nothing reached anyone's review queue. Without that the gate would
promote it again every hour, spending a filing turn each time to reach the same answer.

**The reviewer is not the operator.** A fleet-audit finding lands in front of the person who runs
the cluster it is about. A self-improvement finding lands in front of kube-agents maintainers, who
may have no relationship with the install that produced it. An install therefore cannot be opted
into publishing on its behalf: the default mode files nothing anywhere, and reaching the upstream
repository takes a token the operator has to create, scope and mount deliberately.

**A finding is not an incident.** The loop never pages, never posts to the home channel unless
asked to, and never opens a kanban card on the board the Platform Agent works from. Its output is
a durable artifact read on someone else's schedule. Delivery paths that exist to interrupt a human
are the wrong shape for it, and reusing them would put self-referential noise into the channel
where cluster incidents arrive.

## 2. What "the code base that is currently deployed" means

The investigation begins by cloning the source the pod is actually running. Getting that wrong
makes every downstream conclusion unfalsifiable — a finding written against `main` about a pod
running a three-week-old image describes code that is not there.

**The image carries its own commit, because this feature stamps it.**
[`deploy/docker/Dockerfile`](../../deploy/docker/Dockerfile) takes an `ARG GIT_SHA` and writes it
both to an `org.opencontainers.image.revision` label and to `/opt/build-info.json`, whose whole
content is `{"revision":"<sha>"}`. The label is what registries and scanners read; the file is what
the runner reads, and they are two instructions rather than one so that a gate checking only the
label cannot stay green while the file is missing. Nothing else in the image names its commit —
`.dockerignore` excludes `.git`, so the build context does not contain the metadata, the operator omits `app.kubernetes.io/version` for want of a
build-time version to report (`manifest_helpers.go:80`), and `AgentStatus` records phase, address,
replicas and endpoints but no image, tag, digest or version.

The stamp is the only edit this feature makes to a _runtime_ path outside its own files. It sits at
the end of the stage, so a changing sha rebuilds the one instruction that writes the file and
nothing above it, and both publish workflows pass the argument — `GIT_SHA=${{ github.sha }}` in
`docker-publish-ghcr.yml`, `_GIT_SHA` through Cloud Build in `docker-publish-gcp.yml`.

**There is no registry-digest fallback.** The commit could instead be resolved by reading the
runner's own image digest and taking the 40-hex tag that shares it, since release images are
retagged rather than rebuilt (`docker buildx imagetools create` in `scripts/release/common.sh`).
That is not built: it buys a registry read grant the runner needs for nothing else, and it still
resolves to nothing on the dev-rebuild path, which tags from `IMAGE_TAG` in `vars.sh` and maps to
no commit. Instead an unstamped image refuses to investigate. `selfImprovement.allowUnstampedImage`
overrides that: the run then reads source at `main`, which may not be the code the pod is running,
and the brief instructs every finding to say so.

**Either way the runner reads the answer off itself.** It is scheduled with the same image
reference the agent Deployment is running, so whatever identifies its own filesystem identifies the
deployed code by construction, with no privileged read of the agent's container. Before anything
else it compares its own image reference against the live Deployment's — listing Deployments is in
the `view` role — and aborts on a mismatch, which means the agent was rolled and the CronJob was
not. The operator solves the same problem the same way, reading its own Pod from the API to set
`OPERATOR_IMAGE`, so this is a pattern the codebase already has.

The comparison has three outcomes, not two, and the third is the one worth stating. When either
image reference cannot be read — the Deployment renamed, the list call denied, the pod's own
reference absent — the run does not abort. Aborting would make an RBAC change or a rename silently
switch the loop off, which is a worse failure than investigating: it looks identical to a loop that
is running and finding nothing. So the run proceeds, and carries `unverified` with the reason into
the ledger row and into the brief, which instructs the investigation to repeat it in any finding
that cites a line number — the findings for which "which revision was that read at" is the
question. A pull request built on one says on its face that nobody confirmed the pod was running
the source it quotes.

**The harness is not a clone, and assuming it is would be the subtler mistake.** Hermes is not
vendored and not checked out: it arrives as the prebuilt `docker.io/nousresearch/hermes-agent` base
image pinned in [`tags.env`](../../tags.env), and the build then rewrites its Python source in place
at `/opt/hermes` — `deploy/docker/patches/*.py` make anchored literal and AST edits through
`patchlib.py`, and twenty of them are applied in a mandatory order. A clone of the upstream tag is
therefore _not_ the harness that is executing; it is the harness before this repository got to it.

The executing harness is `/opt/hermes` on the runner's own filesystem, patches already applied, and
that is what the runner reads. Attribution — which behaviour is this repository's and which is
upstream's, and therefore whether a finding belongs in `deploy/docker/patches/` or to Nous
Research — is answered from the patch series itself. The runner reads the pin from the fetched
`tags.env` and hands the agent [`deploy/docker/patches/`](../../deploy/docker/patches/), this
repository's own record of every deliberate divergence from stock Hermes. Cloning the upstream tag
and diffing `/opt/hermes` against it is not built, and would answer a different question: a diff
says what is different, most of which is intended, where the patch series says what this repository
changed and why. The cost is that an _undocumented_ divergence — a Hermes behaviour no patch here
explains — is invisible to the loop. Several signals in §4 originate in the harness — an agentic
loop taking a turn it did not need is usually the harness's scheduling, not a kube-agents skill — so
getting this attribution right is most of the value of looking at the harness at all.

## 3. What it is allowed to look at

The rule the user set is "inspect, but do not modify". Making it mechanically true rather than a
matter of the agent's good behaviour means being specific about each source and, where a source is
unreachable without breaking the rule, saying so instead of quietly reaching for it.

### 3.1 Logs

The agent writes its logs to files on the data volume, not to stdout, which is why `kubectl logs`
on the agent container shows so little. `buildFluentBitConfigMap` in
[`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)
tails `/opt/data/logs/*.log`, stamps each record with `log_source: agent-file`, and prints it to
stdout as `json_lines` — from where GKE's node agent ships it to Cloud Logging.

That is the whole log-access story for an isolated runner: it queries Cloud Logging with
`roles/logging.viewer`, filtered to the install's namespace on every query and to
`jsonPayload.log_source="agent-file"` when `--agent-files` asks for the agent's own files. It never
mounts the data volume, never execs into the pod, and gets the operator's and the credential
proxy's container logs from the same place. The sidecar is already deployed on every agent
pod, so no change to the observed system is needed to make its logs readable.

### 3.2 Traces and metrics

`hermes_otel` is enabled in the _observed_ agent's profile and its endpoint is pointed at the
collector by the entrypoint; the runner's own profile enables no plugins (§10). Latency findings —
the third signal class — come from the agent's spans, not from log timestamps:
a span tree shows which tool call in a turn consumed the wall clock, which a log line cannot.
`roles/cloudtrace.viewer` and `roles/monitoring.viewer` on the project are enough, and both are read
roles. Not `roles/cloudtrace.user`, which reads as the natural choice and is not read-only: it
carries `cloudtrace.tasks.create`, `cloudtrace.tasks.delete` and `cloudtrace.traceScopes.create`
/`delete`/`update`. `viewer` gives up only `resourcemanager.projects.get`, which nothing here needs
— the project id comes from `GCP_PROJECT_ID`, which the chart sets on the container.

Both grants are project-wide, which §3.3 avoids for Kubernetes by binding `view` with a
`RoleBinding` and cannot avoid here: neither API scopes a read below the project. So the boundary
moves into the query. `logs` carries a namespace clause and validates the caller's `--query` so it
cannot escape the parentheses around it. `metrics` does the same with the cluster: a
`kubernetes.io/` filter that does not already name a cluster is ANDed with
`resource.labels.cluster_name`, and because that label is absent from the resource types behind
other metric families — where naming it would fail the whole request rather than narrowing it —
rows carrying some other cluster's name are dropped after the fetch as well. Both halves of that
read `GKE_CLUSTER_NAME`, which the chart sets on the container; an install that empties it gets
neither, so the metric read widens to the project. No flag turns either half off — `--include-self`
widens the self-exclusion in §10 and nothing else.

### 3.3 The Kubernetes API

`view` on the release namespace, bound with a `RoleBinding` rather than a `ClusterRoleBinding`, plus
`get`/`list`/`watch` on `platformagents.kubeagents.x-k8s.io` and `agentplugins.kubeagents.x-k8s.io`.
That covers the CR and its `.status`, the Deployment and its env, the ConfigMaps, events, and pod
state — enough to find a container that has been restarting, an env var that never made it out of
the CR, or a condition that has been `False` for a week.

`view` deliberately excludes Secrets. Three verbs are excluded on top of it and are worth naming so
a later widening has to argue against something: `pods/exec` and `pods/attach`, because exec into
the agent container is arbitrary code execution as the agent and no amount of intent makes it a
read; and `pods/portforward`, for the same reason one step removed.

Kubernetes RBAC is doing more work here than it does for the agent, and the reason is worth stating.
The agent's KSA permissions are not the binding constraint on a _managed_ cluster, because the agent
authenticates to those clusters as its Google service account via
`gcloud container clusters get-credentials` — the KSA grant governs the install's own namespace and
little else. The runner has no such escape hatch: its GSA holds logging, trace and monitoring read
roles and no GKE roles at all, so `get-credentials` fails for every cluster in the project,
including the one it is running on. There is no managed cluster it can reach by any path.

### 3.4 The SQLite stores

This is the source the loop most wants and the one it cannot have cheaply. Session history, the
kanban board and the OTel live store are SQLite files on the agent's data volume, and they hold
the evidence for the signal classes that logs answer worst — how many turns a task took, which
cards ended `blocked` and never recovered, which cron deliveries recorded a `last_delivery_error`.

Three ways to reach them. The third is the right one and **is not built**: what ships reaches
none of these stores, and the shipped loop infers session and board behaviour from logs, events,
metrics and spans. The rest of this section is the design of record for the follow-up.

- **Mount the volume read-only from the runner.** Not available. At the default single replica the
  operator creates the claim `ReadWriteOnce` (`platformagent_manifests.go:126`, and
  `defaultAccessModes` at `:142`), so a second pod on a second node cannot attach it at all. Above
  one replica `getDefaultStorageConfig` switches it to `ReadWriteMany` (`:134-136`) and that
  obstacle goes away, which changes nothing here: a second pod attaching a live SQLite database
  with an active WAL is how these files get corrupted rather than read, and that holds on either
  access mode.

- **Exec into the agent and query in place.** This is how a human operator reads these files, and it
  is the reason to name it rather than pass over it. Rejected in §3.3: exec into the agent container
  is arbitrary code execution as the agent, which holds the minter path, the GSA token and the model
  credential, and the loop is the least-trusted component in the install to hand that to. It is also
  a write — opening a WAL database requires creating or writing the `-shm`, so "just reading" the
  live file modifies it, and on the 9p mount under gVisor that is the documented way these files get
  corrupted.

- **Snapshot the volume and mount the restore.** A `VolumeSnapshot` of the claim, restored into a
  throwaway claim the Job mounts, gives a crash-consistent point-in-time copy that can be opened
  without the live file ever being touched. It is the only one of the three that is genuinely
  non-invasive to the observed system.

  Open the restore read-write, not with `immutable=1`. The snapshot captures the database and its
  `-wal` together, and `immutable=1` disables change detection and ignores the `-wal` — which
  returns the state as of the last checkpoint and silently drops every commit since, exactly the
  recent activity a run is looking for. The copy is a throwaway, so letting SQLite replay the WAL
  into it costs nothing and is the only way to see a current board.

It is also the one place the loop would create cluster objects, so when it is built it belongs
behind its own flag, off even when the feature is on: an install that turns it on consents to two
objects per run, in its own namespace, garbage collected at the end of the run. There is no such
flag today, the loop's Role carries no `volumesnapshots` or `persistentvolumeclaims` verb, and the
evidence tool has no subcommand that opens a database — a knob that silently does nothing is worse
than an absent one, because an operator who sets it believes they have consented to something.
Adding the path later is additive: a flag, two objects per run, and a mount, disturbing nothing that
is here. The consequence of its absence is the one §11 names — findings about session history,
kanban state and cron delivery are inferred from logs and spans rather than read from the stores,
and the row-level questions in §4 that need those files stay unanswerable.

### 3.5 The repository

The source from §2, plus the public GitHub API: open issues and open pull requests. A finding that
duplicates open work is noise, and the check is the same one `AGENTS.md` requires of a human
contributor before starting a task. Read access here is anonymous, which is what lets the check run
in `report-only` as well — `self-investigation/SKILL.md` §5 makes it a step of the investigation
rather than of filing, because in the default mode there is no filing turn to do it in. The
credential in §6 is for writing.

Local history is not available to it. `report-only` extracts a tarball, which has no `.git` at all,
and the filing modes fetch at depth 1 — both checkouts, the investigation's and the base one §10
describes — so "what changed recently" has to come from the API too if a finding needs it. That
includes the difference between the two trees: they are two unrelated single-commit clones, so
`git diff` across them is not available and the skill has the turn read the same file in each
instead.

## 4. The signals

The seven classes below are the ones the loop looks for. The value of the table is the middle
column: a signal with no stated evidence source is an invitation to speculate, and a
self-improvement loop that speculates produces pull requests that waste maintainer attention faster
than it saves it.

| #   | Signal                                      | Where the evidence is                                                                | What a finding must show                                                                    |
| --- | ------------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| 1   | Errors                                      | `log_source: agent-file` at ERROR, container stderr, CR `.status` conditions, events | The stack or message, the code path in the cloned revision, and how often it fired          |
| 2a  | Inefficiency — missing permission or tool   | Denials from `command_policy.py`, `gcloud`/`kubectl` 403s, `command not found`       | The refused call, whether the refusal was correct, and the cost of the retry loop it caused |
| 2b  | Inefficiency — unneeded loop or turn        | Span trees; turn counts per session; repeated identical tool calls in one session    | The turn that added nothing, and what in the prompt or harness produced it                  |
| 3   | Latency                                     | Span durations, p50/p95 per tool and per skill, cron run durations                   | The span that dominates, compared against the same span in earlier runs                     |
| 4   | Wrong, inaccurate or missing user responses | Sessions ending without a delivered reply; kanban cards `blocked` with `result` NULL | The session, what was asked, what was returned, and where the reply was lost                |
| 5   | Failed chat delivery                        | `last_delivery_error` on cron jobs; the delivery paths in the credential proxy       | The target that failed, the platform, and whether the target was resolvable at all          |
| 6   | Failed issue or PR creation                 | Minty refusals in the proxy's own log; `gh` exit codes; the resolver's reason codes  | The HTTP status, the scope requested, and whether the App had the permission                |
| 7   | Anything else                               | any of the above                                                                     | The same bar: evidence first, then a claim                                                  |

Two of these are worth a note because the repository already knows they are hard. Signal 5 has a
known shape where an alert is delivered on one platform and its report on another; signal 6 has a
known shape where a scopeless repository produces an HTTP 500 from the minter rather than a
recognisable refusal. A loop that rediscovers a known-open issue should say so and link it, which
is what the duplicate check in §3.5 is for.

## 5. Architecture

### 5.1 Three shapes that do not work

The isolation requirement rules out the obvious placements, and each rules itself out for a
different reason worth recording.

**A cron job in the Platform Agent's own profile.** This is how every existing audit runs, and it
would be about twenty lines: an entry in `agents/platform/cron/jobs.json` and a skill directory.
It fails the requirement completely. The job would run as the agent's service account, with the
agent's kubeconfig, the agent's GitHub token and the agent's model budget, on the agent's data
volume. An investigation into why the agent is slow that competes with the agent for its own
resources is not an investigation, and the finding "the agent's credentials are over-scoped" would
be written by something holding those credentials.

It also cannot be switched off. Nothing in the `PlatformAgent` CRD or in `values.yaml` adds, edits
or disables a cron job: the roster is baked into the image and merged onto the volume by
`profile_scaffold.py` at pod startup, and the merge rule is that the image wins every key it ships.
An entry in that file is on for every install that pulls the image, which is the opposite of the
disabled-by-default requirement, and the only runtime route to changing it is the `cronjob` tool.

**An `AgentPlugin`.** The right instinct — [`agentplugins/README.md`](../../agentplugins/README.md)
describes exactly the kind of out-of-tree capability this is, and the CR mounts an OCI image into a
profile without touching `deploy/` or `agents/`. But the isolation an AgentPlugin provides is of
_source_, not of _runtime_: the code still executes inside the agent's process, under the agent's
identity, on the agent's volume, so every objection above still holds. It also cannot schedule
anything. The operator mounts the image and symlinks it into the profile; nothing merges a
plugin-supplied `cron/jobs.json` into the profile's cron store, so an `AgentPlugin` has no way to
run hourly without a new entrypoint step and a new mode on `profile_scaffold.py` — a change to the
shared boot path, which is what this feature is trying to avoid.

**A second `PlatformAgent` custom resource.** This would get a separate pod with a separate
identity and let the operator do the work. It is blocked by admission:
[`platformagent_webhook.go:130-154`](../../k8s-operator/internal/webhook/platformagent_webhook.go)
rejects a second `PlatformAgent` in the cluster with "only one PlatformAgent is allowed per
cluster". Relaxing a validating webhook so an optional, off-by-default feature can exist is the
wrong trade; the singleton rule is protecting something real about leader election and volume
ownership.

### 5.2 What it is

A Kubernetes `CronJob`, rendered by the chart only when the feature is switched on, running the
same agent image with a private Hermes home on an `emptyDir`.

```
CronJob  kube-agents-selfimprove          schedule 0 * * * *; not rendered at all when disabled
  └── Job (concurrencyPolicy: Forbid, backoffLimit: 0, activeDeadlineSeconds)
        ├── initContainer credential-proxy   restartPolicy: Always  ← native sidecar
        └── container     runner             the agent image, HERMES_HOME=/home/selfimprove
```

The run is: scaffold a private profile onto the `emptyDir`; fetch this repository at the deployed
revision; investigate; file what clears the gate; write the ledger; exit. There is no gateway, no
chat platform, no dashboard and no PVC. `hermes -z PROMPT --cli` is the invocation — a headless
one-shot turn that takes the brief on the command line — and the investigation is one or more of
those, because the harness caps a headless turn at 90 model calls and §10 explains what the runner
does about it. Not `hermes cron tick`, which is the other invocation that runs a turn to completion
without a gateway: it requires a cron store on the profile holding one job contrived to be always
due, which is state to scaffold and a second scheduler underneath the Kubernetes one deciding
whether the run happens. With `hermes -z` the CronJob schedule is the only schedule and the brief
the runner composed is provably the brief that ran.

**How the source arrives depends on the mode.** Under `report-only` the runner fetches
`codeload.github.com/<repo>/tar.gz/<sha>` and extracts it through a path-traversal loop of its own
plus `filter="data"` — the filter argument falls back to a plain `extractall` on a Python that does
not accept it, which is why the loop is there rather than being replaced by it. The mode needs one
immutable tree at one commit, not history, and this way the fetch is a public anonymous HTTPS GET
with no credential and no `git` binary, which is what lets the mode keep both off the pod. `fork`
and `upstream` take a real checkout instead — `git init` plus a depth-1 fetch of the deployed
commit, `origin` pointing at the upstream repository and `fork` at the push target — because
evidence a finding cites is easier to trust from a tree whose provenance `git` can state, and the
shims are on the `PATH` in exactly those two modes, so the clone costs no credential the mode does
not already have. Depth 1 means `git log`, `git blame` and `git merge-base` see a single commit
inside that checkout; the filing skill says so, because the alternative is an agent drawing
conclusions from history that is not there. A checkout that fails at any step degrades to the same
anonymous tarball rather than failing the run: filing is unaffected, because `fetch_base_checkout`
(§10) fetches the filing tree independently at the base tip, so the fallback costs `git`-backed
evidence in the investigation tree and nothing else. It is a line in the Job log, and the run's
brief does not tell the investigation that `git log` and `git blame` will not work in its checkout.

**The credential proxy, where a mode needs one, must be a native sidecar** — declared as an
`initContainer` with `restartPolicy: Always`, not as a second `containers` entry. A long-running
container in an ordinary `containers` list never exits, so the Job never completes and
`concurrencyPolicy: Forbid` blocks every subsequent run forever. This is a one-word difference in
the manifest with a failure mode that takes a day to diagnose.

It renders only for `fork` and `upstream`. `report-only`'s guarantee is that nothing the loop
produces leaves the cluster, and the way to mean that is for the write path not to exist rather than
to be present, permitted, and relied on not to be called. The same conditional governs the shim
directory on `PATH`, so under `report-only` there is no `git` and no `gh` on it. Nothing else in the
run needs the proxy: telemetry reads call the Logging, Trace and Monitoring REST APIs over `urllib`
with a metadata-server token for the investigator's Workload Identity — no Google client library is
involved, and the evidence CLI's one third-party import is `kubernetes`, deferred into the function
that uses it. (The image does carry `google-cloud-pubsub`, for the chat plugin; the loop does not
touch it.) Kubernetes reads use that in-cluster client against the pod's RBAC.

`concurrencyPolicy: Forbid` is most of the mutual-exclusion story and not all of it. It serialises
the CronJob's own Jobs, so two scheduled runs never overlap; it says nothing about a Job created by
hand or a `kubectl edit` of the ledger, and three concurrent writers is enough to lose a run's rows.
There is no lease. What covers the gap is that the ledger's own write is a compare-and-swap on the
`resourceVersion` its read observed, folding the other writer's rows in on a conflict rather than
overwriting them; §10 has the merge rules. A maintainer weighing a lease should weigh it against
that, not against `Forbid`.

This is the repository's only `CronJob` — the one other `batch/v1` object the chart renders is a
Helm pre-delete `Job`. The convention it follows is `githubMinter.enabled`: a chart-only standalone
workload, guarded on a values key, that the operator knows nothing about and that renders nothing
when the key is false.

### 5.3 The isolation ledger

| Resource         | Shared with the agent? | Why                                                                                              |
| ---------------- | ---------------------- | ------------------------------------------------------------------------------------------------ |
| Pod / process    | no                     | separate `CronJob`; the agent is unaware of it                                                   |
| Container image  | **yes**                | deliberate: it is how the runner knows the deployed revision (§2), and it adds no image to pin   |
| Kubernetes SA    | no                     | `kubeagents-selfimprove`, `view` on one namespace                                                |
| Google SA        | no                     | `kubeagents-selfimprove`, read roles on logging, trace and monitoring                            |
| Data volume      | no                     | `emptyDir`; the agent's `ReadWriteOnce` claim is never mounted                                   |
| GitHub identity  | no                     | a robot account's personal access token, in its own Secret (§6)                                  |
| Credential proxy | separate instance      | the same script and image, its own process, holding its own `gh` state                           |
| Minter           | **not reached at all** | no egress rule to its Service, and its ingress policy does not admit this pod's label            |
| Model endpoint   | **yes** by default     | the in-cluster LiteLLM Service; duplicating a gateway buys nothing. Overridable for budget split |
| Chat platforms   | no                     | the runner has no Slack or Google Chat credential and no home channel                            |
| Kanban board     | no                     | findings go to the ledger, not to the board the agent works from                                 |
| Telemetry        | no                     | `hermes_otel` needs egress to the collector namespace the runner's NetworkPolicy does not open   |

Only two entries are `yes`, and both are argued for rather than inherited. Everything the feature
adds is rendered by one new chart template, `templates/self-improvement.yaml`, guarded on
`selfImprovement.enabled`, so with the flag off it renders nothing and the install is byte-identical
to one from a chart that never had the feature.

**One label must not be copied.** The platform minter's ingress policy admits pods carrying
`kubeagents.x-k8s.io/has-credential-proxy: "true"`
([`github-minter.yaml:199-206`](../../charts/kube-agents/templates/github-minter.yaml)), and the
operator stamps that label on agent pods (`platformagent_manifests.go:2045,2109`). The runner pod runs a
credential proxy and so invites the label by analogy — and carrying it would let the runner reach
the platform minter and mint tokens for the customer's GitOps repository, silently undoing §6.
The runner is labelled `kubeagents.x-k8s.io/selfimprove: "true"` instead, which that policy does not
admit. It is belt to the braces of the runner's own NetworkPolicy rendering no egress rule to the
minter's Service: either alone would do, and the label is the one an operator can see in
`kubectl get pod --show-labels`.

### 5.4 Read-only, in three layers

- **RBAC.** `view` on one namespace, no Secrets, no exec. Nothing in the grant can mutate the
  agent, and nothing in it reaches another namespace or another cluster.
- **`command_policy.py`.** The runner's credential proxy runs with
  `CREDENTIAL_PROXY_ENFORCE_READ_ONLY` left at its default, which
  [`credential_proxy.py:1787`](../../agents/platform/scripts/credential_proxy.py) (`read_only_enforced`)
  reads as enforcing unless the value is literally `false`. As shipped the loop goes further than that: a
  `selfimprove.no-cluster-tools` rule refuses `kubectl` and `gcloud` outright rather than
  allow-listing them down to their read verbs, because the runner reaches Kubernetes through the
  in-cluster client and Google through its REST APIs over `urllib`, and needs neither binary. The
  module's own docstring is worth heeding — it is the only thing enforcing the posture, so a false
  allow is the whole control, not a redundant check.

  **The runner does not inherit the operator's guard on that variable**, and reading §5.3's shared
  proxy as though it did is the mistake to avoid. The agent's copy is defended twice — the name is
  in the operator's `SensitiveEnvVars`
  ([`common_types.go:49`](../../k8s-operator/api/v1alpha1/common_types.go)), so the webhook rejects
  it, and it is separately dropped at reconcile
  ([`platformagent_manifests.go:2548-2575`](../../k8s-operator/internal/controller/platformagent_manifests.go))
  because the chart defaults `failurePolicy` to `Ignore`. Both act on `PlatformAgent` CR env, and
  the CronJob is a chart template that never reaches the operator or its webhook. What keeps the
  variable unset here is the proxy's own default plus the chart's silence: `selfImprovement` has no
  env passthrough, so there is nowhere to write the variable in the first place. Adding one would
  remove the control, and nothing in the operator's code would say so.

- **The write credential cannot reach a cluster.** The runner holds no GitOps token and no
  kubeconfig for a managed cluster, so there is nothing it could push a manifest to even if the
  first two layers failed. That is narrower than "no mutating credential exists": under `fork` and
  `upstream` the sidecar holds a classic personal access token, and a classic token carries `repo` —
  write included — wherever the robot account can reach. What bounds it is the account, a robot that
  is a member of nothing else, and inside the pod the deny policy refusing every `gh` subcommand
  outside the six §6.3 names. `git` and `gh` are outside `command_policy.py` on purpose, and in the
  agent's case the workspace lease governs them. §6.4 sets out what the token costs and §11 records
  that a check on argv is not a permission boundary.

The exception, stated plainly rather than buried: the runner writes its ledger. That is one
`ConfigMap` in the install's own namespace, granted by `resourceNames` on a `Role` so the grant
cannot reach a second object, and it is the runner's own bookkeeping rather than any part of the
system under observation. Where §3.4's snapshot path is enabled, add two short-lived objects per
run to that list.

## 6. The loop's GitHub credential

The loop authenticates to GitHub as a robot account holding a classic personal access token. The
token is created out of band, lives in a Kubernetes Secret the chart never sees the contents of, and
is seeded into `gh` once at the credential proxy sidecar's startup. No GitHub App, no minter, no KMS
key, and no change to any code the Platform Agent runs.

### 6.1 Why not the existing minter, and why not a second one

[`templates/github-minter.yaml`](../../charts/kube-agents/templates/github-minter.yaml) renders one
minty config, keyed `<org>-<repo>.yaml`, holding one scope named `platform-agent-scope` whose rule
is `assertion.email in ['<platform GSA>']` and whose `repositories` list is the single GitOps repo.
A Terraform validation in `terraform/examples/full-install/main.tf` enforces the single repository.

Reusing it is out on two counts. Adding the upstream repository to the existing scope widens the
_Platform Agent's own token_ to reach `gke-labs/kube-agents` — a standing privilege increase for the
component that talks to customers, in exchange for a feature that is off by default. And the loop
needs a different GitHub App in any case: the existing App is installed on the customer's GitOps
repository, and opening pull requests against `gke-labs/kube-agents` requires an App installed
there, with a different app ID and a different private key.

A second minter of the loop's own is out on a third count, and it is the decisive one:
`upstream` mode cannot work through one. A second minty Deployment — Service, ConfigMap, KSA, GSA
annotation, KMS key reference and NetworkPolicy — plus a second GSA, a second import-only KMS key,
and a second GitHub App an operator creates, installs on two repositories and imports the private
key of, buys a credential that still cannot open the pull request. Under `upstream` the fork and the
base live under different owners, so they are different App installations issuing different tokens,
and `gh` stores one token per host: minting the second discards the first, the branch lands, and
`gh pr create` against the base fails. No amount of care in the templates fixes that.

A personal access token has no such problem. One classic token carries the account's `repo` scope
wherever the account has access, so the push and the pull request are the same credential. It also
means there is no second minter at all: no Helm template, no Terraform resources, no KMS key ring
that nothing can ever delete, and no operator step of registering a GitHub App and importing a PEM.

What it costs is set out in §6.4. The short version is that the token is coarser than an App
installation would be and nothing rotates it.

### 6.2 Seeding it, without touching shared code

The credential proxy sidecar runs `CREDENTIAL_PROXY_BOOTSTRAP_COMMAND` before it binds its socket
([`credential_proxy.py:2293`](../../agents/platform/scripts/credential_proxy.py), the
`executor.bootstrap` call in `main`), inside
`self.environment` — the same dict, carrying the same `HOME` and `GH_CONFIG_DIR`, that `_execute`
later runs every shimmed command in. That is what makes one line enough:

```sh
{ gh auth login --with-token < /var/run/secrets/selfimprove-github/token && gh auth setup-git; }
  > /home/selfimprove/.credential-bootstrap.log 2>&1
  || echo "credential bootstrap failed with exit $?; the filing turn will have no GitHub identity" \
     >> /home/selfimprove/.credential-bootstrap.log;
true
```

`gh` and `git` are authenticated from that moment, for the life of the pod, and the runner's own
`git push` and `gh pr create` find the credential already there. The path is not a constant: the
chart builds it from the mount directory and `selfImprovement.github.patSecretKey`, which is
`token` by default, so a Secret keyed on something else moves the filename.

Four details in it are load-bearing:

- **The stdin redirect.** `bootstrap()` runs the command with `stdin=subprocess.DEVNULL`, so
  `--with-token` has to read the token from a file rather than from a pipe.
- **`; true`.** A non-zero exit from the bootstrap command raises and kills the sidecar, taking the
  whole run with it. A missing or unreadable Secret should cost the filing turn, not the
  investigation — §6.3 is what turns it into a clean `SKIPPED`.
- **The log redirect.** `; true` throws the exit status away, and with it `gh auth login`'s account
  of _why_ the token was refused — the one message that distinguishes a missing scope from a
  revoked token. Sending both streams to a file on the shared workspace keeps it: the runner's
  `read_bootstrap_log` quotes the tail of
  `/home/selfimprove/.credential-bootstrap.log` under the preflight's own error, so the diagnosis
  arrives with the failure it explains. The `|| echo` covers the case where `gh` never ran at all
  and the file would otherwise be empty.
- **`defaultMode: 0440` on the Secret volume.** A Secret volume is owned `root:fsGroup` and both
  containers run as uid 10000, so `0400` is unreadable by the process that needs it. The pod still
  reaches 2/2: bash cannot open the stdin redirect, the `|| echo` records that, and `; true` leaves
  a healthy sidecar holding no credential.

It is still not the agent's copy of that variable, which runs `gcloud container clusters
get-credentials`. A kubeconfig for a managed cluster is precisely the credential this loop is
designed not to hold.

The Secret is mounted into the sidecar and **not** into the runner. `run_agent` strips the shim
directory from `PATH` and pops `CREDENTIAL_PROXY_URL` for every turn but the filing one, which
removes environment variables and nothing else — a file mounted into the runner would stay readable
by the investigation turn, which has no business holding a write credential.

`TOKEN_BROKER_URL` is left unset on this pod, and unset is not the same as pointing nowhere:
[`github_token_refresh.py:26`](../../agents/platform/scripts/github_token_refresh.py) defaults it to
`http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token`, the platform minter's
own Service. Two network facts are what actually stop the call. The runner's NetworkPolicy renders
no egress rule to that Service, and the platform minter's ingress policy admits only pods labelled
`kubeagents.x-k8s.io/has-credential-proxy: "true"`, which this pod deliberately does not carry
(§5.3). So nothing the loop runs can obtain an App token for a repository an operator never granted,
and leaving the variable unset is a signpost rather than the control.
[`github_token_refresh.py`](../../agents/platform/scripts/github_token_refresh.py) and
[`credential_proxy.py`](../../agents/platform/scripts/credential_proxy.py) are unmodified by this
feature, and so is the operator — the isolation is that the loop's credential path is entirely in
its own chart template and its own runner.

### 6.3 Proving the token before the turn is paid for

A credential seeded once at startup is never exercised again until the filing turn spends it, so the
run proves it first. `verify_forge_credential` in
[`selfimprove_run.py`](../../agents/selfimprove/scripts/selfimprove_run.py) runs immediately before
the filing turn starts:

- `gh repo view <push target> --json viewerPermission` must return `WRITE`, `MAINTAIN` or `ADMIN`.
  That is the same permission `git push` will be checked against.
- Under `upstream` mode, `gh repo view <base> --json viewerPermission` must succeed. Reachability
  only — opening a pull request from a fork asks nothing of the base beyond read, and requiring
  write there would refuse the exact configuration the mode exists for. The permission it returns
  is not discarded, though: it decides whether the filing turn is asked to label anything, for the
  reason below.

`gh repo view` rather than `gh auth status`, because `selfimprove.unlisted-gh-subcommand` in the
sidecar's deny policy allows `pr`, `search`, `issue`, `repo`, `version` and `help` and refuses
everything else. A preflight the policy blocks is a preflight that fails every run.

Both reads run with `cwd` set to the runner's home, and that is not incidental. The proxy refuses
any command whose working directory falls outside `CREDENTIAL_PROXY_WORKSPACE_ROOT`, which the
chart points at `/home/selfimprove`; the runner process does not start there, so a `subprocess.run`
that omits `cwd` hands the shim a directory the proxy rejects and the preflight reports a healthy
credential as unverifiable — `exited 1: working directory is outside the shared workspace`, on a
token with nothing wrong with it. The argument is required rather than defaulted so the next caller
has to answer the question.

The base repository's permission is read because opening a pull request and labelling one need
different things. Read is enough to open one — that is what a fork-based contribution is — and a
label is repository metadata, so attaching one needs `TRIAGE` or above. The two come apart in
exactly the configuration `upstream` mode exists for: a robot with `ADMIN` on its own fork and
`READ` on the repository it contributes to. A turn told to apply labels its token can never attach
finds out one refused `gh pr edit` at a time, and reports failure on a pull request that opened
successfully. So the preflight answers that question too, and the filing prompt drops to its
unlabelled branch when the answer is no. It is also what keeps the prompt's claim about labels
honest — that the token can attach an existing one and cannot create one is true only on the
installs that reach that branch. In `upstream` mode against a repository the robot does not help
maintain, the labels are unreachable by construction rather than by misconfiguration, and §8's
account of them below should be read with that caveat.

Before the turn rather than inside it, for two reasons. A credential that fails inside the turn
fails at `git push`, an hour of model budget after the point where the cause was knowable, and it
fails as `git` prompting for a username on a terminal nothing is attached to. And the runner can
report a credential failure as the loop's own fault — `SKIPPED`, no charge against the day's ceiling
and no cooldown — where a turn that dies mid-push is `UNCONFIRMED` and costs the finding a slot. Two
reads catch a mis-scoped token, a revoked token and a Secret that never mounted, in seconds rather
than an hour.

The preflight is also the only place any of those surface, which is what `; true` costs. A token
`gh auth login` rejects leaves a pod that boots 2/2 and says nothing on either container's stdout,
because the exit status the failure travelled in is zero by construction. What survives is the
bootstrap log, and the preflight is what puts it in front of a reader: on `gh`'s exit code 4 — its
dedicated "needed a credential and there is none", where every other failure is a 1 — the message
says the Secret named by `selfImprovement.github.patSecret` is empty, absent, or missing the `repo`
and `read:org` scopes, and `read_bootstrap_log` appends the tail of what `gh auth login` actually
printed. That tail is quoted as a claim rather than trusted: `/home/selfimprove` is also the
runner's `HERMES_WRITE_SAFE_ROOT`, so an investigation turn can write the file, and nothing
branches on its contents. `gh`'s own advice cannot be passed through unqualified — it tells the
reader to run `gh auth login` or set `GH_TOKEN`, and in this pod neither is reachable, the login
having happened in another container an hour earlier. A token missing its scopes is the common case
here, and a refusal that names the right remedy is the difference between a five-minute fix and a
hunt.

**There is no expiry story.** A personal access token does not expire partway through a turn, so
`fileTimeoutSeconds` is a share of the hourly schedule rather than a credential deadline, and the
filing prompt carries no refresher. It must not:
[`github_token_refresh.py`](../../agents/platform/scripts/github_token_refresh.py) dials a minter
this pod's NetworkPolicy gives it no route to, and a turn that ran it would read the resulting
failure as the credential being broken. The prompt says to retry once and then print
`SKIPPED: GitHub refused the credential`.

**The separate proxy instance is required, not merely tidy.** `gh auth login --with-token` caches
into the sidecar's private `hosts.yml`, and `gh` holds one token per host. Two identities sharing
one credential proxy would share one `github.com` entry and overwrite each other, so whichever
authenticated last would own both flows — the failure being that the Platform Agent's next GitOps
push runs as the loop's robot account, or the reverse. Separate proxies mean separate state
directories and no interaction at all.

### 6.4 What the token costs

Three things are worse than a per-repository App installation would be, and an operator weighing
`fork` or `upstream` mode should weigh these.

**It is coarser.** An App installation could be granted `contents: write` on the fork and
`pull_requests: write` on the upstream and nothing else. A classic token carries `repo` — read and
write on every repository the account can reach. The narrowing that remains is the account: use a
robot that is a member of nothing else, and its reach is the fork plus whatever public repositories
anyone can open a pull request against. Inside the pod, the credential proxy's deny policy narrows
what the token can be spent on, and the shape of that narrowing is what stays true as rules are
added: `gh` is admitted only for a named set of first subcommands, and further rules refuse
particular argv spellings inside them. It withholds what those rules enumerate and no more, so a
verb inside an admitted family that no rule names is permitted — which is why the policy is a
narrowing rather than a boundary. It narrows nothing about the token itself: the credential still
carries `repo` on every repository the account can reach, and the policy only decides which
spellings of that reach the network from this pod. What the rules do constrain is the destination:
`selfimprove.gh-target-allowlist` refuses a `-R`/`--repo` naming anything but the configured
upstream and fork, and `selfimprove.git-push-fork-only` refuses a push to any remote but `fork`, so
the two repositories an operator configured are the two a well-formed command can name. That is a
statement about argv and not about the credential — an unflagged `gh` call still acts on whatever
repository the checkout points at. The shipped rules are in
[`templates/self-improvement.yaml`](../../charts/kube-agents/templates/self-improvement.yaml) and
are the current answer to what the loop can do with the token; §11 records that a check on argv is
not a permission boundary.

**Nothing rotates it.** An App installation token would live an hour by construction. This one lives
until somebody revokes it. Its lifetime is an operator's to manage, and an install that never
revokes it is an install with a standing write credential in a Secret.

**Its permissions are not visible in the install.** A minty rule file would say, in the cluster,
exactly what the loop could do. A token says nothing about itself; `verify_forge_credential` reads
the permission back from GitHub at filing time, which is the closest thing to that and is a runtime
check rather than a reviewable declaration.

Against those: `upstream` mode works, which it cannot through an App (§6.1); there is no GitHub App
to register, install and keep installed; there is no KMS key that can never be deleted; and the
loop's credential path shares no code with the Platform Agent's, so a change to one cannot break the
other.

### 6.5 Fork topology and the three modes

Repository policy is that branches are pushed to a fork, never to `gke-labs/kube-agents`. A
cross-fork pull request needs write on the fork, to push the branch, and the ability to open a pull
request against the upstream — which any authenticated account has on a public repository. One
token covers both. The chart refuses a configuration where `forkRepo` equals `upstreamRepo` in fork
and upstream mode alike — compared case-insensitively, because GitHub slugs are and `eq` is not.
The deny policy constrains pushes to the remote named `fork`, but that name resolves to whatever
`forkRepo` says, so with one token carrying the same permissions everywhere this render-time guard
is what stops the loop pushing a branch to the upstream directly.

Because that is a real amount of GitHub administration, and because the operator running an install
is usually not a kube-agents maintainer, the destination is a mode rather than an assumption:

- **`report-only` (the default when the feature is enabled).** No GitHub credential, no
  credential-proxy sidecar, and no `git` or `gh` on the runner's `PATH`. Findings
  accumulate in the ledger and are read with `make selfimprove-ledger`. Everything in §§2–5 runs, and
  nothing the loop _produces_ leaves the cluster. That is narrower than "no egress", and the
  difference is worth stating: the run still fetches its own source over HTTPS, still calls the
  Google telemetry APIs, and still has web search enabled, so the NetworkPolicy allows outbound 443
  in every mode. What the mode removes is the write path and the credential that would make one
  usable. Most installs should stay here — the loop is still worth running, because the ledger is
  exactly the evidence a bug report needs.
- **`fork`.** Branches and pull requests go to a fork the operator owns. Useful for an install that
  wants the loop's output as a reviewable artifact without publishing to a repository it does not
  control.
- **`upstream`.** Cross-fork pull requests against `gke-labs/kube-agents`. This is the mode the
  project's own dogfood installs run, and it is the one that makes the feature's stated goal — the
  harness getting less buggy over time — actually happen.

Harness findings are the exception to all three. `nousresearch/hermes-agent` is a third-party
repository; the loop never opens a pull request there. A harness finding becomes a section in the
ledger and stops there, for a maintainer reading the ledger to decide whether to carry a patch in
`deploy/docker/patches/` or raise it upstream themselves.

**There is no issue-filing path**, for a harness finding or for any other. The credential is not
what stops it — a classic token's `repo` scope opens an issue. Two other things do. The sidecar's
`selfimprove.gh-issue-reads-only` rule admits `gh issue view`, `list` and `status` and refuses the
writes among them — `close`, `edit`, `comment`, `transfer` and the rest — so the loop reads the
tracker for §3.5's prior-art check and writes nothing to it. And there would be nothing to write
even without that rule: the filing skill is written against pull requests throughout, as are the
ledger's `filed` accounting and duplicate check, and an issue is an output a maintainer can get by
reading the ledger instead. So every non-pull-request outcome is a `SKIPPED: <why>` line and a
ledger row that keeps counting.

## 7. Grading, frequency, and the gate

### 7.1 Severity

| Severity   | Meaning                                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `critical` | Users cannot use the product, or it is doing damage: data loss, a credential leak, an agent writing to a cluster it should not, the gateway down        |
| `high`     | A real capability is broken or a user-facing failure recurs: a skill that always fails, alerts that never arrive, a reconcile loop that never converges |
| `medium`   | Degraded or wasteful — something works but slowly, expensively, or after retries a user can see                                                         |
| `low`      | Real and worth fixing, but nobody is currently harmed: a confusing log line, a stale document, a warning that fires in normal operation                 |

[`agents/selfimprove/SOUL.md`](../../agents/selfimprove/SOUL.md) §3 is the canonical copy — it is
the text the agent actually grades against, and this table is reconciled to it. Keeping the wording
identical is not pedantry: `critical` carries `minOccurrencesPerDay: 1`, `high` carries 3 and
`medium` carries 5, so a band boundary that moves between the two documents is the difference
between filing a pull request on two runs' agreement and needing most of a day's runs to agree. A
wrong answer to a user sits in `high` for that reason — it is a user-facing failure, but a single
one, read out of log text the loop does not control, is not evidence enough to open a pull request
unreviewed.

Grading is the agent's judgement against that rubric, and it is recorded with the evidence so a
maintainer can disagree with the grade without re-deriving the finding.

### 7.2 Frequency, which is two knobs

The requirement asks that "the PR frequency be tracked" and gives `severity: critical` and
`frequency: 5 a day` as an example gate. That phrasing supports two readings — how often the
_finding_ recurs, and how many _pull requests_ the loop is allowed to open — and both are needed,
so the design implements both under distinct names rather than picking one:

- **`minOccurrencesPerDay`** is an evidence threshold. A finding seen once may be a fluke; a
  finding seen several times a day is a pattern, and the count is itself the strongest sentence in
  the pull request. It counts **runs that reported the finding, not sightings** — the count the
  agent claims is recorded beside it and discarded by the gate, because a number the agent writes
  and the gate reads is the one shape an injected instruction could use to promote itself. The
  consequence is a ceiling nothing validates: on the default hourly schedule a finding cannot be
  seen more than 24 times a day, and on a daily schedule not more than once, at which point the
  shipped `medium` rule's threshold of 5 can never be met and that severity is silently disabled.
  The boundary is six hours — five sightings span four intervals inside a 24-hour window, and
  `4 × 6 = 24`; `high` clears at 3, so it tolerates twelve. `activeDeadlineSeconds` moves the same
  ceiling from the other side, which is easier to miss because the schedule still reads hourly:
  `concurrencyPolicy: Forbid` skips a firing while the previous run is going, so a run that uses its
  whole four-hour deadline costs the three firings behind it, making the ceiling six runs a day
  rather than 24. Both shipped thresholds are set under the pessimistic number. An operator who
  lengthens `selfImprovement.schedule` or the deadline past either boundary has to lower the
  thresholds to match, and nothing warns them.
  Two sightings is the floor, below which the knob does not go:
  `MIN_CORROBORATING_RUNS` in `selfimprove_ledger.py` raises any threshold under it, so the
  `critical` rule's `1` promotes on the second run and not the first. The reason is that severity
  is the investigating agent's own grade of its own finding, read off log text the loop does not
  control — a threshold of one would let a single run write itself a `critical` and open a pull
  request on it. Requiring a second, independent investigation to see the same thing costs an hour
  and removes that path. The run log names both numbers when the floor is the binding one.
- **`maxPullRequestsPerDay`** is a noise ceiling. It bounds what the loop can do to a maintainer's
  inbox regardless of how much it finds, and it is the safety valve for the case where a genuine
  regression makes every run produce a fresh critical finding.

Counting requires identity across runs, which is the same problem the fleet audit solved. A finding
is fingerprinted over the normalised title, with identifiers, timestamps and trailing punctuation
stripped, plus one bare file name taken from its location. The ledger holds the fingerprint, first
and last seen, one timestamped sighting per
run that reported it, the current grade, and any pull request already opened for it. The 24-hour
count the gate reads is derived from those sightings at read time rather than stored (§9.1).
[`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md) is the precedent for the dedup and
lifecycle, and this feature follows it rather than inventing a second scheme — with one deliberate
departure, below.

Neither the severity nor the signal class is part of that identity, and the two exclusions have the
same justification: both are the agent's judgement about a finding rather than the finding, so a
re-grade or a re-classification resets the count and the finding never promotes. A single
`/command` PATH defect reported as `errors` on one run and as `inefficiency` on the next is two
rows, one bug, neither able to reach `minOccurrencesPerDay`. `signal` stays in `fingerprint`'s
signature because every caller has one to hand; the function ignores it. The location is reduced for
the same reason — the agent writes that field as a 300-character sentence naming two files on one
run and as a bare `file.go:1820` on the next, and hashing the prose makes those two findings. What
survives the reduction is one file name: no directory prefix, because the same file arrives as a
repository-relative path, as a bare name, and as the abbreviated `k8s-operator/.../foo.go`; no line
number, because giving one at all forked `run.py:412` away from `run.py` even with the digits
already collapsed; and sorted rather than first-mentioned, because the order two files are named in
is whichever way the sentence came out. Narrowing the identity trades a split for a possible
collision, which is the right way round: a collision files one pull request carrying both sets of
evidence, while a split silently files nothing.

Every narrowing of that material orphans every row in every live ledger, and the part that does not
heal on its own is the promotion records — stranded on a key the new function cannot produce, so the
cooldown they hold can never fire and the next run with budget re-files a pull request already in a
maintainer's queue. Seven rows went that way the first time, two of them holding pull requests. The
repair is mechanical and now runs on every read: an orphaned row stores the title and location it
was keyed on, so it can say what its key ought to be, and it is moved there and merged with whatever
is already sitting at that key by the same code a write conflict uses. The steady state is a no-op —
a ledger written by the current `fingerprint` is already at its own keys.

**The title is part of the identity, and the precedent bans it.** Two of
[`fleet-audit-issue-ledger.md`](fleet-audit-issue-ledger.md)'s principles are inherited — identity
is computed by the harness and not by the agent, and severity is excluded because it is re-judged
every run. The third is not. That ledger's tuple is `(check, cluster, namespace, object)` and
[`audit_report.py`](../../agents/platform/skills/fleet-audit/scripts/audit_report.py) says outright
that no title may enter it, because prose re-derived each run made four unfixed criticals report as
resolved. Here there is no `check` — a self-improvement finding is free-form — so location alone
would collapse every finding in a file into one row. The title is what distinguishes them, and the
cost is the precedent's failure turned around: a reworded title starts a fresh count, the
occurrences never accumulate, and the finding is never promoted. That fails closed rather than open,
and the runner's brief tells the agent in as many words to repeat the title verbatim.

### 7.3 The gate

A finding is promoted to a pull request when the filing turn has not already refused it permanently,
it matches a promotion rule at its own severity with enough occurrences in the window — never
fewer than the two of §7.2, whatever the rule asks for — it has not been promoted inside its
cooldown, and the day's budget is not spent. `evaluate_gate` reads the
refusal first, before any rule: §1 and §8 describe a fix the filing turn declines because of what it
would touch, and no later evidence changes that answer. Everything else stays in the ledger, which
is not a discard: an unpromoted finding keeps accumulating occurrences, and a `high` at two
occurrences a day is one more sighting from crossing on its own.

## 8. The pull request

Five things, because that is what was asked for and because it is also what
[`.github/PULL_REQUEST_TEMPLATE.md`](../../.github/PULL_REQUEST_TEMPLATE.md) wants:

1. **The finding**, in detail: what is wrong, in which file at which revision, and what it costs.
2. **The evidence**: log queries with their results, span timings, the occurrence count and the
   window it was counted over, and the ledger fingerprint so the next run's PR can be recognised as
   the same finding rather than a new one.
3. **The fix and why this fix.** Alternatives considered and why they lost. A finding whose fix is
   not obvious is not filed at all: the filing turn prints `SKIPPED: <why>` and the finding stays in
   the ledger, counting. There is no issue-filing path for it to fall back to; §6.5 says why.
4. **Live validation, honestly scoped.** The runner is read-only, so it cannot exercise a fix
   against a running install, and claiming otherwise would be worse than claiming nothing. What it
   can do is narrower than it first looks. The agent image carries no Go toolchain, so `go build`
   is unavailable; `make docs-check` shells out to `git ls-files` and the image has no `git` on its
   own path; `make test-python` needs third-party packages the image does not install. What runs is
   a directory-scoped `python3 -m unittest discover` against the clone, which is the whole check for
   a Python change and no check at all for a Go or documentation one. Those ship verified by
   reading, and the section says so in those words. `AGENTS.md` accepts "not live-tested" with a
   reason; it does not accept an empty section or an overstated one.
5. **The code changes**, scoped to the finding. One finding, one pull request. Conventional Commit
   title, and the body says it was opened by the self-improvement loop, which install produced it,
   and at which revision. Written in a checkout of `baseBranch` taken for that finding, not in the
   one the investigation read — the finding is evidenced at the deployed revision and the fix is
   authored at the base tip, and §10 covers why those cannot be the same tree.

Each one also carries two labels, applied by the filing agent rather than by hand. The first is
`selfImprovement.github.prLabel`, `self-improvement` by default, so a maintainer can filter for the
loop's output without reading bodies — the body already says where the pull request came from, and a
label is what makes that queryable. The second is `selfImprovement.github.severityLabelPrefix` with
the finding's grade appended, giving `severity:critical` through `severity:low`: the body states the
grade too, but a maintainer with a queue of these reads the list page, and grading a finding is
pointless if it cannot be sorted on. Only the four grades in `selfimprove_ledger.SEVERITIES` produce
a label; the grade is agent-written and reaches a shell command, so anything outside that vocabulary
is dropped rather than sanitised.

Both are applied after the pull request is open rather than at creation, and one `gh pr edit` each.
`gh pr create --label` resolves the name first and fails the whole command on a label the repository
does not have, which would trade the pull request for the tag; `--add-label 'a,b'` has the same
problem one step later, resolving every name before applying any, so a single missing label would
cost both. The loop's token can attach an existing label and cannot create one, so a repository
receiving its first self-improvement pull request gets it unlabelled and a line in the log until a
maintainer creates the labels.

Neither is asked for at all when the token cannot attach a label to the base repository, which
§6.3 covers: the preflight reads that permission, and in `upstream` mode against a repository the
robot only contributes to it will be `READ`. Those installs open their pull requests unlabelled and
say so once in the run log, rather than per pull request. The paragraphs above therefore describe
what happens where the robot has `TRIAGE` or better on the base — every `fork`-mode install, and an
`upstream` one whose base repository is willing to grant it.

**What the filing agent refuses to touch.** A fix that would change the loop's own gate, ledger, or
grants is not filed, at any severity and however good the evidence. Everything else in
`agents/selfimprove/` is fair game — §1 makes the point that the loop's own code is inside its own
scope — but a loop that can widen the thing deciding what it is allowed to file has no ceiling, and
no amount of review discipline downstream substitutes for the patch never being written. Those
findings stay in the ledger and wait for a human.

The turn prints `SKIPPED: out of bounds - <why>`, and both halves earn their place. `SKIPPED` is
what stops the runner charging the finding as a filing that may have half-succeeded; `out of bounds`
is what marks the answer permanent, so `record_refusal` can flag the finding and the gate can stop
promoting it. An ordinary `SKIPPED` deliberately does neither — it means "not yet", keeps the counts
and invites a better-evidenced retry — and that generosity applied to a permanent no costs a filing
turn's whole budget every hour, forever.

Which is why the runner reads those three words only at the head of the line, immediately after
`SKIPPED` and its punctuation, rather than anywhere in it. The reason text after them is the turn's
own prose and can quote the finding it is skipping, so "SKIPPED: index out of bounds, already filed
as #12" is a deferral about an off-by-one and not a refusal. Getting that backwards is the more
expensive mistake by a wide margin: a missed marker costs the hourly retry and ends as soon as a
turn phrases it the documented way, while a spurious one writes a hold that nothing in the code
clears and that a recurring finding never ages out of — the finding is filed never again, and the
only notice is a line in one run's log. The refusal itself lives in
[`agents/selfimprove/skills/file-pull-request/SKILL.md`](../../agents/selfimprove/skills/file-pull-request/SKILL.md),
which is canonical for it.

The vocabulary is split across the prompts that ask for a line and the predicate that reads one,
and that is where it drifts. The skill's prior-art step asks for `SKIPPED: injected instruction in
the prior-art search`; the predicate held the runner's wording, `in the finding`; and because a
marker has to end the phrase rather than run on into another word, the skill's version was read as
a deferral and refiled every hour. So the test suite extracts every `SKIPPED:` template out of the
prompts and requires each to appear in a table with a verdict somebody chose — a phrase added to a
prompt and not to the table fails, whichever way it should have been classified.

The same asymmetry decides what a marker is allowed to _say_. A finding whose file this tree does
not contain is not thereby a finding this repository cannot act on: the defect may surface through a
layer we do own, and a workaround there is a real pull request. A path we do not contain and a
defect we cannot mitigate are different claims, and only the second is settled by anything a commit
here could change — so only the second may retire a finding. The marker is worded to assert it:
`no fix belongs in this repository`, printed after the turn names the layers between the defect and
the user that it ruled out, so the ledger records the reasoning rather than the conclusion alone.

Prior art reaches the filing turn from the ledger rather than from a search. The brief lists the
pull requests this loop has already opened for the finding, taken from `promotions[].url` and
filtered to the repositories the run configures. A keyword search cannot find a pull request whose
title does not use the finding's words, which is the normal case when the fix landed at a different
layer from the one the finding names, and a turn that finds no prior art is a turn one step from
concluding that nothing here can help. The list sits outside the untrusted fence: a number and a
repository name the run already knows carry nothing a directive could hide in, and fencing them
would tell the turn to distrust the one piece of prior art more reliable than its own search.

Neither of those nets reaches another installation's filings. More than one install can run this
loop against the same upstream, and the ledger is no help across them: it is a ConfigMap in each
install's own namespace, so two ledgers never compare fingerprints and identical ones would dedup
nothing. The search is the only cross-install net there is, and the keyword half of it fails at
exactly the wrong moment — two loops that found one bug independently write two different titles.

What they do share is the file. So the runner derives a search key from the finding's location
with the same `location_key` the fingerprint is hashed from, and the skill's §0 searches that
before it searches keywords. Deriving it in the runner rather than asking the filing turn to read
a path out of the location is what makes it work, for two reasons the live ledger shows plainly.
The location is free text — of eighteen rows, only two were a bare `path:line` and the rest were
prose naming two files — and `location_key` is the thing that already reduces all of those to one
name. It is also hostile text: sixteen of those eighteen carried a shell metacharacter and five
carried backticks, and the turn pastes this key into a double-quoted `curl` URL, so the runner
matches its own output against a strict file-name pattern and hands the turn an explicit
"skip this search" when it cannot vouch for the result.

The widened net has a cost that shapes the rest of the rule. A bare file name matches every pull
request that ever touched that file, while two of §0's outcomes (`closed unmerged as #n`,
`fixed in #n`) are permanent and cleared by nothing — so §0 requires a hit to be the same defect,
not merely the same file, before any state rule applies to it, and says to file when unsure: a
duplicate is visible and closable, a wrongly permanent skip is neither. §4 requires every body to
carry its location verbatim on a `Location:` line so the file name is there to be found. Branch
names carry the install's cluster name for the adjacent reason: several loops pushing to one fork
must not collide on a branch name two of them would otherwise both derive from the same bug.

The search runs twice, and the second time is the one that does the work. §0's pass happens before
the turn has read any code; the turn then writes the change, commits and pushes, which on the runs
watched so far takes eight to eighteen minutes. Installs share an hourly schedule, so that window
is precisely when another one files the pull request this turn is about to duplicate — a check that
only ran at the start would miss every collision it exists to catch. So §6 repeats it immediately
before `gh pr create`, narrowed to `is:open` because by then the question is no longer whether the
finding is live but whether someone got there first. Both passes fail open: a search that errors
does not stop a filing, since a finding dropped on a network error is a finding lost and a
duplicate is merely closable.

Two of the repository's rules apply awkwardly to a machine author and are worth settling here. The
**Self-Review** section must not claim a review it did not perform: the runner is the context that
wrote the change, and `AGENTS.md` is explicit that reviewing a diff in the context that produced it
is the one configuration that does not work. The honest content is the checks it ran, the angles it
considered, and a statement that no independent adversarial pass was performed — leaving that pass
to `kube-agents-bot`, which reviews every pull request on open anyway. And the **duplicate-work
scan** is not optional: §3.5's check against open issues and pull requests goes in the PR's Context
section as `Closes #<n>` or as a note on how this differs, exactly as a human contributor's would.

## 9. Configuration

One block, off by default, in `charts/kube-agents/values.yaml`. That file carries the full
commentary on each key and is canonical; what follows is its shape and the reasoning that is not
obvious from a key name.

```yaml
selfImprovement:
  enabled: false
  schedule: "0 * * * *"
  # The Job's hard stop, and the number every budget below is clamped against.
  # The two timeouts bound the agent turns inside it, so the runner still
  # reaches its ledger write when an investigation overruns; the Job deadline
  # kills the pod, recording only a `killed` row. Four hours against an hourly
  # schedule: `concurrencyPolicy: Forbid` skips a fire while a run is still
  # going, so a deep run costs those fires and a quick one keeps the cadence.
  # The cost is paid by the gate — worst-case six runs a day, which is the
  # ceiling `minOccurrencesPerDay` below has to be reachable under.
  activeDeadlineSeconds: 14400
  # Per turn, not per run. Roughly two and a half times the 1424s a turn
  # actually takes, so one that does not hit the call cap cannot spend the
  # deadline.
  investigateTimeoutSeconds: 3600
  # A ceiling on investigation turns, not a target: the runner continues a turn
  # that reports it was cut off, and stops as soon as one reports it finished.
  investigateMaxTurns: 6
  # Also the size of the filing reserve the investigation loop is held back by.
  # 3300 is a recommended ceiling on how much of an hourly schedule one filing
  # turn may consume, not an enforced one; the credential carries no deadline
  # (§6.3) and `budgeted` is what actually clamps a turn.
  fileTimeoutSeconds: 3000

  # SIGTERM budget, and one setting in two places. The handler writes a `killed`
  # row explaining why the run died, and a contended ledger write is four
  # PATCHes and three re-reads at 20s per round trip. Kubernetes' default 30
  # loses exactly that row. Keep the write budget under the grace period.
  terminationGracePeriodSeconds: 150
  killWriteBudgetSeconds: 140

  # report-only  ledger only, no GitHub credential, no write path out of the cluster
  # fork         branches and pull requests to a fork
  # upstream     cross-fork pull requests against github.upstreamRepo
  mode: report-only

  # Which of the seven signal classes to investigate. Narrowing this is the
  # cheapest way to cut the loop's cost.
  signals: [errors, inefficiency, latency, responses, delivery, forge, other]

  # The agent Deployment the runner observes and cross-checks its own image
  # against; empty derives it from the release. A mismatch aborts the run.
  observedDeployment: ""
  # Investigate anyway when the image carries no revision stamp, reading source
  # at `main` and saying so in every finding. Off: an unstamped image refuses.
  allowUnstampedImage: false

  gate:
    # Promoted when a finding matches a rule, has been seen often enough in the
    # last 24 hours, is out of cooldown, and the day's budget is unspent. A
    # severity with no rule is never promoted — which is how `low` is excluded:
    # by omission, not by a separate switch.
    #
    # "Often enough" is never fewer than two runs, whatever a rule asks for, so
    # the 1 below promotes on the second sighting. §7.2 says why.
    rules:
      - severity: critical
        minOccurrencesPerDay: 1
      # Three of a worst-case six runs a day. `high` is a broken capability,
      # so the bar clears inside a day while still needing the finding to
      # survive two further independent investigations.
      - severity: high
        minOccurrencesPerDay: 3
      # Medium is degraded-or-wasteful. 5 of a possible 6 is deliberately near
      # the ceiling: a slow path present in almost every run of a day is a
      # standing property of the install rather than a bad hour.
      - severity: medium
        minOccurrencesPerDay: 5
    maxPullRequestsPerDay: 3
    cooldownHours: 24

  github:
    # Where the run reads its own source from, in every mode. Under `upstream`
    # it is additionally the repository pull requests are opened against.
    upstreamRepo: gke-labs/kube-agents
    # Required when mode is fork or upstream; ignored under report-only.
    forkRepo: ""
    # The branch pull requests are based on, and the one the filing turn checks
    # out and writes its fix in — so the diff is one commit whatever revision
    # the image is stamped at (§10). Not always `main`.
    baseBranch: main
    # Both applied after the pull request is open, one `gh pr edit` each; ""
    # opts out of that label (§8). The prefix takes the finding's grade, so
    # `severity:critical` through `severity:low` and nothing else.
    prLabel: self-improvement
    severityLabelPrefix: "severity:"
    # A Secret holding the robot account's personal access token, created out of
    # band (§6.2). Required under fork and upstream, and the render fails
    # without it rather than deploying a loop that cannot file. The chart never
    # sees the value: it names the Secret in a volume the sidecar mounts.
    patSecret: ""
    patSecretKey: token
    ksaName: kubeagents-selfimprove
    # A usable GCP service account id (6–30 characters, GCP's own cap), checked
    # at render because it reaches a Workload Identity annotation GCP accepts
    # even when the account behind it does not exist.
    gsaName: kubeagents-selfimprove

  # Empty uses the in-cluster LiteLLM Service the agent uses. Set to give the
  # loop its own model budget, or a cheaper model than the one answering users.
  model:
    endpoint: ""
    name: model-default

  # Counts and filed pull requests, between runs. Deliberately not a ConfigMap
  # the agent's Deployment references: the operator hashes those into the pod
  # template, so writing to one would roll the agent every hour.
  ledgerConfigMap: kube-agents-selfimprove-ledger

  # emptyDir sizes for the run's private Hermes home, its source checkout, and
  # one shallow base-branch checkout per promoted finding (§10).
  workspaceSizeLimit: 4Gi

  networkPolicy: true
  # The cluster DNS VIP, for the runner's port 53 egress rule. A policy matches
  # the address the pod dials, so the kube-dns pod selectors alone are not
  # enough. Empty allows both GKE service-CIDR
  # conventions (34.118.224.10, 10.96.0.10).
  dnsCIDRs: []
  # Added to the ClusterIP defaults (34.118.224.1, 10.96.0.1) and to every
  # address the chart finds in the default/kubernetes Endpoints. The Endpoints
  # half is what makes this work on Dataplane V2, which matches egress against
  # the post-translation destination -- see below.
  apiServerCIDRs: []
  # Whether that Endpoints lookup happens. It reads `default/kubernetes`, the
  # one object this chart reads outside its own namespace, and Helm aborts the
  # whole release render on a lookup it is refused -- so on a cluster whose
  # installer identity lacks that read, switching the loop on stops the agent
  # installing too. False falls back to the static defaults plus whatever
  # `apiServerCIDRs` names.
  discoverApiServerEndpoints: true

  # Empty inherits the agent's sandbox
  # (platformAgent.deployment.availability.runtimeClassName): an install that
  # runs the agent under gVisor did not decide that the pod fetching and reading
  # GitHub source should run unsandboxed. Set it to override, `""` cannot mean
  # "sandbox the agent and not the loop".
  runtimeClassName: ""

  resources:
    requests: { cpu: 500m, memory: 2Gi }
    limits: { cpu: "2", memory: 4Gi }
```

There is no `volumeSnapshot` key. §3.4 argues for snapshotting the agent's volume and says why the
path is not built, so the chart ships no flag rather than one that would accept `enabled: true` and
do nothing.

### 9.1 Reading the ledger

`make selfimprove-ledger` renders the ConfigMap as a report: the last run and the run count first,
then the run history, then the findings worst-first, then every pull request the loop has opened.
`scripts/selfimprove_ledger_view.py --help` has the filters; `--file` reads a ledger already on disk
and needs no cluster, and `--json` prints the document for piping into `jq`. Rows in both tables run
several lines tall, so a blank line separates them; `--rows ruled` draws a rule there instead and
`--rows compact` gives the line back.

Two of its columns are derived rather than stored, and both come from `selfimprove_ledger`'s own
functions rather than a second implementation. `SEEN` is `occurrences_in_window` — runs, not claimed
counts, per §7.2 — while `REPORTED` is the untrusted number beside it. The gate line under each
finding is `evaluate_gate` replayed over the whole ledger against the CronJob's current gate, which
answers "what would the next run do with this" and is deliberately not a record of what any past run
decided: a run only ever gates the findings it saw that hour.

File locations are OSC 8 hyperlinks to GitHub, pinned to the revision the finding was made against
rather than to a branch, because the line number is only meaningful against the code the agent read.
The findings table links the first reference in a location; `--detail` lists every one of them. What
gets linked is decided by the first path segment: it has to be a top-level entry of this repository,
which is derived from the checkout the script ships in rather than listed. That rule is what keeps a
finding in `agent/anthropic_adapter.py` — the Hermes harness, a different repository — from getting
a kube-agents URL that 404s, and a 404 reads as a stale finding rather than as a bad link.

A `#123` in a gate verdict or a refusal reason is linked the same way, and needs a repository before
it can be. The ledger's promotions name every number the loop assigned itself, and those resolve
against the pull request's base — `SELFIMPROVE_UPSTREAM_REPO`, which is the fork under `mode: fork`.
Every other number came out of the project's history, where a squash-merge subject line ends
`(#874)`, so it resolves against the base repository's fork parent: one `gh api` call per
invocation, falling back to the base itself whenever `gh` is missing or the base is not a fork. The
reference is linked either way — for a base that is not a fork the base is the right repository, and
for a call that did not land it is where a bare number pointed before any of this existed. Only those two
fields are scanned, because the filing skill dictates their wording; a `#12` in an agent-written
title is as likely to be a hostname suffix.

### 9.2 Turning it on

The chart renders every Kubernetes object the loop needs. It cannot render the half that lives
outside the cluster — the fork, the personal access token, the two labels §7 attaches — and it
cannot check that the four names in §9 agree with what is actually there. Those failures all look
the same from outside: the CronJob fires on schedule, the investigation succeeds, and an hour later
the filing turn writes `SKIPPED` for a reason nobody is reading the ledger closely enough to see.

`make selfimprove-enable` is the tool for that half. `./scripts/selfimprove_enable.py --help` has
the arguments; the five subcommands run in this order:

- **`preflight`** — before anything is applied. Checks the token's scopes and its role on all three
  repositories, that the fork is a fork of the upstream, that the base branch exists, that the
  cluster is at 1.29 or above (below it the credential-proxy sidecar never terminates and
  `concurrencyPolicy: Forbid` blocks every later run), and that the gate is reachable at the
  schedule you are asking for.
- **`secret`** and **`labels`** — the two out-of-band objects. `secret` reads the token from a file,
  from stdin, or from the environment, never from an argument, and applies it as `stringData` over
  a pipe; `kubectl create secret --from-literal` would put it in argv, where the process table and
  the shell's history file on the operator's own machine both keep a copy. The apply is
  server-side, under its own field manager, for the other end of the same property: a client-side
  apply copies the manifest it submitted into `kubectl.kubernetes.io/last-applied-configuration`,
  which would leave the token in cleartext in the Secret's metadata beside its base64 `data`, where
  every tool that redacts `data` and not annotations prints it. `labels` creates
  `self-improvement` and the four severity labels on the repository pull requests are opened
  against, because the filing turn can attach a label and cannot create one.
- **`values`** — emits the `selfImprovement` block as a YAML values file for `helm`, or as HCL for
  `extra_helm_values` in `terraform/examples/full-install`, which is the supported route since the
  composition does not expose the block directly.
- **`verify`** — after the apply, against the live install. Reads what the CronJob is actually
  running rather than what you meant to apply: the env the chart rendered, the KSA's Workload
  Identity annotation, the NetworkPolicy's egress against the endpoints the loop needs, the Secret's
  key name, the build stamp inside the agent pod against the revision the runner would compare it
  to, and the ledger's run history.

`verify` is the one to run on a schedule rather than once. Every check it makes is of a thing that
can drift after the install: a token expires, a fork gets renamed, the agent image moves and the
divergence guard starts refusing.

Nothing in the tool prompts. The token arrives by file, by stdin or from the environment, every
other decision is a flag, and colour turns itself off when stdout is not a terminal — so an agent
walking an operator through the setup can run the whole order unattended. `preflight`, `secret`,
`labels` and `verify` take `--json`, which replaces the prose with one shape,
`{"checks": [{"status", "check", "detail", "fix"}], "failed": bool}`, and writes nothing else to
stdout. One reader parses all four, and it branches on `failed` rather than on which subcommand
produced the document; each row's `fix` is the sentence to read back to the operator. `values` has
no `--json` because its output is the artifact rather than a verdict — `--format json` is where
that lives.

## 10. Failure modes it takes a position on

**The loop investigates itself.** Its own runs produce logs and errors in the same namespace, and a
loop that finds itself slow and opens a pull request about itself is a closed circuit that generates
work indefinitely. Every evidence query therefore excludes the loop's own records by default:
`NOT resource.labels.pod_name:"kube-agents-selfimprove"` on the log filter, and the same prefix
dropped from trace roots and Kubernetes object names after the read. `--include-self` lifts it, for
the one case where the loop is deliberately debugging itself. This is a filter that must be written
on the first day, not the day it goes wrong.

It emits no spans of its own — the profile enables no plugins, so `hermes_otel` is off — which is
why the trace exclusion is a defensive name check rather than a query clause. Cloud Trace has no
`NOT` operator, so were the runner ever given a collector route, that check would spend page size on
records it then discards; `--service` narrows at the source instead.

**A run outlives its schedule.** Hourly with `concurrencyPolicy: Forbid` means a run that takes
seventy minutes silently halves the cadence, and a run that hangs stops the loop entirely with no
error anywhere. `activeDeadlineSeconds` bounds it and a killed run is itself recorded in the ledger.
The row names the stage the signal interrupted, and blames `activeDeadlineSeconds` only when the
run was within five minutes of it — measured from the Job's `.status.startTime`, which is where the
kubelet counts from and can be twenty minutes before the container's own start. A SIGTERM arriving
earlier than that came from an eviction, a node drain or a deleted Job, and the row says so rather
than sending a reader to raise a limit nothing reached. A kill inside a filing turn also charges
that finding an unconfirmed promotion, on the same reasoning as the filing-turn timeout below: the
held the credential and the `gh pr create`, so the pull request may exist, and a finding left
uncharged is re-promoted every hour past a ceiling that counts promotions.

**The image moves and the CronJob does not.** Covered by the abort in §2, and worth repeating
because it is the failure that produces confidently wrong pull requests rather than no pull
requests. Every finding is stamped with the revision it was found at, so a maintainer can check.

**The API server is unreachable, and the runner waits.** Two independent things have to be right
here, and each hides the other when it is not. The NetworkPolicy allowlists the addresses in the
`default/kubernetes` Endpoints, the way the operator's own policy does, rather than naming the API
server by its ClusterIP: GKE Dataplane V2 matches egress against the destination after service
translation — the control-plane endpoint, a private address the wide 443 rule excludes on purpose —
so a ClusterIP rule drops the packets rather than rejecting them. And every API call the loop makes
carries a timeout, because the Kubernetes client waits forever by default and the code's own
degradation paths, which record the run as unverified and carry on, are unreachable from a hang: a
hang is not an exception. The second is the one worth generalising from — an error path that only
runs when the dependency answers is not an error path.

**The gate is set too loose on a large fleet.** `maxPullRequestsPerDay` is per install, and fifty
installs at three a day is a hundred and fifty pull requests against one repository. This is the strongest
argument for `report-only` being the default and for `upstream` being a mode a maintainer chooses
for a small number of installs. Deduplication across installs is not solved here — see §11.

**A finding is right and the fix is wrong.** The most likely bad outcome, and the reason §8 says a
finding whose fix is not obvious is not filed at all. A pull request with correct evidence and a
wrong patch still delivers most of the value, provided the evidence is separable from the patch,
which is
what the five-part structure is for.

**The ledger rolls the agent.** The operator SHA256-hashes the ConfigMaps it owns into the agent's
pod-template annotations, deliberately, because the profile merge only happens at startup and a
config change must therefore roll the pod. A ledger ConfigMap that ended up in that set would roll
the Platform Agent on every write — an hourly restart caused by the thing that is supposed to be
observing it without touching it. The ledger is a chart-owned object the agent's Deployment does not
reference, and it must stay that way.

That set is closed by construction rather than by convention — the operator hashes four ConfigMaps it
builds by name from the PlatformAgent, and there is no label selector, no `ConfigMapList`, and no CR
field that names one — so the default ledger name cannot enter it. What that leaves is the collision:
`selfImprovement.ledgerConfigMap` pointed at one of those four names would not churn the hash, it
would put the runner and the operator's `ForceOwnership` apply in a fight over the agent's own
configuration. The chart rejects those four names at render time.

**Two writers race on the ledger.** `concurrencyPolicy: Forbid` serialises the CronJob's own Jobs
and nothing else (§5.2), so a Job created by hand or a `kubectl edit` can overlap a scheduled run.
An unconditional write loses that race silently, and what it loses is not only occurrence counts: a
promotion record is the only thing holding a cooldown, so dropping one re-files the finding the next
time it is seen and the maintainer gets a duplicate of a pull request already in their queue — the
outcome the gate exists to prevent. `selfimprove_ledger.save` therefore writes against the
`resourceVersion` `load` observed. The precondition alone would be worse than none, because the
window between read and write is the whole of `activeDeadlineSeconds` — four hours by default — and
a concurrent `helm upgrade` reapplying the chart's labels inside it would turn into a 409 that lost
the run's findings entirely. So a 409 is not fatal: `save` re-reads, folds this run's rows into
whatever is there now, and retries — four attempts in all, so three retries — before giving up with
a message saying to go and find the second writer. What it will not do is drop the precondition. A
409 says the object exists and somebody else's document is in it, so a re-read that comes back
without a `resourceVersion` ends the write: the object was deleted, or the Role grants patch but not
get, and `_read` reports both as an empty ledger because that is the right answer for `load` and the
wrong one here. Writing anyway would be an unconditional write over the document the 409 just
announced. Ending costs this run's findings, which recur within the hour; the other writer's
promotion records do not. The merge is well defined because the ledger is almost all append-only and
timestamped. `runs`, `sightings` and `promotions` are unions keyed on their timestamps; `refused`
and `first_seen` keep the earlier of the two, because both measure how long a human has had the
finding; the agent's description of a finding comes from the newer writer. Neither writer loses
rows. The merge runs only on a 409, so the uncontended path writes the run's document verbatim and a
`prune` is not undone by the pre-prune copy still in the ConfigMap.

**The ledger fills up.** A ConfigMap is capped at 1MiB, and the ledger only grows: fingerprints
arrive and the write is where a run's findings become durable, so a ledger too large to write loses
every later run's output and nothing recovers it without someone deleting the object. What keeps it
away from the cap is a set of bounds on what one run can add. An entry's agent-written prose is
truncated at 16KiB across `evidence`, `summary`, `proposed_fix` and `user_impact` together;
`title` and `location` — the two agent-supplied fields that are not prose, and the two that reach
the next run's brief and the filing prompt as well as the ledger — get their own much smaller caps;
and the run history and each finding's promotion list are fixed-length. Past 768KiB, three quarters
of the API server's limit, `save` sheds rather than refuses, in three tiers ordered by what a lost
field costs: the run history down to five rows; then the largest prose fields, each replaced by a
marker so a reader can see that a summary once existed; then, last, the oldest already-thinned
refused rows. That third tier exists because `prune` keeps refused rows indefinitely rather than
deleting them at thirty days, so without it a ledger full of holds has nothing left to give. A row
it drops is one nothing has seen for a month, and the cost is that finding being filed once more if
it ever comes back. It raises only if all three still do not fit, with a message naming the likely
cause rather than letting the API server return a 413. What it never sheds is what the gate reads — a finding
that loses its sightings stops promoting, and one that loses a promotion record is filed again,
which trades a loud failure for a quiet wrong answer. Refusing outright would be worse than both,
because `save` is the last thing a run does: a ledger over the cap would stay over it, every later
run reading it, pruning it, and raising before it could write the smaller document that would have
fixed it. The bound that took a second look is retention: a promoted finding has to outlive the
sighting window, because its pull-request record is what stops the loop re-filing it. Never
deleting it is not the answer — at three pull requests a day that is a row added every eight hours
and none ever removed, which reaches the cap inside a year. A promoted row is held until its last
promotion is older than both the retention period and the cooldown, whichever is longer, since the
cooldown is the only thing that reads it.

A refused finding is the one row retention does not reach, and getting there took two passes. A
refusal charges no promotion, so for a while nothing but `last_seen` held the row open and 30 days
of quiet deleted the finding and the permanent hold on it — which the marker exists to prevent, and
which an injected instruction is best placed to exploit, because the attacker chooses when the text
appears. So the row survives and is thinned instead: its agent-written prose is replaced by a
marker, leaving the fingerprint, the title, the location and the refusal, and a later sighting
writes the prose back. The refusal's own `reason` is clipped on the way in, because it is the one
free-text field nothing ages out. Thinned rows are also the last rung of the shed ladder: if the
ledger is over the cap after every summary has gone, the oldest refusals are deleted rather than
`save` raising, since a dropped hold costs one finding being filed once and a failed write costs
every finding of every later run.

Because the cooldown decides both of those, `cooldownHours` is read once, by
`selfimprove_ledger.sanitise_cooldown_hours`, and the gate and the pruner take the answer. Two
readers of one key agree on every well-formed value and disagree on the malformed ones, and on a
negative value they disagree in the direction that opens pull requests: a negative timedelta is
never greater than an elapsed one, so the gate's cooldown check holds nothing while a pruner given a
corrected value goes on behaving — a loop re-filing the same finding every hour, against a ceiling
that counts promotions and so never intervenes. `.inf` and `nan` are the same input class with a
louder failure: `float()` takes both and `timedelta` then raises inside the gate. Unusable values
fall back to the 24-hour window with a line in the run log; a value past ten years is clamped there,
that being where subtracting it from the current date stops working rather than a policy about how
long a cooldown may be.

The gate's two whole numbers go through `sanitise_gate_count` for the same reason and with a
different rule. `maxPullRequestsPerDay` and `minOccurrencesPerDay` read with a bare `int()` guarded
against `TypeError` and `ValueError` would still die on `.inf`, which raises `OverflowError` —
and `.inf` is how YAML spells infinity, which both keys have a meaning an operator might reach for
it to express. "No ceiling" or "never promote this severity" would kill the run at the gate, after
the investigation had already been paid for, and again on the hour after that. So the sanitiser
clamps rather than rejects, because unlike a malformed cooldown a huge count is a coherent
instruction and a million carries out either reading of it. A negative clamps to zero — which is
what the arithmetic downstream makes of it anyway — and a value that is not a number falls back to
the default. `gate_notes` collects whatever the gate did not take at face value so the runner can
log it, and it calls the same functions the gate does, so a line in the log cannot describe a number
the gate then used differently.

**The turn ends early and the run reports success.** A `hermes -z` turn that exhausts its iteration
budget exits 0 after printing a warning, so a truncated investigation and a clean run that found
nothing are the same event from outside — 34 minutes of real evidence-gathering recorded as
`outcome=ok findings=0`. The runner passes `--usage-file` and logs `completed` and `api_calls` for
every turn, and logs the turn's final response. Between them the Job log distinguishes the two cases
and keeps the only surviving account of what the turn decided, the pod's emptyDir being gone by the
time anyone reads it.

Instrumenting it is not fixing it. The cap cannot be raised: `hermes -z` constructs its agent
without passing `max_iterations`, so the constructor default of 90 applies and `agent.max_turns`,
`--max-turns` and `HERMES_MAX_ITERATIONS` all miss it. Measured investigations stop at exactly 90
calls somewhere between 1100 and 1424 seconds, well short of a 3000-second per-turn budget, which
says the binding limit is calls and not clock — so the run has time it cannot spend inside one turn.
1424 is the figure §9 is sized against: a turn's duration varies with what the evidence tools
return, and sizing to the fastest measurement is how a budget comes out too small.

Two things follow from that. The first is to stop depending on the turn reaching a clean end: the
skill has the agent write `findings.json` after its first confirmed finding and rewrite it as it
goes, so a capped turn hands back what it had. The second is to spend the leftover clock on more
turns. When a turn reports it did not finish, the runner starts another against the same home, up to
`investigateMaxTurns`, and stops as soon as one reports it did. The continuation prompt is the whole
base brief plus a handoff — which turn this is, how many findings are already on disk, and the
previous turn's closing account, which Hermes writes for us because hitting the cap triggers its
end-of-iterations summary. That account is fenced as untrusted for the same reason the ledger
summary is, and the reason survives the extra hop: the turn that wrote it spent itself reading Cloud
Logging, so quoting it into the next turn's instructions unfenced is a two-step path from a Google
Chat message to the operator's voice.

The runner merges each turn's findings itself rather than trusting the file to accumulate. The
continuation prompt asks the agent to append, and an agent that reads its instructions will; the
case this covers is the one where it does not. Agents do empty the file mid-turn without any
continuation involved — the paragraph below is about one shape of that — so reading the file after
every turn, while it is still on disk, is what makes a later turn unable to destroy an earlier one's
work.

A run can still end truncated, when it exhausts `investigateMaxTurns` or the deadline. That costs
depth rather than the run: one run does not cover every signal class, and successive runs plus the
ledger's occurrence counts are what make that acceptable.

Filing must not be crowded out the same way, and adding turns is what made that a real risk rather
than a theoretical one. Both stages clamping against the same remaining clock means the
investigation — which runs first and stops only at its own `MIN_TURN_SECONDS` floor — can spend
every second filing needed, and the run then investigates, grades and promotes a full set of
findings and files none of them. Worse near the boundary: filing gets a budget just over the floor,
times out part-way, and is recorded `UNCONFIRMED`, which charges a slot against
`maxPullRequestsPerDay` and starts a 24-hour cooldown for a pull request that may never have been
opened. So the investigation loop does not see the whole clock. `investigation_budget` subtracts
`fileTimeoutSeconds` before the loop reads it, and the loop stops early enough that the first filing
turn is affordable however deep the investigation went; the reserve is zero under report-only, which
never files. `activeDeadlineSeconds` is then sized against the measurement rather than against the
schedule: four hours covers six turns at the measured 1424s and still leaves a whole
`fileTimeoutSeconds` for the first pull request. A run that files a second one gives it the
remainder, and where that is under half of `fileTimeoutSeconds` the finding is deferred rather than
lost — half, and not the `MIN_TURN_SECONDS` an investigation turn stops at, because the two stages
fail differently. An investigation turn cut short leaves its findings on disk and costs the seconds
it spent; a filing turn cut short is charged for the attempt, which is the `UNCONFIRMED` slot and
cooldown two sentences above. A budget it cannot finish in is worse than no attempt. Deferring, it
keeps its occurrence counts and its gate eligibility. It reaches GitHub an hour later rather than
never — provided the next investigation finds it again, since the gate only ever considers the
fingerprints the run in front of it reported.

A run whose investigation was capped exits 0. The exit code answers whether the runner worked, and
the ledger's `outcome` answers how the investigation went; conflating the two puts the ordinary run
— one that promoted a finding and wrote its ledger — in the Job history's failed bucket, and a
CronJob whose every run shows `Error` is one nobody reads. The counter-argument, that an operator
wants Job status to surface a loop that never completes cleanly, is answered by `outcome` being in
every ledger row, one `make selfimprove-ledger` away, rather than by a false alarm every hour. This
is also what keeps `backoffLimit: 0` honest: non-zero means nothing durable came out of the run, and
those are exactly the failures a retry inside the same `activeDeadlineSeconds` could not have
helped. That deadline bounds the Job across all attempts rather than each one, so a retry after a
long first attempt inherits what is left of that deadline and the runner's turn floor refuses it — a
pod spent to write a `refused` row.

Incremental writes are necessary and not sufficient, because the last write before the cap can be
the empty one: a turn empties `findings.json` on disproving a candidate, confirms a real finding,
spells it out in its response, and hits the cap before writing it back. Taking the file as
authoritative there records `findings=0` and the ledger never sees the finding. `read_findings`
therefore takes the turn's `completed` flag: an empty file from a turn that finished is the answer
and the response must not
override it, while an empty file from a turn known to have been cut off is only where the agent
stopped, and the response is read instead. An unknown completion state keeps the file. The
asymmetry is the reason for that default — recovering wrongly opens a pull request for a hypothesis
the agent disproved out loud, and declining to recover costs one sighting of a finding the gate was
going to make the next run confirm again.

**The filing turn runs out of clock, and leaves no account of how far it got.** A filing turn that
hits its budget with nothing pushed is recorded as `outcome=truncated findings=1 promoted=1
filed=0`: unconfirmed rather than filed, a slot spent from the day's budget, the cooldown started,
and a log line naming the branch prefix to go and check. That accounting is correct and expensive,
so the budget has to be sized for the work — filing is a re-read of the finding against the tree
plus a patch, a commit, a push and a pull request, and the default is 3000 seconds. There is room
for it: measured investigations end at the 90-iteration cap well short of
`investigateTimeoutSeconds`, and this many seconds is reserved out of the investigation rather than
left to whatever survives it. 3000 rather than 1500 because a run at the default
`maxPullRequestsPerDay: 3` can file three times, and only the first is reserved for — the rest take
the remainder and defer if it is under half of this value, which costs a run and not the finding.
3300 is the recommended ceiling — a bound on how much of an hourly schedule one filing turn may
consume — but nothing enforces it: the chart renders whatever is configured and the runner logs no
warning past it. What holds the number down in practice is `budgeted`, which clamps every turn to
what remains of `activeDeadlineSeconds`.

The second half of the failure mode is diagnostic. `run_agent` logs the turn's response on the
timeout path as well as on a clean exit, because the one case where the pod's emptyDir is about to
vanish without anyone reading it is the case that most needs a record: without it there is no way to
tell from the Job log whether the turn pushed a branch, wrote a patch, or never reached `git`. The
general form is the same one the API-server hang above has — diagnostics conditioned on the success
path are absent exactly when they are needed.

**The pull request is opened against a branch that does not contain the deployed revision.** GitHub
computes a pull request's diff from the _merge base_, not from the commit the head branched from —
so a head branched from the deployed revision, against a base that does not contain it, renders
every commit of the difference as part of the change. A one-line fix to `credential_proxy.py`,
committed as exactly that one file, becomes a pull request of 40,346 additions across 261 files when
the install is running a test branch the fork's `main` has never seen. Nothing fails on the way
there, which is the whole problem: `gh pr create` returns a URL, the runner records a filing, and the
turn's own closing reply says the diff is clean — it checked its commit, which was correct, rather
than the pull request, which was not.

Detecting it is not enough. A skill that compares base against head and refuses with `SKIPPED` when
the file count does not match what it committed buys a refusal instead of an unreviewable pull
request, and leaves the finding permanently unfilable on any install whose revision is not on the
base — the loop finds the same thing every hour and refuses it every hour.

So the design removes the condition. One checkout cannot serve both purposes, because the two have
different correctness criteria: a finding has to be evidenced against the commit the observed pod is
running, or it describes code nobody is executing; a fix has to be written against the commit a
maintainer will merge it into, or the distance between the two becomes part of the diff. There are
therefore two checkouts. The investigation keeps the deployed revision. `fetch_base_checkout` takes a
second shallow clone at the tip of `baseBranch` — `selfImprovement.github.baseBranch` rather than a
hardcoded `main`, because an install pinned to a branch of its own is a real configuration and not a
mistake — one per promoted finding, and the filing turn writes there. That makes the head's merge
base the base tip by construction, and the diff one commit, whatever the image is stamped at.

Three things follow from the two-checkout arrangement beyond the diff itself:

- **A deployed image ageing does not degrade the diff.** Its stamped revision only has to be
  reachable for the source fetch, not to be an ancestor of the base. An install six weeks behind
  `main` still opens a one-file pull request.
- **An image built from a fork's unmerged work can file upstream.** A finding about code common to
  both is filed cleanly, and a finding about the fork's own unmerged code fails the skill's §0
  re-read against the base tree and opens nothing — which is the right answer.
- **Two findings in one run do not contaminate each other.** Sharing one checkout, the second
  turn's `git switch -c` would branch from wherever the first left `HEAD` — on top of the first fix,
  which then appears in the second pull request. A tree per fingerprint removes the ordering.

The skill's base-against-head comparison survives as a confirmation rather than a gate, through the
public compare endpoint rather than `gh api`, which the pod's own credential-proxy policy refuses as
a write path its argv rules cannot read. What it catches is a turn that branched in the wrong tree,
not a misconfigured base.

The cost is a shallow clone per promoted finding, capped at 180 seconds so a network fault cannot
eat the finding's model budget, and a failure to obtain it is `SKIPPED` rather than a fallback to the
investigation's tree — falling back would reintroduce exactly the pull request this replaced.

**The agent cannot write its own handoff file.** The upstream Hermes image sets
`HERMES_WRITE_SAFE_ROOT=/opt/data`, which is correct for the Platform Agent, whose PVC that is, and
wrong for a run whose home is an emptyDir elsewhere: every `write_file` is denied and the run
reports nothing found. The runner sets the variable to the run's own home, keeping the confinement
the isolation ledger asks for while putting the one file that matters inside it. The variable is one
instance of a class: the runner inherits an image tuned for a different process, and anything else
in that image keyed to `/opt/data` fails the same way, silently, on a path nobody exercises
interactively.

**Evidence leaks.** Logs and spans contain customer cluster names, project IDs and user identifiers,
and an `upstream`-mode pull request publishes whatever is quoted in it. Every evidence command
therefore redacts on the way out, in `emit`, so a new subcommand cannot forget to: credential shapes
copied from the credential proxy's `redact_credentials`, and a second pass for identifiers, which
are not secrets but are the customer's business — service accounts, bare email addresses,
`projects/…` and `clusters/…` paths, non-loopback IPv4, Google Chat spaces and Slack ids. Keys are
redacted alongside values, because a Kubernetes annotation puts user content in both.

It is shape matching and therefore incomplete by construction. It is a floor under what leaves the
cluster, not a substitute for reading the ledger: no install should be moved to `upstream` mode
without someone having looked at what its ledger actually contains, and `--no-redact` exists for
that reading and says so in its help text.

**The evidence is attacker-reachable.** Cloud Logging holds whatever a user typed into Google Chat,
and the investigation reads Cloud Logging. Anyone who can address the agent can therefore put text
in front of the investigating model, which makes this the one component of the loop with a hostile
input. Two things bound it. The turn that reads that text has no credential: in `fork` and
`upstream` mode the chart puts the proxy shims on the container's `PATH`, and the runner takes that
directory back off for every turn except the filing turn, so an instruction the investigation
followed would find no `git` and no `gh` to follow it with. And the filing turn, which does have
them, never sees the raw evidence — it sees one promoted ledger entry with every agent-authored
field inside a fence, instructed to open a pull request about the finding and to abort if the fenced
text asks for anything else. The fence marker is fixed rather than unguessable, because what stops
content forging it is that both markers are defanged inside the content before the block is
assembled, not that a finding cannot guess them.

That is a boundary, not a proof. The investigating model still decides what to write into a finding,
and a finding is what the filing turn acts on. The fence keeps injected text from reading as
instructions; it does not keep a planted finding from being filed. The gate is the only thing that
does, and it is a weak thing to lean on — `high` needs three occurrences in a day, but the shipped
`critical` rule is `minOccurrencesPerDay: 1`, so a first-hour finding graded `critical` is filed on
the first hour. `maxPullRequestsPerDay: 3` is the backstop. So the honest statement of the worst
case in `upstream` mode is a pull request a maintainer reads and rejects, three times a day, and
`report-only` as the default is the answer to it — which is why the modes that hold a credential are
opt-in and why §7's gate is per-install configuration rather than a constant.

## 11. Limits

- **Revision identification depends on the build passing `GIT_SHA`.** The stamp in §2 is written by
  the Dockerfile from a build argument, and every build path in this repository that publishes an
  image passes it (`docker-build.yml` does not, and does not need to: it builds with `push: false`
  and publishes nothing) — but a build that does not produces an image the loop refuses to
  investigate, which is the intended failure and not a silent one.
  `selfImprovement.allowUnstampedImage` accepts the risk and reads source at `main` instead. Which
  ref that is is not configurable — `selfimprove_run.DEFAULT_FALLBACK_REF` is the constant `"main"`,
  so an install pinned to a branch of its own gets `main` here and a finding whose line numbers
  belong to neither tree. Every finding says the source was read at a fallback ref.
- **Cross-install deduplication is out of scope.** Each install's ledger is its own, so the same bug
  found on ten installs is ten findings and, above the gate, up to ten pull requests. The mitigation
  is `report-only` as the default; a shared ledger would need a service this project does not have.
- **The SQLite stores are unreachable**, so the signal classes that depend on session and board
  history (§4, rows 2b, 3 and 4) run on weaker evidence. §3.4's snapshot path is designed and not
  built, so there is no configuration that lifts this. Row 5 is in the same position for a different reason: its first-named
  evidence, `last_delivery_error` on a cron job, is in the cron store on the agent's volume, and
  the log fallback is partial, and inverted from what is useful — the degraded verdict, where the
  report did arrive, is the one `deploy/docker/plugins/chat/adapter.py` writes to the log
  (`logger.warning`, line 305), while the failures that lost the message entirely are returned to
  the scheduler as an `error` string and never logged: an unset relay API key, and every transport
  outcome `_post` reports. So the class of delivery failure the loop can see from logs alone is the
  one that still reached the channel.
- **A dirty build is a third state, between stamped and unstamped.** `git describe --dirty` yields
  a `-dirty` suffix, which is not a ref; the run fetches the base commit and investigates source
  that is known to differ from what the pod is executing. It proceeds rather than refusing, with
  the warning in the brief, because a dev-rebuild install is exactly where the loop is most useful
  — but line numbers in a finding from such a run are not reliable, and the finding says so.
- **The filing turn holds the write path in the same pod as the investigation.** The proxy shims
  are off the investigate turn's `PATH` and its deny policy refuses the argv shapes that would
  abuse a token, but the proxy is a sidecar on unauthenticated loopback and neither is a boundary.
  The structural fix is a second pod for filing, with the investigation's findings handed to it as
  data; it is not built here. `agents/selfimprove/SOUL.md` tells the agent the same thing directly.
- **A repository can still name a program for the sidecar to run.** Both containers run as uid
  10000 and share the checkout on an `emptyDir`, so a turn in the agent container can write
  `.git/config` in the tree that the sidecar's next `git` command reads. Git has no switch that
  turns repo-local config off, so `credential_proxy.HARDENED_GIT_CONFIG` pins the settings that
  matter through `GIT_CONFIG_COUNT`, which outranks the repository the way `-c` does: hooks, the
  filesystem monitor, pager, editor, sequence editor, `ext://` transport, credential
  helpers, `core.sshCommand` and commit signing. The credential helper needs a second step. An empty value resets the
  accumulated list rather than setting a value, and there is one list however it was filled, so the
  reset drops a repo-local `credential.https://github.com.helper` and the entry `gh auth setup-git`
  wrote alike; `credential_proxy.gh_credential_helpers` reads the latter back out of the sidecar's
  own global config and appends it after the reset. The pair is what stops the next
  `git credential approve` handing the password to a helper the repository named, while leaving the
  push authenticated. That is a list of what is worth pinning and not a proof about the rest, and
  two gaps in it are known: `diff.external` has no value that means "no external diff" — git
  executes an empty one — so pinning it would replace one code-execution setting with a `git diff`
  that always fails, and it is deliberately not pinned, the same tradeoff
  `docs/credential-isolation-design.md` makes for the trusted path; and a `.gitattributes` in the
  tree can attach a `filter.*` or `diff.*.textconv` driver to a path, which repo-local config then
  defines. Reaching the second needs a turn that is already writing files in the checkout, which is
  the same precondition as the bullet above and has the same fix: the tree stops being writable by
  whatever asks for the commit only when filing moves to its own pod.
- **Path containment reads argv conservatively, and can refuse a legitimate command.** With
  `CREDENTIAL_PROXY_UNTRUSTED_WORKSPACE` on, the proxy resolves _every_ argv token against the
  working directory and refuses the command if any of them lands outside the workspace, splitting a
  flag's value off first so that `--body-file=<path>` is tested as the path it opens. Resolving
  everything rather than the tokens that look like paths is what catches a token with no `..` and
  no leading `/` that reaches out through a symlink — and the runner can plant one, since both
  containers mount the same checkout. It costs nothing in false refusals that a shape test avoided,
  because prose resolves to a path under the checkout: `..` or `/` _inside_ a word is not a path
  component, so `-m 'fix: the loader resolves ../x'` and a title naming `/etc/passwd` both survive.
  What does get refused is a token that _begins_ with `/` — a commit message or pull request title
  starting with an absolute path, which Conventional Commits already puts a type in front of. The
  turn then sees a blocked command rather than a validation error. The trade is deliberate: the
  alternative is a denylist of the flags that take a path, and that list is the part that grows.
  The flag is off by default and the Platform Agent does not set it.
- **Harness findings cannot be fixed by this loop.** A change to Hermes behaviour is either an
  upstream request to Nous Research or a new anchored patch under `deploy/docker/patches/`, and the
  patch harness demands an applier with exact-count assertions, a verifier that proves the patched
  code behaves, and a unit suite. The loop files the finding and the attribution; the patch is a
  human decision.
- **Two runs at once are serialised by re-reading, not by locking.** `concurrencyPolicy: Forbid`
  keeps the CronJob's own Jobs apart and does not cover a `kubectl create job --from=cronjob/…`,
  which is how an operator tests the loop. The gate runs on a ledger read before the investigation,
  so a second run reaching the filing loop half an hour later holds the same promotions and would
  open the same pull requests against a budget it thinks is untouched. `refresh_ledger` re-reads the
  ConfigMap and re-asks the gate for that one fingerprint immediately before each filing turn, which
  catches the other run once its promotion is written. What it cannot catch is two runs inside the
  same filing turn: both read before either wrote, and both file. Closing that needs a claim on the
  finding before the turn starts, and the ledger is a ConfigMap patched under a `resourceVersion`
  precondition — it could carry one, but the loop does not take it today.
- **The loop cannot validate a fix against a running install**, by construction. Everything it
  proposes is reviewed and exercised by a human or by CI. That is the correct division: it is a
  detector with a strong evidence habit, not an autonomous committer.
