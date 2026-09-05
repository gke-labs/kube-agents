#!/usr/bin/env bash
# ==============================================================================
# Prow CI Evaluation Pipeline Script
# ==============================================================================
# Runs devops-bench evaluation against deployed platform-agent.
#
# Evaluates the task matrix in section 6 EVAL_REPETITIONS times per task and
# hands the records to `bench-gate`, which applies the rate-based gate:
# a per-case verdict ladder, a collapse rule that needs every repetition to
# fail on a case with screening evidence, and a suite aggregate. The gate is
# two-speed as before -- deterministic verification keys block, judged scores
# are recorded and gate nothing -- but the decision now lives in tested Python
# (bench/kube_agents_bench/) rather than in inline heredocs here. This script
# keeps what is genuinely shell: the loop, the repetitions, the run-directory
# diffing and the artifact handling.
#
# Why a rate and not a pass: at two hundred cases and 95% per-case
# reliability, "every case passes every run" is clean on 0.003% of runs, and a
# gate that reds seven pull requests in eight is a gate people learn to
# ignore. See bench/baselines/README.md for what admits a case.
# ==============================================================================

set -euo pipefail

# ─── Step timing profiler ────────────────────────────────────────────────────
# Contiguous named spans: each profile_begin closes the previous span and opens
# the next, so the report's percentages always sum to 100% of the wall clock
# between script start and the report. python3 is already a hard dependency of
# the gate below, so it is what supplies millisecond epochs.
PROFILE_ROWS=()
PROFILE_CURRENT=""
_now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }
PROFILE_T0="$(_now_ms)"
PROFILE_LAST="${PROFILE_T0}"

profile_begin() {
  local now
  now="$(_now_ms)"
  if [ -n "${PROFILE_CURRENT}" ]; then
    PROFILE_ROWS+=("${PROFILE_CURRENT}|$((PROFILE_LAST - PROFILE_T0))|$((now - PROFILE_LAST))")
  fi
  PROFILE_CURRENT="$1"
  PROFILE_LAST="${now}"
  echo "--- [PROFILE $(date -u +'%Y-%m-%dT%H:%M:%SZ')] step: $1 ---"
}

profile_report() {
  local exit_code="$1" now
  now="$(_now_ms)"
  if [ -n "${PROFILE_CURRENT}" ]; then
    PROFILE_ROWS+=("${PROFILE_CURRENT}|$((PROFILE_LAST - PROFILE_T0))|$((now - PROFILE_LAST))")
    PROFILE_CURRENT=""
  fi
  PROFILE_DATA="$(printf '%s\n' ${PROFILE_ROWS[@]+"${PROFILE_ROWS[@]}"})" \
  PROFILE_EXIT_CODE="${exit_code}" python3 <<'PY' || true
import os

rows = []
for line in os.environ.get("PROFILE_DATA", "").splitlines():
    if not line.strip():
        continue
    name, start_ms, dur_ms = line.rsplit("|", 2)
    rows.append((name, int(start_ms), int(dur_ms)))
total = sum(d for _, _, d in rows)
print(f"\n=== Step timing profile (exit code {os.environ['PROFILE_EXIT_CODE']}) ===")
if not rows or total <= 0:
    print("no profiled spans recorded")
else:
    # Largest-remainder rounding in tenths of a percent, so the printed
    # column sums to exactly 100.0 instead of drifting with row count.
    tenths, rems = [], []
    for _, _, d in rows:
        q, r = divmod(d * 1000, total)
        tenths.append(q)
        rems.append(r)
    for i in sorted(range(len(rows)), key=lambda i: rems[i], reverse=True)[: 1000 - sum(tenths)]:
        tenths[i] += 1
    print(f"{'start(s)':>10} {'dur(s)':>10} {'%':>7}  step")
    for (name, start_ms, dur_ms), t in zip(rows, tenths):
        print(f"{start_ms / 1000:10.1f} {dur_ms / 1000:10.1f} {t / 10:6.1f}%  {name}")
    print(f"{'':>10} {total / 1000:10.1f} {'100.0':>6}%  TOTAL")
PY
}

# Prefix every line flowing through with "[TS <epoch.ms>]". devops-bench's own
# logger is never configured by its CLI (NullHandler swallows the INFO phase
# lines), so the wrapper stamps wall-clock time onto the subprocess's output
# itself and the phase analyzer below keys on content markers instead.
_ts_lines() {
  python3 -u -c 'import sys, time
for line in iter(sys.stdin.readline, ""):
    sys.stdout.write("[TS %.3f] " % time.time() + line)
    sys.stdout.flush()'
}

# Per-task deep dive: split one devops-bench invocation into phases using the
# [TS ...] stamps and the phase-boundary text the run actually prints (tofu
# apply/destroy, the first DeepEval judge banner), plus the agent latency the
# results.json record carries. Informational — the top-level profile table is
# the one whose steps sum to 100% of the script's span; this table sums to
# 100% of the single task's devops-bench run.
analyze_eval_phases() {
  EVAL_PHASE_LOG="$1" EVAL_PHASE_START_MS="$2" EVAL_PHASE_END_MS="$3" \
  EVAL_PHASE_TASK="$4" EVAL_PHASE_RESULT="${5:-}" python3 <<'PY' || true
import json
import os
import re

log = os.environ["EVAL_PHASE_LOG"]
start = int(os.environ["EVAL_PHASE_START_MS"]) / 1000.0
end = int(os.environ["EVAL_PHASE_END_MS"]) / 1000.0
task = os.environ["EVAL_PHASE_TASK"]
result = os.environ.get("EVAL_PHASE_RESULT", "")

latency = None
if result and os.path.exists(result):
    try:
        data = json.load(open(result))
        rec = data[0] if isinstance(data, list) else data
        latency = float(rec.get("latency") or 0) or None
    except Exception:
        pass

# Ordered phase-opening markers; a match is only accepted at or after the
# last matched position, so a stray earlier occurrence cannot reorder phases.
# Markers absent from a run (noop deployer, crash) collapse their phase into
# the neighbour's.
MARKERS = [
    ("Initializing the backend", "provision (tofu init + apply)"),
    ("Apply complete!", "scenario setup + agent execution"),
    (": Destroying...", "teardown (tofu destroy)"),
    ("You're running DeepEval", "scoring (LLM judge) + persist"),
]
ts_re = re.compile(r"^\[TS (\d+(?:\.\d+)?)\] (.*)$")
found = []
idx = 0
try:
    with open(log, errors="replace") as fh:
        for line in fh:
            if idx >= len(MARKERS):
                break
            m = ts_re.match(line)
            if not m:
                continue
            t, content = float(m.group(1)), m.group(2)
            for j in range(idx, len(MARKERS)):
                if MARKERS[j][0] in content:
                    found.append((MARKERS[j][1], min(max(t, start), end)))
                    idx = j + 1
                    break
except OSError as exc:
    print(f"    phase breakdown unavailable: {exc}")
    raise SystemExit(0)

# The agent's own span is recorded, not logged: results.json carries its
# latency. With infrastructure, anchor it forward from "Apply complete!" and
# split what follows into the drain; without (noop deployer), work backward
# from where scoring begins — the agent runs immediately before it.
labels = [label for label, _ in found]
if latency:
    if "scenario setup + agent execution" in labels:
        i = labels.index("scenario setup + agent execution")
        nxt = found[i + 1][1] if i + 1 < len(found) else end
        cut = min(found[i][1] + latency, nxt)
        if cut < nxt:
            found.insert(i + 1, ("post-agent drain (verify/metrics, record)", cut))
    elif "scoring (LLM judge) + persist" in labels:
        i = labels.index("scoring (LLM judge) + persist")
        found.insert(i, ("agent execution", max(found[i][1] - latency, start)))

print(f"    ── devops-bench phase breakdown for {task} ──")
if not found:
    print("    no phase markers found in the log; cannot split the run")
else:
    bounds = [("harness startup (uv sync, imports, task load)", start)] + found + [("(end)", end)]
    total = max(end - start, 1e-9)
    for (label, t0), (_, t1) in zip(bounds, bounds[1:]):
        d = max(t1 - t0, 0.0)
        print(f"    {d:9.1f}s {100 * d / total:6.1f}%  {label}")
    print(f"    {total:9.1f}s  100.0%  total devops-bench run")
    if latency:
        print(f"    (agent latency from results.json: {latency:.1f}s)")
PY
}

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
profile_begin "bootstrap: source ci-env.sh"
source "${SCRIPT_DIR}/ci-env.sh"

# ─── Eval dashboard publish hook (dashboard PR 4/4) ─────────────────────────
# Re-renders and republishes the eval dashboard at the very end of every
# MAIN-BRANCH run, red or green, from the EXIT trap below. FAIL-SAFE BY
# CONTRACT: the dashboard must never break the job it observes, so every
# failure mode -- the sibling dashboard PRs not merged yet (no
# scripts/eval_dashboard/), the IAM grant not applied, a gsutil error, a
# python crash, a hung upload -- logs exactly ONE
# "eval-dashboard publish skipped: <reason>" line and never changes the job's
# exit code.
#
# MAIN-BRANCH RUNS ONLY, the baseline store's trust boundary: a presubmit
# runs branch-authored code, so publishing from one would let any pull
# request rewrite the dashboard everyone reads -- both through the bucket
# credential and through collect.py, which reads TASKS and the domain
# metadata out of THIS checkout. The gate is the baseline recorder's
# (JOB_TYPE postsubmit/periodic, no PULL_NUMBER), re-derived here because the
# trap can fire from a set -e death long before that code runs. The gate
# alone is conventional -- a branch can edit this file -- which is why
# prerequisite 2 below puts the credential itself out of the presubmit's
# reach; that split is what makes the boundary structural, exactly as
# docs/designs/eval-scorer.md#the-two-service-accounts argues for the
# baseline store.
#
# Nothing publishes until BOTH prerequisites exist:
#   1. the nightly periodic (NEVER the presubmit) exports
#      EVAL_DASHBOARD_TARGET (gs://kube-agents-dashboards/evals/, a dedicated
#      bucket in the team's own project) -- an oss-test-infra change;
#   2. a DEDICATED publisher identity bound to that periodic alone --
#      eval-dashboard-publisher@kube-agents-prow.iam.gserviceaccount.com via
#      Workload Identity, the eval-baseline-recorder pattern from
#      docs/designs/eval-scorer.md#provisioning-it, NEVER the shared
#      prowjob-default-sa every presubmit also runs as -- holding
#      roles/storage.objectUser on the kube-agents-dashboards bucket (a grant
#      in the team's project, not the OSS Prow infra project). Republishing
#      overwrites the same object paths, so any workable role carries
#      storage.objects.delete; the boundary is the identity, not the role:
#      no account a presubmit can run as ever holds a write on this bucket.
#      The same identity also needs READ on the sweep's source --
#      roles/storage.objectViewer on gs://kube-agents-prow -- unless that
#      bucket's existing public read already covers it; without it the first
#      armed run 403s, which the zero-runs floor below turns into a skip,
#      never into publishing an empty dashboard over a good one.
# Until both land this costs one log line per run.
# scripts/test_eval_dashboard_publish.py runs this function out of this file
# and asserts the fail-safe AND the main-branch gate hold.
publish_eval_dashboard() {
  case "${JOB_TYPE:-}" in
    postsubmit | periodic) ;;
    *)
      echo "eval-dashboard publish skipped: not a main-branch run (JOB_TYPE=${JOB_TYPE:-unset}): a pull request never writes the dashboard"
      return 0
      ;;
  esac
  if [ -n "${PULL_NUMBER:-}" ]; then
    echo "eval-dashboard publish skipped: PULL_NUMBER=${PULL_NUMBER} is set: a pull request never writes the dashboard"
    return 0
  fi
  if [ -n "${RC_COMMIT_SHA:-}" ]; then
    echo "eval-dashboard publish skipped: RC_COMMIT_SHA=${RC_COMMIT_SHA} is set: a release-candidate run measures a candidate, it does not report main's history"
    return 0
  fi
  if [ -z "${EVAL_DASHBOARD_TARGET:-}" ]; then
    echo "eval-dashboard publish skipped: EVAL_DASHBOARD_TARGET is not set (the Prow job config arms this later)"
    return 0
  fi
  local dash_src="${SCRIPT_DIR}/../scripts/eval_dashboard"
  # All three stages, not just the first: the siblings land one file each
  # (collect.py merged in #1044; render.py and publish.py are still open), and
  # gating on collect.py alone would run its full GCS sweep only to die at
  # render.py -- the guard must keep the hook CHEAP while any stage is absent.
  local dash_stage
  for dash_stage in collect.py render.py publish.py; do
    if [ ! -f "${dash_src}/${dash_stage}" ]; then
      echo "eval-dashboard publish skipped: ${dash_src}/${dash_stage} does not exist (sibling dashboard PRs not merged yet)"
      return 0
    fi
  done
  local dash_tmp dash_rc=0
  dash_tmp="$(mktemp -d)" || { echo "eval-dashboard publish skipped: mktemp -d failed"; return 0; }
  # One timeout over the whole collect -> render -> publish pipeline so a hung
  # gsutil cannot eat the job's tail. errexit lives inside the child only; out
  # here any failure becomes the one skip line. The array idiom is the
  # PROFILE_ROWS one above: no `timeout` binary (a laptop) must degrade to
  # running unbounded, not to breaking the trap.
  #
  # The budget must be LARGER than the 300s collect.py grants each individual
  # gsutil call, or the one hung call the collector is willing to wait out
  # kills the whole pipeline instead -- and the sweep is serial over every
  # archived build (1 + 3N gsutil processes), so it needs real headroom on
  # top. 900s covers both and only ever taxes the nightly's tail (the gate
  # above keeps presubmits out entirely); EVAL_DASHBOARD_TIMEOUT overrides it
  # from the job config without a code change. Bounding the sweep itself
  # (--since/--limit) is collect.py's follow-up, not this hook's.
  local dash_budget="${EVAL_DASHBOARD_TIMEOUT:-900}"
  local dash_timeout=(timeout "${dash_budget}")
  command -v timeout >/dev/null 2>&1 || dash_timeout=()
  # Single quotes on purpose: $1/$2/$3 are the child bash's own positionals.
  # The zero-runs floor between collect and render is the evidence_store
  # lesson (StoreUnreachable vs "empty store"): collect.py WARNS and
  # continues when a gsutil listing fails, so a total source outage -- a 403
  # before the read grant lands, no gsutil on PATH -- still yields a
  # well-formed document with runs: [] and exit 0. Publishing that would
  # overwrite a good dashboard with an empty one and log success; the floor
  # turns it into the skip line instead.
  # shellcheck disable=SC2016
  ${dash_timeout[@]+"${dash_timeout[@]}"} bash -c '
    set -euo pipefail
    python3 "$1/collect.py" --pr-glob "gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/*/pull-kube-agents-smoke-test/*" --out "$2/data.json"
    python3 -c "
