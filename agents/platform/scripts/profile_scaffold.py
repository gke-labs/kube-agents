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


def profiles_base(hermes_home: Path) -> Path:
    # Hermes stores each named profile at $HERMES_HOME/profiles/<name>.
    return hermes_home / "profiles"


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
    """Register a Hermes profile (idempotent) and return its home path."""
    home = profiles_base(hermes_home) / name
    if not home.exists():
        try:
            subprocess.run(
                ["hermes", "profile", "create", name, "--no-skills", "--description", description],
                check=True, capture_output=True, text=True, timeout=60, env=run_env(hermes_home),
            )
        except subprocess.CalledProcessError as e:
            raise SystemExit(
                f"ERROR: 'hermes profile create {name}' failed: {e.stderr.strip() or e.stdout.strip()}"
            )
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


def _make_writable(path: Path) -> None:
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for d in dirs:
            try:
                os.chmod(os.path.join(root, d), 0o755)
            except OSError:
                pass
        for f in files:
            try:
                os.chmod(os.path.join(root, f), 0o644)
            except OSError:
                pass
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


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
            _make_writable(dest)
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    if plugins_dir and plugins_dir.is_dir():
        _make_writable(home / "plugins")
        shutil.copytree(plugins_dir, home / "plugins", dirs_exist_ok=True)
    skills_dest = home / "skills"
    manifest = template_dir / "skills" / "skills_manifest.sha256"
    if skills_dest.is_dir() and manifest.is_file():
        try:
            from verify_skills_provenance import verify_provenance

            verify_provenance(str(manifest), str(skills_dest))
        except Exception as e:
            raise SystemExit(f"ERROR: skill provenance verification failed for {skills_dest}: {e}")
        for root, dirs, files in os.walk(skills_dest):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o555)
                except OSError:
                    pass
            for f in files:
                filepath = os.path.join(root, f)
                try:
                    os.chmod(filepath, 0o555 if "/scripts/" in filepath else 0o444)
                except OSError:
                    pass


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
