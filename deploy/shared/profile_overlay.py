#!/usr/bin/env python3
"""Merge an operator-rendered config overlay into a Hermes profile's config.yaml.

Called by docker-entrypoint.sh once per targeted profile at pod startup, after the
image force-sync. Lives in its own file rather than inline in the entrypoint so it can
be unit tested — see tests/test_profile_overlay.py.

Why a last-applied record
-------------------------
Merging alone is not reversible, and only some profiles get rebuilt from the image on
every start. The platform profile's config.yaml IS force-synced, so a removed overlay
disappears with it. Cluster profiles' config.yaml is deliberately NOT force-synced —
it carries the runtime `cluster_identity` stamp that cluster_agent_reconcile.py matches
a profile to its cluster by — so a merged value would otherwise persist forever. Delete
`tuning.cluster` from the CR and every cluster profile would silently keep the old
limits, quietly breaking the "unset means Hermes defaults" contract.

So each merge records what it applied in `.operator-overlay.json` beside the config.
The next run subtracts that record before applying the current overlay. Subtraction is
conservative: a key is only removed if its current value still equals what we wrote, so
an operator, a human, or another startup step that has since changed it wins.

Usage:
    profile_overlay.py --profile-dir DIR [--overlay FILE]

Omitting --overlay (or passing one that does not exist) unapplies the previous overlay
and writes nothing new, which is what "the overlay was removed" has to mean.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys

import yaml

# Mirrors the CRD pattern on spec.targetProfile. Enforced again here because the
# entrypoint derives the profile name from a ConfigMap key, and ConfigMap keys legally
# contain dots — "profile-...overlay.yaml" would otherwise resolve to ".." and walk out
# of the profiles directory.
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

STATE_FILENAME = ".operator-overlay.json"


def valid_profile_name(name: str) -> bool:
    return bool(name) and name != "default" and PROFILE_NAME_RE.fullmatch(name) is not None


class _Drop:
    def __repr__(self):  # pragma: no cover - debugging aid
        return "<drop>"


class _Absent:
    """Distinguishes "the config had no value here" from "it had None"."""

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<absent>"


_DROP = _Drop()
_ABSENT = _Absent()


def merge(base, overlay):
    """Recursive merge. Dicts merge, lists union (preserving order), scalars replace.

    List union matches deploy/docker/merge_configs.py, and is what plugins.enabled
    wants: the image's built-ins plus whatever the operator adds.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        for k, v in overlay.items():
            base[k] = merge(base[k], v) if k in base else v
        return base
    if isinstance(base, list) and isinstance(overlay, list):
        return list(dict.fromkeys(base + overlay))
    return overlay


def unapply(current, applied, before=_ABSENT):
    """Undo a previous overlay, restoring what the config held before it.

    Subtracting the overlay is not enough, because an overlay and the image can ask for
    the same thing. If the image already lists a toolset that a plugin also declares,
    removing the plugin must not take the image's entry with it; if the image sets a
    scalar the overlay overrode, removal has to put the image's value back rather than
    delete the key. So `before` carries the pre-merge value and this restores it.

    Nothing is touched if the current value no longer matches what was applied — an
    operator, a human, or a later startup step that has since changed it wins.

    Returns the pruned structure, or the sentinel _DROP when the node should disappear.
    """
    if isinstance(applied, dict) and isinstance(current, dict):
        for k, v in applied.items():
            if k not in current:
                continue
            prior = before[k] if isinstance(before, dict) and k in before else _ABSENT
            pruned = unapply(current[k], v, prior)
            if pruned is _DROP:
                del current[k]
            else:
                current[k] = pruned
        # An emptied container we created is noise; a pre-existing empty one is not.
        if not current:
            return _DROP if before is _ABSENT else before
        return current

    if isinstance(applied, list) and isinstance(current, list):
        # Only remove entries this overlay actually introduced. Anything the config
        # already had stays, even though the overlay named it too.
        prior = before if isinstance(before, list) else []
        added = [x for x in applied if x not in prior]
        remaining = [x for x in current if x not in added]
        if not remaining:
            return _DROP if before is _ABSENT else before
        return remaining

    # Scalar: restore the prior value, or drop the key if there was none.
    if current != applied:
        return current
    return _DROP if before is _ABSENT else before



def snapshot(config, overlay):
    """Capture just the parts of `config` an overlay is about to cover.

    Keeping only the touched paths means the record stays small and, more importantly,
    that unapply never reasons about config it did not change.
    """
    if not isinstance(overlay, dict) or not isinstance(config, dict):
        return config
    out = {}
    for k, v in overlay.items():
        if k not in config:
            continue
        out[k] = snapshot(config[k], v) if isinstance(v, dict) else config[k]
    return out


def read_state(state_path: pathlib.Path) -> dict:
    """Load the last-applied record, tolerating corruption and the older format.

    The first version of this file stored the bare overlay. Those records have no
    `before`, so they unapply by subtraction — the pre-fix behaviour — for one startup,
    after which the richer record takes over.
    """
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "overlay" in data:
        return data
    return {"overlay": data, "before": {}}


def load_yaml(path: pathlib.Path):
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def apply_overlay(profile_dir: pathlib.Path, overlay_path: pathlib.Path | None):
    """Reconcile one profile's config to the given overlay. Returns a status string."""
    config_path = profile_dir / "config.yaml"
    state_path = profile_dir / STATE_FILENAME

    if not config_path.is_file():
        return f"skipped: no config at {config_path}"

    config = load_yaml(config_path)

    state = read_state(state_path)
    if state.get("overlay"):
        pruned = unapply(config, state["overlay"], state.get("before", {}))
        config = {} if pruned is _DROP else pruned

    overlay = None
    if overlay_path is not None and overlay_path.is_file():
        overlay = load_yaml(overlay_path)

    # Snapshot what the overlay is about to cover, so the next run can put it back
    # rather than merely subtracting — the image and an overlay can name the same
    # toolset or set the same key, and removal must not take the image's copy with it.
    before = snapshot(config, overlay) if overlay else {}
    if overlay:
        config = merge(config, overlay)

    # Stage and rename: a torn write here leaves a profile with no config at all.
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(config, sort_keys=True))
    os.replace(tmp, config_path)

    if overlay:
        state_tmp = state_path.with_name(state_path.name + ".tmp")
        state_tmp.write_text(json.dumps({"overlay": overlay, "before": before}, sort_keys=True))
        os.replace(state_tmp, state_path)
        return f"applied {sorted(overlay)}"

    if state_path.exists():
        state_path.unlink()
        return "unapplied previous overlay"
    return "no overlay"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--overlay", default=None)
    args = ap.parse_args(argv)

    profile_dir = pathlib.Path(args.profile_dir)
    name = profile_dir.name
    if not valid_profile_name(name):
        print(f"refusing to touch profile directory {name!r}: not a valid profile name", file=sys.stderr)
        return 2

    overlay_path = pathlib.Path(args.overlay) if args.overlay else None
    try:
        status = apply_overlay(profile_dir, overlay_path)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the entrypoint
        print(f"overlay merge failed for {name}: {exc}", file=sys.stderr)
        return 1
    print(f"{name}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
