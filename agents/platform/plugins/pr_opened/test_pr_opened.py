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
REPO = HERE.parents[3]
sys.path.insert(0, str(SCRIPTS))

import vcs_client

_spec = importlib.util.spec_from_file_location("pr_opened_plugin", HERE / "__init__.py")
plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plugin)

URL = "https://github.com/acme/fleet-config/pull/99"
REPOSITORY = "https://github.com/acme/fleet-config"
TASK = "t_9f2a"
TITLE = "Raise the checkout memory limit"


def _vcs_record(url=URL, number=99, title=TITLE, claim=None):
    """The stderr line ``vcs_client.call`` writes for a proposal it opened, with ``claim`` as its token."""
    stderr = io.StringIO()
    answer = {"proposal": {"url": url, "number": number, "title": title, "state": "open"}}
    if claim is not None:
        answer["claim"] = claim
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


class _Refusal(Exception):
    """A broker refusal as ``forge.ForgeError`` carries it: the code on ``reason``."""

    def __init__(self, reason):
        super().__init__(f"{reason}: {URL}")
        self.reason = reason


class _Broker:
    """Stands in for the agent pod's ``forge`` module in front of the broker's ``proposal-claim``.

    Each token :meth:`mint` hands out is accepted once, for the url it was minted for.
    """

    def __init__(self, title=TITLE, fail=None, refuse=None):
        self.title, self.fail, self.refuse = title, fail, refuse
        self.live = {}
        self.asked = []

    def mint(self, url=URL):
        token = f"tok{len(self.live) + len(self.asked)}-{url.rsplit('/', 1)[-1]}"
        self.live[token] = url
        return token

    def line(self, url=URL, number=99, title=TITLE):
        return _vcs_record(url=url, number=number, title=title, claim=self.mint(url))

    def module(self):
        forge = types.ModuleType("forge")
        test = self

        def call(verb, payload, repo, *, retry_transient=False):
            test.asked.append((verb, repo, dict(payload), retry_transient))
            if test.fail:
                raise test.fail
            url = test.live.pop(payload.get("claim"), None)
            if test.refuse:
                raise _Refusal(test.refuse)
            if url is None or url != payload.get("url"):
                raise _Refusal("CLAIM_UNKNOWN")
            return {"url": url, "number": int(url.rsplit("/", 1)[-1]), "title": test.title}

        forge.call = call
        return forge


def _tokens(text):
    """Every claim token left on a record line in ``text``."""
    found = []
    for line in (text or "").splitlines():
        proposal = plugin.opened(line)
        if proposal and proposal.get("claim"):
            found.append(proposal["claim"])
    return found


