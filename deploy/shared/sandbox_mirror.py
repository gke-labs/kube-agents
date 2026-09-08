#!/usr/bin/env python3
"""Give the shell sandbox the same directory layout as the agent pod, and move
the model's existing files across once.

Two problems, one script, because both need the same three things: the list of
Hermes profiles, the SSH key, and a connection to the sandbox. Only the agent
pod has all three.

**Layout.** The sandbox mounts its own PVC at the same absolute path as the
agent pod's Hermes home, so a SOP that says ``/opt/data/scratch/report.md``
resolves on both sides. That holds for the machine home, which the sandbox
image creates, and stops holding one level down: ``/opt/data/profiles/platform``
exists on the agent pod and the sandbox has never heard of it. A file tool
reading ``/opt/data/profiles/platform/governance/inventory_prioritize_sop.md``
runs over SSH now, and it fails there. The sandbox cannot fix this on its own —
the profile list lives on the agent pod's PVC, and cluster profiles are created
at runtime — so the agent pod pushes the skeleton in.

**Migration.** Before the shell moved, the model's work landed on the agent
pod's PVC: ``scratch`` is 91 MB of it on the install this was written against,
plus ``gitops`` clones and a scattering of directories it invented. After the
move those files are still there and the model can no longer see them, which
from a user's point of view is an upgrade that deleted their work. This copies
them over once.

Neither step is fatal on its own. The sandbox is a separate pod with no ordering
against this one, so "not up yet" is an ordinary outcome; both steps are
idempotent and the next container start retries. One thing is fatal, and the
exit codes below say why it is the only one: a copy that ran and failed. That
holds the agent pod down, because coming up healthy with the model's files
stranded is the failure this script exists to prevent.

What does *not* come across, and why the list below is a denylist:

  - Hermes' own runtime state — the session and kanban databases, the caches,
    the plugin trees, the venvs. None of it is reachable from a shell and a
    second stale copy of it in a pod that cannot use it is a trap for whoever
    reads the directory next.
  - Anything holding a credential: the profile ``.env`` files, ``.ssh``,
    ``.kubeconfigs``, ``kubeconfig.yaml``. The sandbox exists so that code the
    model runs cannot reach these.
  - The trees the sandbox image already delivers — ``skills``, ``governance``,
    ``scripts``. The sandbox entrypoint replaces those from ``/opt/defaults`` on
    every start, so copying the agent pod's copies over them would be undone at
    the next restart at best and would shadow a newer image at worst.

A denylist rather than an allowlist because of which way each one fails. An
allowlist of ``scratch`` and ``gitops`` would have silently dropped ``infra``,
``infra-repo``, ``infra_repo`` and ``work-d0452361`` on the install this was
written against — model-created working directories that no instruction names,
which is exactly the "user lost their files" case. A denylist that misses
something copies a directory nobody needed, which costs disk and is visible in
the log this writes.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The managed scope. The operator renders the terminal block here and the agent
# cannot override it, so it is also the authoritative answer to "is there a
# sandbox at all": no ssh backend, nothing to mirror.
MANAGED_CONFIG_PATH = "/etc/hermes/config.yaml"

# Written by the sandbox entrypoint at the root of its own volume. Checked
# before anything is written, because the failure this guards against is
# quiet: point --remote-root at a path the sandbox does not use and every
# mkdir succeeds, the tar extracts, and the model sees none of it.
SANDBOX_MARKER = ".sandbox"

# Written on the sandbox once the copy has run. On the sandbox rather than the
# agent pod so that a fresh sandbox volume gets a fresh migration, which is the
# case that matters: the agent pod's PVC outlives several sandbox PVCs.
MIGRATION_MARKER = ".sandbox-migrated"

# The working directories every home gets, whether or not the agent pod has
# them yet. Cheap, and it means a skill can write to $HERMES_HOME/artifacts
# without a mkdir -p first, which is how it behaved before the shell moved.
SKELETON_DIRS = ("artifacts", "gitops", "plans", "scratch", "tmp", "workspace")

# What this script returns, and what the agent pod's entrypoint does with it.
#
# Almost everything is EXIT_RETRY, because the next container start fixes it on
# its own. That includes a copy that ran and failed. `transfer` reads the agent
# pod's home and never removes from it, so a tar that dies halfway leaves every
# byte where it was and leaves MIGRATION_MARKER unwritten; the next start runs
# the copy again and `--skip-old-files` keeps whatever already landed.
#
# The dividing line is not "how bad does this look" but "who can make it
# happen". Everything below /opt/data on the sandbox is owned by uid 1000, so a
# failure the model can provoke -- a layout the push cannot handle, a marker it
# deleted -- must not be fatal, or a prompt injection stops the agent for good
# and the repair needs the agent that is no longer running. Nothing has been
# copied at any of those points, so nothing is lost by coming up without them.
#
# EXIT_FATAL holds the gateway container down, and it is left for what running
# the script again cannot fix: no tar on PATH, and an unhandled exception,
# which exits 1 on its own. The second is the conservative way round -- an
# unknown failure is a state nobody has reasoned about, and holding the
# container down is the answer that does not continue silently past it.
EXIT_OK = 0
EXIT_FATAL = 1
EXIT_RETRY = 2

# Where a non-directory sitting on a skeleton path is moved to. Renamed rather
# than deleted: `scratch` as a regular file is broken state whichever way it got
# there, but it is the model's own byte and this is not the code that should
# decide it is worthless. The stamp keeps two runs from colliding and leaves a
# reader something to sort by.
DISPLACED_SUFFIX = ".displaced"
DISPLACED_STAMP_FORMAT = "%Y%m%dT%H%M%S"

# One `sh` loop rather than one `mkdir -p` over every target, because a plain
# mkdir -p is the whole vulnerability: it returns 1 when any target exists as
# something other than a directory, the failure is permanent, and it used to
# take the gateway with it. Permanent because nothing on the sandbox side
# reaches a skeleton path -- that entrypoint displaces a non-directory at a
# *home root*, where its own `install -d` would trip on one, and rewrites only
# the trees it ships in /opt/defaults. A plain file at `scratch` survives every
# recycle. Symlinks are displaced here too, including one pointing at a
# directory, which `[ -d ]` alone would accept.
SKELETON_SHELL = """
for target in {targets}; do
  displace=0
  if [ -L "$target" ]; then
    displace=1
  elif [ -e "$target" ] && [ ! -d "$target" ]; then
    displace=1
  fi
  if [ "$displace" = 1 ]; then
    mv -f "$target" "$target"{suffix} || exit 1
    echo "displaced $target"
  fi
  mkdir -p "$target" || exit 1
