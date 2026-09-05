#!/bin/sh
set -e

# The credential sidecar runs as a different user (see the UID constants in the
# operator's platformagent_manifests.go) and executes the proxied gcloud, git and
# gh commands against this container's workspace on the shared PVC. It reaches
# those files through the shared fsGroup, which only helps if what we create is
# group-writable: a leased GitOps directory this container makes has to be a
# directory the sidecar can clone into, and a profile home it makes has to be one
# the sidecar can write a kubeconfig pin into. The kubelet's fsGroup pass fixes up
# files that already exist at mount time; this fixes up the ones created after.
umask 0002

# Below the umask and not above it, where the rest of this file's constants would
# go: tests/test_startup_umask.py asserts that the umask is the first line here
# that can create a file, and it reads the file rather than running it, so an
# assignment above the umask reads to it as that line.
#
# EXIT_RETRY in deploy/shared/sandbox_mirror.py: the mirror failed in a way the
# next container start fixes on its own. Step 5.7 warns on it and lets the agent
# come up; every other non-zero exit there is still fatal. Change both together.
readonly SANDBOX_MIRROR_RETRY_RC=2

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

# 1.5 Exactly one container per pod runs the setup BELOW this line. The others stop here.
#
# "Below this line" is the whole of the claim. Step 1 is deliberately above it and runs in
# every container, including the sidecars — stage2-hook.sh is upstream's own container-local
# init, and it touches the shared tree too (it chowns $TARGET_DIR and $TARGET_DIR/profiles,
# and lays down the Hermes skeleton: config.yaml, sessions/, skills/, logs/). That is
# unchanged from before this gate existed and is not what corrupts a profile; it is
# idempotent and every container genuinely needs it. Worth knowing all the same, because
# "the sidecar does not write to the PVC" is the obvious reading of this gate and it is
# false. If you are hunting a write nobody claims to make, look above, not below. It also
# means $TARGET_DIR/logs is NOT evidence that this setup ran — use scripts/ or
# profiles/platform/profile.yaml, which only the steps below create.
#
# The Deployment runs this image more than once against ONE data PVC — the gateway and
# the dashboard (`hermes dashboard`) — and they are not equivalent. The operator mounts
# the plugin OCI volumes and the operator-rendered overlay ConfigMap into the gateway
# container ONLY, so the same setup code sees a different world in each, and everything
# below writes to the shared tree.
#
# Left ungated, the dashboard's pass actively undoes the gateway's:
#
#   - Step 2.65 links profiles/<p>/plugins/<plugin> -> /opt/agent-plugins/... . That path
#     does not exist in the dashboard container, so its prune_stale_links() reads the
#     gateway's fresh link as dangling and silently removes it.
#   - Step 2.7 merges /opt/agent-config. That directory does not exist there either, so
#     the merge finds no overlay and reverts the one already applied — it logs
#     "unapplied previous overlay" — dropping the plugin from plugins.enabled.
#
# Both containers race to finish, and the loser's work is erased. The symptom lands far
# away and looks like something else entirely: a worker exits 1 with "Unknown skill(s)",
# the task retries twice, the dispatcher gives up, and the board fills with blocked tasks
# while the AgentPlugin still reports Ready and the image is still correctly mounted.
# Step 5's Session KV server has the same shape of problem — two containers, one pod
# network namespace, one port 8699.
#
# WHO OWNS IT is answered by AGENT_SHARED_STATE_SETUP first and by the command line only
# as a fallback. Under the operator the variable is always set — `owner` on the gateway,
# `skip` on the dashboard (buildBaseContainers in platformagent_manifests.go) — so the
# fallback never runs there. It exists for deployments with no operator to ask: compose,
# plain manifests, `docker run`.
#
# The variable comes first because argv is not reliable evidence. At more than one replica
# the gateway container runs `python3 $HERMES_HOME/leader_elect.py`, which starts
# `hermes gateway run` as a child; the word `gateway` appears nowhere in its own argv, so
# argv detection excludes the one container that must do the setup. It reads as a sidecar
# and is not one.
agent_owns_shared_state() {
    # An unrecognised value falls back to auto-detection rather than guessing, but it says
    # so: `Owner`, `true` and `1` are all plausible things to write, and every one of them
    # would otherwise be indistinguishable from not having set the variable at all. The
    # operator who wrote one believes the override took effect. `auto` is spelled out so
    # that the documented default is not itself reported as a typo; the `:-auto` above has
    # already turned unset and empty into it.
    case "${AGENT_SHARED_STATE_SETUP:-auto}" in
        owner|always) return 0 ;;
        skip|never) return 1 ;;
        auto) ;;
        *)
            echo "[ENTRYPOINT] WARN: ignoring unrecognised AGENT_SHARED_STATE_SETUP='$AGENT_SHARED_STATE_SETUP' (expected owner|always|skip|never|auto); falling back to auto-detection." >&2
            ;;
    esac
    # An empty argv is NOT the image CMD arriving. The ENTRYPOINT is exec-form, so the
    # CMD is passed through as "$@" — `hermes gateway run` reaches here as three
    # arguments, not none. Nothing at all means the caller cleared both the CMD and any
    # `args:`, leaving no process to hand over to: a setup-only invocation. Run the setup
    # and let the tail of the script fall off the end.
    [ "$#" -eq 0 ] && return 0
    # Whole-word, not a substring: `*gateway*` would also match a command that merely
    # mentions one, such as `hermes kanban ls --board gateway-migration`. Matching the
    # argument exactly also survives being invoked by absolute path.
    for arg in "$@"; do
        [ "$arg" = "gateway" ] && return 0
    done
    # Unrecognised means excluded, so a new sidecar is opted out by default rather than
    # having to be remembered. The cost of that default is the leader-election case above,
    # which is why the operator names its owner outright instead of relying on this.
    return 1
}

if ! agent_owns_shared_state "$@"; then
    echo "[ENTRYPOINT] '$*' does not own the shared state; skipping setup ($TARGET_DIR belongs to the container that does)." >&2
    # `exec` with no operands is not an error and does not replace the shell: it applies
    # any redirections and RETURNS. So an empty argv here would fall straight through this
    # branch into the setup it exists to skip, reach the identical no-op `exec` at the
    # bottom, and exit 0 as though it had started something — an explicit `skip` doing the
    # exact opposite of what it was told, and reporting success for it. Reachable only by
    # clearing the CMD by hand, which is also the one case where there is nothing to hand
    # over to, so stop here.
    if [ "$#" -eq 0 ]; then
        echo "[ENTRYPOINT] ...and there is no command to exec; nothing to do." >&2
        exit 0
    fi
    # Starting before the owner has populated a fresh PVC is TOLERATED, not prevented.
    # Nothing orders containers within a pod, so on a brand-new volume `hermes dashboard`
    # can reach its first read while $TARGET_DIR is still empty — no config.yaml at all,
    # no scripts/router_server.py, no plugin links. So wait for the one file whose absence
    # is not survivable, and only for it.
    #
    # BELT AND BRACES, not the load-bearing guarantee — say so plainly, because an
    # earlier revision of this comment claimed otherwise and it was wrong. Upstream's
    # stage2 hook seeds $HERMES_HOME/config.yaml from cli-config.yaml.example before this
    # script gets control, in THIS container as much as in the owner's, so in the shipped
    # image the file is already there and the loop below never runs a single iteration.
    # It is kept for the case that stops being true: nothing in this repo owns stage2's
    # seeding, an upstream that stops doing it would otherwise reintroduce the empty-read
    # silently, and an unexecuted `while` costs nothing to carry.
    #
    # What this REPLACED is a subPath mount. The operator used to project the rendered
    # ConfigMap over $TARGET_DIR/config.yaml in the non-owner container. That guaranteed
    # a file, but a mount is not conditional: it shadowed the PVC copy on every volume,
    # so this container read a DIFFERENT config from the gateway's and narrowing the
    # render narrowed this container's whole world with it. It did not even shadow
    # RELIABLY — the bind lives in this container's mount namespace, and the owner
    # replacing config.yaml atomically from its own namespace destroys it outright (the
    # rename does not even get EBUSY, having no bind of its own at that path), so the
    # sidecar read the render until the owner's first write and the PVC file after it.
    # Both containers now read the same PVC file with the same /etc/hermes managed scope
    # over it, which is the property that actually matters here.
    #
    # Bounded, and it proceeds either way. A non-owner that never comes up is worse than
    # one that comes up early: with no probes on this container, exiting here would only
    # buy a kubelet backoff loop, and the owner may legitimately never run at all in a
    # deployment that mounts a pre-populated volume.
    #
    # Gated on HERMES_MANAGED_DIR being SET, for the same reason the managed-scope
    # assertion below is: it is the one marker of an operator-managed pod. The wait is
    # only ever paid off by a SECOND container in the same pod, and the operator's
    # Deployment is the only thing that creates one. Started any other way — compose, a
    # plain manifest, `docker run`, the kustomize bases, a test harness — a missing
    # config.yaml means nobody is coming to write it, and pausing would turn a fast
    # failure into a two-minute one for no possible gain.
    if [ -n "${HERMES_MANAGED_DIR:-}" ] && [ ! -f "$TARGET_DIR/config.yaml" ]; then
        _wait_secs="${AGENT_SHARED_STATE_WAIT_SECS:-120}"
        # A non-numeric value would make the `-lt` below a shell ERROR, and `set -e` is on
        # — so a typo in a knob for waiting would kill the container outright, which is
        # the one outcome this whole branch is written to avoid. Same treatment as an
        # unrecognised AGENT_SHARED_STATE_SETUP above: say so, use the default.
        case "$_wait_secs" in
            "" | *[!0-9]*)
                echo "[ENTRYPOINT] WARN: ignoring non-numeric AGENT_SHARED_STATE_WAIT_SECS='$_wait_secs'; using 120." >&2
                _wait_secs=120
                ;;
        esac
        echo "[ENTRYPOINT] no $TARGET_DIR/config.yaml yet; waiting up to ${_wait_secs}s for the owner to seed it." >&2
        _waited=0
        while [ ! -f "$TARGET_DIR/config.yaml" ] && [ "$_waited" -lt "$_wait_secs" ]; do
            sleep 1
            _waited=$((_waited + 1))
        done
        if [ -f "$TARGET_DIR/config.yaml" ]; then
            echo "[ENTRYPOINT] $TARGET_DIR/config.yaml appeared after ${_waited}s; continuing." >&2
        else
            echo "[ENTRYPOINT] WARN: $TARGET_DIR/config.yaml still absent after ${_wait_secs}s; starting '$*' anyway (it may fail until the owner runs)." >&2
        fi
        unset _wait_secs _waited
    fi
    #
    # KNOWN LIMIT, deliberately accepted rather than fixed here: the REST of the tree is
    # still a race — the wait above covers config.yaml only, and scripts/router_server.py
    # and the plugin links still arrive whenever the owner gets to them. That ordering is
    # the kubelet's to lose. Moving this setup into an initContainer — one carrying the
    # plugin volumes and the overlay ConfigMap, running to completion, leaving every app
    # container on `skip` — is what would turn it into an ordering the pod spec states
    # instead of one it happens to get. It is only the WRITES below that have to belong to
    # one container; the reads merely have to survive being early.
    #
    # Skipping the SETUP is not skipping the cwd. This branch execs ~600 lines above the
    # `cd "$TARGET_DIR"` at the bottom, so without this the handed-over process keeps
    # whatever directory the container started in — /opt/hermes for the dashboard sidecar,
    # which is read-only to the runtime user, so every relative path it writes is lost.
    # The reasoning for the cd, and why the cwd is the only lever that reaches every
    # caller, is at the bottom.
    # Guarded for the same reason it is there: a non-owner can legitimately start before
    # the owner has created the tree, and that must not abort the container.
    if ! cd "$TARGET_DIR"; then
        echo "WARN: could not enter $TARGET_DIR; relative paths will resolve against a read-only directory" >&2
    fi
    exec "$@"
