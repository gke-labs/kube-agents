#!/usr/bin/env bash
# ==============================================================================
# Step 0 of the smoke-test presubmit: reuse a pull request's own green verdict
# ==============================================================================
# Exits 0 when every pull request this job is testing already holds a green
# verdict this run could only repeat, and 1 (one "Step 0: full run:" line
# naming the reason) when it does not. The Prow job runs this BEFORE it
# leases an evaluation project, so a reused verdict costs a pod start and a
# clone rather than the lease, the image build, the deploy and the ~2h eval
# matrix; hack/ci-eval-pr.sh runs it again as its own first step for a job
# definition that has not yet hoisted it, where it saves the matrix alone.
#
# Two things count as a verdict this run could only repeat. The first is a
# green build at THIS head (#1202): Tide credits a presubmit only against the
# base SHA it ran on, so every merge to main retests, or batches, every other
# green pull request whose head has not changed -- over the three weeks to
# 2026-10-05 that was 150 Tide-started runs against 831 merges, 34 of them on
# a pull request that had already merged, and batches held the pool for 53
# hours in all with nothing merging while one was in flight. A head that
# passed once passes. That is a trade, decided with the numbers above, not
# an equivalence: hack/ci-deploy.sh builds the image from the checkout Prow
# hands it, which is the pull request merged onto the current main, so a
# retest at a new base WOULD test a different combination, and reusing the
# head's verdict means that combination -- and, in a batch, the pulls with
# each other -- is not tested before the merge, on every retest rather than
# only when the sticky re-pin wins its race. The nightly eval on main is
# what finds a combination that broke, and so is the next full run any pull
# request starts after it. The second is a green at an EARLIER head from
# which everything since -- on the pull request's side AND on main's side
# -- matches the inert list below (#1179): a push that changes only inert
# files re-runs the whole job and aborts the run in flight, and Prow's
# skip_if_only_changed cannot help because it sees the whole diff against
# the base, not the delta since the last green build. That rule keeps its
# original contract, main's side included, although the first rule would
# excuse it: it predates the first and answers a push, which is the
# author's act, where the first answers a retest nobody asked for. Widening
# it is a separate decision with its own record, not a consequence of this
# one.
#
# A batch job (Tide testing several pull requests merged together) is
# revalidated pull by pull: every one must hold a reusable verdict, else the
# batch runs in full. PULL_REFS carries the batch's pulls; a serial presubmit
# names its one pull in PULL_NUMBER and PULL_PULL_SHA.
#
# FAIL-CLOSED THROUGHOUT: every doubt -- no history, unreadable GCS, an
# unparsable record, a commit the checkout does not have, any file escaping
# the inert list, one pull of a batch without a verdict -- is one log line
# and a full run. The first run on a pull request has no green history, so
# it is always a full run. EVAL_SKIP_REVALIDATION=1 is the escape hatch: it
# forces a full run for debugging a suspect reuse.
#
# One asymmetry is deliberate: the NEWEST GREEN wins -- the newest at this
# head when there is one, else the newest at any -- so a newer red full run
# at the same head, or at inert distance from an older green, is overridden
# on the next trigger. For an inert delta that is the same judgement a
# passing /retest would render -- the delta cannot feed the eval differently,
# so the red was flake or infrastructure by construction. For the same head
# it is not: a full run that reached the matrix at a newer base (a step-0
# fall-through, or the escape hatch) can red on the combination the reuse
# does not test, and the rule still prefers the older green because the rule
# is that the head passed once. Read a same-head red as either, not as
# noise; reproducing one needs a non-inert push or EVAL_SKIP_REVALIDATION=1
# in the job env.
#
# Trust surface. For the SELF case, subsumption: a pull request that wants
# its own context green can already edit this script to `exit 0` -- its own
# code IS the job -- and a pull request that edits the revalidation logic
# touches hack/, which is not inert, so its own run goes full. That argument
# does NOT cover the CROSS-PR case: the job history under gs://kube-agents-prow
# is written by pod utilities that may share the test container's identity,
# so a hostile pull request's run could conceivably plant a fabricated
# "green" record under a VICTIM pull request's history path (kube-agents-bot's
# review of #1186 built the full attack). The GCS records are therefore never
# trusted alone: a candidate green build counts only when GitHub holds a
# SUCCESS status event for this job's context on the recovered head whose
# target URL names that same build id. Statuses are posted by Prow's reporter
# with repository write permission -- google-oss-prow[bot] -- which no pull
# request holds, and the events are append-only per build (a later aborted or
# pending run does not erase an earlier build's success event; verified
# against #1127's head 50e0f44f). The SHAs are also required to be 40-hex
# before any git command sees them, so a forged record cannot smuggle
# arguments.
#
# GitHub read credential. The attestation read is made with a one-hour token
# minted from the ledger-reader App's key when EVAL_LEDGER_APP_KEY_FILE is
# set -- hack/ledger_token_mint.py, the mint hack/ci-eval-pr.sh uses for
# grading, narrowed here to metadata: read, which is enough to list a public
# repository's commit statuses (probed 2026-10-06 against a public repository
# outside the App's installation: HTTP 200, on the App's own 15,000/h limit
# where an anonymous read shares the host's 60/h). A mint that fails is a
# full run: no anonymous second attempt, and never the PAT the job also
# mounts, which the harness itself stopped reading once the App key was
# there. Without a key file the read carries BENCH_GITHUB_TOKEN when the
# shell holds one, else it is anonymous, and one log line says which. The
# token reaches python through its environment, never argv, so ps does not
# show it.
#
# Downstream note: a revalidated run's build log carries no per-task result
# lines and no final-verdict line. scripts/eval_dashboard/collect.py already
# tolerates that shape -- aborted runs produce taskless builds today -- and
# keys nothing on this job exiting through its normal tail; classify.py reads
# a zero-task SUCCESS as a reused verdict.
#
# Sourceable: tests/test_ci_eval_revalidation.py sources this file into a
# fixture checkout and calls revalidate_against_green_history directly, so
# the entrypoint at the bottom runs only when the file is executed.

