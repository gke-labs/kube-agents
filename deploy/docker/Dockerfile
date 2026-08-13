# check=skip=InvalidDefaultArgInFrom
#
# LAYER BUDGET. Docker's overlay2 driver refuses to mount a stack deeper than
# 128 layers ("max depth exceeded"), and the longest chain in this file --
# agent-base -> platform -> credential-proxy -- is measured against that ceiling
# by scripts/check_image_layers.py, which CI runs on every pull request.
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
# budget is shared by all three stages of that chain. Adding one is fine;
# adding a block of them is what to avoid. Where files share a destination they
# travel in one COPY, and appliers are staged into a directory rather than
# copied in one at a time -- that is why those instructions look the way they
# do. Run the checker rather than counting by hand:
#
#     python3 scripts/check_image_layers.py
#
ARG HERMES_AGENT_TAG
ARG ENVOY_VERSION=v1.38.0
FROM envoyproxy/envoy:${ENVOY_VERSION} AS envoy-bin

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
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS watcher-builder
ARG TARGETOS
ARG TARGETARCH
WORKDIR /workspace
COPY k8s-operator/ /workspace/k8s-operator/
WORKDIR /workspace/k8s-operator
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-amd64} \
    go build -ldflags="-w -s" -o /workspace/k8s-event-watcher ./cmd/k8s-event-watcher/

FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base

USER root

# 1. Install all common system utilities and dependencies
RUN apt-get update && apt-get install -y \
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

# 2. Install credential-free utility CLIs used by the sandbox.
RUN ARCH=$(dpkg --print-architecture) && \
    curl -sSLf -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v4.53.3/yq_linux_${ARCH}" && \
    chmod +x /usr/local/bin/yq && \
    curl -sSLf -o /tmp/k9s.tar.gz "https://github.com/derailed/k9s/releases/download/v0.51.0/k9s_Linux_${ARCH}.tar.gz" && \
    tar -xzf /tmp/k9s.tar.gz -C /usr/local/bin k9s && \
    rm /tmp/k9s.tar.gz && \
    curl -sSLf -o /tmp/helm.tar.gz "https://get.helm.sh/helm-v3.15.2-linux-${ARCH}.tar.gz" && \
    tar -xzf /tmp/helm.tar.gz -C /tmp && \
    mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm && \
    rm -rf /tmp/helm.tar.gz /tmp/linux-${ARCH}

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
# notifier just never delivered that kind. Two anchored edits, so the surgery
# lives in an applier script. See the module docstring in
# deploy/docker/patches/kanban_progress_lines.py.
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
COPY deploy/docker/patches/apply_kanban_progress_lines.py /tmp/apply_kanban_progress_lines.py
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_kanban_progress_lines.py /opt/hermes && \
    grep -q '"block_loop_detected", "heartbeat")' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q 'elif kind == "heartbeat":' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q 'note = _progress_note(ev.payload)' /opt/hermes/gateway/kanban_watchers.py && \
    grep -q '_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")' /opt/hermes/gateway/kanban_watchers.py && \
    /opt/hermes/.venv/bin/python3 -c "import pathlib; p='/opt/hermes/gateway/kanban_watchers.py'; compile(pathlib.Path(p).read_text(), p, 'exec')" && \
    cd /opt/hermes && /opt/hermes/.venv/bin/python3 -c "\
from gateway.kanban_progress_lines import progress_note; \
exit(1) if progress_note(None) or progress_note({}) or progress_note({'note': '  '}) else None; \
exit(1) if progress_note({'note': 'scanned 3 of 7'}) != 'scanned 3 of 7' else None; \
exit(1) if len(progress_note({'note': 'word ' * 500})) > 300 else None" && \
    rm /tmp/apply_kanban_progress_lines.py