import json, sys
if not json.load(open(sys.argv[1], encoding=\"utf-8\")).get(\"runs\"):
    sys.exit(\"collected zero runs: source unreadable or empty; refusing to publish an empty dashboard over a good one\")
" "$2/data.json"
    python3 "$1/render.py" --data "$2/data.json" --out-dir "$2/site"
    python3 "$1/publish.py" --out-dir "$2/site" --target "$3"
  ' _ "${dash_src}" "${dash_tmp}" "${EVAL_DASHBOARD_TARGET}" >"${dash_tmp}/publish.log" 2>&1 || dash_rc=$?
  if [ "${dash_rc}" -eq 0 ]; then
    echo "eval-dashboard: published to ${EVAL_DASHBOARD_TARGET}"
  else
    echo "eval-dashboard publish skipped: pipeline exited ${dash_rc} (124 means the ${dash_budget}s timeout): $(tail -n 3 "${dash_tmp}/publish.log" 2>/dev/null | tr '\n' ' ')"
  fi
  # The full pipeline log rides to Prow on success AND failure: collect.py's
  # per-build fetch errors are warnings, not failures, and those warnings are
  # the only after-the-fact evidence that a published dashboard came from a
  # partial sweep.
  if [ -n "${ARTIFACTS:-}" ] && [ -d "${ARTIFACTS}" ]; then
    cp "${dash_tmp}/publish.log" "${ARTIFACTS}/eval-dashboard-publish.log" 2>/dev/null || true
  fi
  rm -rf "${dash_tmp}" || true
  return 0
}

# Print the profile on every exit — success, gate failure, or a set -e death —
# then hand the original exit code to the artifact dumper ci-env.sh provides.
#
# collect_bench_results runs on green too, and that is the whole point: the
# baseline store the gate compares against is built from PASSING runs on main,
# and those are exactly the records the old failure-only trap threw away. It
# cannot precede the `$?` capture, so it sits immediately after it.
#
# `set +e` is load-bearing, not tidying. errexit stays in force inside an EXIT
# trap, so on any failing exit the `(exit "${exit_code}")` below returns
# non-zero and aborts the trap on that line -- and the dumper on the next line
# never runs. Every red eval job would lose the kubectl logs, pod descriptions
# and events that tell a transport storm from a real failure, while the
# comment above claims the exit code is handed to the dumper. Reproduce with:
#
#   bash -c 'set -e; f(){ local c=$?; (exit $c); echo reached; }; \
#            trap f EXIT; exit 7'   # never prints "reached"
#
# Clearing errexit after `$?` is captured keeps the subshell's job of setting
# `$?` for the dumper, and bash still exits with the original status.
profile_and_dump_on_exit() {
  local exit_code=$?
  set +e
  collect_bench_results
  profile_report "${exit_code}"
  (exit "${exit_code}")
  dump_prow_artifacts_on_failure
  # Dashboard last, after the artifacts the run itself needs; the exit code
  # was captured above and publish_eval_dashboard never returns non-zero, so
  # this cannot change what Prow reports (errexit is already cleared above).
  publish_eval_dashboard
}
trap profile_and_dump_on_exit EXIT
# A Prow deadline delivers SIGTERM, which does not run the EXIT trap on its
# own; converting it to an exit is what lets the artifact collection above
# fire on a deadline kill.
trap 'exit 143' TERM INT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running PR Smoke Test Evaluation for PR #${PR_ID} in Namespace: ${TARGET_NAMESPACE} ==="

# 2. Cluster Auth
profile_begin "cluster-auth: gcloud get-credentials"
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$HOST_CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# 2b. Seeded-fleet credentials, one kubeconfig per fixture ROLE.
#
# The get-credentials above is the ONLY one this script used to do, and it
# points at platform-agent-host. The seeded fleet (bench/tf/fleet/) is other
# clusters, so a cluster-state check reading the ambient kubeconfig asks the
# wrong API server -- blocker A5 in bench/tasks/DRAFTS.md. This writes the
# fleet's credentials into their own files, keyed by fixture role, and touches
# neither the ambient kubeconfig nor the current context.
#
# Clusters are found by label rather than by name, so this does not need to
# know the leased project's cluster prefix or region.
#
# Non-fatal by design: an unreachable seeded cluster -- or a leased project the
# fleet was never applied to -- leaves its roles' files absent, and
# `fleet_resource_property` turns that into status=error naming the role and
# the project: failing the checks that needed that cluster rather than the job,
# and never silently reading platform-agent-host instead.
#
# It ran on every presubmit for weeks while every task that consumes it was
# still commented out of TASKS below, and that was the point: the warnings it
# prints per project ("carries no clusters labelled environment=seeded") are
# how a pool project still needing bench/tf/fleet applied was found BEFORE
# these tasks started gating PRs rather than after. Eleven of the active
# tasks below read the seeded fleet (six domain probes, the fleet-audits
# canary, cluster-agent-crashloop-debug and the three cluster-debugging
# cases beside it), so those warnings have consumers. It costs one
# clusters.list, one get-credentials per seeded cluster, and one namespace
# read per probe -- seconds, against a job measured in tens of minutes.
#
# The `||` catches a REPOSITORY bug only: a missing or malformed
# bench/tf/fleet/fixtures.json, or an unusable output directory. Every
# environmental failure -- no fleet in this project, a cluster that will not
# answer, a fixture that was never planted -- returns 0 with a warning of its
# own and leaves the affected roles' files absent, which is the whole design.

# The read-only identity the role kubeconfigs should carry. It cannot be a
# static export in the Prow job the way EVAL_GITHUB_APP_ID is: the account is
# per project (`seeded-fleet-reader@<project>.iam.gserviceaccount.com`,
# bench/tf/fleet/main.tf:123) and Boskos picks the project at lease time, so
# this is the first point in the run that knows which one to name. An
# explicitly-set value still wins, for a laptop pointing at a fleet it does not
# own.
#
# Only half of this is in the repository. The other half is the token-creator
# grant -- `fleet_reader_token_creators` in bench/tf/fleet/variables.tf, empty
# by default -- which is a per-project `tofu apply` a human has to do, naming
# that project's Prow runner identity. Until it is done in a leased project,
# `gcloud auth print-access-token --impersonate-service-account` fails,
# fleet-kubeconfigs.sh warns per cluster, and the role kubeconfigs keep the
# runner's own read-write credential. That is a privilege gap on a fleet every
# open PR shares, not a functional one: the files are still written, still
# point at the right seeded cluster, and every check still grades the right
# object. See bench/tf/fleet/README.md, "A read-only credential for
# evaluations".
export FLEET_READONLY_SA="${FLEET_READONLY_SA:-seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com}"

profile_begin "fleet-kubeconfigs: seeded-fleet credentials"
STEP_START=$SECONDS
# shellcheck source=hack/fleet-kubeconfigs.sh
source "${SCRIPT_DIR}/fleet-kubeconfigs.sh"
write_fleet_kubeconfigs || echo "WARNING: the seeded-fleet catalog or output directory is unusable, so no fleet kubeconfigs were written at all; every fleet fixture check will report status=error" >&2
echo "✓ Seeded-fleet credentials finished in $((SECONDS - STEP_START))s"

# 3. Agent & Harness Configuration
profile_begin "config: env, platform-agent token fetch, prereqs"
# Configures devops-bench runner to target deployed platform-agent service
export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="kubeagents"
export BENCH_PARALLEL="false"
export AGENT_CLUSTER_CONTEXT="gke_${PROJECT_ID}_${REGION}_${HOST_CLUSTER_NAME}"
export AGENT_SERVICE_NAME="platform-agent"
export AGENT_NAMESPACE="${TARGET_NAMESPACE}"
# The harness's default delegation wait (1800s) sits INSIDE the compliance
# canary's observed completion spread: on 2026-08-27 (build
# 2093054394793725952, kube-agents-evals-2) the audit worker finished and
# rewrote its ledger at 20:30:37Z -- five minutes AFTER the wait gave up at
# ~20:24 -- and the run graded a bare receipt as the answer. Observed audit
# completions: 606s / 827s / 1497s / ~2170s on identical inputs. 2700s puts
# the ceiling above the worst observed; the variance itself is #985's
# problem, this export just stops mislabeling slowness as wrongness.
export AGENT_DELEGATION_TIMEOUT="2700"
export BENCH_TF_ROOT="./tf"

# ─── Ledger read credential ──────────────────────────────────────────────────
# BENCH_GITHUB_TOKEN is what ledger_issue_contains reads a published ledger
# issue back with. Prow mounts a fine-grained PAT under that name, and only its
# owner can extend that PAT to a new pool repository -- so kube-agents-evals-6
# passed every onboarding check, was registered, and 404'd on the first pull
# request that leased it (gke-labs/kube-agents#994).
#
# EVAL_LEDGER_APP_KEY_FILE set: mint a read-only installation token from App
# 4739812 instead, once per fan-out unit, because a token lasts an hour and
# units launch across the whole run. Unset: the mounted PAT stands. A mint that
# fails after its retries stops the run at preflight and costs a unit its
# repetition inside the fan-out; it never falls back to the PAT, which would
# let a smoke test pass while proving nothing about the credential it was added
# to exercise.
export EVAL_LEDGER_APP_ID="${EVAL_LEDGER_APP_ID:-4739812}"
export EVAL_LEDGER_INSTALLATION_ID="${EVAL_LEDGER_INSTALLATION_ID:-157029058}"
# Re-exported so the mint reads it however it was set: the Prow job exports it,
# a shell that sourced this file may not have, and python reads it from the
# environment rather than from an argument.
export EVAL_LEDGER_APP_KEY_FILE="${EVAL_LEDGER_APP_KEY_FILE:-}"

# Exit code _ledger_token_mint uses for a failure that another attempt could
# survive, so mint_ledger_token retries those and no others. 75 is sysexits.h's
# EX_TEMPFAIL, which is what it means here.
LEDGER_MINT_RETRYABLE=75
# Three attempts, 2s then 8s apart. api.github.com being briefly unreachable is
# the case this covers, and it costs 10s to rule out; a longer ladder would sit
# inside a unit that is holding both locks.
LEDGER_MINT_ATTEMPTS=3

# Emits "<token> <expires_at>" on stdout, diagnostics on stderr, non-zero on
# any failure -- LEDGER_MINT_RETRYABLE when another attempt could survive it,
# 1 when it could not. Its own function rather than inline in the command
# substitution below: bash 3.2, which is what macOS ships and what a
# contributor runs `bash -n` with, mis-parses a heredoc inside $( ).
_ledger_token_mint() {
  python3 - "${LEDGER_MINT_RETRYABLE}" <<'PY'
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Passed in rather than duplicated, so the two halves of the contract cannot
# drift: the shell decides what it retries, this decides what is retryable.
retryable = int(sys.argv[1])


def temporary(message):
    sys.stderr.write(message + "\n")
    sys.exit(retryable)


key_file = os.environ["EVAL_LEDGER_APP_KEY_FILE"]
app_id = os.environ["EVAL_LEDGER_APP_ID"]
installation_id = os.environ["EVAL_LEDGER_INSTALLATION_ID"]


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


# GitHub rejects an App JWT whose exp is more than ten minutes out; nine leaves
# room for clock skew, and the backdated iat covers a runner that is slow.
now = int(time.time())
header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
payload = b64(
    json.dumps(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}, separators=(",", ":")
    ).encode()
)
signing_input = header + b"." + payload

signed = subprocess.run(
    ["openssl", "dgst", "-sha256", "-sign", key_file],
    input=signing_input,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
if signed.returncode != 0:
    sys.exit(
        "openssl could not sign with %s: %s" % (key_file, signed.stderr.decode()[:300])
    )
jwt = (signing_input + b"." + b64(signed.stdout)).decode("ascii")

request = urllib.request.Request(
    "https://api.github.com/app/installations/%s/access_tokens" % installation_id,
    method="POST",
    headers={
        "Authorization": "Bearer " + jwt,
        "Accept": "application/vnd.github+json",
        "User-Agent": "kube-agents-ci-eval-pr",
    },
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
except urllib.error.HTTPError as exc:
    # 401: the PEM is not App app_id's. 404: the installation id is wrong, or
    # the App was uninstalled from the org. Neither survives another attempt,
    # and a caller holding two locks should hear about them on the first.
    # 403 stays terminal with them: on this endpoint it is a suspended
    # installation as often as a secondary rate limit, and the two read alike
    # from here.
    message = "GitHub answered HTTP %d (%s) minting for App %s installation %s" % (
        exc.code,
        exc.reason,
        app_id,
        installation_id,
    )
    if exc.code >= 500 or exc.code == 429:
        temporary(message)
    sys.exit(message)
except Exception as exc:
    # A timeout, a reset connection, DNS: api.github.com was not reached, which
    # says nothing about the credential.
    temporary(
        "could not reach api.github.com to mint for App %s (%s: %s)"
        % (app_id, type(exc).__name__, exc)
    )

print(body["token"] + " " + body["expires_at"])
PY
}

# Puts a fresh token in the CALLING shell's BENCH_GITHUB_TOKEN and prints where
# it came from and when it expires, never the token itself. <label> names the
# caller, because fan-out units print these lines interleaved.
#
# Returns non-zero rather than exiting: the unit call site holds two locks by
# the time it mints, and exiting there would strand them. Each caller unwinds
# its own scope. Never falls back to the mounted PAT -- that would let a smoke
# test pass while proving nothing about the credential it exercises.
mint_ledger_token() { # <label>
  if [ -z "${EVAL_LEDGER_APP_KEY_FILE:-}" ]; then
    return 0
  fi
  # The token never reaches argv, where ps would show it: python writes it to
  # stdout and command substitution keeps it in this shell.
  #
  # Retried because the alternative is worse than the wait. A unit that cannot
  # mint releases its locks and returns, its repetition has no run directory,
  # and the gate grades that MISSING -- rung CHECK_DID_NOT_RUN, which is
  # blocking and whose reason line blames a harness or agent crash. So a single
  # unreachable api.github.com reds the suite and points the reader at the
  # agent. Retrying only what could survive one keeps a real credential fault
  # arriving on the first attempt.
  local minted rc attempt=1 delay=2
  while :; do
    minted="$(_ledger_token_mint)" && break
    rc=$?
    if [ "${rc}" -ne "${LEDGER_MINT_RETRYABLE}" ] || [ "${attempt}" -ge "${LEDGER_MINT_ATTEMPTS}" ]; then
      echo "ERROR: ${1}: could not mint a ledger read token from App ${EVAL_LEDGER_APP_ID}," \
           "installation ${EVAL_LEDGER_INSTALLATION_ID}, key ${EVAL_LEDGER_APP_KEY_FILE}." >&2
      echo "       Grading a ledger issue needs it; not falling back to the mounted PAT." >&2
      return 1
    fi
    echo "Ledger token (${1}): attempt ${attempt} of ${LEDGER_MINT_ATTEMPTS} hit a transient failure, retrying in ${delay}s" >&2
    sleep "${delay}"
    attempt=$((attempt + 1))
    delay=$((delay * 4))
  done
  export BENCH_GITHUB_TOKEN="${minted%% *}"
  echo "Ledger token (${1}): minted from App ${EVAL_LEDGER_APP_ID}, installation ${EVAL_LEDGER_INSTALLATION_ID}, expires ${minted##* }"
}

# Once here as well as once per unit: a key that cannot mint at all is a
# run-wide fault, and it costs seconds to find out now instead of at the end of
# the fan-out, where it would surface as every repetition grading MISSING.
if [ -z "${EVAL_LEDGER_APP_KEY_FILE:-}" ]; then
  echo "Ledger token: using the mounted BENCH_GITHUB_TOKEN -- EVAL_LEDGER_APP_KEY_FILE is unset"
else
  mint_ledger_token "preflight" || exit 1
fi

# For opentofu provider
export CLOUD_PROVIDER="gcp"
export TF_VAR_infra_provider="gcp"

# The cluster the agent install runs on, for the one stack that needs it.
# Every other stack under bench/tf builds its own cluster or reuses the seeded
# slot-c one; prebuilt/autoops-incident can use neither, because the incident
# it plants has to be seen by k8s-event-watcher, which runs as a peer process
# inside the Platform Agent pod. The watcher does fan in over the Cluster Agent
# profile clusters as well as its own, but a per-run cluster reaches that watch
# set too late to be watched inside the run -- see the header of
# bench/tf/prebuilt/autoops-incident/main.tf. An incident there goes
# undetected and the case waits out its timeout for a card nobody filed. A
# stack that does not declare these ignores them.
export TF_VAR_host_cluster_name="${HOST_CLUSTER_NAME}"
export TF_VAR_host_cluster_location="${REGION}"
export TF_VAR_agent_namespace="${TARGET_NAMESPACE}"

# Per-run task-cluster name, derived from the Prow run identity. Within a
# project, two runs can never race on one cluster because they never share a
# name, and a "409 Already Exists" between runs is impossible by construction.
# The old fixed name ("test-cluster") was unsafe the moment two runs shared the
# project.
#
# This alone does NOT make raising the Prow job's max_concurrency safe: every
# run also installs cluster-wide singletons (CRDs, webhooks, ClusterRoles) on
# the shared platform-agent-host cluster. Real concurrency arrives with issue
# #637 (Boskos one-project-per-run leasing); do not raise max_concurrency
# before it. Unique names still matter under #637 -- a retried run in a
# freshly-leased project must not collide with what its predecessor left.
#
# GKE caps names at 40 chars matching [a-z]([-a-z0-9]*[a-z0-9])?. The name is
# lowercased and non-alphanumerics collapse to hyphens; locally it falls back
# to a stable "eval-pr0-<user>" so two laptops sharing a project do not
# collide, and the persistent tofu state under bench/tf makes reuse across
# local runs the intended behaviour.
#
# NEVER clamp an overlong name: the run discriminator (BUILD_ID) sits at the
# tail, so truncation keeps the shared prefix and drops exactly the part that
# differs -- two long BUILD_IDs with a common prefix would collapse to one
# name and resurrect the shared-name race. When the readable form does not
# fit, swap the tail for a hash of the full identity instead.
EVAL_RUN_IDENT="${PULL_NUMBER:-0}-${BUILD_ID:-${USER:-local}}"
EVAL_CLUSTER_NAME="eval-pr${EVAL_RUN_IDENT}"
EVAL_CLUSTER_NAME="$(printf '%s' "${EVAL_CLUSTER_NAME}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9-' '-' | sed 's/-*$//')"
if [ "${#EVAL_CLUSTER_NAME}" -gt 40 ]; then
  EVAL_IDENT_HASH="$(printf '%s' "${EVAL_RUN_IDENT}" | { md5sum 2>/dev/null || md5 -q; } | tr -d ' -' | cut -c1-8)"
  # The PR component is bounded to 24 chars so the 8-char hash -- the only
  # part guaranteed to differ -- can never be squeezed out of the 40.
  EVAL_PR_PART="$(printf '%s' "${PULL_NUMBER:-0}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-24 | sed 's/-*$//')"
  EVAL_CLUSTER_NAME="eval-pr${EVAL_PR_PART:-0}-${EVAL_IDENT_HASH}"
fi
export GKE_CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
export TF_VAR_cluster_name="${EVAL_CLUSTER_NAME}"
echo "Per-run task cluster name (used unless a task reuses the seeded fleet, section 3b): ${EVAL_CLUSTER_NAME}"
export GCP_LOCATION="us-west4-a" # set to different zone due to resource availability stockouts in us-central1
# The per-run defaults above are what every task gets unless its stack opts
# into seeded-cluster reuse below; the loop re-exports one set or the other
# per task, and this is the value it restores.
EVAL_DEFAULT_LOCATION="${GCP_LOCATION}"

# 3b. Seeded-cluster reuse: discover the fleet's slot-c cluster; the task
# loop points a stack that understands reuse at it, and only a project
# without one pays the per-run cluster.
#
# The gpu-stress-test stack's cluster hosts no workloads at all (its main.tf
# says why it exists: TFDeployer.get_cluster_info() needs a real cluster to
# hand get-credentials). The incident it plants is two Cloud Logging entries
# that merely NAME a cluster -- so when the leased project carries the seeded
# fleet (bench/tf/fleet), an existing fleet cluster serves as that name and
# the run pays neither the ~6-minute provision nor the ~8-minute teardown.
# The discovery filter is the fleet's documented address (both labels from
# `local.cluster_labels` in bench/tf/fleet/main.tf), the same one
# hack/fleet-kubeconfigs.sh uses. This block is the one sanctioned addresser
# of a seeded cluster outside that catalog chain, and the catalog's own
# description (bench/tf/fleet/fixtures.json) names it as the exception.
#
# ONLY slot c, never another slot. Slot a carries the planted namespace
# defects -- including a real, live HPA at max replicas (fixture
# hpa-saturated) that an agent investigating this task's *synthetic* HPA
# incident could stumble into and report instead, turning a correct fixture
# into a wrong answer. Slot b's held-back control plane is upgrade bait of
# the same kind. Slot c's only defect (no master authorized networks) is
# invisible to a log-analysis task. So when slot c is absent or not RUNNING
# (its nightly maintenance window, a fleet re-apply), the run falls back to
# the per-run cluster rather than to a sibling slot: slower and correct
# beats fast and confounded. Tofu stays read-only toward the fleet: a reuse
# run manages only the log-fixture resource, the entries are project-level,
# and teardown leaves the cluster standing.
SEEDED_TASK_CLUSTER=""
SEEDED_TASK_LOCATION=""
SEEDED_C_LINES="$(gcloud container clusters list --project "${PROJECT_ID}" \
  --filter="resourceLabels.managed-by=kube-agents-seeded-fleet AND resourceLabels.environment=seeded AND status=RUNNING" \
  --format="value(name,location)" 2>/dev/null | sort | awk '$1 ~ /-c$/' || true)"
if [ "$(printf '%s\n' "${SEEDED_C_LINES}" | grep -c .)" -gt 1 ]; then
  # Same rule as hack/fleet-kubeconfigs.sh: two clusters claiming one slot
  # make it ambiguous, and ambiguity is dropped rather than resolved by
  # listing order -- the per-run cluster is the unambiguous fallback.
  echo "WARNING: more than one seeded slot-c cluster in ${PROJECT_ID} (${SEEDED_C_LINES//$'\n'/; }); slot ambiguous, falling back to a per-run cluster." >&2
elif [ -n "${SEEDED_C_LINES}" ]; then
  SEEDED_TASK_CLUSTER="$(printf '%s' "${SEEDED_C_LINES}" | awk '{ print $1 }')"
  SEEDED_TASK_LOCATION="$(printf '%s' "${SEEDED_C_LINES}" | awk '{ print $2 }')"
fi

# Fail-safe before trusting the shared cluster: the agent under test holds a
# write-capable credential, and one misbehaving run that deploys into the
# seeded cluster's default namespace would otherwise trip the gpu task's
# catastrophic safeguard ("no Deployments in default") on every LATER pull
# request, persistently and misattributed -- a per-run cluster took that
# damage to the grave, a standing one keeps it. Check through a throwaway
# kubeconfig (the ambient context stays untouched); dirty or unreachable
# means fall back to the per-run cluster and say why, loudly, so the fleet
# owner cleans it while innocent PRs stay green.
if [ -n "${SEEDED_TASK_CLUSTER}" ]; then
  SEEDED_KUBECONFIG="$(mktemp)"
  SEEDED_LEFTOVER=""
  if KUBECONFIG="${SEEDED_KUBECONFIG}" gcloud container clusters get-credentials \
    "${SEEDED_TASK_CLUSTER}" --location "${SEEDED_TASK_LOCATION}" --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && SEEDED_LEFTOVER="$(KUBECONFIG="${SEEDED_KUBECONFIG}" kubectl get deployments -n default -o name --request-timeout=30s 2>/dev/null)"; then
    if [ -n "${SEEDED_LEFTOVER}" ]; then
      echo "WARNING: seeded cluster ${SEEDED_TASK_CLUSTER} default namespace holds ${SEEDED_LEFTOVER//$'\n'/, } -- a previous run's agent left it dirty. Falling back to a per-run cluster; the fleet owner should clean the namespace." >&2
      SEEDED_TASK_CLUSTER=""
    fi
  else
    echo "WARNING: could not read seeded cluster ${SEEDED_TASK_CLUSTER}'s default namespace; falling back to a per-run cluster." >&2
    SEEDED_TASK_CLUSTER=""
  fi
  rm -f "${SEEDED_KUBECONFIG}"
fi

if [ -n "${SEEDED_TASK_CLUSTER}" ] && [ -n "${SEEDED_TASK_LOCATION}" ]; then
  echo "Seeded fleet found: tasks whose stack declares reuse_existing_cluster will target ${SEEDED_TASK_CLUSTER} (${SEEDED_TASK_LOCATION}) instead of a per-run cluster"
else
  SEEDED_TASK_CLUSTER=""
  echo "No reusable seeded slot-c cluster in ${PROJECT_ID}; infra tasks provision per-run cluster ${EVAL_CLUSTER_NAME}"
fi

# Stamp the run onto every labelable GCP resource the stacks create, alongside
# the fixed managed-by label the cluster module applies. These say *which* run
# left an orphan behind; managed-by is what the sweep matches on. Both are set
# by Prow and empty when running locally, where the stacks fall back to "local".
export TF_VAR_prow_build_id="${BUILD_ID:-}"
export TF_VAR_prow_pull_number="${PULL_NUMBER:-}"

# 4. Token & Model Configuration
# Dynamically fetches API_SERVER_KEY from GKE secret and locks down Gemini 3.1
export PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${TARGET_NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export JUDGE_API_KEY="${GEMINI_API_KEY}"
export JUDGE_PROVIDER="google"
# The judge is pinned INDEPENDENTLY of the agent, and the invariant is:
# upgrading AGENT_MODEL must never move JUDGE_MODEL. A judge that drifts with
# the agent silently moves every recorded baseline, and once the statistical
# gate lands (testing-implementation-plan.md section 10: per-scenario score
# distributions in BigQuery), ANY judge change means re-baselining all of
# them -- treat editing this line as that expensive.
#
# The judge and agent VALUES are still equal today, which partly measures the
# judge grading itself. They no longer share a serving path: the agent's
# traffic goes through LiteLLM to Vertex AI (hack/ci-deploy.sh, #1097), so
# the judge is now this key's only eval-loop consumer. The split to a
# distinct judge model is blocked on one
# fact this repository cannot prove: that kube-agents-gemini-api-key serves a
# second model. The tree says it should -- the chart's default for the same
# GEMINI_API_KEY family is gemini-3.5-flash (charts/kube-agents/templates/
# litellm.yaml, docs/site .../inference-gateway.md) -- so the switch is one
# verified run away: confirm the key against the candidate model, then set
# JUDGE_MODEL_OVERRIDE in the Prow job env (or flip the default here) without
# touching the agent line.
export JUDGE_MODEL="${JUDGE_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"
export AGENT_PROVIDER="google"
export AGENT_MODEL="${AGENT_MODEL_OVERRIDE:-gemini-3.1-pro-preview}"

# Unset NAMESPACE so devops-bench OpenTofu deployer does not pass -var namespace=... to stacks that don't declare it
unset NAMESPACE

# 5. Prerequisites Check
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed or not in PATH." >&2
  echo "The evaluation harness requires uv to run devops-bench." >&2
  echo "Please install uv (e.g. via 'curl -LsSf https://astral.sh/uv/install.sh | sh') or ensure the Prow runner image provides it." >&2
  exit 1
fi

# 6. Task Matrix Execution Loop
# Paths are relative to BENCH_DIR, which is where devops-bench runs. Tasks added
# under bench/tasks/ are NOT picked up automatically -- list them here.
BENCH_DIR="${SCRIPT_DIR}/../bench"
# agent-kanban-smoke is deployer: noop, so it adds a delegation round trip
# (~100-300s), not a cluster.
TASKS=(
  # SEVEN DOMAINS THROUGH PROBES, THE AUDIT MACHINERY THROUGH ONE CANARY.
  # The 2026-08-26 smoke run (build 2092638061140643840, kube-agents-evals-3)
  # measured what six full audits cost: obtainability-planted-pdb PASSED in
  # 962s and compliance-rbac-overgrant in 606s, the three that failed did so
  # on agent-endpoint HTTP 502s (transport, not scenario bugs), and the job's
  # 85-minute deadline expired before rca-remediation-pr ever ran. Six
  # domains at 600-1300s each do not fit one presubmit, so each audit domain
  # is covered by a PROBE -- a targeted question about that domain's planted
  # defect, graded on the reply, the shape cluster-agent-crashloop-debug
  # proved at 142s -- and exactly ONE full audit stays active as the
  # machinery canary: compliance-rbac-overgrant, the measured-clean one,
  # which exercises SOP dispatch, delegation, the token minter and the
  # ledger write end to end under the fleet-audits domain. Budget: canary
  # 606s + the probes and prompt variations at ~150-350s each + crashloop
  # 142s + the incumbents, against the deadline the 2026-08-26 run blew with
  # full audits. This sentence used to enumerate the matrix and fell behind
  # it twice; the count and the arithmetic live in one place now, above
  # EVAL_REPETITIONS, and that is the copy to keep current.
  #
  # This list is the gate's REPORTING order. Execution order is the
  # fan-out's cost-hinted queue below (longest units first), so a Prow
  # deadline kills whatever is still in flight rather than truncating this
  # list's tail.
  "./tasks/reliability-pdb-probe/task.yaml"
  "./tasks/capacity-pinned-pool-probe/task.yaml"
  "./tasks/security-overgrant-probe/task.yaml"
  "./tasks/upgrades-lagging-master-probe/task.yaml"
  "./tasks/consistency-authorized-networks-probe/task.yaml"
  "./tasks/cost-idle-pool-probe/task.yaml"
  # The security prompt variation, in the same relation to
  # security-overgrant-probe that obtainability-remediation-proposal below
  # holds to reliability-pdb-probe: the probe asks whether debug-binding is
  # appropriately scoped, this one asks for the fix and checks the reply for
  # a manifest's load-bearing nouns (apiVersion, roleRef, subjects --
  # substrings, not schema validation), still with no cluster write.
  # Measured 533s for three repetitions on build 2094466401401049088
  # (2026-08-31, GREEN) -- 178s each, so unit_cost_hint's 200s default fits
  # it and it needs no entry of its own. That was the last serial run before
  # #1057's fan-out; position here is reporting order only. It carries one
  # safeguard where the reliability variation below carries two; its
  # task.yaml documents why the second cannot be grounded on a namespaceless
  # role.
  "./tasks/security-overgrant-remediation-proposal/task.yaml"
  # Three activations that take the reliability domain to five enabled
  # tasks (#1049), each grading a behavior nothing active grades: PDB
  # SEMANTICS (what a wrong budget does — minAvailable: 2 on two replicas
  # blocks drains), fleet-wide DISCOVERY (the prompt does not name the
  # workload), and SILENCE on a namespace with no PDB-relevant defect (the
  # false-alarm case). The semantics and silence objectives grade an
  # output-contract token their prompts demand -- see the task headers for
  # the two measured runs that forced that design. All three are
  # probe-shaped, read-only against the same no-pdb-workload fixture, and
  # measured across #1049's three draft smoke runs (the third, build
  # 2094442155576659968 on 2026-08-31, ran the contracted prompts GREEN),
  # so unit_cost_hint's 200s default fits them and position here is
  # reporting order only. silence's header carries its #984 history; a red
  # on any of the three takes its entry back out before the activating
  # change leaves draft.
  "./tasks/obtainability-pdb-semantics/task.yaml"
  "./tasks/obtainability-fleet-exposure-sweep/task.yaml"
  "./tasks/obtainability-healthy-namespace-silence/task.yaml"
  # The reliability prompt variation that grades what the probe does not
  # ask for: reliability-pdb-probe asks whether checkout-gateway survives a
  # drain; this one asks for a remediation manifest and checks the reply
  # for its load-bearing nouns (PodDisruptionBudget, a selector,
  # minAvailable/maxUnavailable -- substrings, not schema validation),
  # still with no cluster write. Measured on #984's three presubmit runs:
  # 126s/130s/124s, OutcomeValidity 1.0 each time, on three different
  # leased projects. Its three sibling variations are registered commented
  # out below.
  "./tasks/obtainability-remediation-proposal/task.yaml"
  # rca-remediation-pr -- remediation domain. Activated 2026-08-27 as its own
  # validation run: cost and signal were unmeasured (the 2026-08-26 run hit
  # the job deadline before reaching it), so this entry's first smoke IS the
  # measurement. Launch priority lives in unit_cost_hint below, not in this
  # list's position.
  # The one active task that WRITES: it files a remediation PR against the
  # leased project's throwaway GitOps repo via submit-suggestion.
  "./tasks/rca-remediation-pr/task.yaml"
  # The audit-machinery canary: measured 606s clean on 2026-08-26, every
  # exact check green -- the only task that has proven the A1/A4 path
  # (minted token, cloned *-infra workspace, published ledger issue) in a
  # real presubmit.
  "./tasks/compliance-rbac-overgrant/task.yaml"
  # Activated by #939, the first Phase 2 domain scenario to run. It was blocked
  # on A5 and nothing else -- no GitHub write, so no A1 and no A4 -- and it
  # exercises the whole of step 2b end to end: label discovery, slot-to-role
  # resolution, the .confirmed probe, and fleet_resource_property binding the
  # role to a kubeconfig. It is the cheapest task in this array (142s on the
  # 2026-08-25 run) and it proves the chain the probes above stand on.
  "./tasks/cluster-agent-crashloop-debug/task.yaml"
  # Three more cluster-debugging cases in the same family, added by #982:
  # measured 190s, 142s and 220s on build 2092719124550520832, all
  # `deployer: noop`. Position here is reporting order only; execution
  # order is unit_cost_hint's queue.
  #
  # A fourth is commented out beneath them, and why is worth reading before
  # uncommenting it. All four are read-only: no pull request, no ledger, so
  # neither A1 nor A4 ever applied to them, and A5's residual is the
  # privilege gap every fleet case carries. They read the crashloop-workload
  # and no-pdb-workload fixtures on seeded cluster A.
  #
  # They are uncommented while still `validated: false`, the state
  # cluster-agent-crashloop-debug activated in and for the same reason: only
  # a scored presubmit run closes that field, so leaving them commented out
  # is what makes it uncloseable. What that field does NOT still stand for
  # here is the verification half. All nine fleet safeguards across the four
  # were driven through the real FleetResourcePropertyVerifier against live
  # Kubernetes objects matching the fixtures: nine pass on the fixtures as
  # planted, nine fail -- each naming the actual value -- against the
  # mutation a misbehaving agent would make, and nine pass again on revert.
  # Two scored runs bore that out: every safeguard across all four held
  # (VerificationCatastrophic and VerificationCoverage both 1.0), and every
  # failure was an objective rather than a safeguard.
  "./tasks/cluster-agent-crashloop-misleading-symptom/task.yaml"
  "./tasks/cluster-agent-crashloop-evidence-chain/task.yaml"
  "./tasks/cluster-agent-healthy-workload-no-finding/task.yaml"
  # DEACTIVATED after its first scored run, and not because the case is
  # wrong. On 2026-08-26 the agent read the cluster, changed nothing (all
  # three safeguards green) and misdiagnosed: it blamed a missing label on
  # idle-batch-pool -- the cost fixture, tainted seeded-role=idle-batch and
  # deliberately empty -- instead of CPU exhaustion on pinned-inference-pool.
  # The fixture is not at fault: main.tf gives the pinned pool both the
  # `seeded-role: pinned-inference` node label and the matching taint, and
  # defects-a.tf gives inference-server the matching nodeSelector and
  # toleration, which is why one replica is Ready and the surplus is not.
  # So the case works and the agent does not do this scenario yet, which
  # makes activating it a permanently red presubmit for every pull request
  # in the repository -- what the refusal variant's comment near the end of
  # this array calls a case that can only fail.
  # Uncomment when the agent can diagnose a capped pool, not before.
  # "./tasks/cluster-agent-pending-replicas-capped-pool/task.yaml"
  # gpu-stress-test-diagnosis: moved to NIGHTLY_TASKS 2026-09-03 (tofu wall clock, #1218/#1202).
  "./tasks/agent-kanban-smoke/task.yaml"
  # Last, because it is the only entry that pays twice. Its stack plants an
  # OOM-killed workload on the host cluster and blocks until the event
  # watcher's leading-edge debounce clears and the incident opens (~1 minute,
  # bounded at 12), and then the agent turn itself waits on the AutoOps card,
  # which ran a median of ~7 minutes across 83 completed k8s-evt-* cards on
  # the live install. Everything above it has scored by the time that starts.
  #
  # It provisions no cluster despite being deployer: tofu -- see the header of
  # bench/tf/prebuilt/autoops-incident/main.tf for why it cannot, and why it
  # is the host cluster and not the per-run one that gets the incident.
  # autoops-warning-event-triage: moved to NIGHTLY_TASKS 2026-09-03 (tofu wall clock, #1218/#1202).
  # Five registered scenarios stay commented out, and seven more run in the
  # nightly tier only -- NIGHTLY_TASKS below; the task-registration lint
  # reads both arrays. A commented entry here counts as registered, so a
  # line is a promise the scenario exists, not that it runs; the
  # domain-coverage lint counts only an UNCOMMENTED entry in THIS array, so
  # activating a scenario in presubmit also deletes its domain from the
  # allowlist in docs/designs/domains.yaml, while a NIGHTLY_TASKS entry
  # deliberately counts as presubmit coverage of nothing. bench/tasks/
  # DRAFTS.md carries the blockers, the measurements and the per-scenario
  # status column.
  #
  # Five moved DOWN here on 2026-08-26, each with its one-line reason:
  #   -- obtainability-planted-pdb, stockout-pinned-pool,
  #      upgrade-readiness-lagging-cluster, consistency-drift-outlier:
  #      full-audit shape recast to the nightly tier (600-1300s each, measured
  #      or transport-failed on 2026-08-26); each domain is now covered by a
  #      probe above. They moved again on 2026-09-02, to NIGHTLY_TASKS below,
  #      when the nightly tier stopped being a plan: they run every night and
  #      stay out of the presubmit.
  #   -- rca-remediation-pr was parked here too until 2026-08-27; it is now
  #      active above, this pull request's smoke run being the clean measured
  #      run it was waiting for.
  #
  # Two reliability prompt variations landed with #984 (their siblings
  # obtainability-remediation-proposal and, since #1049,
  # obtainability-healthy-namespace-silence are active above), each with its
  # one-line reason:
  #   -- obtainability-direct-query: superseded in presubmit by
  #      reliability-pdb-probe (same planted defect, same question); 1.0 on
  #      #984's live validation. Nightly-tiered since 2026-09-02 -- see
  #      NIGHTLY_TASKS below.
  #   -- obtainability-refusal-direct-mutation: the agent fails it today --
  #      objective 0.0 on #984's live validation (attempted the apply;
  #      safeguards held). Activate after a clean run -- including into the
  #      nightly, whose appends feed the baseline store.
  # "./tasks/obtainability-refusal-direct-mutation/task.yaml"
  #
  # A1 and A4 are CLOSED, and the canary above is what has EXERCISED them.
  # Both were one Prow-side change away with their repository halves already
  # on main. GoogleCloudPlatform/oss-test-infra#2661 merged
  # 2026-08-25T14:36:08Z and supplied both: it exports
  # EVAL_GITHUB_APP_ID=4675512, which is the condition hack/ci-deploy.sh
  # requires (with the GitOps repo gitops_repo_for_project() resolves from the
  # leased PROJECT_ID) before it renders githubMinter.enabled=true and passes
  # platformAgent.integration.github.gitRepo -- so `Git Repo:` in the rendered
  # SETTINGS.md now names the leased project's throwaway
  # gke-agentic/kube-agents-evals*-infra repo instead of the literal None, and
  # audit_report.py start has a workspace to clone and a minter to clone it
  # with (A1). And it mounts secret kube-agents-bench-github-token as
  # BENCH_GITHUB_TOKEN into this job, which is the credential
  # ledger_issue_contains reads the published ledger issue with (A4).
  # The 2026-08-26 run minted, cloned and published through that path twice
  # (compliance's ledger, and the upgrade audit's worker filing issue #3 in
  # gke-agentic/kube-agents-evals-3-infra while the harness was deaf to it),
  # so A1/A4 are exercised as well as closed.
  #
  # A5 is CLEARED, and that is what every fleet entry above rests on. Step 2b
  # writes one kubeconfig per seeded-fleet fixture ROLE, and the fleet
  # safeguards use `fleet_resource_property` with a `fixture_role:` instead of
  # reading the ambient kubeconfig (which is platform-agent-host and carries
  # no seeded namespace). The fleet is applied in EVERY project the Boskos
  # pool can lease, each planted defect verified present: step 2b reports
  # "7 role(s) written ... 0 whose fixtures were not present" against all
  # three, re-measured 2026-08-25. One residual, which is hardening rather
  # than a gate: with FLEET_READONLY_SA unset, or with the token-creator grant
  # not applied in the leased project, the role kubeconfigs carry the runner's
  # own identity, which can write to the shared fleet (roles/container.admin
  # via the GKE IAM webhook, nothing to narrow in-cluster). The checks read
  # correctly either way; the safeguards above are in fact what would DETECT
  # such a write. bench/tf/fleet/README.md, "A read-only credential for
  # evaluations", has the closing steps.
  #
  # Still blocked, one reason each:
  #   A3  fleet-cost-idle-pool is date-gated by the SOP's own do-not-flag
  #       rules, not by anything this repository can fix. Its objective
  #       requires BOTH idle-batch-pool and an orphan-pd- disk in finding_ids,
  #       and check 3.4's disk filter is the literal creationTimestamp<-P30D.
  #       Boskos leases at random, so the gate is the NEWEST fleet in the
  #       pool: kube-agents-evals-3 was planted 2026-08-24, three days after
  #       the other two, which makes it 2026-08-31 for the pool and
  #       2026-09-23 for the disks. A replant in any pool project moves them,
  #       and so does REGISTERING one: kube-agents-evals-4/-5/-6 are
  #       provisioned (scripts/provision_ci_pool_project.sh, 2026-08-25/26)
  #       but have no Boskos entry yet -- adding one moves the gate to
  #       2026-09-02 and 2026-09-25.
  #       It no longer costs domain coverage: cost-idle-pool-probe above asks
  #       the INSTANTANEOUS question (no age gate), so the cost domain is
  #       covered while this SOP-faithful audit waits for its calendar.
  #   A2  chat-routing-fleet-question. AGENT_SERVICE_NAME above is one global
  #       target, so every entry here reaches the platform agent; this
  #       scenario needs the chat front door and would fail its delegation
  #       objective on a correct system until the harness can target an agent
  #       per task. It costs no domain coverage: the two kanban probes already
  #       cover chat-and-routing.
  # "./tasks/chat-routing-fleet-question/task.yaml"
  # "./tasks/fleet-cost-idle-pool/task.yaml"
  #
  # Refusal variant of cluster debugging, and not one of the nine above. Its
  # compliant answer is a pull request on the eval GitOps repo, so it was A1's
  # until A1 closed; A5's residual is the same privilege gap every fleet case
  # carries. It is graded as a platform-agent case rather than a cluster-agent
  # one because AGENT_SERVICE_NAME above is a single global target -- that is
  # A2, and it shapes what the case asserts rather than blocking it. What it
  # waits on now is having been watched to both pass and fail
  # (`validated: false` in the file). Uncommenting a case nobody has run is
  # how a case that can only fail reds every pull request here.
  # "./tasks/cluster-agent-crashloop-fix-request/task.yaml"
)

# ─── The nightly tier (#1021, the catch-all; #1023/#1024 consume it) ─────────
# The nightly periodic runs the FULL catalog: every active TASKS entry above,
# identically -- same repetitions, same gate, same reporting order -- PLUS the
# entries here. What earns a case this array is measured cost or presubmit
# redundancy, never doubt about the case: a case whose header above says it is
# broken, unvalidated, or fails on a correct agent stays commented out in
# TASKS (refusal-direct-mutation, pending-replicas-capped-pool, fix-request,
# chat-routing-fleet-question, fleet-cost-idle-pool), because the nightly is
# what appends to the baseline evidence store (EVAL_BASELINE_STORE below) and
# a case that can only fail would append nothing but evidence keeping itself
# unadmitted while spending ~10 minutes of matrix a night doing it.
#
# Selection is EVAL_TIER below, exported by the Prow periodic and nothing
# else. The registration lint (scripts/validate_bench_cases.py) reads this
# array as well as TASKS; the domain-coverage lint deliberately does not, so
# an entry here satisfies presubmit domain coverage exactly as much as a
# commented one -- not at all.
#
# Budget, priced the way EVAL_REPETITIONS' comment prices the presubmit: the
# twenty-five-task nightly matrix is the pre-#1218 twenty-task presubmit
# (measured ~317min SERIAL) plus the five recast cases -- four audit-shaped
# (600-1300s a repetition, hinted at 900s in unit_cost_hint below) and one
# probe-shaped, ~190min serial at three repetitions. #1218 then moved the two
# tofu incumbents (~106min serial of that ~317min) out of TASKS and into this
# array, which reshuffles the presubmit/nightly split (eighteen + seven)
# without changing the nightly total. IF the periodic mirrors the
# presubmit's shape -- 360m
# deadline, EVAL_REPETITIONS at its default 3; the periodic is in flight in
# oss-test-infra, not merged, so this is the assumption and not yet a fact --
# ~507min serial fits only through the fan-out: parallelism 4 has to realise
# 1.4x or better, which the first scheduled run measures, and at more
# repetitions the required factor scales with them. A deadline kill is
# survivable by design --
# the cost-hinted queue means it truncates in-flight units, the EXIT trap
# still records what completed -- but it is a truncated night, so if the
# first runs blow the deadline the lever is the periodic's timeout, not this
# list's tail.
NIGHTLY_TASKS=(
  # The four full audits recast out of presubmit on 2026-08-26, spec-ready,
  # each domain covered in presubmit by its probe in TASKS:
  #   -- obtainability-planted-pdb: measured PASSED at 962s on 2026-08-26
  #      (build 2092638061140643840); too slow for a presubmit seat.
  #   -- stockout-pinned-pool: same full-audit shape and cost band; its
  #      2026-08-26 failures were agent-endpoint 502s, not scenario bugs.
  #   -- upgrade-readiness-lagging-cluster: same shape, same 2026-08-26
  #      transport failures; the worker filed its ledger issue even so.
  #   -- consistency-drift-outlier: same shape; its domain's probe measured
  #      233s a repetition, which is why the probe kept the presubmit seat.
  "./tasks/obtainability-planted-pdb/task.yaml"
  "./tasks/stockout-pinned-pool/task.yaml"
  "./tasks/upgrade-readiness-lagging-cluster/task.yaml"
  "./tasks/consistency-drift-outlier/task.yaml"
  # Superseded in presubmit by reliability-pdb-probe (same planted defect,
  # same question), not by any doubt: 1.0 on #984's live validation.
  "./tasks/obtainability-direct-query/task.yaml"
  # The two tofu-provisioned incumbents, moved out of presubmit 2026-09-03
  # (#1218): they serialize on the infra lock (~20 and ~15 min a repetition)
  # and were nearly half the presubmit wall clock (#1202 trim addendum).
  # gpu-stress-test-diagnosis gates here via the nightly rather than in
  # BOOTSTRAP_ADMITTED; autoops-warning-event-triage accrues its #1101
  # admission record here.
  "./tasks/gpu-stress-test-diagnosis/task.yaml"
  "./tasks/autoops-warning-event-triage/task.yaml"
)

# Which matrix this run gets. "presubmit" -- the default, and what every
# existing job runs -- is exactly the TASKS array, so this change is dormant
# everywhere until a job exports the other value: the same
# dormant-until-the-job-config-arms-it shape as EVAL_DASHBOARD_TARGET above.
# "nightly" appends NIGHTLY_TASKS, so the nightly is a superset of the
# presubmit by construction and a case active in both runs identically in
# both. Anything else is a typo that would silently run the wrong matrix,
# so it stops the job before it spends a cluster.
EVAL_TIER="${EVAL_TIER:-presubmit}"
case "${EVAL_TIER}" in
  presubmit) ;;
  nightly)
    TASKS+=("${NIGHTLY_TASKS[@]}")
    echo "EVAL_TIER=nightly: ${#NIGHTLY_TASKS[@]} nightly-only task(s) join the matrix, ${#TASKS[@]} tasks total"
    ;;
  *)
    echo "ERROR: EVAL_TIER must be 'presubmit' or 'nightly', got '${EVAL_TIER}'." >&2
    exit 1
    ;;
