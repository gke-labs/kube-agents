#!/usr/bin/env bash
# Decides whether an unattended run of release-publish.yml should publish a GA
# release.
#
# The publishing pipeline is unchanged by this script. It still calculates a
# version from Conventional Commits and still refuses a commit that has not
# passed the gate. This answers only the question a human used to answer by
# choosing when to click "Run workflow": is this candidate one we are willing to
# ship with nobody watching?
#
# Three conditions:
#
#   1. A candidate has passed the gate — the newest staging_<ts>_<sha> tag,
#      matched on its shape rather than its prefix. Skip.
#   2. There is something to release: commits exist between the newest GA tag
#      and that candidate's commit. Skip.
#   3. Nothing in the range is a breaking change. HALT.
#
# Conditions 1 and 2 are green skips: nothing is published, the run stays green,
# and the next run asks again. Condition 3 is not, and the difference matters. A
# breaking change does not clear itself — every following run takes the same
# branch and GA releases stop until somebody publishes by hand — so it fails the
# job. A skip means "nothing to do this week"; red means "something needs you".
#
# Why this gate is worth having when the publishing path already skips a quiet
# week on its own is `scripts/release/README.md`, "The weekly GA release". It is
# canonical for the reasoning; do not restate it here.
#
# There is deliberately no weekday or elapsed-time check in here. The cron is
# the cadence, so no wall-clock arithmetic exists anywhere in the decision, and
# "has this candidate already been released?" needs no condition of its own:
# if the newest GA tag points at the gated commit, condition 2's range is empty
# and the skip already covers it. The state lives in the tags.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

SHOULD_RELEASE="false"
SKIP_REASON=""
RELEASE_COMMIT=""
GATE_TAG=""
LATEST_GA_TAG=""

# Set only by condition 3. See the halt handling in emit_and_exit.
HALTED_FOR_HUMAN=""

# Set by the two paths that fail on a broken tag graph rather than on a verdict.
# They still leave through emit_and_exit: the job declares gate_tag and
# skip_reason as outputs, and a bare `exit 1` resolves both to empty for the one
# reader who most needs to know why the release did not happen.
ERRORED=""

emit_and_exit() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "should_release=${SHOULD_RELEASE}"
      echo "release_commit=${RELEASE_COMMIT}"
      echo "gate_tag=${GATE_TAG}"
      echo "skip_reason=${SKIP_REASON}"
    } >> "${GITHUB_OUTPUT}"
  fi

  echo "======================================================================"
  if [ -n "${ERRORED}" ]; then
    echo "❌ FAILED — THE TAG GRAPH DID NOT ANSWER"
    echo "Reason:                ${SKIP_REASON}"
    echo "Newest gate tag:       ${GATE_TAG:-<none>}"
    echo "Latest GA tag:         ${LATEST_GA_TAG:-<none>}"
  elif [ "${SHOULD_RELEASE}" = "true" ]; then
    echo "🚀 RELEASING"
    echo "Gate-passing commit:   ${RELEASE_COMMIT}"
    echo "From gate tag:         ${GATE_TAG}"
    echo "Previous GA tag:       ${LATEST_GA_TAG:-<none>}"
  elif [ -n "${HALTED_FOR_HUMAN}" ]; then
    echo "🛑 HALTED — A HUMAN HAS TO PUBLISH THIS ONE"
    echo "Reason:                ${SKIP_REASON}"
    echo "Gate-passing commit:   ${RELEASE_COMMIT:-<none>}"
    echo "Latest GA tag:         ${LATEST_GA_TAG:-<none>}"
  else
    echo "⏭️  NOT RELEASING"
    echo "Reason:                ${SKIP_REASON}"
    echo "Newest gate tag:       ${GATE_TAG:-<none>}"
    echo "Latest GA tag:         ${LATEST_GA_TAG:-<none>}"
  fi
  echo "======================================================================"

  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      if [ -n "${ERRORED}" ]; then
        echo "### Release gate failed"
        echo ""
        echo "${SKIP_REASON}"
      elif [ "${SHOULD_RELEASE}" = "true" ]; then
        echo "### Releasing \`${RELEASE_COMMIT:0:7}\`"
        echo ""
        echo "Gate passed as \`${GATE_TAG}\`."
      elif [ -n "${HALTED_FOR_HUMAN}" ]; then
        echo "### Release halted"
        echo ""
        echo "${SKIP_REASON}"
      else
        echo "### No release this run"
        echo ""
        echo "${SKIP_REASON}"
      fi
    } >> "${GITHUB_STEP_SUMMARY}"
  fi

  if [ -n "${ERRORED}" ]; then
    echo "::error title=GA release gate failed::${SKIP_REASON}"
    exit 1
  fi

  # A halt is not like the other two outcomes: it persists until somebody acts.
  # Failing the job is what makes it visible — a green run notifies nobody, so a
  # halt reported as a skip would stop GA releases silently for as long as the
  # breaking change sits unshipped.
  if [ -n "${HALTED_FOR_HUMAN}" ]; then
    echo "::error title=GA release halted::${SKIP_REASON}"
    exit 1
  fi

  exit 0
}

