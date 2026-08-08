#!/usr/bin/env python3
"""Build gate for the kanban comment status patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier. The
applier only proves its one anchor matched. This drives the *real patched*
handler — the same ``_handle_comment`` a ``kanban_comment`` tool call reaches —
against a real board, and replays the 2026-08-08 shape that produced the
incident: a card completed, its report delivered, its subscription torn down,
and then a comment written to it.

Not vacuous, and measured rather than asserted: run against the same image with
the applier skipped, 17 of the 33 checks fail and the script exits 1, because
upstream's ``_handle_comment`` returns ``{"ok", "task_id", "comment_id"}`` and
nothing else. The checks that pass on an unpatched tree are the ones that are
supposed to — they assert the patch did NOT change what upstream already
returned, and a regression there would fail on the patched tree instead.

Usage::

    cd /opt/hermes && python3 verify_kanban_comment_status.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


TMP = Path(tempfile.mkdtemp())
DB = TMP / "kanban.db"
# Pin the board the tool layer resolves, the way the dispatcher pins it into
# every worker it spawns.
os.environ["HERMES_KANBAN_DB"] = str(DB)
# The handler derives the comment author from HERMES_PROFILE and refuses
# mutations from a delegate_task child; neither should vary this run.
os.environ["HERMES_PROFILE"] = "default"
os.environ.pop("HERMES_KANBAN_TASK", None)

from hermes_cli import kanban_db as K  # noqa: E402
import tools.kanban_tools as kt  # noqa: E402

try:
    import tools.kanban_comment_status as kcs  # noqa: E402
except ImportError:  # the module was never COPYd in
    kcs = None

conn = K.connect(DB)

UPSTREAM_KEYS = {"ok", "task_id", "comment_id"}


def comment(task_id: str, body: str = "a note on this card") -> dict:
    """Call the real tool handler exactly as the model does."""
    return json.loads(kt._handle_comment({"task_id": task_id, "body": body}))


def card(title: str, status: str = "running") -> str:
    """A card pinned to ``status``.

    ``create_task``'s own default is not relied on: it lands a card in ``ready``
    here, upstream's signature says ``running``, and the dispatcher moves cards
    between the two. This patch's behaviour is a function of the status, so the
    status is stated rather than inherited.
    """
    tid = K.create_task(conn, title=title, assignee="platform")
    with K.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    return tid


def subscribe(task_id: str) -> None:
    K.add_notify_sub(
        conn,
        task_id=task_id,
        platform="slack",
        chat_id="C0PLATFORM",
        thread_id="1723033132.001",
        user_id="U0ADAM",
        notifier_profile="default",
    )


def sub_rows(task_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
    ).fetchone()[0]


def bodies(task_id: str) -> list:
    return [c.body for c in K.list_comments(conn, task_id)]


check(
    "the handler resolved the delivery-fields import",
    hasattr(kt, "_comment_delivery_fields"),
    "the trailer import did not execute",
)

# --- 1. Upstream's contract is intact ----------------------------------------
# The model parses these three. Adding fields is safe; changing them is not.
print("upstream fields:")
open_card = card("an open card", "running")
out = comment(open_card, "still upstream's shape")
check("ok is still True", out.get("ok") is True)
check("task_id is still echoed back", out.get("task_id") == open_card)
check(
    "comment_id is still the integer rowid",
    isinstance(out.get("comment_id"), int) and out["comment_id"] > 0,
    f"got {out.get('comment_id')!r}",
)
check(
    "no upstream field was dropped",
    UPSTREAM_KEYS <= set(out),
    f"missing {sorted(UPSTREAM_KEYS - set(out))}",
)
check("the comment landed on the card", "still upstream's shape" in bodies(open_card))

# --- 2. A live card says so --------------------------------------------------
print("open cards:")
check(
    "a running card reports its status",
    out.get("task_status") == "running",
    f"got {out.get('task_status')!r}",
)
check(
    "and names the live worker as the reader",
    out.get("comment_reaches") == "the worker running this card right now",
    f"got {out.get('comment_reaches')!r}",
)
check(
    "an unsubscribed card is honest that completing posts nowhere",
    out.get("completion_posts_to_chat") is False,
    f"got {out.get('completion_posts_to_chat')!r}",
)

subscribe(open_card)
out = comment(open_card, "now with a thread behind it")
check(
    "a subscribed card reports that its completion reaches chat",
    out.get("completion_posts_to_chat") is True,
    f"got {out.get('completion_posts_to_chat')!r}",
)
check("no note is attached to a card that can still deliver", "note" not in out)

queued = card("not started yet", "todo")
out = comment(queued)
check(
    "a queued card points at the worker that will pick it up",
    out.get("task_status") == "todo"
    and out.get("comment_reaches") == "the next worker to run this card",
    f"got {out.get('task_status')!r} / {out.get('comment_reaches')!r}",
)

# --- 3. The 2026-08-08 incident, replayed ------------------------------------
# t_a8f58a2a completed at 19:09:43, its 6,191-char report reached the Slack
# thread at 19:09:44, the notifier unsubscribed on the same tick, and at 19:11:30
# the front door commented on the card and told the user the results would post
# there when the agent finished.
print("2026-08-08 incident replay:")
incident = card("audit the fleet", "running")
subscribe(incident)
report = "FINDINGS\n" + ("cluster row " * 500)
K.complete_task(
    conn, incident, result=report, summary="Audited the fleet; 9 findings.",
)
# What gateway/kanban_watchers.py does on the tick that carries the terminal
# event: task_terminal -> _kanban_unsub.
K.remove_notify_sub(
    conn,
    task_id=incident,
    platform="slack",
    chat_id="C0PLATFORM",
    thread_id="1723033132.001",
)
check("the card is done", K.get_task(conn, incident).status == "done")
check("its subscription is gone", sub_rows(incident) == 0)

out = comment(incident, "Any update on this? Posting results here when done.")
check("the comment still succeeds", out.get("ok") is True)
check(
    "the comment is still stored",
    "Any update on this? Posting results here when done." in bodies(incident),
    "a terminal card must not swallow the annotation",
)
check(
    "the result names the terminal status",
    out.get("task_status") == "done",
    f"got {out.get('task_status')!r}",
)
check(
    "and says nobody will read the comment",
    out.get("comment_reaches") == "nobody",
    f"got {out.get('comment_reaches')!r}",
)
note = out.get("note") or ""
check("a terminal card carries an explicit note", bool(note))
check(
    "the note forbids promising a delivery",
    "Do NOT say that results will follow" in note,
    f"got {note!r}",
)
check(
    "the note points at where the answer already is",
    "kanban_show" in note and incident in note,
)
check(
    "nothing in the payload can be read as a pending delivery",
    "completion_posts_to_chat" not in out,
    "a terminal card must not advertise a delivery channel at all",
)

# --- 4. Status outranks the subscription row ---------------------------------
# The unsub happens when the notifier PROCESSES the terminal event, so for up to
# one tick a done card still has its rows. If the racy field could contradict
# the durable one, the model would resolve it optimistically -- which is the bug.
print("done-but-not-yet-unsubscribed:")
racy = card("just completed", "running")
subscribe(racy)
K.complete_task(conn, racy, result="the answer", summary="done")
check("the subscription row is still there", sub_rows(racy) == 1)
out = comment(racy)
check(
    "the card is still reported as undeliverable",
    out.get("comment_reaches") == "nobody" and out.get("task_status") == "done",
    f"got {out.get('task_status')!r} / {out.get('comment_reaches')!r}",
)
check(
    "the surviving subscription is not advertised",
    "completion_posts_to_chat" not in out,
    "status must outrank the row that is one tick from deletion",
)

print("archived cards:")
gone = card("old card", "running")
K.archive_task(conn, gone)
out = comment(gone)
check(
    "an archived card is terminal too",
    out.get("task_status") == "archived" and out.get("comment_reaches") == "nobody",
    f"got {out.get('task_status')!r} / {out.get('comment_reaches')!r}",
)

# --- 5. The set this patch is coupled to -------------------------------------
# TERMINAL_STATUSES exists to mirror the notifier's own terminal test. If
# upstream widens that test, a card can lose its subscription while this patch
# still calls it deliverable, and the incident comes back on the new status.
print("coupling to the notifier:")
watchers = Path("gateway/kanban_watchers.py").read_text()
check(
    "the notifier still unsubscribes on exactly {done, archived}",
    'task.status in {"done", "archived"}' in watchers,
    "gateway/kanban_watchers.py changed its terminal test — re-derive "
    "TERMINAL_STATUSES in tools/kanban_comment_status.py",
)
check(
    "and this patch's terminal set matches it",
    kcs is not None and set(kcs.TERMINAL_STATUSES) == {"done", "archived"},
    "tools/kanban_comment_status.py is missing" if kcs is None
    else f"got {sorted(kcs.TERMINAL_STATUSES)}",
)

# --- 6. Fails toward upstream -------------------------------------------------
# A comment that reaches the board must never be lost to the bookkeeping that
# describes it. add_comment has already committed its own write_txn by the time
# delivery_fields runs, so the only question is what the handler returns.
print("fail-soft:")
live = card("lookup will break", "running")


def _boom(*a, **kw):
    raise RuntimeError("simulated board failure")


_real_get_task = K.get_task
K.get_task = _boom
try:
    out = comment(live, "written while the status lookup is broken")
finally:
    K.get_task = _real_get_task

check(
    "a broken status lookup still returns success",
    out.get("ok") is True and out.get("comment_id"),
    f"got {out!r}",
)
check(
    "and degrades to exactly upstream's fields",
    set(out) == UPSTREAM_KEYS,
    f"got {sorted(out)}",
)
check(
    "and the comment is on the card",
    "written while the status lookup is broken" in bodies(live),
    "a status field is not worth a lost comment",
)

if kcs is None:
    check("a broken subscription probe keeps the status", False,
          "tools/kanban_comment_status.py is missing")
else:
    _real_probe = kcs._has_chat_subscriber
    kcs._has_chat_subscriber = _boom
    try:
        out = comment(live, "subscription probe broken")
    finally:
        kcs._has_chat_subscriber = _real_probe

    check(
        "a broken subscription probe keeps the status",
        out.get("task_status") == "running",
        f"got {out.get('task_status')!r}",
    )
    check(
        "and drops only the half that failed",
        "completion_posts_to_chat" not in out,
        f"got {sorted(out)}",
    )

# --- 7. The wiring itself -----------------------------------------------------
print("wiring:")
_src = Path("tools/kanban_tools.py").read_text()
_calls = _src.count("**_comment_delivery_fields(kb, conn, tid)")
check(
    "the handler splats the delivery fields into upstream's _ok",
    _calls >= 1,
    "the applier did not run",
)
check(
    "exactly one call site",
    _calls == 1,
    f"found {_calls} — the applier ran twice" if _calls > 1 else "found 0",
)

print()
if FAILURES:
    print(f"verify_kanban_comment_status: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_comment_status: all checks passed")
