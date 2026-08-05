#!/usr/bin/env bash
# ==============================================================================
# 🔍 Documentation terminology guard
# ==============================================================================
# Fails when documentation uses an identifier that does not match the source of
# truth. These are not style preferences: every rule below corresponds to a
# real defect that shipped, where a reader copy-pasting from the docs would
# have got a name that does not resolve.
#
# Portable to bash 3.2 (macOS) as well as CI. Deliberately fails loudly rather
# than skipping checks it cannot run.
# ==============================================================================

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

FAILED=0

# Documentation this guard inspects. The guard itself is excluded, since it
# necessarily contains the strings it forbids.
FILE_LIST=$(mktemp)
trap 'rm -f "$FILE_LIST"' EXIT

git ls-files '*.md' '*.mdx' \
  | grep -v '^docs/site/node_modules/' \
  | grep -v '^hack/check-docs-terminology.sh$' \
  > "$FILE_LIST"

FILE_COUNT=$(wc -l < "$FILE_LIST" | tr -d ' ')
if [ "$FILE_COUNT" -eq 0 ]; then
  echo "ERROR: no documentation files found - the guard cannot run." >&2
  exit 1
fi

echo "Checking terminology across ${FILE_COUNT} documentation files..."

# search <extended-regex> -> prints "path:line:text" matches, empty if none
search() {
  tr '\n' '\0' < "$FILE_LIST" | xargs -0 grep -nEI -H "$1" 2>/dev/null || true
}

# forbid <extended-regex> <explanation>
forbid() {
  local hits
  hits=$(search "$1")
  if [ -n "$hits" ]; then
    echo "::error::$2"
    printf '%s\n\n' "$hits" | sed 's/^/    /'
    FAILED=1
  fi
}

# --- Identity names -------------------------------------------------------
# Ground truth: k8s-operator/scripts/common.sh
forbid 'kubeagents-platform-agent-gsa' \
  "Wrong GCP service account name. common.sh sets PLATFORM_AGENT_GSA_NAME=kubeagents-platform-gsa."

forbid 'platform-agent-ksa' \
  "Wrong Kubernetes service account name. common.sh sets PLATFORM_AGENT_KSA_NAME=kubeagents-platform-agent (no -ksa suffix)."

# --- Namespace ------------------------------------------------------------
forbid 'platform-agent-system' \
  "Stale namespace. The namespace is kubeagents-system."

# --- Go toolchain ---------------------------------------------------------
# Ground truth: k8s-operator/go.mod
GO_MOD_VERSION=$(awk '/^go /{print $2; exit}' k8s-operator/go.mod)
if [ -z "$GO_MOD_VERSION" ]; then
  echo "ERROR: could not read the go directive from k8s-operator/go.mod." >&2
  exit 1
fi
GO_MINOR=$(printf '%s' "$GO_MOD_VERSION" | cut -d. -f1,2)   # 1.25.8 -> 1.25

WRONG_GO=$(search 'Go[^0-9]{0,20}1\.[0-9]+\+' | grep -vF "${GO_MINOR}+" || true)
if [ -n "$WRONG_GO" ]; then
  echo "::error::Documented Go version does not match k8s-operator/go.mod (go ${GO_MOD_VERSION}; expected \"${GO_MINOR}+\")."
  printf '%s\n\n' "$WRONG_GO" | sed 's/^/    /'
  FAILED=1
fi

# --- fleet-audit finding id pattern ---------------------------------------
# Ground truth: FINDING_ID_RE in the fleet-audit harness. Seven documents quote
# this pattern back to the model — SKILL.md, the five governance SOPs, and the
# design doc — and an id that does not match is rejected before anything is
# published. When the pattern was last relaxed, all seven copies silently went
# stale, so the docs told the model to generate ids the validator refused.
AUDIT_SCRIPT=agents/platform/skills/fleet-audit/scripts/audit_report.py
if [ ! -f "$AUDIT_SCRIPT" ]; then
  echo "ERROR: ${AUDIT_SCRIPT} not found; the finding-id guard cannot run." >&2
  exit 1
fi

# The source pattern, rewritten into the form prose should use: a non-capturing
# group reads as noise to a human, and `\Z` is a Python-ism whose only
# difference from `$` (not matching before a trailing newline) is precisely why
# the code uses it and precisely what prose does not need to say.
ID_PATTERN=$(awk -F"r\"" '/^FINDING_ID_RE = re\.compile\(/{print $2; exit}' "$AUDIT_SCRIPT" \
  | sed 's/)$//; s/"$//; s/(?:/(/g; s/\\Z/$/')
if [ -z "$ID_PATTERN" ]; then
  echo "ERROR: could not read FINDING_ID_RE from ${AUDIT_SCRIPT}." >&2
  exit 1
fi

# Any line quoting a finding-id-shaped character class must quote this exact
# pattern. Anchored on `[a-z0-9._-]`, which is specific enough not to collide
# with the unrelated slug rules elsewhere in the docs.
WRONG_ID=$(search '\[a-z0-9\._-\]' | grep -vF "$ID_PATTERN" || true)
if [ -n "$WRONG_ID" ]; then
  echo "::error::Documented finding-id pattern does not match FINDING_ID_RE in ${AUDIT_SCRIPT} (expected ${ID_PATTERN})."
  printf '%s\n\n' "$WRONG_ID" | sed 's/^/    /'
  FAILED=1
fi

# A guard that passes because every copy disappeared is not a passing guard.
ID_COPIES=$(search '\[a-z0-9\._-\]' | grep -cF "$ID_PATTERN" || true)
if [ "${ID_COPIES:-0}" -lt 1 ]; then
  echo "::error::No document quotes the finding-id pattern any more; either restore it or drop this guard."
  FAILED=1
fi

# --- Result ---------------------------------------------------------------
if [ "$FAILED" -eq 0 ]; then
  echo "Terminology check passed."
else
  echo "Terminology check failed. Fix the identifiers above, or update this guard"
  echo "if the source of truth itself has changed."
fi

exit "$FAILED"
