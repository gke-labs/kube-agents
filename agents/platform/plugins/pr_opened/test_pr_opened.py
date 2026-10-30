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

    def test_registers_the_terminal_output_hook(self):
        registered = []
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: registered.append((name, fn))))
        self.assertEqual(registered, [("transform_terminal_output", plugin.on_terminal_output)])


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
