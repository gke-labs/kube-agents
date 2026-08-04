#!/usr/bin/env python3
# profile_scaffold.py - Shared helper to create + overlay a Hermes profile from a baked template.
#
# Used at two points:
#   - Container startup (deploy/shared/docker-entrypoint.sh) scaffolds the static
#     `platform` specialist profile from /opt/platform-template.
#   - Runtime (cluster_agent_profile.py) scaffolds per-cluster profiles from
#     /opt/cluster-template.
#
# Personas are separated by profile identity, persona (SOUL.md), and scoped
# toolset (config.yaml) — all shipped in the template and overlaid here onto the
# profile home under $HERMES_HOME/profiles/<name>. Executable scripts are NOT
# part of a template: they live in the shared /opt/data/scripts and are reachable
# by every profile.

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def make_log(prefix: str):
    """Build a stderr logger tagged with a component prefix (shared across the profile scripts)."""

    def _log(msg: str) -> None:
        print(f"[{prefix}] {msg}", file=sys.stderr)

    return _log


log = make_log("PROFILE-SCAFFOLD")

# `hermes profile create` writes profiles/<name>/profile.yaml, and no template ships one.
# It is therefore the only thing that proves a profile was scaffolded. Directory existence
# does not: the kubelet creates a targeted plugin's mount point inside the data PVC before
# the entrypoint runs, so an unbuilt profile can already have a directory (see
# deploy/shared/profile_plugins.py for the whole failure mode).
PROFILE_MARKER = "profile.yaml"


def profiles_base(hermes_home: Path) -> Path:
    # Hermes stores each named profile at $HERMES_HOME/profiles/<name>.
    return hermes_home / "profiles"


def is_scaffolded(home: Path) -> bool:
    """True when Hermes has registered this profile, not merely that a directory exists."""
    return (home / PROFILE_MARKER).is_file()


def _clear_mount_skeleton(home: Path) -> bool:
    """Remove an unregistered profile home that holds nothing but empty directories.

    That shape is the kubelet's: profiles/<name>/plugins/<plugin>/ and nothing else, left
    from an older layout that mounted plugin image volumes inside the PVC. `hermes profile
    create` can refuse a home that already exists, so clear it — but only when there is
    provably nothing in it. Never deletes a file, so a real profile (including one whose
    Hermes predates profile.yaml) is never touched. Returns True if the home is now gone.
    """
    if not home.exists():
        return True
    if any(p.is_file() or p.is_symlink() for p in home.rglob("*")):
        return False
    shutil.rmtree(home, ignore_errors=True)
    return not home.exists()


def run_env(hermes_home: Path | str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Base env for Hermes/gcloud subprocesses (shared across the profile scripts).

    Always redirects HOME -> /tmp: under RunAsNonRoot the real home is not writable
    and gcloud/hermes must write credentials/state to the scratch disk. When
    ``hermes_home`` is given it pins HERMES_HOME so the subprocess targets that data
    root regardless of the caller's own (possibly rewritten) HERMES_HOME. ``extra``
    overlays additional vars (e.g. KUBECONFIG).
    """
    env = {**os.environ, "HOME": "/tmp"}
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    if extra:
        env.update(extra)
    return env


def ensure_profile(name: str, description: str, hermes_home: Path) -> Path:
    """Register a Hermes profile (idempotent) and return its home path.

    Gated on the scaffold marker rather than on the directory: a home that exists but was
    never registered is exactly what the old plugin mount layout produced, and skipping
    the create for it left a profile Hermes had never heard of.
    """
    home = profiles_base(hermes_home) / name
    if not is_scaffolded(home):
        _clear_mount_skeleton(home)
        pre_existing = home.exists()
        try:
            subprocess.run(
                ["hermes", "profile", "create", name, "--no-skills", "--description", description],
                check=True, capture_output=True, text=True, timeout=60, env=run_env(hermes_home),
            )
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() or e.stdout.strip()
            if not pre_existing and not is_scaffolded(home):
                raise SystemExit(f"ERROR: 'hermes profile create {name}' failed: {detail}")
            # The home was already on disk, so an "already exists" refusal is expected and
            # harmless — the caller overlays the template onto it either way, which is what
            # happened before this gate existed. Still worth a line: a home Hermes has not
            # registered may not be selectable as `hermes -p <name>`.
            log(f"'hermes profile create {name}' failed against an existing home ({detail}); continuing")
        except subprocess.TimeoutExpired:
            raise SystemExit(f"ERROR: 'hermes profile create {name}' timed out after 60s")
        except OSError as e:
            # `hermes` not on PATH or not executable. The entrypoint calls this
            # script with `|| echo WARN ...`, so an uncaught traceback here is
            # noise in the container log rather than a clear cause; SystemExit
            # keeps the failure to one actionable line.
            raise SystemExit(f"ERROR: could not execute 'hermes' to create profile {name}: {e}")
    if not home.is_dir():
        raise SystemExit(f"ERROR: expected profile home not found after create: {home}")
    return home


def overlay_template(
    home: Path,
    template_dir: Path,
    plugins_dir: Path | None = None,
    items: tuple[str, ...] | None = None,
) -> None:
    """Copy a baked template onto a profile home (overwrites).

    If `items` is given, only those top-level names are overlaid; otherwise the
    entire template directory content is copied. Optionally overlays shared
    plugins (otel, etc.) into <home>/plugins for observability parity.
    """
    if not template_dir.is_dir():
        raise SystemExit(f"ERROR: template dir not found: {template_dir}")
    names = items if items is not None else tuple(p.name for p in template_dir.iterdir())
    for item_name in names:
        src = template_dir / item_name
        if not src.exists():
            continue
        dest = home / item_name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    if plugins_dir and plugins_dir.is_dir():
        shutil.copytree(plugins_dir, home / "plugins", dirs_exist_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Create and overlay a Hermes profile from a template.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--template", required=True, help="Baked template dir to overlay onto the profile home.")
    ap.add_argument("--description", default="", help="Profile description (surfaced in discovery).")
    ap.add_argument("--plugins", default="", help="Optional shared plugins dir to overlay for observability.")
    args = ap.parse_args()

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    home = ensure_profile(args.name, args.description, hermes_home)
    overlay_template(home, Path(args.template), Path(args.plugins) if args.plugins else None)
    print(str(home))


if __name__ == "__main__":
    main()
