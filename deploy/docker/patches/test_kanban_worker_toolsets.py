"""Host tests for kanban_worker_toolsets and its applier (#3937)."""

import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import kanban_worker_toolsets as kwt
from apply_kanban_worker_toolsets import BODY_ANCHOR, apply


def profile(root: Path, name="cluster-a") -> Path:
    home = root / name
    (home / "plugins" / "hermes_otel").mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (home / "plugins" / "hermes_otel" / "plugin.yaml").write_text("name: hermes_otel\n", encoding="utf-8")
    return home


def bump(path: Path, text: str) -> None:
    # A later mtime even on a filesystem with coarse timestamps.
    path.write_text(text, encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        kwt._cache.clear()
        self.root = Path(tempfile.mkdtemp())
        self.home = profile(self.root)
        self.managed = self.root / "managed"
        self.managed.mkdir()
        (self.managed / "config.yaml").write_text("x: 1\n", encoding="utf-8")
        patcher = mock.patch.object(kwt, "_managed_dir", return_value=self.managed)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.answers = []
        self.calls = []

        def child(hermes_home):
            self.calls.append(hermes_home)
            return self.answers.pop(0) if self.answers else (True, ["terminal", "kanban"])
        patcher = mock.patch.object(kwt, "_resolve_in_child", side_effect=child)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_unchanged_profile_is_resolved_once(self):
        first = kwt.resolve_worker_toolsets(str(self.home))
        second = kwt.resolve_worker_toolsets(str(self.home))
        self.assertEqual(["terminal", "kanban"], first)
        self.assertEqual(first, second)
        self.assertEqual(1, len(self.calls))

    def test_each_input_change_recomputes(self):
        kwt.resolve_worker_toolsets(str(self.home))
        changes = [
            lambda: bump(self.home / "config.yaml", "model: {provider: x}\n"),
            lambda: bump(self.home / ".env", "HASS_TOKEN=t\n"),
            lambda: bump(self.home / "plugins" / "hermes_otel" / "plugin.yaml", "name: hermes_otel\nv: 2\n"),
            lambda: (self.home / "plugins" / "new_plugin").mkdir() or bump(
                self.home / "plugins" / "new_plugin" / "plugin.yaml", "name: new\n"),
            lambda: bump(self.managed / "config.yaml", "x: 2\n"),
            lambda: (self.home / "plugins" / "new_plugin" / "plugin.yaml").unlink(),
        ]
        for n, change in enumerate(changes, start=2):
            with self.subTest(change=n - 1):
                change()
                kwt.resolve_worker_toolsets(str(self.home))
                self.assertEqual(n, len(self.calls))

    def test_a_stale_entry_is_never_served_when_the_recompute_fails(self):
        self.assertEqual(["terminal", "kanban"], kwt.resolve_worker_toolsets(str(self.home)))
        bump(self.home / "config.yaml", "platform_toolsets: {cli: [terminal]}\n")
        self.answers.append((False, None))
        self.assertIsNone(kwt.resolve_worker_toolsets(str(self.home)))

    def test_a_failure_or_a_none_is_not_cached(self):
        for answer in ((False, None), (True, None)):
            with self.subTest(answer=answer):
                kwt._cache.clear()
                self.calls.clear()
                self.answers.append(answer)
                self.assertIsNone(kwt.resolve_worker_toolsets(str(self.home)))
                self.assertEqual(["terminal", "kanban"], kwt.resolve_worker_toolsets(str(self.home)))
                self.assertEqual(2, len(self.calls))

    def test_profiles_are_cached_separately(self):
        other = profile(self.root, "cluster-b")
        self.answers.extend([(True, ["a"]), (True, ["b"])])
        self.assertEqual(["a"], kwt.resolve_worker_toolsets(str(self.home)))
        self.assertEqual(["b"], kwt.resolve_worker_toolsets(str(other)))
        self.assertEqual(["a"], kwt.resolve_worker_toolsets(str(self.home)))

    def test_no_home_is_no_pin_and_no_child(self):
        self.assertIsNone(kwt.resolve_worker_toolsets(None))
        self.assertIsNone(kwt.resolve_worker_toolsets(""))
        self.assertEqual([], self.calls)

    def test_a_caller_cannot_mutate_the_cached_list(self):
        kwt.resolve_worker_toolsets(str(self.home))
        kwt.resolve_worker_toolsets(str(self.home)).append("injected")  # a cache hit
        self.assertEqual(["terminal", "kanban"], kwt.resolve_worker_toolsets(str(self.home)))


class ChildTest(unittest.TestCase):
    """_resolve_in_child against real child processes running stand-in programs."""

    def run_child(self, program: str, returncode_ok=True):
        with mock.patch.object(kwt, "_CHILD_PROGRAM", textwrap.dedent(program)):
            return kwt._resolve_in_child("/nonexistent-home")

    def test_the_answer_is_the_last_line_after_any_noise(self):
        self.assertEqual((True, ["a", "b"]), self.run_child(
            'import json; print("banner from a plugin"); json.dump({"toolsets": ["a", "b"]}, __import__("sys").stdout)'))

    def test_a_none_answer_is_ok_and_none(self):
        self.assertEqual((True, None), self.run_child('print(\'{"toolsets": null}\')'))

    def test_unreadable_or_failed_children_are_not_ok(self):
        for program in ('import sys; sys.exit(3)', 'print("not json")', 'print("{\\"toolsets\\": [1]}")',
                        'print("[]")', 'print("")'):
            with self.subTest(program=program):
                self.assertEqual((False, None), self.run_child(program))

    def test_the_child_runs_upstream_body_and_sees_the_flag(self):
        self.assertEqual((True, ["1"]), self.run_child(
            'import json, os; print(json.dumps({"toolsets": [os.environ.get("%s", "")]}))' % kwt.INPROC_ENV))

    def test_a_child_past_the_timeout_is_not_ok(self):
        # A good answer that arrives too late is still no answer.
        with mock.patch.object(kwt, "CHILD_TIMEOUT_SECONDS", 0.5):
            self.assertEqual((False, None), self.run_child(
                'import time; time.sleep(3); print(\'{"toolsets": ["late"]}\')'))

    def test_in_child_reads_the_flag(self):
        with mock.patch.dict(os.environ, {kwt.INPROC_ENV: "1"}):
            self.assertTrue(kwt.in_child())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kwt.INPROC_ENV, None)
            self.assertFalse(kwt.in_child())


