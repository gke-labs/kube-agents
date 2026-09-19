#!/usr/bin/env bash
# Tears an ephemeral environment down and provisions it again at a candidate commit.
#
# Called by deploy-environment.yml for both the RC pipeline and the nightly
# pipeline. Which environment it builds comes entirely from GCP_PROJECT_ID /
# GCP_REGION / GKE_CLUSTER_NAME and the rest of the install inputs, which the
# calling workflow reads from its GitHub environment — nothing here is
# RC-specific.
set -euo pipefail

export CLOUDSDK_CORE_DISABLE_PROMPTS="${CLOUDSDK_CORE_DISABLE_PROMPTS:-1}"

# teardown_is_strict, teardown_run and teardown_report_failure are
# shared with teardown_environment.sh, which removes the environment again
# once a run has passed. Both read the same three outcomes out of uninstall.sh.
# shellcheck source=scripts/release/teardown_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/teardown_common.sh"

# Verify required GCP/GKE inputs before executing any destructive teardown.
teardown_require_inputs

# Half-configured minter: refuse before anything is destroyed.
#
# All three of GITOPS_ORG/GITOPS_REPO/GITHUB_APP_ID must be non-empty before
# installer_common.sh provisions the minter at all, and its own "GitHub minter
# deferred" warning only fires once they are, so one missing value skips the
# minter in silence.
#
# Nothing downstream catches it on the RC, which is why the check is here: the
# test that exercises minting runs in an optional, `continue-on-error` suite
# there, so a broken minter costs an HTTP 502 in a tolerated step and validates
# the candidate anyway. The nightly pipeline runs the same test in its blocking
# suite and needs no such help.
#
# Above the teardown deliberately. `teardown_run` below is `uninstall.sh`, so a
# check placed after it would refuse an environment it had already destroyed and
# leave it down until someone re-ran the pipeline.
#
# All three empty stays allowed: that is an install deliberately without a
# minter, which is the default outside the RC and nightly environments.
#
# Why a value goes missing is in scripts/release/README.md under "Enabling the
# GitHub token minter on the RC".
# The deprecated spellings, folded in before the guard rather than after it.
# installer_common.sh's normalize_gitops_repo_vars does the same thing for the
# installers, but this check runs before the repository's helpers are sourced --
# and a half-configured minter passed under the old names has to be caught here
# too, or the deprecation would quietly disable the guard.
: "${GITOPS_ORG:=${GITHUB_ORG:-}}"
: "${GITOPS_REPO:=${GITHUB_REPO:-}}"
export GITOPS_ORG GITOPS_REPO

GITHUB_MINTER_SET=""
GITHUB_MINTER_MISSING=""
for _v in GITOPS_ORG GITOPS_REPO GITHUB_APP_ID; do
  if [ -n "${!_v:-}" ]; then
    GITHUB_MINTER_SET="${GITHUB_MINTER_SET} ${_v}"
  else
    GITHUB_MINTER_MISSING="${GITHUB_MINTER_MISSING} ${_v}"
  fi
done
if [ -n "${GITHUB_MINTER_SET}" ] && [ -n "${GITHUB_MINTER_MISSING}" ]; then
  echo "::error title=GitHub token minter is half-configured::Set:${GITHUB_MINTER_SET}; empty:${GITHUB_MINTER_MISSING}. All three are required. Refusing to tear down and reprovision an environment whose token-minting test would then fail with an HTTP 502. Check that each one is set on the GitHub environment this job binds to, and that the calling pipeline still invokes this workflow with \`secrets: inherit\` — without it an environment secret such as GH_APP_ID reaches this job empty."
  echo "==> GitHub token minter half-configured — set:${GITHUB_MINTER_SET}; empty:${GITHUB_MINTER_MISSING}." >&2
  exit 1
fi

