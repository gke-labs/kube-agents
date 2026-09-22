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

# The eval rosters, three files beside this script under hack/eval/ (#1546):
# what every pull request runs, what can red one on a graded failure, and
# what the nightly adds. Section 6 reads the first and third into TASKS and
# NIGHTLY_TASKS; the BOOTSTRAP_ADMITTED export reads the second. Files rather
# than arrays here so OWNERS can put the eval-crew rule on the presubmit
# pair alone. Relative to this script's directory.
readonly EVAL_PRESUBMIT_CASES_FILE="eval/presubmit-cases.txt"
readonly EVAL_BLOCKING_ROSTER_FILE="eval/blocking-roster.txt"
readonly EVAL_NIGHTLY_CASES_FILE="eval/nightly-cases.txt"

# What `bench-gate suite` exits, and writes as `outcome` in eval-verdict.json,
# when the run could not be evaluated: an admitted case lost every repetition
# to infrastructure, or every case did. Both are bench/kube_agents_bench/
# gate.py's (SUITE_EXIT_NOT_EVALUATED) and scoring.py's
# (SUITE_OUTCOME_NOT_EVALUATED); the verdict step below reads the two
# together, because 2 alone is also what argparse exits on a bad flag.
readonly EVAL_SUITE_NOT_EVALUATED_STATUS=2
readonly EVAL_VERDICT_OUTCOME_NOT_EVALUATED="not_evaluated"

# ─── Step 0: self-revalidation against this PR's own green history (#1179) ───
# A push that changes only inert files re-runs this whole job and aborts the
# run in flight -- #1127's comment-only push cost a 123-minute re-run. Prow's
# skip_if_only_changed filter cannot help: it sees the PR's whole diff against
# the base, not the delta since the last green build. This step applies the
# same kind of path predicate to the DELTAS instead: find this PR's newest
# green build in the job history on GCS, recover that build's head and base
# SHAs, and if everything that changed since -- on the PR side AND on main's
# side -- matches the inert list, reuse the green verdict and exit 0 before
# any cluster work.
#
# FAIL-CLOSED THROUGHOUT: every doubt -- no history, unreadable GCS, an
# unparsable record, a commit the checkout does not have, any file escaping
# the inert list -- is one log line and a full run. The first run on a PR has
# no green history, so it is always a full run. EVAL_SKIP_REVALIDATION=1 is
# the escape hatch: it forces a full run for debugging a suspect reuse.
#
# One asymmetry is deliberate: the NEWEST GREEN wins, so a newer red full run
# at inert distance from an older green is overridden on the next inert
# trigger. That is the same judgement a passing /retest would render -- an
# inert delta cannot feed the eval, so the red was flake or infrastructure by
# construction -- but it does mean reproducing such a red needs either a
# non-inert push or EVAL_SKIP_REVALIDATION=1 in the job env.
#
# What it saves, honestly: the Boskos lease, ci-deploy.sh and ci-teardown.sh
# run BEFORE and AFTER this script in the Prow job wrapper, so a revalidated
# run still pays the lease + image build + deploy + teardown (~20-30min of a
# saturated pool's time), not zero -- what it skips is the eval matrix, the
# ~2h that dominates the job. Hoisting the check ahead of the lease would be
# an oss-test-infra change; this one is deliberately kube-agents-side only.
#
# Trust surface. For the SELF case, subsumption: a pull request that wants
# its own context green can already edit this script to `exit 0` -- its own
# code IS the job -- and a PR that edits the revalidation logic touches
# hack/, which is not inert, so its own run goes full. That argument does
# NOT cover the CROSS-PR case: the job history under gs://kube-agents-prow
# is written by pod utilities that may share the test container's identity,
# so a hostile PR's run could conceivably plant a fabricated "green" record
# under a VICTIM PR's history path (kube-agents-bot's review of #1186 built
# the full attack). The GCS records are therefore never trusted alone: a
# candidate green build counts only when GitHub holds a SUCCESS status
# event for this job's context on the recovered head whose target URL names
# that same build id. Statuses are posted by Prow's reporter with
# repository write permission -- google-oss-prow[bot] -- which no pull
# request holds, and the events are append-only per build (a later aborted
# run does not erase an earlier build's success event; verified against
# #1127's head 50e0f44f). The SHAs are also required to be 40-hex before
# any git command sees them, so a forged record cannot smuggle arguments.
#
# Downstream note: a revalidated run's build log carries no per-task result
# lines and no final-verdict line. scripts/eval_dashboard/collect.py already
# tolerates that shape -- aborted runs produce taskless builds today -- and
# keys nothing on this job exiting through its normal tail.

# The inert-path predicate. This list may be STRICTER than the Prow yaml's
# skip_if_only_changed (prow/prowjobs/gke-labs/kube-agents/
# kube-agents-presubmits.yaml in GoogleCloudPlatform/oss-test-infra), and it
# deliberately lives here rather than being fetched from there: the worst
# case of the two diverging is an unnecessary full run, never a wrongly
# skipped one. Keep it root-anchored -- `docs-evil.go` must not match the
# docs/ branch, `bench/OWNERS` must not match the OWNERS one, and a .md file
# below the root (agents/**/*.md is prompt content shipped in the image)
# must still run the eval.
readonly REVALIDATION_INERT_PATHS='^((docs|\.github|examples)/|[^/]+\.md$|(LICENSE|OWNERS|OWNERS_ALIASES)$)'
# Where the job history lives and how a human opens a build from the log.
readonly REVALIDATION_HISTORY_PREFIX="gs://kube-agents-prow/pr-logs/pull/gke-labs_kube-agents"
readonly REVALIDATION_JOB_NAME="pull-kube-agents-smoke-test"
readonly REVALIDATION_SPYGLASS_PREFIX="https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents"
# The started.json repos key naming this repository's clone record, and the
# base ref assumed when the decoration did not export PULL_BASE_REF.
readonly REVALIDATION_REPO_KEY="gke-labs/kube-agents"
readonly REVALIDATION_DEFAULT_BASE_REF="main"
# Where the Prow-posted status events live: the attestation that a claimed
# green build really ran and really passed (see the trust-surface note
# above). Read with BENCH_GITHUB_TOKEN when the job mounts one, falling back
# to an anonymous read of the public repo.
readonly REVALIDATION_STATUS_API="https://api.github.com/repos/gke-labs/kube-agents/commits"
# How many of the newest builds to inspect for a green one. Each costs one
# gsutil cat (~1s); an active PR rarely stacks this many pushes between
# greens, and a bound keeps the fall-through path seconds long.
readonly REVALIDATION_HISTORY_LIMIT=20