# Hotfix: make a dispatched cron run report back, and stop it closing the
# kanban card of the caller that dispatched it. Upstream discards the run's
# final response and lets the run inherit HERMES_KANBAN_TASK, which is how
# three fleet-audit streams published wrong ledgers on 2026-08-04. Nine
# anchored edits across three files, so the surgery lives in an applier script
# rather than inline; it fails the build on any anchor that no longer matches.
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
RUN /opt/hermes/.venv/bin/python3 /tmp/apply_cron_run_scope.py /opt/hermes && \
    grep -q "outcome=None) -> bool:" /opt/hermes/cron/scheduler.py && \
    grep -q "with cron_run_scope(job_id):" /opt/hermes/tools/cronjob_tools.py && \
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
# Three consequences, all fixed by the eleven anchored edits in the applier:
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
# Must stay AFTER the cron_tick_lock_scope block: two of the fourteen anchors
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
# for `credential-proxy`, which has already hit Docker's layer-depth ceiling
# once (build f9f1747c, 2026-08-07), so patches here do not each buy a layer.
COPY deploy/docker/patches/apply_kanban_notifier.py \
     deploy/docker/patches/verify_kanban_notifier.py \
     deploy/docker/patches/patchlib.py \
     /tmp/kanban-notifier/
RUN /opt/hermes/.venv/bin/python3 /tmp/kanban-notifier/apply_kanban_notifier.py /opt/hermes && \
    grep -q "h = _clip_handoff(payload_summary)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "r = _clip_handoff(task.result)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q "handoff = _kanban_handoff_with_result(handoff, task)" /opt/hermes/gateway/kanban_watchers.py && \
    grep -q '_wake_kinds = _wake_kinds_for(d\["events"\], adapter=adapter)' /opt/hermes/gateway/kanban_watchers.py && \
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
# The patch resolves against the channel rather than the thread, probes each
# channel once and applies the answer to every entry on it (33 calls -> 1), and
# backs a channel off for six hours when it genuinely cannot be resolved so the
# next such bug is bounded rather than perpetual. Entry ids are NOT changed --
# the thread suffix is what routes a reply to the right thread; only the
# human-readable name is rewritten, and only its channel part, so two threads in
# one DM stay distinguishable.
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
    grep -q "conversations_info(channel=_channel_id)" /opt/hermes/gateway/channel_directory.py && \
    grep -q "from gateway.channel_directory_threads import" /opt/hermes/gateway/channel_directory.py && \
    ! grep -q 'conversations_info(channel=entry\["id"\])' /opt/hermes/gateway/channel_directory.py && \
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/channel-directory/verify_channel_directory_threads.py && \
    rm -rf /tmp/channel-directory

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
# block used to have are now two: `credential-proxy` builds FROM `platform` and
# has hit Docker's layer-depth ceiling once already (build f9f1747c,
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
# Must stay ahead of the kanban_wake_nudge block below, which anchors on
# `create_task`, `complete_task` and `unblock_task` in the same file. The six
# anchors here touch none of those three, and test_kanban_scheduling.py asserts
# that rather than leaving it to a reader.
# See the module docstring in deploy/docker/patches/kanban_scheduling.py and the
# applier docstring in deploy/docker/patches/apply_kanban_scheduling.py.
COPY deploy/docker/patches/kanban_scheduling.py /opt/hermes/hermes_cli/kanban_scheduling.py
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
    test "$(grep -c '(failures, error\[:500\], task_id),' /opt/hermes/hermes_cli/kanban_db.py)" = "2" && \
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
COPY deploy/docker/patches/kanban_guardrail_exit.py /opt/hermes/hermes_cli/kanban_guardrail_exit.py
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
# All four patches ship in ONE COPY + ONE RUN on purpose. `credential-proxy`
# builds FROM `platform`, and the block-per-patch layout these would normally get
# pushed that stage past Docker's layer-depth ceiling ("max depth exceeded" on
# build f9f1747c, 2026-08-07). The guarantees are unchanged — same appliers,
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
     deploy/docker/patches/patchlib.py \
     /tmp/latency-patches/
