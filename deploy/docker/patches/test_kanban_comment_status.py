"""Unit tests for the kanban comment status patch installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kanban_comment_status as kcs
from apply_kanban_comment_status import ANCHOR, RELATIVE, SENTINEL, apply
from kanban_comment_status import (
    READER_NEXT,
    READER_NONE,
    READER_RUNNING,
    TERMINAL_STATUSES,
    delivery_fields,
)

# The two columns of the two tables this patch reads, from the real SCHEMA_SQL
# in hermes_cli/kanban_db.py. idx_notify_task is what makes the subscription
# probe a point lookup rather than a scan, so it is here too.
SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    chat_type     TEXT,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
CREATE INDEX idx_notify_task ON kanban_notify_subs(task_id);
"""

CARD = "t_a8f58a2a"  # the card from the 2026-08-08 incident

# Every status hermes_cli/kanban_db.py's VALID_STATUSES allows.
ALL_STATUSES = (
    "triage", "todo", "scheduled", "ready", "running",
    "blocked", "review", "done", "archived",
)


class FakeTask:
    def __init__(self, status):
        self.status = status


class FakeKb:
    """Stands in for the hermes_cli.kanban_db module the handler is handed.

    Only ``get_task`` is used by this patch; the fake makes "the row is gone"
    and "the lookup raised" separately expressible, which a real board cannot
    do without corrupting itself.
    """

    def __init__(self, conn, raises=None):
        self._conn = conn
        self._raises = raises

    def get_task(self, conn, task_id):
        if self._raises is not None:
            raise self._raises
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return FakeTask(row[0]) if row else None


def board(status="running", subscribed=False, card=CARD):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tasks VALUES (?, ?)", (card, status))
    if subscribed:
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, created_at) "
            "VALUES (?, 'slack', 'C0PLATFORM', '1723033132.001', 0)",
            (card,),
        )
    conn.commit()
    return conn


def fields(status="running", subscribed=False, raises=None, card=CARD):
    conn = board(status, subscribed, card)
    return delivery_fields(FakeKb(conn, raises), conn, card)


class OpenCardTest(unittest.TestCase):
    def test_a_running_card_names_the_live_worker(self):
        out = fields("running")
        self.assertEqual(out["task_status"], "running")
        self.assertEqual(out["comment_reaches"], READER_RUNNING)

    def test_an_unstarted_card_names_the_next_worker(self):
        for status in ("triage", "todo", "scheduled", "ready", "blocked", "review"):
            with self.subTest(status=status):
                out = fields(status)
                self.assertEqual(out["task_status"], status)
                self.assertEqual(out["comment_reaches"], READER_NEXT)

    def test_a_subscribed_card_reports_that_completing_reaches_chat(self):
        self.assertIs(fields("running", subscribed=True)["completion_posts_to_chat"], True)

    def test_an_unsubscribed_card_says_so_rather_than_staying_silent(self):
        # False is the answer to "will the eventual completion post to chat";
        # omitting it would read as "not applicable" on a card that can deliver.
        self.assertIs(fields("running")["completion_posts_to_chat"], False)

    def test_an_open_card_carries_no_note(self):
        self.assertNotIn("note", fields("running"))

    def test_another_cards_subscription_is_not_borrowed(self):
        conn = board("running", subscribed=False)
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, created_at) "
            "VALUES ('t_other', 'slack', 'C0PLATFORM', '', 0)"
        )
        conn.commit()
        out = delivery_fields(FakeKb(conn), conn, CARD)
        self.assertIs(out["completion_posts_to_chat"], False)


class TerminalCardTest(unittest.TestCase):
    def test_done_and_archived_are_the_terminal_set(self):
        # Coupled to gateway/kanban_watchers.py's own terminal test; the
        # verifier is what enforces the coupling against the real source.
        self.assertEqual(set(TERMINAL_STATUSES), {"done", "archived"})

    def test_a_terminal_card_reports_that_nobody_will_read_it(self):
        for status in ("done", "archived"):
            with self.subTest(status=status):
                out = fields(status)
                self.assertEqual(out["task_status"], status)
                self.assertEqual(out["comment_reaches"], READER_NONE)

    def test_the_note_forbids_promising_a_delivery(self):
        note = fields("done")["note"]
        self.assertIn("Do NOT say that results will follow", note)

    def test_the_note_says_where_the_answer_already_is(self):
        note = fields("done")["note"]
        self.assertIn("kanban_show", note)
        self.assertIn(CARD, note)
        self.assertIn("kanban_create", note)

    def test_the_note_names_the_status_it_is_talking_about(self):
        self.assertIn("archived", fields("archived")["note"])

    def test_a_terminal_card_never_advertises_a_delivery_channel(self):
        # The incident's whole shape: a done card whose subscription row has
        # not been reaped yet. Status is durable, the row is one notifier tick
        # from deletion, and a model handed both would believe the row.
        for subscribed in (False, True):
            with self.subTest(subscribed=subscribed):
                out = fields("done", subscribed=subscribed)
                self.assertNotIn("completion_posts_to_chat", out)

    def test_the_comment_is_never_refused(self):
        # delivery_fields describes; it cannot veto. There is no return value
        # it can produce that the handler reads as an error, because the
        # handler splats it into _ok().
        out = fields("done")
        self.assertNotIn("ok", out)
        self.assertNotIn("error", out)


