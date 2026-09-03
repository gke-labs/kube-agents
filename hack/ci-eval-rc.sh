#!/usr/bin/env bash
# ==============================================================================
# Release-candidate eval (the periodic job's entrypoint)
# ==============================================================================
# Resolve the newest release candidate, check it out, deploy its published
# images, and run the full eval catalog against them. Non-gating: the verdict
# is reported, and nothing here can hold up a release.
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
# The path of this file is a CONTRACT: the periodic job in oss-test-infra
# invokes hack/ci-eval-rc.sh by name. Do not rename it.
# ==============================================================================

set -euo pipefail

# The tier exported to ci-eval-pr.sh. `nightly` and not a value of this lane's
# own, because the tier switch is #1175's and its default branch rejects
# anything it does not know: a value invented here would exit 1 the day the two
# land together. What distinguishes this run from the main-branch nightly is
# RC_COMMIT_SHA, which ci-eval-pr.sh already reads as the third condition on
# the baseline store, so the tier does not have to carry that meaning too.
#
# Against a candidate whose tree predates the switch the variable is simply
# unread and the run measures the presubmit matrix. That is a smaller run than
# intended, not a wrong one, so it is a note below rather than a failure.
readonly RC_EVAL_TIER="nightly"

# Written by resolve-rc-target.sh through RC_TARGET_OUTPUT: the tag and commit
# in key=value form, for anything downstream that needs to know what was
# measured without parsing a log.
readonly RC_TARGET_FILE="rc-target.env"

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
# the presubmit matrix.
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
  local artifacts_dir summary_path verdict_path deck_url eval_status

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

  # Recorded before anything moves, so a local run can be put back. Prow
  # workspaces are disposable and this is only ever printed, never restored:
  # an automatic checkout on the way out would run while the tree is the one
  # the eval just graded, and a failed restore would be a second failure
  # obscuring the first.
  original_ref="$(git -C "${repo_root}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")"
  if [ "${original_ref}" = "HEAD" ]; then
    original_ref="$(git -C "${repo_root}" rev-parse HEAD)"
  fi

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
    rc_tag="$(sed -n 's/^rc_tag=//p' "${RC_TARGET_OUTPUT}" | tail -n 1)"
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
  git -C "${repo_root}" checkout --detach "${rc_commit_sha}"

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
    echo "NOTE: ${rc_tag} predates the ${RC_EVAL_TIER} tier, so this run measures the presubmit matrix rather than the full catalog. The verdict is valid for the cases it ran."
  fi

  # ─── Steps 3 and 4: deploy the candidate, then grade it ───────────────────
  # RC_COMMIT_SHA is what puts ci-deploy.sh on the published-image path and
  # what keeps ci-eval-pr.sh from filing the candidate's results as main's:
  # no field of the baseline store's VersionKey names the build a sample came
  # from, so a candidate recorded into main's window is not undoable.
  export RC_COMMIT_SHA="${rc_commit_sha}"
  export EVAL_TIER="${RC_EVAL_TIER}"

  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying release candidate ${rc_commit_sha:0:7} ==="
  "${script_dir}/${DEPLOY_SCRIPT}"

  # errexit is suspended for the eval alone. A red verdict is this job's
  # OUTPUT, not its failure -- this lane is non-gating by charter -- so the
  # status is captured, reported, and handed back at the end rather than
  # skipping the summary that says where to read it.
  echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Evaluating release candidate ${rc_commit_sha:0:7} (tier: ${RC_EVAL_TIER}) ==="
  eval_status=0
  "${script_dir}/${EVAL_SCRIPT}" || eval_status=$?

  # ─── Reporting: make the verdict findable without a credential ────────────
  deck_url=""
  if [ -n "${JOB_NAME:-}" ] && [ -n "${BUILD_ID:-}" ]; then
    deck_url="${PROW_DECK_BUILD_BASE}/${JOB_NAME}/${BUILD_ID}"
  fi

  echo "======================================================================"
  echo "🏷️ RELEASE CANDIDATE EVAL"
  echo "Candidate:   ${rc_tag} (${rc_commit_sha})"
  echo "Tier:        ${RC_EVAL_TIER}"
  echo "Verdict:     $([ "${eval_status}" -eq 0 ] && echo "GREEN" || echo "RED") (advisory: this lane gates nothing)"
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
      echo "| Verdict | $([ "${eval_status}" -eq 0 ] && echo "GREEN" || echo "RED") |"
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
      else
        echo "No \`${RC_VERDICT_FILE}\` was written: the run did not reach the"
        echo "verdict step. The build log above is where it stopped."
      fi
    } >"${summary_path}"
    echo "rc eval summary: ${summary_path}"
  fi

  echo "This tree is detached at ${rc_commit_sha:0:7}; \`git checkout ${original_ref}\` restores it."

  # The eval's own status is preserved rather than forced to 0. Which one the
  # job reports is the JOB's decision (an `|| true` in its config), kept there
  # so "non-gating" is one line in the config a reader can see, instead of a
  # swallowed exit code here that makes a red run indistinguishable from a
  # green one to anything reading exit statuses.
  exit "${eval_status}"
}

main "$@"
