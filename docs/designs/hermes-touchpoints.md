# Hermes touchpoint inventory

> **Status:** current-state audit, not a design. This document enumerates every place
> `kube-agents` reaches into upstream `nousresearch/hermes-agent` — its source, its plugin
> API, its databases, its on-disk layout — and classifies each touchpoint by what would
> have to happen for it to go away. It describes `main` as it ships today. When a
> touchpoint is removed, delete its row here in the same pull request.

Hermes is consumed as an image and then rewritten at build time. That is a fork maintained
inside a Dockerfile, and every upstream bump is a bet that the anchors still hold. The
harness-v2 direction is to demote Hermes to one runner behind a contract, which means
knowing exactly what is coupled before anything can be uncoupled. This is that list.

`make patch-metric` counts the coarse version of the same thing — 13 patch sets, 12
verifiers, 39 Dockerfile references — and fails if any count grows. The counts in
[Build-time patches](#build-time-patches-into-hermes-source) must agree with it; if they
diverge, one of the two is stale.

## How to read the classification

Every touchpoint carries one of three verdicts.

**Dies** — a named milestone removes it, and that milestone is not done until this row can
be deleted. The milestone identifier is the ticking item.

**Stays** — the touchpoint uses a supported, documented interface and survives the runner
boundary intact. It costs nothing to keep and is not evidence of coupling.

**Decide** — nothing in the plan removes it and it is not obviously harmless. Each of these
states the open question. They are the reason this document exists: the patch count is easy
to see and these are not.

## Build-time patches into Hermes source

Thirteen `apply_*.py` sets under `deploy/docker/patches/`, invoked from
`deploy/docker/Dockerfile`. Each uses `patchlib.Patch` — exact-count literal anchors and/or
AST locators, an `ast.parse` gate, an atomic commit — so a drifted anchor fails the image
build rather than silently skipping. Twelve ship a paired `verify_*.py`; the thirteenth is
covered differently, noted below.

| Patch set                   | Patches                                                                                      | What it fixes                                                                                                       | Verdict            |
| --------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `cron_run_scope`            | `cron/scheduler.py`, `tools/cronjob_tools.py`, `tools/kanban_tools.py`                       | A dispatched cron run discards its final response and inherits `HERMES_KANBAN_TASK`, closing the caller's card.     | Dies with **M3.3** |
| `cron_tick_lock_scope`      | `cron/scheduler.py`, `tools/cronjob_tools.py`, `hermes_cli/cron.py`                          | `tick()` holds one global lock across the whole wait, so one fleet audit starves every other job.                   | Dies with **M3.3** |
| `cron_skip_ledger`          | `cron/executions.py`, `cron/scheduler.py`, `cron/jobs.py`, `agent/monitoring/cron_health.py` | No `skipped` status exists, so a watchdog that stops firing looks like one with nothing to report.                  | Dies with **M3.3** |
| `cron_tirith_scan`          | `tools/approval.py`                                                                          | Under `approvals.cron_mode: approve` the content scanner is consulted zero times.                                   | **Decide**         |
| `kanban_notifier`           | `gateway/kanban_watchers.py`                                                                 | Terminal-event delivery: truncated handoffs, dropped results, a wake message costing a full model turn.             | Dies with **M3.5** |
| `kanban_worker_tools`       | `tools/kanban_tools.py`                                                                      | Worker-only lifecycle tools are shipped to orchestrator profiles — 10,041 characters of schema on every call.       | Dies with **M3.2** |
| `kanban_result_required`    | `tools/kanban_tools.py`                                                                      | Cards close `done` with `result IS NULL` and the answer never reaches the user.                                     | Dies with **M4.1** |
| `kanban_scheduling`         | `hermes_cli/kanban_db.py`                                                                    | Self-parenting deadlock, claim tokens outliving the process, a circuit breaker that does not survive its own tick.  | Dies with **M3.2** |
| `kanban_guardrail_exit`     | `agent/conversation_loop.py`, `agent/turn_finalizer.py`                                      | A worker leaves the conversation loop without a board write; the reaper stamps a failure with no explanation in it. | Dies with **M4.1** |
| `kanban_wake_nudge`         | `hermes_cli/kanban_db.py`, `gateway/kanban_watchers.py`                                      | Dispatcher and notifier are pure 5s polls, so every hop of a task chain pays for rows already committed.            | Dies with **M3.2** |
| `kanban_auto_subscribe`     | `tools/kanban_tools.py`                                                                      | A worker's child cards do not inherit its chat subscription, so the user's thread loses the fan-out.                | Dies with **M3.5** |
| `kanban_comment_status`     | `tools/kanban_tools.py`                                                                      | Commenting on a done card silently does nothing and reports success.                                                | Dies with **M3.5** |
| `mcp_remote_forward_errors` | `/opt/mcp-remote` `src/lib/utils.ts`                                                         | A rejected forward is logged and dropped, leaving the client waiting on a request id that never returns.            | **Stays**          |

`mcp_remote_forward_errors` is the exception in three ways and the only one that is not
Hermes coupling at all: it patches `/opt/mcp-remote`, it is the sole patch set with no
runtime companion module, and it has no `verify_*.py` — its build-time proof is a `grep -q`
against the built `dist/*.js`. It is upstream geelen/mcp-remote issue #293 / PR #308; drop
it when #308 merges and the pin advances. Nothing in this plan touches it, so it stays, and
it is why the ratchet's floor is one rather than zero.

`cron_tirith_scan` is the one patch set the plan does not obviously absorb. Moving cron to
Routines (M3.3) removes the cron scheduler, but the defect is in `approvals.py` — the
content scan is skipped on the `approve` arm regardless of who scheduled the run. Phase 2
moves enforcement to the credential proxy, which would make the Hermes-side scanner
redundant, but only if the proxy's coverage is a superset of Tirith's. **Open question:**
does the M2.1–M2.3 proxy enforcement subsume the Tirith content scan, or is the scan
catching a class of prompt-injection content the proxy's argv-level rules cannot see? Answer
before deleting this patch.

Two further Hermes source edits are **not** applier-based and so are invisible to
`make patch-metric`: `deploy/docker/Dockerfile:55-62` applies inline `sed`/`python3 -c`
rewrites to the bundled `google_chat` adapter — upstream PR #51567's nested-inline-formatting
and thread-creation fixes, plus explicit target routing in `send_message_tool.py`. Both are
`grep -q` guarded, so they hard-fail the build on drift. They die with **M3.5**. M1.3 should
count them.

## Runtime monkey patches via `sitecustomize.py`

`agents/platform/scripts/sitecustomize.py` is installed to `/opt/defaults/scripts/` and
activated implicitly: the operator sets `PYTHONPATH=/opt/defaults/scripts` on the agent
container (`k8s-operator/internal/controller/platformagent_manifests.go:1361`), so **every
Python process in the pod auto-imports it**. It is never referenced by name anywhere else in
the repository, which makes it the least discoverable coupling in the inventory.

It installs nothing inline. It conditionally invokes two installers, each gated on an
environment variable and each swallowing `ModuleNotFoundError` for `gateway`/`plugins` so
that credential-free wrapper scripts running under the system Python are unaffected.

| Installer                         | Gate                    | Patch targets                                                                                                                                                                                        | Verdict            |
| --------------------------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `google_chat_relay_patch.install` | `GOOGLE_CHAT_RELAY_URL` | `PlatformRegistry.create_adapter`, then four `GoogleChatAdapter` methods: `connect`, `disconnect`, `_new_authed_http`, `_handle_setup_files_command`                                                 | Dies with **M3.5** |
| `slack_relay_patch.install`       | `SLACK_RELAY_URL`       | `PlatformRegistry.create_adapter`, four module-attribute rebinds of `AsyncWebClient`/`AsyncApp` (including two inside `slack_bolt` itself), seven `SlackAdapter` methods, and `HTTPError.fp`/`.read` | Dies with **M3.5** |

Both exist for a good reason — the pod holds no chat credentials, so all traffic is proxied
through the credential-proxy sidecar — and the relay shape is what Harness v2 wants anyway.
What dies is patching a vendored adapter to get it. Note the depth: the Slack patch has to
rebind `AsyncWebClient` inside `slack_bolt.app.async_app` and `slack_bolt.context.async_context`
because `AsyncApp._init_context` builds a fresh client per request and ignores the one it was
handed. That is coupling to a transitive dependency's internals, two layers below Hermes.

Only the Slack patch has a test (`agents/platform/scripts/test_slack_relay_patch.py`). The
Google Chat patch has none, and it depends on undocumented adapter internals —
`_shutting_down`, `_on_pubsub_message`, `_thread_count_store.load`, `_load_cached_bot_id`,
`_mark_connected`, `_mark_disconnected`. **Open question:** should M3.5 be preceded by a
characterisation test for the Google Chat relay, given the extraction has to preserve
behaviour nothing currently pins down?

## Plugins written against the Hermes plugin API

Nine plugins plus one hook. All are loaded by Hermes calling a module-level `register(ctx)`
and declared by a sibling `plugin.yaml`. Using the plugin API is not itself coupling —
importing Hermes internals from inside a plugin is. The last column names what each one
reaches for beyond the documented interface.

| Plugin                      | Where                                           | Beyond the plugin API                                                                                                                                      | Verdict            |
| --------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `session_store`             | `agents/chat/defaults/plugins/`                 | Nothing — stdlib, one hook, duck-typed event                                                                                                               | **Stays**          |
| `session_otel_bridge`       | `agents/chat/defaults/plugins/`                 | Imports `hermes_plugins.hermes_otel.tracer` and monkey patches `start_span`                                                                                | **Decide**         |
| `tool_call_audit`           | `agents/chat/defaults/plugins/`                 | Nothing — stdlib, five hooks                                                                                                                               | Dies with **M1.1** |
| `bootstrap_onboarding`      | `agents/chat/defaults/plugins/`                 | `gateway.session_context`, `cron.jobs`, `plugins.platforms.google_chat.adapter`; mutates `GoogleChatAdapter.splits_long_messages`                          | Dies with **M3.5** |
| `legacy_slash_commands`     | `agents/chat/defaults/plugins/`                 | `hermes_cli.commands.slack_subcommand_map`, plus a hardcoded mirror of an alias the adapter adds afterwards                                                | Dies with **M3.5** |
| `multiuser_memory`          | `agents/platform/plugins/memory/`               | `agent.memory_provider`, `tools.registry`, `utils`, `hermes_cli.config`; subclasses `MemoryProvider`                                                       | **Decide**         |
| `incident_context`          | `agents/platform/plugins/`                      | Nothing — stdlib, HTTP to the local session-kv server                                                                                                      | **Stays**          |
| `gke-stockout-investigator` | `agentplugins/gke-stockout-investigator/files/` | Nothing — `ctx.register_skill` only                                                                                                                        | **Stays**          |
| `pubsub-platform`           | `agentplugins/pubsub-platform/files/`           | Six internal modules across `gateway`, `tools`, `agent`; subclasses `BasePlatformAdapter`; reaches into `gateway_runner.adapters`; shells the `hermes` CLI | Dies with **M3.3** |
| `chat_message_audit` (hook) | `agents/chat/defaults/hooks/`                   | Nothing — stdlib; a `HOOK.yaml` `handle()` rather than `register(ctx)`                                                                                     | Dies with **M1.1** |

`tool_call_audit` and `chat_message_audit` both die with the runner contract rather than
with a chat or cron milestone: once tool calls and turn boundaries are events on the M1.1
stream, an audit sink consumes the stream instead of registering Hermes hooks. They are the
clearest case in the inventory of coupling that the seam dissolves without anyone
deliberately removing it.

`pubsub-platform` is the deepest importer in the repository and also the most clearly
doomed: it is a trigger wearing a chat adapter's clothes. M3.3 turns triggers into Routines,
which is exactly what a Pub/Sub subscription that files kanban cards should have been.

Two need decisions:

- **`session_otel_bridge`** patches another plugin's tracer because, in its own words,
  "Hermes does not currently expose a span-attribute provider hook." It also validates the
  upstream signature at install time and raises if `session_id`/`attributes` disappear —
  honest, but it means an upstream bump can fail the pod at import. **Open question:**
  upstream a span-attribute provider hook, or make attribution a runner-contract concern so
  the bridge reads from the event stream? M3.4 threads a principal through; if spans get
  stamped there, this plugin goes away with it.
- **`multiuser_memory`** reasons explicitly about Hermes internals it cannot import —
  `plugins/memory/__init__.py` re-running `register()`, `agent/agent_init.py` freezing
  `agent._user_id`, `build_session_key()` omitting the participant id inside a thread. It is
  currently inert (the operator renders `memory.memory_enabled: false`) while still being
  named as the provider. **Open question:** is per-user memory a runner responsibility or a
  control-plane one? If the latter, this plugin is replaced rather than ported, and the
  decision belongs with M3.4.

Enablement, for the record: `agents/chat/config.yaml` enables six, `agents/platform/config.yaml`
three, `agents/cluster/config.yaml` one. `hermes_otel` is upstream-bundled and its config is
rewritten twice — once at build (`deploy/docker/Dockerfile:636`) and once at startup
(`deploy/shared/docker-entrypoint.sh:429-435`).

## Direct SQLite access into Hermes-owned databases

This is the coupling class M1.2 exists to kill. Reaching past the `hermes_cli.kanban_db` API
into the tables underneath means an upstream schema change is a silent data corruption rather
than an import error.

**One repository-owned script does it.** `agents/platform/scripts/kanban_notify_propagate.py`
opens `$HERMES_KANBAN_DB` directly and reads `sqlite_master` and `tasks`, then reads and
writes `kanban_notify_subs`. It pins a five-column contract (`platform`, `chat_id`,
`thread_id`, `user_id`, `notifier_profile`) plus `task_id`, `created_at`, `last_event_id`. It
is invoked from prompt text, not code — `agents/cluster/AGENTS.md:17` tells the Cluster Agent
to shell out to it. It is fail-soft and always exits 0, so a schema drift would degrade
silently into "the user stops getting replies." **Dies with M1.2**; this is the named
offender in the milestone.

**Six shipped patch modules also execute raw SQL**, but they live inside `/opt/hermes` as
part of their patch sets and die with them: `kanban_auto_subscribe` and `kanban_comment_status`
(→ M3.5), `kanban_scheduling`, `kanban_wake_nudge` and `kanban_guardrail_exit` (→ M3.2/M4.1),
and `cron_skip_ledger` (→ M3.3). `cron_skip_ledger` is worth singling out: it performs live
**DDL** on a Hermes-owned table, rebuilding `executions` under `BEGIN IMMEDIATE` through a
scratch table to widen a `CHECK` constraint. That is the highest-risk single operation in the
inventory.

**`session_kv.db` is ours, but it is a cross-boundary contract.** The file at
`/var/lib/kube-agents/session/session_kv.db` is owned and written by
`agents/platform/scripts/session_kv_server.py`, yet two of its four readers are Hermes
plugins (`session_store` writes it, `session_otel_bridge` reads it) and the path is a
constant in Go (`platformagent_manifests.go:49`), in `platform_mcp_server.py`, and in two
documents. **Stays** as a store; the question of who reads it rides along with the
`session_otel_bridge` decision above.

Three negative findings, verified rather than assumed, that narrow the surface usefully:

- **No Go code opens SQLite.** There is no `database/sql` or `mattn/go-sqlite3` anywhere in
  `k8s-operator/`. The event watcher only scans `/opt/data/profiles` for `kubeconfig.yaml`
  and `config.yaml`.
- **No shell script shells out to the `sqlite3` CLI.** `deploy/shared/`, `scripts/`, `hack/`,
  the three lifecycle installers, `bench/` and `agentplugins/` are clean. The only mentions
  are a Dockerfile comment recording that a wedged worker once escaped by doing so, and a
  documented manual recipe.
- **`agents/chat/scripts/platform_cron_dispatch.py` couples to the kanban CLI, not the DB**
  (`from hermes_cli.kanban import run_slash`). That is the correct side of the boundary and
  needs no change for M1.2.

So the M1.2 exit criterion — "direct SQLite writes: 0" — is one script away, not a program.

## Operator-rendered Hermes configuration

`k8s-operator/internal/controller/platformagent_manifests.go` renders Hermes' `config.yaml`,
per-profile overlays, and the container environment. Every key name in it is Hermes
vocabulary that a second runner would not recognise.

The `spec.harness.hermes` CRD stanza (`k8s-operator/api/v1alpha1/common_types.go:38-56`)
names the runner in the API itself: `dashboardEnabled`, `pluginsDebug`, `agentHome`,
`apiServerSecretRef`. **Decide** — M4.2 introduces a second runner, at which point a
`hermes:`-keyed stanza is either deprecated in favour of a runner-agnostic shape or accepted
as permanently runner-specific. This is an API-compatibility decision, not a code one, and it
should be made before M4.2 rather than during it.

Two of those fields have **stale doc-comments that contradict the code**, found while
compiling this inventory and unrelated to any milestone:

- `dashboardEnabled`'s comment claims it "toggles the `AGENT_DASHBOARD` environment
  variable". It does not; no such variable is ever set. `isDashboardEnabled()` only decides
  whether the dashboard container is added.
- `pluginsDebug`'s comment names `AGENT_PLUGINS_DEBUG`. The operator emits
  `PLATFORM_AGENT_PLUGINS_DEBUG`.

Both are worth a small standalone fix; a reader setting `AGENT_PLUGINS_DEBUG` by hand gets
nothing and no error.

The rendered `config.yaml` carries Hermes-shaped keys throughout — `model.*`, `terminal.*`,
`mcp_servers.router`, `platform_toolsets`, `toolsets`, `agent.disabled_toolsets`, `kanban.*`,
`approvals.cron_mode`, `memory.*`, `platforms.*`, `plugins.enabled`, `leader_election.*`.
`agent.disabled_toolsets` is the sharpest example: a hardcoded list of 24 Hermes toolset
names, maintained by exclusion. **Dies with M1.1** in the sense that the runner contract
should declare capabilities positively rather than have the control plane enumerate a
foreign runner's features to switch off; the rendering itself survives until M4.2 proves a
second runner needs a different shape.

Two values in the environment are Phase 2 business, not Phase 3:

- `API_SERVER_KEY` is set to the literal `"cluster-internal-trusted"`. **Dies with M2.4** —
  this is the shared symmetric key that milestone exists to remove.
- `model.api_key` is rendered as the literal `"none"`. **Dies with M2.4** alongside it.

Three structural assumptions are **Decide**, all of the same shape — they are properties of
the Hermes image that the operator states as facts:

- **UID/GID 10000**, asserted twice (`fsGroup` at line 1132, `RunAsUser` at line 1454), both
  commented as matching "the canonical unprivileged 'hermes' runtime user created in
  NousResearch/hermes-agent upstream Dockerfile". A second runner with a different UID
  breaks the PVC.
- **`/opt/hermes/.venv/bin/python3`** hardcoded in three places: the MCP router command, the
  leader-election argv, and the container `PATH`.
- **`defaultAgentHome = "/opt/data"`** duplicated as a literal at line 1159 and again as the
  fluent-bit mount at line 2109.

**Open question for all three:** does the M1.1 runner contract describe the runtime
(user, interpreter, home) as declared runner properties, or does the operator keep
per-runner branches? The contract is the better home, but M1.1 as scoped covers dispatch,
not packaging. Worth an explicit paragraph in `09-runner-contract.md` saying which.

`SensitiveEnvVars` hard-blocks user override of exactly `API_SERVER_KEY` and `HERMES_HOME` —
the second of which the operator never sets. That is the entrypoint's job.

## Entrypoint assumptions

`deploy/shared/docker-entrypoint.sh` (460 lines, `/bin/sh`) is where the Hermes on-disk
contract is actually asserted. It sources nothing; it invokes helpers.

`HERMES_HOME` originates here, not in the operator — the script exports it equal to
`${PLATFORM_AGENT_HOME:-/opt/data}`. `INSTALL_DIR` is hardcoded `/opt/hermes` with no
override. From there it assumes: the venv interpreter at `/opt/hermes/.venv/bin/python3`
(used at eight call sites), upstream's `/opt/hermes/docker/stage2-hook.sh`, a
`/opt/hermes/.playwright` browser tree, `hermes` on `PATH`, and `yaml` plus `uvicorn`
importable inside the venv.

The profile tree is equally Hermes-shaped: the default profile's config is
`$TARGET_DIR/config.yaml` and is deliberately _not_ under `profiles/`; named profiles live at
`profiles/<name>/`; and the scaffold marker is `profiles/<name>/profile.yaml`, **written only
by `hermes profile create` and shipped by no template**. That last one is load-bearing — it
is the idempotency gate for the whole startup sequence, and it is a file whose existence only
the Hermes CLI can cause. **Dies with M3.2**, where `AgentSession` owns session and profile
lifecycle; until then a second runner cannot satisfy the gate.

Two limits the script records about itself are worth carrying forward because neither is a
Hermes problem and neither is on the plan:

- **Step 1 runs above the ownership gate.** `stage2-hook.sh` executes in _every_ container,
  including the ones set to `skip`, so "the sidecar does not write to the PVC" is explicitly
  false and `$TARGET_DIR/logs` existing is not evidence the setup ran. The script names the
  fix: move setup into an initContainer. **Decide** — worth doing, unrelated to Harness v2,
  and it gets harder once M3.2 adds a session controller on top.
- **Port 8699 is pod-wide and unscopeable.** The session-kv server binds `0.0.0.0:8699` and
  every container in the pod can reach it. **Decide**, and it is security-adjacent enough to
  belong in Phase 2's thinking even though no milestone names it.

The overlay mechanism adds and overwrites but never prunes, so a skill dropped from the image
survives on the PVC indefinitely. **Stays** — a known, documented limitation of the current
scaffolder, and not coupling to Hermes as such.

## What this means for the ratchet

M1.3 extends the metric beyond patch-set counts. Based on this inventory the additional
countable quantities are:

| Quantity                                          | Count today |
| ------------------------------------------------- | ----------: |
| Patch sets (`apply_*.py`)                         |          13 |
| Non-applier Hermes source edits in the Dockerfile |           2 |
| `sitecustomize` patch targets                     |          19 |
| Plugins importing Hermes internals                |           5 |
| Repository scripts writing Hermes tables directly |           1 |

Every one of those numbers should only ever fall. The five plugins are
`session_otel_bridge`, `bootstrap_onboarding`, `legacy_slash_commands`, `multiuser_memory`,
and `pubsub-platform`; the nineteen `sitecustomize` targets are five Google Chat and
fourteen Slack.

Nineteen is the count of assignment _sites_, not of distinct attributes, and the difference
is a decision rather than an accident: both installers rebind
`PlatformRegistry.create_adapter`, chained one over the other, so there are eighteen
distinct attributes and nineteen places that write one. Sites is the right unit for a
ratchet — deleting the Slack patch removes real coupling that a distinct-attribute count
would report as no change, because the Google Chat patch still binds the shared attribute.

## Open questions, collected

Seven decisions this inventory surfaced that no milestone currently owns. They are listed
here rather than only inline so that a reviewer can find them in one place.

1. Does Phase 2 proxy enforcement subsume the Tirith content scan (`cron_tirith_scan`)?
2. Should M3.5 be preceded by a characterisation test for the untested Google Chat relay patch?
3. Span attribution after `session_otel_bridge`: upstream hook, or runner-contract concern folded into M3.4?
4. Is per-user memory a runner responsibility or a control-plane one (`multiuser_memory`)?
5. Does `spec.harness.hermes` survive M4.2, and in what shape?
6. Does the runner contract declare runtime properties (UID 10000, interpreter path, home), or does the operator branch per runner?
7. Two pre-existing items with no Harness v2 connection: move entrypoint setup into an initContainer, and scope the pod-wide port 8699.
