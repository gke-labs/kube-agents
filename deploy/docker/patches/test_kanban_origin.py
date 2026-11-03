"""Unit tests for a kanban card's origin stamp (kanban_origin.py, apply_kanban_origin.py).

The runtime is exercised through the applier: a synthetic ``hermes_cli``
package shaped like upstream's insert path (``create_task`` and
``_insert_decomposed_child`` calling ``inherit_creator_origin`` inside the
insert's transaction, the CLI's ``_cmd_create`` dispatched through
``_HANDLERS``) is patched by ``apply_kanban_origin.apply`` and imported, so the
trailers' rebinding is what the tests drive. ``verify_kanban_origin.py`` runs
the same checks against the real Hermes tree at build time.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_kanban_origin.py'
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apply_kanban_origin as applier
import kanban_origin

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCRIPTS = REPO / "agents" / "platform" / "scripts"
SESSION_KV_SERVER = SCRIPTS / "session_kv_server.py"
DOCKERFILE = HERE.parent / "Dockerfile"

CHAT_SESSION = "20261103_101500_ab12cd34"

# ---------------------------------------------------------------------------
# A synthetic hermes_cli, shaped like upstream's insert path
# ---------------------------------------------------------------------------

DB_SOURCE = textwrap.dedent('''
    import contextlib
    import sqlite3
    import time
    import uuid

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT,"
        " status TEXT, created_by TEXT, created_at INTEGER, session_id TEXT, idempotency_key TEXT)"
    )


    def connect(path):
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(SCHEMA)
        return conn


    @contextlib.contextmanager
    def write_txn(conn, allow_nested=False):
        if conn.in_transaction:
            yield
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


    def create_task(conn, *, title, assignee=None, created_by=None, idempotency_key=None,
                    creator_task_id=None, session_id=None, triage=False):
        from hermes_cli.kanban_db_graph import inherit_creator_origin

        if idempotency_key:
            row = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if row:
                return row["id"]
        now = int(time.time())
        task_id = "t_" + uuid.uuid4().hex[:8]
        with write_txn(conn, allow_nested=True):
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_by, created_at,"
                " session_id, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, title, assignee, "triage" if triage else "ready", created_by, now,
                 session_id, idempotency_key),
            )
            inherit_creator_origin(conn, task_id, creator_task_id, created_at=now)
        return task_id


    def get_task(conn, task_id):
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
''')

GRAPH_SOURCE = textwrap.dedent('''
    import time


    def inherit_creator_origin(conn, task_id, creator_task_id, *, created_at):
        """Copy durable origin inside creation's transaction, never adding dependencies."""
        if not creator_task_id:
            return
        conn.execute(
            "UPDATE tasks SET session_id = COALESCE(session_id, "
            "(SELECT session_id FROM tasks WHERE id = ?)) WHERE id = ?",
            (creator_task_id, task_id),
        )


    def decompose_triage_task(conn, task_id, *, children, author=None):
        from hermes_cli.kanban_db import write_txn

        now = int(time.time())
        with write_txn(conn):
            return [
                _insert_decomposed_child(conn, task_id, child, author, now) for child in children
            ]


    def _insert_decomposed_child(conn, root_id, child, author, now):
        new_id = root_id + "-" + child["title"]
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, created_by) VALUES (?, ?, 'todo', ?, ?)",
            (new_id, child["title"], now, author or "decomposer"),
        )
        inherit_creator_origin(conn, new_id, root_id, created_at=now)
        return new_id
