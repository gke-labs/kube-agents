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
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Paths inside the template that hold runtime state as well as image-owned
# configuration, and so must be merged rather than replaced. Relative to the
# profile home, POSIX-separated; each one needs a merge rule below.
MERGE_PATHS: tuple[str, ...] = ("cron/jobs.json",)


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


def read_json(path: Path) -> object | None:
    """Parse `path` as JSON, or None if it is absent, unreadable, or malformed.

    None is "no usable prior state", and every caller treats that as "let the
    image's copy stand". A half-written jobs.json must not take the profile's
    whole cron roster down with it.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def merge_cron_store(image: object, live: object) -> object:
    """Overlay the image's cron definitions onto the volume's cron store.

    `cron/jobs.json` is two things in one file. The job definitions —
    schedule, prompt, skills, `enabled` — are image-owned, and an upgrade has
    to be able to change them; that is the whole reason the entrypoint force-
    syncs this directory. But the same file is where the scheduler records
    runtime state (`last_run` and friends), and where an operator's own jobs
    live. A straight copytree took all three: every run's history was erased
    on every pod restart, so a daily audit could fire twice in a morning, and
    a job added through the operator vanished.

    The rule is per key, which needs no list of "state" fields to keep in step
    with Hermes: **the image wins every key it ships, and every key it does not
    ship is left as the volume had it.** `enabled: false` in the image
    therefore disables a job (the documented way to turn a watchdog off), while
    `last_run`, which no shipped entry carries, survives. Jobs on the volume
    with no counterpart in the image are kept as they are.

    That last rule is also the limit: nothing here can tell an operator's own
    job from one this release deleted, so *removing* an entry from the shipped
    roster does not stop it firing on a cluster that already has it. Retire a
    watchdog with `enabled: false`, not by deleting it — which is what the
    shipped `cron/jobs.json` already does with the five it no longer runs.
    """
    if not isinstance(image, dict) or not isinstance(live, dict):
        return image
    merged = {**live, **{k: v for k, v in image.items() if k != "jobs"}}
    image_jobs = image.get("jobs")
    if not isinstance(image_jobs, list):
        return merged

    raw_live = live.get("jobs")
    live_jobs = [j for j in raw_live if isinstance(j, dict)] if isinstance(raw_live, list) else []
    live_by_id = {str(j["id"]): j for j in live_jobs if j.get("id")}

    out: list[object] = []
    for job in image_jobs:
        existing = live_by_id.get(str(job.get("id", ""))) if isinstance(job, dict) else None
        if existing is None:
            out.append(job)
            continue
        # Image fields first so the file still reads in the shipped order; the
        # volume contributes only the keys the image is silent about.
        out.append({**job, **{k: v for k, v in existing.items() if k not in job}})

    shipped = {str(j.get("id", "")) for j in image_jobs if isinstance(j, dict)}
    out += [j for j in live_jobs if str(j.get("id", "")) not in shipped]
    merged["jobs"] = out
    return merged


def _merge_after_overlay(
    home: Path, template_dir: Path, names: tuple[str, ...], prior: dict[str, object]
) -> None:
    """Restore the merged form of every MERGE_PATHS entry the copy just replaced.

    Done after the copy rather than instead of it: the copy is what creates the
    file on a first scaffold, and re-deriving the merge from contents read
    *before* the copy keeps this a pure add-on to the existing behaviour.
    """
    for relative, previous in prior.items():
        parts = relative.split("/")
        if parts[0] not in names:
            continue
        source = template_dir.joinpath(*parts)
        if not source.is_file():
            continue
        merged = merge_cron_store(read_json(source), previous)
        destination = home.joinpath(*parts)
        try:
            # Temp file and os.replace, not a plain write: a torn jobs.json is
            # a profile with no cron roster at all, and this runs during
            # start-up on a volume that may be mid-restart.
            scratch = destination.with_name(destination.name + ".tmp")
            scratch.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            os.replace(scratch, destination)
        except OSError as exc:
            # The image's copy is already in place, so the profile still runs;
            # what is lost is the run history. Say so rather than fail the
            # whole start-up over it.
            log(f"WARN: could not merge {relative}; image copy stands ({exc})")


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

    Everything named in `MERGE_PATHS` is the exception: it is read first,
    overwritten with the rest, and then rewritten as a merge of the two. See
    `merge_cron_store` for why a file can be both image-owned and runtime state.
    """
    if not template_dir.is_dir():
        raise SystemExit(f"ERROR: template dir not found: {template_dir}")
    names = tuple(items) if items is not None else tuple(p.name for p in template_dir.iterdir())
    prior = {
        relative: contents
        for relative in MERGE_PATHS
        if (contents := read_json(home.joinpath(*relative.split("/")))) is not None
    }
    for item_name in names:
        src = template_dir / item_name
        if not src.exists():
            continue
        dest = home / item_name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    _merge_after_overlay(home, template_dir, names, prior)
    if plugins_dir and plugins_dir.is_dir():
        shutil.copytree(plugins_dir, home / "plugins", dirs_exist_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Create and overlay a Hermes profile from a template.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--template", required=True, help="Baked template dir to overlay onto the profile home.")
    ap.add_argument("--description", default="", help="Profile description (surfaced in discovery).")
    ap.add_argument("--plugins", default="", help="Optional shared plugins dir to overlay for observability.")
    ap.add_argument(
        "--items",
        default="",
        help="Space-separated template entries (files or dirs) to overlay; default overlays the whole template.",
    )
    args = ap.parse_args()

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    home = ensure_profile(args.name, args.description, hermes_home)
    overlay_template(
        home,
        Path(args.template),
        Path(args.plugins) if args.plugins else None,
        tuple(args.items.split()) or None,
    )
    print(str(home))


if __name__ == "__main__":
    main()