fi

# The matching half of the skip message above, and the only positive evidence the gate
# leaves. Both branches announce, so "which container built the tree" is answered by the
# logs of the container that did it rather than inferred from the silence of the ones that
# did not — and the decision is readable without inspecting the filesystem it is about to
# change.
#
# That last part is why this line exists rather than being obvious. The tests assert on
# this pair, because a filesystem side effect is only evidence where the setup can actually
# run, and on a developer host it cannot: every step below is guarded on /opt/defaults or
# /opt/hermes. The marker they used to key on, $TARGET_DIR/logs, is worse than merely
# unavailable there — inside the real image step 1 creates it in EVERY container, so it
# reports "the setup ran" in precisely the containers this gate exists to stop.
echo "[ENTRYPOINT] '$*' owns the shared state; building $TARGET_DIR." >&2

# Which CONTAINER of this pod may hold the pod-scoped singletons. This is NOT a
# per-replica election and must not be read as one: every replica is built from
# a single PodTemplateSpec, so no env var can single one of them out, and
# leadership above one replica is a dynamic Lease held by leader_elect.py. The
# operator stamps PLATFORM_AGENT_ROLE `sidecar` on platform-agent-dashboard
# alone — the isDashboardEnabled block in platformagent_manifests.go, whose own
# comment states this same per-container contract — and leaves it unset
# everywhere else, so an image running anywhere else — plain docker, the
# kustomize bases, a cluster profile — is the primary by default and behaves
# exactly as before.
#
# In the operator as it ships this is belt and braces: the one container that
# carries `sidecar` also carries AGENT_SHARED_STATE_SETUP=skip, so the step-1.5
# gate has already exec'd it away above and nothing reaching this line is
# anything but primary. It stays because the two are set independently — an
# older operator, a hand-written pod, or some future container that owns the
# tree without owning the pod's ports would arrive here with the role set and
# no gate to stop it.
#
# What it gates is per-POD by design, not once-per-volume: the session KV server
# (each pod's event-watcher posts to its OWN 127.0.0.1:8699, so every pod must
# run one — across replicas "always primary" is the required answer here, not a
# bug) and the OTel service-name stamp (a container with no OTEL_SERVICE_NAME
# running step 4 DELETED the name the agent had just written — the observed
# `resource_attributes: {}`). The step-2d rebuild is gated here for the
# per-container reason its own comment gives; across replicas it is not skipped
# but serialised by the step-1.6 lock, and it is idempotent — the three-way
# merge carries runtime keys through, so a peer's boot does not undo a live
# `/sethome`.
if [ "$PLATFORM_AGENT_ROLE" = "sidecar" ]; then
    IS_BOOTSTRAP_PRIMARY=0
else
    IS_BOOTSTRAP_PRIMARY=1
fi

# 1.55 Check the image's skill trees against the SHA-256 manifests the build wrote
# beside them, before anything below propagates one of them.
#
# The trees are prompt material: a SKILL.md tells the agent what to do and a
# skills/*/scripts/*.py runs with the agent's credentials. Until now nothing compared
# what boots against what was built, so a tree that had been altered — by a previous
# boot under an older image, by a corrupted layer, by a bad build — was copied to every
# profile and loaded, indistinguishably from a good one.
#
# Here rather than anywhere later because everything that spreads these trees is below:
# step 2 copies /opt/defaults onto the PVC and sync_profile_skills installs
# $PLATFORM_TEMPLATE/skills and $CLUSTER_TEMPLATE/skills into each profile. Failing at
# this line leaves the PVC exactly as the last good boot left it.
#
# It is the DETECTION half of the pair; the barrier is that these trees, and the two
# templates around them, are root-owned in the image while the agent runs as uid 10000
# (the ownership comment in deploy/docker/Dockerfile has the reasoning, including why
# the modes are left writable-looking). Read it as "this image's skills are the ones it
# was built with", not as a runtime sandbox.
#
# The manifest, not the script, decides whether the check is mandatory. Both sides are
# root-owned in the image — the manifest inside the tree it describes, the verifier in
# /opt/defaults/scripts — so neither is something the runtime user removes, and the
# FATAL branch below reads as a corrupted or mis-built image rather than as the last
# thing standing between an agent and a disabled check. It is kept because the
# alternative to reporting a missing checker is skipping the tree it would have checked:
# a tree that carries a manifest is verified or the container does not start. A tree
# with no manifest beside it is one no manifesting build produced (agent-base ships
# /opt/hermes/skills and never reaches the platform stage) and is skipped, which is also
# what makes this a no-op on a developer host.
#
# Fail-closed, which is a deliberate departure from the WARN-and-continue that every
# other step here uses. Those steps degrade to a stale file; this one degrades to
# running instructions nobody can account for, which is the whole thing it exists to
# report. The blast radius is bounded on the other side: the manifest ships in the same
# image layer as the files it covers and the build asserts it is complete, so a mismatch
# is never a version skew between the two — it is the image having changed since it was
# built. There is deliberately no env-var override; a bypass switch would be readable to
# exactly the caller this is meant to be honest with.
SKILL_PROVENANCE_SCRIPT="/opt/defaults/scripts/verify_skills_provenance.py"
for _tree in /opt/hermes/skills /opt/platform-template/skills /opt/cluster-template/skills; do
    [ -f "$_tree/skills_manifest.sha256" ] || continue
    if [ ! -f "$SKILL_PROVENANCE_SCRIPT" ] || [ ! -x "$INSTALL_DIR/.venv/bin/python3" ]; then
        echo "FATAL: $_tree carries a build-time manifest but nothing here can check it ($SKILL_PROVENANCE_SCRIPT or $INSTALL_DIR/.venv/bin/python3 is missing); refusing to start" >&2
        exit 1
    fi
    # The script names the specific difference on stderr; this only says what the
    # container did about it.
    if ! "$INSTALL_DIR/.venv/bin/python3" "$SKILL_PROVENANCE_SCRIPT" \
        --manifest "$_tree/skills_manifest.sha256" --dir "$_tree"; then
        echo "FATAL: $_tree does not match the manifest baked beside it at build time; refusing to start" >&2
        exit 1
    fi
done
unset _tree

# 1.6 Serialise everything below that writes to $TARGET_DIR.
#
# The step-1.5 gate leaves at most one owner per POD, not one owner per VOLUME.
# At more than one replica the operator declares every pod's gateway container
# the owner (AGENT_SHARED_STATE_SETUP=owner — argv cannot identify the
# leader-election wrapper, see above), so each replica performs the identical
# bootstrap, concurrently, on the identical paths of the same shared volume.
# Before the gate existed the dashboard sidecar raced the gateway the same way
# inside a single pod.
#
# Observed damage, not theoretical: both containers abort step 2.5 with
# `shutil.Error` from the plugin copytree, each naming DIFFERENT files (one
# `hermes_otel/website/README.md`, the other `hermes_otel/docker-compose/...`)
# on the same boot — the signature of a write-write race rather than a
# permission problem. Two concurrent overlays of cron/jobs.json can likewise
# interleave a read-merge-write and lose one side's run history.
#
# Nothing downstream was corrupted only because the two partial copies happened
# to union to a complete tree. That is luck: --plugins is passed in step 2.5
# alone, which is gated on first scaffold, so a genuine gap would persist for
# the life of the PVC.
#
# A lock rather than "only the primary bootstraps", deliberately: each owner
# still leaves the volume ready by itself, so there is no start-order dependency
# and no wait-for-peer timeout to tune. The loser of the race runs second and
# finds every step already idempotently satisfied. Steps that are singletons
# rather than shared-state writes (the session KV server, the OTel service-name
# stamp) are guarded by IS_BOOTSTRAP_PRIMARY instead — a lock cannot serialise a
# port bind held for the container's lifetime.
#
# Absent flock, or an unwritable volume, this degrades to today's behaviour
# rather than refusing to start.
BOOTSTRAP_LOCK="$TARGET_DIR/.bootstrap.lock"
BOOTSTRAP_LOCK_FD=""
if command -v flock >/dev/null 2>&1; then
    mkdir -p "$TARGET_DIR" 2>/dev/null || true
    # `touch`, not `: >>"$LOCK"`. A redirection that fails on a POSIX *special*
    # builtin — and `:` is one — exits a non-interactive shell outright, before
    # the `2>/dev/null` on the same line is even installed. Under dash (Debian's
    # /bin/sh) an unwritable $TARGET_DIR then killed the container at this line
    # instead of falling through to the unlocked path. Proving writability with
    # an external command first also makes the `exec` below safe, which would
    # abort the shell the same way.
    if touch "$BOOTSTRAP_LOCK" 2>/dev/null; then
        exec 9>>"$BOOTSTRAP_LOCK"
        BOOTSTRAP_LOCK_FD=9
        # Bounded: a peer wedged mid-bootstrap must not hold this container at
        # the starting line forever. Timing out and proceeding is the current
        # behaviour, so the worst case is no worse than before the lock.
        flock -w 300 9 || echo "WARN: timed out waiting for the $TARGET_DIR bootstrap lock; proceeding concurrently with the peer container" >&2
    fi