set -euo pipefail

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
# The job whose history and status context step 0 reads: the running job's
# own name, which Prow exports as JOB_NAME. Every presubmit that runs this
# script has its own history path and its own status context, so keying the
# reuse on a fixed name would let a second job (the next-mode lane, which
# runs step 0 too, under EVAL_MODE_NEXT=1) find the today job's green build
# at the same head and run nothing. Outside Prow the default keeps the log
# lines and the tests naming the job that exists.
readonly REVALIDATION_DEFAULT_JOB_NAME="pull-kube-agents-smoke-test"
readonly REVALIDATION_JOB_NAME="${JOB_NAME:-${REVALIDATION_DEFAULT_JOB_NAME}}"
readonly REVALIDATION_SPYGLASS_PREFIX="https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents"
# The started.json repos key naming this repository's clone record, and the
# base ref assumed when the decoration did not export PULL_BASE_REF.
readonly REVALIDATION_REPO_KEY="gke-labs/kube-agents"
readonly REVALIDATION_DEFAULT_BASE_REF="main"
# Where the Prow-posted status events live: the attestation that a claimed
# green build really ran and really passed (see the trust-surface note
# above). Read with the credential revalidation_read_credential chooses
# (header, "GitHub read credential").
readonly REVALIDATION_STATUS_API="https://api.github.com/repos/gke-labs/kube-agents/commits"
# How many of the newest builds to inspect for a green one. Each costs one
# gsutil cat (~1s); an active PR rarely stacks this many pushes between
# greens, and a bound keeps the fall-through path seconds long.
readonly REVALIDATION_HISTORY_LIMIT=20
# What Prow exports as JOB_TYPE for a Tide batch, whose pulls arrive in
# PULL_REFS as "<base_ref>:<base_sha>,<number>:<sha>[,...]" (each pull entry
# may carry a third ":<ref>" field) with no PULL_NUMBER or PULL_PULL_SHA.
readonly REVALIDATION_BATCH_JOB_TYPE="batch"
# The shapes a SHA and a pull number are held to before anything reads them,
# matched with [[ =~ ]] against the whole value: a `grep -q` on the value would
# pass one whose first line is well-formed and whose second is anything.
readonly REVALIDATION_SHA_RE='^[0-9a-f]{40}$'
readonly REVALIDATION_NUMBER_RE='^[0-9]+$'
# The mint behind the read credential (header, "GitHub read credential"):
# the harness's own, beside this file. Its one argument is the exit code it
# uses for a failure another attempt could survive; step 0 does not retry,
# so a transient and a credential fault are the same full run, and the code
# is passed only because the module's contract asks for one. The body is the
# narrowest the mint accepts, and all a public repository's statuses need.
REVALIDATION_MINT_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ledger_token_mint.py"
readonly REVALIDATION_MINT_SCRIPT
readonly REVALIDATION_MINT_BODY='{"permissions":{"metadata":"read"}}'
readonly REVALIDATION_MINT_RETRYABLE=75
readonly REVALIDATION_STATUS_TIMEOUT_SECONDS=30
readonly REVALIDATION_USER_AGENT="kube-agents-ci-revalidate"

