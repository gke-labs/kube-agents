#!/usr/bin/env python3
"""Link profile-targeted plugin image volumes into their Hermes profile homes.

An AgentPlugin with `spec.targetProfile` ships as an OCI image volume. The operator mounts
it at /opt/agent-plugins/<profile>/<plugin> — outside $HERMES_HOME — and this links it to
$HERMES_HOME/profiles/<profile>/plugins/<plugin>, which is where Hermes resolves a
profile's plugins from.

Why the mount does not go straight there
----------------------------------------
$HERMES_HOME is the data PVC, and the kubelet creates a volume's mount point before the
container entrypoint runs. Mounting at profiles/<profile>/plugins/<plugin> therefore
created profiles/<profile> *inside the PVC* ahead of the scaffold, and both scaffold gates
— docker-entrypoint.sh step 2.5 and profile_scaffold.ensure_profile — read "the directory
exists" as "the profile is built". A fresh PVC that came up with a targeted plugin got a
profile that was never registered with Hermes and never received the template's skills,
procedures, cron, or governance; the symptom is the "Unknown skill(s)" the plugin work
exists to remove. Worse, the directory persists on the PVC, so every later start skipped
the scaffold again and it never self-healed. Mounting outside the PVC and linking in keeps
the kubelet out of the profile tree entirely.

Symlinks are safe here: an image volume is read-only either way, and Python imports a
package through a symlinked directory normally.

Usage:
    profile_plugins.py --hermes-home DIR [--mount-root DIR] [--profile NAME]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

# Shipped side by side in the image (/opt/defaults/scripts, reachable as
# $HERMES_HOME/scripts). The profile-name rule has one home, in profile_overlay.
from profile_overlay import valid_profile_name

DEFAULT_MOUNT_ROOT = "/opt/agent-plugins"


def log(msg: str) -> None:
    print(f"[PROFILE-PLUGINS] {msg}", file=sys.stderr)


def _links_into(link: pathlib.Path, root: pathlib.Path) -> bool:
    """True when `link` is a symlink pointing somewhere under `root`.

    Compares the recorded target without resolving it: a dangling link — the case this
    exists to find — has nothing to resolve to.
    """
    if not link.is_symlink():
        return False
    target = pathlib.Path(os.readlink(link))
    if not target.is_absolute():
        target = pathlib.Path(os.path.normpath(link.parent / target))
    return target == root or root in target.parents


def _looks_like_a_profile(home: pathlib.Path) -> bool:
    """True when the directory is a profile rather than a bare mount point.

    `profile.yaml` is written by `hermes profile create`; `config.yaml` is accepted too,
    so a profile built by an older layout — which has one but may predate the marker — is
    never mistaken for a skeleton. A kubelet-made mount point has neither.
    """
    return (home / "profile.yaml").is_file() or (home / "config.yaml").is_file()


def prune_stale_links(profile_home, mount_root=DEFAULT_MOUNT_ROOT) -> list[str]:
    """Drop links left behind by a plugin that is no longer mounted.

    The AgentPlugin was deleted or retargeted, so the pod spec no longer carries its
    volume. Only links pointing into `mount_root` are ever removed — a real plugin
    directory, and anything the template or the shared plugins overlay copied in, is left
    alone. Returns the names removed.
    """
    plugins_dir = pathlib.Path(profile_home) / "plugins"
    mount_root = pathlib.Path(mount_root)
    removed = []
    if not plugins_dir.is_dir():
        return removed
    for entry in sorted(plugins_dir.iterdir()):
        if _links_into(entry, mount_root) and not entry.exists():
            entry.unlink()
            removed.append(entry.name)
    return removed


def link_plugins(profile_home, mount_root=DEFAULT_MOUNT_ROOT, profile=None) -> list[str]:
    """Link every plugin mounted for this profile into <profile_home>/plugins.

    `profile` defaults to the profile home's directory name. Returns the names linked.
    Idempotent: an existing link is repointed, which matters when a plugin's mount path
    changes between image rolls.
    """
    profile_home = pathlib.Path(profile_home)
    mount_root = pathlib.Path(mount_root)
    name = profile or profile_home.name
    if not valid_profile_name(name):
        raise ValueError(f"not a valid profile name: {name!r}")

    linked = []
    prune_stale_links(profile_home, mount_root)

    source = mount_root / name
    if not source.is_dir():
        return linked

    plugins_dir = profile_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for plugin in sorted(source.iterdir()):
        if not plugin.is_dir():
            continue
        dest = plugins_dir / plugin.name
        if dest.exists() and not dest.is_symlink():
            if dest.is_dir() and not any(dest.iterdir()):
                # An empty directory here is the old layout's mount point, left on the
                # PVC when the volume moved out of the profile tree. Removing it is what
                # lets the upgrade land; rmdir refuses anything that is not empty.
                dest.rmdir()
            else:
                # Real content: the shared plugins overlay or the template put it there.
                # Replacing it would delete image content, and the collision is the
                # operator's to resolve, so say so and move on.
                log(f"{name}: {dest} exists and is not a link; leaving the mounted copy unused")
                continue
        dest.unlink(missing_ok=True)
        dest.symlink_to(plugin, target_is_directory=True)
        linked.append(plugin.name)
    return linked


def link_all(hermes_home, mount_root=DEFAULT_MOUNT_ROOT) -> dict[str, list[str]]:
    """Link mounted plugins for every profile that exists; report ones that do not.

    Sweeps the profiles rather than the mounts so a profile whose plugin was removed still
    gets its dangling link pruned. A mount whose profile is missing is warned about, not
    an error: a plugin may legitimately target a cluster profile that has not been
    scaffolded yet, and cluster_agent_profile.create_profile links it when it is.
    """
    hermes_home = pathlib.Path(hermes_home)
    mount_root = pathlib.Path(mount_root)
    profiles_dir = hermes_home / "profiles"

    results: dict[str, list[str]] = {}
    existing: set[str] = set()
    skipped: set[str] = set()
    if profiles_dir.is_dir():
        for home in sorted(profiles_dir.iterdir()):
            if not home.is_dir() or not valid_profile_name(home.name):
                continue
            if not _looks_like_a_profile(home):
                # A directory left by the older mount layout, holding nothing but empty
                # mount points. Linking into it would add content that
                # profile_scaffold._clear_mount_skeleton then refuses to remove, which
                # is what keeps the profile from ever being scaffolded.
                log(f"WARN: {home.name} is not a scaffolded profile; not linking into it")
                skipped.add(home.name)
                continue
            existing.add(home.name)
            try:
                linked = link_plugins(home, mount_root)
            except OSError as exc:
                # Isolated per profile, the way cluster_agent_reconcile sweeps: one
                # unwritable profile directory must not cost every other profile its
                # plugins, and the pod is coming up either way.
                log(f"WARN: {home.name}: linking failed ({exc}); its targeted plugins will not load")
                continue
            if linked:
                results[home.name] = linked
                log(f"{home.name}: linked {', '.join(linked)}")

    if mount_root.is_dir():
        for source in sorted(mount_root.iterdir()):
            # `skipped` is already reported above, with the reason; repeating it here as
            # "does not exist" would contradict that and send the reader looking for the
            # wrong problem.
            if source.is_dir() and source.name not in existing and source.name not in skipped:
                log(
                    f"WARN: plugin volume(s) mounted for profile '{source.name}', which does "
                    "not exist yet; they will be linked when it is scaffolded"
                )
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hermes-home", required=True)
    ap.add_argument("--mount-root", default=DEFAULT_MOUNT_ROOT)
    ap.add_argument("--profile", default=None, help="Link one profile instead of sweeping all.")
    args = ap.parse_args(argv)

    try:
        if args.profile:
            home = pathlib.Path(args.hermes_home) / "profiles" / args.profile
            if not home.is_dir():
                print(f"no such profile home: {home}", file=sys.stderr)
                return 2
            linked = link_plugins(home, args.mount_root, args.profile)
            print(f"{args.profile}: linked {', '.join(linked) if linked else 'nothing'}")
            return 0
        link_all(args.hermes_home, args.mount_root)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the entrypoint
        print(f"linking plugin volumes failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
