#!/usr/bin/env bash
# Common helper functions for Release Candidate CI/CD automation scripts.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

# gke_dns_endpoint_flag, so release automation reaches a cluster over the same
# endpoint the installer would.
# shellcheck source=scripts/installer/gke_dns_endpoint.sh
source "${REPO_ROOT}/scripts/installer/gke_dns_endpoint.sh"

# Centralized definition of required container images and registry defaults
export DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"
export DEFAULT_RELEASE_REPO="gke-labs/kube-agents"
export DEFAULT_INITIAL_VERSION="0.1.0"

# The shape of a GA release tag: pure numeric X.Y.Z, no 'v' prefix. One
# definition, so the validator and the two tag lookups below cannot drift apart.
readonly GA_TAG_SHAPE_REGEX='^[0-9]+\.[0-9]+\.[0-9]+$'
# The subject the GA tagger gives the stamped release commit; the version
# follows. What is_valid_stamped_or_direct_release_commit recognises.
readonly RELEASE_STAMP_SUBJECT_PREFIX="chore(release): stamp release version"

# The branch each GA release commit is pushed to, alongside its tag: the
# release line `release/<X.Y>`. The first release of a line creates it at its
# stamped commit (a minor, or the first patch of a minor that predates the
# lines); each later patch is stamped as a child of the line's head and
# fast-forwards it, so nothing is ever force-pushed and every release commit on
# the line stays reachable from a branch. (Releases 0.1.0 to 0.7.0 predate the
# lines and sit on per-release `release/<X.Y.Z>` branches, left as they are.)
readonly RELEASE_BRANCH_PREFIX="release/"
# The shape of a release line: `X.Y`, the version with its patch component off.
readonly RELEASE_LINE_SHAPE_REGEX='^[0-9]+\.[0-9]+$'
# A Conventional Commits feature subject, which bumps MINOR on main and is
# refused on a release line. Read by calculate_next_version.sh.
# shellcheck disable=SC2034
readonly FEAT_SUBJECT_REGEX='^feat(\([^)]+\))?:'
# The full ref a branch lives under, for the lookups and refspecs that must not
# be satisfied by a tag of the same name.
readonly GIT_BRANCH_REF_PREFIX="refs/heads/"
# The branch every nightly and eval candidate is cut from, the remote-tracking
# ref a full clone keeps for it, and the ref a bare `git fetch` leaves behind.
readonly RELEASE_MAIN_BRANCH="main"
readonly RELEASE_MAIN_TRACKING_REF="refs/remotes/origin/main"
readonly GIT_FETCH_HEAD_REF="FETCH_HEAD"
# Where the required release image list lives in the tree, and the array that
# holds it: how required_release_images_at reads a candidate commit's own list
# instead of this checkout's.
readonly REQUIRED_RELEASE_IMAGES_PATH="scripts/release/common.sh"
readonly REQUIRED_RELEASE_IMAGES_NAME="REQUIRED_RELEASE_IMAGES"

# The registry the docker-free existence probe below knows how to query, and the
# manifest media types that probe must accept. Omitting the OCI types gets a
# MANIFEST_UNKNOWN carrying "Accept header does not support OCI manifests" — a
# 404 that reads as a missing image rather than as a wrong header.
export GHCR_REGISTRY_HOST="ghcr.io"
export GHCR_MANIFEST_ACCEPT="application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json"
# The two registry answers ghcr_image_status tells apart; anything else is an error.
readonly HTTP_STATUS_OK="200"
readonly HTTP_STATUS_NOT_FOUND="404"

# Declarative registry of all required release container images
export REQUIRED_RELEASE_IMAGES=(
  "k8s-operator"
  "platform-agent"
  "credential-proxy"
  "agent-sandbox"
  "replay-proxy"
  "pubsub-platform"
  "gke-stockout-investigator"
  "a2a-gateway"
  "a2a-worker"
  "a2a-authcallout"
  "a2a-verifier"
  "a2a-console"
  "hermes-bridge"
)

# Declarative registry of release bundle directories, root files, and Helm charts
export RELEASE_BUNDLE_DIRECTORIES=(
  "terraform"
  "k8s-operator"
  "deploy"
  "charts"
  "scripts"
  "examples"
)

export RELEASE_INSTALLER_SCRIPTS=(
  "install.sh"
  "uninstall.sh"
  "upgrade.sh"
)

export RELEASE_HELM_CHARTS=(
  "charts/kube-agents"
)

export RELEASE_TERRAFORM_EXAMPLE_VARS="terraform/examples/full-install/variables.tf"
export RELEASE_TERRAFORM_EXAMPLE_TFVARS="terraform/examples/full-install/terraform.tfvars.example"

export RELEASE_BUNDLE_ROOT_FILES=(
  "${RELEASE_INSTALLER_SCRIPTS[@]}"
  "install.defaults.env"
  "install.env.example"
  "images.json"
  "Makefile"
  "INSTALL.md"
  "README.md"
  "LICENSE"
)

# ─── Git Commit Extraction ───────────────────────────────────────────────────
# Extracts specific paths (or the full tree) from a Git commit directly into a
# target directory using git archive. Ensures extraction reflects only tracked
# files at the target commit SHA, excluding dirty working-tree state or ignored files.
extract_commit_tree() {
  local commit_sha="$1"
  local target_dir="$2"
  shift 2
  local paths=("$@")

  mkdir -p "${target_dir}"

  if [ "${#paths[@]}" -gt 0 ]; then
    if ! git -C "${REPO_ROOT}" archive "${commit_sha}" "${paths[@]}" | tar -x -C "${target_dir}"; then
      echo "❌ ERROR: Failed to extract ${paths[*]} from commit ${commit_sha:0:7}!" >&2
      return 1
    fi
  else
    if ! git -C "${REPO_ROOT}" archive "${commit_sha}" | tar -x -C "${target_dir}"; then
      echo "❌ ERROR: Failed to extract git archive from commit ${commit_sha:0:7}!" >&2
      return 1
    fi
  fi
}

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
# Release scripts point kubectl at the RC cluster before doing anything to it,
# such as wait_for_gke_readiness.sh, resolving the target itself. The helpers live
# here rather than being duplicated, because the resolution order below is a
# contract with the workflows: GKE_CLUSTER_NAME/GCP_REGION/GCP_PROJECT_ID are what
# the `env:` blocks set, and CLUSTER_NAME/REGION/PROJECT_ID are the installer's own
# names, which a developer running these by hand after install.sh already has exported.
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
  if [[ ! "${ver}" =~ ${GA_TAG_SHAPE_REGEX} ]]; then
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
  latest="$(git tag -l --sort=version:refname '[0-9]*' 2>/dev/null | grep -E "${GA_TAG_SHAPE_REGEX}" | tail -n 1 || true)"
  if [ -n "${latest}" ]; then
    echo "${latest}"
  else
    echo "${default_fallback}"
  fi
}

# Whether a GA tag sits on the stamped child the tagger creates (single parent,
# subject `chore(release): stamp release version <tag>`), as 0.3.0 onward do.
# 0.1.0 and 0.2.0 predate stamping and sit directly on main.
# Arguments: $1 = tag, $2 = the tag's commit
ga_tag_is_stamped() {
  local tag="${1:-}" tag_commit="${2:-}" parent
  parent="$(git rev-parse --verify --quiet "${tag_commit}^1" 2>/dev/null)" || return 1
  is_valid_stamped_or_direct_release_commit "${parent}" "${tag_commit}" "${tag}" 2>/dev/null
}

# A GA tag is a base for a candidate when the tag's commit — or, for a stamped
# tag, the commit the stamp was cut from, which is where the branch's history
# meets it — is in the history of `tip`, which defaults to the candidate itself.
# `--is-ancestor` holds for equality, which is what lets a re-run whose tag
# already exists (parent == candidate) find its own release.
#
# The tip is the branch the candidate is released from, when that is more than
# the candidate: main's callers pass main's head, so a release somebody cut by
# hand from a later main commit still counts as main's base and an older
# candidate reads as "nothing new" rather than as a new release that would
# collide with it. A release line's stamps have parents off main, so they never
# qualify for main whichever tip is given.
# Arguments: $1 = tag, $2 = candidate commit, $3 = branch tip (optional)
ga_tag_is_base_of() {
  local tag="${1:-}" candidate="${2:-}" tip="${3:-}" tag_commit
  tip="${tip:-${candidate}}"
  tag_commit="$(git rev-parse --verify --quiet "refs/tags/${tag}^{commit}" 2>/dev/null)" || return 1
  git merge-base --is-ancestor "${tag_commit}" "${tip}" 2>/dev/null && return 0
  ga_tag_is_stamped "${tag}" "${tag_commit}" || return 1
  git merge-base --is-ancestor "${tag_commit}^1" "${tip}" 2>/dev/null
}

# The highest GA tag in a candidate's own history: the base the version bump,
# the scheduled-release range and the release notes all start from. Found by
# ancestry, not by number, so main's base stays 0.7.0 once 0.7.1 exists on
# release/0.7, and 0.7.1's base is 0.7.0. Optional $2 looks strictly below a
# version: the notes step passes the release being published, whose own tag is
# by then in its history. Optional $3 is the branch tip the tags are qualified
# against (see ga_tag_is_base_of). Prints nothing when no GA tag qualifies.
# Arguments: $1 = candidate commit-ish, $2 = ceiling version (exclusive), optional,
#            $3 = branch tip commit-ish, optional
get_base_ga_tag_for_commit() {
  local candidate="${1:-}" below="${2:-}" tip="${3:-}" candidate_sha best="" tag

  if [ -z "${candidate}" ]; then
    echo "❌ ERROR: a candidate commit is required for get_base_ga_tag_for_commit." >&2
    return 1
  fi
  if ! candidate_sha="$(git rev-parse --verify "${candidate}^{commit}" 2>/dev/null)"; then
    echo "❌ ERROR: '${candidate}' does not resolve to a commit." >&2
    return 1
  fi
  if [ -n "${below}" ]; then
    validate_pure_numeric_semver "${below}" "Ceiling version" || return 1
  fi

  while IFS= read -r tag; do
    [ -n "${tag}" ] || continue
    if [ -n "${below}" ] && [ "$(compare_semver "${tag}" "${below}")" != "-1" ]; then
      continue
    fi
    ga_tag_is_base_of "${tag}" "${candidate_sha}" "${tip}" || continue
    if [ -z "${best}" ] || [ "$(compare_semver "${tag}" "${best}")" = "1" ]; then
      best="${tag}"
    fi
  done < <(git tag -l '[0-9]*' 2>/dev/null | grep -E "${GA_TAG_SHAPE_REGEX}" || true)
  echo "${best}"
}