_revalidation_print_delta() { # <label> <range> <files-or-empty>
  echo "${1} (${2}):"
  if [ -n "${3}" ]; then
    printf '%s\n' "${3}" | sed 's/^/    /'
  else
    echo "    (empty -- identical trees, trivially inert)"
  fi
}

# Chooses the credential the status reads carry, says which in the log, and
# leaves it in REVALIDATION_STATUS_TOKEN (empty for an anonymous read). Once
# per run, ahead of the pulls, so a batch mints once. Returns 1 when a key
# file is set and the mint fails: that is a full run, not an anonymous
# retry and not the mounted PAT -- the mint's own message precedes the
# reason line. Header, "GitHub read credential".
revalidation_read_credential() {
  REVALIDATION_STATUS_TOKEN=""
  if [ -n "${EVAL_LEDGER_APP_KEY_FILE:-}" ]; then
    local minted
    if ! minted="$(LEDGER_MINT_BODY="${REVALIDATION_MINT_BODY}" python3 "${REVALIDATION_MINT_SCRIPT}" "${REVALIDATION_MINT_RETRYABLE}")"; then
      echo "Step 0: full run: could not mint a GitHub read token from the App key at ${EVAL_LEDGER_APP_KEY_FILE} (the mint's message is above); not reading anonymously and not reading the mounted PAT"
      return 1
    fi
    REVALIDATION_STATUS_TOKEN="${minted%% *}"
    echo "Step 0: GitHub statuses read with a token minted from the App key at ${EVAL_LEDGER_APP_KEY_FILE}, narrowed to ${REVALIDATION_MINT_BODY}, expires ${minted##* }"
  elif [ -n "${BENCH_GITHUB_TOKEN:-}" ]; then
    REVALIDATION_STATUS_TOKEN="${BENCH_GITHUB_TOKEN}"
    echo "Step 0: GitHub statuses read with the BENCH_GITHUB_TOKEN this shell holds (no EVAL_LEDGER_APP_KEY_FILE to mint from)"
  else
    echo "Step 0: GitHub statuses read anonymously: neither EVAL_LEDGER_APP_KEY_FILE nor BENCH_GITHUB_TOKEN is set, so the read shares this host's anonymous rate limit"
  fi
  return 0
}

