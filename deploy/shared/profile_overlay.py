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


def unapply(current, applied):
    """Remove values a previous overlay contributed, leaving everything else intact.

    Only strips a value that still matches what was applied, so a later edit is never
    clobbered. Returns the pruned structure, or the sentinel _DROP when the whole node
    should disappear.
    """
    if isinstance(applied, dict) and isinstance(current, dict):
        for k, v in applied.items():
            if k not in current:
                continue
            pruned = unapply(current[k], v)
            if pruned is _DROP:
                del current[k]
            else:
                current[k] = pruned
        # An emptied container we created is noise; drop it too.
        return _DROP if not current else current
    if isinstance(applied, list) and isinstance(current, list):
        remaining = [x for x in current if x not in applied]
        return _DROP if not remaining else remaining
    # Scalars: only remove if unchanged since we wrote it.
    return _DROP if current == applied else current


class _Drop:
    def __repr__(self):  # pragma: no cover - debugging aid
        return "<drop>"


_DROP = _Drop()


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

    previous = None
    if state_path.is_file():
        try:
            previous = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            previous = None

    if previous:
        pruned = unapply(config, previous)
        config = {} if pruned is _DROP else pruned

    overlay = None
    if overlay_path is not None and overlay_path.is_file():
        overlay = load_yaml(overlay_path)
        if overlay:
            config = merge(config, overlay)

    # Stage and rename: a torn write here leaves a profile with no config at all.
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(config, sort_keys=True))
    os.replace(tmp, config_path)

    if overlay:
        state_tmp = state_path.with_name(state_path.name + ".tmp")
        state_tmp.write_text(json.dumps(overlay, sort_keys=True))
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