# The ref that stands for `main` in this checkout. In CI it is the release
# repository's `main`, fetched now: a checkout whose origin is a fork, or whose
# remote-tracking ref is stale, must not answer with an old main, so a fetch
# that fails is an error there rather than a fall-through to the tracking ref.
# A shallow CI checkout is unshallowed first and refused if that fails, since
# past a shallow boundary every ancestry test reads "no", which would drop every
# candidate but the tip. Off CI nothing is fetched: the tracking ref or a local
# `main` answers, with a note that it is only as fresh as the last fetch, and a
# shallow checkout resolves nothing. Prints the ref, or nothing.
release_main_ref() {
  local shallow
  shallow="$(git rev-parse --is-shallow-repository 2>/dev/null || echo false)"
  if is_ci_pipeline; then
    if [ "${shallow}" = "true" ]; then
      git fetch --unshallow "$(release_repo_url)" >/dev/null 2>&1 || true
      if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
        echo "❌ ERROR: This checkout is shallow, and ancestry against main cannot be read past its boundary." >&2
        return 1
      fi
    fi
    # The full ref, so a tag that happens to be named `main` cannot answer for
    # the branch: a bare `main` refspec resolves tags before heads.
    if ! git fetch "$(release_repo_url)" "${GIT_BRANCH_REF_PREFIX}${RELEASE_MAIN_BRANCH}" >/dev/null 2>&1; then
      echo "❌ ERROR: Could not fetch ${RELEASE_MAIN_BRANCH} from $(release_repo_url); not falling back to a tracking ref that may be stale." >&2
      return 1
    fi
    echo "${GIT_FETCH_HEAD_REF}"
    return 0
  fi
  if [ "${shallow}" = "true" ]; then
    return 1
  fi
  local ref
  for ref in "${RELEASE_MAIN_TRACKING_REF}" "${GIT_BRANCH_REF_PREFIX}${RELEASE_MAIN_BRANCH}"; do
    if git rev-parse --verify --quiet "${ref}" >/dev/null 2>&1; then
      echo "ℹ️ Filtering candidates against ${ref}, which is as fresh as this checkout's last fetch." >&2
      echo "${ref}"
      return 0
    fi
  done
  return 1
}

# The commit main is at, for the callers that qualify a GA base against main's
# whole history rather than the candidate's (get_base_ga_tag_for_commit). In CI
# an unreadable main is an error; off CI it prints nothing, and the caller
# falls back to the candidate's own history.
release_main_tip() {
  local main_ref
  if ! main_ref="$(release_main_ref)"; then
    if is_ci_pipeline; then
      echo "❌ ERROR: Could not resolve main in this checkout; refusing to pick a GA base without it." >&2
      return 1
    fi
    echo "⚠️ Warning: main cannot be read reliably here; qualifying the GA base against the candidate alone." >&2
    return 0
  fi
  git rev-parse --verify "${main_ref}^{commit}"
}

# Lists the tags matching a glob whose commit is on main, newest by name first,
# which for the rc_ families is newest by timestamp. The candidate pickers below
# sort tags by name, and a release line's RC tags share the namespace: without
# this the first `rc_` tag cut on `release/<X.Y>` would be the newest of all,
# and the nightly promotion and the Prow eval would both adopt a line commit
# as main's candidate. In CI a `main` that cannot be read is an error rather
# than a guess; off CI the list passes through unfiltered with a warning, so a
# hand run in a partial checkout still answers. A caller that has already
# resolved main (release_main_tip) passes it, so one run fetches it once.
# Arguments: $1 = tag glob, $2 = main's commit or ref, already resolved (optional)
list_tags_on_main() {
  local glob="${1:-}"
  local main_ref="${2:-}"

  if [ -z "${glob}" ]; then
    echo "❌ ERROR: a tag glob is required for list_tags_on_main." >&2
    return 1
  fi

  if [ -n "${main_ref}" ]; then
    :
  elif ! main_ref="$(release_main_ref)"; then
    if is_ci_pipeline; then
      echo "❌ ERROR: Could not resolve main in this checkout; refusing to pick a candidate without it." >&2
      return 1
    fi
    echo "⚠️ Warning: main cannot be read reliably here (missing, or a shallow checkout); not filtering candidates to it." >&2
    git tag -l --sort=-v:refname "${glob}" 2>/dev/null || true
    return 0
  fi
  # A tag that does not peel to a commit is passed through rather than dropped:
  # `--merged` cannot place it, and the caller's own resolution then fails
  # loudly, which is what a broken tag graph owes rather than a quiet
  # "no candidate" that stays green until somebody deletes the tag.
  local all_tags on_main tag
  all_tags="$(git tag -l --sort=-v:refname "${glob}" 2>/dev/null || true)"
  on_main="$(git tag -l --sort=-v:refname --merged "${main_ref}" "${glob}" 2>/dev/null || true)"
  while IFS= read -r tag; do
    [ -n "${tag}" ] || continue
    if grep -Fxq "${tag}" <<<"${on_main}"; then
      echo "${tag}"
    elif ! git rev-parse --verify --quiet "refs/tags/${tag}^{commit}" >/dev/null 2>&1; then
      echo "${tag}"
    fi
  done <<<"${all_tags}"
}

# Finds the latest validated release candidate tag (rc_*_validated) on main.
# A release line's validation is the gate for that line's own patch release,
# never a nightly candidate: see list_tags_on_main.
get_latest_validated_rc_tag() {
  local on_main validated
  on_main="$(list_tags_on_main 'rc_*_validated')" || return 1
  # Materialised before `head`: under pipefail, `head` closing the pipe after
  # the first line would end the producer with SIGPIPE and read as a failure.
  validated="$(grep -E '^rc_.*_validated$' <<<"${on_main}" || true)"
  head -n 1 <<<"${validated}"
}

# Reads the commits between a base GA tag and a candidate, into
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
# non-empty, and an unattended run publishes a release for a day with nothing in
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
# the regexes. calculate_next_version.sh reads it to pick the bump (bumping MINOR
# in 0.y.z under SemVer Clause 4, or MAJOR in >= 1.0.0), and
# resolve_scheduled_release.sh reads it to decide whether an unattended release on
# stable GA (>= 1.0.0) has to stop for a human. Two copies drift in a way nothing
# notices: widen one to catch a footer variant and the gate silently stops
# halting on that shape on stable releases.
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

# Answers "is this GA release version in pre-1.0 initial development under SemVer Clause 4?"
# That is, does MAJOR == 0?
#
# Shared by calculate_next_version.sh (to select minor-breaking vs major bump)
# and resolve_scheduled_release.sh (to decide whether a breaking change halts for human review).
# Keeping the predicate in one place ensures the automated release gate and the version calculator
# agree on what ends initial development.
#
# Arguments: $1 = version tag or string (e.g., "0.4.0", "1.0.0").
ga_tag_is_initial_development() {
  local tag="${1:-}"
  local major
  IFS='.' read -r major _ _ <<< "${tag}"
  if [[ "${major}" =~ ^[0-9]+$ ]] && [ "${major}" -eq 0 ]; then
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

# The https URL of the release repository, which the release scripts use as the
# remote when `origin` is not it: a checkout whose origin is a fork, or a plain
# lookup that should answer for the repository being released regardless.
release_repo_url() {
  echo "https://github.com/$(get_target_repo).git"
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

# Checks whether one fully-qualified image reference exists in its registry.
#
# `docker manifest inspect` is the preferred probe and the only one here that
# works against every registry. It is guarded because not every caller has
# docker: the Prow job image has none — hack/ci-deploy.sh builds through
# `gcloud builds submit` for exactly that reason — and an unguarded call there
# fails for every image, so the caller reports a publish outage when the real
# problem is a missing binary. The fallback is the GHCR registry API, which
# needs only curl and answers anonymously for a public package.
registry_image_exists() {
  local img="$1"

  if command -v docker >/dev/null 2>&1; then
    # Spelled out rather than `docker manifest inspect ...; return`, which
    # propagates $? correctly but only survives errexit while every caller keeps
    # this function in a condition context. They all do today; the next one
    # written as a plain statement would kill the script on a missing image
    # instead of getting a 1 back.
    if docker manifest inspect "${img}" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi

  case "${img}" in
    "${GHCR_REGISTRY_HOST}"/*) ;;
    *)
      echo "⚠️ Warning: cannot probe ${img}: no docker on PATH and no API fallback for this registry." >&2
      return 1
      ;;
  esac

  # Split the reference the way the registry API does, so this branch accepts
  # what the docker branch above accepts. A digest reference separates on `@`,
  # a tag on the last `:` — but only when that colon comes after the last `/`,
  # since a registry host may carry a port. Anything else is the whole path with
  # no reference, which the API spells `latest`.
  local path="${img#"${GHCR_REGISTRY_HOST}"/}"
  local last_segment="${path##*/}"
  local repo reference
  if [ "${path}" != "${path#*@}" ]; then
    repo="${path%%@*}"
    reference="${path#*@}"
  elif [ "${last_segment}" != "${last_segment%:*}" ]; then
    repo="${path%:*}"
    reference="${path##*:}"
  else
    repo="${path}"
    reference="latest"
  fi

  local token
  token="$(curl -fsSL "https://${GHCR_REGISTRY_HOST}/token?scope=repository:${repo}:pull&service=${GHCR_REGISTRY_HOST}" 2>/dev/null |
    sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  if [ -z "${token}" ]; then
    return 1
  fi
  curl -fsSL -o /dev/null -I \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: ${GHCR_MANIFEST_ACCEPT}" \
    "https://${GHCR_REGISTRY_HOST}/v2/${repo}/manifests/${reference}" >/dev/null 2>&1
}

# Whether a GHCR image is there, with the answer the boolean probe above
# cannot give: `present`, `absent` (the registry said 404) or `error` (the
# registry could not be asked, or answered anything else). A caller deciding
# whether to overwrite a tag needs the third answer; a probe failure read as
# "absent" is a rebuild over manifests that were validated. Always exits 0 and
# prints one word; the registry is asked over its API, never through docker,
# whose `manifest inspect` exits 1 for a missing image and an outage alike.
# Arguments: $1 = image reference under GHCR_REGISTRY_HOST
ghcr_image_status() {
  local img="${1:-}"

  case "${img}" in
    "${GHCR_REGISTRY_HOST}"/*) ;;
    *)
      echo "error"
      return 0
      ;;
  esac

  local path="${img#"${GHCR_REGISTRY_HOST}"/}"
  local last_segment="${path##*/}"
  local repo reference
  if [ "${path}" != "${path#*@}" ]; then
    repo="${path%%@*}"
    reference="${path#*@}"
  elif [ "${last_segment}" != "${last_segment%:*}" ]; then
    repo="${path%:*}"
    reference="${path##*:}"
  else
    repo="${path}"
    reference="latest"
  fi

  local token
  token="$(curl -fsSL "https://${GHCR_REGISTRY_HOST}/token?scope=repository:${repo}:pull&service=${GHCR_REGISTRY_HOST}" 2>/dev/null |
    sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  if [ -z "${token}" ]; then
    echo "error"
    return 0
  fi

  local http_code
  if ! http_code="$(curl -sS -o /dev/null -I -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: ${GHCR_MANIFEST_ACCEPT}" \
    "https://${GHCR_REGISTRY_HOST}/v2/${repo}/manifests/${reference}" 2>/dev/null)"; then
    echo "error"
    return 0
  fi
  case "${http_code}" in
    "${HTTP_STATUS_OK}") echo "present" ;;
    "${HTTP_STATUS_NOT_FOUND}") echo "absent" ;;
    *) echo "error" ;;
  esac
}

