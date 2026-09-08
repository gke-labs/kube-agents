#!/usr/bin/env bash
# ==============================================================================
# Release-candidate eval (the periodic job's entrypoint)
# ==============================================================================
# Resolve the newest release candidate, check it out, deploy its published
# images, and evaluate them. Non-gating: the verdict is reported, and nothing
# here can hold up a release. How wide that evaluation is depends on a tier
# switch that has not landed yet -- see RC_EVAL_TIER below, which is the one
# place in this file describing something the repository does not have.
#
# The four steps, and why they need a driver at all:
#
#   1. hack/resolve-rc-target.sh   -> the candidate's commit SHA
#   2. git checkout --detach       -> the tree becomes the candidate's
#   3. hack/ci-deploy.sh           -> installs the candidate's published images
#   4. hack/ci-eval-pr.sh          -> runs the catalog against them
#
# Step 2 needs a caller that outlives it. Bash reads a script incrementally as
# it executes and keeps a byte offset into the file, so a script that rewrites
# its own file mid-run can resume at that offset in different content. What
# happens then is not defined by anything you can check: measured on this
# repository it ranges from every step running normally to one silently
# skipped, varying with the file's size and where the read buffer happened to
# land. `git checkout` usually lands on the benign side because it replaces
# the file rather than truncating it, leaving the descriptor bash holds
# pointed at the intact original -- but "usually", for a failure that produces
# a green run with steps missing from it, is not a property to build on.
#
# So this file removes the question instead of answering it. Everything below
# is inside main(), which bash parses into memory in full before running any
# of it, and which exits rather than returning. Past `main "$@"` the file is
# never read from disk again, so it does not matter what the candidate's tree
# holds in its place -- a different version, or for any candidate cut before
# this lane existed, nothing at all. tests/test_ci_eval_rc.py runs that case
# against a real checkout that deletes this script mid-run.
#
# What runs after step 2 is the CANDIDATE's ci-deploy.sh and ci-eval-pr.sh,
# not this checkout's, and deliberately -- resolve-rc-target.sh's header has
# the reasoning. Only the images being the candidate's, while the chart, the
# CRDs and bench/tasks stay on main, would grade a build nobody is shipping.
# The corollary is that a candidate predating the RC deploy path cannot be
# measured by it, which is what the DEPLOY_RC_MARKER check below reports.
#
# Environment:
#   RC_EVAL_ENABLED   any non-empty value arms this script. Unset = dormant,
#                     one skip line, exit 0. The Prow job config arms it; until
#                     the companion oss-test-infra job exists this file is
#                     inert wherever it runs.
#   RC_TAG            pin a candidate instead of resolving the newest one.
#                     Passed through to resolve-rc-target.sh.
#   PULL_NUMBER       Prow's. Set = a pull request, which never measures a
#                     release candidate; skip.
#   ARTIFACTS         Prow's. When set, receives rc-target.env and the
#                     summary below alongside the verdict ci-eval-pr.sh writes.
#   JOB_NAME/BUILD_ID Prow's. Both set, the summary carries the run's Deck URL
#                     so the verdict is findable without a credential.
#
# NOT a whole job: the four steps stop at the verdict, and teardown is the job
# config's to run -- as a separate step, unconditionally, whatever this script
# exits with. hack/ci-teardown.sh explains what a leased project left holding a
# live install does to the next lease that gets it (#1006), and this lane
# borrows the same pool the presubmit eval does, so skipping it is the one way
# a non-gating lane can still cost somebody a run.
#
# The path of this file is a CONTRACT: the periodic job in oss-test-infra
# invokes hack/ci-eval-rc.sh by name. Do not rename it.
# ==============================================================================

set -euo pipefail

# The tier exported to ci-eval-pr.sh. NOTHING READS IT TODAY: the switch that
# would is #1175's and is not on main, so `grep EVAL_TIER` finds only this file.
# Every run therefore measures the presubmit matrix, and the NOTE below prints
# for every candidate rather than only for old ones. That is a smaller run than
# the lane intends, not a wrong one, which is why it is a note and not a
# failure. The export is here so the lane widens to the full catalog on the day
# that switch merges, with no edit to this file.
#
# `nightly` and not a value of this lane's own, because #1175's switch rejects
# anything it does not know: a value invented here would exit 1 the day the two
# land together. What distinguishes this run from the main-branch nightly is
# RC_COMMIT_SHA, which ci-eval-pr.sh already reads as the third condition on
# the baseline store, so the tier does not have to carry that meaning too.
readonly RC_EVAL_TIER="nightly"

# Written by resolve-rc-target.sh through RC_TARGET_OUTPUT: the tag and commit
# in key=value form, for anything downstream that needs to know what was
# measured without parsing a log.
readonly RC_TARGET_FILE="rc-target.env"