''')

CLI_SOURCE = textwrap.dedent('''
    import argparse
    import contextlib
    import io
    import json
    import os
    import shlex

    from hermes_cli import kanban_db as kb

    BOARD_ENV = "KANBAN_TEST_BOARD"


    def build_parser():
        parser = argparse.ArgumentParser(prog="/kanban", exit_on_error=False)
        sub = parser.add_subparsers(dest="action")
        create = sub.add_parser("create", exit_on_error=False)
        create.add_argument("title")
        create.add_argument("--body")
        create.add_argument("--assignee")
        create.add_argument("--idempotency-key")
        create.add_argument("--created-by", default="user")
        create.add_argument("--json", action="store_true")
        return parser


    def _cmd_create(args):
        with contextlib.closing(kb.connect(os.environ[BOARD_ENV])) as conn:
            task_id = kb.create_task(
                conn, title=args.title, assignee=args.assignee, created_by=args.created_by,
                idempotency_key=args.idempotency_key,
                creator_task_id=os.environ.get("HERMES_KANBAN_TASK"),
            )
            task = kb.get_task(conn, task_id)
        print(json.dumps(task) if args.json else f"Created {task_id}")
        return 0


    _HANDLERS = {
        "create": _cmd_create,
    }


    def kanban_command(args):
        return _HANDLERS.get(args.action)(args)


    def run_slash(rest):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                args = build_parser().parse_args(shlex.split(rest))
        except (SystemExit, argparse.ArgumentError) as exc:
            return f"usage error: {exc} {err.getvalue()}"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            kanban_command(args)
        return (out.getvalue() + err.getvalue()).strip()
