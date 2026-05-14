#!/usr/bin/env bash
# Bootstrap the upgrade-handshake demo project.
# Idempotent: rerunning re-renders MEMORY.md and opening-prompt.md
# from the current env vars and re-symlinks templates.
#
# Flags:
#   --cleanup   Delete previously-bootstrapped state before running:
#               stop+delete the coordinator agent (in this grove and
#               the kube-agents grove if accidentally created there),
#               remove .scion/, the rendered MEMORY.md and opening-
#               prompt.rendered.md, and any stray .scion/ at the repo
#               root. Then continues with the normal bootstrap (which
#               will exit on missing env vars). Run with --cleanup
#               alone (no env vars set) to do cleanup-only.

set -euo pipefail

usage() {
  cat <<'EOF'
Bootstrap the upgrade-handshake demo project.

Usage: ./bootstrap.sh [--cleanup] [--help]

Flags:
  --cleanup   Delete previously-bootstrapped state before running.
              Stops + deletes the coordinator agent (in this grove
              and the kube-agents grove if it was accidentally created
              there); removes .scion/, the rendered MEMORY.md and
              opening-prompt.rendered.md, and any stray .scion/ at the
              repo root; removes orphan docker containers from prior
              runs. Then continues with the normal bootstrap. If env
              vars are not set, --cleanup runs cleanup and exits
              cleanly (cleanup-only mode).
  -h, --help  Show this help.

Required env vars (for full bootstrap, not for --cleanup-only):
  GKE_PROJECT, GKE_LOCATION, GKE_CLUSTER, GKE_NAMESPACES_IN_SCOPE,
  PROD_NAMESPACE, PRIMARY_WORKLOAD, GKE_TARGET_VERSION
EOF
}

CLEANUP=false
for arg in "$@"; do
  case "$arg" in
    --cleanup) CLEANUP=true ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $arg (use --help)" >&2
      exit 1
      ;;
  esac
done

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/templates"

echo "[bootstrap] demo dir:   $DEMO_DIR"
echo "[bootstrap] repo root:  $REPO_ROOT"

# --- Optional cleanup of previous bootstrap state ----------------------
if [ "$CLEANUP" = "true" ]; then
  echo "[bootstrap] --cleanup requested; delegating to ./cleanup.sh"
  "$DEMO_DIR/cleanup.sh"
fi

# --- Required env vars (skipped only by cleanup-only invocation that
#     followed the early exit; if we reach here with cleanup-only we
#     simply exit clean). ----------------------------------------------
REQUIRED=(GKE_PROJECT GKE_LOCATION GKE_CLUSTER GKE_NAMESPACES_IN_SCOPE PROD_NAMESPACE PRIMARY_WORKLOAD GKE_TARGET_VERSION)
missing=()
for v in "${REQUIRED[@]}"; do
  if [ -z "${!v:-}" ]; then
    missing+=("$v")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  if [ "$CLEANUP" = "true" ]; then
    echo "[bootstrap] env vars not set; cleanup-only mode, exiting cleanly."
    exit 0
  fi
  cat >&2 <<EOF
Error: required environment variables not set: ${missing[*]}

Set them before running, e.g.:
  export GKE_PROJECT=my-sandbox-project
  export GKE_LOCATION=us-central1
  export GKE_CLUSTER=mercury-01
  export GKE_NAMESPACES_IN_SCOPE=prod-checkout
  export PROD_NAMESPACE=prod-checkout
  export PRIMARY_WORKLOAD=payment-api
  export GKE_TARGET_VERSION=1.29.x
  ./bootstrap.sh
EOF
  exit 1
fi

# Safety check: a stray .scion/ at the repo root will be picked up by
# Scion's project resolution ahead of the demo's own .scion/, so any
# `scion init` run accidentally from the repo root needs to be cleaned
# up before we proceed (otherwise templates land in the wrong place).
if [ -d "$REPO_ROOT/.scion" ]; then
  cat >&2 <<EOF
Error: a stray .scion/ directory exists at the repo root:
  $REPO_ROOT/.scion

