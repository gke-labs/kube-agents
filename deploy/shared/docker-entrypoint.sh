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

# 2b. Force-sync the shared scripts, for the reason step 2a gives for the default
# profile's files: they are image-owned, never runtime state, and `cp -ru` above can skip
# them. It skips whenever the destination looks newer, which covers both a rollback to an
# older image and any build that stamps deterministic file timestamps — in the second case
# a new script never lands at all. The runtime paths that scaffold a cluster profile run
# from here (cluster_agent_profile.py and what it imports), and a stale copy of those
# silently drops the overlay merge and the plugin links for every cluster onboarded after
# the pod started. Extra files already on the PVC are left alone.
#
# Reported, not swallowed, for the reason step 2.7 gives: a silent no-op here IS the bug
# this step exists to prevent, and it surfaces far away — as a cluster agent that quietly
# runs untuned, or without the plugin it was given.
if [ -d "/opt/defaults/scripts" ]; then
    mkdir -p "$TARGET_DIR/scripts"
    cp -rf /opt/defaults/scripts/. "$TARGET_DIR/scripts/" \
        || echo "WARN: could not refresh $TARGET_DIR/scripts from the image; runtime profile scaffolding may run stale code" >&2
fi

# 2.5 Scaffold the Platform Agent specialist profile (idempotent).
# The `default` profile is the front-door Chat Agent (synced above). Today's
# Platform Agent runs as a separate named `platform` profile so the Chat Agent
# can route to it. Its persona/config/skills are baked at /opt/platform-template;
# executable scripts stay in the shared $TARGET_DIR/scripts and are not overlaid.
#
# Gated on profile.yaml — written by `hermes profile create`, shipped by no template —
# rather than on the directory. A directory is not evidence of a scaffold: the kubelet
# creates a mounted volume's mount point before this script runs, so anything mounted
# under profiles/<name>/ brings the directory into being on the PVC first. Targeted
# plugins are mounted outside $HERMES_HOME for exactly that reason (step 2.65), and this
# gate is the belt to that pair of braces: on a PVC already carrying such a directory,
# the scaffold now still runs instead of being skipped forever.
PLATFORM_TEMPLATE="/opt/platform-template"
# The image's own copy of the scaffolder, never the volume's. Step 2 seeds
# $TARGET_DIR/scripts with `cp -u`, which SKIPS any file the PVC holds a newer
# mtime for — the same trap step 2a exists to work around for config.yaml. This
# is the one script in the pod whose job is to make the volume track the image,
# so it is the one script that must not be read back off the volume: last
# release's scaffolder running this release's template is how a partial upgrade
# looks like a successful one. (Step 2b force-syncs the rest of the scripts for
# the same reason; this one cannot wait for that to have worked.)
SCAFFOLD="/opt/defaults/scripts/profile_scaffold.py"
if [ -d "$PLATFORM_TEMPLATE" ] && [ ! -f "$TARGET_DIR/profiles/platform/profile.yaml" ] && [ -f "$SCAFFOLD" ]; then
    PLATFORM_DESC="Platform Agent: fleet-wide GKE architecture, cluster lifecycle/provisioning, multi-tenancy, and the GitOps write path (Pull Requests). Owns per-cluster agent lifecycle."
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --plugins /opt/defaults/plugins \
        --description "$PLATFORM_DESC" || echo "WARN: platform profile scaffold failed; continuing" >&2
