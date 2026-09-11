#!/usr/bin/env bash
# Runs lifecycle.sh's tfvar() against a real `terraform console` for every
# variable the script reads, and fails if any of them comes back in a shape the
# helper did not normalise.
#
# The unit tests in tests/test_lifecycle_script.py stub `terraform`, so they
# check what the script does with an answer, never what the binary actually
# answers. #1309's stub said `null` for an unset nullable string; Terraform
# says `tostring(null)`. The GSA guard read that as a custom name, refused
# every apply whose tfvars left the default in place, and the autopush deploy
# was the first thing to run the real binary -- four and a half hours after
# merge (#1350). validate.yml already installs Terraform and initialises the
# composition; this is the step that puts the helper in front of it.
#
# The sweep is read out of the source rather than listed here: every variable
# named literally at a tfvar call site in lifecycle.sh, plus every string
# variable variables.tf declares with `default = null`. The second set is what
# makes a call that passes the name in a shell variable -- `$(tfvar "$name")`
# -- safe to leave unread: a string with a non-null default cannot come back
# as anything but a plain value, so the nullable strings are the only ones that
# can leak whichever way they are read. A nullable bool or list read through
# tfvar is the bound: the helper does not normalise those shapes today, and
# nothing reads one. The variables without a default are given placeholders so
# their answer is a value rather than `(known after apply)`.
#
# Run via `make tfvar-check`; CI runs it in validate.yml. `--list` prints the
# variables the sweep would evaluate and exits, without touching terraform.
set -euo pipefail
cd "$(dirname "$0")/.."

readonly COMPOSITION=terraform/examples/full-install
readonly LIFECYCLE=lifecycle.sh
readonly VARIABLES_TF=variables.tf
# A Terraform identifier: letters, digits, underscores and hyphens.
readonly TFVAR_CALL_PATTERN='\$\(tfvar [A-Za-z0-9_-]+'
readonly REPORT_NAME_WIDTH=32
# Shapes `terraform console` prints for anything that is not a plain value:
# `tostring(null)`, `tobool(null)`, `tolist(null) /* of string */`,
# `(known after apply)`, `(sensitive value)`, and for a populated list or map
# the closing `])` or `}` that tfvar's `tail -1` would keep. Every one of them
# carries a bracket or the word `null`, and no variable lifecycle.sh reads
# legitimately does.
readonly LEAK_PATTERN='null|[][(){}]'
readonly LIST_FLAG=--list
readonly PLACEHOLDER_PROJECT=tfvar-check-project
readonly PLACEHOLDER_CLUSTER=tfvar-check-cluster
readonly PLACEHOLDER_LOCATION=us-central1
readonly PLACEHOLDER_API_SERVER_KEY=tfvar-check-key

cd "$COMPOSITION"

# The lifecycle script's `set -euo pipefail` and its `cd` to its own directory
# both come along with the source; the composition directory is that directory.
# lifecycle.sh is linted on its own; following it here would lint it twice.
# shellcheck disable=SC1090,SC1091
KUBE_AGENTS_SOURCE_ONLY=true source "./$LIFECYCLE"

literal_callers() { grep -oE "$TFVAR_CALL_PATTERN" "$LIFECYCLE" | awk '{print $2}'; }
nullable_strings() {
  awk '
    /^variable "/ { name = $2; gsub(/"/, "", name); is_string = 0; is_null = 0 }
    /^[ \t]*type[ \t]*=[ \t]*string[ \t]*$/ { is_string = 1 }
    /^[ \t]*default[ \t]*=[ \t]*null[ \t]*$/ { is_null = 1 }
    /^}/ { if (name != "" && is_string && is_null) print name; name = "" }
  ' "$VARIABLES_TF"
}
variables=()
while IFS= read -r name; do variables+=("$name"); done < <({ literal_callers; nullable_strings; } | sort -u)
if [[ $(literal_callers | wc -l) -eq 0 ]]; then
  echo "ERROR: found no tfvar callers in $COMPOSITION/$LIFECYCLE; the pattern is stale." >&2
  exit 1
fi
if [[ "${1:-}" == "$LIST_FLAG" ]]; then
  printf '%s\n' "${variables[@]}"
  exit 0
fi

command -v terraform >/dev/null 2>&1 || {
  echo "ERROR: terraform is required to check the tfvar helper." >&2
  exit 1
}

export TF_VAR_project_id=$PLACEHOLDER_PROJECT
export TF_VAR_cluster_name=$PLACEHOLDER_CLUSTER
export TF_VAR_location=$PLACEHOLDER_LOCATION
export TF_VAR_api_server_key=$PLACEHOLDER_API_SERVER_KEY

# -backend=false: console evaluates variables from configuration and tfvars
# alone, so on a fresh checkout -- CI's case -- no state and no credentials are
# needed. A checkout that lifecycle.sh has already initialised against a remote
# backend keeps that backend, and console then opens it. A no-op when
# validate.yml's own init has already run.
terraform init -backend=false -input=false >/dev/null

status=0
for name in "${variables[@]}"; do
  # tfvar exits the shell on a console failure; the subshell keeps that to one
  # variable so the rest still report.
  if ! value=$(tfvar "$name"); then
    echo "ERROR: tfvar $name: terraform console could not evaluate it." >&2
    status=1
    continue
  fi
  if grep -qE "$LEAK_PATTERN" <<<"$value"; then
    echo "ERROR: tfvar $name returned raw console output '$value'; tfvar() must normalise this shape." >&2
    status=1
    continue
  fi
  printf "%-${REPORT_NAME_WIDTH}s -> %s\n" "$name" "${value:-<empty>}"
done

if [[ $status -ne 0 ]]; then
  echo "ERROR: lifecycle.sh's tfvar() leaked terraform console syntax for at least one variable (see above)." >&2
fi
exit "$status"
