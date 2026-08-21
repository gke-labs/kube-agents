#!/usr/bin/env python3
"""Wire gateway/channel_directory_threads.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two edits, both
inside ``_build_slack``, plus an import placed before the function that uses it.

Both anchors are literal because in both the text being replaced *is* the edit:
the first passes a thread-qualified entry id to a lookup keyed by channel id, and
the second passes it to ``conversations.info``. Neither is a call site
``find_call`` can select on -- ``conversations_info`` takes one keyword and the
bug is the value of that keyword -- so the slice is the anchor.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/channel_directory_threads.py``. Usage::

    python3 apply_channel_directory_threads.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "gateway/channel_directory.py"

IMPORT_PREAMBLE = (
    "# kube-agents patch: see gateway/channel_directory_threads.py\n"
    "from gateway.channel_directory_threads import (\n"
    "    base_channel_id as _base_channel_id,\n"
    "    note_resolved as _note_resolved,\n"
    "    note_unresolvable as _note_unresolvable,\n"
    "    rename_entry as _rename_entry,\n"
    "    unresolved_by_channel as _unresolved_by_channel,\n"
    ")\n"
    "\n"
    "\n"
)

# --- Edit 1: the cheap lookup, and the name it overwrites -------------------
# This patch used to key the lookup on the base id here: api_name_lookup is
# keyed by channel id, eid carries a `:<thread>` suffix for every
# session-derived entry, and the branch could only ever match entries with no
# thread. v2026.8.13 does that itself, via its own `slack_lookup_id` — kept as
# upstream wrote it. What is left is the assignment: `entry["name"] = <channel>`
# throws away the ` / topic <ts>` label that is the only thing telling two
# entries on the same channel apart, and _rename_entry keeps it.
OLD_LOOKUP = '''            # If the entry name is still a raw Slack ID (e.g. C0xxx / D0xxx),
            # try to resolve it from the API lookup using the base conversation ID.
            if entry.get("name", "").startswith(("C0", "D0", "G0")):
                base_id = slack_lookup_id(eid)
                if base_id in api_name_lookup:
                    entry["name"] = api_name_lookup[base_id]
'''

NEW_LOOKUP = '''            # If the entry name is still a raw Slack ID (e.g. C0xxx / D0xxx),
            # try to resolve it from the API lookup using the base conversation ID.
            # kube-agents patch: see gateway/channel_directory_threads.py --
            # the entry's thread label has to survive the rename.
            if entry.get("name", "").startswith(("C0", "D0", "G0")):
                base_id = slack_lookup_id(eid)
                if base_id in api_name_lookup:
                    _rename_entry(entry, api_name_lookup[base_id])
'''

# --- Edit 2: the storm ------------------------------------------------------
# v2026.8.13 rewrote this loop into a per-base-conversation coroutine gathered
# concurrently, which absorbs the "one probe per channel, addressed to the
# channel and not to the thread within it" half of this patch. Upstream's shape
# is kept, including the concurrency, which this patch never had. What upstream
# still has no answer for is repetition: a channel the bot cannot resolve is
# re-probed on every five-minute refresh forever, which is the unbounded part of
# the 9,500-calls-a-day figure in the module docstring. So the work list comes
# from _unresolved_by_channel (miss cache + per-refresh cap) and every dead end
# records itself.
OLD_RESOLVE = '''    # Resolve remaining raw-ID entries (DMs, private channels not in bot scope)
    # by calling conversations.info + users.info once per base conversation,
    # with all base-ID lookups running concurrently.
    unresolved = [ch for ch in channels if ch.get("name", "").startswith(("C0", "D0", "G0"))]
    if unresolved and team_clients:
        client = next(iter(team_clients.values()))
        unresolved_by_base = {}
        for entry in unresolved:
            unresolved_by_base.setdefault(slack_lookup_id(entry["id"]), []).append(entry)

        async def _resolve_base(base_id: str, entries: list) -> None:
            try:
                resp = await client.conversations_info(channel=base_id)
                if not resp.get("ok"):
                    return
                ch_info = resp.get("channel", {})
                resolved_name = None
                resolved_type = None
                if ch_info.get("is_im"):
                    peer_user = ch_info.get("user", "")
                    if peer_user:
                        user_resp = await client.users_info(user=peer_user)
                        if user_resp.get("ok"):
                            u = user_resp["user"]
                            resolved_name = (
                                u.get("profile", {}).get("display_name")
                                or u.get("real_name")
                                or u.get("name")
                            )
                            resolved_type = "dm"
                else:
                    resolved_name = ch_info.get("name") or ch_info.get("name_normalized")
                if resolved_name:
                    for entry in entries:
                        entry["name"] = resolved_name
                        if resolved_type:
                            entry["type"] = resolved_type
            except Exception as e:
                logger.debug("Channel directory: failed to resolve %s: %s", base_id, e)

        await asyncio.gather(
            *[_resolve_base(bid, ents) for bid, ents in unresolved_by_base.items()]
        )
'''

NEW_RESOLVE = '''    # Resolve remaining raw-ID entries (DMs, private channels not in bot scope)
    # by calling conversations.info + users.info once per base conversation,
    # with all base-ID lookups running concurrently.
    # kube-agents patch: see gateway/channel_directory_threads.py -- the grouping
    # helper below also drops channels that failed recently and caps how many are
    # probed in one refresh, and every dead end records itself so the cache can
    # do its job.
    unresolved = _unresolved_by_channel(channels)
    if unresolved and team_clients:
        client = next(iter(team_clients.values()))

        async def _resolve_base(base_id: str, entries: list) -> None:
            try:
                resp = await client.conversations_info(channel=base_id)
                if not resp.get("ok"):
                    _note_unresolvable(base_id, resp.get("error", "not ok"))
                    return
                ch_info = resp.get("channel", {})
                resolved_name = None
                resolved_type = None
                if ch_info.get("is_im"):
                    peer_user = ch_info.get("user", "")
                    if peer_user:
                        user_resp = await client.users_info(user=peer_user)
                        if user_resp.get("ok"):
                            u = user_resp["user"]
                            resolved_name = (
                                u.get("profile", {}).get("display_name")
                                or u.get("real_name")
                                or u.get("name")
                            )
                            resolved_type = "dm"
                else:
                    resolved_name = ch_info.get("name") or ch_info.get("name_normalized")
                if resolved_name:
                    for entry in entries:
                        _rename_entry(entry, resolved_name, resolved_type or "")
                    _note_resolved(base_id)
                else:
                    # Every path that ends without a name records the miss: an
                    # ok response with nothing usable in it (an IM with no peer
                    # user, a users.info that failed) would otherwise leave the
                    # channel unresolved AND unsuppressed, re-probed on every
                    # refresh forever, holding a slot under
                    # MAX_PROBES_PER_REFRESH a resolvable channel could use.
                    _note_unresolvable(base_id, "no name in conversations.info")
            except Exception as e:
                _note_unresolvable(base_id, e)
                logger.debug("Channel directory: failed to resolve %s: %s", base_id, e)

        await asyncio.gather(
            *[_resolve_base(bid, ents) for bid, ents in unresolved]
        )
'''


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="channel_directory_threads")

    site = patch.find_def("_build_slack", label="_build_slack")
    patch.insert(site.start, IMPORT_PREAMBLE)

    patch.substitute(OLD_LOOKUP, NEW_LOOKUP, label="session-entry name lookup")
    patch.substitute(OLD_RESOLVE, NEW_RESOLVE, label="conversations.info resolution")

    patch.commit("2 anchors + 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