fi

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 2a. Force-sync the image-managed default-profile files so they ALWAYS track the
# image, not the persistent PVC. The update-only copy above (cp -u) can skip them,
# and on a long-lived volume it eventually always does, by either of two routes.
# `cp` without -p stamps the destination with the time of the copy, so the moment
# step 2 lands a file the PVC copy is NEWER than the image file it came from, and
# every subsequent boot's cp -u declines to overwrite it; anything that rewrites one
# at runtime bumps its mtime the same way. Both leave a stale persona live across
# the image roll that was supposed to replace it. (Rollback to an older image and
# builds with deterministic file timestamps get there from the other direction; step
# 2b describes that pair for the shared scripts.) These files are image-owned (not
# runtime state), so overwrite them unconditionally.
#
# config.yaml is NOT in this list, and is not in /opt/defaults at all: it is the one
# file here the agent itself writes to (`/sethome`'s home channel, the monitoring
# install id, saved slash-command preferences), so force-copying it discarded all of
# that on every start. Step 2d rebuilds it instead, from the image template, the
# operator's overlay and the runtime's own edits.
#
# hindsight/config.json is in this list because it was NOT, once: the memory
# provider's connection config was hand-written onto the PVC and so survived
# every roll carrying whichever design was current when it was last touched. It
# kept pointing the single-bank provider at a bank name from the two-bank era —
# invisible to any code review or manifest diff. It is image-owned because
# nothing in it is per-install: the bank is a constant in the provider, and the
# service address is not in this file at all — the operator derives it from the
# agent's namespace and passes HINDSIGHT_API_URL, which the plugin reads only
# when the file is silent. That is why no `api_url` key belongs here.
if [ -d "/opt/defaults" ]; then
    for f in SOUL.md AGENTS.md CAPABILITIES.md hindsight/config.json; do
        if [ -f "/opt/defaults/$f" ]; then
            # Nested paths need their parent: step 2's recursive copy creates it
            # on a fresh PVC, but the force-sync must not depend on that.
            mkdir -p "$(dirname "$TARGET_DIR/$f")" 2>/dev/null || true
            cp -f "/opt/defaults/$f" "$TARGET_DIR/$f" 2>/dev/null || true
        fi
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

# 2c. Invalidate locally-hosted MCP server schemas from each profile's on-disk cache.
#
# Why: Hermes caches tool definitions from `mcp_servers` in `<profile>/cache/mcp_schema_cache.json`
# so lazy startup can register tools without spawning subprocesses on every turn.
# However, Hermes keys the cache entry only on `config_fingerprint` (command/args/url),
# which does not change when an image update modifies the contents of a local script
# (e.g. platform_mcp_server.py or router_server.py).
# When an upgraded container image starts over an existing PVC, the stale cached tool list
# shadows newly added or modified tools indefinitely (Issue #854).
# Dropping local server entries forces Hermes to re-discover tools on first connect.
# Remote servers (e.g. developer_knowledge, gke) are preserved to avoid unnecessary
# network round-trips.
INVALIDATE_MCP_SCRIPT="/opt/defaults/scripts/invalidate_mcp_cache.py"
[ -f "$INVALIDATE_MCP_SCRIPT" ] || INVALIDATE_MCP_SCRIPT="$TARGET_DIR/scripts/invalidate_mcp_cache.py"
if [ -f "$INVALIDATE_MCP_SCRIPT" ]; then
    PYTHON="python3"
    [ -x "$INSTALL_DIR/.venv/bin/python3" ] && PYTHON="$INSTALL_DIR/.venv/bin/python3"
    "$PYTHON" "$INVALIDATE_MCP_SCRIPT" "$TARGET_DIR" \
        || echo "WARN: could not invalidate local MCP schema caches; upgraded tools may not be discovered" >&2
fi

# Where the operator mounts its per-profile overlay ConfigMap (the operator's
# profileOverlayDir), and the script that consumes what it holds. Resolved here rather
# than at step 2.7, its only consumer, so the two paths sit next to the step 2d comment
# on how the default profile's config is assembled: the image seeds it, this overlay
# merges the operator's mutable settings into it, and the managed scope at
# $HERMES_MANAGED_DIR pins the immutable ones over the top at load time.
#
# Prefer the IMAGE copy of the script over the PVC copy. Step 2 syncs /opt/defaults with
# `cp -ru`, which skips a destination that looks newer — the same trap step 2a documents
# — so a PVC copy can outlive the image it came from. This script decides what every
# named profile's config ends up containing, so it must track the image.
OVERLAY_DIR="/opt/agent-config"
OVERLAY_SCRIPT="/opt/defaults/scripts/profile_overlay.py"
[ -f "$OVERLAY_SCRIPT" ] || OVERLAY_SCRIPT="$TARGET_DIR/scripts/profile_overlay.py"

# 2d. Seed the default profile's config.yaml, and check that the managed scope carrying
# the operator's pins actually arrived.
#
# The pins do NOT come through this file. They are mounted read-only at
# $HERMES_MANAGED_DIR (/etc/hermes) as Hermes' managed scope, and Hermes overlays them,
# per leaf key, on top of whatever config.yaml holds — on every load, in both the CLI
# (hermes_cli/config.py) and the gateway (gateway/config.py). So $TARGET_DIR/config.yaml
# stays the agent's own writable file and nothing HERE has to merge into it. Step 2.7
# does merge the operator's mutable settings into it, from profile-default.overlay.yaml,
# and it runs after this step for the reason given there.
#
# Two earlier shapes did need to, and both were worse:
#
#   * The operator subPath-mounted its rendering over $TARGET_DIR/config.yaml. A subPath
#     mount is a read-only mount POINT, so the agent could no longer save anything to its
#     own config: `/sethome` failed with EACCES (os.replace over a mount point gives
#     EBUSY, and the copyfile fallback then gives EACCES), the monitoring policy could
#     not persist `monitoring.install_id`, and every saved slash-command preference was
#     lost. The error the user saw had the path scrubbed out of it by the Slack egress
#     sanitiser, so it read "Permission denied: ''".
#   * Rebuilding the file here from image + overlay + the runtime's own edits fixed the
#     writability, but every rebuilt key stayed mutable. The merge rule — runtime wins
#     where the baseline has not moved — is what made that dangerous: a value the agent
#     wrote for itself survived every restart, so an agent that repointed model.base_url
#     at nothing could not be recovered by restarting it.
#
# The image's copy lives at /opt/chat-template, NOT in /opt/defaults, precisely so that
# step 2's `cp -ru` cannot reach it: that copy is mtime-driven and races an image roll,
# and losing the live file to it would discard the runtime's own state.
CHAT_TEMPLATE_CONFIG="/opt/chat-template/config.yaml"

# A FRESH VOLUME IS NOT AN ABSENT FILE. Upstream's stage2 hook runs before this script
# and seeds $HERMES_HOME/config.yaml from Hermes' own cli-config.yaml.example
# (docker/stage2-hook.sh, `seed_one "config.yaml" "cli-config.yaml.example"`), so by the
# time control reaches here the file always exists — on a brand-new PVC as much as on a
# ten-week-old one. Testing `! -f` therefore tested for a state that never occurs, and
# the whole fresh-volume path below was dead code.
#
# What that cost is not subtle, and only a fresh volume shows it: the back-fill is
# FILL-ONLY, so every key the example already spells out keeps ITS value, forever. A new
# install came up on upstream defaults for terminal, browser, code_execution, delegation,
# telemetry, database and streaming — none of which the image template had any way to
# correct, because they were present, not missing. Measured on a live cluster: 26
# top-level keys and 6488 bytes of upstream example where the template asks for 9 keys,
# with the template's contribution reduced to the 12 keys the example happened to omit.
#
# So detect the example itself. stage2 copies it verbatim and only chowns/chmods after,
# so a byte-compare against the file it copied FROM is exact at this moment, and it is
# the one moment that matters. It cannot false-positive on a real install: Hermes
# rewrites the file on first save, the managed-scope strip empties `model:`, and this
# very step back-fills into it — any of which ends the equality. And if a future Hermes
# templates the example on the way in and the compare stops matching, this degrades to
# the back-fill below, which is exactly today's behaviour: no worse, just not better.
#
# A function, and not three lines inline, so the tests can lift it out and run it against
# real files the way they already lift the back-fill program out of its heredoc — the
# branch it feeds sits in the owner-only path, which no unit test can reach.
# $1 = the live config.yaml, $2 = the example stage2 seeds from.
config_is_pristine_upstream_example() {
    [ -f "$1" ] && [ -f "$2" ] && cmp -s "$2" "$1"
}

# Fill the keys an image template declares and the live config.yaml does not, at any
# depth, and change nothing the file already says. $1 = the template, $2 = the live file.
#
# A function because two profiles need it: the default profile immediately below, and —
# when the front-door flag makes the gateway write to it — the platform profile at step
# 2.6b. One copy so the two cannot drift, and one heredoc so the tests can go on lifting
# this program out by its marker and running it against real files.
#
# The caller reports its own failure: which file the fill was for is the whole of what a
# reader needs from the warning, and only the caller knows.
backfill_config_from_template() {
    "$INSTALL_DIR/.venv/bin/python3" - "$1" "$2" <<'PYEOF'
import os
import sys

import yaml

template_path, live_path = sys.argv[1], sys.argv[2]

with open(template_path) as fh:
    template = yaml.safe_load(fh) or {}
with open(live_path) as fh:
    live = yaml.safe_load(fh) or {}

if not isinstance(template, dict) or not isinstance(live, dict):
    print("[ENTRYPOINT] config backfill skipped: not a mapping", file=sys.stderr)
    raise SystemExit(0)

added = []


def fill(src, dst, path):
    for key, value in src.items():
        if key not in dst:
            dst[key] = value
            added.append(".".join(path + [key]))
        elif isinstance(value, dict) and isinstance(dst[key], dict):
            # Recurse only where BOTH sides are mappings. Where the live file holds a
            # scalar or a list the agent has spoken, and a merge would overrule it.
            fill(value, dst[key], path + [key])


fill(template, live, [])

if added:
    # Atomic, because a truncated config.yaml is worse than a hollow one: os.replace
    # over a plain PVC file is a rename, and the reader either sees all of the old file
    # or all of the new one. (The subPath mount that made this impossible is gone —
    # see the note above.)
    tmp_path = live_path + ".backfill.tmp"
    with open(tmp_path, "w") as fh:
        yaml.safe_dump(live, fh, sort_keys=False, default_flow_style=False)
    os.replace(tmp_path, live_path)
    print(f"[ENTRYPOINT] config backfill: restored {len(added)} key(s) the live file did not hold: {', '.join(added)}")
PYEOF
}

