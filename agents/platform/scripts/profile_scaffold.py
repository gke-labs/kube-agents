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


# Absolute, because a kanban worker's terminal runs with a stripped environment in
# which /opt/hermes/.venv/bin is not on PATH — a bare `hermes` raises ENOENT there
# while working fine from an interactive shell. Same trap as `_roster_command` in
# agents/chat/scripts/bootstrap_scan_gate.py.
HERMES_BIN = os.environ.get("HERMES_BIN") or "/opt/hermes/.venv/bin/hermes"


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
                [HERMES_BIN, "profile", "create", name, "--no-skills", "--description", description],
                check=True, capture_output=True, text=True, timeout=60, env=run_env(hermes_home),
            )
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() or e.stdout.strip()
            if not pre_existing and not is_scaffolded(home):
                raise SystemExit(f"ERROR: '{HERMES_BIN} profile create {name}' failed: {detail}")
            # The home was already on disk, so an "already exists" refusal is expected and
            # harmless — the caller overlays the template onto it either way, which is what
            # happened before this gate existed. Still worth a line: a home Hermes has not
            # registered may not be selectable as `hermes -p <name>`.
            log(f"'{HERMES_BIN} profile create {name}' failed against an existing home ({detail}); continuing")
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


def merge_cron_store(
    image: object, live: object, only_ids: tuple[str, ...] | None = None
) -> object:
    """Overlay the image's cron definitions onto the volume's cron store.

    `cron/jobs.json` is two things in one file. The job definitions —
    schedule, prompt, skills, `enabled` — are image-owned, and an upgrade has
    to be able to change them; that is the whole reason the entrypoint force-
    syncs this directory. But the same file is where the scheduler records
    runtime state (`last_run` and friends), and where an operator's own jobs
    live. A straight copytree took all three, on every pod restart: a job
    added through the operator vanished, and every run's history went with
    it. Losing `last_run_at` is the sharp edge — for a one-shot that field
    *is* the already-ran guard (`_recoverable_oneshot_run_at` returns None
    once it is set), so erasing it makes the job eligible all over again and
    it runs a second time. Recurring jobs fail the other way: a wiped
    `next_run_at` is recomputed from *now*, always landing on a future
    occurrence, which throws away the stale past timestamp the scheduler's
    catch-up window needs. A merely-late daily audit is then skipped rather
    than caught up.

    The rule is per key, which needs no list of "state" fields to keep in step
    with Hermes: **the image wins every key it ships, and every key it does not
    ship is left as the volume had it.** `enabled: false` in the image
    therefore disables a job (the documented way to turn a watchdog off), while
    `last_run`, which no shipped entry carries, survives. Jobs on the volume
    with no counterpart in the image are kept as they are.

    That last rule is also the limit: nothing here can tell an operator's own
    job from one this release deleted, so *removing* an entry from the shipped
    roster does not stop it firing on a cluster that already has it — it only
    ends the image's ability to hold it off. Retire a watchdog by shipping
    `enabled: false` and leaving the entry in place; an id is safe to drop from
    the roster only once every live cluster has merged that disabled form,
    because from then on the volume's copy stays off on its own. The five
    unrunnable watchdogs were retired that way before being deleted here.

    `only_ids` narrows the image side to the ids it names; every other shipped
    job is treated as if the image did not carry it, so the volume's copy — or
    its deliberate absence — stands. The default profile needs that, because
    two of the jobs it ships **delete themselves**: `bootstrap_delivery.py`
    calls `remove_job` on the onboarding pair once the report is delivered.
    Merging that roster unfiltered would resurrect both on the next pod
    restart, and they would then poll once a minute forever, no-op on the
    `.bootstrap_completed` marker, and record a scheduler execution every time.
    Naming the ids keeps the force-merge to the entries whose definition the
    image genuinely owns; the platform profile passes nothing here and merges
    its whole roster as before.
    """
    if not isinstance(image, dict) or not isinstance(live, dict):
        return image
    merged = {**live, **{k: v for k, v in image.items() if k != "jobs"}}
    image_jobs = image.get("jobs")
    if not isinstance(image_jobs, list):
        return merged
    if only_ids is not None:
        allowed = set(only_ids)
        image_jobs = [
            j for j in image_jobs if isinstance(j, dict) and str(j.get("id", "")) in allowed
        ]

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


def retire_cron_jobs(store: object, retire_ids: tuple[str, ...]) -> object:
    """Delete the named ids from a cron store outright.

    `merge_cron_store` can only ever *hold a job off* — it has no way to tell an
    operator's own job from one this release dropped, so it keeps every entry the
    image is silent about. That is the right default, and it is also why retiring
    a watchdog normally takes two releases: ship `enabled: false`, wait for every
    live volume to merge it, then delete the entry.

    This is the escape hatch for the case that rule cannot cover: an id that has
    to stop firing on volumes that already have it *enabled*, in one release,
    because something else has taken over its work. Deleting the shipped entry
    alone would strand the volume's copy at `enabled: true` — still firing, and
    now with the image unable to reach it. That is how the same audit ends up
    running twice: once from the roster it moved to, once from the copy nobody
    can turn off.

    Naming an id here asserts the image owns it. An operator's job that happens
    to share the name goes with it, which is why the entrypoint's list is
    hand-maintained and short rather than derived from what the image stopped
    shipping.
    """
    if not retire_ids or not isinstance(store, dict):
        return store
    jobs = store.get("jobs")
    if not isinstance(jobs, list):
        return store
    doomed = set(retire_ids)
    kept = [
        j for j in jobs if not (isinstance(j, dict) and str(j.get("id", "")) in doomed)
    ]
    if len(kept) == len(jobs):
        return store
    return {**store, "jobs": kept}


