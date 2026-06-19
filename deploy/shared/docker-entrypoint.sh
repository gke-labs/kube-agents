#!/bin/sh
set -e

TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
INSTALL_DIR="/opt/hermes"

# Ensure target directory exists
mkdir -p "$TARGET_DIR"

# 1. Symlink Migration (/opt/data/.hermes -> /opt/data)
if [ -d "$TARGET_DIR" ]; then
    if [ -L "$TARGET_DIR/.hermes" ]; then
        if [ "$(readlink "$TARGET_DIR/.hermes")" != "$TARGET_DIR" ]; then
            rm -f "$TARGET_DIR/.hermes"
            ln -s "$TARGET_DIR" "$TARGET_DIR/.hermes"
        fi
    else
        if [ -d "$TARGET_DIR/.hermes" ]; then
            if cp -rp "$TARGET_DIR/.hermes/." "$TARGET_DIR/" 2>/dev/null; then
                rm -rf "$TARGET_DIR/.hermes"
            else
                echo "Warning: Failed to migrate data from .hermes..." >&2
            fi
        fi
        ln -s "$TARGET_DIR" "$TARGET_DIR/.hermes"
    fi
fi

# 2. Blueprint Bootstrapping (SOUL.md, AGENTS.md, cron/)
if [ ! -f "$TARGET_DIR/SOUL.md" ] && [ -d "/opt/defaults" ]; then
    echo "Initializing $TARGET_DIR with defaults from /opt/defaults..."
    cp -rp /opt/defaults/. "$TARGET_DIR"/
fi

# 3. Dynamic Plugin Syncing
if [ -d "/opt/defaults/plugins" ]; then
    mkdir -p "$TARGET_DIR/plugins"
    cp -ru /opt/defaults/plugins/. "$TARGET_DIR/plugins/" 2>/dev/null || cp -rp /opt/defaults/plugins/. "$TARGET_DIR/plugins/"
fi

# 4. Enable OTel Plugin in config.yaml (only if read-write)
if [ -f "$TARGET_DIR/config.yaml" ] && [ -w "$TARGET_DIR/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; enabled = c.setdefault('plugins', {}).setdefault('enabled', []); 'hermes_otel' not in enabled and enabled.append('hermes_otel'); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/config.yaml" 2>/dev/null || true
fi

# 5. Inject Dynamic OTEL_SERVICE_NAME (only if read-write)
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true
fi

# Launch PID 1 daemon (hermes gateway run)
exec "$@"