done
"""

# Hermes runtime state. Reachable in-process from the agent pod and from
# nowhere else; a copy in the sandbox is dead weight that reads as live.
HERMES_RUNTIME = frozenset(
    {
        "audio_cache",
        "backups",
        "bin",
        "cache",
        "channel_directory.json",
        "config",
        "cron",
        "event-watcher",
        "gateway",
        "gateway_state.json",
        "google_chat_thread_counts.json",
        "hindsight",
        "hook_outputs",
        "hooks",
        "image_cache",
        "kanban",
        "lazy-packages",
        "logs",
        "lost+found",
        "lsp",
        "memories",
        "onboarding",
        "pairing",
        "pending_messages",
        "platforms",
        "plugins",
        "processes.json",
        "sandboxes",
        "sessions",
        "skins",
        "state",
        "venv-yaml",
        "__pycache__",
    }
)

# $HOME, not a working directory, whatever its name suggests. The agent
# entrypoint points the process's HOME at $HERMES_HOME/home (step 4a), so what
# accumulates there is what any Unix process puts in a home directory: on the
# install this was written against, 831 MiB of pip and gcloud cache under
# .cache, 46 MiB of kubeconfigs under .kube, gcloud's own credentials under
# .config, and MCP auth tokens under .mcp-auth. Copying it would put a
# kubeconfig in the pod the sandbox exists to keep credentials out of, and
# would spend a fifth of the sandbox's volume on caches for a Python that is
# not installed there. The sandbox has its own $HOME at /home/agent.
#
# The cost is real and is logged rather than hidden: a handful of one-off
# scripts the model wrote straight into $HOME stay on the agent pod's volume.
PROCESS_HOME = "home"

# Staged at /opt/defaults in the sandbox image and copied onto the volume by
# its entrypoint on every start. Copying the agent pod's would overwrite a
# current tree with an older one, and the sandbox's copy is the one the shell
# reads. Keep this in step with what deploy/sandbox/Dockerfile actually stages;
# the sandbox delivers these three and nothing else.
IMAGE_OWNED = frozenset({"governance", "scripts", "skills"})

# Also withheld, but for the opposite reason: nothing delivers these to the
# sandbox and nothing there should read them. The persona files are read into
# the prompt in the agent pod and never through the shell, which is what makes
# the agent unable to edit its own persona — see the design doc's "the persona
# stays behind, and that is a property rather than an omission". SETTINGS.md is
# per-install and reaches the sandbox as a ConfigMap mount instead, so the
# agent pod's copy must not land on top of it. The rest is Hermes' own
# profile plumbing.
AGENT_POD_ONLY = frozenset(
    {
        "AGENTS.md",
        "CAPABILITIES.md",
        "SETTINGS.md",
        "SOUL.md",
        "USER.md",
        "docs",
        "leader_elect.py",
        "profile.yaml",
    }
)

# Credentials, and the files that name them. Nothing here crosses, ever.
# gke_gcloud_auth_plugin_cache is a file rather than the directory its name
# suggests, and it holds a cached GKE access token.
CREDENTIALS = frozenset(
    {"auth.lock", "gke_gcloud_auth_plugin_cache", "kubeconfig.yaml"}
)

# The one entry above the sandbox needs a copy of, and only inside a Cluster
# Agent's profile. cluster_preflight.sh reads the project, cluster and location
# out of it, and it runs in the sandbox like every other command the agent
# issues -- so with the file left behind, its check 1 reports that the Cluster
# Agent has no identity, on every profile, forever. Pushed by
# push_cluster_identities rather than by dropping the name from AGENT_POD_ONLY,
# which would carry the default profile's USER.md across as well: that one is
# persona, and the persona stays behind.
#
# The copy is on a volume uid 1000 owns, so the model can rewrite it. That does
# not make preflight weaker than it was: the kubeconfig it checks the identity
# against sits on the same volume. Preflight tells a broken scaffold from a
# working one; it is not a control against the model, and never was.
CLUSTER_IDENTITY_FILE = "USER.md"

# What cluster_agent_profile.profile_name() prefixes every Cluster Agent profile
# with. A profile named anything else is not one, and its USER.md stays where
# AGENT_POD_ONLY puts it.
CLUSTER_PROFILE_PREFIX = "cluster-"

# Overwrites, unlike the tar in transfer(): a re-scaffolded profile's identity
# has to replace the old one, and a disagreement between the two is exactly the
# case where the stale copy is the wrong answer. `set -e` so a failed write
# stops the batch instead of leaving the exit status to whichever write ran
# last. printf with the content as an argument, never as the format string.
IDENTITY_WRITE_SHELL = "printf '%s' {content} > {path}"

# Handled by recursion rather than excluded: each profile home is walked with
# the same rules and its contents land under profiles/<name>/ on the far side.
PROFILES_DIR = "profiles"

# Matched against the entry name. Databases and their write-ahead logs, the
# config files the operator and the image own, and the model-provider caches.
EXCLUDE_GLOBS = (
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "*.db.*",
    "config.yaml",
    "config.yaml.*",
    "*_cache.json",
    "*.lock",
    "*.log",
    "*.log.*",
    "*.pid",
    "gateway-*",
)

# The rules above decide whether a top-level entry is copied at all. These
# decide what is dropped from *inside* one that is, and they exist because the
# first live run of this script copied /opt/data/tmp wholesale and carried a
# cached GKE access token across in tmp/gke_gcloud_auth_plugin_cache, along with
# a tmp/.kube. A directory the model owns is a directory the model has been
# running gcloud and kubectl inside, so its credential files land wherever
# $HOME or $KUBECONFIG happened to point at the time; no top-level rule can see
# them.
#
# These go to tar as --exclude patterns, which GNU tar matches unanchored
# against every member name: --exclude=.kube drops tmp/.kube as readily as
# .kube. That is the property being relied on, so the entries are bare names
# rather than paths.
RECURSIVE_EXCLUDES = (
    ".kube",
    ".kubeconfigs",
    "kubeconfig.yaml",
    "gke_gcloud_auth_plugin_cache",
    "application_default_credentials.json",
    ".config/gcloud",
    ".gsutil",
    ".mcp-auth",
    ".env",
    ".envrc",
    ".netrc",
    ".git-credentials",
    ".ssh",
    ".docker",
    ".npmrc",
    ".pypirc",
    "id_rsa",
    "id_ed25519",
    "*.pem",
    "*.p12",
    "service-account*.json",
    # Not credentials, but the same argument in reverse: no reason to spend the
    # sandbox's 5 GiB on a nested cache or a checked-out .git object store.
    ".cache",
    "__pycache__",
    ".venv",
    "node_modules",
)

# Everything starting with a dot. Every dotfile at the root of a Hermes home is
# either state (.bootstrap_completed, .mcp-discovery.lock), a credential (.env,
# .ssh, .kubeconfigs) or a snapshot Hermes rebuilds. The model is told to work
# in scratch and gitops and does not write dotfiles at a home root; treating the
# whole class as excluded is one rule instead of fifteen, and it is the rule
# that keeps .env out.
#
# The one cost is a dotfile the model *did* write at a home root, which this
# leaves behind. That is logged as skipped rather than dropped silently.

# No byte cap by default. The sandbox's volume is sized from the agent's
# (agentDataStorageSize in the operator), and what crosses is a subset of the
# agent's volume, so the copy fits by construction. A fixed cap here could only
# do one thing the free-space floor below does not: silently truncate a migration
# on an install whose working directories are larger than the guess, which is the
# "upgraded and lost my files" outcome this whole path exists to prevent.
DEFAULT_MAX_BYTES = 0  # 0 means unbounded; --max-bytes still overrides

# The floor stays, and does the real work. Without it tar streams until ENOSPC,
# which leaves a truncated file at the cut point *and* a volume at 100% — and
# everything in the sandbox pod needs to write there: sshd, the shell's scratch,
# the credential proxy's workspace. A full volume is a broken sandbox, which is
# worse than a skipped directory.
FREE_SPACE_HEADROOM = 512 * 1024 * 1024  # leave this much on the sandbox volume

# What the terminal block leaves unset. The port and the account are sshd's own
# defaults in `deploy/sandbox/Dockerfile`; an operator that publishes neither is
# describing that image.
DEFAULT_SSH_PORT = 22
DEFAULT_SSH_USER = "agent"

# Long enough to open a connection to a pod that is up, short enough that a pod
# that is not does not hold the agent's own start behind it. The retry loop
# behind `--wait` is what covers a sandbox still being scheduled.
CONNECT_TIMEOUT_SECONDS = 10

# Keepalives on the established session, matching sandbox_exec.py. 45 seconds
# of silence from a pod that was evicted mid-transfer ends the call instead of
# leaving it to the far side's TCP.
SERVER_ALIVE_INTERVAL_SECONDS = 15
SERVER_ALIVE_COUNT_MAX = 3

# The ceiling on any single ssh call, and the reason it exists is the far side
# rather than the network. sshd runs the login shell for a non-interactive
# command too, so it sources ~/.bashrc -- a file the sandbox image deliberately
# leaves writable by the model. A `sleep infinity` at the top of it makes every
# call here hang forever, and this script runs in the gateway's entrypoint
# before `exec "$@"`, so the hang is the whole agent, permanently, across
# restarts. Neither ConnectTimeout nor the keepalives above cover it: the
# connection is healthy and the command is simply not returning. Generous
# because a real mirror of a large profile tree legitimately takes minutes.
SSH_CALL_TIMEOUT_SECONDS = 600

# The shorter ceiling for the liveness probe, which runs `true` and nothing
# else. A probe that takes longer than a connection setup is not a slow
# transfer, it is the hang above, and the retry loop has its own deadline.
SSH_PROBE_TIMEOUT_SECONDS = 30

# How long `--wait` waits by default: three minutes, which covers a sandbox
# StatefulSet pulling its image on a cold node.
DEFAULT_WAIT_SECONDS = 180

# `df -Pk` prints Filesystem, 1024-blocks, Used, Available, Capacity, Mounted —
# POSIX-mandated, one row per filesystem, which is why the caller takes the last
# line and this column of it.
DF_AVAILABLE_COLUMN = 3
DF_BLOCK_BYTES = 1024


def log(message: str) -> None:
    print(f"sandbox-mirror: {message}", flush=True)


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------


def read_terminal_config(path: str) -> dict | None:
    """The operator-rendered terminal block, or None if there is no sandbox.

    Parsed with PyYAML when it is importable — this runs from the gateway's
    venv, where it is — and the caller treats every failure the same way: no
    sandbox configured, nothing to do, exit 0.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - the agent venv always has it
        log("PyYAML is not importable; cannot read the managed terminal config")
        return None
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        log(f"{path} is not valid YAML: {exc}")
        return None
    terminal = parsed.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("backend") != "ssh":
        return None
    return terminal


