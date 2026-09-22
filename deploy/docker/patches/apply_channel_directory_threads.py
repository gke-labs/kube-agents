#!/usr/bin/env python3
"""Wire gateway/channel_directory_threads.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Five small edits
across two functions of ``gateway/channel_directory.py``, plus an import placed
before the first function that uses it.

v2026.9.14 split the old monolithic ``_build_slack`` (upstream 24fd09be10,
"split _build_slack", then dc40ec89e9, "collapse ... slack resolve branches"):
the ``users.conversations`` listing lives in ``_slack_team_channels``, the
``conversations.info`` probing in ``_slack_resolve_raw_names``, and the base-id
helper is now ``_slack_base_id``. ``_build_slack`` itself keeps only the merge
of session-derived entries. This applier follows that split: one edit in the
merge (edit 1) and four inside ``_slack_resolve_raw_names`` (edits 2-5).

Every anchor is literal because in every one the text being replaced *is* the
edit: an assignment that overwrites the whole name, a ``return`` that forgets
the miss, an ``except`` that forgets the miss, a grouping that never consults
the miss cache. None is a call site ``find_call`` can select on, so the slice is
the anchor. Each is a few lines rather than the whole function so that a
compaction pass upstream that touches, say, the ``users.info`` branch does not
by itself invalidate the anchors around it; every anchor is still exact-count
checked, so a change *inside* one fails the build rather than the behaviour.

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

#: The first function that uses the imported helpers; the import lands just
#: above it. ``_build_slack`` uses ``_rename_entry`` too, and is defined after it.
FIRST_USER = "_slack_resolve_raw_names"

IMPORT_PREAMBLE = (
    "# kube-agents patch: see gateway/channel_directory_threads.py\n"
    "from gateway.channel_directory_threads import (\n"
    "    note_resolved as _note_resolved,\n"
    "    note_unresolvable as _note_unresolvable,\n"
    "    rename_entry as _rename_entry,\n"
    "    unresolved_by_channel as _unresolved_by_channel,\n"
    ")\n"
    "\n"
    "\n"
)

#: Text that only exists after a successful run. Edit 1 consumes its anchor, so
#: a second run would fail on the count anyway; this names the reason instead.
PATCHED_MARKER = "from gateway.channel_directory_threads import"

# --- Edit 1: the cheap lookup in _build_slack, and the name it overwrites -----
# Upstream keys this lookup on the base id itself (its own `_slack_base_id`),
# so the "resolve against the channel, not the thread" half of this patch is
# upstream's now and is kept as upstream wrote it. What is left is the
# assignment: `entry["name"] = <channel>` throws away the ` / topic <ts>` label
# that is the only thing telling two entries on the same channel apart, and
# _rename_entry keeps it.
OLD_LOOKUP = '''        if _slack_has_raw_name(entry) and _slack_base_id(eid) in api_name_lookup:
            entry["name"] = api_name_lookup[_slack_base_id(eid)]
'''

NEW_LOOKUP = '''        # kube-agents patch: see gateway/channel_directory_threads.py --
        # the entry's thread label has to survive the rename.
        if _slack_has_raw_name(entry) and _slack_base_id(eid) in api_name_lookup:
            _rename_entry(entry, api_name_lookup[_slack_base_id(eid)])
'''

# --- Edits 2-5: the storm, inside _slack_resolve_raw_names --------------------
# Upstream groups the probes by base conversation and gathers them concurrently,
# which absorbs the "one probe per channel, addressed to the channel" half of
# this patch. Upstream's shape is kept, including the concurrency, which this
# patch never had. What upstream still has no answer for is repetition: a
# channel the bot cannot resolve is re-probed on every five-minute refresh
# forever, which is the unbounded part of the 9,500-calls-a-day figure in the
# module docstring. So the work list comes from _unresolved_by_channel (miss
# cache + per-refresh cap) and every dead end records itself.

# Edit 2: the work list. `dict(...)` keeps upstream's `.items()` at the gather
# untouched; the helper returns pairs in first-seen order, and a dict keeps it.
OLD_GROUPING = '''    unresolved_by_base: Dict[str, list] = {}
    for entry in channels:
        if _slack_has_raw_name(entry):
            unresolved_by_base.setdefault(_slack_base_id(entry["id"]), []).append(entry)
    if not unresolved_by_base:
        return
'''

NEW_GROUPING = '''    # kube-agents patch: see gateway/channel_directory_threads.py -- the grouping
    # helper also drops channels that failed recently and caps how many are
    # probed in one refresh, and every dead end below records itself so the
    # cache can do its job.
    unresolved_by_base: Dict[str, list] = dict(_unresolved_by_channel(channels))
    if not unresolved_by_base:
        return
'''

# Edit 3: a not-ok answer (channel_not_found, missing_scope) is a dead end.
OLD_NOT_OK = '''            if not resp.get("ok"):
                return
'''

NEW_NOT_OK = '''            if not resp.get("ok"):
                _note_unresolvable(base_id, resp.get("error", "not ok"))
                return
'''

# Edit 4: the rename, and the two outcomes it has to record. An ok response
# with nothing usable in it (an IM with no peer user, a users.info that failed)
# would otherwise leave the channel unresolved AND unsuppressed, re-probed on
# every refresh forever, holding a slot under MAX_PROBES_PER_REFRESH a
# resolvable channel could use.
OLD_RENAME = '''            for entry in entries if resolved_name else ():
                entry["name"] = resolved_name
                if resolved_type:
                    entry["type"] = resolved_type
'''

NEW_RENAME = '''            for entry in entries if resolved_name else ():
                _rename_entry(entry, resolved_name, resolved_type or "")
            if resolved_name:
                _note_resolved(base_id)
            else:
                _note_unresolvable(base_id, "no name in conversations.info")
'''

# Edit 5: a raised SlackApiError (the proxy's 502) is a dead end too.
OLD_EXCEPT = '''        except Exception as e:
            logger.debug("Channel directory: failed to resolve %s: %s", base_id, e)
'''

NEW_EXCEPT = '''        except Exception as e:
            _note_unresolvable(base_id, e)
            logger.debug("Channel directory: failed to resolve %s: %s", base_id, e)
'''


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="channel_directory_threads")
    patch.refuse_if_patched(PATCHED_MARKER)

    site = patch.find_def(FIRST_USER, label="conversations.info resolver")
    patch.insert(site.start, IMPORT_PREAMBLE)

    patch.substitute(OLD_LOOKUP, NEW_LOOKUP, label="session-entry name lookup")
    patch.substitute(OLD_GROUPING, NEW_GROUPING, label="resolver work list")
    patch.substitute(OLD_NOT_OK, NEW_NOT_OK, label="resolver not-ok return")
    patch.substitute(OLD_RENAME, NEW_RENAME, label="resolver rename loop")
    patch.substitute(OLD_EXCEPT, NEW_EXCEPT, label="resolver except")

    patch.commit("5 anchors + 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
