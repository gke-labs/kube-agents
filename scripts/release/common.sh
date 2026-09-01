#!/usr/bin/env bash
# Common helper functions for Release Candidate CI/CD automation scripts.
set -euo pipefail

export REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# gke_dns_endpoint_flag, so release automation reaches a cluster over the same
# endpoint the installer would.
# shellcheck source=k8s-operator/scripts/gke_dns_endpoint.sh
source "${REPO_ROOT}/k8s-operator/scripts/gke_dns_endpoint.sh"

# Centralized definition of required container images and registry defaults
export DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"
export DEFAULT_RELEASE_REPO="gke-labs/kube-agents"
export DEFAULT_INITIAL_VERSION="0.1.0"

# Declarative registry of all 4 required container images
export REQUIRED_RELEASE_IMAGES=(
  "k8s-operator"
  "platform-agent"
  "credential-proxy"
  "replay-proxy"
)

# ─── Boolean Parsing ──────────────────────────────────────────────────────────
# Interpret a value as a boolean toggle. Returns 0 (success) for common
# affirmative spellings and 1 otherwise. Matching is case-insensitive and
# surrounding whitespace is ignored, so all of the following are truthy:
#   true, yes, y, 1, on  (in any letter case, e.g. "True", "YES", "On")
# Everything else — including false, no, n, 0, off, and empty/unset — is falsy.
is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

is_ci_pipeline() {
  is_truthy "${CI:-}"
}

# ─── Cluster connection ───────────────────────────────────────────────────────
# Two scripts in this directory point kubectl at the RC cluster before doing
# anything to it — install_pubsub_platform.sh and wait_for_gke_readiness.sh — and
# a workflow runs them as separate steps, so each starts from a fresh shell and
# has to resolve the target itself. The pair lives here rather than being
# duplicated, because the resolution order below is a contract with the
# workflows: GKE_CLUSTER_NAME/GCP_REGION/GCP_PROJECT_ID are what the `env:` blocks
# set, and CLUSTER_NAME/REGION/PROJECT_ID are the installer's own names, which a
# developer running these by hand after install.sh already has exported.
#
# Assigns to globals rather than echoing: a caller reading an echo would need
# command substitution, and a `set -u` abort inside a subshell would leave the
# variable empty and the script running against an unnamed target.
#
# None of the four get a default in CI. A pipeline that reaches here with
# GCP_PROJECT_ID unset has a misconfigured `env:` block or a variable missing
# from its GitHub environment; defaulting PROJECT_ID to kube-agents-rc there
# does not rescue the run, it points a real teardown-and-reinstall at a real
# project nobody named. Failing names the variable instead, at the first script
# that needs it rather than several steps later against a cluster that does not
# exist. The defaults stay for the developer path, which is what the
# CLUSTER_NAME/REGION/PROJECT_ID half of the contract above is for.
#
# AGENT_NAMESPACE is in the list because the `rc` and `nightly` environments
# both define it. A workflow that binds neither environment reaches here with
# all four empty and fails on the targeting trio regardless, so requiring the
# namespace costs those callers nothing — and a job that sets the other three
# but not this one is misconfigured in exactly the way silence used to hide,
# since `vars.AGENT_NAMESPACE` expanding to empty is indistinguishable from the
# default being correct.
release_resolve_target() {
  CLUSTER_NAME="${GKE_CLUSTER_NAME:-${CLUSTER_NAME:-}}"
  REGION="${GCP_REGION:-${REGION:-}}"
  PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-}}"
  AGENT_NAMESPACE="${AGENT_NAMESPACE:-}"

  if is_ci_pipeline; then
    # A string rather than an array: `${#arr[@]}` on an empty array aborts under
    # `set -u` on bash 3.2, which is what a developer on macOS runs these with.
    local missing=""
    [ -n "${CLUSTER_NAME}" ] || missing="${missing} GKE_CLUSTER_NAME"
    [ -n "${REGION}" ] || missing="${missing} GCP_REGION"
    [ -n "${PROJECT_ID}" ] || missing="${missing} GCP_PROJECT_ID"
    [ -n "${AGENT_NAMESPACE}" ] || missing="${missing} AGENT_NAMESPACE"
    if [ -n "${missing}" ]; then
      echo "❌ Unset in CI:${missing}" >&2
      echo "   These come from the job's \`env:\` block, which reads them from the" >&2
      echo "   workflow's GitHub environment. Set them there rather than relying on" >&2
      echo "   a default — a release script must not guess which project it targets." >&2
      return 1
    fi
  else
    CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
    REGION="${REGION:-us-central1}"
    PROJECT_ID="${PROJECT_ID:-kube-agents-rc}"
    AGENT_NAMESPACE="${AGENT_NAMESPACE:-kubeagents-system}"
  fi

  export CLUSTER_NAME REGION PROJECT_ID AGENT_NAMESPACE
}

# Points kubectl at the resolved cluster, unless it is already there.
#
# The context test checks the cluster name AND the project: a developer with
# several installs has more than one context whose name ends in the default
# cluster name, and matching on the cluster alone would silently accept the
# wrong one. Call release_resolve_target first.
release_connect_kubectl() {
  unset CLOUDSDK_PYTHON || true
  unset CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE || true
  export CLOUDSDK_PYTHON_SITEPACKAGES="0"
  export PYTHONNOUSERSITE="1"
  export USE_GKE_GCLOUD_AUTH_PLUGIN="True"
  export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS="false"
  gcloud config set container/use_application_default_credentials false --quiet || true

  if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
    gcloud auth activate-service-account --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" --quiet || true
  fi

  local current_ctx
  current_ctx="$(kubectl config current-context 2>/dev/null || echo "")"
  if ! kubectl cluster-info >/dev/null 2>&1 ||
    [[ "${current_ctx}" != *"${CLUSTER_NAME}"* || "${current_ctx}" != *"${PROJECT_ID}"* ]]; then
    echo "Connecting kubectl to target cluster '${CLUSTER_NAME}' in project '${PROJECT_ID}'..."
    gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}"
    # Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
    # shellcheck disable=SC2086
    gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" --project "${PROJECT_ID}" \
      ${GKE_DNS_ENDPOINT_FLAG}
  fi
}