def ssh_base_command(
    terminal: dict, connect_timeout: int = CONNECT_TIMEOUT_SECONDS
) -> list[str]:
    """The ssh argv every call below shares.

    Connects as ``terminal.ssh_user`` — ``agent``, uid 1000 — and not as the
    ``hermes`` principal that agent-pod code otherwise uses. This writes into
    /opt/data on the sandbox, which is agent-owned and which ``hermes`` cannot
    write; and the reason ``hermes`` exists at all does not apply here. That
    account keeps the model from forging the *result* of a trusted command. The
    payload here is the model's own files going to a directory the model owns,
    so the worst a shadowed binary on the far side buys it is a migration of
    its own work that it chose to break.
    """
    key = terminal.get("ssh_key") or ""
    host = terminal["ssh_host"]
    user = terminal.get("ssh_user") or DEFAULT_SSH_USER
    port = str(terminal.get("ssh_port") or DEFAULT_SSH_PORT)
    argv = [
        "ssh",
        "-p",
        port,
        # No user ssh config, the same rule sandbox_exec.py states for the same
        # connection. OpenSSH reads ~/.ssh/config from the passwd entry's home
        # directory and ignores $HOME, and this process's uid has /opt/data --
        # the gateway PVC -- as its pw_dir. A config file planted there could
        # redirect this connection or hand it a ProxyCommand, and it would run
        # during the entrypoint, before Hermes starts, in the pod that holds the
        # profile keys and the sandbox private key.
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        # Offer only the key named below. Without it ssh also tries whatever
        # agent or default identity it can find, which is how -F /dev/null gets
        # quietly undone by a key file that appears next to the config it just
        # stopped reading.
        "-o",
        "IdentitiesOnly=yes",
        # A connection to a pod that went away mid-transfer otherwise sits
        # half-open until the far side's TCP gives up. ConnectTimeout covers
        # only the handshake; these cover the session after it.
        "-o",
        f"ServerAliveInterval={SERVER_ALIVE_INTERVAL_SECONDS}",
        "-o",
        f"ServerAliveCountMax={SERVER_ALIVE_COUNT_MAX}",
        f"-o=ConnectTimeout={connect_timeout}",
    ]
    if key:
        argv += ["-i", key]
    argv.append(f"{user}@{host}")
    return argv