class PrOpenedTest(unittest.TestCase):
    def setUp(self):
        self.ledger = []

    def run_hook(self, output, kanban, task=TASK, broker=None, origins=None):
        env = {plugin.TASK_ENV: task} if task else {}
        modules = {**kanban.modules(), "forge": (broker or _Broker()).module(), "kanban_origin": origins}

        def post(path, payload):
            self.ledger.append((path, payload))
            return {"recorded": True}

        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(plugin, "_session_kv_post", post):
            return plugin.on_terminal_output(command="python3 vcs.py proposal create", output=output, returncode=0)

    def test_the_broker_is_asked_to_claim_the_token_for_its_url_once(self):
        broker = _Broker()
        line = broker.line()
        token = _tokens(line)[0]
        self.run_hook(line, _Kanban(), broker=broker)
        self.assertEqual(broker.asked, [("proposal-claim", REPOSITORY, {"claim": token, "url": URL}, False)])

    def test_a_claimed_record_is_written_to_the_ledger_with_the_origin(self):
        broker = _Broker()
        origins = types.ModuleType("kanban_origin")
        origins.origin_of = lambda conn, task_id: ("audit", "")
        origins.turn_origin = lambda: ("chat", "slack")
        self.run_hook(broker.line(), _Kanban(), broker=broker, origins=origins)
        self.assertEqual(self.ledger, [("/v1/proposals/opened", {"url": URL, "origin": "audit", "platform": ""})])
        self.ledger.clear()
        self.run_hook(broker.line(), _Kanban(), task="", broker=broker, origins=origins)
        self.assertEqual(self.ledger, [("/v1/proposals/opened", {"url": URL, "origin": "chat", "platform": "slack"})])

    def test_a_non_worker_process_claims_and_keeps_the_line_without_its_token(self):
        kanban, broker = _Kanban(), _Broker()
        line = broker.line()
        out = self.run_hook(line + "done\n", kanban, task="", broker=broker)
        self.assertEqual(len(broker.asked), 1)
        self.assertEqual(_tokens(out), [])
        self.assertEqual(plugin.opened(out.splitlines()[0])["url"], URL)
        self.assertTrue(out.endswith("done\n"))
        self.assertEqual(kanban.events, [])
        self.assertEqual([payload["url"] for _, payload in self.ledger], [URL])

    def test_a_worker_still_records_the_card_event_and_removes_the_line(self):
        kanban, broker = _Kanban(), _Broker()
        stdout = json.dumps({"proposal": {"url": URL, "number": 99}}) + "\n"
        out = self.run_hook(broker.line() + stdout, kanban, broker=broker)
        self.assertEqual(out, stdout)
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": URL, "number": 99, "title": TITLE}, 7)])
        self.assertEqual([payload["url"] for _, payload in self.ledger], [URL])

    def test_records_past_the_cap_lose_their_token_unclaimed(self):
        cap = plugin.RECORDS_CHECKED_MAX
        urls = [f"{REPOSITORY}/pull/{n}" for n in range(cap + 3)]
        issue = "https://github.com/acme/fleet-config/issues/3"
        for task in (TASK, ""):
            kanban, broker = _Kanban(), _Broker()
            first = broker.line(url=urls[0], number=0)
            flood = broker.line(url=issue, number=3) + first * 3 + "".join(
                broker.line(url=url, number=n) for n, url in enumerate(urls[1:], 1)
            )
            with self.subTest(worker=bool(task)):
                out = self.run_hook(flood, kanban, task=task, broker=broker)
                self.assertEqual(
                    [payload["url"] for _, _, payload, _ in broker.asked], urls[:cap],
                    "one claim per distinct url, up to the cap; a url that names no proposal takes no slot",
                )
                self.assertEqual(_tokens(out), [], "no token survives, claimed or not")
                kept = [plugin.opened(line)["url"] for line in out.splitlines()]
                if task:
                    self.assertEqual(len(kanban.events), cap)
                    self.assertEqual(kept, [issue] + urls[cap:], "a claimed url's repeats leave with it")
                else:
                    self.assertEqual(kanban.events, [])
                    self.assertEqual(kept, [issue] + [urls[0]] * 3 + urls[1:], "outside a worker every line stays")

    def test_a_repeat_of_a_url_whose_claim_failed_stays_without_its_token(self):
        kanban, broker = _Kanban(), _Broker(refuse="CLAIM_NOT_CONFIRMED")
        with self.assertLogs(plugin.logger, "WARNING"):
            out = self.run_hook(broker.line() * 2, kanban, broker=broker)
        self.assertEqual(len(broker.asked), 1)
        self.assertEqual([plugin.opened(line)["url"] for line in out.splitlines()], [URL, URL])
        self.assertEqual(_tokens(out), [])

    def test_a_record_too_deeply_nested_to_parse_does_not_return_a_token(self):
        broker = _Broker()
        deep = '{"kube-agents.event":' + "[" * 200000 + "\n"
        self.assertIsNone(plugin.opened(deep), "the parser's RecursionError reads as not a record")
        for task in (TASK, ""):
            with self.subTest(worker=bool(task)):
                out = self.run_hook(deep + broker.line() + "done\n", _Kanban(), task=task, broker=broker)
                self.assertNotIn('"claim"', out)
                self.assertTrue(out.endswith("done\n"))

    def test_a_resolver_that_raises_does_not_return_a_token(self):
        origins = types.ModuleType("kanban_origin")
        origins.origin_of = lambda conn, task_id: ("other", "")

        def broken():
            raise RuntimeError("session store down")

        origins.turn_origin = broken
        broker = _Broker()
        with self.assertLogs(plugin.logger, "WARNING"):
            out = self.run_hook(broker.line() + "done\n", _Kanban(), task="", broker=broker, origins=origins)
        self.assertNotIn('"claim"', out)
        self.assertEqual([payload["origin"] for _, payload in self.ledger], ["other"])
        broker = _Broker()
        line = broker.line()
        with mock.patch.object(plugin, "resolve_origin", side_effect=RuntimeError("boom")), \
                self.assertLogs(plugin.logger, "WARNING") as logs:
            out = self.run_hook(line + "done\n", _Kanban(), broker=broker)
        self.assertEqual(_tokens(out), [])
        self.assertEqual(plugin.opened(out.splitlines()[0])["url"], URL)
        self.assertIn("RuntimeError", "\n".join(logs.output))

    def test_a_record_merged_after_other_text_loses_its_token_unclaimed(self):
        # `2>&1` can put the record after text on the same line; it is not counted, and never read back.
        kanban, broker = _Kanban(), _Broker()
        line = broker.line()
        token = _tokens(line)[0]
        out = self.run_hook("Cloning into 'wc'... " + line, kanban, broker=broker)
        self.assertNotIn(token, out)
        self.assertIn('"claim":""', out)
        self.assertEqual((broker.asked, kanban.events), ([], []))

    def test_a_failed_claim_leaves_the_line_without_its_token(self):
        for refusal in ("CLAIM_NOT_CONFIRMED", "REPOSITORY_NOT_MANAGED", "FORGE_UNAVAILABLE"):
            kanban, broker = _Kanban(), _Broker(refuse=refusal)
            line = broker.line()
            with self.subTest(refusal=refusal), self.assertLogs(plugin.logger, "WARNING"):
                out = self.run_hook(line, kanban, broker=broker)
            self.assertEqual(_tokens(out), [])
            self.assertEqual(plugin.opened(out)["url"], URL)
            self.assertEqual((kanban.events, self.ledger), ([], []))

    def test_a_replayed_token_counts_nothing_and_logs_no_url(self):
        broker = _Broker()
        line = broker.line()
        token = _tokens(line)[0]
        self.run_hook(line, _Kanban(), broker=broker)
        kanban = _Kanban()
        self.ledger.clear()
        with self.assertLogs(plugin.logger, "WARNING") as logs:
            out = self.run_hook(line, kanban, broker=broker)
        self.assertEqual((kanban.events, self.ledger), ([], []))
        self.assertEqual(_tokens(out), [])
        joined = "\n".join(logs.output)
        self.assertIn("CLAIM_UNKNOWN", joined)
        self.assertNotIn(URL, joined)
        self.assertNotIn(token, joined)

    def test_resolution_order_card_then_run_then_session_then_convention_then_other(self):
        origins = _load_kanban_origin()
        cron = types.ModuleType("cron_run_scope")
        priority = types.ModuleType("kanban_priority")
        state = {"card": ("audit", ""), "cron": ("scheduled", ""), "session": "sess-1", "record": None}
        state["record"] = {"origin": "chat", "origin_platform": "slack"}
        cron.current_cron_origin = lambda: state["cron"]
        priority.trusted_origin_session = lambda: state["session"]
        modules = {
            **_Kanban().modules(), "kanban_origin": origins, "cron_run_scope": cron, "kanban_priority": priority,
            "tools.cron_run_scope": None, "hermes_cli.kanban_priority": None, "hermes_cli.kanban_origin": None,
        }
        steps = [
            ({}, ("audit", "")),
            ({"card": origins.NO_ORIGIN}, ("scheduled", "")),
            ({"cron": None}, ("chat", "slack")),
            ({"record": None, "session": "web-console-42"}, ("console", "")),
            ({"session": "sess-2"}, ("other", "")),
        ]
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {plugin.TASK_ENV: TASK}, clear=True), \
                mock.patch.object(origins, "origin_of", lambda conn, task_id: state["card"]), \
                mock.patch.object(origins, "_fetch_session_metadata", lambda session_id: state["record"]):
            for change, want in steps:
                state.update(change)
                with self.subTest(**{key: str(value) for key, value in change.items()}):
                    self.assertEqual(plugin.resolve_origin(), want)
            state.update({"card": ("audit", ""), "cron": ("scheduled", "")})
            os.environ.pop(plugin.TASK_ENV)
            self.assertEqual(plugin.resolve_origin(), ("scheduled", ""), "no card outside a worker")
        with mock.patch.dict(sys.modules, {"kanban_origin": None, "hermes_cli.kanban_origin": None}):
            self.assertEqual(plugin.resolve_origin(), ("other", ""), "no module, no origin")

    def test_a_record_line_without_a_token_is_left_as_text_and_counts_nothing(self):
        # A broker older than the token, or a line someone typed: nothing to claim.
        kanban, broker = _Kanban(), _Broker()
        self.assertIsNone(self.run_hook("    " + _vcs_record(), kanban, broker=broker))
        self.assertEqual((broker.asked, kanban.events, self.ledger), ([], [], []))

    def test_the_names_agree_with_the_client_that_writes_the_record(self):
        self.assertEqual(plugin.PROPOSAL_OPENED_KEY, vcs_client.PROPOSAL_OPENED_KEY)
        self.assertEqual(plugin.PROPOSAL_OPENED_EVENT, vcs_client.PROPOSAL_OPENED_EVENT)
        self.assertEqual(plugin.PROPOSAL_OPENED_FIELDS, vcs_client.PROPOSAL_OPENED_FIELDS)
        self.assertEqual(plugin.CLAIM_FIELD, vcs_client.CLAIM_TOKEN_FIELD)

    def test_a_gitlab_merge_request_is_recorded_the_same_way(self):
        kanban, broker = _Kanban(), _Broker(title="Bump")
        mr = "https://gitlab.com/acme/infra/-/merge_requests/12"
        self.assertEqual(self.run_hook(broker.line(url=mr, number=12, title="Bump"), kanban, broker=broker), "")
        self.assertEqual(broker.asked[0][1], "https://gitlab.com/acme/infra")
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": mr, "number": 12, "title": "Bump"}, 7)])

    def test_the_brokers_fields_are_recorded_not_the_lines(self):
        kanban, broker = _Kanban(), _Broker(title="Raise replicas")
        self.run_hook(broker.line(number=7, title="planted words"), kanban, broker=broker)
        self.assertEqual(kanban.events, [(TASK, "pr_opened", {"url": URL, "number": 99, "title": "Raise replicas"}, 7)])

    def test_output_with_no_record_is_left_alone(self):
        kanban = _Kanban()
        cited = f"See {URL}, opened by bob\n" + json.dumps({"proposal": {"url": URL}}) + "\n"
        self.assertIsNone(self.run_hook(cited, kanban))
        self.assertIsNone(self.run_hook(f'{{"kube-agents.event": "something-else", "url": "{URL}"}}\n', kanban))
        self.assertIsNone(self.run_hook("kube-agents.event in prose is not a record\n", kanban))
        self.assertEqual(kanban.events, [])

    def test_a_record_that_cannot_be_written_is_logged_and_the_line_stays_without_its_token(self):
        kanban, broker = _Kanban(fail=RuntimeError("database is locked")), _Broker()
        with self.assertLogs(plugin.logger, "WARNING"):
            out = self.run_hook(broker.line(), kanban, broker=broker)
        self.assertEqual(_tokens(out), [])
        self.assertEqual(plugin.opened(out)["url"], URL)
        self.assertEqual([payload["url"] for _, payload in self.ledger], [URL], "the ledger still counts it")

    def test_a_ledger_write_that_fails_is_logged_with_the_url_and_the_card_still_gets_its_event(self):
        kanban, broker = _Kanban(), _Broker()
        env = {plugin.TASK_ENV: TASK}
        modules = {**kanban.modules(), "forge": broker.module(), "kanban_origin": None}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(plugin, "_session_kv_post", side_effect=OSError("connection refused")), \
                self.assertLogs(plugin.logger, "WARNING") as logs:
            out = plugin.on_terminal_output(output=broker.line())
        self.assertEqual(out, "")
        self.assertEqual(len(kanban.events), 1)
        self.assertEqual(len(broker.asked), 1, "a burned token is not claimed again")
        self.assertIn(URL, "\n".join(logs.output))

    def test_a_claim_that_is_not_answered_ends_the_asking_and_still_strips_every_token(self):
        # A broker or sandbox hop that is down costs one timeout, not one per url.
        for fail in (_Refusal("SANDBOX_UNREACHABLE"), RuntimeError("timed out")):
            kanban, broker = _Kanban(), _Broker(fail=fail)
            output = "".join(broker.line(url=f"{REPOSITORY}/pull/{n}", number=n) for n in range(3))
            with self.subTest(fail=repr(fail)), self.assertLogs(plugin.logger, "WARNING") as logs:
                out = self.run_hook(output, kanban, broker=broker)
            self.assertEqual(len(broker.asked), 1)
            self.assertEqual(_tokens(out), [])
            self.assertEqual(len(out.splitlines()), 3)
            self.assertEqual((kanban.events, self.ledger), ([], []))
            self.assertNotIn(REPOSITORY, "\n".join(logs.output))

    def test_no_forge_here_still_strips_every_token(self):
        kanban, broker = _Kanban(), _Broker()
        line = broker.line()
        modules = {**kanban.modules(), "forge": None}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {}, clear=True), \
                self.assertLogs(plugin.logger, "WARNING"):
            out = plugin.on_terminal_output(output=line)
        self.assertEqual(_tokens(out), [])

    def test_the_title_is_bounded(self):
        kanban, broker = _Kanban(), _Broker(title="x" * (plugin.TITLE_MAX * 2))
        self.run_hook(broker.line(), kanban, broker=broker)
        self.assertEqual(len(kanban.events[0][2]["title"]), plugin.TITLE_MAX)

    def test_registers_the_terminal_output_and_pre_tool_call_hooks(self):
        registered = []
        plugin.register(types.SimpleNamespace(register_hook=lambda name, fn: registered.append((name, fn))))
        self.assertEqual(registered, [
            ("transform_terminal_output", plugin.on_terminal_output),
            ("pre_tool_call", plugin.on_pre_tool_call),
        ])