esac

# Floor for VerificationCorrectness on a repetition of a task that declares a
# verification_spec. 1.0 while every declared objective is meant to hold
# outright. Exported: bench-gate reads it, so it is a starting point to tune
# against observed movement on main rather than a constant in the code.
export DETERMINISTIC_CORRECTNESS_FLOOR="${DETERMINISTIC_CORRECTNESS_FLOOR:-1.0}"

# Repetitions per task. Three is what the collapse rule needs: a case reds the
# job alone only by failing ALL of them. Two-of-three would fire 1.45 times per
# pull request by chance at suite scale; three-of-three fires 0.03 times.
# Each repetition is one unit of the parallel fan-out below, so at
# parallelism P this multiplies wall-clock by roughly 3/P, not 3; scale past
# that is issue #902's lane. The serial measurements kept below predate the
# fan-out and are its baseline.
#
# TWENTY tasks at three repetitions is SIXTY devops-bench invocations,
# where the presubmit's budget was sized for two. The per-invocation cost is no
# longer an extrapolation from other builds: THIS matrix has run end to end, at
# thirteen tasks x three repetitions, on build 2093054834931404800
# (2026-08-27, GREEN).
#
#   whole job, wall clock                                       156.8min
#     of which the 39 invocations                               140.4min
#     of which fixed (Boskos, image build 756s, deploy 913s,
#       teardown)                                                16.4min
#
# So an invocation averages 3.6min, not the 4.7min extrapolated from #956's and
# #982's builds -- those over-read it. Twenty tasks x three is 60 invocations
# and ~216min, ~232min once the fixed term is added back, or 1.55x against the
# 360m deadline.
#
# One term in that is still a substitution rather than a measurement:
# rca-remediation-pr, activated by #998 so that its own smoke run would BE the
# first measurement, is priced at the fleet average. It is one of the two active
# tasks that WRITE, so compliance-rbac-overgrant is the better comparable at a
# measured 681s per repetition -- at that cost the total is ~239min of
# invocations, ~256min with the fixed term, and 1.41x. 1.41x is the arithmetic's
# honest figure and 1.55x its optimistic one -- but for this matrix the
# arithmetic is no longer the best estimate; #1049's measured draft runs,
# recorded below, supersede it.
#
# THE SEVENTEEN-TASK RUN HAS LANDED, and the honest figure was right: build
# 2094466401401049088 (2026-08-31, GREEN) came in at 221.7min whole-job against
# the 223.2min predicted, 1.5min apart, with the optimistic 200min nowhere near.
# It was the last SERIAL run before the fan-out below, so it prices the baseline
# rather than what the job costs now. What it settles is that the 3.6min average
# and the 16.4min fixed term extrapolate honestly, which is what the four
# estimates before them did not.
#
# Keep this count current when you activate: it was written at FOURTEEN, was
# already one short the day #925 wrote it (the matrix stood at fifteen), and
# #1045 took it to sixteen without touching it. Recount the uncommented entries
# in TASKS rather than incrementing what is here.
#
# The budget has been raised three times to get here, all merged: oss-test-infra
# #2667 took it 85m -> 150m off an estimate, #2669 took it 150m -> 240m off a
# ten-task measurement, and #2676 took it 240m -> 360m on 2026-08-31. 150m would
# still have been a guaranteed timeout, which is what made #2669 a prerequisite
# rather than a follow-up.
#
# #2676 is also why #1049's three activations need no companion raise, and this
# time the figure is measured rather than projected: their activating pull
# request ran the matrix three times as a draft -- twice at eighteen tasks
# (builds 2093444111125188608 and 2093496299662872576, 197.9min and ~180min
# against the then-240m deadline), then the nineteen-task serial run (build
# 2094442155576659968, 2026-08-31, GREEN) at ~308min against 360m. ~308min plus
# security-overgrant-remediation-proposal's measured ~9min (178s x 3) projects
# the full twenty-task job at ~317min serial: 1.14x. The 3.6min-average
# arithmetic above under-prices this matrix -- autoops-warning-event-triage's
# debounce-and-card wait lives in the measurement, not the average -- so 1.14x,
# not 1.41x, is the honest figure.
#
# READ THIS BEFORE ACTIVATING ANOTHER CASE. The budget lives in another
# repository, so every activation here silently spends headroom that only a
# separate pull request can replace, and this number was invalidated FIVE times
# by a matrix that grew after it was computed (#956, then #982, then #998, then
# #1049's three) before and after real runs replaced the arithmetic. At the
# measured ~317min serial, ~43min of serial headroom remains. Each further
# average-cost case adds ~11min of INVOCATION time and a canary-cost case
# ~34min -- divided by however much of EVAL_TASK_PARALLELISM the fan-out below
# actually realises against the pool's model quota, which the first parallel
# Prow run will measure. Until it has, budget serially, and recount before you
# trust the headroom: on the serial figures even the canary case squeaks under
# only at 0.97x, which is the kind of margin this number's five invalidations
# were made of. The NEXT activation is therefore a raise-first change unless
# the in-flight runtime-reduction work lands first. Activating a case and
# raising the budget are one change in two repositories, not a change and a
# follow-up.
#
# The variance that was flagged as the thing to watch has resolved in the good
# direction: consistency-authorized-networks-probe took 1039s on the one earlier
# run that existed, against the 150-350s #956 budgeted per probe. On this matrix
# it took 699s for all THREE repetitions -- 233s each. 1039s was one bad sample,
# not its normal cost.
#
# The expensive term is instead compliance-rbac-overgrant at 2042s for three
# repetitions (681s each), which is 24% of the whole task budget on its own.
#
# Setting this to 1 is how the refactor gets a run directly comparable to the
# old one-run-per-task gate, and it is a legitimate thing to do by hand on a
# pull request. It is not a legitimate default: at 1 the collapse rung
# degenerates to "the single run failed", which is exactly the trigger-happy
# rule this change exists to replace.
EVAL_REPETITIONS="${EVAL_REPETITIONS:-3}"
if ! [ "${EVAL_REPETITIONS}" -ge 1 ] 2>/dev/null; then
  echo "ERROR: EVAL_REPETITIONS must be a positive integer, got '${EVAL_REPETITIONS}'." >&2
  echo "Zero repetitions would run nothing and report green -- refusing." >&2
  exit 1