def wait_for_sandbox(ssh: list[str], deadline: float) -> bool:
    """Poll until the sandbox answers, or the deadline passes.

    A fixed interval rather than a backoff: the sandbox is either already up,
    in which case the first probe succeeds, or it is being scheduled, in which
    case the wait is dominated by the scheduler and the probe cost is noise.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            result = subprocess.run(
                ssh + ["true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=SSH_PROBE_TIMEOUT_SECONDS,
            )
            returncode = result.returncode
            stderr = result.stderr
        except subprocess.TimeoutExpired:
            # The connection succeeded and `true` did not return, which is the
            # far side's shell startup hanging rather than a sandbox that is
            # not up yet. Treated as a failed attempt so the deadline still
            # bounds the loop.
            returncode = 1
            stderr = (
                f"the sandbox accepted the connection but did not answer within "
                f"{SSH_PROBE_TIMEOUT_SECONDS}s"
            )
        if returncode == 0:
            if attempt > 1:
                log(f"sandbox answered on attempt {attempt}")
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log(
                "sandbox did not answer before the deadline "
                f"({stderr.strip() or 'no error output'}); "
                "the next container start will retry"
            )
            return False
        time.sleep(min(5.0, max(0.5, remaining)))


class RemoteTimeout(RuntimeError):
    """An ssh call connected and then never returned.

    Its own type because the caller answers it with EXIT_RETRY rather than
    EXIT_FATAL: the far side's shell startup is something a human can fix by
    recycling the sandbox pod, and holding the gateway down for it turns a
    broken shell into a broken agent.
    """


def remote(
    ssh: list[str],
    command: str,
    *,
    check: bool = True,
    timeout: int = SSH_CALL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ssh + ["--", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Not strict UTF-8. The far side's shell startup files are writable
            # by the model, so one non-UTF-8 byte echoed from ~/.bashrc would
            # otherwise raise UnicodeDecodeError out of the decode and take the
            # whole mirror down on every start.
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteTimeout(
            f"remote command did not return within {timeout}s: {command}"
        ) from exc
    if check and result.returncode != 0:
        raise RuntimeError(
            f"remote command failed ({result.returncode}): {command}\n{result.stderr.strip()}"
        )
    return result


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def home_relative_paths(agent_home: Path) -> list[str]:
    """Every Hermes home, as a path relative to the machine home.

    "" is the machine home itself. The rest are profiles/<name>, in the order
    the filesystem lists them, sorted so the log and the tests are stable.
    """
    homes = [""]
    profiles = agent_home / PROFILES_DIR
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                homes.append(f"{PROFILES_DIR}/{entry.name}")
    return homes


def push_skeleton(ssh: list[str], remote_root: str, homes: list[str]) -> None:
    """Make every home's working directories exist, whatever is there now.

    A single remote shell invocation rather than one per directory: nine homes
    times seven directories is sixty-three SSH round trips otherwise, on a path
    that runs on every container start.

    Targets are pushed parent-first, so a home root that has to be displaced is
    a directory again before its own skeleton is created inside it.
    """
    targets = []
    for home in homes:
        base = f"{remote_root}/{home}" if home else remote_root
        targets.append(base)
        targets.extend(f"{base}/{d}" for d in SKELETON_DIRS)
    suffix = f"{DISPLACED_SUFFIX}-{time.strftime(DISPLACED_STAMP_FORMAT)}"
    result = remote(
        ssh,
        SKELETON_SHELL.format(
            targets=" ".join(shlex.quote(t) for t in targets),
            suffix=shlex.quote(suffix),
        ),
    )
    for line in result.stdout.splitlines():
        if line.startswith("displaced "):
            path = line[len("displaced ") :]
            log(
                f"{path} was not a directory; moved it to {path}{suffix} and created "
                "the directory. Nothing reads the moved copy -- delete it once you "
                "know what put it there."
            )
    log(f"skeleton in place for {len(homes)} home(s): {', '.join(h or '<machine>' for h in homes)}")


def push_cluster_identities(
    ssh: list[str], agent_home: Path, remote_root: str, homes: list[str]
) -> None:
    """Put every Cluster Agent's identity file on the sandbox's volume.

    Runs alongside the skeleton, on every start and on every scaffold, because
    both are cases where the sandbox has a profile home and nothing in it: a
    recreated sandbox volume, a profile created after the last start, a
    re-scaffold that corrected an identity. Overwrites, for the last of those.

    One remote call for the whole set. Nine cluster profiles is nine SSH round
    trips otherwise, on a path that runs on every container start, and the
    files are a few hundred bytes each.

    A profile whose USER.md is missing is skipped without comment: the scaffold
    writes it last, so this legitimately runs before it exists (the scaffold's
    step 2e) and again after (its step 5).
    """
    writes: list[str] = []
    pushed: list[str] = []
    prefix = f"{PROFILES_DIR}/{CLUSTER_PROFILE_PREFIX}"
    for home in homes:
        if not home.startswith(prefix):
            continue
        try:
            content = (agent_home / home / CLUSTER_IDENTITY_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        writes.append(
            IDENTITY_WRITE_SHELL.format(
                content=shlex.quote(content),
                path=shlex.quote(f"{remote_root}/{home}/{CLUSTER_IDENTITY_FILE}"),
            )
        )
        pushed.append(home)
    if not writes:
        return
    remote(ssh, "\n".join(["set -e", *writes]))
    log(
        f"delivered {CLUSTER_IDENTITY_FILE} to {len(pushed)} cluster profile(s): "
        + ", ".join(pushed)
    )


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def is_excluded(name: str) -> str | None:
    """Why this entry stays on the agent pod, or None if it should cross."""
    if name.startswith("."):
        return "dotfile: Hermes state or a credential"
    if name == PROFILES_DIR:
        return "walked separately"
    if name == PROCESS_HOME:
        return "the process $HOME: caches and credentials, not a working directory"
    if name in CREDENTIALS:
        return "credential"
    if name in IMAGE_OWNED:
        return "delivered by the sandbox image"
    if name in AGENT_POD_ONLY:
        return "stays in the agent pod; nothing reads it through the shell"
    if name in HERMES_RUNTIME:
        return "Hermes runtime state"
    for pattern in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return f"matches {pattern}"
    return None


def migration_candidates(agent_home: Path, homes: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """(paths to copy, [(path, why it was skipped)]), both relative to the home.

    Skips are returned rather than logged here so the caller can report them in
    one block. A skipped entry that is not empty is worth a human's attention:
    it is the shape a missing denylist rule has.
    """
    include: list[str] = []
    skipped: list[tuple[str, str]] = []
    for home in homes:
        base = agent_home / home if home else agent_home
        try:
            entries = sorted(base.iterdir())
        except OSError as exc:
            log(f"cannot list {base}: {exc}")
            continue
        for entry in entries:
            rel = f"{home}/{entry.name}" if home else entry.name
            reason = is_excluded(entry.name)
            if reason:
                skipped.append((rel, reason))
                continue
            include.append(rel)
    return include, skipped


def recursively_excluded(rel: str) -> str | None:
    """The pattern from RECURSIVE_EXCLUDES that drops ``rel``, or None.

    GNU tar matches an unanchored ``--exclude`` against any run of whole name
    components, which is what lets a bare ``.kube`` drop ``tmp/.kube``. This
    reproduces that so the size estimate and the dry-run plan account for the
    same bytes the transfer moves; tar remains the thing that enforces it.
    """
    parts = rel.split("/")
    for pattern in RECURSIVE_EXCLUDES:
        depth = pattern.count("/") + 1
        for i in range(len(parts) - depth + 1):
            if fnmatch.fnmatch("/".join(parts[i : i + depth]), pattern):
                return pattern
    return None


def measure(agent_home: Path, paths: list[str]) -> dict[str, int]:
    """Bytes per top-level path, so the cap can drop the biggest first."""
    sizes: dict[str, int] = {}
    for rel in paths:
        target = agent_home / rel
        total = 0
        if target.is_dir() and not target.is_symlink():
            for root, dirs, files in os.walk(target, followlinks=False):
                # Member names as tar sees them: relative to the -C directory,
                # so a two-component pattern matches the same way there.
                base = os.path.relpath(root, agent_home)
                dirs[:] = [
                    d
                    for d in dirs
                    if not os.path.islink(os.path.join(root, d))
                    and not recursively_excluded(f"{base}/{d}")
                ]
                for name in files:
                    if recursively_excluded(f"{base}/{name}"):
                        continue
                    try:
                        total += os.lstat(os.path.join(root, name)).st_size
                    except OSError:
                        pass
        else:
            try:
                total = target.lstat().st_size
            except OSError:
                total = 0
        sizes[rel] = total
    return sizes


def effective_budget(max_bytes: int, free: int | None) -> int | None:
    """The byte ceiling for this copy, or None when nothing bounds it.

    ``--max-bytes`` is an escape hatch and defaults to 0, meaning no cap of its
    own — see DEFAULT_MAX_BYTES. The sandbox's free space less
    FREE_SPACE_HEADROOM applies whenever it can be read, and it is the bound that
    matters: it stops the copy filling the volume it is writing into.
    """
    limits = []
    if max_bytes > 0:
        limits.append(max_bytes)
    if free is not None:
        limits.append(max(0, free - FREE_SPACE_HEADROOM))
    return min(limits) if limits else None


def apply_budget(
    sizes: dict[str, int], budget: int | None
) -> tuple[list[str], list[tuple[str, int]]]:
    """Fit as much as the budget allows, smallest first.

    A budget of None is unbounded and everything is kept.

    Smallest first rather than largest: the point is to lose as few directories
    as possible, and the one directory that blows the budget is usually a clone
    the model can recreate. Whatever is dropped is named in the log — a
    migration that silently copies 90% of someone's work and reports success is
    worse than one that says which 10% it left.
    """
    if budget is None:
        return sorted(sizes), []
    kept: list[str] = []
    dropped: list[tuple[str, int]] = []
    running = 0
    for rel, size in sorted(sizes.items(), key=lambda kv: (kv[1], kv[0])):
        if running + size > budget:
            dropped.append((rel, size))
            continue
        running += size
        kept.append(rel)
    return sorted(kept), dropped


def remote_free_bytes(ssh: list[str], remote_root: str) -> int | None:
    result = remote(ssh, f"df -Pk {shlex.quote(remote_root)} | tail -1", check=False)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.split()[DF_AVAILABLE_COLUMN]) * DF_BLOCK_BYTES
    except (IndexError, ValueError):
        return None


def transfer(ssh: list[str], agent_home: Path, remote_root: str, paths: list[str]) -> None:
    """tar over the SSH connection, NUL-separated list, never overwriting.

    ``--skip-old-files`` is what makes this safe to run against a sandbox the
    model is already using. The two pods have no start ordering, so this can
    land mid-turn; without it a tar arriving thirty seconds late would replace
    a file the model had just written with the agent pod's older copy.

    ``--no-same-owner`` because the two pods disagree about uids — the agent
    pod runs Hermes as 10000, the sandbox's shell account is 1000 — and an
    extract that preserved ownership would leave the model unable to write its
    own files. Non-root tar defaults to this; stated because the default is
    the correctness argument here rather than an accident.

    The ``--exclude`` patterns are the second half of the exclusion rules:
    ``is_excluded`` decides which top-level entries are named at all, and these
    prune what is inside them. Both halves are needed — see RECURSIVE_EXCLUDES.
    """
    excludes = [f"--exclude={pattern}" for pattern in RECURSIVE_EXCLUDES]
    with tempfile.NamedTemporaryFile("wb", suffix=".lst", delete=False) as handle:
        listfile = handle.name
        handle.write(b"\0".join(p.encode("utf-8") for p in paths))
        handle.write(b"\0")
    try:
        source = subprocess.Popen(
            [
                "tar",
                "-c",
                "-C",
                str(agent_home),
                *excludes,
                "--null",
                "--files-from",
                listfile,
                "--warning=no-file-changed",
                "--warning=no-file-removed",
                "-f",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sink_cmd = (
            f"tar -x --skip-old-files --no-same-owner -C {shlex.quote(remote_root)} -f -"
        )
        sink = subprocess.Popen(
            ssh + ["--", sink_cmd],
            stdin=source.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert source.stdout is not None
        source.stdout.close()
        try:
            sink_out, sink_err = sink.communicate(timeout=SSH_CALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            # Kill both ends. Leaving the local tar running would hold the
            # pipe, and leaving the ssh client running would hold the
            # entrypoint that is about to exit.
            sink.kill()
            source.kill()
            sink.communicate()
            source.communicate()
            raise RemoteTimeout(
                f"the transfer into the sandbox did not finish within "
                f"{SSH_CALL_TIMEOUT_SECONDS}s"
            ) from exc
        if source.stderr:
            source_err = source.stderr.read()
            source.stderr.close()
        else:
            source_err = b""
        source.wait()
        # tar exits 1 for "file changed as we read it", which is routine on a
        # live home and is not a failed transfer. Only a hard error (2) is.
        if source.returncode not in (0, 1):
            raise RuntimeError(
                f"reading the agent pod's files failed ({source.returncode}): "
                f"{source_err.decode('utf-8', 'replace').strip()}"
            )
        if sink.returncode != 0:
            raise RuntimeError(
                f"extracting into the sandbox failed ({sink.returncode}): "
                f"{sink_err.decode('utf-8', 'replace').strip()}"
            )
        if source.returncode == 1:
            log(
                "tar reported files changing while it read them, which is "
                "expected on a live home; the transfer itself succeeded"
            )
        if sink_out.strip():
            log(sink_out.decode("utf-8", "replace").strip())
    finally:
        os.unlink(listfile)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-home",
        default=os.environ.get("HERMES_HOME", "/opt/data"),
        help="the agent pod's Hermes home (default: $HERMES_HOME)",
    )
    parser.add_argument(
        "--remote-root",
        default=os.environ.get("SANDBOX_DATA_ROOT", "/opt/data"),
        help="the sandbox's data root (default: /opt/data)",
    )
    parser.add_argument(
        "--config",
        default=MANAGED_CONFIG_PATH,
        help="the managed config carrying the terminal block",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="seconds to wait for the sandbox to answer before giving up",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="refuse to migrate more than this; the excess is named in the log. "
        "0 (the default) means no cap beyond leaving the sandbox volume some free space",
    )
    parser.add_argument(
        "--skeleton-only",
        action="store_true",
        help="create the directory layout and skip the one-shot migration",
    )
    parser.add_argument(
        "--force-migrate",
        action="store_true",
        help="migrate even if the sandbox already carries the marker",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be created and copied, and change nothing",
    )
    args = parser.parse_args(argv)

    agent_home = Path(args.agent_home)
    if not agent_home.is_dir():
        log(f"{agent_home} is not a directory; nothing to mirror")
        return EXIT_OK

    terminal = read_terminal_config(args.config)
    if terminal is None:
        log("no ssh terminal backend in the managed config; no sandbox to mirror")
        return EXIT_OK
    if not terminal.get("ssh_host"):
        log("the managed terminal block names no ssh_host; refusing to guess")
        return EXIT_OK

    ssh = ssh_base_command(terminal)
    homes = home_relative_paths(agent_home)

    if args.dry_run:
        include, skipped = migration_candidates(agent_home, homes)
        sizes = measure(agent_home, include)
        # No ssh yet on this path, so no free-space reading: the dry run reports
        # what --max-bytes alone would drop, which on the default of 0 is nothing.
        kept, dropped = apply_budget(sizes, effective_budget(args.max_bytes, None))
        report = {
            "homes": homes,
            "skeleton_dirs": list(SKELETON_DIRS),
            "would_copy": {rel: sizes[rel] for rel in kept},
            "would_drop_over_budget": dict(dropped),
            "skipped": dict(skipped),
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return EXIT_OK

    if not wait_for_sandbox(ssh, time.monotonic() + args.wait):
        return EXIT_OK

    # The sandbox writes this at the root of its own volume on every start. Its
    # absence means --remote-root does not name that volume, and every write
    # below would land somewhere nobody reads.
    #
    # Refusing is the point; holding the gateway down while refusing is not. The
    # marker lives under the same uid-1000 $DATA as everything else, so `rm -f
    # /opt/data/.sandbox` from a sandbox shell makes it missing, and the sandbox
    # does not rewrite it until its own container restarts -- which a
    # crash-looping gateway is in no position to arrange. Nothing has been copied
    # at this point, so this is the same trade as the layout push below: warn
    # loudly, come up, try again next start. An operator who really did point
    # --remote-root somewhere wrong reads the same line on every restart.
    marker = remote(
        ssh, f"test -f {shlex.quote(args.remote_root + '/' + SANDBOX_MARKER)}", check=False
    )
    if marker.returncode != 0:
        log(
            f"{args.remote_root}/{SANDBOX_MARKER} is missing on the far side: "
            f"{args.remote_root} is not the sandbox's data volume, or the marker was "
            "removed from it. Refusing to write, and leaving it for the next start."
        )
        return EXIT_RETRY

    # Not fatal, and this is the one call where that distinction earns its keep.
    # The sandbox has no start ordering against this pod, so it can be
    # rescheduled between wait_for_sandbox answering and this line running --
    # and everything under its /opt/data is owned by uid 1000, so the model can
    # also leave the layout in a state the push refuses. Either way nothing has
    # been copied yet, so nothing is lost by coming up without the layout and
    # pushing it on the next start. Holding the gateway down instead handed a
    # prompt injection a way to stop the agent for good.
    try:
        push_skeleton(ssh, args.remote_root, homes)
    except RuntimeError as exc:
        log(
            f"could not push the directory layout into the sandbox: {exc}. "
            "Starting anyway and leaving it for the next start; nothing has been "
            "copied, so nothing is lost. Skills that write to a home's working "
            "directories will fail until it succeeds."
        )
        return EXIT_RETRY

    # Same trade, one step later and for the same reasons: the sandbox can go
    # away between the skeleton push and this, and nothing here is destructive.
    # A Cluster Agent whose identity did not land reports a failed preflight and
    # blocks its card, which is loud and recoverable; a gateway held down is
    # neither.
    try:
        push_cluster_identities(ssh, agent_home, args.remote_root, homes)
    except (RuntimeError, OSError) as exc:
        log(
            f"could not deliver {CLUSTER_IDENTITY_FILE} into the sandbox: {exc}. "
            "Starting anyway and leaving it for the next start; a Cluster Agent "
            "without it fails preflight check 1 rather than working on the wrong "
            "cluster."
        )
        return EXIT_RETRY

    if args.skeleton_only:
        return EXIT_OK

    migrated_path = f"{args.remote_root}/{MIGRATION_MARKER}"
    already = remote(ssh, f"test -f {shlex.quote(migrated_path)}", check=False)
    if already.returncode == 0 and not args.force_migrate:
        log("the sandbox already carries the migration marker; nothing to copy")
        return EXIT_OK

    include, skipped = migration_candidates(agent_home, homes)
    noisy_skips = [
        (rel, why)
        for rel, why in skipped
        if (agent_home / rel).is_dir() and any((agent_home / rel).iterdir())
    ]
    copied: list[str] = []
    deferred: list[str] = []
    if not include:
        log("nothing on the agent pod's home qualifies for migration")
    else:
        sizes = measure(agent_home, include)
        free = remote_free_bytes(ssh, args.remote_root)
        if free is not None:
            log(f"sandbox volume has {free // (1024 * 1024)} MiB free")
        budget = effective_budget(args.max_bytes, free)
        kept, dropped = apply_budget(sizes, budget)
        deferred = [rel for rel, _ in dropped]
        for rel, size in dropped:
            log(
                f"NOT migrating {rel} ({size // (1024 * 1024)} MiB): it does not fit "
                f"in the {budget // (1024 * 1024)} MiB budget. It is untouched on the "
                "agent pod's volume."
            )
        if kept:
            total = sum(sizes[rel] for rel in kept)
            log(f"copying {len(kept)} path(s), {total // (1024 * 1024)} MiB, into the sandbox")
            try:
                transfer(ssh, agent_home, args.remote_root, kept)
            except (RuntimeError, OSError) as exc:
                # A failed copy is the one case the exit-code contract above
                # used to call fatal, on the reading that it might have moved
                # files off this volume without landing them. It cannot: tar
                # reads here and extracts there, and the only thing this
                # script unlinks is its own file list. So a broken pipe, a
                # dead sshd or a full sandbox volume costs a retry, not a
                # gateway container that will not come up.
                log(
                    f"copying into the sandbox failed: {exc}. Nothing was removed from "
                    "the agent pod's volume and no marker was written, so the next "
                    "start runs the copy again."
                )
                return EXIT_RETRY
            log("copy finished: " + ", ".join(kept))
            copied = kept
        else:
            log("no path fits the budget; nothing copied")

    for rel, why in noisy_skips:
        log(f"left on the agent pod: {rel} ({why})")

    if deferred:
        # The marker means "the copy is done", and every later start reads it
        # as permission to skip. Writing it while paths are still on the agent
        # pod's volume makes a budget that was too tight for one start
        # permanent: the sandbox volume grows, the next start would have had
        # room, and nothing ever looks again. Left unwritten instead, so the
        # retry happens on its own. Re-copying what already landed is safe --
        # `transfer` passes `--skip-old-files` and never overwrites the
        # sandbox's copy.
        log(
            f"NOT writing {migrated_path}: {len(deferred)} path(s) are still on the "
            "agent pod's volume (" + ", ".join(sorted(deferred)) + "). The next "
            "start will try again; raise --max-bytes or free space on the sandbox "
            "volume to finish it sooner."
        )
        return EXIT_OK

    summary = json.dumps(
        {
            "homes": homes,
            "copied": sorted(copied),
            "skipped": {rel: why for rel, why in skipped},
        },
        sort_keys=True,
    )
    # Also not fatal. The copy is already done at this point, and the marker
    # only says "do not do it again": without it the next start re-copies,
    # which transfer makes harmless -- --skip-old-files never overwrites what
    # the sandbox already has.
    try:
        remote(
            ssh,
            f"cat > {shlex.quote(migrated_path)} <<'SANDBOX_MIRROR_EOF'\n{summary}\nSANDBOX_MIRROR_EOF",
        )
    except RuntimeError as exc:
        log(
            f"the copy finished but {migrated_path} could not be written: {exc}. "
            "The next start will run the copy again, which changes nothing the "
            "sandbox already has."
        )
        return EXIT_RETRY
    log(f"wrote {migrated_path}; later starts will skip the copy")
    return EXIT_OK


if __name__ == "__main__":
    if shutil.which("tar") is None:
        log("no tar on PATH; cannot move files to the sandbox")
        sys.exit(EXIT_FATAL)
    try:
        sys.exit(main())
    except RemoteTimeout as exc:
        # Not every remote() call in main() sits inside a try. An uncaught
        # exception exits 1, which is EXIT_FATAL, which holds the gateway
        # container down -- and a sandbox whose shell startup hangs is the one
        # thing the model itself can arrange, so that would be a way to stop
        # the agent for good. Retry instead: the next start tries again, and a
        # human recycling the sandbox pod is what actually clears it.
        log(f"{exc}. Leaving it for the next start.")
        sys.exit(EXIT_RETRY)
