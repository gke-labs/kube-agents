#!/bin/sh
set -e

export TARGET_DIR="${PLATFORM_AGENT_HOME:-/opt/data}"
export HERMES_HOME="$TARGET_DIR"
export INSTALL_DIR="/opt/hermes"
export PATH="/usr/local/bin:/command:$PATH"

mkdir -p "$TARGET_DIR"

# Ensure s6-setuidgid works unprivileged when running in non-root K8s containers
if [ ! -x "/usr/local/bin/s6-setuidgid" ]; then
    cat << 'EOF' > /tmp/s6-setuidgid
#!/bin/sh
if [ "$(id -u)" != "0" ]; then
    if [ "$1" = "hermes" ]; then shift; fi
    exec "$@"
else
    exec /command/s6-setuidgid "$@"
fi
EOF
    chmod +x /tmp/s6-setuidgid
    export PATH="/tmp:$PATH"
fi

# Manually trigger s6-overlay stage 2 initialization script (stage2-hook.sh)
if [ -f "/opt/hermes/docker/stage2-hook.sh" ]; then
    echo "Triggering s6-overlay stage 2 initialization (/opt/hermes/docker/stage2-hook.sh)..."
    /opt/hermes/docker/stage2-hook.sh
fi

# Manually trigger any remaining s6 cont-init scripts
if [ -d "/etc/cont-init.d" ]; then
    for script in /etc/cont-init.d/*; do
        if [ -x "$script" ] && [ "$script" != "/etc/cont-init.d/01-hermes-setup" ]; then
            echo "Running cont-init script: $script..."
            "$script" || true
        fi
    done
fi

# Launch PID 1 daemon
exec "$@"
