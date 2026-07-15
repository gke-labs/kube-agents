#!/bin/sh
set -e

export TARGET_DIR="${OPENCLAW_HOME:-${PLATFORM_AGENT_HOME:-/opt/data}}"
if [ -n "${PLATFORM_AGENT_HOME:-}" ] || [ -d "/opt/hermes" ]; then
    export HERMES_HOME="$TARGET_DIR"
fi

# Determine active install directory and check for browser binaries
if [ -d "/opt/openclaw" ]; then
    export INSTALL_DIR="/opt/openclaw"
    if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/openclaw/.playwright" ]; then
        export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/openclaw/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
    fi
elif [ -d "/opt/hermes" ]; then
    export INSTALL_DIR="/opt/hermes"
    if [ -z "$AGENT_BROWSER_EXECUTABLE_PATH" ] && [ -d "/opt/hermes/.playwright" ]; then
        export AGENT_BROWSER_EXECUTABLE_PATH="$(find /opt/hermes/.playwright -type f -executable \( -name 'chrome' -o -name 'chromium' -o -name 'chrome-headless-shell' -o -name 'headless_shell' -o -name 'chromium-browser' \) 2>/dev/null | head -n 1)"
    fi
fi

# 1. Execute container initialization
if [ -f "/opt/openclaw/docker/stage2-hook.sh" ]; then
    /opt/openclaw/docker/stage2-hook.sh
elif [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    /opt/hermes/docker/stage2-hook.sh
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 3. Sync OpenClaw-specific assets (like sidecars config) when OpenClaw mode
if [ -d "/openclaw" ] && [ -f "$TARGET_DIR/openclaw.json" -o -n "${OPENCLAW_HOME:-}" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /openclaw/. "$TARGET_DIR/" 2>/dev/null || cp -rp /openclaw/. "$TARGET_DIR/" 2>/dev/null || true
fi
if [ -f "$TARGET_DIR/openclaw.json" -o -n "${OPENCLAW_HOME:-}" ]; then
    mkdir -p "$TARGET_DIR/workspace"
    if [ ! -f "$TARGET_DIR/workspace/MEMORY.md" ]; then
        echo "# Memory" > "$TARGET_DIR/workspace/MEMORY.md"
    fi
fi

# 4. Enable OpenTelemetry plugin in active config.yaml when Hermes mode (if writable)
if [ -f "$TARGET_DIR/config.yaml" ] && [ -w "$TARGET_DIR/config.yaml" ] && [ -d "/opt/hermes/.venv" ]; then
    "/opt/hermes/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; enabled = c.setdefault('plugins', {}).setdefault('enabled', []); 'hermes_otel' not in enabled and enabled.append('hermes_otel'); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/config.yaml" 2>/dev/null || true
fi

# 5. Inject dynamic OpenTelemetry service name when Hermes mode (if writable)
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -d "/opt/hermes/.venv" ]; then
    "/opt/hermes/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true
fi

# 6. Execute primary process
exec "$@"