# Fresh volume: lay the image's copy down before anything can read it, so neither the
# gateway nor the dashboard sidecar comes up against a missing config, falls back to
# Hermes' built-in defaults, and saves those over the top.
if [ ! -f "$TARGET_DIR/config.yaml" ] && [ -f "$CHAT_TEMPLATE_CONFIG" ]; then
    cp "$CHAT_TEMPLATE_CONFIG" "$TARGET_DIR/config.yaml" \
        || echo "WARN: could not seed $TARGET_DIR/config.yaml from $CHAT_TEMPLATE_CONFIG" >&2
elif [ -f "$CHAT_TEMPLATE_CONFIG" ] \
    && config_is_pristine_upstream_example \
        "$TARGET_DIR/config.yaml" "$INSTALL_DIR/cli-config.yaml.example"; then
    # Fresh volume, stage2 having got here first. Overwrite rather than fill: nothing in
    # that file is a runtime edit — no agent has run yet — so there is nothing to
    # preserve, and filling into it is precisely what leaves the upstream defaults live.
    echo "[ENTRYPOINT] config.yaml is upstream's untouched cli-config.yaml.example (fresh volume); replacing it with the image template." >&2
    cp "$CHAT_TEMPLATE_CONFIG" "$TARGET_DIR/config.yaml" \
        || echo "WARN: could not seed $TARGET_DIR/config.yaml from $CHAT_TEMPLATE_CONFIG" >&2
elif [ -f "$CHAT_TEMPLATE_CONFIG" ]; then
    # Existing volume: FILL what the file does not say, and change nothing it does.
    #
    # Two things hollow out the live file, and neither is a bug in the agent. Hermes'
    # save_config strips every leaf the managed scope holds before writing (config.py,
    # `_strip_dotted_keys(config, managed_keys)`), so one `/sethome` turns a pinned
    # `model:` block into `model: {}` on disk. And a release that STOPS pinning a leaf —
    # this one narrows the managed render to the model, the chat platforms and the cron
    # approval mode — hands that leaf back to a file the previous release already emptied
    # of it. The pod then runs on Hermes' built-in defaults for everything the image
    # template was supposed to supply, with green health checks and no error anywhere.
    #
    # Fill-only, so it cannot become the thing it is repairing: a key the file already
    # holds is left exactly as the agent last wrote it, including one the agent set to
    # an empty string or an empty list on purpose. Only ABSENT keys are added, at any
    # depth, which is why `model: {}` is repaired while `model: {default: other}` is not
    # touched. That is what keeps a `/sethome` home channel, the monitoring install id
    # and saved slash-command preferences across restarts — the property the deleted
    # three-way merge kept getting wrong in the other direction.
    backfill_config_from_template "$CHAT_TEMPLATE_CONFIG" "$TARGET_DIR/config.yaml" \
        || echo "WARN: could not backfill $TARGET_DIR/config.yaml from $CHAT_TEMPLATE_CONFIG; the agent may be running on Hermes defaults for keys the image template owns" >&2
fi

# Managed scope fails OPEN by design: a missing directory, or a config.yaml that does not
# parse, is logged by hermes and otherwise ignored so a bad policy file can never brick
# startup (managed_scope.py). That is the right default for Hermes and the wrong one for
# us — here it would mean the model endpoint and the chat platform settings are silently
# unpinned, on a pod whose health checks stay green. So assert it, loudly.
#
# Report, never exit: an agent that comes up unpinned is still an agent that answers, and
# failing the container would trade a degraded front door for no front door at all.
#
# Gated on HERMES_MANAGED_DIR being SET, not on the default path existing. The operator
# sets it on the one container it mounts the scope into; an image started any other way —
# compose, a plain manifest, `docker run`, the kustomize bases — has no managed scope by
# design and is not misconfigured for lacking one. Warning there would make the alarm
# routine, which is the only way to lose the one case it exists for: an operator-managed
# pod whose mount did not arrive. The env var is set by the Deployment, so it survives
# exactly the failure being watched for.
MANAGED_DIR="${HERMES_MANAGED_DIR:-}"
if [ -z "$MANAGED_DIR" ]; then
    :
elif [ ! -f "$MANAGED_DIR/config.yaml" ]; then
    echo "WARN: no managed config at $MANAGED_DIR/config.yaml — the operator's settings (model endpoint, chat platforms) are NOT pinned and the agent can overwrite them" >&2
elif ! "$INSTALL_DIR/.venv/bin/python3" -c '
import sys
from hermes_cli import managed_scope

keys = managed_scope.managed_config_keys()
if not keys:
    sys.exit(1)
# Named individually rather than just counted. A non-empty key set only proves the file
# parsed; these are the leaves the pin exists for — the model endpoint the agent reasons
# through, and the wire protocol that has to keep matching it. Everything else the render
# emits is conditional on what the CR enables: the chat platform settings are checked by
# their presence in the managed .env instead, and a deployment with no chat integration
# at all legitimately renders neither.
for expected in ("model.default", "model.base_url", "model.api_mode"):
    if expected not in keys:
        print(f"managed scope is live but does not pin {expected}", file=sys.stderr)
        sys.exit(1)
print(f"managed scope: {len(keys)} pinned config keys from {managed_scope.get_managed_dir()}")
'; then
    echo "WARN: $MANAGED_DIR/config.yaml did not load as a managed scope (unparseable, or missing the model keys) — hermes fails open, so the agent is running UNPINNED" >&2
fi

# The same assertion for the ONE key in the managed .env that is not about chat, and the
# only one whose absence takes the whole API surface down rather than degrading a feature.
#
# What it is guarding against, and why it needs guarding at all (issue #786). Hermes'
# Docker stage2 hook — step 1 above, upstream's, in every container — generates a strong
# random API_SERVER_KEY into $TARGET_DIR/.env whenever that file does not already carry
# one, and load_hermes_dotenv applies that file with override=True. Before the operator
# pinned this key, the gateway therefore authenticated against a value invented at boot
# inside the pod that nothing else knew: `os.environ["API_SERVER_KEY"]`, the Secret, the
# credential proxy's AGENT_API_UPSTREAM_KEY and the container's own startup probe all
# presented the sentinel and all got 401. The managed .env is applied last of everything,
# so pinning API_SERVER_KEY there is what makes the whole pod agree — but managed scope
# fails open, so an absent or unmounted pin restores the broken state silently.
#
# It is checked HERE, before `exec`, rather than left to the container's startup probe.
# The probe (agentAPIProbe in platformagent_manifests.go) does catch the failure — it
# curls an authenticated route with $API_SERVER_KEY and the pod never goes Ready — but a
# probe reports THAT something is wrong, not WHICH of the several things it could be. One
# line naming the missing pin turns a CrashLoopBackOff into a diagnosis.
#
# Reads the file with grep rather than asking Hermes: this is the operator's rendered
# text, and the point is to check what was MOUNTED, not what some library would resolve.
# Comparing values, not just presence, because the two renders sit in different files in
# different languages and agreeing is the entire property.
#
# Report, never exit, for the reason the block above gives.
#
# A function taking its inputs as arguments, so the tests can run the shipped definition
# rather than a copy of it (tests/test_docker_entrypoint.py).
warn_unless_api_key_is_pinned() {
    _wuakip_managed_env="$1"
    _wuakip_key="$2"

    if [ ! -f "$_wuakip_managed_env" ]; then
        echo "WARN: no managed $_wuakip_managed_env — API_SERVER_KEY is NOT pinned, so Hermes' stage2 hook may have generated a different key into $TARGET_DIR/.env and every authenticated call to this agent's API will 401 (issue #786)" >&2
        return 0
    fi
    # -x and -F together: a line that merely CONTAINS the value is a different pin, and a
    # key whose value happens to look like a pattern is still just a string.
    if grep -qxF "API_SERVER_KEY=$_wuakip_key" "$_wuakip_managed_env"; then
        return 0
    fi
    # The two remaining shapes are both broken, but they are NOT broken the same way, and
    # the difference is which file an operator should go and read. Say which one wins.
    if grep -q '^API_SERVER_KEY=' "$_wuakip_managed_env"; then
        # The managed scope is applied last with override=True, so a file that pins some
        # OTHER value is the value the gateway will accept — and it will then 401 this
        # container's env, which is what the sidecar, the startup probe and every in-pod
        # caller present.
        echo "WARN: $_wuakip_managed_env pins API_SERVER_KEY to a different value than this container's env — the managed file is applied last, so the gateway will accept ITS key and 401 the credential proxy, the startup probe and every in-pod caller (issue #786). Re-render the operator's config ConfigMap; renderManagedEnv must emit the same value as the container env." >&2
    else
        # No pin for this key at all: the managed file overrides nothing, so the winner is
        # $TARGET_DIR/.env — where Hermes' stage2 hook generates a random key that nothing
        # outside the gateway process knows. This is the pre-fix shape exactly, and it is
        # what a stale (chat-only) ConfigMap still renders.
        echo "WARN: $_wuakip_managed_env does not pin API_SERVER_KEY — with no managed pin the PVC's $TARGET_DIR/.env wins, and Hermes' stage2 hook generates a random key there that the credential proxy, the startup probe and every in-pod caller will 401 against (issue #786). Re-render the operator's config ConfigMap; renderManagedEnv must emit API_SERVER_KEY." >&2
    fi
    return 0
}

# Gated on the key being SET as well as on the managed dir, and not only because there is
# nothing to compare otherwise: an unset API_SERVER_KEY is Hermes' own supported shape —
# stage2 generates one and the pod is internally consistent — so it is the operator's
# arrangement, half-applied, that this is looking for.
if [ -n "$MANAGED_DIR" ] && [ -n "${API_SERVER_KEY:-}" ]; then
    warn_unless_api_key_is_pinned "$MANAGED_DIR/.env" "$API_SERVER_KEY"
fi

# The image's own copy of the scaffolder, never the volume's. Step 2 seeds
# $TARGET_DIR/scripts with `cp -u`, which SKIPS any file the PVC holds a newer
# mtime for — the same trap step 2a exists to work around for config.yaml. This
# is the one script in the pod whose job is to make the volume track the image,
# so it is the one script that must not be read back off the volume: last
# release's scaffolder running this release's template is how a partial upgrade
# looks like a successful one. (Step 2b force-syncs the rest of the scripts for
# the same reason; this one cannot wait for that to have worked.)
SCAFFOLD="/opt/defaults/scripts/profile_scaffold.py"