UPSTREAM_STUB = (
    "def _resolve_worker_cli_toolsets(hermes_home):\n"
    '    """Upstream docstring."""\n'
    + BODY_ANCHOR +
    "            build_profile_secret_scope)\n"
    "        return ['upstream']\n"
    "    except Exception:\n"
    "        return None\n"
)


class ApplyTest(unittest.TestCase):
    def stage(self, source=UPSTREAM_STUB) -> Path:
        root = Path(tempfile.mkdtemp())
        target = root / "hermes_cli" / "kanban_db_dispatch.py"
        target.parent.mkdir(parents=True)
        target.write_text(source, encoding="utf-8")
        return target

    def test_the_hook_goes_in_ahead_of_upstreams_body_with_the_import(self):
        target = self.stage()
        apply(target.parent.parent)
        text = target.read_text(encoding="utf-8")
        hook = text.index("return _kanban_worker_toolsets.resolve_worker_toolsets(hermes_home)")
        self.assertLess(text.index("if not _kanban_worker_toolsets.in_child():"), hook)
        self.assertLess(hook, text.index("from agent.secret_scope import ("))
        self.assertIn("from tools import kanban_worker_toolsets as _kanban_worker_toolsets", text)

    def test_a_second_apply_is_refused(self):
        target = self.stage()
        apply(target.parent.parent)
        with self.assertRaises(SystemExit):
            apply(target.parent.parent)

    def test_a_moved_anchor_is_refused(self):
        target = self.stage(UPSTREAM_STUB.replace("    if not hermes_home:\n", "    if hermes_home is None:\n"))
        with self.assertRaises(SystemExit):
            apply(target.parent.parent)


if __name__ == "__main__":
    unittest.main()