# Chat allowlists on a long-lived environment: refuse before anything is
# destroyed, in the same place and for the same reason as the minter check.
#
# render_install_env.sh makes this check for the reconcile path, under
# --strict. This is the same guarantee for the rebuild path, and the two have
# to agree: deploy-environment.yml offers `autopush` and `staging` in its
# dropdown, so without it the escape hatch you reach for when a reconcile
# cannot converge is also the one route into these environments that does not
# ask about the allowlist.
#
# Empty is not "no opinion". install.sh renders `google_chat_allowed_users =
# []`, the chart's `with` omits the key, and the operator turns an absent list
# into allow-all (platformagent_manifests.go's allowAllUsers) -- so a rebuild
# of an environment whose allowlist variable was never set admits the whole
# domain, and nothing in the run says so.
#
# LONG_LIVED_ENVIRONMENT only. `rc` and `nightly` carry GOOGLE_CHAT_ENABLED=true
# with no ALLOWED_USERS today and are deliberately open: they are destroyed and
# rebuilt every run and no real user reaches them. An unconditional guard here
# would fail the RC pipeline on its next run rather than protect anything.
#
# Truthiness and emptiness are both inlined for the reason teardown_common.sh
# gives -- these scripts do not source installer_common.sh -- and both match it
# exactly. Emptiness in particular is the installer's, not `-z`: hcl_csv_list
# splits on `, \t\n` and drops empty items, so a list cleared down to a stray
# comma names nobody and still renders `[]`.
provision_is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

provision_names_nobody() {
  local val="${1:-}"
  # Every separator hcl_csv_list splits on; what is left is the real items.
  val="${val//[, $'\t'$'\n']/}"
  [ -z "$val" ]
}

provision_check_allowlist() {
  local enabled_var="$1" list_var="$2" allow_all_var="$3" platform="$4"
  provision_is_truthy "${!enabled_var:-}" || return 0
  provision_names_nobody "${!list_var:-}" || return 0
  ! provision_is_truthy "${!allow_all_var:-}" || return 0
  echo "::error title=${platform} is enabled with no allowlist::${list_var} names no users on this environment — it is unset, or it holds only separators — and an empty allowlist means EVERY user is admitted, because the operator turns an absent list into allow-all for ${platform}. Refusing to tear down and rebuild '${GKE_CLUSTER_NAME:-this environment}' into a wider-open install than the one it is replacing. Set ${list_var} to the users this install should admit, or set ${allow_all_var}=true to say the open allowlist is intended."
  echo "==> ${platform} enabled with an empty ${list_var} and no ${allow_all_var}=true." >&2
  return 1
}

if provision_is_truthy "${LONG_LIVED_ENVIRONMENT:-}"; then
  ALLOWLIST_STATUS=0
  provision_check_allowlist GOOGLE_CHAT_ENABLED ALLOWED_USERS \
    GOOGLE_CHAT_ALLOW_ALL_USERS "Google Chat" || ALLOWLIST_STATUS=1
  provision_check_allowlist SLACK_ENABLED SLACK_ALLOWED_USERS \
    SLACK_ALLOW_ALL_USERS "Slack" || ALLOWLIST_STATUS=1
  [ "$ALLOWLIST_STATUS" -eq 0 ] || exit 1
fi

# The GitHub variables this script turns into `--enable-*` flags, in the
# spelling install.sh's validator accepts.
#
# The flag route and the file route do not judge a value the same way:
# validate_bool_flag_value matches ^(true|false)$ and exits 1 naming the flag,
# while a value that reaches the installer through the rendered install.env goes
# to is_truthy, which takes True/yes/y/1/on as well. A human types these into a
# GitHub environment form, so an environment that deploys today on `True` must
# keep deploying once the value travels as a flag.
#
# A value neither list recognises is returned untouched rather than folded into
# "false": `ture` should still be refused rather than silently disabling the
# feature. The guard below is what refuses it, above the teardown.
#
# That includes a value which is nothing but whitespace, which is why no arm
# here matches the empty string. Both callers skip a genuinely empty variable,
# so an empty `stripped` means the operator saved a space or a tab into the
# GitHub environment — and `--enable-gvisor=false` is the one answer that must
# not be inferred from it. Folded, that rebuild redeploys a long-lived
# environment onto the standard runtime with nothing in the log saying so;
# returned untouched, it is refused with the variable named and the environment
# still up.
provision_canonical_bool() {
  local val="${1:-}"
  if provision_is_truthy "$val"; then
    echo "true"
    return 0
  fi
  local stripped="${val//[[:space:]]/}"
  case "$stripped" in
    [Ff][Aa][Ll][Ss][Ee] | [Nn][Oo] | [Nn] | 0 | [Oo][Ff][Ff]) echo "false" ;;
    *) echo "$val" ;;
  esac
}