fi
# Point the platform profile's home-relative `scripts/` at the shared scripts dir
# (executable scripts are shared across profiles, not copied per-profile). Self-heal
# on every start. Cluster agents use absolute /opt/data/scripts paths and need no link.
# Requires evidence that the directory is a profile at all — profile.yaml from `hermes
# profile create`, or a config.yaml from a profile built before that marker existed.
# Putting a symlink inside a bare mount point would leave content that the skeleton
# cleanup then refuses to remove, wedging the scaffold; gating on the marker ALONE would
# instead strip a legacy profile of its scripts link, which nothing else restores.
if { [ -f "$TARGET_DIR/profiles/platform/profile.yaml" ] || [ -f "$TARGET_DIR/profiles/platform/config.yaml" ]; } \
    && [ -d "$TARGET_DIR/scripts" ]; then
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
#
# The sync goes through profile_scaffold.py --items rather than a `cp -f` loop
# because the list is no longer files-only: cron/, skills/, and governance/ carry
# the machinery CAPABILITIES.md advertises. `[ -f ]` is false for a directory, so
# naming them in a shell loop would be a silent no-op — an upgraded install would
# take the new CAPABILITIES.md and none of what it describes. --items copies each
# entry with copytree(dirs_exist_ok=True), which handles both. The profile already
# exists here, so the scaffold's `hermes profile create` is a no-op and only the
# overlay runs; --plugins is deliberately omitted (step 2.5 owns that).
#
# cron/jobs.json is the one entry that is merged rather than replaced, inside
# profile_scaffold.py. It is image-owned and runtime state in the same file: the
# schedules, prompts and `enabled` flags ship in the image, but the scheduler
# writes each job's run history back into it and the operator can add jobs of
# its own. Copying it wholesale erased both on every pod restart, which let a
# daily audit fire a second time the same morning. The merge is per key — the
# image wins every key it ships, the volume keeps every key it does not — so
# flipping `enabled` to false in the image still disables a watchdog.
#
# Known limit: the overlay adds and overwrites, it never prunes. A skill or SOP
# dropped from the image stays on the PVC until an operator removes it by hand.
# That is the deliberate trade — this path must not start silently deleting from
# a user's volume — not an oversight.
# Gated on profile.yaml, not on the directory: a bare mount point is not a profile, and
# dressing one in a persona and a config makes it indistinguishable from a real profile at
# the next start — which is how a half-built profile used to become permanent.
if [ -f "$TARGET_DIR/profiles/platform/profile.yaml" ] && [ -d "$PLATFORM_TEMPLATE" ] && [ -f "$SCAFFOLD" ]; then
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --items "config.yaml SOUL.md AGENTS.md CAPABILITIES.md cron skills governance" \
        >/dev/null || echo "WARN: platform profile force-sync failed; continuing" >&2
fi
CLUSTER_TEMPLATE="/opt/cluster-template"
if [ -d "$CLUSTER_TEMPLATE" ]; then
    for d in "$TARGET_DIR"/profiles/cluster-*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
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

# 2.65 Link profile-targeted plugin image volumes into their profile homes.
#
# The operator mounts a plugin with spec.targetProfile at /opt/agent-plugins/<profile>/<plugin>,
# outside $HERMES_HOME, and this links it to profiles/<profile>/plugins/<plugin> where Hermes
# resolves a profile's plugins from. Mounting it there directly is what the kubelet cannot be
# allowed to do: it creates the mount point before this script runs, which brings
# profiles/<profile> into existence on the PVC ahead of the scaffold and permanently convinces
# every "is this profile built?" check that it is. The whole failure mode is written up in
# deploy/shared/profile_plugins.py.
#
# Runs after 2.5/2.6 so the profile home exists. Cluster profiles scaffolded later, at runtime,
# are linked by cluster_agent_profile.create_profile instead.
#
# Prefer the IMAGE copy of the script over the PVC copy, for the reason step 2.7 documents.
PLUGIN_LINK_SCRIPT="/opt/defaults/scripts/profile_plugins.py"
[ -f "$PLUGIN_LINK_SCRIPT" ] || PLUGIN_LINK_SCRIPT="$TARGET_DIR/scripts/profile_plugins.py"
if [ -f "$PLUGIN_LINK_SCRIPT" ]; then
    # --mount-root is deliberately not passed: the path is the script's own default, and
    # the operator's pluginProfileMountRoot is the other end of it. A third copy here
    # would be the one that silently keeps pointing at the old location.
    "$INSTALL_DIR/.venv/bin/python3" "$PLUGIN_LINK_SCRIPT" --hermes-home "$TARGET_DIR" \
        || echo "WARN: linking targeted plugin volumes failed; plugins targeting a named profile will not load" >&2