# revalidate_one_pull <number> <head-sha> <base-sha>
# Returns 0 when the pull request's own green history holds a verdict this
# run could only repeat, 1 for a full run. Every fall-through path logs
# exactly one "Step 0: full run:" line naming its reason. A reuse logs the
# pull's record and appends "<build> (PR #n)" to REVALIDATION_REUSED; the
# job-level REVALIDATED banner is the caller's, printed only once every pull
# has one -- a batch whose later pull falls through must not carry a line
# saying the matrix was skipped.
revalidate_one_pull() {
  local pull_number="$1" cur_head="$2" cur_base="$3"
  local repo_dir
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

  local history_dir="${REVALIDATION_HISTORY_PREFIX}/${pull_number}/${REVALIDATION_JOB_NAME}"
  local listing
  if ! listing="$(gsutil ls "${history_dir}/*/finished.json" 2>/dev/null)"; then
    echo "Step 0: full run: no finished ${REVALIDATION_JOB_NAME} build for PR #${pull_number} (first run on this PR, or GCS unreadable)"
    return 1
  fi

  # Newest first: build IDs are numeric and monotonically increasing.
  local candidates
  candidates="$(printf '%s\n' "${listing}" | sed -n 's|.*/\([0-9][0-9]*\)/finished\.json$|\1|p' | sort -rn | head -n "${REVALIDATION_HISTORY_LIMIT}")"
  if [ -z "${candidates}" ]; then
    echo "Step 0: full run: the job history listing for PR #${pull_number} held no parseable build ids"
    return 1
  fi

  # The green to reuse: the newest one AT THIS HEAD if any of the candidates
  # is, else the newest green. A head that passed once passes even when a
  # newer green at another head sits in front of it (a force-push back to an
  # earlier head), so the scan does not stop at the first green unless it is
  # this head's. finished.json's revision is the head a build ran at, and is
  # held to started.json's below for whichever build is chosen.
  # One parse per candidate: it yields "passed <revision>" or nothing, and the
  # chosen build's revision is kept for the started.json check below. The
  # scan stops only at a same-head green, so every other path reads all of
  # REVALIDATION_HISTORY_LIMIT finished.json records -- a second each, ahead
  # of a run that then spends minutes (an inert reuse) or hours (the matrix).
  local build prev_green="" finished_revision="" candidate_parsed candidate_revision
  while read -r build; do
    [ -n "${build}" ] || continue
    candidate_parsed="$(gsutil cat "${history_dir}/${build}/finished.json" 2>/dev/null | python3 -c '
import json
import sys

record = json.load(sys.stdin)
if record.get("passed") is True:
    print("passed", record.get("revision") or "")
' 2>/dev/null)" || candidate_parsed=""
    [ "${candidate_parsed%% *}" = "passed" ] || continue
    candidate_revision="${candidate_parsed#passed}"
    candidate_revision="${candidate_revision# }"
    if [ -z "${prev_green}" ]; then
      prev_green="${build}"
      finished_revision="${candidate_revision}"
    fi
    if [ "${candidate_revision}" = "${cur_head}" ]; then
      prev_green="${build}"
      finished_revision="${candidate_revision}"
      break
    fi
  done <<EOF_REVALIDATION_CANDIDATES
${candidates}
EOF_REVALIDATION_CANDIDATES
  if [ -z "${prev_green}" ]; then
    echo "Step 0: full run: no green build among the newest ${REVALIDATION_HISTORY_LIMIT} ${REVALIDATION_JOB_NAME} builds for PR #${pull_number}"
    return 1
  fi

  # That build's head and base SHAs, from its started.json clone record:
  # repos["gke-labs/kube-agents"] reads "main:<base_sha>,<pr>:<head_sha>",
  # the same Refs.String() shape as PULL_REFS, so a pull entry may carry a
  # third ":<ref>" field; only the first two are read.
  local started shas prev_base prev_head
  if ! started="$(gsutil cat "${history_dir}/${prev_green}/started.json" 2>/dev/null)"; then
    echo "Step 0: full run: green build ${prev_green} of PR #${pull_number} has no readable started.json"
    return 1
  fi
  if ! shas="$(printf '%s' "${started}" | python3 -c '
import json
import sys

base_ref, pull, repo_key = sys.argv[1], sys.argv[2], sys.argv[3]
refs = json.load(sys.stdin)["repos"][repo_key]
parts = {}
for part in refs.split(","):
    fields = part.split(":")
    if len(fields) >= 2:
        parts[fields[0]] = fields[1]
base, head = parts.get(base_ref), parts.get(pull)
if not base or not head:
    raise SystemExit(1)
print(base, head)
' "${PULL_BASE_REF:-${REVALIDATION_DEFAULT_BASE_REF}}" "${pull_number}" "${REVALIDATION_REPO_KEY}" 2>/dev/null)"; then
    echo "Step 0: full run: could not recover base/head SHAs from green build ${prev_green}'s started.json (PR #${pull_number})"
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
    if ! [[ "${sha}" =~ ${REVALIDATION_SHA_RE} ]]; then
      echo "Step 0: full run: build ${prev_green}'s started.json holds a malformed SHA (PR #${pull_number})"
      return 1
    fi
  done
  if [ "${finished_revision}" != "${prev_head}" ]; then
    echo "Step 0: full run: build ${prev_green}'s finished.json revision (${finished_revision:-unreadable}) does not match its started.json head (${prev_head})"
    return 1
  fi
  # The read carries the credential revalidation_read_credential chose,
  # handed to python through its environment so it is on no argv. One
  # attempt: a read GitHub refuses names its HTTP code and is a full run,
  # with no second, anonymous try (header, "GitHub read credential").
  local attested
  attested="$(REVALIDATION_STATUS_TOKEN="${REVALIDATION_STATUS_TOKEN:-}" python3 -c '
import json
import os
import sys
import urllib.error
import urllib.request

url, context, build, timeout, agent = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
headers = {"Accept": "application/vnd.github+json", "User-Agent": agent}
token = os.environ.get("REVALIDATION_STATUS_TOKEN")
if token:
    headers["Authorization"] = "Bearer " + token
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        statuses = json.load(response)
except urllib.error.HTTPError as exc:
    print("http", exc.code)
    sys.exit(0)
except Exception as exc:
    print("error", type(exc).__name__)
    sys.exit(0)
needle = "/" + context + "/" + build
for status in statuses:
    if (
        status.get("context") == context
        and status.get("state") == "success"
        and needle in (status.get("target_url") or "")
    ):
        print("attested")
        sys.exit(0)
print("absent")
' "${REVALIDATION_STATUS_API}/${prev_head}/statuses?per_page=100" "${REVALIDATION_JOB_NAME}" "${prev_green}" "${REVALIDATION_STATUS_TIMEOUT_SECONDS}" "${REVALIDATION_USER_AGENT}" 2>/dev/null)" || attested="error python3"
  case "${attested}" in
    attested) ;;
    absent)
      echo "Step 0: full run: GitHub holds no ${REVALIDATION_JOB_NAME} success status on ${prev_head} naming build ${prev_green} -- refusing to trust the GCS record alone"
      return 1 ;;
    http\ *)
      echo "Step 0: full run: GitHub answered HTTP ${attested#http } reading statuses for ${prev_head} to attest green build ${prev_green}; not retrying anonymously"
      return 1 ;;
    *)
      echo "Step 0: full run: could not read GitHub statuses for ${prev_head} to attest green build ${prev_green} (${attested#error })"
      return 1 ;;
  esac

  # The head itself has passed: the first kind of reusable verdict. The
  # base is not compared -- a retest Tide starts because main moved, serial
  # or batch, is exactly this case, and the header says what that trades
  # away. No git work is needed: the attested record names the head, and
  # the head is this run's.
  if [ "${prev_head}" = "${cur_head}" ]; then
    echo "PR #${pull_number} holds a reusable verdict: green build ${prev_green} -- this head already passed"
    echo "Reused verdict: ${REVALIDATION_SPYGLASS_PREFIX}/${pull_number}/${REVALIDATION_JOB_NAME}/${prev_green}"
    echo "Attested by the Prow-posted ${REVALIDATION_JOB_NAME} success status on ${prev_head}"
    echo "Same head ${cur_head}; base ${prev_base} then, ${cur_base} now -- a head that passed once passes"
    REVALIDATION_REUSED+=("${prev_green} (PR #${pull_number})")
    return 0
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
  if ! head_delta="$(git -C "${repo_dir}" diff --no-renames --name-only "${prev_head}" "${cur_head}" 2>/dev/null)"; then
    echo "Step 0: full run: git diff ${prev_head}..${cur_head} failed"
    return 1
  fi
  if ! base_delta="$(git -C "${repo_dir}" diff --no-renames --name-only "${prev_base}" "${cur_base}" 2>/dev/null)"; then
    echo "Step 0: full run: git diff ${prev_base}..${cur_base} failed"
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

  echo "PR #${pull_number} holds a reusable verdict: green build ${prev_green} -- every change since is inert"
  echo "Reused verdict: ${REVALIDATION_SPYGLASS_PREFIX}/${pull_number}/${REVALIDATION_JOB_NAME}/${prev_green}"
  echo "Attested by the Prow-posted ${REVALIDATION_JOB_NAME} success status on ${prev_head}"
  _revalidation_print_delta "head delta" "${prev_head}..${cur_head}" "${head_delta}"
  _revalidation_print_delta "base delta" "${prev_base}..${cur_base}" "${base_delta}"
  echo "Predicate: every file above matches REVALIDATION_INERT_PATHS ${REVALIDATION_INERT_PATHS}"
  REVALIDATION_REUSED+=("${prev_green} (PR #${pull_number})")
  return 0
}