# 2c. Retire the cron ids this release moved off the default profile's roster,
# and land the tick job that replaces them.
#
# Step 2's `cp -ru` cannot touch this file and never could: the scheduler
# rewrites $TARGET_DIR/cron/jobs.json on every tick, so the volume's copy is
# always newer than the image's and the update-only copy always skips it. Step
# 2c-bis below is the general reconcile for that. This step is the half it cannot
# do, because cron_jobs_sync.py never prunes: its ledger stops a retired job
# coming BACK, it never removes one the volume already has.
#
# `--cron-retire` deletes the governance ids from this roster outright. They ran
# here only while the platform profile's store had no ticker; now that
# `profile-cron-tick` gives it one, they are back on the Platform Agent's own
# roster where the scheduler can reach their `skills`, `model` and `max_turns`
# (see profile_cron_tick.py). Dropping the shipped entries is not enough on its
# own: an upgraded PVC would go on firing its enabled copies here while step 2.6
# fires the re-enabled originals over there — every audit running twice, against
# itself. Retiring the ids is what makes the move a move rather than a fork.
#
# stockout-prevention moves for the same reason as the other seven, one release
# later: it landed on this roster after the move was written, so it is the one id
# here that an upgraded PVC can be carrying while no default-profile roster in
# this image ships it. Retiring it and shipping it on the Platform Agent's roster
# is the same move, applied late.
#
# It runs BEFORE 2c-bis deliberately, and the two lists must not overlap. The
# order only decides how fast an accidental overlap bites, not whether it does:
# retire-last would delete a job the image still ships on every single boot,
# while retire-first survives exactly one. cron_jobs_sync records every id in
# the image roster in its ledger on the boot it reinstalls one, and from the
# next boot on it reads "in the ledger, absent from the file" as a deliberate
# runtime removal and leaves it out for good. Neither order self-corrects, so
# an id belongs on this list only once no roster in this image ships it.
#
# This list shrinks to nothing once no live volume can still be carrying the
# entries; until then removing a name from it silently restores the double-fire.
#
# `--home` overlays the default profile in place, because that profile IS
# $TARGET_DIR and has no entry under profiles/ to name. `--cron-jobs` names the
# only id this call may force, which keeps the merge it does alongside the
# retirement a subset of 2c-bis rather than a second policy for the same file:
# two of the jobs in this roster DELETE THEMSELVES — bootstrap_delivery.py's
# _cleanup removes the scan/delivery pair once the onboarding report lands — and
# an unfiltered merge would put both back.
if [ -f "/opt/defaults/cron/jobs.json" ] && [ -f "$SCAFFOLD" ]; then
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --home "$TARGET_DIR" \
        --template /opt/defaults \
        --items "cron" \
        --cron-jobs "profile-cron-tick" \
        --cron-retire "compliance-audit obtainability-audit security-patch-orchestrator fleet-wide-cost-analysis fleet-consistency-drift ai-security-audit stockout-prevention github-issue-resolver" \
        >/dev/null || echo "WARN: default-profile cron merge failed; jobs added by this image will not run" >&2
fi

# 2c-bis. Reconcile the image's cron jobs into the running agent's job file.
# cron/jobs.json cannot join either force-sync above: the scheduler writes last_run into it
# on every tick (which is also why `cp -u` never overwrites it — the PVC copy is always the
# newer one), and the bootstrap_onboarding plugin writes a chat binding into it. Overwriting
# would reset every schedule and unbind the chat; not overwriting means a job added to the
# image never appears on an existing deployment. cron_jobs_sync.py merges by job id instead,
# per key: the image wins every key it ships (the definition, including `enabled`), and every
# key it does not ship (the scheduler's own state) stays as the volume had it.
#
# The image's own copy of the script, not the volume's, for the reason step 2.5 gives for
# the scaffolder: this is a script whose whole job is to make the volume track the image, so
# reading it back off the volume is the one place a partial upgrade can hide. It also frees
# this step from depending on step 2b having worked.
#
# Writing jobs.json without a lock is safe WITHIN THIS POD, on two facts. The scheduler in
# THIS container is not running yet — everything here is ahead of `exec "$@"`. And no OTHER
# container in this pod is running this code: step 1.5 hands the shared tree to a single
# owner, so the dashboard, which has no scheduler and no reason to touch the schedule, stops
# before it gets here.
#
# Both facts stop at the pod boundary. Step 1.5 elects an owner per pod, not per volume, so
# at availability.replicas > 1 — where the operator gives the replicas ONE ReadWriteMany PVC
# rather than a volume each — every replica's gateway is an owner and several of them run
# this against the same file, with a rolling update overlapping new pods and old. The
# exposure and why it is not fixable from inside the script are set out in cron_jobs_sync.py's
# Concurrency section; the short version is that the reconcile wants to run once per volume,
# which is a topology change rather than a lock. Single-replica installs, the default, are
# unaffected. Do not restore a bare "there is no second writer" claim here: it was written
# once, it was wrong, and it read as verified.
#
# --assume-retired covers the one case the script's ledger cannot know on its first run: a
# deployment that finished onboarding before this existed has no record that
# bootstrap_delivery.py:_cleanup retired the two onboarding jobs, so they would look new and
# be reinstalled. .bootstrap_completed is that record.
CRON_SYNC="/opt/defaults/scripts/cron_jobs_sync.py"
if [ -f "$CRON_SYNC" ] && [ -f "/opt/defaults/cron/jobs.json" ]; then
    ASSUME_RETIRED=""
    if [ -f "$TARGET_DIR/.bootstrap_completed" ]; then
        ASSUME_RETIRED="bootstrap-inventory-scan,bootstrap-inventory-delivery"
    fi
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$CRON_SYNC" \
        --image-jobs /opt/defaults/cron/jobs.json \
        --assume-retired "$ASSUME_RETIRED" \
        || echo "WARN: cron job reconcile failed; scheduled jobs may be stale" >&2
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
#     profiles/platform/config.yaml at runtime — step 2.7's overlay merge is the
#     one exception, and it runs after this on purpose. Without syncing it, an
#     image that changes the platform's toolsets or plugins has no effect on any
#     existing deployment.
#
#     That last premise stops holding when HERMES_GATEWAY_PROFILE names this
#     profile. The gateway is then homed here, so this is the file `/sethome`
#     persists the home channel into and the monitoring policy mints
#     `monitoring.install_id` in — exactly the runtime state step 2d exists to
#     protect on the default profile, and a force-sync discards it on every
#     restart. So config.yaml leaves the --items list, and step 2.6b below
#     back-fills it the way step 2d back-fills the default profile's, with the
#     same fill-only rule and the same trade: keys the image ADDS still arrive,
#     keys the file already holds stay as the agent last wrote them. Everything
#     else here force-syncs either way.
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
# overlay runs.
#
# --plugins is passed here as well as in step 2.5, for the reason this whole step
# exists: 2.5 runs on first scaffold only, so a plugin the image adds or changes
# after the PVC was created would otherwise never arrive, and a plugin copy that
# failed part-way through would stay half-copied for the volume's lifetime. The
# copy adds and overwrites without pruning, so plugin-owned runtime state on the
# volume — hermes_otel's live.db and the rest — is not in the source tree and
# survives. Targeted plugin volumes are linked in afterwards by step 2.65.
#
# cron/jobs.json is the one entry that is merged rather than replaced, inside
# profile_scaffold.py. It is image-owned and runtime state in the same file: the
# schedules, prompts and `enabled` flags ship in the image, but the scheduler
# writes each job's run history back into it and the operator can add jobs of
# its own. Copying it wholesale erased both on every pod restart, losing the
# operator's jobs and re-firing one-shots (an erased `last_run_at` is an erased
# already-ran guard) while leaving recurring jobs to skip a late run instead of
# catching it up. The merge is per key — the image wins every key it ships, the
# volume keeps every key it does not — so flipping `enabled` to false in the
# image still disables a watchdog.
#
# Known limit: the overlay adds and overwrites, it never prunes. An SOP dropped
# from the image stays on the PVC until an operator removes it by hand. That is
# the deliberate trade — this path must not start silently deleting from a user's
# volume — not an oversight.
#
# `skills/` is the one exception, and step 2.6a below is where it is made rather
# than here. Prune-never costs more there than it does for governance/: a skill
# is loaded by name from a catalogue the agent enumerates, so a retired one is
# not inert on the volume the way an unreferenced SOP is — it stays offerable,
# and a worker picks it over the procedure that replaced it. Read the two
# paragraphs together: this overlay refreshes what the image still ships, and
# 2.6a is what makes what the image dropped actually go away.
#
# hindsight/ carries the memory provider's connection config. It is image-owned
# for the same reason step 2a's copy is: a hand-edited copy left on the volume
# silently outlives the design that wrote it. The platform profile needs its own
# because a kanban worker runs with HERMES_HOME set to profiles/platform, and the
# plugin resolves $HERMES_HOME/hindsight/config.json — the default profile's copy
# is not on that path. It is named as a directory, not as hindsight/config.json:
# --items joins the name onto the profile home and copy2 needs the parent to
# exist, where a directory goes through copytree(dirs_exist_ok=True).
# Gated on profile.yaml, not on the directory: a bare mount point is not a profile, and
# dressing one in a persona and a config makes it indistinguishable from a real profile at
# the next start — which is how a half-built profile used to become permanent.
#
# `--cron-retire` finishes a retirement the two-release rule started. The first
# five ids named here shipped `enabled: false` for several releases and are now
# gone from the image's roster; none could produce a finding on a stock install
# anyway (see the retired-watchdog note in
# docs/site/src/content/docs/concepts/autonomous-watchdogs.md). Dropping the
# shipped entries alone would stop there: merge_cron_store keeps every volume
# job the image is silent about, so each PVC would carry five disabled entries
# no image could ever reach again, and `cronjob(action='list')` would go on
# showing them. Retiring the ids is what makes the deletion reach the volume.
#
# `github-issue-resolver` is here for the other reason the README gives: an id
# that has to stop firing in ONE release, not two. It was a `*/30` prompt job,
# so every tick it left behind is a full agent turn spent learning there was no
# work; `github-repo-watcher` replaces it with a `no_agent` gate that files a
# card only when the poll finds something. Leaving the old id enabled on the
# volume for a release would mean both run — the model woken 48 times a day for
# nothing, alongside the job whose entire purpose is to stop that. It is safe to
# cut in one step because nothing is lost when it stops: the replacement polls
# the same repository through the same `resolver.py poll`, three times as often.
#
# This list shrinks to nothing once no live volume can still be carrying the
# entries. Until then, removing a name from it silently strands that id.