# Everything install.sh would refuse this configuration for, checked here.
#
# Above the teardown, for the reason the minter and allowlist guards give: the
# refusal otherwise arrives from `./install.sh` at the bottom of this script,
# by which point `teardown_run` has destroyed the environment — and on the
# autopush/staging rebuild path that is a long-lived install left down over a
# typo in a GitHub variable or an unset secret.
#
# Each of these is knowable before anything is destroyed, so each is checked
# before anything is destroyed. Keep this in step with install.sh's parse-time
# validators and its Slack token guard: a refusal added there and not mirrored
# here reverts to failing after the teardown.
INSTALL_REFUSAL_STATUS=0

for _bool_var in ENABLE_GKE_BACKUP_PLAN ENABLE_GVISOR HERMES_DASHBOARD_ENABLED ENABLE_WEBUI; do
  [ -n "${!_bool_var:-}" ] || continue
  _canonical="$(provision_canonical_bool "${!_bool_var}")"
  case "$_canonical" in
    true | false) ;;
    *)
      echo "::error title=${_bool_var} is not a boolean::'${!_bool_var}' is neither true nor false, and this script passes it to install.sh as a flag, whose validator exits 1 on it. Refusing before the teardown rather than after. Set ${_bool_var} on this GitHub environment to true or false."
      echo "==> ${_bool_var}='${!_bool_var}' is not a boolean." >&2
      INSTALL_REFUSAL_STATUS=1
      ;;
  esac
done

# install.sh refuses --enable-slack without both tokens when there is no tty,
# which is this job. Its own guard runs after the Secret-recovery loop, which
# can read them off a live install -- but this script has just destroyed that
# install by the time it runs, so nothing can recover them here and the refusal
# is certain.
if provision_is_truthy "${SLACK_ENABLED:-}"; then
  SLACK_MISSING=""
  [ -n "${SLACK_BOT_TOKEN:-}" ] || SLACK_MISSING="${SLACK_MISSING} SLACK_BOT_TOKEN"
  [ -n "${SLACK_APP_TOKEN:-}" ] || SLACK_MISSING="${SLACK_MISSING} SLACK_APP_TOKEN"
  if [ -n "${SLACK_MISSING}" ]; then
    echo "::error title=Slack is enabled with no tokens::SLACK_ENABLED is set on this environment but${SLACK_MISSING} reached this job empty, and install.sh refuses --enable-slack without both. Refusing to tear down '${GKE_CLUSTER_NAME:-this environment}' for an install that cannot complete. Check the secret is set on the GitHub environment this job binds to, and that the calling pipeline still invokes this workflow with \`secrets: inherit\`."
    echo "==> Slack enabled with missing:${SLACK_MISSING}." >&2
    INSTALL_REFUSAL_STATUS=1
  fi
fi

[ "$INSTALL_REFUSAL_STATUS" -eq 0 ] || exit 1

TEARDOWN_LOG="$(mktemp)"

echo "==> Tearing down the existing environment (${TEARDOWN_TARGET}) via canonical uninstall.sh..."
TEARDOWN_STATUS=0
teardown_run "${TEARDOWN_LOG}" || TEARDOWN_STATUS=$?

