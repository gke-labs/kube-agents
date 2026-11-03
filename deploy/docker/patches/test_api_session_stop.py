"""Unit tests for the API-server session stop installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import asyncio
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import api_session_stop as stop

# The #3263 shape: the bridge's session for a gateway conversation.
SESSION = "a2a-ctx-0b1e5ccf90bd164ea10f7d31"
OTHER = "a2a-ctx-77777777777777777777777777"


class _Adapter:
    def __init__(self, auth_err=None):
        self.auth_err = auth_err

    def _check_auth(self, request):
        return self.auth_err


class _Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status


def _aiohttp_stub():
    web = types.SimpleNamespace(json_response=lambda body, status=200: _Response(body, status))
    return mock.patch.dict(sys.modules, {"aiohttp": types.SimpleNamespace(web=web), "aiohttp.web": web})


def _request(session_id="", headers=None):
    return types.SimpleNamespace(headers=headers or {}, match_info={"session_id": session_id})


class _Interrupts:
    """Stands in for upstream's hard interrupt and reap; records each agent."""

    def __init__(self):
        self.agents = []

    def __call__(self, agent):
        self.agents.append(agent)

    def patch(self):
        return mock.patch.object(stop, "_hard_interrupt", self)


def running(adapter, session_id=SESSION):
    with stop._LOCK:
        return len(stop._turns(adapter).get(session_id, ()))


class TestTheHandlerWrapper(unittest.TestCase):
    def test_a_request_naming_a_session_is_running_until_it_returns(self):
        adapter, seen = _Adapter(), {}

        async def handler(self_, request):
            seen["running"] = running(self_)
            return "ok"

        result = asyncio.run(stop.track(handler)(adapter, _request(headers={stop.SESSION_HEADER: SESSION})))
        self.assertEqual(result, "ok")
        self.assertEqual(seen["running"], 1)
        self.assertEqual(running(adapter), 0)

    def test_a_request_with_no_session_is_untouched(self):
        adapter, seen = _Adapter(), {}

        async def handler(self_, request):
            seen["turn"] = stop._CURRENT.get()
            return "ok"

        asyncio.run(stop.track(handler)(adapter, _request()))
        self.assertIsNone(seen["turn"])
        self.assertEqual(stop._turns(adapter), {})

    def test_a_raising_handler_still_releases_its_turn(self):
        adapter = _Adapter()

        async def handler(self_, request):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            asyncio.run(stop.track(handler)(adapter, _request(headers={stop.SESSION_HEADER: SESSION})))
        self.assertEqual(running(adapter), 0)


class TestTheStop(unittest.TestCase):
    def test_a_running_turn_with_an_agent_is_interrupted(self):
        adapter, agent, interrupts = _Adapter(), object(), _Interrupts()
        turn = stop._Turn(SESSION)
        turn.agent = agent
        stop._turns(adapter)[SESSION] = {turn}
        with interrupts.patch():
            turns, interrupted = stop.stop(adapter, SESSION)
        self.assertEqual((turns, interrupted), ([turn], 1))
        self.assertTrue(turn.stopped)
        self.assertEqual(interrupts.agents, [agent])

    def test_a_turn_stopped_before_its_agent_existed_is_interrupted_at_bind(self):
        adapter, agent, interrupts = _Adapter(), object(), _Interrupts()
        turn = stop._Turn(SESSION)
        stop._turns(adapter)[SESSION] = {turn}
        with interrupts.patch():
            self.assertEqual(stop.stop(adapter, SESSION), ([turn], 0))
            token = stop._CURRENT.set(turn)
            try:
                stop.bind(agent)
            finally:
                stop._CURRENT.reset(token)
        self.assertEqual(interrupts.agents, [agent])

    def test_a_turn_that_was_not_stopped_binds_without_an_interrupt(self):
        interrupts, turn = _Interrupts(), stop._Turn(SESSION)
        with interrupts.patch():
            token = stop._CURRENT.set(turn)
            try:
                stop.bind(object())
            finally:
                stop._CURRENT.reset(token)
        self.assertEqual(interrupts.agents, [])

    def test_another_sessions_turn_is_left_alone(self):
        adapter, interrupts = _Adapter(), _Interrupts()
        other = stop._Turn(OTHER)
        other.agent = object()
        stop._turns(adapter)[OTHER] = {other}
        with interrupts.patch():
            self.assertEqual(stop.stop(adapter, SESSION), ([], 0))
        self.assertFalse(other.stopped)
        self.assertEqual(interrupts.agents, [])

    def test_the_next_turn_after_a_stop_runs_normally(self):
        adapter, seen, interrupts = _Adapter(), {}, _Interrupts()

        async def handler(self_, request):
            seen["refusal"] = stop.refuse_create_on_stop()
            return "ok"

        with interrupts.patch():
            stop.stop(adapter, SESSION)
            asyncio.run(stop.track(handler)(adapter, _request(headers={stop.SESSION_HEADER: SESSION})))
        self.assertIsNone(seen["refusal"])