def _load_kanban_origin():
    """``deploy/docker/patches/kanban_origin.py``, loaded fresh by path."""
    spec = importlib.util.spec_from_file_location("kanban_origin", REPO / "deploy/docker/patches/kanban_origin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



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
    """:func:`plugin.claim` through the real ``forge.py``, with only the broker under ``vcs_client`` stubbed."""

    def claim(self, broker):
        import forge
        import sandbox_exec

        asked = []

        def call(verb, payload):
            asked.append((verb, dict(payload)))
            return broker(verb, payload)

        with mock.patch.object(vcs_client, "_call", call), \
                mock.patch.object(sandbox_exec, "sandbox_enabled", return_value=False), \
                mock.patch.dict(sys.modules, {"forge": forge}):
            verdict = plugin.claim({"url": URL, "number": 99, "title": "t", "claim": "tok"})
        self.assertEqual(asked, [("proposal-claim", {"claim": "tok", "url": URL, "repository": REPOSITORY})])
        return verdict

    def test_the_brokers_answer_is_the_verdict_and_a_refusal_is_none(self):
        answer = {"url": URL, "number": 99, "title": "Raise the limit"}
        self.assertEqual(self.claim(lambda verb, payload: answer), answer)

        def refuse(verb, payload):
            raise vcs_client.VcsError("no live claim", code="CLAIM_UNKNOWN")

        with self.assertLogs(plugin.logger, "WARNING"):
            self.assertIsNone(self.claim(refuse))

    def test_a_broker_that_does_not_answer_is_unanswered(self):
        def down(verb, payload):
            raise vcs_client.VcsError("connection refused")

        with self.assertRaises(plugin.ClaimUnanswered):
            self.claim(down)


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
                broker = _Broker()
                with mock.patch.dict(os.environ, {plugin.TASK_ENV: task_id}), \
                        mock.patch.dict(sys.modules, {"forge": broker.module(), "kanban_origin": None}), \
                        mock.patch.object(plugin, "_session_kv_post", return_value={"recorded": True}):
                    plugin.on_terminal_output(output=broker.line(), returncode=0)
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
        kanban, broker = _Kanban(), _Broker()
        stdout = json.dumps({"proposal": {"url": URL, "number": 99}}) + "\n"
        modules = {**kanban.modules(), "forge": broker.module(), "kanban_origin": None}
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, {plugin.TASK_ENV: TASK}), \
                mock.patch.object(plugin, "_session_kv_post", return_value={"recorded": True}):
            out = terminal_tool_result._apply_output_transform_hook(
                "python3 vcs.py proposal create --title t", broker.line() + stdout, 0, "task-1", "ssh",
            )
        self.assertEqual(out, stdout)
        self.assertEqual([event[1] for event in kanban.events], ["pr_opened"])


if __name__ == "__main__":
    unittest.main()
