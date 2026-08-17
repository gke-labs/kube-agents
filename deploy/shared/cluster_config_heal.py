#!/usr/bin/env python3
"""Backfill image-owned MCP servers into a Cluster Agent config already on the PVC.

Called by docker-entrypoint.sh once per `profiles/cluster-*` at pod startup, in step
2.6 beside the `memory.provider` strip. Lives in its own file rather than inline in the
entrypoint so it can be unit tested — see tests/test_cluster_config_heal.py.

Why this exists
---------------
A cluster profile's `config.yaml` is the one image-managed file that is deliberately
NOT force-synced: it carries the runtime `cluster_identity` stamp that
cluster_agent_reconcile.py matches a profile to its cluster by, and overwriting it from
the template would erase that. Reconcile does not repair it either — it skips every
cluster already holding a profile (`existing_keys`), so `create_profile`'s overlay only
ever runs once, at onboarding.

The consequence is that a new MCP server added to the template reaches profiles created
after the upgrade and no others. That is how `notify` shipped: on an install with three
cluster profiles, the one scaffolded after the upgrade had `send_notification` and the
two older ones did not — while all three got the new persona telling them they MUST
call it, because personas ARE force-synced. An agent told to use a tool it does not
have writes the report and drops it, which is issue #630 with the fix installed.

Scope
-----
Additive only, and only for the names in `HEALED_SERVERS`. It adds a missing MCP server
block and the toolset entries that expose it; it never removes, never overwrites a
server the profile already declares, and never touches anything else in the file. A
profile that already has everything is not rewritten at all, so the common case — every
profile healed, every startup after the first — costs a read and nothing else.

Not a general "re-sync config.yaml from the template". That would clobber both the
identity stamp and step 2.7's operator overlay merge, and picking up template changes
wholesale is exactly what the no-force-sync rule is protecting the file from.

Usage:
    cluster_config_heal.py --profile-dir DIR [--template FILE]
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import yaml

DEFAULT_TEMPLATE = pathlib.Path("/opt/cluster-template/config.yaml")

# The MCP servers this backfills, and the `platform_toolsets` entry each is exposed
# through. Deliberately a fixed list rather than "every server in the template": a
# server the template gains for a reason that does not apply to an existing profile
# should not arrive by accident, and the toolset name is not derivable from the server
# name in general — Hermes maps it, and the config states it.
HEALED_SERVERS: dict[str, str] = {"notify": "mcp-notify"}


def load_yaml(path: pathlib.Path):
    if not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _mapping(container, key) -> dict:
    """`container[key]` if it is a mapping, else an empty one.

    `mcp_servers:` with nothing under it parses as None, not as {}. Reading it through
    here means `_pending` and `_apply` cannot disagree about what that means — and a key
    holding something stranger than a mapping reads as empty rather than raising, so one
    hand-mangled profile does not take the rest of the loop down with it.
    """
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def _pending(config: dict, template: dict):
    """Yield `(dotted path, mutation)` for everything missing from `config`.

    Deciding and mutating are separate so `heal` can answer "is there anything to do?"
    without having already done half of it — the no-op case must not rewrite the file —
    and so what it reports is the same traversal that produced what it wrote.
    """
    servers = _mapping(config, "mcp_servers")
    toolsets = _mapping(config, "platform_toolsets")
    template_servers = _mapping(template, "mcp_servers")
    template_toolsets = _mapping(template, "platform_toolsets")

    for name, toolset_entry in HEALED_SERVERS.items():
        if name not in template_servers:
            # The template dropped it. Nothing to backfill, and removing it from the
            # profile is not this script's job — additive only.
            continue
        if name not in servers:
            yield f"mcp_servers.{name}", ("server", name, template_servers[name])
        # The toolset entries are considered independently of the server block: a run
        # interrupted between the two writes, or a config hand-edited to drop one,
        # leaves a server that exists and is exposed nowhere.
        for toolset, entries in template_toolsets.items():
            if not isinstance(entries, list) or toolset_entry not in entries:
                continue
            current = toolsets.get(toolset)
            # A toolset the profile does not declare is not created. Its absence says
            # this profile is not exposed on that surface, and inventing the list would
            # hand it one the image never gave it.
            if isinstance(current, list) and toolset_entry not in current:
                yield (
                    f"platform_toolsets.{toolset}[{toolset_entry}]",
                    ("toolset", toolset, toolset_entry),
                )


def _apply(config: dict, pending) -> None:
    for _, (kind, key, value) in pending:
        if kind == "server":
            # Not setdefault: `mcp_servers:` with nothing under it is a key that exists
            # holding None, which setdefault hands straight back.
            if not isinstance(config.get("mcp_servers"), dict):
                config["mcp_servers"] = {}
            config["mcp_servers"][key] = value
        else:
            # `_pending` only yields this when the profile already declares the toolset
            # as a list, so there is nothing to create here.
            _mapping(config, "platform_toolsets")[key].append(value)


def heal(config_path: pathlib.Path, template_path: pathlib.Path) -> list[str]:
    """Add the missing servers and toolset entries. Returns what changed.

    Writes through a temp file and `os.replace`, as the `memory.provider` strip beside
    the call site does and for the same reason: a torn write here drops
    `cluster_identity`, and reconcile then treats the profile as unidentifiable — it
    scaffolds a duplicate AND stops pruning the orphan.
    """
    config = load_yaml(config_path)
    template = load_yaml(template_path)
    if config is None or template is None:
        return []

    pending = list(_pending(config, template))
    if not pending:
        return []
    _apply(config, pending)

    # sort_keys defaults to True, matching the two other writers of this file — the
    # `memory.provider` strip beside the call site, and profile_overlay.apply_overlay in
    # step 2.7. All three normalise it the same way, so a heal shows up as the keys it
    # added rather than as a whole-file reordering.
    tmp = config_path.with_name(config_path.name + ".heal.tmp")
    tmp.write_text(yaml.safe_dump(config), encoding="utf-8")
    os.replace(tmp, config_path)
    return [path for path, _ in pending]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    args = parser.parse_args(argv)

    config_path = pathlib.Path(args.profile_dir) / "config.yaml"
    changes = heal(config_path, pathlib.Path(args.template))
    if changes:
        print(f"[cluster-config-heal] {config_path}: added {', '.join(changes)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
