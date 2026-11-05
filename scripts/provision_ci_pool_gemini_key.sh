#!/usr/bin/env bash
# ==============================================================================
# Gemini Developer API key for one CI pool project
# ==============================================================================
# Mints the eval nightlies' Gemini Developer API key in a pool project and keeps
# the key string as the current version of a Secret Manager secret in the same
# project. Re-runs converge: an existing key is reused, and a secret version is
# added only when the stored value differs from the key string.
#
# The key string never reaches stdout, stderr, a command line or an xtrace: it
# moves from gcloud to Secret Manager through a variable and stdin, and the
# script turns xtrace off first, so `bash -x` or an exported SHELLOPTS with
# xtrace cannot print it.
#
# Other files read the constants below with
#   sed -n 's/^readonly NAME="\(.*\)"$/\1/p' scripts/provision_ci_pool_gemini_key.sh
# so each one stays a single double-quoted line.
#
# Usage:
#   ./scripts/provision_ci_pool_gemini_key.sh <PROJECT_ID>
# ==============================================================================

set -euo pipefail
{ set +x; } 2>/dev/null

readonly GEMINI_KEY_DISPLAY_NAME="kube-agents-eval-gemini"
readonly GEMINI_KEY_SECRET_NAME="kube-agents-eval-gemini-api-key"
readonly GEMINI_KEY_API_TARGET="generativelanguage.googleapis.com"
readonly GEMINI_KEY_APIS="apikeys.googleapis.com secretmanager.googleapis.com generativelanguage.googleapis.com"
# The two runners: prowjob-default-sa runs the presubmits, eval-baseline-recorder
# runs the nightlies.
readonly GEMINI_KEY_SECRET_ACCESSORS="prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com eval-baseline-recorder@kube-agents-prow.iam.gserviceaccount.com"
readonly GEMINI_KEY_ACCESSOR_ROLE="roles/secretmanager.secretAccessor"
readonly GEMINI_KEY_REPLICATION_POLICY="automatic"
# gcloud's error text for an absent secret, and for a secret with no readable
# latest version (none added yet, or the latest one disabled or destroyed).
readonly GCLOUD_NOT_FOUND="NOT_FOUND"
readonly GCLOUD_FAILED_PRECONDITION="FAILED_PRECONDITION"
# gcloud's error text for an API that is disabled, or enabled moments ago and
# not yet ready: a fresh enable of the API Keys API answers this for a while.
readonly GCLOUD_SERVICE_DISABLED="SERVICE_DISABLED|before or it is disabled"
# How often, and how far apart, the first key listing runs on that answer.
readonly GEMINI_KEY_LIST_ATTEMPTS=3
readonly GEMINI_KEY_LIST_RETRY_SECONDS=20
# The create operation prints its result, key string included; drop that line
# from any error output shown.
readonly GCLOUD_KEY_STRING_FIELD="keyString"
readonly EX_USAGE=64
# Appended inside a command substitution so trailing newlines in the stored
# value survive the comparison.
readonly CAPTURE_SENTINEL="x"

usage() {
  echo "usage: $0 <PROJECT_ID>" >&2
  exit "${EX_USAGE}"
}

die() {
  echo "ERROR: [${PROJECT_ID}] $*" >&2
  exit 1
}

