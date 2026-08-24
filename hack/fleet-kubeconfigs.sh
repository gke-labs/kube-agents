#!/usr/bin/env bash
# ==============================================================================
# Seeded-fleet kubeconfigs, one per fixture ROLE
# ==============================================================================
# Cluster-state checks against the seeded fleet (bench/tf/fleet/) cannot use the
# ambient kubeconfig. ci-eval-pr.sh authenticates once, to platform-agent-host,
# and never switches context; the seeded namespaces are on other clusters
# entirely, so `kubectl get deployment payments-api -n seeded-debug` resolves
# against a cluster that has no such namespace. This script is the other half of
# the fix (bench/tasks/DRAFTS.md, blocker A5): it fetches credentials for the
# seeded clusters into their OWN files and leaves the ambient kubeconfig alone.
#
# The files are keyed by fixture ROLE, never by cluster name and never by
# project. Each eval project carries its own trio of seeded clusters, so a case
# that named `seeded-a` would be a case that only runs in one project. A case
# names `fixture_role: crashloop-workload`; bench/tf/fleet/fixtures.json says
# which SLOT of the trio that role lives on; this script finds the leased
# project's trio and matches each cluster to its slot. The catalog is the only
# place the role->slot mapping exists -- the verifier never re-derives it, it
# just opens "${BENCH_FLEET_KUBECONFIG_DIR}/<role>.kubeconfig".
#
# Clusters are DISCOVERED BY LABEL, not composed from a name:
#
#   resourceLabels.environment=seeded AND
#   resourceLabels.managed-by=kube-agents-seeded-fleet
#
# which is exactly `local.cluster_labels` in bench/tf/fleet/main.tf and is
# carried by the trio and by nothing else in an eval project (neither
# platform-agent-host nor the per-run eval-pr-* clusters have either label).
# Composing "${prefix}-${slot}" from catalog constants instead would silently
# address the wrong thing the first time someone applies the stack with a
# non-default -var cluster_prefix or into another region: the name would still
# be well-formed, so the failure would arrive as a check result rather than as
# an error here. The slot is read back off the discovered name's trailing
# "-<slot>" segment, so the prefix and the location stay the Terraform's
# business.
#
# A cluster that cannot be reached -- including a leased project where the
# stack was never applied at all, which is a live possibility while the pool is
# being filled out -- leaves its roles' files ABSENT rather than stale or
# wrong, and this script still exits 0. That is deliberate: the verifier turns a
# missing file into `status: "error"` naming the role AND the project, which
# fails exactly the checks that needed that cluster instead of the whole job,
# and it never falls back to the ambient kubeconfig -- falling back is the bug
# this script exists to remove.
#
# A role's file is only written once its namespace has been SEEN on the slot's
# cluster, before the agent runs. A labelled cluster is not the same thing as a
# planted fixture -- an apply that created the clusters and stopped before the
# Kubernetes provider ran leaves a trio that answers every API call and holds
# none of the objects -- and the verifier's whole fail/error distinction rests
# on being able to say a namespace that is gone AT CHECK TIME went missing
# DURING the run. Confirming it here is what makes that true; a fixture that
# was never planted leaves the role unresolvable, which is an error about the
# environment, not a failure blamed on the agent.
#
# Usage:
#   source hack/fleet-kubeconfigs.sh && write_fleet_kubeconfigs
#   hack/fleet-kubeconfigs.sh            # same thing; prints the directory
#
# Inputs (all optional except a project):
#   FLEET_PROJECT_ID          project holding the trio; defaults to PROJECT_ID
#   FLEET_CATALOG             path to fixtures.json
#   BENCH_FLEET_KUBECONFIG_DIR  where to write; defaults under TMPDIR
#   FLEET_READONLY_SA         service account to mint a read-only token for
#
# Output: exports BENCH_FLEET_KUBECONFIG_DIR when sourced; prints it on stdout
# when executed. Everything else this script says goes to stderr.
# ==============================================================================

_FLEET_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Written into the directory before anything is removed from it. `rm -rf` on a
# caller-supplied path is otherwise one typo'd BENCH_FLEET_KUBECONFIG_DIR away
# from deleting something that matters.
_FLEET_MARKER=".kube-agents-fleet-kubeconfigs"

