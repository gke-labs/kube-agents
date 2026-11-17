#!/usr/bin/env bash
# ==============================================================================
# Google Cloud Dedicated / Sovereign Cloud universe_domain environment helper
# ==============================================================================
# Configures gcloud and Terraform environment variables for a target Google
# Cloud universe_domain without mutating the operator's global gcloud config or
# ~/.config/gcloud/application_default_credentials.json.
#
# Like gke_dns_endpoint.sh, this file is deliberately dependency-free so both
# scripts/installer/installer_common.sh and terraform/examples/full-install/
# lifecycle.sh share one implementation with zero drift.

[ -n "${COMMERCIAL_UNIVERSE_DOMAIN:-}" ] || readonly COMMERCIAL_UNIVERSE_DOMAIN="googleapis.com"
[ -n "${UNIVERSE_DOMAIN_MAX_LENGTH:-}" ] || readonly UNIVERSE_DOMAIN_MAX_LENGTH=253
[ -n "${UNIVERSE_DOMAIN_PATTERN:-}" ] || readonly UNIVERSE_DOMAIN_PATTERN='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
[ -n "${ADC_DEFAULT_RELPATH:-}" ] || readonly ADC_DEFAULT_RELPATH=".config/gcloud/application_default_credentials.json"
[ -n "${ADC_GCLOUD_FILENAME:-}" ] || readonly ADC_GCLOUD_FILENAME="application_default_credentials.json"

_universe_report_error() {
  if declare -F print_error >/dev/null 2>&1; then
    print_error "$1"
  elif declare -F warn >/dev/null 2>&1; then
    warn "$1"
  else
    printf 'error: %s\n' "$1" >&2
  fi
}

_universe_expand_home() {
  # shellcheck disable=SC2088
  case "${1:-}" in
    "~") printf '%s' "${HOME:-}" ;;
    "~/"*) printf '%s%s' "${HOME:-}" "${1#\~}" ;;
    *) printf '%s' "${1:-}" ;;
  esac
}

# True when an explicit Terraform or gcloud token/keyfile credential variable is
# already present in the environment (mutually exclusive with minting a new
# GOOGLE_OAUTH_ACCESS_TOKEN for the Terraform google provider).
_universe_explicit_credentials_set() {
  [ -n "${GOOGLE_OAUTH_ACCESS_TOKEN:-}" ] ||
    [ -n "${GOOGLE_CREDENTIALS:-}" ] ||
    [ -n "${GOOGLE_CLOUD_KEYFILE_JSON:-}" ] ||
    [ -n "${GCLOUD_KEYFILE_JSON:-}" ] ||
    [ -n "${CLOUDSDK_AUTH_ACCESS_TOKEN:-}" ]
}

# True when $1 is a bare lowercase DNS domain (at least two labels) suitable
# for core/universe_domain (googleapis.com or a Google Cloud Dedicated /
# Sovereign Cloud universe domain).
is_valid_universe_domain() {
  local domain="${1:-}"
  [ -n "$domain" ] && [ "${#domain}" -le "$UNIVERSE_DOMAIN_MAX_LENGTH" ] &&
    [[ "$domain" =~ $UNIVERSE_DOMAIN_PATTERN ]]
}

# True when $1 names a non-default universe domain (not googleapis.com).
is_custom_universe_domain() {
  local domain="${1:-}"
  [ -n "$domain" ] && [ "$domain" != "${DEFAULT_UNIVERSE_DOMAIN:-$COMMERCIAL_UNIVERSE_DOMAIN}" ]
}

# True when the workstation's Application Default Credentials file exists and
# declares universe_domain matching $1 (defaulting an omitted field to
# googleapis.com, as Google client libraries do). Never modifies the ADC file.
adc_matches_universe_domain() {
  local target_domain="${1:-$COMMERCIAL_UNIVERSE_DOMAIN}" adc_file=""
  if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
    adc_file="$(_universe_expand_home "$GOOGLE_APPLICATION_CREDENTIALS")"
  elif [ -n "${CLOUDSDK_CONFIG:-}" ]; then
    adc_file="${CLOUDSDK_CONFIG%/}/${ADC_GCLOUD_FILENAME}"
  elif [ -n "${HOME:-}" ]; then
    adc_file="${HOME%/}/${ADC_DEFAULT_RELPATH}"
  fi
  [ -n "$adc_file" ] && [ -f "$adc_file" ] || return 1
  (trap - ERR; python3 -c '
import json, sys
adc_path, want, commercial = sys.argv[1:4]
try:
    with open(adc_path, encoding="utf-8") as fh:
        doc = json.load(fh) or {}
except Exception:
    sys.exit(1)
recorded = (doc.get("universe_domain") or commercial).strip()
sys.exit(0 if recorded == want else 1)
' "$adc_file" "$target_domain" "$COMMERCIAL_UNIVERSE_DOMAIN" 2>/dev/null)
}

# Exports the gcloud and Terraform environment variables required to talk to a
# custom universe_domain without mutating the operator's global gcloud config
# or ~/.config/gcloud/application_default_credentials.json. When ADC is absent
# or bound to another universe and no explicit Terraform credential variable is
# set, mints an in-memory access token from the active gcloud session.
configure_installer_universe_env() {
  local domain="${1:-${UNIVERSE_DOMAIN:-${DEFAULT_UNIVERSE_DOMAIN:-$COMMERCIAL_UNIVERSE_DOMAIN}}}"
  if ! is_valid_universe_domain "$domain"; then
    _universe_report_error "UNIVERSE_DOMAIN='${domain}' is not a valid universe API domain (expected a bare lowercase domain such as ${COMMERCIAL_UNIVERSE_DOMAIN})."
    return 1
  fi
  export UNIVERSE_DOMAIN="$domain"
  if ! is_custom_universe_domain "$domain"; then
    export CLOUDSDK_CORE_UNIVERSE_DOMAIN="$COMMERCIAL_UNIVERSE_DOMAIN"
    unset GOOGLE_CLOUD_UNIVERSE_DOMAIN
    return 0
  fi
  export GOOGLE_CLOUD_UNIVERSE_DOMAIN="$domain"
  export CLOUDSDK_CORE_UNIVERSE_DOMAIN="$domain"
  export CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE="false"
  export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS="false"
  if ! _universe_explicit_credentials_set && ! adc_matches_universe_domain "$domain"; then
    local minted=""
    minted="$(trap - ERR; gcloud auth print-access-token 2>/dev/null || true)"
    if [ -n "$minted" ]; then
      export GOOGLE_OAUTH_ACCESS_TOKEN="$minted"
    fi
  fi
  return 0
}
