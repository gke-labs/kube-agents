#!/usr/bin/env bash
# ==============================================================================
# Minimum Supported Tool Versions
# ==============================================================================
# The single home for every "you need at least version X" number in the
# installer front doors. Kept free of side effects — no state loading, no
# argument parsing, no output at source time — because both the standalone
# installer (install.sh, which does not source common.sh) and the scripts
# that source common.sh need these numbers. Sourcing this file must be safe
# from either.
#
# Print/exit behaviour lives in the callers; the helpers here only compare.
# ==============================================================================

# gcloud 576.0.0 (2026-07-14) promoted --managed-otel-scope to GA on
# `container clusters create`, `create-auto`, and `update`. Earlier releases
# expose it on the alpha/beta tracks only, so install.sh's post-create
# `clusters update --managed-otel-scope` — issued on the GA surface — fails
# argument parsing before it issues a single API call. That failure arrives
# *after* the cluster and its GCP resources have been applied, which is why
# the check runs up front rather than being left to gcloud.
MIN_GCLOUD_VERSION="576.0.0"

# Compare two dotted version strings. Returns 0 when $1 is strictly older than
# $2. Uses sort -V rather than a field-by-field loop so that the many shapes
# gcloud has shipped over the years (450.0.0, 2026.05.08) all order sanely.
version_lt() {
  local lhs="$1" rhs="$2"
  [ "$lhs" = "$rhs" ] && return 1
  [ "$(printf '%s\n%s\n' "$lhs" "$rhs" | sort -V | head -n1)" = "$lhs" ]
}

# Echo the core Google Cloud SDK version, e.g. "576.0.0".
#
# `gcloud version` prints one component per line and the alpha/beta components
# carry date-style versions ("alpha 2026.05.08"). Only the "Google Cloud SDK"
# line is the release number the release notes are keyed on, so it is matched
# by name instead of by position.
gcloud_core_version() {
  gcloud version 2>/dev/null | sed -n 's/^Google Cloud SDK \([0-9][0-9.]*\).*/\1/p' | head -n1
}

# The Terraform configurations pin `required_version = "~> 1.5"`, which
# `terraform init` enforces before anything is created. This front-door twin
# exists so the failure lands in the prerequisite step with the other tool
# checks rather than at the init inside step 12. Keep the two in step:
# terraform/examples/full-install/providers.tf owns the authoritative pin.
MIN_TERRAFORM_VERSION="1.5.0"

# Echo the Terraform core version, e.g. "1.15.8", from the first line of
# `terraform version` ("Terraform v1.15.8").
terraform_core_version() {
  terraform version 2>/dev/null | sed -n 's/^Terraform v\([0-9][0-9.]*\).*/\1/p' | head -n1
}

# Fail unless the installed terraform is at least MIN_TERRAFORM_VERSION.
# An unreadable version warns rather than errors, for the same reason the
# gcloud check below does: required_version still backstops it at init.
require_min_terraform_version() {
  local found
  found="$(terraform_core_version)"

  if [ -z "$found" ]; then
    print_warning "Could not determine the Terraform version; skipping the >= ${MIN_TERRAFORM_VERSION} check (terraform init enforces it anyway)."
    return 0
  fi

  if version_lt "$found" "$MIN_TERRAFORM_VERSION"; then
    print_error "Terraform ${found} is too old; ${MIN_TERRAFORM_VERSION} or newer is required (the configurations pin ~> 1.5)."
    print_info "Upgrade terraform (brew upgrade hashicorp/tap/terraform, or apt-get install terraform from HashiCorp's repository)."
    return 1
  fi

  print_success "Terraform ${found} meets the minimum of ${MIN_TERRAFORM_VERSION}."
  return 0
}

# Fail unless the installed gcloud is at least MIN_GCLOUD_VERSION.
#
# An unreadable version is a warning, not an error: gcloud has changed the
# output of `gcloud version` before, and refusing to install because a regex
# missed would be a worse failure than letting the flag error out downstream.
require_min_gcloud_version() {
  local found
  found="$(gcloud_core_version)"

  if [ -z "$found" ]; then
    print_warning "Could not determine the Google Cloud SDK version; skipping the >= ${MIN_GCLOUD_VERSION} check."
    return 0
  fi

  if version_lt "$found" "$MIN_GCLOUD_VERSION"; then
    print_error "Google Cloud SDK ${found} is too old; ${MIN_GCLOUD_VERSION} or newer is required."
    print_info "Cluster creation passes --managed-otel-scope on the GA surface, which arrived in gcloud ${MIN_GCLOUD_VERSION} (2026-07-14)."
    print_info "Upgrade with 'gcloud components update', or reinstall the SDK if it was installed from a package manager."
    return 1
  fi

  print_success "Google Cloud SDK ${found} meets the minimum of ${MIN_GCLOUD_VERSION}."
  return 0
}