# Roles the catalog declares, as "<role> <cluster_slot> <namespace-or-->" lines.
# Parsed with the standard library rather than jq: the Prow image is not
# guaranteed to carry jq, and it already runs python3 for every task-file read
# in ci-eval-pr.sh.
_fleet_catalog_rows() {
  python3 -c "
import json, re, sys
NAME = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*')
with open(sys.argv[1]) as fh:
    catalog = json.load(fh)
roles = catalog.get('roles') or {}
slots = catalog.get('cluster_slots') or {}
if not roles:
    sys.exit('fleet catalog declares no roles')
for slot in slots:
    # A slot becomes both a path segment and the suffix matched against a
    # discovered cluster name, so validate the shape here rather than
    # discovering a traversal later.
    if not NAME.fullmatch(str(slot)):
        sys.exit(f'fleet catalog cluster slot {slot!r} is not a lowercase-hyphen name')
for role, spec in sorted(roles.items()):
    if not NAME.fullmatch(role):
        sys.exit(f'fleet catalog role {role!r} is not a lowercase-hyphen name')
    slot = spec.get('cluster_slot')
    if slot not in slots:
        sys.exit(f'fleet catalog role {role!r} names unknown cluster slot {slot!r}')
    namespace = spec.get('namespace')
    if namespace is not None and not NAME.fullmatch(str(namespace)):
        sys.exit(f'fleet catalog role {role!r} names a malformed namespace {namespace!r}')
    print(role, slot, namespace if namespace else '-')
" "$1"
}

# The leased project's seeded trio, as "<slot>\t<name>\t<location>" lines, for
# each discovered cluster whose name ends in "-<slot>" for a slot the catalog
# declares. A cluster that matches the labels but no slot is reported and
# skipped: that is a fleet the catalog does not describe, and guessing at it
# would put a check on a cluster nobody wrote a fixture for.
#
# Two clusters claiming the SAME slot -- a leftover trio under an old
# cluster_prefix, or the same prefix in two zones, both legal -- drops the slot
# entirely rather than letting gcloud's listing order decide. An ambiguous
# fixture address must not resolve: silently picking one turns "the wrong
# cluster" into "the agent destroyed the fixture", which is the exact
# misdiagnosis this whole change exists to prevent.
_fleet_discover_clusters() {
  local project="$1" slots="$2" name location slot matched
  local listing rows dupes
  listing="$(gcloud container clusters list --project "$project" \
    --filter='resourceLabels.environment=seeded AND resourceLabels.managed-by=kube-agents-seeded-fleet' \
    --format='value(name,location)' 2>/dev/null)" || return 1

  rows=""
  while IFS=$'\t' read -r name location; do
    [ -n "$name" ] || continue
    matched=""
    for slot in $slots; do
      if [ "${name%-"${slot}"}" != "$name" ]; then
        matched="$slot"
        break
      fi
    done
    if [ -z "$matched" ]; then
      echo "WARNING: seeded cluster ${name} in ${project} matches no slot the catalog declares; ignoring it" >&2
      continue
    fi
    rows+="${matched}"$'\t'"${name}"$'\t'"${location}"$'\n'
  done <<<"$listing"

  dupes="$(printf '%s' "$rows" | awk -F'\t' 'NF {c[$1]++} END {for (s in c) if (c[s] > 1) print s}')"
  while read -r slot; do
    [ -n "$slot" ] || continue
    echo "WARNING: ${project} has more than one labelled seeded cluster whose name ends in '-${slot}': $(printf '%s' "$rows" | awk -F'\t' -v s="$slot" '$1 == s {printf "%s ", $2}'). The slot is ambiguous, so every check naming a role on it will report status=error rather than reading a cluster picked at random." >&2
    rows="$(printf '%s' "$rows" | awk -F'\t' -v s="$slot" '$1 != s')"
    [ -n "$rows" ] && rows+=$'\n'
  done <<<"$dupes"

  printf '%s' "$rows"
}