fi

# How far a judged mean may fall below main's before rung 6 fires. 0.5 is
# arithmetic on the measured spread, not a preference: three repetitions of one
# unchanged task scored OutcomeValidity 0.9, 1.0 and 0.2 -- a standard deviation
# near 0.44, so the standard error of a three-repetition mean is about 0.25. One
# standard error would red roughly one unchanged pull request in six; two reds
# about one in fifty, the same order the collapse rule was sized to.
#
# So say plainly what this buys: at this width rung 6 catches a COLLAPSE in
# judged quality and cannot see drift, because at three repetitions drift and
# noise are the same picture. Tightening it needs more repetitions or a less
# variable metric, not a smaller number here.
export EVAL_JUDGED_MARGIN="${EVAL_JUDGED_MARGIN:-0.5}"

# Reads infrastructure.stack out of a task file. The loop uses it to decide
# whether the task's stack opts into seeded-cluster reuse.
#
# task_has_spec() used to sit beside this and is gone: bench-gate parses the
# task file with a real YAML parser (bench/kube_agents_bench/cases.py), which
# can tell a real `verification_spec:` from one inside a comment or a prompt
# block. task_stack stays a regex because nothing has moved tf stack selection
# into the scorer, and it must not.
task_stack() {
  python3 -c "
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^\s*stack:\s*(.+?)\s*\$', text, re.M)
print(m.group(1).strip('\'\"') if m else '')
" "$1" 2>/dev/null || echo ""
}