# Validates that a string is a valid pure numeric SemVer (X.Y.Z without 'v' prefix)
validate_pure_numeric_semver() {
  local ver="${1:-}"
  local label="${2:-Target release tag}"
  if [ -z "${ver}" ]; then
    echo "❌ ERROR: ${label} must be specified." >&2
    return 1
  fi
  if [[ ! "${ver}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "❌ ERROR: ${label} '${ver}' is not a valid pure numeric SemVer (e.g. 0.1.0, 0.2.0). 'v' prefix is not supported." >&2
    return 1
  fi
  return 0
}

# Hermetic component-wise SemVer 2.0 comparator
# Returns: 1 if v1 > v2, 0 if v1 == v2, -1 if v1 < v2
compare_semver() {
  local v1="$1" v2="$2"
  if [ "$v1" = "$v2" ]; then echo "0"; return 0; fi
  local M1 N1 P1 M2 N2 P2
  IFS='.' read -r M1 N1 P1 <<< "$v1"
  IFS='.' read -r M2 N2 P2 <<< "$v2"
  if [ "$M1" -gt "$M2" ]; then echo "1"; return 0; fi
  if [ "$M1" -lt "$M2" ]; then echo "-1"; return 0; fi
  if [ "$N1" -gt "$N2" ]; then echo "1"; return 0; fi
  if [ "$N1" -lt "$N2" ]; then echo "-1"; return 0; fi
  if [ "$P1" -gt "$P2" ]; then echo "1"; return 0; fi
  if [ "$P1" -lt "$P2" ]; then echo "-1"; return 0; fi
  echo "0"
}

# Finds the latest pure numeric GA SemVer release tag in git repository (e.g. 0.2.0).
# Accepts an optional fallback default value if no GA tags are found.
get_latest_ga_tag() {
  local default_fallback="${1:-}"
  local latest
  latest="$(git tag -l --sort=version:refname '[0-9]*' 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | tail -n 1 || true)"
  if [ -n "${latest}" ]; then
    echo "${latest}"
  else
    echo "${default_fallback}"
  fi
}

# Finds the latest validated release candidate tag (rc_*_validated)
get_latest_validated_rc_tag() {
  git tag -l --sort=-v:refname 'rc_*_validated' 2>/dev/null | grep -E '^rc_.*_validated$' | head -n 1 || echo ""
}

# Reads the commits between the last GA tag and a candidate, into
# RELEASE_RANGE_SUBJECTS (`%s`) and RELEASE_RANGE_BODIES (`%b`).
#
# Shared for the same reason the predicate below is: calculate_next_version.sh
# applies it to pick a bump and resolve_scheduled_release.sh applies it to decide
# whether to release at all, so the two have to be looking at the same commits.
# A `--no-merges` or a path filter added to one range and not the other would
# scope the bump and the halt differently, silently.
#
# Stderr is kept out of the captured value. `$(git log … 2>&1)` merges warnings
# into the output on SUCCESS, not only on failure — and git warns on success for
# an ambiguous refname, which is what a branch sharing a GA tag's name produces.
# An empty range then captures `warning: refname '0.1.0' is ambiguous.`, reads as
# non-empty, and an unattended run publishes a release for a week with nothing in
# it. The message is still reported, from the failure branch, where it belongs.
#
# Arguments: $1 = base GA tag, $2 = candidate commit-ish. Returns non-zero if the
# range cannot be read.
#
# shellcheck disable=SC2034  # RELEASE_RANGE_* are the return channel, read by callers.
release_read_commit_range() {
  local base_tag="${1:-}"
  local target="${2:-}"
  local range="${base_tag}..${target}"
  local stderr_file
  stderr_file="$(mktemp)"

  RELEASE_RANGE_SUBJECTS=""
  RELEASE_RANGE_BODIES=""

  if ! RELEASE_RANGE_SUBJECTS="$(git log "${range}" --format="%s" 2>"${stderr_file}")"; then
    echo "❌ ERROR: Failed to read commit log for range '${range}': $(cat "${stderr_file}")" >&2
    rm -f "${stderr_file}"
    return 1
  fi
  rm -f "${stderr_file}"

  RELEASE_RANGE_BODIES="$(git log "${range}" --format="%b" 2>/dev/null || echo "")"
  return 0
}

# Answers "does this commit range carry a breaking change?" — a `feat!:`-style
# bang on the type, or a BREAKING CHANGE / BREAKING-CHANGE footer.
#
# Both callers take the same answer from here rather than each holding a copy of
# the regexes. calculate_next_version.sh reads it to pick the bump, and
# resolve_scheduled_release.sh reads it to decide whether an unattended release
# has to stop for a human. Two copies drift in a way nothing notices: widen one
# to catch a footer variant and the gate silently stops halting on that shape,
# so a breaking change ships unattended with every suite green.
#
# Herestrings rather than `echo … | grep -q`. Under `set -o pipefail` grep exits
# on its first match, the producer then dies on SIGPIPE, and the pipeline reports
# 141 — so a corpus large enough to still be buffered makes matching input read
# as "no breaking change". That is the unsafe direction, and it is the same
# hazard candidate_supports_shared_pipeline already avoids for the same reason.
#
# Arguments: $1 = commit subjects (`git log --format=%s`), $2 = bodies (`%b`).
commit_messages_have_breaking_change() {
  local subjects="${1:-}"
  local bodies="${2:-}"

  if grep -qE "^[a-z]+(\([^)]+\))?!:" <<<"${subjects}"; then
    return 0
  fi
  if grep -qE "^[[:space:]]*BREAKING[ -]CHANGE:[[:space:]]+" <<<"${bodies}"; then
    return 0
  fi
  return 1
}

# Resolves target GitHub repository (e.g. gke-labs/kube-agents)
get_target_repo() {
  if [ -n "${GH_ORG:-}" ] && [ -n "${GH_REPO:-}" ]; then
    echo "${GH_ORG}/${GH_REPO}"
  elif [ -n "${GITHUB_REPOSITORY:-}" ]; then
    echo "${GITHUB_REPOSITORY}"
  else
    echo "${DEFAULT_RELEASE_REPO}"
  fi
}

# Resolves registry prefix (e.g. ghcr.io/gke-labs/kube-agents)
get_registry_prefix() {
  if [ -n "${REGISTRY_PREFIX:-}" ]; then
    echo "${REGISTRY_PREFIX}"
  else
    local target_repo
    target_repo="$(get_target_repo)"
    if [ "$target_repo" = "$DEFAULT_RELEASE_REPO" ]; then
      echo "$DEFAULT_REGISTRY_PREFIX"
    else
      local repo_downcased
      repo_downcased="$(echo "$target_repo" | tr '[:upper:]' '[:lower:]')"
      echo "ghcr.io/${repo_downcased}"
    fi
  fi
}

# Checks if all required candidate container images exist in GHCR for a specific commit SHA
check_commit_images_exist() {
  local sha="$1"
  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    local target_img="${registry_prefix}/${img}:${sha}"
    if ! docker manifest inspect "${target_img}" >/dev/null 2>&1; then
      return 1
    fi
  done
  return 0
}

# Finds an existing rc_* tag for a commit SHA (excluding *_validated tags)
get_existing_rc_tag() {
  local sha="$1"
  git tag --points-at "${sha}" "rc_*" 2>/dev/null | grep -v '_validated$' | head -n 1 || echo ""
}

# Checks if a commit SHA has already been attempted in a previous RC run (rc_* tag exists)
is_commit_already_attempted() {
  local sha="$1"
  local rc_tag
  rc_tag=$(get_existing_rc_tag "${sha}")
  [ -n "${rc_tag}" ]
}

# Checks if a commit SHA carries the RC pipeline's validation marker (rc_*_validated).
#
# Anchored to the rc_ family, and named for it: this gates resolve_rc_tag.sh's
# skip decision and the nightly promotion, so a marker minted by some other tag
# family must not read as an RC validation. get_latest_validated_rc_tag anchors
# the same way. The GA gate does not appear in that list any more:
# verify_release_eligibility.sh reads the staging family alone, and takes the RC
# validation as implied by it — see STAGING_TAG_SHAPE_REGEX below.
is_rc_candidate_commit_already_validated() {
  local sha="$1"
  local validated_tags
  validated_tags=$(git tag --points-at "${sha}" "rc_*_validated" 2>/dev/null || echo "")
  [ -n "${validated_tags}" ]
}

# ─── Staging promotion tags ───────────────────────────────────────────────────
# The nightly pipeline promotes a validated RC candidate by tagging its commit
# staging_<ts>_<sha>, which is what staging-redeploy-*.yml triggers on.
export STAGING_TAG_PREFIX="staging_"

# Derives the staging promotion tag from a validated RC tag:
#   rc_2608241820_b35543c_validated  ->  staging_2608241820_b35543c
#
# The timestamp stays first after the prefix so `git tag -l --sort=-v:refname
# 'staging_*'` orders by time, and the transform is mechanical in both
# directions, so a staging tag reads back to its candidate without a lookup. The
# _validated suffix is dropped: it records that the RC gate passed, not that the
# promotion did.
#
# Refuses anything outside the rc_ family rather than composing staging_<junk>,
# because the result is a live deploy trigger.
staging_tag_for_rc() {
  local rc_tag="${1:-}"
  if [ -z "${rc_tag}" ]; then
    echo "❌ ERROR: an RC tag is required for staging_tag_for_rc." >&2
    return 1
  fi

  local core="${rc_tag%_validated}"
  case "${core}" in
    rc_?*) core="${core#rc_}" ;;
    *)
      echo "❌ ERROR: '${rc_tag}' is not an rc_* candidate tag; refusing to derive a staging tag from it." >&2
      return 1
      ;;
  esac

  echo "${STAGING_TAG_PREFIX}${core}"
}