# Rewrite a gcloud-written kubeconfig so kubectl authenticates as $2 instead of
# whoever ran gcloud. get-credentials writes an exec entry that shells out to
# gke-gcloud-auth-plugin, and that plugin has no impersonation of its own -- so
# --impersonate-service-account on get-credentials changes who READ the cluster
# metadata and nothing about who kubectl later talks to the API server as. A
# minted access token is the only form that actually binds.
#
# The token is static and expires (one hour, unless the organization allows
# extended lifetimes). A run longer than the lifetime sees its fleet checks
# start erroring, which is loud and correct but is a real operational bound --
# re-run this script rather than lengthening a run past it.
#
# The replacement file is composed from scratch and moved into place, so a
# failure at any step leaves the gcloud-written credential intact rather than a
# context wired to a user that does not exist. It is composed with a here-doc
# rather than `kubectl config set-credentials --token=...` because that form
# puts a live bearer token in argv, where `ps` and `set -x` can both read it.
_fleet_use_readonly_token() {
  local kubeconfig="$1" sa="$2"
  local token server ca errors staged
  errors="$(mktemp)" || return 1
  # NOT 2>&1. On the SUCCESS path gcloud prints "WARNING: This command is using
  # service account impersonation..." to stderr; folding that into stdout makes
  # $token a multi-line blob that set-credentials still accepts, and every
  # subsequent API call 401s while this script reports success.
  token="$(gcloud auth print-access-token --impersonate-service-account="$sa" 2>"$errors")" || {
    echo "WARNING: could not mint a read-only token for ${sa}: $(tr '\n' ' ' <"$errors")" >&2
    rm -f "$errors"
    return 1
  }
  rm -f "$errors"
  # Belt and braces for the same failure: an OAuth2 bearer token is a run of
  # unreserved characters, so anything with whitespace or punctuation in it is
  # diagnostic text that leaked into stdout, not a credential.
  if [ -z "$token" ] || printf '%s' "$token" | LC_ALL=C grep -q '[^A-Za-z0-9._~+/=-]'; then
    echo "WARNING: what gcloud returned for ${sa} is not a bare access token; refusing to write it" >&2
    return 1
  fi

  server="$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify \
    -o jsonpath='{.clusters[0].cluster.server}')" || return 1
  [ -n "$server" ] || return 1
  ca="$(KUBECONFIG="$kubeconfig" kubectl config view --raw --minify \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')" || return 1

  staged="${kubeconfig}.staged"
  (
    umask 077
    {
      echo "apiVersion: v1"
      echo "kind: Config"
      echo "current-context: fleet"
      echo "clusters:"
      echo "  - name: fleet"
      echo "    cluster:"
      echo "      server: ${server}"
      [ -n "$ca" ] && echo "      certificate-authority-data: ${ca}"
      echo "contexts:"
      echo "  - name: fleet"
      echo "    context:"
      echo "      cluster: fleet"
      echo "      user: fleet-reader"
      echo "users:"
      echo "  - name: fleet-reader"
      echo "    user:"
      echo "      token: ${token}"
    } >"$staged"
  ) || {
    rm -f "$staged"
    return 1
  }
  mv "$staged" "$kubeconfig" || {
    rm -f "$staged"
    return 1
  }
  return 0
}