class TestTheCarry(unittest.TestCase):
    def test_the_thread_holds_the_turn_after_the_handler_returns(self):
        adapter, started, finish = _Adapter(), threading.Event(), threading.Event()
        seen = {}

        def turn_body():
            seen["turn"] = stop._CURRENT.get()
            started.set()
            finish.wait(5)

        async def handler(self_, request):
            fn = stop.carry(self_, turn_body)
            threading.Thread(target=fn).start()
            started.wait(5)
            return "ok"

        asyncio.run(stop.track(handler)(adapter, _request(headers={stop.SESSION_HEADER: SESSION})))
        self.assertIsNotNone(seen["turn"])
        self.assertEqual(running(adapter), 1, "the thread still runs the turn")
        finish.set()
        for _ in range(50):
            if not running(adapter):
                break
            threading.Event().wait(0.02)
        self.assertEqual(running(adapter), 0)

    def test_no_turn_passes_the_function_through(self):
        def fn():
            return None

        self.assertIs(stop.carry(_Adapter(), fn), fn)


class TestTheRoute(unittest.TestCase):
    def run_route(self, adapter, session_id=SESSION):
        with _aiohttp_stub():
            return asyncio.run(stop.handle(adapter, _request(session_id)))

    def test_it_answers_once_the_stopped_turn_ends(self):
        adapter, interrupts = _Adapter(), _Interrupts()
        turn = stop._Turn(SESSION)
        turn.agent = object()
        stop._turns(adapter)[SESSION] = {turn}

        def end_turn(agent):
            interrupts(agent)
            threading.Timer(0.2, stop._release, (adapter, turn)).start()

        with mock.patch.object(stop, "_hard_interrupt", end_turn):
            resp = self.run_route(adapter)
        self.assertEqual(resp.body, {"session_id": SESSION, "turns": 1, "interrupted": 1, "ended": True})

    def test_a_turn_still_running_at_the_wait_says_so(self):
        adapter, interrupts = _Adapter(), _Interrupts()
        stop._turns(adapter)[SESSION] = {stop._Turn(SESSION)}
        with interrupts.patch(), mock.patch.object(stop, "WAIT_SECONDS", 0.2):
            resp = self.run_route(adapter)
        self.assertEqual(resp.body["ended"], False)
        self.assertEqual(resp.body["interrupted"], 0)

    def test_a_session_with_nothing_running_answers_at_once(self):
        resp = self.run_route(_Adapter())
        self.assertEqual(resp.body, {"session_id": SESSION, "turns": 0, "interrupted": 0, "ended": True})

    def test_the_servers_key_is_required(self):
        denied = _Response({"error": "unauthorized"}, 401)
        self.assertIs(self.run_route(_Adapter(auth_err=denied)), denied)

    def test_an_overlong_session_id_is_refused(self):
        self.assertEqual(self.run_route(_Adapter(), "s" * (stop.MAX_SESSION_ID_LEN + 1)).status, 400)

    def test_the_route_row_is_a_bound_coroutine(self):
        method, path, handler = stop.route(_Adapter())
        self.assertEqual((method, path), ("POST", stop.ROUTE_PATH))
        self.assertTrue(asyncio.iscoroutinefunction(handler))