RUN cp /tmp/latency-patches/kanban_wake_nudge.py /opt/hermes/hermes_cli/kanban_wake_nudge.py && \
    cp /tmp/latency-patches/kanban_auto_subscribe.py /opt/hermes/tools/kanban_auto_subscribe.py && \
    cp /tmp/latency-patches/kanban_comment_status.py /opt/hermes/tools/kanban_comment_status.py && \
    cp /tmp/latency-patches/kanban_notify_delivery.py /opt/hermes/gateway/kanban_notify_delivery.py && \
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
    cd /opt/hermes && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_wake_nudge.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_auto_subscribe.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_comment_status.py && \
    /opt/hermes/.venv/bin/python3 /tmp/latency-patches/verify_kanban_notify_delivery.py && \
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
COPY --chown=hermes:hermes agents/platform/docs/glossary.md \
     agents/platform/docs/gcp-console-links.md \
     /opt/defaults/docs/


# 5.5. Install and enable hermes-otel plugin in /opt/defaults
RUN HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins install briancaffey/hermes-otel && \
    HOME=/opt/defaults HERMES_HOME=/opt/defaults /opt/hermes/.venv/bin/hermes plugins enable hermes_otel && \
    /opt/hermes/.venv/bin/python3 -c "import yaml, pathlib; p = pathlib.Path('/opt/defaults/plugins/hermes_otel/config.yaml'); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; c['backends'] = [{'name': 'gke-managed-otel', 'type': 'otlp', 'endpoint': 'http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces'}]; p.write_text(yaml.safe_dump(c))"