_revalidation_print_delta() { # <label> <range> <files-or-empty>
  echo "${1} (${2}):"
  if [ -n "${3}" ]; then
    printf '%s\n' "${3}" | sed 's/^/    /'
  else
    echo "    (empty -- identical trees, trivially inert)"
  fi
}

# Returns 0 when the previous green verdict still stands (caller exits 0) and
# 1 for a full run. Every fall-through path logs exactly one "Step 0: full
# run:" line naming its reason.
revalidate_against_green_history() {
  local repo_dir
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  if [ "${EVAL_SKIP_REVALIDATION:-}" = "1" ]; then
    echo "Step 0: full run: EVAL_SKIP_REVALIDATION=1 (escape hatch)"
    return 1
  fi
  if [ -z "${PULL_NUMBER:-}" ] || [ -z "${PULL_PULL_SHA:-}" ] || [ -z "${PULL_BASE_SHA:-}" ]; then
    echo "Step 0: full run: not a decorated Prow presubmit (PULL_NUMBER, PULL_PULL_SHA or PULL_BASE_SHA unset)"
    return 1
  fi
  if ! command -v gsutil >/dev/null 2>&1; then
    echo "Step 0: full run: no gsutil on PATH to read the job history with"
    return 1
  fi
  # Preflighted like gsutil so a missing interpreter logs its own reason
  # instead of every finished.json silently classifying as not-green.
  if ! command -v python3 >/dev/null 2>&1; then
    echo "Step 0: full run: no python3 on PATH to parse the job records with"
    return 1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "Step 0: full run: no curl on PATH to read the GitHub status attestation with"
    return 1
  fi

  local history_dir="${REVALIDATION_HISTORY_PREFIX}/${PULL_NUMBER}/${REVALIDATION_JOB_NAME}"
  local listing
  if ! listing="$(gsutil ls "${history_dir}/*/finished.json" 2>/dev/null)"; then
    echo "Step 0: full run: no finished ${REVALIDATION_JOB_NAME} build for PR #${PULL_NUMBER} (first run on this PR, or GCS unreadable)"
    return 1
  fi

  # Newest first: build IDs are numeric and monotonically increasing.
  local candidates
  candidates="$(printf '%s\n' "${listing}" | sed -n 's|.*/\([0-9][0-9]*\)/finished\.json$|\1|p' | sort -rn | head -n "${REVALIDATION_HISTORY_LIMIT}")"
  if [ -z "${candidates}" ]; then
    echo "Step 0: full run: the job history listing held no parseable build ids"
    return 1
  fi

  local build prev_green="" finished
  while read -r build; do
    [ -n "${build}" ] || continue
    finished="$(gsutil cat "${history_dir}/${build}/finished.json" 2>/dev/null)" || continue
    if printf '%s' "${finished}" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("passed") is True else 1)' 2>/dev/null; then
      prev_green="${build}"
      break
    fi
  done <<EOF_REVALIDATION_CANDIDATES
${candidates}
EOF_REVALIDATION_CANDIDATES
  if [ -z "${prev_green}" ]; then
    echo "Step 0: full run: no green build among the newest ${REVALIDATION_HISTORY_LIMIT} ${REVALIDATION_JOB_NAME} builds for PR #${PULL_NUMBER}"
    return 1
  fi

  # That build's head and base SHAs, from its started.json clone record:
  # repos["gke-labs/kube-agents"] reads "main:<base_sha>,<pr>:<head_sha>".
  local started shas prev_base prev_head
  if ! started="$(gsutil cat "${history_dir}/${prev_green}/started.json" 2>/dev/null)"; then
    echo "Step 0: full run: green build ${prev_green} has no readable started.json"
    return 1
  fi
  if ! shas="$(printf '%s' "${started}" | python3 -c '
import json
import sys

base_ref, pull, repo_key = sys.argv[1], sys.argv[2], sys.argv[3]
refs = json.load(sys.stdin)["repos"][repo_key]
parts = dict(part.split(":", 1) for part in refs.split(","))
base, head = parts.get(base_ref), parts.get(pull)
if not base or not head:
    raise SystemExit(1)
print(base, head)
' "${PULL_BASE_REF:-${REVALIDATION_DEFAULT_BASE_REF}}" "${PULL_NUMBER}" "${REVALIDATION_REPO_KEY}" 2>/dev/null)"; then
    echo "Step 0: full run: could not recover base/head SHAs from green build ${prev_green}'s started.json"
    return 1
  fi
  prev_base="${shas%% *}"
  prev_head="${shas##* }"

  # Nothing recovered from GCS is trusted yet -- see the trust-surface note
  # in the header. Three bindings, all fail-closed:
  #   1. well-formed SHAs, so a forged record cannot smuggle git arguments;
  #   2. the build's two records agree on the head they claim;
  #   3. GitHub holds a Prow-posted SUCCESS status event for this job's
  #      context on that head whose target URL names this very build.
  local sha
  for sha in "${prev_base}" "${prev_head}"; do
    if ! printf '%s' "${sha}" | grep -Eq '^[0-9a-f]{40}$'; then
      echo "Step 0: full run: build ${prev_green}'s started.json holds a malformed SHA"
      return 1
    fi
  done
  local finished_revision
  finished_revision="$(printf '%s' "${finished}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision") or "")' 2>/dev/null)" || finished_revision=""
  if [ "${finished_revision}" != "${prev_head}" ]; then
    echo "Step 0: full run: build ${prev_green}'s finished.json revision (${finished_revision:-unreadable}) does not match its started.json head (${prev_head})"
    return 1
  fi
  local statuses curl_auth=()
  [ -n "${BENCH_GITHUB_TOKEN:-}" ] && curl_auth=(-H "Authorization: Bearer ${BENCH_GITHUB_TOKEN}")
  statuses="$(curl -fsS --max-time 30 ${curl_auth[@]+"${curl_auth[@]}"} "${REVALIDATION_STATUS_API}/${prev_head}/statuses?per_page=100" 2>/dev/null)" \
    || statuses="$(curl -fsS --max-time 30 "${REVALIDATION_STATUS_API}/${prev_head}/statuses?per_page=100" 2>/dev/null)" \
    || { echo "Step 0: full run: could not read GitHub statuses for ${prev_head} to attest green build ${prev_green}"; return 1; }
  if ! printf '%s' "${statuses}" | python3 -c '
import json
import sys