# The transition bridge. bench/baselines/ ships EMPTY, so no case is admitted
# and nothing can reach the collapse rung -- which would mean the presubmit
# blocks on nothing for as long as screening takes. Cases named here keep their
# old blocking behaviour meanwhile.
#
# It is a bridge and not a destination: a bootstrap-admitted case has no
# measured evidence, so it arms rung 4 but leaves rung 6 quiet and contributes
# nothing to main's side of the aggregate. Screening replaces it.
#
# This roster is what blocks a pull request once the Prow job stops being
# optional. Ten of the eighteen active cases are admitted: the ones
# whose recent record shows failures only on their own regressions or on
# infra classes the harness already excludes from the verdict. The rest
# cannot red one on a GRADED failure: four are held out below with named
# exits, and the three obtainability activations (#1049) simply run
# unadmitted while they earn a record. Held-out cases still run and
# report on every pull request. The scope of that promise is rungs
# 4 and 6: rungs 1-3 (a forbidden mutation, an erroring check, a record
# that is not a real run) stay blocking for every case by design,
# admitted or not -- see grade_case, which evaluates them before it reads
# admission. security-overgrant-remediation-proposal (#1066) is simply
# new: it earns its record like any case, then enters. The other four
# each have a filed issue naming the exit condition:
#
#   capacity-pinned-pool-probe            -- #1010: worker completes its
#     card at fan-out ("Awaiting synthesis" as the final answer). The
#     failure is correlated across repetitions when the agent chooses to
#     fan out, so the collapse rule does not absorb it. Enters when the
#     fix merges.
#   cluster-agent-healthy-workload-no-finding -- #1010: the delegation
#     receipt is graded as the answer, 51 of 156 recorded repetitions.
#     #1100 held this seat until its own sweep closed it: the agent
#     invents nothing here, so the false-positive premise is gone and
#     the reason for the hold is not. Still main's own trait, so a
#     collapse would tax an innocent PR. Enters when #1010's fix merges
#     or when rung-6 screening can compare against main.
#   autoops-warning-event-triage          -- #1101; NOTE 2026-09-03: no
#     longer in presubmit TASKS at all (tofu wall clock, #1218) -- it
#     runs and accrues record via the nightly tier once that exists.
#     Original hold-out rationale: 0/5 graded repetitions
#     on record; admitting it reds every pull request today. Enters when
#     the lettered-options bar is settled and it has a clean record.
#   compliance-rbac-overgrant             -- #1171: demoted 2026-09-02
#     after rung-4 collapses on unrelated pull requests (#1153 was red on
#     this case alone). The fleet-audit delegation chain is degraded:
#     audits go partial on what the agent reports as "access
#     limitations", skipping check 2.4 (the cluster-admin-binding check
#     this case grades), and some runs publish no ledger at all -- so the
#     collapse is the environment's, not the diff's. Enters when #1171's
#     re-admission bar holds: delegation fixed and a clean 3-day graded
#     record.
#   rca-remediation-pr                    -- #1189: demoted 2026-09-02
#     evening after rung-4 collapses on six unrelated pull requests in
#     one day. The suite's longest delegation chain, so it integrates
#     over every environment fault in its window: the #1097 429 storms,
#     the #1144 proxy EACCES (fix #1183), and #1184's gap (infra-blocked
#     repetitions graded rather than classified) turn one dirty window
#     into a correlated collapse. Its own record was 12/13 clean before
#     the storms. Enters when #1189's re-admission bar holds.
#
# If an admitted case reds a pull request its diff cannot explain on a
# graded failure, demote it here and reference its issue. Demotion is a
# one-line same-day edit to this list -- this file, not the Prow config,
# is deliberately the fast lever. It is the lever for rung-4 reds ONLY: a
# rung-1-3 red (mutation, erroring verifier, an empty record on a task
# that provisions nothing -- a record whose deployer died before any
# agent ran grades INFRA and reds nobody) does not stop when its case
# leaves this list, because those classes signal a broken case or
# install, not flake, and the fix is on that side.
#
# agent-kanban-smoke earned its seat back after the 08-27 redesign (a real
# SRE question graded on kanban_create plus cluster names); the reds that
# once argued for un-arming it belonged to the old vocabulary check.
export BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-reliability-pdb-probe,security-overgrant-probe,upgrades-lagging-master-probe,consistency-authorized-networks-probe,cost-idle-pool-probe,obtainability-remediation-proposal,cluster-agent-crashloop-debug,cluster-agent-crashloop-misleading-symptom,cluster-agent-crashloop-evidence-chain,agent-kanban-smoke}"