# The key read back out of that file. A cross-file contract with the writer in
# resolve-rc-target.sh, so it is named for the same reason the filename is.
readonly RC_TARGET_TAG_KEY="rc_tag"

# This script's own artifact. The verdict file is bench-gate's, named here so
# the summary can point at it; keep in step with the --markdown-out in
# hack/ci-eval-pr.sh.
readonly RC_SUMMARY_FILE="rc-eval-summary.md"
readonly RC_VERDICT_FILE="eval-verdict.md"

# Deck serves what raw GCS refuses anonymously, so this is the form of the
# link a reader can actually open. Periodic build directories live under
# logs/<job>/<build>; a presubmit's are elsewhere, which is why the summary
# only builds a URL when Prow supplied both halves.
readonly PROW_DECK_BUILD_BASE="https://oss.gprow.dev/view/gs/kube-agents-prow/logs"

# Grepped for in the CANDIDATE's ci-deploy.sh after the checkout. Its presence
# is what says the candidate's tree can install published images instead of
# building; without it ci-deploy.sh would build the candidate's source into the
# leased project and measure something that was never published.
readonly DEPLOY_RC_MARKER="RC_COMMIT_SHA"

# The same question asked of the candidate's ci-eval-pr.sh, with a softer
# answer: absent, the tier switch is not there to read and the run measures
# the presubmit matrix. Absent is the norm until #1175 lands, so expect this
# one to fire on every candidate for now.
readonly EVAL_TIER_MARKER="EVAL_TIER"

# The siblings this script drives. Named because each name is a contract with
# hack/ -- the marker greps above read the same files the invocations below
# run, and a rename that moved one and not the other would leave this grepping
# a file nobody was about to execute.
readonly DEPLOY_SCRIPT="ci-deploy.sh"
readonly EVAL_SCRIPT="ci-eval-pr.sh"
readonly RESOLVE_SCRIPT="resolve-rc-target.sh"

