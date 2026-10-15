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

    def __init__(self, viewer="kube-agents-bot", author="kube-agents-bot", fail=None):
        self.viewer, self.author, self.fail = viewer, author, fail
        self.asked = []

    def module(self):
        forge = types.ModuleType("forge")
        test = self

        class BrokerProvider:
            def viewer_login(self, repo):
                test.asked.append(("identity", repo))
                return test.viewer

        def call(verb, payload, repo, *, retry_transient=False):
            test.asked.append((verb, repo, payload["number"]))
            if test.fail:
                raise test.fail
            base = repo + ("/-/merge_requests/" if "gitlab" in repo else "/pull/")
            return {"proposal": {"number": payload["number"], "url": f"{base}{payload['number']}", "author": test.author}}

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
        self.assertEqual(self.run_hook(_vcs_record(url=mr, number=12, title="Bump"), kanban), "")
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": mr, "number": 12, "title": "Bump"}, 7)])

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

    def test_the_title_is_bounded(self):
        kanban = _Kanban()
        self.run_hook(_vcs_record(title="x" * (plugin.TITLE_MAX * 2)), kanban)
        self.assertEqual(len(kanban.events[0][2]["title"]), plugin.TITLE_MAX)

    def test_registers_the_terminal_output_hook(self):
        registered = []
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: registered.append((name, fn))))
        self.assertEqual(registered, [("transform_terminal_output", plugin.on_terminal_output)])


class RealForgeTest(unittest.TestCase):
    """:func:`plugin.ours` through the real ``forge.py``, with only the broker under ``vcs_client`` stubbed."""

    def check(self, author):
        import forge
        import sandbox_exec

        asked = []

        def broker(verb, payload):
            asked.append((verb, payload.get("repository"), payload.get("number")))
            if verb == "identity":
                return {"identity": {"login": "kube-agents-bot[bot]"}}
            return {"proposal": {"number": payload["number"], "url": URL, "author": author}}

        with mock.patch.object(vcs_client, "_call", broker), \
                mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
                mock.patch.dict(sys.modules, {"forge": forge}):
            verdict = plugin.ours({"url": URL, "number": 99, "title": "t"})
        repository = "https://github.com/acme/fleet-config"
        self.assertEqual(asked, [("identity", repository, None), ("proposal-view", repository, 99)])
        return verdict

    def test_the_agents_own_pull_request_is_ours_and_a_strangers_is_not(self):
        self.assertTrue(self.check("kube-agents-bot"))
        self.assertFalse(self.check("mallory"))


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


if __name__ == "__main__":
    unittest.main()
