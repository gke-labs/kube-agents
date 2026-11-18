#!/usr/bin/env python3
"""Build gate for the eval run tag patch (apply_eval_run_tag.py, eval_run_tag.py).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after the applier, with
``hermes_cli/eval_run_tag.py`` installed. The applier proves its anchors
matched once; this proves the result behaves:

1. In both ``_handle_chat_completions`` and ``_handle_responses`` the patched
   ``await _kube_agents_eval_run_record(request, session_id)`` is there once and
   follows every assignment to ``session_id``, so it records the session the
   turn runs under.
2. The patched module imports, and its ``_kube_agents_eval_run_record`` is
   ``hermes_cli.eval_run_tag.record_request``, not the fallback.
3. ``record_request`` off reads nothing (a request whose headers raise is not
   touched) and records nothing; on, a well-formed header is recorded for the
   session, a malformed one is not, and a failing record raises nothing.

Usage::

    cd /opt/hermes && python3 verify_eval_run_tag.py
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import os
import sys
from pathlib import Path
from unittest import mock

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
ROUTES_RELATIVE = "gateway/platforms/api_server_openai_routes.py"
ROUTES_MODULE = "gateway.platforms.api_server_openai_routes"
ROUTES_CLASS = "OpenAICompatRoutesMixin"
HANDLERS = ("_handle_chat_completions", "_handle_responses")
RECORD = "_kube_agents_eval_run_record"
TAG_MODULE = "hermes_cli.eval_run_tag"
SESSION = "20261117_120000_abcdef12"
TAG = "devops-bench-0123456789ab/some-case/2"
FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _record_awaits(function: ast.AST) -> list[ast.Await]:
    found = []
    for node in ast.walk(function):
        if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name) and node.value.func.id == RECORD):
            found.append(node)
    return found


def _session_assignments(function: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "session_id" for t in node.targets):
                lines.append(node.lineno)
    return lines


def check_call_sites(source: str) -> None:
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == ROUTES_CLASS]
    if len(classes) != 1:
        fail(f"expected one class {ROUTES_CLASS} in {ROUTES_RELATIVE}, found {len(classes)}")
        return
    methods = {n.name: n for n in classes[0].body if isinstance(n, ast.AsyncFunctionDef)}
    for name in HANDLERS:
        function = methods.get(name)
        if function is None:
            fail(f"{ROUTES_CLASS}.{name} is missing")
            continue
        awaits = _record_awaits(function)
        if len(awaits) != 1:
            fail(f"{name}: expected one await {RECORD}(...), found {len(awaits)}")
            continue
        call = awaits[0].value
        args = [a.id for a in call.args if isinstance(a, ast.Name)]
        if args != ["request", "session_id"]:
            fail(f"{name}: {RECORD} is called with {args}, not (request, session_id)")
        later = [line for line in _session_assignments(function) if line > awaits[0].lineno]
        if later:
            fail(f"{name}: session_id is reassigned after the record (lines {later}): the tag would land on the wrong session")


def check_import() -> None:
    sys.path.insert(0, str(HERMES))
    try:
        routes = importlib.import_module(ROUTES_MODULE)
        tag_module = importlib.import_module(TAG_MODULE)
    except Exception as exc:  # noqa: BLE001 - the build log needs the reason
        fail(f"the patched module does not import: {exc!r}")
        return
    if getattr(routes, RECORD, None) is not tag_module.record_request:
        fail(f"{ROUTES_MODULE}.{RECORD} is the fallback, not {TAG_MODULE}.record_request")


class _Request:
    def __init__(self, headers):
        self._headers = headers

    @property
    def headers(self):
        if isinstance(self._headers, Exception):
            raise self._headers
        return self._headers


def check_behaviour() -> None:
    try:
        tag_module = importlib.import_module(TAG_MODULE)
    except Exception as exc:  # noqa: BLE001
        fail(f"{TAG_MODULE} does not import: {exc!r}")
        return
    header = tag_module.HEADER
    env = tag_module.ENABLE_ENV
    with mock.patch.object(tag_module, "record_session_tag") as record:
        with mock.patch.dict(os.environ, {env: ""}):
            asyncio.run(tag_module.record_request(_Request(AssertionError("headers read while off")), SESSION))
            asyncio.run(tag_module.record_request(_Request({header: TAG}), SESSION))
        if record.called:
            fail("off, record_request recorded a tag")
        with mock.patch.dict(os.environ, {env: "1"}):
            asyncio.run(tag_module.record_request(_Request({header: TAG}), SESSION))
            if record.call_args_list != [mock.call(SESSION, TAG)]:
                fail(f"on, a well-formed tag was recorded as {record.call_args_list}")
            record.reset_mock()
            asyncio.run(tag_module.record_request(_Request({header: TAG + "\r\nX-Other: 1"}), SESSION))
            asyncio.run(tag_module.record_request(_Request({}), SESSION))
            if record.called:
                fail("on, a malformed or missing tag was recorded")
    with mock.patch.object(tag_module, "_session_kv", side_effect=OSError("down")), \
            mock.patch.dict(os.environ, {env: "1"}):
        try:
            asyncio.run(tag_module.record_request(_Request({header: TAG}), SESSION))
        except Exception as exc:  # noqa: BLE001
            fail(f"a failing Session KV raised out of record_request: {exc!r}")


def main() -> int:
    path = HERMES / ROUTES_RELATIVE
    try:
        source = path.read_text()
    except OSError as exc:
        fail(f"{path} is unreadable: {exc}")
    else:
        check_call_sites(source)
        check_import()
        check_behaviour()
    for failure in FAILURES:
        print(f"verify_eval_run_tag: FAIL: {failure}", file=sys.stderr)
    if FAILURES:
        return 1
    print("verify_eval_run_tag: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
