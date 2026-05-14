#!/usr/bin/env bash
# Reset everything created by bootstrap.sh that does not belong in the repo:
#   - stop+delete the coordinator agent (in this grove and in the
#     kube-agents grove, in case it was accidentally created there)
#   - remove .scion/ in the demo dir
#   - remove rendered MEMORY.md and opening-prompt.rendered.md
#   - remove any stray .scion/ at the repo root (from accidental
#     `scion init` runs from the wrong cwd)
#   - remove orphan docker containers from prior runs
#
# Idempotent and best-effort: failures (Hub down, agent missing, etc.)
# are suppressed.

set -euo pipefail

usage() {
  cat <<'EOF'
Reset bootstrapped state for the upgrade-handshake demo.

Usage: ./cleanup.sh [--help]

Removes:
  - coordinator agent (this grove and the kube-agents grove)
  - <demo>/.scion/
  - <demo>/MEMORY.md
  - <demo>/opening-prompt.rendered.md
  - stray <repo-root>/.scion/
  - orphan docker containers from prior runs
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $arg (use --help)" >&2
      exit 1
      ;;
  esac
done

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

echo "[cleanup] demo dir:   $DEMO_DIR"
echo "[cleanup] repo root:  $REPO_ROOT"
echo "[cleanup] tearing down previous env..."

# Delete agent records (best-effort; failures suppressed since the
# Hub may be down or the agent may not exist). We try both the demo
# grove and the repo-root grove because past invocations have
# accidentally created agents in the kube-agents grove.
( cd "$DEMO_DIR"  && scion delete coordinator --yes >/dev/null 2>&1 ) || true
( cd "$REPO_ROOT" && scion delete coordinator --yes >/dev/null 2>&1 ) || true

# On-disk state in the demo project
rm -rf "$DEMO_DIR/.scion"
rm -f  "$DEMO_DIR/MEMORY.md"
rm -f  "$DEMO_DIR/opening-prompt.rendered.md"

# Stray repo-root .scion (left by accidental scion init from the wrong cwd)
rm -rf "$REPO_ROOT/.scion"

# Orphan docker containers from prior runs (project-name-prefixed)
docker ps -aq --filter "name=upgrade-handshake--" \
              --filter "name=kube-agents--" 2>/dev/null \
  | xargs -r docker rm -f >/dev/null 2>&1 || true

echo "[cleanup] complete"
