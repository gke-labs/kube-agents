---
name: cluster-agent-lifecycle
description: Create, delegate to, and tear down per-cluster Cluster Agent Hermes profiles. Use whenever a GKE cluster is onboarded or deleted, or whenever a single-cluster runtime debugging/operations task should be delegated to that cluster's Cluster Agent.
---

# Cluster Agent Lifecycle Skill

As the Platform Agent you own the lifecycle of **Cluster Agents**. A Cluster Agent is a Hermes _profile_ — an isolated agent instance with its own persona (`SOUL.md`), scoped toolset, and home directory — that you create dynamically **inside your own pod**, one per managed GKE cluster. It handles read-only runtime operations and deep workload diagnostics on that single cluster, and returns its findings to you.

You never debug tenant workloads directly while the cluster has a Cluster Agent. You delegate that to it and act on what it returns.

The engine for all of this is the helper script `scripts/cluster_agent_profile.py` (resolved at `/opt/data/scripts/cluster_agent_profile.py` at runtime).

## When to create a profile

Create the Cluster Agent profile as part of **cluster onboarding** — immediately after a cluster is successfully provisioned (see `gke-cluster-creation`) or when an existing cluster is first brought under management (see `manage-cluster`). A managed cluster and its Cluster Agent profile are created together: when you onboard or tear down a cluster, keep the profile in step with it. That rule is scoped to onboarding and teardown, where the profile is yours to manage. It is not a roster invariant to enforce from other work — the reconcile job below owns the roster, and an empty roster is a supported state, not damage to repair. In particular, the first-run discovery sweep must never create or repair profiles.