context, build = sys.argv[1], sys.argv[2]
needle = "/" + context + "/" + build
for status in json.load(sys.stdin):
    if (
        status.get("context") == context
        and status.get("state") == "success"
        and needle in (status.get("target_url") or "")
    ):
        sys.exit(0)
sys.exit(1)
' "${REVALIDATION_JOB_NAME}" "${prev_green}" 2>/dev/null; then
    echo "Step 0: full run: GitHub holds no ${REVALIDATION_JOB_NAME} success status on ${prev_head} naming build ${prev_green} -- refusing to trust the GCS record alone"
    return 1
  fi

  # Both previous SHAs must exist locally. The decorated checkout normally
  # has them (they are ancestors of the current base and head); a force-push
  # can orphan prev_head, so try one fetch from origin -- the clonerefs
  # remote for this repository, never anywhere else -- then fail closed.
  for sha in "${prev_base}" "${prev_head}"; do
    if ! git -C "${repo_dir}" cat-file -e "${sha}^{commit}" 2>/dev/null; then
      git -C "${repo_dir}" fetch --quiet origin "${sha}" 2>/dev/null || true
      if ! git -C "${repo_dir}" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        echo "Step 0: full run: commit ${sha} from green build ${prev_green} is not in this checkout"
        return 1
      fi
    fi
  done

  # --no-renames is load-bearing: with rename detection (git's default) a
  # `git mv hack/tool.sh docs/tool.md` lists ONLY the inert destination, and
  # the deletion of the non-inert source becomes invisible to the predicate.
  # Disabling it makes every rename a delete + add, so the non-inert side
  # always surfaces.
  local head_delta base_delta
  if ! head_delta="$(git -C "${repo_dir}" diff --no-renames --name-only "${prev_head}" "${PULL_PULL_SHA}" 2>/dev/null)"; then
    echo "Step 0: full run: git diff ${prev_head}..${PULL_PULL_SHA} failed"
    return 1
  fi
  if ! base_delta="$(git -C "${repo_dir}" diff --no-renames --name-only "${prev_base}" "${PULL_BASE_SHA}" 2>/dev/null)"; then
    echo "Step 0: full run: git diff ${prev_base}..${PULL_BASE_SHA} failed"
    return 1
  fi

  # The predicate: EVERY file in BOTH deltas matches the inert list. An empty
  # delta (identical SHAs) is trivially inert -- nothing changed on that side.
  local survivors
  survivors="$(printf '%s\n%s\n' "${head_delta}" "${base_delta}" | grep -v '^$' | grep -Ev "${REVALIDATION_INERT_PATHS}" || true)"
  if [ -n "${survivors}" ]; then
    echo "Step 0: full run: files outside REVALIDATION_INERT_PATHS changed since green build ${prev_green}:"
    printf '%s\n' "${survivors}" | sed 's/^/    /'
    return 1
  fi

  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 0: REVALIDATED against green build ${prev_green} -- every change since is inert, skipping the eval matrix ==="
  echo "Reused verdict: ${REVALIDATION_SPYGLASS_PREFIX}/${PULL_NUMBER}/${REVALIDATION_JOB_NAME}/${prev_green}"
  echo "Attested by the Prow-posted ${REVALIDATION_JOB_NAME} success status on ${prev_head}"
  _revalidation_print_delta "head delta" "${prev_head}..${PULL_PULL_SHA}" "${head_delta}"
  _revalidation_print_delta "base delta" "${prev_base}..${PULL_BASE_SHA}" "${base_delta}"
  echo "Predicate: every file above matches REVALIDATION_INERT_PATHS ${REVALIDATION_INERT_PATHS}"
  return 0
}

if revalidate_against_green_history; then
  exit 0
fi

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
# credential and through collect.py, which reads the hack/eval/ rosters and
# the domain metadata out of THIS checkout. The gate is the baseline recorder's
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
#      (.github/workflows/ci-health.yml publishes the same dashboard, plus
#      health.json and health-state.json, as this same publisher identity
#      reached through GitHub Actions' Workload Identity -- so "bound to
#      that periodic alone" now reads "to that periodic and that workflow",
#      still nothing a presubmit can run as.)
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
  # kills the whole pipeline instead -- and the sweep is 1 + 3N gsutil
  # processes over every archived build (READ_WORKERS at a time), so it
  # needs real headroom on top. 900s covers both and only ever taxes the
  # nightly's tail (the gate
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
    # The adjudicator verdict and its history, when the target has them, so the
    # rendered Brief bakes the current state instead of waiting for the first
    # page poll (and on storage.cloud.google.com that XHR redirects and fails).
    # Missing is the normal case until the adjudicator has run. The published
    # store.json rides along the same way for the Trend page: this hook never
    # reads the evidence store itself (the ci-health workflow does), it only
    # keeps the page on the last read instead of blanking it.
    case "$3" in
      gs://*)
        gsutil cp "${3%/}/health.json" "$2/health.json" 2>&1 || rm -f "$2/health.json"
        gsutil cp "${3%/}/health-history.jsonl" "$2/health-history.jsonl" 2>&1 || rm -f "$2/health-history.jsonl"
        gsutil cp "${3%/}/store.json" "$2/store.json" 2>&1 || rm -f "$2/store.json"
        ;;
      *)
        [ -f "${3%/}/health.json" ] && cp "${3%/}/health.json" "$2/health.json" || true
        [ -f "${3%/}/health-history.jsonl" ] && cp "${3%/}/health-history.jsonl" "$2/health-history.jsonl" || true
        [ -f "${3%/}/store.json" ] && cp "${3%/}/store.json" "$2/store.json" || true
        ;;
    esac
    render_args=()
    [ -f "$2/health.json" ] && render_args+=(--health "$2/health.json")
    [ -f "$2/health-history.jsonl" ] && render_args+=(--health-history "$2/health-history.jsonl")
    [ -f "$2/store.json" ] && render_args+=(--store "$2/store.json")
    # Same --public-url rule as hack/ci-dashboard-refresh.sh: a bucket target
    # is the published site, so pass the target to derive <base href> without
    # hardcoding production when targeting a staging bucket. A local directory
    # target keeps links relative.
    case "$3" in gs://*) render_args+=(--public-url "$3") ;; esac
    python3 "$1/render.py" --data "$2/data.json" --out-dir "$2/site" ${render_args[@]+"${render_args[@]}"}
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
  # Last, after everything slow: the eval-lifetime Boskos heartbeat started
  # below the traps keeps the lease alive through this tail too. On a run
  # past boskosctl's 5h --timeout nothing else beats, and the tail is not
  # bounded by the ~5m reaper window (the artifact dump's kubectl calls
  # carry no --request-timeout; the dashboard publish has a 900s budget), so
  # killing it first would reopen the gap this daemon closes. The wrapper's
  # release comes after ci-teardown.sh, minutes from now, so the daemon is
  # gone long before it; caller_alive stops it even if this kill is never
  # reached. Unset outside Prow.
  kill "${EVAL_HEARTBEAT_PID:-}" 2>/dev/null || true
}
trap profile_and_dump_on_exit EXIT
# A Prow deadline delivers SIGTERM, which does not run the EXIT trap on its
# own; converting it to an exit is what lets the artifact collection above
# fire on a deadline kill.
trap 'exit 143' TERM INT

