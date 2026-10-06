"""Unit tests for the kanban progress lines installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "agents" / "platform" / "scripts"))

import apply_kanban_progress_lines as applier
import kanban_notifier
import kanban_progress_lines
import slack_moments
import slack_presenter
import slack_ux_moments
import kanban_notify_delivery
from kanban_handoff_clip import ELLIPSIS
from kanban_progress_lines import (
    BULLET,
    DEFAULT_NOTE_LIMIT,
    ELIDED,
    FINISHED,
    IN_PROGRESS,
    MAX_LINES,
    MOVED_TO_PLAN,
    MAX_RENDER,
    MAX_TRACKED,
    STOPPED,
    deliver,
    progress_note,
    render,
    result_line,
    rolling_line,
    silent_event,
    slack_line,
    sub_key,
    tracked_messages,
)

RUN_URL = "https://github.com/gke-agentic/adamparco-infra/actions/runs/9912345678"


class ProgressNoteTest(unittest.TestCase):
    def test_a_deliberate_note_is_delivered(self):
        self.assertEqual(
            progress_note({"note": "Scanned 3 of 7 clusters; no drift so far."}),
            "Scanned 3 of 7 clusters; no drift so far.",
        )

    def test_an_auto_heartbeat_is_silent(self):
        # The ~2,100 heartbeat rows on the live board are all payload=None.
        # This empty return is the whole reason widening TERMINAL_KINDS does
        # not turn every tool call into a chat message.
        self.assertEqual(progress_note(None), "")

    def test_a_payload_without_a_note_is_silent(self):
        self.assertEqual(progress_note({}), "")
        self.assertEqual(progress_note({"stage": "scanning"}), "")
        self.assertEqual(progress_note({"note": None}), "")

    def test_a_blank_note_is_silent(self):
        self.assertEqual(progress_note({"note": ""}), "")
        self.assertEqual(progress_note({"note": "   \n "}), "")

    def test_a_non_mapping_payload_is_silent(self):
        for payload in ("a bare string", 42, ["note", "x"], object()):
            with self.subTest(payload=payload):
                self.assertEqual(progress_note(payload), "")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(progress_note({"note": "  working  \n"}), "working")

    def test_an_overlong_note_is_clipped_on_a_token_boundary(self):
        note = " ".join(f"step{i}" for i in range(200))
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        body = clipped[: -len(ELLIPSIS)]
        for token in body.split():
            self.assertIn(token, note.split(), f"token {token!r} was cut")

    def test_a_url_is_dropped_rather_than_severed(self):
        note = ("filler " * 60) + RUN_URL
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://", clipped)

    def test_a_note_that_ends_in_a_url_within_budget_keeps_it_whole(self):
        note = "Kicked off the rollout: " + RUN_URL
        self.assertLess(len(note), DEFAULT_NOTE_LIMIT)
        self.assertIn(RUN_URL, progress_note({"note": note}))

    def test_the_limit_is_honoured_at_every_width(self):
        note = "Reconciling the fleet inventory across every managed cluster. " * 10
        for limit in range(1, 320):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(progress_note({"note": note}, limit)), limit)

    def test_the_default_limit_is_a_ping_not_a_report(self):
        # Deliberately far below the completion handoff's 1200: a worker with
        # more than this to say should be completing the card, not pinging it.
        self.assertLessEqual(DEFAULT_NOTE_LIMIT, 500)


# --- the applier's own safety net -------------------------------------------
#
# The send anchor carries upstream's own ``await``, so a synchronous
# ``_send_event`` cannot match it; what an anchor cannot check is that the
# text it matched, or the text it inserted, is legal where it sits. An
# ``await`` outside a coroutine is exactly what ast.parse() accepts and
# compile() rejects, so a fixture with a plain ``def`` around the anchor is
# the one shape that tells the two apart: it pins that patchlib compiles what
# it wrote rather than parsing it, the same net that catches an inserted
# branch spliced one indent level out of its ``for``.

# The tuple the applier widens. Spelled out here rather than imported from the
# applier, because the whole point of locating it by name is that the applier no
# longer holds a copy of upstream's membership: a fixture that imported one
# could not tell the locator from the literal anchor it replaced. Module-level
# since v2026.9.14 (gateway/kanban_watchers_notifier.py).
_KINDS_ASSIGN = (
    'TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", '
    '"timed_out", "status", "archived", "unblocked", "block_loop_detected", '
    '"review_requested", "changes_requested")\n'
)
# The formatter the heartbeat def is inserted after, and the table it is
# registered in — upstream's shape, reduced to what the locator and the anchor
# read.
_FORMATTERS_HOST = (
    "\n\n"
    "def _fmt_completed(ev, n) -> tuple:\n"
    '    return "done", None, None\n'
    "\n\n"
    "def _fmt_changes_requested(ev, n) -> tuple:\n"
    '    return "changes", None, None\n'
    "\n\n"
    "# archived / unblocked are claimed but intentionally silent.\n"
)
_FORMATTERS_BODY = (
    '    "completed": _fmt_completed,\n'
    "}\n"
)
# The class the header and send anchors sit in. ``tag`` is the local upstream
# computes on the line above the header anchor.
_CLASS_HEAD = (
    "\n\nclass _KanbanNotification:\n"
    "    def __init__(self, runner, d):\n"
    "        self.runner = runner\n"
    '        tag = "@w "\n'
    '        self.board_tag = "[b] "\n'
    '        self.task_id = "t_1"\n'
)


def _send_method(coroutine: bool) -> str:
    keyword = "async def" if coroutine else "def"
    return (
        f"\n    {keyword} _send_event(self, ev, msg):\n"
        "        sub, adapter, metadata = self.sub, self.adapter, {}\n"
    )


# The loop that skips an event with no message, where the silent-kind hook goes.
_PINGS_HEAD = (
    "\n    async def _send_pings(self):\n"
    "        for ev in self.events:\n"
)


def _notifier_source(coroutine: bool, kinds: str = _KINDS_ASSIGN) -> str:
    """A stand-in kanban_watchers_notifier.py carrying every site at its real shape.

    ``coroutine=False`` makes ``_send_event`` a plain ``def`` — the shape the
    applier has to reject, because its replacement awaits. ``kinds`` overrides
    the TERMINAL_KINDS assignment, for the drift cases the locator has to
    survive.
    """
    return (
        kinds
        + _FORMATTERS_HOST
        + applier.FORMATTERS_ANCHOR
        + _FORMATTERS_BODY
        + _CLASS_HEAD
        + applier.HEADER_ANCHOR
        + _send_method(coroutine)
        + applier.SEND_ANCHOR
        + _PINGS_HEAD
        + applier.SILENT_ANCHOR
    )


# The schema module builds every tool's schema through _schema()/_prop(); the
# two anchors are the argument text of the heartbeat one.
_SCHEMAS_SOURCE = (
    "KANBAN_HEARTBEAT_SCHEMA = _schema(\n"
    '    "kanban_heartbeat",\n'
    + applier.SCHEMA_ANCHOR
    + "    {\n"
    + applier.NOTE_ANCHOR
    + "    },\n"
    + "    [],\n"
    + ")\n"
)


class ApplierTest(unittest.TestCase):
    def _tree(self, coroutine: bool = True, kinds: str = _KINDS_ASSIGN) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "gateway").mkdir()
        (root / "tools").mkdir()
        (root / applier.NOTIFIER_RELATIVE).write_text(
            _notifier_source(coroutine, kinds)
        )
        (root / applier.SCHEMAS_RELATIVE).write_text(_SCHEMAS_SOURCE)
        return root

    def _notifier(self, root: Path) -> str:
        return (root / applier.NOTIFIER_RELATIVE).read_text()

    def test_it_patches_a_tree_that_matches_the_shipped_image(self):
        root = self._tree()
        applier.apply(root)
        patched = self._notifier(root)
        self.assertIn('"heartbeat": _fmt_heartbeat,', patched)
        self.assertIn("def _fmt_heartbeat(ev, n) -> tuple:", patched)
        self.assertIn('+ ("heartbeat",)', patched)
        self.assertIn("self.progress_header = ", patched)
        self.assertIn("from gateway.kanban_progress_lines import progress_note", patched)
        self.assertIn("from gateway.kanban_progress_lines import deliver", patched)
        # Every line the notifier posts now goes through deliver(), so a
        # surviving bare send() would be a line that skipped the rolling
        # message entirely — and the map has to hang off the runner, because
        # the notification object is rebuilt for every delivery.
        self.assertIn("_send_res = await _progress_deliver(", patched)
        self.assertIn("self.runner, adapter, sub, ev.kind, ev, msg, metadata,", patched)
        self.assertNotIn("_send_res = await adapter.send(", patched)
        # A skipped event reaches the silent-kind hook, and is still skipped.
        self.assertIn(
            "                await _progress_silent_event(self, ev)\n                continue\n", patched,
        )
        self.assertIn("from gateway.kanban_progress_lines import silent_event as _progress_silent_event", patched)
        self.assertIn(
            "A one-line progress update",
            (root / applier.SCHEMAS_RELATIVE).read_text(),
        )

    def test_the_formatter_lands_between_the_last_upstream_one_and_the_table(self):
        # The table references the def at import time, so the def has to come
        # before it; sitting after upstream's last formatter keeps the file
        # reading in upstream's order.
        root = self._tree()
        applier.apply(root)
        patched = self._notifier(root)
        self.assertLess(
            patched.index("def _fmt_changes_requested"),
            patched.index("def _fmt_heartbeat"),
        )
        self.assertLess(patched.index("def _fmt_heartbeat"), patched.index("_EVENT_FORMATTERS"))
        ast.parse(patched)

    def test_a_noteless_heartbeat_formats_to_none(self):
        # The silent path: format_event() returns the formatter's message and
        # _send_pings() skips a None, which is what keeps ~2,100 auto-heartbeats
        # off the thread. Exercised on the applier's own emitted source.
        namespace = {"_progress_note": progress_note}
        exec(applier.FORMATTER_DEF, namespace)  # noqa: S102 - the patch's own text
        fmt = namespace["_fmt_heartbeat"]
        n = SimpleNamespace(progress_header="[default] @platform ")
        self.assertEqual(fmt(SimpleNamespace(payload=None), n), (None, None, None))
        self.assertEqual(fmt(SimpleNamespace(payload={}), n), (None, None, None))
        self.assertEqual(
            fmt(SimpleNamespace(payload={"note": "scanned 3 of 7"}), n),
            ("⏳ [default] @platform scanned 3 of 7", None, None),
        )

    def test_an_await_outside_a_coroutine_fails_the_build(self):
        root = self._tree(coroutine=False)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("no longer parses", str(caught.exception))
        # ast.parse would have waved this through, which is why it is gone.
        ast.parse(_notifier_source(coroutine=False))

    def test_a_drifted_anchor_fails_the_build(self):
        root = self._tree()
        path = root / applier.NOTIFIER_RELATIVE
        path.write_text(path.read_text().replace(applier.SEND_ANCHOR, ""))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("found 0", str(caught.exception))
        self.assertIn("notifier send", str(caught.exception))

    def test_a_drifted_skip_fails_the_build(self):
        root = self._tree()
        path = root / applier.NOTIFIER_RELATIVE
        path.write_text(path.read_text().replace("if msg is None:", "if not msg:"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("silent-kind skip", str(caught.exception))

    def test_a_missing_last_formatter_fails_the_build(self):
        root = self._tree()
        path = root / applier.NOTIFIER_RELATIVE
        path.write_text(path.read_text().replace("_fmt_changes_requested", "_fmt_review_changes"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("expected 1 module-level def _fmt_changes_requested()", str(caught.exception))


# --- what the TERMINAL_KINDS locator buys over the literal it replaced --------
#
# The literal anchor on that line failed the build on every upstream edit to the
# tuple, and v2026.8.13 and v2026.9.14 each made exactly such an edit
# ("review_requested", then "changes_requested"). These pin the trade the
# locator makes: membership upstream owns may change, the tuple still being the
# terminal-kind filter may not.


class TerminalKindsLocatorTest(ApplierTest):
    def test_an_upstream_added_kind_still_patches(self):
        widened = _KINDS_ASSIGN.replace(
            '"changes_requested")', '"changes_requested", "abandoned")'
        )
        root = self._tree(kinds=widened)
        applier.apply(root)
        patched = self._notifier(root)
        # Upstream's addition survives, and heartbeat is appended to it.
        self.assertIn('"abandoned"', patched)
        self.assertIn('+ ("heartbeat",)', patched)

    def test_a_reformatted_tuple_still_patches(self):
        multiline = (
            "TERMINAL_KINDS = (\n"
            '    "completed",\n'
            '    "blocked",\n'
            '    "gave_up",\n'
            '    "crashed",\n'
            '    "timed_out",\n'
            ")\n"
        )
        root = self._tree(kinds=multiline)
        applier.apply(root)
        self.assertIn('+ ("heartbeat",)', self._notifier(root))

    def test_a_filter_that_lost_a_kind_fails_the_build(self):
        # Membership this patch reasons about is asserted, not searched on, so
        # a repurposed tuple fails with what it expected rather than vanishing.
        gutted = _KINDS_ASSIGN.replace('"crashed", ', "")
        root = self._tree(kinds=gutted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("no longer holds 'crashed'", str(caught.exception))

    def test_a_renamed_filter_fails_the_build(self):
        renamed = _KINDS_ASSIGN.replace("TERMINAL_KINDS", "CLAIMED_KINDS")
        root = self._tree(kinds=renamed)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("expected 1 assignment to TERMINAL_KINDS", str(caught.exception))

    def test_a_second_run_is_refused(self):
        # The kinds edit does not consume an anchor, so the count check cannot
        # catch a re-run; without refuse_if_patched it would append a second
        # "heartbeat".
        root = self._tree()
        applier.apply(root)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(root)
        self.assertIn("already patched", str(caught.exception))


# --- what goes into the rolling message --------------------------------------


class RollingLineTest(unittest.TestCase):
    def test_a_heartbeat_contributes_its_note(self):
        self.assertEqual(
            rolling_line("heartbeat", {"note": "Scanned 3 of 7 clusters."}),
            "Scanned 3 of 7 clusters.",
        )

    def test_a_noteless_heartbeat_contributes_nothing(self):
        self.assertEqual(rolling_line("heartbeat", None), "")

    def test_a_status_event_contributes_the_transition(self):
        self.assertEqual(rolling_line("status", {"status": "running"}), "→ running")

    def test_a_status_event_without_a_status_contributes_nothing(self):
        self.assertEqual(rolling_line("status", {}), "")
        self.assertEqual(rolling_line("status", {"status": "  "}), "")

    def test_a_terminal_kind_contributes_nothing(self):
        for kind in ("completed", "blocked", "crashed", "timed_out", "gave_up"):
            with self.subTest(kind=kind):
                self.assertEqual(rolling_line(kind, {"note": "x"}), "")


class ResultLineTest(unittest.TestCase):
    def test_a_completed_event_gives_its_summary_line(self):
        self.assertEqual(result_line("completed", {"summary": " seeded-a is current. "}), "seeded-a is current.")

    def test_a_heading_loses_its_marks(self):
        self.assertEqual(result_line("completed", {"summary": "## Both pods are up"}), "Both pods are up")
        self.assertEqual(result_line("completed", {"summary": "#1234 merged"}), "#1234 merged")

    def test_inline_marks_are_reduced_to_their_text(self):
        summary = "## Fixed — see [PR #12](https://github.com/o/r/pull/12)"
        self.assertEqual(result_line("completed", {"summary": summary}), "Fixed — see PR #12")
        self.assertEqual(result_line("completed", {"summary": "**Both pods are up**"}), "Both pods are up")
        self.assertEqual(
            result_line("completed", {"summary": "`kube-system/coredns` is *healthy*"}),
            "kube-system/coredns is healthy",
        )
        summary = "![graph](https://x/y.png) `.env` **Done**."
        self.assertEqual(result_line("completed", {"summary": summary}), "graph .env Done.")

    def test_identifiers_and_arithmetic_keep_their_characters(self):
        for summary in (
            "seeded_a and __init__ are current",
            "2*3*4 replicas",
            "a * b",
            "Cleaned gs://bkt/logs/*/2026/*.json",
            "Applied *.yaml* overlays",
            "Files *_test.go and *.go* skipped",
        ):
            self.assertEqual(result_line("completed", {"summary": summary}), summary)

    def test_a_long_summary_is_clipped_as_a_note_is(self):
        self.assertLessEqual(len(result_line("completed", {"summary": "word " * 100})), DEFAULT_NOTE_LIMIT)

    def test_nothing_without_a_summary_or_for_another_kind(self):
        self.assertEqual(result_line("completed", {"summary": None}), "")
        self.assertEqual(result_line("completed", None), "")
        self.assertEqual(result_line("gave_up", {"summary": "x"}), "")


class RenderTest(unittest.TestCase):
    HEADER = "[default] @platform "

    def test_the_first_note_renders_exactly_as_it_did_before_rolling(self):
        # The pre-rolling notifier built `f"⏳ {board_tag}{tag}{note}"`. A card
        # that heartbeats once must be byte-for-byte unchanged.
        note = "Reading the scheduler directly: 8 configured jobs found."
        self.assertEqual(
            render(self.HEADER, [note]),
            f"{IN_PROGRESS} [default] @platform {note}",
        )

    def test_a_second_note_moves_the_header_onto_its_own_line(self):
        self.assertEqual(
            render(self.HEADER, ["first", "second"]),
            f"{IN_PROGRESS} [default] @platform\n{BULLET}first\n{BULLET}second",
        )

    def test_the_trail_stays_in_the_order_it_arrived(self):
        text = render(self.HEADER, ["one", "two", "three"])
        self.assertLess(text.index("one"), text.index("two"))
        self.assertLess(text.index("two"), text.index("three"))

    def test_an_empty_header_still_renders(self):
        self.assertEqual(render("", ["only"]), f"{IN_PROGRESS} only")

    def test_a_settled_message_carries_no_hourglass(self):
        done = render(self.HEADER, ["a", "b"], FINISHED)
        self.assertTrue(done.startswith(FINISHED))
        self.assertNotIn(IN_PROGRESS, done)

    def test_a_failed_card_settles_to_something_other_than_a_tick(self):
        # The rolling log must never imply success for a card that crashed;
        # the outcome itself is on the terminal message below it.
        self.assertNotEqual(FINISHED, STOPPED)
        stopped = render(self.HEADER, ["a"], STOPPED)
        self.assertTrue(stopped.startswith(STOPPED))
        self.assertNotIn(FINISHED, stopped)

    def test_a_long_trail_drops_its_oldest_entries_and_says_so(self):
        lines = [f"step {i}" for i in range(MAX_LINES + 5)]
        text = render(self.HEADER, lines)
        self.assertIn(ELIDED, text)
        self.assertNotIn("step 0", text)
        self.assertIn(f"step {MAX_LINES + 4}", text)

    def test_the_render_stays_under_the_chunking_threshold(self):
        # Over the Google Chat adapter's 4000-character ceiling, send() splits
        # into a second message and edit_message() truncates — both of which
        # break the one-message-per-card promise. MAX_RENDER sits below it.
        self.assertLess(MAX_RENDER, 4000)
        fat = [("word " * 60).strip() for _ in range(MAX_LINES)]
        text = render(self.HEADER, fat)
        self.assertLessEqual(len(text), MAX_RENDER)
        self.assertIn(ELIDED, text)

    def test_blank_entries_are_skipped(self):
        self.assertEqual(render(self.HEADER, ["", "real", ""]),
                         f"{IN_PROGRESS} [default] @platform real")

    def test_an_empty_trail_renders_the_header_alone(self):
        self.assertEqual(render(self.HEADER, []), f"{IN_PROGRESS} [default] @platform")


class SubKeyTest(unittest.TestCase):
    SUB = {
        "task_id": "t_a18254ca",
        "platform": "google_chat",
        "chat_id": "spaces/AAQA",
        "thread_id": "spaces/AAQA/threads/x",
    }

    def test_it_agrees_with_the_delivery_patch(self):
        # The two maps are keyed on the same subscription and must not disagree
        # about what "the same subscription" is. This module carries its own
        # copy only because kanban_notify_delivery.py is copied into the image
        # hundreds of Dockerfile lines later.
        self.assertEqual(sub_key(self.SUB), kanban_notify_delivery.sub_key(self.SUB))

    def test_a_missing_thread_id_agrees_too(self):
        sub = {k: v for k, v in self.SUB.items() if k != "thread_id"}
        self.assertEqual(sub_key(sub), kanban_notify_delivery.sub_key(sub))


# --- the delivery behaviour ---------------------------------------------------


class _Result:
    """Stands in for gateway.platforms.base.SendResult."""

    def __init__(self, success, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class _Adapter:
    """A push adapter that records what it was asked to post and to edit."""

    def __init__(self, *, can_edit=True, edit_raises=False):
        self.can_edit = can_edit
        self.edit_raises = edit_raises
        self.sent = []
        self.edits = []
        self._posted = 0

    async def send(self, chat_id, content, metadata=None):
        self._posted += 1
        message_id = f"spaces/S/messages/m{self._posted}"
        self.sent.append((chat_id, content, message_id))
        return _Result(True, message_id)

    async def edit_message(self, chat_id, message_id, content):
        if self.edit_raises:
            raise RuntimeError("patch exploded")
        if not self.can_edit:
            return _Result(False, error="Not supported")
        self.edits.append((message_id, content))
        return _Result(True, message_id)


SUB = {
    "task_id": "t_a18254ca",
    "platform": "google_chat",
    "chat_id": "spaces/AAQA",
    "thread_id": "spaces/AAQA/threads/x",
}
HEADER = "[default] @platform "


def _beat(event_id, note):
    return SimpleNamespace(id=event_id, kind="heartbeat", payload={"note": note})


def _terminal(event_id, kind="completed"):
    return SimpleNamespace(id=event_id, kind=kind, payload={})


class DeliverTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.watcher = SimpleNamespace()
        self.adapter = _Adapter()

    async def _deliver(self, ev, message=None, adapter=None):
        return await deliver(
            self.watcher,
            adapter or self.adapter,
            SUB,
            ev.kind,
            ev,
            message if message is not None else f"{IN_PROGRESS} {HEADER}{ev.payload.get('note', '')}",
            {"thread_id": SUB["thread_id"]},
            HEADER,
        )

    async def test_the_first_note_posts_one_message(self):
        await self._deliver(_beat(1, "Reading the scheduler directly."))
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.edits, [])
        self.assertEqual(
            self.adapter.sent[0][1],
            f"{IN_PROGRESS} [default] @platform Reading the scheduler directly.",
        )

    async def test_later_notes_edit_that_message_instead_of_posting(self):
        await self._deliver(_beat(1, "Reading the scheduler directly."))
        await self._deliver(_beat(2, "Found two separate cron stores."))
        await self._deliver(_beat(3, "Checking delivery failures."))
        self.assertEqual(len(self.adapter.sent), 1, "posted more than one message")
        self.assertEqual(len(self.adapter.edits), 2)
        message_id, text = self.adapter.edits[-1]
        self.assertEqual(message_id, self.adapter.sent[0][2])
        self.assertEqual(
            text,
            f"{IN_PROGRESS} [default] @platform\n"
            f"{BULLET}Reading the scheduler directly.\n"
            f"{BULLET}Found two separate cron stores.\n"
            f"{BULLET}Checking delivery failures.",
        )

    async def test_the_result_arrives_as_its_own_message(self):
        await self._deliver(_beat(1, "Working."))
        await self._deliver(_terminal(2), message="✔ [default] @platform done — title")
        self.assertEqual(len(self.adapter.sent), 2)
        self.assertEqual(self.adapter.sent[-1][1], "✔ [default] @platform done — title")

    async def test_a_completion_settles_the_rolling_message(self):
        await self._deliver(_beat(1, "Working."))
        await self._deliver(_terminal(2), message="✔ done")
        self.assertEqual(len(self.adapter.edits), 1)
        self.assertEqual(self.adapter.edits[-1][1], f"{FINISHED} [default] @platform Working.")

    async def test_a_failure_settles_it_without_claiming_success(self):
        for kind in ("blocked", "crashed", "timed_out", "gave_up"):
            with self.subTest(kind=kind):
                self.setUp()
                await self._deliver(_beat(1, "Working."))
                await self._deliver(_terminal(2, kind), message="✖ failed")
                self.assertEqual(
                    self.adapter.edits[-1][1], f"{STOPPED} [default] @platform Working.",
                )

    async def test_a_terminal_event_with_no_progress_behind_it_just_posts(self):
        await self._deliver(_terminal(1), message="✔ done")
        self.assertEqual(self.adapter.edits, [])
        self.assertEqual(len(self.adapter.sent), 1)

    async def test_the_next_card_starts_a_fresh_message(self):
        await self._deliver(_beat(1, "Working."))
        await self._deliver(_terminal(2), message="✔ done")
        await self._deliver(_beat(3, "A later run on the same subscription."))
        self.assertEqual(len(self.adapter.sent), 3)

    async def test_a_platform_that_cannot_edit_posts_one_message_per_note(self):
        adapter = _Adapter(can_edit=False)
        for i, note in enumerate(("one", "two", "three"), start=1):
            await self._deliver(_beat(i, note), adapter=adapter)
        self.assertEqual(len(adapter.sent), 3)
        self.assertEqual(adapter.edits, [])
        # Each one is the plain single-line form the notifier posted before.
        self.assertEqual(adapter.sent[-1][1], f"{IN_PROGRESS} [default] @platform three")

    async def test_a_deleted_message_is_replaced_rather_than_lost(self):
        await self._deliver(_beat(1, "one"))
        self.adapter.can_edit = False
        await self._deliver(_beat(2, "two"))
        self.assertEqual(len(self.adapter.sent), 2)
        self.adapter.can_edit = True
        await self._deliver(_beat(3, "three"))
        # Tracking resumed on the replacement, so the trail continues there.
        self.assertEqual(self.adapter.edits[-1][0], self.adapter.sent[1][2])
        self.assertIn(f"{BULLET}two", self.adapter.edits[-1][1])

    async def test_a_replayed_event_is_not_appended_twice(self):
        # kanban_notify_delivery.py made delivery at-least-once: a batch that
        # fails partway is re-read and replayed whole on the next tick.
        await self._deliver(_beat(1, "one"))
        await self._deliver(_beat(2, "two"))
        await self._deliver(_beat(1, "one"))
        await self._deliver(_beat(2, "two"))
        self.assertEqual(len(self.adapter.edits), 1)
        self.assertEqual(self.adapter.edits[-1][1].count(BULLET), 2)

    async def test_a_replay_still_reports_the_event_as_delivered(self):
        # The notifier reads getattr(res, "success", True); anything falsy for
        # `success` would rewind the cursor and replay it forever.
        await self._deliver(_beat(1, "one"))
        result = await self._deliver(_beat(1, "one"))
        self.assertIsNot(getattr(result, "success", True), False)

    async def test_a_settling_edit_that_explodes_never_reaches_the_notifier(self):
        # It is cosmetic. Raising here would land in the notifier's except,
        # rewind the cursor and count against the send-failure budget that
        # drops the subscription.
        await self._deliver(_beat(1, "Working."))
        self.adapter.edit_raises = True
        result = await self._deliver(_terminal(2), message="✔ done")
        self.assertTrue(result.success)
        self.assertEqual(len(self.adapter.sent), 2)

    async def test_a_send_that_reports_failure_is_not_tracked(self):
        class _Failing(_Adapter):
            async def send(self, chat_id, content, metadata=None):
                return _Result(False, error="rate limited")

        adapter = _Failing()
        await self._deliver(_beat(1, "one"), adapter=adapter)
        self.assertEqual(tracked_messages(self.watcher), {})

    async def test_the_map_drains_when_cards_terminate(self):
        await self._deliver(_beat(1, "one"))
        self.assertEqual(len(tracked_messages(self.watcher)), 1)
        await self._deliver(_terminal(2), message="✔ done")
        self.assertEqual(tracked_messages(self.watcher), {})

    async def test_the_map_is_bounded(self):
        tracked = tracked_messages(self.watcher)
        for i in range(MAX_TRACKED + 10):
            tracked[("t%d" % i, "google_chat", "spaces/A", "")] = {
                "message_id": "m", "lines": ["x"], "last_event_id": 0,
            }
            while len(tracked) > MAX_TRACKED:
                tracked.pop(next(iter(tracked)), None)
        await self._deliver(_beat(1, "one"))
        self.assertLessEqual(len(tracked_messages(self.watcher)), MAX_TRACKED)

    async def test_the_thread_metadata_is_passed_through_on_a_new_message(self):
        captured = {}

        class _Capturing(_Adapter):
            async def send(self, chat_id, content, metadata=None):
                captured["metadata"] = metadata
                return await super().send(chat_id, content, metadata)

        await self._deliver(_beat(1, "one"), adapter=_Capturing())
        self.assertEqual(captured["metadata"], {"thread_id": SUB["thread_id"]})


class SettleReactionHookTest(unittest.IsolatedAsyncioTestCase):
    """The KAGE_SLACK_UX settle hook on the terminal path.

    ``gateway.slack_ux_reactions`` is faked in ``sys.modules``: the real one
    is covered by ``test_slack_ux_reactions.py``; this pins where deliver()
    calls it and that, flag off, deliver() posts exactly what it did before.
    """

    def setUp(self):
        self.calls = []
        self.flag = False
        test = self

        async def settle_delegated(adapter, sub, kind, board=None):
            test.calls.append((sub["task_id"], kind, board))

        fake = SimpleNamespace(enabled=lambda: test.flag, settle_delegated=settle_delegated)
        package = SimpleNamespace(slack_ux_reactions=fake)
        patcher = mock.patch.dict(
            sys.modules, {"gateway": package, "gateway.slack_ux_reactions": fake}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _run(self, adapter, **kwargs):
        watcher = SimpleNamespace()
        await deliver(watcher, adapter, SUB, "heartbeat", _beat(1, "Working."), "", None, HEADER, **kwargs)
        await deliver(watcher, adapter, SUB, "completed", _terminal(2), "✔ done", None, HEADER, **kwargs)

    async def test_flag_off_posts_exactly_what_it_did_before(self):
        with_hook, without = _Adapter(), _Adapter()
        await self._run(with_hook, board="b1")
        with mock.patch.dict(sys.modules, {"gateway": None, "gateway.slack_ux_reactions": None}):
            await self._run(without)
        self.assertEqual((with_hook.sent, with_hook.edits), (without.sent, without.edits))
        self.assertEqual(self.calls, [])

    async def test_flag_on_settles_after_the_terminal_send_only(self):
        self.flag = True
        adapter = _Adapter()
        await self._run(adapter, board="b1")
        self.assertEqual(self.calls, [(SUB["task_id"], "completed", "b1")])

    async def test_a_terminal_post_that_failed_does_not_settle(self):
        self.flag = True

        class _Failing(_Adapter):
            async def send(self, chat_id, content, metadata=None):
                await super().send(chat_id, content, metadata)
                return _Result(False, error="channel_not_found")

        await self._run(_Failing(), board="b1")
        self.assertEqual(self.calls, [])

    async def test_a_settle_that_explodes_never_reaches_the_notifier(self):
        self.flag = True

        async def boom(*_args, **_kwargs):
            raise RuntimeError("reactions.add exploded")

        sys.modules["gateway.slack_ux_reactions"].settle_delegated = boom
        adapter = _Adapter()
        await self._run(adapter)
        self.assertEqual(adapter.sent[-1][1], "✔ done")


SLACK_SUB = {
    "task_id": "t_e0c1",
    "platform": "slack",
    "chat_id": "C0BHY4P7DJM",
    "thread_id": "1790717879.123899",
    "delivery_mode": "notify+wake",
}


class SlackQuietTerminalTest(unittest.IsolatedAsyncioTestCase):
    """KAGE_SLACK_UX on a Slack card: the trail settles to one line, and a
    failure the creator's wake explains is held for the wake step, not posted.

    ``gateway.slack_ux_reactions`` is faked as in :class:`SettleReactionHookTest`;
    ``kanban_notifier`` is the real module, imported flat.
    """

    def setUp(self):
        self.calls = []
        self.flag = True
        test = self

        async def settle_delegated(adapter, sub, kind, board=None):
            test.calls.append((sub["task_id"], kind))

        fake = SimpleNamespace(enabled=lambda: test.flag, settle_delegated=settle_delegated)
        patcher = mock.patch.dict(
            sys.modules,
            {"gateway": SimpleNamespace(slack_ux_reactions=fake), "gateway.slack_ux_reactions": fake},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        wake = mock.patch(
            "kanban_notifier.resolve_wake_kinds",
            return_value=("gave_up", "crashed", "timed_out", "blocked"),
        )
        wake.start()
        self.addCleanup(wake.stop)

    async def _run(self, adapter, sub, kind, message):
        watcher = self.watcher = SimpleNamespace()
        await deliver(watcher, adapter, sub, "heartbeat", _beat(1, "Checking seeded-a."), "", None, HEADER)
        await deliver(watcher, adapter, sub, "heartbeat", _beat(2, "Reading pod state."), "", None, HEADER)
        return await deliver(watcher, adapter, sub, kind, _terminal(3, kind), message, None, HEADER)

    async def test_a_completion_settles_the_trail_to_its_last_line(self):
        adapter = _Adapter()
        await self._run(adapter, SLACK_SUB, "completed", "Both pods are up.")
        self.assertEqual(adapter.edits[-1][1], f"{FINISHED} [default] @platform Reading pod state.")
        self.assertEqual(adapter.sent[-1][1], "Both pods are up.")

    async def test_an_explained_failure_posts_nothing_and_still_settles(self):
        adapter = _Adapter()
        result = await self._run(adapter, SLACK_SUB, "gave_up", "✖ gave up")
        self.assertIsNone(result)
        self.assertEqual(len(adapter.sent), 1, "the gave-up line was posted")
        self.assertEqual(adapter.edits[-1][1], f"{STOPPED} [default] @platform Reading pod state.")
        self.assertEqual(self.calls, [("t_e0c1", "gave_up")])

    async def test_an_explained_failure_is_held_for_the_wake_step(self):
        await self._run(_Adapter(), SLACK_SUB, "gave_up", "✖ gave up")
        held = getattr(self.watcher, kanban_notifier.HELD_ATTR)
        self.assertEqual(
            held[sub_key(SLACK_SUB)], {3: ("gave_up", SLACK_SUB["chat_id"], "✖ gave up", {})},
        )

    async def test_a_line_that_cannot_be_held_is_posted(self):
        adapter = _Adapter()
        with mock.patch("kanban_notifier.hold_explained", side_effect=RuntimeError("boom")):
            result = await self._run(adapter, SLACK_SUB, "gave_up", "✖ gave up")
        self.assertIsNotNone(result)
        self.assertEqual(adapter.sent[-1][1], "✖ gave up")

    async def test_a_failure_nobody_is_woken_for_still_posts(self):
        adapter = _Adapter()
        sub = dict(SLACK_SUB, delivery_mode="notify")
        await self._run(adapter, sub, "gave_up", "✖ gave up")
        self.assertEqual(adapter.sent[-1][1], "✖ gave up")

    async def test_an_explained_by_wake_that_raises_posts_the_line(self):
        adapter = _Adapter()
        with mock.patch("kanban_notifier.resolve_wake_kinds", side_effect=RuntimeError("boom")):
            await self._run(adapter, SLACK_SUB, "crashed", "✖ crashed")
        self.assertEqual(adapter.sent[-1][1], "✖ crashed")

    async def test_other_platforms_are_unchanged_with_the_flag_on(self):
        on, off = _Adapter(), _Adapter()
        await self._run(on, SUB, "gave_up", "✖ gave up")
        self.flag = False
        await self._run(off, SUB, "gave_up", "✖ gave up")
        self.assertEqual((on.sent, on.edits), (off.sent, off.edits))

    async def test_flag_off_slack_posts_exactly_what_it_did_before(self):
        self.flag = False
        for kind, message in (("completed", "✔ done"), ("gave_up", "✖ gave up")):
            with_flag_module, without = _Adapter(), _Adapter()
            await self._run(with_flag_module, SLACK_SUB, kind, message)
            with mock.patch.dict(sys.modules, {"gateway": None, "gateway.slack_ux_reactions": None}):
                await self._run(without, SLACK_SUB, kind, message)
            self.assertEqual(
                (with_flag_module.sent, with_flag_module.edits), (without.sent, without.edits), kind,
            )
            self.assertIn(STOPPED if kind == "gave_up" else FINISHED, with_flag_module.edits[-1][1])
            self.assertIn(f"{BULLET}Checking seeded-a.", with_flag_module.edits[-1][1])


BOARD = "default"
SLACK_HEAD = f"{HEADER}Kanban {SLACK_SUB['task_id']}"
FAILURE_LINES = {
    "gave_up": f"✖ {SLACK_HEAD} gave up after repeated spawn failures",
    "crashed": f"✖ {SLACK_HEAD} worker crashed (pid gone); dispatcher will retry",
    "blocked": f"⏸ {SLACK_HEAD} blocked: needs the prod-a kubeconfig",
    "timed_out": f"⏱ {SLACK_HEAD} timed out (max_runtime=600s); will retry",
}


class SlackHeadsTest(unittest.IsolatedAsyncioTestCase):
    """KAGE_SLACK_UX on a Slack card: no line carries the board tag or ``Kanban <id>``.

    The ``@assignee`` stays: it is how the thread names the specialist.
    """

    setUp = SlackQuietTerminalTest.setUp

    async def _run(self, adapter, sub, kind, message, header=HEADER):
        watcher = self.watcher = SimpleNamespace()
        await deliver(watcher, adapter, sub, "heartbeat", _beat(1, "Checking seeded-a."), "", None, header, board=BOARD)
        await deliver(watcher, adapter, sub, "heartbeat", _beat(2, "Reading pod state."), "", None, header, board=BOARD)
        return await deliver(watcher, adapter, sub, kind, _terminal(3, kind), message, None, header, board=BOARD)

    async def test_progress_lines_keep_only_the_agent(self):
        adapter = _Adapter()
        await self._run(adapter, SLACK_SUB, "completed", "Both pods are up.")
        self.assertEqual(adapter.sent[0][1], f"{IN_PROGRESS} @platform Checking seeded-a.")
        self.assertEqual(adapter.edits[0][1], f"{IN_PROGRESS} @platform\n{BULLET}Checking seeded-a.\n{BULLET}Reading pod state.")
        self.assertEqual(adapter.edits[-1][1], f"{FINISHED} @platform Reading pod state.")

    async def test_the_no_summary_done_head_keeps_only_the_agent(self):
        adapter = _Adapter()
        head = kanban_notifier.COMPLETION_HEAD.format(head=SLACK_HEAD, title="check seeded-a")
        await self._run(adapter, SLACK_SUB, "completed", head)
        self.assertEqual(adapter.sent[-1][1], "✔ @platform done — check seeded-a")

    async def test_a_held_failure_line_keeps_only_the_agent(self):
        for kind, line in FAILURE_LINES.items():
            with self.subTest(kind=kind):
                adapter = _Adapter()
                await self._run(adapter, SLACK_SUB, kind, line)
                held = getattr(self.watcher, kanban_notifier.HELD_ATTR)[sub_key(SLACK_SUB)]
                told = held[3][2]
                self.assertEqual(told, line.replace(SLACK_HEAD, "@platform"))
                await kanban_notifier.tell_unexplained(self.watcher, adapter, SLACK_SUB, set())
                self.assertEqual(adapter.sent[-1][1], told)

    async def test_a_posted_failure_line_keeps_only_the_agent(self):
        adapter = _Adapter()
        sub = dict(SLACK_SUB, delivery_mode="notify")
        await self._run(adapter, sub, "blocked", FAILURE_LINES["blocked"])
        self.assertEqual(adapter.sent[-1][1], "⏸ @platform blocked: needs the prod-a kubeconfig")

    async def test_a_card_with_no_assignee_loses_the_head(self):
        adapter = _Adapter()
        sub = dict(SLACK_SUB, delivery_mode="notify")
        line = f"✖ [{BOARD}] Kanban {SLACK_SUB['task_id']} gave up after repeated spawn failures"
        await self._run(adapter, sub, "gave_up", line, header=f"[{BOARD}] ")
        self.assertEqual(adapter.sent[0][1], f"{IN_PROGRESS} Checking seeded-a.")
        self.assertEqual(adapter.sent[-1][1], "✖ gave up after repeated spawn failures")

    async def test_no_line_carries_the_board_or_the_card(self):
        for kind, line in FAILURE_LINES.items():
            adapter = _Adapter()
            await self._run(adapter, dict(SLACK_SUB, delivery_mode="notify"), kind, line)
            for content in [c for _chat, c, _id in adapter.sent] + [c for _id, c in adapter.edits]:
                self.assertNotIn(f"[{BOARD}]", content)
                self.assertNotIn(SLACK_SUB["task_id"], content)
                self.assertIn("@platform", content)

    async def test_flag_off_keeps_upstreams_heads_byte_for_byte(self):
        self.flag = False
        for kind, line in (("completed", f"✔ {SLACK_HEAD} done — check seeded-a"), *FAILURE_LINES.items()):
            with self.subTest(kind=kind):
                with_flag_module, without = _Adapter(), _Adapter()
                await self._run(with_flag_module, SLACK_SUB, kind, line)
                with mock.patch.dict(sys.modules, {"gateway": None, "gateway.slack_ux_reactions": None}):
                    await self._run(without, SLACK_SUB, kind, line)
                self.assertEqual(
                    (with_flag_module.sent, with_flag_module.edits), (without.sent, without.edits),
                )
                self.assertEqual(with_flag_module.sent[0][1], f"{IN_PROGRESS} {HEADER}Checking seeded-a.")
                self.assertEqual(with_flag_module.sent[-1][1], line)

    async def test_other_platforms_keep_upstreams_heads_with_the_flag_on(self):
        adapter = _Adapter()
        line = f"✖ {HEADER}Kanban {SUB['task_id']} gave up"
        await self._run(adapter, SUB, "gave_up", line)
        self.assertEqual(adapter.sent[0][1], f"{IN_PROGRESS} {HEADER}Checking seeded-a.")
        self.assertEqual(adapter.sent[-1][1], line)


class SlackLineTest(unittest.TestCase):
    def test_changes_requested_names_the_agent(self):
        line = f"🛑 [{BOARD}] Kanban t_1 review requested changes/BLOCK: fix it — reviewer @qa"
        self.assertEqual(
            slack_line(line, HEADER, BOARD, "t_1"),
            "🛑 @platform review requested changes/BLOCK: fix it — reviewer @qa",
        )

    def test_only_the_head_is_trimmed(self):
        line = f"✔ {HEADER}Kanban t_1 done — t\nSee Kanban t_1 in the board."
        self.assertEqual(slack_line(line, HEADER, BOARD, "t_1"), "✔ @platform done — t\nSee Kanban t_1 in the board.")

    def test_no_board(self):
        self.assertEqual(slack_line("✖ @platform Kanban t_1 gave up", "@platform ", None, "t_1"), "✖ @platform gave up")

    def test_a_line_without_the_head_is_unchanged(self):
        self.assertEqual(slack_line("Both pods are up.", HEADER, BOARD, "t_1"), "Both pods are up.")

    def test_a_head_quoted_in_the_handoff_is_the_workers_words(self):
        # completion_text already dropped the head: what is left is the handoff alone.
        for line in (
            f"Retried after {HEADER}Kanban t_1 timed out.",
            f"Both pods are up.\n{HEADER}Kanban t_1 done — earlier run",
        ):
            with self.subTest(line=line):
                self.assertEqual(slack_line(line, HEADER, BOARD, "t_1"), line)

    def test_a_head_after_the_workers_punctuation_is_the_workers_words(self):
        # A quote, emphasis or dash before the head is the worker's, not a marker.
        for prefix in ("> ", "*", "— ", "- ", "✔✔ "):
            line = f"{prefix}{HEADER}Kanban t_1 timed out — retried, now healthy"
            with self.subTest(prefix=prefix):
                self.assertEqual(slack_line(line, HEADER, BOARD, "t_1"), line)

    def test_a_marker_with_a_variation_selector_is_a_marker(self):
        line = f"⏸\ufe0f {HEADER}Kanban t_1 blocked"
        self.assertEqual(slack_line(line, HEADER, BOARD, "t_1"), "⏸\ufe0f @platform blocked")

    def test_no_assignee_drops_the_head_once(self):
        line = f"✔ [{BOARD}] Kanban t_1 done — [{BOARD}] Kanban t_1 was a retry"
        self.assertEqual(
            slack_line(line, f"[{BOARD}] ", BOARD, "t_1"), f"✔ done — [{BOARD}] Kanban t_1 was a retry",
        )


class SlackPlanHookTest(unittest.IsolatedAsyncioTestCase):
    """KAGE_SLACK_UX on a Slack card: notes go on the thread's plan, not a rolling message.

    ``gateway.slack_ux_status`` is faked: the real one is covered by
    ``test_slack_ux_status.py``; this pins where deliver() calls it and what
    it falls back to.
    """

    def setUp(self):
        self.rows = []
        self.moves = []
        self.settled = []
        self.results = []
        self.titles = []
        self.flag = True
        self.plan = True
        self.takes = True
        test = self

        async def deliver_row(adapter, sub, event_id, title, line, moved=None):
            test.rows.append((sub["task_id"], event_id, title, line))
            test.moves.append(moved)
            if isinstance(test.takes, Exception):
                raise test.takes
            return test.takes

        async def settle_row(adapter, sub, kind, result="", title=""):
            test.settled.append((sub["task_id"], kind))
            test.results.append(result)
            test.titles.append(title)

        async def settle_delegated(adapter, sub, kind, board=None):
            return None

        reactions = SimpleNamespace(enabled=lambda: test.flag, settle_delegated=settle_delegated)
        status = SimpleNamespace(enabled=lambda: test.flag and test.plan, deliver_row=deliver_row, settle_row=settle_row)
        patcher = mock.patch.dict(
            sys.modules,
            {
                "gateway": SimpleNamespace(slack_ux_reactions=reactions, slack_ux_status=status),
                "gateway.slack_ux_reactions": reactions,
                "gateway.slack_ux_status": status,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _run(self, adapter, sub=SLACK_SUB):
        watcher = SimpleNamespace()
        for event_id, note in ((1, "Checking seeded-a."), (2, "Reading pod state.")):
            await deliver(
                watcher, adapter, sub, "heartbeat", _beat(event_id, note),
                f"{IN_PROGRESS} {HEADER}{note}", None, HEADER, title="check seeded-a",
            )
        return await deliver(
            watcher, adapter, sub, "completed", _terminal(3), "Both pods are up.", None, HEADER,
            title="check seeded-a",
        )

    async def test_notes_go_on_the_plan_and_the_report_is_posted(self):
        adapter = _Adapter()
        await self._run(adapter)
        self.assertEqual(
            self.rows,
            [("t_e0c1", 1, "check seeded-a", "Checking seeded-a."), ("t_e0c1", 2, "check seeded-a", "Reading pod state.")],
        )
        self.assertEqual(self.settled, [("t_e0c1", "completed")])
        self.assertEqual(self.results, [""], "the completed event carried no summary")
        self.assertEqual([content for _chat, content, _id in adapter.sent], ["Both pods are up."])
        self.assertEqual(adapter.edits, [])

    async def test_a_completed_cards_summary_settles_its_row(self):
        completed = SimpleNamespace(id=3, kind="completed", payload={"summary": "seeded-a · 1.33.4 = default"})
        await deliver(SimpleNamespace(), _Adapter(), SLACK_SUB, "completed", completed, "Done.", None, HEADER)
        self.assertEqual(self.results, ["seeded-a · 1.33.4 = default"])

    async def test_a_terminal_event_names_the_card_for_a_row_it_opens(self):
        # A card that sent no note gets its row from the settle, led by its title.
        completed = SimpleNamespace(id=3, kind="completed", payload={"summary": "1.33.4 = default"})
        await deliver(
            SimpleNamespace(), _Adapter(), SLACK_SUB, "completed", completed, "Done.", None, HEADER, title="seeded-a",
        )
        self.assertEqual(self.titles, ["seeded-a"])

    async def test_a_plan_that_refuses_falls_back_to_the_rolling_line(self):
        # The fallback is the flag-on rolling line: the trail settles to its last line.
        for takes in (False, RuntimeError("boom")):
            with self.subTest(takes=takes):
                self.takes = takes
                refused, without_plan = _Adapter(), _Adapter()
                await self._run(refused)
                self.plan = False
                await self._run(without_plan)
                self.plan = True
                self.assertEqual((refused.sent, refused.edits), (without_plan.sent, without_plan.edits))
                self.assertEqual(refused.edits[-1][1], f"{FINISHED} [default] @platform Reading pod state.")

    async def test_a_plan_taking_over_a_rolling_card_settles_its_message(self):
        adapter, watcher = _Adapter(), SimpleNamespace()
        self.takes = False
        await deliver(
            watcher, adapter, SLACK_SUB, "heartbeat", _beat(1, "Checking seeded-a."),
            f"{IN_PROGRESS} {HEADER}Checking seeded-a.", None, HEADER, title="check seeded-a",
        )
        self.takes = True
        await deliver(
            watcher, adapter, SLACK_SUB, "heartbeat", _beat(2, "Reading pod state."),
            f"{IN_PROGRESS} {HEADER}Reading pod state.", None, HEADER, title="check seeded-a",
        )
        self.assertEqual(len(adapter.edits), 1)
        self.assertTrue(adapter.edits[0][1].startswith(MOVED_TO_PLAN))
        self.assertIn("Checking seeded-a.", adapter.edits[0][1])
        self.assertEqual(tracked_messages(watcher), {})

    async def test_a_move_the_plan_takes_leaves_a_rolling_message_rolling(self):
        adapter, watcher = _Adapter(), SimpleNamespace()
        self.takes = False
        await deliver(
            watcher, adapter, SLACK_SUB, "heartbeat", _beat(1, "Checking seeded-a."),
            f"{IN_PROGRESS} {HEADER}Checking seeded-a.", None, HEADER, title="check seeded-a",
        )
        self.takes = True
        move = SimpleNamespace(id=2, kind="status", payload={"status": "ready"})
        await deliver(watcher, adapter, SLACK_SUB, "status", move, "🔄", None, HEADER)
        self.assertEqual(adapter.edits, [])
        self.assertNotEqual(tracked_messages(watcher), {})

    async def test_a_status_move_reaches_the_plan_as_a_move(self):
        move = SimpleNamespace(id=1, kind="status", payload={"status": " ready "})
        await deliver(SimpleNamespace(), _Adapter(), SLACK_SUB, "status", move, "🔄", None, HEADER)
        await deliver(SimpleNamespace(), _Adapter(), SLACK_SUB, "heartbeat", _beat(2, "note"), "note", None, HEADER)
        self.assertEqual(self.moves, ["ready", None])

    async def test_other_platforms_never_reach_the_plan(self):
        await self._run(_Adapter(), SUB)
        self.assertEqual((self.rows, self.settled), ([], []))

    async def test_a_silent_kind_moves_the_plan_row(self):
        # archived and unblocked format to None upstream, so deliver() never sees them.
        for kind in ("archived", "unblocked"):
            await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind=kind))
        self.assertEqual(self.settled, [("t_e0c1", "archived"), ("t_e0c1", "unblocked")])

    def _blocked_batch(self, last_ping):
        # unblocked, a note, then blocked again: the batch a rewound claim replays.
        events = [SimpleNamespace(id=6, kind="unblocked"), _beat(7, "retrying"), SimpleNamespace(id=8, kind="blocked")]
        sub = {**SLACK_SUB, "last_ping_event_id": last_ping}
        return SimpleNamespace(sub=sub, adapter=_Adapter(), d={"events": events}), events[0]

    async def test_a_replayed_unblock_leaves_a_later_block_waiting(self):
        # The wake after the batch was not accepted; its pings were recorded through 8.
        # Without the batch, last_ping_event_id alone marks the replay.
        notification, unblocked = self._blocked_batch(last_ping=8)
        del notification.d
        await silent_event(notification, unblocked)
        self.assertEqual(self.settled, [])

    async def test_an_unblock_overtaken_in_its_batch_moves_nothing_when_the_ping_record_lags(self):
        # blocked 8 was sent but recording its ping failed, so last_ping_event_id is behind it.
        notification, unblocked = self._blocked_batch(last_ping=0)
        await silent_event(notification, unblocked)
        self.assertEqual(self.settled, [])

    async def test_an_unblock_after_the_last_settle_in_its_batch_still_moves_the_row(self):
        events = [SimpleNamespace(id=5, kind="blocked"), SimpleNamespace(id=6, kind="unblocked"), _beat(7, "retrying")]
        notification = SimpleNamespace(
            sub={**SLACK_SUB, "last_ping_event_id": 5}, adapter=_Adapter(), d={"events": events},
        )
        await silent_event(notification, events[1])
        self.assertEqual(self.settled, [("t_e0c1", "unblocked")])

    async def test_a_retried_failure_after_an_unblock_leaves_the_row_running(self):
        # crashed and timed_out leave a row alone while the dispatcher retries
        # the card, so they settle nothing past the unblock before them.
        for kind in ("crashed", "timed_out"):
            with self.subTest(kind=kind):
                self.settled.clear()
                events = [SimpleNamespace(id=6, kind="unblocked"), SimpleNamespace(id=7, kind=kind)]
                notification = SimpleNamespace(sub=dict(SLACK_SUB), adapter=_Adapter(), d={"events": events})
                await silent_event(notification, events[0])
                self.assertEqual(self.settled, [("t_e0c1", "unblocked")])

    async def test_other_silent_events_and_platforms_do_not(self):
        await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind="heartbeat"))
        await silent_event(SimpleNamespace(sub=SUB, adapter=_Adapter()), SimpleNamespace(kind="archived"))
        self.flag = False
        await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind="archived"))
        self.assertEqual(self.settled, [])

    async def test_a_silent_event_never_raises(self):
        async def boom(adapter, sub, kind, result=""):
            raise RuntimeError("slack down")

        sys.modules["gateway.slack_ux_status"].settle_row = boom
        await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind="archived"))
        await silent_event(SimpleNamespace(), SimpleNamespace(kind="archived"))

    async def test_flag_off_posts_exactly_what_it_did_before(self):
        self.flag = False
        with_module, without = _Adapter(), _Adapter()
        await self._run(with_module)
        with mock.patch.dict(
            sys.modules,
            {"gateway": None, "gateway.slack_ux_reactions": None, "gateway.slack_ux_status": None},
        ):
            await self._run(without)
        self.assertEqual((with_module.sent, with_module.edits), (without.sent, without.edits))
        self.assertEqual((self.rows, self.settled), ([], []))


class CreatorOpenTest(unittest.IsolatedAsyncioTestCase):
    """The read that decides whether a card beneath a fan-out folds its report, against a sqlite board."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "kanban.db"
        self.addCleanup(shutil.rmtree, self.path.parent)
        conn = sqlite3.connect(self.path)
        conn.executescript(
            "CREATE TABLE tasks (id TEXT, status TEXT);"
            "CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT);"
            "CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT);"
            "INSERT INTO tasks VALUES ('t_parent', 'running'), ('t_e0c1', 'done'), ('t_e0c2', 'running');"
            "INSERT INTO task_events VALUES ('t_e0c1', 'created', '{\"creator_task_id\": \"t_parent\"}');"
            "INSERT INTO task_events VALUES ('t_e0c2', 'created', '{\"creator_task_id\": \"t_parent\"}');"
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs VALUES ('t_parent', 'Slack', ?, ?)",
            (SLACK_SUB["chat_id"], SLACK_SUB["thread_id"]),
        )
        conn.commit()
        conn.close()
        connect = SimpleNamespace(connect=lambda board=None: sqlite3.connect(self.path))
        patcher = mock.patch.dict(
            sys.modules,
            {"hermes_cli": SimpleNamespace(kanban_db_connect=connect), "hermes_cli.kanban_db_connect": connect},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set(self, sql, *params):
        conn = sqlite3.connect(self.path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    async def test_an_open_creator_on_the_same_thread_folds(self):
        self.assertTrue(await kanban_progress_lines._folds(SLACK_SUB, None))

    async def test_a_creator_that_is_done_or_archived_does_not(self):
        # A worker that answered and then filed a follow-up: the follow-up is the answer.
        for status in ("done", "archived"):
            with self.subTest(status=status):
                self._set("UPDATE tasks SET status = ? WHERE id = 't_parent'", status)
                self.assertFalse(await kanban_progress_lines._folds(SLACK_SUB, None))

    async def test_a_creators_only_child_does_not(self):
        # A single-cluster delegation: the Cluster Agent's whole report is the answer's evidence.
        self._set("DELETE FROM task_events WHERE task_id = 't_e0c2'")
        self.assertFalse(await kanban_progress_lines._folds(SLACK_SUB, None))

    def _grandchild(self, creator="t_e0c1"):
        self._set(
            "INSERT INTO task_events VALUES ('t_g1', 'created', ?)", f'{{"creator_task_id": "{creator}"}}',
        )
        return {**SLACK_SUB, "task_id": "t_g1"}

    async def test_a_card_a_fanned_out_card_filed_folds(self):
        # A Platform Agent card per cluster handing its cluster to the Cluster Agent:
        # the per-cluster card's row and the fan-out's answer carry the report.
        self.assertTrue(await kanban_progress_lines._folds(self._grandchild(), None))

    async def test_a_chain_with_no_fan_out_above_it_does_not(self):
        # Planning Agent, Platform Agent, Cluster Agent, one card each: a single-cluster delegation.
        self._set("DELETE FROM task_events WHERE task_id = 't_e0c2'")
        self.assertFalse(await kanban_progress_lines._folds(self._grandchild(), None))

    async def test_the_nearest_fan_out_decides(self):
        # t_e0c1 fanned out in turn and has finished: its own answer stands, whatever its creator is doing.
        sub = self._grandchild()
        self._set("INSERT INTO task_events VALUES ('t_g2', 'created', '{\"creator_task_id\": \"t_e0c1\"}')")
        self.assertFalse(await kanban_progress_lines._folds(sub, None))
        self._set("INSERT INTO kanban_notify_subs VALUES ('t_e0c1', 'slack', ?, ?)", SLACK_SUB["chat_id"], SLACK_SUB["thread_id"])
        self._set("UPDATE tasks SET status = 'running' WHERE id = 't_e0c1'")
        self.assertTrue(await kanban_progress_lines._folds(sub, None))

    async def test_a_creator_on_another_thread_or_none_does_not(self):
        self.assertFalse(await kanban_progress_lines._folds({**SLACK_SUB, "thread_id": "1790717879.000001"}, None))
        self._set("DELETE FROM task_events")
        self.assertFalse(await kanban_progress_lines._folds(SLACK_SUB, None))

    async def test_a_read_that_fails_posts_the_report(self):
        sys.modules["hermes_cli.kanban_db_connect"].connect = mock.Mock(side_effect=RuntimeError("locked"))
        self.assertFalse(await kanban_progress_lines._folds(SLACK_SUB, None))


class SlackMomentsHookTest(unittest.IsolatedAsyncioTestCase):
    """KAGE_SLACK_UX on a Slack card: a question and an opened PR get messages of their own.

    ``gateway.slack_ux_moments`` is faked: the real one is covered by
    ``test_slack_ux_moments.py``; this pins where deliver() calls it and what
    it falls back to.
    """

    PR_NOTE = "Opened https://github.com/acme/fleet/pull/7 raising the limit."

    def setUp(self):
        self.flag = True
        self.asked = []
        self.announced = []
        self.posts_question = True
        self.settled = []
        self.questions_settled = []
        self.asked_event = 0
        test = self

        async def needs_you(adapter, sub, payload, event_id=0):
            test.asked.append((sub["task_id"], payload, event_id))
            if isinstance(test.posts_question, Exception):
                raise test.posts_question
            return test.posts_question

        async def pr_opened(adapter, sub, text):
            test.announced.append((sub["task_id"], text, len(adapter.sent)))
            return True

        async def settle_delegated(adapter, sub, kind, board=None):
            test.settled.append((sub["task_id"], kind))

        async def settle_question(adapter, sub):
            test.questions_settled.append(sub["task_id"])

        reactions = SimpleNamespace(enabled=lambda: test.flag, settle_delegated=settle_delegated)
        moments = SimpleNamespace(
            enabled=lambda: test.flag,
            asked=lambda sub, event_id: event_id == test.asked_event,
            needs_you=needs_you,
            pr_opened=pr_opened,
            settle_question=settle_question,
        )
        self.modules = {
            "gateway": SimpleNamespace(slack_ux_reactions=reactions, slack_ux_moments=moments),
            "gateway.slack_ux_reactions": reactions,
            "gateway.slack_ux_moments": moments,
        }
        patcher = mock.patch.dict(sys.modules, self.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        wake = mock.patch("kanban_notifier.resolve_wake_kinds", return_value=("blocked",))
        wake.start()
        self.addCleanup(wake.stop)

    async def _blocked(self, adapter, payload, sub=SLACK_SUB):
        self.watcher = SimpleNamespace()
        ev = SimpleNamespace(id=3, kind="blocked", payload=payload)
        return await deliver(self.watcher, adapter, sub, "blocked", ev, "⏸ t_e0c1 blocked: which?", None, HEADER)

    async def _notes_then_report(self, adapter, report="Both pods are up.", sub=SLACK_SUB):
        watcher = SimpleNamespace()
        for event_id, note in ((1, "Checking seeded-a."), (2, self.PR_NOTE)):
            await deliver(watcher, adapter, sub, "heartbeat", _beat(event_id, note), "", None, HEADER)
        return await deliver(watcher, adapter, sub, "completed", _terminal(3), report, None, HEADER)

    def _plan(self, shows, folds):
        # The thread's plan, and whether the card's creator is still open on it.
        test = self
        test.plan_settled = []

        async def settle_row(adapter, sub, kind, result="", title=""):
            test.plan_settled.append(kind)
            return shows

        status = SimpleNamespace(enabled=lambda: True, settle_row=settle_row)
        sys.modules["gateway.slack_ux_status"] = status
        sys.modules["gateway"].slack_ux_status = status
        reader = mock.AsyncMock(return_value=folds)
        patcher = mock.patch.object(kanban_progress_lines, "_folds", reader)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reader

    async def _child(self, adapter, kind="completed", payload=None, message="seeded-a: 1.33.4 = default."):
        ev = SimpleNamespace(id=3, kind=kind, payload=payload or {})
        return await deliver(SimpleNamespace(), adapter, SLACK_SUB, kind, ev, message, None, HEADER, title="seeded-a")

    async def test_a_fanned_out_cards_completion_folds_into_its_row(self):
        self._plan(shows=True, folds=True)
        adapter = _Adapter()
        self.assertIsNone(await self._child(adapter, message=f"seeded-a: {self.PR_NOTE}"))
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.settled, [("t_e0c1", "completed")], "the arrival reaction was left on")
        self.assertEqual(self.announced, [("t_e0c1", f"seeded-a: {self.PR_NOTE}", 0)], "a PR it opened lost its message")

    async def test_a_completion_the_plan_does_not_show_or_with_no_open_creator_posts(self):
        for shows, folds in ((False, True), (True, False)):
            with self.subTest(shows=shows, folds=folds):
                reader = self._plan(shows=shows, folds=folds)
                adapter = _Adapter()
                await self._child(adapter)
                self.assertEqual([content for _chat, content, _id in adapter.sent], ["seeded-a: 1.33.4 = default."])
                self.assertEqual(reader.await_count, int(shows), "the creator was read for a row not shown")

    async def test_a_fanned_out_cards_question_still_posts(self):
        reader = self._plan(shows=True, folds=True)
        payload = {"kind": "needs_input", "reason": "seeded-c is unreachable. Skip it?"}
        self.assertIsNone(await self._child(_Adapter(), "blocked", payload, "⏸ t_e0c1 blocked: skip it?"))
        self.assertEqual(self.asked, [("t_e0c1", payload, 3)])
        self.assertEqual(self.plan_settled, ["blocked"])
        reader.assert_not_awaited()

    async def test_a_fanned_out_card_that_fails_still_posts(self):
        for kind in ("gave_up", "crashed", "timed_out"):
            with self.subTest(kind=kind):
                reader = self._plan(shows=True, folds=True)
                adapter = _Adapter()
                await self._child(adapter, kind, message=f"✖ t_e0c1 {kind}")
                self.assertEqual(self.plan_settled, [kind])
                self.assertEqual([content for _chat, content, _id in adapter.sent], [f"✖ t_e0c1 {kind}"])
                reader.assert_not_awaited()

    async def test_a_needs_input_block_posts_the_question_instead_of_the_line(self):
        adapter = _Adapter()
        payload = {"kind": "needs_input", "reason": "Which cluster?"}
        self.assertIsNone(await self._blocked(adapter, payload))
        self.assertEqual(self.asked, [("t_e0c1", payload, 3)])
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.settled, [("t_e0c1", "blocked")])
        self.assertFalse(getattr(self.watcher, kanban_notifier.HELD_ATTR, {}), "the line was also held")

    async def test_a_replayed_block_neither_settles_nor_reposts_its_question(self):
        self.asked_event = 3
        adapter = _Adapter()
        self.assertIsNone(await self._blocked(adapter, {"kind": "needs_input", "reason": "Which cluster?"}))
        self.assertEqual((self.asked, self.questions_settled, adapter.sent), ([], [], []))
        self.assertFalse(getattr(self.watcher, kanban_notifier.HELD_ATTR, {}), "the replay held the line")

    async def test_a_failed_replay_check_posts_the_question(self):
        def asked(sub, event_id):
            raise RuntimeError("boom")

        self.modules["gateway.slack_ux_moments"].asked = asked
        self.assertIsNone(await self._blocked(_Adapter(), {"kind": "needs_input", "reason": "Which cluster?"}))
        self.assertEqual(len(self.asked), 1)

    async def test_a_block_with_no_question_keeps_the_blocked_path(self):
        for posts in (False, RuntimeError("boom")):
            with self.subTest(posts=posts):
                self.posts_question = posts
                adapter = _Adapter()
                self.assertIsNone(await self._blocked(adapter, {"kind": "capability", "reason": "no GPU"}))
                held = getattr(self.watcher, kanban_notifier.HELD_ATTR)
                self.assertIn(3, held[sub_key(SLACK_SUB)], "the blocked line was not held for the wake")

    async def test_every_event_settles_the_open_question(self):
        await self._blocked(_Adapter(), {"kind": "needs_input", "reason": "Which cluster?"})
        self.assertEqual(self.questions_settled, ["t_e0c1"], "a block settles any earlier question first")
        await self._notes_then_report(_Adapter())
        self.assertEqual(self.questions_settled, ["t_e0c1"] * 4)

    async def test_a_block_with_no_question_settles_the_earlier_one(self):
        self.posts_question = False
        await self._blocked(_Adapter(), {"kind": "capability", "reason": "no GPU"})
        self.assertEqual(self.questions_settled, ["t_e0c1"])

    async def test_an_unblocked_or_archived_card_settles_its_question(self):
        # Both format to None upstream, so only silent_event sees them.
        for kind in ("unblocked", "archived"):
            await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind=kind))
        await silent_event(SimpleNamespace(sub=SLACK_SUB, adapter=_Adapter()), SimpleNamespace(kind="heartbeat"))
        self.assertEqual(self.questions_settled, ["t_e0c1"] * 2)

    async def test_a_failed_settle_still_delivers(self):
        async def settle_question(adapter, sub):
            raise RuntimeError("boom")

        self.modules["gateway.slack_ux_moments"].settle_question = settle_question
        adapter = _Adapter()
        result = await self._notes_then_report(adapter)
        self.assertTrue(result.success)
        self.assertEqual(len(adapter.sent), 2)

    async def test_other_terminal_kinds_never_ask(self):
        await self._notes_then_report(_Adapter())
        self.assertEqual(self.asked, [])

    async def test_an_opened_pr_follows_the_note_and_the_report(self):
        adapter = _Adapter()
        await self._notes_then_report(adapter, report=f"Done. {self.PR_NOTE}")
        self.assertEqual(
            [(text, sent) for _task, text, sent in self.announced],
            [("Checking seeded-a.", 1), (self.PR_NOTE, 1), (f"Done. {self.PR_NOTE}", 2)],
        )

    async def test_a_long_note_is_scanned_whole(self):
        # The rolling line clips at 300 characters, cutting a url past it whole.
        note = "Reconciled the overlays. " * 13 + self.PR_NOTE
        await deliver(SimpleNamespace(), _Adapter(), SLACK_SUB, "heartbeat", _beat(1, note), "", None, HEADER)
        self.assertEqual([text for _task, text, _sent in self.announced], [note])

    async def test_a_failed_send_announces_nothing(self):
        class _Refusing(_Adapter):
            async def send(self, chat_id, content, metadata=None):
                return _Result(False, error="channel_not_found")

        await self._notes_then_report(_Refusing())
        self.assertEqual(self.announced, [])

    async def test_other_platforms_never_reach_the_moments(self):
        await self._notes_then_report(_Adapter(), sub=SUB)
        await self._blocked(_Adapter(), {"kind": "needs_input", "reason": "Which?"}, sub=SUB)
        self.assertEqual((self.asked, self.announced), ([], []))

    async def test_flag_off_posts_exactly_what_it_did_before(self):
        self.flag = False
        payload = {"kind": "needs_input", "reason": "Which cluster?"}
        with_module, without = _Adapter(), _Adapter()
        await self._notes_then_report(with_module)
        await self._blocked(with_module, payload)
        with mock.patch.dict(sys.modules, {name: None for name in self.modules}):
            await self._notes_then_report(without)
            await self._blocked(without, payload)
        self.assertEqual((with_module.sent, with_module.edits), (without.sent, without.edits))
        self.assertEqual((self.asked, self.announced, self.questions_settled), ([], [], []))