''')

PRISTINE = {
    applier.DB_RELATIVE: DB_SOURCE,
    applier.GRAPH_RELATIVE: GRAPH_SOURCE,
    applier.CLI_RELATIVE: CLI_SOURCE,
}


def _write_tree(root: Path, bodies: dict | None = None) -> dict:
    targets = {}
    for relative, source in PRISTINE.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        body = (bodies or {}).get(relative)
        target.write_text(source if body is None else body)
        targets[relative] = target
    (root / "hermes_cli" / "__init__.py").write_text("")
    return targets


def _purge_hermes_modules() -> None:
    for name in [n for n in sys.modules if n == "hermes_cli" or n.startswith("hermes_cli.")]:
        del sys.modules[name]


class Hermes(unittest.TestCase):
    """A patched synthetic Hermes, imported fresh for each test."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        _write_tree(self.root)
        shutil.copy(HERE / "kanban_origin.py", self.root / "hermes_cli" / "kanban_origin.py")
        applier.apply(self.root)
        self.board = self.root / "kanban.db"
        self.home = self.root / "home"
        self.home.mkdir()
        env = {
            "KANBAN_TEST_BOARD": str(self.board),
            kanban_origin.HERMES_HOME_ENV: str(self.home),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (kanban_origin.CARD_ORIGIN_ENV, "HERMES_KANBAN_TASK", "HERMES_SESSION_ID"):
            os.environ.pop(name, None)
        _purge_hermes_modules()
        sys.path.insert(0, str(self.root))
        self.addCleanup(_purge_hermes_modules)
        self.addCleanup(sys.path.remove, str(self.root))
        from hermes_cli import kanban, kanban_db, kanban_db_graph, kanban_origin as ko

        self.kanban, self.kb, self.graph, self.ko = kanban, kanban_db, kanban_db_graph, ko
        self.records: dict = {}
        self.session = ""
        for name, fake in (
            ("_session_id", lambda: self.session),
            ("_fetch_session_metadata", lambda sid: self.records.get(sid)),
            ("_cron_origin", lambda: None),
        ):
            patcher = mock.patch.object(ko, name, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers --------------------------------------------------------------

    def conn(self):
        return closing(self.kb.connect(str(self.board)))

    def in_chat_turn(self, platform="slack"):
        self.session = CHAT_SESSION
        self.records[CHAT_SESSION] = {"origin": "chat", "origin_platform": platform}

    def create(self, **kwargs):
        kwargs.setdefault("title", "a card")
        kwargs.setdefault("assignee", "platform")
        with self.conn() as conn:
            return self.kb.create_task(conn, **kwargs)

    def slash(self, rest):
        out = self.kanban.run_slash(rest)
        start, end = out.find("{"), out.rfind("}")
        return json.loads(out[start : end + 1])["id"] if start != -1 else out

    def origin(self, task_id):
        with self.conn() as conn:
            return self.ko.origin_of(conn, task_id)

    def stamped(self, task_id):
        with self.conn() as conn:
            conn.execute(kanban_origin._TABLE_DDL)
            return conn.execute(
                "SELECT COUNT(*) FROM kanban_task_origins WHERE task_id = ?", (task_id,)
            ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# The brief's seven
# ---------------------------------------------------------------------------


class StampAtInsertTest(Hermes):
    def setUp(self):
        super().setUp()
        self.route_results: list = []

    def test_a_fresh_insert_is_stamped_with_the_turns_origin(self):
        self.in_chat_turn("googlechat")
        tool_card = self.create()
        slash_card = self.slash("create --json --assignee platform from-chat")
        self.assertEqual(self.origin(tool_card), ("chat", "googlechat"))
        self.assertEqual(self.origin(slash_card), ("chat", "googlechat"))

    def test_a_worker_child_inherits_the_workers_card(self):
        self.records["k8s-evt-0a1b"] = {"origin": "event"}
        self.session = "k8s-evt-0a1b"
        worker_card = self.create(title="triage")
        self.assertEqual(self.origin(worker_card), ("event", ""))
        # The worker runs later, inside whatever turn context; its card wins.
        self.in_chat_turn()
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": worker_card}):
            # kanban_create passes HERMES_KANBAN_TASK as creator_task_id ...
            tool_child = self.create(creator_task_id=os.environ["HERMES_KANBAN_TASK"])
            # ... and so does `hermes kanban create`.
            cli_child = self.slash("create --json --assignee platform child")
        self.assertEqual(self.origin(tool_child), ("event", ""))
        self.assertEqual(self.origin(cli_child), ("event", ""))

    def test_a_decomposed_child_inherits_its_parent(self):
        self.records["20261103_api"] = {"origin": "api"}
        self.session = "20261103_api"
        root = self.create(title="root", triage=True)
        self.in_chat_turn()
        with self.conn() as conn:
            children = self.graph.decompose_triage_task(
                conn, root, children=[{"title": "a"}, {"title": "b"}]
            )
        for child in children:
            self.assertEqual(self.origin(child), ("api", ""))

    def test_a_dedupe_hit_keeps_the_first_stamp(self):
        gate = _import_gate()
        key = "issue-resolve-example-fleet-42-20261103T10"
        self.in_chat_turn()
        first = self.slash(f"create --json --assignee platform --idempotency-key {key} squat")
        self.assertEqual(self.origin(first), ("chat", "slack"))
        self.session = ""  # the gate's tick has no turn
        with mock.patch.object(self.ko, "put_card_origin", side_effect=self._route) as put:
            got = gate.file_card(gate.Card(title="t", body="b", idempotency_key=key, origin="issue"))
        self.assertEqual(got, first)
        self.assertEqual(self.origin(first), ("chat", "slack"))
        self.assertEqual(self.route_results, [False] * put.call_count, "the route stamps nothing over a stamp")

    def test_no_create_argument_can_set_the_origin(self):
        out = self.kanban.run_slash("create --json --origin issue --assignee platform x")
        self.assertIn("usage error", out)
        self.assertIn("--origin", out)
        with self.conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            with self.assertRaises(TypeError):
                self.kb.create_task(conn, title="x", origin="issue")
        # `--created-by` is an argument too: it does not make a card the dashboard's.
        card = self.slash("create --json --created-by dashboard --assignee platform y")
        self.assertFalse(self.stamped(card))

    def test_the_gate_stamps_issue_and_scheduled(self):
        gate = _import_gate()
        now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
        issue = gate._issue_card({"repository": "example/fleet", "issue_number": 42, "title_plain": "Fix"}, now=now)
        pr = SimpleNamespace(number=7, head_ref="kage/fix")
        trigger = SimpleNamespace(
            trigger=SimpleNamespace(ref="c-1", kind="review", summary="please rename"),
            comment=SimpleNamespace(author="ada"),
            pr=pr,
        )
        follow_up = gate._pr_card(pr, [trigger], "example/fleet", now=now)
        self.assertEqual((issue.origin, follow_up.origin), ("issue", "scheduled"))
        with mock.patch.object(self.ko, "put_card_origin", side_effect=self._route):
            issue_id = gate.file_card(issue)
            pr_id = gate.file_card(follow_up)
        self.assertEqual(self.origin(issue_id), ("issue", ""))
        self.assertEqual(self.origin(pr_id), ("scheduled", ""))
        self.assertNotIn(kanban_origin.CARD_ORIGIN_ENV, os.environ, "the override does not outlive the call")

    def test_the_console_convention_resolves_portal_and_web_console_ids(self):
        ko = self.ko
        for session, want in (
            ("web-console-0123456789abcdef0123456789abcdef", ("console", "")),
            ("portal_eval_0a1b2c", ("console", "")),
            ("20261103_101500_ab12cd34", ("other", "")),
            ("", ("other", "")),
        ):
            with self.subTest(session=session):
                self.session = session
                self.assertEqual(ko.turn_origin(), want)
        self.session = "web-console-abc"
        self.records["web-console-abc"] = {"origin": "api"}
        self.assertEqual(ko.turn_origin(), ("api", ""), "a record beats the convention")
        self.session = "portal_x"
        self.assertEqual(self.origin(self.create()), ("console", ""))

    # -- the in-process stand-in for PUT /v1/board/tasks/{id}/origin -------------

    def _route(self, task_id, origin, platform=""):
        with self.conn() as conn:
            result = self.ko.stamp(conn, task_id, origin, platform)
        self.route_results.append(result)
        return result


# ---------------------------------------------------------------------------
# The override, read on the CLI create path only
# ---------------------------------------------------------------------------


class OverrideTest(Hermes):
    def setUp(self):
        super().setUp()
        os.environ[kanban_origin.CARD_ORIGIN_ENV] = "issue"

    def test_the_cli_create_path_reads_the_override(self):
        self.assertEqual(self.origin(self.slash("create --json --assignee platform gate")), ("issue", ""))

    def test_no_other_create_path_reads_it(self):
        self.assertFalse(self.stamped(self.create()), "the tool path ignores the variable")
        self.assertFalse(self.stamped(self.create(created_by="platform")))
        self.assertIsNone(self.ko.cli_override())

    def test_the_turn_beats_the_override_so_a_chat_user_cannot_borrow_it(self):
        self.in_chat_turn()
        self.assertEqual(self.origin(self.slash("create --json --assignee platform mine")), ("chat", "slack"))

    def test_a_dotenv_naming_the_override_refuses_it(self):
        for line in ("KUBE_AGENTS_CARD_ORIGIN=issue", "export KUBE_AGENTS_CARD_ORIGIN = drift"):
            with self.subTest(line=line):
                (self.home / ".env").write_text(f"OTHER=1\n{line}\n")
                self.assertFalse(self.stamped(self.slash("create --json --assignee platform x")))
        (self.home / ".env").write_text("OTHER=1\n# KUBE_AGENTS_CARD_ORIGIN is the writers'\n")
        self.assertEqual(self.origin(self.slash("create --json --assignee platform y")), ("issue", ""))

    def test_a_value_outside_the_vocabulary_is_ignored(self):
        for value in ("eval", "other", "issue:slack"):
            with self.subTest(value=value):
                os.environ[kanban_origin.CARD_ORIGIN_ENV] = value
                self.assertFalse(self.stamped(self.slash("create --json --assignee platform x")))


# ---------------------------------------------------------------------------
# The rest of the contract
# ---------------------------------------------------------------------------


class ContractTest(Hermes):
    def test_an_unresolved_create_writes_no_stamp_so_a_writer_can(self):
        card = self.create()
        self.assertFalse(self.stamped(card))
        self.assertEqual(self.origin(card), ("other", ""))
        with self.conn() as conn:
            self.assertTrue(self.ko.stamp(conn, card, "drift"))
            self.assertFalse(self.ko.stamp(conn, card, "issue"), "the first stamp wins")
        self.assertEqual(self.origin(card), ("drift", ""))

    def test_the_dashboard_route_files_console(self):
        self.assertEqual(self.origin(self.create(created_by="dashboard")), ("console", ""))

    def test_a_tick_started_run_stamps_its_context_when_hermes_has_it(self):
        with mock.patch.object(self.ko, "_cron_origin", return_value=("audit", "")):
            self.assertEqual(self.origin(self.create()), ("audit", ""))

    def test_the_session_lookup_runs_before_the_insert_transaction(self):
        seen = []

        def lookup(sid):
            with self.conn() as probe:
                seen.append(probe.in_transaction)
                probe.execute("BEGIN IMMEDIATE")  # would fail under the insert's write lock
                probe.execute("ROLLBACK")
            return {"origin": "chat", "origin_platform": "slack"}

        self.session = CHAT_SESSION
        with mock.patch.object(self.ko, "_fetch_session_metadata", side_effect=lookup):
            self.assertEqual(self.origin(self.create()), ("chat", "slack"))
        self.assertEqual(seen, [False])

    def test_a_failing_stamp_never_fails_the_create(self):
        self.in_chat_turn()
        with mock.patch.object(self.ko, "stamp", side_effect=sqlite3.OperationalError("locked")):
            card = self.create()
        with self.conn() as conn:
            self.assertEqual(self.kb.get_task(conn, card)["title"], "a card")
        with mock.patch.object(self.ko, "_session_id", side_effect=RuntimeError("no context")):
            self.assertFalse(self.stamped(self.create()))

    def test_a_rolled_back_insert_leaves_no_stamp(self):
        self.in_chat_turn()
        with self.conn() as conn:
            with self.assertRaises(RuntimeError):
                with self.kb.write_txn(conn):
                    tid = self.kb.create_task(conn, title="nested", assignee="platform")
                    raise RuntimeError("the outer graph builder fails")
            self.assertEqual(self.ko.origin_of(conn, tid), ("other", ""))
            self.assertIsNone(conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone())

    def test_origin_of_reads_a_board_that_predates_the_table(self):
        with self.conn() as conn:
            self.assertEqual(self.ko.origin_of(conn, "t_old"), ("other", ""))

    def test_a_stamp_outside_the_vocabulary_is_refused(self):
        with self.conn() as conn, self.assertRaises(ValueError):
            self.ko.stamp(conn, "t_x", "eval")


class SessionRecordTest(unittest.TestCase):
    """The HTTP read, with the real ``_fetch_session_metadata``."""

    def _answer(self, status=200, body=b"{}"):
        import io
        import urllib.error

        if status != 200:
            return mock.patch.object(
                kanban_origin.urllib.request, "urlopen",
                side_effect=urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(b"")),
            )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        return mock.patch.object(kanban_origin.urllib.request, "urlopen", return_value=response)

    def test_the_record_is_read_with_the_pods_key(self):
        with mock.patch.dict(os.environ, {kanban_origin.SESSION_KV_KEY_ENV: "k"}), self._answer(
            body=b'{"origin": "chat", "origin_platform": "slack", "chat_id": "c"}'
        ) as urlopen:
            self.assertEqual(kanban_origin._record_origin("s/1"), ("chat", "slack"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8699/v1/sessions/s%2F1/metadata")
        self.assertEqual(request.get_header("Authorization"), "Bearer k")

    def test_no_record_and_an_other_record_are_unresolved(self):
        with self._answer(status=404):
            self.assertIsNone(kanban_origin._record_origin("s"))
        with self._answer(body=b'{"origin": "other"}'):
            self.assertIsNone(kanban_origin._record_origin("s"))
        with self._answer(body=b'{"chat_id": "c"}'):
            self.assertIsNone(kanban_origin._record_origin("s"))

    def test_an_absent_cron_scope_is_no_run(self):
        with mock.patch.object(kanban_origin, "_first_module", return_value=None):
            self.assertIsNone(kanban_origin._cron_origin())

    def test_the_re_stamp_never_raises(self):
        with mock.patch.object(kanban_origin.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(kanban_origin.put_card_origin("t_1", "issue"))

    def test_created_just_now(self):
        now = time.time()
        self.assertTrue(kanban_origin.created_just_now(int(now), now=now))
        self.assertFalse(kanban_origin.created_just_now(int(now) - 3600, now=now))
        self.assertFalse(kanban_origin.created_just_now(None, now=now))


class PinTest(unittest.TestCase):
    def test_the_vocabulary_and_schema_match_the_session_kv_server(self):
        tree = ast.parse(SESSION_KV_SERVER.read_text())
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in ("ORIGINS", "KANBAN_TASK_ORIGINS_SCHEMA", "DRIFT_BACKLOG_CARD_ORIGIN_ENV",
                                       "DRIFT_BACKLOG_CARD_CREATED_JUST_NOW_SECONDS")
        }
        self.assertEqual(values["ORIGINS"], kanban_origin.ORIGINS)
        squash = lambda sql: re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()
        self.assertEqual(squash(values["KANBAN_TASK_ORIGINS_SCHEMA"]), squash(kanban_origin._TABLE_DDL))
        self.assertEqual(values["DRIFT_BACKLOG_CARD_ORIGIN_ENV"], kanban_origin.CARD_ORIGIN_ENV)
        self.assertEqual(
            values["DRIFT_BACKLOG_CARD_CREATED_JUST_NOW_SECONDS"], kanban_origin.CREATED_JUST_NOW_SECONDS
        )

    def test_the_dockerfile_installs_applies_and_verifies_the_patch(self):
        text = DOCKERFILE.read_text()
        self.assertIn("deploy/docker/patches/kanban_origin.py", text)
        self.assertIn(
            "install -m 0644 /tmp/latency-patches/kanban_origin.py /opt/hermes/hermes_cli/kanban_origin.py", text
        )
        self.assertIn("/tmp/latency-patches/apply_kanban_origin.py /opt/hermes", text)
        self.assertIn("/tmp/latency-patches/verify_kanban_origin.py", text)
        self.assertLess(
            text.index("/tmp/latency-patches/apply_kanban_priority.py /opt/hermes"),
            text.index("/tmp/latency-patches/apply_kanban_origin.py /opt/hermes"),
        )


class ApplierTest(unittest.TestCase):
    def _tree(self, bodies=None):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        return root, _write_tree(root, bodies)

    def test_the_fixtures_carry_the_insert_sites(self):
        for relative, function, call in applier.INSERT_SITES:
            self.assertEqual(PRISTINE[relative].count(call), 1, function)

    def test_every_locator_is_load_bearing(self):
        for relative, anchor in (
            (applier.DB_RELATIVE, applier.INSERT_SITES[0][2]),
            (applier.GRAPH_RELATIVE, applier.INSERT_SITES[1][2]),
            (applier.GRAPH_RELATIVE, "def inherit_creator_origin("),
            (applier.CLI_RELATIVE, "def _cmd_create("),
            (applier.CLI_RELATIVE, applier.HANDLERS_ENTRY),
        ):
            with self.subTest(anchor=anchor):
                root, targets = self._tree({relative: PRISTINE[relative].replace(anchor, "pass  # ", 1)})
                with self.assertRaises(SystemExit):
                    applier.apply(root)
                for rel, target in targets.items():
                    self.assertEqual(target.read_text(), (
                        PRISTINE[rel].replace(anchor, "pass  # ", 1) if rel == relative else PRISTINE[rel]
                    ), "nothing written")

    def test_a_second_run_is_refused(self):
        root, _ = self._tree()
        applier.apply(root)
        with self.assertRaises(SystemExit):
            applier.apply(root)


def _import_gate():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import github_scan_gate

    return github_scan_gate


if __name__ == "__main__":
    unittest.main()