# Remove the build-only clone dependency after all plugin installation is done.
RUN apt-get purge -y git && apt-get autoremove -y && \
	rm -rf /var/lib/apt/lists/* && \
	for binary in gcloud kubectl gh git; do \
		if command -v "${binary}" >/dev/null 2>&1; then \
			echo "unexpected credential-aware CLI in sandbox image: ${binary}" >&2; \
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
# This must run AFTER every patch this stage applies to /opt/hermes: compileall
# stamps each .pyc with its source's mtime and size, so bytecode written before a
# patch would simply be discarded at import and we would be back to compiling on
# every start. Python that later stages add is not covered here, and the two
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

# 5.8. Copy custom agent startup script
# --chmod on the COPY rather than a chmod in the RUN below: same result, and the
# three destinations differ so these cannot be folded into one COPY. The layer
# budget is why the modes are not a fourth instruction — see the note at the top.
COPY --chmod=0755 deploy/shared/docker-entrypoint.sh /usr/local/bin/agent-entrypoint
COPY --chmod=0755 deploy/shared/credential-proxy-path.sh /etc/profile.d/credential-proxy-path.sh
COPY --chmod=0755 agents/platform/scripts/credential_proxy_client.py /usr/local/bin/credential-proxy-exec
RUN mkdir -p /opt/credential-proxy/bin && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/kubectl && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gcloud && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/gh && \
    ln -s /usr/local/bin/credential-proxy-exec /opt/credential-proxy/bin/git

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
# discard all of it. Step 2d rebuilds config.yaml from this template plus the operator's
# overlay plus the runtime's own edits, and it is the only writer.
COPY --chown=hermes:hermes agents/chat/config.yaml /opt/chat-template/config.yaml
RUN rm -f /opt/defaults/config.yaml

# --- Shared scripts (reachable by every profile at $HERMES_HOME/scripts) ---
# Executable scripts are shared, not per-profile: the router (Chat Agent) and the
# platform/cluster tooling all resolve under $HERMES_HOME/scripts.
COPY agents/chat/scripts/ /opt/defaults/scripts/
COPY agents/platform/scripts/ /opt/defaults/scripts/
# One COPY for the four, not four: `credential-proxy` builds FROM this stage and the
# chain is bounded at 128 layers — see the layer-budget note at the top of the file.
# They share a destination anyway, which default_profile_config.py requires.
#
#   profile_overlay.py         merges the operator's per-profile config overlays at
#                              startup (entrypoint step 2.7). Shipped as a file rather
#                              than inlined in the entrypoint so it can be unit tested —
#                              see tests/test_profile_overlay.py.
#   profile_plugins.py         links profile-targeted plugin image volumes into their
#                              profile homes (entrypoint step 2.65, and
#                              cluster_agent_profile.py for profiles scaffolded later) —
#                              see tests/test_profile_plugins.py.
#   default_profile_config.py  rebuilds the DEFAULT profile's config.yaml at startup
#                              (entrypoint step 2d) — see
#                              tests/test_default_profile_config.py. Imports merge() from
#                              profile_overlay.py, which is why the two must land in the
#                              same directory.
#   otel_config.py             points the hermes_otel plugin at the collector the operator
#                              resolved (entrypoint step 4, and cluster_agent_profile.py
#                              for profiles scaffolded later). The plugin does not read
#                              OTEL_EXPORTER_OTLP_ENDPOINT, so without this the endpoint
#                              baked above is the only one that ever takes effect — see
#                              tests/test_otel_config.py.
COPY deploy/shared/profile_overlay.py \
     deploy/shared/profile_plugins.py \
     deploy/shared/default_profile_config.py \
     deploy/shared/otel_config.py \
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
COPY deploy/shared/defaults/config.yaml /tmp/platform-config/base.yaml
COPY agents/platform/config.yaml \
     deploy/docker/merge_configs.py \
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
    rm -rf /tmp/platform-config

# Copy the Cluster Agent profile TEMPLATE (persona, config, skills), scaffolded
# into per-cluster Hermes profiles at runtime by scripts/cluster_agent_profile.py.
COPY --chown=hermes:hermes agents/cluster/ /opt/cluster-template/

# Plugins land in the shared plugin root regardless of which profile owns them —
# /opt/hermes/plugins is the only path Hermes resolves them from. The source
# directory records ownership: kube_agents_memory needs the user identity that
# only chat ingress carries, so it lives with the Chat Agent.
COPY agents/platform/plugins/ /opt/hermes/plugins/
COPY agents/chat/plugins/ /opt/hermes/plugins/

# NOTE: the platform overlay is no longer merged into /opt/defaults/config.yaml.
# /opt/defaults now belongs to the Chat Agent (`default` profile) and its config
# is copied verbatim above; the platform overlay is merged into
# /opt/platform-template/config.yaml instead (see the RUN block above).

# Normalize ownership and permissions at the end of build.
#
# `set -eu` rather than an `&&` chain ending in `|| true`: that trailing `|| true`
# bound to the WHOLE chain, so a failure in any early command short-circuited
# every later chmod and still exited 0 — a build that silently shipped
# unnormalized permissions. Each command must now fail the build loudly.
# Every path below is guaranteed to exist at this point: /opt/hermes/skills and
# /opt/hermes/plugins come from the base image, the rest are COPYed above.
RUN set -eu; \
    chown -R hermes:hermes /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/cluster-template /opt/platform-template /opt/chat-template; \
    for d in /opt/defaults /opt/hermes/skills /opt/hermes/plugins /opt/hermes/gateway /opt/hermes/tools /opt/cluster-template /opt/platform-template /opt/chat-template; do \
        find "$d" -type d -exec chmod 755 {} + ; \
        find "$d" -type f -exec chmod 644 {} + ; \
    done; \
    find /opt/defaults/scripts -type f -exec chmod 755 {} + ; \
    for d in /opt/hermes/skills /opt/platform-template /opt/cluster-template; do \
        find "$d" -path '*/scripts/*' -type f -exec chmod 755 {} + ; \
    done

# Second bytecode pass, for the Python this stage added.
#
# Step 5.7 in `agent-base` compiled /opt/hermes and the stdlib, and states why
# that has to happen at build time at all. It cannot cover what comes after it:
# `agents/platform/plugins/` lands in /opt/hermes/plugins here, so without this
# pass those modules are the one part of the runtime still re-parsed from source
# by every gateway start, every MCP child and every kanban worker. Everything
# already compiled above is up to date and skipped, so the marginal cost is the
# handful of files this stage introduced.
#
# Use the venv's own interpreter — the one that will import these modules. A
# .pyc is only loaded by an interpreter whose magic tag matches the writer's.
#
# No `-o`: the runtime sets neither PYTHONOPTIMIZE nor -O, so it looks for the
# plain `.cpython-NNN.pyc` that a bare compileall writes, not an `.opt-N` one.
# Setting PYTHONOPTIMIZE in the pod later without adding a matching `-o` here
# would quietly put the runtime back to compiling from source.
#
# Likewise assumes PYTHONPYCACHEPREFIX stays unset (it is), since that would
# redirect the lookup away from the in-tree __pycache__ this writes.
#
# Deliberately NOT extended to /opt/defaults, /opt/platform-template or
# /opt/cluster-template. The entrypoint copies all three onto the PVC at
# startup, and `cp` rewrites the destination mtime, which invalidates a
# timestamp-validated .pyc anyway — it would ship bytes that are guaranteed to
# be ignored.
#
# Runs after the normalization above rather than before it, so the generated
# __pycache__ directories are not swept up by the `*/scripts/*` chmod 755 there.
# They land root-owned 0644/0755 from the build umask, which is what the rest of
# /opt/hermes already is and is readable by the runtime uid.
#
# The failure branch reports rather than aborting, which is not the `|| true`
# the block above warns against: that one masked failures of unrelated commands
# in the same chain. This is one command whose only effect is a cache. A module
# that will not precompile — a py2 relic or a conditionally-invalid file, which
# large vendored trees routinely carry — simply compiles at import as it does
# today. `set -eu` still governs everything else.
#
# No speedup figure is claimed for this pass on its own; the 12.5s -> 6.0s in
# step 5.7 is the whole tree, of which the plugins here are a few modules.
RUN set -eu; \
    /opt/hermes/.venv/bin/python3 -m compileall -q /opt/hermes \
      || echo "WARN: some modules under /opt/hermes did not precompile; they will compile at import, as they do without this step" >&2

# --- CREDENTIAL PROXY TARGET ---
# Only this image contains the real credential-aware CLIs. The platform target
# above contains wrappers with the same names and cannot execute locally.
FROM platform AS credential-proxy

USER root

# No `mkdir -p /etc/envoy` ahead of the config COPY: COPY creates the missing
# parent itself, and the RUN below sets its mode. This stage sits at the end of
# the longest chain in the file, so an instruction that buys nothing is one the
# chain cannot afford — see the layer-budget note at the top.
COPY --from=envoy-bin --chmod=0755 /usr/local/bin/envoy /usr/local/bin/envoy
COPY --chmod=0644 deploy/shared/envoy-credential-proxy.yaml /etc/envoy/envoy-credential-proxy.yaml
COPY --chmod=0755 deploy/shared/start-services.sh /usr/local/bin/start-services

RUN curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor --yes -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y \
      git \
      google-cloud-cli \
      google-cloud-cli-gke-gcloud-auth-plugin \
      kubectl && \
    ARCH=$(dpkg --print-architecture) && \
    curl -fL -o /tmp/gh.deb "https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_${ARCH}.deb" && \
    dpkg -i /tmp/gh.deb && \
    rm -f /tmp/gh.deb && \
    rm -rf /var/lib/apt/lists/* && \
    chmod 755 /etc/envoy


# --- ENTRYPOINT GATE TEST TARGET ---
# Not shipped, and not a layer of anything that is. Built only by
# `--target entrypoint-gate-test`, where the RUN either passes or fails the build.
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
FROM platform AS entrypoint-gate-test

COPY --chmod=0755 deploy/shared/entrypoint_gate_check.sh /usr/local/bin/entrypoint-gate-check

# As the runtime UID, not as root, and the difference decides whether this stage tests
# anything at all. The `platform` stage is still USER root from the base image's setup, but
# no container ever runs it that way: the operator pins runAsUser/fsGroup 10000 with
# runAsNonRoot, so every pod executes the entrypoint as hermes.
#
# Step 1 of the entrypoint runs upstream's stage2-hook.sh, which branches on exactly this:
#
#     as_hermes() { [ "$(id -u)" = 0 ] || { "$@"; return; }; s6-setuidgid hermes "$@"; }
#
# As hermes that short-circuits. As root it reaches for s6-setuidgid, which lives in
# /command and is on no PATH outside s6-overlay's own init — so the hook exits non-zero,
# the entrypoint's `set -eu` takes the script down at step 1, and all sixteen checks fail
# having never reached the gate they exist to test. A green build here has to mean the gate
# is right, not that the image happened to be assembled by the same user that runs it.
USER 10000

RUN /usr/local/bin/entrypoint-gate-check