# ─── Boskos lease heartbeat for the eval's own lifetime ──────────────────────
# The Prow wrapper's `boskosctl heartbeat` covers this step, but boskosctl
# stops beating after its default --timeout of 5h ("reached timeout,
# stopping heartbeats", exit 0, so the wrapper's abort fallback never fires)
# and the wrapper sets no --timeout. Boskos's ~5m reaper then clears the
# lease's owner, and every later /update and the final release answer 401
# OwnerNotMatch: the first full nightly (build 2100374258805903360,
# 2026-09-17) lost kube-agents-evals-6 that way at 05:01Z of a run that ended
# 05:57Z and could not hand the project back (#1491). A presubmit that
# outlives 5h under a quota storm (#1214) is exposed the same way.
#
# The daemon ci-teardown.sh already runs beats here for exactly this script's
# lifetime -- it has no timeout -- so the lease stays fresh however long the
# fan-out and the EXIT trap's artifact tail take; profile_and_dump_on_exit
# kills it as its last act, minutes before the wrapper's release, and the
# daemon stops itself once this script is gone. Same endpoint and owner convention as the
# wrapper (BOSKOS_OWNER="${JOB_NAME}-${BUILD_ID}", oss-test-infra
# prow/prowjobs/gke-labs/kube-agents/*.yaml); outside Prow nothing is derived
# and the daemon disables itself with one line. `disown` is load-bearing: the
# fan-out below sizes its lanes with `jobs -rp`, and a daemon left in the job
# table would take one of them for the whole run (and be waited on).
if [ -n "${JOB_NAME:-}" ] && [ -n "${BUILD_ID:-}" ]; then
  export BOSKOS_HOST="${BOSKOS_HOST:-http://boskos.boskos.svc.cluster.local}"
  export BOSKOS_RESOURCE_NAME="${BOSKOS_RESOURCE_NAME:-${PROJECT_ID}}"
  export BOSKOS_OWNER_NAME="${BOSKOS_OWNER_NAME:-${JOB_NAME}-${BUILD_ID}}"
fi
"${SCRIPT_DIR}/boskos_heartbeat.sh" &
EVAL_HEARTBEAT_PID=$!
# Outside Prow the daemon exits within milliseconds; if bash has already
# reaped it, disown answers "no such job", which must not be a set -e death.
disown "${EVAL_HEARTBEAT_PID}" 2>/dev/null || true

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
# still parked outside the matrix, and that was the point: the warnings it
# prints per project ("carries no clusters labelled environment=seeded") are
# how a pool project still needing bench/tf/fleet applied was found BEFORE
# these tasks started gating PRs rather than after. Most of the active
# tasks below read the seeded fleet -- the six domain probes, the
# fleet-audits canary, the cluster-debugging family, the incident-triage
# probe over the same crashloop, and the remediation cases that propose
# fixes for those fixtures -- so those warnings have consumers. It costs one
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
# The other half is the token-creator grant -- `fleet_reader_token_creators`
# in bench/tf/fleet/variables.tf, which defaults to both runners, the
# presubmit's and the nightly's, and to the CI health bot, so an apply of that
# stack grants each. In a project whose fleet was applied before that default
# landed, `gcloud auth print-access-token
# --impersonate-service-account` fails, fleet-kubeconfigs.sh warns per cluster,
# and the role kubeconfigs keep the runner's own read-write credential. That is
# a privilege gap on a fleet every open PR shares, not a functional one: the
# files are still written, still point at the right seeded cluster, and every
# check still grades the right object. `scripts/verify_ci_pool_project.py`
# fails a project missing the binding. See bench/tf/fleet/README.md, "A
# read-only credential for evaluations".
export FLEET_READONLY_SA="${FLEET_READONLY_SA:-seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com}"

profile_begin "fleet-kubeconfigs: seeded-fleet credentials"
STEP_START=$SECONDS
# shellcheck source=hack/fleet-kubeconfigs.sh
source "${SCRIPT_DIR}/fleet-kubeconfigs.sh"
write_fleet_kubeconfigs || echo "WARNING: the seeded-fleet catalog or output directory is unusable, so no fleet kubeconfigs were written at all; every fleet fixture check will report status=error" >&2
echo "✓ Seeded-fleet credentials finished in $((SECONDS - STEP_START))s"

