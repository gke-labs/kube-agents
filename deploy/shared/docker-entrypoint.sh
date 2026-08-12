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
    # can reach its first read while $TARGET_DIR is still empty. Its config.yaml is the
    # one thing always present — the operator mounts the rendered ConfigMap over that
    # path, the same file it gives the gateway — but that config names
    # scripts/router_server.py and a plugins.enabled list only the owner lands, moments
    # later. The container carries no probes, so the failure mode is a restart or two
    # against the kubelet's backoff until the tree appears, not a wedge.
    #
    # KNOWN LIMIT, deliberately accepted rather than fixed here: that ordering is the
    # kubelet's to lose. Moving this setup into an initContainer — one carrying the plugin
    # volumes and the overlay ConfigMap, running to completion, leaving every app
    # container on `skip` — is what would turn it into an ordering the pod spec states
    # instead of one it happens to get. It is only the WRITES below that have to belong to
    # one container; the reads merely have to survive being early.
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

# 2. Sync default agent files and subdirectories (plugins, SOUL.md, AGENTS.md, procedures, cron, scripts, governance)
if [ -d "/opt/defaults" ]; then
    mkdir -p "$TARGET_DIR"
    cp -ru /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || cp -rp /opt/defaults/. "$TARGET_DIR/" 2>/dev/null || true
fi

# 2a. Force-sync the image-managed default-profile files so they ALWAYS track the
# image, not the persistent PVC. The update-only copy above (cp -u) can skip
# config.yaml, and on a long-lived volume it eventually always does. `cp` without
# -p stamps the destination with the time of the copy, so the moment step 2 lands
# config.yaml the PVC copy is NEWER than the image file it came from, and every
# subsequent boot's cp -u declines to overwrite it — leaving a stale
# toolset/persona config live across the image roll that was supposed to replace
# it. Step 2a-bis re-stamps it again on every operator-managed start. (Rollback to
# an older image and builds with deterministic file timestamps get there by the
# other direction; step 2b describes that pair for the shared scripts.) These
# files are image-owned (not runtime state), so overwrite them unconditionally.
if [ -d "/opt/defaults" ]; then
    for f in config.yaml SOUL.md AGENTS.md CAPABILITIES.md; do
        [ -f "/opt/defaults/$f" ] && cp -f "/opt/defaults/$f" "$TARGET_DIR/$f" 2>/dev/null || true
    done
fi

# 2a-bis. The operator owns the default profile's config.yaml, and delivers it two
# ways: as a subPath mount straight onto $TARGET_DIR/config.yaml, and as part of the
# whole-ConfigMap directory mount at /opt/agent-config. The subPath is not reliable —
# on a first boot against a brand-new PVC kubelet does not establish it (its sibling
# subPath from the same volume, leader_elect.py, mounts fine), and step 2a above then
# leaves the image default live. The agent starts with no `platforms.slack` entry, no
# Slack consumer runs, and chat is silently dead while every health check passes.
#
# So take config.yaml from the directory mount, which is a plain projected volume and
# always present. When the subPath *did* mount, this copy fails with EBUSY against the
# mount point and the already-correct content stays — hence the `|| true`, which is a
# no-op rather than a fallback. This touches only the default profile's config.yaml;
# the cluster profiles are identity-stamped at scaffold time and are synced separately
# below, so they are unaffected.
if [ -f "/opt/agent-config/config.yaml" ]; then
    cp -f "/opt/agent-config/config.yaml" "$TARGET_DIR/config.yaml" 2>/dev/null || true
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

# 2c. Reconcile the image's cron jobs into the running agent's job file.
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
#     profiles/platform/config.yaml at runtime — step 2.7's overlay merge is the
#     one exception, and it runs after this on purpose. Without syncing it, an
#     image that changes the platform's toolsets or plugins has no effect on any
#     existing deployment.
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
    # every replica the SAME PVC (ReadWriteMany; see step 2c and cron_jobs_sync.py's
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

# 3. (removed) Enabling hermes_otel in the default profile's config.yaml.
#
# The step appended `hermes_otel` to plugins.enabled if it was missing, guarded on the file
# being writable. It could not fire, and had nothing to do if it did:
#
#   - Under the operator that path is a ConfigMap subPath mount, and ConfigMap volumes are
#     mounted read-only whatever the mount's readOnly field says, so `[ -w ]` was false in
#     both the gateway and the dashboard. The mode is 0400/0755 on root-owned files against
#     RunAsUser 10000 besides (buildDefaultVolumeMounts and the volume's DefaultMode in
#     platformagent_manifests.go), so it fails the ownership test as well as the mount one.
#   - The content was already there either way. `hermes_otel` heads plugins.enabled in
#     agents/chat/config.yaml, which the image installs as /opt/defaults/config.yaml, and it
#     is in the operator's DefaultBuiltInPlugins, so it is in the rendered ConfigMap too.
#
# Where the guard DID pass — compose, `docker run`, or the fresh-PVC boot where the subPath
# fails to establish and step 2a-bis leaves a plain file behind — the step found the plugin
# already enabled, appended nothing, and rewrote the file anyway: yaml.safe_dump round-trips
# it, so it sorted the keys and dropped every comment in a config people read.
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
if [ -f "$TARGET_DIR/scripts/otel_config.py" ]; then
    PYTHONPATH="$TARGET_DIR/scripts" "$INSTALL_DIR/.venv/bin/python3" "$TARGET_DIR/scripts/otel_config.py" \
        --hermes-home "$TARGET_DIR" \
        --service-name "${OTEL_SERVICE_NAME:-}" \
        --endpoint "${OTEL_EXPORTER_OTLP_ENDPOINT:-}" \
        --defaults-plugins /opt/defaults/plugins \
        || echo "WARN: could not update the OpenTelemetry plugin config; traces may go to the image default" >&2
fi

# 4a. Compat symlink, unchanged.
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
# request. The k8s-event-watcher does not need a copy either: it runs inside the
# credential-proxy container, not this one.

# 6. Execute primary process
exec "$@"