# ─── Required images, as the candidate lists them ─────────────────────────────
# The names a scripts/release/common.sh text lists in REQUIRED_RELEASE_IMAGES,
# one per line, read from stdin. The parse is the one
# tests/test_promotion_pipeline_wiring.py applies to this file: the block from
# `<name>=(` to the first `)`, every non-blank line in it one double-quoted
# name, no name twice, at least one name. A list that test accepts reads the
# same here, and nothing else does: a comment inside the block, two names on a
# line, an unquoted name, a duplicate or an empty block return 1 with nothing
# printed, so the caller falls back whole rather than act on a partial list.
required_release_images_in_text() {
  awk -v name="${REQUIRED_RELEASE_IMAGES_NAME}" '
    BEGIN { opener = name "=("; count = 0; bad = 0; inblock = 0; closing = 0 }
    !inblock {
      at = index($0, opener)
      if (at == 0) next
      inblock = 1
      $0 = substr($0, at + length(opener))
    }
    {
      close_at = index($0, ")")
      if (close_at > 0) { closing = 1; $0 = substr($0, 1, close_at - 1) }
      if ($0 ~ /^[[:space:]]*"[^"[:space:]]+"[[:space:]]*$/) {
        gsub(/[[:space:]"]/, "")
        if ($0 in seen) bad = 1
        seen[$0] = 1
        names[count++] = $0
      } else if ($0 !~ /^[[:space:]]*$/) {
        bad = 1
      }
      if (closing) exit
    }
    END {
      if (!inblock || bad || count == 0) exit 1
      for (i = 0; i < count; i++) print names[i]
    }
  '
}

# The required release images of a candidate commit, one per line: the
# REQUIRED_RELEASE_IMAGES its own scripts/release/common.sh lists, read with
# `git show <sha>:scripts/release/common.sh` in the current repository. A
# commit's images are the ones its publish run built from its own list, so that
# list, not this checkout's, is what every rung asks the registry for: a
# candidate from before the list grew is not refused for a name it never built,
# and a release line that grew its list by backport is checked, promoted and
# signed for every name it has, however old the main these scripts run from.
# The candidate's list wins in both directions.
#
# When that list cannot be read -- no commit named, a commit this repository
# does not have, no such file at it, a `git show` of it that fails, or an
# array the parse above does not accept -- this checkout's array is printed
# instead and one line on stderr names the reason. That is the behaviour every rung had before this helper,
# so the fallback never refuses or accepts anything the old gate did not; the
# one shape it never takes is an empty list, which would pass the gate with
# nothing checked. Always exits 0; a caller that reads nothing back has a
# broken pipe, not an empty list, and must fail rather than proceed.
# Arguments: $1 = candidate commit-ish
required_release_images_at() {
  local sha="${1:-}" text="" names="" fallback_reason=""
  # The file is read whole before it is parsed, not piped into the parse. The
  # parse stops at the list's closing `)` near the top of a file far larger
  # than a pipe buffer, so `git show` would die on SIGPIPE mid-write and, under
  # the pipefail this file sets, every candidate would read as unreadable
  # (#2542). Captured, git show's own status still says whether the read
  # failed, and a failed read falls back like any other unreadable list.
  if [ -z "${sha}" ]; then
    fallback_reason="no candidate commit named"
  elif ! git rev-parse --verify --quiet "${sha}^{commit}" >/dev/null 2>&1; then
    fallback_reason="commit ${sha:0:7} is not in this repository"
  elif ! git cat-file -e "${sha}:${REQUIRED_RELEASE_IMAGES_PATH}" 2>/dev/null; then
    fallback_reason="${sha:0:7} has no ${REQUIRED_RELEASE_IMAGES_PATH}"
  elif ! text="$(git show "${sha}:${REQUIRED_RELEASE_IMAGES_PATH}" 2>/dev/null)"; then
    fallback_reason="git show could not read ${REQUIRED_RELEASE_IMAGES_PATH} at ${sha:0:7}"
  elif ! names="$(required_release_images_in_text <<<"${text}")"; then
    fallback_reason="${REQUIRED_RELEASE_IMAGES_PATH} at ${sha:0:7} has no readable ${REQUIRED_RELEASE_IMAGES_NAME}"
  fi
  if [ -n "${fallback_reason}" ]; then
    echo "⚠️ Required release images: ${fallback_reason}; using the ${#REQUIRED_RELEASE_IMAGES[@]} this checkout's ${REQUIRED_RELEASE_IMAGES_PATH} lists." >&2
    printf '%s\n' "${REQUIRED_RELEASE_IMAGES[@]}"
    return 0
  fi
  echo "ℹ️ Required release images: the $(grep -c . <<<"${names}") ${REQUIRED_RELEASE_IMAGES_PATH} lists at ${sha:0:7}." >&2
  printf '%s\n' "${names}"
}

# The required release images of a GA version, one per line, for the rungs the
# workflow hands only the version (signing, the SBOMs): required_release_images_at
# on the version's tag commit. A stamped release commit changes the installer
# scripts, the chart and the Terraform example only (create_stamped_release_commit),
# so the tag carries its candidate's scripts/release/common.sh and reads the same
# list the candidate was published from. Without the tag in this repository the
# list is this checkout's, and both this and required_release_images_at say so.
# Arguments: $1 = GA version (X.Y.Z)
required_release_images_for_release() {
  local version="${1:-}" release_commit
  release_commit="$(resolve_release_commit "${version}" 2>/dev/null || true)"
  if [ -z "${release_commit}" ]; then
    echo "⚠️ No tag '${version}' in this repository to read the release's image list at." >&2
  fi
  required_release_images_at "${release_commit}"
}

# Checks if all required candidate container images exist in GHCR for a specific
# commit SHA: the images the commit's own scripts/release/common.sh lists
# (required_release_images_at), since those are what its publish run built.
check_commit_images_exist() {
  local sha="$1"
  local registry_prefix
  registry_prefix="$(get_registry_prefix)"

  local images=() img
  while IFS= read -r img; do images+=("${img}"); done < <(required_release_images_at "${sha}")
  if [ "${#images[@]}" -eq 0 ]; then
    echo "❌ ERROR: no required release images resolved for commit ${sha:0:7}; refusing to call its images complete." >&2
    return 1
  fi

  for img in "${images[@]}"; do
    local target_img="${registry_prefix}/${img}:${sha}"
    if ! registry_image_exists "${target_img}"; then
      return 1
    fi
  done
  return 0
}

# Finds the RC pipeline's own rc_<ts>_<sha> tag on a commit, for resolve_rc_tag.sh to
# reuse; empty when there is none. Matched by name (rc_tag_name_regex, the sha field
# bound to this commit), not the rc_* glob and not the bare shape: a hand-named
# `rc_tag` dispatch input, whether `rc_hotfix` or a pipeline-shaped name carrying
# another commit's sha, earns `<name>_validated`, which a release line's gate does
# not read (validated_rc_tags_at_commit, the same name rule), and reusing that name
# on the next dispatch would re-earn the same refused marker. A re-dispatch with the
# input empty mints the pipeline's name beside the hand-named tag instead, and the
# gate clears.
get_existing_rc_tag() {
  local sha="$1" full tags
  full="$(git rev-parse --verify --quiet "${sha}^{commit}" 2>/dev/null || echo "${sha}")"
  tags="$(git tag --points-at "${full}" "rc_*" 2>/dev/null | grep -E "$(rc_tag_name_regex "${full}")" || true)"
  head -n 1 <<<"${tags}"
}

# Checks if a commit SHA has already been attempted in a previous RC run: any
# unvalidated rc_* tag, hand-named included, since a hand dispatch is an attempt too.
is_commit_already_attempted() {
  local sha="$1"
  [ -n "$(git tag --points-at "${sha}" "rc_*" 2>/dev/null | grep -v '_validated$' || true)" ]
}

# Checks if a commit SHA carries the RC pipeline's validation marker (rc_*_validated).
#
# Anchored to the rc_ family, and named for it: this gates resolve_rc_tag.sh's
# skip decision and the nightly promotion, so a marker minted by some other tag
# family must not read as an RC validation. get_latest_validated_rc_tag anchors
# the same way. For a release from main the GA gate reads the staging family
# alone and takes the RC validation as implied by it (STAGING_TAG_SHAPE_REGEX
# below); for a release line it reads this family by the pipeline's own name
# for the commit (validated_rc_tags_at_commit, rc_validated_tag_name_regex).
#
# The glob, not the shape: this is the "already tried" marker the RC scheduler
# and the nightly read, and a hand-placed marker suppressing a re-validation is
# the operator's own doing. The release line's gate is the stricter question,
# "did the RC pipeline pass here", and reads the shape (validated_rc_tags_at_commit).
is_rc_candidate_commit_already_validated() {
  local sha="${1:-}"
  [ -n "$(git tag --points-at "${sha}" "rc_*_validated" 2>/dev/null || true)" ]
}

# ─── Promotion tag cores ──────────────────────────────────────────────────────
# Two tag families are derived from a validated RC tag — evalcand_ below and
# staging_ after it — and the design rests on them sharing a core. A reader who
# sees evalcand_2608241820_b35543c has to be able to name the staging_ tag it
# becomes without a lookup, and resolve_promotion_candidate.sh composes both from
# the same rc_ tag and expects them to agree. One extractor rather than two is
# what makes that structural instead of a convention two functions happen to
# follow.
#
# `family` names what the caller is composing, and appears only in the refusal.
rc_tag_core() {
  local rc_tag="${1:-}"
  local family="${2:-promotion}"
  if [ -z "${rc_tag}" ]; then
    echo "❌ ERROR: an RC tag is required to derive a ${family} tag." >&2
    return 1
  fi

  # The _validated suffix is dropped: it records that the RC gate passed, not
  # that anything downstream of it did.
  local core="${rc_tag%_validated}"
  case "${core}" in
    rc_?*) core="${core#rc_}" ;;
    *)
      echo "❌ ERROR: '${rc_tag}' is not an rc_* candidate tag; refusing to derive a ${family} tag from it." >&2
      return 1
      ;;
  esac

  printf '%s\n' "${core}"
}