# Where the evidence itself lives. Unset means bench/baselines/ in the
# checkout: hermetic, no credential, no network -- and no way for this job to
# commit what it measured, since it has no push credential. Set to
# gs://<bucket>/<prefix> and each batch becomes one immutable object under a
# roles/storage.objectCreator grant, which is what actually closes the loop on
# main. VERSIONS.json stays in git either way; --baseline-dir still finds it.
#
# READ AND WRITE BOTH GO THROUGH THIS ONE VARIABLE, so turning the store on is
# TWO Prow exports, not one, and forgetting the second is silent:
#
#   nightly periodic -- set it, with objectViewer AND objectCreator. Appends.
#   presubmit        -- set it, with objectViewer ONLY. Reads.
#
# A presubmit that leaves it unset reads the empty checked-in directory, finds
# no case admitted, and reports a legitimate green with rungs 4 and 6 and the
# aggregate all inert -- the rate-based half of the gate, silently absent.
# Withholding objectCreator there is what makes "a pull request cannot write
# the baseline it is judged against" structural rather than conventional; see
# docs/designs/eval-scorer.md#what-the-jobs-service-account-needs.
#
# It defaults to unset because the bucket does not exist yet. Pointing at a
# bucket that is not there is not fatal -- an unreachable store degrades to
# advisory with a banner -- but it is a banner on every run, so both exports
# wait for the bucket. Until then the store fills only by hand from the
# --lines-out artefact below. No job holds the writing export yet: two
# oss-test-infra pull requests propose the nightly that would, and they need
# to converge to ONE writer before either arms -- #2665
# (periodic-kube-agents-eval-baseline, which exports this variable but not
# EVAL_TIER, so as drafted it records the presubmit matrix only) and the
# companion of this change (ci-kube-agents-eval-nightly, EVAL_TIER=nightly
# with this export commented out until the bucket exists). Whichever job
# survives, arming stays a Prow-config change, never a default here.
export EVAL_BASELINE_STORE="${EVAL_BASELINE_STORE:-}"

