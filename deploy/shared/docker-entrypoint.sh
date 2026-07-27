#!/bin/sh
set -e

export TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
export HERMES_HOME="$TARGET_DIR"
export INSTALL_DIR="/opt/hermes"

# Pre-export AGENT_BROWSER_EXECUTABLE_PATH before running stage2-hook.sh.
# Why: Upstream stage2-hook.sh scans for Playwright's Chromium binary and
# attempts to export it to s6-overlay by creating /run/s6/container_environment/.
# In unprivileged Kubernetes Pods (RunAsNonRoot: true), /run is read-only or
# root-owned, so stage2-hook.sh crashes on `mkdir -p /run/s6/` with Permission denied.
# By pre-exporting AGENT_BROWSER_EXECUTABLE_PATH here, stage2-hook.sh detects
# [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] is false and cleanly skips writing to /run/s6/.
if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/hermes/.playwright" ]; then
    export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/hermes/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
fi

# 1. Execute upstream container initialization natively (inherits 100% of upstream updates)
if [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    /opt/hermes/docker/stage2-hook.sh
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 3. Enable OpenTelemetry plugin in active config.yaml (if writable)
if [ -f "$TARGET_DIR/config.yaml" ] && [ -w "$TARGET_DIR/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; enabled = c.setdefault('plugins', {}).setdefault('enabled', []); 'hermes_otel' not in enabled and enabled.append('hermes_otel'); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/config.yaml" 2>/dev/null || true
fi

# 4. Inject dynamic OpenTelemetry service name (if writable)
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true

    # hermes-otel resolves config below ~/.hermes even when HERMES_HOME points
    # elsewhere. Expose the generated config at both locations.
    OTEL_CONFIG="$TARGET_DIR/plugins/hermes_otel/config.yaml"
    OTEL_COMPAT_CONFIG="$HOME/.hermes/plugins/hermes_otel/config.yaml"
    mkdir -p "$(dirname "$OTEL_COMPAT_CONFIG")"
    if [ ! "$OTEL_CONFIG" -ef "$OTEL_COMPAT_CONFIG" ]; then
        ln -sf "$OTEL_CONFIG" "$OTEL_COMPAT_CONFIG"
    fi
fi

# 5. Start background microservices (FastAPI proxy)
mkdir -p "$TARGET_DIR/logs"
if [ -f "$TARGET_DIR/scripts/session_kv_server.py" ]; then
    echo "Starting Session KV server on port 8699..."
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 0.0.0.0 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi

# 5.5. Initialize default GKE context for the container to the host cluster
if [ -n "$GKE_CLUSTER_NAME" ] && [ -n "$GKE_LOCATION" ]; then
    echo "Configuring default kubectl context to host cluster: $GKE_CLUSTER_NAME ($GKE_LOCATION)..."
    gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --location="$GKE_LOCATION" ${GOOGLE_CHAT_PROJECT_ID:+--project="$GOOGLE_CHAT_PROJECT_ID"} >/dev/null 2>&1 || true
    # Backup static context configuration specifically for the event-watcher sidecar
    if [ -f "$HOME/.kube/config" ]; then
        cp "$HOME/.kube/config" "$HOME/.kube/watcher.config.tmp" && mv "$HOME/.kube/watcher.config.tmp" "$HOME/.kube/watcher.config"
    fi
fi


# 6. Execute primary process
exec "$@"