# Returns 0 when every pull request this job tests holds a reusable verdict
# (caller exits 0) and 1 for a full run.
revalidate_against_green_history() {
  if [ "${EVAL_SKIP_REVALIDATION:-}" = "1" ]; then
    echo "Step 0: full run: EVAL_SKIP_REVALIDATION=1 (escape hatch)"
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

  # The pulls to revalidate, one "<number> <head-sha>" per line. JOB_TYPE
  # decides which shape is read: a batch's pulls are PULL_REFS and nothing
  # else, so a PULL_NUMBER beside them (not Prow's doing, but an operator
  # shell's) cannot narrow a batch to one pull; a serial presubmit names its
  # one pull in PULL_NUMBER and PULL_PULL_SHA.
  # Prow's decoration exports 40-hex SHAs. The ones this run was handed are
  # held to that shape before any of them reaches gsutil or git, as the
  # recovered ones are: a value shaped like an option would otherwise reach
  # `git diff` as one, and an empty file list reads as an inert delta.
  if [ -n "${PULL_BASE_SHA:-}" ] && ! [[ "${PULL_BASE_SHA}" =~ ${REVALIDATION_SHA_RE} ]]; then
    echo "Step 0: full run: PULL_BASE_SHA is not a 40-hex SHA (${PULL_BASE_SHA})"
    return 1
  fi
  local pulls=""
  if [ "${JOB_TYPE:-}" = "${REVALIDATION_BATCH_JOB_TYPE}" ]; then
    if [ -z "${PULL_REFS:-}" ] || [ -z "${PULL_BASE_SHA:-}" ]; then
      echo "Step 0: full run: a batch job with PULL_REFS or PULL_BASE_SHA unset"
      return 1
    fi
    # The first entry must be the base, "<base_ref>:<PULL_BASE_SHA>" -- held
    # to that, not assumed, so a pull in that slot is refused rather than
    # dropped unread -- and every entry after it "<number>:<40-hex>[:<ref>]";
    # the SHAs are checked here, before anything reads them, for the same
    # reason the recovered ones are. Anything else is a full run, not a guess.
    if ! pulls="$(printf '%s' "${PULL_REFS}" | python3 -c '
import re
import sys

base_ref, base_sha = sys.argv[1], sys.argv[2]
entries = sys.stdin.read().split(",")
head = entries[0].split(":")
if len(head) != 2 or head[0] != base_ref or head[1] != base_sha or len(entries) < 2:
    raise SystemExit(1)
for entry in entries[1:]:
    fields = entry.split(":")
    if len(fields) < 2 or not re.fullmatch(r"[0-9]+", fields[0]) or not re.fullmatch(r"[0-9a-f]{40}", fields[1]):
        raise SystemExit(1)
    print(fields[0], fields[1])
' "${PULL_BASE_REF:-${REVALIDATION_DEFAULT_BASE_REF}}" "${PULL_BASE_SHA}" 2>/dev/null)"; then
      echo "Step 0: full run: PULL_REFS is not <base_ref>:<PULL_BASE_SHA> followed by the batch's pulls as <number>:<sha> entries (${PULL_REFS})"
      return 1
    fi
    echo "Step 0: batch of $(printf '%s\n' "${pulls}" | wc -l | tr -d ' ') pull requests; every one must hold a reusable verdict"
  elif [ -n "${PULL_NUMBER:-}" ] && [ -n "${PULL_PULL_SHA:-}" ] && [ -n "${PULL_BASE_SHA:-}" ]; then
    if ! [[ "${PULL_NUMBER}" =~ ${REVALIDATION_NUMBER_RE} ]] || ! [[ "${PULL_PULL_SHA}" =~ ${REVALIDATION_SHA_RE} ]]; then
      echo "Step 0: full run: PULL_NUMBER or PULL_PULL_SHA is not <number> and <40-hex SHA> (${PULL_NUMBER}, ${PULL_PULL_SHA})"
      return 1
    fi
    pulls="${PULL_NUMBER} ${PULL_PULL_SHA}"
  else
    echo "Step 0: full run: not a decorated Prow presubmit or batch (PULL_NUMBER, PULL_PULL_SHA or PULL_BASE_SHA unset, and JOB_TYPE is not batch)"
    return 1
  fi

  revalidation_read_credential || return 1

  REVALIDATION_REUSED=()
  local number head
  while read -r number head; do
    [ -n "${number}" ] || continue
    revalidate_one_pull "${number}" "${head}" "${PULL_BASE_SHA}" || return 1
  done <<EOF_REVALIDATION_PULLS
${pulls}
EOF_REVALIDATION_PULLS
  # The one line humans grep for and a collector may key on, so it appears
  # only when every pull holds a verdict and the exit is 0. Serial: one
  # build. Batch: one per pull, in PULL_REFS order.
  local reused
  reused="$(printf '%s, ' "${REVALIDATION_REUSED[@]}")"
  reused="${reused%, }"
  if [ "${#REVALIDATION_REUSED[@]}" -eq 1 ]; then
    reused="${reused%% (PR #*}"
  fi
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 0: REVALIDATED against green build ${reused} -- skipping the eval matrix ==="
  return 0
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if revalidate_against_green_history; then
    exit 0
  fi
  exit 1
fi
