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
import json
import os
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


def stub_redact(text: str, **_kwargs) -> str:
    """A stand-in for redact_sensitive_text: masks the token shape, nothing else; ``force`` is accepted."""
    return TOKEN_RE.sub(lambda m: m.group(0)[:6] + "..." + m.group(0)[-4:], text)


# A shape Hermes' prefix list masks and the shared module does not (``hf_``), built by
# concatenation so no token-shaped literal sits in the file.
HERMES_ONLY_SHAPE = "hf_" + "AbCdEfGhIj" * 3


def opt_out_aware_redact(text: str, *, force: bool = False, **_kwargs) -> str:
    """A stand-in that honours the profile opt-out unless forced, like upstream."""
    return text.replace(HERMES_ONLY_SHAPE, "xoxb-...ghij") if force else text


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

RULES_YAML = """\
ip:
  action: pseudonym
  allowCidrs: [127.0.0.0/8]
rules:
  - name: cluster-name
    literal: prod-eu-1
    action: mask
  - name: project
    pattern: "acme-[a-z]+-prod"
    action: pseudonym
"""


class OperatorRulesTest(unittest.TestCase):
    """The operator's rule file, applied after the upstream redactor (issue #1985)."""

    def setUp(self):
        cr.reset_operator_rules()
        self.addCleanup(cr.reset_operator_rules)
        self.tmp = Path(tempfile.mkdtemp())
        self.rule_file = self.tmp / "redaction.yaml"
        self.rule_file.write_text(RULES_YAML)
        self.env = {
            cr.RULE_FILE_ENV: str(self.rule_file),
            cr.REDACTOR_MODULE_ENV: str(GATEWAY_REDACTOR),
        }

    def switched_off(self):
        env = {k: v for k, v in os.environ.items() if k != cr.RULE_FILE_ENV}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_without_the_variable_nothing_changes(self):
        with self.switched_off():
            self.assertFalse(cr.disk_redaction_enabled())
            self.assertIs(cr.install_operator_rules(stub_redact), stub_redact)
            self.assertEqual(cr.apply_operator_rules("prod-eu-1 at 10.1.2.3"), "prod-eu-1 at 10.1.2.3")

    def test_the_projected_file_at_the_fixed_path_is_the_switch(self):
        with self.switched_off(), mock.patch.object(cr, "DEFAULT_RULE_FILE", str(self.rule_file)):
            with mock.patch.dict(os.environ, {cr.REDACTOR_MODULE_ENV: str(GATEWAY_REDACTOR)}):
                self.assertTrue(cr.disk_redaction_enabled())
                self.assertEqual(cr.rule_file_path(), str(self.rule_file))
                redact = cr.install_operator_rules(stub_redact)
                self.assertIn("[REDACTED_CLUSTER_NAME]", redact("prod-eu-1"))

    def test_the_variable_overrides_the_fixed_path(self):
        other = self.tmp / "other.yaml"
        other.write_text("rules:\n  - name: other\n    literal: other-name\n")
        with mock.patch.dict(os.environ, {**self.env, cr.RULE_FILE_ENV: str(other)}):
            with mock.patch.object(cr, "DEFAULT_RULE_FILE", str(self.rule_file)):
                self.assertEqual(cr.rule_file_path(), str(other))

    def test_the_path_is_resolved_once(self):
        with self.switched_off(), mock.patch.object(cr, "DEFAULT_RULE_FILE", str(self.tmp / "absent.yaml")):
            self.assertFalse(cr.disk_redaction_enabled())
            (self.tmp / "absent.yaml").write_text(RULES_YAML)
            self.assertFalse(cr.disk_redaction_enabled())

    def test_the_rules_apply_after_the_upstream_redactor(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(cr.disk_redaction_enabled())
            redact = cr.install_operator_rules(stub_redact)
            out = redact(
                f"deploy {TOKEN} to prod-eu-1 (acme-web-prod) at 10.1.2.3 and 127.0.0.1"
            )
        self.assertNotIn(TOKEN, out)
        self.assertIn("[REDACTED_CLUSTER_NAME]", out)
        self.assertNotIn("acme-web-prod", out)
        self.assertRegex(out, r"\[project:[0-9a-f]{12}\]")
        self.assertNotIn("10.1.2.3", out)
        self.assertIn("127.0.0.1", out)
        self.assertTrue(getattr(redact, cr.RULES_INSTALLED_MARKER))
        self.assertIs(cr.install_operator_rules(redact), redact)

    def test_keyword_arguments_reach_the_upstream_redactor(self):
        seen = {}

        def upstream(text, *, force=False, code_file=False):
            seen.update(force=force, code_file=code_file)
            return text

        with mock.patch.dict(os.environ, self.env):
            redact = cr.install_operator_rules(upstream)
            self.assertEqual(redact("plain", force=True, code_file=True), "plain")
        self.assertEqual(seen, {"force": True, "code_file": True})

    def test_an_empty_text_passes_through(self):
        with mock.patch.dict(os.environ, self.env):
            redact = cr.install_operator_rules(stub_redact)
            self.assertEqual(redact(""), "")

    def test_a_bad_rule_raises_and_names_the_file(self):
        self.rule_file.write_text("rules:\n  - name: bad\n    literal: ''\n")
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(ValueError) as ctx:
                cr.install_operator_rules(stub_redact)
        self.assertIn(str(self.rule_file), str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith(cr.RULE_FILE_ENV + ":"))

    def test_an_error_on_the_projected_file_names_the_file_not_the_variable(self):
        self.rule_file.write_text("rules:\n  - name: bad\n    literal: ''\n")
        with self.switched_off(), mock.patch.object(cr, "DEFAULT_RULE_FILE", str(self.rule_file)):
            with mock.patch.dict(os.environ, {cr.REDACTOR_MODULE_ENV: str(GATEWAY_REDACTOR)}):
                with self.assertRaises(ValueError) as ctx:
                    cr.install_operator_rules(stub_redact)
        self.assertTrue(str(ctx.exception).startswith(cr.RULE_FILE_SOURCE_NAME + ":"))
        self.assertNotIn(cr.RULE_FILE_ENV, str(ctx.exception))

    def test_a_missing_redactor_module_raises_naming_the_path(self):
        with mock.patch.dict(os.environ, {**self.env, cr.REDACTOR_MODULE_ENV: str(self.tmp / "nowhere.py")}):
            with self.assertRaises(ValueError) as ctx:
                cr.install_operator_rules(stub_redact)
        self.assertIn("nowhere.py", str(ctx.exception))

    def test_the_default_redactor_module_is_where_the_image_puts_the_plugins(self):
        dockerfile = (REPO_ROOT / "deploy" / "docker" / "Dockerfile").read_text()
        self.assertIn("COPY --chown=hermes:hermes agents/chat/defaults/ /opt/defaults/\n", dockerfile)
        relative = Path(cr.DEFAULT_REDACTOR_MODULE).relative_to("/opt/defaults")
        self.assertTrue((REPO_ROOT / "agents" / "chat" / "defaults" / relative).is_file())
        self.assertEqual(GATEWAY_REDACTOR, REPO_ROOT / "agents" / "chat" / "defaults" / relative)

    def test_the_salt_is_resolved_before_the_composition_is_installed(self):
        env = {k: v for k, v in self.env.items()}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("SESSION_KV_SALT", None)
            module = cr._redactor_module()
            module._fallback_salt = None
            cr.install_operator_rules(stub_redact)
            self.assertIsNotNone(module._fallback_salt)

    def test_a_file_that_is_not_a_mapping_raises(self):
        self.rule_file.write_text("- a\n- b\n")
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(ValueError) as ctx:
                cr.install_operator_rules(stub_redact)
        self.assertIn("mapping", str(ctx.exception))

    def test_a_falsy_document_is_the_default_rules_as_at_the_gateway(self):
        # The shared loader reads every falsy document as the empty mapping, as
        # the gateway hook always did, so the two pods agree on the same file.
        for document in ("", "# only a comment\n", "[]\n", "0\n", "false\n"):
            cr.reset_operator_rules()
            self.rule_file.write_text(document)
            with mock.patch.dict(os.environ, self.env):
                redact = cr.install_operator_rules(stub_redact)
                self.assertNotIn("10.1.2.3", redact("at 10.1.2.3"), document)

    def test_a_missing_file_raises(self):
        self.rule_file.unlink()
        with mock.patch.dict(os.environ, self.env):
            with self.assertRaises(ValueError) as ctx:
                cr.install_operator_rules(stub_redact)
        self.assertIn(str(self.rule_file), str(ctx.exception))

    def test_the_file_is_read_once(self):
        with mock.patch.dict(os.environ, self.env):
            cr.install_operator_rules(stub_redact)
            self.rule_file.write_text("rules:\n  - name: other\n    literal: other-name\n")
            out = cr.apply_operator_rules("prod-eu-1 other-name")
        self.assertIn("[REDACTED_CLUSTER_NAME]", out)
        self.assertIn("other-name", out)


class StoreRedactionTest(unittest.TestCase):
    """What the session store and the kanban board receive when the switch is on."""

    MESSAGE = {
        "role": "tool",
        "content": [
            {"type": "text", "text": f"token {TOKEN}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "api_content": TOKEN,
        "reasoning": f"saw {TOKEN}",
        "reasoning_content": None,
        "reasoning_details": [{"type": "text", "text": TOKEN}],
        "tool_call_id": "c1",
        "tool_name": "terminal",
        "token_count": 3,
        "observed": True,
    }
    TOOL_CALLS = [
        {
            "id": "c1",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": f"curl -H 'Authorization: Bearer {TOKEN}'"}),
            },
        }
    ]

    def setUp(self):
        cr.reset_operator_rules()
        self.addCleanup(cr.reset_operator_rules)
        self.tmp = Path(tempfile.mkdtemp())
        rule_file = self.tmp / "redaction.yaml"
        rule_file.write_text(RULES_YAML)
        self.env = {cr.RULE_FILE_ENV: str(rule_file), cr.REDACTOR_MODULE_ENV: str(GATEWAY_REDACTOR)}

    def switched_off(self):
        env = {k: v for k, v in os.environ.items() if k != cr.RULE_FILE_ENV}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_a_message_and_its_tool_calls_are_redacted_in_copies(self):
        msg = json.loads(json.dumps(self.MESSAGE))
        calls = json.loads(json.dumps(self.TOOL_CALLS))
        with mock.patch.dict(os.environ, self.env):
            out_msg, out_calls = cr.redact_message_for_store(msg, calls, redact=stub_redact)
        self.assertNotIn(TOKEN, json.dumps(out_msg))
        self.assertNotIn(TOKEN, json.dumps(out_calls))
        self.assertIn(TOKEN, json.dumps(msg))
        self.assertIn(TOKEN, json.dumps(calls))
        self.assertEqual(out_msg["role"], "tool")
        self.assertEqual(out_msg["token_count"], 3)
        self.assertIs(out_msg["observed"], True)
        self.assertIsNone(out_msg["reasoning_content"])
        self.assertEqual(out_msg["content"][1], self.MESSAGE["content"][1])
        self.assertEqual(out_calls[0]["function"]["name"], "terminal")
        command = json.loads(out_calls[0]["function"]["arguments"])["command"]
        self.assertTrue(command.startswith("curl -H 'Authorization: Bearer "))
        self.assertNotIn(TOKEN, command)

    def test_tool_call_ids_names_and_types_are_structure_and_stay(self):
        rules = self.tmp / "loose.yaml"
        rules.write_text("ip:\n  action: \"off\"\nrules:\n  - name: project\n    pattern: \"[a-z][a-z0-9_-]{5,28}\"\n    action: pseudonym\n")
        calls = [{"id": "call_abcdef123456", "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "kubectl get pods -n acme-prod"})}}]
        with mock.patch.dict(os.environ, {**self.env, cr.RULE_FILE_ENV: str(rules)}):
            redact = cr.install_operator_rules(stub_redact)
            out_msg, out_calls = cr.redact_message_for_store(
                {"role": "assistant", "content": "", "tool_call_id": None}, calls, redact=redact
            )
        self.assertEqual(out_calls[0]["id"], "call_abcdef123456")
        self.assertEqual(out_calls[0]["type"], "function")
        self.assertEqual(out_calls[0]["function"]["name"], "terminal")
        arguments = json.loads(out_calls[0]["function"]["arguments"])
        self.assertNotIn("acme-prod", arguments["command"])
        # Applied once: the rules ran in the key-aware pass and not again over the
        # leaves, so every marker is whole and none nests inside another.
        self.assertRegex(arguments["command"], r"^(\[project:[0-9a-f]{12}\]|[^\[\]]+)+$")
        self.assertIn(" get pods -n [project:", arguments["command"])

    def test_arguments_are_masked_by_key_name_as_the_hook_does(self):
        calls = [{"id": "c1", "type": "function", "function": {"name": "mcp_login", "arguments": json.dumps(
            {"user": "svc-reader", "password": "hunter2-example-pw", "env": [{"name": "API_TOKEN", "value": "plain-looking-value"}]})}}]
        with mock.patch.dict(os.environ, self.env):
            redact = cr.install_operator_rules(stub_redact)
            _, out_calls = cr.redact_message_for_store({"role": "assistant"}, calls, redact=redact)
        arguments = json.loads(out_calls[0]["function"]["arguments"])
        self.assertNotIn("hunter2-example-pw", json.dumps(arguments))
        self.assertNotIn("plain-looking-value", json.dumps(arguments))
        self.assertEqual(arguments["user"], "svc-reader")
        self.assertEqual(arguments["env"][0]["name"], "API_TOKEN")

    def test_opaque_parts_stay_byte_for_byte_under_a_loose_rule(self):
        rules = self.tmp / "loose.yaml"
        rules.write_text("ip:\n  action: \"off\"\nrules:\n  - name: project\n    pattern: \"[a-z][a-z0-9_-]{5,28}\"\n    action: pseudonym\n")
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8gcHJvZC1ldS0x"}}
        signed = {"type": "reasoning.text", "text": "thinking about acme-prod", "signature": "abcdefgh12345678"}
        encrypted = {"type": "reasoning.encrypted", "data": "encryptedpayloadvalue"}
        msg = {"role": "assistant", "content": [{"type": "text", "text": "deploy acme-prod"}, image],
               "reasoning_details": [signed, encrypted]}
        with mock.patch.dict(os.environ, {**self.env, cr.RULE_FILE_ENV: str(rules)}):
            redact = cr.install_operator_rules(stub_redact)
            out_msg, _ = cr.redact_message_for_store(msg, None, redact=redact)
        self.assertNotIn("acme-prod", out_msg["content"][0]["text"])
        self.assertEqual(out_msg["content"][1], image)
        self.assertEqual(out_msg["reasoning_details"][0]["signature"], "abcdefgh12345678")
        self.assertNotIn("acme-prod", out_msg["reasoning_details"][0]["text"])
        self.assertEqual(out_msg["reasoning_details"][1], encrypted)

    def test_argument_leaves_are_redacted_forced_despite_the_profile_opt_out(self):
        calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": json.dumps(
            {"command": f"curl -H 'X-Hermes: {HERMES_ONLY_SHAPE}' https://example.invalid/"})}}]
        with mock.patch.dict(os.environ, self.env):
            module = cr._redactor_module()
            # The premise: the shared module leaves this shape alone, so only the Hermes layer can mask it.
            self.assertIn(HERMES_ONLY_SHAPE, module.AuditRedactor.redact_text(f"x {HERMES_ONLY_SHAPE} y"))
            redact = cr.install_operator_rules(opt_out_aware_redact)
            _, out_calls = cr.redact_message_for_store({"role": "assistant"}, calls, redact=redact)
        self.assertNotIn(HERMES_ONLY_SHAPE, out_calls[0]["function"]["arguments"])
        # And with the leaves unforced the shape would survive: the stub honours the opt-out.
        cr.reset_operator_rules()
        with mock.patch.dict(os.environ, self.env):
            cr.install_operator_rules(opt_out_aware_redact)
            cr._UPSTREAM_REDACTOR[:] = [opt_out_aware_redact]
            _, leaked = cr.redact_message_for_store({"role": "assistant"}, calls, redact=redact)
        self.assertIn(HERMES_ONLY_SHAPE, leaked[0]["function"]["arguments"])

    def test_responses_and_openrouter_shapes_have_their_prose_redacted(self):
        rules = self.tmp / "literal.yaml"
        rules.write_text("ip:\n  action: \"off\"\nrules:\n  - name: cluster-name\n    literal: prod-eu-1\n")
        msg = {
            "role": "assistant",
            "codex_message_items": [{"type": "message", "role": "assistant", "id": "msg_1",
                                     "content": [{"type": "output_text", "text": "deploy to prod-eu-1"}]}],
            "codex_reasoning_items": [{"type": "reasoning", "id": "rs_1", "encrypted_content": "prod-eu-1-looking-blob",
                                       "summary": [{"type": "summary_text", "text": "about prod-eu-1"}]}],
            "reasoning_details": [{"type": "reasoning.summary", "summary": "summary of prod-eu-1"},
                                  {"text": "untyped prod-eu-1"}],
        }
        with mock.patch.dict(os.environ, {**self.env, cr.RULE_FILE_ENV: str(rules)}):
            redact = cr.install_operator_rules(stub_redact)
            out, _ = cr.redact_message_for_store(msg, None, redact=redact)
        self.assertEqual(out["codex_message_items"][0]["content"][0]["text"], "deploy to [REDACTED_CLUSTER_NAME]")
        self.assertEqual(out["codex_message_items"][0]["id"], "msg_1")
        self.assertEqual(out["codex_reasoning_items"][0]["summary"][0]["text"], "about [REDACTED_CLUSTER_NAME]")
        self.assertEqual(out["codex_reasoning_items"][0]["encrypted_content"], "prod-eu-1-looking-blob")
        self.assertEqual(out["reasoning_details"][0]["summary"], "summary of [REDACTED_CLUSTER_NAME]")
        self.assertEqual(out["reasoning_details"][1]["text"], "untyped [REDACTED_CLUSTER_NAME]")
        self.assertIn("prod-eu-1", json.dumps(msg))

    def test_a_greedy_pattern_leaves_the_arguments_parseable(self):
        rules = self.tmp / "greedy.yaml"
        rules.write_text("ip:\n  action: \"off\"\nrules:\n  - name: ctx\n    pattern: \"gke_\\\\S+\"\n")
        raw = json.dumps({"command": "kubectl --context=gke_acme_us_prod get ns"})
        calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": raw}}]
        with mock.patch.dict(os.environ, {**self.env, cr.RULE_FILE_ENV: str(rules)}):
            redact = cr.install_operator_rules(stub_redact)
            _, out_calls = cr.redact_message_for_store({"role": "assistant"}, calls, redact=redact)
            _, out_text = cr.redact_message_for_store({"role": "assistant"}, json.dumps(calls), redact=redact)
        parsed = json.loads(out_calls[0]["function"]["arguments"])
        self.assertEqual(parsed["command"], "kubectl --context=[REDACTED_CTX] get ns")
        self.assertEqual(json.loads(json.loads(out_text)[0]["function"]["arguments"]), parsed)

    def test_unchanged_arguments_keep_their_original_text(self):
        raw = '{"command":   "echo hi"}'
        calls = [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": raw}}]
        with mock.patch.dict(os.environ, self.env):
            _, out_calls = cr.redact_message_for_store({"role": "assistant"}, calls, redact=stub_redact)
            _, out_text = cr.redact_message_for_store({"role": "assistant"}, json.dumps(calls), redact=stub_redact)
        self.assertIs(out_calls[0]["function"]["arguments"], raw)
        self.assertEqual(out_text, json.dumps(calls))

    def test_a_string_content_and_a_string_tool_calls_column_are_redacted(self):
        with mock.patch.dict(os.environ, self.env):
            out_msg, out_calls = cr.redact_message_for_store(
                {"role": "user", "content": TOKEN}, json.dumps(self.TOOL_CALLS), redact=stub_redact
            )
        self.assertNotIn(TOKEN, out_msg["content"])
        self.assertIsInstance(out_calls, str)
        self.assertNotIn(TOKEN, out_calls)
        self.assertEqual(json.loads(out_calls)[0]["id"], "c1")

    def test_the_operator_rules_reach_the_store_through_the_wrapped_redactor(self):
        with mock.patch.dict(os.environ, self.env):
            redact = cr.install_operator_rules(stub_redact)
            out_msg, _ = cr.redact_message_for_store(
                {"role": "assistant", "content": "prod-eu-1 is at 10.1.2.3"}, None, redact=redact
            )
        self.assertEqual(out_msg["content"].count("[REDACTED_CLUSTER_NAME]"), 1)
        self.assertNotIn("10.1.2.3", out_msg["content"])

    def test_nothing_is_touched_without_the_switch(self):
        msg, calls = dict(self.MESSAGE), list(self.TOOL_CALLS)
        with self.switched_off():
            out_msg, out_calls = cr.redact_message_for_store(msg, calls, redact=stub_redact)
            self.assertIs(out_msg, msg)
            self.assertIs(out_calls, calls)
            text = f"a {TOKEN}"
            self.assertIs(cr.redact_store_text(text, redact=stub_redact), text)

    def test_store_text_is_redacted_with_the_switch(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertNotIn(TOKEN, cr.redact_store_text(f"a {TOKEN}", redact=stub_redact))
            self.assertIsNone(cr.redact_store_text(None, redact=stub_redact))
            self.assertEqual(cr.redact_store_text(7, redact=stub_redact), 7)

    def test_the_default_redactor_is_the_forced_upstream_call(self):
        calls = []
        fake_module = mock.Mock()
        fake_module.redact_sensitive_text = lambda text, **kw: calls.append(kw) or text.replace(TOKEN, "x")
        with mock.patch.dict(sys.modules, {"agent": mock.Mock(), "agent.redact": fake_module}):
            with mock.patch.dict(os.environ, self.env):
                out = cr.redact_store_text(f"a {TOKEN}")
        self.assertEqual(out, "a x")
        self.assertEqual(calls, [{"force": True}])


UPSTREAM_STATE = '''\
import json


class SessionMessagesMixin:
    """Message rows."""

    def _message_row_params(self, session_id: str, role: str, msg, tool_calls,
        message_timestamp: float, *, keep_reasoning: bool) -> tuple:
        """Bind values for ``_INSERT_MESSAGE_SQL`` from one message dict."""
        encoded_content = msg.get("content")
        return (session_id, role, encoded_content, tool_calls, message_timestamp, keep_reasoning)

    def append_message(self, session_id, role, msg):
        return self._message_row_params(session_id, role, msg, None, 0.0, keep_reasoning=True)
'''

UPSTREAM_KANBAN = '''\
import sqlite3
from typing import Optional


def create_task(
    conn: sqlite3.Connection, *, title: str, body: Optional[str] = None,
    assignee: Optional[str] = None, priority: int = 0,
) -> str:
    """Create a task."""
    conn.execute("INSERT INTO tasks (title, body) VALUES (?, ?)", (title, body))
    return "t_1"


def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
) -> bool:
    """``running -> done``; records ``result``."""
    handoff_summary = summary if summary is not None else result
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (result, task_id))
    return handoff_summary is not None


def edit_completed_task_result(
    conn: sqlite3.Connection, task_id: str, *, result: str, summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    conn.execute("UPDATE tasks SET result = ? WHERE id = ?", (result, task_id))
    return True


def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    return conn.execute("INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)", (task_id, author, body)).lastrowid


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
) -> bool:
    """``running``/``ready`` -> ``blocked``."""
    conn.execute("UPDATE tasks SET status = 'blocked', block_reason = ? WHERE id = ?", (reason, task_id))
    return True
'''


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
            (apply_credential_redaction.STATE_RELATIVE, UPSTREAM_STATE),
            (apply_credential_redaction.KANBAN_RELATIVE, UPSTREAM_KANBAN),
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

    def test_the_operator_rules_trailer_wraps_the_redactor_outside_the_guard(self):
        root = self.write_tree()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)
        patched = self.read(root, apply_credential_redaction.REDACT_RELATIVE)
        self.assertEqual(patched.count("_kube_agents_install_operator_rules("), 1)
        self.assertIn(
            "redact_sensitive_text = _kube_agents_install_operator_rules(redact_sensitive_text)",
            patched,
        )
        # The rule-file error must reach the importer: the install call is not
        # inside the registration trailer's try block.
        tree = ast.parse(patched)
        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try)
            and "_kube_agents_install_operator_rules(" in ast.unparse(node)
        ]
        self.assertEqual(guarded, [])

    def test_the_message_binder_gets_a_prologue_before_its_first_statement(self):
        root = self.write_tree()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)
        patched = self.read(root, apply_credential_redaction.STATE_RELATIVE)
        self.assertEqual(patched.count("_kube_agents_redact_message_for_store("), 1)
        self.assertIn(
            '        """Bind values for ``_INSERT_MESSAGE_SQL`` from one message dict."""\n'
            "        # kube-agents patch:",
            patched,
        )
        self.assertIn(
            "        msg, tool_calls = _kube_agents_redact_message_for_store(msg, tool_calls)\n"
            '        encoded_content = msg.get("content")',
            patched,
        )

    def test_each_kanban_writer_rebinds_its_text_parameters(self):
        root = self.write_tree()
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            apply_credential_redaction.apply(root)
        patched = self.read(root, apply_credential_redaction.KANBAN_RELATIVE)
        self.assertEqual(patched.count("_kube_agents_redact_store_text("), 8)
        self.assertIn("    title = _kube_agents_redact_store_text(title)\n", patched)
        self.assertEqual(patched.count("    body = _kube_agents_redact_store_text(body)\n"), 2)
        self.assertEqual(patched.count("    result = _kube_agents_redact_store_text(result)\n"), 2)
        self.assertEqual(patched.count("    summary = _kube_agents_redact_store_text(summary)\n"), 2)
        self.assertIn("    reason = _kube_agents_redact_store_text(reason)\n", patched)
        # add_comment has no docstring: the prologue lands ahead of its first statement.
        self.assertIn("    body = _kube_agents_redact_store_text(body)\n    if not body or not body.strip():", patched)
        self.assertEqual(patched.count("from agent.credential_redaction import"), 5)

    def test_a_renamed_binder_parameter_fails_before_writing(self):
        root = self.write_tree()
        target = root / apply_credential_redaction.STATE_RELATIVE
        target.write_text(UPSTREAM_STATE.replace("msg, tool_calls,", "msg, calls,"))
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                apply_credential_redaction.apply(root)
        self.assertIn("tool_calls", str(ctx.exception))
        self.assertNotIn("_kube_agents", target.read_text())

    def test_a_kanban_writer_without_its_parameter_fails_before_writing(self):
        root = self.write_tree()
        target = root / apply_credential_redaction.KANBAN_RELATIVE
        target.write_text(
            UPSTREAM_KANBAN.replace("summary: Optional[str] = None, metadata", "metadata", 1)
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                apply_credential_redaction.apply(root)
        self.assertIn("complete_task", str(ctx.exception))
        self.assertIn("summary", str(ctx.exception))
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