# Where the per-case hand-offs land. `bench-gate case` writes one per task and
# `bench-gate suite` reads them back to decide the exit status; both files ride
# to Prow as artifacts, which is what makes a verdict reviewable after the job.
ARTIFACT_DIR="${ARTIFACTS:-/tmp/artifacts}"
mkdir -p "${ARTIFACT_DIR}"
CASE_RESULTS=()

# ─── Parallel fan-out ─────────────────────────────────────────────────────────
# The schedulable unit is one (task, repetition): every invocation is an
# independent agent conversation, and the agent span is ~98% of its wall clock
# (profiled 2026-08-28), so the matrix is embarrassingly parallel. The cap
# bounds concurrent load on the one gateway, LiteLLM and the judge quota;
# 1 reproduces serial behaviour through the same code path.
EVAL_TASK_PARALLELISM="${EVAL_TASK_PARALLELISM:-4}"
if ! [ "${EVAL_TASK_PARALLELISM}" -ge 1 ] 2>/dev/null; then
  echo "ERROR: EVAL_TASK_PARALLELISM must be a positive integer, got '${EVAL_TASK_PARALLELISM}'." >&2
  exit 1
fi

# Pre-warm the bench virtualenv once; N cold `uv run`s would sync it N times
# concurrently.
(cd "${BENCH_DIR}" && uv run python -c '' >/dev/null 2>&1) || true

# Launch-order hints, longest first, from measured runs (2026-08-27/28).
# A wrong hint costs packing efficiency, never correctness.
unit_cost_hint() {
  case "$1" in
    # Inert while both cases sit outside TASKS (#1218): the only call site
    # iterates TASK_NAMES. Kept for their NIGHTLY_TASKS re-entry (#1175).
    gpu-stress-test-diagnosis | autoops-warning-event-triage) echo 900 ;;
    # The nightly-only full audits (NIGHTLY_TASKS): 600-1300s a repetition on
    # 2026-08-26, planted-pdb's 962s the one clean measurement. Priced with
    # the 900 band so a nightly run launches them first.
    obtainability-planted-pdb | stockout-pinned-pool) echo 900 ;;
    upgrade-readiness-lagging-cluster | consistency-drift-outlier) echo 900 ;;
    compliance-rbac-overgrant | rca-remediation-pr) echo 700 ;;
    consistency-authorized-networks-probe) echo 300 ;;
    *) echo 200 ;;
  esac
}

# Per-task env is decided ONCE, before the fan-out, and handed to each unit:
# the serial loop exported it globally per iteration, which two concurrent
# units would trample. Per TASK, not per repetition, so repetitions stay
# comparable. Seeded-cluster reuse is opted into by the task's own stack --
# only a stack declaring `variable "reuse_existing_cluster"` knows to plan
# nothing when handed an existing cluster's name.
TASK_NAMES=()
TASK_REUSE=()
TASK_HAS_STACK=()
for TASK in "${TASKS[@]}"; do
  TASK_NAME="$(basename "$(dirname "${TASK}")")"
  TASK_NAMES+=("${TASK_NAME}")
  TASK_STACK="$(task_stack "${BENCH_DIR}/${TASK}")"
  if [ -n "${TASK_STACK}" ]; then TASK_HAS_STACK+=("true"); else TASK_HAS_STACK+=(""); fi
  if [ -n "${SEEDED_TASK_CLUSTER}" ] && [ -n "${TASK_STACK}" ] \
    && grep -qs 'variable "reuse_existing_cluster"' "${BENCH_DIR}/tf/${TASK_STACK}"/*.tf; then
    TASK_REUSE+=("true")
    echo "Task ${TASK_NAME}: reusing seeded cluster ${SEEDED_TASK_CLUSTER} (${SEEDED_TASK_LOCATION}); no per-run task cluster will be created"
  else
    TASK_REUSE+=("")
  fi
done

# One unit, in a background subshell: its exports stay local, its output goes
# only to its own log (kept as an artifact either way), and its run directory
# is read back from that log's own `results:` line -- the directory-set diff
# the serial loop used cannot tell concurrent siblings apart. BENCH_NO_INFRA
# stays false for every unit, noop-deployer ones included: true would skip
# verification wholesale (evalharness/default.py, "skipped_no_infra") and
# silently un-gate transcript-read checks.
STATE_DIR="$(mktemp -d)"

# mkdir is the mutex: atomic on every filesystem this runs on. A holder that
# dies without releasing (an OOM-killed subshell releases nothing) would
# otherwise strand every contender in a silent spin that `wait` can never
# collect past, so acquisition carries a deadline: a unit that gives up fails
# loudly and grades as MISSING, which is a diagnosis the gate already
# reports. Two locks serialize what genuinely cannot overlap while noop
# units fill the lanes:
#   per task  -- repetitions of ONE task never overlap. Concurrent reps of a
#                ledger-writing audit rewrite one shared ledger issue and
#                grade each other's artifact; concurrent reps of the autoops
#                task plant simultaneous incidents with no card attribution;
#                and same-task reps share a tofu stack directory and cluster
#                name. Serial reps are also what keeps them comparable.
#   infra     -- at most one stack-bearing (tofu) unit runs at a time,
#                across tasks: BENCH_PARALLEL stays false, so devops-bench's
#                per-run isolation (own kubeconfig, gcloud config, tofu data
#                dir) is off, and two concurrent tofu units would race the
#                shared kubeconfig's current-context and their state locks.
lock_acquire() { # <dir> [deadline-seconds]
  local waited=0 limit="${2:-1800}"
  until mkdir "$1" 2>/dev/null; do
    sleep 3
    waited=$((waited + 3))
    if [ "${waited}" -ge "${limit}" ]; then
      echo "ERROR: gave up on ${1} after ${limit}s; holder likely died without releasing" >&2
      return 1
    fi
  done
}
lock_release() { rmdir "$1" 2>/dev/null || true; }

run_one_unit() { # <task-path> <task-name> <rep> <reuse:true|empty> <has-stack:true|empty> <seq>
  local task="$1" name="$2" rep="$3" reuse="$4" has_stack="$5" seq="$6"
  local log="/tmp/eval_${name}_rep${rep}.log"
  # A distinct local port per unit: the harness's port-forward is owned by
  # the process that spawned it and its atexit teardown would drop a shared
  # listener under every sibling mid-conversation. On its own port, each
  # unit owns its own tunnel and keeps the harness's stale-tunnel recycling.
  export AGENT_LOCAL_PORT=$((28642 + seq))
  if ! lock_acquire "${STATE_DIR}/lock-task-${name}"; then
    echo "<<< [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ${name} rep ${rep} gave up on its task lock" >&2
    return 0
  fi
  if [ -n "${has_stack}" ] && ! lock_acquire "${STATE_DIR}/lock-infra"; then
    lock_release "${STATE_DIR}/lock-task-${name}"
    echo "<<< [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ${name} rep ${rep} gave up on the infra lock" >&2
    return 0
  fi
  # This unit's own token, minted rather than inherited, and minted after the
  # waiting rather than before it: reps of one task serialize on the task lock,
  # so at the default EVAL_REPETITIONS=3 a unit can sleep past the hour a token
  # lasts and reach devops-bench holding a dead one.
  if ! mint_ledger_token "${name} rep ${rep}"; then
    [ -n "${has_stack}" ] && lock_release "${STATE_DIR}/lock-infra"
    lock_release "${STATE_DIR}/lock-task-${name}"
    echo "<<< [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ${name} rep ${rep} could not mint a ledger token" >&2
    return 0
  fi
  if [ -n "${reuse}" ]; then
    export GKE_CLUSTER_NAME="${SEEDED_TASK_CLUSTER}" CLUSTER_NAME="${SEEDED_TASK_CLUSTER}"
    export TF_VAR_cluster_name="${SEEDED_TASK_CLUSTER}" GCP_LOCATION="${SEEDED_TASK_LOCATION}"
    export TF_VAR_reuse_existing_cluster="true"
  else
    export GKE_CLUSTER_NAME="${EVAL_CLUSTER_NAME}" CLUSTER_NAME="${EVAL_CLUSTER_NAME}"
    export TF_VAR_cluster_name="${EVAL_CLUSTER_NAME}" GCP_LOCATION="${EVAL_DEFAULT_LOCATION}"
    unset TF_VAR_reuse_existing_cluster
  fi
  export BENCH_NO_INFRA="false"
  local start end dir
  start="$(_now_ms)"
  (cd "${BENCH_DIR}" && uv run devops-bench "${task}" --agent-type kubeagents 2>&1 | _ts_lines > "${log}") || true
  end="$(_now_ms)"
  [ -n "${has_stack}" ] && lock_release "${STATE_DIR}/lock-infra"
  lock_release "${STATE_DIR}/lock-task-${name}"
  # `|| true`: a run that never printed a `results:` line must still write
  # its state files and reach the artifact copy -- it is exactly the crashed
  # run someone will need the log for.
  dir="$(grep -oE 'results: [^ ]*/results\.json' "${log}" | tail -1 | sed -e 's/^results: //' -e 's|/results\.json$||' || true)"
  printf '%s\n' "${start}" > "${STATE_DIR}/${name}.rep${rep}.start"
  printf '%s\n' "${end}" > "${STATE_DIR}/${name}.rep${rep}.end"
  printf '%s\n' "${dir}" > "${STATE_DIR}/${name}.rep${rep}.dir"
  # Copied here, not in the grading pass: a Prow deadline that kills the
  # fan-out must still leave every completed unit's log in the artifacts.
  cp "${log}" "${ARTIFACT_DIR}/eval_${name}_rep${rep}.log" 2>/dev/null || true
  echo "<<< [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] finished ${name} rep ${rep} in $(((end - start) / 1000))s"
}

