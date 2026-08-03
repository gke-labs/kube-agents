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

# 2a. Force-sync the image-managed default-profile files so they ALWAYS track the
# image, not the persistent PVC. The update-only copy above (cp -u) can skip
# config.yaml: step 3 below rewrites config.yaml on every start (to enable otel),
# bumping its mtime, so on the next image roll cp -u sees the PVC copy as "newer"
# and never overwrites it — leaving a stale toolset/persona config live. These
# files are image-owned (not runtime state), so overwrite them unconditionally.
if [ -d "/opt/defaults" ]; then
    for f in config.yaml SOUL.md AGENTS.md CAPABILITIES.md; do
        [ -f "/opt/defaults/$f" ] && cp -f "/opt/defaults/$f" "$TARGET_DIR/$f" 2>/dev/null || true
    done
fi

# 2.5 Scaffold the Platform Agent specialist profile (idempotent).
# The `default` profile is the front-door Chat Agent (synced above). Today's
# Platform Agent runs as a separate named `platform` profile so the Chat Agent
# can route to it. Its persona/config/skills are baked at /opt/platform-template;
# executable scripts stay in the shared $TARGET_DIR/scripts and are not overlaid.
PLATFORM_TEMPLATE="/opt/platform-template"
if [ -d "$PLATFORM_TEMPLATE" ] && [ ! -d "$TARGET_DIR/profiles/platform" ] && [ -f "$TARGET_DIR/scripts/profile_scaffold.py" ]; then
    PLATFORM_DESC="Platform Agent: fleet-wide GKE architecture, cluster lifecycle/provisioning, multi-tenancy, and the GitOps write path (Pull Requests). Owns per-cluster agent lifecycle."
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$TARGET_DIR/scripts/profile_scaffold.py" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --plugins /opt/defaults/plugins \
        --description "$PLATFORM_DESC" || echo "WARN: platform profile scaffold failed; continuing" >&2
fi
# Point the platform profile's home-relative `scripts/` at the shared scripts dir
# (executable scripts are shared across profiles, not copied per-profile). Self-heal
# on every start. Cluster agents use absolute /opt/data/scripts paths and need no link.
if [ -d "$TARGET_DIR/profiles/platform" ] && [ -d "$TARGET_DIR/scripts" ]; then
    ln -sfn "$TARGET_DIR/scripts" "$TARGET_DIR/profiles/platform/scripts" 2>/dev/null || true
fi

# 2.6 Force-sync the image-managed persona and config files of the specialist
# profiles so they ALWAYS track the image, not the persistent PVC — the same
# guarantee step 2a gives the default profile. The scaffold in 2.5 only runs when
# a profile is ABSENT, so without this an existing platform/cluster profile on
# the PVC keeps stale personas after an image roll.
#
# The platform profile also force-syncs config.yaml, the cluster profiles do NOT,
# and that asymmetry is deliberate:
#   - The platform config.yaml is entirely image-owned — built at image build
#     time by merging the shared defaults with the platform overlay. `hermes
#     profile create` emits no config.yaml, and nothing writes to
#     profiles/platform/config.yaml at runtime (step 3's otel injection targets
#     only the default profile; the platform template already enables
#     hermes_otel). Without syncing it, an image that changes the platform's
#     toolsets or plugins has no effect on any existing deployment.
#   - A cluster config.yaml is identity-stamped at scaffold time with that
#     cluster's `cluster_identity` block (project/cluster/location), so it is
#     runtime state. Overwriting it from the template would strip the record
#     cluster_agent_reconcile.py matches a profile to its cluster by, and the
#     reconciler would then scaffold a duplicate profile it can never prune.
#     (KUBECONFIG is not in this file — it is pinned in the profile's .env by
#     cluster_agent_profile.py:_pin_kubeconfig_env.)
#
# Profile identity is NOT at risk either way: `hermes profile create` records the
# name and description in profiles/<name>/profile.yaml, a separate file that no
# template ships, so it is never overwritten here. Per-profile runtime state
# (USER.md, memory/, sessions/) is likewise left untouched.
if [ -d "$TARGET_DIR/profiles/platform" ] && [ -d "$PLATFORM_TEMPLATE" ]; then
    for f in config.yaml SOUL.md AGENTS.md CAPABILITIES.md; do
        [ -f "$PLATFORM_TEMPLATE/$f" ] && cp -f "$PLATFORM_TEMPLATE/$f" "$TARGET_DIR/profiles/platform/$f" 2>/dev/null || true
    done
