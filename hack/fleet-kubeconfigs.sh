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
# A role's file is only written once EVERY object in its catalog `probes` list
# has been SEEN on the slot's cluster, before the agent runs, and the list of
# what was seen is written beside it as "<role>.confirmed". A labelled cluster
# is not the same thing as a planted fixture -- an apply that created the
# clusters and stopped before the Kubernetes provider ran leaves a trio that
# answers every API call and holds none of the objects -- and the verifier's
# whole fail/error distinction rests on being able to say an object that is
# gone AT CHECK TIME went missing DURING the run. Confirming it here is what
# makes that true, and confirming it PER OBJECT rather than per namespace is
# what makes it true for the roles that have no namespace: four of the seven
# are cluster-scoped, so a namespace-only gate waved them through and let a
# check on a live-but-empty cluster blame an agent that touched nothing. A
# fixture that was never planted leaves the role unresolvable, which is an
# error about the environment, not a failure blamed on the agent.
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

# Roles the catalog declares, one per line:
#
#   <role> <cluster_slot> <namespace-or--> [<probe> ...]
#
# A role's namespace is emitted as an implicit leading `namespace/<ns>` probe,
# so the loop below has one uniform list to walk. Probes never contain
# whitespace (they are `<kind>/<name>` or `<kind>?<selector>`), which is what
# lets the caller read the tail with plain word splitting.
#
# Parsed with the standard library rather than jq: the Prow image is not
# guaranteed to carry jq, and it already runs python3 for every task-file read
# in ci-eval-pr.sh.
_fleet_catalog_rows() {
  python3 -c "
import json, re, sys
NAME = re.compile(r'[a-z0-9]+(-[a-z0-9]+)*')
# A probe reaches kubectl as a kind plus either a name or a label selector.
# Anchored so nothing in the catalog can smuggle a flag, a space or a shell
# metacharacter into that command line.
PROBE = re.compile(r'[a-z][a-z0-9.]*(/[a-z0-9][a-z0-9.-]*|\?[A-Za-z0-9][A-Za-z0-9._/=,-]*)')
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
    probes = spec.get('probes')
    if not isinstance(probes, list):
        sys.exit(f'fleet catalog role {role!r} declares no probes list')
    for probe in probes:
        if not isinstance(probe, str) or not PROBE.fullmatch(probe):
            sys.exit(f'fleet catalog role {role!r} declares a malformed probe {probe!r}')
    if namespace:
        probes = [f'namespace/{namespace}'] + probes
    print(role, slot, namespace if namespace else '-', *probes)
" "$1"
}

# Is the object a probe names present on $1 right now? The role's namespace is
# $2 ('-' when it has none).
#
# A selector probe must match at least one object: `kubectl get node -l
# app=nope` exits ZERO with no output, which is the same trap that made the
# pathless `absent` safeguards read as passes on the wrong cluster.
_fleet_probe_present() {
  local kubeconfig="$1" namespace="$2" probe="$3" kind rest
  case "$probe" in
    *\?*)
      kind="${probe%%\?*}"
      rest="${probe#*\?}"
      if [ "$namespace" != "-" ] && [ -n "$namespace" ]; then
        [ -n "$(KUBECONFIG="$kubeconfig" kubectl get "$kind" -n "$namespace" -l "$rest" -o name 2>/dev/null)" ]
      else
        [ -n "$(KUBECONFIG="$kubeconfig" kubectl get "$kind" -l "$rest" -o name 2>/dev/null)" ]
      fi
      ;;
    */*)
      kind="${probe%%/*}"
      rest="${probe#*/}"
      # Spelled out twice rather than built into an array: bash 3.2 (still what
      # macOS ships, and what a contributor runs the tests on) errors on
      # "${empty[@]}" under `set -u`.
      if [ "$namespace" != "-" ] && [ -n "$namespace" ]; then
        KUBECONFIG="$kubeconfig" kubectl get "$kind" "$rest" -n "$namespace" >/dev/null 2>&1
      else
        KUBECONFIG="$kubeconfig" kubectl get "$kind" "$rest" >/dev/null 2>&1
      fi
      ;;
    *)
      return 1
      ;;
  esac
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
#
# Split in two so the caller can tell "no seeded clusters here at all" (apply
# the stack) from "seeded clusters are here but none resolved to a slot"
# (ambiguous, or named under a prefix scheme the catalog does not describe) --
# two different WARNINGs for two different people.
_fleet_list_seeded_clusters() {
  gcloud container clusters list --project "$1" \
    --filter='resourceLabels.environment=seeded AND resourceLabels.managed-by=kube-agents-seeded-fleet' \
    --format='value(name,location)' 2>/dev/null
}