fi

# 2.7 Merge operator-rendered per-profile config overlays.
#
# An AgentPlugin with spec.targetProfile is linked into profiles/<name>/plugins/<plugin>,
# but a mounted plugin is inert until it is listed in that profile's plugins.enabled:
# Hermes only calls register(ctx) — and therefore ctx.register_skill() — for enabled
# plugins. The operator cannot write the profile's config.yaml directly (step 2.6
# force-syncs it from the image, and the operator has no copy of the image-built merge
# to reproduce), so it emits an overlay per profile and this step merges it in.
#
# ORDERING IS LOAD-BEARING: this must run AFTER step 2.6, or the force-sync overwrites
# the merge and every targeted plugin silently goes missing again.
#
# The merge itself lives in profile_overlay.py so it can be unit tested, and because it
# is more than a merge: it records what it applied so a withdrawn overlay can be undone.
# Cluster profiles are NOT force-synced (their config.yaml carries the cluster_identity
# stamp), so without that, removing tuning from the CR would leave every cluster agent
# running the old limits forever.
#
# Failures are reported, not swallowed: a silent no-op here reproduces exactly the bug
# this step exists to prevent, and the symptom surfaces far away — as "Unknown skill(s)"
# in a worker, or as an agent that improvises without the skill it was told to use.
OVERLAY_DIR="/opt/agent-config"
# Prefer the IMAGE copy over the PVC copy. Step 2 syncs /opt/defaults with `cp -ru`,
# which skips a destination that looks newer — the same trap step 2a documents for
# config.yaml — so a PVC copy can outlive the image it came from. This script decides
# what every profile's config ends up containing, so it must track the image.
OVERLAY_SCRIPT="/opt/defaults/scripts/profile_overlay.py"
[ -f "$OVERLAY_SCRIPT" ] || OVERLAY_SCRIPT="$TARGET_DIR/scripts/profile_overlay.py"

if [ -f "$OVERLAY_SCRIPT" ]; then
    # Every profile directory is reconciled — including ones with no overlay, so a
    # withdrawn overlay is undone rather than left applied. Which files apply to a given
    # profile is resolved by name inside the script (profile_overlay.overlays_for): a
    # `cluster-*` profile takes the cluster class overlay AND its own profile-<name> one,
    # if a plugin targets that specific cluster. Matching only the class overlay here is
    # what left such a plugin mounted but never enabled.
    for d in "$TARGET_DIR"/profiles/*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
        name=$(basename "$d")
        "$INSTALL_DIR/.venv/bin/python3" "$OVERLAY_SCRIPT" --profile-dir "$d" --overlay-dir "$OVERLAY_DIR" \
            || echo "WARN: overlay sync failed for profile '$name'; settings it carries will not apply" >&2
    done

    # Warn when an overlay names a profile that does not exist. The operator cannot
    # validate spec.targetProfile — profiles are scaffolded here at startup, not by the
    # operator — so this is the only place a typo becomes visible. A `cluster-*` name is
    # reported differently: those profiles appear when their cluster is onboarded, and
    # cluster_agent_profile.create_profile applies the overlay then, so a missing one is
    # ordinary rather than a mistake.
    for overlay in "$OVERLAY_DIR"/profile-*.overlay.yaml; do
        [ -f "$overlay" ] || continue
        base=$(basename "$overlay"); name=${base#profile-}; name=${name%.overlay.yaml}
        [ -d "$TARGET_DIR/profiles/$name" ] && continue
        case "$name" in
            cluster-*) echo "NOTE: overlay $base names cluster profile '$name', which is not scaffolded yet; it applies when that cluster is onboarded" >&2 ;;
            *)         echo "WARN: overlay $base names profile '$name', which does not exist; plugins targeting it will not load" >&2 ;;
        esac
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