This was likely created by an accidental \`scion init\` run from the repo
root (not from the demo project dir). Scion's project resolution will
pick this one up before the demo's own .scion/ at:
  $DEMO_DIR/.scion

Remove it and re-run bootstrap:
  rm -rf "$REPO_ROOT/.scion"
  $DEMO_DIR/bootstrap.sh
EOF
  exit 1
fi

# 1. scion init (if not already)
if [ ! -d "$DEMO_DIR/.scion" ]; then
  if ! command -v scion >/dev/null 2>&1; then
    echo "Error: 'scion' CLI not found on PATH. Install Scion first." >&2
    exit 1
  fi
  echo "[bootstrap] running 'scion init'..."
  ( cd "$DEMO_DIR" && scion init )
else
  echo "[bootstrap] .scion/ already present, skipping init"
fi

# 2. Import role templates from the top-level templates/ directory.
#
# Two Scion-side quirks to work around:
#   (a) The import discovery walker uses os.ReadDir + e.IsDir(), which
#       returns false for symlinks pointing to directories. We therefore
#       can't point it at .scion/templates/ if its contents are symlinks.
#   (b) The copyDir during import uses filepath.WalkDir (also no symlink
#       follow), but the file copy uses os.Open (which DOES follow), so
#       each per-template skills/<name> symlink-to-dir crashes the copy
#       with "copy_file_range: is a directory".
#
# Fix: materialize a symlink-free staging copy of templates/ via cp -aL,
# then import from the staging dir. The staging dir is removed on exit.
DEMO_TEMPLATES=(platform-coordinator upgrade-coordinator dev-workload-guardian node-pool-provisioner workload-deployer)
for t in "${DEMO_TEMPLATES[@]}"; do
  if [ ! -f "$TEMPLATES_DIR/$t/scion-agent.yaml" ]; then
    echo "Error: template '$t' missing scion-agent.yaml at $TEMPLATES_DIR/$t/" >&2
    exit 1
  fi
done

STAGE_DIR="$(mktemp -d -t kube-agents-import.XXXXXX)"
trap 'rm -rf "$STAGE_DIR"' EXIT
echo "[bootstrap] staging templates (deref symlinks) at $STAGE_DIR"
cp -aL "$TEMPLATES_DIR/." "$STAGE_DIR/"

echo "[bootstrap] importing templates..."
( cd "$DEMO_DIR" && scion templates import --all --force "$STAGE_DIR" )

# Sanity check: confirm all expected templates are now registered.
if scion templates list >/tmp/scion-tpl-list.$$ 2>&1; then
  for t in "${DEMO_TEMPLATES[@]}"; do
    if ! grep -q -E "(^|[[:space:]])${t}([[:space:]]|$)" /tmp/scion-tpl-list.$$; then
      echo "[bootstrap] WARN template '$t' not visible in 'scion templates list' after import" >&2
    fi
  done
  rm -f /tmp/scion-tpl-list.$$
fi

# 3. Render workspace-seed -> ./MEMORY.md and opening-prompt.md
# Pure bash to avoid the envsubst dependency.
render() {
  local src="$1" dst="$2"
  local content
  content="$(cat "$src")"
  for v in "${REQUIRED[@]}"; do
    local val="${!v}"
    content="${content//\$\{${v}\}/${val}}"
  done
  printf '%s\n' "$content" > "$dst"
  echo "[bootstrap] rendered $(basename "$dst")"
}

render "$DEMO_DIR/workspace-seed/MEMORY.md" "$DEMO_DIR/MEMORY.md"
render "$DEMO_DIR/opening-prompt.md.template" "$DEMO_DIR/opening-prompt.rendered.md"

# 4. Best-effort host-side service checks
check_url() {
  local url="$1" name="$2"
  if curl -fsS -o /dev/null -m 2 "$url" 2>/dev/null; then
    echo "[bootstrap] OK    $name reachable at $url"
  else
    echo "[bootstrap] WARN  $name not reachable at $url — start ../../tools/ scripts before running the demo"
  fi
}

check_url "http://localhost:9080/" "local gke-mcp"
check_url "http://localhost:8081/" "remote-mcp-proxy (full)"
check_url "http://localhost:8082/" "remote-mcp-proxy (read-only)"

# 5. Print run instructions
cat <<EOF

[bootstrap] done.

To run the demo:

  cd "$DEMO_DIR"
  scion start coordinator --type platform-coordinator \\
    "\$(cat opening-prompt.rendered.md)" --attach

Detach with Ctrl-P Ctrl-Q. Reattach with: scion attach coordinator
Inspect other agents with: scion list

EOF