# Whether the platform profile is the one the GATEWAY runs as, rather than one only
# kanban workers and cron jobs ever home to. The operator sets HERMES_GATEWAY_PROFILE
# from spec.harness.experimental.platformFrontDoor; two steps below turn on the answer,
# and they have to agree, so they ask it here rather than each testing the variable.
# Unset — every install that has not opted in — both of them are a no-op.
platform_is_front_door() {
    [ "${HERMES_GATEWAY_PROFILE:-}" = "platform" ]
}

# The image-owned entries step 2.6 force-syncs into the platform profile.
#
# config.yaml is on the list only while the image owns it outright. At the front door
# the running agent writes to it — `/sethome` persists the home channel there and the
# monitoring policy mints monitoring.install_id — so a force-sync discards both on
# every restart. It comes off the list and step 2.6b back-fills the file instead, the
# way step 2d does for the default profile. Nothing else on the list is written at
# runtime, so nothing else moves either way; the one entry is derived rather than a
# second list so the two answers cannot drift apart.
platform_sync_items() {
    _items="SOUL.md AGENTS.md CAPABILITIES.md cron skills governance hindsight"
    platform_is_front_door || _items="config.yaml $_items"
    echo "$_items"
}

if [ -f "$TARGET_DIR/profiles/platform/profile.yaml" ] && [ -d "$PLATFORM_TEMPLATE" ] && [ -f "$SCAFFOLD" ]; then
    HOME=/tmp HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SCAFFOLD" \
        --name platform \
        --template "$PLATFORM_TEMPLATE" \
        --plugins /opt/defaults/plugins \
        --items "$(platform_sync_items)" \
        --cron-retire "blueprint-sync policy-propagation global-capacity-orchestrator standardization-validator lifecycle-deprecation-manager github-issue-resolver" \
        >/dev/null || echo "WARN: platform profile force-sync failed; continuing" >&2
fi

# 2.6a Re-sync each specialist profile's skills from the image on every start.
# Same reasoning as 2.6, applied to the directory that carries the agent's
# executable procedures. The scaffold in 2.5 (and cluster_agent_profile.py for
# the cluster profiles) overlays skills only when the profile is ABSENT, and no
# cluster profile has skills in any force-sync list, so profiles/cluster-*/skills
# is otherwise frozen at whatever version first created the PVC — a helper script
# fixed months ago is still the broken one on every upgraded cluster.
#
# Skills are wholly image-owned (nothing writes runtime state under them; the
# cluster overlay list in cluster_agent_profile.py:OVERLAY_ITEMS treats them the
# same way), so this is a whole-directory REPLACE rather than a copy-over: a
# skill deleted from the image has to actually disappear, or a retired procedure
# stays loadable forever. That is also why this still runs for the platform
# profile even though step 2.6 just listed `skills` in its --items: the
# scaffolder overlays with copytree(dirs_exist_ok=True), which refreshes what the
# image still ships and leaves what it dropped.
#
# Building the replacement alongside and renaming keeps the window where `skills`
# does not exist to two renames, and nothing reads the profile until `exec "$@"`
# below.
#
# EVERY step is guarded, and the function never returns non-zero. It is called as
# a bare command under `set -e`, so an unguarded `mv` that fails does not degrade
# the sync — it kills the container before it ever reaches `exec "$@"`, turning a
# stale skills directory into a CrashLoopBackOff. The filesystem here is a PVC
# whose writes can fail for reasons that have nothing to do with this script
# (ENOSPC, a permission change, an `.old` left behind by a previous boot that was
# killed mid-swap), and none of them are worth refusing to start over: the
# profile keeps the skills it already had, which is exactly the state this step
# exists to improve on and not one it can make worse.
#
# The rollback matters for the same reason. Between the two renames `skills` does
# not exist, and a profile with no skills at all is worse than one with stale
# ones — `hermes` reports "Unknown skill(s)" and the worker exits 1. So a failure
# there puts the original back rather than leaving the gap.
sync_profile_skills() {
    _src="$1/skills"
    _dst="$2/skills"
    [ -d "$_src" ] || return 0

    # The staging paths are per POD, and that is load-bearing rather than tidy.
    # $_dst lives on the PVC, and at availability.replicas > 1 the operator hands
    # every replica the SAME PVC (ReadWriteMany; see step 2c-bis and cron_jobs_sync.py's
    # Concurrency section), so fixed siblings named `skills.new` and `skills.old`
    # are shared names on a shared volume. The unconditional `rm -rf` below then
    # reaches into another pod's swap: pod A completes `mv skills skills.old`, so
    # the profile's only copy is the aside-moved one; pod B enters here and deletes
    # both it and A's staged tree; A's install fails, A's rollback finds nothing to
    # restore, and A prints "the profile keeps its existing copy" over a profile
    # that now has no skills/ at all. Everything downstream reads that volume.
    #
    # $$ would not fix it. This script is the container ENTRYPOINT, so it is pid 1
    # or near it, and replicas of one scale-up boot identically — they would agree
    # on the suffix. The pod name is what differs: it is unique in the cluster and
    # never reused, the kubelet puts it in HOSTNAME, and `hostname` reports it if
    # the variable is missing. The pid is only the last resort, for a shell that has
    # neither.
    #
    # $_src is NOT shared: it is the read-only image template inside this container,
    # so only the destination side needs this.
    _tag="${HOSTNAME:-}"
    [ -n "$_tag" ] || _tag="$(hostname 2>/dev/null || true)"
    [ -n "$_tag" ] || _tag="$$"
    _new="$_dst.new.$_tag"
    _old="$_dst.old.$_tag"

    # Clearing only this pod's own litter is the price of the rename. A tree left
    # by a DIFFERENT pod is not cleaned here, because from inside this script a
    # foreign staging directory is indistinguishable from one a live pod is filling
    # right now, and deleting that is the bug above. It leaks only when a pod dies
    # inside the swap window — the normal path renames `.new` away and removes
    # `.old` — and a leaked tree is inert: nothing loads from a suffixed path. A
    # restarted container keeps its pod name, so the common crash-loop case does
    # clean up after itself on the next boot.
    rm -rf "$_new" "$_old" 2>/dev/null || true
    # That cleanup is best-effort by necessity — a failed `rm` must not kill start-up
    # — so the next line cannot assume it worked. `cp -a src dst` nests INSIDE dst
    # when dst already exists, exactly as the `mv` below does, and this is the half
    # that loses data rather than the half that fails safe: a surviving `.new` makes
    # the staging copy land at skills.new.<tag>/skills, which then installs as
    # skills/skills and takes the closing `rm -rf "$_old"` with it. The profile is
    # left with no loadable skills, its previous copy deleted, and every command in
    # the chain having exited 0. So confirm the ground is clear instead of testing
    # the cp.
    #
    # A surviving `.old` alone is harmless — `mv "$_dst" "$_old"` nesting into it
    # still frees $_dst for the real install — but it is checked here too so that no
    # reader has to redo that case analysis to trust the block below.
    if [ -e "$_new" ] || [ -e "$_old" ]; then
        echo "WARN: could not clear a staging directory beside $_dst; the profile keeps its existing skills" >&2
        return 0
    fi

    if ! cp -a "$_src" "$_new" 2>/dev/null; then
        rm -rf "$_new" 2>/dev/null || true
        echo "WARN: could not stage new skills for $2; the profile keeps its existing copy" >&2
        return 0
    fi

    if [ -e "$_dst" ] && ! mv "$_dst" "$_old" 2>/dev/null; then
        rm -rf "$_new" 2>/dev/null || true
        echo "WARN: could not move the existing skills aside in $2; the profile keeps its existing copy" >&2
        return 0
    fi

    # `mv a b` where b is an existing directory moves a INSIDE it, so a $_dst that
    # somehow survived the step above would silently produce skills/skills rather
    # than fail. Nothing loads from there and nothing prunes it. With per-pod
    # staging names this is now also the arm that catches the benign version of the
    # race: another replica installing its own copy — byte-identical, from the same
    # image — into $_dst while this one was staging.
    if [ -e "$_dst" ] || ! mv "$_new" "$_dst" 2>/dev/null; then
        # The rollback has the same nesting hazard as the line it is rolling back,
        # and reaches it more easily: the left arm above fires precisely BECAUSE
        # $_dst exists, which is the one condition that makes this `mv` nest rather
        # than restore. Unguarded it buries the previous skills at skills/skills.old
        # — invisible to the loader, never pruned, and reported as a clean warning.
        # Restoring is only correct when $_dst is free; when it is not, something
        # already occupies the destination and .old is left for the next boot's
        # opening guard to report rather than silently folded into the tree.
        if [ -e "$_dst" ]; then
            echo "WARN: $_dst reappeared during the swap in $2; leaving $_old in place rather than nesting it" >&2
        else
            mv "$_old" "$_dst" 2>/dev/null || true
        fi
        rm -rf "$_new" 2>/dev/null || true
        echo "WARN: could not install new skills into $2; the profile keeps its existing copy" >&2
        return 0
    fi

    rm -rf "$_old" 2>/dev/null || true
    return 0
}
if [ -d "$TARGET_DIR/profiles/platform" ] && [ -d "$PLATFORM_TEMPLATE" ]; then
    sync_profile_skills "$PLATFORM_TEMPLATE" "$TARGET_DIR/profiles/platform"
fi
# 2.6 (continued), for the cluster profiles: personas from the template, skills through
# the helper defined just above, and one targeted config repair. Kept after 2.6a only
# because it is the caller — everything here belongs to 2.6's force-sync, not to it.
CLUSTER_TEMPLATE="/opt/cluster-template"
if [ -d "$CLUSTER_TEMPLATE" ]; then
    for d in "$TARGET_DIR"/profiles/cluster-*; do
        [ -d "$d" ] && [ -f "$d/config.yaml" ] || continue
        for f in SOUL.md AGENTS.md CAPABILITIES.md; do
            [ -f "$CLUSTER_TEMPLATE/$f" ] && cp -f "$CLUSTER_TEMPLATE/$f" "$d/$f" 2>/dev/null || true
        done
        sync_profile_skills "$CLUSTER_TEMPLATE" "$d"
        # Targeted self-heal: drop `memory.provider` from cluster configs already
        # on the PVC. The template no longer sets it (per-user memory scopes by
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

