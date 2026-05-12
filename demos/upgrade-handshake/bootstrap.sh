#!/usr/bin/env bash
# Bootstrap the upgrade-handshake demo project.
# Idempotent: rerunning re-renders MEMORY.md and opening-prompt.md
# from the current env vars and re-symlinks templates.

set -euo pipefail

REQUIRED=(GKE_PROJECT GKE_LOCATION GKE_CLUSTER GKE_NAMESPACES_IN_SCOPE PROD_NAMESPACE PRIMARY_WORKLOAD GKE_TARGET_VERSION)
missing=()
for v in "${REQUIRED[@]}"; do
  if [ -z "${!v:-}" ]; then
    missing+=("$v")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
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

DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/templates"

echo "[bootstrap] demo dir:   $DEMO_DIR"
echo "[bootstrap] repo root:  $REPO_ROOT"

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

# 2. Symlink the templates this demo uses
mkdir -p "$DEMO_DIR/.scion/templates"
DEMO_TEMPLATES=(platform-coordinator upgrade-coordinator dev-workload-guardian node-pool-provisioner workload-deployer)
for t in "${DEMO_TEMPLATES[@]}"; do
  link="$DEMO_DIR/.scion/templates/$t"
  target="../../../templates/$t"
  if [ ! -d "$TEMPLATES_DIR/$t" ]; then
    echo "Error: template '$t' missing at $TEMPLATES_DIR/$t" >&2
    exit 1
  fi
  rm -rf "$link"
  ln -s "$target" "$link"
  echo "[bootstrap] linked template: $t"
done

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