# The shape a staging tag must have to count as release evidence:
# staging_<YYMMDDHHMM>_<7-hex>, which is exactly what staging_tag_for_rc composes
# from a validated rc_ tag and therefore exactly what the nightly pipeline
# pushes.
#
# The GA gate matches this rather than the STAGING_TAG_PREFIX the deploy
# workflows trigger on, and the difference is the whole defence. The prefix is a
# trigger anyone can push by hand; a `staging_hotfix` typed at a terminal would
# otherwise read back to the release gate as "the full nightly matrix passed on
# this commit". The timestamp and short SHA in the right places are not produced
# by accident.
#
# It stops an accident, not an attacker. Nothing checks that the 7-hex field is
# the short SHA of the commit the tag points at, or that the commit carries
# rc_*_validated, so a deliberately composed `staging_<ts>_<sha>` satisfies the
# gate. That is no weaker than the rc_*_validated gate it replaces — equally a
# tag anyone with push access could create — but it is not the stronger
# guarantee the shape makes it look like.
export STAGING_TAG_SHAPE_REGEX='^staging_[0-9]{10}_[0-9a-f]{7}$'

# Finds the newest shape-valid staging promotion tag anywhere in the repository.
# Empty output means nothing has been promoted to staging.
#
# `--sort=-v:refname` orders by the timestamp immediately after the prefix, which
# is why staging_tag_for_rc puts it there. The list is materialised before it is
# filtered rather than piped into `grep | head`: under `set -o pipefail` head
# closing the pipe early makes grep exit 141, which a trailing `|| echo ""` then
# turns into "nothing has passed the gate" — a skipped release, silently, once
# the tag list outgrows a pipe buffer.
get_latest_staging_tag() {
  local tags
  tags="$(git tag -l --sort=-v:refname "${STAGING_TAG_PREFIX}*" 2>/dev/null || true)"
  grep -m1 -E "${STAGING_TAG_SHAPE_REGEX}" <<<"${tags}" || true
}