# uninstall.sh exits 0 when it tore the environment down, 3 when there was no
# Terraform state to tear down (not a failure), and anything else when the
# teardown could not start or did not finish; `./uninstall.sh --help` is the
# contract. Collapsing the three into one warning is how a teardown that tore
# nothing down went unremarked: the environment survived from run to run
# while the pipeline reported provisioning a fresh one, and the AgentPlugins
# and Secrets it left behind were read as E2E flakes.
case "${TEARDOWN_STATUS}" in
  0)
    echo "==> Teardown complete (uninstall.sh exit 0)."
    ;;
  3)
    echo "==> Nothing to tear down: no Terraform state for '${GKE_CLUSTER_NAME}' (uninstall.sh exit 3), so there is no environment to remove."
    ;;
  *)
    teardown_report_failure \
      "${TEARDOWN_STATUS}" "${TEARDOWN_LOG}" \
      "uninstall.sh exited ${TEARDOWN_STATUS}; the environment was NOT torn down and this run reinstalls over whatever survived." \
      "⚠️ Environment teardown failed" \
      "\`uninstall.sh\` did not tear the environment down. The install below runs" \
      "on top of the previous run's cluster, CRs, Secrets and pods, so any E2E" \
      "failure may be stale state rather than a regression in the candidate."
    # Whether this is fatal is the caller's choice, because the two answers
    # trade different things. Stopping keeps a candidate from being validated
    # against stale state; continuing keeps a teardown problem from blocking
    # every release. TEARDOWN_STRICT picks, and the pipeline sets it from a
    # variable on the bound GitHub environment so the choice is a setting rather
    # than a commit. It is read under both TEARDOWN_STRICT and the legacy
    # RC_TEARDOWN_STRICT; see teardown_common.sh.
    if teardown_is_strict; then
      echo "$(teardown_strict_source) is set: refusing to provision on top of a failed teardown." >&2
      rm -f "${TEARDOWN_LOG}"
      exit "${TEARDOWN_STATUS}"
    fi
    echo "==> Proceeding with provisioning anyway ($(teardown_strict_source) is not set); the environment is NOT fresh." >&2
    ;;
esac

rm -f "${TEARDOWN_LOG}"

# The GitHub variables this script turns into `--enable-*` flags travel
# through provision_canonical_bool, defined with the guards above: the spelling
# check has to happen before the teardown, and the canonicalisation is the same
# call.
INSTALL_ARGS=(
  --non-interactive -y
  --gcp-project-id="${GCP_PROJECT_ID}"
  --gcp-region="${GCP_REGION}"
  --gke-cluster-name="${GKE_CLUSTER_NAME}"
  --image-tag="${IMAGE_TAG}"
)

# Everything below reaches install.sh as a flag rather than as an inherited
# environment variable. The two routes are not equivalent: install.env is
# sourced with `set -a` and so beats an exported variable of the same name, and
# these environments render an install.env from their GitHub variables -- so a
# setting passed only by export is silently overridden by whatever the rendered
# file happens to say. A flag is the one thing that wins for a single run.
if [ -n "${AGENT_NAMESPACE:-${NAMESPACE:-}}" ]; then
  INSTALL_ARGS+=(--agent-namespace="${AGENT_NAMESPACE:-${NAMESPACE}}")
fi

if [ -n "${GKE_CLUSTER_MODE:-${CLUSTER_MODE:-}}" ]; then
  INSTALL_ARGS+=(--gke-cluster-mode="${GKE_CLUSTER_MODE:-${CLUSTER_MODE}}")
fi

if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
  INSTALL_ARGS+=(--enable-google-chat)
fi

if [ -n "${GOOGLE_CHAT_MODE:-}" ]; then
  INSTALL_ARGS+=(--google-chat-mode="${GOOGLE_CHAT_MODE}")
fi

if [ -n "${GOOGLE_CHAT_HOME_CHANNEL:-}" ]; then
  INSTALL_ARGS+=(--google-chat-home-channel="${GOOGLE_CHAT_HOME_CHANNEL}")
fi

if [ -n "${CHAT_TOPIC_NAME:-}" ]; then
  INSTALL_ARGS+=(--chat-topic-name="${CHAT_TOPIC_NAME}")
fi

# The Google Chat allowlist. Empty is NOT "no opinion" -- the operator turns an
# absent list into allow-all -- which is why provision_check_allowlist above
# refuses an empty one on a long-lived environment. Passing it explicitly means
# the value this job was given is the value the install gets.
if [ -n "${GOOGLE_CHAT_ALLOWED_USERS:-${ALLOWED_USERS:-}}" ]; then
  INSTALL_ARGS+=(--google-chat-allowed-users="${GOOGLE_CHAT_ALLOWED_USERS:-${ALLOWED_USERS}}")
fi

