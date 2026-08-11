#!/usr/bin/env bash
# Syncs the Helm chart's copies of operator-owned manifests from their source
# of truth under k8s-operator/config/:
#   - charts/kube-agents/crds/*.yaml are verbatim copies of config/crd/bases/.
#   - The ClusterRole rules in templates/operator-rbac.yaml are spliced from
#     config/rbac/role.yaml between the GENERATED RULES markers.
# Run with --check (CI, `make chart-check`) to fail instead of rewriting.
set -euo pipefail
cd "$(dirname "$0")/.."

CRD_SRC=k8s-operator/config/crd/bases
CRD_DST=charts/kube-agents/crds
ROLE_SRC=k8s-operator/config/rbac/role.yaml
RBAC_TPL=charts/kube-agents/templates/operator-rbac.yaml

check=false
[[ "${1:-}" == "--check" ]] && check=true

fail() {
  echo "ERROR: $1 (run 'make chart-sync' and commit the result)" >&2
  exit 1
}

# CRDs: verbatim copies, and no stale extras left behind in the chart.
for src in "$CRD_SRC"/*.yaml; do
  dst="$CRD_DST/$(basename "$src")"
  if $check; then
    [[ -f "$dst" ]] || fail "chart CRD copy missing: $dst"
    diff -u "$dst" "$src" >&2 || fail "chart CRD copy out of date: $dst"
  else
    cp "$src" "$dst"
  fi
done
for dst in "$CRD_DST"/*.yaml; do
  src="$CRD_SRC/$(basename "$dst")"
  if [[ ! -f "$src" ]]; then
    if $check; then
      fail "stale chart CRD with no source: $dst"
    else
      rm -f "$dst"
      echo "Removed stale chart CRD copy: $dst"
    fi
  fi
done

# ClusterRole rules: splice role.yaml's rules block between the markers.
# Without the BEGIN marker the awk pass would copy the template unchanged and
# both modes would silently pass with stale rules, so fail up front.
grep -q '# BEGIN GENERATED RULES' "$RBAC_TPL" ||
  fail "missing BEGIN GENERATED RULES marker in $RBAC_TPL"
tmp=$(mktemp)
awk -v rolefile="$ROLE_SRC" '
  /# BEGIN GENERATED RULES/ {
    print
    found = 0
    while ((getline line < rolefile) > 0) {
      if (found) print line
      if (line == "rules:") found = 1
    }
    close(rolefile)
    skip = 1
    next
  }
  /# END GENERATED RULES/ { skip = 0 }
  !skip { print }
' "$RBAC_TPL" >"$tmp"

# Sanity-check the splice: a missing END marker would truncate the template,
# and a reformatted role.yaml (e.g. "rules:" no longer at column 0) would
# splice zero rules into the ClusterRole.
grep -q '# END GENERATED RULES' "$tmp" || { rm -f "$tmp"; fail "splice lost the END marker in $RBAC_TPL"; }
grep -q '^  - apiGroups:' "$tmp" || { rm -f "$tmp"; fail "splice produced no rules — check $ROLE_SRC formatting"; }

if $check; then
  diff -u "$RBAC_TPL" "$tmp" >&2 || { rm -f "$tmp"; fail "chart ClusterRole rules out of date vs $ROLE_SRC"; }
  rm -f "$tmp"
  echo "Chart CRD and RBAC copies are in sync with k8s-operator/config."
else
  mv "$tmp" "$RBAC_TPL"
  echo "Chart CRD and RBAC copies synced from k8s-operator/config."
fi
