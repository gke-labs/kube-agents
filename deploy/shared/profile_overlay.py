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

So each merge records, in `.operator-overlay.json` beside the config, both what it
applied and what the config held beforehand. The next run undoes that record before
applying the current overlay.

Undoing restores rather than subtracts, because an overlay and the image can name the
same thing. A plugin declaring a toolset the profile already listed must not take the
image's entry with it on removal, and an overlay that overrode a scalar has to put the
image's value back rather than delete the key. Restoration is also conservative: a
value is only touched if it still equals what we wrote, so an operator, a human, a
later image, or another startup step that has since changed it wins.

Usage:
    profile_overlay.py --profile-dir DIR [--profile-name NAME]
                       [--overlay FILE ...] [--overlay-dir DIR]

Omitting both (or naming files that do not exist) unapplies the previous overlay and
writes nothing new, which is what "the overlay was removed" has to mean.
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

# Overlay file naming, mirroring profileOverlayKey and clusterProfileClassKey in the
# operator's platformagent_manifests.go — it writes these ConfigMap keys and this is what
# reads them back, so the two must change together.
OVERLAY_SUFFIX = ".overlay.yaml"
PROFILE_OVERLAY_PREFIX = "profile-"
CLUSTER_PROFILE_PREFIX = "cluster-"
CLUSTER_CLASS_OVERLAY = "profileclass-cluster" + OVERLAY_SUFFIX

# Where the config ConfigMap is mounted (the operator's profileOverlayDir).
DEFAULT_OVERLAY_DIR = "/opt/agent-config"


# The front door. It has no directory under $HERMES_HOME/profiles — its home IS
# $HERMES_HOME — so the entrypoint passes it by --profile-name rather than letting the
# name be read off the directory (which would be "data").
DEFAULT_PROFILE_NAME = "default"


def valid_profile_name(name: str) -> bool:
    """True for a NAMED profile, i.e. one with a directory under profiles/.

    "default" is deliberately excluded: nothing may scaffold profiles/default, and an
    AgentPlugin naming it is rejected at admission. The front door is reached through
    --profile-name instead, which accepts it by name and nothing else.
    """
    return bool(name) and name != DEFAULT_PROFILE_NAME and PROFILE_NAME_RE.fullmatch(name) is not None


def overlays_for(name: str, overlay_dir) -> list[pathlib.Path]:
    """Return the overlays that apply to profile `name`, least specific first.

    A cluster profile takes two: the class overlay carrying `spec.harness.tuning.cluster`
    — the operator cannot name cluster profiles individually, they are scaffolded at
    runtime, one per managed cluster — and then its own `profile-<name>` overlay if a
    plugin targets that specific cluster. The per-profile file merges last, so it wins a
    scalar conflict with the class-wide policy.

    Resolving only the class overlay for a `cluster-*` name, which is what the entrypoint
    used to do, left a plugin targeting a named cluster profile mounted but missing from
    that profile's `plugins.enabled`. Hermes only calls `register(ctx)` for enabled
    plugins, so the plugin was inert and nothing said so.
    """
    overlay_dir = pathlib.Path(overlay_dir)
    candidates = []
    if name.startswith(CLUSTER_PROFILE_PREFIX):
        candidates.append(overlay_dir / CLUSTER_CLASS_OVERLAY)
    candidates.append(overlay_dir / f"{PROFILE_OVERLAY_PREFIX}{name}{OVERLAY_SUFFIX}")
    return [p for p in candidates if p.is_file()]


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


def _as_paths(overlays) -> list[pathlib.Path]:
    """Accept None, a single path, or an iterable of them (what `overlays_for` returns)."""
    if overlays is None:
        return []
    if isinstance(overlays, (str, os.PathLike)):
        overlays = [overlays]
    return [pathlib.Path(p) for p in overlays if p is not None]


def load_overlays(overlays) -> dict | None:
    """Merge the overlay files into the single effective overlay to apply; later wins.

    Combining before applying — rather than applying each file in turn — keeps one
    last-applied record per profile. Separate records would have to be unapplied in exact
    reverse order to restore the config, and the set of files changes between starts, so
    that order is not reconstructable after the fact.
    """
    combined: dict = {}
    for path in overlays:
        loaded = load_yaml(path) if path.is_file() else None
        if loaded:
            combined = merge(combined, loaded)
    return combined or None


def apply_overlay(profile_dir: pathlib.Path, overlays=None):
    """Reconcile one profile's config to the given overlay(s). Returns a status string.

    `overlays` is None, one path, or several merged in order (later wins).
    """
    profile_dir = pathlib.Path(profile_dir)
    config_path = profile_dir / "config.yaml"
    state_path = profile_dir / STATE_FILENAME

    if not config_path.is_file():
        return f"skipped: no config at {config_path}"

    config = load_yaml(config_path)

    state = read_state(state_path)
    if state.get("overlay"):
        pruned = unapply(config, state["overlay"], state.get("before", {}))
        config = {} if pruned is _DROP else pruned

    overlay = load_overlays(_as_paths(overlays))

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


def sync_profile(profile_dir, overlay_dir=DEFAULT_OVERLAY_DIR) -> str:
    """Apply whatever the operator currently says about this profile. Returns a status.

    The single entry point for both callers: docker-entrypoint.sh sweeps every profile at
    pod startup, and cluster_agent_profile.create_profile calls this for a cluster profile
    scaffolded later, at runtime. Startup-only application meant a cluster onboarded
    between two pod starts ran on Hermes' defaults — 3 retries, 90 turns — no matter what
    `spec.harness.tuning.cluster` said, until something unrelated restarted the pod.
    """
    profile_dir = pathlib.Path(profile_dir)
    if not valid_profile_name(profile_dir.name):
        raise ValueError(f"not a valid profile name: {profile_dir.name!r}")
    return apply_overlay(profile_dir, overlays_for(profile_dir.name, overlay_dir))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile-dir", required=True)
    ap.add_argument("--profile-name", default=None,
                    help="Profile the directory belongs to, when it cannot be read off "
                         f"the directory name. Only {DEFAULT_PROFILE_NAME!r} needs this: "
                         "its home is $HERMES_HOME itself.")
    ap.add_argument("--overlay", action="append", default=None,
                    help="Overlay file to merge. Repeatable; later files win.")
    ap.add_argument("--overlay-dir", default=None,
                    help="Directory of operator-rendered overlays (in the pod: "
                         f"{DEFAULT_OVERLAY_DIR}). The ones that apply are resolved from "
                         "the profile's name, so a cluster profile picks up both the "
                         "cluster class overlay and its own.")
    args = ap.parse_args(argv)

    profile_dir = pathlib.Path(args.profile_dir)
    name = args.profile_name or profile_dir.name
    if name != DEFAULT_PROFILE_NAME and not valid_profile_name(name):
        print(f"refusing to touch profile directory {name!r}: not a valid profile name", file=sys.stderr)
        return 2

    # Name-resolved first, explicit --overlay last, so an explicitly named file wins.
    overlays = _as_paths(args.overlay)
    if args.overlay_dir:
        overlays = overlays_for(name, args.overlay_dir) + overlays
    try:
        status = apply_overlay(profile_dir, overlays)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the entrypoint
        print(f"overlay merge failed for {name}: {exc}", file=sys.stderr)
        return 1
    print(f"{name}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