# ─── Everything below runs inside main() ────────────────────────────────────
# See the header: the checkout in step 2 rewrites this file, so the body has to
# be in memory before it happens, and control must never return to the file.
main() {
  local script_dir repo_root rc_commit_sha rc_tag original_ref
  local artifacts_dir summary_path verdict_path deck_url
  local deploy_status eval_status verdict

  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "${script_dir}/.." && pwd)"

  # ─── Dormancy and trust gates (exit 0: nothing to do, or must not do it) ──
  if [ -z "${RC_EVAL_ENABLED:-}" ]; then
    echo "rc eval skipped: RC_EVAL_ENABLED is not set (the Prow job config arms this later)"
    exit 0
  fi
  if [ -n "${PULL_NUMBER:-}" ]; then
    echo "rc eval skipped: PULL_NUMBER=${PULL_NUMBER} is set: a pull request measures itself, never a release candidate"
    exit 0
  fi
  # PULL_NUMBER alone is one condition short. Prow sets it for presubmits, but
  # a batch job carries PULL_REFS and no PULL_NUMBER, so it would walk through
  # the check above and have the tree replaced underneath the merge it is
  # testing. Both siblings gate on the job shape as well -- ci-eval-pr.sh at
  # its baseline-store append, ci-dashboard-refresh.sh at its gs:// write --
  # and there is no reason for this one to be the weaker of the three.
  case "${JOB_TYPE:-periodic}" in
    periodic | postsubmit) ;;
    *)
      echo "rc eval skipped: JOB_TYPE=${JOB_TYPE:-} is not a main-branch job shape; only a periodic or postsubmit measures a release candidate"
      exit 0
      ;;
  esac

  # Recorded before anything moves, so a local run can be put back. Prow
  # workspaces are disposable and this is only ever printed, never restored:
  # an automatic checkout on the way out would run while the tree is the one
  # the eval just graded, and a failed restore would be a second failure
  # obscuring the first.
  original_ref="$(git -C "${repo_root}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")"
  if [ "${original_ref}" = "HEAD" ]; then
    original_ref="$(git -C "${repo_root}" rev-parse HEAD)"
  fi
  # On an EXIT trap rather than inline, because every interesting way to leave
  # a detached tree behind is a failure path: the marker refusal below, an
  # errexit abort inside ci-deploy.sh, a Prow deadline. Printing the hint only
  # on success would withhold it from exactly the runs that need it. The guard
  # keeps it quiet on the paths that never moved the tree.
  RC_EVAL_CHECKOUT_DONE="false"
  RC_EVAL_ORIGINAL_REF="${original_ref}"
  trap 'if [ "${RC_EVAL_CHECKOUT_DONE}" = "true" ]; then echo "This tree is detached at $(git -C "'"${repo_root}"'" rev-parse --short HEAD 2>/dev/null || echo unknown); \`git checkout ${RC_EVAL_ORIGINAL_REF}\` restores it."; fi' EXIT

  artifacts_dir="${ARTIFACTS:-}"
  if [ -n "${artifacts_dir}" ] && [ ! -d "${artifacts_dir}" ]; then
    mkdir -p "${artifacts_dir}"
  fi

  # ─── Step 1: which candidate ──────────────────────────────────────────────
  # stdout is the SHA and nothing else; the banner and every diagnostic go to
  # stderr. RC_TARGET_OUTPUT lands the tag alongside it as an artifact, which
  # is also how the tag reaches the summary below without a second resolve --
  # a second call could legitimately answer differently if a candidate is cut
  # between them.
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Resolving the release candidate to measure ==="
  if [ -n "${artifacts_dir}" ]; then
    export RC_TARGET_OUTPUT="${artifacts_dir}/${RC_TARGET_FILE}"
    : >"${RC_TARGET_OUTPUT}"
  fi
  rc_commit_sha="$("${script_dir}/${RESOLVE_SCRIPT}")"
  rc_tag="${RC_TAG:-}"
  if [ -z "${rc_tag}" ] && [ -n "${RC_TARGET_OUTPUT:-}" ] && [ -f "${RC_TARGET_OUTPUT}" ]; then
    rc_tag="$(sed -n "s/^${RC_TARGET_TAG_KEY}=//p" "${RC_TARGET_OUTPUT}" | tail -n 1)"
  fi
  rc_tag="${rc_tag:-${rc_commit_sha}}"

  # ─── Step 2: become the candidate ─────────────────────────────────────────
  # A tracked file modified in the workspace makes the checkout abort with
  # git's own message, which names the file but not why a job that never
  # edits anything is holding one. Prow starts from a clean clone, so this
  # firing means an earlier step wrote into the tree; saying so here is worth
  # the four lines it costs to diagnose from a build log.
  if [ -n "$(git -C "${repo_root}" status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: the working tree has modified tracked files, so checking out ${rc_tag} would abort partway." >&2
    git -C "${repo_root}" status --short --untracked-files=no >&2
    exit 1
  fi

  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Checking out ${rc_tag} (${rc_commit_sha:0:7}); this tree was ${original_ref} ==="
  # The pre-check above deliberately ignores untracked files -- a Prow
  # workspace legitimately holds build output -- which leaves one case it
  # cannot see: an untracked file at a path the candidate tracks, where git
  # refuses rather than clobbering it. Widening the pre-check would fail runs
  # that are fine, so the explanation is attached to the abort instead.
  if ! git -C "${repo_root}" checkout --detach "${rc_commit_sha}"; then
    echo "ERROR: checking out ${rc_tag} (${rc_commit_sha:0:7}) failed, so nothing was measured. If git named untracked files above, the workspace is holding files the candidate tracks; clear them and re-run." >&2
    exit 1
  fi
  RC_EVAL_CHECKOUT_DONE="true"

  # The candidate's ci-deploy.sh is what runs next, and a candidate cut before
  # the RC deploy path landed does not have one that can install published
  # images. Caught here rather than 15 minutes into a build that would have
  # measured the wrong artefact.
  if ! grep -q "${DEPLOY_RC_MARKER}" "${script_dir}/${DEPLOY_SCRIPT}"; then
    echo "ERROR: ${rc_tag} (${rc_commit_sha:0:7}) predates the release-candidate deploy path: its hack/${DEPLOY_SCRIPT} has no ${DEPLOY_RC_MARKER} handling and would build the candidate from source instead of installing its published images." >&2
    echo "       Measure a candidate cut after that path landed, or pin one with RC_TAG." >&2
    exit 1
  fi
  if ! grep -q "${EVAL_TIER_MARKER}" "${script_dir}/${EVAL_SCRIPT}"; then
    echo "NOTE: ${rc_tag} carries no ${EVAL_TIER_MARKER} switch, so this run measures the presubmit matrix rather than the full ${RC_EVAL_TIER} catalog. Expected until that switch lands; the verdict is valid for the cases it ran."
  fi

  # ─── Steps 3 and 4: deploy the candidate, then grade it ───────────────────
  # RC_COMMIT_SHA is what puts ci-deploy.sh on the published-image path and
  # what keeps ci-eval-pr.sh from filing the candidate's results as main's:
  # no field of the baseline store's VersionKey names the build a sample came
  # from, so a candidate recorded into main's window is not undoable.
  export RC_COMMIT_SHA="${rc_commit_sha}"
  export EVAL_TIER="${RC_EVAL_TIER}"

  # errexit is suspended for both steps, for the same reason: whatever happens,
  # this run owes Prow an artifact saying what happened. A bare invocation here
  # aborts main() on the spot, which skips the summary below and leaves a
  # deploy failure legible only to somebody willing to read the raw log --
  # and makes the summary's own "did not reach the verdict step" branch
  # unreachable. Measured against a real candidate: ci-deploy.sh exits non-zero
  # before it reaches a cluster if the environment is short a variable, which
  # is an ordinary Tuesday for a job whose config is maintained in another
  # repository.
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying release candidate ${rc_commit_sha:0:7} ==="
  deploy_status=0
  "${script_dir}/${DEPLOY_SCRIPT}" || deploy_status=$?

  # A failed deploy is not a red verdict. Nothing was measured, so saying RED
  # would report a judgement on the candidate that this run never formed --
  # the one reading that matters, because the whole lane exists to answer
  # "is this candidate worse than main".
  eval_status=0
  if [ "${deploy_status}" -ne 0 ]; then
    verdict="NOT RUN"
    echo "ERROR: deploying ${rc_tag} (${rc_commit_sha:0:7}) failed with status ${deploy_status}, so the candidate was never evaluated. This is not a verdict on the candidate." >&2
  else
    echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Evaluating release candidate ${rc_commit_sha:0:7} (tier: ${RC_EVAL_TIER}) ==="
    "${script_dir}/${EVAL_SCRIPT}" || eval_status=$?
    if [ "${eval_status}" -eq 0 ]; then
      verdict="GREEN"
    else
      verdict="RED"
    fi
  fi

  # ─── Reporting: make the verdict findable without a credential ────────────
  deck_url=""
  if [ -n "${JOB_NAME:-}" ] && [ -n "${BUILD_ID:-}" ]; then
    deck_url="${PROW_DECK_BUILD_BASE}/${JOB_NAME}/${BUILD_ID}"
  fi

  echo "======================================================================"
  echo "🏷️ RELEASE CANDIDATE EVAL"
  echo "Candidate:   ${rc_tag} (${rc_commit_sha})"
  echo "Tier:        ${RC_EVAL_TIER}"
  echo "Verdict:     ${verdict} (advisory: this lane gates nothing)"
  if [ -n "${deck_url}" ]; then
    echo "Artifacts:   ${deck_url}"
  fi
  echo "======================================================================"

  if [ -n "${artifacts_dir}" ]; then
    summary_path="${artifacts_dir}/${RC_SUMMARY_FILE}"
    verdict_path="${artifacts_dir}/${RC_VERDICT_FILE}"
    {
      echo "# Release candidate eval — ${rc_tag}"
      echo
      echo "| | |"
      echo "| --- | --- |"
      echo "| Candidate | \`${rc_tag}\` |"
      echo "| Commit | \`${rc_commit_sha}\` |"
      echo "| Tier | \`${RC_EVAL_TIER}\` |"
      echo "| Verdict | ${verdict} |"
      if [ -n "${deck_url}" ]; then
        echo "| Run | ${deck_url} |"
      fi
      echo
      echo "Advisory. This lane reports and does not gate: a red verdict here"
      echo "does not hold a release, and the non-inferiority comparison stays"
      echo "advisory while the baseline store is maturing."
      echo
      if [ -f "${verdict_path}" ]; then
        echo "Per-case detail is in \`${RC_VERDICT_FILE}\` alongside this file."
      elif [ "${deploy_status}" -ne 0 ]; then
        echo "The candidate was never evaluated: deploying it failed with"
        echo "status ${deploy_status}. Nothing here is a judgement on the"
        echo "candidate. The build log above is where it stopped."
      else
        echo "No \`${RC_VERDICT_FILE}\` was written: the run did not reach the"
        echo "verdict step. The build log above is where it stopped."
      fi
    } >"${summary_path}"
    echo "rc eval summary: ${summary_path}"
  fi

  # The restore hint is the EXIT trap's, so that the failure paths get it too.

  # The failing step's own status is preserved rather than forced to 0. Which
  # one the job reports is the JOB's decision (an `|| true` in its config),
  # kept there so "non-gating" is one line in the config a reader can see,
  # instead of a swallowed exit code here that makes a red run
  # indistinguishable from a green one to anything reading exit statuses.
  # Deploy first: on that path eval_status is 0 because the eval never ran,
  # and returning it would call a run that measured nothing a success.
  if [ "${deploy_status}" -ne 0 ]; then
    exit "${deploy_status}"
  fi
  exit "${eval_status}"
}

main "$@"
