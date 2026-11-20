"""Unit tests for the eval run tag (eval_run_tag.py, apply_eval_run_tag.py, verify_eval_run_tag.py).

The board half runs through kanban_origin's patched synthetic Hermes
(test_kanban_origin.Hermes) with eval_run_tag.py installed beside it; the API
server half applies the patch to a miniature of the two handlers and runs the
verifier and the patched handlers against it; the model request half drives
``on_llm_request`` directly.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_eval_run_tag.py'
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import apply_eval_run_tag as applier
import eval_run_tag
import verify_eval_run_tag as verify
from test_kanban_origin import CHAT_SESSION, Hermes

HERE = Path(__file__).resolve().parent
TAG = "devops-bench-0123456789ab/some-case/2"
OTHER_TAG = "devops-bench-ba9876543210/other-case/1"
ENV_ON = {eval_run_tag.ENABLE_ENV: "1"}
ENV_OFF = {eval_run_tag.ENABLE_ENV: ""}

CONNECT_SOURCE = textwrap.dedent('''
    import contextlib
    import os

    from hermes_cli import kanban_db as kb


    @contextlib.contextmanager
    def connect_closing():
        conn = kb.connect(os.environ["KANBAN_TEST_BOARD"])
        try:
            yield conn
        finally:
            conn.close()
''')


def _purge(prefixes):
    for name in [n for n in sys.modules if any(n == p or n.startswith(p + ".") for p in prefixes)]:
        del sys.modules[name]


# ---------------------------------------------------------------------------
# The tag and the switch
# ---------------------------------------------------------------------------


class TagTest(unittest.TestCase):
    def test_valid_tags(self):
        for good in (TAG, "inject-run/case_a.b/10", " devops-bench-x/c/1 "):
            self.assertEqual(eval_run_tag.valid_tag(good), good.strip(), good)
        for bad in ("", "a/b", "a/b/c", "a/b/1/2", "a b/c/1", "a/b/1\r\nX: y", None, 7, "/b/1"):
            self.assertEqual(eval_run_tag.valid_tag(bad), "", bad)

    def test_the_switch(self):
        for value, on in (("1", True), ("true", True), ("ON", True), ("", False), ("0", False), ("false", False)):
            with mock.patch.dict(os.environ, {eval_run_tag.ENABLE_ENV: value}):
                self.assertIs(eval_run_tag.enabled(), on, value)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(eval_run_tag.enabled())

    def test_the_pattern_matches_the_proxys_the_publishers_and_the_session_kv_servers(self):
        repo = HERE.parents[2]
        for path, name in (
            (repo / "hack" / "replay-proxy" / "replay_proxy.py", "EVAL_RUN_RE"),
            (repo / "hack" / "replay-proxy" / "run_attribution.py", "EVAL_RUN_RE"),
            (repo / "agents" / "platform" / "scripts" / "session_kv_server.py", "SESSION_EVAL_RUN_RE"),
        ):
            source = path.read_text()
            self.assertIn(f'{name} = re.compile(r"{eval_run_tag.TAG_RE.pattern}")', source, path)

    def test_the_pattern_matches_the_hermes_bridges(self):
        source = (HERE.parents[2] / "a2a" / "hermes-bridge" / "api.go").read_text()
        found = re.search(r"apiEvalRunPattern = regexp\.MustCompile\(`\^(.*)\$`\)", source)
        self.assertIsNotNone(found, "apiEvalRunPattern is gone from a2a/hermes-bridge/api.go")
        self.assertEqual(found.group(1), eval_run_tag.TAG_RE.pattern)

    def test_the_profile(self):
        for home, name in (("/opt/data", "default"), ("/opt/data/profiles/platform", "platform"),
                           ("/opt/data/profiles/cluster-p-us-c", "cluster-p-us-c"), ("", "default")):
            with mock.patch.dict(os.environ, {"HERMES_HOME": home}):
                self.assertEqual(eval_run_tag.profile_name(), name, home)


# ---------------------------------------------------------------------------
# The model request
# ---------------------------------------------------------------------------


class LlmRequestTest(unittest.TestCase):
    REQUEST = {"model": "model-default", "messages": [{"role": "user", "content": "hi"}],
               "extra_headers": {"X-Existing": "1"}}

    def setUp(self):
        eval_run_tag._SESSION_CACHE.clear()
        eval_run_tag._CARD_CACHE.clear()
        self.addCleanup(eval_run_tag._SESSION_CACHE.clear)
        self.addCleanup(eval_run_tag._CARD_CACHE.clear)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": "/opt/data/profiles/platform"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(eval_run_tag.WORKER_TASK_ENV, None)

    def call(self, **context):
        request = {**self.REQUEST, "extra_headers": dict(self.REQUEST["extra_headers"])}
        before = repr(request)
        result = eval_run_tag.on_llm_request(request=request, original_request=request, **context)
        self.assertEqual(repr(request), before, "the request handed in is not edited")
        return result

    def test_off_leaves_the_request_alone(self):
        with mock.patch.dict(os.environ, ENV_OFF), \
                mock.patch.object(eval_run_tag, "_fetch_session_tag", side_effect=AssertionError("read while off")):
            self.assertIsNone(self.call(session_id=CHAT_SESSION))

    def test_on_tags_from_the_session_and_changes_only_the_headers(self):
        with mock.patch.dict(os.environ, ENV_ON), \
                mock.patch.object(eval_run_tag, "_fetch_session_tag", return_value=TAG) as fetch:
            result = self.call(session_id=CHAT_SESSION)
            self.call(session_id=CHAT_SESSION)
        fetch.assert_called_once_with(CHAT_SESSION)
        tagged = result["request"]
        self.assertEqual(tagged["extra_headers"], {
            "X-Existing": "1", eval_run_tag.HEADER: TAG, eval_run_tag.AGENT_HEADER: "platform;session",
        })
        # Nothing the model reads, or the proxy keys on, changes.
        self.assertEqual({k: v for k, v in tagged.items() if k != "extra_headers"},
                         {k: v for k, v in self.REQUEST.items() if k != "extra_headers"})

    def test_on_a_worker_tags_from_its_card(self):
        with mock.patch.dict(os.environ, {**ENV_ON, eval_run_tag.WORKER_TASK_ENV: "t_1"}), \
                mock.patch.object(eval_run_tag, "worker_card_tag", return_value=OTHER_TAG), \
                mock.patch.object(eval_run_tag, "_fetch_session_tag", return_value=TAG):
            headers = self.call(session_id=CHAT_SESSION)["request"]["extra_headers"]
        self.assertEqual(headers[eval_run_tag.HEADER], OTHER_TAG)
        self.assertEqual(headers[eval_run_tag.AGENT_HEADER], "platform;card")

    def test_on_with_no_tag_still_names_the_agent(self):
        with mock.patch.dict(os.environ, ENV_ON), \
                mock.patch.object(eval_run_tag, "_fetch_session_tag", return_value=""):
            headers = self.call(session_id="")["request"]["extra_headers"]
        self.assertNotIn(eval_run_tag.HEADER, headers)
        self.assertEqual(headers[eval_run_tag.AGENT_HEADER], "platform;none")

    def test_a_failing_lookup_never_fails_the_call(self):
        with mock.patch.dict(os.environ, ENV_ON), \
                mock.patch.object(eval_run_tag, "worker_card_tag", side_effect=sqlite3.OperationalError("locked")), \
                mock.patch.object(eval_run_tag, "session_tag", side_effect=RuntimeError("boom")):
            headers = self.call(session_id=CHAT_SESSION)["request"]["extra_headers"]
        self.assertEqual(headers[eval_run_tag.AGENT_HEADER], "platform;none")
        with mock.patch.dict(os.environ, ENV_ON), \
                mock.patch.object(eval_run_tag, "current_tag", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.call(session_id=CHAT_SESSION))

    def test_a_session_miss_is_asked_again_after_its_ttl_and_a_hit_is_kept(self):
        clock = [100.0]
        with mock.patch.object(eval_run_tag.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(eval_run_tag, "_fetch_session_tag", side_effect=["", "", TAG]) as fetch:
            self.assertEqual(eval_run_tag.session_tag("s"), "")
            self.assertEqual(eval_run_tag.session_tag("s"), "")
            clock[0] += eval_run_tag.MISS_CACHE_TTL_SECONDS + 1
            self.assertEqual(eval_run_tag.session_tag("s"), "")
            clock[0] += eval_run_tag.MISS_CACHE_TTL_SECONDS + 1
            self.assertEqual(eval_run_tag.session_tag("s"), TAG)
            clock[0] += eval_run_tag.MISS_CACHE_TTL_SECONDS * 10
            self.assertEqual(eval_run_tag.session_tag("s"), TAG)
        self.assertEqual(fetch.call_count, 3)

    def test_an_unreachable_session_kv_reads_as_no_tag(self):
        with mock.patch.object(eval_run_tag, "_session_kv", side_effect=OSError("down")):
            self.assertEqual(eval_run_tag._fetch_session_tag("s"), "")
            self.assertFalse(eval_run_tag.record_session_tag("s", TAG))


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


class BoardTest(Hermes):
    def setUp(self):
        super().setUp()
        shutil.copy(HERE / "eval_run_tag.py", self.root / "hermes_cli" / "eval_run_tag.py")
        (self.root / "hermes_cli" / "kanban_db_connect.py").write_text(CONNECT_SOURCE)
        from hermes_cli import eval_run_tag as ert

        self.ert = ert
        self.tags: dict = {}
        self.fetch = mock.patch.object(ert, "_fetch_session_tag", side_effect=lambda sid: self.tags.get(sid, ""))
        self.fetched = self.fetch.start()
        self.addCleanup(self.fetch.stop)
        patcher = mock.patch.object(ert, "_turn_session_id", side_effect=lambda: self.session)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tag(self, task_id):
        with self.conn() as conn:
            return self.ert.card_tag_in(conn, task_id)

    def table_exists(self):
        with self.conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (self.ert.TABLE,)
            ).fetchone()[0] == 1

    def test_off_writes_nothing_and_reads_nothing(self):
        self.in_chat_turn()
        self.tags[CHAT_SESSION] = TAG
        with mock.patch.dict(os.environ, ENV_OFF):
            card = self.create()
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": card}):
                self.slash("create --json --assignee platform child")
        self.assertFalse(self.table_exists())
        self.fetched.assert_not_called()
        self.assertEqual(self.origin(card), ("chat", "slack"), "the origin stamp is untouched")

    def test_on_a_top_level_card_takes_the_sessions_tag(self):
        self.in_chat_turn()
        self.tags[CHAT_SESSION] = TAG
        with mock.patch.dict(os.environ, ENV_ON):
            tool_card = self.create()
            slash_card = self.slash("create --json --assignee platform from-chat")
        self.assertEqual(self.tag(tool_card), TAG)
        self.assertEqual(self.tag(slash_card), TAG)
        self.assertEqual(self.origin(tool_card), ("chat", "slack"))

    def test_on_children_inherit_their_creator(self):
        self.in_chat_turn()
        self.tags[CHAT_SESSION] = TAG
        with mock.patch.dict(os.environ, ENV_ON):
            parent = self.create(title="parent")
            # The worker runs later, in another session whose tag differs.
            self.session = "another-session"
            self.tags["another-session"] = OTHER_TAG
            with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": parent}):
                tool_child = self.create(creator_task_id=parent)
                cli_child = self.slash("create --json --assignee platform child")
                # A worker's create that names no creator takes the card it runs.
                loose = self.create(title="no creator")
            root = self.create(title="root", triage=True)
            with self.conn() as conn:
                decomposed = self.graph.decompose_triage_task(conn, root, children=[{"title": "a"}])
        self.assertEqual([self.tag(t) for t in (tool_child, cli_child, loose)], [TAG] * 3)
        self.assertEqual(self.tag(root), OTHER_TAG)
        self.assertEqual(self.tag(decomposed[0]), OTHER_TAG)

    def test_on_an_untagged_session_stamps_nothing(self):
        self.in_chat_turn()
        with mock.patch.dict(os.environ, ENV_ON):
            card = self.create()
        self.assertEqual(self.tag(card), "")

    def test_on_a_swarm_under_one_transaction_pays_one_session_lookup(self):
        self.in_chat_turn()
        self.tags[CHAT_SESSION] = TAG
        with mock.patch.dict(os.environ, ENV_ON):
            out = self.kanban.run_slash("swarm --worker a goal")
        import json

        ids = json.loads(out[out.find("{"):])["ids"]
        self.assertEqual({self.tag(t) for t in ids}, {TAG})
        self.assertEqual(self.fetched.call_count, 1)

    def test_on_a_failing_tag_never_fails_the_create(self):
        self.in_chat_turn()
        self.tags[CHAT_SESSION] = TAG
        with mock.patch.dict(os.environ, ENV_ON):
            with mock.patch.object(self.ert, "session_tag", side_effect=RuntimeError("boom")):
                first = self.create()
            with mock.patch.object(self.ert, "stamp_card", side_effect=sqlite3.OperationalError("locked")):
                second = self.create()
            with mock.patch.object(self.ert, "resolve_new_card", side_effect=RuntimeError("boom")):
                third = self.create()
        for card in (first, second, third):
            self.assertEqual(self.tag(card), "")
            self.assertEqual(self.origin(card), ("chat", "slack"))


# ---------------------------------------------------------------------------
# The API server patch
# ---------------------------------------------------------------------------

ROUTES_STUB = textwrap.dedent('''
    import uuid


    def _derive_chat_session_id(system_prompt, first_user):
        return "derived-" + first_user


    def _coerce_request_bool(value, default=False):
        return default if value is None else bool(value)


    class OpenAICompatRoutesMixin:
        async def _handle_chat_completions(self, request):
            provided_session_id = request.headers.get("X-Hermes-Session-Id", "").strip()
            system_prompt, first_user = "", "hello"
            if provided_session_id:
                session_id = provided_session_id
            else:
                session_id = _derive_chat_session_id(system_prompt, first_user)
            completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
            return session_id

        async def _handle_responses(self, request):
            body = {}
            stored_session_id = None
            gateway_session_key = None
            session_id = (
                stored_session_id
                or self._declared_conversation_session(gateway_session_key)
                or str(uuid.uuid4()))
            stream = _coerce_request_bool(body.get("stream"), default=False)
            return session_id

        def _declared_conversation_session(self, key):
            return "declared"
''')


class _Request:
    def __init__(self, headers):
        self.headers = headers


class ApiServerPatchTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        routes = self.root / applier.ROUTES_RELATIVE
        routes.parent.mkdir(parents=True)
        routes.write_text(ROUTES_STUB)
        for package in ("gateway", "gateway/platforms", "hermes_cli"):
            (self.root / package).mkdir(parents=True, exist_ok=True)
            (self.root / package / "__init__.py").write_text("")
        shutil.copy(HERE / "eval_run_tag.py", self.root / "hermes_cli" / "eval_run_tag.py")
        _purge(("gateway", "hermes_cli"))
        self.addCleanup(_purge, ("gateway", "hermes_cli"))
        sys.path.insert(0, str(self.root))
        self.addCleanup(lambda: [sys.path.remove(p) for p in list(sys.path) if p == str(self.root)])

    def run_verifier(self):
        with mock.patch.object(verify, "HERMES", self.root), mock.patch.object(verify, "FAILURES", []):
            rc = verify.main()
            return rc, list(verify.FAILURES)

    def test_both_handlers_record_the_settled_session(self):
        applier.apply(self.root)
        self.assertEqual(self.run_verifier(), (0, []))
        routes = importlib.import_module("gateway.platforms.api_server_openai_routes")
        from hermes_cli import eval_run_tag as ert

        mixin = routes.OpenAICompatRoutesMixin()
        with mock.patch.object(ert, "record_session_tag") as record:
            with mock.patch.dict(os.environ, ENV_OFF):
                asyncio.run(mixin._handle_chat_completions(_Request({ert.HEADER: TAG})))
            record.assert_not_called()
            with mock.patch.dict(os.environ, ENV_ON):
                asyncio.run(mixin._handle_chat_completions(_Request({ert.HEADER: TAG})))
                asyncio.run(mixin._handle_chat_completions(_Request({ert.HEADER: TAG, "X-Hermes-Session-Id": "given"})))
                asyncio.run(mixin._handle_responses(_Request({ert.HEADER: OTHER_TAG})))
        self.assertEqual(record.call_args_list, [
            mock.call("derived-hello", TAG), mock.call("given", TAG), mock.call("declared", OTHER_TAG),
        ])

    def test_a_missing_module_falls_back_to_a_no_op(self):
        applier.apply(self.root)
        (self.root / "hermes_cli" / "eval_run_tag.py").unlink()
        routes = importlib.import_module("gateway.platforms.api_server_openai_routes")
        self.assertEqual(asyncio.run(routes.OpenAICompatRoutesMixin()._handle_responses(_Request({}))), "declared")

    def test_a_second_apply_is_refused(self):
        applier.apply(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_a_moved_anchor_is_refused_and_nothing_is_written(self):
        routes = self.root / applier.ROUTES_RELATIVE
        routes.write_text(ROUTES_STUB.replace("        completion_id = ", "        completion  = "))
        with self.assertRaises(SystemExit):
            applier.apply(self.root)
        self.assertNotIn(applier.MARKER, routes.read_text())

    def test_the_verifier_refuses_an_unpatched_tree(self):
        rc, failures = self.run_verifier()
        self.assertEqual(rc, 1)
        self.assertTrue(any("expected one await" in f for f in failures), failures)

    def test_the_verifier_catches_a_record_before_the_session_is_settled(self):
        applier.apply(self.root)
        routes = self.root / applier.ROUTES_RELATIVE
        source = routes.read_text().replace(
            "        stream = _coerce_request_bool",
            "        session_id = 'late'\n        stream = _coerce_request_bool",
        )
        routes.write_text(source)
        rc, failures = self.run_verifier()
        self.assertEqual(rc, 1)
        self.assertTrue(any("reassigned after the record" in f for f in failures), failures)

    def test_the_verifier_catches_extra_call_arguments(self):
        call = f"await {applier.RECORD}(request, session_id)"
        for extra in ("request, None, session_id", "request, session_id, 'x'", "request, self.sid, session_id",
                      "request, session_id, foo=1"):
            with self.subTest(extra=extra):
                self.setUp()
                applier.apply(self.root)
                routes = self.root / applier.ROUTES_RELATIVE
                source = routes.read_text()
                self.assertEqual(source.count(call), 2)
                routes.write_text(source.replace(call, f"await {applier.RECORD}({extra})"))
                rc, failures = self.run_verifier()
                self.assertEqual(rc, 1)
                self.assertTrue(any("not (request, session_id)" in f for f in failures), failures)

    def test_the_verifier_catches_a_switch_that_does_not_gate(self):
        applier.apply(self.root)
        module = self.root / "hermes_cli" / "eval_run_tag.py"
        module.write_text(module.read_text().replace(
            "    if not enabled():\n        return\n    try:\n        tag = valid_tag(request.headers",
            "    try:\n        tag = valid_tag(request.headers",
        ))
        rc, failures = self.run_verifier()
        self.assertEqual(rc, 1)
        self.assertTrue(any("off, record_request" in f for f in failures), failures)


class DockerfileTest(unittest.TestCase):
    def test_the_image_installs_applies_and_verifies(self):
        text = (HERE.parent / "Dockerfile").read_text()
        for needle in (
            "deploy/docker/patches/eval_run_tag.py",
            "install -m 0644 /tmp/latency-patches/eval_run_tag.py /opt/hermes/hermes_cli/eval_run_tag.py",
            "/tmp/latency-patches/apply_eval_run_tag.py /opt/hermes",
            "/tmp/latency-patches/verify_eval_run_tag.py",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
