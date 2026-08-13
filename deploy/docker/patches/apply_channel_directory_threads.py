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

# --- Edit 1: the cheap lookup, which never hit ------------------------------
# api_name_lookup is keyed by channel id; eid carries a `:<thread>` suffix for
# every session-derived entry, so this branch could only ever match entries that
# had no thread. Keying on the base id makes it match, which also means fewer
# entries reach the API round trip below.
OLD_LOOKUP = '''            # If the entry name is still a raw Slack ID (e.g. C0xxx / D0xxx),
            # try to resolve it from the API lookup first.
            if entry.get("name", "").startswith(("C0", "D0", "G0")):
                if eid in api_name_lookup:
                    entry["name"] = api_name_lookup[eid]
'''

NEW_LOOKUP = '''            # If the entry name is still a raw Slack ID (e.g. C0xxx / D0xxx),
            # try to resolve it from the API lookup first.
            # kube-agents patch: see gateway/channel_directory_threads.py --
            # eid may be "<channel>:<thread>", and the lookup is keyed by channel.
            if entry.get("name", "").startswith(("C0", "D0", "G0")):
                _base_eid = _base_channel_id(eid)
                if _base_eid in api_name_lookup:
                    _rename_entry(entry, api_name_lookup[_base_eid])
'''

# --- Edit 2: the storm ------------------------------------------------------
OLD_RESOLVE = '''    # Resolve remaining raw-ID entries (DMs, private channels not in bot scope)
    # by calling conversations.info + users.info for each.
    unresolved = [ch for ch in channels if ch.get("name", "").startswith(("C0", "D0", "G0"))]
    if unresolved and team_clients:
        client = next(iter(team_clients.values()))
        for entry in unresolved:
            try:
                resp = await client.conversations_info(channel=entry["id"])
                if not resp.get("ok"):
                    continue
                ch_info = resp.get("channel", {})
                if ch_info.get("is_im"):
                    peer_user = ch_info.get("user", "")
                    if peer_user:
                        user_resp = await client.users_info(user=peer_user)
                        if user_resp.get("ok"):
                            u = user_resp["user"]
                            entry["name"] = (
                                u.get("profile", {}).get("display_name")
                                or u.get("real_name")
                                or u.get("name")
                                or entry["id"]
                            )
                            entry["type"] = "dm"
                else:
                    entry["name"] = ch_info.get("name") or ch_info.get("name_normalized") or entry["id"]
            except Exception as e:
                logger.debug("Channel directory: failed to resolve %s: %s", entry["id"], e)
                continue

'''

# One probe per channel rather than one per entry, addressed to the channel
# rather than to the thread, and not repeated for a channel that just failed.
# The loop variable is now (channel_id, entries): whatever the probe learns is
# applied to every entry on that channel, which is the reason the call count
# drops from one-per-thread to one-per-channel.
NEW_RESOLVE = '''    # Resolve remaining raw-ID entries (DMs, private channels not in bot scope)
    # by calling conversations.info + users.info for each.
    # kube-agents patch: see gateway/channel_directory_threads.py -- one probe
    # per channel, addressed to the channel and not to the thread within it.
    unresolved = _unresolved_by_channel(channels)
    if unresolved and team_clients:
        client = next(iter(team_clients.values()))
        for _channel_id, _entries in unresolved:
            try:
                resp = await client.conversations_info(channel=_channel_id)
                if not resp.get("ok"):
                    _note_unresolvable(_channel_id, resp.get("error", "not ok"))
                    continue
                ch_info = resp.get("channel", {})
                if ch_info.get("is_im"):
                    # Both dead ends record the miss. A probe that returns
                    # neither outcome leaves the channel unresolved AND
                    # unsuppressed, so it is re-probed on every refresh forever
                    # and holds a slot under MAX_PROBES_PER_REFRESH that a
                    # resolvable channel could have used.
                    peer_user = ch_info.get("user", "")
                    if not peer_user:
                        _note_unresolvable(_channel_id, "im with no peer user")
                        continue
                    user_resp = await client.users_info(user=peer_user)
                    if not user_resp.get("ok"):
                        _note_unresolvable(
                            _channel_id,
                            "users.info: " + str(user_resp.get("error", "not ok")),
                        )
                        continue
                    u = user_resp["user"]
                    _resolved = (
                        u.get("profile", {}).get("display_name")
                        or u.get("real_name")
                        or u.get("name")
                        or _channel_id
                    )
                    for _entry in _entries:
                        _rename_entry(_entry, _resolved, "dm")
                    _note_resolved(_channel_id)
                else:
                    _resolved = (
                        ch_info.get("name") or ch_info.get("name_normalized") or _channel_id
                    )
                    for _entry in _entries:
                        _rename_entry(_entry, _resolved)
                    _note_resolved(_channel_id)
            except Exception as e:
                _note_unresolvable(_channel_id, e)
                logger.debug("Channel directory: failed to resolve %s: %s", _channel_id, e)
                continue

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
