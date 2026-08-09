#!/usr/bin/env bash
# Syncs the Helm chart's copies of operator-owned manifests from their source
# of truth under k8s-operator/config/:
#   - charts/kube-agents/crds/*.yaml are verbatim copies of config/crd/bases/.
#   - The ClusterRole rules in templates/operator-rbac.yaml are spliced from
#     config/rbac/role.yaml between the GENERATED RULES markers.
#   - templates/agent-rbac-admission-policy.yaml is config/admission/agent-rbac-policy.yaml
#     wrapped in the chart's values gate. The script-based install applies that
#     source file directly, so the chart and the scripts must not drift apart.
# Run with --check (CI, `make chart-check`) to fail instead of rewriting.
set -euo pipefail
cd "$(dirname "$0")/.."

CRD_SRC=k8s-operator/config/crd/bases
CRD_DST=charts/kube-agents/crds
ROLE_SRC=k8s-operator/config/rbac/role.yaml
RBAC_TPL=charts/kube-agents/templates/operator-rbac.yaml
VAP_SRC=k8s-operator/config/admission/agent-rbac-policy.yaml
VAP_TPL=charts/kube-agents/templates/agent-rbac-admission-policy.yaml

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
else
  mv "$tmp" "$RBAC_TPL"
fi

# Admission policies: the whole template is generated, so there is nothing in it
# to hand-edit and nothing to splice around — the chart adds only the values gate.
# Everything else, including the honesty header about what the policies do not
# cover, lives in the source file so every install that applies it carries it.
[[ -f "$VAP_SRC" ]] || fail "admission policy source missing: $VAP_SRC"
tmp=$(mktemp)
{
  echo '{{- if .Values.admissionPolicy.enabled }}'
  echo "# GENERATED from $VAP_SRC — do not edit by hand; run \`make chart-sync\`."
  echo '# See values.yaml (admissionPolicy) for the gate and when to turn it off.'
  cat "$VAP_SRC"
  echo '{{- end }}'
} >"$tmp"

if $check; then
  diff -u "$VAP_TPL" "$tmp" >&2 || { rm -f "$tmp"; fail "chart admission policy out of date vs $VAP_SRC"; }
  rm -f "$tmp"
  echo "Chart CRD, RBAC and admission-policy copies are in sync with k8s-operator/config."
else
  mv "$tmp" "$VAP_TPL"
  echo "Chart CRD, RBAC and admission-policy copies synced from k8s-operator/config."
fi
