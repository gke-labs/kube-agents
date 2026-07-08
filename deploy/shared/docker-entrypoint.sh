#!/bin/sh
set -e

export TARGET_DIR="${OPENCLAW_HOME:-/opt/data}"
export INSTALL_DIR="/opt/openclaw"

# Pre-export AGENT_BROWSER_EXECUTABLE_PATH before running stage2-hook.sh.
# Why: stage2-hook.sh scans for Playwright's Chromium binary and
# attempts to export it to s6-overlay by creating /run/s6/container_environment/.
# In unprivileged Kubernetes Pods (RunAsNonRoot: true), /run is read-only or
# root-owned, so stage2-hook.sh crashes on `mkdir -p /run/s6/` with Permission denied.
# By pre-exporting AGENT_BROWSER_EXECUTABLE_PATH here, stage2-hook.sh detects
# [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] is false and cleanly skips writing to /run/s6/.
if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/openclaw/.playwright" ]; then
    export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/openclaw/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
fi

# 1. Execute container initialization
if [ -f "/opt/openclaw/docker/stage2-hook.sh" ]; then
    /opt/openclaw/docker/stage2-hook.sh
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 3. Sync OpenClaw-specific assets (like sidecars config)
if [ -d "/openclaw" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /openclaw/. "$TARGET_DIR/" 2>/dev/null || cp -rp /openclaw/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 4. Execute primary process
exec "$@"