# Lists the shape-valid staging promotion tags pointing at a commit, one per
# line. Empty output means this commit has not passed the nightly matrix.
staging_promotion_tags_at_commit() {
  local sha="${1:-}"
  local tags
  tags="$(git tag --points-at "${sha}" "${STAGING_TAG_PREFIX}*" 2>/dev/null || true)"
  grep -E "${STAGING_TAG_SHAPE_REGEX}" <<<"${tags}" || true
}

# Finds an existing staging promotion tag on a commit SHA, if any. Empty output
# means the commit has not been promoted yet.
#
# Shape-matched, like the two above, and it has to be. This is what
# resolve_promotion_candidate.sh reads to set `skip_promotion`, so a prefix match
# here means a hand-pushed `staging_hotfix` — which staging-redeploy-*.yml
# legitimately triggers on — tells the nightly the commit is already promoted. It
# then never pushes the real staging_<ts>_<sha> tag, and the release gate, which
# does match on shape, reads that same commit as unreleasable. The candidate goes
# quietly unshippable, and the two lookups have to agree for it not to.
#
# Erring towards not-yet-promoted is the safe direction on its own terms too:
# `ensure_git_tag` no-ops when the tag already points at the same commit, so a
# redundant promotion costs nothing.
get_existing_staging_tag() {
  local sha="$1"
  local tags
  tags="$(staging_promotion_tags_at_commit "${sha}")"
  # Narrowed to the first line with a parameter expansion rather than a pipe into
  # `head -n 1`, for the reason get_latest_staging_tag gives above: under
  # `set -o pipefail` head closing the pipe early makes the producer exit 141, and
  # the `|| echo ""` that usually sits beside it reads that as "not promoted" —
  # which is the exact misreport this function was shape-anchored to prevent.
  [ -n "${tags}" ] && printf '%s\n' "${tags%%$'\n'*}"
  return 0
}

# Reports whether a candidate commit's tree carries what the shared pipeline
# workflows invoke against it.
#
# deploy-environment.yml, e2e-run.yml and teardown-environment.yml each check the
# candidate out over the workspace and then run scripts from THAT tree, while the
# workflow YAML comes from the caller's ref. A candidate validated before that
# structure landed is therefore driven by workflows expecting scripts and a suite
# selector it does not have — and two of those mismatches are silent rather than
# loud, which is what makes this worth refusing over:
#
#   * e2e-run.yml names the suite in E2E_SUITE. A pre-rename runner reads only
#     E2E_ENV, so it falls back to its own default and the blocking gate tests
#     something other than what the run reports it gated on.
#   * run_optional_e2e_suites.sh is absent there entirely, and its step is
#     continue-on-error, so the optional suites contribute nothing and the run
#     still goes green.
#
# Both markers are checked because they fail independently.
#
# This does not expire with the restructure. Once the RC pipeline validates a
# post-restructure commit, `get_latest_validated_rc_tag` stops returning an old
# one and the default path never reaches this check again — but
# nightly-pipeline.yml takes an `rc_tag` dispatch input whose description offers
# any validated candidate, and the tag graph keeps every candidate it ever
# validated. Naming one by hand is a supported thing to do and stays wrong for
# the same reason it is wrong today.
#
# The two markers probe one epoch boundary — the shared-pipeline restructure —
# and not the general question of whether a tree can be driven by these
# workflows. Nine scripts run out of the candidate's checkout; these sample two.
# That is sound for the boundary they were chosen for, because both arrived in
# the commit that created it. A later restructure that adds a seam needs its own
# marker here; this function will not notice on its own.
candidate_supports_shared_pipeline() {
  local sha="${1:-}"

  if [ -z "${sha}" ]; then
    echo "❌ ERROR: a commit is required for candidate_supports_shared_pipeline." >&2
    return 2
  fi

  git cat-file -e "${sha}:scripts/release/run_optional_e2e_suites.sh" 2>/dev/null || return 1

  # `git grep`, not `git show | grep -q`. Under the `pipefail` this file sets,
  # the pipeline reports whatever killed the producer: `grep -q` exits the moment
  # it matches, `git show` then dies on SIGPIPE, and the pipeline fails with 141
  # on a tree that does carry the marker. It needs a blob larger than the pipe
  # buffer, so it would not fire today — execute_e2e_tests.py is around 16 KB —
  # and it fails in the direction that skips a good candidate silently.
  #
  # Anything non-zero refuses, including an unreadable object. Refusing is the
  # safe direction: the cost is a skipped night, and the alternative is testing a
  # candidate whose tree we could not read.
  git grep -q "E2E_SUITE" "${sha}" -- scripts/release/execute_e2e_tests.py 2>/dev/null || return 1

  return 0
}