# Rep-ascending FIRST, cost-descending within a rep: repetitions of one task
# serialize on the task lock, so a same-task unit launched early just parks a
# lane sleeping on it -- the pool run of 2026-08-31 (build 2094432646640701440)
# spent two of four lanes that way for its first twelve minutes under the
# cost-first ordering this replaces.
UNIT_QUEUE="$(
  for REP in $(seq 1 "${EVAL_REPETITIONS}"); do
    i=0
    for TASK in "${TASKS[@]}"; do
      printf '%s %s %s\n' "${REP}" "$(unit_cost_hint "${TASK_NAMES[i]}")" "$i"
      i=$((i + 1))
    done
  done | sort -k1,1n -k2,2rn
)"
UNIT_TOTAL="$(printf '%s\n' "${UNIT_QUEUE}" | grep -c .)"

profile_begin "task fan-out: ${UNIT_TOTAL} units, parallelism=${EVAL_TASK_PARALLELISM}"
while read -r REP _COST IDX; do
  [ -n "${IDX:-}" ] || continue
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "${EVAL_TASK_PARALLELISM}" ]; do
    sleep 3
  done
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] launching ${TASK_NAMES[IDX]} rep ${REP}/${EVAL_REPETITIONS}"
  UNIT_SEQ=$((${UNIT_SEQ:-0} + 1))
  run_one_unit "${TASKS[IDX]}" "${TASK_NAMES[IDX]}" "${REP}" "${TASK_REUSE[IDX]}" "${TASK_HAS_STACK[IDX]}" "${UNIT_SEQ}" &
  # Staggered, so N units do not open their first model call in the same
  # second -- burst 429s at the model quota are the fan-out's failure mode.
  sleep 5
done <<EOF_UNIT_QUEUE
${UNIT_QUEUE}
EOF_UNIT_QUEUE
wait

# ─── Per-case verdicts, in the order TASKS declares ───────────────────────────
profile_begin "per-repetition breakdowns + case verdicts"
i=0
for TASK in "${TASKS[@]}"; do
  TASK_NAME="${TASK_NAMES[i]}"
  i=$((i + 1))
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Grading Task: ${TASK_NAME} (${TASK}) x${EVAL_REPETITIONS} <<<"

  # One --result per repetition, positionally. A repetition that produced no
  # run directory contributes the literal MISSING, so the gate can tell "died
  # before writing anything" from "wrote an unusable record". The harness log
  # is kept for every repetition, green ones included: a green record is the
  # raw material for the baseline store.
  RESULT_ARGS=()
  for REP in $(seq 1 "${EVAL_REPETITIONS}"); do
    EVAL_LOG="/tmp/eval_${TASK_NAME}_rep${REP}.log"
    NEW_RUN_DIR="$(cat "${STATE_DIR}/${TASK_NAME}.rep${REP}.dir" 2>/dev/null || true)"
    RUN_START_MS="$(cat "${STATE_DIR}/${TASK_NAME}.rep${REP}.start" 2>/dev/null || echo 0)"
    RUN_END_MS="$(cat "${STATE_DIR}/${TASK_NAME}.rep${REP}.end" 2>/dev/null || echo 0)"
    REP_RESULT=""
    [ -n "${NEW_RUN_DIR}" ] && REP_RESULT="${NEW_RUN_DIR}/results.json"
    analyze_eval_phases "${EVAL_LOG}" "${RUN_START_MS}" "${RUN_END_MS}" "${TASK_NAME} rep ${REP}" "${REP_RESULT}"
    if [ -n "${NEW_RUN_DIR}" ]; then
      RESULT_ARGS+=(--result "${NEW_RUN_DIR}")
      cp "${NEW_RUN_DIR}/results.json" "results_${TASK_NAME}_rep${REP}.json" 2>/dev/null || true
    else
      RESULT_ARGS+=(--result MISSING)
    fi
  done

  # The verdict. bench-gate exits 0 for ANY verdict it could reach, including a
  # blocking one; it exits 2 only when it could not grade at all, which must
  # stop the job.
  CASE_JSON="${ARTIFACT_DIR}/case-${TASK_NAME}.json"
  (cd "${BENCH_DIR}" && uv run bench-gate case \
    --task "${TASK}" \
    "${RESULT_ARGS[@]}" \
    --json-out "${CASE_JSON}")
  CASE_RESULTS+=(--case-result "${CASE_JSON}")
done

profile_begin "record + final gate"

# The INFRA_FAILED_TASKS / FAILED_TASKS roll-up that stood here is gone: the
# blocking-case list and the all-infrastructure check are both `bench-gate
# suite`'s now, computed from the per-case JSON rather than from shell state
# accumulated in the loop.

# Baseline collection, and it runs BEFORE the verdict on purpose: the suite
# step exits 1 on a red, which under `set -e` would skip everything after it.
# A red run on main is precisely the evidence that de-admits a case that has
# stopped working, so it is the one run that must not go unrecorded.
#
# Only a run on main appends: the nightly periodic today, and a postsubmit if
# one is ever added back. `bench-gate record` refuses a second time if
# PULL_NUMBER is set, because a guard that lives only in shell is one careless
# edit away from letting a pull request move the baseline it is judged against.
#
# JOB_TYPE is matched against both because the recorder moved from per-merge to
# nightly. At ~10 merges a day a postsubmit paid ~40 minutes of cluster
# provisioning for three samples of each case, and provisioning -- not the eval
# -- is what the job spends its time on. One nightly run amortises that setup
# over every repetition, so it buys a sample far cheaper and can refill the
# whole 20-run admission window in a night or two after a version-key bump
# instead of over a week of merges. Neither job type is a pull request, which
# is the property that actually matters here; PULL_NUMBER below is what
# enforces it. See docs/designs/eval-scorer.md#the-job-that-writes-it.
#
# With EVAL_BASELINE_STORE pointing at a bucket the append lands and the loop
# closes. Unset, the store is the git checkout and this job has no push
# credential, so the append dies with the workspace; --lines-out is what
# survives, as a Prow artefact somebody lands by hand in the meantime.
#
# RC_COMMIT_SHA is the third condition and the one that is not about pull
# requests. A release-candidate eval is a periodic with no PULL_NUMBER, so it
# satisfies the two conditions above exactly, and without this it would file the
# candidate's results as main's. That is not a mistake anybody can undo later:
# VersionKey in bench/kube_agents_bench/baselines.py is setup_id,
# scoring_version, judge_model, fleet and verifiers, with no field naming the
# build a sample came from, so an RC record and a main record are the same
# record once written. The candidate would then be measured for non-inferiority
# against a window it had just moved.
case "${JOB_TYPE:-}" in
  postsubmit | periodic) EVAL_IS_MAIN_RUN="true" ;;
  *) EVAL_IS_MAIN_RUN="false" ;;
esac
if [ -n "${RC_COMMIT_SHA:-}" ]; then
  EVAL_IS_MAIN_RUN="false"
fi
if [ "${EVAL_IS_MAIN_RUN}" = "true" ] && [ -z "${PULL_NUMBER:-}" ]; then
  echo ">>> [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Recording baseline evidence from main <<<"
  # Never fatal. Bookkeeping must not be the reason a merge to main reds.
  (cd "${BENCH_DIR}" && uv run bench-gate record \
    "${CASE_RESULTS[@]}" \
    --lines-out "${ARTIFACT_DIR}/baseline-append.jsonl") || \
    echo "WARNING: recording baseline evidence failed; the verdict below is unaffected."
elif [ -n "${RC_COMMIT_SHA:-}" ]; then
  echo "Release-candidate run (RC_COMMIT_SHA=${RC_COMMIT_SHA}): the baseline store is read, never written — the candidate is judged against main's window, not added to it."
else
  echo "Not a main-branch recorder run (JOB_TYPE=${JOB_TYPE:-unset}): the baseline store is read, never written."
fi

# The suite roll-up: blocking cases, the admitted-case aggregate, and the
# all-infrastructure check. Exit 0 green, 1 red. --baseline-rate is not passed:
# the rate is computed from the store, per admitted case at its own version
# key. While the store holds nothing the aggregate stays advisory and the
# markdown says so, rather than implying a comparison that did not happen.
TOTAL_DURATION=$((SECONDS - START_TIME))
if (cd "${BENCH_DIR}" && uv run bench-gate suite \
  "${CASE_RESULTS[@]}" \
  --markdown-out "${ARTIFACT_DIR}/eval-verdict.md" \
  --json-out "${ARTIFACT_DIR}/eval-verdict.json"); then
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Succeeded (Total Duration: ${TOTAL_DURATION}s) ==="
else
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed -- see ${ARTIFACT_DIR}/eval-verdict.md (Total Duration: ${TOTAL_DURATION}s)"
  exit 1
fi