fi
CLUSTER_TEMPLATE="/opt/cluster-template"
if [ -d "$CLUSTER_TEMPLATE" ]; then
    for d in "$TARGET_DIR"/profiles/cluster-*; do
        [ -d "$d" ] || continue
        for f in SOUL.md AGENTS.md CAPABILITIES.md; do
            [ -f "$CLUSTER_TEMPLATE/$f" ] && cp -f "$CLUSTER_TEMPLATE/$f" "$d/$f" 2>/dev/null || true
        done
        # Targeted self-heal: drop `memory.provider` from cluster configs already
        # on the PVC. The template no longer sets it (multiuser_memory scopes by
        # gateway user identity, which a dispatcher-spawned worker never has), but
        # cluster config.yaml is NOT force-synced above — it is identity-stamped
        # with `cluster_identity`, the record cluster_agent_reconcile.py reads to
        # match a profile to its cluster. (KUBECONFIG is pinned separately, in the
        # profile's .env by cluster_agent_profile.py:_pin_kubeconfig_env.) So
        # remove just this one key and leave everything else, rather than
        # overwriting the file.
        #
        # The rewrite goes through a temp file and os.replace: a torn write here
        # would drop `cluster_identity`, and reconcile then treats the profile as
        # unidentifiable — it scaffolds a duplicate AND stops pruning the orphan.
        # Errors are reported, not swallowed: a silent no-op is the exact failure
        # mode this whole change exists to fix.
        if [ -f "$d/config.yaml" ] && [ -w "$d/config.yaml" ]; then
            "$INSTALL_DIR/.venv/bin/python3" -c "import os, sys, yaml, pathlib; p = pathlib.Path(sys.argv[1]); c = yaml.safe_load(p.read_text()) or {}; m = c.get('memory'); sys.exit(0) if not isinstance(m, dict) or 'provider' not in m else None; m.pop('provider'); t = p.with_name(p.name + '.tmp'); t.write_text(yaml.safe_dump(c)); os.replace(t, p)" "$d/config.yaml" \
                || echo "WARN: failed to strip memory.provider from $d/config.yaml; this cluster agent keeps an inert provider" >&2
        fi
    done
fi

# 2.7 Merge operator-rendered per-profile config overlays.
#
# An AgentPlugin with spec.targetProfile is mounted under
# profiles/<name>/plugins/<plugin>, but a mounted plugin is inert until it is listed in
# that profile's plugins.enabled: Hermes only calls register(ctx) — and therefore
# ctx.register_skill() — for enabled plugins. The operator cannot write the profile's
# config.yaml directly (step 2.6 force-syncs it from the image on every start, and the
# operator has no copy of the image-built merge to reproduce), so it emits an overlay per
# profile and this step merges it in.
#
# ORDERING IS LOAD-BEARING: this must run AFTER step 2.6, or the force-sync overwrites
# the merge and every targeted plugin silently goes missing again.
#
# The merge is a union, matching deploy/docker/merge_configs.py, which is what
# plugins.enabled wants. Failures are reported, not swallowed: a silent no-op here
# reproduces exactly the bug this step exists to prevent, and the symptom surfaces far
# away — as "Unknown skill(s)" in a worker, or as an agent that improvises without the
# skill it was told to use.
OVERLAY_DIR="/opt/agent-config"

# merge_profile_overlay <profile-config-path> <overlay-path> <label>
merge_profile_overlay() {
    _target="$1"; _overlay="$2"; _label="$3"
    if [ ! -w "$_target" ]; then
        echo "WARN: $_target is not writable; skipping overlay $_label" >&2
        return 1
    fi
    if "$INSTALL_DIR/.venv/bin/python3" - "$_target" "$_overlay" <<'PYMERGE'
import os, pathlib, sys, yaml

def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        for k, v in b.items():
            a[k] = merge(a[k], v) if k in a else v
        return a
    if isinstance(a, list) and isinstance(b, list):
        return list(dict.fromkeys(a + b))
    return b

target, overlay = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
base = yaml.safe_load(target.read_text()) or {}
over = yaml.safe_load(overlay.read_text()) or {}
merged = merge(base, over)
# Torn writes here leave a profile with no config at all, so stage and rename.
tmp = target.with_name(target.name + ".tmp")
tmp.write_text(yaml.safe_dump(merged, sort_keys=True))
os.replace(tmp, target)
PYMERGE
    then
        echo "Merged overlay $_label into $_target"
        return 0
    fi
    echo "WARN: failed to merge overlay $_label into $_target; settings it carries will not apply" >&2
    return 1
}

# 2.7a Class overlay for the Cluster Agents. Cluster profiles are scaffolded at runtime,
# one per managed cluster, so the operator cannot name them individually — it emits one
# overlay for the whole class and it is applied to each. Absent any cluster profile this
# is a silent no-op, which is correct: a fleet with no managed clusters has none.
if [ -f "$OVERLAY_DIR/profileclass-cluster.overlay.yaml" ]; then
    for d in "$TARGET_DIR"/profiles/cluster-*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
        merge_profile_overlay "$d/config.yaml" "$OVERLAY_DIR/profileclass-cluster.overlay.yaml" "profileclass-cluster" || true
    done
fi

if [ -d "$OVERLAY_DIR" ]; then
    for overlay in "$OVERLAY_DIR"/profile-*.overlay.yaml; do
        [ -f "$overlay" ] || continue
        base=$(basename "$overlay")
        profile=${base#profile-}
        profile=${profile%.overlay.yaml}
        target="$TARGET_DIR/profiles/$profile/config.yaml"
        if [ ! -f "$target" ]; then
            echo "WARN: overlay $base names profile '$profile', which has no config at $target; anything it carries (plugins, execution limits) will not apply" >&2
            continue
        fi
        merge_profile_overlay "$target" "$overlay" "$base" || true
    done
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

# 5.5. The default kubectl context is NOT established here. `gcloud` in this
# container is the credential-proxy shim, so get-credentials would execute in
# the sidecar and write the sidecar's kubeconfig, not ours — and it is rejected
# outright, because this script runs from a working directory outside
# CREDENTIAL_PROXY_WORKSPACE_ROOT. The sidecar bootstraps its own context from
# CREDENTIAL_PROXY_BOOTSTRAP_COMMAND (see buildCredentialProxyEnv in the
# operator), which runs inside the workspace root before the proxy serves any
# request. The event-watcher does not need a copy either: it reads
# /var/run/event-watcher/watcher.config and falls back to its in-cluster config
# when that file is absent, which it always is.

# 6. Execute primary process
exec "$@"
