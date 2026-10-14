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


class PrOpenedTest(unittest.TestCase):
    def run_hook(self, output, kanban, task=TASK):
        env = {plugin.TASK_ENV: task} if task else {}
        with mock.patch.dict(sys.modules, kanban.modules()), mock.patch.dict(os.environ, env, clear=True):
            return plugin.on_terminal_output(command="python3 vcs.py proposal create", output=output, returncode=0)

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

    def test_outside_a_kanban_worker_the_line_goes_and_nothing_is_recorded(self):
        kanban = _Kanban()
        self.assertEqual(self.run_hook(_vcs_record() + "done\n", kanban, task=""), "done\n")
        self.assertEqual(kanban.events, [])

    def test_a_record_that_cannot_be_written_is_logged_and_the_line_still_goes(self):
        kanban = _Kanban(fail=RuntimeError("database is locked"))
        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertEqual(self.run_hook(_vcs_record(), kanban), "")

    def test_the_title_is_bounded(self):
        kanban = _Kanban()
        self.run_hook(_vcs_record(title="x" * (plugin.TITLE_MAX * 2)), kanban)
        self.assertEqual(len(kanban.events[0][2]["title"]), plugin.TITLE_MAX)

    def test_registers_the_terminal_output_hook(self):
        registered = []
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: registered.append((name, fn))))
        self.assertEqual(registered, [("transform_terminal_output", plugin.on_terminal_output)])


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
                with mock.patch.dict(os.environ, {plugin.TASK_ENV: task_id}):
                    plugin.on_terminal_output(output=_vcs_record(), returncode=0)
                with connect_closing() as conn:
                    rows = conn.execute(
                        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = ?",
                        (task_id, plugin.PR_OPENED_KIND),
                    ).fetchall()
        self.assertEqual([(row[0], json.loads(row[1])["url"]) for row in rows], [("pr_opened", URL)])


if __name__ == "__main__":
    unittest.main()