class _SlackClient:
    def __init__(self, adapter):
        self.adapter = adapter

    async def chat_postMessage(self, **kwargs):
        self.adapter.posts.append(kwargs)
        return {"ts": "1790717880.000300"}

    async def chat_update(self, **kwargs):
        self.adapter.updates.append(kwargs)


class _SlackAdapter(_Adapter):
    """A Slack adapter: the moments module posts and updates through its client."""

    def __init__(self):
        super().__init__()
        self.posts = []
        self.updates = []

    def _get_client(self, chat_id, team_id=None):
        return _SlackClient(self)


class TypedAnswerSettlesTheQuestionTest(unittest.IsolatedAsyncioTestCase):
    """KAGE_SLACK_UX on: a card unblocked by a typed answer takes its question's buttons off.

    Upstream claims ``unblocked`` but formats it to None, so ``_send_pings``
    skips it before ``deliver`` and only the applier's silent-kind hook passes
    it on. This runs the applied notifier with the real ``deliver``,
    ``silent_event`` and ``slack_ux_moments``: the question posts on
    ``blocked`` and loses its buttons and "waiting on you" on ``unblocked``,
    not at the card's next delivered event.
    """

    QUESTION = {"kind": "needs_input", "reason": "Which cluster should I check first?\n- seeded-a\n- seeded-b"}

    def setUp(self):
        self.flag = "1"
        test = self

        async def settle_delegated(adapter, sub, kind, board=None):
            return None

        reactions = SimpleNamespace(
            enabled=lambda: test.flag == "1", settle_delegated=settle_delegated,
        )
        modules = {
            "gateway": SimpleNamespace(
                slack_ux_reactions=reactions,
                slack_ux_moments=slack_ux_moments,
                kanban_progress_lines=kanban_progress_lines,
            ),
            "gateway.slack_ux_reactions": reactions,
            "gateway.slack_ux_moments": slack_ux_moments,
            "gateway.kanban_progress_lines": kanban_progress_lines,
        }
        for patcher in (
            mock.patch.dict(sys.modules, modules),
            mock.patch.object(slack_ux_moments, "_moments", slack_moments),
            mock.patch.object(slack_ux_moments, "_presenter", slack_presenter),
            mock.patch.object(slack_ux_moments, "_questions", type(slack_ux_moments._questions)()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _notification(self, adapter):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "gateway").mkdir()
        (root / "tools").mkdir()
        # The fixture's _send_pings ends at the skip; upstream sends what it did not skip.
        (root / applier.NOTIFIER_RELATIVE).write_text(
            _notifier_source(True) + "            await self._send_event(ev, msg)\n"
        )
        (root / applier.SCHEMAS_RELATIVE).write_text(_SCHEMAS_SOURCE)
        applier.apply(root)
        path = root / applier.NOTIFIER_RELATIVE
        namespace = {}
        exec(compile("from typing import Any, Callable\n" + path.read_text(), str(path), "exec"), namespace)
        formatters = namespace["_EVENT_FORMATTERS"]
        formatters["blocked"] = lambda ev, n: (f"⏸ {n.head} blocked", None, None)
        notification = namespace["_KanbanNotification"](SimpleNamespace(), {})
        notification.sub, notification.adapter = SLACK_SUB, adapter
        notification.board_slug, notification.title = "default", "Pick a cluster"
        # Upstream's format_event, reduced: a kind with no formatter is silent.
        notification.format_event = lambda ev: (
            formatters[ev.kind](ev, notification)[0] if ev.kind in formatters else None
        )
        return notification

    async def _ask_then_unblock(self, adapter):
        notification = self._notification(adapter)
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": self.flag}):
            notification.events = [SimpleNamespace(id=3, kind="blocked", payload=self.QUESTION)]
            await notification._send_pings()
            posted = list(adapter.posts)
            notification.events = [SimpleNamespace(id=4, kind="unblocked", payload=None)]
            await notification._send_pings()
        return posted

    async def test_an_unblocked_card_takes_the_buttons_off_its_question(self):
        adapter = _SlackAdapter()
        posted = await self._ask_then_unblock(adapter)
        self.assertEqual(len(posted), 1)
        self.assertTrue(_choices(slack_presenter.message_blocks(posted[0])))
        self.assertEqual(len(adapter.updates), 1, "the unblock left the question's buttons live")
        settled = adapter.updates[0]
        self.assertEqual(settled["ts"], "1790717880.000300")
        self.assertEqual(_choices(slack_presenter.message_blocks(settled)), [])
        self.assertNotIn(
            slack_presenter.WAITING_BLOCK_ID, [block.get("block_id") for block in slack_presenter.message_blocks(settled)],
        )
        self.assertEqual(adapter.sent, [])

    async def test_flag_off_the_unblock_touches_nothing(self):
        self.flag = "0"
        adapter = _SlackAdapter()
        await self._ask_then_unblock(adapter)
        self.assertEqual((adapter.posts, adapter.updates), ([], []))
        self.assertEqual(len(adapter.sent), 1)


def _choices(blocks):
    return [
        element for block in blocks if block.get("type") == "actions"
        for element in block.get("elements") or ()
        if slack_presenter.CHOICE_ACTION_ID_PATTERN.search(str(element.get("action_id") or ""))
    ]


class ReplayedUnblockTest(TypedAnswerSettlesTheQuestionTest):
    """A rewound claim replays its batch: a replayed ``unblocked`` must not undo the ``blocked`` after it.

    Upstream skips a ping already sent by ``last_ping_event_id``; a silent kind
    is never recorded there. ``record`` False is a send whose ping record
    failed, so ``last_ping_event_id`` lags the ``blocked`` that was sent.
    """

    SECOND = {"kind": "needs_input", "reason": "Which namespace?\n- default\n- prod"}

    def _replaying(self, adapter, record):
        notification = self._notification(adapter)
        notification.sub = dict(SLACK_SUB)
        sent = notification._send_event

        async def send(ev, msg):
            if ev.id <= notification.sub.get("last_ping_event_id", 0):
                return  # upstream's dedup, ahead of the send
            await sent(ev, msg)
            if record:
                notification.sub["last_ping_event_id"] = ev.id

        notification._send_event = send
        return notification

    async def _deliver(self, notification, events):
        notification.events = events
        notification.d = {"events": events}
        await notification._send_pings()

    async def _ask_again_then_replay(self, record):
        adapter = _SlackAdapter()
        notification = self._replaying(adapter, record)
        batch = [
            SimpleNamespace(id=4, kind="unblocked", payload=None),
            SimpleNamespace(id=5, kind="blocked", payload=self.SECOND),
        ]
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            await self._deliver(notification, [SimpleNamespace(id=3, kind="blocked", payload=self.QUESTION)])
            await self._deliver(notification, batch)
            # The wake was not accepted: the claim rewinds and the same batch is delivered again.
            await self._deliver(notification, batch)
        return adapter, notification, batch

    async def test_a_replayed_unblock_leaves_the_later_question_open(self):
        adapter, _notification, _batch = await self._ask_again_then_replay(record=True)
        self.assertEqual([entry[0] for entry in slack_ux_moments._questions.values()], [5])
        self.assertEqual(len(adapter.updates), 1, "the replay settled the question the later block posted")

    async def test_the_retried_wake_keeps_its_no_re_ask_note(self):
        _adapter, notification, batch = await self._ask_again_then_replay(record=True)
        text = slack_ux_moments.wake_text(notification.sub, batch, ["blocked"], "W")
        self.assertIn(slack_ux_moments.WAKE_NOTE, text)

    async def test_a_replayed_unblock_leaves_the_question_open_when_the_ping_record_lags(self):
        # The lagging record re-sends blocked 5; unguarded, the replayed unblock
        # first settles its question, so the user is asked it a second time.
        adapter, notification, batch = await self._ask_again_then_replay(record=False)
        self.assertEqual((len(adapter.posts), len(adapter.updates)), (2, 1))
        self.assertEqual([entry[0] for entry in slack_ux_moments._questions.values()], [5])
        text = slack_ux_moments.wake_text(notification.sub, batch, ["blocked"], "W")
        self.assertIn(slack_ux_moments.WAKE_NOTE, text)


if __name__ == "__main__":
    unittest.main()