```bash
python3 /opt/data/scripts/cluster_agent_profile.py create \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This scaffolds the profile home on the persistent data PVC, pins a kubeconfig scoped to that cluster, writes the cluster identity into the profile's `USER.md`, and registers the profile. It is **idempotent** — safe to re-run. It prints the profile name.

## How to delegate a debugging / runtime-ops task (kanban board)

For any request that concerns runtime behavior of workloads on a **single, specific** cluster (crash loops, OOMs, scheduling failures, mount errors, connectivity, autoscaling, storage, observability gaps), delegate to that cluster's Cluster Agent instead of investigating yourself.

**Personas never pass context directly.** Delegation runs on the shared **kanban board**: you create a card assigned to the cluster's profile; the gateway's kanban dispatcher **auto-spawns** the Cluster Agent to work it; it reports a structured result on the card. You do **not** invoke the agent yourself.

1. **Resolve the cluster's profile name** (the kanban `assignee`):

   - **If the request names a specific cluster**, resolve its profile name with the `get_cluster_profile_name(project, cluster, location)` tool. It returns `name` and `exists`; both run in the agent pod — do not look the name up with `cluster_agent_profile.py name`, which is a stub in your shell, and do not block on it.
     - Assign only to a profile that `exists`. A card for a name that is not a profile is never dispatched.
     - If it does not exist, the cluster has no usable Cluster Agent: none yet (the hourly reconcile job below creates one for every cluster in scope), a scaffold that never finished, or a profile under that name pinned to a different cluster. Investigate it yourself and say in your `result` that it had no Cluster Agent.

   - **If the request does NOT name a cluster** (names only a namespace or workload):
     **Do not ask the user which cluster before searching.** You have fleet-wide read visibility and per-cluster Cluster Agents; the user does not. Resolve the cluster before asking:
     1. **Enumerate the fleet:** call the `list_cluster_profiles()` platform MCP tool. It runs in the agent pod and returns every ready profile with its `name`, `project`, `cluster`, and `location`; do not run `cluster_agent_profile.py list`, which is a stub in your shell.
     2. **Fan out read-only existence checks:** create one card per profile in one burst **with no `parents`**, asking each Cluster Agent whether the target namespace/workload exists on its cluster. Because child cards inherit the creator's chat subscription rows, instruct workers to record existence in `metadata` (`metadata={'existence': true|false}`) and keep `result` minimal or silent (`[SILENT]`) so throwaway probe completions do not pollute the requester's chat thread: e.g. `kanban_create(assignee="<profile>", title="Check existence: <workload> in <namespace>", body="Probe only: check whether namespace '<namespace>' or workload '<workload>' exists on this cluster. Return existence in metadata (metadata={'existence': true|false}). Keep result minimal or silent ([SILENT]) so the probe does not clutter the user's chat thread.")`.
     3. **Wait and poll to settlement:** poll each probe card with `kanban_show(<id>)` (`sleep 60` between rounds per `SOUL.md` §6) until all fanned-out probe cards have settled (`done` or `archived`) or blocked (`needs_input`). All existence probes must reach settlement before you delegate and complete your card: never abandon an unsettled probe queued in `ready` or rely on the single refusal nudge waiver to complete past unfinished children — an orphaned probe left in `ready` keeps its inherited chat subscription, is claimed by the dispatcher later, and posts an unwanted completion into the chat thread after the user has already received the final report. Do NOT classify cards in `ready` as timed out based on fixed wave counts or assume a static concurrency: the kanban dispatcher enforces a board concurrency cap (`max_in_progress`, default 2, configurable per install via `spec.harness.tuning.maxInProgress`), so cards legitimately queue in `ready` with a NULL `claim_lock` until earlier waves or concurrent tasks finish and free concurrency slots across the host. Keep polling as long as probes are running or progressing through the queue; do not time out queued cards in `ready` while other cards are actively running or finishing. Only if no cards are progressing or a probe remains completely stalled in `running` without state changes across at least 5 polling rounds should it be treated as timed out.
     4. **Evaluate the findings:**
        - A probe that completes with `existence: true` (or confirms workload presence in `result` or `metadata`) counts as a match.
        - A probe that blocks (e.g. `needs_input`), fails, or times out after the polling budget expires does **not** count as a match for the target workload. Record any blocked/failed/unresponsive probe in your synthesis.
        - **Exactly one cluster matches:** proceed to step 2 to delegate the debugging investigation to that cluster's profile. In your report, state clearly which cluster was resolved and how (e.g. _"Resolved `payments-api` in namespace `seeded-debug` to cluster `seeded-a` after fleet discovery"_). Never resolve silently.
        - **Zero clusters match, multiple clusters match, or probes block/fail leaving ambiguity:** _then_ ask the user for clarification, stating explicitly which clusters were checked and what was found on each (including any clusters where the probe blocked or failed). Ask only after looking.

2. **Create the card** with the request in the body:

   ```
   kanban_create(
     assignee="<profile-name>",
     title="<short title>",
     body="<full request: namespace/workload, symptom, time window>"
   )
   ```

   The dispatcher spawns the Cluster Agent (`hermes -p <profile> chat -q "work kanban task <id>"`) automatically; it reads the card, does read-only diagnostics, and calls `kanban_complete(result=<the RCA>, summary=<one-line status>, metadata={...})`.

3. **Read the result** — the card carries the requester's chat subscription, so the completion (or a `needs_input` block) is posted into their thread. You can also inspect it: `kanban_show(<id>)`. The RCA is in the card's `result` — the field the gateway posts verbatim, and the only one the requester receives — with any proposed patch in `metadata`; neither is ever in the worker's chat reply, which is a bare acknowledgement by design.

**Multi-cluster (fan-out):** create one card per cluster **with no `parents`**, in one burst, so they run in parallel. `parents` means "runs after", so a per-cluster card that lists your own running card as a parent can never be claimed — see `SOUL.md` §0. Then **keep your own card open**: poll each per-cluster card with `kanban_show(<id>)` (`sleep 60` between rounds), and once all of them are settled, synthesize their `result`/`metadata` into your own `kanban_complete(result=...)`. Completing your card is the delivery, so never complete it on a dispatch receipt — the image refuses a `kanban_complete` while your fanned-out cards are unfinished (#1010). See the **`workload-rebalancing`** skill for the validation-then-declare pattern.

## Acting on the result

The Cluster Agent is **read-only** and does not open Pull Requests. After reading the completed card:

1. Review the RCA in the card's `result` and the proposed manifest patch in its `metadata`.
2. If a change is warranted, **you** open (or update) the Pull Request via the `submit-suggestion` skill — you own the GitOps write path. Reconcile against any existing branch/PR for the same workload before creating a new one.
3. Report the outcome to the user as a clean SRE status update.

## When to delete a profile

Delete the Cluster Agent profile as part of **cluster teardown** (see `gke-cluster-creation`), after the cluster itself is removed:

```bash
python3 /opt/data/scripts/cluster_agent_profile.py delete \
  --project "<project>" --cluster "<cluster>" --location "<location>"