write_fleet_kubeconfigs() {
  local catalog project dir sa
  catalog="${FLEET_CATALOG:-${_FLEET_SCRIPT_DIR}/../bench/tf/fleet/fixtures.json}"
  project="${FLEET_PROJECT_ID:-${PROJECT_ID:-}}"
  dir="${BENCH_FLEET_KUBECONFIG_DIR:-${TMPDIR:-/tmp}/kube-agents-fleet-kubeconfigs}"
  sa="${FLEET_READONLY_SA:-}"

  # Dropped before any early return below. Leaving it set on a failure path
  # would point every check at a directory this run did not write -- the stale
  # credential problem, one level up.
  unset BENCH_FLEET_KUBECONFIG_DIR

  if [ ! -f "$catalog" ]; then
    echo "ERROR: fleet fixture catalog not found at ${catalog}" >&2
    return 1
  fi
  if [ -z "$project" ]; then
    echo "ERROR: no project for the seeded fleet; set FLEET_PROJECT_ID or PROJECT_ID" >&2
    return 1
  fi
  case "$dir" in
    /*) ;;
    *)
      echo "ERROR: BENCH_FLEET_KUBECONFIG_DIR must be an absolute path, got ${dir}" >&2
      return 1
      ;;
  esac

  # A malformed catalog is a repository bug and fails the caller. An
  # unreachable cluster below is weather and does not.
  local rows slots
  rows="$(_fleet_catalog_rows "$catalog")" || return 1
  slots="$(printf '%s\n' "$rows" | awk '{print $2}' | sort -u)"

  # Rebuilt from scratch: a file left by a previous run is a credential for a
  # previous project, and pointing a check at the wrong project's fixture is
  # the failure mode this whole change is about. Refuse to recurse into a
  # directory this script did not create.
  if [ -e "$dir" ] && [ ! -e "${dir}/${_FLEET_MARKER}" ]; then
    echo "ERROR: ${dir} exists and was not written by this script; refusing to remove it" >&2
    return 1
  fi
  rm -rf "$dir"
  (umask 077 && mkdir -p "$dir/clusters") || return 1
  : >"${dir}/${_FLEET_MARKER}"
  # So a check that cannot resolve its role can name the project it was looking
  # in. "role X is unavailable" is a bug report nobody can act on; "role X is
  # unavailable in kube-agents-evals-3" is one sentence from the answer.
  printf 'project=%s\n' "$project" >"${dir}/.fleet-context"
  chmod 600 "${dir}/${_FLEET_MARKER}" "${dir}/.fleet-context"

  local slot cluster location slot_config discovered errors
  local role namespace written=0 skipped=0 unplanted=0 found=0
  if ! discovered="$(_fleet_discover_clusters "$project" "$slots")"; then
    echo "WARNING: could not list clusters in ${project}; every fleet check will report status=error" >&2
    discovered=""
  fi

  while IFS=$'\t' read -r slot cluster location; do
    [ -n "$slot" ] || continue
    found=$((found + 1))
    slot_config="${dir}/clusters/${slot}.kubeconfig"
    errors="$(mktemp)"
    if ! KUBECONFIG="$slot_config" gcloud container clusters get-credentials \
      "$cluster" --location "$location" --project "$project" --quiet >/dev/null 2>"$errors"; then
      # gcloud's own message, not just ours: "cluster is RECONCILING" and
      # "insufficient permission" want different people, and an operator
      # reading Prow logs cannot re-run this by hand.
      echo "WARNING: no credentials for seeded cluster ${cluster} in ${project}: $(tr '\n' ' ' <"$errors"). Every check naming a role on slot '${slot}' will report status=error." >&2
      rm -f "$errors" "$slot_config"
      continue
    fi
    rm -f "$errors"
    if [ -n "$sa" ] && ! _fleet_use_readonly_token "$slot_config" "$sa"; then
      echo "WARNING: ${cluster} kubeconfig keeps the runner's own credential; FLEET_READONLY_SA=${sa} could not be used" >&2
    fi
    chmod 600 "$slot_config"
  done <<<"$discovered"

  if [ "$found" -eq 0 ]; then
    echo "WARNING: project ${project} carries no clusters labelled environment=seeded,managed-by=kube-agents-seeded-fleet. If the pool leased a project the fleet stack was never applied to, apply bench/tf/fleet/ there; until then every fleet check in this run reports status=error." >&2
  fi

  while read -r role slot namespace; do
    [ -n "$role" ] || continue
    slot_config="${dir}/clusters/${slot}.kubeconfig"
    if [ ! -f "$slot_config" ]; then
      skipped=$((skipped + 1))
      continue
    fi
    # The fixture must be THERE, now, before the agent has done anything. This
    # is what lets the verifier call a namespace that disappears later a
    # destroyed fixture rather than an unplanted one: a cluster can exist,
    # carry the labels and answer the API while holding none of the objects --
    # an apply that created the clusters and failed before the Kubernetes
    # provider ran leaves exactly that state. Unplanted has to resolve to
    # "error, the environment is not ready", and the only moment the two are
    # still distinguishable is this one, before the run starts.
    if [ "$namespace" != "-" ] &&
      ! KUBECONFIG="$slot_config" kubectl get namespace "$namespace" >/dev/null 2>&1; then
      echo "WARNING: namespace ${namespace} is absent from ${slot_config##*/} in ${project}, so fixture role '${role}' was never planted (or has already been destroyed). Its checks will report status=error rather than blaming the run." >&2
      unplanted=$((unplanted + 1))
      continue
    fi
    # A copy per role, not a symlink: the role file is what a check opens, and
    # a broken symlink reads as "unreadable" where an absent file reads as
    # "this role was never provisioned" -- the diagnosis the verifier prints.
    cp "$slot_config" "${dir}/${role}.kubeconfig"
    chmod 600 "${dir}/${role}.kubeconfig"
    written=$((written + 1))
  done <<<"$rows"

  export BENCH_FLEET_KUBECONFIG_DIR="$dir"
  echo "Seeded-fleet kubeconfigs: ${written} role(s) written to ${dir}, ${skipped} on unreachable clusters, ${unplanted} not planted (project ${project})" >&2
  if [ -z "$sa" ]; then
    echo "WARNING: FLEET_READONLY_SA is unset, so these kubeconfigs carry the runner's own credential, which can WRITE to the shared fleet. See bench/tf/fleet/README.md, 'A read-only credential for evaluations'." >&2
  fi
  return 0
}

# Executed rather than sourced: the export cannot outlive this process, so
# print the directory instead and let the caller capture it.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  write_fleet_kubeconfigs
  printf '%s\n' "$BENCH_FLEET_KUBECONFIG_DIR"
fi
