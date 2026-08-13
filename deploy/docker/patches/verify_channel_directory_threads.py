#!/usr/bin/env python3
"""Build gate for the channel-directory thread-id fix.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_channel_directory_threads.py``.

Textual checks would not have caught this bug and will not catch its return. The
original code was correct-looking -- it resolved every unresolved entry, handled
the error, and logged it -- and wrong only in the value it passed and the number
of times it passed it. So the check that matters is a count: drive the real
``_build_slack`` with the directory that produced the storm and see how many
Slack calls come out. Before the patch: 33, all failing. After: 1.

The one substitution is ``_build_from_sessions``, which reads ``state.db`` -- a
file that does not exist in the build. That replaces the *data source*, not the
code under test; ``_build_slack``, the merge, the grouping and the resolution
loop are all the real, patched ones.

Usage::

    cd /opt/hermes && python3 verify_channel_directory_threads.py
"""

from __future__ import annotations

import asyncio
import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


DM = "D0BKGRBM6RH"

#: The directory as it stood on 2026-08-09: 33 threads, one DM, 33 API calls.
STORM_ENTRIES = [
    {
        "id": f"{DM}:17862404{38 + i}.796879",
        "name": f"{DM} / topic 17862404{38 + i}.796879",
        "type": "dm",
    }
    for i in range(33)
]


class FakeClient:
    """A Slack web client that answers the way the real workspace does.

    ``users.conversations`` is refused for want of a scope -- which is what sends
    the directory down the session-history path in the first place -- and
    ``conversations.info`` answers only for real channel ids, exactly like Slack:
    a composite ``<channel>:<thread>`` gets ``channel_not_found``.
    """

    def __init__(self, known=(DM,)):
        self.known = set(known)
        self.info_calls: list[str] = []
        self.user_calls: list[str] = []

    async def users_conversations(self, **kw):
        return {"ok": False, "error": "missing_scope"}

    async def conversations_info(self, channel):
        self.info_calls.append(channel)
        if channel not in self.known:
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True, "channel": {"id": channel, "is_im": True, "user": "U0PEER"}}

    async def users_info(self, user):
        self.user_calls.append(user)
        return {"ok": True, "user": {"profile": {"display_name": "adamparco"}}}


class FakeAdapter:
    def __init__(self, client):
        self._team_clients = {"T0TEAM": client}


import gateway.channel_directory as cd  # noqa: E402


def run(client, entries):
    """Drive the real _build_slack over ``entries`` and return what it built."""
    original = cd._build_from_sessions
    cd._build_from_sessions = lambda platform: [dict(e) for e in entries]
    try:
        return asyncio.run(cd._build_slack(FakeAdapter(client)))
    finally:
        cd._build_from_sessions = original


# --- 1. The helper module is where the patched source expects it ------------
print("gateway.channel_directory_threads:")
import gateway.channel_directory_threads as cdt  # noqa: E402

cdt._misses.clear()
check(
    "the resolution loop is wired to the grouping helper",
    getattr(cd, "_unresolved_by_channel", None) is cdt.unresolved_by_channel,
)
_src = open("gateway/channel_directory.py").read()
check(
    "conversations.info is addressed to the channel",
    "conversations_info(channel=_channel_id)" in _src,
)
check(
    "no caller still passes a directory entry id to conversations.info",
    'conversations_info(channel=entry["id"])' not in _src,
    "the composite <channel>:<thread> id is back",
)

# --- 2. The storm, replayed -------------------------------------------------
print("2026-08-09 storm replay:")
client = FakeClient()
built = run(client, STORM_ENTRIES)

check(
    "33 thread entries cost one Slack call",
    len(client.info_calls) == 1,
    f"made {len(client.info_calls)} calls: {client.info_calls[:3]}",
)
check(
    "the call names the channel, not a thread within it",
    client.info_calls == [DM],
    f"called {client.info_calls[:3]}",
)
check(
    "no call could return channel_not_found",
    all(":" not in c for c in client.info_calls),
)

resolved = [c for c in built if c.get("id", "").startswith(DM)]
check("every thread entry survives the merge", len(resolved) == 33)
check(
    "every one of them is renamed from the single answer",
    all(c["name"].startswith("adamparco / topic ") for c in resolved),
    f"unrenamed: {[c['name'] for c in resolved if not c['name'].startswith('adamparco')][:2]}",
)
check(
    "the thread each entry belongs to is still legible",
    len({c["name"] for c in resolved}) == 33,
    "the thread label was overwritten, so 33 entries collapsed to one name",
)
check("the peer was looked up once, not 33 times", len(client.user_calls) == 1)

# --- 3. The failure that used to repeat forever -----------------------------
# The original loop caught the error, logged at DEBUG and continued, so the same
# doomed call was reissued every five minutes for as long as the pod lived.
print("unresolvable channel:")
cdt._misses.clear()
gone = [{"id": "C0GONEGONE:1786240438.796879", "name": "C0GONEGONE / topic x"}]
client2 = FakeClient()
run(client2, gone)
check("an unknown channel is probed once", len(client2.info_calls) == 1)
run(client2, gone)
check(
    "and not probed again on the next refresh",
    len(client2.info_calls) == 1,
    f"reissued the failing call: {client2.info_calls}",
)
check("the backoff is remembered against the channel", "C0GONEGONE" in cdt._misses)

cdt._misses["C0GONEGONE"] -= cdt.MISS_TTL_SECONDS + 1
run(client2, gone)
check(
    "the backoff expires so a re-invited bot recovers",
    len(client2.info_calls) == 2,
)
cdt._misses.clear()

# --- 4. Nothing was broken for ordinary entries -----------------------------
print("plain entries:")
client3 = FakeClient(known=("D0PLAINDM",))
plain = [{"id": "D0PLAINDM", "name": "D0PLAINDM", "type": "dm"}]
built3 = run(client3, plain)
check("a thread-less entry still resolves", client3.info_calls == ["D0PLAINDM"])
check("and is renamed whole", built3[0]["name"] == "adamparco")

client4 = FakeClient()
named = [{"id": f"{DM}:1", "name": "adamparco / topic 1", "type": "dm"}]
run(client4, named)
check(
    "an already-named entry costs nothing",
    client4.info_calls == [],
    "the directory re-resolves names it already has on every refresh",
)
cdt._misses.clear()

# --- Result -----------------------------------------------------------------
if FAILURES:
    print(f"\nverify_channel_directory_threads: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nverify_channel_directory_threads: all checks passed")