# Reports whether the staging redeploys AT A GIVEN COMMIT would start on a given
# tag, by reading the `push: tags:` patterns out of that commit's own copy of
# staging-redeploy-agent.yml.
#
# A push event runs the workflows in the pushed ref's tree, not the ones on the
# default branch, and a promotion tag lands on a candidate commit that can be days
# old. So the question of whether a tag deploys anything is answered by the
# candidate, and a promotion pushed at a commit whose trigger does not match the
# tag succeeds, deploys nothing, and reports green — after which
# get_existing_staging_tag sees the tag and no later run retries that candidate.
#
# The three redeploys share one trigger, so agent stands for all three.
staging_trigger_matches_at_commit() {
  local commit="${1:-}" tag="${2:-}"
  local workflow=".github/workflows/staging-redeploy-agent.yml"
  local yaml patterns pattern

  if [ -z "${commit}" ] || [ -z "${tag}" ]; then
    echo "❌ ERROR: a commit and a tag are required for staging_trigger_matches_at_commit." >&2
    return 2
  fi

  yaml="$(git show "${commit}:${workflow}" 2>/dev/null)" || return 1

  # The list items under the single `tags:` key, unquoted. Stops at the first
  # line that is neither a list item nor blank, so it cannot run on into the rest
  # of the file if the key is ever absent.
  patterns="$(printf '%s\n' "${yaml}" | awk '
    /^[[:space:]]*tags:[[:space:]]*$/ { in_tags = 1; next }
    in_tags && /^[[:space:]]*#/ { next }
    in_tags && /^[[:space:]]*$/ { next }
    in_tags && /^[[:space:]]*-[[:space:]]/ {
      item = $0
      sub(/^[[:space:]]*-[[:space:]]*/, "", item)
      sub(/[[:space:]]*$/, "", item)
      gsub(/^"|"$/, "", item)
      gsub(/^'"'"'|'"'"'$/, "", item)
      print item
      next
    }
    in_tags { in_tags = 0 }
  ')"

  [ -n "${patterns}" ] || return 1

  while IFS= read -r pattern; do
    [ -n "${pattern}" ] || continue
    # Glob-matched rather than compared: the point is what GitHub would do with
    # the pattern, not whether the file says what this branch expects. So the
    # expansion is deliberately unquoted.
    # shellcheck disable=SC2254
    case "${tag}" in
      ${pattern}) return 0 ;;
    esac
  done <<EOF
${patterns}
EOF

  return 1
}

# Finds the latest commit on main whose required container images are already built in the registry
find_latest_built_commit() {
  local target_repo
  target_repo="$(get_target_repo)"
  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  echo "🔍 [Schedule / Auto-resolve] Scanning recent commits on main for prebuilt container images (${registry_prefix})..." >&2

  local is_shallow
  is_shallow="$(git rev-parse --is-shallow-repository 2>/dev/null || echo "false")"
  local depth_arg=()
  if [ "${is_shallow}" = "true" ]; then
    depth_arg=("--depth=30")
  fi

  local fetch_ok="false"
  if is_ci_pipeline; then
    if [ -n "${target_repo}" ]; then
      # bash 3.2 compatibility: guard empty array expansion under set -u
      if git fetch "https://github.com/${target_repo}.git" main --tags ${depth_arg[@]+"${depth_arg[@]}"} >/dev/null 2>&1; then
        fetch_ok="true"
      else
        echo "⚠️ Warning: Failed to fetch from target_repo (${target_repo}), falling back to origin..." >&2
      fi
    fi

    if [ "${fetch_ok}" != "true" ]; then
      # bash 3.2 compatibility: guard empty array expansion under set -u
      if git fetch origin main --tags ${depth_arg[@]+"${depth_arg[@]}"} >/dev/null 2>&1; then
        fetch_ok="true"
      else
        echo "⚠️ Warning: Failed to fetch from origin remote, checking available local refs..." >&2
      fi
    fi
  fi

  local candidate_commits
  candidate_commits=$(git log -n 30 --format="%H" FETCH_HEAD 2>/dev/null || git log -n 30 --format="%H" origin/main 2>/dev/null || git log -n 30 --format="%H" HEAD 2>/dev/null || echo "")

  if [ -z "${candidate_commits}" ]; then
    echo "❌ ERROR: Cannot retrieve commit history from git repository!" >&2
    return 1
  fi

  for sha in $candidate_commits; do
    if check_commit_images_exist "${sha}"; then
      echo "✅ Found latest commit with verified container images: ${sha}" >&2
      echo "$sha"
      return 0
    else
      echo "  ⏳ Images not ready yet in GHCR for commit ${sha:0:7}, checking previous commit..." >&2
    fi
  done

  echo "❌ ERROR: Could not find any commit in the last 30 commits on main with published images in GHCR (${registry_prefix})!" >&2
  return 1
}

