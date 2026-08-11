#!/usr/bin/env python3
"""Rebuild the default (Chat Agent) profile's config.yaml at pod startup.

Called by docker-entrypoint.sh step 2d, once, in the primary container. Lives in its own
file rather than inline in the entrypoint so it can be unit tested — see
tests/test_default_profile_config.py.

Why the default profile needs its own algorithm
-----------------------------------------------
Every OTHER profile is reconciled by profile_overlay.apply_overlay: merge the operator's
overlay into a config the image force-syncs (platform) or never touches (cluster), and
keep a last-applied record so a withdrawn overlay can be undone. The default profile
fits neither shape:

  * Its config.yaml is the file the running agent writes to. `/sethome` persists the
    home channel there (gateway/config.py persist_home_channel), the monitoring policy
    mints `monitoring.install_id` there, and a dozen slash commands save preferences
    there. Force-syncing it from the image throws all of that away on every restart.
  * It is also the file the operator has the most to say about: renderConfigYAML emits
    the model endpoint, the platform adapters, the toolset lockdown and the approval
    policy, and a change to the PlatformAgent CR has to reach the pod.

So it is rebuilt from scratch on every start, and the runtime's own edits are carried
across by a three-way merge rather than by an allow-list of paths to rescue. An
allow-list was the first design and was rejected: Hermes has 60-odd `save_config` call
sites and gains more with every release, and a path missing from the list is lost
silently on the next restart — the same silent-no-op failure this whole mechanism exists
to remove.

The three inputs
----------------
  base   the baseline this script wrote last time, kept beside the config in
         `.default-config-baseline.yaml`.
  ours   the live config.yaml, i.e. base plus whatever the runtime has written since.
  theirs the new baseline: the image's config.yaml merged with the operator's
         `profile-default.overlay.yaml`.

Resolution, per key:

  * unchanged at runtime (ours == base)          -> theirs wins
  * changed at runtime, baseline unchanged       -> ours wins
  * changed at runtime AND in the baseline       -> theirs wins (the operator is
                                                    reconciling a CR; a stale local
                                                    edit must not veto it)
  * present at runtime, unknown to either baseline -> ours wins (runtime-only state:
                                                    home_channel, install_id, a
                                                    user-added mcp_servers entry)
  * absent at runtime, present in base           -> theirs wins. Absence is NOT read as
                                                    a deletion: hermes_cli.config's
                                                    save_config drops keys whose value
                                                    equals a Hermes default, so a
                                                    missing key means nothing.

Lists follow the same rule, with one refinement: when the baseline changed, the entries
the runtime added are appended to it rather than the runtime list replacing it. When the
baseline did not change, the runtime list stands verbatim — a toolset toggled OFF in the
dashboard writes a SHORTER list, and unioning would resurrect the entry on every restart.

The first start, when there is no recorded baseline
---------------------------------------------------
All three rules above need `base`, and on the very first start on a volume there is no
`base`: this script has never run here, so nothing recorded what the runtime started
from. There is no way to recover it either — the previous image is gone.

`adopt` is that case, and it does not guess. A key the image or the operator declares is
taken from the new baseline; a key neither of them mentions is runtime state and is
carried across. So `/sethome`'s home channel and `monitoring.install_id` survive the
upgrade, and a pre-upgrade local edit to a *declared* key does not — which is the same
answer the steady-state rules give that edit on the next CR change anyway.

Standing the image config in for the missing `base` was the first design and is wrong in
exactly the case it exists for. That start is an image *upgrade*: the live file is the
OLD image's config plus runtime edits, and the stand-in is the NEW image's. Every key the
release changed then reads as a runtime edit and `ours` wins — and because the baseline
written at the end is the correct new one, every later start re-derives the same verdict.
An upgraded volume pins its old config for good, silently. This branch's own two fixes
(the `${HERMES_HOME}` router path, the `incident_context` plugin) were both discarded
that way; `tests/test_default_profile_config.py` has the regression.

What this deliberately does not preserve
----------------------------------------
Anything the operator changes in the same window. Change `model.default` in the CR while
someone has changed it with `/model` and the CR wins on the next restart. That is the
point of a CR, and it is the only rule under which "edit the PlatformAgent" reliably
means anything.

Usage:
    default_profile_config.py --config FILE --image FILE [--overlay FILE ...]
                              [--baseline FILE]
"""