# 2.6b Back-fill the platform profile's config.yaml when it is the front door.
#
# Step 2d's problem, arriving at a second profile. Step 2.6 just left this file alone
# because the gateway now writes to it, and an untouched file tracks the image for as
# long as the volume lives: an image that adds an mcp server, a toolset or a plugin to
# agents/platform/config.yaml would reach a fresh volume and no existing one. So the
# platform profile takes the same deal the default profile takes — fill what the file
# does not say, change nothing it does. Keys the image ADDS arrive; keys the file
# already holds stay as the agent last wrote them, which is what makes `/sethome` and
# monitoring.install_id survive the restart.
#
# The operator's own settings do NOT come through here. profile-platform.overlay.yaml
# is merged by step 2.7 like every other profile's, and the managed scope pins the
# immutable leaves at load time — the same two routes the default profile takes.
#
# Runs after step 2.6, which is what leaves config.yaml alone for it to read, and
# before step 2.7, so the overlay is merged into the filled file rather than a fill
# landing on top of the overlay.
#
# PRIMARY ONLY, exactly as step 2d is: nothing here is per-container, and a second
# writer racing the first over one file on a shared PVC is what step 1.5 exists to stop.
#
# Flag off, this step does not run and step 2.6 force-syncs config.yaml as it always
# has, which subsumes any fill.
#
# Both of step 2d's arms, for the same reason it has two. Once config.yaml is off the
# force-sync, this step is the ONLY thing left that can write it: step 2.5 is gated on
# the profile being absent, step 2.6 no longer carries the name in --items, and step
# 2.7 skips a profile directory whose config.yaml is missing. So a profile registered
# without one — profile_scaffold writes profile.yaml before it copies the template, and
# the caller above swallows a failure between the two with a WARN — would stay that way
# for the life of the volume, on the profile receiving chat, with an absent
# platform_toolsets resolving to the full core bundle rather than failing closed. Flag
# off the next boot healed it; flag on nothing does. Seed from the template when the
# file is absent, fill it when it is not.
if platform_is_front_door && [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] \
    && [ -f "$PLATFORM_TEMPLATE/config.yaml" ] \
    && [ -d "$TARGET_DIR/profiles/platform" ]; then
    if [ -f "$TARGET_DIR/profiles/platform/config.yaml" ]; then
        backfill_config_from_template \
            "$PLATFORM_TEMPLATE/config.yaml" "$TARGET_DIR/profiles/platform/config.yaml" \
            || echo "WARN: could not backfill profiles/platform/config.yaml from $PLATFORM_TEMPLATE/config.yaml; the front door may be missing keys the image template owns" >&2
    else
        echo "[ENTRYPOINT] profiles/platform/config.yaml is missing; seeding it from the image template." >&2
        cp "$PLATFORM_TEMPLATE/config.yaml" "$TARGET_DIR/profiles/platform/config.yaml" \
            || echo "WARN: could not seed profiles/platform/config.yaml from $PLATFORM_TEMPLATE/config.yaml; the front door has no config of its own" >&2
    fi
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
#
# $OVERLAY_DIR and $OVERLAY_SCRIPT are resolved above step 2d. The default profile is
# reconciled here too, but separately — see the block after the loop.
#
# Gated on $OVERLAY_DIR existing, not merely on the script existing. Only the agent
# container mounts platform-agent-config-vol; in the dashboard sidecar the directory is
# absent, overlays_for() finds no files, and apply_overlay reads that as "the operator
# withdrew the overlay" and deletes every profile's last-applied record. A container that
# cannot see what the operator rendered must not get to decide what the operator said.
if [ -f "$OVERLAY_SCRIPT" ] && [ -d "$OVERLAY_DIR" ]; then
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

    # The front door, which the loop above cannot reach: it has no directory under
    # profiles/, its home IS $TARGET_DIR, so it is passed by name.
    #
    # Guarded on there being something to do. apply_overlay rewrites config.yaml
    # unconditionally — through yaml.safe_dump, which sorts the keys and drops every
    # comment — and this is the one profile config a human reads and the agent writes.
    # With no overlay rendered and none previously applied, that rewrite would churn the
    # file on every start for no change at all. When either exists the rewrite is the
    # point: applying, or undoing a withdrawn overlay.
    #
    # Runs AFTER step 2d, which seeds a fresh volume from the image and back-fills lost
    # keys. Reversing the two would merge the overlay into a config the seed then
    # replaced.
    DEFAULT_OVERLAY="$OVERLAY_DIR/profile-default.overlay.yaml"
    if [ -f "$DEFAULT_OVERLAY" ] || [ -f "$TARGET_DIR/.operator-overlay.json" ]; then
        "$INSTALL_DIR/.venv/bin/python3" "$OVERLAY_SCRIPT" --profile-dir "$TARGET_DIR" \
            --profile-name default --overlay-dir "$OVERLAY_DIR" \
            || echo "WARN: overlay sync failed for the default profile; AgentPlugins with no targetProfile will not load" >&2
    fi

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
            # The default profile IS $TARGET_DIR and has no entry under profiles/, so the
            # directory test above can never find it. It is reconciled by name just
            # above; this case only keeps it out of the typo warning.
            default)   continue ;;
            cluster-*) echo "NOTE: overlay $base names cluster profile '$name', which is not scaffolded yet; it applies when that cluster is onboarded" >&2 ;;
            *)         echo "WARN: overlay $base names profile '$name', which does not exist; plugins targeting it will not load" >&2 ;;
        esac
    done
fi

# 3. (removed) Enabling hermes_otel in the default profile's config.yaml.
#
# The step appended `hermes_otel` to plugins.enabled if it was missing, guarded on the file
# being writable. It had nothing to do, and for most of its life could not fire at all:
#
#   - The content was already there. `hermes_otel` heads plugins.enabled in
#     agents/chat/config.yaml, which the image installs as /opt/chat-template/config.yaml,
#     and it is in the operator's DefaultBuiltInPlugins — so both of step 2d's inputs list
#     it and the append was a no-op.
#   - The write could not land anyway while the operator subPath-mounted its rendering
#     over that path: ConfigMap volumes are mounted read-only whatever the mount's readOnly
#     field says, so `[ -w ]` was false in both the gateway and the dashboard. The mode is
#     0400/0755 on root-owned files against RunAsUser 10000 besides (the volume's
#     DefaultMode in platformagent_manifests.go), so it failed the ownership test too.
#
# Step 2d has since made that an ordinary writable file on the PVC, which only makes
# restoring this worse rather than newly safe: the guard would now pass, and the step would
# rewrite — non-atomically, over a file step 2d has just staged-and-renamed into place — a
# config it has nothing to add to. Where the guard did pass before (compose, `docker run`)
# that is exactly what it did, and yaml.safe_dump round-trips the file: it sorted the keys
# and dropped every comment in a config people read.
#
# Do not restore it as a "belt and braces" measure. If a profile ever needs a plugin the
# image does not ship enabled, the operator overlay in step 2.7 is the mechanism, and it is
# one that works on the path this ran on.

# 4. Point the hermes_otel plugin at the resolved collector and stamp the service name.
#
# Both values come from the operator's env. The endpoint matters because hermes_otel does
# NOT read OTEL_EXPORTER_OTLP_ENDPOINT — its backend URL is baked into the image, so
# without this sweep a customer-configured collector would show up in the pod env and in
# .status.telemetry while every span still went to the GKE managed collector.
#
# Every profile carries its own copy of the plugin config (profile_scaffold copytrees
# /opt/defaults/plugins), so otel_config sweeps them all, deriving each from the pristine
# image copy. Profiles scaffolded later — the cluster agents — are handled by
# cluster_agent_profile.py at onboarding time. Never fatal: see otel_config.py.
#
# PRIMARY ONLY. These files are shared through the PVC but service.name is per-container,
# and a container that reaches this step with no OTEL_SERVICE_NAME clears the attribute for
# everyone — turning the agent's service.name into an empty resource_attributes map, which
# is what the deployed pod was observed doing back when the dashboard sidecar still got
# this far. The step-1.5 gate now stops that container much earlier; this guard is what
# keeps a non-primary owner (an HA replica) from repeating the damage.
if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$TARGET_DIR/scripts/otel_config.py" ]; then
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" "$TARGET_DIR/scripts/otel_config.py" \
        --hermes-home "$TARGET_DIR" \
        --service-name "${OTEL_SERVICE_NAME:-}" \
        --endpoint "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" \
        --defaults-plugins /opt/defaults/plugins \
        || echo "WARN: could not update the OpenTelemetry plugin config; traces may go to the image default" >&2
fi

# 4a. Compat symlink. Unlike step 4 this runs in every container that gets here: $HOME can
# differ between the gateway and a sidecar, so the link is per-container, and relinking an
# existing one is a no-op.
if [ -f "$TARGET_DIR/plugins/hermes_otel/config.yaml" ] && [ -w "$TARGET_DIR/plugins/hermes_otel/config.yaml" ]; then
    # hermes-otel resolves config below ~/.hermes even when HERMES_HOME points
    # elsewhere. Expose the generated config at both locations.
    OTEL_CONFIG="$TARGET_DIR/plugins/hermes_otel/config.yaml"
    OTEL_COMPAT_CONFIG="$HOME/.hermes/plugins/hermes_otel/config.yaml"
    mkdir -p "$(dirname "$OTEL_COMPAT_CONFIG")"
    if [ ! "$OTEL_CONFIG" -ef "$OTEL_COMPAT_CONFIG" ]; then
        ln -sf "$OTEL_CONFIG" "$OTEL_COMPAT_CONFIG"
    fi
fi

# Everything that writes shared volume state is done; release the bootstrap lock
# before starting anything long-lived, so a peer container is not blocked behind
# this one for the life of the pod. Closing the fd releases it too, but do both
# explicitly — the fd is inherited across the `exec` in step 6 otherwise.
if [ -n "$BOOTSTRAP_LOCK_FD" ]; then
    flock -u 9 2>/dev/null || true
    exec 9>&-
fi

# 4.5 The scratch directory scripts stage files in — the fleet audit's findings
# JSON, and the `gh --body-file` payloads github_scan_gate._post_body hands to
# the credential sidecar. The audit's own bodies no longer come through here:
# they travel on stdin (audit_report.BODY_STDIN), which is where the remaining
# body-file caller should end up too.
# Created HERE, deterministically and under this script's umask-0002 discipline
# (the header comment on the #955 UID split), rather than lazily by whichever
# process reaches it first with whatever umask it happens to carry: the sandbox
# (uid 10000) writes these files and the sidecar (uid 10001) reads them through
# the shared fsGroup, which only works if the directory is group-accessible.
# The chmod also repairs a long-lived PVC where a pre-#955 process already
# created it too tight; guarded because an already-correct dir owned by the
# peer uid may refuse the chmod, and that is fine.
mkdir -p "$TARGET_DIR/scratch"
chmod 0775 "$TARGET_DIR/scratch" 2>/dev/null || true