class FailsTowardUpstreamTest(unittest.TestCase):
    def test_a_raising_lookup_degrades_to_no_fields(self):
        self.assertEqual(fields(raises=RuntimeError("board is locked")), {})

    def test_a_missing_card_degrades_to_no_fields(self):
        conn = board("running")
        self.assertEqual(delivery_fields(FakeKb(conn), conn, "t_nope"), {})

    def test_a_task_row_without_a_status_degrades_to_no_fields(self):
        conn = board("")
        self.assertEqual(delivery_fields(FakeKb(conn), conn, CARD), {})

    def test_a_board_with_no_subscriptions_table_keeps_the_status(self):
        # A legacy board predating kanban_notify_subs; kanban_db's
        # count_notify_subs documents the same case.
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.execute("INSERT INTO tasks VALUES (?, 'running')", (CARD,))
        conn.commit()
        out = delivery_fields(FakeKb(conn), conn, CARD)
        self.assertEqual(out["task_status"], "running")
        self.assertEqual(out["comment_reaches"], READER_RUNNING)
        self.assertNotIn("completion_posts_to_chat", out)

    def test_a_raising_subscription_probe_keeps_the_status(self):
        conn = board("running")
        with mock.patch.object(kcs, "_has_chat_subscriber", side_effect=OSError):
            out = delivery_fields(FakeKb(conn), conn, CARD)
        self.assertEqual(out["task_status"], "running")
        self.assertNotIn("completion_posts_to_chat", out)

    def test_no_status_the_kernel_allows_is_left_undescribed(self):
        for status in ALL_STATUSES:
            with self.subTest(status=status):
                out = fields(status)
                self.assertEqual(out["task_status"], status)
                self.assertIn(
                    out["comment_reaches"],
                    (READER_RUNNING, READER_NEXT, READER_NONE),
                )

    def test_every_field_survives_json_encoding(self):
        # _ok() json.dumps whatever it is splatted; a value it cannot encode
        # would turn a written comment into a TypeError.
        import json

        for status in ALL_STATUSES:
            json.dumps({"ok": True, **fields(status)})

    def test_a_weird_status_is_reported_verbatim_not_guessed_at(self):
        out = fields("some_new_upstream_status")
        self.assertEqual(out["task_status"], "some_new_upstream_status")
        self.assertEqual(out["comment_reaches"], READER_NEXT)


# The comment handler, reduced to the two lines the patch anchors on plus the
# neighbours that make the shape real: `conn` is still open, and add_comment has
# already committed.
UPSTREAM_TOOLS = '''\
def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    body = args.get("body")
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
'''

# The other handlers' returns, verbatim from the real module, as a collision
# guard: the anchor must not match anything outside _handle_comment.
NEIGHBOURS = '''\
            return _ok(task_id=tid)
            return _ok(task_id=tid, attachment_id=aid)
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
'''


def patch_tree(source):
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_text(source)
    apply(root)
    return target.read_text()


