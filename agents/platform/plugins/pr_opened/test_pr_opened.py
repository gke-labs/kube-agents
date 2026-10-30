"""Tests for the pr_opened plugin. No Hermes install required; the last test uses one when importable.

Run: python3 -m unittest agents/platform/plugins/pr_opened/test_pr_opened.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import vcs_client

_spec = importlib.util.spec_from_file_location("pr_opened_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

URL = "https://github.com/acme/fleet-config/pull/99"
TASK = "t_9f2a"


def _vcs_record(url=URL, number=99, title="Raise the checkout memory limit"):
    """The stderr line ``vcs_client.call`` writes for a proposal it opened."""
    stderr = io.StringIO()
    answer = {"proposal": {"url": url, "number": number, "title": title, "state": "open"}}
    with mock.patch.object(vcs_client, "_call", lambda verb, payload: answer), mock.patch("sys.stderr", stderr):
        vcs_client.call("proposal-create", {})
    return stderr.getvalue()


class _Kanban:
    """Stands in for ``hermes_cli.kanban_db`` and ``kanban_db_connect``, recording each event."""

    def __init__(self, fail=None):
        self.events = []
        self.fail = fail

    def modules(self):
        kb = types.ModuleType("hermes_cli.kanban_db")
        connect = types.ModuleType("hermes_cli.kanban_db_connect")

        @contextlib.contextmanager
        def connect_closing(db_path=None, *, board=None):
            if self.fail:
                raise self.fail
            yield "conn"

        @contextlib.contextmanager
        def write_txn(conn):
            yield

        kb.write_txn = write_txn
        kb._current_run_id = lambda conn, task_id: 7
        kb._append_event = lambda conn, task_id, kind, payload, run_id=None: self.events.append(
            (task_id, kind, payload, run_id)
        )
        connect.connect_closing = connect_closing
        package = types.ModuleType("hermes_cli")
        package.kanban_db, package.kanban_db_connect = kb, connect
        return {"hermes_cli": package, "hermes_cli.kanban_db": kb, "hermes_cli.kanban_db_connect": connect}


class _Forge:
    """Stands in for the agent pod's ``forge`` module: the broker's identity and proposal reads."""

    def __init__(self, viewer="kube-agents-bot", author="kube-agents-bot", fail=None,
                 title="Raise the checkout memory limit", state="open", age_seconds=5):
        self.viewer, self.author, self.fail = viewer, author, fail
        self.title, self.state = title, state
        self.created = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.asked = []
        self.providers = 0

    def module(self):
        forge = types.ModuleType("forge")
        test = self

        class BrokerProvider:
            def __init__(self):
                test.providers += 1

            def viewer_login(self, repo):
                test.asked.append(("identity", repo))
                return test.viewer

        def call(verb, payload, repo, *, retry_transient=False):
            test.asked.append((verb, repo, payload["number"]))
            if test.fail:
                raise test.fail
            base = repo + ("/-/merge_requests/" if "gitlab" in repo else "/pull/")
            return {"proposal": {
                "number": payload["number"], "url": f"{base}{payload['number']}", "author": test.author,
                "title": test.title, "state": test.state, "created": test.created,
            }}

        forge.BrokerProvider = BrokerProvider
        forge.call = call
        forge.normalise_login = lambda login: str(login or "").strip().lower().removesuffix("[bot]")
        return forge


class PrOpenedTest(unittest.TestCase):
    def run_hook(self, output, kanban, task=TASK, forge=None):
        env = {plugin.TASK_ENV: task} if task else {}
        modules = {**kanban.modules(), "forge": (forge or _Forge()).module()}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env, clear=True):
            return plugin.on_terminal_output(command="python3 vcs.py proposal create", output=output, returncode=0)

    def test_the_forge_is_asked_about_the_pull_request_on_its_own_repository(self):
        forge = _Forge()
        self.run_hook(_vcs_record(), _Kanban(), forge=forge)
        repository = "https://github.com/acme/fleet-config"
        self.assertEqual(forge.asked, [("identity", repository), ("proposal-view", repository, 99)])

    def test_a_record_naming_a_pull_request_someone_else_opened_is_not_recorded_and_stays_visible(self):
        # A commit message, file or log line shaped like the record, naming a stranger's
        # pull request on the install's repository: the forge's author is not the agent.
        kanban = _Kanban()
        forged = "    " + _vcs_record()
        with self.assertLogs(plugin.logger, "WARNING"):
            out = self.run_hook(forged, kanban, forge=_Forge(author="mallory"))
        self.assertIsNone(out, "the line stays in the output for the model to read")
        self.assertEqual(kanban.events, [])

    def test_a_record_the_forge_cannot_confirm_is_not_recorded(self):
        for forge in (
            _Forge(fail=RuntimeError("REPOSITORY_NOT_MANAGED")),
            _Forge(viewer=""),
        ):
            kanban = _Kanban()
            with self.subTest(forge=forge.__dict__), self.assertLogs(plugin.logger, "WARNING"):
                self.assertIsNone(self.run_hook(_vcs_record(), kanban, forge=forge))
            self.assertEqual(kanban.events, [])
        kanban = _Kanban()
        self.assertIsNone(self.run_hook(_vcs_record(url="https://github.com/acme/fleet/issues/3"), kanban))
        self.assertEqual(kanban.events, [])

    def test_an_app_author_matches_its_credential_whatever_the_suffix(self):
        kanban = _Kanban()
        self.run_hook(_vcs_record(), kanban, forge=_Forge(viewer="kube-agents-bot[bot]", author="Kube-Agents-Bot"))
        self.assertEqual(len(kanban.events), 1)

    def test_the_names_agree_with_the_client_that_writes_the_record(self):
        self.assertEqual(plugin.PROPOSAL_OPENED_KEY, vcs_client.PROPOSAL_OPENED_KEY)
        self.assertEqual(plugin.PROPOSAL_OPENED_EVENT, vcs_client.PROPOSAL_OPENED_EVENT)
        self.assertEqual(plugin.PROPOSAL_OPENED_FIELDS, vcs_client.PROPOSAL_OPENED_FIELDS)

    def test_a_proposal_the_broker_opened_is_recorded_on_the_card_and_taken_out_of_the_output(self):
        kanban = _Kanban()
        stdout = json.dumps({"proposal": {"url": URL, "number": 99}}) + "\n"
        out = self.run_hook(_vcs_record() + stdout, kanban)
        self.assertEqual(out, stdout)
        self.assertEqual(
            kanban.events,
            [(TASK, "pr_opened", {"url": URL, "number": 99, "title": "Raise the checkout memory limit"}, 7)],
        )

    def test_a_gitlab_merge_request_is_recorded_the_same_way(self):
        kanban = _Kanban()
        mr = "https://gitlab.com/acme/infra/-/merge_requests/12"
        self.assertEqual(self.run_hook(_vcs_record(url=mr, number=12, title="Bump"), kanban, forge=_Forge(title="Bump")), "")
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": mr, "number": 12, "title": "Bump"}, 7)])

    def test_the_forges_fields_are_recorded_not_the_lines(self):
        kanban = _Kanban()
        self.run_hook(_vcs_record(number=7, title="planted words"), kanban, forge=_Forge(title="Raise replicas"))
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": URL, "number": 99, "title": "Raise replicas"}, 7)])

    def test_a_pull_request_the_agent_opened_earlier_or_that_is_closed_is_not_recorded(self):
        # A copy of an old record (a log, an issue body) names a PR the agent did open, long ago.
        for forge in (_Forge(age_seconds=plugin.CREATED_WITHIN_SECONDS + 60), _Forge(state="merged")):
            kanban = _Kanban()
            with self.subTest(state=forge.state, created=forge.created), self.assertLogs(plugin.logger, "WARNING"):
                self.assertIsNone(self.run_hook(_vcs_record(), kanban, forge=forge))
            self.assertEqual(kanban.events, [])

    def test_repeated_records_are_checked_once_per_url_and_at_most_the_cap(self):
        kanban, forge = _Kanban(), _Forge(author="mallory")
        urls = [f"https://github.com/acme/fleet-config/pull/{n}" for n in range(plugin.RECORDS_CHECKED_MAX + 3)]
        flood = _vcs_record() * 50 + "".join(_vcs_record(url=url) for url in urls)
        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertIsNone(self.run_hook(flood, kanban, forge=forge))
        views = [entry for entry in forge.asked if entry[0] == "proposal-view"]
        self.assertEqual(len(views), plugin.RECORDS_CHECKED_MAX)
        self.assertEqual(forge.providers, 1, "one provider, so the credential's login is read once per repository")
        self.assertEqual(kanban.events, [])

    def test_output_with_no_record_is_left_alone(self):
        kanban = _Kanban()
        cited = f"See {URL}, opened by bob\n" + json.dumps({"proposal": {"url": URL}}) + "\n"
        self.assertIsNone(self.run_hook(cited, kanban))
        self.assertIsNone(self.run_hook(f'{{"kube-agents.event": "something-else", "url": "{URL}"}}\n', kanban))
        self.assertIsNone(self.run_hook("kube-agents.event in prose is not a record\n", kanban))
        self.assertEqual(kanban.events, [])

    def test_outside_a_kanban_worker_nothing_is_checked_recorded_or_taken_out(self):
        kanban, forge = _Kanban(), _Forge()
        self.assertIsNone(self.run_hook(_vcs_record() + "done\n", kanban, task="", forge=forge))
        self.assertEqual((kanban.events, forge.asked), ([], []))

    def test_a_record_that_cannot_be_written_is_logged_and_the_line_stays(self):
        kanban = _Kanban(fail=RuntimeError("database is locked"))
        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertIsNone(self.run_hook(_vcs_record(), kanban))

    def test_a_check_that_fails_ends_the_asking_for_that_output(self):
        # A broker or sandbox hop that is down costs one timeout, not one per url.
        kanban, forge = _Kanban(), _Forge(fail=RuntimeError("SANDBOX_UNREACHABLE"))
        urls = [f"https://github.com/acme/fleet-config/pull/{n}" for n in range(3)]
        output = "".join(_vcs_record(url=url) for url in urls)
        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertIsNone(self.run_hook(output, kanban, forge=forge))
        self.assertEqual(len([entry for entry in forge.asked if entry[0] == "proposal-view"]), 1)
        self.assertEqual(kanban.events, [])

    def test_the_title_is_bounded(self):
        kanban = _Kanban()
        self.run_hook(_vcs_record(), kanban, forge=_Forge(title="x" * (plugin.TITLE_MAX * 2)))
        self.assertEqual(len(kanban.events[0][2]["title"]), plugin.TITLE_MAX)

    def test_registers_the_terminal_output_and_pre_tool_call_hooks(self):
        registered = []
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: registered.append((name, fn))))
        self.assertEqual(registered, [
            ("transform_terminal_output", plugin.on_terminal_output),
            ("pre_tool_call", plugin.on_pre_tool_call),
        ])


PREPARE = (
    'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \\\n'
    '  --repo "acme/fleet-config" --branch "platform-agent/add-checkout-gateway-pdb"'
)
SUBMIT = (
    'BODY=$(mktemp -p /opt/data/scratch pr_body.XXXXXX.md)\ncat > "$BODY" <<\'EOF\'\nAdds a PDB\nEOF\n'
    'python3 /opt/data/profiles/platform/skills/submit-suggestion/scripts/submit_suggestion.py submit '
    '--branch platform-agent/add-checkout-gateway-pdb --title "Add PDB" --body-file "$BODY"'
)
RUN_ID = 41


class _Board:
    """A sqlite board holding one card, behind a stand-in ``hermes_cli.kanban_db_connect``."""

    def __init__(self, tmp, status="running", current_run_id=RUN_ID, fail=None):
        import sqlite3

        self.path = str(Path(tmp) / "kanban.db")
        self.fail = fail
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER)")
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (TASK, status, current_run_id))
            conn.commit()

    def modules(self):
        import sqlite3

        connect = types.ModuleType("hermes_cli.kanban_db_connect")
        board = self

        @contextlib.contextmanager
        def connect_closing(db_path=None, *, board_slug=None):
            if board.fail:
                raise board.fail
            # As Hermes' connect does: a 120 s writer timeout, set by PRAGMA.
            with contextlib.closing(sqlite3.connect(board.path, timeout=120)) as conn:
                conn.execute("PRAGMA busy_timeout=120000")
                yield conn

        connect.connect_closing = connect_closing
        package = types.ModuleType("hermes_cli")
        package.kanban_db_connect = connect
        return {"hermes_cli": package, "hermes_cli.kanban_db_connect": connect}


class EndedRunGuardTest(unittest.TestCase):
    """submit_suggestion.py is refused once the worker's own card run has ended (#2610)."""

    def call(self, command, *, status="running", current_run_id=RUN_ID, env=None, fail=None, tool="terminal"):
        env = {plugin.TASK_ENV: TASK, plugin.RUN_ID_ENV: str(RUN_ID)} if env is None else env
        with tempfile.TemporaryDirectory() as tmp:
            board = _Board(tmp, status=status, current_run_id=current_run_id, fail=fail)
            with mock.patch.dict(sys.modules, board.modules()), mock.patch.dict(os.environ, env, clear=True):
                return plugin.on_pre_tool_call(tool_name=tool, args={"command": command}, task_id="x")

    def test_a_locked_board_lets_the_call_through_well_inside_hermes_hook_timeout(self):
        # Hermes blocks a pre_tool_call hook that runs past 30 s, so a read that waited out the
        # board's 120 s writer timeout would refuse a live card's submit.
        import sqlite3
        import time

        env = {plugin.TASK_ENV: TASK, plugin.RUN_ID_ENV: str(RUN_ID)}
        with tempfile.TemporaryDirectory() as tmp:
            board = _Board(tmp, status="done")
            with contextlib.closing(sqlite3.connect(board.path, isolation_level=None)) as holder:
                holder.execute("BEGIN EXCLUSIVE")
                with mock.patch.dict(sys.modules, board.modules()), mock.patch.dict(os.environ, env, clear=True):
                    start = time.monotonic()
                    with self.assertLogs(plugin.logger, "WARNING"):
                        verdict = plugin.on_pre_tool_call(tool_name="terminal", args={"command": PREPARE}, task_id="x")
                    took = time.monotonic() - start
                holder.execute("ROLLBACK")
        self.assertIsNone(verdict)
        self.assertLess(took, 10)

    def test_a_worker_whose_card_is_done_is_refused_prepare_and_submit(self):
        # #3622's shape: kanban_complete, then the worker acts on its own [next: ...] line.
        for command in (PREPARE, SUBMIT):
            with self.subTest(command=command[:40]), self.assertLogs(plugin.logger, "WARNING"):
                verdict = self.call(command, status="done")
            self.assertEqual(verdict["action"], "block")
            self.assertIn(f"card {TASK} is done", verdict["message"])
            self.assertIn("[next: ...] line", verdict["message"])

    def test_a_run_another_run_took_over_or_that_was_blocked_is_refused(self):
        for status, current in (("running", RUN_ID + 1), ("blocked", None), ("todo", None)):
            with self.subTest(status=status, current=current), self.assertLogs(plugin.logger, "WARNING"):
                self.assertEqual(self.call(PREPARE, status=status, current_run_id=current)["action"], "block")

    def test_a_live_run_is_let_through(self):
        # The card asked for the pull request and the run is still open: the write path stays.
        self.assertIsNone(self.call(PREPARE))
        self.assertIsNone(self.call(SUBMIT))

    def test_a_process_that_is_not_a_dispatcher_worker_is_let_through(self):
        # The gateway's own sessions and cron runs carry no card: the main-session path is unchanged.
        for env in ({}, {plugin.TASK_ENV: TASK}, {plugin.TASK_ENV: TASK, plugin.RUN_ID_ENV: "not-a-number"}):
            with self.subTest(env=env):
                self.assertIsNone(self.call(PREPARE, status="done", env=env))

    def test_an_unreadable_board_or_a_missing_card_is_let_through(self):
        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertIsNone(self.call(PREPARE, status="done", fail=RuntimeError("database is locked")))
        env = {plugin.TASK_ENV: "t_other", plugin.RUN_ID_ENV: str(RUN_ID)}
        self.assertIsNone(self.call(PREPARE, status="done", env=env))

    def test_the_bare_old_shape_is_refused_too(self):
        # The script reads a first argument that isn't a command as `submit`.
        for command in (
            'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py --branch platform-agent/pdb --title "Add PDB" --body-file "$BODY"',
            "cd /tmp/wc && python3 submit_suggestion.py --branch x --title y",
        ):
            with self.subTest(command=command[:50]), self.assertLogs(plugin.logger, "WARNING"):
                self.assertEqual(self.call(command, status="done")["action"], "block")

    def test_other_commands_and_tools_are_never_looked_at(self):
        for command in (
            "kubectl get pdb -A -o json",
            'python3 "$HERMES_HOME"/scripts/gitops_preflight.py --list',
            "python3 submit_suggestion.py --help",
            "python3 submit_suggestion.py -h",
            "python3 submit_suggestion.py list --handle x",
            "cat agents/platform/skills/submit-suggestion/scripts/submit_suggestion.py",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.call(command, status="done"))
        self.assertIsNone(self.call(PREPARE, status="done", tool="read_file"))