# ─── Eval-candidate tags ──────────────────────────────────────────────────────
# The staging promotion pipeline nominates a candidate for staging by tagging
# its commit evalcand_<ts>_<sha>. That push is what fires
# post-kube-agents-eval-rc, the release-candidate eval in
# GoogleCloudPlatform/oss-test-infra, and the pipeline then waits for that job's
# verdict before pushing the staging_ tag below.
#
# WHY A SECOND TAG FAMILY AND NOT JUST staging_. The eval used to trigger on
# staging_, which is also what staging-deploy.yml triggers on: both fired off one
# push event, and the deploy — minutes — finished long before the eval — hours —
# had a verdict, so the verdict could only ever describe a deploy that had
# already happened. evalcand_ puts the eval between the two.
#
# WHAT READS IT. resolve_promotion_candidate.sh, and only it: a commit already
# carrying an evalcand_ tag has been answered about, so it skips. That makes the
# leftover tag on a rejected candidate the thing that keeps it rejected, rather
# than an inert marker — which is why drop_eval_candidate.sh exists for the runs
# where no verdict arrived at all. Every other tag reader here is prefix-scoped
# to a family that is not this one. A rejected candidate leaving a staging_ tag
# behind would be worse than either: get_latest_staging_tag would offer it to a
# manual deploy dispatch.
export EVALCAND_TAG_PREFIX="evalcand_"

# Derives the eval-candidate tag from a validated RC tag:
#   rc_2608241820_b35543c_validated  ->  evalcand_2608241820_b35543c
evalcand_tag_for_rc() {
  local core
  core="$(rc_tag_core "${1:-}" "eval-candidate")" || return 1
  printf '%s\n' "${EVALCAND_TAG_PREFIX}${core}"
}

# The shape an eval-candidate tag must have, mirrored by the `branches` regex on
# post-kube-agents-eval-rc in GoogleCloudPlatform/oss-test-infra. Change one and
# the other stops matching: the pipeline pushes a tag no eval fires on, waits out
# its poll deadline, and promotes nothing.
#
# Shape and not the bare prefix, for the reason STAGING_TAG_SHAPE_REGEX gives
# below — a prefix is a trigger anyone can push by hand, and here the cost of an
# `evalcand_hotfix` typed at a terminal is a project taken out of a pool shared
# with the merge-blocking presubmit for hours.
export EVALCAND_TAG_SHAPE_REGEX='^evalcand_[0-9]{10}_[0-9a-f]{7}$'

# Lists the shape-valid eval-candidate tags pointing at a commit, one per line.
# Empty output means this commit has never been nominated.
evalcand_tags_at_commit() {
  local sha="${1:-}"
  local tags
  tags="$(git tag --points-at "${sha}" "${EVALCAND_TAG_PREFIX}*" 2>/dev/null || true)"
  grep -E "${EVALCAND_TAG_SHAPE_REGEX}" <<<"${tags}" || true
}

# Finds an existing eval-candidate tag on a commit SHA, if any. Empty output
# means the commit has not been nominated yet.
#
# This is what resolve_promotion_candidate.sh will read to set `skip_promotion`,
# a job get_existing_staging_tag does today. The move is the point of the split:
# the staging_ tag will appear only on a green verdict, so keying the skip on it
# would re-nominate every candidate the eval had already rejected, once per
# nightly, each attempt costing hours of a leased project to reach the same
# answer. The evalcand_ tag records that the candidate was measured, which is the
# question being asked.
#
# "Measured" and not "attempted", which is a distinction the tag alone cannot
# carry and the pipeline supplies: a nomination whose eval never returned a
# verdict has its tag deleted again, so the commit is eligible tomorrow. Read
# this function as answering "has this candidate been answered about", because
# by the time anything calls it that is what the tag's presence means.
#
# A missed lookup is therefore not free, but it is self-correcting. It leads to a
# redundant nomination, `ensure_git_tag` no-ops because the tag already points at
# the same commit, nothing is pushed, no eval fires, and the poll reports
# never_ran at its 45-minute clock — unsettled, so the tag is dropped and the
# next nightly nominates cleanly. The cost is one night, not a stranded
# candidate, which is why erring towards not-yet-nominated remains the safe
# direction here even though the no-op is the failure rather than the harmless
# case.
get_existing_evalcand_tag() {
  local sha="$1"
  local tags
  tags="$(evalcand_tags_at_commit "${sha}")"
  # First line by parameter expansion rather than `head -n 1`, for the reason
  # get_latest_staging_tag gives below: under `set -o pipefail` head closing the
  # pipe early makes the producer exit 141, and the `|| echo ""` that usually
  # sits beside it would read that as "not nominated".
  [ -n "${tags}" ] && printf '%s\n' "${tags%%$'\n'*}"
  return 0
}

# ─── Staging promotion tags ───────────────────────────────────────────────────
# The nightly pipeline promotes a candidate by tagging its commit
# staging_<ts>_<sha>, which is what staging-deploy.yml triggers on. It pushes
# this only after the eval fired by the evalcand_ tag above returns green; a red,
# timed-out or never-started eval leaves staging on the build it is running.
export STAGING_TAG_PREFIX="staging_"