[[ $# -eq 1 && -n "${1}" ]] || usage
readonly PROJECT_ID="$1"

ERR_FILE="$(mktemp)"
readonly ERR_FILE
trap 'rm -f "${ERR_FILE}"' EXIT

show_err() {
  grep -v "${GCLOUD_KEY_STRING_FIELD}" "${ERR_FILE}" >&2 || true
}

# ─── APIs ──────────────────────────────────────────────────────────────────────
echo "[${PROJECT_ID}] Enabling ${GEMINI_KEY_APIS}"
# shellcheck disable=SC2086 # GEMINI_KEY_APIS is a space-separated list
gcloud services enable ${GEMINI_KEY_APIS} --project="${PROJECT_ID}" ||
  die "could not enable ${GEMINI_KEY_APIS}"

# ─── API key ───────────────────────────────────────────────────────────────────
find_key() {
  gcloud services api-keys list --project="${PROJECT_ID}" \
    --filter="displayName=${GEMINI_KEY_DISPLAY_NAME}" --format='value(name)'
}

# The first listing follows the enable above, so it retries while the API
# Keys API still answers SERVICE_DISABLED.
list_keys_after_enable() {
  local attempt
  for ((attempt = 1; ; attempt++)); do
    if KEY_NAMES="$(find_key 2>"${ERR_FILE}")"; then
      return 0
    fi
    if ((attempt >= GEMINI_KEY_LIST_ATTEMPTS)) || ! grep -qE "${GCLOUD_SERVICE_DISABLED}" "${ERR_FILE}"; then
      show_err
      return 1
    fi
    echo "[${PROJECT_ID}] The API Keys API is not ready yet; retrying in ${GEMINI_KEY_LIST_RETRY_SECONDS}s" >&2
    sleep "${GEMINI_KEY_LIST_RETRY_SECONDS}"
  done
}

list_keys_after_enable || die "could not list API keys"
if [[ -z "${KEY_NAMES}" ]]; then
  echo "[${PROJECT_ID}] Creating API key ${GEMINI_KEY_DISPLAY_NAME} for ${GEMINI_KEY_API_TARGET}"
  if ! gcloud services api-keys create --project="${PROJECT_ID}" \
    --display-name="${GEMINI_KEY_DISPLAY_NAME}" \
    --api-target="service=${GEMINI_KEY_API_TARGET}" >/dev/null 2>"${ERR_FILE}"; then
    show_err
    die "could not create API key ${GEMINI_KEY_DISPLAY_NAME}"
  fi
  KEY_NAMES="$(find_key)" || die "could not list API keys"
  [[ -n "${KEY_NAMES}" ]] || die "API key ${GEMINI_KEY_DISPLAY_NAME} is absent after create"
fi
[[ "${KEY_NAMES}" != *$'\n'* ]] ||
  die "more than one API key named ${GEMINI_KEY_DISPLAY_NAME}: $(echo "${KEY_NAMES}" | tr '\n' ' ')"
readonly KEY_NAME="${KEY_NAMES}"
echo "[${PROJECT_ID}] Using API key ${KEY_NAME}"

KEY_STRING="$(gcloud services api-keys get-key-string "${KEY_NAME}" \
  --project="${PROJECT_ID}" --format='value(keyString)')" ||
  die "could not read the key string of ${KEY_NAME}"
[[ -n "${KEY_STRING}" ]] || die "the key string of ${KEY_NAME} is empty"

# ─── Secret ────────────────────────────────────────────────────────────────────
if gcloud secrets describe "${GEMINI_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
  --format='value(name)' >/dev/null 2>"${ERR_FILE}"; then
  echo "[${PROJECT_ID}] Secret ${GEMINI_KEY_SECRET_NAME} exists"
elif grep -q "${GCLOUD_NOT_FOUND}" "${ERR_FILE}"; then
  echo "[${PROJECT_ID}] Creating secret ${GEMINI_KEY_SECRET_NAME}"
  gcloud secrets create "${GEMINI_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
    --replication-policy="${GEMINI_KEY_REPLICATION_POLICY}" ||
    die "could not create secret ${GEMINI_KEY_SECRET_NAME}"
else
  show_err
  die "could not describe secret ${GEMINI_KEY_SECRET_NAME}"
fi

HAS_CURRENT=0
CURRENT_VALUE=""
if CURRENT_VALUE="$(gcloud secrets versions access latest --secret="${GEMINI_KEY_SECRET_NAME}" \
  --project="${PROJECT_ID}" 2>"${ERR_FILE}" && printf '%s' "${CAPTURE_SENTINEL}")"; then
  CURRENT_VALUE="${CURRENT_VALUE%"${CAPTURE_SENTINEL}"}"
  HAS_CURRENT=1
elif ! grep -qE "${GCLOUD_NOT_FOUND}|${GCLOUD_FAILED_PRECONDITION}" "${ERR_FILE}"; then
  show_err
  die "could not read the latest version of secret ${GEMINI_KEY_SECRET_NAME}"
fi

if [[ "${HAS_CURRENT}" -eq 1 && "${CURRENT_VALUE}" == "${KEY_STRING}" ]]; then
  echo "[${PROJECT_ID}] Secret ${GEMINI_KEY_SECRET_NAME} already holds the key of ${KEY_NAME}"
else
  echo "[${PROJECT_ID}] Adding a version of secret ${GEMINI_KEY_SECRET_NAME} with the key of ${KEY_NAME}"
  printf '%s' "${KEY_STRING}" |
    gcloud secrets versions add "${GEMINI_KEY_SECRET_NAME}" --project="${PROJECT_ID}" --data-file=- ||
    die "could not add a version to secret ${GEMINI_KEY_SECRET_NAME}"
fi
unset KEY_STRING CURRENT_VALUE

# ─── IAM ───────────────────────────────────────────────────────────────────────
grant() {
  local member="$1" role="$2"
  echo "[${PROJECT_ID}] Granting ${role} on secret ${GEMINI_KEY_SECRET_NAME} to ${member}"
  gcloud secrets add-iam-policy-binding "${GEMINI_KEY_SECRET_NAME}" --project="${PROJECT_ID}" \
    --member="serviceAccount:${member}" --role="${role}" --format=none ||
    die "could not grant ${role} on secret ${GEMINI_KEY_SECRET_NAME} to ${member}"
}

for sa in ${GEMINI_KEY_SECRET_ACCESSORS}; do
  grant "${sa}" "${GEMINI_KEY_ACCESSOR_ROLE}"
done

echo "[${PROJECT_ID}] Gemini API key ${KEY_NAME} is in secret ${GEMINI_KEY_SECRET_NAME}"