class ApplyTest(unittest.TestCase):
    def test_the_anchor_matches_upstream_exactly_once(self):
        self.assertEqual(UPSTREAM_TOOLS.count(ANCHOR), 1)

    def test_the_anchor_does_not_match_a_neighbouring_return(self):
        self.assertEqual(NEIGHBOURS.count(ANCHOR), 0)

    def test_the_lookup_lands_between_the_write_and_the_return(self):
        patched = patch_tree(UPSTREAM_TOOLS)
        write = patched.index("cid = kb.add_comment(")
        call = patched.index(SENTINEL)
        close = patched.index("conn.close()")
        self.assertTrue(write < call < close, "the lookup must see an open conn")

    def test_upstream_return_fields_are_preserved(self):
        patched = patch_tree(UPSTREAM_TOOLS)
        self.assertIn("task_id=tid,", patched)
        self.assertIn("comment_id=cid,", patched)

    def test_the_import_trailer_is_appended(self):
        patched = patch_tree(UPSTREAM_TOOLS)
        self.assertIn(
            "from tools.kanban_comment_status import (  # noqa: E402", patched
        )
        self.assertIn("delivery_fields as _comment_delivery_fields", patched)

    def test_the_patched_module_still_parses(self):
        ast.parse(patch_tree(UPSTREAM_TOOLS))

    def test_the_patched_handler_returns_the_merged_fields(self):
        # Execute the patched handler rather than reading it: an anchored edit
        # that parses can still splat into the wrong call.
        patched = patch_tree(UPSTREAM_TOOLS).replace(
            "from tools.kanban_comment_status import (  # noqa: E402",
            "from kanban_comment_status import (  # noqa: E402",
        )
        conn = board("done")
        ns = {
            "os": __import__("os"),
            "_ok": lambda **f: {"ok": True, **f},
            "tool_error": lambda m: {"ok": False, "error": m},
            "_connect": lambda board=None: (FakeKb(conn), conn),
        }
        exec(compile(patched, "<patched>", "exec"), ns)
        # The handler closes conn in its `finally`; the lookup has already run
        # by then, which is the property this test is checking.
        with mock.patch.object(FakeKb, "add_comment", create=True, return_value=7):
            out = ns["_handle_comment"]({"task_id": CARD, "body": "annotating"})
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["task_id"], CARD)
        self.assertEqual(out["comment_id"], 7)
        self.assertEqual(out["task_status"], "done")
        self.assertEqual(out["comment_reaches"], READER_NONE)

    def test_a_drifted_anchor_fails_loudly(self):
        drifted = UPSTREAM_TOOLS.replace(
            "kb.add_comment(conn, tid, author=author, body=str(body))",
            "kb.add_comment(conn, tid, author=author, body=str(body), board=board)",
        )
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(drifted)
        self.assertIn("found 0", str(ctx.exception))

    def test_a_duplicated_anchor_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            patch_tree(UPSTREAM_TOOLS + UPSTREAM_TOOLS)
        self.assertIn("found 2", str(ctx.exception))

    def test_applying_twice_fails_rather_than_stacking_a_second_import(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_TOOLS)
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))

    def test_a_missing_file_fails_loudly(self):
        with self.assertRaises(SystemExit) as ctx:
            apply(Path(tempfile.mkdtemp()))
        self.assertIn("does not exist", str(ctx.exception))


class AnchorCollisionTest(unittest.TestCase):
    """The anchor must not overlap the four patches already in this file.

    Two appliers matching the same text is the failure mode that ships a
    half-patched image, and the ordering between them would then be silently
    load-bearing.
    """

    def test_no_other_applier_claims_these_lines(self):
        here = Path(__file__).resolve().parent
        others = (
            "apply_kanban_auto_subscribe.py",
            "apply_kanban_result_required.py",
            "apply_kanban_worker_tools.py",
            "apply_cron_run_scope.py",
        )
        for name in others:
            with self.subTest(applier=name):
                src = (here / name).read_text()
                for line in ANCHOR.strip("\n").split("\n"):
                    self.assertNotIn(
                        line.strip(), src,
                        f"{name} references a line this patch anchors on",
                    )

    def test_the_patch_adds_no_copy_of_the_string_cron_run_scope_counts(self):
        # apply_cron_run_scope.py asserts exactly 7 occurrences of this literal
        # in tools/kanban_tools.py. An eighth from here would fail its build.
        counted = '"task_id is required (or set HERMES_KANBAN_TASK in the env)"'
        from apply_kanban_comment_status import PATCHED, TRAILER

        self.assertNotIn(counted, PATCHED + TRAILER)

    def test_the_patch_adds_no_copy_of_the_cron_run_scope_import_anchor(self):
        # apply_cron_run_scope.py requires exactly 1 occurrence of it.
        from apply_cron_run_scope import KANBAN_IMPORT_ANCHOR
        from apply_kanban_comment_status import PATCHED, TRAILER

        self.assertNotIn(KANBAN_IMPORT_ANCHOR, PATCHED + TRAILER)


if __name__ == "__main__":
    unittest.main()