# Derives the staging promotion tag from a validated RC tag:
#   rc_2608241820_b35543c_validated  ->  staging_2608241820_b35543c
#
# The timestamp stays first after the prefix so `git tag -l --sort=-v:refname
# 'staging_*'` orders by time, and the transform is mechanical in both
# directions, so a staging tag reads back to its candidate — and to the
# evalcand_ tag it shares a core with — without a lookup.
#
# Refuses anything outside the rc_ family rather than composing staging_<junk>,
# because the result is a live deploy trigger.
staging_tag_for_rc() {
  local core
  core="$(rc_tag_core "${1:-}" "staging")" || return 1
  printf '%s\n' "${STAGING_TAG_PREFIX}${core}"
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
# this commit and the eval that followed it came back green". The timestamp and
# short SHA in the right places are not produced by accident.
#
# It stops an accident, not an attacker. Nothing checks that the 7-hex field is
# the short SHA of the commit the tag points at, or that the commit carries
# rc_*_validated, so a deliberately composed `staging_<ts>_<sha>` satisfies the
# gate. That is no weaker than the rc_*_validated gate it replaces — equally a
# tag anyone with push access could create — but it is not the stronger
# guarantee the shape makes it look like.
export STAGING_TAG_SHAPE_REGEX='^staging_[0-9]{10}_[0-9a-f]{7}$'
# The RC pipeline's own names for a commit: `rc_<ts>_<its short sha>`, as
# resolve_rc_tag.sh mints it when the dispatch names none, and that with
# `_validated` appended, which tag_validated_release.sh places when the suite
# passes. The release line's gate and the name reuse both match these, with the
# sha field bound to the commit in question, rather than the `rc_*` globs or the
# bare shape: a hand-typed `rc_hotfix_validated` and a composed
# `rc_<ts>_0000000_validated` must not read as "the RC suite passed here", and a
# hand-named tag must not be the name the next dispatch reuses (see
# get_existing_rc_tag). The pipeline never mints anything else, so a genuine
# validation loses nothing.
export RC_TAG_TIMESTAMP_PREFIX_REGEX='^rc_[0-9]{10}_'
# Arguments: $1 = commit sha (full or short; the first seven characters are the field)
rc_tag_name_regex() {
  echo "${RC_TAG_TIMESTAMP_PREFIX_REGEX}${1:0:7}\$"
}
rc_validated_tag_name_regex() {
  echo "${RC_TAG_TIMESTAMP_PREFIX_REGEX}${1:0:7}_validated\$"
}

# Finds the newest shape-valid staging promotion tag on main. Empty output
# means nothing has been promoted to staging.
#
# `--sort=-v:refname` orders by the timestamp immediately after the prefix, which
# is why staging_tag_for_rc puts it there. The list is materialised before it is
# filtered rather than piped into `grep | head`: under `set -o pipefail` head
# closing the pipe early makes grep exit 141, which a trailing `|| echo ""` then
# turns into "nothing has passed the gate" — a skipped release, silently, once
# the tag list outgrows a pipe buffer.
# On main, like the rc_ pickers (list_tags_on_main): the GA gate, the publish
# auto-resolve, the version calculator and the staging deploy all read this,
# and a staging_ tag a hand-dispatched promotion left on a release-line commit
# must not become main's release candidate.
# Arguments: $1 = main's commit, already resolved (optional; see list_tags_on_main)
get_latest_staging_tag() {
  local tags
  tags="$(list_tags_on_main "${STAGING_TAG_PREFIX}*" "${1:-}")" || return 1
  grep -m1 -E "${STAGING_TAG_SHAPE_REGEX}" <<<"${tags}" || true
}

# Lists the shape-valid staging promotion tags pointing at a commit, one per
# line. Empty output means the commit has not been promoted, which since the
# eval gate no longer implies it failed the nightly matrix: a candidate whose
# matrix was green is unpromoted while its eval runs, and stays unpromoted if
# that eval comes back red. `evalcand_tags_at_commit` is what tells those apart.
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
# here means a hand-pushed `staging_hotfix` — which staging-deploy.yml
# legitimately triggers on, its `tags:` pattern being `staging_*` — tells the
# nightly the commit is already promoted. It
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
# staging-promotion-pipeline.yml takes an `rc_tag` dispatch input whose description offers
# any validated candidate, and the tag graph keeps every candidate it ever
# validated. Naming one by hand is a supported thing to do and stays wrong for
# the same reason it is wrong today.
#
# The markers probe epoch boundaries — points at which the workflows started
# driving the candidate's tree in a way an older tree cannot answer — and not
# the general question of whether a tree can be driven by these workflows. Ten
# scripts run out of the candidate's checkout; these sample three. That is sound
# for the boundaries they were chosen for, because each arrived in the commit
# that created one. A later restructure that adds a seam needs its own marker
# here; this function will not notice on its own.
#
# Boundary 1, the shared-pipeline restructure: run_optional_e2e_suites.sh and
# the E2E_SUITE selector. Boundary 2, the in-place reconcile: the nightly checks
# the candidate OUT to reconcile staging at it, so a tree without
# reconcile_environment.sh aborts the reconcile step on a missing file. That
# failure is not self-announcing, because the promotion is deliberately
# decoupled from the reconcile's outcome — the staging tag would still be
# pushed, staging's images would move, and its infrastructure would stay exactly
# as stale as before.
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

  git cat-file -e "${sha}:scripts/release/reconcile_environment.sh" 2>/dev/null || return 1

  return 0
}

# Reports whether the staging deploy AT A GIVEN COMMIT would start on a given
# tag, by reading the `push: tags:` patterns out of that commit's own copy of
# staging-deploy.yml (or legacy staging-redeploy-agent.yml for older commits).
#
# A push event runs the workflows in the pushed ref's tree, not the ones on the
# default branch, and a promotion tag lands on a candidate commit that can be days
# old. So the question of whether a tag deploys anything is answered by the
# candidate, and a promotion pushed at a commit whose trigger does not match the
# tag succeeds, deploys nothing, and reports green — after which
# get_existing_staging_tag sees the tag and no later run retries that candidate.
staging_trigger_matches_at_commit() {
  local commit="${1:-}" tag="${2:-}"
  local workflow=".github/workflows/staging-deploy.yml"
  local yaml patterns pattern

  if [ -z "${commit}" ] || [ -z "${tag}" ]; then
    echo "❌ ERROR: a commit and a tag are required for staging_trigger_matches_at_commit." >&2
    return 2
  fi

  yaml="$(git show "${commit}:${workflow}" 2>/dev/null)" || {
    # Backward compatibility with candidates that predate the consolidation to staging-deploy.yml
    workflow=".github/workflows/staging-redeploy-agent.yml"
    yaml="$(git show "${commit}:${workflow}" 2>/dev/null)" || return 1
  }

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

  release_push_ref "${rc_tag}" "Git tag '${rc_tag}'"
}

# Pushes refs to the release repository in one atomic push: all of them land or
# none does. `origin` first, then the plain https URL of the target repository.
# Never `--force`: a ref the remote already holds at another commit is refused
# there, the whole push with it, and the error names it.
# Arguments: $1 = what is being pushed, for the messages; $2... = refspecs
release_push_refs() {
  local label="${1:-}"
  shift || true

  if [ $# -eq 0 ]; then
    echo "❌ ERROR: at least one refspec is required for release_push_refs." >&2
    return 1
  fi

  local target_repo
  target_repo="$(get_target_repo)"

  local fallback_url
  fallback_url="$(release_repo_url)"

  local origin_err fallback_err
  if origin_err=$(git push --atomic origin "$@" 2>&1); then
    echo "✅ ${label} successfully pushed to remote repository (${target_repo})!"
  elif fallback_err=$(git push --atomic "${fallback_url}" "$@" 2>&1); then
    echo "✅ ${label} successfully pushed to remote repository (${target_repo})!"
  else
    # Both attempts are reported: in CI they name the same repository and
    # usually agree, but a mis-set GH_ORG/GH_REPO makes the fallback fail for a
    # reason of its own, and origin's is then the one that says what happened.
    echo "❌ ERROR: Could not push ${label} to remote repository (${target_repo})." >&2
    echo "   origin: ${origin_err}" >&2
    echo "   ${fallback_url}: ${fallback_err}" >&2
    return 1
  fi
}

# One ref, the form the candidate-rung taggers use.
# Arguments: $1 = refspec, $2 = what it is, for the messages ("Git tag '0.7.0'")
release_push_ref() {
  local refspec="${1:-}"
  local label="${2:-ref '${1:-}'}"

  if [ -z "${refspec}" ]; then
    echo "❌ ERROR: a refspec is required for release_push_ref." >&2
    return 1
  fi
  release_push_refs "${label}" "${refspec}"
}

# The release line a version belongs to: `X.Y.Z` -> `X.Y`.
release_line_for_version() {
  local version="${1:-}"
  validate_pure_numeric_semver "${version}" "Version" || return 1
  echo "${version%.*}"
}

# The branch of a release line: `X.Y` -> `release/X.Y`.
release_branch_for_line() {
  local line="${1:-}"
  if ! [[ "${line}" =~ ${RELEASE_LINE_SHAPE_REGEX} ]]; then
    echo "❌ ERROR: '${line}' is not a release line; expected X.Y." >&2
    return 1
  fi
  echo "${RELEASE_BRANCH_PREFIX}${line}"
}

# Prints the commit a tag points at on the release repository (peeled, so an
# annotated tag reads as its commit), empty when the remote has no such tag,
# and fails when the remote cannot be read.
# Arguments: $1 = tag name
release_tag_remote_commit() {
  local tag="${1:-}"

  if [ -z "${tag}" ]; then
    echo "❌ ERROR: a tag name is required for release_tag_remote_commit." >&2
    return 1
  fi

  local remote_url
  remote_url="$(release_repo_url)"

  local listing
  if ! listing="$(git ls-remote --tags "${remote_url}" "refs/tags/${tag}" "refs/tags/${tag}^{}" 2>&1)"; then
    echo "❌ ERROR: Could not read tags of ${remote_url}: ${listing}" >&2
    return 1
  fi
  # The peeled line (`^{}`) is the commit; the plain line is the tag object for an
  # annotated tag and the commit itself for a lightweight one.
  local peeled plain
  peeled="$(awk -v ref="refs/tags/${tag}^{}" '$2 == ref { print $1 }' <<<"${listing}")"
  plain="$(awk -v ref="refs/tags/${tag}" '$2 == ref { print $1 }' <<<"${listing}")"
  echo "${peeled:-${plain}}"
}

# The commit the release repository's copy of a branch points at, or nothing
# when it has none. The match is exact: ls-remote's own pattern is tail-matched,
# so `refs/heads/release/0.7` alone would also answer for a stray
# `x/refs/heads/release/0.7`. A remote that cannot be read is an error rather
# than an empty answer: in CI the remote is the truth about the branch, and a
# guess of "absent" would let a plain push fast-forward a branch that exists.
# Arguments: $1 = branch ref (refs/heads/...)
release_branch_remote_commit() {
  local branch_ref="${1:-}"

  if [ -z "${branch_ref}" ]; then
    echo "❌ ERROR: a branch ref is required for release_branch_remote_commit." >&2
    return 1
  fi

  local remote_url
  remote_url="$(release_repo_url)"

  local listing
  if ! listing="$(git ls-remote --heads "${remote_url}" "${branch_ref}" 2>&1)"; then
    echo "❌ ERROR: Could not read branches of ${remote_url}: ${listing}" >&2
    return 1
  fi
  awk -v ref="${branch_ref}" '$2 == ref { print $1 }' <<<"${listing}"
}

# The head of a release line: on the release repository in CI (fetched into
# this checkout if it is not already here), the local branch off CI. An
# unreadable remote or a line that does not exist is an error.
# Arguments: $1 = line (X.Y)
release_line_head() {
  local line="${1:-}" branch branch_ref head
  branch="$(release_branch_for_line "${line}")" || return 1
  branch_ref="${GIT_BRANCH_REF_PREFIX}${branch}"

  if is_ci_pipeline; then
    head="$(release_branch_remote_commit "${branch_ref}")" || return 1
    if [ -z "${head}" ]; then
      echo "❌ ERROR: No release line '${branch}' on $(get_target_repo)." >&2
      return 1
    fi
    if ! git rev-parse --verify --quiet "${head}^{commit}" >/dev/null 2>&1; then
      git fetch "$(release_repo_url)" "${branch_ref}" >/dev/null 2>&1 || true
    fi
    if ! git rev-parse --verify --quiet "${head}^{commit}" >/dev/null 2>&1; then
      echo "❌ ERROR: The head of '${branch}' (${head}) is not in this checkout and could not be fetched." >&2
      return 1
    fi
    echo "${head}"
    return 0
  fi

  # Off CI: the local branch, else the tracking ref a developer's clone holds.
  if ! head="$(git rev-parse --verify --quiet "${branch_ref}" 2>/dev/null)" &&
    ! head="$(git rev-parse --verify --quiet "refs/remotes/origin/${branch}" 2>/dev/null)"; then
    echo "❌ ERROR: No local release line '${branch}'." >&2
    return 1
  fi
  echo "${head}"
}

# Whether a release line exists: on the release repository in CI, locally off
# CI. Returns 2 when the remote cannot be read, so a caller fails closed rather
# than treat "unknown" as "no".
# Arguments: $1 = line (X.Y)
release_line_branch_exists() {
  local line="${1:-}" branch branch_ref head
  branch="$(release_branch_for_line "${line}")" || return 2
  branch_ref="${GIT_BRANCH_REF_PREFIX}${branch}"
  if is_ci_pipeline; then
    head="$(release_branch_remote_commit "${branch_ref}")" || return 2
  else
    head="$(git rev-parse --verify --quiet "${branch_ref}" 2>/dev/null ||
      git rev-parse --verify --quiet "refs/remotes/origin/${branch}" 2>/dev/null || true)"
  fi
  [ -n "${head}" ]
}