class RealForgeTest(unittest.TestCase):
    """:meth:`plugin.Checker.ours` through the real ``forge.py``, with only the broker under ``vcs_client`` stubbed."""

    def check(self, author):
        import forge
        import sandbox_exec

        asked = []
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def broker(verb, payload):
            asked.append((verb, payload.get("repository"), payload.get("number")))
            if verb == "identity":
                return {"identity": {"login": "kube-agents-bot[bot]"}}
            return {"proposal": {
                "number": payload["number"], "url": URL, "author": author, "title": "Raise the limit",
                "state": "open", "created": created,
            }}

        with mock.patch.object(vcs_client, "_call", broker), \
                mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
                mock.patch.dict(sys.modules, {"forge": forge}):
            verdict = plugin.Checker().ours({"url": URL, "number": 99, "title": "t"})
        repository = "https://github.com/acme/fleet-config"
        self.assertEqual(asked, [("identity", repository, None), ("proposal-view", repository, 99)])
        return verdict

    def test_the_agents_own_pull_request_is_ours_and_a_strangers_is_not(self):
        self.assertEqual(self.check("kube-agents-bot"), {"url": URL, "number": 99, "title": "Raise the limit"})
        self.assertIsNone(self.check("mallory"))


def _hermes_importable():
    try:
        return importlib.util.find_spec("hermes_cli.kanban_db") is not None
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(_hermes_importable(), "hermes_cli is not importable here")
class RealBoardTest(unittest.TestCase):
    """Against Hermes's own kanban DB: the event lands on the card the notifier claims from."""

    def test_the_event_lands_in_the_cards_task_events(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": db, "HERMES_HOME": tmp}):
                kb.init_db()
                with connect_closing() as conn:
                    task_id = kb.create_task(conn, title="raise the limit")
                with mock.patch.dict(os.environ, {plugin.TASK_ENV: task_id}), \
                        mock.patch.dict(sys.modules, {"forge": _Forge().module()}):
                    plugin.on_terminal_output(output=_vcs_record(), returncode=0)
                with connect_closing() as conn:
                    rows = conn.execute(
                        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = ?",
                        (task_id, plugin.PR_OPENED_KIND),
                    ).fetchall()
        self.assertEqual([(row[0], json.loads(row[1])["url"]) for row in rows], [("pr_opened", URL)])

    def test_submit_suggestion_is_refused_after_kanban_complete_on_a_real_card(self):
        # The real board: a live run is let through; once complete_task ends the run, refused.
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect_closing

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_DB": db, "HERMES_HOME": tmp}):
                kb.init_db()
                with connect_closing() as conn:
                    task_id = kb.create_task(conn, title="propose a PDB", assignee="platform")
                    claimed = kb.claim_task(conn, task_id)
                    run_id = claimed.current_run_id
                worker = {plugin.TASK_ENV: task_id, plugin.RUN_ID_ENV: str(run_id)}
                with mock.patch.dict(os.environ, worker):
                    live = plugin.on_pre_tool_call(tool_name="terminal", args={"command": PREPARE})
                    with connect_closing() as conn:
                        self.assertTrue(kb.complete_task(conn, task_id, result="the manifest", expected_run_id=run_id))
                    with self.assertLogs(plugin.logger, "WARNING"):
                        ended = plugin.on_pre_tool_call(tool_name="terminal", args={"command": PREPARE})
        self.assertIsNone(live)
        self.assertEqual(ended["action"], "block")

    def test_hermes_own_pre_tool_call_seam_blocks_with_the_message(self):
        # Hermes's own dispatch: the block directive becomes the message the tool call returns.
        from hermes_cli import lifecycle  # noqa: F401
        from hermes_cli import plugins as hermes_plugins

        manager = hermes_plugins.get_plugin_manager()
        mock.patch.object(manager, "_discovered", True, create=True).start()
        self.addCleanup(mock.patch.stopall)
        hooks = manager._hooks.setdefault("pre_tool_call", [])
        hooks.append(plugin.on_pre_tool_call)
        self.addCleanup(hooks.remove, plugin.on_pre_tool_call)
        with tempfile.TemporaryDirectory() as tmp:
            board = _Board(tmp, status="done")
            env = {plugin.TASK_ENV: TASK, plugin.RUN_ID_ENV: str(RUN_ID)}
            with mock.patch.dict(sys.modules, board.modules()), mock.patch.dict(os.environ, env), \
                    self.assertLogs(plugin.logger, "WARNING"):
                message, _ = hermes_plugins._dispatch_pre_tool_call_hooks("terminal", {"command": PREPARE})
        self.assertEqual(message, plugin.ended_run_text(TASK, "done"))

    def test_hermes_own_terminal_output_seam_calls_the_hook_and_takes_its_answer(self):
        # The plugin registered through Hermes's plugin manager, and the output transform driven by
        # Hermes's own terminal-result code: the keyword names it passes and what a returned string does.
        # lifecycle is imported before the kanban stand-in replaces the hermes_cli package, as the
        # running gateway has it imported long before any terminal call.
        from hermes_cli import lifecycle  # noqa: F401
        from hermes_cli import plugins as hermes_plugins
        from tools import terminal_tool_result

        manager = hermes_plugins.get_plugin_manager()
        # Discovery is the gateway's job at start; here it would load every bundled plugin, some of
        # which need packages a test host lacks, and the seam's own guard would swallow that failure.
        mock.patch.object(manager, "_discovered", True, create=True).start()
        self.addCleanup(mock.patch.stopall)
        hooks = manager._hooks.setdefault("transform_terminal_output", [])
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: hooks.append(fn)))
        self.addCleanup(hooks.remove, plugin.on_terminal_output)
        kanban = _Kanban()
        stdout = json.dumps({"proposal": {"url": URL, "number": 99}}) + "\n"
        modules = {**kanban.modules(), "forge": _Forge().module()}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {plugin.TASK_ENV: TASK}):
            out = terminal_tool_result._apply_output_transform_hook(
                "python3 vcs.py proposal create --title t", _vcs_record() + stdout, 0, "task-1", "ssh",
            )
        self.assertEqual(out, stdout)
        self.assertEqual([event[1] for event in kanban.events], ["pr_opened"])


if __name__ == "__main__":
    unittest.main()