def _merge_after_overlay(
    home: Path,
    template_dir: Path,
    names: tuple[str, ...],
    prior: dict[str, object],
    cron_job_ids: tuple[str, ...] | None = None,
    cron_retire_ids: tuple[str, ...] = (),
) -> None:
    """Restore the merged form of every MERGE_PATHS entry the copy just replaced.

    Done after the copy rather than instead of it: the copy is what creates the
    file on a first scaffold, and re-deriving the merge from contents read
    *before* the copy keeps this a pure add-on to the existing behaviour.

    The retire pass runs last, on the merged result, because the ids it deletes
    are by definition ones the image no longer ships — `merge_cron_store` will
    have carried the volume's copies through untouched, which is exactly what
    has to be undone.
    """
    for relative, previous in prior.items():
        parts = relative.split("/")
        if parts[0] not in names:
            continue
        source = template_dir.joinpath(*parts)
        if not source.is_file():
            continue
        merged = retire_cron_jobs(
            merge_cron_store(read_json(source), previous, cron_job_ids),
            cron_retire_ids,
        )
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
    cron_job_ids: tuple[str, ...] | None = None,
    cron_retire_ids: tuple[str, ...] = (),
) -> None:
    """Copy a baked template onto a profile home (overwrites).

    If `items` is given, only those top-level names are overlaid; otherwise the
    entire template directory content is copied. Optionally overlays shared
    plugins (otel, etc.) into <home>/plugins for observability parity.

    Everything named in `MERGE_PATHS` is the exception: it is read first,
    overwritten with the rest, and then rewritten as a merge of the two. See
    `merge_cron_store` for why a file can be both image-owned and runtime state,
    and what `cron_job_ids` narrows that merge to; `cron_retire_ids` names the
    ids to delete from the volume outright (see `retire_cron_jobs`).
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
    _merge_after_overlay(home, template_dir, names, prior, cron_job_ids, cron_retire_ids)
    if plugins_dir and plugins_dir.is_dir():
        try:
            shutil.copytree(plugins_dir, home / "plugins", dirs_exist_ok=True)
        except (shutil.Error, OSError) as exc:
            # Reported, not raised, for the reason _merge_after_overlay gives.
            # The plugins are observability parity — hermes_otel and friends —
            # and they are the LAST thing this function does, but an exception
            # here still leaves the caller's `|| echo WARN` as the only handler,
            # and the entrypoint reads that as "the whole scaffold failed". It
            # did not: the persona, config, skills, cron and governance above
            # all landed. shutil.Error in particular is a *collection* of
            # per-file failures that copytree accumulates and raises at the end,
            # so the tree is as complete as it was going to get either way.
            # The entrypoint re-runs this copy on every start, so a transient
            # failure self-heals on the next boot.
            log(f"WARN: could not overlay plugins into {home / 'plugins'}; "
                f"this profile may be missing observability plugins ({exc})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Create and overlay a Hermes profile from a template.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="Named profile under $HERMES_HOME/profiles to create and overlay.")
    # The `default` profile IS $HERMES_HOME — it has no entry under profiles/ and
    # `hermes profile create` does not make it — so the only way to give it the
    # same image-tracking overlay the named profiles get is to name its home
    # directly and skip registration. The entrypoint uses this to merge the Chat
    # Agent's cron/jobs.json, which `cp -ru` can never refresh: the ticker
    # rewrites that file constantly, so the volume's copy always looks newer than
    # the image's and a newly shipped job would never land on an existing PVC.
    group.add_argument("--home", help="Overlay directly onto this home; skips profile registration.")
    ap.add_argument("--template", required=True, help="Baked template dir to overlay onto the profile home.")
    ap.add_argument("--description", default="", help="Profile description (surfaced in discovery).")
    ap.add_argument("--plugins", default="", help="Optional shared plugins dir to overlay for observability.")
    ap.add_argument(
        "--items",
        default="",
        help="Space-separated template entries (files or dirs) to overlay; default overlays the whole template.",
    )
    ap.add_argument(
        "--cron-jobs",
        default="",
        help=(
            "Space-separated cron job ids the image may force onto the volume's roster; "
            "default merges every job the image ships (see merge_cron_store)."
        ),
    )
    ap.add_argument(
        "--cron-retire",
        default="",
        help=(
            "Space-separated cron job ids to delete from the volume's roster outright, "
            "for jobs this release moved elsewhere (see retire_cron_jobs)."
        ),
    )
    args = ap.parse_args()

    if args.home:
        home = Path(args.home)
        if not home.is_dir():
            raise SystemExit(f"ERROR: --home is not a directory: {home}")
    else:
        hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
        home = ensure_profile(args.name, args.description, hermes_home)
    overlay_template(
        home,
        Path(args.template),
        Path(args.plugins) if args.plugins else None,
        tuple(args.items.split()) or None,
        tuple(args.cron_jobs.split()) or None,
        tuple(args.cron_retire.split()),
    )
    print(str(home))


if __name__ == "__main__":
    main()
