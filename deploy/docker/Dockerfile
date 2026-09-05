# check=skip=InvalidDefaultArgInFrom
#
# LAYER BUDGET. Docker's overlay2 driver refuses to mount a stack deeper than
# 128 layers ("max depth exceeded"). The deepest chain that ships --
# agent-base -> platform -- is measured against that ceiling by
# scripts/check_image_layers.py, which CI runs on every pull request. That
# docstring owns the rest of the rationale, including why the gate follows
# whichever shipping chain is deepest; do not restate it here.
#
# entrypoint-gate-test is two instructions deeper still, but it is a build-time
# assertion that buildx alone ever produces and no daemon ever mounts, so the
# gate deliberately does not follow it.
#
# The ceiling is a property of the classic Docker daemon, not of images: buildx
# has no such limit, so a build that is over budget passes `docker-build.yml`
# and the GHCR publish and then fails only in Cloud Build, on main, after merge.
# It has done exactly that twice -- build f9f1747c (2026-08-07, mitigated by
# consolidating COPYs) and again on 2026-08-12 at 132 layers, which broke six
# consecutive publishes to Artifact Registry (#658). The checker exists so the
# third time is a red pull request instead.
#
# What this means when editing: every RUN and COPY below is a layer, and the
# budget is shared by both stages of that chain. Adding one is fine;
# adding a block of them is what to avoid. Where files share a destination they
# travel in one COPY, and appliers are staged into a directory rather than
# copied in one at a time -- that is why those instructions look the way they
# do. Run the checker rather than counting by hand:
#
#     python3 scripts/check_image_layers.py
#
# Every base image is a build arg so this can be rebuilt behind a firewall,
# where the public registries are unreachable and the bases have been copied
# into an approved one. Each takes a full reference rather than a shared
# registry prefix: `make mirror-images` writes a flat layout that keeps the
# trailing name only, so gcr.io/distroless/static lands as <prefix>/distroless
# -static and no amount of prefix-splicing reproduces the original path.
#
# HERMES_AGENT_TAG has no default — the image-build workflows read the one pin
# from tags.env, and the root Makefile `include`s it — so a direct docker build has
# to pass it too, or the agent-base stage resolves to a reference with an empty
# tag:
#
#   docker build -f deploy/docker/Dockerfile --target platform \
#     --build-arg HERMES_AGENT_TAG="$(sed -n 's/^HERMES_AGENT_TAG=//p' tags.env)" \
#     --build-arg HERMES_AGENT_IMAGE=registry.example.com/mirror/hermes-agent \
#     --build-arg ENVOY_IMAGE=registry.example.com/mirror/envoy \
#     --build-arg GOLANG_IMAGE=registry.example.com/mirror/golang .
#
# The defaults are the upstream references, so an ordinary build is unchanged.
ARG HERMES_AGENT_TAG
ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent
ARG ENVOY_VERSION=v1.39.1
ARG ENVOY_IMAGE=envoyproxy/envoy
ARG GOLANG_IMAGE=golang
ARG GOLANG_VERSION=1.27-alpine
# Declared here as well as in the `platform` stage that consumes it, so the
# credential-proxy build -- which shares deploy/docker/cloudbuild.yaml and is
# therefore handed the same --build-arg -- does not warn about an argument no
# stage declares. The value and what it is for are documented where the ENV is
# set, at the end of the `platform` stage.
ARG KUBE_AGENTS_VERSION=dev
FROM ${ENVOY_IMAGE}:${ENVOY_VERSION} AS envoy-bin

# Every binary in the final image must match the architecture the image ships
# as, and nothing in this Dockerfile can pin that: the runtime base
# (nousresearch/hermes-agent) and envoy are multi-arch indexes, so the image
# follows whatever platform the build resolves to. Deployment targets are
# always linux/amd64 GKE nodes, which is why every build invocation in this
# repository passes --platform=linux/amd64 (Makefile, cloudbuild.yaml,
# dev_rebuild_agent.sh, and the docker-build / docker-publish-ghcr workflows).
# A build that omits it produces a host-arch image: internally consistent, but
# it will CrashLoopBackOff on "exec format error" if pushed to a cluster from
# an arm64 machine (#560).
#
# $BUILDPLATFORM keeps the Go toolchain running natively on the build host; it
# cross-compiles to TARGETARCH, the architecture of the image being produced.
# The :-amd64 fallback only fires on builders that don't populate TARGETARCH.
FROM --platform=$BUILDPLATFORM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS watcher-builder
ARG TARGETOS
ARG TARGETARCH
WORKDIR /workspace
COPY k8s-operator/ /workspace/k8s-operator/
WORKDIR /workspace/k8s-operator
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    go build -ldflags="-w -s" -o /workspace/k8s-event-watcher ./cmd/k8s-event-watcher/

FROM ${HERMES_AGENT_IMAGE}:${HERMES_AGENT_TAG} AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    apt-transport-https \
    ca-certificates \
    curl \
    dnsutils \
    gnupg \
    iputils-ping \
    jq \
    nano \
    patch \
    vim \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. No helm, k9s or yq. They were here for the model's shell, and the shell now
# runs in the sandbox image (deploy/sandbox/Dockerfile), which installs the two
# of them it can use at the same pinned versions. Nothing left in this container
# invokes a cluster CLI: Hermes and the Python services that used to shell out to
# one call agents/platform/scripts/sandbox_exec.py and run it over SSH instead.
# The guard at the end of this stage fails the build if any of them returns.

RUN /usr/local/bin/uv pip install --python /opt/hermes/.venv/bin/python \
    ddgs \
    google-api-python-client \
    google-auth \
    google-cloud-pubsub \
    "hermes-agent[google_chat]" \
    "hermes-agent[slack]" \
    kubernetes \
    opentelemetry-api \
    opentelemetry-sdk \
    opentelemetry-exporter-otlp-proto-http

# Apply upstream PR #51567 patch to google_chat adapter to fix nested inline formatting leaks (GC0, GC1, etc.) and standalone thread creation bugs
RUN sed -i 's/for key, value in placeholders.items():/for key, value in reversed(list(placeholders.items())):/' /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/plugins/platforms/google_chat/adapter.py'); c = p.read_text(); t = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"'; r = 'url = f\"https://chat.googleapis.com/v1/{chat_id}/messages\"\n    if thread_id:\n        url += \"?messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD\"'; p.write_text(c.replace(t, r)) if t in c else exit(1)" && \
    grep -q "reversed(list(placeholders.items()))" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    grep -q "messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD" /opt/hermes/plugins/platforms/google_chat/adapter.py

# Hotfix: Allow explicit target routing for google_chat spaces/threads in send_message_tool
RUN /opt/hermes/.venv/bin/python3 -c "import pathlib; p = pathlib.Path('/opt/hermes/tools/send_message_tool.py'); c = p.read_text(); t = 'def _parse_target_ref(platform_name: str, target_ref: str):\n    \"\"\"Parse a tool target into chat_id/thread_id and whether it is explicit.\"\"\"'; r = t + '\n    if platform_name == \"google_chat\":\n        parts = target_ref.split(\":\", 1)\n        if len(parts) == 2 and parts[0].startswith(\"spaces/\"):\n            return parts[0].strip(), parts[1].strip(), True\n        elif target_ref.startswith(\"spaces/\"):\n            return target_ref.strip(), None, True'; p.write_text(c.replace(t, r)) if t in c else exit(1)"

# A dependency-free text utility, copied in on its own and ahead of both of its
# callers. tools/cron_run_scope.py imports it a few layers below for a different
# clip entirely, and gateway/kanban_watchers.py is wired to it by the
# kanban_notifier patch much further down. See the module docstring in
# deploy/docker/patches/kanban_handoff_clip.py.
COPY --chmod=0644 deploy/docker/patches/kanban_handoff_clip.py /opt/hermes/gateway/kanban_handoff_clip.py

# Hotfix: let a worker report progress mid-run. A delegated card is silent from
# claim to completion — p50 264s and p90 1102s of nothing on the live board —
# which is the single biggest reason delegation feels slower than doing the work
# in the thread. kanban_heartbeat(note=...) already writes the note; the
# notifier just never delivered that kind. Three anchored edits, so the surgery
# lives in an applier script. See the module docstring in
# deploy/docker/patches/kanban_progress_lines.py.
# The third edit is what keeps the fix from becoming its own nuisance: the notes
# roll into ONE message per card, edited in place, so a five-milestone card pings
# the space once instead of five times and the completion is the notification.
# Every failure mode of that is silent -- an unresolved import, an adapter that
# stopped overriding edit_message, a render budget that crossed the adapter's
# text cap all degrade to one-message-per-note, which is exactly what shipped
# before and so looks like nothing is wrong. verify_kanban_progress_lines.py
# therefore drives the patched runtime rather than grepping it.
# The _WAKE_KINDS grep below pins upstream's tuple as it stands AT THIS POINT in
# the build, and nothing more: apply_kanban_notifier.py replaces that whole line
# further down this stage, so the tuple does not exist in the shipped image. The
# invariant it used to claim -- heartbeat never wakes the creator, because a
# progress note that costs a full LLM turn is the most expensive thing on the
# board rather than a free one -- is enforced against the set that actually
# ships, DEFAULT_WAKE_KINDS in gateway/kanban_notifier.py, by the assertion in
# the kanban_notifier RUN below. Keep this grep anyway: it fails the build if a
# base-image bump changes the text apply_kanban_notifier.py anchors on, which is
# a clearer error than that applier's own anchor miss.
COPY --chmod=0644 deploy/docker/patches/kanban_progress_lines.py /opt/hermes/gateway/kanban_progress_lines.py
# Applier and verifier share one COPY into a directory, for the same reason the
# kanban_notifier pair below does: this stage is the base for `platform`, the
# chain measured against Docker's layer-depth ceiling, which a build from here
# broke through once already (f9f1747c, 2026-08-07). A new build gate does not
# buy a layer.
COPY deploy/docker/patches/apply_kanban_progress_lines.py \
     deploy/docker/patches/verify_kanban_progress_lines.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-progress/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-progress/apply_kanban_progress_lines.py /opt/hermes && \
    grep -qF 'TERMINAL_KINDS = ' /opt/hermes/gateway/kanban_watchers.py && \
    grep -qF '+ ("heartbeat",)' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q 'elif kind == "heartbeat":' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q 'note = _progress_note(ev.payload)' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q '_send_res = await _progress_deliver(' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q 'from gateway.kanban_progress_lines import deliver as _progress_deliver' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q '_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")' /opt/hermes/gateway/kanban_watchers.py && \
    /opt/hermes/.venv/bin/python3 -c "import pathlib; p='/opt/hermes/gateway/kanban_watchers.py'; compile(pathlib.Path(p).read_text(), p, 'exec')" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/kanban-progress/verify_kanban_progress_lines.py && \
    rm -rf /tmp/kanban-progress

# Hotfix: make a dispatched cron run report back, and stop it closing the
# kanban card of the caller that dispatched it. Upstream discards the run's
# final response and lets the run inherit HERMES_KANBAN_TASK, which is how
# three fleet-audit streams published wrong ledgers on 2026-08-04. Twelve edits
# across three files, so the surgery lives in an applier script rather than
# inline; it fails the build on any anchor that no longer matches.
# verify_cron_run_scope.py then exercises the patched code. The applier only
# proves the anchors matched, and the first regression here was a runtime type
# error no anchor could have caught: save_job_output returns a pathlib.Path,
# the run action json.dumps it, and the caller got a TypeError in place of the
# report it had waited six minutes for.
# See the module docstring in deploy/docker/patches/cron_run_scope.py.
COPY --chmod=0644 deploy/docker/patches/cron_run_scope.py /opt/hermes/tools/cron_run_scope.py
# patchlib.py is the shared build-time helper every apply_*.py below imports:
# the exact-once anchor check, the ast.parse gate, the write. It is staged
# beside the appliers rather than installed -- Python puts the running script's
# own directory first on sys.path, and nothing else puts /tmp there, so an
# applier resolves `import patchlib` only against a copy in its OWN directory.
# Hence this copy for the appliers staged flat in /tmp (this block and
# mcp-remote, which removes it), and a further copy inside each of the
# per-patch staging directories below. Those directories exist to keep a block
# to one COPY instead of one per file: the layer budget at the top of this file
# is what pays for the duplication, and it is bytes in a discarded build stage
# rather than layers in the image.
COPY deploy/docker/patches/patchlib.py \
     deploy/docker/patches/apply_cron_run_scope.py \
     deploy/docker/patches/verify_cron_run_scope.py \
     /tmp/
# The signature grep matches the text the applier inserts, not the signature it
# inserts into: run_one_job's parameter list is upstream's to reformat, and
# v2026.8.13 wrapped it onto three lines. The applier locates it by AST.
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_cron_run_scope.py /opt/hermes && \
    grep -qF ", outcome=None" /opt/hermes/cron/scheduler.py && \
    grep -q "with cron_run_scope(job_id):" /opt/hermes/tools/cronjob_tools.py && \
    grep -qF "extra_prompt=extra_prompt, outcome=outcome," /opt/hermes/tools/cronjob_tools.py && \
    grep -q "cron_ownership_violation(tid)" /opt/hermes/tools/kanban_tools.py && \
    grep -q 'outcome\["output_file"\] = str(output_file)' /opt/hermes/cron/scheduler.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
from gateway.kanban_handoff_clip import clip_handoff; \
from tools.cron_run_scope import clip_cron_response, cron_run_scope; \
clipped = clip_cron_response(('word ' * 2000) + 'https://example.invalid/issues/27'); \
exit(1) if ('https://' in clipped or not 3000 < len(clipped) <= 4000) else None" && \
    /opt/hermes/.venv/bin/python3 /tmp/verify_cron_run_scope.py && \
    rm /tmp/apply_cron_run_scope.py /tmp/verify_cron_run_scope.py

