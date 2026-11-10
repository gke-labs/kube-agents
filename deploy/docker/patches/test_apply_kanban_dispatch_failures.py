"""Unit tests for apply_kanban_dispatch_failures.py against a miniature dispatcher.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The shipped tree is checked at image build by the grep lines and
verify_kanban_dispatch_failures.py beside the applier in deploy/docker/Dockerfile;
these tests cover the applier's contract (one call at each of the two sites,
one import, and a refusal when an anchor is missing, repeated or already
patched), what the patched tick records for a good, a failed and a corrupt-board
tick, and the helper module with and without the plugin in reach.
"""

import dataclasses
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apply_kanban_dispatch_failures as applier

# gateway/kanban_watchers_dispatcher.py at v2026.9.14, cut to the imports and
# tick_once_for_board.
DISPATCHER_STUB = '''"""Embedded kanban dispatcher."""

from __future__ import annotations

import contextlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch


class _Dispatcher:
    def __init__(self, settings):
        self.settings = settings
        self.disabled_corrupt_boards = {}

    def is_corrupt_board_db_error(self, exc):
        return "malformed" in str(exc)

    def tick_once_for_board(self, slug: str) -> Optional[object]:
        conn = None
        fingerprint = ("kanban.db", None, None)
        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}
        try:
            conn = _kbc().connect(board=slug)
            return _kbd().dispatch_once(conn, board=slug, **kwargs)
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error("kanban dispatcher: board %s database %s is not a valid SQLite database", slug, fingerprint[0])
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
'''


@dataclasses.dataclass
class _Settings:
    interval: float = 5.0


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dispatcher = _write(self.root, applier.DISPATCHER, DISPATCHER_STUB)

    def test_both_sites_record_and_the_file_imports_the_helper_once(self):
        with mock.patch("sys.stdout"):
            applier.apply(self.root)
        source = self.dispatcher.read_text(encoding="utf-8")
        self.assertEqual(1, source.count("from gateway import kanban_dispatch_failures as _kage_dispatch_failures"))
        self.assertEqual(1, source.count("_kage_dispatch_failures.record_tick(slug)  #"))
        self.assertEqual(1, source.count("_kage_dispatch_failures.record_tick(slug, exc)"))
        # The failure is recorded after the corrupt-board branch has returned.
        self.assertLess(source.index("is_corrupt_board_db_error(exc)"), source.index("record_tick(slug, exc)"))
        compile(source, str(self.dispatcher), "exec")

    def test_a_patched_tree_is_refused(self):
        with mock.patch("sys.stdout"):
            applier.apply(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_a_moved_anchor_fails_the_build(self):
        _write(self.root, applier.DISPATCHER, DISPATCHER_STUB.replace("tick failed on board", "tick failed for board"))
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_a_repeated_anchor_fails_the_build(self):
        _write(self.root, applier.DISPATCHER, DISPATCHER_STUB + DISPATCHER_STUB.split("class _Dispatcher", 1)[1].join(["class _Other", ""]))
        with self.assertRaises(SystemExit):
            applier.apply(self.root)


class PatchedTickTest(unittest.TestCase):
    """The patched tick_once_for_board records a good tick, a failed tick with
    its class, and nothing for the corrupt-board branch."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = _write(root, applier.DISPATCHER, DISPATCHER_STUB)
        with mock.patch("sys.stdout"):
            applier.apply(root)
        self.recorded = []
        self.outcome = None
        helper = _module("gateway.kanban_dispatch_failures", record_tick=lambda board, exc=None: self.recorded.append((board, type(exc).__name__ if exc else None)))
        common = _module("gateway.kanban_watchers_common", _board_slugs=None, _positive_int_setting=None, logger=logging.getLogger("dispatcher-under-test"))
        gateway = _module("gateway", kanban_dispatch_failures=helper, kanban_watchers_common=common)
        conn = SimpleNamespace(close=lambda: None)
        connect = _module("hermes_cli.kanban_db_connect", connect=lambda board: conn)
        dispatch = _module("hermes_cli.kanban_db_dispatch", dispatch_once=self._dispatch_once)
        hermes_cli = _module("hermes_cli", kanban_db_connect=connect, kanban_db_dispatch=dispatch)
        modules = {
            "gateway": gateway,
            "gateway.kanban_dispatch_failures": helper,
            "gateway.kanban_watchers_common": common,
            "hermes_cli": hermes_cli,
            "hermes_cli.kanban_db_connect": connect,
            "hermes_cli.kanban_db_dispatch": dispatch,
        }
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = importlib.util.spec_from_file_location("dispatcher_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.dispatcher = module._Dispatcher(_Settings())

    def _dispatch_once(self, conn, board):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def test_a_good_tick_is_recorded_and_its_result_returned(self):
        self.outcome = "spawned"
        self.assertEqual("spawned", self.dispatcher.tick_once_for_board("default"))
        self.assertEqual([("default", None)], self.recorded)

    def test_a_failed_tick_is_recorded_with_its_class(self):
        self.outcome = OSError("disk I/O error")
        with self.assertLogs("dispatcher-under-test", level=logging.ERROR):
            self.assertIsNone(self.dispatcher.tick_once_for_board("default"))
        self.assertEqual([("default", "OSError")], self.recorded)

    def test_the_corrupt_board_branch_records_nothing(self):
        self.outcome = ValueError("database disk image is malformed")
        with self.assertLogs("dispatcher-under-test", level=logging.ERROR):
            self.assertIsNone(self.dispatcher.tick_once_for_board("default"))
        self.assertEqual([], self.recorded)


class HelperTest(unittest.TestCase):
    """gateway/kanban_dispatch_failures.py: records on the registry
    chat_turn_metrics finds, records nothing and never raises without one."""

    def setUp(self):
        here = Path(__file__).parent
        spec = importlib.util.spec_from_file_location("chat_turn_metrics_for_dispatch", here / "chat_turn_metrics.py")
        self.lookup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lookup)
        gateway = _module("gateway", chat_turn_metrics=self.lookup)
        with mock.patch.dict(sys.modules, {"gateway": gateway, "gateway.chat_turn_metrics": self.lookup}):
            spec = importlib.util.spec_from_file_location("kanban_dispatch_failures_under_test", here / "kanban_dispatch_failures.py")
            self.helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.helper)

    def _with_registry(self, registry):
        metrics = SimpleNamespace(REGISTRY=registry)
        return mock.patch.dict(sys.modules, {"chat_metrics": SimpleNamespace(metrics=metrics), "chat_metrics.metrics": metrics})

    def test_a_failed_and_a_good_tick_reach_the_registry(self):
        registry = mock.Mock()
        with self._with_registry(registry):
            self.helper.record_tick("default", OSError("disk I/O error"))
            self.helper.record_tick("default")
        self.assertEqual([mock.call("default", "OSError"), mock.call("default", "")], registry.record_dispatch_tick.call_args_list)

    def test_a_gateway_without_the_plugin_records_nothing(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with (
            mock.patch.object(self.lookup, "DEFAULT_PLUGINS_DIR", temp.name),
            mock.patch.dict(sys.modules, {"chat_metrics": None}),
            self.assertLogs(self.lookup.LOGGER, level=logging.INFO),
        ):
            self.helper.record_tick("default", OSError())
        self.assertIsNone(self.lookup._registry)

    def test_a_registry_that_raises_does_not_fail_the_tick(self):
        registry = mock.Mock()
        registry.record_dispatch_tick.side_effect = RuntimeError("boom")
        with self._with_registry(registry):
            self.helper.record_tick("default", OSError())


if __name__ == "__main__":
    unittest.main()