_fleet_match_slots() {
  local listing="$1" slots="$2" project="$3" name location slot matched
  local rows dupes

  rows=""
  while IFS=$'\t' read -r name location; do
    [ -n "$name" ] || continue
    matched=""
    for slot in $slots; do
      # Longest suffix wins. Slots "a" and "batch-a" both end
      # "seeded-batch-a", and taking the first match in sorted order would
      # hand slot 'a' a cluster belonging to 'batch-a'. The longest match is
      # unique by construction: two different slots cannot both be the
      # same-length tail of one name.
      if [ "${name%-"${slot}"}" != "$name" ] &&
        [ "${#slot}" -gt "${#matched}" ]; then
        matched="$slot"
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

_fleet_discover_clusters() {
  local listing
  listing="$(_fleet_list_seeded_clusters "$1")" || return 1
  _fleet_match_slots "$listing" "$2" "$1"
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

  local slot cluster location slot_config listing discovered errors
  local role namespace probe probes confirmed missing
  local written=0 unresolved=0 unplanted=0 found=0 labelled=0
  if ! listing="$(_fleet_list_seeded_clusters "$project")"; then
    echo "WARNING: could not list clusters in ${project}; every fleet check will report status=error" >&2
    listing=""
  fi
  # `[ -n ... ] && x=...` would be an AND-list whose failure trips errexit in
  # ci-eval-pr.sh, which sources this file under `set -e`.
  if [ -n "$listing" ]; then
    labelled="$(printf '%s\n' "$listing" | grep -c '[^[:space:]]' || true)"
  fi
  discovered="$(_fleet_match_slots "$listing" "$slots" "$project")"

  while IFS=$'\t' read -r slot cluster location; do
    [ -n "$slot" ] || continue
    found=$((found + 1))
    slot_config="${dir}/clusters/${slot}.kubeconfig"
    if ! errors="$(mktemp)"; then
      echo "WARNING: could not create a temporary file; skipping seeded cluster ${cluster}" >&2
      continue
    fi
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

  if [ "$labelled" -eq 0 ]; then
    echo "WARNING: project ${project} carries no clusters labelled environment=seeded,managed-by=kube-agents-seeded-fleet. If the pool leased a project the fleet stack was never applied to, apply bench/tf/fleet/ there; until then every fleet check in this run reports status=error." >&2
  elif [ "$found" -eq 0 ]; then
    # Clusters are there and labelled; not one of them resolved. Telling this
    # operator to apply the stack would send them to re-create what already
    # exists. The warnings above name the individual clusters.
    echo "WARNING: project ${project} carries ${labelled} labelled seeded cluster(s) but none resolved to a catalog slot -- see the per-cluster warnings above for whether they were ambiguous or named outside the catalog's slots. Every fleet check in this run reports status=error." >&2
  fi

  while read -r role slot namespace probes; do
    [ -n "$role" ] || continue
    slot_config="${dir}/clusters/${slot}.kubeconfig"
    if [ ! -f "$slot_config" ]; then
      unresolved=$((unresolved + 1))
      continue
    fi
    # Every object the role's checks assert on must be THERE, now, before the
    # agent has done anything. This is what lets the verifier call an object
    # that disappears later a destroyed fixture rather than an unplanted one: a
    # cluster can exist, carry the labels and answer the API while holding none
    # of the objects -- an apply that created the clusters and failed before
    # the Kubernetes provider ran leaves exactly that state. Unplanted has to
    # resolve to "error, the environment is not ready", and the only moment the
    # two are still distinguishable is this one, before the run starts.
    #
    # Confirming the NAMESPACE alone is not enough, and was the first version
    # of this gate: four of the roles have no namespace at all, so it waved
    # them through and let a check on a live-but-empty cluster report a
    # catastrophic `fail` against an agent that never touched anything.
    confirmed=""
    missing=""
    for probe in $probes; do
      if _fleet_probe_present "$slot_config" "$namespace" "$probe"; then
        confirmed+="${probe}"$'\n'
      else
        missing+="${probe} "
      fi
    done
    if [ -n "$missing" ]; then
      echo "WARNING: ${missing% } absent from ${slot_config##*/} in ${project}, so fixture role '${role}' was never planted (or has already been destroyed). Its checks will report status=error rather than blaming the run." >&2
      unplanted=$((unplanted + 1))
      continue
    fi
    # A copy per role, not a symlink: the role file is what a check opens, and
    # a broken symlink reads as "unreadable" where an absent file reads as
    # "this role was never provisioned" -- the diagnosis the verifier prints.
    cp "$slot_config" "${dir}/${role}.kubeconfig"
    chmod 600 "${dir}/${role}.kubeconfig"
    # The subjects this run is entitled to blame the agent for losing. The
    # verifier reads it; a subject absent from this file was never seen, so
    # its later absence is the environment's, not the run's.
    printf '%s' "$confirmed" >"${dir}/${role}.confirmed"
    chmod 600 "${dir}/${role}.confirmed"
    written=$((written + 1))
  done <<<"$rows"

  export BENCH_FLEET_KUBECONFIG_DIR="$dir"
  echo "Seeded-fleet kubeconfigs: ${written} role(s) written to ${dir}, ${unresolved} on clusters that could not be resolved or reached, ${unplanted} whose fixtures were not present (project ${project})" >&2
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