# Fix: give a spawned `hermes cron tick` the scheduler lifecycle it never had.
# The platform profile has no ticker thread -- it is ticked once a minute by
# spawning `hermes cron tick`, whose body is verbatim `tick(verbose=True)` --
# so everything Hermes hangs off the ticker's lifetime is absent or wrong there.
# Three consequences, all fixed by the fourteen anchored edits in the applier:
#   * `tick()` defaults to sync=True and held <HERMES_HOME>/cron/.tick.lock
#     across the whole `as_completed` wait, so a fleet audit starved every other
#     job for its full runtime. Measured over 17h: 3 of 35
#     github-issue-resolver firings 418s / 179s / 1142s late, each unblocking
#     within seconds of the blocking audit finishing. The lock now covers only
#     what its own comment says it is for -- get_due_jobs, advance_next_runs,
#     create_execution, pool.submit -- and a per-job flock replaces the
#     cross-process in-flight guard the wide lock was implicitly providing.
#     flock rather than a ledger row on purpose: the kernel drops it when the
#     holder dies, so unlike a `running` row it cannot wedge a job id forever.
#   * `cronjob(action='run')` took no per-job lock at all, and the
#     claim_job_for_fire CAS it relied on never blocked a tick, so a dispatched
#     audit could run on top of a scheduled one and share its output file.
#   * recover_interrupted_executions() runs only from the two gateway-ticker
#     lifecycles, so nothing ever reaped an abandoned attempt on this profile:
#     the live ledger held 6 permanently `running` rows, the oldest a day old,
#     against 0 of 1000 on the default store the gateway does sweep.
# Must stay AFTER the cron_run_scope block: every anchor was verified against
# the post-cron_run_scope source.
# See the module docstring in deploy/docker/patches/cron_tick_lock_scope.py.
COPY deploy/docker/patches/cron_tick_lock_scope.py /opt/hermes/tools/cron_tick_lock_scope.py
COPY deploy/docker/patches/apply_cron_tick_lock_scope.py \
     deploy/docker/patches/verify_cron_tick_lock_scope.py \
     deploy/docker/patches/patchlib.py \
     /tmp/cron-tick-lock/
RUN /opt/hermes/.venv/bin/python3 /tmp/cron-tick-lock/apply_cron_tick_lock_scope.py /opt/hermes && \
    grep -q "from tools.cron_tick_lock_scope import AdvisoryLock, JobLocks" /opt/hermes/cron/scheduler.py && \
    grep -q "_tick_lock = AdvisoryLock(lock_fd, fcntl, globals().get(\"msvcrt\"))" /opt/hermes/cron/scheduler.py && \
    grep -q "_job_lock = _job_locks.claim(job_id)" /opt/hermes/cron/scheduler.py && \
    grep -q "_run_lock = _job_locks.claim(job_id)" /opt/hermes/tools/cronjob_tools.py && \
    grep -q "recover_interrupted_executions" /opt/hermes/hermes_cli/cron.py && \
    ! grep -q "fcntl.flock(lock_fd, fcntl.LOCK_UN)" /opt/hermes/cron/scheduler.py && \
    ! grep -q "_job_locks.release(" /opt/hermes/cron/scheduler.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/cron-tick-lock/verify_cron_tick_lock_scope.py && \
    rm -rf /tmp/cron-tick-lock

# Cron ledger: record occurrences that were due but did not run, and retain
# history per job rather than per board.
#
# Upstream's status CHECK admits only claimed/running/completed/failed/unknown,
# so a skipped occurrence leaves no trace at all -- a watchdog that silently
# stops firing looks exactly like one with nothing to report. The applier
# widens the CHECK to include 'skipped', migrates the existing ledger in place
# (table rebuild under BEGIN IMMEDIATE, refused rather than guessed if the DDL
# is ambiguous), and writes a row at each of the five places the scheduler
# drops an occurrence today.
#
# Upstream also prunes to the newest 1000 terminal rows across ALL jobs, so the
# minute-ly ticker evicts a daily watchdog's only row inside 17 hours.
# Retention becomes per job, with a global ceiling and a per-job floor.
#
# Must stay AFTER the cron_tick_lock_scope block: two of the seventeen anchors
# are text that patch inserts.
COPY --chmod=0644 deploy/docker/patches/cron_skip_ledger.py /opt/hermes/tools/cron_skip_ledger.py
COPY deploy/docker/patches/apply_cron_skip_ledger.py \
     deploy/docker/patches/verify_cron_skip_ledger.py \
     deploy/docker/patches/patchlib.py \
     /tmp/cron-skip-ledger/
RUN /opt/hermes/.venv/bin/python3 /tmp/cron-skip-ledger/apply_cron_skip_ledger.py /opt/hermes && \
    grep -q "from tools.cron_skip_ledger import (" /opt/hermes/cron/executions.py && \
    grep -q "'claimed','running','completed','failed','unknown','skipped'" /opt/hermes/cron/executions.py && \
    grep -q "_ensure_skip_schema(conn)" /opt/hermes/cron/executions.py && \
    grep -q "def record_skipped_execution(" /opt/hermes/cron/executions.py && \
    grep -q "def skip_execution(" /opt/hermes/cron/executions.py && \
    grep -q "reason=SKIP_ALREADY_RUNNING_ELSEWHERE" /opt/hermes/cron/scheduler.py && \
    grep -q "reason=SKIP_INTERPRETER_SHUTDOWN" /opt/hermes/cron/scheduler.py && \
    grep -q "reason=SKIP_DISPATCH_CLAIM_REJECTED" /opt/hermes/cron/scheduler.py && \
    grep -q "reason=SKIP_MISSED_WINDOW" /opt/hermes/cron/jobs.py && \
    grep -q "reason=SKIP_ALREADY_RUNNING," /opt/hermes/tools/cronjob_tools.py && \
    grep -q "reason=SKIP_ALREADY_RUNNING_ELSEWHERE" /opt/hermes/tools/cronjob_tools.py && \
    grep -q '"unknown", "skipped"}' /opt/hermes/agent/monitoring/cron_health.py && \
    ! grep -q "LIMIT -1 OFFSET" /opt/hermes/cron/executions.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/cron-skip-ledger/verify_cron_skip_ledger.py && \
    rm -rf /tmp/cron-skip-ledger

# Security: scan the cron commands that skip the approval prompt.
# approvals.cron_mode: approve is a fleet-wide default here -- a scheduled run
# has nobody at the keyboard, so it cannot answer an approval prompt. Upstream
# read that as permission to skip the Tirith content scan as well: the scanner
# is called only from the cron `deny` arm of check_all_command_guards, so under
# `approve` every command in every watchdog run executed unscanned. Measured
# against the live image, not inferred: the scanner is consulted 0 times.
# That is the wrong half to drop. github-issue-resolver fires every 30 minutes
# over issue text written by anyone with a GitHub account, and Tirith's whole
# subject is the content-level threats that arrive that way -- homograph and
# lookalike-TLD URLs, pipe-to-interpreter, terminal escape injection, encoded
# payloads -- none of which DANGEROUS_PATTERNS is looking for. The patch runs
# the scan on both arms; the mode goes on deciding only what happens to a
# pattern-flagged command, which is the part that genuinely needs a human.
# The applier proves the anchor matched; it cannot prove the gate now refuses
# anything, and every way this patch fails is silent -- an unscanned run looks
# exactly like a clean one. verify_cron_tirith_scan.py drives the real patched
# check_all_command_guards and asserts on its verdict.
# See the module docstring in deploy/docker/patches/cron_tirith_scan.py.
COPY --chmod=0644 deploy/docker/patches/cron_tirith_scan.py /opt/hermes/tools/cron_tirith_scan.py
COPY deploy/docker/patches/apply_cron_tirith_scan.py \
     deploy/docker/patches/verify_cron_tirith_scan.py \
     deploy/docker/patches/patchlib.py \
     /tmp/cron-tirith-scan/
RUN /opt/hermes/.venv/bin/python3 /tmp/cron-tirith-scan/apply_cron_tirith_scan.py /opt/hermes && \
    grep -q "from tools.cron_tirith_scan import cron_tirith_block" /opt/hermes/tools/approval.py && \
    grep -q "describe=_format_tirith_description" /opt/hermes/tools/approval.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/cron-tirith-scan/verify_cron_tirith_scan.py && \
    rm -rf /tmp/cron-tirith-scan

# Cron delivery: `deliver: "chat"` hands a job's finished report to the Chat
# Agent instead of posting it, so the finding lands in a thread that can answer
# "why does that matter?".
# Upstream resolves `deliver` to a chat platform and sends. On this install that
# is where a watchdog's report goes to die: profile_cron_tick.py can only hand a
# child profile the Slack home channel, so on Google Chat `deliver: "all"`
# expands to an empty target list and the run records "no delivery target
# resolved" while posting nowhere. Even where it does post, the report arrives
# from a process that has exited -- the Chat Agent, which is who the user
# replies to, never saw it.
# A delivery MODE rather than a line in every job's prompt, because the prompt
# only reaches jobs shipped in this image. A job the user asks for at runtime is
# created through cronjob(action='create') and carries no such contract, which
# is the case this has to cover: `deliver` is a field every creation path can
# set, including the model's own tool call.
# NOT a patch. Upstream already routes `deliver=<name>` through the platform
# registry, and cron/scheduler.py::_plugin_cron_env_var says in its own words
# that a plugin setting `cron_deliver_env_var` gets "cron delivery support
# without editing this module". So this is a bundled platform plugin, dropped
# into plugins/platforms/ where Hermes auto-registers it: zero lines of the
# Hermes tree are modified and there is no anchor to go stale.
# What replaces the patch's build-time failure is verify_chat_relay.py. The
# plugin depends on a handful of upstream behaviours and every one of them
# fails QUIETLY -- a watchdog that reports nothing looks exactly like a watchdog
# with nothing to report -- so the verifier drives the real _deliver_result
# against a loopback stand-in for the Session KV server and asserts on what
# crossed the wire. Its own docstring enumerates them.
# See the module docstring in deploy/docker/plugins/chat/adapter.py.
#
# The verifier rides in on the plugin's own COPY and is deleted by the RUN that
# executes it, rather than being staged into /tmp of its own: one COPY for the
# four, not two for three and one, because `platform` builds FROM this chain
# and the budget at the top of this file is the binding constraint. It
# resolves the plugin through `os.getcwd()`, so /opt/hermes is still the cwd it
# needs and nothing about the check changes -- only where the file sits for the
# one layer it exists.
COPY --chmod=0644 deploy/docker/plugins/chat/plugin.yaml \
     deploy/docker/plugins/chat/__init__.py \
     deploy/docker/plugins/chat/adapter.py \
     deploy/docker/plugins/verify_chat_relay.py \
     /opt/hermes/plugins/platforms/chat/
RUN cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 plugins/platforms/chat/verify_chat_relay.py && \
    rm -f plugins/platforms/chat/verify_chat_relay.py