# Configures Git bot user identity for automated tagging and committing
setup_git_bot_user() {
  export GIT_AUTHOR_NAME="github-actions[bot]"
  export GIT_AUTHOR_EMAIL="github-actions[bot]@users.noreply.github.com"
  export GIT_COMMITTER_NAME="github-actions[bot]"
  export GIT_COMMITTER_EMAIL="github-actions[bot]@users.noreply.github.com"
}

# Syncs remote tags into the local repository, in CI only.
#
# Every script that answers a question from the tag graph calls this first: a
# shallow or tagless checkout otherwise resolves "no such tag" rather than
# failing, which is the quiet way to skip a candidate or promote nothing.
#
# `|| true` throughout, deliberately. An unreachable network is not itself the
# error; the caller's own lookup fails afterwards naming the tag it wanted, which
# is the message worth printing.
#
# find_latest_built_commit does not use this — it fetches `main` too, handles a
# shallow clone's --depth, and reports which remote answered.
release_fetch_tags() {
  is_ci_pipeline || return 0

  local target_repo
  target_repo="$(get_target_repo)"
  git fetch "https://github.com/${target_repo}.git" --tags >/dev/null 2>&1 ||
    git fetch origin --tags >/dev/null 2>&1 || true
}

# Ensures a Git tag exists for a given commit SHA idempotently and pushes to origin.
# Arguments: $1 = rc_tag, $2 = commit_sha, $3 = tag_message
ensure_git_tag() {
  local rc_tag="${1:-${RC_TAG:-}}"
  local commit_sha="${2:-${COMMIT_SHA:-}}"
  local tag_message="${3:-Release Candidate ${rc_tag}}"

  if [ -z "${rc_tag}" ] || [ -z "${commit_sha}" ]; then
    echo "❌ ERROR: RC_TAG and COMMIT_SHA are required for ensure_git_tag." >&2
    return 1
  fi

  local target_repo
  target_repo="$(get_target_repo)"

  release_fetch_tags

  # Canonicalize commit SHA to full 40-character hash before comparison
  local target_full_sha
  target_full_sha="$(git rev-parse --verify "${commit_sha}^{commit}" 2>/dev/null || echo "${commit_sha}")"

  # Check if tag already exists in Git
  local existing_sha
  if existing_sha="$(git rev-parse --verify "refs/tags/${rc_tag}^{commit}" 2>/dev/null)"; then
    if [ "${existing_sha}" = "${target_full_sha}" ]; then
      echo "✅ Git tag '${rc_tag}' already exists and points to target commit ${target_full_sha}. Idempotent skip."
      return 0
    else
      echo "❌ ERROR: Tag '${rc_tag}' already exists but points to commit ${existing_sha}, not target SHA ${target_full_sha}!" >&2
      return 1
    fi
  fi

  setup_git_bot_user
  git tag -a "${rc_tag}" "${target_full_sha}" -m "${tag_message}"

  # Safety Guard: Remote push executes exclusively inside CI
  if ! is_ci_pipeline; then
    echo "⚠️ [Local Execution] Dry-run: Git tag '${rc_tag}' created locally. Remote push skipped (runs only in CI)."
    return 0
  fi

  local push_err
  if push_err=$(git push origin "${rc_tag}" 2>&1); then
    echo "✅ Git tag '${rc_tag}' successfully pushed to remote repository (${target_repo})!"
  elif push_err=$(git push "https://github.com/${target_repo}.git" "${rc_tag}" 2>&1); then
    echo "✅ Git tag '${rc_tag}' successfully pushed to remote repository (${target_repo})!"
  else
    echo "❌ ERROR: Could not push git tag '${rc_tag}' to remote repository (${target_repo}): ${push_err}" >&2
    return 1
  fi
}

# Stamps BAKED_RELEASE_VERSION into root installer scripts (install.sh, uninstall.sh, upgrade.sh)
stamp_baked_release_version() {
  local version="${1:-}"
  local repo_dir="${2:-${REPO_ROOT}}"

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for stamp_baked_release_version." >&2
    return 1
  fi

  for script_name in install.sh uninstall.sh upgrade.sh; do
    local script_path="${repo_dir}/${script_name}"
    if [ -f "${script_path}" ]; then
      sed -i.bak -E "s/^BAKED_RELEASE_VERSION=[\"'].*[\"']/BAKED_RELEASE_VERSION=\"${version}\"/" "${script_path}" && rm -f "${script_path}.bak"
      if ! grep -q "^BAKED_RELEASE_VERSION=\"${version}\"" "${script_path}"; then
        echo "❌ ERROR: Failed to stamp BAKED_RELEASE_VERSION in ${script_name} (placeholder line '^BAKED_RELEASE_VERSION=...' not found)." >&2
        git -C "${repo_dir}" checkout -- install.sh uninstall.sh upgrade.sh >/dev/null 2>&1 || true
        return 1
      fi
    fi
  done
}