from __future__ import annotations

import argparse
import copy
import os
import pathlib
import sys
import uuid

import yaml

# profile_overlay.py sits beside this file, both in the image (/opt/defaults/scripts)
# and in the repo. Import it by making that directory importable rather than copying
# merge() here: one merge implementation in the image is the whole point of shipping
# profile_overlay.py as a file (deploy/docker/Dockerfile deletes merge_configs.py from
# the runtime image for the same reason).
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from profile_overlay import load_yaml, merge  # noqa: E402

# Written beside the config so the next start can tell the runtime's edits apart from
# the image's and the operator's. Dot-prefixed and inside $HERMES_HOME: it is per-volume
# state, not something the image ships or the operator renders.
BASELINE_FILENAME = ".default-config-baseline.yaml"


class _Absent:
    """Distinguishes "no value here" from "the value None"."""

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _Absent()


def reconcile(base, ours, theirs):
    """Three-way merge: carry the runtime's edits onto the new baseline.

    `base` is the previous baseline, `ours` the live config, `theirs` the new baseline.
    See the module docstring for the per-key rules. Nothing is mutated.
    """
    if not isinstance(ours, dict):
        return copy.deepcopy(theirs)
    return _reconcile_dict(
        base if isinstance(base, dict) else {},
        ours,
        theirs if isinstance(theirs, dict) else {},
    )


def _reconcile_dict(base, ours, theirs):
    out = copy.deepcopy(theirs)
    for key, ours_v in ours.items():
        base_v = base.get(key, _ABSENT)
        theirs_v = theirs.get(key, _ABSENT)

        # Not a runtime edit at all — whatever the new baseline says stands.
        if base_v is not _ABSENT and ours_v == base_v:
            continue

        if theirs_v is _ABSENT:
            if base_v is _ABSENT:
                # Neither baseline has ever mentioned this key, so it is runtime state
                # and nothing else can restore it. This is the `/sethome` case.
                out[key] = copy.deepcopy(ours_v)
            # Otherwise the baseline dropped a key it used to set: honour the removal.
            continue

        if isinstance(ours_v, dict) and isinstance(theirs_v, dict):
            out[key] = _reconcile_dict(base_v if isinstance(base_v, dict) else {}, ours_v, theirs_v)
            continue

        if isinstance(ours_v, list) and isinstance(theirs_v, list):
            out[key] = _reconcile_list(base_v if isinstance(base_v, list) else [], ours_v, theirs_v)
            continue

        # Scalar (or a type change, which is the same decision). The runtime only wins
        # where the baseline has not moved; if both changed, the operator is mid-
        # reconcile and wins.
        if base_v is _ABSENT or theirs_v != base_v:
            continue
        out[key] = copy.deepcopy(ours_v)
    return out


def _reconcile_list(base, ours, theirs):
    """Lists: verbatim when the baseline held still, baseline + runtime additions when not.

    Holding still has to mean the runtime list wins outright, removals included. Hermes
    rewrites whole lists — hermes_cli.tools_config._save_platform_tools writes a shorter
    platform_toolsets entry when a toolset is switched off — and a union would put the
    removed entry back on every restart, with nothing to say why.
    """
    if theirs == base:
        return copy.deepcopy(ours)
    added = [x for x in ours if x not in base and x not in theirs]
    return copy.deepcopy(theirs) + copy.deepcopy(added)


def adopt(ours, theirs):
    """No recorded baseline: take the declarations, keep what only the runtime knows.

    Used on the first start on a volume, where `reconcile`'s three-way rules have no
    third input. See the module docstring for why the image config must not be
    substituted for it. Nothing is mutated.
    """
    if not isinstance(ours, dict):
        return copy.deepcopy(theirs)
    out = copy.deepcopy(theirs) if isinstance(theirs, dict) else {}
    for key, ours_v in ours.items():
        theirs_v = out.get(key, _ABSENT)
        if theirs_v is _ABSENT:
            # Neither the image nor the operator has ever mentioned this key, so it is
            # runtime state and nothing else can restore it.
            out[key] = copy.deepcopy(ours_v)
        elif isinstance(ours_v, dict) and isinstance(theirs_v, dict):
            # Recurse: a declared subtree can still hold undeclared runtime keys, which
            # is precisely where `/sethome` writes (platforms.<adapter>.home_channel).
            out[key] = adopt(ours_v, theirs_v)
    return out