# The commit a release line's next patch is cut from: the head, unless the head
# is a GA release's stamped commit, in which case the commit that stamp was cut
# from. That is what makes an idle line, or a re-run after its tag was pushed,
# resolve to the same candidate as the run that released it, and so land in the
# eligibility check's idempotent path rather than as a new release of nothing.
#
# A version names a release to resume instead: when its tag exists on the line
# (a stamped commit whose parent is in the head's history), the candidate is
# that parent, whatever has merged since. That is the pin for a line release
# that died after its push and whose line moved on before the re-run — on main
# the same pin is target_commit — so the re-run finishes that release rather
# than cutting the next patch from the new head and leaving a tag with nothing
# published behind it.
# Arguments: $1 = line (X.Y), $2 = version to resume (optional)
release_line_candidate() {
  local line="${1:-}" version="${2:-}" head tag tag_commit
  head="$(release_line_head "${line}")" || return 1
  if [ -n "${version}" ] && tag_commit="$(git rev-parse --verify --quiet "refs/tags/${version}^{commit}" 2>/dev/null)"; then
    if ga_tag_is_stamped "${version}" "${tag_commit}" &&
      git merge-base --is-ancestor "${tag_commit}^1" "${head}" 2>/dev/null; then
      git rev-parse --verify "${tag_commit}^1"
      return 0
    fi
  fi
  while IFS= read -r tag; do
    [ -n "${tag}" ] || continue
    if ga_tag_is_stamped "${tag}" "${head}"; then
      git rev-parse --verify "${head}^1"
      return 0
    fi
  done < <(git tag --points-at "${head}" 2>/dev/null | grep -E "${GA_TAG_SHAPE_REGEX}" || true)
  echo "${head}"
}

# The line's candidate, or an error when a commit named alongside the line is
# not it. The calculator and the eligibility check both resolve through this,
# so the two cannot disagree about which commit a line releases: the branch
# step can only fast-forward from the head, and the line's branch protection,
# once it exists, vouches for the head alone.
# Arguments: $1 = line (X.Y), $2 = commit-ish named by the caller (optional),
#            $3 = version to resume (optional; see release_line_candidate)
release_line_resolve_candidate() {
  local line="${1:-}" named="${2:-}" version="${3:-}" candidate named_sha
  candidate="$(release_line_candidate "${line}" "${version}")" || return 1
  if [ -n "${named}" ] && [ "${named}" != "null" ]; then
    named_sha="$(git rev-parse --verify "${named}^{commit}" 2>/dev/null || echo "")"
    if [ "${named_sha}" != "${candidate}" ]; then
      echo "❌ ERROR: Release line ${line} resolves to its own head (${candidate:0:7}), not to '${named}'. Either a target commit was named — a line takes none — or the line moved since it was last read. Nothing has been pushed; re-run without a target commit." >&2
      return 1
    fi
  fi
  echo "${candidate}"
}

# The RC validation markers on a commit, one per line: a release line's gate.
# Matched to the pipeline's own name for this commit (rc_validated_tag_name_regex;
# RC_TAG_TIMESTAMP_PREFIX_REGEX says why that and not the glob or the bare shape),
# on a branch whose only gate this is.
validated_rc_tags_at_commit() {
  local sha="${1:-}" full tags
  full="$(git rev-parse --verify --quiet "${sha}^{commit}" 2>/dev/null || echo "${sha}")"
  tags="$(git tag --points-at "${full}" "rc_*_validated" 2>/dev/null || true)"
  grep -E "$(rc_validated_tag_name_regex "${full}")" <<<"${tags}" || true
}

# Where a version's release line is, relative to the release commit and the
# candidate it was stamped from: prints `remote` or `local` (at the release
# commit, on the release repository or in this checkout only), `remote-candidate`
# or `local-candidate` (at the candidate, which is the stamped commit's parent,
# so the branch has a fast-forward ahead of it), `remote-past` or `local-past`
# (already beyond the release commit, which is where a re-run finds a line that
# took a merge after the release's push landed: nothing to move), or `absent`. A
# branch anywhere else is an error naming both commits, and so is a remote that
# cannot be read. In CI the remote is what is read: a local branch the remote
# lacks is a leftover (a dry run's, or a killed run's) and reads as `absent`,
# and a local branch that has moved on from the remote's line is an error; off
# CI the local branch stands in for the remote. Read-only; ensure_ga_release_refs
# reads it before anything is pushed.
# Arguments: $1 = version, $2 = release commit, $3 = candidate commit (optional)
release_branch_placement() {
  local version="${1:-}"
  local release_commit="${2:-}"
  local candidate="${3:-}"

  if [ -z "${version}" ] || [ -z "${release_commit}" ]; then
    echo "❌ ERROR: version and release commit are required for release_branch_placement." >&2
    return 1
  fi

  local line branch branch_ref
  line="$(release_line_for_version "${version}")" || return 1
  branch="$(release_branch_for_line "${line}")" || return 1
  branch_ref="${GIT_BRANCH_REF_PREFIX}${branch}"

  local target_full_sha candidate_full_sha=""
  target_full_sha="$(git rev-parse --verify "${release_commit}^{commit}" 2>/dev/null || echo "${release_commit}")"
  if [ -n "${candidate}" ]; then
    candidate_full_sha="$(git rev-parse --verify "${candidate}^{commit}" 2>/dev/null || echo "${candidate}")"
  fi

  # Names what a branch at `sha` is, or fails naming where it is instead. A
  # commit the checkout does not hold yet is fetched first, so the ancestry
  # test can read it.
  classify() {
    local sha="$1" where="$2" whose="$3"
    if [ "${sha}" = "${target_full_sha}" ]; then
      echo "${where}"
      return 0
    fi
    if [ -n "${candidate_full_sha}" ] && [ "${sha}" = "${candidate_full_sha}" ]; then
      echo "${where}-candidate"
      return 0
    fi
    if ! git rev-parse --verify --quiet "${sha}^{commit}" >/dev/null 2>&1; then
      git fetch "$(release_repo_url)" "${branch_ref}" >/dev/null 2>&1 || true
    fi
    if git merge-base --is-ancestor "${target_full_sha}" "${sha}" 2>/dev/null; then
      echo "${where}-past"
      return 0
    fi
    echo "❌ ERROR: Release line '${branch}' already exists ${whose} but points to commit ${sha}, not the release commit ${target_full_sha}${candidate_full_sha:+ or its candidate ${candidate_full_sha}}!" >&2
    return 1
  }

  local local_sha
  local_sha="$(git rev-parse --verify --quiet "${branch_ref}" 2>/dev/null || true)"

  # Whether a local copy of the line, in CI, is one this run may move: at the
  # release commit or its candidate, a stamp of this version (what a dry run or a
  # run killed before its push leaves), or behind the remote's head. Anything else
  # holds work the remote never took, which no run may drop from the branch.
  local_is_leftover() {
    local sha="$1" remote="$2"
    [ "${sha}" = "${target_full_sha}" ] && return 0
    [ -n "${candidate_full_sha}" ] && [ "${sha}" = "${candidate_full_sha}" ] && return 0
    [ "$(git log -1 --format=%s "${sha}" 2>/dev/null)" = "${RELEASE_STAMP_SUBJECT_PREFIX} ${version}" ] && return 0
    [ -n "${remote}" ] && git merge-base --is-ancestor "${sha}" "${remote}" 2>/dev/null && return 0
    return 1
  }

  if is_ci_pipeline; then
    local remote_sha
    remote_sha="$(release_branch_remote_commit "${branch_ref}")" || return 1
    # In CI the remote decides, and a local branch is a leftover or a mistake: a
    # fresh checkout has no local release/ branch at all. A leftover (a dry run's,
    # or a killed run's, or a rejected push's before the take-back existed) is
    # moved, since reading it would refuse the re-run once the candidate has moved
    # on; a branch that has moved on from the remote's line is refused before
    # anything is moved.
    local placement
    if [ -n "${remote_sha}" ]; then
      placement="$(classify "${remote_sha}" "remote" "on $(get_target_repo)")" || return 1
    fi
    if [ -n "${local_sha}" ] && ! local_is_leftover "${local_sha}" "${remote_sha}"; then
      echo "❌ ERROR: Release line '${branch}' in this checkout is at ${local_sha}: not the release commit, its candidate, a stamp of ${version}, or behind $(get_target_repo)'s line, so it holds work the remote never took. Move it aside (git branch -m) and re-run; nothing has been pushed." >&2
      return 1
    fi
    if [ -n "${remote_sha}" ]; then
      echo "${placement}"
      return 0
    fi
    if [ -n "${local_sha}" ]; then
      echo "ℹ️ Release line '${branch}' exists only in this checkout, at ${local_sha:0:7}: left behind by a run that did not push. Recreating it at release commit ${target_full_sha:0:7}." >&2
    fi
    echo "absent"
    return 0
  fi

  if [ -n "${local_sha}" ]; then
    classify "${local_sha}" "local" "locally"
    return
  fi
  echo "absent"
}