# Section 2c resized slot a's default pool to two nodes here (#1278). The incident's
# own manual sweep had already raised all 30 projects and main.tf declares the same
# count, so the heal never fired: this job's last deliberate fleet write (#1693).


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
#
# This is the ceiling every unit inherits. The full-audit units override it
# per unit in run_one_unit through unit_delegation_timeout (beside
# unit_cost_hint, below): the measured audit outgrew 2700s too (#1683).
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
# hack/fleet-kubeconfigs.sh uses. This block is the only sanctioned addresser
# of a seeded cluster outside that catalog chain, and the catalog's own
# description (bench/tf/fleet/fixtures.json) names it as the exception. It
# mutates nothing in-cluster; since #1693 no part of this job writes to the
# fleet's clusters at all.
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
PLATFORM_AGENT_TOKEN="$(kubectl get secret platform-agent-secrets -n "${TARGET_NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"
export PLATFORM_AGENT_TOKEN
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
# The matrix is data, not code: three files under hack/eval/, read here at
# startup (#1546, 2026-09-15). presubmit-cases.txt is what every pull request
# runs (TASKS), nightly-cases.txt is what EVAL_TIER=nightly appends
# (NIGHTLY_TASKS), and blocking-roster.txt, read further down, is what can red
# a pull request on a graded failure (BOOTSTRAP_ADMITTED). The split exists
# so OWNERS can tell them apart: hack/OWNERS puts the two presubmit files
# under the eval-crew alias and lets the nightly file and this script fall
# through to the root approvers. Each file's header says what belongs in it;
# docs/designs/bench-case-format.md "Registration" is the rule.
#
# Paths in the files are relative to BENCH_DIR, which is where devops-bench
# runs. Tasks added under bench/tasks/ are NOT picked up automatically -- a
# case runs because a file names it, and scripts/validate_bench_cases.py
# fails a case named in neither. A missing file, a line that is not a
# ./tasks/<id>/task.yaml path, a path with no case directory behind it, or a
# nightly entry that is also a presubmit one (it would run twice a night) all
# stop the job here, before it spends a cluster, rather than running a wrong
# matrix and reporting green around it.
BENCH_DIR="${SCRIPT_DIR}/../bench"
PRESUBMIT_CASES_FILE="${SCRIPT_DIR}/${EVAL_PRESUBMIT_CASES_FILE}"
NIGHTLY_CASES_FILE="${SCRIPT_DIR}/${EVAL_NIGHTLY_CASES_FILE}"
BLOCKING_ROSTER_FILE="${SCRIPT_DIR}/${EVAL_BLOCKING_ROSTER_FILE}"

# One roster file's entries, one per line: `#` to end of line is a comment,
# blank lines are skipped, surrounding whitespace is trimmed. Called inside a
# command substitution, so a missing file fails the assignment and set -e
# stops the job with the message. No mapfile: the bash the unit tests lift
# this into on a developer machine is 3.2.
roster_entries() {
  local file="$1"
  if [ ! -f "${file}" ]; then
    echo "ERROR: roster file ${file} is missing. The eval matrix is read from hack/eval/ (#1546); a checkout without it cannot run." >&2
    return 1
  fi
  sed -e 's/#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "${file}" | grep -v '^$' || true
}

# Every entry of one case file is a ./tasks/<id>/task.yaml path with a case
# directory behind it. $1 is the file, for the message; the rest are entries.
check_case_entries() {
  local file="$1"
  shift
  local entry
  for entry in "$@"; do
    case "${entry}" in
      ./tasks/*/task.yaml) ;;
      *)
        echo "ERROR: ${file}: '${entry}' is not a ./tasks/<id>/task.yaml path." >&2
        exit 1
        ;;
    esac
    if [ ! -f "${BENCH_DIR}/${entry}" ]; then
      echo "ERROR: ${file}: '${entry}' names no case under bench/tasks/. Register a case that exists, or delete the line." >&2
      exit 1
    fi
  done
}

# The presubmit matrix. The file's order is the gate's REPORTING order;
# execution order is the fan-out's cost-hinted queue below (longest units
# first), so a Prow deadline kills whatever is still in flight rather than
# truncating the list's tail.
PRESUBMIT_ENTRIES="$(roster_entries "${PRESUBMIT_CASES_FILE}")"
TASKS=()
while IFS= read -r ENTRY; do
  if [ -n "${ENTRY}" ]; then TASKS+=("${ENTRY}"); fi
done <<< "${PRESUBMIT_ENTRIES}"
if [ "${#TASKS[@]}" -eq 0 ]; then
  echo "ERROR: ${PRESUBMIT_CASES_FILE} names no case; the presubmit would run nothing and report green." >&2
  exit 1
fi
check_case_entries "${PRESUBMIT_CASES_FILE}" "${TASKS[@]}"
# The presubmit's case ids, one per line, for the blocking roster's subset
# check below -- taken before the tier switch can append the nightly.
PRESUBMIT_CASE_NAMES="$(for ENTRY in "${TASKS[@]}"; do basename "$(dirname "${ENTRY}")"; done)"

# ─── The nightly tier (#1021, the catch-all; #1023/#1024 consume it) ─────────
# The nightly periodic runs the FULL catalog: every presubmit case above,
# identically -- same repetitions, same gate, same reporting order -- PLUS
# the entries of nightly-cases.txt. That file is the default home of a new
# case (decided 2026-09-15 on #1546/#1564): it lands there, builds its record
# in the evidence store (EVAL_BASELINE_STORE below), and earns a presubmit
# seat on that record; measured cost, presubmit redundancy or grading
# something outside the core journeys keep a case there for good. The file's
# header carries the budget arithmetic against the periodic's 480m deadline
# at EVAL_TASK_PARALLELISM=6 (#1491), and that is the copy to keep current.
NIGHTLY_ENTRIES="$(roster_entries "${NIGHTLY_CASES_FILE}")"
NIGHTLY_TASKS=()
while IFS= read -r ENTRY; do
  if [ -n "${ENTRY}" ]; then NIGHTLY_TASKS+=("${ENTRY}"); fi
done <<< "${NIGHTLY_ENTRIES}"
if [ "${#NIGHTLY_TASKS[@]}" -eq 0 ]; then
  # A tier that appends nothing is a job that costs a Boskos lease to rerun
  # the presubmit at midnight; if every nightly case graduates or is retired,
  # delete the tier rather than leaving it vacuous.
  echo "ERROR: ${NIGHTLY_CASES_FILE} names no case; the nightly tier would be the presubmit under another name." >&2
  exit 1
fi
check_case_entries "${NIGHTLY_CASES_FILE}" "${NIGHTLY_TASKS[@]}"
for ENTRY in "${NIGHTLY_TASKS[@]}"; do
  if grep -qxF -- "${ENTRY}" <<< "${PRESUBMIT_ENTRIES}"; then
    echo "ERROR: ${NIGHTLY_CASES_FILE}: '${ENTRY}' is also in ${PRESUBMIT_CASES_FILE}; a nightly would run it twice, six repetitions graded as two cases of one name." >&2
    exit 1
  fi
done

# Which matrix this run gets. "presubmit" -- the default, and what every
# existing job runs -- is exactly the presubmit file, so the tier is dormant
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
# #1045 took it to sixteen without touching it. Recount the entries in
# hack/eval/presubmit-cases.txt rather than incrementing what is here.
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
# 2026-09-22: pdb-remediation-pr moved in from the nightly (#1023), the
# nineteenth case, measured rather than projected. Under the fan-out at
# parallelism 4 the serial arithmetic above no longer prices the job: the 14
# green presubmits of 09-19 to 09-21 ran the fan-out in 6073-10940s (median
# 8147s) against the 360m deadline, with ~15min of build and deploy outside
# it. This case costs 980-1929s a repetition in presubmit (420-1153s on the
# four graded nights), so three repetitions add ~3750-5800s of lane time,
# ~16-24min of wall clock at four lanes when the lanes are full. Only a
# repetition that ran to the 2700s delegation ceiling plus grading could
# make it the last unit; on the record it does not: its 1250s hint is the
# presubmit's largest, so it launches first in each repetition round, and
# the round-3 tail (first rep-3 launch to the fan-out's end) ran
# 2118-5849s in those 14 runs, longer at its shortest than 1929s,
# its longest measured repetition. The last unit stays one of the
# obtainability probes launched at the tail of round 3. No Prow deadline
# change rides with this activation.
#
# The same edit moved incident-triage-oom-event-probe in as the twentieth
# case: 737/599/1357s a repetition in its one presubmit run, 529-2808s on the
# four graded nights, so three repetitions add ~1800-4100s of lane time
# (~8-17min of wall clock at four lanes; up to ~35min if it runs at its
# nightly maximum). Hinted at 700 it launches right behind pdb-remediation-pr
# in each round, and its presubmit band ends well inside the shortest
# round-3 tail measured (2118s); only a repetition at the nightly maximum
# (2808s) could outlast that tail and make it the last unit, by minutes.
# Still no Prow deadline change.
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

# Whether the suite aggregate -- admitted-case pass rate against main's, over
# at least EVAL_AGGREGATE_MIN_SCORED repetitions -- may red the job. Unset,
# the default, it is computed and written into the verdict but cannot block:
# the 0.05 margin has never been measured against how much an unchanged pull
# request moves the aggregate on main, and arming a flat margin before the
# store can say is arming a guess. Set it to 1 in the Prow job config, not
# here, once the store holds enough nights to size it.
export EVAL_AGGREGATE_ARMED="${EVAL_AGGREGATE_ARMED:-}"

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

# Who admits a case: the roster below, or the evidence store. Decided
# 2026-09-14 on #1493: the roster stays hand-edited and the record informs.
#
#   roster  (default) BOOTSTRAP_ADMITTED decides, outright. The store's own
#                     verdict on each case -- would-admit, would-demote,
#                     collecting, stale, none -- rides in the per-case
#                     hand-off (record_verdict) and, once a store is
#                     configured, in the verdict's "Record says" column,
#                     so a roster edit cites it. Nothing the nightly appends
#                     changes which cases block.
#   record            The store decides once it holds a full window for a
#                     case at the current key (EVAL_ADMISSION_MIN_RUNS
#                     runs), either way; the list is the fallback until
#                     then. Kept for a later decision, not a schedule.
#
# Nobody should be able to move a case into or out of the blocking set
# without the eval crew knowing, and a roster edit reviewed in a pull
# request is that knowledge. Switching to `record` is a Prow-config change
# and a team decision, never a default here; docs/eval-gate-roster.md says
# what the record has to show first. Any other value stops the job here,
# before a task runs: `records` grading as `roster` would look like a
# working switch and switch nothing, and bench-gate refuses it too.
export EVAL_ADMISSION_MODE="${EVAL_ADMISSION_MODE:-roster}"
case "${EVAL_ADMISSION_MODE}" in
  roster | record) ;;
  *)
    echo "ERROR: EVAL_ADMISSION_MODE must be 'roster' or 'record', got '${EVAL_ADMISSION_MODE}'." >&2
    exit 1
    ;;
esac

# The blocking roster: cases named in hack/eval/blocking-roster.txt arm rung
# 4 -- three failed repetitions red the job -- and, while the store holds
# nothing for them at the current key, leave rung 6 quiet. Under
# EVAL_ADMISSION_MODE=roster that list is the whole answer to "which case can
# red a pull request on a graded failure"; the store's record beside it says
# whether the evidence agrees. The file is the default; an environment
# override still wins, for a laptop run, and is not checked against the
# presubmit the way the file is. Comma- or whitespace-separated task ids;
# bench-gate's _bootstrap_admitted() accepts either.
#
# The prose about this roster -- the admission bar, who is held out and on
# which issue, the rung scoping, the demotion protocol -- lives in
# docs/eval-gate-roster.md, deliberately: docs/ edits are inert to the eval
# (the Prow path filter and step 0 above both skip them), so a review
# finding against that prose no longer costs a 2-hour run (#1179). Edit the
# list in the file, the prose there. hack/OWNERS puts the file under the
# eval-crew alias (#1546).
#
# Demoting a flaky case is a one-line same-day edit: delete its name from
# the file, referencing the issue that names its re-admission condition
# and citing what the record says about it.
#
# A name that is not a presubmit case stops the job here: a misspelled entry
# would otherwise arm nothing and look like a working roster, and a nightly
# case cannot block a pull request it does not run on. A file that names
# nothing stops it too: under EVAL_ADMISSION_MODE=roster an empty roster
# disarms rung 4 for every pull request while the job reports green, so an
# intentionally empty roster is an explicit BOOTSTRAP_ADMITTED="" in the
# job's environment, never a file with only comments left in it.
BLOCKING_ROSTER_ENTRIES="$(roster_entries "${BLOCKING_ROSTER_FILE}")"
if [ -z "${BLOCKING_ROSTER_ENTRIES}" ]; then
  echo "ERROR: ${BLOCKING_ROSTER_FILE} names no case; an empty blocking roster would disarm rung 4 for every pull request. Set BOOTSTRAP_ADMITTED explicitly if that is the intent." >&2
  exit 1
fi
BLOCKING_ROSTER_DEFAULT=""
while IFS= read -r NAME; do
  if [ -z "${NAME}" ]; then continue; fi
  if ! grep -qxF -- "${NAME}" <<< "${PRESUBMIT_CASE_NAMES}"; then
    echo "ERROR: ${BLOCKING_ROSTER_FILE}: '${NAME}' is not a case in ${PRESUBMIT_CASES_FILE}; the blocking roster is a subset of the presubmit." >&2
    exit 1
  fi
  BLOCKING_ROSTER_DEFAULT="${BLOCKING_ROSTER_DEFAULT:+${BLOCKING_ROSTER_DEFAULT},}${NAME}"
done <<< "${BLOCKING_ROSTER_ENTRIES}"

export BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-${BLOCKING_ROSTER_DEFAULT}}"

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
# It defaults to unset because arming is a Prow-config decision, never a
# default here: a laptop run must not read, let alone write, the production
# store. Both Prow jobs export it since oss-test-infra#2698 (2026-09-14) --
# the nightly periodic (ci-kube-agents-eval-nightly, EVAL_TIER=nightly) as
# eval-baseline-recorder with objectViewer and objectCreator on
# gs://kube-agents-evals-bench, the presubmit as prowjob-default-sa with
# objectViewer only. Pointing at a bucket that is unreachable is not fatal --
# the store degrades to advisory with a banner -- but it is a banner on every
# run, which is how a revoked grant would announce itself.
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
    # The two tofu incumbents, nightly-only since #1218: ~20 and ~15 min a
    # repetition on the infra lock.
    gpu-stress-test-diagnosis | autoops-warning-event-triage) echo 900 ;;
    # The nightly-only full audits: 600-1300s a repetition on 2026-08-26,
    # planted-pdb's 962s the one clean measurement. Priced with the 900 band
    # so a nightly run launches them first. fleet-cost-idle-pool joined the
    # nightly 2026-09-15 unmeasured; same SOP-faithful audit shape, same band.
    obtainability-planted-pdb | stockout-pinned-pool) echo 900 ;;
    upgrade-readiness-lagging-cluster | consistency-drift-outlier) echo 900 ;;
    fleet-cost-idle-pool) echo 900 ;;
    compliance-rbac-overgrant | rca-remediation-pr) echo 700 ;;
    # Presubmit since 2026-09-22 (#1023), nightly-only before that. Measured
    # 980-1929s across build 2099539376672346112's three repetitions (267-559s
    # in August); median of the September run, kept although the four graded
    # nights of 09-16 to 09-20 ran 420-1153s: the presubmit shares the model
    # quota with the daytime herd, which is where the September run was.
    pdb-remediation-pr) echo 1250 ;;
    # Nightly-only. The audit measured 1415-1488s a repetition with its ledger
    # write (build 2099607409826729984); the crashloop triage takes the
    # incumbent autoops hint (same watcher and card waits) until it passes.
    ai-security-planted-model-audit) echo 1450 ;;
    autoops-crashloop-config-triage) echo 900 ;;
    consistency-authorized-networks-probe) echo 300 ;;
    # Median of its 1155 presubmit repetitions 2026-09-04 to 09-15 (p10 248s,
    # p90 1318s); the 200s default under-packed it by 2.7x (#1023).
    capacity-pinned-pool-probe) echo 540 ;;
    # Nightly-only since 2026-09-09. Median of its first three measured
    # repetitions (615/715/166s, build 2097362391401500672); the 200s default
    # under-packs it by 3x.
    knowledge-grounding-sources-probe) echo 600 ;;
    # Presubmit since 2026-09-22 (#1023), nightly-only from 2026-09-15 before
    # that. Median of its three measured presubmit repetitions (737/599/1357s,
    # build 2099969322708373504); the 200s default under-packs it by 3x. The
    # four graded nights ran 529-2808s a repetition at parallelism 6 beside
    # the tofu cases; the hint stays at the presubmit measurement until the
    # presubmit record says otherwise.
    incident-triage-oom-event-probe) echo 700 ;;
    *) echo 200 ;;
  esac
}

# The harness's delegation ceiling for one unit, in seconds: how long
# devops-bench keeps polling the Platform Agent for a delegated worker before
# it grades whatever the parent has said so far. Every unit inherits the
# global AGENT_DELEGATION_TIMEOUT exported in section 3 (2700s); the six
# full-audit units -- SOP dispatch, a delegated worker sweeping the fleet,
# a ledger write, one closing line -- get 3000s.
#
# Why more than 2700 (#1683). The nightly of 2026-09-16 (build
# 2100374258805903360) cut three audits at the ceiling after their work was
# done: compliance-rbac-overgrant rep 1's worker rewrote its ledger 2520s
# after launch and the harness gave up 192s later, while the same case's
# passing rep needed 160s between its ledger write and its delivered answer;
# upgrade-readiness-lagging-cluster rep 1 timed out 30s after its ledger was
# created; fleet-cost-idle-pool rep 1 ran 2739s. The audit's own wall clock
# at p90 is ~35 minutes (2074s over 903 presubmit repetitions, 2026-09-04 to
# 09-15), so a run at p90 fits under 2700s -- the reps that hit the ceiling
# are the tail beyond it, whose true length the graded durations cannot show
# because they are cut there. The variance is still #985's problem.
#
# Why not more than 3000. The ledger read token is minted just before
# devops-bench starts (mint_ledger_token, below) and lives one hour, and
# ledger_issue_contains reads GitHub with it only after the agent turn, the
# delegation wait and the settle. The delegation clock starts after the
# opening turn, so everything outside it -- startup, port-forward, the
# opening turn, settle, the verifier's own GET -- has to fit in the hour
# minus this ceiling. 3000 leaves 600s for that; 3600 left nothing, and a
# worker that delivered its URL in the last minutes would have handed the
# verifier an expired token and a rung-2 "checks errored" red on a run that
# had done its work. 300s over 2700 covers all three cut reps above with
# margin. The task lock a later repetition waits behind is sized from this
# ceiling (run_one_unit), so a unit that uses all of it cannot make its
# successor give up.
unit_delegation_timeout() {
  case "$1" in
    compliance-rbac-overgrant | obtainability-planted-pdb | stockout-pinned-pool) echo 3000 ;;
    upgrade-readiness-lagging-cluster | consistency-drift-outlier | fleet-cost-idle-pool) echo 3000 ;;
    *) echo "${AGENT_DELEGATION_TIMEOUT:-1800}" ;;
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

# How long a stack-bearing unit waits for lock-infra before giving up. The
# lock is held for a unit's whole invocation and the queue launches every
# repetition-1 unit within the first few lanes, so with N stack-bearing
# tasks the last waiter has to outlast N-1 holders in a row: at 1800s flat
# the third and fourth contenders in a four-tofu nightly cannot, and which
# one loses is the mkdir race (the #1103 presubmit run showed it at N=2, a
# 2279s audit rep starving its sibling). 1800s a contender keeps the
# holder-died guard the flat figure was for, scaled to the matrix; the
# presubmit, with no stack-bearing task, keeps the flat 1800s.
STACK_CONTENDERS=0
for HAS in "${TASK_HAS_STACK[@]}"; do [ -n "${HAS}" ] && STACK_CONTENDERS=$((STACK_CONTENDERS + 1)); done
INFRA_LOCK_DEADLINE=$(( 1800 * (STACK_CONTENDERS > 1 ? STACK_CONTENDERS : 1) ))
echo "Infra lock: ${STACK_CONTENDERS} stack-bearing task(s); a unit waits up to ${INFRA_LOCK_DEADLINE}s for lock-infra"

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
  # The task lock is held for the holder's whole unit, so the wait must
  # outlast one: the unit's delegation ceiling plus grading and teardown
  # (about 300s on the record; 600s here). A fixed 1800s deadline under a
  # 3000s ceiling would make a same-task successor give up while its
  # predecessor was still legitimately running -- 24% of presubmit runs
  # launch compliance rep 2 within 2090s of rep 1 (385 logs, 09-04 to
  # 09-15). The infra lock keeps its default: audit units carry no stack.
  if ! lock_acquire "${STATE_DIR}/lock-task-${name}" \
    "$(($(unit_delegation_timeout "${name}") + 600))"; then
    echo "<<< [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] ${name} rep ${rep} gave up on its task lock" >&2
    return 0
  fi
  if [ -n "${has_stack}" ] && ! lock_acquire "${STATE_DIR}/lock-infra" "${INFRA_LOCK_DEADLINE}"; then
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
  # Per unit, inside this subshell, so the audit units' longer ceiling never
  # leaks to a sibling lane; see unit_delegation_timeout.
  AGENT_DELEGATION_TIMEOUT="$(unit_delegation_timeout "${name}")"
  export AGENT_DELEGATION_TIMEOUT
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
# whole 20-run admission window in seven nights at the default three
# repetitions after a version-key bump, fewer once the count is raised on
# measured wall clock. Neither job type is a pull request, which
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
  # The commit each line is stamped with. A postsubmit carries it as
  # PULL_BASE_SHA; a periodic carries neither that nor PULL_PULL_SHA (Prow's
  # EnvForSpec returns before setting them for JOB_TYPE=periodic), so
  # `bench-gate record`'s own default would leave the nightly's evidence
  # unattributed. extra_refs has checked out main's head, so HEAD is the
  # commit the run measured.
  EVAL_RECORD_COMMIT="${PULL_BASE_SHA:-$(git -C "${SCRIPT_DIR}/.." rev-parse HEAD 2>/dev/null || true)}"
  # Never fatal. Bookkeeping must not be the reason a merge to main reds.
  (cd "${BENCH_DIR}" && uv run bench-gate record \
    "${CASE_RESULTS[@]}" \
    ${EVAL_RECORD_COMMIT:+--commit "${EVAL_RECORD_COMMIT}"} \
    --lines-out "${ARTIFACT_DIR}/baseline-append.jsonl") || \
    echo "WARNING: recording baseline evidence failed; the verdict below is unaffected."
elif [ -n "${RC_COMMIT_SHA:-}" ]; then
  echo "Release-candidate run (RC_COMMIT_SHA=${RC_COMMIT_SHA}): the baseline store is read, never written — the candidate is judged against main's window, not added to it."
else
  echo "Not a main-branch recorder run (JOB_TYPE=${JOB_TYPE:-unset}): the baseline store is read, never written."
fi

# The suite roll-up: blocking cases, the admitted-case aggregate, the
# per-admitted-case coverage floor and the all-infrastructure check. Exit 0
# green, 1 red, 2 not evaluated -- an admitted case lost every repetition to
# infrastructure (or every case did), so the run cannot certify green and has
# nothing against the change to debug either. Prow reds 2 as it reds 1, which
# is right: a run that proved nothing does not merge. The distinct status and
# the `outcome` in eval-verdict.json are for the artifact and for the
# release-candidate lane, which reports NOT RUN rather than RED on them. The
# dashboard and the health bot do not read either yet: they classify this
# run from the final line's `Failed` word and from Prow's FAILURE, so until
# #1782 they still call it red; the banner in eval-verdict.md is what says
# otherwise. --baseline-rate is not passed: the rate is computed from the
# store, per admitted case at its own version key. While the store holds
# nothing, and until EVAL_AGGREGATE_ARMED is set to 1, the aggregate stays
# advisory and the markdown says so, rather than implying a comparison that
# did not happen or a rule that was armed.
#
# A function, so tests/test_ci_eval_verdict.py can lift it out of this file
# and run it: the status-2 confirmation below is the one branch of the verdict
# the bench tests cannot reach, and the shell's copy of the outcome word has
# to keep agreeing with scoring.py's. Prints the final line; returns the
# status the job exits with.
announce_suite_verdict() {
  local suite_status=$1 verdict_json=$2 verdict_md=$3 total_duration=$4
  # The status alone does not prove a not-evaluated verdict: argparse exits 2
  # on a bad flag, and `uv run` can exit 2 without ever reaching bench-gate.
  # Only a verdict file that says so is announced as one; anything else that
  # is not 0 is the plain failure it always was, so a broken invocation
  # cannot dress itself as weather.
  local not_evaluated="false"
  if [ "${suite_status}" -eq "${EVAL_SUITE_NOT_EVALUATED_STATUS}" ] && \
    python3 -c 'import json, sys; sys.exit(0 if json.load(open(sys.argv[1])).get("outcome") == sys.argv[2] else 1)' \
      "${verdict_json}" "${EVAL_VERDICT_OUTCOME_NOT_EVALUATED}" 2>/dev/null; then
    not_evaluated="true"
  fi
  # The final line keeps the `PR Smoke Test Evaluation Failed` and
  # `(Total Duration: Ns)` anchors that scripts/eval_dashboard/collect.py
  # matches, so a not-evaluated run does not lose its final line on the
  # dashboard; the classification words sit between them.
  if [ "${suite_status}" -eq 0 ]; then
    echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Succeeded (Total Duration: ${total_duration}s) ==="
    return 0
  fi
  if [ "${not_evaluated}" = "true" ]; then
    echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed -- NOT EVALUATED: an admitted case (or every case) lost every repetition to infrastructure, so this run cannot certify green. Not a finding against the change: rerun when the environment is healthy rather than debugging it. See ${verdict_md} (Total Duration: ${total_duration}s)"
    return "${EVAL_SUITE_NOT_EVALUATED_STATUS}"
  fi
  echo "❌ [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] PR Smoke Test Evaluation Failed -- see ${verdict_md} (Total Duration: ${total_duration}s)"
  return 1
}

TOTAL_DURATION=$((SECONDS - START_TIME))
SUITE_STATUS=0
(cd "${BENCH_DIR}" && uv run bench-gate suite \
  "${CASE_RESULTS[@]}" \
  --markdown-out "${ARTIFACT_DIR}/eval-verdict.md" \
  --json-out "${ARTIFACT_DIR}/eval-verdict.json") || SUITE_STATUS=$?
announce_suite_verdict "${SUITE_STATUS}" "${ARTIFACT_DIR}/eval-verdict.json" \
  "${ARTIFACT_DIR}/eval-verdict.md" "${TOTAL_DURATION}" || exit $?