class TestTheCreateGate(unittest.TestCase):
    def test_a_stopped_turn_is_refused(self):
        turn = stop._Turn(SESSION)
        turn.stopped = True
        token = stop._CURRENT.set(turn)
        try:
            self.assertEqual(stop.refuse_create_on_stop(), stop.CREATE_REFUSAL)
        finally:
            stop._CURRENT.reset(token)

    def test_a_running_turn_and_no_turn_are_not(self):
        self.assertIsNone(stop.refuse_create_on_stop())
        token = stop._CURRENT.set(stop._Turn(SESSION))
        try:
            self.assertIsNone(stop.refuse_create_on_stop())
        finally:
            stop._CURRENT.reset(token)


# The wake guard's output at the lines this patch anchors on, as
# apply_kanban_wake_guard.py leaves them.
API_FIXTURE = """import asyncio
from contextvars import ContextVar
from tools.kanban_wake_guard import (
    carry_wake_mark as _kanban_wake_carry,
    wake_marked as _kanban_wake_marked,
)


class APIServerAdapter(OpenAICompatRoutesMixin):
    _handle_chat_completions = _admit_api_agent_request(
        _kanban_wake_marked(OpenAICompatRoutesMixin._handle_chat_completions)
    )

    def _http_route_table(self):
        routes = []
        routes.extend(_api_runs._http_routes(self))
        return routes

    async def _run_agent(self, loop):
        def _run():
            agent = None
            try:
                if True:
                    self._shutdown_interruptible_agents[id(agent)] = agent
            finally:
                pass
        try:
            return await loop.run_in_executor(None, _kanban_wake_carry(_run))
        finally:
            pass
"""
TOOLS_FIXTURE = """def _handle_create(args):
    _reject_delegated_child_mutation("kanban_create")
    _wake_err = _kanban_refuse_create_on_wake()
    if _wake_err:
        return tool_error(_wake_err)
    title = args.get("title")
"""


class TestTheApplier(unittest.TestCase):
    def setUp(self):
        import apply_api_session_stop as applier

        self.applier = applier
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        for relative, source in ((applier.API_RELATIVE, API_FIXTURE), (applier.TOOLS_RELATIVE, TOOLS_FIXTURE)):
            path = self.tmp / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)

    def read(self, relative):
        return (self.tmp / relative).read_text()

    def test_every_hook_lands_and_the_files_still_parse(self):
        self.applier.apply(self.tmp)
        api = self.read(self.applier.API_RELATIVE)
        compile(api, "api_server.py", "exec")
        self.assertIn("_kanban_wake_marked(_kube_session_stop.track(OpenAICompatRoutesMixin._handle_chat_completions))", api)
        self.assertIn("_kanban_wake_carry(_kube_session_stop.carry(self, _run))", api)
        self.assertIn("_kube_session_stop.bind(agent)", api)
        self.assertIn("routes.append(_kube_session_stop.route(self))", api)
        self.assertLess(api.index("import api_session_stop as _kube_session_stop"), api.index("class APIServerAdapter"))
        tools = self.read(self.applier.TOOLS_RELATIVE)
        compile(tools, "kanban_tools.py", "exec")
        self.assertLess(tools.index("_kube_refuse_create_on_stop()"), tools.index('args.get("title")'))

    def test_a_second_run_refuses(self):
        self.applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_without_the_wake_guard_first_the_build_fails_and_writes_nothing(self):
        path = self.tmp / self.applier.API_RELATIVE
        path.write_text(API_FIXTURE.replace("_kanban_wake_carry(_run)", "_run"))
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)
        self.assertNotIn(self.applier.MARKER, path.read_text())
        self.assertNotIn(self.applier.MARKER, self.read(self.applier.TOOLS_RELATIVE))


if __name__ == "__main__":
    unittest.main()