# One patch for the kanban notifier's terminal-event path, because clipping the
# handoff, delivering `result`, and deciding whether to wake the creator are
# three edits to the same twenty lines of gateway/kanban_watchers.py. Three
# appliers meant four anchors into one function and four ways for an upstream
# edit to break the build; this is two.
#   * Clip: upstream hard-slices the handoff at 200/160 characters and discards
#     every line after the first, which delivered a severed GitHub link -- a
#     ledger URL cut to ".../is" -- on 2026-08-03. Both slices are swapped for a
#     whitespace-boundary clip, so a URL survives whole or is dropped, never
#     truncated into a dead link.
#   * Deliver: `result` goes into the completion message the notifier already
#     builds -- one send, inheriting the existing failure counter, cursor
#     rewind, and subscription-drop machinery. It REPLACES the handoff rather
#     than appending to it, because in the branch with no event summary the
#     handoff is itself a 1200-character clip of `result`, and appending sent
#     the opening of a report and then the report.
#   * Wake: on a terminal event the notifier both sends the worker's summary to
#     the thread AND injects a synthetic message that costs the creator a full
#     model turn, which re-reads the card and paraphrases a message the user is
#     already looking at. Measured at 5.9s and 32,460 input tokens on task
#     t_c31a1f00. Upstream hardcodes the wake set with no config key; this makes
#     it read kanban.wake_on_events, defaulting to the upstream tuple. The
#     narrowing is scoped to push-capable adapters: where the notifier skips its
#     own send(), the wake is the delivery rather than a duplicate of it.
#   * Record: the wake was doing two jobs and only one was redundant. It told
#     the user (already done by the send above) and it told the AGENT -- the
#     synthetic message was the only thing that ever entered the creator's
#     transcript to say the card had finished. Dropping it left the front door
#     with `subscribed: true` as the last thing it knew, so on task t_a8f58a2a
#     it promised results that had been on screen for 106 seconds and the user
#     waited 9m46s. A one-shot note is parked on the creator's session state
#     instead, riding the next user message's api_content sidecar: no model
#     turn, ~70 tokens, no new config key -- it fires only where
#     kanban.wake_on_events suppressed a wake.
#
# The wake edit is now three greps rather than one because v2026.8.13 gave the
# subscription its own `delivery_mode`, so the patched line is a parenthesized
# `_wake_kinds_for(...) if wake_agent else set()` and one grep no longer spans
# it. All three needles are text the applier itself emits, so -- like the
# signature grep above -- they pin that the edit landed in the shape expected,
# not that upstream still writes `wake_agent`. They cannot fail independently of
# the applier: WAKE_ANCHOR carries `if wake_agent` too, so upstream dropping the
# per-subscription gate fails the substitution first.
#
# The pin on upstream is in verify_kanban_notifier.py, which asserts
# `send_passive = mode != "wake"` is still in the notifier's own source. That
# string belongs to upstream and no applier writes it, so a rename is caught
# there -- before the patched line reaches a notifier tick and raises NameError.
#
# The first of those greps carries `passive_delivered=send_passive` rather than
# stopping at `adapter=adapter`, and the argument is not cosmetic: it is the
# whole difference between narrowing a wake that duplicates a text ping and
# narrowing one that IS the delivery. `delivery_mode="wake"` skips the ping on a
# push adapter, so a gate satisfied by `adapter=adapter` alone would pass on a
# build that drops completions for every wake-only subscriber.
#
# The anchors prove only that the text landed, and every way the wake half can
# go wrong is silent: it fails towards upstream, so a moved hermes_cli.config, a
# moved gateway.wake, or a DEFAULT_WAKE_KINDS whitelist that fell behind the
# notifier all look exactly like an operator who never set the key.
# verify_kanban_notifier.py therefore drives the patched runtime -- the real
# config loader, the real APIServerAdapter (the non-push adapter the carve-out
# exists for), and the notifier's own list of describable kinds -- rather than
# hand-rolled stand-ins. The marker is worse again -- it writes into gateway
# session state that nothing else in the build reads back, so a note parked on a
# session id instead of a session key is a silent no-op -- so section 9 drives a
# real SessionStore, the real GatewayRunner sidecar-note methods, and the real
# consume_gateway_turn_context_notes the next turn calls.
# See the module docstring in deploy/docker/patches/kanban_notifier.py.
COPY deploy/docker/patches/kanban_notifier.py /opt/hermes/gateway/kanban_notifier.py
# Applier and verifier share one COPY into a directory: this stage is the base
# for `platform`, whose chain broke through Docker's layer-depth ceiling once
# already (build f9f1747c, 2026-08-07), so patches here do not each buy a layer.
COPY deploy/docker/patches/apply_kanban_notifier.py \
     deploy/docker/patches/verify_kanban_notifier.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-notifier/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-notifier/apply_kanban_notifier.py /opt/hermes && \
    grep -q "h = _clip_handoff(payload_summary)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "r = _clip_handoff(task.result)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "handoff = _kanban_handoff_with_result(handoff, task)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -qF 'd["events"], adapter=adapter, passive_delivered=send_passive' /opt/hermes/gateway/kanban_watchers.py && \
    grep -qF 'if wake_agent' /opt/hermes/gateway/kanban_watchers.py && \
    grep -qF 'wake_configured=wake_agent,' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "_kanban_note_suppressed(" /opt/hermes/gateway/kanban_watchers.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
import sys; from gateway.kanban_notifier import DEFAULT_WAKE_KINDS; \
sys.exit('heartbeat is in DEFAULT_WAKE_KINDS: every mid-run progress note would wake the card creator for a full LLM turn') if 'heartbeat' in DEFAULT_WAKE_KINDS else None" && \
    /opt/hermes/.venv/bin/python3 /tmp/kanban-notifier/verify_kanban_notifier.py && \
    rm -rf /tmp/kanban-notifier

# Hotfix: stop the Slack channel-directory refresh probing thread ids as if they
# were channels. Measured 2026-08-09: the credential proxy returned a 502 on
# every Slack call it relayed for this, in bursts of 33, every five minutes --
# about 9,500 failed calls a day, all channel_not_found. `_session_entry_id`
# composes a session-derived entry's id as `<channel>:<thread>`, so 33 threads in
# one DM became 33 directory entries; the unresolved filter matched all of them
# (their names begin with the raw channel id) and each was passed verbatim to
# conversations.info, which has never heard of a thread. The handler logged the
# failure at DEBUG and continued, so no name was ever fixed and the identical 33
# calls were reissued on the next refresh, forever.
#
# v2026.8.13 absorbed the first half: upstream now resolves against the channel
# rather than the thread and probes each channel once, concurrently, applying the
# answer to every entry on it (33 calls -> 1). Upstream's shape is kept as-is.
# What is left to this patch is the repetition -- a channel the bot genuinely
# cannot resolve is still re-probed on every five-minute refresh forever, which
# is the unbounded part of the 9,500 -- so the work list comes from
# _unresolved_by_channel (six-hour miss cache plus a per-refresh cap) and every
# dead end records itself through _note_unresolvable. Entry ids are NOT changed
# -- the thread suffix is what routes a reply to the right thread; only the
# human-readable name is rewritten, by _rename_entry, and only its channel part,
# so two threads in one DM stay distinguishable.
#
# The negative gate is on upstream's unfiltered work list rather than on the old
# `conversations_info(channel=entry["id"])`, which upstream itself no longer
# writes and so could no longer fail. Be precise about what the replacement
# buys, because it is less than it looks: the applier's OLD_RESOLVE anchor
# already contains `unresolved = [ch for ch in channels ...]` and substitute()
# demands exactly one occurrence, so the ordinary regression -- upstream
# reinstating the unfiltered list -- fails the applier, not this grep. What is
# left to the grep is the case substitute() cannot see: a SECOND occurrence of
# that work list somewhere else in channel_directory.py, which the applier would
# leave untouched and every positive gate above would still match. That is a
# narrow catch, and it is the honest description of one.
#
# A grep guard cannot catch this class of bug: the original code resolved every
# entry, handled the error and logged it, and was wrong only in the value it
# passed and how many times. verify_channel_directory_threads.py therefore drives
# the real, patched `_build_slack` over the directory that produced the storm and
# counts the Slack calls that come out. It buys two layers rather than riding an
# existing block: this is a different subsystem from every kanban patch here, and
# the block above just gave two layers back by consolidating its COPYs.
# See the module docstring in deploy/docker/patches/channel_directory_threads.py.
COPY deploy/docker/patches/channel_directory_threads.py \
     deploy/docker/patches/apply_channel_directory_threads.py \
     deploy/docker/patches/verify_channel_directory_threads.py \
     deploy/docker/patches/patchlib.py \
     /tmp/channel-directory/
RUN cp /tmp/channel-directory/channel_directory_threads.py /opt/hermes/gateway/channel_directory_threads.py && \
    /opt/hermes/.venv/bin/python3 /tmp/channel-directory/apply_channel_directory_threads.py /opt/hermes && \
    grep -qF "conversations_info(channel=base_id)" /opt/hermes/gateway/channel_directory.py && \
    grep -q "from gateway.channel_directory_threads import" /opt/hermes/gateway/channel_directory.py && \
    grep -qF "unresolved = _unresolved_by_channel(channels)" /opt/hermes/gateway/channel_directory.py && \
    grep -qF "_note_unresolvable(" /opt/hermes/gateway/channel_directory.py && \
    grep -qF "_rename_entry(" /opt/hermes/gateway/channel_directory.py && \
    ! grep -qF "unresolved = [ch for ch in channels" /opt/hermes/gateway/channel_directory.py && \
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/channel-directory/verify_channel_directory_threads.py && \
    rm -rf /tmp/channel-directory

# Slack: let bold and italic survive an inline code span in Block Kit output.
# `_inline_elements` tokenized inline code by *splitting* the run on it and
# handing each gap to the emphasis scan separately, so a `**` that opened before
# a code span and closed after it could never pair -- both delimiters reached
# Slack as literal asterisks. Card t_549d081c posted
# "**`adam-new-cluster`** (us-east4) -> ..." into a user's thread that way. Only
# the rich_text path is affected (list items, quotes, table cells): section
# blocks go through the adapter's format_message, which protects inline code
# before converting bold, so the identical markdown rendered correctly in a
# paragraph and wrongly in a list item.
#
# A grep guard cannot catch this class of bug. All four anchors can match and
# the renderer still emit a literal "**" -- a sentinel that is never restored, a
# style dict that comes back empty, an emphasis rule that stops pairing across
# the mask -- and every one of those failures looks exactly like a report that
# happened to contain no bolded code. verify_slack_code_emphasis.py therefore
# drives the real patched render_blocks over the reported line and asserts on
# the elements it returns.
# See the module docstring in deploy/docker/patches/apply_slack_code_emphasis.py.
#
# Google Chat: say the failed-attachment notice in English, and only offer
# /setup-files where it works. Upstream writes the four user-facing lines of
# `_post_attachment_fallback` in Spanish, inside an otherwise English adapter,
# so a user who asked for a report in English gets a Spanish refusal and reads
# it as the agent having switched language mid-thread. It is not a rare path:
# native media.upload needs a *per-user* OAuth token, so until each user has run
# /setup-files in their own DM every attachment lands here, and on the install
# where this was reported no user had. The second half of the fix is the advice
# itself: agents/platform/scripts/google_chat_relay_patch.py stubs /setup-files
# out, and the operator sets GOOGLE_CHAT_RELAY_URL on every install with Google
# Chat enabled, so on all of them that instruction is a dead end. The notice
# branches on the _credential_proxy_relay_patched marker that patch leaves on
# the class, and offers what does work there instead. The direct branch is for
# an image run outside the operator; it is kept rather than folded away because
# deleting it hard-codes the instruction off in the one place it works.
#
# A grep guard cannot catch this class of bug. The two branches differ only in
# which strings are appended, so a patch that wired the condition backwards --
# offering /setup-files precisely where it has been stubbed out -- greps
# identically to a correct one, and the next reader of the difference is a user
# following an instruction into a dead end.
# verify_google_chat_attachment_notice.py therefore builds the real notice from
# the patched adapter on both branches and asserts on the text.
#
# This rides the Slack block's COPY and RUN rather than buying two layers of its
# own, and it is a different subsystem, which the channel-directory block above
# treats as grounds for its own pair. The difference is the budget: this stage
# is the base for `credential-proxy`, the longest chain in the file and the one
# the layer note at the top of this file is about, so a pair of its own is two
# layers out of what little that chain has left. Both patches here rewrite an
# outbound message surface, which is as close to one subject as the grouping
# gets.
# See the module docstring in
# deploy/docker/patches/apply_google_chat_attachment_notice.py.
#
# Slack: ask for the reactions scope the adapter already spends. `hermes slack
# manifest` is the only description of the Slack app an installer ever writes --
# the operator pastes that JSON in and clicks Save -- and its bot_scopes list
# grants reactions:read and stops. The adapter calls reactions.add three times
# (eyes on pickup, white_check_mark/x on the outcome) and reactions.remove once,
# all of which need reactions:write, so Slack answers every one missing_scope
# and no kube-agents install has ever shown a reaction. The manifest is
# self-inconsistent rather than merely short: it already subscribes to
# reaction_added and reaction_removed, so the app it describes is built to hear
# reactions and not to make any.
#
# Nothing surfaces the failure. _add_reaction logs at debug with a comment that
# names this exact case ("may fail if already reacted or missing scope"),
# neither caller reads the False it returns, and the turn otherwise succeeds --
# a dead feature is indistinguishable from a slow agent. SLACK_REACTIONS
# defaults to true, so it is enabled everywhere and works nowhere.
#
# A grep guard is not enough on its own here either. _build_full_manifest
# appends to bot_scopes on two of its three messaging_experience branches and
# sorts before emitting, so the element being in the source says nothing about
# the JSON that gets pasted. verify_slack_reactions_scope.py calls the real
# function once per branch and reads the scope out of the manifest it returns,
# and fails the build the other way too -- if upstream drops the reaction calls,
# the patch is widening the app's permissions for nothing.
#
# Rides this COPY and RUN for the same budget reason as the block above.
# See the module docstring in
# deploy/docker/patches/apply_slack_reactions_scope.py.
COPY deploy/docker/patches/apply_slack_code_emphasis.py \
     deploy/docker/patches/verify_slack_code_emphasis.py \
     deploy/docker/patches/apply_google_chat_attachment_notice.py \
     deploy/docker/patches/verify_google_chat_attachment_notice.py \
     deploy/docker/patches/apply_slack_reactions_scope.py \
     deploy/docker/patches/verify_slack_reactions_scope.py \
     deploy/docker/patches/patchlib.py \
     /tmp/message-surface/
RUN /opt/hermes/.venv/bin/python3 /tmp/message-surface/apply_slack_code_emphasis.py /opt/hermes && \
    grep -q "_CODE_SENTINEL_RE" /opt/hermes/plugins/platforms/slack/block_kit.py && \
    ! grep -q "inline code is opaque" /opt/hermes/plugins/platforms/slack/block_kit.py && \
    /opt/hermes/.venv/bin/python3 /tmp/message-surface/apply_google_chat_attachment_notice.py /opt/hermes && \
    grep -qF "kube-agents patch: attachment-fallback notice" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    grep -qF "_credential_proxy_relay_patched" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    ! grep -qF "No he podido adjuntar" /opt/hermes/plugins/platforms/google_chat/adapter.py && \
    /opt/hermes/.venv/bin/python3 /tmp/message-surface/apply_slack_reactions_scope.py /opt/hermes && \
    grep -qF '"reactions:write"' /opt/hermes/hermes_cli/slack_cli.py && \
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/message-surface/verify_slack_code_emphasis.py && \
    /opt/hermes/.venv/bin/python3 /tmp/message-surface/verify_google_chat_attachment_notice.py && \
    /opt/hermes/.venv/bin/python3 /tmp/message-surface/verify_slack_reactions_scope.py && \
    rm -rf /tmp/message-surface

# Perf: hide worker-only kanban tools from orchestrator profiles. Upstream gates
# every lifecycle tool with _check_kanban_mode, which is true for any profile
# carrying the kanban toolset — so the Chat Agent front door is shipped
# kanban_complete/block/heartbeat/link/attach* on every call despite SOUL.md
# §1.5 forbidding it from calling them. Hermes has no per-tool denylist, so the
# 10,041 characters (~2,510 tokens) of schema cannot be dropped by config.
#
# The gate is also closed for a delegate_task child, which runs in its parent's
# own process and inherits its HERMES_KANBAN_TASK: upstream's two kanban gates
# both draw that line, and a gate that read the env var alone was the only one
# in the file that called a child a worker. The grep proves the anchors landed;
# verify_kanban_worker_tools.py proves the gate answers correctly against the
# real agent.delegation_context and that the schemas actually disappear.
# See the module docstring in deploy/docker/patches/kanban_worker_tools.py.
COPY deploy/docker/patches/kanban_worker_tools.py /opt/hermes/tools/kanban_worker_tools.py
# The three ownership predicates -- dispatcher worker, delegate_task child, cron
# run -- shared with hermes_cli/kanban_guardrail_exit.py further down, which asks
# the same two questions with the opposite `on_unknown`. Landed in this block
# because it is the first that needs it; the guardrail block imports the same
# tools.kanban_ownership and adds no COPY of its own.
COPY deploy/docker/patches/kanban_ownership.py /opt/hermes/tools/kanban_ownership.py
# Also staged next to the applier: it imports WORKER_ONLY_TOOLS from
# kanban_worker_tools, which imports kanban_ownership, and only the script's own
# directory is on sys.path.
COPY deploy/docker/patches/kanban_worker_tools.py \
     deploy/docker/patches/kanban_ownership.py \
     deploy/docker/patches/apply_kanban_worker_tools.py \
     deploy/docker/patches/verify_kanban_worker_tools.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-worker-tools/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-worker-tools/apply_kanban_worker_tools.py /opt/hermes && \
    test "$(grep -c 'check_fn=_check_kanban_worker_mode' /opt/hermes/tools/kanban_tools.py)" = "7" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/kanban-worker-tools/verify_kanban_worker_tools.py && \
    rm -rf /tmp/kanban-worker-tools

# Hotfix: make a card's answer reach the person who asked for it. On 2026-08-07
# a user asked which cron jobs were enabled; three cards ran, all closed `done`,
# and the list never arrived. The board shows why: `tasks.result IS NULL` on all
# three. The workers built the catalogue, printed it to their own stdout, and
# dropped it at the kanban_complete call -- because upstream's schema calls
# `result` a "Short result log line (legacy field)... Use ``summary`` instead"
# and accepts `summary or result`, while the kernel stores the chat line as
# `(summary or result).strip().splitlines()[0][:400]`. So the only field that
# can carry a report is marked legacy, the field the model is steered to holds
# one line of 400 characters, and nothing posts `result` to chat at all.
#
# Applied last so it composes with the patches above rather than competing for
# the same lines, and deterministic -- it does not try to classify a card as
# "asked for information", because every card has an answer.
#   * kanban_result_required: rewrites the schema wording and requires `result`.
#     Refuses a card once, remembers that refusal for NUDGE_TTL_SECONDS (15
#     minutes, the dispatcher's claim TTL), and lets the retry through --
#     promoting `summary` into `result` -- so the gate can never wedge a card
#     shut. The memory expires rather than lasting for the process's life: a
#     refusal older than the claim belongs to a run that is already gone, and
#     the fresh worker now holding that card has not been nudged yet.
#   * kanban_report_format (added 2026-08-08): says what a report should look
#     like, and measures. Requiring `result` got the answer into chat; it did
#     not make two answers to the same question look alike. A fan-out of four
#     near-identical sleep cards produced four formats, two of which a human
#     read as wrong -- an `#` H1 duplicating the card title the message already
#     shows, and a banner over a bare list. All four were valid Markdown, so the
#     Block Kit renderer was working; nothing had ever told the worker a shape.
#     The stanza now travels in the card body at kanban_create, where it
#     competes with the task text instead of losing to it from the persona.
#     Shape is asked for, never enforced: this module also backed a refusal in
#     kanban_complete from 2026-08-08 to 2026-08-11, and it was removed because
#     it could lose a complete report but could not guarantee a well-formed one
#     -- the retry skipped the check and stored the same ugly copy. What is left
#     is a two-level log on the delivery path. See that module's docstring.
# Getting that answer into the chat message is the other half of the fix and
# belongs to the kanban_notifier patch above; verify_kanban_result.py still owns
# the seam, replaying a real refusal through the real gate and into the real
# delivery path.
#
# Both patches ship in ONE COPY pair + ONE RUN, and the four COPY lines this
# block used to have are now two: this stage is the base for `platform`, whose
# chain has hit Docker's layer-depth ceiling once already (build f9f1747c,
# 2026-08-07), so the report-format patch is added at no layer cost rather than
# buying a block of its own. The guarantees are unchanged -- same appliers, same
# grep guards, same behavioural verifies.
#
# Order within the RUN is a requirement, not a preference: both appliers rewrite
# tools/kanban_tools.py, and verify_kanban_report_format.py drives the completion
# path, so it can only run once BOTH patches are in. The COPY puts both modules
# in /opt/hermes/tools first; report-format is applied first so a failure in the
# smaller, newer patch is not reported as a failure of the older one.
# See the module docstrings in deploy/docker/patches/kanban_result_required.py
# and deploy/docker/patches/kanban_report_format.py.
COPY deploy/docker/patches/kanban_result_required.py \
     deploy/docker/patches/kanban_report_format.py \
     /opt/hermes/tools/
# The appliers import their anchors from the modules, and only the script's own
# directory is on sys.path -- hence the second copy of each module alongside them.
COPY deploy/docker/patches/apply_kanban_result_required.py \
     deploy/docker/patches/verify_kanban_result.py \
     deploy/docker/patches/apply_kanban_report_format.py \
     deploy/docker/patches/verify_kanban_report_format.py \
     deploy/docker/patches/kanban_result_required.py \
     deploy/docker/patches/kanban_report_format.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-result/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-result/apply_kanban_report_format.py /opt/hermes && \
    grep -q "body = _with_report_format(body)" /opt/hermes/tools/kanban_tools.py && \
    grep -q "from tools.kanban_report_format import with_report_format" /opt/hermes/tools/kanban_tools.py && \
    /opt/hermes/.venv/bin/python3 /tmp/kanban-result/apply_kanban_result_required.py /opt/hermes && \
    grep -q "_result_err, result = _require_result(tid, summary, result)" /opt/hermes/tools/kanban_tools.py && \
    grep -q "_apply_result_schema(KANBAN_COMPLETE_SCHEMA)" /opt/hermes/tools/kanban_tools.py && \
    ! grep -q "provide at least one of: summary (preferred), result" /opt/hermes/tools/kanban_tools.py && \
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/kanban-result/verify_kanban_result.py && \
    /opt/hermes/.venv/bin/python3 /tmp/kanban-result/verify_kanban_report_format.py && \
    rm -rf /tmp/kanban-result

# Hotfix: three kanban scheduler faults, all measured on the cluster 2026-08-07,
# all in `hermes_cli/kanban_db.py`. One patch, because three appliers rewriting
# the same file could not see each other's anchors and their ordering lived in a
# comment rather than in a constraint.
#
#   * Self-parenting deadlock. `task_links` is a predecessor edge, so a card
#     that creates work with `parents=[<itself>]` and then blocks on it can
#     never be satisfied -- claim_task refuses the children until the parent is
#     done, and the parent is waiting on them. Worse, a `dependency` block
#     routes to `todo` rather than `blocked`, which skips the block-recurrence
#     loop breaker, so the parent respawned a full agent run every few seconds.
#     One worker escaped by shelling out to sqlite3 and closing three cards
#     `done` with a fabricated 27-character result. The patch inverts the
#     mis-directed edges at the block point, but only for children the blocking
#     card fanned out after it started running and is the last unfinished parent
#     of. A pipeline successor someone else planned predates the card's first
#     `claimed` event, so real pipelines and fan-ins are untouched.
#
#   * Claim tokens outlive the process that made them. `_claimer_id()` is
#     `hostname:pid`, and under Kubernetes hostname is the pod name --
#     pod-scoped, not restart-scoped. When the gateway took a SIGBUS (exit 135)
#     the replacement container kept the pod name, reset its PID namespace, and
#     adjudicated its predecessor's worker PIDs: six cards marked `crashed`.
#     `_error_fingerprint` then normalised the PID out of "pid <N> not alive",
#     so six independent crashes read as one systemic fault, which drops
#     failure_limit to 1 -- every one of them gave up on its first failure.
#     The fence narrows the liveness check to this exact process life and hands
#     back cards whose owner is provably gone, closing the 900s dark window a
#     rollout otherwise costs. The fence is what keeps a predecessor's workers
#     out of the fingerprint bucket, so `_error_fingerprint` itself is left
#     alone -- every message on that path is pid-prefixed, and dropping the
#     substitution gives each worker its own bucket, which the systemic
#     heuristic can then never fill.
#
#     A handed-back card is charged one failure against the dispatcher's own
#     retry budget *unless* the reclaim was infrastructure rather than fault.
#     Free reclaims across the board were a closed loop: a card that kills the
#     gateway is reclaimed by the replacement process, dispatched, and kills it
#     again, with nothing counting. Charging all of them was the opposite
#     failure: DEFAULT_FAILURE_LIMIT is 2 and nothing overrides it, so two
#     ordinary rollouts in a long-lived card's life parked it in `blocked`. The
#     discriminator charges unless the claim came from a *different* pod AND
#     predates this process's start -- the only combination that proves nobody
#     watched the owner die. Same pod is a container restart, which is exactly
#     the poison-card signature, and is still charged. The charge also runs
#     outside the fingerprint pass, so an in-place death releasing every
#     in-flight card at once still cannot collapse into one systemic verdict.
#
#   * The circuit breaker's trip does not survive the tick that wrote it.
#     `dispatch_once` calls `detect_crashed_workers` and then, twenty lines
#     later, `recompute_ready`. Both decide whether a card is over its retry
#     budget, and they do not use the same threshold. `_record_task_failure`
#     persists `consecutive_failures = old + 1` -- the raw attempt count --
#     while the two callers that trip early pass a threshold the dispatcher
#     never sees: the clean-exit protocol-violation path adjudicates its own
#     streak and passes `force_trip=True`, and the systemic same-error path
#     passes `failure_limit=1`. `recompute_ready` re-derives the verdict from
#     the column alone against the dispatcher's limit (2), sees 1 < 2, flips the
#     card back to `ready` and emits `promoted`; the next lines of the same tick
#     claim and respawn it. The policy "stop after 3 protocol violations and
#     hand to a human" silently means 4. The fix stores the exhausted budget
#     rather than the raw attempt count, so the counter -- which stays the
#     single arbiter -- says what the trip decided. That matters more than it
#     sounds: the obvious alternative, having `recompute_ready` read the
#     `gave_up` event, was built and rejected because `assign_task` zeroes the
#     counter and emits `assigned`, not `unblocked`, so an event-stickiness
#     guard pins every reassigned card in `blocked` forever.
#
#     Two consequences operators need. `assign_task` to the SAME profile no
#     longer frees a tripped card -- it only zeroes the counter when the
#     assignee actually changes, which was survivable while a systemic trip
#     stored 1 (below the dispatcher's 2). Reassign elsewhere or
#     `hermes kanban unblock`. And the floor is `DEFAULT_FAILURE_LIMIT`, not the
#     configured limit, because `detect_crashed_workers` never sees the latter:
#     raise `kanban.failure_limit` above 2 and systemic trips become revertible
#     again, above 3 and protocol trips follow.
#
# All three are emergent scheduling behaviour rather than a bad string, so the
# anchors alone prove nothing: verify_kanban_scheduling.py replays every
# incident against a real board through the patched engine, and -- the part that
# actually earns its runtime -- asserts that `assign_task`, `unblock_task`,
# parent-dependency promotion and worker `kanban_block` stickiness all still
# behave exactly as they did before.
#
# The counted greps below are the floor's blast radius: exactly the two trip
# UPDATEs bind `persisted_failures`, and the two below-threshold UPDATEs still
# bind the raw `failures`, so a floor that leaked into a retry would show up as
# 3-and-1. The raw count is 1+1 rather than 2 because v2026.8.13 gave the
# below-threshold spawn path a leading `retry_status` bind -- it restores the
# phase it claimed from -- so the two below-threshold binds no longer share a
# string. Counting them separately keeps both pinned; a single `-c` on the
# shorter text would have matched neither shape reliably.
#
# Must stay ahead of the kanban_wake_nudge block below, which anchors on
# `create_task`, `complete_task` and `unblock_task` in the same file. The six
# anchors here touch none of those three, and test_kanban_scheduling.py asserts
# that rather than leaving it to a reader.
# See the module docstring in deploy/docker/patches/kanban_scheduling.py and the
# applier docstring in deploy/docker/patches/apply_kanban_scheduling.py.
COPY --chmod=0644 deploy/docker/patches/kanban_scheduling.py /opt/hermes/hermes_cli/kanban_scheduling.py
COPY deploy/docker/patches/apply_kanban_scheduling.py \
     deploy/docker/patches/verify_kanban_scheduling.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-scheduling/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-scheduling/apply_kanban_scheduling.py /opt/hermes && \
    grep -q "_kanban_repair_inverted_deps(conn, task_id, reason)" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "conn, _kanban_claimer, _pid_alive, _resolve_crash_grace_seconds" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "_kanban_claim_is_self(lock, _kanban_claimer)" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "conn, _kanban_reclaimed.chargeable, _record_task_failure" /opt/hermes/hermes_cli/kanban_db.py && \
    grep -q "persisted_failures = max(failures, effective_limit)" /opt/hermes/hermes_cli/kanban_db.py && \
    test "$(grep -c 'lock.startswith(host_prefix)' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
    test "$(grep -c '(persisted_failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
    test "$(grep -c '(failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "1" && \
    test "$(grep -c '(retry_status, failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "1" && \
    test "$(grep -c 'from hermes_cli.kanban_scheduling import' /opt/hermes/hermes_cli/kanban_db.py)" = "1" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/kanban-scheduling/verify_kanban_scheduling.py && \
    rm -rf /tmp/kanban-scheduling

# Hotfix: kanban workers leaving `run_conversation` without a board write.
#
# Upstream guards this in exactly one place -- the end of the no-tool-calls
# branch, where `build_kanban_stop_nudge` gives a worker that is about to stop
# talking another turn to call `kanban_complete`/`kanban_block`. Every other way
# out of that loop is a bare `break` several hundred lines earlier and jumps
# straight over it. The worker exits rc=0 having never touched the board and the
# reaper stamps a `protocol_violation` after the fact -- a failure with no
# explanation in it, because by then the reason is gone.
#
# Card t_d3efabef burned four runs and 38 minutes this way on 2026-08-07. Each
# run halted on `loop_web_search_cap` with 173 successful searches in hand and
# was never told; the halt path breaks out of the tool-call branch without
# showing the model the block result.
#
#   * kanban_guardrail_exit's first edit makes that halt kanban-aware: reuse
#     upstream's own nudge helper (already bounded at two attempts, already a
#     no-op for non-workers) and add the one thing its text does not say -- the
#     tool is gone for this run, stop trying to use it. Clearing
#     `_tool_guardrail_halt_decision` is mandatory; `reset_for_turn` clears it
#     per turn, not per iteration.
#   * The second edit backstops the six sibling exits that leak identically, in
#     `finalize_turn` -- the single funnel every `break` passes through. An
#     unexplained protocol violation becomes a counted `timed_out` whose error
#     text names the exit reason. Goal-mode workers are excluded: they call
#     `finalize_turn` once per turn, so recording on turn 1 of N would release
#     the claim underneath the loop. Dispatched cron runs are excluded for the
#     same reason from the other end: cron_run_scope (above) leaves
#     HERMES_KANBAN_TASK pointing at the DISPATCHER's card so its claim keeps
#     heart-beating, and the run may not terminate that card -- so recording
#     would charge a caller that is still blocked on the run. `delegate_task`
#     children are excluded on the same argument once more: they run in the
#     parent's process with the parent's HERMES_KANBAN_TASK still set and own no
#     card, which is why upstream's `_reject_delegated_child_mutation` refuses
#     every board mutation from one.
#
# The anchors prove only that the text landed. verify_kanban_guardrail_exit.py
# additionally parses the patched loop to prove the nudge is reachable, ahead of
# the break, and terminal; drives it against the real `agent.kanban_stop` (whose
# signature the patch would otherwise absorb in silence); and records a leak on a
# real board through the real `_record_task_failure`.
#
# The per-turn `max_web_searches` ceiling that triggered this is a config change
# rather than a patch -- see the /opt/platform-template/config.yaml assertion
# further down. It must not ship alone: raising the cap only moves when the bug
# fires.
# See the module docstring in deploy/docker/patches/kanban_guardrail_exit.py.
COPY --chmod=0644 deploy/docker/patches/kanban_guardrail_exit.py /opt/hermes/hermes_cli/kanban_guardrail_exit.py
COPY deploy/docker/patches/apply_kanban_guardrail_exit.py \
     deploy/docker/patches/verify_kanban_guardrail_exit.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-guardrail-exit/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-guardrail-exit/apply_kanban_guardrail_exit.py /opt/hermes && \
    grep -q "if _kanban_halt_nudge:" /opt/hermes/agent/conversation_loop.py && \
    grep -q "_kanban_should_record_missing(" /opt/hermes/agent/turn_finalizer.py && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 /tmp/kanban-guardrail-exit/verify_kanban_guardrail_exit.py && \
    rm -rf /tmp/kanban-guardrail-exit

# Perf: let a board mutation wake the kanban watchers instead of waiting out a
# poll. The dispatcher and notifier are pure 5s polls, so every hop of a task
# chain pays avg 2.5s (max 5s) for rows that were already committed: measured
# 2026-08-07, 4.9s coordinator claim wait + 2.2s synthesizer claim wait +
# notify delays on an 87s round trip. Workers are separate processes, so the
# nudge is a wake file derived from the board DB path (<db>.wake):
# create/complete/unblock bump its mtime after their commits, and each watcher
# loop swaps its fixed sleep for a wait sliced at <=0.25s that breaks early on
# the bump. Claims and heartbeats deliberately do not nudge -- a dispatcher
# that wakes on its own tick's writes is a busy loop. Everything fails towards
# upstream: an unwritable wake file means 5s polling again, and
# verify_kanban_wake_nudge.py proves that property (and the nudges, and the
# dispatcher's silence) against the real patched engine on a real board.
# Must stay AFTER the scheduling/breaker/guardrail blocks above: the anchors
# were derived from the fully-patched tree.
# See the module docstring in deploy/docker/patches/kanban_wake_nudge.py.
#
# Hotfix: a worker's child cards inherit the chat subscription of the card the
# worker is running. `_maybe_auto_subscribe` needs session context that exists
# only on inbound user messages, and upstream's create-time inheritance only
# follows explicit `parents=[...]` edges -- a worker's own card is neither, so
# the user's thread lost the fan-out at the first hop. Measured 2026-08-07
# 12:58: coordinator t_b9659077 was subscribed, the 4 cards it created as a
# WORKER were not, and the synthesizer's answer sat undelivered for 91.3s
# (gateway.log: zero Slack sends 12:59:02-13:02:15) until the user asked. The
# manual remedy (scripts/kanban_notify_propagate.py) relies on the worker
# remembering to run it; it didn't. Same column set and INSERT OR IGNORE
# idempotency as that script, so keeping it around as a back-fill tool
# double-writes harmlessly. verify_kanban_auto_subscribe.py replays the
# incident through the real patched create handler on a real board.
# See the module docstring in deploy/docker/patches/kanban_auto_subscribe.py.
#
# Hotfix: kanban_comment tells the caller what the card is doing. On 2026-08-08
# card t_a8f58a2a completed at 19:09:43 and its 6,191-char report reached the
# Slack thread at 19:09:44; at 19:11:30 the front door commented on that card and
# told the user "You'll see the results post here as soon as the agent
# completes." The user waited 9m46s for an answer already sitting above the
# question. The comment succeeded and said nothing: a comment does not respawn a
# done card, the subscription was already reaped, and `commented` is not one of
# the notifier's TERMINAL_KINDS -- so a comment never posts to chat on ANY card,
# only transitively via a worker that reads it and then completes. The handler now
# returns task_status, who will read the comment, and -- for open cards only --
# whether completing posts to chat. Terminal cards get an explicit note instead:
# the write always lands (annotating a finished audit is legitimate, and losing a
# comment is worse than the incident), but it can no longer read as pending.
# Order-independent w.r.t. the other kanban_tools.py patches -- it shares no line
# with any of them; it rides in this group only for the layer budget below.
# See the module docstring in deploy/docker/patches/kanban_comment_status.py.
#
# Hotfix: the notifier delivered at-MOST-once. `claim_unseen_events_for_sub`
# committed the subscription's cursor and only then, ~130 lines of control flow
# later, tried to send -- so anything that killed the process in between
# destroyed the notification permanently. On 2026-08-09 13:00:29 task
# t_a18254ca's `completed` event (id 971) was claimed, the cursor moved 958 ->
# 971, and the gateway took a fatal signal 162ms later at the `get_task` on the
# very next line. The 7,124-byte report was intact in tasks.result and no tick
# ever looked at it again; the user asked for the answer by hand. The crash is
# not the defect -- the per-subscription handler catches bare Exception and does
# NOT rewind, so an ordinary exception in that window loses the message just as
# completely. The claim becomes a pure read, the cursor is written once after
# delivery, and an in-process high-water map bounds the duplicates that
# at-least-once buys this with to one per process. Depends on the notifier being
# the single writer of these cursors, which it is. It must run AFTER
# apply_kanban_wake_nudge.py: the anchors were derived from the fully-patched
# tree, and although wake_nudge's own anchors are disjoint from all three here,
# deriving against a different tree than the build produces is how an anchor
# rots undetected. verify_kanban_notify_delivery.py replays the incident on a
# real board and -- the check the file exists for -- asserts upstream's
# unseen_events_for_sub is still a pure read, because the fix silently reverts
# to at-most-once if it ever grows a write.
# See the module docstring in deploy/docker/patches/kanban_notify_delivery.py.
#
# Hotfix: an event-triage card's subscription was addressed nowhere. A
# Kubernetes event reaches the agent over the REST API, and api_server.py's
# session-context chokepoint binds platform="api_server" with the session id as
# chat_id -- deliberately, as the structural guard against #10760. One layer
# down, `_maybe_auto_subscribe` copies those values into kanban_notify_subs and
# writes a well-formed row naming a platform no notifier can deliver to, so the
# card completes and the root-cause analysis reaches nobody (#630). On the
# install this was diagnosed against, 14 subscription rows were sitting in that
# state. The REST bridge already recorded the alert's real chat route under the
# session id, in session_kv.db's session_metadata, and the
# session id is the value sitting in the row's chat_id -- so the three
# destination values are rewritten as the row is written and everything
# downstream is untouched. Confirmed before the code was written by hand-editing
# one undelivered card's row to a real address and rewinding its cursor: the
# report was delivered, and with thread_id set it threaded under the alert it
# answered. Fails open everywhere -- kanban_tools also runs in CLI workers with
# no routing database, and a missing, unreadable or malformed one leaves today's
# behaviour exactly as it is. verify_kanban_event_routing.py drives the real
# patched create handler with an event session's context and checks every
# fall-through.
# See the module docstring in deploy/docker/patches/kanban_event_routing.py.
#
# Hotfix: a worker that fanned work out completed its own card on a dispatch
# receipt, and the receipt was delivered as the final answer (issue #1010: card
# t_470a97c5 closed "finished" with "Fanned out cluster investigation tasks ...
# Awaiting synthesis"; 49 of 156 scored repetitions of the same eval case show
# the identical shape). Completing IS the delivery, and nothing on the board
# connected the receipt to the work it dispatched: task_links is a predecessor
# edge and fan-out children are created parentless. So kanban_create now
# records which running card created which child (kanban_worker_children), and
# kanban_complete refuses — once, never wedging — to close a card whose
# recorded children are neither settled nor gated on it, telling the worker to
# wait and synthesize instead. Must run AFTER apply_kanban_auto_subscribe.py
# and AFTER apply_kanban_result_required.py (earlier stage): both of its
# anchors are text those patches introduced, deliberately, because the sites
# it needs only exist in the patched tree. kanban_result_required.py is copied
# here again solely so the applier can import the anchor constant; the module
# itself was installed by its own stage.
# See the module docstring in deploy/docker/patches/kanban_children_settled.py.
#
# All six patches ship in ONE COPY + ONE RUN on purpose. This stage is the base
# for `platform`, and the block-per-patch layout these would normally get pushed
# that chain past Docker's layer-depth ceiling ("max depth exceeded" on build
# f9f1747c, 2026-08-07). The guarantees are unchanged — same appliers,
# same grep guards, same behavioural verifies — only the layer count differs.
COPY deploy/docker/patches/kanban_wake_nudge.py \
     deploy/docker/patches/apply_kanban_wake_nudge.py \
     deploy/docker/patches/verify_kanban_wake_nudge.py \
     deploy/docker/patches/kanban_auto_subscribe.py \
     deploy/docker/patches/apply_kanban_auto_subscribe.py \
     deploy/docker/patches/verify_kanban_auto_subscribe.py \
     deploy/docker/patches/kanban_comment_status.py \
     deploy/docker/patches/apply_kanban_comment_status.py \
     deploy/docker/patches/verify_kanban_comment_status.py \
     deploy/docker/patches/kanban_notify_delivery.py \
     deploy/docker/patches/apply_kanban_notify_delivery.py \
     deploy/docker/patches/verify_kanban_notify_delivery.py \
     deploy/docker/patches/kanban_event_routing.py \
     deploy/docker/patches/apply_kanban_event_routing.py \
     deploy/docker/patches/verify_kanban_event_routing.py \
     deploy/docker/patches/kanban_children_settled.py \
     deploy/docker/patches/apply_kanban_children_settled.py \
     deploy/docker/patches/verify_kanban_children_settled.py \
     deploy/docker/patches/kanban_result_required.py \
     deploy/docker/patches/patchlib.py \
     /tmp/latency-patches/
# `install -m 0644`, not `cp`, for the first line. `cp` gives a newly created
# destination the source's mode masked by the umask in force, and the source
# here is whatever the build context held: git tracks one permission bit, so a
# tree checked out under `umask 077` carries 0600 throughout and `cp`
# propagates it rather than repairing it. The agent runs as uid 10000 and
# cannot read the result. Same failure agentplugins/lib/plugin_image.sh:798
# stages against for the plugin image, same cause.
#
# The other four land in /opt/hermes/tools and /opt/hermes/gateway, which the
# `platform` stage normalizes; /opt/hermes/hermes_cli is the one patch
# destination no loop walks, so it is stated here at the write site.
RUN install -m 0644 /tmp/latency-patches/kanban_wake_nudge.py /opt/hermes/hermes_cli/kanban_wake_nudge.py && \
    cp /tmp/latency-patches/kanban_auto_subscribe.py /opt/hermes/tools/kanban_auto_subscribe.py && \
    cp /tmp/latency-patches/kanban_comment_status.py /opt/hermes/tools/kanban_comment_status.py && \
    cp /tmp/latency-patches/kanban_notify_delivery.py /opt/hermes/gateway/kanban_notify_delivery.py && \
    cp /tmp/latency-patches/kanban_event_routing.py /opt/hermes/tools/kanban_event_routing.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_wake_nudge.py /opt/hermes && \
    test "$(grep -c '_kanban_record_wake(conn)' /opt/hermes/hermes_cli/kanban_db.py)" = "3" && \
    test "$(grep -c '_wake_monitor = _KanbanWakeMonitor(_kb.kanban_db_path)' /opt/hermes/gateway/kanban_watchers.py)" = "2" && \
    test "$(grep -c 'await _kanban_wait_interval(' /opt/hermes/gateway/kanban_watchers.py)" = "2" && \
    ! grep -q "Sleep with cancellation checks" /opt/hermes/gateway/kanban_watchers.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_notify_delivery.py /opt/hermes && \
    ! grep -q '_kb.claim_unseen_events_for_sub(' /opt/hermes/gateway/kanban_watchers.py && \
    ! grep -q '_kb.rewind_notify_cursor(' /opt/hermes/gateway/kanban_watchers.py && \
    grep -qF 'old_cursor, cursor, events = _kanban_read_unclaimed(' /opt/hermes/gateway/kanban_watchers.py && \
    test "$(grep -cF '_kanban_mark_delivered(self, sub, d["cursor"])' /opt/hermes/gateway/kanban_watchers.py)" = "1" && \
    test "$(grep -cF 'self._kanban_advance, sub, d["cursor"], board_slug,' /opt/hermes/gateway/kanban_watchers.py)" = "1" && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_auto_subscribe.py /opt/hermes && \
    grep -q "_kanban_inherit_worker_subs(conn, new_tid)" /opt/hermes/tools/kanban_tools.py && \
    grep -q "from tools.kanban_auto_subscribe import" /opt/hermes/tools/kanban_tools.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_comment_status.py /opt/hermes && \
    grep -qF '**_comment_delivery_fields(kb, conn, tid)' /opt/hermes/tools/kanban_tools.py && \
    grep -q "from tools.kanban_comment_status import" /opt/hermes/tools/kanban_tools.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_event_routing.py /opt/hermes && \
    grep -qF 'platform, chat_id, thread_id = _kanban_event_route(' /opt/hermes/tools/kanban_tools.py && \
    grep -q "from tools.kanban_event_routing import" /opt/hermes/tools/kanban_tools.py && \
    cp /tmp/latency-patches/kanban_children_settled.py /opt/hermes/tools/kanban_children_settled.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/apply_kanban_children_settled.py /opt/hermes && \
    grep -qF '_kanban_record_worker_child(conn, new_tid)' /opt/hermes/tools/kanban_tools.py && \
    grep -qF 'tid, _kanban_children_connect, result' /opt/hermes/tools/kanban_tools.py && \
    grep -q "from tools.kanban_children_settled import" /opt/hermes/tools/kanban_tools.py && \
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_wake_nudge.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_auto_subscribe.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_comment_status.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_notify_delivery.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_event_routing.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_children_settled.py && \
    rm -rf /tmp/latency-patches


# 3. Clone and build mcp-remote proxy. Git is a build-only dependency here;
# the sandbox runtime exposes only the credential-proxy Git wrapper.
#
# The source is a fork, and deliberately so: it is the only build that speaks
# Google ADC. Its commits ahead of upstream add `google-auth-library`, mint a
# fresh bearer token per call (handling expiry), and attach the
# `X-Goog-User-Project` header. Upstream geelen/mcp-remote has none of that, so
# switching the clone URL to the official repository would break every GKE and
# developer_knowledge MCP call. Do not "clean this up" without replacing the
# auth path first.
#
# The SDK is pinned explicitly because the git SHA alone does not pin it. The
# fork commits only a `pnpm-lock.yaml`, which `npm install` ignores, and
# declares `^1.23.0`; with tsup's `external: []` the SDK is bundled into the
# shipped chunk. Without this line the transport baked into the image is
# whatever was latest on build day, so two builds of this same Dockerfile can
# ship different error semantics (1.24.0 changed the thrown class).
COPY deploy/docker/patches/apply_mcp_remote_forward_errors.py /tmp/apply_mcp_remote_forward_errors.py
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    git clone https://github.com/bradhoekstra/mcp-remote.git /opt/mcp-remote && \
    cd /opt/mcp-remote && \
    git checkout 2ff8f16753d8c744e6982b99e6ed5ab62f44f311 && \
    npm pkg set devDependencies.@modelcontextprotocol/sdk=1.30.0 && \
    npm install && \
    /opt/hermes/.venv/bin/python3 /tmp/apply_mcp_remote_forward_errors.py /opt/mcp-remote && \
    npm run build && \
    grep -q 'Failed to forward message to remote server' dist/*.js && \
    npm prune --production && \
    npm cache clean --force && \
    rm -rf .git /tmp/apply_mcp_remote_forward_errors.py /tmp/patchlib.py

# 5. Create base defaults directory and copy shared defaults
RUN mkdir -p /opt/defaults && chown hermes:hermes /opt/defaults
COPY --chown=hermes:hermes deploy/shared/defaults/ /opt/defaults/


# 5.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel/hermes_otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"

# Remove the build-only clone dependency after all plugin installation is done.
#
# The loop is what step 2 above defers to. git is native up to the purge on the
# line above and nothing by the other six names is ever installed here — they all
# left with #737 — so a hit is a step someone added rather than a leftover. This
# catches a real binary only; the symlink case is the `platform` stage's guard,
# which runs last and so sees everything both stages wrote.
RUN apt-get purge -y git && apt-get autoremove -y && \
	rm -rf /var/lib/apt/lists/* && \
	for binary in gcloud kubectl gh git helm k9s yq; do \
		if command -v "${binary}" >/dev/null 2>&1; then \
			echo "unexpected cluster CLI in the agent image: ${binary}" >&2; \
			exit 1; \
		fi; \
	done

# 5.7. Precompile the Python tree to bytecode.
#
# Every kanban worker is a fresh `hermes -p <profile> --cli chat -q` process, and
# the base image sets PYTHONDONTWRITEBYTECODE=1 while /opt/hermes is read-only to
# the runtime user — so the interpreter recompiled ~1,400 modules from source on
# EVERY worker start and could never keep the result. The shipped image held
# exactly one .pyc. Measured on the live deployment by pointing
# PYTHONPYCACHEPREFIX at a writable directory: a worker's pre-first-token startup
# went from 12.5s to 6.0s. That ~6s is paid per delegated card, which is a large
# part of why a delegated task feels slower than doing the work in the thread.
# `import run_agent` alone, base image vs this one, both read-only: 3.0s → 1.0s.
# The bytecode costs ~170MB of image.
#
# This must run AFTER everything this stage writes into /opt/hermes — the patches
# above and the bundled `chat` platform plugin alike: compileall stamps each .pyc
# with its source's mtime and size, so bytecode written before the write would
# simply be discarded at import and we would be back to compiling on every start. Python that later stages add is not covered here, and the two
# cases go different ways. `agents/platform/plugins/` lands in /opt/hermes/plugins
# in the `platform` stage, which runs a second pass of its own over the same tree
# — everything compiled here is already up to date and skipped, so that pass costs
# only the handful of files it introduced. What the entrypoint syncs into
# $HERMES_HOME on the writable PVC is out of reach of both, for the reason the
# platform stage's own comment gives.
#
# compileall exits non-zero if ANY file fails to parse, and site-packages ships
# deliberately-unparseable test fixtures, so its status is not a useful signal —
# the three checks after it are. The stdlib is compiled too: 199 of the modules a
# worker loads live there and Debian's python3-minimal ships them uncompiled.
# The stdlib path is asked of the interpreter rather than written out, so a base
# image on a new Python still gets compiled instead of quietly skipped — and
# because `|| true` would swallow a bad path just as happily as a bad fixture,
# the directory is checked before the run and its .pyc counted after it (602 in
# the image today). Without that, a sysconfig change would silently cost every
# worker the stdlib half of this win.
RUN STDLIB="$(/opt/hermes/.venv/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')" && \
    [ -d "$STDLIB" ] && \
    { /opt/hermes/.venv/bin/python3 -m compileall -q -j 0 /opt/hermes "$STDLIB" || true; } && \
    ls /opt/hermes/__pycache__/run_agent.*.pyc >/dev/null && \
    [ "$(find /opt/hermes -name '*.pyc' | head -5000 | wc -l)" -ge 5000 ] && \
    [ "$(find "$STDLIB" -name '*.pyc' | head -500 | wc -l)" -ge 500 ]

# Below 5.5-5.7 on purpose: those steps never read these files, and above
# them every docs edit re-ran the plugin install, purge and bytecode pass.
COPY --chown=hermes:hermes agents/platform/docs/glossary.md \
     agents/platform/docs/gcp-console-links.md \
     /opt/defaults/docs/

# 5.8. Copy custom agent startup script
# --chmod on the COPY rather than a chmod in the RUN below: same result, and the
# three destinations differ so these cannot be folded into one COPY. The layer
# budget is why the modes are not a fourth instruction — see the note at the top.
COPY --chmod=0755 deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint

# No credential-proxy shims here, and no /opt/credential-proxy at all. There
# were four — kubectl, gcloud, gh, git — each a symlink to a client that posted
# the command to the sidecar. #737 moved every caller of all four out of this
# container: the model's shell runs in the sandbox, and the agent-pod code that
# invoked one directly (the platform MCP server, gke_endpoint.py, forge.py,
# resolver.py) calls agents/platform/scripts/sandbox_exec.py and runs it over
# SSH. The sandbox image carries the shims, credential-proxy-exec and
# credential-proxy-path.sh instead; deploy/sandbox/Dockerfile is where they live
# now. The guard at the end of the `platform` stage fails the build if any of it
# reappears here.
#
# Their absence is what makes the sandbox load-bearing rather than optional. A
# `git` or `gh` issued in this container gets "command not found", which is the
# honest answer for an install that turned the sandbox off. That is a known cost
# of taking the credential out of reach of the container the model's code used
# to run in.

# Copy event-watcher binary from builder stage
COPY --from=watcher-builder --chmod=0755 /workspace/k8s-event-watcher /usr/local/bin/k8s-event-watcher

# 6. Expose ports shared by all agent gateways
EXPOSE 8642
EXPOSE 9119

# 7. Execution lifecycle
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
CMD ["hermes", "gateway", "run"]



# --- AGENT GATEWAY TARGET ---
# One image, one pod, multiple co-located Hermes profiles sharing one PVC:
#   - `default`   = Chat Agent (the front door; receives chat ingress) — synced
#                   from /opt/defaults into $HERMES_HOME at startup, except for
#                   its config.yaml, which is rebuilt from /opt/chat-template
#                   (see docker-entrypoint.sh step 2d).
#   - `platform`  = Platform Agent specialist — scaffolded at startup from
#                   /opt/platform-template (see docker-entrypoint.sh).
#   - `cluster-*` = per-cluster read-only agents — scaffolded at runtime from
#                   /opt/cluster-template (see scripts/cluster_agent_profile.py).
FROM agent-base AS platform

# Base trees this stage never writes to normalize first — including the
# plugin tree's root-ownership barrier — so the ~150MB copy-up layer caches
# across template edits; the staged repo plugins arrive root-owned via the
# final RUN's cp -a. Skills stay in the final RUN with their provenance
# manifest. Not in agent-base: credential-proxy carries neither.
#
# /opt/hermes/hermes_cli is deliberately not on the list. The only files in it
# whose mode is ever in question are the three modules the patch layers write,
# and those state their mode on the instruction that writes them, back in
# agent-base -- where the mode is wrong, and in the one image this stage is not
# a part of. Cost is not the reason either way: a find/chmod over a tree whose
# modes are already correct exports a ~4kB layer, since BuildKit diffs the
# result rather than the syscall.
RUN set -eu; \
    for d in /opt/hermes/plugins /opt/hermes/gateway /opt/hermes/tools; do \
        find "$d" -type d -exec chmod 755 {} + ; \
        find "$d" -type f -exec chmod 644 {} + ; \
    done; \
    chown -R root:root /opt/hermes/plugins

# --- Chat Agent (the `default` profile) ---
# Chat ingress plugins/hooks (session_store, session_otel_bridge, tool_call_audit,
# chat_message_audit) live with the front door that handles ingress.
COPY --chown=hermes:hermes agents/chat/defaults/ /opt/defaults/
COPY agents/chat/SOUL.md agents/chat/AGENTS.md /opt/defaults/
# Self-contained Chat Agent config: it replaces (NOT merges with) the shared-defaults
# config, so the front door stays minimal — router tools only, no cloud/write tools.
#
# It goes to /opt/chat-template rather than /opt/defaults, and the shared-defaults copy
# is removed, so that NOTHING under /opt/defaults is called config.yaml. Entrypoint step
# 2 bulk-copies /opt/defaults onto the PVC with `cp -ru`, and this is the one file there
# the running agent writes to — `/sethome`'s home channel, the monitoring install id,
# saved slash-command preferences. An mtime-driven copy racing an image roll would
# discard all of it. Step 2d copies this template onto a FRESH volume only, and thereafter
# only back-fills keys the live file has lost; the running agent is otherwise the file's
# sole writer, and the operator's settings reach it as a read-only managed-scope overlay
# at /etc/hermes rather than by rewriting it.
COPY --chown=hermes:hermes agents/chat/config.yaml /opt/chat-template/config.yaml
RUN rm -f /opt/defaults/config.yaml

# --- Shared scripts (reachable by every profile at $HERMES_HOME/scripts) ---
# Executable scripts are shared, not per-profile: the router (Chat Agent) and the
# platform/cluster tooling all resolve under $HERMES_HOME/scripts.
# One COPY for the two, for the same destination-sharing reason as the three
# below.
COPY agents/chat/scripts/ agents/platform/scripts/ /opt/defaults/scripts/
# One COPY for the three, not three: this is the chain the layer budget is measured
# against, and it is bounded at 128 — see the layer-budget note at the top of the
# file. They share a destination anyway.
#
#   profile_overlay.py         merges the operator's per-profile config overlays at
#                              startup (entrypoint step 2.7). Shipped as a file rather
#                              than inlined in the entrypoint so it can be unit tested —
#                              see tests/test_profile_overlay.py.
#   profile_plugins.py         links profile-targeted plugin image volumes into their
#                              profile homes (entrypoint step 2.65, and
#                              cluster_agent_profile.py for profiles scaffolded later) —
#                              see tests/test_profile_plugins.py.
#   otel_config.py             points the hermes_otel plugin at the collector the operator
#                              resolved (entrypoint step 4, and cluster_agent_profile.py
#                              for profiles scaffolded later). The plugin does not read
#                              OTEL_EXPORTER_OTLP_ENDPOINT, so without this the endpoint
#                              baked above is the only one that ever takes effect — see
#                              tests/test_otel_config.py.
#   sandbox_mirror.py          pushes the profile directory layout into the shell sandbox
#                              and moves the model's existing files across once (entrypoint
#                              step 5.7, and cluster_agent_profile.py for profiles
#                              scaffolded later). Runs here rather than in the sandbox
#                              because the profile list and the SSH key are both on this
#                              side — see tests/test_sandbox_mirror.py.
COPY deploy/shared/profile_overlay.py \
     deploy/shared/profile_plugins.py \
     deploy/shared/otel_config.py \
     deploy/shared/sandbox_mirror.py \
     /opt/defaults/scripts/

# --- Platform Agent specialist profile TEMPLATE (scaffolded at startup) ---
COPY --chown=hermes:hermes agents/platform/SOUL.md \
     agents/platform/AGENTS.md \
     agents/platform/CAPABILITIES.md \
     /opt/platform-template/
COPY --chown=hermes:hermes agents/platform/skills/ /opt/platform-template/skills/
COPY --chown=hermes:hermes agents/platform/cron/ /opt/platform-template/cron/
COPY --chown=hermes:hermes agents/platform/governance/ /opt/platform-template/governance/
# The specialist's Hindsight connection file. Profile-scoped: a kanban worker
# runs with HERMES_HOME pointed at profiles/platform, and the plugin resolves
# $HERMES_HOME/hindsight/config.json, so the default profile's copy is not
# visible from here. Image-owned and force-synced by the entrypoint, for the
# reason in step 2a.
COPY --chown=hermes:hermes agents/platform/hindsight/ /opt/platform-template/hindsight/

# Platform config is self-contained (scaffold does not merge again): build it by
# merging the shared defaults base with the platform overrides and bake it into
# the template. This reproduces the exact config the platform ran with as the
# former default profile.
#
# The two ceilings the platform overlay raises are asserted here rather than
# eyeballed, because a merge that dropped either fails silently: the agent falls
# back to the stock default and the only symptom is a run that dies early. They
# are read back the way the agent reads them -- through `load_config_readonly`,
# which deep-merges DEFAULT_CONFIG *under* the file -- so this also proves the
# overlay did not clobber its untouched siblings.
#
# Two COPYs rather than three: the base has to be renamed on the way in (both
# sources are called config.yaml), but the overlay and the merger can share one
# COPY into the staging directory. Layers are budgeted here — see the note at
# the top of the file.
#
# verify_slack_relay_registry_contract.py rides along in that second COPY, and
# runs from the same RUN, for the layer budget alone — it has nothing to do
# with the platform config. It checks the relay patches copied to
# /opt/defaults/scripts above against the Hermes tree they monkeypatch, which
# is why it cannot run in CI: this is the `platform` chain that
# `scripts/check_image_layers.py` measures, and it sits at 114 of the 120 layers
# that check allows, so a COPY/RUN pair of its own spends a third of what is
# left. See the
# module docstring for what a failure here means.
COPY deploy/shared/defaults/config.yaml /tmp/platform-config/base.yaml
COPY agents/platform/config.yaml \
     deploy/docker/merge_configs.py \
     deploy/docker/patches/verify_slack_relay_registry_contract.py \
     /tmp/platform-config/
RUN /opt/hermes/.venv/bin/python3 /tmp/platform-config/merge_configs.py /tmp/platform-config/base.yaml /tmp/platform-config/config.yaml && \
    cp /tmp/platform-config/base.yaml /opt/platform-template/config.yaml && \
    cd /opt/hermes && HERMES_HOME=/opt/platform-template /opt/hermes/.venv/bin/python3 -c "\
from hermes_cli.config import load_config_readonly; \
from agent.tool_guardrails import ToolCallGuardrailConfig; \
cfg = load_config_readonly(); \
caps = ToolCallGuardrailConfig.from_mapping(cfg.get('tool_loop_guardrails', {})).loop_caps; \
assert caps.max_web_searches == 200, 'per-card web_search budget lost: %r' % caps.max_web_searches; \
assert caps.max_subagents == 50, 'overlay clobbered max_subagents: %r' % caps.max_subagents; \
assert cfg.get('agent', {}).get('max_turns') == 250, 'iteration budget lost: %r' % cfg.get('agent', {}).get('max_turns')" && \
    cd /opt/hermes && PYTHONPATH=/opt/defaults/scripts /opt/hermes/.venv/bin/python3 /tmp/platform-config/verify_slack_relay_registry_contract.py && \
    rm -rf /tmp/platform-config

# Copy the Cluster Agent profile TEMPLATE (persona, config, skills), scaffolded
# into per-cluster Hermes profiles at runtime by scripts/cluster_agent_profile.py.
COPY --chown=hermes:hermes agents/cluster/ /opt/cluster-template/

# Plugins land in the shared plugin root regardless of which profile owns them —
# /opt/hermes/plugins is the only path Hermes resolves them from. The source
# directory records ownership: kube_agents_memory needs the user identity that
# only chat ingress carries, so it lives with the Chat Agent.
# One COPY for the two: they share a destination, and the chain is bounded at
# 128 layers — see the layer-budget note at the top of the file.
# Staged, not copied straight into /opt/hermes/plugins: the late RUN installs
# from here so the base's ~130MB plugin tree stays out of the layer every
# agents/ edit rewrites. `cp -a` merges exactly as the direct COPY did.
COPY agents/platform/plugins/ agents/chat/plugins/ /tmp/repo-plugins/

# NOTE: the platform overlay is no longer merged into /opt/defaults/config.yaml.
# /opt/defaults now belongs to the Chat Agent (`default` profile) and its config
# is copied verbatim above; the platform overlay is merged into
# /opt/platform-template/config.yaml instead (see the RUN block above).

# Normalize what THIS stage added, and install the staged repo plugins —
# normalized before `cp -a` merges them, so they land with the same ownership
# and modes the old combined pass produced. This RUN re-runs on every agents/
# change; the base trees stay in the stage's first RUN, where they cache.
#
# `set -eu` rather than an `&&` chain ending in `|| true`: that trailing `|| true`
# bound to the WHOLE chain, so a failure in any early command short-circuited
# every later chmod and still exited 0 — a build that silently shipped
# unnormalized permissions. Each command must now fail the build loudly.
# Every path below is guaranteed to exist at this point: /opt/hermes/skills and
# /opt/hermes/plugins come from the base image, the rest are COPYed above.
#
# The last steps close out the baked prompt material, and do two things rather
# than one because neither is sufficient alone.
#
# `chown -R root:root` is the barrier. The pod runs the sandbox as uid 10000
# (RunAsUser in the operator's buildDeployment), so up to this line the material
# the agent reads was owned by the uid reading it: /opt/platform-template/skills/*/SKILL.md
# — the agent's own instructions — was writable by the agent, and the entrypoint
# would then propagate a rewrite to every profile on the next boot. It is the layer
# BENEATH the container's read-only root filesystem rather than the only thing
# standing there: hardenedSecurityContext in the operator sets
# readOnlyRootFilesystem on every container it builds, which already turns that
# write into EROFS. What ownership adds is everything that flag does not reach —
# the image run outside this operator, a container someone gives a writable rootfs,
# and the build itself. Root-owned 0644/0755 leaves all of it readable and
# executable by the runtime user and writable by nobody it can reach. The MODE is
# deliberately left as the loops above set it: `cp -a` in the entrypoint's
# sync_profile_skills preserves mode but cannot preserve ownership as non-root, so
# a 0444/0555 tree here would arrive on the PVC read-only and break both the
# profile overlay and the swap's own `rm -rf` of the previous copy. Ownership does
# not travel; mode does. That asymmetry is the whole reason this is a chown.
#
# What it covers is wider than the skill trees, because each of these reaches the
# agent's prompt or its process and none of them is written at runtime:
#
#   - the two template ROOTS, not just their skills/ subdirectories. SOUL.md,
#     AGENTS.md, CAPABILITIES.md, governance/, cron/ and config.yaml sit beside
#     skills/ and are force-synced into the platform profile on every boot by
#     entrypoint step 2.6, and the cluster template's personas are re-copied into
#     every cluster-* profile by its continuation. A rewritten SOUL.md is more
#     load-bearing than any single SKILL.md.
#   - /opt/hermes/plugins (barriered by the stage's first RUN; listed for
#     what it covers). It is the only path Hermes resolves plugins from, holding
#     Python imported into the agent's own process — a strictly stronger capability
#     than a SKILL.md. Precompiling does not protect it: a rewritten .py has a
#     newer mtime, so the cached .pyc is invalidated and the modified source is
#     what the next gateway start, MCP child or kanban worker imports.
#   - /opt/defaults/scripts, which holds the shared scripts the entrypoint invokes
#     by absolute path, the sitecustomize.py the operator puts on PYTHONPATH, and
#     verify_skills_provenance.py — the checker for the manifest below. A checker
#     that the checked party can rewrite reports whatever it is told to.
#   - /opt/chat-template, the Chat Agent's config.yaml. It is the template the
#     entrypoint seeds $TARGET_DIR/config.yaml from on a fresh or pristine-upstream
#     volume, and back-fills every ABSENT key from on every boot thereafter (step
#     2d). A key added to the image copy therefore installs itself into the live
#     default-profile config on the next restart and persists — the same
#     rewrite-the-image-copy-and-let-the-entrypoint-propagate-it route the two
#     templates above are covered against, over a file that holds toolsets,
#     plugins.enabled and mcp_servers.
#
# /opt/defaults itself stays hermes-owned, scripts/ aside. It is the Chat Agent's
# own default profile, and while the entrypoint copies it onto the PVC at boot, the
# image copy is still read back afterwards (cron/jobs.json as --image-jobs, plugins/
# by profile scaffolding and by otel_config.py, onboarding/ by the
# bootstrap_onboarding plugin). Root-owned 0644 would still be readable, so the
# exclusion is caution about a directory with several live readers rather than a
# requirement; that residue is recorded in the security reference as not covered
# rather than papered over, and the read-only root filesystem is what closes it in
# a pod.
#
# Nothing at runtime writes to any path in the chown — the entrypoint,
# profile_scaffold.py and cluster_agent_profile.py only ever read from the
# templates, every /opt/defaults/scripts reference in the tree is a read or a
# copy OUT of it into $HERMES_HOME, and every CHAT_TEMPLATE_CONFIG reference is a
# plain `cp` or a read to back-fill from. A plain `cp` gives the destination to
# the copying uid, so the PVC copies stay writable — so root ownership costs
# nothing.
#
# The hermes chown that opens this RUN therefore names only what STAYS
# hermes-owned. The five roots below are COPYed --chown=hermes:hermes already and
# end up root:root regardless, so listing them there too would be extra recursive
# walks per build and two contradictory lines for the next reader.
# /opt/defaults is the exception that has to appear in both: the directory stays
# hermes-owned and only its scripts/ subtree is taken back.
#
# What this does NOT reach, and the security reference records the same way: the
# rest of /opt/hermes — gateway/, tools/ and the venv interpreter this RUN invokes
# — whose ownership comes from the Hermes base image and which nothing here
# asserts. Those are a wider argument than this change, not an oversight it
# covered.
#
# The manifest is the detection half, for what ownership cannot cover — a
# corrupted layer, a bad build, or a code path that ran before the barrier did.
# verify_skills_provenance.py checks it at boot, before anything is copied onto
# the PVC (entrypoint step 1.55). It covers the three skill trees, which are the
# part that grows a file per skill and is rewritten by contributors weekly; the
# personas, plugins and shared scripts get the barrier but not the hash.
#
# A missing tree fails the build rather than being skipped. The boot check refuses
# to start a pod whose tree exists with no manifest inside it, so skipping one here
# would turn a silent ordering slip in this file — a later instruction that recreates
# or restores one of these paths, in this stage or in one built FROM it — into a
# green build and a fleet-wide crash-loop. Everything this loop names
# is COPYed above or comes from the base image, so the failure branch is unreachable
# today and exists to stay that way.
#
# The manifest keeps the 0644 the loops above give every other file, and must: it
# sits INSIDE the tree it describes, so both mechanisms that put a tree on a profile
# copy it too, and both preserve mode — `cp -a` in sync_profile_skills and
# shutil.copytree/copy2 in profile_scaffold.overlay_template. A 0444 manifest
# therefore lands on the PVC owned by uid 10000 with no write bit, and the NEXT
# overlay of that profile reopens it for writing, raising shutil.Error out of
# overlay_template and abandoning every item after `skills` — governance, hindsight
# and the cron merge — behind a WARN. Ownership does not travel; mode does, and
# read-only is the one mode this file must not have.
#
# Generated here rather than in a RUN of its own: agent-base -> platform is the
# deepest chain that ships and it is bounded at 128 layers — see the layer-budget
# note at the top of the file.
#
# Bytecode is INSIDE the manifest, and that is why the second compileall pass runs
# a few lines above it rather than in the RUN it used to have of its own. CPython's
# default invalidation is timestamp-based — source mtime plus size — not a content
# hash, and nothing here passes `--invalidation-mode checked-hash` or sets
# SOURCE_DATE_EPOCH. A .pyc is therefore an independent artifact rather than a
# derivative of the .py beside it: rewrite one while keeping the source's size and
# mtime and the interpreter runs the rewritten bytecode.
# test_an_existing_cache_is_read_even_when_writes_are_suppressed in
# deploy/docker/test_image_startup_contract.py demonstrates that on the host
# interpreter running the suite, not inside this image — it is the CPython
# premise that is pinned there, not this build. Excluding __pycache__ would have
# left a hole in the middle of what the manifest claims to cover, so the order
# is: compile everything, then hash everything.
#
# The entry count is asserted because the generating pipeline's exit status is
# `sort`'s, not `find`'s, and no POSIX sh has pipefail: a find that failed
# half-way would otherwise ship a short manifest and a build that exited 0.
#
# Symlinks abort the build rather than being recorded. `find -type f` tests the
# link, not its target, so a symlink is never in the manifest; the boot check
# treats that asymmetry as tamper evidence and refuses any symlink it finds,
# because a link pointing at matching content hashes identically while what the
# tree loads has moved somewhere the manifest says nothing about. Either the two
# sides agree that a symlink is an anomaly or the check is defeated by one, so a
# base image that starts shipping links has to be discovered here — in a red
# build with the paths printed — rather than in a pod that will not start. No path
# is carved out of that probe, for the same reason nothing is carved out of the
# manifest: an exclusion is a name a link can be given.
#
# --- the compileall pass, folded in above ---
#
# Step 5.7 in `agent-base` compiled /opt/hermes and the stdlib, and states why that
# has to happen at build time at all. It cannot cover what comes after it:
# `agents/platform/plugins/` lands in /opt/hermes/plugins here, so without a second
# pass those modules are the one part of the runtime still re-parsed from source by
# every gateway start, every MCP child and every kanban worker. Everything already
# compiled above is up to date and skipped, so the marginal cost is the handful of
# files this stage introduced. No speedup figure is claimed for the pass on its
# own; the 12.5s -> 6.0s in step 5.7 is the whole tree.
#
# Use the venv's own interpreter — the one that will import these modules. A .pyc
# is only loaded by an interpreter whose magic tag matches the writer's.
#
# No `-o`: the runtime sets neither PYTHONOPTIMIZE nor -O, so it looks for the
# plain `.cpython-NNN.pyc` that a bare compileall writes, not an `.opt-N` one.
# Setting PYTHONOPTIMIZE in the pod later without adding a matching `-o` here would
# quietly put the runtime back to compiling from source. Likewise assumes
# PYTHONPYCACHEPREFIX stays unset (it is), since that would redirect the lookup
# away from the in-tree __pycache__ this writes.
#
# Deliberately NOT extended to /opt/defaults, /opt/platform-template or
# /opt/cluster-template. The entrypoint copies all three onto the PVC at startup,
# and `cp` rewrites the destination mtime, which invalidates a timestamp-validated
# .pyc anyway — it would ship bytes that are guaranteed to be ignored.
#
# It sits after the chmod loops so the generated __pycache__ directories are not
# swept up by the `*/scripts/*` chmod 755, and before the manifest loop so its
# output is hashed. They land root-owned 0644/0755 from the build umask, which is
# what the rest of /opt/hermes already is and is readable by the runtime uid.
#
# Its failure branch reports rather than aborting, which is not the `|| true` this
# block warns against above: that one masked failures of unrelated commands in the
# same chain. This is one command whose only effect is a cache. A module that will
# not precompile — a py2 relic or a conditionally-invalid file, which large
# vendored trees routinely carry — simply compiles at import as it does today.
# `set -eu` still governs everything else. A file it fails to compile is a file the
# manifest then records as source-only, which is correct: there is no bytecode to
# account for.
RUN set -eu; \
    chown -R hermes:hermes /opt/defaults; \
    for d in /opt/defaults /opt/hermes/skills /opt/cluster-template /opt/platform-template /opt/chat-template /tmp/repo-plugins; do \
        find "$d" -type d -exec chmod 755 {} + ; \
        find "$d" -type f -exec chmod 644 {} + ; \
    done; \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + ; \
    for d in /opt/hermes/skills /opt/platform-template /opt/cluster-template; do \
        find "$d" -path '*/scripts/*' -type f -exec chmod 755 {} + ; \
    done; \
    cp -a /tmp/repo-plugins/. /opt/hermes/plugins/; \
    rm -rf /tmp/repo-plugins; \
    /opt/hermes/.venv/bin/python3 -m compileall -q /opt/hermes \
      || echo "WARN: some modules under /opt/hermes did not precompile; they will compile at import, as they do without this step" >&2; \
    for d in /opt/hermes/skills /opt/platform-template/skills /opt/cluster-template/skills; do \
        [ -d "$d" ] \
            || { echo "$d does not exist at manifest generation; the boot check refuses to start a pod whose skill tree has no manifest inside it, so shipping this image would crash-loop the fleet" >&2; exit 1; }; \
        [ -z "$(find "$d" -type l -print -quit)" ] \
            || { echo "$d contains a symlink; the manifest records -type f only and the boot check refuses any symlink it finds, so this would fail every pod: $(find "$d" -type l | head -n 3)" >&2; exit 1; }; \
        n="$(cd "$d" && find . -type f ! -name skills_manifest.sha256 | wc -l)"; \
        ( cd "$d" && find . -type f ! -name skills_manifest.sha256 \
            -exec sha256sum {} + | LC_ALL=C sort -k 2 > skills_manifest.sha256 ); \
        [ "$(wc -l < "$d/skills_manifest.sha256")" -eq "$n" ] \
            || { echo "manifest for $d has $(wc -l < "$d/skills_manifest.sha256") entries, expected $n" >&2; exit 1; }; \
        chmod 644 "$d/skills_manifest.sha256"; \
    done; \
    chown -R root:root /opt/hermes/skills /opt/platform-template /opt/cluster-template /opt/chat-template /opt/defaults/scripts

# Guard: nothing that carries a cluster credential may come back into this image.
#
# It rides at the end of the stage that produces the shipped agent image, so it
# sees everything `agent-base` and `platform` wrote between them. `agent-base`'s
# own loop catches a real binary at the point git is purged; this one catches the
# other way cluster tooling comes back, which is a credential-proxy symlink.
#
# Worth a guard because reinstating one is a two-word diff with no visible
# effect, and what it produces is a credentialed command running in the pod that
# holds the Workload Identity — succeeding, silently, in the container #737
# exists to take that capability away from. Nothing else in the build or the
# test suite would report it.
RUN set -eu; \
    if [ -e /opt/credential-proxy ]; then \
      echo "credential-proxy shims are back in the agent image: /opt/credential-proxy exists" >&2; \
      exit 1; \
    fi; \
    for binary in gcloud kubectl gh git helm k9s yq; do \
      if command -v "${binary}" >/dev/null 2>&1; then \
        echo "cluster tooling is back in the agent image: ${binary}" >&2; \
        exit 1; \
      fi; \
    done

USER 10000:10000

# The build this image came from, for the `User-Agent` that remote MCP calls
# carry. Hermes resolves `${KUBE_AGENTS_VERSION}` in an `mcp_servers` entry's
# `args` from the agent process environment, so the value has to be on the
# container rather than only in the config -- see the `--header` argument on the
# mcp-remote entries in deploy/shared/defaults/config.yaml.
#
# Last instruction in the stage, not beside the ARGs at the top: the value
# changes on every commit, and an ENV that early would invalidate the whole
# `platform` chain's cache on every build. ENV writes no filesystem diff, so it
# costs nothing against the layer budget.
#
# `dev` is the default (declared with the other build args above) because an
# unset build arg must still produce a well-formed header. The publish workflows
# pass the commit SHA the image is tagged with, so a User-Agent on the wire
# names the exact image.
ARG KUBE_AGENTS_VERSION
ENV KUBE_AGENTS_VERSION=${KUBE_AGENTS_VERSION}

# --- CREDENTIAL PROXY TOOLING ---
# Everything the sidecar needs that is not agent source: the credential-aware
# CLIs, the Envoy binary and its config, and the container's entrypoint.
#
# FROM agent-base, not FROM platform. Everything the sidecar reads from that
# chain comes from agent-base itself — the venv at /opt/hermes/.venv, the
# k8s-event-watcher binary, the hermes user and /opt/defaults — plus
# /opt/defaults/scripts, which the stage below copies directly. Nothing
# from the ~20 instructions the `platform` stage spends on SOUL.md, the skill
# and cluster templates, the plugins and the config merges. Inheriting them
# bought the sidecar no files it uses and cost it a rebuild whenever any of them
# changed: `COPY agents/platform/scripts/` sits two thirds of the way down
# `platform`, so editing one agent Python file invalidated it and every layer
# after it in BOTH images. The apt/gh install below is the most expensive
# instruction in the file (~300MB over the network) and it sat under all of that
# churn. Here it sits under agent-base alone, which changes only when the Hermes
# pin or the system tooling does.
#
# It also stops being true that credential-proxy is the deepest chain shipped.
# `platform` is, which is the image scripts/check_image_layers.py now measures —
# see the layer-budget note at the top.
#
# Order within this stage is cache order, not reading order: the install is
# first because it is the most expensive and the least likely to change, and the
# three COPYs follow in the order they churn. `mkdir -p /etc/envoy` rides along
# in the install rather than letting the config COPY create that directory,
# because a `--chmod`ed COPY propagates its file mode to the parents it creates
# and 0644 on /etc/envoy is not traversable by the runtime uid. Creating it here
# costs no layer; the `chmod 755 /etc/envoy` this replaces had to run after the
# COPY, which would have pinned the install above the churn again.
FROM agent-base AS proxy-tools

USER root

# The Pod runs this container as UID 10001 — not the sandbox's 10000 — so the two
# containers do not share a user (see the UID constants in the operator's
# platformagent_manifests.go). An unknown UID is a passwd lookup that fails, and
# gcloud, gh and git all do one; give the UID a name here rather than debug it
# later. The group stays hermes (GID 10000): both containers mount the agent PVC
# and each has to be able to write what the other created there.
#
# The chmod is the other half of the same split. `agent-base` creates
# /opt/defaults mode 0700, and only the `platform` stage normalises it to 0755 — a stage
# this image no longer builds from. UID 10001 therefore cannot traverse into
# /opt/defaults/scripts, where start-services runs credential_proxy.py from, and
# the container dies with `Permission denied` on its own entrypoint. Nothing
# secret lives there: it is the same defaults tree the sandbox image ships
# world-readable.
RUN getent group 10000 >/dev/null || groupadd --gid 10000 hermes; \
    useradd --no-log-init --no-create-home --uid 10001 --gid 10000 \
      --home-dir /tmp/credential-proxy --shell /usr/sbin/nologin credential-proxy; \
    chmod 0755 /opt/defaults

RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      git \
      google-cloud-cli \
      google-cloud-cli-gke-gcloud-auth-plugin \
      kubectl && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" && \
    dpkg -i /tmp/gh.deb && \
    rm -f /tmp/gh.deb && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /etc/envoy

# The Pod runs this container as UID 10001 — not the sandbox's 10000 — so the two
# containers do not share a user (see the UID constants in the operator's
# platformagent_manifests.go). An unknown UID is a passwd lookup that fails, and
# gcloud, gh and git all do one; give the UID a name here rather than debug it
# later. The group stays hermes (GID 10000): both containers mount the agent PVC
# and each has to be able to write what the other created there.
#
# The chmod is the other half of the same split. `agent-base` creates
# /opt/defaults mode 0700, and only the `platform` stage normalises the tree —
# a stage this image no longer builds from. UID 10001 therefore cannot traverse
# into /opt/defaults/scripts, where start-services runs credential_proxy.py
# from, and the container dies with `Permission denied` on its own entrypoint.
# 0750 rather than 0755: the directory is hermes:hermes and every process in
# this image is in that group, so the group bit is the whole fix and `other`
# needs nothing.
#
# Below the install, not above it, per the ordering note on this stage: the apt
# layer is the expensive one and belongs first.
RUN getent group 10000 >/dev/null || groupadd --gid 10000 hermes; \
    useradd --no-log-init --no-create-home --uid 10001 --gid 10000 \
      --home-dir /tmp/credential-proxy --shell /usr/sbin/nologin credential-proxy; \
    chmod 0750 /opt/defaults

COPY --from=envoy-bin --chmod=0755 /usr/local/bin/envoy /usr/local/bin/envoy
COPY --chmod=0644 deploy/shared/envoy-credential-proxy.yaml /etc/envoy/envoy-credential-proxy.yaml
COPY --chmod=0755 deploy/shared/start-services.sh /usr/local/bin/start-services

# --- CREDENTIAL PROXY TARGET ---
# Only this image contains the real credential-aware CLIs. The platform target
# contains wrappers with the same names and cannot execute locally.
#
# One instruction, and it is the one that churns: an agent source edit now costs
# the sidecar a single COPY on top of a cached proxy-tools instead of a rebuild
# of the whole platform chain.
FROM proxy-tools AS credential-proxy

# agent-base leaves /opt/defaults at 0700 hermes:hermes. Co-located with the
# shell sandbox this container runs as uid 1000 — the sandbox login, so that the
# two containers can share a working tree — and 0700 makes everything under the
# directory unreachable for it. What that looks like is the container exiting on
# its first line with "Permission denied" against a path whose own mode is 0755,
# and no indication that the parent is the problem. Nothing below here is a
# secret: it is the same scripts the agent image ships, world-readable one level
# down. Ahead of the COPY so an agent source edit does not invalidate it.
RUN chmod 0755 /opt/defaults

# The same set the `platform` stage lays down at /opt/defaults/scripts, with the
# same ownership and mode, so the directory is identical in both images. That
# used to be structural — the sidecar WAS the platform image — and is now two
# lists that have to agree, so
# scripts/test_check_prompt_assets.py::test_the_two_images_fill_opt_defaults_scripts_from_the_same_sources
# parses both stages and fails if they stop agreeing. Add a source to one list
# and that test tells you about the other.
#
# Only two entries are load-bearing here — start-services.sh runs
# credential_proxy.py from this path, and that module execs
# github_token_refresh.py from it and imports gke_endpoint from beside it — but
# the sidecar has always carried the whole directory and narrowing it is a
# separate argument from this one.
#
# --chmod=0755 reproduces the `find /opt/defaults/scripts -type f -exec chmod
# 755` the platform stage runs. Only github_token_refresh.py is executed
# directly and it is already 0755 in git, so this is about the two images
# agreeing rather than about that one file.
#
# --chown=root:root reproduces the platform stage's closing barrier the same
# way, and the sidecar is where it matters most: this is the container holding
# the credentials, and credential_proxy.py, github_token_refresh.py and
# gke_endpoint.py all run out of this directory as uid 10000. Nothing in the
# tree writes here — every reference is a read or a copy out — so root
# ownership costs the runtime nothing.
COPY --chown=root:root --chmod=0755 \
     agents/chat/scripts/ \
     agents/platform/scripts/ \
     deploy/shared/profile_overlay.py \
     deploy/shared/profile_plugins.py \
     deploy/shared/otel_config.py \
     deploy/shared/sandbox_mirror.py \
     /opt/defaults/scripts/

USER 10000:10000


# --- ENTRYPOINT GATE TEST TARGET ---
# Not shipped, and not a layer of anything that is. Built only by
# `--target entrypoint-gate-test`, where the RUN either passes or fails the build.
# `docker-build.yml` then loads the stage and runs the same check a second time as a
# container under `--read-only`, because a build layer is writable by definition and so
# the RUN cannot tell you whether the entrypoint needs the write.
#
# It exists because the unit tests structurally cannot cover this. They run the entrypoint
# on a developer host, where every step below the shared-state gate is guarded on
# /opt/defaults or /opt/hermes and therefore does nothing — so they pin which branch the
# gate takes, and nothing about what the branch does. Inside the image both halves are
# real, which is the only place the two can be checked against each other.
#
# Everything the check needs is already in the `platform` stage above, so this reuses its
# cache entirely: on a build that has just produced `platform`, the marginal cost is one
# COPY, one RUN, and a few seconds.
#
# Invariant: This stage depends on `platform` ending as non-root (USER 10000:10000) so that
# entrypoint step 1 (stage2-hook.sh) executes as hermes rather than root.
FROM platform AS entrypoint-gate-test

COPY --chmod=0755 deploy/shared/entrypoint_gate_check.sh /usr/local/bin/entrypoint-gate-check


RUN /usr/local/bin/entrypoint-gate-check