# Validates if a release tag commit is either directly the candidate commit
# or a single-parent stamped child commit derived from the candidate.
is_valid_stamped_or_direct_release_commit() {
  local candidate_sha="${1:-}"
  local tag_commit="${2:-}"
  local version="${3:-}"

  if [ -z "${candidate_sha}" ] || [ -z "${tag_commit}" ] || [ -z "${version}" ]; then
    echo "❌ ERROR: candidate_sha, tag_commit, and version are all required for is_valid_stamped_or_direct_release_commit." >&2
    return 1
  fi

  # Case 1: Exact match (tag placed directly on candidate)
  if [ "${candidate_sha}" = "${tag_commit}" ]; then
    return 0
  fi

  # Case 2: Direct single-parent stamped child
  local parent_sha
  if ! parent_sha="$(git rev-parse --verify "${tag_commit}^1" 2>/dev/null)"; then
    echo "⚠️ Tag commit ${tag_commit:0:7} has no resolvable parent commit in repository." >&2
    return 1
  fi

  # Reject merge commits (must have no second parent)
  if git rev-parse --verify "${tag_commit}^2" >/dev/null 2>&1; then
    echo "⚠️ Tag commit ${tag_commit:0:7} is a merge commit; expected single-parent stamped release commit." >&2
    return 1
  fi

  if [ "${parent_sha}" != "${candidate_sha}" ]; then
    echo "⚠️ Tag commit ${tag_commit:0:7} parent (${parent_sha:0:7}) does not match candidate commit (${candidate_sha:0:7})." >&2
    return 1
  fi

  local commit_subject
  commit_subject="$(git log -1 --format=%s "${tag_commit}" 2>/dev/null || echo "")"
  local expected_subject="chore(release): stamp release version ${version}"
  if [ "${commit_subject}" != "${expected_subject}" ]; then
    echo "⚠️ Tag commit ${tag_commit:0:7} subject '${commit_subject}' does not match expected stamped subject '${expected_subject}'." >&2
    return 1
  fi

  return 0
}