```

This deregisters the profile and removes its home directory. Do not delete a profile while its cluster still exists.

## Automatic reconciliation (create and prune)

The roster is also reconciled automatically. An hourly, deterministic `no_agent` cron job
(`cluster-agent-reconcile`) runs `scripts/cluster_agent_reconcile.py`, which drives the roster in
both directions:

- **Create** — every cluster in every project in scope gets a profile, including the management
  cluster kube-agents itself runs on. The scope is the management project alone unless the
  `PlatformAgent` declares `spec.scope`; the only exceptions are clusters named in
  `spec.scope.exclude.clusters` or, for one more release, bare names in `RECONCILE_EXCLUDE`. A
  project the pod cannot list, or a folder or organisation it cannot read (whose previous members are then carried forward frozen, with no CREATE under them), is recorded with its outcome in `fleet_scope.json` at the root of the data volume (`/opt/data`, beside `profiles/`; the reconcile's own `HERMES_HOME`, not a profile's) (written by every run except `--dry-run`) and
  skipped for that run rather than guessed at. The management cluster is included because its own workloads fail like any other cluster's,
  and the agent that triages a Kubernetes event is the one scoped to the cluster that raised it.
- **Prune** — a profile is deleted when its GKE cluster is definitively gone (a `NotFound` from
  `gcloud container clusters describe`), when it belongs to an excluded cluster, which must not
  carry a profile even though that cluster exists, or when its project has left the scope, over two clean runs: a clean run (no project unreachable, every folder and organisation resolved `ok` or `over-cap`, the management project resolved, listed its own clusters and unchanged since the last run, the scope file readable) that finds a previously in-scope project absent marks it `retiring` in
  `fleet_scope.json`; the next clean run prunes its profiles. A profile whose project the scope never produced, or whose prune waits for that second run, is
  kept and listed as `unmanaged`; one whose identity could not be read is kept and appears under the report's
  `skipped_no_identity`, and under the snapshot's `profiles` once an earlier run has read its identity. This closes the loop when a cluster is deleted
  out-of-band, so its profile is never left orphaned pointing at a dead kubeconfig.

A create that fails is recorded rather than only logged: the cluster goes into a `create_failed`
bucket. Both places that bucket surfaces are narrower than it looks. The chat summary names the
failures only on a run that also created or pruned something — a run whose sole outcome is a failed
create posts nothing, because a permanent cause repeats every minute while the bootstrap gate is
ticking. A failed create is only one of the things `--require-create-pass` reports. Under that flag the run
exits `3` when the CREATE direction never ran (the management project would not resolve, or
its clusters could not be listed), when `reconcile()` raised, or when it ran and every create failed with no fully
scaffolded profile already on the roster — and `4` when another reconcile holds the lock. One
failure alongside a success exits 0 and costs the sweep one `gaps` row. Without the flag every one
of those exits 0, because a cron producer must. That flag's caller is the bootstrap scan gate,
which gates a one-shot fleet sweep on the exit code, so a non-zero exit means "the roster is not
worth fanning out against yet", not specifically "a create failed".

A profile whose `cluster_identity` is stamped but whose `kubeconfig.yaml` or `USER.md` is missing is
treated as absent and recreated. Profile creation stamps the identity before it writes either file,
so a run killed mid-create leaves a home that satisfies the create direction's existence check and
the prune direction's "identity present, cluster exists" check — unrepaired, it stays half-built
forever.

It never deletes on ambiguity: any inconclusive check (auth/network/timeout, or a missing
`cluster_identity`) leaves the profile untouched. `created=0 pruned=0 kept=0` is a normal,
successful result. When it creates or prunes anything it posts a summary to every chat platform it
can resolve as enabled, falling back to Google Chat when it can resolve none.

Profile lifecycle belongs to this script and to the explicit onboarding/teardown paths above. Do
not repair the roster from other work by calling `cluster_agent_profile.py` directly — a profile
created for an excluded cluster is one the next run will prune.

To preview what would change without touching anything:

```bash
python3 /opt/data/scripts/cluster_agent_reconcile.py --dry-run
```

You still delete a profile explicitly during planned teardown (above) — reconciliation is the safety
net, not the primary path.

## Listing profiles

Call `list_cluster_profiles()`. It lists the currently provisioned Cluster Agent profiles (one per managed cluster) with the cluster each is pinned to.