if [ "${SLACK_ENABLED:-false}" = "true" ]; then
  INSTALL_ARGS+=(--enable-slack)
  # install.sh refuses --enable-slack without both tokens when there is no tty,
  # which is this job. Passing them unconditionally inside this branch keeps
  # that refusal about a genuinely missing secret rather than about the route
  # it travelled.
  INSTALL_ARGS+=(--slack-bot-token="${SLACK_BOT_TOKEN:-}")
  INSTALL_ARGS+=(--slack-app-token="${SLACK_APP_TOKEN:-}")
  if [ -n "${SLACK_ALLOWED_USERS:-}" ]; then
    INSTALL_ARGS+=(--slack-allowed-users="${SLACK_ALLOWED_USERS}")
  fi
  if [ -n "${SLACK_HOME_CHANNEL:-}" ]; then
    INSTALL_ARGS+=(--slack-home-channel="${SLACK_HOME_CHANNEL}")
  fi
  if [ -n "${SLACK_HOME_CHANNEL_NAME:-}" ]; then
    INSTALL_ARGS+=(--slack-home-channel-name="${SLACK_HOME_CHANNEL_NAME}")
  fi
fi

if [ -n "${ENABLE_GKE_BACKUP_PLAN:-}" ]; then
  INSTALL_ARGS+=(--enable-gke-backup-plan="$(provision_canonical_bool "${ENABLE_GKE_BACKUP_PLAN}")")
fi

if [ -n "${HERMES_DASHBOARD_ENABLED:-${ENABLE_WEBUI:-}}" ]; then
  INSTALL_ARGS+=(--enable-hermes-dashboard="$(provision_canonical_bool "${HERMES_DASHBOARD_ENABLED:-${ENABLE_WEBUI}}")")
fi

if [ -n "${MODEL_PROVIDER:-}" ]; then
  INSTALL_ARGS+=(--model-provider="${MODEL_PROVIDER}")
fi

if [ -n "${MODEL_DEFAULT_NAME:-}" ]; then
  INSTALL_ARGS+=(--model-default-name="${MODEL_DEFAULT_NAME}")
fi

if [ -n "${ENABLE_GVISOR:-}" ]; then
  INSTALL_ARGS+=(--enable-gvisor="$(provision_canonical_bool "${ENABLE_GVISOR}")")
fi

if [ -n "${PLATFORM_AGENT_PERMISSION_SET:-}" ]; then
  INSTALL_ARGS+=(--permission-set="${PLATFORM_AGENT_PERMISSION_SET}")
fi

if [ -n "${REGISTRY_PREFIX:-}" ]; then
  INSTALL_ARGS+=(--registry-prefix="${REGISTRY_PREFIX}")
fi

if [ -n "${USER_PROFILE_ENABLED:-}" ]; then
  INSTALL_ARGS+=(--user-profile-enabled="${USER_PROFILE_ENABLED}")
fi

if [ "${ENABLE_PUBSUB_PLATFORM:-false}" = "true" ]; then
  INSTALL_ARGS+=(--enable-pubsub-platform)
fi

if [ "${ENABLE_STOCKOUT_INVESTIGATOR:-false}" = "true" ]; then
  INSTALL_ARGS+=(--enable-stockout-investigator)
fi

# No --gitops-org/--gitops-repo flags here: install.sh already seeds PARAM_GITOPS_ORG
# and PARAM_GITOPS_REPO from the GITOPS_ORG and GITOPS_REPO this step exports
# (the PARAM_GITOPS_* assignments near the top of install.sh), so passing them again
# would be the same values by a second route. GITHUB_APP_ID is read from the
# environment the same way. All three unset leaves enable_github_minter false and the
# install byte-identical to one that never had them (the three-way guard on
# GITOPS_ORG/GITOPS_REPO/GITHUB_APP_ID in installer_common.sh's write_tfvars_from_state).
#
# The half-configured case is refused at the top of this script, above the
# teardown, so it never reaches here.

# Memory mode mapping: kube_agents_memory/hindsight -> hindsight, none/off -> off, else -> file
if [ "${MEMORY_PROVIDER:-}" = "kube_agents_memory" ] || [ "${MEMORY_PROVIDER:-}" = "hindsight" ]; then
  INSTALL_ARGS+=(--memory=hindsight)
elif [ "${MEMORY_PROVIDER:-}" = "none" ] || [ "${MEMORY_PROVIDER:-}" = "off" ]; then
  INSTALL_ARGS+=(--memory=off)
else
  INSTALL_ARGS+=(--memory=file)
fi

echo "==> Provisioning the environment at the candidate commit via canonical install.sh..."
./install.sh "${INSTALL_ARGS[@]}"