def build_baseline(image_path, overlay_paths):
    """The config as the image and the operator jointly declare it, with no runtime state.

    profile_overlay.merge is the merge every other profile gets: dicts merge, lists union,
    scalars replace. Union is right here because both sides are declarations of intent —
    the removal problem it creates belongs to whoever edits the two declarations, and
    TestRenderConfigYAMLListsMatchChatConfig in the operator keeps them from diverging.
    """
    baseline = copy.deepcopy(load_yaml(pathlib.Path(image_path)))
    for path in overlay_paths:
        path = pathlib.Path(path)
        if not path.is_file():
            continue
        overlay = load_yaml(path)
        if overlay:
            baseline = merge(baseline, overlay)
    return baseline


def _atomic_write_yaml(path: pathlib.Path, data) -> None:
    """Stage and rename. A torn write here leaves the front door with no config at all.

    The staging name carries a uuid, the way multiuser_memory's writer does. A FIXED
    `config.yaml.tmp` is only atomic while exactly one writer exists, and the step-1.6
    bootstrap lock that normally guarantees that has three documented ways to let two
    through — no `flock` binary, an unwritable $TARGET_DIR, or the `flock -w 300` timeout,
    which says outright that it is "proceeding concurrently with the peer container".
    Sharing one staging path across those writers is what turns a survivable repeat into
    the failure this docstring warns about: the loser's `os.replace` fires on a name the
    winner already renamed away, and a reader between the two sees config.yaml at zero
    bytes. Per-writer names cost nothing and make the repeat harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(yaml.safe_dump(data, sort_keys=True))
        os.replace(tmp, path)
    except BaseException:
        # A staging file left behind is not inert: it sits in $TARGET_DIR beside the real
        # config, and the uuid means nothing will ever reuse or overwrite it.
        tmp.unlink(missing_ok=True)
        raise


def rebuild(config_path, image_path, overlay_paths=(), baseline_path=None) -> str:
    """Rewrite `config_path` from the image, the operator overlays and the runtime's edits.

    Returns a one-line status for the startup log.
    """
    config_path = pathlib.Path(config_path)
    image_path = pathlib.Path(image_path)
    if baseline_path is None:
        baseline_path = config_path.with_name(BASELINE_FILENAME)
    baseline_path = pathlib.Path(baseline_path)

    if not image_path.is_file():
        raise FileNotFoundError(f"no image config at {image_path}")

    baseline = build_baseline(image_path, overlay_paths)

    live = load_yaml(config_path)

    # No record of a previous baseline means this is the first start on this volume, so
    # there is no third input to merge against and `adopt` decides instead.
    if baseline_path.is_file():
        config = reconcile(load_yaml(baseline_path), live, baseline)
    else:
        config = adopt(live, baseline)

    _atomic_write_yaml(config_path, config)
    _atomic_write_yaml(baseline_path, baseline)

    applied = [str(p) for p in overlay_paths if pathlib.Path(p).is_file()]
    carried = sum(1 for k, v in config.items() if baseline.get(k, _ABSENT) != v)
    return f"rebuilt from {image_path} + {applied or 'no overlay'}; {carried} top-level key(s) carry runtime state"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True, help="the profile's live config.yaml (rewritten in place)")
    ap.add_argument("--image", required=True, help="the image's copy of the config, used as the merge base")
    ap.add_argument("--overlay", action="append", default=[],
                    help="Operator-rendered overlay to merge. Repeatable; later files win. "
                         "Missing files are skipped, which is what 'no operator' has to mean.")
    ap.add_argument("--baseline", default=None,
                    help=f"where to record the merged baseline (default: {BASELINE_FILENAME} "
                         "beside --config)")
    args = ap.parse_args(argv)

    # Deliberately not wrapped in try/except: the caller reports the consequence and the
    # traceback is the only evidence of the cause. A swallowed failure here is how the
    # config.yaml EACCES this replaces went unnoticed for the life of a deployment.
    print(rebuild(args.config, args.image, args.overlay, args.baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
