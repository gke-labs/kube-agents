# shellcheck shell=bash
# Sourced by render_install_env.sh (the in-place reconcile) and
# provision_environment.sh (the destroy-and-rebuild of rc and nightly), so the
# two paths can't disagree about the token minter's names.
#
# A GitHub environment has always run its minter on the project-wide key ring,
# key and GSA. The installer derives per-install names for an install with no
# minter in its state, which is what a rebuild after an uninstall is. So an
# environment with the minter on (GITHUB_APP_ID set) and no names of its own
# states the project-wide ones explicitly: the installer adopts an unlabelled
# key it is named explicitly as this install's own, and the apply labels it.
#
# Sets and exports KMS_KEYRING and KMS_KEY when unset, and sets
# MINTER_ENVIRONMENT_GSA_NAME and MINTER_ENVIRONMENT_KSA_NAME to the
# project-wide GSA and KSA names (#5282: the KSA its Workload Identity binding
# was made with), for the caller to hand on in its own way. Does nothing for an
# environment without the minter.
minter_environment_names() {
  [ -n "${GITHUB_APP_ID:-}" ] || return 0
  local defaults ring key gsa ksa
  defaults="$(dirname "${BASH_SOURCE[0]}")/../../install.defaults.env"
  IFS='|' read -r ring key gsa ksa < <(
    # shellcheck source=/dev/null
    . "$defaults"
    printf '%s|%s|%s|%s\n' "$DEFAULT_KMS_KEYRING" "$DEFAULT_KMS_KEY" "$DEFAULT_GITHUB_MINTER_GSA_NAME" \
      "$DEFAULT_GITHUB_MINTER_KSA_NAME"
  )
  KMS_KEYRING="${KMS_KEYRING:-$ring}"
  KMS_KEY="${KMS_KEY:-$key}"
  # shellcheck disable=SC2034 # read by the caller
  MINTER_ENVIRONMENT_GSA_NAME="$gsa"
  # shellcheck disable=SC2034 # read by the caller
  MINTER_ENVIRONMENT_KSA_NAME="$ksa"
  export KMS_KEYRING KMS_KEY
}