# 5. Start background microservices (FastAPI proxy)
#
# Primary only: this binds a fixed port in the pod's shared network namespace,
# and the sidecar's copy lost the race with `[Errno 98] address already in use`
# every boot while both wrote the same log file, interleaved. The port is what
# both containers reach it on, so one server serves the pod.
mkdir -p "$TARGET_DIR/logs"
if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$TARGET_DIR/scripts/session_kv_server.py" ]; then
    echo "Starting Session KV server on port 8699..."
    # Bound to loopback, not 0.0.0.0. Every caller — this container's MCP
    # server, incident_context plugin, and kanban notifier (the gateway this
    # script execs below), and the event watcher in the credential-proxy
    # container — reaches it over the shared pod network namespace, so nothing
    # needs it published on the pod IP. It carries chat identifiers, so the
    # narrower bind is the correct default.
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" -m uvicorn scripts.session_kv_server:app --app-dir "$TARGET_DIR" --host 127.0.0.1 --port 8699 >"$TARGET_DIR/logs/session_kv_server.log" 2>&1 &
fi

# 5.5. The default kubectl context is NOT established here. There is no `gcloud`
# in this container at all as of #737 — not the binary and not the shim — and
# before that the shim would have run get-credentials in the sidecar and written
# the sidecar's kubeconfig, not ours. The sidecar bootstraps its own context from
# CREDENTIAL_PROXY_BOOTSTRAP_COMMAND (see buildCredentialProxyEnv in the
# operator), which runs inside the workspace root before the proxy serves any
# request. The k8s-event-watcher does not need a copy either: it runs inside the
# credential-proxy container, not this one.

# 5.6. Migrate any file-based memory store into Hindsight.
#
# A volume that predates the Hindsight provider still has its memory sitting in
# Markdown. The new provider never reads those files, so without this the day the
# image rolls is the day everything the agent had learned goes dark while staying
# perfectly intact on disk — neither reachable nor gone, and nobody notices until
# a question that used to work stops working.
#
# Backgrounded, because it waits on Hindsight and on LLM extraction and must not
# hold up readiness; non-fatal, because a failed migration leaves every file
# exactly where it was and the next start tries again. The script is idempotent
# and exits immediately when there is nothing to move, which is every start after
# the one that moved it. See the script's own docstring for how deletion is gated
# on verification.
#
# Deliberately unlocked: two pods briefly sharing the volume during a rollout
# could at worst retain an entry twice, which consolidation absorbs, whereas a
# lock file outliving a SIGKILL would skip the migration permanently and
# silently.
#
# Gated on the chosen provider, because this is the one step here that is not
# reversible: it moves the Markdown into the provider and unlinks the original.
# It used to be gated on hindsight/config.json existing, but that file is
# image-owned and therefore always present, so an install that had deliberately
# kept the file-based store still had it taken away. MEMORY_PROVIDER comes from
# the operator (see buildDeployment); an empty value is a real answer — "no
# provider" — while an *unset* one means an operator too old to send it, where
# the old file-presence behaviour is the safe reading.
memory_import_wanted() {
    # ${VAR+x} is "x" when VAR is set to anything at all, including the empty
    # string, and "" only when it is unset — which is the distinction that
    # matters here and that a plain -z test would collapse.
    if [ -z "${MEMORY_PROVIDER+x}" ]; then
        [ -f "$TARGET_DIR/hindsight/config.json" ]
        return
    fi
    case "$MEMORY_PROVIDER" in
        kube_agents_memory | hindsight) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -f "$TARGET_DIR/scripts/memory_file_import.py" ] && memory_import_wanted; then
    echo "Checking for a file-based memory store to migrate..."
    (
        HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
            "$TARGET_DIR/scripts/memory_file_import.py" \
            >>"$TARGET_DIR/logs/memory_file_import.log" 2>&1 \
            || echo "WARN: file-memory migration did not complete; the store is untouched and the next start will retry (see logs/memory_file_import.log)" >&2
    ) &
fi

# 5.7. Mirror the profile layout into the shell sandbox, and move the model's
# existing files across the first time.
#
# The shell, the file tools and execute_code all run in the sandbox pod now, so
# two things that used to be true stopped being true on the day that landed.
# Paths under $TARGET_DIR/profiles/<name> no longer resolve for a file tool —
# the sandbox has the machine home and nothing below it — and everything the
# model had already written to $TARGET_DIR became invisible to it while staying
# on the volume. The script's docstring covers what crosses and what does not.
#
# Here rather than in the sandbox's own entrypoint because the profile list is
# on this pod's PVC and cluster profiles are created at runtime; the sandbox
# cannot enumerate what it has never seen. cluster_agent_profile.py calls the
# same script with --skeleton-only when it scaffolds one, so a profile created
# between restarts does not wait for the next one.
#
# Foreground, and fatal on EXIT_FATAL only. It was neither, and the reason it
# was both is sound as far as it goes: the sandbox is a separate StatefulSet
# with no start ordering against this Deployment, so "not up yet" is an ordinary
# outcome rather than an error. What that misses is that the script already
# draws the distinction. A sandbox that never answers inside --wait returns 0
# and leaves the marker unwritten, so the next start retries; the fatal exits
# are a --remote-root that is not the sandbox's volume, and a transfer that ran
# and failed. Neither is survivable and neither is transient, so backgrounding
# them bought nothing but silence: the agent came up healthy, the model's files
# were still on this volume where its shell can no longer see them, and the sole
# trace was a WARN line in a log file nobody reads. That happened on a live
# upgrade — ten cluster profile homes, every one Permission denied — and it was
# found by hand, days later.
#
# Fatal on *those* and not on everything non-zero, which is the correction. The
# layout push and the marker write both reach a filesystem uid 1000 owns, so the
# model can make either fail — and while any non-zero exit landed here, that was
# a way for a prompt injection to CrashLoopBackOff this container permanently,
# with no path back because the thing that would repair it is the thing that is
# down. Those two now return EXIT_RETRY and warn instead.
#
# Exiting non-zero here is what makes it visible, and it needs no privilege the
# agent does not have. The kubelet restarts the container, CrashLoopBackOff
# follows, and getDeploymentStatusDetails in the operator already scans the
# gateway pod's container statuses for a waiting reason and writes it into the
# CR: phase Degraded, Ready=False, and a message naming this container. The
# tail below puts the cause in `kubectl logs`, which is where the CR sends you.
#
# The cost is startup latency in the one case that blocks — a sandbox that is
# slow rather than absent — bounded by --wait at 180s. The startupProbe budget
# is agentAPIProbe(10, 60), 600s, and progressDeadlineSeconds is 1200, so the
# wait fits with room over. The copy itself runs once: it is guarded by a marker
# on the sandbox's own volume, so a fresh sandbox PVC gets a fresh copy and every
# later start skips it and re-pushes only the layout.
#
# Gated on IS_BOOTSTRAP_PRIMARY: every replica can reach the sandbox, and two
# tar streams extracting into the same directory would race. --skip-old-files
# makes that lossless rather than harmful, but there is no reason to run it
# twice.
SANDBOX_MIRROR_SCRIPT="/opt/defaults/scripts/sandbox_mirror.py"
[ -f "$SANDBOX_MIRROR_SCRIPT" ] || SANDBOX_MIRROR_SCRIPT="$TARGET_DIR/scripts/sandbox_mirror.py"
if [ "$IS_BOOTSTRAP_PRIMARY" = "1" ] && [ -f "$SANDBOX_MIRROR_SCRIPT" ]; then
    echo "Mirroring the profile layout into the shell sandbox..."
    SANDBOX_MIRROR_RC=0
    HERMES_HOME="$TARGET_DIR" "$INSTALL_DIR/.venv/bin/python3" \
        "$SANDBOX_MIRROR_SCRIPT" --agent-home "$TARGET_DIR" \
        >>"$TARGET_DIR/logs/sandbox_mirror.log" 2>&1 || SANDBOX_MIRROR_RC=$?
    if [ "$SANDBOX_MIRROR_RC" = "$SANDBOX_MIRROR_RETRY_RC" ]; then
        echo "WARN: the shell sandbox mirror did not finish, in a way the next start retries. Nothing has been lost -- the copy either has not run or has already landed. Last lines of logs/sandbox_mirror.log:" >&2
        tail -n 20 "$TARGET_DIR/logs/sandbox_mirror.log" >&2 || true
    elif [ "$SANDBOX_MIRROR_RC" != "0" ]; then
        echo "FATAL: the shell sandbox migration failed. The model's files are still on this pod's volume, where its shell cannot reach them, so this container is refusing to start rather than come up looking healthy with the agent's work missing. Last lines of logs/sandbox_mirror.log:" >&2
        tail -n 20 "$TARGET_DIR/logs/sandbox_mirror.log" >&2 || true
        exit 1
    fi
fi

# 6. Execute primary process from inside the agent's own directory.
#
# The image inherits WORKDIR /opt/hermes from the upstream base, which is
# read-only to the runtime user, so a process left there writes every relative
# path into a directory it cannot write and loses every one it can. $TARGET_DIR
# is the writable home on the PVC.
#
# This used to be a credential rule as well: every CLI in this container was a
# credential-proxy shim, the client posted `"cwd": os.getcwd()` on every
# request, and the proxy refused any cwd outside its workspace root — so a plain
# `git status` launched from /opt/hermes failed before it ran. #737 took the
# shims out of this image entirely, so what is left is the write-path reason
# above, which is enough on its own.
#
# The cwd is the only lever that reaches every caller. Hermes resolves the
# terminal and execute_code working directories from a ladder that ends at
# os.getcwd(), and for the `local` backend the CLI *overwrites* any configured
# terminal.cwd with os.getcwd() outright ("Local backend: always os.getcwd().
# Use `cd /dir && hermes` to control it."), so a config key cannot fix this —
# and kanban workers, which the dispatcher spawns as child processes, inherit
# whatever cwd the agent was started with.
#
# Guarded rather than unconditional: `set -e` would abort the container on a
# missing directory, and a shell whose cd fails silently continues in the old
# one. $TARGET_DIR is created in step 2 and written throughout, so the warning
# is a canary for a broken mount rather than an expected path.
if ! cd "$TARGET_DIR"; then
    echo "WARN: could not enter $TARGET_DIR; relative paths will resolve against a read-only directory" >&2
fi

exec "$@"