# Creates a release commit on detached HEAD with stamped BAKED_RELEASE_VERSION
create_stamped_release_commit() {
  local version="${1:-}"
  local target_sha="${2:-}"
  local repo_dir="${3:-${REPO_ROOT}}"

  if [ -z "${version}" ] || [ -z "${target_sha}" ]; then
    echo "❌ ERROR: version and target_sha are required for create_stamped_release_commit." >&2
    return 1
  fi

  # Preserve caller's current branch / ref and restore on function return
  local orig_ref
  orig_ref="$(git -C "${repo_dir}" symbolic-ref --short -q HEAD 2>/dev/null || git -C "${repo_dir}" rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "${orig_ref}" ]; then
    # shellcheck disable=SC2064
    trap "git -C '${repo_dir}' checkout -- install.sh uninstall.sh upgrade.sh >/dev/null 2>&1 || true; git -C '${repo_dir}' checkout '${orig_ref}' >/dev/null 2>&1 || true" RETURN
  fi

  # Idempotency check: if release tag already exists and is a valid release commit for target_sha, reuse it
  local existing_tag_sha
  if existing_tag_sha="$(git -C "${repo_dir}" rev-parse --verify "refs/tags/${version}^{commit}" 2>/dev/null)"; then
    if is_valid_stamped_or_direct_release_commit "${target_sha}" "${existing_tag_sha}" "${version}"; then
      echo "ℹ️ Release tag '${version}' already exists on valid release commit ${existing_tag_sha:0:7}. Reusing existing release commit." >&2
      echo "${existing_tag_sha}"
      return 0
    fi
  fi

  # 1. Checkout detached HEAD at candidate commit
  if ! git -C "${repo_dir}" checkout --detach "${target_sha}"; then
    echo "❌ ERROR: Failed to checkout candidate commit '${target_sha}' on detached HEAD." >&2
    return 1
  fi

  # 2. Stamp BAKED_RELEASE_VERSION in root installer scripts
  if ! stamp_baked_release_version "${version}" "${repo_dir}"; then
    echo "❌ ERROR: Failed to stamp baked release version into installer scripts." >&2
    return 1
  fi

  # 3. If files were modified, create release commit on detached HEAD (does NOT touch main branch)
  local modified_files=()
  for script_name in install.sh uninstall.sh upgrade.sh; do
    if [ -f "${repo_dir}/${script_name}" ] && [ -n "$(git -C "${repo_dir}" status --porcelain "${script_name}" 2>/dev/null || true)" ]; then
      modified_files+=("${script_name}")
    fi
  done

  if [ ${#modified_files[@]} -gt 0 ]; then
    echo "📝 Stamping baked release version '${version}' in release tag commit..." >&2
    setup_git_bot_user
    git -C "${repo_dir}" add "${modified_files[@]}"
    git -C "${repo_dir}" commit -m "chore(release): stamp release version ${version}" >/dev/null
    git -C "${repo_dir}" rev-parse HEAD
  else
    echo "${target_sha}"
  fi
}

# Resolves the exact commit SHA for a release tag
resolve_release_commit() {
  local version="${1:-}"
  local tag_sha=""

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for resolve_release_commit." >&2
    return 1
  fi

  if tag_sha="$(git rev-parse --verify "refs/tags/${version}^{commit}" 2>/dev/null)"; then
    echo "${tag_sha}"
    return 0
  fi

  echo "❌ ERROR: Cannot resolve valid Git commit for release tag '${version}' (tag does not exist in repository)!" >&2
  return 1
}

# Retrieves canonical manifest digest (sha256:...) for a remote container image
get_image_manifest_digest() {
  local img="${1:-}"
  if [ -z "${img}" ] || ! command -v docker >/dev/null 2>&1; then
    return 1
  fi

  local digest=""
  if digest="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "${img}" 2>/dev/null)" && [ -n "${digest}" ] && [ "${digest}" != "<no value>" ]; then
    echo "${digest}"
    return 0
  fi

  # Fallback to computing raw manifest sha256 if raw inspect succeeds
  local raw_output
  if raw_output="$(docker buildx imagetools inspect --raw "${img}" 2>/dev/null)" && [ -n "${raw_output}" ]; then
    local raw_sha
    raw_sha="$(printf '%s' "${raw_output}" | sha256sum | awk '{print $1}')"
    if [ -n "${raw_sha}" ]; then
      echo "sha256:${raw_sha}"
      return 0
    fi
  fi

  # Fallback to computing manifest sha256 from docker manifest inspect if available
  local manifest_output
  if manifest_output="$(docker manifest inspect "${img}" 2>/dev/null)" && [ -n "${manifest_output}" ]; then
    local manifest_sha
    manifest_sha="$(printf '%s' "${manifest_output}" | sha256sum | awk '{print $1}')"
    if [ -n "${manifest_sha}" ]; then
      echo "sha256:${manifest_sha}"
      return 0
    fi
  fi

  return 1
}

# Resolves the candidate commit SHA where CI built the container images.
# If a SemVer version is provided, checks:
# 1. Direct 40-character SHA if passed as argument
# 2. Stamped release tag parent commit refs/tags/${version}^ (where images were built by CI prior to tag stamping)
# 3. Direct tag commit refs/tags/${version}^{commit}
resolve_source_image_commit() {
  local version_or_commit="${1:-}"

  if [ -z "${version_or_commit}" ]; then
    echo "❌ ERROR: version_or_commit is required for resolve_source_image_commit." >&2
    return 1
  fi

  # If version_or_commit is a 40-char SHA
  if git rev-parse --verify "${version_or_commit}^{commit}" >/dev/null 2>&1 && [[ ! "${version_or_commit}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    git rev-parse --verify "${version_or_commit}^{commit}"
    return 0
  fi

  local version="${version_or_commit}"
  local tag_commit=""
  if tag_commit="$(git rev-parse --verify "refs/tags/${version}^{commit}" 2>/dev/null)"; then
    local parent_commit=""
    if parent_commit="$(git rev-parse --verify "${tag_commit}^" 2>/dev/null)"; then
      if is_ci_pipeline && command -v docker >/dev/null 2>&1; then
        if check_commit_images_exist "${tag_commit}" 2>/dev/null; then
          echo "${tag_commit}"
          return 0
        fi
        if check_commit_images_exist "${parent_commit}" 2>/dev/null; then
          echo "${parent_commit}"
          return 0
        fi
      fi
      # Fallback detection: if tag commit is a stamped release commit, return its parent
      local commit_msg
      commit_msg="$(git log -1 --format="%s" "${tag_commit}" 2>/dev/null || echo "")"
      if [[ "${commit_msg}" =~ ^chore(\(release\))?:\ stamp ]]; then
        echo "${parent_commit}"
        return 0
      fi
    fi
    echo "${tag_commit}"
    return 0
  fi

  echo "❌ ERROR: Cannot resolve source image commit for version '${version}' (tag 'refs/tags/${version}' not found in repository)." >&2
  return 1
}

# Clean Promotion: Tags verified container images in GHCR without rebuilding
promote_release_images() {
  local commit_sha="${1:-}"
  local release_version="${2:-}"

  # Sibling symmetry: support swapped args if version was passed first
  if [[ "${commit_sha}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ ! "${release_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    local tmp="${commit_sha}"
    commit_sha="${release_version}"
    release_version="${tmp}"
  fi

  if [ -z "${commit_sha}" ] || [ -z "${release_version}" ]; then
    echo "❌ ERROR: commit_sha and release_version are required for promote_release_images." >&2
    return 1
  fi

  validate_pure_numeric_semver "${release_version}" "Release version" || return 1

  if ! command -v docker >/dev/null 2>&1; then
    echo "❌ ERROR: 'docker buildx' CLI is required for image promotion!" >&2
    return 1
  fi

  local resolved_commit
  resolved_commit="$(git rev-parse --verify "${commit_sha}^{commit}" 2>/dev/null || echo "${commit_sha}")"

  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  echo "🚀 Promoting verified container images (${resolved_commit:0:7}) -> (${release_version})..."

  # Safety Guard: Remote image promotion executes exclusively inside CI
  if ! is_ci_pipeline; then
    echo "⚠️ [Local Execution] Dry-run: Remote image promotion to (${release_version}) in ${registry_prefix} skipped (runs only in CI)."
    return 0
  fi

  for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
    local source_image="${registry_prefix}/${img}:${resolved_commit}"
    local target_image="${registry_prefix}/${img}:${release_version}"
    echo "  • Promoting ${img}..."

    # Safety Guard: Check if target image tag already exists in registry
    local target_digest=""
    if target_digest="$(get_image_manifest_digest "${target_image}")"; then
      local source_digest=""
      if ! source_digest="$(get_image_manifest_digest "${source_image}")"; then
        echo "❌ ERROR: Target image '${target_image}' already exists in registry, but failed to inspect source image '${source_image}'!" >&2
        return 1
      fi

      local raw_target=""
      if [ "${target_digest}" = "${source_digest}" ] || \
         ( [ -n "${source_digest}" ] && raw_target="$(docker buildx imagetools inspect --raw "${target_image}" 2>/dev/null)" && printf '%s' "${raw_target}" | grep -q "${source_digest}" ); then
        echo "    ℹ️ Target image '${target_image}' already exists in registry and matches source image (${resolved_commit:0:7}). Skipping duplicate promotion."
        continue
      else
        echo "❌ ERROR: Target image '${target_image}' already exists in registry (digest: ${target_digest}) but does NOT match source image '${source_image}' (digest: ${source_digest})!" >&2
        echo "Release promotion blocked to prevent artifact mismatch across commits." >&2
        return 1
      fi
    fi

    docker buildx imagetools create --prefer-index=false --tag "${target_image}" "${source_image}"
    echo "    ✅ Promoted ${img} to ${release_version}"
  done
}
