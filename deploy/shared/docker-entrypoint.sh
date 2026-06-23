#!/bin/sh
set -e

export TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
export HERMES_HOME="$TARGET_DIR"
export INSTALL_DIR="/opt/hermes"

# 1. Execute upstream container initialization natively (inherits 100% of upstream updates)
if [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    /opt/hermes/docker/stage2-hook.sh
fi

# 2. Sync default plugins
if [ -d "/opt/defaults/plugins" ]; then
    mkdir -p "$TARGET_DIR/plugins"
    cp -ru /opt/defaults/plugins/. "$TARGET_DIR/plugins/" 2>/dev/null || cp -rp /opt/defaults/plugins/. "$TARGET_DIR/plugins/" 2>/dev/null || true
fi

# 3. Enable OpenTelemetry plugin in active config.yaml (if writable)
if [ -f "$TARGET_DIR/config.yaml" ] && [ -w "$TARGET_DIR/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; enabled = c.setdefault('plugins', {}).setdefault('enabled', []); 'hermes_otel' not in enabled and enabled.append('hermes_otel'); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/config.yaml" 2>/dev/null || true
fi

# 4. Inject dynamic OpenTelemetry service name (if writable)
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    "$INSTALL_DIR/.venv/bin/python3" -c "import sys, os, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {} if p.exists() else {}; svc = os.getenv('OTEL_SERVICE_NAME'); attrs = c.setdefault('resource_attributes', {}); attrs.update({'service.name': svc}) if svc else attrs.pop('service.name', None); p.write_text(yaml.safe_dump(c))" "$TARGET_DIR/plugins/hermes_otel/config.yaml" 2>/dev/null || true
fi

# 5. Execute primary process
exec "$@"
