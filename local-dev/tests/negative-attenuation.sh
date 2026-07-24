#!/usr/bin/env bash
# A3 / 03 §11 attenuation negative test (load-bearing).
# Applies the agent-read-only ValidatingAdmissionPolicy, then asserts:
#   - a Role granting an agent SA a write verb            -> DENIED
#   - a ClusterRole granting a privilege-escalation verb  -> DENIED
#   - a ClusterRole for the namespace tier (wrong-scope)  -> DENIED
#   - a read-only agent Role                              -> ADMITTED
# Adversarially distinguishes a real policy denial from a malformed-object error.
#
# DESTRUCTIVE-TEST GUARD: only runs against a Kind or scratch-GKE context.
# Usage: local-dev/tests/negative-attenuation.sh [kube-context]
set -uo pipefail  # -e omitted deliberately: kubectl exit codes are inspected manually below.

CTX="${1:-kind-kube-agents-dev}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VAP="$REPO_ROOT/examples/gitops-repo/policy/vap-agent-readonly.yaml"
K="kubectl --context $CTX"

# Anchored allow-list: kind-* (up.sh) and gke-scratch-* (create.sh rename) ONLY. Substring globs like
# *scratch* would let a prod context (e.g. gke_prod_..._kube-agents-dev-prod) slip through — never do that.
case "$CTX" in
  kind-* | gke-scratch-*) : ;;
  *) echo "REFUSING: context '$CTX' is not a Kind/scratch cluster (destructive-test guard)." >&2; exit 2 ;;
esac

fail=0
pass() { echo "PASS: $1"; }
bad()  { echo "FAIL: $1"; fail=1; }

echo "== applying ValidatingAdmissionPolicy =="
$K apply -f "$VAP" || { echo "could not apply VAP"; exit 1; }
$K create namespace team-x --dry-run=client -o yaml | $K apply -f - >/dev/null 2>&1 || true
sleep 3  # allow the policy/binding to register

# Helpers read the manifest from stdin ONCE into a variable, then feed both apply and delete from it.
# (A heredoc is consumed by the first read; a second `kubectl -f -` would get empty stdin and silently
# no-op, leaking the admitted object — so we re-emit the captured manifest for cleanup.)
expect_deny() {
  local name="$1" needle="$2"; shift 2
  local manifest; manifest="$(cat)"
  local out rc; out="$(printf '%s' "$manifest" | $K apply -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    bad "$name was ADMITTED (expected denial)"
    printf '%s' "$manifest" | $K delete -f - >/dev/null 2>&1 || true
    return
  fi
  if echo "$out" | grep -qi "$needle"; then pass "$name denied by policy"; else
    bad "$name rejected but NOT by our policy (adversarial check): $out"; fi
}
expect_admit() {
  local name="$1"
  local manifest; manifest="$(cat)"
  local out rc; out="$(printf '%s' "$manifest" | $K apply -f - 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then
    pass "$name admitted"
    printf '%s' "$manifest" | $K delete -f - >/dev/null 2>&1 || true
  else bad "$name was DENIED (expected admit): $out"; fi
}

echo "== 1) write-verb Role (expect deny) =="
expect_deny "write-verb Role" "read verbs" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: evil-writer, namespace: team-x, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "create", "delete"]
EOF

echo "== 2) privilege-escalation ClusterRole (impersonate; expect deny) =="
expect_deny "impersonate ClusterRole" "read verbs" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: evil-impersonator, labels: { kube-agents/tier: platform } }
rules:
  - apiGroups: [""]
    resources: ["users", "groups", "serviceaccounts"]
    verbs: ["impersonate"]
EOF

echo "== 3) wrong-scope ClusterRole for namespace tier (expect deny) =="
expect_deny "wrong-scope ClusterRole" "wrong-scope" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: { name: evil-scope, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
EOF

echo "== 4) read-only agent Role (expect admit) =="
expect_admit "read-only Role" <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: { name: good-reader, namespace: team-x, labels: { kube-agents/tier: developer-team } }
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
EOF

echo
if [ "$fail" -eq 0 ]; then echo "A3 attenuation: ALL CHECKS PASSED"; else echo "A3 attenuation: FAILURES ABOVE"; fi
exit "$fail"
