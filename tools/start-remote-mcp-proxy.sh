#!/usr/bin/env bash
# Start the GKE remote-MCP token-refreshing proxies, one per blast-radius
# scope actually used by an agent. Each proxy fetches a fresh GCP access
# token per request and forwards to https://container.googleapis.com/mcp
# (or /mcp/read-only).
#
# Containers reach these via:
#   http://host.docker.internal:8081/mcp   (full read-write)
#   http://host.docker.internal:8082/mcp   (read-only)
#
# The /mcp/delete-tools endpoint is supported by proxy.py but no agent
# template currently consumes it, so it isn't started here. Add a third
# instance (e.g., on :8083) if a future role needs it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_DIR="$ROOT/remote-mcp-proxy"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found on PATH." >&2
  exit 1
fi

if ! python3 -c 'import aiohttp' >/dev/null 2>&1; then
  echo "Installing Python deps in a venv..."
  python3 -m venv "$PROXY_DIR/.venv"
  "$PROXY_DIR/.venv/bin/pip" install --quiet -r "$PROXY_DIR/requirements.txt"
  PY="$PROXY_DIR/.venv/bin/python"
else
  PY="python3"
fi

if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "Warning: ADC not available. Run 'gcloud auth application-default login' first." >&2
fi

PIDS=()
trap 'echo "stopping proxies..."; kill "${PIDS[@]}" 2>/dev/null || true' EXIT INT TERM

echo "[remote-mcp-proxy] starting full     on :8081 -> /mcp"
"$PY" "$PROXY_DIR/proxy.py" --port 8081 --upstream-path /mcp &
PIDS+=($!)

echo "[remote-mcp-proxy] starting readonly on :8082 -> /mcp/read-only"
"$PY" "$PROXY_DIR/proxy.py" --port 8082 --upstream-path /mcp/read-only &
PIDS+=($!)

echo "[remote-mcp-proxy] all running. Ctrl-C to stop."
wait