# Sets the local copy of a release line to the release commit according to its
# placement: created when absent, left alone when already there, fast-forwarded
# from the candidate otherwise. Not while it is checked out: update-ref would
# advance HEAD under a worktree still at the candidate, leaving the stamp staged
# as a reversal.
# Arguments: $1 = placement, $2 = branch, $3 = branch ref, $4 = release commit
set_release_branch_locally() {
  local placement="${1:-}" branch="${2:-}" branch_ref="${3:-}" target_full_sha="${4:-}"
  case "${placement}" in
    absent)
      # --force for the leftover release_branch_placement reads as absent in CI;
      # git still refuses to move a branch that is checked out.
      if ! git branch --force "${branch}" "${target_full_sha}"; then
        echo "❌ ERROR: Could not set release line '${branch}' to release commit ${target_full_sha:0:7} in this checkout; nothing has been pushed." >&2
        return 1
      fi
      ;;
    local | remote | local-past | remote-past) ;;
    remote-candidate | local-candidate)
      if [ "$(git symbolic-ref -q --short HEAD 2>/dev/null || true)" = "${branch}" ]; then
        echo "❌ ERROR: Release line '${branch}' is checked out here; switch to another branch before releasing from it." >&2
        return 1
      fi
      # A compare-and-swap against the value read here, and a failure is a
      # failure: the ref moved between the read and the swap, or something else
      # holds it, and a run that read that as done would push a line that is
      # not at the release commit.
      local local_sha
      local_sha="$(git rev-parse --verify --quiet "${branch_ref}" 2>/dev/null || true)"
      if [ -n "${local_sha}" ]; then
        if ! git update-ref "${branch_ref}" "${target_full_sha}" "${local_sha}"; then
          echo "❌ ERROR: Could not fast-forward release line '${branch}' from ${local_sha:0:7} to release commit ${target_full_sha:0:7} in this checkout: the ref moved or is held. Nothing has been pushed; re-run." >&2
          return 1
        fi
      elif ! git branch "${branch}" "${target_full_sha}"; then
        echo "❌ ERROR: Could not create release line '${branch}' at release commit ${target_full_sha:0:7} in this checkout; nothing has been pushed." >&2
        return 1
      fi
      echo "➡️ Release line '${branch}' fast-forwards to release commit ${target_full_sha:0:7}."
      ;;
    *)
      echo "❌ ERROR: Unexpected release line placement '${placement}'." >&2
      return 1
      ;;
  esac
}