# Tags are the entire input to this decision, and a checkout that fetched none
# answers "nothing to release" for a repository full of candidates. No-op
# outside CI, where the local tag graph is already whatever the caller intended.
release_fetch_tags

LATEST_GA_TAG="$(get_latest_ga_tag)"

# ── 1. Has anything passed the gate? ─────────────────────────────────────────
#
# The staging tag is the evidence, and it is the only evidence: it means the full
# nightly matrix passed on this commit, where an rc_*_validated tag means only the
# narrow three-hourly suite did. Requiring both would re-check a property the
# first already guarantees — the nightly only ever promotes a candidate that
# carries rc_*_validated — and would leave two gates to keep in step.
#
# Reusing common.sh's lookup rather than re-implementing it: a second answer to
# "which candidate has been promoted" is how this gate and
# verify_release_eligibility.sh drift apart, and they have to agree or the
# resolver waves through a commit the publish job then refuses with exit 1.
GATE_TAG="$(get_latest_staging_tag)"
if [ -z "${GATE_TAG}" ]; then
  SKIP_REASON="No candidate has passed the gate — no 'staging_<ts>_<sha>' tag exists."
  emit_and_exit
fi

if ! RELEASE_COMMIT="$(git rev-parse --verify "${GATE_TAG}^{commit}" 2>/dev/null)"; then
  ERRORED="true"
  SKIP_REASON="Gate tag '${GATE_TAG}' does not resolve to a commit."
  emit_and_exit
fi

# ── 2. Is there anything new in it? ──────────────────────────────────────────
#
# With no GA tag there is no range and nothing to check: the repository has never
# released, so everything reachable is new. Both remaining conditions are skipped
# rather than evaluated against all of history — see condition 3 for why that
# matters — which is also what calculate_next_version.sh does in this state,
# publishing DEFAULT_INITIAL_VERSION without scanning.
if [ -z "${LATEST_GA_TAG}" ]; then
  SHOULD_RELEASE="true"
  emit_and_exit
fi

# The helper has already printed git's own stderr, which says more about why than
# anything reconstructable here; this only carries the failure into the outputs.
if ! release_read_commit_range "${LATEST_GA_TAG}" "${RELEASE_COMMIT}"; then
  ERRORED="true"
  SKIP_REASON="Could not read the commit range ${LATEST_GA_TAG}..${RELEASE_COMMIT:0:7} — see the log."
  emit_and_exit
fi

if [ -z "${RELEASE_RANGE_SUBJECTS}" ]; then
  # Two shapes reach here and both are "nothing to ship". The one this condition
  # exists for is the emergency leftover: a GA tag sitting on a commit that is not
  # this candidate's stamped child, which makes verify_release_eligibility.sh
  # report "tag already exists on a different commit" and exit 1 — a red run with
  # nothing wrong, every week, until somebody releases by hand.
  #
  # The ordinary quiet week reaches here too and does not need it; that path is
  # already green without this. What the skip is and is not worth on that path is
  # `scripts/release/README.md`, "The weekly GA release", which is canonical.
  SKIP_REASON="No commits between ${LATEST_GA_TAG} and the gate-passing commit ${RELEASE_COMMIT:0:7}."
  emit_and_exit
fi

# ── 3. Is any of it a breaking change? ───────────────────────────────────────
#
# Spelled as "breaking" rather than "MAJOR" deliberately. calculate_next_version.sh
# implements SemVer clause 4, so while the repository is on 0.y.z a breaking
# change bumps MINOR and the MAJOR digit never moves — a guard written against
# MAJOR would pass every breaking release straight through until 1.0.0.
#
# The definition is common.sh's, shared with calculate_next_version.sh, because a
# second copy here is how the bump and the halt come to disagree about what
# "breaking" means — and the direction that fails silently is the gate waving one
# through into an unattended release.
#
# Reached only with a GA tag in hand, which is what keeps this bounded. Against
# all of history it would match some long-shipped `feat!:` and then never stop
# matching it, since there is no range to shrink: one permanent halt, every run.
if commit_messages_have_breaking_change "${RELEASE_RANGE_SUBJECTS}" "${RELEASE_RANGE_BODIES}"; then
  HALTED_FOR_HUMAN="true"
  SKIP_REASON="A breaking change is waiting to ship. Releases carrying one are published by a human: run release-publish.yml manually against ${RELEASE_COMMIT:0:7}."
  emit_and_exit
fi

SHOULD_RELEASE="true"
emit_and_exit
