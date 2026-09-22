"""Unit tests for credential_redaction.py and apply_credential_redaction.py.

Nothing here imports Hermes: the module defers every ``agent.redact`` import to
call time, and the tests hand it a stand-in redactor. What the real engine does
with the registered pattern is ``verify_credential_redaction.py``'s job, inside
the image.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

from __future__ import annotations

import ast
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import apply_credential_redaction
import credential_redaction as cr

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_REDACTOR = REPO_ROOT / "agents" / "chat" / "defaults" / "plugins" / "common" / "redactor.py"

TOKEN = "ya29." + "A" * 40
TOKEN_RE = re.compile(cr.GCP_OAUTH_TOKEN_PATTERN)


def stub_redact(text: str) -> str:
    """A stand-in for redact_sensitive_text: masks the token shape, nothing else."""
    return TOKEN_RE.sub(lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)


class FakeStream(io.StringIO):
    def __init__(self, tty: bool = False) -> None:
        super().__init__()
        self.tty = tty
        self.flushes = 0

    def isatty(self) -> bool:
        return self.tty

    def fileno(self) -> int:
        return 42

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class PatternTest(unittest.TestCase):
    def test_the_pattern_is_the_gateway_redactors(self):
        spec = importlib.util.spec_from_file_location("gateway_redactor", GATEWAY_REDACTOR)
        module = importlib.util.module_from_spec(spec)
        # Its frozen dataclasses resolve annotations through sys.modules.
        with mock.patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
        self.assertEqual(
            cr.GCP_OAUTH_TOKEN_PATTERN, module.AuditRedactor.GCP_OAUTH_TOKEN_PATTERN.pattern
        )

    def test_the_pattern_starts_with_a_literal_prefix(self):
        # Upstream's registry refuses a pattern whose leading literal is under
        # two characters; ``ya29`` is the literal before the first escape.
        self.assertTrue(cr.GCP_OAUTH_TOKEN_PATTERN.startswith("ya29"))
        self.assertTrue(TOKEN_RE.fullmatch(TOKEN))
        self.assertIsNone(TOKEN_RE.search("ya29.tooshort"))


class RedactToolArgumentTest(unittest.TestCase):
    def test_a_terminal_command_is_redacted_in_a_copy(self):
        args = {"command": f"curl -H 'Authorization: Bearer {TOKEN}'", "timeout": 5}
        out = cr.redact_tool_argument_for_display("terminal", args, stub_redact)
        self.assertNotIn(TOKEN, out["command"])
        self.assertEqual(out["timeout"], 5)
        self.assertIn(TOKEN, args["command"], "the caller's dict must not be mutated")

    def test_execute_code_source_is_redacted(self):
        out = cr.redact_tool_argument_for_display("execute_code", {"code": f"t='{TOKEN}'"}, stub_redact)
        self.assertNotIn(TOKEN, out["code"])

    def test_other_tools_and_shapes_pass_through_untouched(self):
        for tool, args in (
            ("web_search", {"query": TOKEN}),
            ("terminal", {"command": ["not", "a", "string"]}),
            ("terminal", {"cwd": "/tmp"}),
            ("terminal", None),
        ):
            with self.subTest(tool=tool, args=args):
                out = cr.redact_tool_argument_for_display(tool, args, stub_redact)
                self.assertIs(out, args)


class RedactingTextStreamTest(unittest.TestCase):
    def setUp(self):
        self.inner = FakeStream()
        self.stream = cr.RedactingTextStream(self.inner, stub_redact)

    def test_a_token_split_across_two_writes_is_masked(self):
        self.stream.write("export TOKEN=" + TOKEN[:12])
        self.assertEqual(self.inner.getvalue(), "", "a partial line is held")
        self.stream.write(TOKEN[12:] + "\n")
        self.assertNotIn(TOKEN, self.inner.getvalue())
        self.assertEqual(self.inner.getvalue(), f"export TOKEN={TOKEN[:6]}...{TOKEN[-4:]}\n")

    def test_lines_without_a_secret_pass_byte_for_byte(self):
        text = "┊ 💻 $  kubectl get pods\nBearer as a plain word\n\n  trailing  \n"
        self.stream.write(text)
        self.assertEqual(self.inner.getvalue(), text)

    def test_several_lines_in_one_write_are_each_redacted(self):
        self.stream.write(f"a {TOKEN}\nb {TOKEN}\nc")
        self.assertEqual(self.inner.getvalue().count("..."), 2)
        self.assertTrue(self.inner.getvalue().endswith("\n"))

    def test_flush_carries_the_partial_line_and_flushes_inner(self):
        self.stream.write("last words " + TOKEN)
        self.stream.flush()
        self.assertEqual(self.inner.getvalue(), f"last words {TOKEN[:6]}...{TOKEN[-4:]}")
        self.assertEqual(self.inner.flushes, 1)
        self.stream.flush()
        self.assertEqual(self.inner.getvalue(), f"last words {TOKEN[:6]}...{TOKEN[-4:]}")

    def test_an_overlong_partial_line_is_written_rather_than_held(self):
        self.stream.write("x" * (cr.MAX_PENDING_CHARS + 1))
        self.assertEqual(len(self.inner.getvalue()), cr.MAX_PENDING_CHARS + 1)

    def test_writelines_and_the_return_value(self):
        self.assertEqual(self.stream.write("abc\n"), 4)
        self.stream.writelines(["one\n", "two\n"])
        self.assertEqual(self.inner.getvalue(), "abc\none\ntwo\n")

    def test_non_string_writes_are_coerced(self):
        self.stream.write(42)
        self.stream.write("\n")
        self.assertEqual(self.inner.getvalue(), "42\n")

    def test_stream_protocol_is_delegated(self):
        self.assertEqual(self.stream.fileno(), 42)
        self.assertFalse(self.stream.isatty())
        self.assertEqual(self.stream.encoding, self.inner.encoding)
        self.assertEqual(self.stream.errors, self.inner.errors)
        self.assertIs(self.stream.closed, False)
        self.assertTrue(getattr(self.stream, cr.WRAPPED_MARKER))
        with self.assertRaises(AttributeError):
            getattr(self.stream, "_no_such_private_attribute")

    def test_bytes_through_buffer_share_the_line_buffer(self):
        # prompt_toolkit writes the completion line through stdout.buffer.
        self.assertTrue(hasattr(self.stream, "buffer"))
        self.stream.write("text-first ")
        written = self.stream.buffer.write(("bytes " + TOKEN + "\n").encode("utf-8"))
        self.assertEqual(written, len("bytes " + TOKEN + "\n"))
        self.assertEqual(self.inner.getvalue(), f"text-first bytes {TOKEN[:6]}...{TOKEN[-4:]}\n")
        self.stream.buffer.write(b"\xff partial")
        self.stream.buffer.flush()
        self.assertTrue(self.inner.getvalue().endswith("\ufffd partial"))
        self.assertEqual(self.inner.flushes, 1)

    def test_close_drains_then_closes(self):
        self.stream.write("tail")
        self.stream.close()
        self.assertTrue(self.inner.closed)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.out = FakeStream()
        self.err = FakeStream()
        patcher_out = mock.patch.object(sys, "stdout", self.out)
        patcher_err = mock.patch.object(sys, "stderr", self.err)
        patcher_out.start()
        patcher_err.start()
        self.addCleanup(patcher_out.stop)
        self.addCleanup(patcher_err.stop)

    def install(self, environ):
        return cr.install_worker_transcript_redaction(environ=environ, redact=stub_redact)

    def test_a_worker_gets_both_streams_wrapped(self):
        self.assertTrue(self.install({cr.WORKER_TASK_ENV: "t_123"}))
        self.assertIsInstance(sys.stdout, cr.RedactingTextStream)
        self.assertIsInstance(sys.stderr, cr.RedactingTextStream)
        print("hello " + TOKEN)
        print("oops " + TOKEN, file=sys.stderr)
        self.assertNotIn(TOKEN, self.out.getvalue())
        self.assertNotIn(TOKEN, self.err.getvalue())
        self.assertIn("hello ya29.A", self.out.getvalue())

    def test_no_op_without_the_task_variable(self):
        self.assertFalse(self.install({}))
        self.assertFalse(self.install({cr.WORKER_TASK_ENV: ""}))
        self.assertIs(sys.stdout, self.out)
        self.assertIs(sys.stderr, self.err)

    def test_no_op_on_a_terminal(self):
        self.out.tty = True
        self.assertFalse(self.install({cr.WORKER_TASK_ENV: "t_123"}))
        self.assertIs(sys.stdout, self.out)
        self.assertIs(sys.stderr, self.err)

    def test_a_second_call_does_not_stack_a_wrapper(self):
        env = {cr.WORKER_TASK_ENV: "t_123"}
        self.assertTrue(self.install(env))
        first_out, first_err = sys.stdout, sys.stderr
        self.assertFalse(self.install(env))
        self.assertIs(sys.stdout, first_out)
        self.assertIs(sys.stderr, first_err)

    def test_a_tty_stderr_is_left_alone(self):
        self.err.tty = True
        self.assertTrue(self.install({cr.WORKER_TASK_ENV: "t_123"}))
        self.assertIsInstance(sys.stdout, cr.RedactingTextStream)
        self.assertIs(sys.stderr, self.err)

    def test_install_or_report_writes_a_failure_to_stderr(self):
        with mock.patch.object(cr, "install_worker_transcript_redaction", side_effect=RuntimeError("boom")):
            self.assertFalse(cr.install_or_report())
        self.assertIn(cr.INSTALL_FAILURE_PREFIX, self.err.getvalue())
        self.assertIn("boom", self.err.getvalue())

    def test_the_default_redactor_is_the_forced_upstream_call(self):
        calls = []

        def fake_redact_sensitive_text(text, **kwargs):
            calls.append(kwargs)
            return text.replace(TOKEN, "<masked>")

        fake_module = mock.Mock(redact_sensitive_text=fake_redact_sensitive_text)
        with mock.patch.dict(sys.modules, {"agent": mock.Mock(), "agent.redact": fake_module}):
            self.assertTrue(cr.install_worker_transcript_redaction(environ={cr.WORKER_TASK_ENV: "t_1"}))
            print(TOKEN)
        self.assertEqual(self.out.getvalue(), "<masked>\n")
        self.assertEqual(calls, [{"force": True}])


#: The three upstream sites, reduced to the text the applier anchors on.
UPSTREAM_REDACT = '''\
import logging

logger = logging.getLogger(__name__)

_PREFIX_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{10,}",
]


def redact_sensitive_text(text, *, force=False):
    return text


def register_redaction_patterns(patterns, source: str = "plugin") -> int:
    return len(list(patterns))
'''

UPSTREAM_DISPLAY = '''\
import logging

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)


def redact_tool_args_for_display(tool_name: str, args: dict | None) -> dict | None:
    """Return a copy of tool args safe for logs/progress UI."""
    if not isinstance(args, dict):
        return args
    if tool_name == "browser_type" and isinstance(args.get("text"), str):
        return {**args, "text": redact_sensitive_text(args["text"], force=True)}
    return args


def _delegate_task_goal_parts(tasks):
    return 0, []
'''

UPSTREAM_MAIN = '''\
import os
import sys


def _set_process_title():
    pass


def main():
    """Main entry point for hermes CLI."""
    # Cosmetic: make the process show up as 'hermes' instead of 'python3.11'
    _set_process_title()
    if _try_fast_chat_launch():
        return
    return 0
'''


class ApplierTest(unittest.TestCase):
    def write_tree(self) -> Path:
        root = Path(tempfile.mkdtemp())
        for relative, source in (
            (apply_credential_redaction.REDACT_RELATIVE, UPSTREAM_REDACT),
            (apply_credential_redaction.DISPLAY_RELATIVE, UPSTREAM_DISPLAY),
            (apply_credential_redaction.MAIN_RELATIVE, UPSTREAM_MAIN),
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        return root

    def read(self, root: Path, relative: str) -> str:
        return (root / relative).read_text()

    def test_each_site_is_patched_once_and_parses(self):
        root = self.write_tree()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)

        redact = self.read(root, apply_credential_redaction.REDACT_RELATIVE)
        display = self.read(root, apply_credential_redaction.DISPLAY_RELATIVE)
        main = self.read(root, apply_credential_redaction.MAIN_RELATIVE)
        for source in (redact, display, main):
            ast.parse(source)

        self.assertTrue(redact.startswith(UPSTREAM_REDACT), "the trailer is appended, not spliced")
        self.assertEqual(redact.count("_kube_agents_register_patterns()"), 1)

        self.assertEqual(display.count("_kube_agents_redact_tool_argument"), 2)
        self.assertIn('return {**args, "text": redact_sensitive_text(args["text"], force=True)}', display)
        self.assertNotIn('force=True)}\n    return args\n', display)

        # The prologue is main()'s first statement, after the docstring and
        # ahead of the comment that introduces upstream's first statement.
        main_def = [n for n in ast.parse(main).body if isinstance(n, ast.FunctionDef) and n.name == "main"][0]
        self.assertIsInstance(main_def.body[1], ast.Try)
        self.assertIn("install_or_report", ast.unparse(main_def.body[1]))
        self.assertIn(
            '    """Main entry point for hermes CLI."""\n    # kube-agents patch:',
            main,
        )
        self.assertIn(
            "        _kube_agents_install_transcript_redaction()\n\n    # Cosmetic:",
            main,
        )

    def test_the_patch_is_not_applied_twice(self):
        root = self.write_tree()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)
            with self.assertRaises(SystemExit) as ctx:
                apply_credential_redaction.apply(root)
        self.assertIn("already patched", str(ctx.exception))

    def test_a_missing_registry_function_fails_before_writing(self):
        root = self.write_tree()
        target = root / apply_credential_redaction.REDACT_RELATIVE
        target.write_text(UPSTREAM_REDACT.replace("def register_redaction_patterns", "def register_patterns"))
        with self.assertRaises(SystemExit) as ctx:
            apply_credential_redaction.apply(root)
        self.assertIn("pattern registry", str(ctx.exception))
        self.assertNotIn("_kube_agents", target.read_text())

    def test_a_moved_display_branch_fails_loudly(self):
        root = self.write_tree()
        target = root / apply_credential_redaction.DISPLAY_RELATIVE
        target.write_text(UPSTREAM_DISPLAY.replace("force=True", "force=False"))
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                apply_credential_redaction.apply(root)
        self.assertIn("tool-args display branch", str(ctx.exception))
        self.assertNotIn("_kube_agents", target.read_text())

    def test_the_prologue_indent_follows_the_body(self):
        root = self.write_tree()
        target = root / apply_credential_redaction.MAIN_RELATIVE
        target.write_text(UPSTREAM_MAIN.replace("    ", "  "))
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)
        patched = target.read_text()
        ast.parse(patched)
        self.assertIn('\n  """Main entry point for hermes CLI."""\n  # kube-agents patch:', patched)
        self.assertIn("\n  try:\n      from agent.credential_redaction import (", patched)


if __name__ == "__main__":
    unittest.main()
