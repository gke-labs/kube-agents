"""Archive every kanban card left live since the fan-out began. Runs in the agent container.

Usage: python3 - <home> <since-epoch> <passes> <budget-seconds> < ci_fence_cards.py

hack/ci-eval-pr.sh pipes this into the gateway before each unit of the fan-out's second
phase (fence_live_cards), where the units run one at a time and the GitHub-write safeguard
marks each window exclusive: a write nothing of the repetition names is charged to it all
the same. A card an earlier unit left running keeps its worker writing after that unit is
graded, and its pull requests land in the next window (#4425). That happens when the inject
exchange is cut and the cancel cannot be sent over the same failed transport, when a card is
still running at the delegation ceiling, and when a worker's own fan-out children outlive
the wait. The phase is serial, so a card still live at this point is an earlier unit's, or
one the install filed for itself during the fan-out (an `oobe` stage run still going, say).
Either would write into the exclusive window, so both are archived.

A card counts as live when its status is not one the harness reads as settled
(bench/kube_agents_bench/harness.py, _TERMINAL_STATUSES): a worker runs it now or the
dispatcher may run it later. Only cards created at or after <since-epoch>, the fan-out's
start, so a card the install filed for itself before the eval began is left alone.
``hermes kanban archive`` moves each one to ``archived`` and stops its worker, as the
harness does to the cards it gave up on at the ceiling. A worker can file a child between
the read and its own archive, so the board is read again, up to <passes> times.

The helper keeps inside <budget-seconds>, which the runner sets a little under its own bound
on the exec. The runner's `timeout` stops kubectl, not this process in the pod, so a helper
that kept going after it could archive the card of the unit launched next. An archive starts
only while ARCHIVE_FLOOR_SECONDS are left, and each archive and board read is bounded by what
is left. A budget that runs out ends the helper, naming the cards it did not reach.

Prints one line. Exits 0 when nothing live is left (none found, or every one archived), and
EXIT_LEFT_LIVE (3) when the board could not be read, the budget ran out, or a card is still
live after the last pass. That code is the helper's own verdict, told apart from kubectl's exit
1 for an exec that never reached the container, or for a helper that did not run to its verdict:
the runner retries those and not this (EVAL_FENCE_DECIDED_EXIT in hack/ci-eval-pr.sh).
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import time

home, since, passes, budget = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
BOARD = os.path.join(home, "kanban.db")
# harness.py's _TERMINAL_STATUSES: a card in one of these has no worker that writes.
SETTLED = ("done", "archived", "blocked", "failed", "cancelled")
# The hermes CLI in the agent pod, for when it is not on the exec shell's PATH (harness.py's
# _HERMES_BIN_FALLBACK).
HERMES_FALLBACK = "/opt/hermes/.venv/bin/hermes"
# What `hermes kanban archive <id>` prints on success (harness.py's _ARCHIVED_PREFIX).
ARCHIVED_PREFIX = "Archived "
ARCHIVE_TIMEOUT_SECONDS = 30
# The least time left in the budget for which an archive is started: a hermes CLI start.
ARCHIVE_FLOOR_SECONDS = 5
SQLITE_BUSY_TIMEOUT_SECONDS = 10
# How much of a refused archive's reply the line quotes.
EXCERPT_CHARS = 200
EXIT_LEFT_LIVE = 3
STARTED = time.monotonic()


class BudgetSpent(Exception):
    """The budget ran out before the next step could start."""


def left():
    """Seconds of the budget still to spend."""
    return budget - (time.monotonic() - STARTED)


def live_cards():
    """``[(id, status)]`` of the live cards created since the fan-out began, oldest first."""
    room = left()
    if room <= 0:
        raise BudgetSpent()
    conn = sqlite3.connect(f"file:{BOARD}?mode=ro", uri=True, timeout=min(SQLITE_BUSY_TIMEOUT_SECONDS, room))
    try:
        marks = ",".join("?" * len(SETTLED))
        return conn.execute(
            f"SELECT id, status FROM tasks WHERE status NOT IN ({marks}) AND COALESCE(created_at, 0) >= ? "
            "ORDER BY created_at, id",
            (*SETTLED, since),
        ).fetchall()
    finally:
        conn.close()


def archive(hermes, card):
    """Archive one card; ``None`` on success, else hermes's reason."""
    try:
        done = subprocess.run(
            [hermes, "kanban", "archive", card],
            capture_output=True, text=True, timeout=min(ARCHIVE_TIMEOUT_SECONDS, max(left(), 0)), check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)[:EXCERPT_CHARS]
    out = (done.stdout + done.stderr).strip()
    return None if out.startswith(ARCHIVED_PREFIX) else (out[:EXCERPT_CHARS] or f"exit {done.returncode}")


def main():
    if not os.path.exists(BOARD):
        print("no board, nothing live")
        return 0
    hermes = shutil.which("hermes") or HERMES_FALLBACK
    archived, refused = [], {}
    try:
        return fence(hermes, archived, refused)
    except BudgetSpent as spent:
        unreached = spent.args[0] if spent.args else ["the board could not be read again inside the budget"]
        print(
            f"budget of {budget}s spent; archived {len(archived)}: {', '.join(archived) or 'none'}; "
            f"not reached: {', '.join(unreached)}"
        )
        return EXIT_LEFT_LIVE


def fence(hermes, archived, refused):
    """Archive the live cards in passes and print the line, unless the budget runs out."""
    for _ in range(passes):
        try:
            cards = [(card, status) for card, status in live_cards() if card not in refused]
        except sqlite3.Error as exc:
            print(f"board unreadable: {exc}; archived {len(archived)}: {', '.join(archived) or 'none'}")
            return EXIT_LEFT_LIVE
        if not cards:
            break
        for i, (card, status) in enumerate(cards):
            if left() < ARCHIVE_FLOOR_SECONDS:
                raise BudgetSpent([f"{c} ({st})" for c, st in cards[i:]])
            reason = archive(hermes, card)
            if reason is None:
                archived.append(f"{card} ({status})")
            else:
                refused[card] = f"{card} ({status}): {reason}"
    else:
        try:
            remaining = [f"{card} ({status})" for card, status in live_cards() if card not in refused]
        except sqlite3.Error as exc:
            remaining = [f"unread: {exc}"]
        if remaining:
            print(f"archived {len(archived)}: {', '.join(archived)}; still live after {passes} passes: {', '.join(remaining)}")
            return EXIT_LEFT_LIVE
    if refused:
        # A card that settled between the read and its archive (its worker finished, the
        # dispatcher archived it) is refused, but nothing of it is left to fence.
        try:
            still = {card for card, _ in live_cards()}
        except (sqlite3.Error, BudgetSpent):
            still = set(refused)
        refused = {card: why for card, why in refused.items() if card in still}
    if refused:
        print(f"archived {len(archived)}: {', '.join(archived) or 'none'}; would not archive: {'; '.join(refused.values())}")
        return EXIT_LEFT_LIVE
    if not archived:
        print("nothing live")
        return 0
    print(f"archived {len(archived)} card(s) left live since the fan-out began: {', '.join(archived)}")
    return 0


sys.exit(main())