# The GA rung: the tag and the release line, pushed in one atomic push so that
# neither can exist on the remote without the other. That is what makes the run
# repeatable whatever happens around it: a merge that lands on the line between
# the placement check and the push rejects both refs, nothing is published, and
# the re-run stamps from the new head; a run that dies after the push re-runs
# like one that failed at image promotion, reusing the tagged commit, and a
# line that took a merge in between is already past the release commit and is
# left where it is. A tag or a line already on the remote at the release commit
# is skipped, and whichever is missing is pushed alone; "already there" is read
# from the remote, so a re-run in the checkout a rejected push left its tag in
# pushes the tag too. Off CI both are set locally and neither is pushed.
# Arguments: $1 = version, $2 = release commit, $3 = candidate commit
ensure_ga_release_refs() {
  local version="${1:-}"
  local release_commit="${2:-}"
  local candidate="${3:-}"

  if [ -z "${version}" ] || [ -z "${release_commit}" ]; then
    echo "❌ ERROR: version and release commit are required for ensure_ga_release_refs." >&2
    return 1
  fi

  local placement
  placement="$(release_branch_placement "${version}" "${release_commit}" "${candidate}")" || return 1

  local line branch branch_ref
  line="$(release_line_for_version "${version}")" || return 1
  branch="$(release_branch_for_line "${line}")" || return 1
  branch_ref="${GIT_BRANCH_REF_PREFIX}${branch}"

  local target_full_sha
  target_full_sha="$(git rev-parse --verify "${release_commit}^{commit}" 2>/dev/null || echo "${release_commit}")"

  release_fetch_tags

  local tag_ref="refs/tags/${version}" refspecs=() push_tag="false" push_line="false"
  local existing_tag_sha prior_branch_sha
  existing_tag_sha="$(git rev-parse --verify --quiet "${tag_ref}^{commit}" 2>/dev/null || true)"
  prior_branch_sha="$(git rev-parse --verify --quiet "${branch_ref}" 2>/dev/null || true)"

  # In CI the remote decides whether the tag is still to be pushed, not this
  # checkout: a dry run, or a run killed before its push, leaves the local tag
  # behind (release_fetch_tags does not prune), and a CI run in that checkout
  # that read the local tag as "already there" would push the line alone, which
  # is the state the atomic push exists to rule out. A local tag the remote lacks
  # is that leftover, and is recreated at the release commit, which after a merge
  # on the line is a new stamp. Off CI nothing is pushed and the local tag is what
  # a dry run inspects, so there the local read stands.
  if is_ci_pipeline; then
    local remote_tag_sha
    remote_tag_sha="$(release_tag_remote_commit "${version}")" || return 1
    if [ -n "${remote_tag_sha}" ] && [ "${remote_tag_sha}" != "${target_full_sha}" ]; then
      echo "❌ ERROR: Tag '${version}' already exists on $(get_target_repo) but points to commit ${remote_tag_sha}, not target SHA ${target_full_sha}!" >&2
      return 1
    fi
    if [ -z "${remote_tag_sha}" ] && [ -n "${existing_tag_sha}" ]; then
      echo "ℹ️ Git tag '${version}' exists only in this checkout, at ${existing_tag_sha:0:7}: left behind by a push that did not land. Recreating it at release commit ${target_full_sha:0:7}."
      git tag -d "${version}" >/dev/null
      existing_tag_sha=""
    fi
  fi

  if [ -n "${existing_tag_sha}" ]; then
    if [ "${existing_tag_sha}" != "${target_full_sha}" ]; then
      echo "❌ ERROR: Tag '${version}' already exists but points to commit ${existing_tag_sha}, not target SHA ${target_full_sha}!" >&2
      return 1
    fi
    echo "✅ Git tag '${version}' already exists and points to target commit ${target_full_sha}. Idempotent skip."
  else
    setup_git_bot_user
    git tag -a "${version}" "${target_full_sha}" -m "Release ${version}"
    refspecs+=("${tag_ref}")
    push_tag="true"
  fi

  # A run that fails from here on leaves the checkout as it was found: the tag
  # this run created is deleted and the line put back. The workflow's fresh
  # checkout would not care, but a persistent clone would, and off CI the
  # calculator does not prune: a local tag no push ever took would be the next
  # run's GA base and its notes-start tag. Reads the enclosing locals.
  take_back_local_refs() {
    local taken=()
    if [ "${push_tag}" = "true" ]; then
      git tag -d "${version}" >/dev/null 2>&1 || true
      taken+=("Git tag '${version}'")
    fi
    if [ "${push_line}" = "true" ]; then
      if [ -n "${prior_branch_sha}" ]; then
        git update-ref "${branch_ref}" "${prior_branch_sha}" 2>/dev/null || true
      else
        git branch -D "${branch}" >/dev/null 2>&1 || true
      fi
      taken+=("release line '${branch}'")
    fi
    if [ ${#taken[@]} -gt 0 ]; then
      echo "↩️ Nothing was pushed; $(IFS=,; echo "${taken[*]}") taken back from this checkout, which is left as it was found." >&2
    fi
  }

  case "${placement}" in
    remote)
      echo "✅ Release line '${branch}' already exists on $(get_target_repo) at release commit ${target_full_sha}. Idempotent skip."
      ;;
    remote-past)
      echo "✅ Release line '${branch}' on $(get_target_repo) is already past release commit ${target_full_sha:0:7}; nothing to move."
      ;;
    *)
      if ! set_release_branch_locally "${placement}" "${branch}" "${branch_ref}" "${target_full_sha}"; then
        take_back_local_refs
        return 1
      fi
      if [ "${placement}" = "local-past" ]; then
        echo "✅ Local release line '${branch}' is already past release commit ${target_full_sha:0:7}; nothing to move."
      else
        refspecs+=("${branch_ref}:${branch_ref}")
        push_line="true"
      fi
      ;;
  esac

  # Safety Guard: Remote push executes exclusively inside CI
  if ! is_ci_pipeline; then
    echo "⚠️ [Local Execution] Dry-run: Git tag '${version}' and release line '${branch}' set locally. Remote push skipped (runs only in CI)."
    return 0
  fi

  if [ ${#refspecs[@]} -eq 0 ]; then
    echo "✅ Nothing to push: the tag and the release line are already on the remote."
    return 0
  fi

  local label
  if [ "${push_tag}" = "true" ] && [ "${push_line}" = "true" ]; then
    label="Git tag '${version}' and release line '${branch}'"
  elif [ "${push_tag}" = "true" ]; then
    label="Git tag '${version}'"
  else
    label="Release line '${branch}'"
  fi
  if release_push_refs "${label}" "${refspecs[@]}"; then
    return 0
  fi
  take_back_local_refs
  return 1
}

# Stamps BAKED_RELEASE_VERSION into root installer scripts (install.sh, uninstall.sh, upgrade.sh)
stamp_baked_release_version() {
  local version="${1:-}"
  local repo_dir="${2:-${REPO_ROOT}}"

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for stamp_baked_release_version." >&2
    return 1
  fi

  for script_name in "${RELEASE_INSTALLER_SCRIPTS[@]}"; do
    local script_path="${repo_dir}/${script_name}"
    if [ ! -f "${script_path}" ]; then
      echo "❌ ERROR: Target installer script not found at ${script_path}!" >&2
      return 1
    fi
    sed -i.bak -E "s/^BAKED_RELEASE_VERSION=[\"'].*[\"']/BAKED_RELEASE_VERSION=\"${version}\"/" "${script_path}" && rm -f "${script_path}.bak"
    if ! grep -q "^BAKED_RELEASE_VERSION=\"${version}\"" "${script_path}"; then
      echo "❌ ERROR: Failed to stamp BAKED_RELEASE_VERSION in ${script_name} (placeholder line '^BAKED_RELEASE_VERSION=...' not found)." >&2
      return 1
    fi
  done
}

# Stamps version and appVersion into Helm Chart.yaml for each chart in RELEASE_HELM_CHARTS
stamp_helm_chart_versions() {
  local version="${1:-}"
  local repo_dir="${2:-${REPO_ROOT}}"

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for stamp_helm_chart_versions." >&2
    return 1
  fi

  for chart_rel_path in "${RELEASE_HELM_CHARTS[@]}"; do
    local chart_yaml="${repo_dir}/${chart_rel_path}/Chart.yaml"
    if [ ! -f "${chart_yaml}" ]; then
      echo "❌ ERROR: Helm chart file not found at ${chart_yaml}!" >&2
      return 1
    fi
    sed -i.bak -E \
      -e "s/^version:[[:space:]].*/version: ${version}/" \
      -e "s/^appVersion:[[:space:]].*/appVersion: \"${version}\"/" \
      "${chart_yaml}" && rm -f "${chart_yaml}.bak"

    if ! grep -q -E "^version:[[:space:]]+${version}$" "${chart_yaml}"; then
      echo "❌ ERROR: Failed to stamp version in ${chart_yaml}!" >&2
      return 1
    fi
    if ! grep -q -E "^appVersion:[[:space:]]+\"${version}\"$" "${chart_yaml}"; then
      echo "❌ ERROR: Failed to stamp appVersion in ${chart_yaml}!" >&2
      return 1
    fi
  done
}

# Stamps release image tag into Terraform defaults (variables.tf and terraform.tfvars.example)
stamp_terraform_release_versions() {
  local version="${1:-}"
  local repo_dir="${2:-${REPO_ROOT}}"

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for stamp_terraform_release_versions." >&2
    return 1
  fi

  local var_file="${repo_dir}/${RELEASE_TERRAFORM_EXAMPLE_VARS}"
  if [ ! -f "${var_file}" ]; then
    echo "❌ ERROR: Terraform variables file not found at ${var_file}!" >&2
    return 1
  fi
  sed -i.bak -E "/variable \"image_tag\"/,/^[[:space:]]*\}/ s/([[:space:]]*default[[:space:]]*=[[:space:]]*)\"[^\"]*\"/\1\"${version}\"/" "${var_file}" && rm -f "${var_file}.bak"

  if ! grep -q -E "default[[:space:]]*=[[:space:]]*\"${version}\"" "${var_file}"; then
    echo "❌ ERROR: Failed to stamp image_tag default in ${var_file}!" >&2
    return 1
  fi

  local tfvars_file="${repo_dir}/${RELEASE_TERRAFORM_EXAMPLE_TFVARS}"
  if [ ! -f "${tfvars_file}" ]; then
    echo "❌ ERROR: Terraform example tfvars file not found at ${tfvars_file}!" >&2
    return 1
  fi
  sed -i.bak -E "s/^#([[:space:]]*image_tag[[:space:]]*=[[:space:]]*)\"[^\"]*\"/#\1\"${version}\"/" "${tfvars_file}" && rm -f "${tfvars_file}.bak"
  if ! grep -q -E "^#[[:space:]]*image_tag[[:space:]]*=[[:space:]]*\"${version}\"" "${tfvars_file}"; then
    echo "❌ ERROR: Failed to stamp image_tag example in ${tfvars_file}!" >&2
    return 1
  fi
}

# Stamps all release version touchpoints: installer scripts, Helm Chart.yaml, and Terraform defaults
stamp_release_versions() {
  local version="${1:-}"
  local repo_dir="${2:-${REPO_ROOT}}"

  if [ -z "${version}" ]; then
    echo "❌ ERROR: version is required for stamp_release_versions." >&2
    return 1
  fi

  stamp_baked_release_version "${version}" "${repo_dir}" || return 1
  stamp_helm_chart_versions "${version}" "${repo_dir}" || return 1
  stamp_terraform_release_versions "${version}" "${repo_dir}" || return 1
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
  local expected_subject="${RELEASE_STAMP_SUBJECT_PREFIX} ${version}"
  if [ "${commit_subject}" != "${expected_subject}" ]; then
    echo "⚠️ Tag commit ${tag_commit:0:7} subject '${commit_subject}' does not match expected stamped subject '${expected_subject}'." >&2
    return 1
  fi

  return 0
}

# Creates a release commit on detached HEAD with stamped release versions
create_stamped_release_commit() {
  local version="${1:-}"
  local target_sha="${2:-}"
  local repo_dir="${3:-${REPO_ROOT}}"

  if [ -z "${version}" ] || [ -z "${target_sha}" ]; then
    echo "❌ ERROR: version and target_sha are required for create_stamped_release_commit." >&2
    return 1
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

  local candidate_files=(
    "${RELEASE_INSTALLER_SCRIPTS[@]}"
  )
  for chart_rel_path in "${RELEASE_HELM_CHARTS[@]}"; do
    candidate_files+=("${chart_rel_path}/Chart.yaml")
  done
  candidate_files+=(
    "${RELEASE_TERRAFORM_EXAMPLE_VARS}"
    "${RELEASE_TERRAFORM_EXAMPLE_TFVARS}"
  )

  # Refuse to proceed if any release candidate files have uncommitted changes
  local dirty_release_files
  dirty_release_files="$(git -C "${repo_dir}" status --porcelain -- "${candidate_files[@]}" 2>/dev/null || true)"
  if [ -n "${dirty_release_files}" ]; then
    echo "❌ ERROR: Cannot create stamped release commit with uncommitted changes in release files:" >&2
    echo "${dirty_release_files}" >&2
    echo "Please commit, stash, or revert changes in release files before releasing." >&2
    return 1
  fi

  # Preserve caller's current branch / ref and restore on function return
  local orig_ref
  orig_ref="$(git -C "${repo_dir}" symbolic-ref --short -q HEAD 2>/dev/null || git -C "${repo_dir}" rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "${orig_ref}" ]; then
    # shellcheck disable=SC2064,SC2154 # expand now on purpose; f is the trap's own loop variable
    trap "for f in \"\${candidate_files[@]}\"; do git -C '${repo_dir}' checkout -- \"\$f\" >/dev/null 2>&1 || true; done; git -C '${repo_dir}' checkout '${orig_ref}' >/dev/null 2>&1 || true" RETURN
  fi

  # 1. Checkout detached HEAD at candidate commit
  if ! git -C "${repo_dir}" checkout --detach "${target_sha}" >/dev/null; then
    echo "❌ ERROR: Failed to checkout candidate commit '${target_sha}' on detached HEAD." >&2
    return 1
  fi

  # 2. Stamp release versions across installer scripts, Helm Chart.yaml, and Terraform defaults
  if ! stamp_release_versions "${version}" "${repo_dir}"; then
    echo "❌ ERROR: Failed to stamp release versions." >&2
    return 1
  fi

  # 3. If files were modified, create release commit on detached HEAD (does NOT touch main branch)
  local modified_files=()
  for file_rel in "${candidate_files[@]}"; do
    if [ -f "${repo_dir}/${file_rel}" ] && [ -n "$(git -C "${repo_dir}" status --porcelain "${file_rel}" 2>/dev/null || true)" ]; then
      modified_files+=("${file_rel}")
    fi
  done

  if [ ${#modified_files[@]} -gt 0 ]; then
    echo "📝 Stamping release version '${version}' in release tag commit..." >&2
    setup_git_bot_user
    git -C "${repo_dir}" add "${modified_files[@]}"
    git -C "${repo_dir}" commit -m "${RELEASE_STAMP_SUBJECT_PREFIX} ${version}" >/dev/null
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

  # The candidate's own list (required_release_images_at): the images its
  # publish run built, which may be fewer or more than this checkout's.
  local images=() img
  while IFS= read -r img; do images+=("${img}"); done < <(required_release_images_at "${resolved_commit}")
  if [ "${#images[@]}" -eq 0 ]; then
    echo "❌ ERROR: no required release images resolved for commit ${resolved_commit:0:7}; nothing promoted." >&2
    return 1
  fi

  for img in "${images[@]}"; do
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
